"""
The closed reason-code catalog and the reason-item wire shape (Legal spec §A, §0.1.6: every refusal, block or
escalation cites a written rule id).

Item: ``{"code", "rule_id", "cq_id" | null, "message" (<= 200), "evidence_ids"}``; ``reason_lines`` render as
``legal/{rule_id}/{code}: {message}`` (<= 400 chars). A message is written by Legal from ids, dates and codes
only (never caller text), and it passes the advice-text guard at construction: a message that reads as advice
is a programming error and raises (so it can never reach a caller).
"""

from __future__ import annotations

from typing import Iterable, Optional

from advice import default_guard

CATALOG: dict[str, str] = {
    "RULES_NOT_IN_FORCE": "LG-00",
    "ADVICE_TEXT_BLOCKED": "LG-01", "ROUTE_ONLY": "LG-01", "UNREVIEWED": "LG-01",
    "COUNSEL_RECORD_MISSING": "LG-02", "HASH_MISMATCH": "LG-02", "VERSION_NOT_APPROVABLE": "LG-02",
    "NO_CURRENT_VERSION": "LG-02", "DOCUMENT_UNKNOWN": "LG-02", "NO_APPROVED_TEMPLATE": "LG-02",
    "ACCEPTANCE_HASH_MISMATCH": "LG-03", "PRESENTED_TEXT_MISMATCH": "LG-03", "VERSION_NOT_IN_FORCE": "LG-03",
    "AFFIRMATIVE_ACT_MISSING": "LG-03", "ESIGN_CONSENT_MISSING": "LG-03", "EVIDENCE_INSUFFICIENT": "LG-03",
    "ESIGN_UNAVAILABLE": "LG-03", "ESIGN_DOC_TYPE_EXCLUDED": "LG-03", "SIGNED_HASH_MISMATCH": "LG-03",
    "ENTITY_MISMATCH": "LG-03",
    "DEVIATION_ESCALATED": "LG-04", "WALK_AWAY": "LG-04", "UNMATCHED_CLAUSE": "LG-04",
    "NO_APPROVED_PLAYBOOK": "LG-04", "FACT_UNKNOWN": "LG-04", "COUNTERPARTY_PAPER": "LG-04",
    "PLAYBOOK_MEMO_MISSING": "LG-05", "WEAKENING_NOT_ACKNOWLEDGED": "LG-05",
    "NO_SUFFICIENT_ACCEPTANCE": "LG-06", "OBLIGATION_DUE_SOON": "LG-06", "OBLIGATION_MISSED": "LG-06",
    "NOT_OWNER": "LG-06",
    "MEMO_DOES_NOT_CITE": "LG-07", "MEMO_DUPLICATE": "LG-07", "MEMO_CITES_ALIAS": "LG-07",
    "MEMO_UNKNOWN": "LG-07", "PENDING_COMPLIANCE": "LG-07",
    "COUNSEL_SAME_DAY": "LG-08", "AMBIGUOUS_ESCALATED": "LG-08", "COUNSEL_STANDARD": "LG-08",
    "HELD": "LG-09", "HOLD_RELEASE_NEEDS_MEMO": "LG-09", "NOT_FROZEN": "LG-09", "NOTICE_TEMPLATE_MISSING": "LG-09",
    "NOTICE_INVALID": "LG-10", "RESTORE_WINDOW_NOT_OPEN": "LG-10", "CLAIMANT_ACTION_FILED": "LG-10",
    "HOLIDAYS_UNKNOWN": "LG-10", "RESTORE_LATE": "LG-10", "TAKEDOWN_STATE": "LG-10",
    "FILING_LAPSED": "LG-11", "FILING_DUE_SOON": "LG-11", "FILING_WINDOW_OPEN": "LG-11",
    "MUSIC_CHANGED": "LG-12", "MUSIC_LICENSED_BLOCKED": "LG-12", "TRACK_ID_MISSING": "LG-12",
    "PLATFORM_LIBRARY_UNVERIFIED": "LG-12", "HELD_PENDING_COUNSEL": "LG-12", "MUSIC_SOURCE_UNDECLARED": "LG-12",
    "RETENTION_UNVERIFIED": "LG-13",
    "INJECTION_TEXT_IGNORED": "LG-14",
    "SUBPOENA_NOTHING_PRODUCED": "LG-15",
    "DSAR_CLOCK_UNVERIFIED": "LG-16",
    "ENGAGEMENT_NOT_APPROVED": "LG-17", "ENGAGEMENT_AI_CLAUSE_MISSING": "LG-17", "COUNSEL_NOT_DELIVERED": "LG-17",
    "CQ_UNVERIFIED": "LG-18", "SIGNOFF_NOT_VERIFIED": "LG-18", "SIGNOFF_OUT_OF_SCOPE": "LG-18",
    "TOPIC_UNKNOWN": "LG-18",
    "OUTBOUND_NEEDS_COUNSEL": "LG-19",
}
CODE_CITED = frozenset(CATALOG.values())
MESSAGE_MAX = 200
LINE_MAX = 400


class UnknownReason(ValueError):
    pass


def item(code: str, message: str, evidence_ids: Iterable[str] = (), cq_id: Optional[str] = None) -> dict:
    if code not in CATALOG:
        raise UnknownReason(code)
    msg = " ".join(str(message).split())[:MESSAGE_MAX] or code.lower()
    if default_guard().scan(msg):
        raise AssertionError(f"reason message for {code} reads as advice; reword it (LG-01)")
    return {"code": code, "rule_id": CATALOG[code], "cq_id": cq_id, "message": msg,
            "evidence_ids": sorted({e for e in evidence_ids if e})[:50]}


def line(r: dict) -> str:
    return f"legal/{r['rule_id']}/{r['code']}: {r['message']}"[:LINE_MAX]


def lines(reasons: list[dict]) -> list[str]:
    return [line(r) for r in reasons]


def codes(reasons: list[dict]) -> list[str]:
    return sorted({r["code"] for r in reasons})


def rules_not_in_force() -> dict:
    return item("RULES_NOT_IN_FORCE", "no Andre-approved Legal rule version is in force")
