"""
Client offboarding, Finance's side (ADR 0027 decision 3; MSA 15.5: delete Client Data within 30 days after the last
SOW ends, keep only what the law requires -- invoices, payment records and other financial records for 7 years after
the end of the tax year they relate to; red team OPS-H8).

``POST /fin/v1/clients/{client_id}/offboarding`` (Andre only):

- ``dry_run: true``: what Finance holds for the client, what happens to each kind, blockers and warnings, and a
  ``plan_sha256`` over all of it. Writes nothing (no log line, no ledger event).
- ``dry_run: false`` with that ``plan_sha256`` (refused 409 when anything changed since): deletes the billing contact
  (the vault file: the only place the address and name ever were), withdraws the client email not yet sent, keeps
  every money record, and records a hash-only DELETION CERTIFICATE on the ledger (``client_offboarded``), with the
  certificate hashes of the other services' runs (``peer_certificates``) when the offboarding devtool passes them.

Never deleted here (kept 7 years, MSA 15.5(a)): invoices, receipts, credit notes, refunds, disputes, Stripe payment
records (ids only), the client's tax jurisdiction (nexus records) and every journal entry. They hold no personal
information by design (ids, amounts, codes, dates). The ledger and the log are append-only: personal information is
never written there (the contact vault, ``pii_vault``), and the run checks the log for the contact it deletes.

Blocked: an invoice with an open Compliance hold (sanctions records are never touched while a hold is open) and an
open Stripe dispute (its evidence is still needed). Warned: unpaid invoices (offboarding never cancels them: void or
credit them first if that is the deal) and refunds still owed (they are paid to the original payment method; the
confirmation email can no longer be sent once the contact is deleted).
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from clock import iso
from errors import Conflict
from ledger import derived_id
from service import Op, rid, sha

KEEP = "7 years after the end of the tax year (MSA 15.5(a))"
UNSENT = ("pending", "pending_send", "send_failed")


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class OffboardingMixin:

    def _offboarding_plan(self, client_id: str) -> dict:
        """Under the lock: the inventory, blockers and warnings (no personal information in it)."""
        invs = {k: v for k, v in self.db["invoices"].items() if v.get("client_id") == client_id}
        inv_ids = set(invs)
        receipts = {k for k, v in self.db["receipts"].items() if v.get("invoice_id") in inv_ids}
        cns = {k for k, v in self.db["credit_notes"].items()
               if v.get("invoice_id") in inv_ids or v.get("client_id") == client_id}
        refunds = {k for k, v in self.db["refunds"].items() if v.get("invoice_id") in inv_ids}
        sessions = {k for k, v in self.db["stripe_sessions"].items() if v.get("invoice_id") in inv_ids}
        # a dispute names its payment intent; the receipt Finance booked for that intent names the invoice
        disputes = {k: v for k, v in self.db["stripe_disputes"].items() if v.get("payment_intent")
                    and rid("rct", "stripe", v["payment_intent"]) in receipts}
        objs = inv_ids | receipts | cns | refunds
        sub = f"client:{client_id}"
        entries = [e for e in self.entries if any(ln.get("subledger") == sub for ln in e["lines"])
                   or (e.get("source") or {}).get("id") in objs]
        unsent = [k for k, v in self.db["mail_outbox"].items() if v.get("client_id") == client_id
                  and v.get("status") in UNSENT]
        unsent += [k for k, v in self.db["client_receipts"].items() if v.get("client_id") == client_id
                   and v.get("status") in UNSENT]
        crec = self.db["client_contacts"].get(client_id) or {}
        contact = 1 if (self.vault.get(client_id) is not None or crec.get("email")) else 0
        legacy = 1 if crec.get("email") else 0
        held = [
            {"kind": "billing_contact", "count": contact, "action": "delete",
             "where": "the contact vault (never the log or the ledger)", "basis": "MSA 15.5"},
            {"kind": "unsent_client_email", "count": len(unsent), "action": "withdraw",
             "where": "the client-mail outbox", "basis": "nothing more is sent once the contact is deleted"},
            {"kind": "invoices", "count": len(invs), "action": "keep_7_years", "retention": KEEP},
            {"kind": "payment_records", "count": len(receipts) + len(sessions) + len(disputes), "action": "keep_7_years",
             "retention": KEEP},
            {"kind": "credit_notes_and_refunds", "count": len(cns) + len(refunds), "action": "keep_7_years",
             "retention": KEEP},
            {"kind": "journal_entries", "count": len(entries), "action": "keep_7_years", "retention": KEEP},
            {"kind": "tax_jurisdiction", "count": 1 if client_id in self.db["client_profiles"] else 0,
             "action": "keep_7_years", "retention": KEEP},
        ]
        blockers, warnings = [], []
        held_cmp = sorted(k for k, v in invs.items() if v.get("held_compliance"))
        if held_cmp:
            blockers.append(f"{len(held_cmp)} invoice(s) held by Compliance: resolve the hold first (a sanctions hold's "
                            "records are never touched while it is open)")
        open_d = sorted(k for k, v in disputes.items() if not v.get("finalized"))
        if open_d:
            blockers.append(f"{len(open_d)} Stripe dispute(s) still open: answer them first (the evidence is needed)")
        unpaid = sorted(k for k, v in invs.items() if v.get("status") in ("issued", "partially_paid"))
        if unpaid:
            warnings.append(f"{len(unpaid)} unpaid invoice(s): offboarding never cancels them (void or credit them first "
                            "if that is the deal)")
        owed = [k for k in cns if self.db["credit_notes"][k].get("status") == "refund_pending"]
        if owed:
            warnings.append(f"{len(owed)} refund(s) still owed: pay them to the original payment method; the "
                            "confirmation email cannot be sent after the contact is deleted")
        if legacy:
            warnings.append("the contact was recorded before ADR 0027: its address is in the local log, which the "
                            "run cannot rewrite (it is reported in the certificate)")
        plan = {"client_id_sha256": hashlib.sha256(client_id.encode()).hexdigest(), "held": held,
                "blockers": blockers, "warnings": warnings, "unsent_mail_ids": sorted(unsent),
                "log_length": len(self.log)}
        plan["plan_sha256"] = sha(plan)
        return plan

    def offboard_client(self, request_id: str, client_id: str, body: dict) -> dict:
        with self.lock:
            key, h, ent = self._idem("andre", request_id, f"clients/{client_id}/offboarding", body)
            if ent:
                return ent["response"]
            plan = self._offboarding_plan(client_id)
            public = {k: v for k, v in plan.items() if k not in ("unsent_mail_ids", "log_length")}
            if body["dry_run"]:
                return {**public, "dry_run": True, "request_id": request_id}
            if body.get("plan_sha256") != plan["plan_sha256"]:
                raise Conflict("the plan changed since the dry run (or this is not its plan_sha256): run the dry run "
                               "again and name its plan_sha256")
            if plan["blockers"]:
                raise Conflict("offboarding blocked: " + "; ".join(plan["blockers"]))
            now = self._now()
            # 1. the contact: read it once (to check the log for it), then delete the vault file FIRST
            v = self.vault.get(client_id) or {}
            crec = self.db["client_contacts"].get(client_id) or {}
            needles = [x for x in (v.get("email"), v.get("name"), crec.get("email"), crec.get("name")) if x]
            left = self._pii_lines(needles)
            deleted = self.vault.delete(client_id)
            if crec.get("email") and not deleted:
                deleted = 1
            # 2. the bookkeeping record, the withdrawn mail and the certificate, in one anchored commit
            op = Op(self, f"offb|{request_id}", "andre", client_id)
            withdrawn = 0
            for mid in plan["unsent_mail_ids"]:
                coll = "mail_outbox" if mid in self.db["mail_outbox"] else "client_receipts"
                rec = self.db[coll][mid]
                op.put(coll, mid, {**rec, "status": "withdrawn", "withdrawn_reason": "client_offboarded",
                                   "withdrawn_at": iso(now)})
                withdrawn += 1
            if crec or deleted:
                op.put("client_contacts", client_id, {"client_id": client_id, "contact_ref": None,
                                                      "erased_at": iso(now), "erased_by": "andre"})
            kept = {h_["kind"]: {"count": h_["count"], "retention": KEEP} for h_ in plan["held"]
                    if h_["action"] == "keep_7_years"}
            cert = {"certificate_version": 1, "service": "finance", "client_id_sha256": plan["client_id_sha256"],
                    "executed_at": iso(now), "executed_by": "andre", "plan_sha256": plan["plan_sha256"],
                    "deleted": {"billing_contact": deleted}, "withdrawn": {"unsent_client_email": withdrawn},
                    "kept": kept, "pii_left_in_log": left, "warnings": len(plan["warnings"]),
                    "peer_certificates": [{"service": p["service"], "certificate_sha256": p["certificate_sha256"]}
                                          for p in body.get("peer_certificates") or []]}
            cert_sha = hashlib.sha256(_canon(cert)).hexdigest()
            oid = rid("offb", client_id, request_id)
            op.put("client_offboardings", oid, {"offboarding_id": oid, "client_id": client_id, "certificate": cert,
                                                "certificate_sha256": cert_sha})
            op.record(derived_id("offb", client_id, cert_sha), "client_offboarded", "andre", client_id,
                      {"certificate_sha256": cert_sha, "plan_sha256": plan["plan_sha256"],
                       "deleted": deleted, "withdrawn": withdrawn, "pii_left_in_log": left,
                       "peers": len(cert["peer_certificates"])},
                      "Client offboarded: deletion certificate recorded")
            resp = self._idem_add(op, key, h, {"offboarding_id": oid, "certificate": cert,
                                               "certificate_sha256": cert_sha, "ledger_event_ids": op.events,
                                               "request_id": request_id})
            self._commit(op)
            return resp

    def _pii_lines(self, needles: list) -> int:
        """How many local-log lines still hold any of ``needles`` (exact text): the redaction-by-design check."""
        if not needles:
            return 0
        enc = [json.dumps(n)[1:-1].encode() for n in needles]
        return sum(1 for ln in self.log.raw_lines() if any(e in ln for e in enc))

    def offboarding(self, client_id: str) -> Optional[dict]:
        with self.lock:
            recs = [r for r in self.db["client_offboardings"].values() if r["client_id"] == client_id]
            return recs[-1] if recs else None
