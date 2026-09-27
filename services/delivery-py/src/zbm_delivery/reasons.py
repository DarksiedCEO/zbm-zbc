"""
The closed reason-code catalog and the reason-item wire shape (spec §A).

``{"code", "rule_id", "message", "evidence_ids"}``; ``reason_lines`` render as ``dlv/{rule_id}/{code}: {message}``
(≤ 400 chars). Messages carry ids, hashes, counts and enums only — never prompt text, tool output or a key.
"""

from __future__ import annotations

from typing import Iterable

CATALOG: dict[str, str] = {
    "CONFIG_REFUSED": "DLV-01",
    "SANDBOX_UNAVAILABLE": "DLV-02",
    "LLM_NOT_CONFIGURED": "DLV-03",
    "LEDGER_UNAVAILABLE": "DLV-04",
    "RUN_IN_PROGRESS": "DLV-05",
    "GIT_REFUSED": "DLV-06",
    "NO_RUN": "DLV-07",
    "IDENTITY_MISMATCH": "DLV-07",
    "TOOL_DENIED": "DLV-08",
    "EGRESS_REFUSED": "DLV-09",
    "TRANSITION_REFUSED": "DLV-10",
    "ROUND_FAILED": "DLV-11",
    "BLOCKED": "DLV-12",
    "DEADLINE": "DLV-13",
    "CANCELLED": "DLV-14",
    "REVIEW_STATE": "DLV-15",
    "NEW_DEFECT": "DLV-16",
    "EVIDENCE_UNAVAILABLE": "DLV-17",
    "PRINCIPAL_MISSING": "DLV-07",
    "SUITE_FAILED": "DLV-16",
    "HARNESS_ERROR": "DLV-18",
}
MESSAGE_MAX = 200
LINE_MAX = 400


class UnknownReason(ValueError):
    pass


def item(code: str, message: str, evidence_ids: Iterable[str] = (), rule: str | None = None) -> dict:
    base = code.split(":", 1)[0]
    if base not in CATALOG:
        raise UnknownReason(code)
    msg = " ".join(str(message).split())[:MESSAGE_MAX] or base.lower()
    return {"code": code[:96], "rule_id": rule or CATALOG[base], "message": msg,
            "evidence_ids": sorted({e for e in evidence_ids if e})[:50]}


def line(r: dict) -> str:
    return f"dlv/{r['rule_id']}/{r['code']}: {r['message']}"[:LINE_MAX]


def lines(reasons: list[dict]) -> list[str]:
    return [line(r) for r in reasons]
