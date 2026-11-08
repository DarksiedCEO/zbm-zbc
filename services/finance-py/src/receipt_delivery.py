"""Durable receipt claims and envelopes (ADR 0032/0038). Default provider remains disabled."""
from copy import deepcopy
from datetime import timedelta
import json

from clock import iso, parse_iso
from errors import Conflict, NotFound
from intelligences import i02_receivables as I2
from ports import ReceiptDeliveryOutcome
from receipt_resend_adapter import ResendReceiptMail, SCHEMA, validate_saved, same_request
from resend_receipts import valid_message_id
from service import Gather, Op, rid


class ReceiptDeliveryMixin:
    RECEIPT_ATTEMPT_LIMIT = 7
    RECEIPT_RETRY_HOURS = 23

    def put_receipt_contact(self, request_id, client_id, body):
        self._refuse_test_client(client_id)
        with self.lock:
            key, digest, ent = self._idem("andre", request_id, f"billing-contact/{client_id}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            # Opaque revision, not a guessable bare email hash. Contact/body stay in the private journal only.
            revision = rid("contact", client_id, request_id)
            op = Op(self, f"contact|{request_id}", "andre", client_id)
            op.put("client_contacts", client_id, {"email": body["email"], "revision": revision})
            op.record(revision, "billing_contact_updated", "andre", client_id,
                      {"client_id": client_id, "revision": revision, "enabled": body["email"] is not None},
                      "Founder updated receipt contact")
            resp = self._idem_add(op, key, digest, {"client_id": client_id, "enabled": body["email"] is not None,
                                                   "revision": revision, "request_id": request_id})
            self._commit(op)
            return resp

    def review_receipt_delivery(self, request_id, crid, body):
        with self.lock:
            key, digest, ent = self._idem("andre", request_id, f"receipt-review/{crid}", body)
            if ent:
                return ent["response"]
            self.require_rules()
            rec = self.db["client_receipts"].get(crid)
            if rec is None:
                raise NotFound("no such client receipt")
            if rec["status"] not in ("delivery_uncertain", "delivery_failed"):
                raise Conflict("receipt does not require delivery review")
            op = Op(self, f"receipt-review|{request_id}", "andre", crid)
            new = {**rec, "status": "delivery_reviewed", "delivery_review": body["evidence"]}
            op.put("client_receipts", crid, new)
            op.record(rid("review", crid, request_id), "receipt_delivery_reviewed", "andre", crid,
                      {"client_receipt_id": crid, "evidence": body["evidence"]},
                      "Founder recorded external delivery review; no send authorized")
            resp = self._idem_add(op, key, digest, {"client_receipt": new, "sent": False,
                                                   "request_id": request_id})
            self._commit(op)
            return resp

    def _receipt_stop(self, rec, request_id, detail, uncertain=False):
        if uncertain:
            op = Op(self, f"receipt-stop|{request_id}", I2.ACTOR, rec["client_receipt_id"])
            rec = {**rec, "status": "delivery_uncertain", "claimed_by": None, "claimed_at": None}
            op.put("client_receipts", rec["client_receipt_id"], rec)
            op.record(rid("receipt-stop", rec["client_receipt_id"], request_id), "receipt_delivery_uncertain",
                      I2.ACTOR, rec["client_receipt_id"], {"client_receipt_id": rec["client_receipt_id"],
                      "reason": detail}, "Receipt requires founder delivery review")
            self._commit(op)
        return {"client_receipt": dict(rec), "sent": False, "detail": detail, "request_id": request_id}

    def send_receipt_envelope(self, principal, request_id, crid):
        with self.lock:
            key, digest, ent = self._idem(principal, request_id, f"client-receipts/{crid}/send", None)
            if ent:
                return ent["response"]
            self.require_rules()
            rec = self.db["client_receipts"].get(crid)
            if rec is None:
                raise NotFound("no such client receipt")
            if rec["status"] in ("delivery_uncertain", "delivery_failed", "delivery_reviewed", "sent",
                                  "withdrawn", "withdrawn_after_send"):
                return self._receipt_stop(rec, request_id, "terminal_delivery_state")
            mail = self.ports.client_mail
            if getattr(mail, "enabled", False) is not True or not callable(getattr(mail, "deliver_receipt", None)):
                return self._receipt_stop(rec, request_id, "mail_disabled")
            real = type(mail) is ResendReceiptMail
            if real and rec['entity'] != 'zbm':
                return self._receipt_stop(rec, request_id, "entity_mail_disabled")
            contact = self.db["client_contacts"].get(rec["client_id"])
            saved = self.db["receipt_envelopes"].get(crid)
            now = self._now()
            # A fresh claim must not be terminalized by another caller inside the existing claim interval.
            refusal = self._send_refusal(rec)
            if refusal and rec["status"] == "sending":
                return self._receipt_stop(rec, request_id, refusal)
            if real and (rec['status'] == 'sending' or rec['attempts'] > 0):
                return self._receipt_stop(rec, request_id, "real_prior_attempt_requires_review", True)
            if real and saved and (saved.get('schema') != SCHEMA or not mail.matches(saved)):
                return self._receipt_stop(rec, request_id, "legacy_or_changed_mail_profile", True)
            if not real and saved and saved.get('schema') == SCHEMA:
                return self._receipt_stop(rec, request_id, "real_envelope_adapter_unavailable")
            if saved:
                # Deadline and content were fixed before the FIRST possible I/O, including a crashed claim.
                if now < parse_iso(rec["last_attempt_at"]) or now >= parse_iso(saved["retry_until"]) or \
                        rec["attempts"] >= self.RECEIPT_ATTEMPT_LIMIT:
                    return self._receipt_stop(rec, request_id, "delivery_window_closed", True)
                if not contact or contact["revision"] != saved["contact_revision"] or not contact["email"]:
                    return self._receipt_stop(rec, request_id, "contact_changed", True)
            elif rec["attempts"] > 0 or rec["status"] == "sending":
                return self._receipt_stop(rec, request_id, "historical_attempt_without_envelope", True)
            elif not contact or not contact["email"]:
                return self._receipt_stop(rec, request_id, "contact_missing")
            if refusal:
                return self._receipt_stop(rec, request_id, refusal)
            if not saved:
                view = {k: rec[k] for k in ("client_receipt_id", "entity", "invoice_id", "what_you_bought",
                                           "items", "period", "amount_paid", "currency", "paid_on", "method")}
                text = "Payment receipt\n" + json.dumps(view, ensure_ascii=True, sort_keys=True)
                if len(text.encode("utf-8")) > 32768:
                    return self._receipt_stop(rec, request_id, "receipt_body_too_large")
                saved = {"envelope": {"to": contact["email"], "subject": "Your payment receipt", "text": text,
                                       "idempotency_key": rid("mail", crid)},
                         "contact_revision": contact["revision"], "first_attempt_at": iso(now),
                         "retry_until": iso(now + timedelta(hours=self.RECEIPT_RETRY_HOURS))}
                if real:
                    try:
                        saved = mail.prepare(rec, contact, text, rid("mail", crid), now)
                    except Exception:
                        return self._receipt_stop(rec, request_id, "invalid_complete_envelope")
            previous = self.db['receipt_envelopes'].get(crid)
            if real and (not validate_saved(saved, crid) or previous and not same_request(previous, saved)):
                return self._receipt_stop(rec, request_id, "immutable_envelope_refused", True)
            claim = Op(self, f"envelope-claim|{principal}|{request_id}", I2.ACTOR, crid)
            claim.record(rid("mail-claim", crid, request_id), "client_receipt_send_claimed", I2.ACTOR, crid,
                         {"client_receipt_id": crid, "attempt": rec["attempts"] + 1}, "Client receipt send started")
            claim.put("receipt_envelopes", crid, saved)
            claim.put("client_receipts", crid, {**rec, "status": "sending", "claimed_at": iso(now),
                       "last_attempt_at": iso(now), "claimed_by": request_id, "attempts": rec["attempts"] + 1})
            self._commit(claim)
            envelope = deepcopy(saved if real else saved["envelope"])
        g = Gather(self, f"envelope-send|{principal}|{request_id}", I2.ACTOR, crid)
        answer = g.call("client_mail", "deliver_receipt", (crid,), lambda: mail.deliver_receipt(envelope),
                        ReceiptDeliveryOutcome("uncertain"))
        outcome = answer.outcome if type(answer) is ReceiptDeliveryOutcome and \
            type(answer.outcome) is str and answer.outcome in ("accepted", "retry", "failed", "uncertain") else "uncertain"
        if real and (outcome == 'retry' or outcome == 'accepted' and not valid_message_id(answer.provider_message_id)
                     or outcome != 'accepted' and getattr(answer, 'provider_message_id', None) is not None):
            outcome = 'uncertain'
        with self.lock:
            rec = self.db["client_receipts"][crid]
            mine = rec["status"] == "sending" and rec.get("claimed_by") == request_id
            accepted = outcome == "accepted"
            current_saved = self.db['receipt_envelopes'].get(crid)
            if real and (not validate_saved(current_saved, crid) or not same_request(saved, current_saved)
                         or current_saved['acceptance'] is not None):
                return self._receipt_stop(rec, request_id, "acceptance_conflict_requires_review")
            withdrawn = rec["status"] in ("withdrawn", "withdrawn_after_send")
            status = rec["status"]
            if accepted and real and not mine and not withdrawn:
                # Late acceptance may be retained privately, never erase founder review or uncertainty.
                status = rec['status']
            elif accepted:
                status = "withdrawn_after_send" if withdrawn else "sent"
            elif mine:
                status = {"retry": "pending_send", "failed": "delivery_failed", "uncertain": "delivery_uncertain"}[outcome]
            new = {**rec, "status": status,
                   **({"claimed_by": None, "claimed_at": None} if mine or accepted else {}),
                   **({"sent_at": iso(self._now())} if accepted else {})}
            op = Op(self, f"envelope-send|{principal}|{request_id}", I2.ACTOR, crid, g)
            op.record(rid("mail-outcome", crid, request_id), "client_receipt_sent" if accepted else
                      "client_receipt_send_failed", I2.ACTOR, crid, {"client_receipt_id": crid, "outcome": outcome},
                      "Receipt adapter accepted" if accepted else "Receipt adapter did not confirm acceptance")
            if real and accepted:
                op.put('receipt_envelopes', crid, {**current_saved, 'acceptance': {
                    'provider_message_id': answer.provider_message_id, 'accepted_at': iso(self._now())}})
            op.put("client_receipts", crid, new)
            resp = self._idem_add(op, key, digest, {"client_receipt": new, "sent": accepted,
                                                   "detail": outcome, "request_id": request_id})
            self._commit(op)
            return resp
