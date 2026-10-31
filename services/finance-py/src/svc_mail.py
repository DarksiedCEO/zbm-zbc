"""
Client email: billing contacts, the outbox and the ``client-mail`` job (ADR 0026; red team OPS-H3, RTM-12).

- ``PUT /fin/v1/clients/{client_id}/billing-contact`` (Andre): the ONE address client mail goes to. The address is kept
  in Finance's own store; the evidence ledger gets only its SHA-256.
- Anything Finance tells a client is QUEUED in the same commit as the bookkeeping that caused it (a receipt, a credit
  note, a void, a refund linked to a credit note, Andre's acknowledgement of a client request). Sending happens later,
  outside every bookkeeping lock, so a slow or failing mail provider never blocks or undoes a booking.
- The ``client-mail`` job (``POST /fin/v1/jobs/client-mail/run``; the cron service runs it hourly) sends what is due,
  at most MAIL_BATCH per run. A failed attempt backs off exponentially (15 min, 30, 60, 120, 240, 480); after
  MAIL_MAX_ATTEMPTS attempts, or a refusal the provider says is permanent, the message is ``send_failed``: a break
  (severity high) and an ALERT log line tell Andre. The last retry is about 16 hours after the first, inside the 24-hour
  window in which Resend replays an idempotency key, so a retry after a lost answer is never a second email.
- No billing contact on file: nothing is sent, the message waits (no attempt is spent), and a break asks Andre to
  record the contact. Mail switched off (the default): nothing is sent, nothing is spent, nothing alerts.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import mail as MAIL
import reasons as R
from clock import iso, parse_iso
from errors import Conflict, NotFound
from intelligences import i02_receivables as I2
from ledger import derived_id
from ports import MailOutcome
from service import Gather, Op, rid
from svc_credits import add_business_days

alert_log = logging.getLogger("finance.alert")
MAIL_MAX_ATTEMPTS = 7
MAIL_BACKOFF_FIRST_MIN = 15
MAIL_BACKOFF_MAX_MIN = 480
MAIL_BATCH = 25


def backoff_minutes(attempts: int) -> int:
    """Minutes to wait after the ``attempts``-th failed attempt (1 -> 15, 2 -> 30, ... capped at 480)."""
    return min(MAIL_BACKOFF_FIRST_MIN * (2 ** max(attempts - 1, 0)), MAIL_BACKOFF_MAX_MIN)


class MailMixin:

    # ================================================================== billing contacts

    @property
    def vault(self):
        """ADR 0027: the contact vault, the only home of a client's address and name (pii_vault)."""
        v = getattr(self, "_contact_vault", None)
        if v is None:
            from pii_vault import ContactVault
            v = self._contact_vault = ContactVault(self.cfg.data_dir)
        return v

    def put_billing_contact(self, request_id: str, client_id: str, body: dict) -> dict:
        """ADR 0027 (redaction by design): the address and the name go to the contact vault only. The log and the
        ledger get a ``contact_ref``; the idempotency hash sees an HMAC keyed by the client's vault salt, never the
        address or a plain hash of it, so deleting the vault file (offboarding) leaves nothing that links back."""
        with self.lock:
            email, name = body["email"], body.get("name")
            salt = self.vault.salt(client_id)
            shadow = {"email_hmac": self.vault.digest(salt, email), "name_hmac": self.vault.digest(salt, name)}
            key, h, ent = self._idem("andre", request_id, f"billing-contact/{client_id}", shadow)
            if ent:
                return ent["response"]
            contact_ref = rid("contact", client_id, request_id)
            self.vault.put(client_id, contact_ref, email, name, salt)       # first: the address lives only there
            rec = {"client_id": client_id, "contact_ref": contact_ref, "set_at": iso(self._now()), "set_by": "andre"}
            op = Op(self, f"contact|{request_id}", "andre", client_id)
            op.record(derived_id("contact", client_id, contact_ref), "client_contact_recorded", "andre",
                      client_id, {"client_id": client_id, "contact_ref": contact_ref},
                      "Andre recorded a client's billing contact")
            op.put("client_contacts", client_id, rec)
            b = self.db["breaks"].get(rid("brk", "client_mail_no_contact", client_id))
            if b is not None and b["status"] != "resolved":
                op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                 "resolution": {"basis": "contact_recorded"}})
            resp = self._idem_add(op, key, h, {"client_id": client_id, "contact_ref": contact_ref,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def _contact(self, client_id: str) -> Optional[str]:
        rec = self.db["client_contacts"].get(client_id)
        if not rec:
            return None
        if rec.get("email") and not rec.get("contact_ref"):
            return rec["email"]                 # a record written before ADR 0027 (address in the log; offboarding says so)
        if not rec.get("contact_ref"):
            return None                         # erased by offboarding
        v = self.vault.get(client_id)
        return v["email"] if v and v.get("contact_ref") == rec["contact_ref"] else None

    # ================================================================== the outbox

    def _enqueue_client_mail(self, op: Op, kind: str, client_id: str, ref: str, data: dict) -> str:
        """Queue one message in the SAME commit as the bookkeeping behind it (never sent here)."""
        mid = rid("mail", kind, ref)
        if op.get("mail_outbox", mid) is not None:
            return mid
        op.put("mail_outbox", mid, {"mail_id": mid, "kind": kind, "client_id": client_id, "ref": ref, "data": data,
                                    "status": "pending", "attempts": 0, "created_at": iso(self._now()),
                                    "last_attempt_at": None, "sent_at": None, "last_code": None})
        op.record(derived_id("mailq", mid), "client_mail_queued", I2.ACTOR, mid,
                  {"mail_id": mid, "kind": kind, "ref": ref}, f"Client email queued ({kind})")
        return mid

    def acknowledge(self, request_id: str, client_id: str, body: dict) -> dict:
        """Andre has a client's written request; the acknowledgement MSA 4.12(f) promises within 2 business days is
        queued now."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"acknowledgements/{client_id}", body)
            if ent:
                return ent["response"]
            if body.get("invoice_id"):
                inv = self.db["invoices"].get(body["invoice_id"])
                if inv is None or inv["client_id"] != client_id:
                    raise NotFound("no such invoice for this client")
            received = body["received_on"]
            data = {"topic": body["topic"], "received_on": received.isoformat(), "invoice_id": body.get("invoice_id"),
                    # refund policy 8.3: a billing dispute is worked through within 15 business days
                    "reply_by": add_business_days(received, 15).isoformat() if body["topic"] == "billing_dispute"
                    else None}
            op = Op(self, f"ack|{request_id}", "andre", client_id)
            mid = self._enqueue_client_mail(op, "acknowledgement", client_id, f"{client_id}|{request_id}", data)
            due = add_business_days(received, 2)
            resp = self._idem_add(op, key, h, {"mail_id": mid, "acknowledge_by": due.isoformat(),
                                               "late": self._business_today() > due,
                                               "ledger_event_ids": op.events, "request_id": request_id})
            self._commit(op)
            return resp

    def mail_status(self, mail_id: str) -> dict:
        with self.lock:
            rec = self.db["mail_outbox"].get(mail_id) or self.db["client_receipts"].get(mail_id)
            if rec is None:
                raise NotFound("no such client email")
            return {k: v for k, v in rec.items() if k != "data"}

    def requeue_mail(self, request_id: str, mail_id: str) -> dict:
        """Andre fixed the cause of a permanent failure (the contact, the domain, the key): the message is due again."""
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"client-mail/{mail_id}/requeue", None)
            if ent:
                return ent["response"]
            coll = "mail_outbox" if mail_id in self.db["mail_outbox"] else \
                "client_receipts" if mail_id in self.db["client_receipts"] else None
            if coll is None:
                raise NotFound("no such client email")
            rec = self.db[coll][mail_id]
            if rec["status"] != "send_failed":
                raise Conflict(f"only a message that failed for good is requeued; this one is {rec['status']}")
            pending = "pending" if coll == "mail_outbox" else "pending_send"
            op = Op(self, f"requeue|{request_id}", "andre", mail_id)
            op.put(coll, mail_id, {**rec, "status": pending, "attempts": 0, "last_attempt_at": None})
            op.record(derived_id("mailrq", mail_id, request_id), "client_mail_requeued", "andre", mail_id,
                      {"mail_id": mail_id}, "Andre requeued a client email")
            b = self.db["breaks"].get(rid("brk", "client_mail_failed", mail_id))
            if b is not None and b["status"] != "resolved":
                op.put("breaks", b["break_id"], {**b, "status": "resolved", "resolved_at": iso(self._now()),
                                                 "resolution": {"basis": "requeued"}})
            resp = self._idem_add(op, key, h, {"mail_id": mail_id, "status": pending, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    # ================================================================== sending

    def _mail_due(self, rec: dict, pending: str) -> Optional[str]:
        """Why this message is not sent now (None = send it)."""
        if rec["status"] == "sent":
            return "already sent"
        if rec["status"] == "send_failed":
            return "sending failed for good: Andre requeues it once the cause is fixed"
        now = self._now()
        if rec["status"] == "sending" and rec.get("claimed_at") and \
                (now - parse_iso(rec["claimed_at"])).total_seconds() < self.SEND_CLAIM_STALE_MIN * 60:
            return "a send is already in progress"
        last = rec.get("last_attempt_at")
        if rec["status"] == pending and last and rec.get("attempts", 0) > 0 and \
                (now - parse_iso(last)).total_seconds() < backoff_minutes(rec["attempts"]) * 60:
            return f"backoff: the last attempt failed less than {backoff_minutes(rec['attempts'])} minutes ago"
        if rec["status"] == pending and last and rec.get("waiting_for") and \
                (now - parse_iso(last)).total_seconds() < MAIL_BACKOFF_FIRST_MIN * 60:
            return f"backoff: waiting for {rec['waiting_for']}"
        return None

    def _deliver(self, g: Gather, kind: str, client_id: str, ref: str, data: dict) -> MailOutcome:
        """Build the message and hand it to the adapter (outside every lock). Never raises."""
        to = self._contact(client_id)
        try:
            msg = MAIL.message(kind, client_id, to, data, ref)
        except (ValueError, KeyError, TypeError):
            return MailOutcome("permanent", code="render_failed")
        port = self.ports.client_mail

        def call():
            d = getattr(port, "deliver", None)
            if d is None:                      # an adapter of the old shape (receipts only)
                ok = port.send_receipt(client_id, data) if kind == "receipt" else False
                return MailOutcome("sent" if ok is True else "retry")
            return d(msg)
        out = g.call("client_mail", "deliver", (kind, ref), call, MailOutcome("retry", code="adapter_error"))
        return out if isinstance(out, MailOutcome) else MailOutcome("retry", code="adapter_answer")

    def _mail_after(self, op: Op, coll: str, key: str, rec: dict, out: MailOutcome, pending: str,
                    mine: bool) -> dict:
        """The record after one attempt: sent, back to pending (with backoff), or send_failed (break + ALERT)."""
        now = iso(self._now())
        withdrawn = rec["status"] in ("withdrawn", "withdrawn_after_send")
        spend = out.outcome in ("retry", "permanent")
        attempts = rec.get("attempts", 0) + (1 if spend else 0)
        upd: dict = {"last_attempt_at": now, "last_code": out.code, "attempts": attempts}
        if out.outcome == "sent":
            upd.update(status="withdrawn_after_send" if withdrawn else "sent", sent_at=now,
                       provider_id=out.provider_id, waiting_for=None)
        elif not mine:
            upd["status"] = rec["status"]       # AEGIS N1: a late failure never undoes another request's outcome
        elif out.outcome == "permanent" or attempts >= MAIL_MAX_ATTEMPTS:
            upd.update(status="send_failed", waiting_for=None)
            self._mail_failed_break(op, key, rec, out)
        else:
            upd.update(status=pending,
                       waiting_for={"no_recipient": "a billing contact", "disabled": "mail to be configured"}
                       .get(out.outcome))
            if out.outcome == "no_recipient":
                self._no_contact_break(op, rec["client_id"])
        keep_claim = not mine and rec["status"] == "sending" and out.outcome != "sent"
        return {**rec, **upd, **({} if keep_claim else {"claimed_at": None, "claimed_by": None})}

    def _mail_failed_break(self, op: Op, key: str, rec: dict, out: MailOutcome) -> None:
        bid = rid("brk", "client_mail_failed", key)
        if op.get("breaks", bid) is None:
            op.put("breaks", bid, {"break_id": bid, "leg": "client_mail_failed", "subject": f"mail:{key}"[:160],
                                   "difference": "0.00", "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre", "severity": "high",
                                   "explanation_code": "unknown", "status": "open", "receipt_id": None,
                                   "resolution": None,
                                   "note": f"client email {key} could not be sent ({str(out.code)[:40]}): fix the "
                                           "cause, then POST /fin/v1/client-mail/{id}/requeue, or send it by hand"})
            op.record(derived_id("brk", bid), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": "client_mail_failed", "difference": "0.00"},
                      "Break opened: a client email failed for good")
        alert_log.error("ALERT finance: client_mail_failed mail=%s code=%s break=%s", key, str(out.code)[:40], bid)

    def _no_contact_break(self, op: Op, client_id: str) -> None:
        bid = rid("brk", "client_mail_no_contact", client_id)
        if op.get("breaks", bid) is None or op.get("breaks", bid)["status"] == "resolved":
            op.put("breaks", bid, {"break_id": bid, "leg": "client_mail_no_contact", "subject": "mail:contact",
                                   "difference": "0.00", "opened_at": iso(self._now()),
                                   "opened_on": self._today_la().isoformat(), "owner": "andre",
                                   "explanation_code": "unknown", "status": "open", "receipt_id": None,
                                   "client_id": client_id, "resolution": None,
                                   "note": "client email is waiting: record the client's billing contact (PUT "
                                           "/fin/v1/clients/{id}/billing-contact)"})
            op.record(derived_id("brk", bid, iso(self._now())), "break_opened", "intel_07_reconciliation", bid,
                      {"break_id": bid, "leg": "client_mail_no_contact", "difference": "0.00"},
                      "Break opened: client email waiting for a billing contact")

    def _send_outbox(self, principal: str, request_id: str, mail_id: str) -> str:
        """One attempt for one outbox message: claimed (committed) before the adapter call, like receipts."""
        with self.lock:
            rec = self.db["mail_outbox"].get(mail_id)
            if rec is None or self._mail_due(rec, "pending"):
                return "skipped"
            claim = Op(self, f"mailc|{principal}|{request_id}", I2.ACTOR, mail_id)
            claim.record(derived_id("mailc", mail_id, request_id), "client_mail_send_claimed", I2.ACTOR, mail_id,
                         {"mail_id": mail_id, "attempt": rec["attempts"] + 1}, "Client email send started")
            claim.put("mail_outbox", mail_id, {**rec, "status": "sending", "claimed_at": iso(self._now()),
                                               "claimed_by": request_id})
            self._commit(claim)
        g = Gather(self, f"mails|{principal}|{request_id}", I2.ACTOR, mail_id)
        out = self._deliver(g, rec["kind"], rec["client_id"], mail_id, rec["data"])
        with self.lock:
            cur = self.db["mail_outbox"][mail_id]
            mine = cur["status"] == "sending" and cur.get("claimed_by") == request_id
            op = Op(self, f"mails|{principal}|{request_id}", I2.ACTOR, mail_id, g)
            new = self._mail_after(op, "mail_outbox", mail_id, cur, out, "pending", mine)
            op.record(derived_id("mails", mail_id, request_id),
                      "client_mail_sent" if out.outcome == "sent" else "client_mail_send_failed", I2.ACTOR, mail_id,
                      {"mail_id": mail_id, "outcome": out.outcome, "attempt": new["attempts"]},
                      f"Client email {out.outcome} ({rec['kind']})")
            op.put("mail_outbox", mail_id, new)
            self._commit(op)
            return out.outcome

    def job_client_mail(self, prefix: str) -> dict:
        """Repeatable (hourly by the cron service): send due receipts and outbox messages, at most MAIL_BATCH. Mail
        off (the stand-in): nothing is touched. A real adapter and no billing contact: the message waits untouched
        (the break from its first attempt already asks Andre for the contact)."""
        from ports import NotBuiltClientMail
        port = self.ports.client_mail
        if isinstance(port, NotBuiltClientMail):
            with self.lock:
                waiting = sum(1 for r in self.db["client_receipts"].values() if r["status"] == "pending_send") + \
                    sum(1 for r in self.db["mail_outbox"].values() if r["status"] == "pending")
            return {"sent": 0, "waiting": waiting, "failed_for_good": 0, "skipped": 0, "due": 0, "mail_off": True}
        needs_contact = bool(getattr(port, "requires_contact", False))
        with self.lock:
            receipts = sorted((r["created_at"], k) for k, r in self.db["client_receipts"].items()
                              if r["status"] in ("pending_send", "sending") and not self._send_refusal(r))
            outbox = sorted((r["created_at"], k) for k, r in self.db["mail_outbox"].items()
                            if r["status"] in ("pending", "sending") and not self._mail_due(r, "pending"))
            if needs_contact:
                def known(k, coll):
                    r = self.db[coll][k]
                    return self._contact(r["client_id"]) is not None or not r.get("waiting_for")
                receipts = [x for x in receipts if known(x[1], "client_receipts")]
                outbox = [x for x in outbox if known(x[1], "mail_outbox")]
        todo = ([("receipt", k) for _, k in receipts] + [("outbox", k) for _, k in outbox])[:MAIL_BATCH]
        summary = {"sent": 0, "waiting": 0, "failed_for_good": 0, "skipped": 0, "due": len(receipts) + len(outbox)}
        for i, (what, k) in enumerate(todo):
            req = rid("mailjob", prefix, what, k)
            if what == "receipt":
                res = self.send_client_receipt("ops_cron", req, k)
                st = res["client_receipt"]["status"]
                outcome = "sent" if res.get("sent") else ("permanent" if st == "send_failed" else "retry")
            else:
                outcome = self._send_outbox("ops_cron", req, k)
            key = {"sent": "sent", "permanent": "failed_for_good", "skipped": "skipped"}.get(outcome, "waiting")
            with self.lock:
                rec = (self.db["client_receipts"] if what == "receipt" else self.db["mail_outbox"]).get(k) or {}
            if rec.get("status") == "send_failed" and key != "failed_for_good":
                key = "failed_for_good"
            summary[key] += 1
        return summary
