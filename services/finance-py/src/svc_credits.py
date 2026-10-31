"""
Invoice void and credit notes (ADR 0026; MSA 4.10 and 4.12(g); refund policy sections 5 and 8; red team OPS-H9).

Andre-only. Two things the refund policy promises and Finance could not do before:

- **Void** an ISSUED invoice nobody has paid on: "we cancel the invoice in full". The open Stripe pages of the invoice
  are closed at Stripe FIRST (a page Stripe does not confirm closed, or one the client already paid on, refuses the
  void), then F11v reverses what is still owed: Dr revenue (4110, or 2110 deferred revenue for a retainer) / Cr 1100
  A/R. The invoice becomes ``void``.
- **Credit note** against an issued or paid invoice, partial or full: "a credit note that reduces it to the amount
  Client owes" (MSA 4.12(g)) and "a corrected invoice or a refund" (policy 8.4). F11c posts Dr revenue for the amount
  and splits the credit:
    * the part the client still owes on paper (A/R open on this invoice) -> Cr 1100 A/R[client];
    * the part the client already PAID -> Cr 2050 Refunds payable[client]: a refund ZBM now owes, due within 10
      business days (MSA 4.10). A break tracks it until the refund is booked.
  The journal balances by construction (one debit, credits that sum to it).

Refunds link to credit notes: a refund booked from Stripe (``charge.refunded``, the Stripe re-import) on a paid
invoice is paid out of 2050 for the part a pending credit note promised (Dr 2050 / Cr 1060), oldest credit note
first; only the rest raises A/R again (the pre-existing F7r path, with its break). A credit note issued AFTER a Stripe
refund absorbs that refund (Cr 1100 against the A/R the refund raised) and records the link. A failed refund
(``_stripe_refund_reversed``) on an invoice with credit notes is never booked automatically: a break tells Andre.

Refused: ZBC invoices and media prepayments (cancel the media buy instead), drafts (reject them), invoices with a
Compliance hold (release or settle the hold first), invoices with a dispute or chargeback, and any credit that would
take more than the invoice's total or (for money already paid) more than the money ZBM still holds. A credit note that
creates a refund asks Compliance first: a client refused on SANCTIONS grounds is never promised a refund (31 CFR
501.603: blocked property is reported, not returned); no answer from Compliance refuses (fail closed).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import money as M
import reasons as R
from clock import iso
from errors import Conflict, Invalid, NotFound
from intelligences import i01_journal as J
from intelligences import i02_receivables as I2
from ledger import derived_id
from service import Gather, InvalidReasons, Op, PostingRefused, Refused, rid, sha_text

ACTOR = I2.ACTOR
REFUND_DUE_BUSINESS_DAYS = 10            # MSA 4.10 / refund policy 9
PAID_RECEIPT = ("matched", "partially_refunded", "refunded")


def add_business_days(day: date, n: int) -> date:
    """``n`` business days after ``day`` (Monday to Friday; US bank holidays are NOT skipped, so the date is never
    later than the contract's -- Andre may be a day early, never late)."""
    d = day
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def credit_note_entry_id(cnid: str) -> str:
    return rid("je", "zbm", f"F11c|{cnid}")


def credit_note_payload(cnid, number, iid, amount, to_refund, reason, due_from) -> dict:
    return {"credit_note_id": cnid, "number": number, "invoice_id": iid, "amount": amount,
            "to_ar": M.fmt(M.D(amount) - M.D(to_refund)), "to_refund": to_refund,
            "entry_id": credit_note_entry_id(cnid), "reason_code": reason, "refund_due_from": due_from}


CN_SUMMARY = re.compile(r"Credit note (CN-\d{4}-\d{4,}) (fin-cn-[0-9A-Z]{26}) on (fin-inv-[0-9A-Z]{26}): "
                        r"(\d+\.\d{2}), refund (\d+\.\d{2}) due from (\d{4}-\d{2}-\d{2}|-), ([a-z_]+), line (\d+)")
VOID_SUMMARY = re.compile(r"Invoice (fin-inv-[0-9A-Z]{26}) voided: (\d+\.\d{2}) no longer owed, ([a-z_]+), line (\d+)")


def credit_note_summary(cnid, number, iid, amount, to_refund, reason, due_from, seq) -> str:
    return (f"Credit note {number} {cnid} on {iid}: {amount}, refund {to_refund} due from {due_from or '-'}, "
            f"{reason}, line {seq}")


def void_payload(iid, amount, reason) -> dict:
    return {"invoice_id": iid, "amount": amount, "entry_id": rid("je", "zbm", f"F11v|{iid}")
            if M.D(amount) > 0 else None, "reason_code": reason}


def void_summary(iid, amount, reason, seq) -> str:
    return f"Invoice {iid} voided: {amount} no longer owed, {reason}, line {seq}"


class CreditsMixin:

    # ================================================================== reads

    def _paid_gross(self, invoice_id: str, op: Optional[Op] = None) -> Decimal:
        """What the client paid on this invoice and Finance still counts as paid (refunds are tracked separately)."""
        recs = dict(self.db["receipts"])
        if op is not None:
            recs.update(op.staged.get("receipts", {}))
        return M.total(M.D(r["amount"]) for r in recs.values()
                       if r.get("invoice_id") == invoice_id and r.get("status") in PAID_RECEIPT)

    @staticmethod
    def _credit_account(inv: dict) -> tuple[str, Optional[str]]:
        """The account F11 credited when the invoice was issued (what a credit note or void debits back)."""
        if inv["kind"] == "retainer":
            return "2110", f"client:{inv['client_id']}"
        return "4110", None

    def _credit_state(self, inv: dict, op: Optional[Op] = None) -> dict:
        """The invoice's numbers for a credit: total, credited so far, A/R still open on it, paid, refunded, the refund
        still owed, and what ZBM still holds of the client's money that no credit note has promised back yet."""
        total = M.D(inv["total"])
        credited = M.D(inv.get("credited") or "0.00")
        credited_ar = M.D(inv.get("credited_ar") or "0.00")
        paid = self._paid_gross(inv["invoice_id"], op)
        refunded = M.D(inv.get("refunded") or "0.00")
        via_credit = M.D(inv.get("refunded_via_credit") or "0.00")
        refund_due = M.D(inv.get("refund_due") or "0.00")
        ar_open = M.q(total - credited_ar - paid + (refunded - via_credit))
        return {"total": total, "credited": credited, "creditable": M.q(total - credited), "ar_open": max(ar_open, M.ZERO),
                "paid": paid, "refunded": refunded, "refund_due": refund_due,
                "refundable": max(M.q(paid - refunded - refund_due), M.ZERO)}

    def get_credit_note(self, credit_note_id: str) -> dict:
        with self.lock:
            cn = self.db["credit_notes"].get(credit_note_id)
            if cn is None:
                raise NotFound("no such credit note")
            return dict(cn)

    def invoice_credit_notes(self, invoice_id: str) -> dict:
        with self.lock:
            inv = self.db["invoices"].get(invoice_id)
            if inv is None:
                raise NotFound("no such invoice")
            notes = [dict(self.db["credit_notes"][c]) for c in inv.get("credit_note_ids") or []
                     if c in self.db["credit_notes"]]
            st = self._credit_state(inv)
            return {"invoice_id": invoice_id, "status": inv["status"], "total": inv["total"],
                    "amount_due": M.fmt(I2.amount_due(inv)) if inv["status"] == "issued" else "0.00",
                    "credited": M.fmt(st["credited"]), "refund_due": M.fmt(st["refund_due"]), "credit_notes": notes}

    # ================================================================== shared checks

    def _credit_refusals(self, inv: dict, what: str) -> None:
        if inv["entity"] != "zbm":
            raise Conflict(f"a {what} is for ZBM invoices; ZBC campaign deposits are refunded by the campaign refund "
                           "flow (POST /fin/v1/refunds/{campaign})")
        if inv["kind"] == I2.MEDIA_KIND:
            raise Conflict(f"a media prepayment is cancelled through its media buy (POST /fin/v1/media-buys/{{id}}/"
                           f"cancel), never by a {what}")
        if inv["status"] == "draft":
            raise Conflict("a draft is not an invoice yet: reject it (POST /fin/v1/invoices/{id}/decision)")
        if inv.get("held_compliance"):
            raise Conflict(f"this invoice carries a Compliance hold: no {what} until the hold is released or settled "
                           "(runbook, Compliance hold steps)")
        rcts = {k for k, r in self.db["receipts"].items() if r.get("invoice_id") == inv["invoice_id"]}
        if M.D(inv.get("charged_back") or "0.00") > 0 or any(
                d.get("receipt_id") in rcts and (d.get("status") not in ("won", "warning_closed", "prevented")
                                                 or M.D(d.get("held") or "0.00") > 0)
                for d in self.db["stripe_disputes"].values()):
            raise Conflict(f"this invoice has a dispute or chargeback: settle it in Stripe first; no {what} meanwhile")
        for r in self.db["receipts"].values():
            if r.get("invoice_id") == inv["invoice_id"] and r.get("status") in ("returned", "charged_back"):
                raise Conflict(f"a payment on this invoice was returned or charged back: no {what} until it is settled")

    def _close_invoice_pages(self, g: Gather, op_id: str, invoice_id: str) -> None:
        """Every open Stripe page of the invoice is closed AT STRIPE before its amount changes (ADR 0026): a page the
        client already paid on refuses (book the payment first, then credit the paid invoice), and a page Stripe does
        not confirm closed refuses (nothing changed; retry)."""
        with self.lock:
            open_ = sorted(s["session_id"] for s in self.db["stripe_sessions"].values()
                           if s["invoice_id"] == invoice_id and s["status"] == "open")
        if not open_:
            return
        with self._stripe_turn("close an invoice's pages before a credit"):
            res = {sid: self._expire_page(g, sid) for sid in open_}
            closed = [sid for sid, a in res.items() if a.available and a.found and a.status == "expired"]
            paid = [sid for sid, a in res.items() if a.available and a.found and a.status == "complete"]
            if closed or paid:
                with self.lock:
                    op = Op(self, f"{op_id}|pages", ACTOR, invoice_id, g)
                    for sid in closed + paid:
                        a = res[sid]
                        op.put("stripe_sessions", sid, {**self.db["stripe_sessions"][sid], "status": a.status,
                                                        "expired_at": iso(self._now()),
                                                        **({"payment_intent": a.payment_intent} if a.payment_intent
                                                           else {})})
                    self._commit(op)
        if paid:
            raise Conflict("the client has paid (or is paying) on a Stripe page of this invoice: Finance books the "
                           "payment when Stripe confirms it; then credit the paid invoice")
        if len(closed) < len(open_):
            raise Refused("an open Stripe page of this invoice could not be closed; nothing was changed",
                          [R.item("DEPENDENCY_UNAVAILABLE:stripe", "Stripe did not confirm the page closed; retry")],
                          ledger_event_ids=g.events)

    # ================================================================== void

    def void_invoice(self, request_id: str, invoice_id: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"invoices/{invoice_id}/void", body)
        if ent:
            return ent["response"]
        self.require_rules()
        with self.lock:
            inv = self.db["invoices"].get(invoice_id)
            if inv is None:
                raise NotFound("no such invoice")
            self._void_checks(inv, body)
        g = Gather(self, f"void|{request_id}", "andre", invoice_id)
        self._close_invoice_pages(g, f"void|{request_id}", invoice_id)
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"invoices/{invoice_id}/void", body)
            if ent:
                return ent["response"]
            inv = self.db["invoices"][invoice_id]
            self._void_checks(inv, body)
            if any(s["invoice_id"] == invoice_id and s["status"] == "open" for s in self.db["stripe_sessions"].values()):
                raise Conflict("a Stripe page of this invoice was opened meanwhile; void again")
            due = I2.amount_due(inv)
            acct, sub = self._credit_account(inv)
            op = Op(self, f"void|{request_id}", "andre", invoice_id, g)
            entry_id = None
            if due > 0:
                try:
                    e = self._post(op, "zbm", [J.dr(acct, due, sub), J.cr("1100", due, f"client:{inv['client_id']}")],
                                   "F11v", {"kind": "invoice_void", "id": invoice_id}, f"F11v|{invoice_id}",
                                   approval_ref=request_id)
                except PostingRefused as exc:
                    raise InvalidReasons("void posting refused", exc.reasons) from None
                entry_id = e["entry_id"]
            now = iso(self._now())
            new = {**inv, "status": "void", "voided_at": now, "voided_by": "andre", "void_entry_id": entry_id,
                   "void_reason": body["reason_code"], "decided_at": inv.get("decided_at") or now,
                   "credited": M.fmt(M.D(inv.get("credited") or "0.00") + due),
                   "credited_ar": M.fmt(M.D(inv.get("credited_ar") or "0.00") + due)}
            if body.get("note"):
                new["void_note_sha256"] = sha_text(body["note"])
            # AEGIS F2: the ledger event carries everything a restore needs to rebuild the void (svc_restore)
            op.record(derived_id("invv", invoice_id), "invoice_voided", "andre", invoice_id,
                      void_payload(invoice_id, M.fmt(due), body["reason_code"]),
                      void_summary(invoice_id, M.fmt(due), body["reason_code"], len(self.log) + 1))
            op.put("invoices", invoice_id, new)
            self._resolve_deadlines(op, "cancel_or_credit_due", invoice_id, "voided")
            self._enqueue_client_mail(op, "invoice_cancelled", inv["client_id"], invoice_id,
                                      {"invoice_id": invoice_id, "amount_cancelled": M.fmt(due),
                                       "cancelled_on": self._business_today().isoformat()})
            resp = self._idem_add(op, key, h, {"invoice": new, "entry_id": entry_id, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def _void_checks(self, inv: dict, body: dict) -> None:
        self._credit_refusals(inv, "void")
        self._restore_refusal("void")
        if inv["status"] == "paid":
            raise Conflict("this invoice is paid: it is not voided; issue a credit note instead (it records the refund "
                           "ZBM owes)")
        if inv["status"] != "issued":
            raise Conflict(f"only an issued invoice can be voided; this one is {inv['status']}")
        if body["content_sha256"] != inv["content_sha256"]:
            raise Conflict("invoice changed since you read it (content_sha256 mismatch)")
        if any(r.get("invoice_id") == inv["invoice_id"] for r in self.db["receipts"].values()) \
                or M.D(inv.get("refunded") or "0.00") > 0 or inv.get("paid_at"):
            raise Conflict("money was paid on this invoice: it is not voided; issue a credit note instead (it records "
                           "the refund ZBM owes)")

    # ================================================================== credit notes

    def issue_credit_note(self, request_id: str, invoice_id: str, body: dict) -> dict:
        key, h, ent = self._idem("andre", request_id, f"invoices/{invoice_id}/credit-notes", body)
        if ent:
            return ent["response"]
        self.require_rules()
        amount = M.D(body["amount"])
        with self.lock:
            inv = self.db["invoices"].get(invoice_id)
            if inv is None:
                raise NotFound("no such invoice")
            split = self._credit_checks(inv, body, amount)
        g = Gather(self, f"cn|{request_id}", "andre", invoice_id)
        if split["to_refund"] > 0:
            # a refund will be owed: Compliance is asked now (outside the lock); sanctions or no answer refuses
            act = self._activation(g, inv["client_id"])
            if not act.available:
                raise Refused("no credit note that owes a refund while Compliance (38) cannot answer for the client",
                              self._activation_reasons(act, full=True), ledger_event_ids=g.events)
            if act.sanctions:
                raise Refused("the client is refused on SANCTIONS grounds: money it paid is not refunded (31 CFR "
                              "501.603); report it to OFAC and follow the runbook's sanctions steps",
                              self._activation_reasons(act, full=True), ledger_event_ids=g.events)
        if inv["status"] == "issued":
            self._close_invoice_pages(g, f"cn|{request_id}", invoice_id)
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"invoices/{invoice_id}/credit-notes", body)
            if ent:
                return ent["response"]
            inv = self.db["invoices"][invoice_id]
            split = self._credit_checks(inv, body, amount)
            if inv["status"] == "issued" and any(s["invoice_id"] == invoice_id and s["status"] == "open"
                                                 for s in self.db["stripe_sessions"].values()):
                raise Conflict("a Stripe page of this invoice was opened meanwhile; issue the credit note again")
            op = Op(self, f"cn|{request_id}", "andre", invoice_id, g)
            resp = self._book_credit_note(op, inv, amount, split, body, request_id)
            resp = self._idem_add(op, key, h, {**resp, "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def _credit_checks(self, inv: dict, body: dict, amount: Decimal) -> dict:
        self._credit_refusals(inv, "credit note")
        self._restore_refusal("credit note")
        if inv["status"] not in ("issued", "paid", "credited"):
            raise Conflict(f"a credit note is issued against an issued or paid invoice; this one is {inv['status']}")
        if body["content_sha256"] != inv["content_sha256"]:
            raise Conflict("invoice changed since you read it (content_sha256 mismatch)")
        st = self._credit_state(inv)
        if amount > st["creditable"]:
            raise Conflict(f"the credit ({M.fmt(amount)}) is more than what is left to credit on this invoice "
                           f"({M.fmt(st['creditable'])})")
        to_ar = min(amount, st["ar_open"])
        to_refund = M.q(amount - to_ar)
        if to_refund > st["refundable"]:
            raise Conflict(f"the credit would owe a refund of {M.fmt(to_refund)}, more than the client's money ZBM "
                           f"still holds on this invoice ({M.fmt(st['refundable'])})")
        due_from = body.get("refund_due_from")
        if to_refund > 0:
            # AEGIS F4: MSA 4.10 counts the 10 business days from the day the refund BECAME due (the SOW's end, the
            # agreement on an invoice dispute, ...), which Andre states; never from the day he issues the credit note
            if due_from is None:
                raise Conflict("this credit note owes a refund: say the day the refund became due under MSA 4.10 "
                               "(refund_due_from); the 10 business days count from it")
            if due_from > self._business_today():
                raise Invalid("refund_due_from is in the future")
        return {"to_ar": M.q(to_ar), "to_refund": to_refund, "state": st,
                "refund_due_from": due_from if to_refund > 0 else None}

    def _next_credit_note_number(self) -> str:
        """Strictly increasing: one more than the highest number this Finance holds (after a restore, the re-import
        rebuilds every credit note from the ledger before a new one can be issued: AEGIS F2)."""
        year = self._business_today().year
        top = 0
        for c in self.db["credit_notes"].values():
            m = re.fullmatch(r"CN-(\d{4})-(\d{4,})", c.get("number") or "")
            if m and int(m.group(1)) == year:
                top = max(top, int(m.group(2)))
        return f"CN-{year}-{top + 1:04d}"

    def _book_credit_note(self, op: Op, inv: dict, amount: Decimal, split: dict, body: dict, request_id: str, *,
                          cnid: Optional[str] = None, number: Optional[str] = None, rebuilt: bool = False) -> dict:
        iid, cid = inv["invoice_id"], inv["client_id"]
        cnid = cnid or rid("cn", iid, request_id)
        number = number or self._next_credit_note_number()
        acct, sub = self._credit_account(inv)
        to_ar, to_refund = split["to_ar"], split["to_refund"]
        lines = [J.dr(acct, amount, sub)]
        if to_ar > 0:
            lines.append(J.cr("1100", to_ar, f"client:{cid}"))
        if to_refund > 0:
            lines.append(J.cr("2050", to_refund, f"client:{cid}"))
        try:
            e = self._post(op, "zbm", lines, "F11c", {"kind": "credit_note", "id": cnid}, f"F11c|{cnid}",
                           approval_ref=request_id)
        except PostingRefused as exc:
            raise InvalidReasons("credit note posting refused", exc.reasons) from None
        today = self._business_today()
        due_from = split.get("refund_due_from")
        due_by = add_business_days(date.fromisoformat(str(due_from)), REFUND_DUE_BUSINESS_DAYS) \
            if to_refund > 0 and due_from else None
        late = due_by is not None and due_by < today
        # a credit note issued after a Stripe refund absorbs that refund (the A/R it raised): linked, oldest first
        refund_log = [dict(x) for x in inv.get("refund_log") or []]
        absorbed, links = to_ar, []
        for x in refund_log:
            free = M.D(x.get("ar_unlinked") or "0.00")
            if absorbed <= 0 or free <= 0:
                continue
            take = min(free, absorbed)
            x["ar_unlinked"] = M.fmt(free - take)
            x["credit_note_ids"] = list(x.get("credit_note_ids") or []) + [cnid]
            links.append({"entry_id": x["entry_id"], "amount": M.fmt(take), "booked": "before_credit_note"})
            absorbed = M.q(absorbed - take)
        if links and all(M.D(x.get("ar_unlinked") or "0.00") == 0 for x in refund_log):
            # AEGIS M2b: every Stripe refund on this invoice is now accounted for by a credit note: the "refunded at
            # Stripe with no credit note" breaks are answered
            rcts = {k for k, r in self.db["receipts"].items() if r.get("invoice_id") == iid}
            for b in list(self.db["breaks"].values()):
                if b.get("leg") == "stripe_refund" and b.get("receipt_id") in rcts and b["status"] != "resolved":
                    op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                     "resolution": {"basis": "credit_note", "credit_note_id": cnid}})
        cn = {"credit_note_id": cnid, "number": number, "invoice_id": iid, "client_id": cid, "entity": "zbm",
              "amount": M.fmt(amount), "to_ar": M.fmt(to_ar), "to_refund": M.fmt(to_refund),
              "refund_remaining": M.fmt(to_refund), "refunds": links, "reason_code": body["reason_code"],
              "note_sha256": sha_text(body["note"]) if body.get("note") else None, "entry_id": e["entry_id"],
              "issued_at": iso(self._now()), "issued_on": today.isoformat(), "issued_by": "andre",
              "refund_due_from": str(due_from) if due_by else None,
              "refund_due_by": due_by.isoformat() if due_by else None,
              "status": "refund_pending" if to_refund > 0 else "applied", "break_id": None,
              **({"rebuilt_from_ledger": True} if rebuilt else {})}
        new = {**inv, "credited": M.fmt(split["state"]["credited"] + amount),
               "credited_ar": M.fmt(M.D(inv.get("credited_ar") or "0.00") + to_ar),
               "refund_due": M.fmt(split["state"]["refund_due"] + to_refund),
               "credit_note_ids": list(inv.get("credit_note_ids") or []) + [cnid], "refund_log": refund_log}
        if to_refund > 0:
            bid = rid("brk", "credit_note_refund_due", cnid)
            cn["break_id"] = bid
            op.put("breaks", bid, {"break_id": bid, "leg": "credit_note_refund_due", "subject": f"zbm:2050:client:{cid}"[:160],
                                   "difference": M.fmt(to_refund), "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre",
                                   "explanation_code": "timing", "status": "open", "receipt_id": None,
                                   "credit_note_id": cnid, "due_by": cn["refund_due_by"], "resolution": None,
                                   "deadline": "MSA 4.10", "late": late, "severity": "high" if late else "normal",
                                   "note": f"refund {M.fmt(to_refund)} to the original payment method by "
                                           f"{cn['refund_due_by']} (MSA 4.10): refund it in Stripe; Finance links it"})
            op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": "credit_note_refund_due", "difference": M.fmt(to_refund)},
                      f"Refund owed under credit note {number}: {M.fmt(to_refund)} by {cn['refund_due_by']}")
        # status: fully credited -> void (nothing was paid) or credited (money was paid; refunds follow)
        fully = M.D(new["credited"]) >= M.D(inv["total"])
        if fully:
            new["status"] = "credited" if split["state"]["paid"] > 0 else "void"
            new["paid_at"] = None if new["status"] != "paid" else new.get("paid_at")
            if new["status"] == "void":
                new.update(voided_at=iso(self._now()), voided_by="andre", void_reason=body["reason_code"])
        # AEGIS F2: the ledger event carries everything a restore needs to rebuild the credit note (svc_restore)
        due_s = str(due_from) if due_by else None
        if rebuilt:
            op.record(derived_id("cnrb", cnid), "credit_note_rebuilt", "andre", cnid,
                      {"credit_note_id": cnid, "number": number, "entry_id": e["entry_id"]},
                      f"Credit note {number} rebuilt from the ledger after a restore")
        else:
            op.record(derived_id("cn", cnid), "credit_note_issued", "andre", cnid,
                      credit_note_payload(cnid, number, iid, M.fmt(amount), M.fmt(to_refund), body["reason_code"], due_s),
                      credit_note_summary(cnid, number, iid, M.fmt(amount), M.fmt(to_refund), body["reason_code"], due_s,
                                          len(self.log) + 1))
        op.put("credit_notes", cnid, cn)
        op.put("invoices", iid, new)
        self._resolve_deadlines(op, "cancel_or_credit_due", iid, "credit_note")
        if rebuilt:
            return {"credit_note": cn, "invoice": new, "entry_id": e["entry_id"]}
        self._enqueue_client_mail(op, "credit_note", cid, cnid,
                                  {"credit_note_number": number, "invoice_id": iid, "amount": M.fmt(amount),
                                   "issued_on": cn["issued_on"], "refund_amount": M.fmt(to_refund),
                                   "refund_due_by": cn["refund_due_by"],
                                   "amount_still_due": M.fmt(max(I2.amount_due(new), M.ZERO))
                                   if new["status"] == "issued" else "0.00"})
        return {"credit_note": cn, "invoice": new, "entry_id": e["entry_id"]}

    def _resolve_deadlines(self, op: Op, leg: str, invoice_id: str, basis: str) -> None:
        """AEGIS F9: a contract deadline (cancel-or-credit) is met by the void or credit note that answers it."""
        for b in list(self.db["breaks"].values()):
            if b.get("leg") == leg and b.get("invoice_id") == invoice_id and b["status"] != "resolved":
                op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                 "resolution": {"basis": basis}})

    def sow_ended(self, request_id: str, invoice_id: str, body: dict) -> dict:
        """AEGIS F9 / MSA 4.12(g): an SOW ended while this invoice is unpaid: within 10 business days ZBM cancels it
        or credits it down. The deadline is a break until a void or a credit note on the invoice closes it."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"invoices/{invoice_id}/sow-ended", body)
            if ent:
                return ent["response"]
            inv = self.db["invoices"].get(invoice_id)
            if inv is None:
                raise NotFound("no such invoice")
            if inv["status"] != "issued":
                raise Conflict(f"only an unpaid (issued) invoice is cancelled or credited; this one is {inv['status']}")
            ended = body["sow_ended_on"]
            if ended > self._business_today():
                raise Invalid("sow_ended_on is in the future")
            due = add_business_days(ended, REFUND_DUE_BUSINESS_DAYS)
            bid = rid("brk", "cancel_or_credit_due", invoice_id)
            if bid in self.db["breaks"] and self.db["breaks"][bid]["status"] != "resolved":
                raise Conflict("this invoice already has an open cancel-or-credit deadline")
            op = Op(self, f"sowend|{request_id}", "andre", invoice_id)
            op.put("breaks", bid, {"break_id": bid, "leg": "cancel_or_credit_due", "subject": f"zbm:invoice:{invoice_id}",
                                   "difference": inv["total"], "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre",
                                   "explanation_code": "timing", "status": "open", "receipt_id": None,
                                   "invoice_id": invoice_id, "due_by": due.isoformat(), "deadline": "MSA 4.12(g)",
                                   "late": due < self._business_today(), "resolution": None,
                                   "note": f"the SOW ended {ended.isoformat()}: void this invoice or credit it down to "
                                           f"the work done by {due.isoformat()} (MSA 4.12(g))"})
            op.record(derived_id("brk", bid, ended.isoformat()), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": "cancel_or_credit_due", "difference": inv["total"]},
                      f"Cancel or credit by {due.isoformat()} (SOW ended {ended.isoformat()})")
            resp = self._idem_add(op, key, h, {"break_id": bid, "due_by": due.isoformat(), "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    # ================================================================== the refund side (called from svc_stripe)

    def _credit_refund_share(self, inv: Optional[dict], delta: Decimal) -> Decimal:
        """How much of a refund just booked at Stripe pays credit notes still owing one (out of 2050)."""
        if inv is None or not inv.get("credit_note_ids"):
            return M.ZERO
        return M.q(min(delta, M.D(inv.get("refund_due") or "0.00")))

    def _credit_refund_link(self, op: Op, inv: dict, entry_id: str, delta: Decimal, linked: Decimal) -> dict:
        """Record the refund on the invoice and, for the part it paid out of 2050, on the credit notes (oldest first).
        A credit note whose refund is complete is ``refunded``, its break resolved, and the client is emailed."""
        left = linked
        for cnid in inv.get("credit_note_ids") or []:
            if left <= 0:
                break
            cn = op.get("credit_notes", cnid)
            if cn is None:
                continue
            rem = M.D(cn.get("refund_remaining") or "0.00")
            if rem <= 0:
                continue
            take = min(rem, left)
            left = M.q(left - take)
            rem = M.q(rem - take)
            cn = {**cn, "refund_remaining": M.fmt(rem),
                  "refunds": list(cn.get("refunds") or []) + [{"entry_id": entry_id, "amount": M.fmt(take),
                                                              "booked": "against_credit_note"}]}
            if rem == 0:
                cn["status"] = "refunded"
                cn["refunded_at"] = iso(self._now())
                b = op.get("breaks", cn.get("break_id") or "")
                if b is not None and b["status"] != "resolved":
                    op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                     "resolution": {"basis": "refund_booked", "entry_id": entry_id}})
                self._enqueue_client_mail(op, "refund_sent", cn["client_id"], cnid,
                                          {"credit_note_number": cn["number"], "invoice_id": cn["invoice_id"],
                                           "refund_amount": cn["to_refund"],
                                           "refunded_on": self._business_today().isoformat()})
            op.put("credit_notes", cnid, cn)
            op.record(derived_id("cnr", cnid, entry_id), "credit_note_refund_linked", ACTOR, cnid,
                      {"credit_note_id": cnid, "entry_id": entry_id, "amount": M.fmt(take)},
                      f"Refund {M.fmt(take)} linked to credit note {cn['number']}")
        new = {**inv, "refund_due": M.fmt(M.D(inv.get("refund_due") or "0.00") - linked),
               "refunded_via_credit": M.fmt(M.D(inv.get("refunded_via_credit") or "0.00") + linked),
               "refund_log": list(inv.get("refund_log") or []) + [
                   {"entry_id": entry_id, "amount": M.fmt(delta), "via_credit": M.fmt(linked),
                    "ar_unlinked": M.fmt(delta - linked), "credit_note_ids": []}]}
        op.put("invoices", inv["invoice_id"], new)
        return new

    @staticmethod
    def _credited_status(inv: dict, refunded: Decimal, paid: Decimal) -> str:
        if M.D(inv.get("credited") or "0.00") >= M.D(inv["total"]):
            return "credited"
        if paid > 0 and refunded >= paid:
            return "issued"            # every paid cent went back; the client owes the amount due
        return "paid" if inv["status"] in ("paid", "credited") else inv["status"]
