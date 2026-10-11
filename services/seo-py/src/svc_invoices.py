"""
Invoice verification for paid runs (ADR 0017 Wave 3, W3-2). A client tenant's audit or schedule names a Finance (31)
invoice id; with ``SEO_INVOICE_VERIFICATION=finance`` (the default) this service asks Finance before anything is
recorded, and refuses the run unless Finance confirms, for exactly that invoice:

  - it is Z Best Media's (entity ``zbm``) and belongs to this tenant's Finance client (``finance_client_id``, which
    Andre binds to the tenant: ``POST /tenants/{tid}/finance-client``);
  - it is in USD, ``paid``, with nothing refunded or charged back;
  - it has not paid for another run already: one invoice pays for one one-off audit, or for one schedule and every
    slot of it (founder-pending default 9). An interrupted audit (nothing delivered) gives its invoice back.

Finance's answer is either DEFINITIVE (not found, not ours, not paid, refunded: refused 409, and nobody can override
it — the fix is in Finance) or UNVERIFIABLE (Finance not configured, unreachable, refusing our token, or answering
something malformed: refused 503 with ``override_allowed``). Only for an UNVERIFIABLE answer may Andre override, by
naming a reason from a closed set (``invoice_override``) on a request that already carries his verified approval;
the override is recorded on the ledger as its own event. A reused invoice is refused whatever the override.

Network calls are made outside the service lock; every rule that depends on state is checked again after them.
``SEO_INVOICE_VERIFICATION=trust`` keeps the Wave 1/2 behaviour (the id is an unchecked input) and says so in
/status.
"""

from __future__ import annotations

from typing import Optional

from envelope import sha
from errors import Conflict, Invalid, Unavailable
from finance_client import DEFINITIVE, PAID, FinanceLookupError, assess
from reasons import R


class InvoicesMixin:
    def _invoice_owner(self, invoice_id: str) -> Optional[str]:
        """Who this invoice already pays for: ``schedule:<sid>`` (the schedule and every audit of it) or
        ``audit:<aid>`` (a one-off audit that was not interrupted). None when it is unused."""
        for s in self.schedules.values():
            if s.get("invoice_id") == invoice_id:
                return f"schedule:{s['schedule_id']}"
        for a in self.audits.values():
            if a.get("invoice_id") == invoice_id and a["status"] != "interrupted":
                return f"schedule:{a['schedule_id']}" if a.get("schedule_id") else f"audit:{a['audit_id']}"
        return None

    def _invoice_plan(self, tid: str, body: dict, andre: bool, owner: Optional[str] = None) -> Optional[dict]:
        """Under the lock: what the invoice check of this request will be. None = no invoice check (own tenant)."""
        t = self.tenants[tid]
        override = body.get("invoice_override")
        if t["kind"] != "client" or not body.get("invoice_id"):
            if override:
                raise Invalid(R("INVOICE_OVERRIDE_NOT_ALLOWED"))
            return None
        if self.settings.invoice_verification == "trust":
            if override:
                raise Invalid(R("INVOICE_OVERRIDE_NOT_ALLOWED"))
            return {"mode": "trust"}
        used = self._invoice_owner(body["invoice_id"])
        if used is not None and used != owner:
            raise Conflict(R("INVOICE_ALREADY_USED"))
        client = t.get("finance_client_id")
        if getattr(self.ports.finance, "connected", False) and not client:
            raise Conflict(R("TENANT_FINANCE_CLIENT_UNBOUND"))
        return {"mode": "finance", "invoice_id": body["invoice_id"], "client_id": client,
                "override": override if andre else None, "owner": owner}

    def _verify_invoice(self, plan: Optional[dict]) -> Optional[dict]:
        """Outside the lock: ask Finance. Returns the verification record, or raises the refusal."""
        if plan is None:
            return None
        if plan["mode"] == "trust":
            return {"mode": "trust", "verdict": "UNCHECKED"}
        facts = None
        try:
            facts = self.ports.finance.lookup(plan["invoice_id"])
            code = assess(facts, plan["client_id"] or "")
        except FinanceLookupError as exc:
            code = exc.code
        if code == PAID:
            return {"mode": "finance", "verdict": PAID, "cause": None, "override": None, "invoice_status": "paid",
                    "facts_sha256": sha(facts)}
        if code in DEFINITIVE:
            raise Conflict(R(code), override_allowed=False)
        if plan["override"]:
            return {"mode": "finance", "verdict": "OVERRIDDEN", "cause": code, "override": plan["override"],
                    "invoice_status": None, "facts_sha256": None}
        raise Unavailable(R(code), override_allowed=True)

    def _recheck_invoice(self, plan: Optional[dict]) -> None:
        """Under the lock again, after the Finance call: the invoice may have been used meanwhile."""
        if plan is not None and plan["mode"] == "finance":
            used = self._invoice_owner(plan["invoice_id"])
            if used is not None and used != plan["owner"]:
                raise Conflict(R("INVOICE_ALREADY_USED"))

    @staticmethod
    def _invoice_evidence(verification: Optional[dict], subject: str, ids: dict, invoice_id: Optional[str],
                          actor: str, rk: str) -> list:
        """The typed evidence of an invoice check: ``invoice_verified`` (Finance confirmed it) or
        ``invoice_verification_overridden_by_andre`` (Finance could not answer and Andre overrode)."""
        if verification is None or verification["mode"] != "finance":
            return []
        if verification["verdict"] == PAID:
            return [("invoice_verified", subject, {**ids, "invoice_id": invoice_id, "verdict": PAID,
                                                   "facts_sha256": verification["facts_sha256"]}, (actor, rk))]
        return [("invoice_verification_overridden_by_andre", subject,
                 {**ids, "invoice_id": invoice_id, "cause": verification["cause"],
                  "override": verification["override"]}, ("andre", rk))]

    def invoice_verification_status(self) -> dict:
        mode = self.settings.invoice_verification
        connected = bool(getattr(self.ports.finance, "connected", False))
        if mode == "trust":
            effect = "invoice ids are NOT checked with Finance (31): SEO_INVOICE_VERIFICATION=trust"
        elif connected:
            effect = "every paid run needs Finance (31) to confirm a paid, unused invoice of this tenant's client"
        else:
            effect = ("Finance (31) is not configured: every paid run is refused FINANCE_NOT_CONFIGURED unless Andre "
                      "overrides")
        return {"mode": mode, "finance": "connected" if connected else "NOT_CONNECTED", "effect": effect}
