"""
OOH-4 (ADR 0022): buy and contract controls — the vendor contract (insertion order) every purchase is made under.

Rules enforced here, each with its own reason code and test:

* A preparer drafts a contract from the vendor's document: the vendor, the exact quotes, the total, the
  cancellation terms, the payment terms and the vendor payment method. The total is exactly the sum of the quotes
  (CONTRACT_TOTAL_MISMATCH); the quotes are this campaign's live vendor quotes of that vendor; the cancellation terms
  are the commercial model's (TERMS_CHANGED). A draft that shares a quote with an open contract supersedes it.
* Vendors are paid by ACH or check, never card (locked): the method is one of those two by type, and it must be one
  the vendor's sourced supplier profile accepts (ADR 0020): unknown is refused (VENDOR_PAYMENT_METHOD_UNKNOWN), an
  unlisted one too (VENDOR_PAYMENT_METHOD_NOT_ACCEPTED).
* Only Andre approves a contract's terms (TERMS_APPROVED_BY_ANDRE); agents draft, never commit.
* The purchase gate (ADR 0019 decision 17), at authorization and at submission, requires the authorization's
  contract to be approved and to match it exactly: vendor, quotes, amount, cancellation terms (CONTRACT_MISMATCH),
  with the payment method still accepted.
* The order flow moves the contract: submission → SUBMITTED; the vendor's ACCEPTED → EXECUTED, REJECTED → VOID. A
  change request or a cancellation voids every open contract (its terms belong to the old brief).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

import policy as P
from errors import Conflict, Invalid, NotFound
from ledger import derived_id
from reasons import R
from svc_campaigns import observed


class ContractsMixin:
    def accepted_vendor_methods(self, vendor: str) -> Optional[list]:
        """The vendor's sourced, current payment methods (ADR 0020), or None when nobody sourced them."""
        e = (self.vendors.get(vendor) or {}).get("profile", {}).get("payment_methods")
        cur = next((h for h in e["history"] if h["current"]), None) if e else None
        return cur["value"] if cur else None

    def _method_problem(self, vendor: str, method: str) -> Optional[str]:
        accepted = self.accepted_vendor_methods(vendor)
        if accepted is None:
            return "VENDOR_PAYMENT_METHOD_UNKNOWN"
        if method not in accepted:
            return "VENDOR_PAYMENT_METHOD_NOT_ACCEPTED"
        return None

    def _contract_terms_problem(self, c: dict, k: dict) -> Optional[str]:
        """What a contract must still be true to, at draft, at approval and at the gate."""
        cm = c["commercial"]
        if cm is None:
            return "COMMERCIAL_MODEL_MISSING"
        qs = [self.prices.get(q) for q in k["quote_refs"]]
        if len(set(k["quote_refs"])) != len(k["quote_refs"]) or any(
                q is None or q["campaign_id"] != c["campaign_id"] or q["price_type"] != "VENDOR_QUOTE" for q in qs):
            return "QUOTE_NOT_FOUND"
        if any(q["vendor"] != k["vendor"] for q in qs):
            return "VENDOR_MISMATCH"
        if any(q.get("retired") for q in qs):
            return "QUOTE_RETIRED"
        if any(self._expired(q["valid_until"]) for q in qs):
            return "QUOTE_EXPIRED"
        if self._unit_in_two_quotes(qs):
            return "UNIT_IN_MULTIPLE_QUOTES"
        if any(q["currency"] != k["currency"] for q in qs):
            return "CURRENCY_MISMATCH"
        if Decimal(k["total"]) != sum((Decimal(q["amount"]) for q in qs), Decimal(0)):
            return "CONTRACT_TOTAL_MISMATCH"
        if k["cancellation_terms_ref"] != cm["cancellation_terms_ref"]:
            return "TERMS_CHANGED"
        return self._method_problem(k["vendor"], k["vendor_payment_method"])

    def draft_contract(self, actor: str, tid: str, cid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            rk = self.rk("contract", cid, body)
            prev = self._idem(actor, rk, body)
            if prev:
                return self.contract_view(prev[1])
            self.check_kill(tenant=tid, capability="purchase")
            self._writable(c, P.CONTRACT_WRITABLE)
            self.require_vendor(body["vendor"])
            if body["currency"] not in P.CURRENCIES:
                raise Invalid(R("CURRENCY_UNSUPPORTED"))
            k = {k: body[k] for k in ("vendor", "quote_refs", "total", "currency", "io_doc_ref",
                                      "cancellation_terms_ref", "payment_terms_days", "vendor_payment_method")}
            k["quote_refs"] = sorted(k["quote_refs"])
            code = self._contract_terms_problem(c, k)
            if code:
                raise Conflict(R(code))
            kid = derived_id("ctr", cid, actor, rk)
            superseded = sorted(o for o in c["contract_ids"] if self.contracts[o]["status"] in P.CONTRACT_OPEN
                                and set(self.contracts[o]["quote_refs"]) & set(k["quote_refs"]))
            data = {"contract_id": kid, "campaign_id": cid, **k, "superseded": superseded,
                    "vendor_text": observed(body.get("vendor_text"))}
            ev = {kk: v for kk, v in data.items() if kk != "vendor_text"}
            ev["vendor_text_sha256"] = (data["vendor_text"] or {}).get("sha256")
            self._commit("contract_drafted", self._req(data, actor, rk, body, kid), actor,
                         evidence=self._ev("vendor_contract_drafted", cid, ev, actor, rk))
            return self.contract_view(kid)

    def _a_contract_drafted(self, d, at):
        c = self.campaigns[d["campaign_id"]]
        for old in d.get("superseded", []):
            self._void_contract(c, old, "SUPERSEDED")
        self.contracts[d["contract_id"]] = {
            **{k: d[k] for k in ("contract_id", "campaign_id", "vendor", "quote_refs", "total", "currency",
                                 "io_doc_ref", "cancellation_terms_ref", "payment_terms_days",
                                 "vendor_payment_method", "vendor_text")},
            "status": "DRAFT", "drafted_by": d["actor"], "drafted_at": at, "approved_by": None, "approved_at": None,
            "void_reason": None}
        c["contract_ids"].append(d["contract_id"])
        c["version"] += 1

    def _void_contract(self, c: dict, kid: str, reason: str) -> None:
        """A contract leaves the open states; any unsubmitted authorization under it is void too."""
        k = self.contracts[kid]
        if k["status"] not in P.CONTRACT_OPEN:
            return
        k.update(status="VOID", void_reason=reason)
        for aid in c["authorization_ids"]:
            a = self.authorizations[aid]
            if a.get("contract_id") == kid and a["status"] == "AUTHORIZED" and a["submission_state"] == "NOT_SUBMITTED":
                a["status"], a["void_reason"] = "VOID", f"CONTRACT_{reason}"

    def approve_contract(self, tid: str, cid: str, kid: str, body: dict) -> dict:
        with self.lock:
            self._gate()
            c = self._campaign(tid, cid)
            k = self.contracts.get(kid)
            if k is None or k["campaign_id"] != cid:
                raise NotFound(R("CONTRACT_NOT_FOUND"))
            rk = self.rk("contract_approval", kid, body)
            if self._idem("andre", rk, body):
                return self.contract_view(kid)
            self.check_kill(tenant=tid, capability="purchase")
            self._writable(c, P.CONTRACT_WRITABLE)
            self._expect(c, body)
            if k["status"] != "DRAFT":
                raise Conflict(R("CONTRACT_NOT_DRAFT"))
            code = self._contract_terms_problem(c, k)
            if code:
                self._refuse(code, c, rk)
            data = {"contract_id": kid, "campaign_id": cid, "vendor": k["vendor"], "quote_refs": k["quote_refs"],
                    "total": k["total"], "vendor_payment_method": k["vendor_payment_method"]}
            self._commit("contract_approved", self._req(data, "andre", rk, body, kid), "andre",
                         evidence=self._ev("vendor_contract_terms_approved_by_andre", cid, data, "andre", rk))
            return self.contract_view(kid)

    def _a_contract_approved(self, d, at):
        self.contracts[d["contract_id"]].update(status="TERMS_APPROVED_BY_ANDRE", approved_by=d["actor"],
                                                approved_at=at)
        self.campaigns[d["campaign_id"]]["version"] += 1

    def contract_gate_problem(self, c: dict, a: dict) -> Optional[str]:
        """The purchase gate's contract checks (OOH-4), after the terms check."""
        k = self.contracts.get(a.get("contract_id") or "")
        if k is None or k["campaign_id"] != c["campaign_id"]:
            return "CONTRACT_NOT_FOUND"
        if k["status"] != "TERMS_APPROVED_BY_ANDRE":
            return "CONTRACT_NOT_APPROVED"
        if k["vendor"] != a["vendor"] or k["quote_refs"] != sorted(a["exact_quote_refs"]) \
                or k["total"] != a["authorized_amount"] or k["currency"] != a["authorized_currency"] \
                or k["cancellation_terms_ref"] != a["cancellation_terms_ref"]:
            return "CONTRACT_MISMATCH"
        return self._method_problem(k["vendor"], k["vendor_payment_method"])

    def contracts_on_order_submitted(self, a: dict) -> None:
        k = self.contracts.get(a.get("contract_id") or "")
        if k is not None:
            k["status"] = "SUBMITTED"

    def contracts_on_outcome(self, a: dict, accepted: bool) -> None:
        k = self.contracts.get(a.get("contract_id") or "")
        if k is not None and k["status"] == "SUBMITTED":
            k.update(status="EXECUTED" if accepted else "VOID", void_reason=None if accepted else "VENDOR_REJECTED")

    def contracts_on_campaign_move(self, c: dict, to: str) -> None:
        if to in ("CHANGE_REQUESTED", "CANCELLED"):
            for kid in c["contract_ids"]:
                self._void_contract(c, kid, f"CAMPAIGN_{to}")

    def contract_view(self, kid: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.contracts[kid]))

    def contract_for(self, tid: str, cid: str, kid: str) -> dict:
        with self.lock:
            self._campaign(tid, cid)
            k = self.contracts.get(kid)
            if k is None or k["campaign_id"] != cid:
                raise NotFound(R("CONTRACT_NOT_FOUND"))
            return self.contract_view(kid)

