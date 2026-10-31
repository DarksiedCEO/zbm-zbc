"""
Backups and restores (red team CH-3; runbook docs/runbooks/restore-and-reimport.md; ADR 0026).

1. A coordinated snapshot. Railway backs up each volume on its own (daily schedules at independent times; manual
   backups whenever asked) and says nothing about consistency across volumes or while a service writes. So:
   ``POST /fin/v1/backup/quiesce`` (Andre) records a snapshot MARKER (the local log head, the journal heads) on the
   evidence ledger and refuses every Finance write for up to N minutes (503 ``QUIESCED``: Stripe and the cron service
   simply retry later). Andre then takes manual backups of every department volume and of the LEDGER LAST, and
   ``POST /fin/v1/backup/resume`` with the marker id ends the window and says whether Finance's log stayed at the marker
   (``consistent``). With the ledger newest, a restore of that set can only be RECONCILABLE, never FATAL (a ledger older
   than a department is the red team's R2, which no reconcile can fix).

2. The restore guard. After a restore the ledger holds evidence the restored Finance log does not. The reconcile plan
   (``GET /fin/v1/reconcile``) now lists ``money_at_risk``: the voidable ledger events that are money (payments,
   invoices, refunds, credit notes, journal entries). Andre's reconcile refuses while any is listed, unless he passes
   ``money_events_acknowledged: true``; then it also opens a ``restore_reimport_required`` break that stays open until
   a Stripe re-import has run for the period. A cleanup that silently forgets paid invoices is not possible.

3. The Stripe re-import (``POST /fin/v1/stripe/reimport``, Andre). Lists the payments, refunds and disputes Stripe
   created in a period (read only) and books every one Finance does not have, through the SAME code a webhook uses
   (idempotent per Stripe object; Compliance asked per payment). A page Finance lost is re-linked from Stripe's own
   Checkout Session (same invoice id, amount and currency) so the payment matches its invoice again. Always a dry run
   first: the real run names the dry run's ``plan_sha256`` and is refused if Stripe's answer changed since.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

import money as M
import reasons as R
from clock import iso
from errors import Conflict, Invalid, NotFound, Unavailable
from intelligences import i02_receivables as I2
from ledger import derived_id
from ports import StripeDispute, StripeList, StripePayment
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha

ACTOR = I2.ACTOR
MAX_REIMPORT_DAYS = 92
# ledger event types that are money: a reconcile that voids any of them needs the re-import
MONEY_EVENT_TYPES = ("journal_entry_posted", "receipt_matched", "receipt_unapplied", "invoice_issued",
                     "invoice_approved", "invoice_voided", "credit_note_issued", "credit_note_refund_linked",
                     "stripe_event_applied", "stripe_refund_booked", "stripe_checkout_created", "dispute_opened",
                     "dispute_closed", "compliance_hold_opened", "client_receipt_created")


class RestoreMixin:

    # ================================================================== 1. quiesce + snapshot marker

    def quiesced(self) -> Optional[dict]:
        q = getattr(self, "_quiesce", None)
        if q is None:
            return None
        if self._now() >= q["until"]:
            self._quiesce = None
            return None
        return q

    def _heads(self) -> dict:
        shas = self.log.line_shas()
        return {"log_head_seq": len(shas), "log_head_sha256": shas[-1] if shas else None,
                "journal_heads": {k: v for k, v in sorted(self.heads.items())}}

    def backup_quiesce(self, request_id: str, minutes: int) -> dict:
        with self.lock:
            if self.quiesced():
                raise Conflict("Finance is already quiesced for a backup; resume it first")
            now = self._now()
            mid = rid("bkp", request_id, iso(now))
            marker = {"marker_id": mid, "requested_at": iso(now), "until": iso(now + timedelta(minutes=minutes)),
                      "status": "quiesced", **self._heads()}
            op = Op(self, f"bkp|{request_id}", "andre", mid)
            op.record(derived_id("bkp", mid), "backup_marker_recorded", "andre", mid,
                      {"marker_id": mid, "log_head_seq": marker["log_head_seq"],
                       "log_head_sha256": marker["log_head_sha256"], "journal_heads": marker["journal_heads"]},
                      f"Backup marker at local log line {marker['log_head_seq']}: Finance writes paused")
            op.put("backup_markers", mid, marker)
            self._commit(op)
            # the marker line itself is the last line a consistent snapshot may hold
            marker = {**marker, **self._heads()}
            self._quiesce = {"marker_id": mid, "until": now + timedelta(minutes=minutes),
                             "log_head_seq": marker["log_head_seq"], "log_head_sha256": marker["log_head_sha256"]}
            return {"marker": marker, "writes_refused_until": marker["until"],
                    "next": "take manual backups of finance-data, legal-data, compliance-data and seo-data, then "
                            "ledger-data LAST; then POST /fin/v1/backup/resume with this marker_id"}

    def backup_resume(self, request_id: str, marker_id: str) -> dict:
        with self.lock:
            m = self.db["backup_markers"].get(marker_id)
            if m is None:
                raise NotFound("no such backup marker")
            q = getattr(self, "_quiesce", None)
            heads = self._heads()
            consistent = q is not None and q["marker_id"] == marker_id and \
                heads["log_head_sha256"] == q["log_head_sha256"] and self._now() < q["until"]
            self._quiesce = None
            op = Op(self, f"bkpr|{request_id}", "andre", marker_id)
            op.record(derived_id("bkpr", marker_id), "backup_marker_closed", "andre", marker_id,
                      {"marker_id": marker_id, "consistent": consistent, "log_head_seq": heads["log_head_seq"]},
                      f"Backup window closed ({'consistent' if consistent else 'NOT consistent: take it again'})")
            op.put("backup_markers", marker_id, {**m, "status": "closed", "closed_at": iso(self._now()),
                                                 "consistent": consistent})
            self._commit(op)
            return {"marker_id": marker_id, "consistent": consistent, "log_head_seq_at_marker": m["log_head_seq"],
                    "detail": "the backup set taken in this window is consistent for Finance" if consistent else
                    "Finance's log moved or the window expired before resume: take the backup set again"}

    # ================================================================== 2. the restore guard

    def money_at_risk(self, void_event_ids) -> list[dict]:
        """The voidable ledger events (a reconcile plan) that are money: never voided without the re-import."""
        ids = set(void_event_ids or ())
        if not ids:
            return []
        try:
            entries = self.recorder.client.entries()
        except Exception:  # noqa: BLE001 - unreadable: every voidable event counts as money (fail closed)
            return [{"event_id": i, "event_type": "unknown"} for i in sorted(ids)]
        out = [{"event_id": e["event_id"], "event_type": e["event_type"], "summary": str(e.get("summary") or "")[:120]}
               for e in entries if e.get("event_id") in ids and e.get("event_type") in MONEY_EVENT_TYPES]
        return sorted(out, key=lambda x: x["event_id"])

    def restore_guard(self, plan: dict, acknowledged: bool) -> None:
        if plan.get("money_at_risk") and not acknowledged:
            raise Conflict("RESTORE_GUARD: this reconcile would declare void ledger evidence of money Finance no "
                           f"longer holds ({len(plan['money_at_risk'])} event(s): payments, invoices, refunds or "
                           "journal entries made after the backup). They are NOT deleted at Stripe: reconcile with "
                           "money_events_acknowledged=true, then run the Stripe re-import for the period "
                           "(docs/runbooks/restore-and-reimport.md). Never treat this as a cleanup.",
                           money_at_risk=plan["money_at_risk"][:50])

    def _restore_reimport_break(self, plan: dict) -> Optional[str]:
        """Committed right after the reconcile line: a break that stays open until a re-import ran (CH-3)."""
        if not plan.get("money_at_risk"):
            return None
        since = self._log_last_day()
        bid = rid("brk", "restore_reimport_required", plan["head_sha256"] or "empty")
        if bid in self.db["breaks"]:
            return bid
        op = Op(self, f"restore|{bid}", "andre", bid)
        op.put("breaks", bid, {"break_id": bid, "leg": "restore_reimport_required", "subject": "zbm:1060",
                               "difference": "0.00", "opened_at": iso(self._now()),
                               "opened_on": self._today_la().isoformat(), "owner": "andre", "severity": "high",
                               "explanation_code": "unknown", "status": "open", "receipt_id": None,
                               "since": since, "money_events": len(plan["money_at_risk"]), "resolution": None,
                               "note": f"restored from a backup: {len(plan['money_at_risk'])} money event(s) after it "
                                       f"were voided; run POST /fin/v1/stripe/reimport (dry run, then real) from "
                                       f"{since} to today"})
        op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                  {"break_id": bid, "leg": "restore_reimport_required", "difference": "0.00"},
                  "Break opened: restored from a backup; Stripe re-import required")
        self._commit(op)
        return bid

    def _log_last_day(self) -> str:
        last = None
        for rec in self.log.iter_records():
            last = rec.get("at") or last
        try:
            d = datetime.fromisoformat(str(last).replace("Z", "+00:00")) if last else self._now()
        except ValueError:
            d = self._now()
        from zoneinfo import ZoneInfo
        return d.astimezone(ZoneInfo(self.cfg.business_tz)).date().isoformat()

    # ================================================================== 3. the Stripe re-import

    def _period(self, since, until) -> tuple[int, int]:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.cfg.business_tz)
        if since > until:
            raise Invalid("since is after until")
        if (until - since).days + 1 > MAX_REIMPORT_DAYS:
            raise Invalid(f"a re-import covers at most {MAX_REIMPORT_DAYS} days; run it in parts")
        if until > self._business_today():
            raise Invalid("until is in the future")
        gte = int(datetime.combine(since, time(0), tz).timestamp())
        lt = int(datetime.combine(until + timedelta(days=1), time(0), tz).timestamp())
        return gte, lt

    def _reimport_plan(self, g: Gather, since, until) -> dict:
        port = self.ports.stripe_in
        gte, lt = self._period(since, until)
        lists = {k: g.call("stripe", "list", (k, gte, lt), lambda k=k: port.list_objects(k, gte, lt),
                           StripeList(False)) for k in ("payment_intents", "refunds", "disputes")}
        if any(not isinstance(v, StripeList) or not v.available for v in lists.values()):
            raise Unavailable("Stripe could not be listed; nothing was planned (retry)", ledger_event_ids=g.events)
        if any(not v.complete for v in lists.values()):
            raise Conflict("more Stripe objects in this period than one re-import reads: run it for a shorter period")
        pis = sorted({i["payment_intent"] for v in lists.values() for i in v.items if i.get("payment_intent")})
        disputes = sorted({i["id"] for i in lists["disputes"].items})
        actions: list[dict] = []
        payments: dict[str, StripePayment] = {}
        for pi in pis:
            p = g.call("stripe", "payment", (pi,), lambda pi=pi: port.payment(pi), StripePayment(False))
            if not p.available:
                raise Unavailable("a Stripe payment could not be read; nothing was planned (retry)",
                                  ledger_event_ids=g.events)
            if not p.found or p.livemode != self.cfg.stripe_livemode:
                continue
            payments[pi] = p
            if p.status != "succeeded" or p.charge_status != "succeeded":
                continue
            rct = rid("rct", "stripe", pi)
            with self.lock:
                rc = self.db["receipts"].get(rct)
                inv = self.db["invoices"].get(p.invoice_id) if p.invoice_id else None
                has_page = inv is not None and any(s["invoice_id"] == inv["invoice_id"]
                                                   for s in self.db["stripe_sessions"].values())
            if rc is None:
                if inv is not None and not has_page:
                    sl = g.call("stripe", "sessions_for_payment", (pi,), lambda pi=pi: port.sessions_for_payment(pi),
                                StripeList(False))
                    if not sl.available:
                        raise Unavailable("Stripe's Checkout pages could not be read; nothing was planned (retry)",
                                          ledger_event_ids=g.events)
                    for s in sl.items:
                        if s["client_reference_id"] == inv["invoice_id"] and s["currency"] == "usd" \
                                and s["livemode"] == self.cfg.stripe_livemode and s["status"] == "complete":
                            actions.append({"action": "relink_page", "session_id": s["id"], "payment_intent": pi,
                                            "invoice_id": inv["invoice_id"],
                                            "amount": f"{s['amount_total'] // 100}.{s['amount_total'] % 100:02d}"
                                            if type(s["amount_total"]) is int and s["amount_total"] >= 0 else None,
                                            "methods": [m for m in s["allowed_payment_method_types"]
                                                        if m in ("card", "us_bank_account")]})
                            break
                actions.append({"action": "book_payment", "payment_intent": pi, "amount": p.amount_received,
                                "invoice_id": p.invoice_id})
            elif p.amount_refunded is not None and M.D(p.amount_refunded) != \
                    M.D((rc.get("stripe") or {}).get("refunded") or "0.00"):
                actions.append({"action": "book_refund", "payment_intent": pi, "refunded_total": p.amount_refunded,
                                "invoice_id": rc.get("invoice_id")})
        for du in disputes:
            d = g.call("stripe", "dispute", (du,), lambda du=du: port.dispute(du), StripeDispute(False))
            if not d.available:
                raise Unavailable("a Stripe dispute could not be read; nothing was planned (retry)",
                                  ledger_event_ids=g.events)
            if not d.found or d.livemode != self.cfg.stripe_livemode:
                continue
            with self.lock:
                rec = self.db["stripe_disputes"].get(du) or {}
            posted = set(rec.get("posted_txns") or [])
            if not rec.get("recorded") or any(t["txn_id"] not in posted for t in d.txns) or rec.get("status") != d.status:
                actions.append({"action": "book_dispute", "dispute_id": du, "payment_intent": d.payment_intent,
                                "status": d.status})
        actions.sort(key=lambda a: ({"relink_page": 0, "book_payment": 1, "book_refund": 2, "book_dispute": 3}
                                    [a["action"]], a.get("payment_intent") or "", a.get("dispute_id") or ""))
        plan = {"since": since.isoformat(), "until": until.isoformat(), "livemode": self.cfg.stripe_livemode,
                "seen": {"payment_intents": len(lists["payment_intents"].items), "refunds": len(lists["refunds"].items),
                         "disputes": len(disputes)}, "actions": actions}
        plan["plan_sha256"] = sha({k: plan[k] for k in ("since", "until", "livemode", "actions")})
        return plan

    def stripe_reimport(self, request_id: str, body: dict) -> dict:
        if not self.cfg.stripe_incoming:
            raise Refused("Stripe is not wired", [R.item("DEPENDENCY_UNAVAILABLE:stripe", "FIN_STRIPE_INCOMING is not set")])
        key, h, ent = self._idem("andre", request_id, "stripe/reimport", body)
        if ent:
            return ent["response"]
        self.require_rules()
        since, until, dry = body["since"], body["until"], body.get("dry_run", True)
        g = Gather(self, f"reimp|{request_id}", "andre", "stripe-reimport")
        with self._stripe_turn("stripe re-import (plan)"):
            plan = self._reimport_plan(g, since, until)
        params = f"{since.isoformat()}|{until.isoformat()}"
        if dry:
            with self.lock:
                iid = rid("imp", params, plan["plan_sha256"])
                op = Op(self, f"reimp|{request_id}", "andre", iid, g)
                op.record(derived_id("imp", iid), "stripe_reimport_planned", "andre", iid,
                          {"import_id": iid, "since": plan["since"], "until": plan["until"],
                           "plan_sha256": plan["plan_sha256"], "actions": len(plan["actions"])},
                          f"Stripe re-import planned (dry run): {len(plan['actions'])} action(s)")
                op.put("stripe_imports", iid, {"import_id": iid, "params": params, "status": "planned",
                                               "plan_sha256": plan["plan_sha256"], "planned_at": iso(self._now()),
                                               "actions": len(plan["actions"])})
                resp = self._idem_add(op, key, h, {"dry_run": True, "import_id": iid, "plan": plan,
                                                   "ledger_event_ids": op.events, "request_id": request_id})
                self._commit(op)
                return resp
        if not body.get("plan_sha256"):
            raise Conflict("a real re-import names the plan_sha256 of its dry run (run with dry_run=true first)")
        with self.lock:
            iid = rid("imp", params, body["plan_sha256"])
            prior = self.db["stripe_imports"].get(iid)
        if prior is None or prior["status"] != "planned":
            raise Conflict("no dry run of this period with that plan_sha256 is waiting: run the dry run first")
        if plan["plan_sha256"] != body["plan_sha256"]:
            raise Conflict("Stripe's record or Finance's books changed since the dry run: dry-run again and review it",
                           plan_sha256_now=plan["plan_sha256"])
        results = []
        for a in plan["actions"]:
            try:
                results.append({**a, "result": self._reimport_one(request_id, a)})
            except (Unavailable, Conflict, Invalid, InvalidReasons, Refused) as exc:
                results.append({**a, "result": "failed", "why": str(getattr(exc, "reason", exc))[:160]})
        failed = [r for r in results if r["result"] == "failed"]
        with self.lock:
            op = Op(self, f"reimp|{request_id}|done", "andre", iid, g)
            op.put("stripe_imports", iid, {**self.db["stripe_imports"][iid], "status": "partial" if failed else "done",
                                           "ran_at": iso(self._now()), "failed": len(failed)})
            op.record(derived_id("impd", iid, request_id), "stripe_reimport_run", "andre", iid,
                      {"import_id": iid, "actions": len(results), "failed": len(failed)},
                      f"Stripe re-import ran: {len(results) - len(failed)} booked or present, {len(failed)} failed")
            closed = []
            if not failed:
                for b in list(self.db["breaks"].values()):
                    if b["status"] != "resolved" and b.get("leg") == "restore_reimport_required" \
                            and (b.get("since") or "9999") >= plan["since"]:
                        op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                         "resolution": {"basis": "stripe_reimport", "import_id": iid}})
                        closed.append(b["break_id"])
            resp = self._idem_add(op, key, h, {"dry_run": False, "import_id": iid, "results": results,
                                               "failed": len(failed), "breaks_resolved": closed,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def _reimport_one(self, request_id: str, a: dict) -> str:
        """One planned action, booked exactly as a webhook would book it (idempotent per Stripe object)."""
        port = self.ports.stripe_in
        with self._stripe_turn("stripe re-import"):
            g = Gather(self, f"reimp1|{request_id}|{a['action']}|{a.get('payment_intent') or a.get('dispute_id')}",
                       ACTOR, a.get("payment_intent") or a.get("dispute_id"))
            if a["action"] == "relink_page":
                with self.lock:
                    if a["session_id"] in self.db["stripe_sessions"]:
                        return "present"
                    inv = self.db["invoices"].get(a["invoice_id"])
                    if inv is None:
                        return "skipped_no_invoice"
                    op = Op(self, g.op_id, ACTOR, a["invoice_id"], g)
                    op.put("stripe_sessions", a["session_id"], {
                        "session_id": a["session_id"], "invoice_id": a["invoice_id"], "url": None, "expires_at": None,
                        "methods": a["methods"], "total": a["amount"], "status": "complete",
                        "payment_intent": a["payment_intent"], "created_by": "stripe_reimport",
                        "created_at": iso(self._now()), "restored_from_stripe": True})
                    op.record(derived_id("relink", a["session_id"]), "stripe_checkout_relinked", "andre",
                              a["invoice_id"], {"invoice_id": a["invoice_id"], "session_id": a["session_id"]},
                              "A Stripe page Finance lost was re-linked from Stripe (re-import)")
                    for b in list(self.db["breaks"].values()):
                        if b.get("leg") == "stripe_unknown_session" and b.get("session_id") == a["session_id"] \
                                and b["status"] != "resolved":
                            op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                             "resolution": {"basis": "stripe_reimport_relinked"}})
                    self._commit(op)
                    return "relinked"
            if a["action"] in ("book_payment", "book_refund"):
                return self._reimport_payment(g, a["payment_intent"])
            d = g.call("stripe", "dispute", (a["dispute_id"],), lambda: port.dispute(a["dispute_id"]),
                       StripeDispute(False))
            if not d.available:
                raise Unavailable("Stripe could not be read")
            with self.lock:
                booked = d.payment_intent and rid("rct", "stripe", d.payment_intent) in self.db["receipts"]
            if d.payment_intent and not booked:
                # a dispute is never booked before its payment (AEGIS H2): the payment first, in this same turn
                self._reimport_payment(Gather(self, f"{g.op_id}|payment", ACTOR, d.payment_intent), d.payment_intent)
            with self.lock:
                op = Op(self, g.op_id, ACTOR, a["dispute_id"], g)
                try:
                    status = self._stripe_dispute(op, d)
                except PostingRefused as exc:
                    raise InvalidReasons("the re-imported dispute could not be posted", exc.reasons) from None
                self._commit(op)
                return status

    def _reimport_payment(self, g: Gather, pi: str) -> str:
        """Book one payment (and any refund on it) exactly as its webhook would; the caller holds the Stripe turn."""
        port = self.ports.stripe_in
        p = g.call("stripe", "payment", (pi,), lambda: port.payment(pi), StripePayment(False))
        if not p.available:
            raise Unavailable("Stripe could not be read")
        check = self._payment_activation(g, p)
        with self.lock:
            op = Op(self, g.op_id, ACTOR, pi, g)
            op.compliance_check = check
            try:
                status = self._stripe_payment(op, p)
            except PostingRefused as exc:
                raise InvalidReasons("the re-imported payment could not be posted", exc.reasons) from None
            self._commit(op)
            return status
