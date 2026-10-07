"""
The closed reason-code catalog and the reason-item wire shape (Finance spec §A, §0.1.9).

A reason item is ``{code, rule_id, obligation_id, message, evidence_ids}``; ``message`` is at most 200 characters
and carries ids, dates, counts and money strings only (never names, bank data, TINs or free caller text).
``reason_lines`` render as ``fin/{rule_id}/{code}: {message}`` (at most 400 characters). A code may carry a
``:suffix`` naming the dependency (``DEPENDENCY_UNAVAILABLE:verification_integrity``); the base code picks the
default rule, and a caller may cite a more specific rule of the register (``rule=``) — G4 checks that every cited
rule exists in the version in force.
"""

from __future__ import annotations

from typing import Iterable, Optional

CATALOG: dict[str, str] = {
    "RULES_NOT_IN_FORCE": "FIN-00", "ENTITY_MIX": "FIN-01", "RESTRICTED_CASH_MISUSE": "FIN-01",
    "UNBALANCED": "FIN-02", "JOURNAL_INVALID": "FIN-02", "MONEY_FORMAT": "FIN-03",
    "NOT_CERTIFIED": "FIN-04", "COMPLIANCE_NOT_ALLOWED": "FIN-04", "CERT_CHANGED": "FIN-04",
    "COMPLIANCE_HOLD": "FIN-04", "CALLER_COUNT_REFUSED": "FIN-04",
    "RATE_CARD_MISSING": "FIN-05", "TIER_UNKNOWN": "FIN-05",
    "CAMPAIGN_NOT_FUNDED": "FIN-06", "OVER_BUDGET": "FIN-06",
    "NOT_APPROVED": "FIN-07", "APPROVAL_EXPIRED": "FIN-07", "RELEASE_TOO_EARLY": "FIN-07",
    "DEPENDENCY_UNAVAILABLE": "FIN-08", "RULE_NOT_IN_FORCE": "FIN-08", "CONTROL_RED": "FIN-08",
    "PAYABLE_IN_LIVE_ITEM": "FIN-09", "BELOW_MINIMUM": "FIN-10", "TAX_NOT_READY": "FIN-11",
    "B_NOTICE_OVERDUE": "FIN-12", "OFAC_STALE": "FIN-13", "OFAC_NOT_CLEAR": "FIN-13",
    "PAYEE_HOLD": "FIN-14", "CALLBACK_CONTACT_MISMATCH": "FIN-14", "LIMIT_EXCEEDED": "FIN-15",
    "CLAWBACK_WRITEOFF_TOO_EARLY": "FIN-16", "RECON_BREAK": "FIN-17", "TREASURY_BREACH": "FIN-18",
    "SOD_VIOLATION": "FIN-19", "ACCESS_REVIEW_OVERDUE": "FIN-20", "SURCHARGE_REFUSED": "FIN-21",
    "RECURRING_INCOMPLETE": "FIN-22", "DISPUTE_OPEN": "FIN-23", "PERIOD_LOCKED": "FIN-25",
    "COUNSEL_UNVERIFIED": "FIN-27", "TAX_TREATMENT_UNVERIFIED": "FIN-27", "SENSITIVE_DATA_REFUSED": "FIN-28",
    "BANNED_WORD": "FIN-29", "LEGAL_NOT_CURRENT": "FIN-29", "RAIL_NOT_READY": "FIN-30", "PAYEE_UNKNOWN": "FIN-30",
    "CARD_DISABLED": "FIN-21",
    # AEGIS round 17 (ADR 0009 amendment)
    "PAYABLE_IDENTITY_CONFLICT": "FIN-04", "AMOUNT_OUT_OF_RANGE": "FIN-03", "DEPOSIT_SHORTFALL": "FIN-18",
    # media billing (ADR 0009 amendment, Oct 5 2026)
    "CARD_NOT_ALLOWED": "FIN-31", "COLLECT_BEFORE_PAY": "FIN-31",
    # Stripe incoming (ADR 0009 amendment, Oct 5 2026)
    "STRIPE_NOT_ALLOWED": "FIN-31",
    # treasury settlement by Andre (AEGIS 25290ee M-N3)
    "BANK_EVIDENCE_REQUIRED": "FIN-18",
}
MESSAGE_MAX = 200
LINE_MAX = 400


class UnknownReason(ValueError):
    pass


def item(code: str, message: str, evidence_ids: Iterable[str] = (), rule: Optional[str] = None,
         obligation_id: Optional[str] = None) -> dict:
    base = code.split(":", 1)[0]
    if base not in CATALOG:
        raise UnknownReason(code)
    msg = " ".join(str(message).split())[:MESSAGE_MAX] or base.lower()
    return {"code": code[:96], "rule_id": rule or CATALOG[base], "obligation_id": obligation_id, "message": msg,
            "evidence_ids": sorted({e for e in evidence_ids if e})[:50]}


def line(r: dict) -> str:
    return f"fin/{r['rule_id']}/{r['code']}: {r['message']}"[:LINE_MAX]


def lines(reasons: list[dict]) -> list[str]:
    return [line(r) for r in reasons]


def dedupe(reasons: list[dict]) -> list[dict]:
    out: dict[tuple, dict] = {}
    for r in reasons:
        k = (r["code"], r["message"])
        if k in out:
            out[k]["evidence_ids"] = sorted(set(out[k]["evidence_ids"]) | set(r["evidence_ids"]))[:50]
        else:
            out[k] = dict(r)
    return sorted(out.values(), key=lambda r: (r["rule_id"], r["code"], r["message"]))


def codes(reasons: list[dict]) -> list[str]:
    return sorted({r["code"] for r in reasons})
