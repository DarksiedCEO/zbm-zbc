"""
Stripe incoming: ZBM client payments through ZBM's own Stripe account (founder M6/M9/M10/M11; ADR 0009 amendment
"Stripe incoming", Oct 5 2026).

What it does:
- ``POST /fin/v1/invoices/{id}/stripe-checkout`` makes a Stripe-hosted Checkout page for an ISSUED ZBM invoice.
  ACH (``us_bank_account``) is offered whenever the invoice allows ACH; a card only when the invoice allows card AND
  the card policy passes now (Revenue Recovery only, at most FIN_CARD_MAX_INVOICE, FIN_CARD_PREPAYMENTS=1; FIN-31).
  ZBC never gets a Stripe checkout: client deposits sitting in a Stripe balance is the custody question FIN-CQ-02,
  and ZBC has its own EIN and bank account (it would need its own Stripe account).
- ``POST /fin/v1/stripe/events`` takes a Stripe webhook, forwarded by the gateway (caller ``rail_gateway``) with the
  RAW body and the ``Stripe-Signature`` header. The signature is checked, then the event is used ONLY as a signal:
  the object it names is read back from Stripe and that answer is what gets booked. Events are deduplicated by id;
  order does not matter (every apply step is idempotent and keyed by the Stripe object).

Flows (ZBM only):

  F13   payment succeeded     Dr 1060 Stripe balance               Cr 1100 A/R[client]   (or Cr 2070 unapplied + break)
  F13f  Stripe's fee          Dr 5020 Processing fees              Cr 1060
  F13x  failed after success  Dr 1100 A/R[client] (or 2070)        Cr 1060   (+ the fee change, if any)
  F7    dispute withdrawn     Dr 1300 Disputed funds               Cr 1060   (+ Dr 5030 / Cr 1060 for the fee)
  F7a   dispute reinstated    Dr 1060                              Cr 1300   (+ any fee given back)
  F7l   dispute lost          Dr 1100 A/R[client] (2070 / 5030)    Cr 1300   the client owes the invoice again
  F7r   refunded at Stripe    Dr 1100 A/R[client] (2070 / 5030)    Cr 1060   (sweep X-6: a Dashboard refund; break for Andre)
  F13p  payout paid           Dr 1010 Cash - Operating             Cr 1060
  F13q  payout failed         Dr 1060                              Cr 1010

The money date for the media hold (founder M3/M11) is the day Finance first sees the payment succeeded, never
earlier than Stripe's: a late webhook only makes the hold longer.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import money as M
import reasons as R
from clock import iso
from errors import Conflict, Invalid, NotFound, Unavailable
from intelligences import i01_journal as J
from intelligences import i02_receivables as I2
from ledger import derived_id
from ports import StripeCheckout, StripeDispute, StripePayment, StripePayout, StripeSession
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid

ACTOR = I2.ACTOR
STRIPE_DEP = "DEPENDENCY_UNAVAILABLE:stripe"
CHECKOUT_TTL = timedelta(hours=23)                # Stripe allows 30 min .. 24 h; an hour of margin for clock skew
MIN_CHARGE = Decimal("0.50")                      # Stripe's USD minimum
MAX_CHARGE = Decimal("999999.99")                 # eight digits of cents
MAX_PAYLOAD = 256 * 1024
STRIPE_WAIT_S = 30                                # longest a Stripe request waits for its turn before a 503 (AEGIS N3)
MAX_JOB_CLOSES = 25                               # pages one job run closes (bounds how long it holds the turn)
# AEGIS r2 N-4: bounds for the activation sweep. A refused checkout closes at most this many of the client's pages in
# one request (the rest: the next request or the hourly job); a job run reads Compliance for at most this many clients
# (rotating, so every client with an open page is reached), outside the Stripe turn.
MAX_REQUEST_CLOSES = 25
MAX_JOB_ACTIVATION_CHECKS = 25
_SID = re.compile(r"[a-z]{2,8}_[A-Za-z0-9_]{1,120}")
# AEGIS r5-r7: how a sanctions hold leaves (31 CFR 501.603 / 501.604 / 501.806; OFAC FAQ 1196)
OFAC_BASES = ("ofac_authorization", "ofac_rejected_return")
BLOCKED_EXIT_KINDS = ("license", "unblocking_report", "compliance_release", "delisting")
def merge_ground(*grounds) -> Optional[str]:
    """AEGIS r8: the sanctions ground of a hold is sticky and "match" wins; a bare True (sanctions, ground unknown) is
    "match", the strictest."""
    gs = {("match" if g is True else g) for g in grounds if g}
    return "match" if "match" in gs or (gs and not gs <= {"place"}) else ("place" if gs else None)


def hold_ground(hold: dict) -> Optional[str]:
    """The stored ground of a hold; a sanctions hold recorded without one (before AEGIS r8) counts as "match"."""
    g = (hold or {}).get("sanctions_ground")
    if g in ("match", "place"):
        return g
    return "match" if (hold or {}).get("sanctions") else None


def act_ground(act) -> Optional[str]:
    return merge_ground(act.ground if act.ground in ("match", "place") else True) if act.sanctions else None


METHOD_LABEL = {"card": "card (Stripe)", "us_bank_account": "ACH bank debit (Stripe)"}
DONE_DISPUTE = ("won", "lost", "warning_closed", "prevented")


def parse_event(payload: str) -> dict:
    """The few fields Finance reads from a VERIFIED event: its id, type, mode and the id of the object it names.
    Nothing else of the body is kept or logged (it can carry names, emails and bank details)."""
    try:
        ev = json.loads(payload)
    except ValueError:
        raise Invalid("Stripe event body is not JSON") from None
    if not isinstance(ev, dict):
        raise Invalid("Stripe event body is not an object")
    obj = (ev.get("data") or {}).get("object") if isinstance(ev.get("data"), dict) else None
    eid, typ, live = ev.get("id"), ev.get("type"), ev.get("livemode")
    if not (isinstance(eid, str) and _SID.fullmatch(eid) and eid.startswith("evt_") and isinstance(typ, str)
            and re.fullmatch(r"[a-z_.]{3,80}", typ) and isinstance(live, bool) and isinstance(obj, dict)
            and isinstance(obj.get("id"), str) and _SID.fullmatch(obj["id"])):
        raise Invalid("Stripe event is missing its id, type, livemode or object id")
    pi = obj.get("payment_intent")
    pi = pi.get("id") if isinstance(pi, dict) else pi
    return {"event_id": eid, "type": typ, "livemode": live, "object_id": obj["id"],
            "payment_intent": pi if isinstance(pi, str) and _SID.fullmatch(pi) else None}


def event_kind(typ: str) -> Optional[str]:
    if typ.startswith("checkout.session."):
        return "session"
    if typ.startswith("payment_intent."):
        return "payment"
    if typ.startswith("charge.dispute."):
        return "dispute"
    if typ.startswith("charge."):
        return "charge"
    if typ.startswith("payout."):
        return "payout"
    return None


class StripeMixin:

    # ================================================================== checkout

    def _stripe_methods(self, inv: dict) -> tuple[tuple, list[dict]]:
        """The Stripe payment method types this invoice may be paid with now, or the reasons it cannot."""
        if inv["entity"] != "zbm":
            return (), [R.item("ENTITY_MIX", "ZBC client deposits never go through Stripe until counsel answers the "
                                             "custody question FIN-CQ-02 (ZBC also needs its own Stripe account)",
                               obligation_id="FIN-CQ-02")]
        total = M.D(inv["total"])
        if total < MIN_CHARGE or total > MAX_CHARGE:
            return (), [R.item("STRIPE_NOT_ALLOWED", f"Stripe takes 0.50 to 999999.99 per payment; this invoice is "
                                                     f"{inv['total']}")]
        methods = []
        if "ach" in inv["payment_methods"]:
            methods.append("us_bank_account")
        if "card" in inv["payment_methods"] and not I2.card_problems(
                [l["line_code"] for l in inv["lines"]], total, self.cfg.card_prepayments, self.cfg.card_max_invoice):
            methods.append("card")
        if not methods:
            return (), [R.item("STRIPE_NOT_ALLOWED", "this invoice allows neither ACH nor (under FIN-31) a card; pay it "
                                                     "by wire to the operating account")]
        return tuple(methods), []

    def _stripe_lock(self):
        """Every Stripe read-back and the booking that follows it run one at a time (AEGIS H1): an answer read from
        Stripe is applied before any other Stripe answer is read, so a stale answer can never overwrite a newer one.
        Webhook volume is small; the main service lock is NOT held while Stripe is being called."""
        lk = self.__dict__.get("_stripe_serial")
        if lk is None:
            lk = self.__dict__.setdefault("_stripe_serial", threading.Lock())
        return lk

    @contextmanager
    def _stripe_turn(self, what: str):
        """Take the Stripe turn, waiting at most STRIPE_WAIT_S (AEGIS N3): when Stripe is slow, requests answer 503
        (Stripe redelivers webhooks; a caller retries) instead of piling up on the shared worker pool."""
        lk = self._stripe_lock()
        if not lk.acquire(timeout=STRIPE_WAIT_S):
            raise Unavailable(f"Stripe work is busy ({what}); nothing was done, try again")
        try:
            yield
        finally:
            lk.release()

    def _expire_page(self, g: Gather, session_id: str) -> StripeSession:
        port = self.ports.stripe_in
        return g.call("stripe", "expire_session", (session_id,), lambda: port.expire_session(session_id),
                      StripeSession(False))

    def _close_client_pages(self, g: Gather, op_id: str, client_id: str) -> tuple[list[str], int]:
        """AEGIS F-5: Compliance answered REFUSE for this client, so no page of theirs may stay payable: every open
        page of every invoice of the client is expired at Stripe and recorded expired here. A page Stripe does not
        confirm closed is left as it is (and reported); a page already paid on stays for the webhook to book."""
        with self.lock:
            invs = {i for i, inv in self.db["invoices"].items() if inv.get("client_id") == client_id}
            open_ = sorted((dict(s) for s in self.db["stripe_sessions"].values()
                            if s["invoice_id"] in invs and s["status"] == "open"), key=lambda x: x["session_id"])
        closed, not_closed = [], []
        not_attempted = len(open_[MAX_REQUEST_CLOSES:])    # over the cap: left for the job (AEGIS r2 N-4, r3 N-13)
        for s in open_[:MAX_REQUEST_CLOSES]:
            a = self._expire_page(g, s["session_id"])
            (closed if a.available and a.found and a.status == "expired" else not_closed).append(s["session_id"])
        if closed:
            with self.lock:
                op = Op(self, op_id, ACTOR, client_id, g)
                for sid in closed:
                    op.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": "expired",
                                                    "expired_at": iso(self._now())})
                self._commit(op)
        return not_closed, not_attempted

    def _checkout_gate(self, principal: str, request_id: str, invoice_id: str) -> None:
        """ADR 0024 + AEGIS F-5: a client is paid from only while Compliance's activation is a current ALLOW. Checked
        on EVERY request, before the idempotent replay and before an open page is handed back. On a REFUSE the
        client's open pages are closed; on an outage (unknown) nothing is closed, but no page is handed back.
        Compliance is read outside the Stripe turn; the turn is taken only to close pages (AEGIS r2 N-4)."""
        with self.lock:
            inv0 = self.db["invoices"].get(invoice_id)
        if inv0 is None:
            return
        if inv0.get("held_compliance"):
            # AEGIS r6 N3: no new page on an invoice that carries a Compliance hold (release it first)
            raise Refused("no Stripe checkout for this invoice", [R.item(
                "COMPLIANCE_NOT_ACTIVATED", "a payment on this invoice is held by Compliance; no new payment is taken "
                                            "until that hold is released")])
        ga = Gather(self, f"chk-act|{principal}|{request_id}|{iso(self._now())}", ACTOR, invoice_id)
        act = self._activation(ga, inv0["client_id"])
        act_reasons = self._activation_reasons(act, full=principal == "andre")
        if not act_reasons:
            return
        if act.available:
            with self._stripe_turn("checkout: close a refused client's pages"):
                not_closed, skipped = self._close_client_pages(
                    ga, f"chk-close|{principal}|{request_id}|{iso(self._now())}", inv0["client_id"])
            if not_closed:
                act_reasons = [*act_reasons, R.item(STRIPE_DEP, f"Stripe did not confirm {len(not_closed)} open "
                                                                "page(s) of this client closed; retry")]
            if skipped:
                act_reasons = [*act_reasons, R.item(STRIPE_DEP, f"{skipped} open page(s) of this client not attempted "
                                                                f"(cap of {MAX_REQUEST_CLOSES} per request); the next "
                                                                "request or the hourly job closes them")]
        raise Refused("no Stripe checkout for this invoice", act_reasons, ledger_event_ids=ga.events)

    def stripe_checkout(self, principal: str, request_id: str, invoice_id: str) -> dict:
        key, h, ent = self._idem(principal, request_id, f"stripe-checkout/{invoice_id}", invoice_id)
        self._checkout_gate(principal, request_id, invoice_id)
        if ent:
            return ent["response"]
        resp = self._stripe_checkout_make(principal, request_id, invoice_id, key, h)
        self._checkout_recheck(principal, request_id, invoice_id, resp)
        return resp

    def _checkout_recheck(self, principal: str, request_id: str, invoice_id: str, resp: dict) -> None:
        """AEGIS r3 N-9: Compliance is read again AFTER the page is made or handed back (the gate's read and the
        page's creation are two Stripe turns apart, up to tens of seconds). Anything but a current ALLOW, an outage
        included, expires that page at once and refuses; the page is recorded expired only when Stripe confirms."""
        page = (resp or {}).get("checkout") or {}
        sid = page.get("session_id")
        if not sid or page.get("status") != "open":
            return
        with self.lock:
            inv = self.db["invoices"].get(invoice_id)
        if inv is None:
            return
        g = Gather(self, f"chk-recheck|{principal}|{request_id}|{iso(self._now())}", ACTOR, invoice_id)
        reasons = self._activation_reasons(self._activation(g, inv["client_id"]), full=principal == "andre")
        if not reasons:
            return
        with self._stripe_turn("checkout: close the page just made"):
            a = self._expire_page(g, sid)
            if a.available and a.found and a.status == "expired":
                with self.lock:
                    op = Op(self, f"chk-recheck|{principal}|{request_id}", ACTOR, invoice_id, g)
                    op.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": "expired",
                                                    "expired_at": iso(self._now())})
                    self._commit(op)
            else:
                reasons = [*reasons, R.item(STRIPE_DEP, "Stripe did not confirm the page just made is closed; the "
                                                        "hourly job retries")]
        raise Refused("no Stripe checkout for this invoice", reasons, ledger_event_ids=g.events)

    def _stripe_checkout_make(self, principal: str, request_id: str, invoice_id: str, key: tuple, h: str) -> dict:
        self.require_rules()
        with self._stripe_turn("checkout"):
            now = self._now()
            with self.lock:
                inv = self.db["invoices"].get(invoice_id)
                if inv is None:
                    raise NotFound("no such invoice")
                if inv["status"] != "issued":
                    raise Conflict(f"only an issued invoice can be paid; this one is {inv['status']}")
                methods, reasons = self._stripe_methods(inv)
                if reasons:
                    raise Refused("no Stripe checkout for this invoice", reasons)
                if not self.cfg.stripe_incoming:
                    raise Refused("Stripe is not wired", [R.item(STRIPE_DEP, "FIN_STRIPE_INCOMING is not set: no "
                                                                             "Stripe account is connected")])
                mine = [dict(s) for s in self.db["stripe_sessions"].values() if s["invoice_id"] == invoice_id]
                for s in mine:                       # one live page per invoice: hand back the open one
                    if s["status"] == "open" and s["methods"] == list(methods) and s["total"] == inv["total"] \
                            and s["expires_at"] > iso(now + timedelta(minutes=30)):
                        return {"checkout": s, "reused": True, "ledger_event_ids": [], "request_id": request_id}
                stale = [s for s in mine if s["status"] == "open"]
                attempt = len(mine)
                total = inv["total"]
            g = Gather(self, f"chk|{principal}|{request_id}", ACTOR, invoice_id)
            port = self.ports.stripe_in
            # AEGIS M2: a page whose methods or amount are no longer right is closed at Stripe BEFORE a new one is
            # made, so a client can never pay on an outdated page (a card the rule no longer allows, a second payment)
            closed: list = []
            dead: list = []

            def keep_closed() -> None:
                # AEGIS N4: pages already closed at Stripe are recorded even when this request then refuses
                if closed or dead:
                    with self.lock:
                        op0 = Op(self, f"chk|{principal}|{request_id}|closed", ACTOR, invoice_id, g)
                        for sid in closed:
                            op0.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": "expired",
                                                             "expired_at": iso(self._now())})
                        for sid in dead:
                            op0.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid],
                                                             "status": "payment_failed"})
                        self._commit(op0)
                    closed.clear()
                    dead.clear()

            for s in stale:
                a = self._expire_page(g, s["session_id"])
                if not (a.available and a.found and a.status in ("expired", "complete")):
                    keep_closed()
                    raise Refused("an earlier Stripe page for this invoice could not be closed; no new page is made",
                                  [R.item(STRIPE_DEP, "Stripe did not confirm the earlier page is closed")],
                                  ledger_event_ids=g.events)
                if a.status == "complete":
                    # AEGIS N6: a page that was paid on, but whose payment then failed, no longer blocks a new page
                    p = g.call("stripe", "payment", (a.payment_intent,), lambda pi=a.payment_intent:
                               port.payment(pi), StripePayment(False)) if a.payment_intent else StripePayment(False)
                    if p.available and p.found and p.status in ("requires_payment_method", "canceled"):
                        dead.append(s["session_id"])
                        continue
                    keep_closed()
                    if not p.available:
                        raise Refused("Stripe could not say whether the earlier page was paid", [R.item(
                            STRIPE_DEP, "the earlier page is complete and its payment could not be read")],
                            ledger_event_ids=g.events)
                    raise Conflict("the client already paid on an earlier Stripe page; Finance books it when Stripe "
                                   "confirms the payment")
                closed.append(s["session_id"])
            # AEGIS M1: every parameter sent is fixed by the key. The expiry is rounded down to the hour, so a retry
            # after a lost answer (same hour) replays Stripe's first answer instead of failing as a changed request
            expires = int((now + CHECKOUT_TTL).timestamp()) // 3600 * 3600
            label = f"Z Best Media invoice {invoice_id}"
            idem_key = rid("chk", invoice_id, attempt, total, list(methods), expires, self.cfg.stripe_success_url,
                           self.cfg.stripe_cancel_url, label)
            ans = g.call("stripe", "create_checkout", (invoice_id, total, list(methods), idem_key),
                         lambda: port.create_checkout(invoice_id, total, methods, idem_key, expires, label),
                         StripeCheckout("unavailable"))
            with self.lock:
                op = Op(self, f"chk|{principal}|{request_id}", ACTOR, invoice_id, g)
                for sid in closed:
                    op.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": "expired",
                                                    "expired_at": iso(self._now())})
                for sid in dead:
                    op.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": "payment_failed"})
                inv = self.db["invoices"][invoice_id]
                if ans.outcome != "created" or inv["status"] != "issued" or inv["total"] != total:
                    if closed or dead:
                        self._commit(op)
                    if ans.outcome != "created":
                        raise Refused("Stripe did not make the checkout page", [R.item(
                            STRIPE_DEP, f"Stripe answered {ans.outcome}: {ans.reason}"[:200])],
                            ledger_event_ids=g.events)
                    raise Conflict("the invoice changed while the checkout was being made; ask again")
                existing = self.db["stripe_sessions"].get(ans.session_id)
                if existing is not None:             # Stripe replayed the same idempotency key
                    if closed or dead:
                        self._commit(op)
                    return {"checkout": dict(existing), "reused": True, "ledger_event_ids": g.events,
                            "request_id": request_id}
                rec = {"session_id": ans.session_id, "invoice_id": invoice_id, "url": ans.url,
                       "expires_at": ans.expires_at, "methods": list(methods), "total": total, "status": "open",
                       "created_by": principal, "created_at": iso(now)}
                op.put("stripe_sessions", ans.session_id, rec)
                op.record(derived_id("chk", ans.session_id), "stripe_checkout_created", ACTOR, invoice_id,
                          {"invoice_id": invoice_id, "session_id": ans.session_id, "total": total,
                           "methods": list(methods), "closed": closed}, f"Stripe checkout made for an invoice ({total})")
                resp = self._idem_add(op, key, h, {"checkout": rec, "reused": False, "closed": closed,
                                                   "ledger_event_ids": op.events, "request_id": request_id})
                self._commit(op)
                return resp

    def job_stripe_sessions(self, prefix: str) -> dict:
        """Scheduler, hourly (it may run any number of times a day, AEGIS N2): close every open Stripe page that should
        no longer take money -- its invoice is no longer issued, its amount changed, or it offers a method the invoice
        may no longer be paid with (a card after FIN_CARD_PREPAYMENTS was turned off). At most MAX_JOB_CLOSES pages a
        run; a page that cannot be closed is tried again on the next run. A card payment that still lands on such a
        page is re-checked when it arrives (unapplied + a break, never applied).

        AEGIS F-5: also every open page of a client whose Compliance activation is now REFUSE (an OFAC hit, a service
        area change, a lapsed screening...). Compliance is asked once per client with an open page; an outage
        (unknown) closes nothing and is counted ``activation_unknown`` (the checkout itself refuses meanwhile)."""
        if not self.cfg.stripe_incoming:
            return {"closed": 0, "failed": 0, "checked": 0, "refused_clients": 0, "activation_unknown": 0,
                    "activation_checked": 0, "holds_released": 0, "holds_converted": 0, "holds_upgraded": 0}
        # AEGIS r2 N-4: Compliance is read OUTSIDE the Stripe turn, for at most MAX_JOB_ACTIVATION_CHECKS clients per
        # run, rotating through the clients with an open page (a cursor after the last one checked)
        with self.lock:
            open_by_client: dict[str, list[dict]] = {}
            for s in self.db["stripe_sessions"].values():
                if s["status"] == "open":
                    cid = (self.db["invoices"].get(s["invoice_id"]) or {}).get("client_id")
                    if cid:
                        open_by_client.setdefault(cid, []).append(dict(s))
            # AEGIS r4 R4-1: clients with a payment held because Compliance was unreachable are re-asked too
            unverified: dict[str, list[str]] = {}
            refused_holds: dict[str, list[str]] = {}       # AEGIS r6 N2: re-read for a sanctions upgrade
            for i, inv in self.db["invoices"].items():
                hk = (inv.get("held_compliance") or {}).get("kind")
                if hk == "unverified":
                    unverified.setdefault(inv["client_id"], []).append(i)
                elif hk == "refused" and inv["held_compliance"].get("sanctions_ground") != "match":
                    refused_holds.setdefault(inv["client_id"], []).append(i)
        clients = sorted(set(open_by_client) | set(unverified) | set(refused_holds))
        cursor = getattr(self, "_activation_sweep_cursor", "")
        clients = ([c for c in clients if c > cursor] + [c for c in clients if c <= cursor])[:MAX_JOB_ACTIVATION_CHECKS]
        ga = Gather(self, f"{prefix}|activation", ACTOR, "stripe-sessions")
        refused_ids: set[str] = set()
        refused_clients = unknown = 0
        settle: list[tuple[str, str, str, str, bool]] = []    # (invoice, client, "release" | "convert", why, sanctions)
        for cid in clients:
            act = self._activation(ga, cid)
            if not act.available:
                unknown += 1
            elif not act.allowed:
                refused_clients += 1
                refused_ids.update(s["session_id"] for s in open_by_client.get(cid, []))
                why = self._activation_reasons(act, full=True)[0]["message"]
                settle += [(i, cid, "convert", why, act_ground(act)) for i in unverified.get(cid, [])]
                if act.sanctions:
                    settle += [(i, cid, "upgrade", why, act_ground(act)) for i in refused_holds.get(cid, [])]
            else:
                settle += [(i, cid, "release", "", False) for i in unverified.get(cid, [])]
        released = converted = upgraded = 0
        for iid, cid, what, why, sanctions in settle:
            g_h = Gather(self, f"{prefix}|hold|{iid}", ACTOR, iid)
            ruling = None
            if what == "release":
                # AEGIS r5 R5-3: Compliance is read again for THIS hold immediately before it is released
                act = self._activation(g_h, cid)
                if not act.available:
                    continue
                if not act.allowed:
                    what, why, sanctions = "convert", self._activation_reasons(act, full=True)[0]["message"], \
                        act_ground(act)
                ruling = act.ruling_id
            with self.lock:
                hold = (self.db["invoices"].get(iid) or {}).get("held_compliance") or {}
                if what == "upgrade":
                    if hold.get("kind") != "refused" or merge_ground(hold_ground(hold), sanctions) == hold_ground(hold):
                        continue
                    op_h = Op(self, f"{prefix}|hold|{iid}", ACTOR, iid, g_h)
                    self._upgrade_to_sanctions(op_h, iid, why, ACTOR, sanctions)
                    upgraded += 1
                    self._commit(op_h)
                    continue
                if hold.get("kind") != "unverified":
                    continue        # released or changed meanwhile (Andre, another run): nothing to do (R5-4)
                if what == "release" and hold.get("sanctions"):
                    continue        # AEGIS r6 N3: a sanctions marker is released only with an OFAC document
                op_h = Op(self, f"{prefix}|hold|{iid}", ACTOR, iid, g_h)
                if what == "release":
                    self._release_compliance_hold(op_h, iid, "compliance_allow", {"ruling_id": ruling}, ACTOR)
                    released += 1
                else:
                    self._release_compliance_hold(op_h, iid, "converted_to_refused", {}, ACTOR)
                    self._open_compliance_hold(op_h, iid, "refused", why, hold["receipt_id"], hold["amount"],
                                               hold["payment_entry_id"], hold["method_label"], hold["value_date"],
                                               sanctions=merge_ground(sanctions, hold_ground(hold)))
                    converted += 1
                self._commit(op_h)
        if clients:
            self._activation_sweep_cursor = clients[-1]
        with self._stripe_turn("stripe-sessions job"):
            with self.lock:
                todo = []
                for s in self.db["stripe_sessions"].values():
                    if s["status"] not in ("open", "invoice_paid"):
                        continue
                    inv = self.db["invoices"].get(s["invoice_id"]) or {}
                    methods = self._stripe_methods(inv)[0] if inv.get("status") == "issued" else ()
                    if inv.get("status") != "issued" or inv.get("total") != s["total"] \
                            or any(m not in methods for m in s["methods"]) or s["session_id"] in refused_ids:
                        todo.append(dict(s))
                todo = sorted(todo, key=lambda s: s["created_at"])[:MAX_JOB_CLOSES]
            g = Gather(self, f"{prefix}|stripe", ACTOR, "stripe-sessions")
            results = {s["session_id"]: self._expire_page(g, s["session_id"]) for s in todo}
            with self.lock:
                op = Op(self, f"{prefix}|stripe", ACTOR, "stripe-sessions", g)
                closed = failed = 0
                for s in todo:
                    a = results[s["session_id"]]
                    cur = self.db["stripe_sessions"][s["session_id"]]
                    if a.available and a.found and a.status in ("expired", "complete"):
                        op.put("stripe_sessions", s["session_id"], {**cur, "status": a.status,
                                                                    "expired_at": iso(self._now())})
                        closed += 1
                    else:
                        failed += 1
                self._commit(op)
            return {"closed": closed, "failed": failed, "checked": len(todo), "refused_clients": refused_clients,
                    "activation_unknown": unknown, "activation_checked": len(clients), "holds_released": released,
                    "holds_converted": converted, "holds_upgraded": upgraded}

    # ================================================================== webhook events

    def stripe_event(self, principal: str, request_id: str, payload: str, signature: str) -> dict:
        if not self.cfg.stripe_incoming:
            raise Refused("Stripe is not wired", [R.item(STRIPE_DEP, "FIN_STRIPE_INCOMING is not set")])
        if len(payload.encode("utf-8")) > MAX_PAYLOAD:
            raise Invalid("Stripe event body too large")
        self.require_rules()
        port = self.ports.stripe_in
        # AEGIS L7: the signature is checked locally, before anything is written to the ledger (a junk call from the
        # gateway leaves no trace but its 409)
        try:
            ok = port.verify_event(payload, signature, int(self._now().timestamp())) is True
        except Exception:  # noqa: BLE001 - an adapter that raises has not verified anything
            ok = False
        if not ok:
            raise Refused("Stripe event signature not verified", [R.item(
                STRIPE_DEP, "the Stripe-Signature did not verify (wrong secret, stale or altered body)")])
        ev = parse_event(payload)
        with self._stripe_turn("webhook"):
            return self._stripe_event_serial(principal, request_id, ev)

    def _stripe_event_serial(self, principal: str, request_id: str, ev: dict) -> dict:
        port = self.ports.stripe_in
        with self.lock:
            if ev["event_id"] in self.db["stripe_events"]:
                return {"event_id": ev["event_id"], "status": "duplicate", "ledger_event_ids": []}
        g = Gather(self, f"sev|{principal}|{request_id}", ACTOR, ev["event_id"])
        kind = event_kind(ev["type"]) if ev["livemode"] == self.cfg.stripe_livemode else None

        def read_payment(pi: str) -> StripePayment:
            return g.call("stripe", "payment", (pi,), lambda: port.payment(pi), StripePayment(False))

        truth: dict = {}
        if kind == "session":
            truth["session"] = s = g.call("stripe", "session", (ev["object_id"],),
                                          lambda: port.session(ev["object_id"]), StripeSession(False))
            if s.available and s.found and s.payment_intent:
                truth["payment"] = read_payment(s.payment_intent)
        elif kind == "payment" or (kind == "charge" and ev["payment_intent"]):
            truth["payment"] = read_payment(ev["object_id"] if kind == "payment" else ev["payment_intent"])
        elif kind == "dispute":
            truth["dispute"] = d = g.call("stripe", "dispute", (ev["object_id"],),
                                          lambda: port.dispute(ev["object_id"]), StripeDispute(False))
            # AEGIS H2: a dispute is never applied before its payment is booked -- the payment is read and booked
            # first, in the same step, so the media buy is flagged whatever order Stripe's events arrive in
            if d.available and d.found and d.payment_intent:
                with self.lock:
                    booked = rid("rct", "stripe", d.payment_intent) in self.db["receipts"]
                if not booked:
                    truth["payment"] = read_payment(d.payment_intent)
        elif kind == "payout":
            truth["payout"] = g.call("stripe", "payout", (ev["object_id"],),
                                     lambda: port.payout(ev["object_id"]), StripePayout(False))
        if any(not a.available for a in truth.values()):
            # nothing is recorded as handled: the gateway answers Stripe non-2xx and Stripe sends the event again
            raise Unavailable("Stripe could not be read back; the event will be retried", ledger_event_ids=g.events)
        if any(a.found and a.livemode != self.cfg.stripe_livemode for a in truth.values()):
            raise Invalid("Stripe answered from the other mode (test vs live)")
        check = self._payment_activation(g, truth.get("payment"))
        with self.lock:
            if ev["event_id"] in self.db["stripe_events"]:
                return {"event_id": ev["event_id"], "status": "duplicate", "ledger_event_ids": g.events}
            to_close: list = []
            if kind == "dispute" and "payment" in truth:
                # AEGIS N1: the payment is booked and COMMITTED on its own first; a dispute step that then refuses can
                # never leave the payment's ledger events without their local record
                op_p = Op(self, f"sev|{ev['event_id']}|payment", ACTOR, ev["event_id"], g)
                op_p.compliance_check = check
                try:
                    self._stripe_payment(op_p, truth["payment"])
                except PostingRefused as exc:
                    raise InvalidReasons("the Stripe event could not be posted", exc.reasons) from None
                to_close += [sid for sid, rec in op_p.staged.get("stripe_sessions", {}).items()
                             if rec["status"] == "invoice_paid"]
                self._commit(op_p)
            op = Op(self, f"sev|{ev['event_id']}", ACTOR, ev["event_id"], g)
            op.compliance_check = check
            try:
                if ev["livemode"] != self.cfg.stripe_livemode:
                    status = "ignored_other_mode"
                elif kind is None:
                    status = "ignored_type"
                elif kind == "session":
                    status = self._stripe_session(op, truth["session"], truth.get("payment"))
                elif kind == "dispute":
                    status = self._stripe_dispute(op, truth["dispute"])
                elif "payment" in truth:
                    status = self._stripe_payment(op, truth["payment"])
                elif kind == "payout":
                    status = self._stripe_payout(op, truth["payout"])
                else:
                    status = "ignored_no_payment"
            except PostingRefused as exc:
                if kind == "dispute":
                    self._stripe_dispute_refused(ev, truth["dispute"])
                raise InvalidReasons("the Stripe event could not be posted", exc.reasons) from None
            except Invalid:
                if kind == "dispute":
                    self._stripe_dispute_refused(ev, truth["dispute"])
                raise
            op.put("stripe_events", ev["event_id"], {"event_id": ev["event_id"], "type": ev["type"],
                                                    "object_id": ev["object_id"], "status": status,
                                                    "at": iso(self._now())})
            op.record(derived_id("sev", ev["event_id"]), "stripe_event_applied", ACTOR, ev["event_id"],
                      {"event_id": ev["event_id"], "type": ev["type"], "object_id": ev["object_id"],
                       "status": status}, f"Stripe event {ev['type']}: {status}"[:200])
            to_close += [sid for sid, rec in op.staged.get("stripe_sessions", {}).items()
                         if rec["status"] == "invoice_paid"]
            resp = {"event_id": ev["event_id"], "status": status, "ledger_event_ids": op.events}
            self._commit(op)
        if to_close:
            # the invoice is paid: its other open pages are closed at Stripe now (the daily job retries a failure)
            g2 = Gather(self, f"sev|{ev['event_id']}|close", ACTOR, ev["event_id"])
            res = {sid: self._expire_page(g2, sid) for sid in to_close}
            with self.lock:
                op2 = Op(self, f"sev|{ev['event_id']}|close", ACTOR, ev["event_id"], g2)
                for sid, a in res.items():
                    if a.available and a.found and a.status in ("expired", "complete"):
                        op2.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": a.status,
                                                         "expired_at": iso(self._now())})
                self._commit(op2)
        return resp

    def _stripe_dispute_refused(self, ev: dict, d: StripeDispute) -> None:
        """AEGIS R1: Finance could not book a dispute Stripe reports (anomalous data, a refused posting). Fail closed
        while it is refused: the media buy behind the payment is blocked from vendor payments, the client receipt is
        held back, and a break tells Andre. Committed on its own (the caller re-raises; Stripe redelivers)."""
        rct = rid("rct", "stripe", d.payment_intent) if d.payment_intent else None
        rc = self.db["receipts"].get(rct) if rct else None
        inv = self.db["invoices"].get(rc["invoice_id"]) if rc and rc.get("invoice_id") else None
        op = Op(self, f"sev|{ev['event_id']}|refused", ACTOR, ev["event_id"])
        if inv is not None and inv["kind"] == I2.MEDIA_KIND:
            buy = self.db["media_buys"].get(inv["media_buy_id"])
            if buy is not None and not buy.get("payment_disputed"):
                op.put("media_buys", buy["buy_id"], {**buy, "payment_disputed": True})
        if rc is not None:
            self._withdraw_client_receipt(op, rct)
        bid = rid("brk", "stripe_dispute_refused", d.dispute_id)
        if bid not in self.db["breaks"]:
            op.put("breaks", bid, {"break_id": bid, "leg": "stripe_dispute", "subject": f"zbm:dispute:{d.dispute_id}",
                                   "difference": d.amount, "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre",
                                   "explanation_code": "unknown", "status": "open", "receipt_id": rct,
                                   "resolution": None})
            op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": "stripe_dispute", "difference": d.amount},
                      f"Break opened: a Stripe dispute ({d.amount}) Finance could not book")
        self._commit(op)

    # --- sessions ------------------------------------------------------------------------------------------------------

    def _stripe_session(self, op: Op, s: StripeSession, p: Optional[StripePayment]) -> str:
        rec = op.get("stripe_sessions", s.session_id) if s.found else None
        if rec is None:
            return "unknown_session"
        if rec["status"] != s.status and s.status in ("complete", "expired"):
            op.put("stripe_sessions", rec["session_id"], {**rec, "status": s.status, "payment_intent": s.payment_intent})
        if p is not None and p.found:
            return self._stripe_payment(op, p)
        return f"session_{s.status}"

    # --- payments --------------------------------------------------------------------------------------------------

    def _stripe_invoice_ok(self, op: Op, inv: Optional[dict], p: StripePayment, gross: Decimal) -> bool:
        if inv is None or inv["entity"] != "zbm" or inv["status"] != "issued" or M.D(inv["total"]) != gross:
            return False
        if not any(s["invoice_id"] == inv["invoice_id"] for s in self.db["stripe_sessions"].values()):
            return False                                 # Finance never made a checkout for it: not Finance's payment
        if p.method == "us_bank_account":
            return "ach" in inv["payment_methods"]
        if p.method == "card":
            return "card" in inv["payment_methods"] and not I2.card_problems(
                [l["line_code"] for l in inv["lines"]], gross, self.cfg.card_prepayments, self.cfg.card_max_invoice)
        return False

    def _unapplied_break(self, op: Op, rct: str, amount: Decimal, why: str, key: Optional[str] = None) -> None:
        bid = rid("brk", "unapplied", key or rct)
        op.put("breaks", bid, {"break_id": bid, "leg": "unapplied_cash", "subject": "zbm:1060",
                               "difference": M.fmt(amount), "opened_at": iso(self._now()),
                               "opened_on": self._today_la().isoformat(), "owner": "andre",
                               "explanation_code": "misapplied_receipt", "status": "open", "receipt_id": rct,
                               "resolution": None, "note": why[:200]})
        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                  {"break_id": bid, "leg": "unapplied_cash", "difference": M.fmt(amount)},
                  f"Break opened: Stripe payment {M.fmt(amount)} not applied ({why})"[:200])

    def _payment_activation(self, g: Gather, p: Optional[StripePayment]) -> Optional[tuple]:
        """AEGIS r3 N-9: before a client's Stripe payment is booked, Compliance is asked (outside the lock) whether the
        client is still activated. None when there is nothing to book or the client is ALLOW; otherwise
        ("refused" | "unverified", reason)."""
        if p is None or not p.found or not p.invoice_id or not p.payment_intent:
            return None
        with self.lock:
            inv = self.db["invoices"].get(p.invoice_id)
            booked = rid("rct", "stripe", p.payment_intent) in self.db["receipts"]
        if inv is None or booked:
            return None
        act = self._activation(g, inv["client_id"])
        if not act.available:
            return ("unverified", "Compliance (38) could not confirm the client's activation when the payment was booked",
                    False)
        if not act.allowed:
            return ("refused", self._activation_reasons(act, full=True)[0]["message"], act_ground(act))
        return None

    HOLD_NOTE = {"refused": ("compliance_hold", "payment from a client Compliance refuses: refund it at Stripe, then "
                                                "release the hold with the refund's journal entry"),
                 "sanctions": ("compliance_hold", "payment from a client refused on SANCTIONS grounds: do not refund or "
                                                  "move it; report to OFAC within 10 business days (31 CFR 501.603)"),
                 "unverified": ("compliance_unverified", "payment booked while Compliance was unreachable: held until "
                                                         "the hourly job (or Andre) gets a current answer")}

    def _open_compliance_hold(self, op: Op, invoice_id: str, kind: str, why: str, rct: str, amount: str,
                              payment_entry_id: str, method_label: str, value_date: str, *,
                              sanctions: bool = False) -> dict:
        prev = (op.get("invoices", invoice_id) or {}).get("held_compliance")
        if prev:
            # AEGIS r5 R5-2: a second held payment on the same invoice: the first hold goes to history and its break
            # is closed as superseded (the new hold, and its break, keep the invoice held). AEGIS r6 N3: a sanctions
            # marker is sticky: the new hold inherits it, so its (open) break is the OFAC one
            sanctions = merge_ground(sanctions, hold_ground(prev))
            self._release_compliance_hold(op, invoice_id, "superseded_by_new_hold", {"new_receipt_id": rct}, ACTOR)
        ground = merge_ground(sanctions)
        leg, note = self.HOLD_NOTE["sanctions" if ground else kind]
        cur = op.get("invoices", invoice_id)
        bid = self._compliance_break(op, leg, cur, rct, amount, note)
        hold = {"kind": kind, "sanctions": bool(ground), "sanctions_ground": ground, "reason": why[:200], "break_id": bid,
                "since": iso(self._now()), "receipt_id": rct, "amount": amount, "payment_entry_id": payment_entry_id,
                "method_label": method_label, "value_date": value_date}
        op.put("invoices", invoice_id, {**cur, "held_compliance": hold})
        op.record(derived_id("hold", invoice_id, kind, rct), "compliance_hold_opened", ACTOR, invoice_id,
                  {"invoice_id": invoice_id, "kind": kind, "receipt_id": rct, "amount": amount, "break_id": bid},
                  f"Payment held ({kind}): {amount}")
        return hold

    def _upgrade_to_sanctions(self, op: Op, invoice_id: str, why: str, actor: str, ground=True) -> None:
        """AEGIS r6 N2, r8: Compliance now says the refusal rests on sanctions: the hold is marked (never unmarked),
        its ground merged (match wins, never back to place) and its open break carries the OFAC note."""
        cur = op.get("invoices", invoice_id)
        hold = cur["held_compliance"]
        new_ground = merge_ground(ground, hold_ground(hold))
        if hold.get("sanctions") and new_ground == hold.get("sanctions_ground"):
            return
        op.put("invoices", invoice_id, {**cur, "held_compliance": {
            **hold, "sanctions": True, "sanctions_ground": new_ground,
            "sanctions_since": hold.get("sanctions_since") or iso(self._now())}})
        b = op.get("breaks", hold["break_id"])
        if b is not None:
            op.put("breaks", b["break_id"], {**b, "note": self.HOLD_NOTE["sanctions"][1][:200]})
        # AEGIS r8: a neutral event id prefix (the id is visible to every audit reader; the type is not)
        op.record(derived_id("holdevt", invoice_id, hold["receipt_id"], new_ground), "compliance_hold_sanctions", actor,
                  invoice_id, {"invoice_id": invoice_id, "receipt_id": hold["receipt_id"], "break_id": hold["break_id"],
                               "ground": new_ground},
                  f"Held payment is now sanctions-grounded: {why}"[:200])

    def _release_compliance_hold(self, op: Op, invoice_id: str, basis: str, detail: dict, actor: str) -> None:
        """Clear the hold: its break is resolved with the basis, the hold is kept in history, and on a Compliance ALLOW
        the client receipt that was held back is prepared now (a refund issues none)."""
        cur = op.get("invoices", invoice_id)
        hold = cur["held_compliance"]
        b = op.get("breaks", hold["break_id"])
        if b is not None and b["status"] != "resolved":
            op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolution": {"basis": basis, **detail},
                                             "resolved_at": iso(self._now())})
        hist = list(cur.get("held_compliance_history") or []) + [{**hold, "released_at": iso(self._now()),
                                                                  "basis": basis, **detail}]
        new = {k: v for k, v in cur.items() if k != "held_compliance"}
        op.put("invoices", invoice_id, {**new, "held_compliance_history": hist})
        rc = op.get("receipts", hold["receipt_id"]) or {}
        if basis == "compliance_allow" and rc.get("status") == "matched" \
                and M.D((rc.get("stripe") or {}).get("refunded") or "0.00") == 0:
            # AEGIS r5 R5-1: a receipt only for money that is still the client's payment (none after any refund)
            self._client_receipt(op, hold["receipt_id"], new, hold["amount"], hold["value_date"], hold["method_label"])
        op.record(derived_id("holdrel", invoice_id, hold["receipt_id"], hold["kind"]), "compliance_hold_released",
                  actor, invoice_id, {"invoice_id": invoice_id, "kind": hold["kind"], "basis": basis, **detail},
                  f"Payment hold released ({basis})")

    @staticmethod
    def _release_ground_ok(hold: dict, basis: str, body: dict) -> None:
        """AEGIS r5 C1: a hold grounded in sanctions is never refunded or released on an ALLOW: OFAC's blocking rules
        apply (report within 10 business days, 31 CFR 501.603 / 501.604), and only Andre's release recording the OFAC
        authorization or reference clears it. That ground is for sanctions holds only."""
        kind = body.get("ofac_document_kind")
        if hold.get("sanctions"):
            if basis not in OFAC_BASES:
                raise Conflict("this payment is held on SANCTIONS grounds: it is not refunded or released on an ALLOW; "
                               "it is reported to OFAC and released only with an OFAC document (basis "
                               "ofac_authorization, or ofac_rejected_return for a rejected payment returned to the "
                               "sender)")
            if not body.get("ofac_authorization_sha256") or not kind:
                raise Conflict("an OFAC release names the SHA-256 of the OFAC document (ofac_authorization_sha256) and "
                               "its kind (ofac_document_kind)")
            if basis == "ofac_authorization" and kind not in BLOCKED_EXIT_KINDS:
                raise Conflict("ofac_authorization takes a license, an unblocking report, a Compliance Release or a "
                               "delisting; a rejected-transaction report goes with ofac_rejected_return")
            if basis == "ofac_rejected_return":
                # AEGIS r8: the default for every sanctions hold is BLOCKED. A return is possible only when the ground
                # is a sanctioned place (never an OFAC list match) AND OFAC has said in writing to reject the funds
                if hold_ground(hold) != "place":
                    raise Conflict("this payment rests on an OFAC list match: it stays blocked; a rejected return is "
                                   "never allowed for a match (license, unblocking report, Compliance Release or "
                                   "delisting only)")
                if kind != "rejected_transaction_report":
                    raise Conflict("ofac_rejected_return takes the filed 501.604 rejected-transaction report "
                                   "(ofac_document_kind rejected_transaction_report)")
                if not body.get("ofac_guidance_sha256"):
                    raise Conflict("a rejected return needs OFAC's written guidance that the funds be rejected "
                                   "(ofac_guidance_sha256, document kind ofac_guidance); without it the payment "
                                   "stays blocked")
        elif basis in OFAC_BASES:
            raise Conflict(f"{basis} releases a sanctions hold only; this hold is not grounded in sanctions")

    def compliance_hold_release(self, request_id: str, invoice_id: str, body: dict) -> dict:
        """AEGIS r4 R4-2: Andre releases a held payment, on one of two grounds only.
        - ``refund``: ``entry_id`` names an F7r refund entry of THIS payment's receipt, posted after the payment entry
          in the journal (journal order, never timestamps), and the refunds booked on the receipt cover the held
          amount. The client gets no receipt.
        - ``compliance_allow``: Compliance (38) answers a current ALLOW for the client now (read before the lock). The
          held-back client receipt is prepared (unless anything was refunded).
        - ``ofac_authorization``: a sanctions hold only, with the SHA-256 and kind of the OFAC document: a license, an
          unblocking report (FAQ 1196), a Compliance Release (501.806) or a delisting notice (AEGIS r5 C1, r6, r7).
        - ``ofac_rejected_return``: a sanctions hold whose payment was REJECTED, not blocked: it was returned to the
          sender (a refund proven as for ``refund``) and the 501.604 rejected-transaction report is hashed (AEGIS r7)."""
        basis = body["basis"]
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"compliance-hold/{invoice_id}",
                                     {"basis": basis, "entry_id": body.get("entry_id"),
                                      "ofac_authorization_sha256": body.get("ofac_authorization_sha256"),
                                      "ofac_document_kind": body.get("ofac_document_kind"),
                                      "ofac_guidance_sha256": body.get("ofac_guidance_sha256")})
            if ent:
                return ent["response"]
            inv = self.db["invoices"].get(invoice_id)
            if inv is None:
                raise NotFound("no such invoice")
            if not inv.get("held_compliance"):
                raise Conflict("this invoice has no Compliance hold")
            self._release_ground_ok(inv["held_compliance"], basis, body)
            client = inv["client_id"]
        g = Gather(self, f"holdrel|{request_id}", "andre", invoice_id)
        detail: dict = {}
        if basis == "refund":
            # AEGIS r6 N2: a refund release asks Compliance first: if the refusal now rests on sanctions, the hold is
            # upgraded and the release refused; if Compliance cannot answer, refused (fail closed)
            act = self._activation(g, client)
            if not act.available:
                raise Refused("the hold stays: Compliance (38) cannot confirm the refusal is not sanctions-grounded",
                              self._activation_reasons(act, full=True), ledger_event_ids=g.events)
            if act.sanctions:
                with self.lock:
                    if (self.db["invoices"][invoice_id].get("held_compliance") or {}).get("kind"):
                        op_u = Op(self, f"holdrel|{request_id}|sanctions", "andre", invoice_id, g)
                        self._upgrade_to_sanctions(op_u, invoice_id, act.reasons[0] if act.reasons else "", "andre",
                                                   act_ground(act))
                        self._commit(op_u)
                raise Conflict("Compliance now says this refusal rests on SANCTIONS: the payment is not refunded; it "
                               "is reported to OFAC (the hold is now a sanctions hold)")
        if basis == "compliance_allow":
            act = self._activation(g, client)
            reasons = self._activation_reasons(act, full=True)
            if reasons:
                raise Refused("the hold stays: Compliance does not currently ALLOW this client", reasons,
                              ledger_event_ids=g.events)
            detail = {"ruling_id": act.ruling_id}
        with self.lock:
            inv = self.db["invoices"][invoice_id]
            hold = inv.get("held_compliance")
            if not hold:
                raise Conflict("this invoice has no Compliance hold")
            self._release_ground_ok(hold, basis, body)
            if basis == "ofac_authorization":
                detail = {"ofac_authorization_sha256": body["ofac_authorization_sha256"],
                          "ofac_document_kind": body["ofac_document_kind"]}
            if basis in ("refund", "ofac_rejected_return"):
                # AEGIS r7: a rejected payment returned to the sender is proven like a refund (an F7r of THIS payment,
                # after it, covering it), plus the hash of the filed 501.604 report
                eid = body.get("entry_id")
                if not eid:
                    raise Conflict("this release names the refund's journal entry (entry_id): refund the payment to "
                                   "the sender at Stripe first")
                order = {e["entry_id"]: i for i, e in enumerate(self.entries)}
                e = self.entries_by_id.get(eid)
                if e is None or e.get("memo_code") != "F7r" or (e.get("source") or {}).get("kind") != "stripe_refund" \
                        or (e.get("source") or {}).get("id") != hold["receipt_id"]:
                    raise Conflict("entry_id is not a Stripe refund (F7r) of this held payment")
                if order.get(eid, -1) <= order.get(hold["payment_entry_id"], 10 ** 12):
                    raise Conflict("the refund entry is not after the payment entry in the journal")
                rc = self.db["receipts"].get(hold["receipt_id"]) or {}
                refunded = M.D((rc.get("stripe") or {}).get("refunded") or "0.00")
                if refunded < M.D(hold["amount"]):
                    raise Conflict(f"refunds booked ({M.fmt(refunded)}) do not cover the held amount ({hold['amount']})")
                detail = {"entry_id": eid, "refunded": M.fmt(refunded)}
                if basis == "ofac_rejected_return":
                    detail.update(returned_to_sender=True, ofac_authorization_sha256=body["ofac_authorization_sha256"],
                                  ofac_document_kind=body["ofac_document_kind"],
                                  ofac_guidance_sha256=body["ofac_guidance_sha256"],
                                  documents=[{"kind": "rejected_transaction_report",
                                              "sha256": body["ofac_authorization_sha256"]},
                                             {"kind": "ofac_guidance", "sha256": body["ofac_guidance_sha256"]}])
            op = Op(self, f"holdrel|{request_id}", "andre", invoice_id, g)
            self._release_compliance_hold(op, invoice_id, basis, detail, "andre")
            resp = self._idem_add(op, key, h, {"invoice_id": invoice_id, "released": True, "basis": basis, **detail,
                                               "ledger_event_ids": op.events})
            self._commit(op)
            return resp

    def _compliance_break(self, op: Op, leg: str, inv: dict, rct: str, amount: str, note: str) -> str:
        bid = rid("brk", leg, rct)
        if bid not in self.db["breaks"]:
            op.put("breaks", bid, {"break_id": bid, "leg": leg, "subject": f"zbm:invoice:{inv['invoice_id']}",
                                   "difference": amount, "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre",
                                   "explanation_code": "unknown", "status": "open", "receipt_id": rct,
                                   "resolution": None, "note": note[:200]})
            op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": leg, "difference": amount}, f"Break opened: {note}"[:200])
        return bid

    def _stripe_payment(self, op: Op, p: StripePayment) -> str:
        if not p.found:
            return "unknown_payment"
        rct = rid("rct", "stripe", p.payment_intent)
        rc = op.get("receipts", rct)
        if p.charge_status == "failed" and rc is not None:
            return self._stripe_payment_failed(op, rc, p)
        if p.status != "succeeded" or p.charge_status != "succeeded":
            return f"payment_{p.status}"
        if rc is not None:
            # sweep X-6: a refund made at Stripe (``charge.refunded``) is booked from the charge's amount_refunded
            return self._stripe_refund(op, rct, p) or "payment_already_booked"
        if p.currency != "usd" or p.balance_txn is None or p.gross is None or p.fee is None:
            # succeeded but Stripe has not exposed its balance transaction yet: let Stripe send the event again
            raise Unavailable("the payment's Stripe balance transaction is not readable yet; retried later")
        gross, fee = M.D(p.gross), M.D(p.fee)
        if gross <= 0:
            raise Invalid("Stripe reported a payment of zero or less")
        inv = op.get("invoices", p.invoice_id) if p.invoice_id else None
        ok = gross == M.D(p.amount_received) and self._stripe_invoice_ok(op, inv, p, gross)
        src = {"kind": "receipt", "id": rct}
        value_date = self._today_la().isoformat()
        if ok:
            e = self._post(op, "zbm", [J.dr("1060", gross), J.cr("1100", gross, f"client:{inv['client_id']}")],
                           "F13", src, f"F13|{p.payment_intent}", actor=ACTOR)
            op.put("invoices", inv["invoice_id"], {**inv, "status": "paid", "paid_at": iso(self._now())})
            if inv["kind"] == I2.MEDIA_KIND:
                self._media_prepaid(op, inv, rct, value_date, M.fmt(gross))
                if any(d.get("payment_intent") == p.payment_intent and M.D(d["held"]) > 0
                       for d in self.db["stripe_disputes"].values()):
                    buy = op.get("media_buys", inv["media_buy_id"])
                    if buy is not None:
                        op.put("media_buys", buy["buy_id"], {**buy, "payment_disputed": True})
            status = "matched"
        else:
            e = self._post(op, "zbm", [J.dr("1060", gross), J.cr("2070", gross)], "F13", src,
                           f"F13u|{p.payment_intent}", actor=ACTOR)
            self._unapplied_break(op, rct, gross, "no issued invoice of that amount and method was paid through a "
                                                  "Finance checkout")
            status = "unapplied"
        fee_entry = None
        if fee > 0:
            fee_entry = self._post(op, "zbm", [J.dr("5020", fee), J.cr("1060", fee)], "F13f", src,
                                   f"F13f|{p.payment_intent}", actor=ACTOR)
        elif fee < 0:
            fee_entry = self._post(op, "zbm", [J.dr("1060", -fee), J.cr("5020", -fee)], "F13f", src,
                                   f"F13f|{p.payment_intent}", actor=ACTOR)
        method = "stripe_card" if p.method == "card" else "stripe_ach" if p.method == "us_bank_account" \
            else "stripe_other"
        op.put("receipts", rct, {"receipt_id": rct, "invoice_id": inv["invoice_id"] if ok else None, "entity": "zbm",
                                 "amount": M.fmt(gross), "method": method, "bank_txn_ref_sha256": None,
                                 "into_account": "1060", "value_date": value_date, "matched_at": iso(self._now()),
                                 "status": status, "entry_id": e["entry_id"],
                                 "stripe": {"payment_intent": p.payment_intent, "charge": p.charge,
                                            "balance_txn": p.balance_txn, "fee": M.sfmt(fee),
                                            "fee_entry_id": fee_entry["entry_id"] if fee_entry else None}})
        op.record(derived_id("rct", rct), "receipt_matched" if ok else "receipt_unapplied", ACTOR, rct,
                  {"receipt_id": rct, "entity": "zbm", "account": "1060", "amount": M.fmt(gross),
                   "invoice_id": inv["invoice_id"] if ok else None, "fee": M.sfmt(fee)},
                  f"Stripe payment {status}: {M.fmt(gross)} (fee {M.sfmt(fee)})")
        if ok:
            for s in self.db["stripe_sessions"].values():
                if s["invoice_id"] == inv["invoice_id"] and s["status"] == "open":
                    op.put("stripe_sessions", s["session_id"], {**s, "status": "invoice_paid"})
            check = getattr(op, "compliance_check", None)
            if check:
                # AEGIS r3 N-9, r4 R4-1: the money is booked as received (truthfully), but nothing follows from it
                # while Compliance does not currently ALLOW the client (refused) or could not say (unverified): the
                # invoice is held, no client receipt goes out, a media buy's vendor is not paid, the invoice status
                # SEO reads says held. Released only by compliance_hold_release (a refund, or a fresh ALLOW).
                kind, why, sanctions = check
                self._open_compliance_hold(op, inv["invoice_id"], kind, why, rct, M.fmt(gross), e["entry_id"],
                                           METHOD_LABEL.get(p.method, "Stripe"), value_date, sanctions=sanctions)
                status = f"matched_held_{'compliance' if kind == 'refused' else 'unverified'}"
            else:
                self._client_receipt(op, rct, inv, M.fmt(gross), value_date, METHOD_LABEL.get(p.method, "Stripe"))
        return self._stripe_refund(op, rct, p) or status

    def _stripe_refund(self, op: Op, rct: str, p: StripePayment) -> Optional[str]:
        """Sweep X-6: money refunded at Stripe (the Dashboard, or any refund Finance did not make) left the Stripe
        balance. The increase of the charge's ``amount_refunded`` over what Finance booked is posted once (F7r, keyed
        by the new cumulative total), like a lost dispute: the client owes it again until Andre settles it (a break
        tells him). Any refund on a media prepayment blocks vendor payments on that buy."""
        if p.amount_refunded is None:
            return None
        rc = op.get("receipts", rct)
        if rc is None:
            return None
        total = M.D(p.amount_refunded)
        done = M.D((rc.get("stripe") or {}).get("refunded") or "0.00")
        if total == done:
            return None
        if total < done:
            raise Invalid("Stripe reported less refunded on a payment than Finance already booked")
        if total > M.D(rc["amount"]):
            raise Invalid("Stripe reported a refund above the payment")
        if rc["status"] not in ("matched", "partially_refunded", "unapplied", "refunded", "charged_back"):
            raise Invalid(f"Stripe reported a refund on a payment Finance holds as {rc['status']}")
        delta = M.q(total - done)
        inv = op.get("invoices", rc["invoice_id"]) if rc.get("invoice_id") else None
        full = total == M.D(rc["amount"])
        # AEGIS 5a56a3a L2: every receipt state ends in a booked, correct status (the money left the Stripe balance
        # whatever Finance thought of the payment; a refusal here would leave 1060 overstated for good):
        #   matched / partially_refunded -> Dr 1100 A/R[client]; ``partially_refunded`` or, in full, ``refunded``
        #   unapplied                    -> Dr 2070; stays ``unapplied`` while unapplied cash remains, ``refunded``
        #                                   once it is all refunded
        #   charged_back                 -> the payment was already taken back (F7l): a refund on top is the
        #                                   client's over-recovery, Dr 1100 A/R[client] (Dr 5030 if no invoice is
        #                                   known); the status stays, the break names it for Andre
        prior = rc["status"]
        reversed_before = prior == "charged_back"
        matched = prior in ("matched", "partially_refunded", "refunded") and inv is not None
        if matched or (reversed_before and inv is not None):
            debit = J.dr("1100", delta, f"client:{inv['client_id']}")
        elif reversed_before:
            debit = J.dr("5030", delta)
        else:
            debit = J.dr("2070", delta)
        e = self._post(op, "zbm", [debit, J.cr("1060", delta)], "F7r", {"kind": "stripe_refund", "id": rct},
                       f"F7r|{p.payment_intent}|{M.fmt(total)}", actor=ACTOR, fact=True)
        st = dict(rc.get("stripe") or {})
        st.update(refunded=M.fmt(total), refund_entry_ids=list(st.get("refund_entry_ids") or []) + [e["entry_id"]])
        # AEGIS f751017 L-N1, the other order: a partly lost dispute first, then a refund of the rest
        charged = M.D((inv or {}).get("charged_back") or "0.00") if matched else M.ZERO
        self._over_recovery_break(op, rct, rc, total, self._charged_back_total(rc, inv))
        net_reversed = matched and charged > 0 and total + charged == M.D(rc["amount"])
        if reversed_before:
            status = prior
        elif net_reversed:
            status = "charged_back"
        elif full:
            status = "refunded"
        else:
            status = "partially_refunded" if matched else "unapplied"
        op.put("receipts", rct, {**rc, "stripe": st, "status": status})
        if matched and prior != "refunded":
            # the client receipt states the full amount paid: no longer true after any refund (never sent; one
            # already sent is marked withdrawn_after_send)
            self._withdraw_client_receipt(op, rct)
        if matched and inv["status"] == "paid":
            if full or net_reversed:
                op.put("invoices", inv["invoice_id"], {**inv, "status": "issued", "paid_at": None,
                                                       "refunded": M.fmt(total)})
                if inv["kind"] == I2.MEDIA_KIND:
                    self._media_unpaid(op, inv, rct, "the client was refunded at Stripe", break_key=f"refund:{rct}")
            else:
                op.put("invoices", inv["invoice_id"], {**inv, "refunded": M.fmt(total)})
        elif reversed_before and inv is not None:
            op.put("invoices", inv["invoice_id"], {**inv, "refunded": M.fmt(total)})
        if inv is not None and inv["kind"] == I2.MEDIA_KIND:
            buy = op.get("media_buys", inv["media_buy_id"])
            if buy is not None:
                op.put("media_buys", buy["buy_id"], {**buy, "payment_refunded": True})
        bid = rid("brk", "stripe_refund", rct, M.fmt(total))
        if op.get("breaks", bid) is None:
            op.put("breaks", bid, {"break_id": bid, "leg": "stripe_refund", "subject": f"zbm:1060:{rct}"[:160],
                                   "difference": M.fmt(delta), "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre",
                                   "explanation_code": "unknown", "status": "open", "receipt_id": rct,
                                   "resolution": None})
            op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": "stripe_refund", "difference": M.fmt(delta)},
                      f"Break opened: {M.fmt(delta)} refunded at Stripe outside Finance")
        op.record(derived_id("rfds", rct, M.fmt(total)), "stripe_refund_booked", ACTOR, rct,
                  {"receipt_id": rct, "amount": M.fmt(delta), "refunded_total": M.fmt(total), "entry_id": e["entry_id"],
                   "invoice_id": inv["invoice_id"] if inv else None},
                  f"Stripe refund booked: {M.fmt(delta)} (total refunded {M.fmt(total)})")
        return "refund_booked"

    @staticmethod
    def _charged_back_total(rc: dict, inv: Optional[dict]) -> Decimal:
        """What lost disputes took back on this payment (receipt record; invoices before c0869c4 carry it only)."""
        v = (rc.get("stripe") or {}).get("charged_back")
        if v is None and inv is not None and rc.get("status") in ("charged_back", "matched", "partially_refunded"):
            v = inv.get("charged_back")
        return M.D(v or "0.00")

    def _over_recovery_break(self, op: Op, rct: str, rc: dict, refunded: Decimal, charged: Decimal) -> None:
        """AEGIS c0869c4 L-N2: refunds plus lost chargebacks above the payment -- the client got more back than it
        paid. Booked as Stripe reports it (the money left the balance), and a break tells Andre (once per total)."""
        over = M.q(refunded + charged - M.D(rc["amount"]))
        if over <= 0:
            return
        bid = rid("brk", "over_recovery", rct, M.fmt(refunded), M.fmt(charged))
        if op.get("breaks", bid) is not None:
            return
        op.put("breaks", bid, {"break_id": bid, "leg": "stripe_over_recovery", "subject": f"zbm:1060:{rct}"[:160],
                               "difference": M.fmt(over), "opened_at": iso(self._now()),
                               "opened_on": self._today_la().isoformat(), "owner": "andre",
                               "explanation_code": "unknown", "status": "open", "receipt_id": rct,
                               "resolution": None, "refunded": M.fmt(refunded), "charged_back": M.fmt(charged)})
        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                  {"break_id": bid, "leg": "stripe_over_recovery", "difference": M.fmt(over)},
                  f"Break opened: refunded plus charged back exceeds the payment by {M.fmt(over)}")

    def _stripe_payment_failed(self, op: Op, rc: dict, p: StripePayment) -> str:
        """A payment Finance booked as succeeded failed afterwards (an ACH debit can): the money left the Stripe
        balance; the client owes the invoice again."""
        if rc["status"] in ("returned", "charged_back"):
            return "payment_failure_already_booked"
        amt = -M.D(p.failure_amount) if p.failure_amount is not None else M.D(rc["amount"])
        if amt <= 0:
            raise Invalid("Stripe reported a payment failure that took no money back")
        inv = op.get("invoices", rc["invoice_id"]) if rc.get("invoice_id") else None
        src = {"kind": "receipt_return", "id": rc["receipt_id"]}
        live = rc["status"] in ("matched", "partially_refunded")      # AEGIS 5a56a3a L2: still the client's payment
        debit = J.dr("1100", amt, f"client:{inv['client_id']}") if (inv and live) else J.dr("2070", amt)
        e = self._post(op, "zbm", [debit, J.cr("1060", amt)], "F13x", src, f"F13x|{p.payment_intent}", actor=ACTOR,
                       fact=True)
        ff = M.D(p.failure_fee) if p.failure_fee is not None else M.ZERO
        if ff > 0:
            self._post(op, "zbm", [J.dr("5020", ff), J.cr("1060", ff)], "F13x", src, f"F13xf|{p.payment_intent}",
                       actor=ACTOR, fact=True)
        elif ff < 0:
            self._post(op, "zbm", [J.dr("1060", -ff), J.cr("5020", -ff)], "F13x", src, f"F13xf|{p.payment_intent}",
                       actor=ACTOR, fact=True)
        op.put("receipts", rc["receipt_id"], {**rc, "status": "returned", "returned": {
            "cause": "stripe_payment_failed", "failure_txn": p.failure_txn, "entry_id": e["entry_id"],
            "at": iso(self._now())}})
        exposed = M.ZERO
        if inv and live and inv["status"] == "paid":
            op.put("invoices", inv["invoice_id"], {**inv, "status": "issued", "paid_at": None})
            self._withdraw_client_receipt(op, rc["receipt_id"])
            if inv["kind"] == I2.MEDIA_KIND:
                exposed = self._media_unpaid(op, inv, rc["receipt_id"], "whose Stripe payment failed")
        op.record(derived_id("rctx", rc["receipt_id"]), "deposit_returned", ACTOR, rc["receipt_id"],
                  {"receipt_id": rc["receipt_id"], "amount": M.fmt(amt), "entity": "zbm", "entry_id": e["entry_id"],
                   "vendor_exposure": M.fmt(exposed)}, f"Stripe payment failed after success: {M.fmt(amt)}")
        return "payment_failed_after_success"

    # --- disputes --------------------------------------------------------------------------------------------------

    def _stripe_dispute(self, op: Op, d: StripeDispute) -> str:
        if not d.found:
            return "unknown_dispute"
        did = d.dispute_id
        rec = op.get("stripe_disputes", did) or {"dispute_id": did, "payment_intent": d.payment_intent,
                                                 "posted_txns": [], "held": "0.00", "status": None,
                                                 "flagged": False, "finalized": False, "opened_at": iso(self._now())}
        rct = rid("rct", "stripe", d.payment_intent) if d.payment_intent else None
        rc = op.get("receipts", rct) if rct else None
        inv = op.get("invoices", rc["invoice_id"]) if rc and rc.get("invoice_id") else None
        matched = rc is not None and rc["status"] in ("matched", "partially_refunded") and inv is not None
        src = {"kind": "stripe_dispute", "id": did}
        held = M.D(rec["held"])
        posted = list(rec["posted_txns"])
        new = [t for t in d.txns if t["txn_id"] not in posted]
        if held - M.total(M.D(t["amount"]) for t in new) < 0:
            raise Invalid("Stripe reinstated more for this dispute than it withdrew")
        for t in new:
            if t["txn_id"] in posted:
                continue
            amt, fee = M.D(t["amount"]), M.D(t["fee"])
            if amt < 0:
                self._post(op, "zbm", [J.dr("1300", -amt), J.cr("1060", -amt)], "F7", src, f"F7|{t['txn_id']}",
                           actor=ACTOR, fact=True)
                held -= amt
            elif amt > 0:
                self._post(op, "zbm", [J.dr("1060", amt), J.cr("1300", amt)], "F7a", src, f"F7a|{t['txn_id']}",
                           actor=ACTOR, fact=True)
                held -= amt
            if fee > 0:
                self._post(op, "zbm", [J.dr("5030", fee), J.cr("1060", fee)], "F7", src, f"F7f|{t['txn_id']}",
                           actor=ACTOR, fact=True)
            elif fee < 0:
                self._post(op, "zbm", [J.dr("1060", -fee), J.cr("5030", -fee)], "F7a", src, f"F7af|{t['txn_id']}",
                           actor=ACTOR, fact=True)
            posted.append(t["txn_id"])
        first_seen = not rec.get("recorded")
        rec = {**rec, "posted_txns": posted, "held": M.fmt(held), "status": d.status, "amount": d.amount,
               "receipt_id": rct if rc else None}
        if held > 0 and not rec["flagged"] and matched:
            rec["flagged"] = True
            if inv["kind"] == I2.MEDIA_KIND:
                buy = op.get("media_buys", inv["media_buy_id"])
                if buy is not None:
                    op.put("media_buys", buy["buy_id"], {**buy, "payment_disputed": True})
                    if M.D(buy["vendor_paid"]) > 0:
                        bid = rid("brk", "media_exposure", buy["buy_id"], did)
                        op.put("breaks", bid, {"break_id": bid, "leg": "media_exposure",
                                               "subject": f"zbm:buy:{buy['buy_id']}", "difference": buy["vendor_paid"],
                                               "opened_at": iso(self._now()), "opened_on": self._today_la().isoformat(),
                                               "owner": "andre", "explanation_code": "unknown", "status": "open",
                                               "receipt_id": rct, "resolution": None})
                        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                                  {"break_id": bid, "leg": "media_exposure", "difference": buy["vendor_paid"]},
                                  f"Break opened: vendor paid {buy['vendor_paid']} from a prepayment now disputed")
        status = f"dispute_{d.status}"
        # AEGIS L1: "won" (or a closed inquiry) is final only once Stripe has given the money back (held 0); a loss is
        # final at once
        closing = d.status in DONE_DISPUTE and not rec["finalized"] and (d.status == "lost" or held == 0)
        if closing:
            rec["finalized"] = True
            if d.status == "lost" and held > 0:
                if matched:
                    line = J.dr("1100", held, f"client:{inv['client_id']}")
                elif rc is not None and rc["status"] == "unapplied":
                    line = J.dr("2070", held)
                else:
                    line = J.dr("5030", held)            # a payment Finance never booked: a loss Andre explains
                self._post(op, "zbm", [line, J.cr("1300", held)], "F7l", src, f"F7l|{did}", actor=ACTOR, fact=True)
                rec["held"] = "0.00"
                if rc is not None:
                    prior_cb = M.D((rc.get("stripe") or {}).get("charged_back") or "0.00")
                    st_rc = {**(rc.get("stripe") or {}), "charged_back": M.fmt(prior_cb + held)}
                    rc = {**rc, "stripe": st_rc}
                    op.put("receipts", rct, rc)
                    self._over_recovery_break(op, rct, rc, M.D(st_rc.get("refunded") or "0.00"), prior_cb + held)
                if matched:
                    # AEGIS f751017 L-N1: a charge whose refunds and chargeback together take back the whole payment
                    # is fully reversed, whatever the order (e.g. 300 refunded, then a 700 dispute lost)
                    refunded = M.D((rc.get("stripe") or {}).get("refunded") or "0.00")
                    if held == M.D(inv["total"]) or (refunded > 0 and held + refunded == M.D(rc["amount"])):
                        op.put("invoices", inv["invoice_id"], {**inv, "status": "issued", "paid_at": None,
                                                               "charged_back": M.fmt(held)})
                        op.put("receipts", rct, {**rc, "status": "charged_back"})
                        self._withdraw_client_receipt(op, rct)
                        if inv["kind"] == I2.MEDIA_KIND:
                            self._media_unpaid(op, inv, rct, "the client took back through a Stripe dispute",
                                               break_key=did)
                    else:
                        op.put("invoices", inv["invoice_id"], {**inv, "charged_back": M.fmt(held)})
                        bid = rid("brk", "dispute_receivable", did)
                        op.put("breaks", bid, {"break_id": bid, "leg": "dispute_receivable",
                                               "subject": f"zbm:1100:client:{inv['client_id']}"[:160],
                                               "difference": M.fmt(held), "opened_at": iso(self._now()),
                                               "opened_on": self._today_la().isoformat(), "owner": "andre",
                                               "explanation_code": "partial_chargeback", "status": "open",
                                               "receipt_id": rct, "resolution": None})
                        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                                  {"break_id": bid, "leg": "dispute_receivable", "difference": M.fmt(held)},
                                  f"Break opened: client owes {M.fmt(held)} after a partly lost Stripe dispute")
            elif matched and inv["kind"] == I2.MEDIA_KIND and held == 0:
                buy = op.get("media_buys", inv["media_buy_id"])
                if buy is not None and buy.get("payment_disputed"):
                    op.put("media_buys", buy["buy_id"], {**buy, "payment_disputed": False})
        op.put("stripe_disputes", did, {**rec, "recorded": True})
        if first_seen:
            op.record(derived_id("sdsp", did), "dispute_opened", ACTOR, did,
                      {"dispute_id": did, "invoice_id": inv["invoice_id"] if inv else None, "amount": d.amount,
                       "kind": "stripe_dispute"}, f"Stripe dispute on a client payment ({d.amount})")
        if closing:
            op.record(derived_id("sdspc", did), "dispute_closed", ACTOR, did, {"dispute_id": did, "outcome": d.status},
                      f"Stripe dispute closed: {d.status}")
        return status

    # --- payouts ---------------------------------------------------------------------------------------------------

    def _stripe_payout(self, op: Op, po: StripePayout) -> str:
        if not po.found:
            return "unknown_payout"
        rec = op.get("stripe_payouts", po.payout_id) or {"payout_id": po.payout_id, "posted": False,
                                                         "reversed": False, "bank_receipt_id": None,
                                                         "first_seen_on": self._today_la().isoformat()}
        amt = M.D(po.amount)
        src = {"kind": "stripe_payout", "id": po.payout_id}
        if rec.get("amount") not in (None, po.amount):
            raise Invalid("Stripe reported a different amount for a payout already recorded")
        if po.status == "paid" and not rec["posted"]:
            banked, suggested = self._unapplied_payout_receipt(op, po.payout_id, amt, rec)
            if banked is not None:
                # AEGIS (launch hardening) H2: the bank showed this payout before Stripe said so and it was booked as
                # unapplied cash; it is reclassified (Dr 2070 / Cr 1060), never posted to 1010 a second time
                e = self._post(op, "zbm", [J.dr("2070", amt), J.cr("1060", amt)], "F13p", src,
                               f"F13p|{po.payout_id}", actor=ACTOR, fact=True)
                op.put("receipts", banked["receipt_id"], {**banked, "status": "stripe_payout",
                                                          "stripe_payout_id": po.payout_id,
                                                          "reclassified_entry_id": e["entry_id"]})
                bid = rid("brk", "unapplied", banked["receipt_id"])
                brk = op.get("breaks", bid)
                if brk is not None and brk["status"] in ("open", "explained"):         # AEGIS N4: explained too
                    op.put("breaks", bid, {**brk, "status": "resolved", "resolution": {
                        "entry_id": e["entry_id"], "approved_by": "auto_exact_payout_id", "at": iso(self._now()),
                        "why": f"the bank line carried Stripe payout id {po.payout_id}"}})
                rec = {**rec, "posted": True, "bank_receipt_id": banked["receipt_id"]}
            else:
                self._post(op, "zbm", [J.dr("1010", amt), J.cr("1060", amt)], "F13p", src, f"F13p|{po.payout_id}",
                           actor=ACTOR, fact=True)
                rec = {**rec, "posted": True}
                if suggested is not None:
                    # AEGIS N2: an amount match is a suggestion for Andre, never an automatic relabel. Until he acts,
                    # reconciliation L2 shows 1010 above the bank by this amount (the payout and the unapplied credit
                    # are the same money); he exact-reverses the unapplied receipt's entry if the suggestion is right.
                    sbid = rid("brk", "unapplied", suggested["receipt_id"])
                    sbrk = op.get("breaks", sbid)
                    if sbrk is not None and sbrk["status"] in ("open", "explained"):
                        op.put("breaks", sbid, {**sbrk, "suggested_stripe_payout_id": po.payout_id,
                                                "note": f"may be Stripe payout {po.payout_id} (same amount, no "
                                                        "reference): if so, reverse this receipt's entry"[:200]})
        elif po.status == "failed" and rec["posted"] and not rec["reversed"]:
            self._post(op, "zbm", [J.dr("1060", amt), J.cr("1010", amt)], "F13q", src, f"F13q|{po.payout_id}",
                       actor=ACTOR, fact=True)
            rec = {**rec, "reversed": True}
            if rec.get("bank_receipt_id"):
                # AEGIS L1: the bank showed this money arriving; Stripe now says the payout failed -- Andre checks
                bid = rid("brk", "stripe_payout_failed", po.payout_id)
                if op.get("breaks", bid) is None:
                    op.put("breaks", bid, {"break_id": bid, "leg": "stripe_payout",
                                           "subject": f"zbm:1010:{po.payout_id}", "difference": po.amount, "opened_at": iso(self._now()),
                                           "opened_on": self._today_la().isoformat(), "owner": "andre",
                                           "explanation_code": "unknown", "status": "open",
                                           "receipt_id": rec["bank_receipt_id"], "resolution": None})
                    op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                              {"break_id": bid, "leg": "stripe_payout", "difference": po.amount},
                              f"Break opened: Stripe payout {po.amount} failed after the bank showed it arriving")
        rec = {**rec, "status": po.status, "amount": po.amount, "seen_at": iso(self._now())}
        op.put("stripe_payouts", po.payout_id, rec)
        return f"payout_{po.status}"

    def _unapplied_payout_receipt(self, op: Op, payout_id: str, amt: Decimal,
                                  rec: dict) -> tuple[Optional[dict], Optional[dict]]:
        """(exact, suggested) unapplied ZBM operating-account receipts for this payout. Only a receipt still live --
        its unapplied break open or explained and its entry not reversed by Andre (AEGIS N1) -- counts.
        ``exact``: the bank line carried this payout's id; it is reclassified automatically. ``suggested``: no
        reference, same amount, dated from 10 days before to 2 days after the payout was first seen; only flagged."""
        seen = date.fromisoformat(rec["first_seen_on"]) if rec.get("first_seen_on") else None
        reversed_ids = {e.get("reverses_entry_id") for e in self.entries_by_id.values() if e.get("reverses_entry_id")}
        exact, near = [], []
        for r in self.db["receipts"].values():
            r = op.get("receipts", r["receipt_id"])
            if r["status"] != "unapplied" or r["entity"] != "zbm" or r.get("into_account") != "1010" \
                    or M.D(r["amount"]) != amt or r.get("entry_id") in reversed_ids:
                continue
            brk = op.get("breaks", rid("brk", "unapplied", r["receipt_id"]))
            if brk is None or brk["status"] not in ("open", "explained"):
                continue
            token = r.get("reference_token")
            if token:
                if token == payout_id:
                    exact.append(r)
            elif seen is not None and seen - timedelta(days=10) <= date.fromisoformat(r["value_date"]) \
                    <= seen + timedelta(days=2):
                near.append(r)
        first = (lambda xs: min(xs, key=lambda r: (r["value_date"], r["receipt_id"])) if xs else None)
        return first(exact), first(near)

    def _bank_stripe_payout(self, op: Op, ln: dict, amt: Decimal, rct: str) -> Optional[dict]:
        """A ZBM operating-account credit that IS a Stripe payout (launch hardening): matched to the payout instead of
        being booked again as unapplied cash. Exact when the bank line carries the payout id as its reference;
        otherwise the oldest unmatched payout of exactly that amount first seen from 2 days before to 10 days before
        the line's value date. If the payout's paid event has not arrived yet, the bank is the proof: F13p posts now
        (the same idempotency key the event uses, so it never posts twice)."""
        token = ln.get("reference_token") or ""
        vd = ln["value_date"] if isinstance(ln["value_date"], date) else date.fromisoformat(str(ln["value_date"]))
        cands = []
        for p in self.db["stripe_payouts"].values():
            p = op.get("stripe_payouts", p["payout_id"])
            if p.get("bank_receipt_id") or p["status"] in ("failed", "canceled") or M.D(p["amount"]) != amt:
                continue
            if token:
                # AEGIS M2: a line carrying any reference is matched to a payout only by that payout's own id
                if p["payout_id"] == token:
                    cands.append(p)
                continue
            if not p.get("first_seen_on"):
                continue                                 # AEGIS M1: no first-seen date, no matching by amount
            seen = date.fromisoformat(p["first_seen_on"])
            if seen - timedelta(days=2) <= vd <= seen + timedelta(days=10):
                cands.append(p)
        if not cands:
            return None
        p = min(cands, key=lambda x: (x.get("first_seen_on") or "", x["payout_id"]))
        src = {"kind": "stripe_payout", "id": p["payout_id"]}
        e = self._post(op, "zbm", [J.dr("1010", amt), J.cr("1060", amt)], "F13p", src, f"F13p|{p['payout_id']}",
                       actor=ACTOR, fact=True)
        op.put("stripe_payouts", p["payout_id"], {**p, "posted": True, "bank_receipt_id": rct})
        op.record(derived_id("spb", p["payout_id"]), "stripe_payout_banked", ACTOR, p["payout_id"],
                  {"payout_id": p["payout_id"], "receipt_id": rct, "amount": M.fmt(amt), "entry_id": e["entry_id"]},
                  f"Stripe payout {M.fmt(amt)} reached the operating account")
        return {"payout_id": p["payout_id"], "entry_id": e["entry_id"]}

    def _stripe_in_transit(self) -> Decimal:
        """Payouts that already left the Stripe balance but have not reached the bank (not yet posted)."""
        return M.total(M.D(p["amount"]) for p in self.db["stripe_payouts"].values()
                       if not p["posted"] and p["status"] in ("pending", "in_transit"))

    def get_stripe_checkout(self, invoice_id: str) -> dict:
        with self.lock:
            if invoice_id not in self.db["invoices"]:
                raise NotFound("no such invoice")
            return {"invoice_id": invoice_id,
                    "sessions": sorted((dict(s) for s in self.db["stripe_sessions"].values()
                                        if s["invoice_id"] == invoice_id), key=lambda s: s["created_at"])}
