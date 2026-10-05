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
  F13p  payout paid           Dr 1010 Cash - Operating             Cr 1060
  F13q  payout failed         Dr 1060                              Cr 1010

The money date for the media hold (founder M3/M11) is the day Finance first sees the payment succeeded, never
earlier than Stripe's: a late webhook only makes the hold longer.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
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
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha_text

ACTOR = I2.ACTOR
STRIPE_DEP = "DEPENDENCY_UNAVAILABLE:stripe"
CHECKOUT_TTL = timedelta(hours=23)                # Stripe allows 30 min .. 24 h; an hour of margin for clock skew
MIN_CHARGE = Decimal("0.50")                      # Stripe's USD minimum
MAX_CHARGE = Decimal("999999.99")                 # eight digits of cents
MAX_PAYLOAD = 256 * 1024
_SID = re.compile(r"[a-z]{2,8}_[A-Za-z0-9_]{1,120}")
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

    def stripe_checkout(self, principal: str, request_id: str, invoice_id: str) -> dict:
        key, h, ent = self._idem(principal, request_id, f"stripe-checkout/{invoice_id}", invoice_id)
        if ent:
            return ent["response"]
        self.require_rules()
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
                raise Refused("Stripe is not wired", [R.item(STRIPE_DEP, "FIN_STRIPE_INCOMING is not set: no Stripe "
                                                                         "account is connected")])
            for s in self.db["stripe_sessions"].values():   # one live page per invoice: hand back the open one
                if s["invoice_id"] == invoice_id and s["status"] == "open" and s["methods"] == list(methods) \
                        and s["total"] == inv["total"] and s["expires_at"] > iso(now + timedelta(minutes=30)):
                    return {"checkout": dict(s), "reused": True, "ledger_event_ids": [], "request_id": request_id}
            attempt = sum(1 for s in self.db["stripe_sessions"].values() if s["invoice_id"] == invoice_id)
            total = inv["total"]
        idem_key = rid("chk", invoice_id, attempt, total, list(methods))
        label = f"Z Best Media invoice {invoice_id}"
        expires = int((now + CHECKOUT_TTL).timestamp())
        port = self.ports.stripe_in
        g = Gather(self, f"chk|{principal}|{request_id}", ACTOR, invoice_id)
        ans = g.call("stripe", "create_checkout", (invoice_id, total, list(methods), idem_key),
                     lambda: port.create_checkout(invoice_id, total, methods, idem_key, expires, label),
                     StripeCheckout("unavailable"))
        with self.lock:
            inv = self.db["invoices"][invoice_id]
            if inv["status"] != "issued" or inv["total"] != total:
                raise Conflict("the invoice changed while the checkout was being made; ask again")
            if ans.outcome != "created":
                raise Refused("Stripe did not make the checkout page", [R.item(
                    STRIPE_DEP, f"Stripe answered {ans.outcome}: {ans.reason}"[:200])], ledger_event_ids=g.events)
            existing = self.db["stripe_sessions"].get(ans.session_id)
            if existing is not None:                 # Stripe replayed the same idempotency key
                return {"checkout": dict(existing), "reused": True, "ledger_event_ids": g.events,
                        "request_id": request_id}
            rec = {"session_id": ans.session_id, "invoice_id": invoice_id, "url": ans.url,
                   "expires_at": ans.expires_at, "methods": list(methods), "total": total, "status": "open",
                   "created_by": principal, "created_at": iso(now)}
            op = Op(self, f"chk|{principal}|{request_id}", ACTOR, invoice_id, g)
            op.put("stripe_sessions", ans.session_id, rec)
            op.record(derived_id("chk", ans.session_id), "stripe_checkout_created", ACTOR, invoice_id,
                      {"invoice_id": invoice_id, "session_id": ans.session_id, "total": total,
                       "methods": list(methods)}, f"Stripe checkout made for an invoice ({total})")
            resp = self._idem_add(op, key, h, {"checkout": rec, "reused": False, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    # ================================================================== webhook events

    def stripe_event(self, principal: str, request_id: str, payload: str, signature: str) -> dict:
        if not self.cfg.stripe_incoming:
            raise Refused("Stripe is not wired", [R.item(STRIPE_DEP, "FIN_STRIPE_INCOMING is not set")])
        if len(payload.encode("utf-8")) > MAX_PAYLOAD:
            raise Invalid("Stripe event body too large")
        self.require_rules()
        port = self.ports.stripe_in
        now = self._now()
        g = Gather(self, f"sev|{principal}|{request_id}", ACTOR, "stripe")
        ok = g.call("stripe", "verify_event", (sha_text(payload), sha_text(signature or "")),
                    lambda: port.verify_event(payload, signature, int(now.timestamp())), False) is True
        if not ok:
            raise Refused("Stripe event signature not verified", [R.item(
                STRIPE_DEP, "the Stripe-Signature did not verify (wrong secret, stale or altered body)")],
                ledger_event_ids=g.events)
        ev = parse_event(payload)
        with self.lock:
            if ev["event_id"] in self.db["stripe_events"]:
                return {"event_id": ev["event_id"], "status": "duplicate", "ledger_event_ids": g.events}
        kind = event_kind(ev["type"]) if ev["livemode"] == self.cfg.stripe_livemode else None
        truth: dict = {}
        if kind == "session":
            truth["session"] = s = g.call("stripe", "session", (ev["object_id"],),
                                          lambda: port.session(ev["object_id"]), StripeSession(False))
            if s.available and s.found and s.payment_intent:
                truth["payment"] = g.call("stripe", "payment", (s.payment_intent,),
                                          lambda: port.payment(s.payment_intent), StripePayment(False))
        elif kind == "payment" or (kind == "charge" and ev["payment_intent"]):
            pi = ev["object_id"] if kind == "payment" else ev["payment_intent"]
            truth["payment"] = g.call("stripe", "payment", (pi,), lambda: port.payment(pi), StripePayment(False))
        elif kind == "dispute":
            truth["dispute"] = g.call("stripe", "dispute", (ev["object_id"],),
                                      lambda: port.dispute(ev["object_id"]), StripeDispute(False))
        elif kind == "payout":
            truth["payout"] = g.call("stripe", "payout", (ev["object_id"],),
                                     lambda: port.payout(ev["object_id"]), StripePayout(False))
        if any(not a.available for a in truth.values()):
            # nothing is recorded as handled: the gateway answers Stripe non-2xx and Stripe sends the event again
            raise Unavailable("Stripe could not be read back; the event will be retried", ledger_event_ids=g.events)
        if any(a.found and a.livemode != self.cfg.stripe_livemode for a in truth.values()):
            raise Invalid("Stripe answered from the other mode (test vs live)")
        with self.lock:
            if ev["event_id"] in self.db["stripe_events"]:
                return {"event_id": ev["event_id"], "status": "duplicate", "ledger_event_ids": g.events}
            op = Op(self, f"sev|{ev['event_id']}", ACTOR, ev["event_id"], g)
            try:
                if ev["livemode"] != self.cfg.stripe_livemode:
                    status = "ignored_other_mode"
                elif kind is None:
                    status = "ignored_type"
                elif kind == "session":
                    status = self._stripe_session(op, truth["session"], truth.get("payment"))
                elif "payment" in truth:
                    status = self._stripe_payment(op, truth["payment"])
                elif kind == "dispute":
                    status = self._stripe_dispute(op, truth["dispute"])
                elif kind == "payout":
                    status = self._stripe_payout(op, truth["payout"])
                else:
                    status = "ignored_no_payment"
            except PostingRefused as exc:
                raise InvalidReasons("the Stripe event could not be posted", exc.reasons) from None
            op.put("stripe_events", ev["event_id"], {"event_id": ev["event_id"], "type": ev["type"],
                                                    "object_id": ev["object_id"], "status": status,
                                                    "at": iso(self._now())})
            op.record(derived_id("sev", ev["event_id"]), "stripe_event_applied", ACTOR, ev["event_id"],
                      {"event_id": ev["event_id"], "type": ev["type"], "object_id": ev["object_id"],
                       "status": status}, f"Stripe event {ev['type']}: {status}"[:200])
            resp = {"event_id": ev["event_id"], "status": status, "ledger_event_ids": op.events}
            self._commit(op)
            return resp

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
            return "payment_already_booked"
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
                                            "balance_txn": p.balance_txn, "fee": M.fmt(fee),
                                            "fee_entry_id": fee_entry["entry_id"] if fee_entry else None}})
        op.record(derived_id("rct", rct), "receipt_matched" if ok else "receipt_unapplied", ACTOR, rct,
                  {"receipt_id": rct, "entity": "zbm", "account": "1060", "amount": M.fmt(gross),
                   "invoice_id": inv["invoice_id"] if ok else None, "fee": M.fmt(fee)},
                  f"Stripe payment {status}: {M.fmt(gross)} (fee {M.fmt(fee)})")
        if ok:
            for s in self.db["stripe_sessions"].values():
                if s["invoice_id"] == inv["invoice_id"] and s["status"] == "open":
                    op.put("stripe_sessions", s["session_id"], {**s, "status": "invoice_paid"})
            self._client_receipt(op, rct, inv, M.fmt(gross), value_date, METHOD_LABEL.get(p.method, "Stripe"))
        return status

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
        debit = J.dr("1100", amt, f"client:{inv['client_id']}") if (inv and rc["status"] == "matched") \
            else J.dr("2070", amt)
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
        if inv and rc["status"] == "matched":
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
        matched = rc is not None and rc["status"] == "matched" and inv is not None
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
        if held > 0 and not rec["flagged"]:
            rec["flagged"] = True
            if matched and inv["kind"] == I2.MEDIA_KIND:
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
        closing = d.status in DONE_DISPUTE and not rec["finalized"]
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
                if matched:
                    if held == M.D(inv["total"]):
                        op.put("invoices", inv["invoice_id"], {**inv, "status": "issued", "paid_at": None})
                        op.put("receipts", rct, {**rc, "status": "charged_back"})
                        self._withdraw_client_receipt(op, rct)
                        if inv["kind"] == I2.MEDIA_KIND:
                            self._media_unpaid(op, inv, rct, "the client took back through a Stripe dispute")
                    else:
                        self._unapplied_break(op, rct, held, "partial dispute lost: the invoice is part paid; Andre "
                                                             "re-bills", key=f"{rct}|{did}")
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
                                                         "reversed": False}
        amt = M.D(po.amount)
        src = {"kind": "stripe_payout", "id": po.payout_id}
        if rec.get("amount") not in (None, po.amount):
            raise Invalid("Stripe reported a different amount for a payout already recorded")
        if po.status == "paid" and not rec["posted"]:
            self._post(op, "zbm", [J.dr("1010", amt), J.cr("1060", amt)], "F13p", src, f"F13p|{po.payout_id}",
                       actor=ACTOR, fact=True)
            rec = {**rec, "posted": True}
        elif po.status == "failed" and rec["posted"] and not rec["reversed"]:
            self._post(op, "zbm", [J.dr("1060", amt), J.cr("1010", amt)], "F13q", src, f"F13q|{po.payout_id}",
                       actor=ACTOR, fact=True)
            rec = {**rec, "reversed": True}
        rec = {**rec, "status": po.status, "amount": po.amount, "seen_at": iso(self._now())}
        op.put("stripe_payouts", po.payout_id, rec)
        return f"payout_{po.status}"

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
