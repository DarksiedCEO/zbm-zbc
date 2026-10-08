"""
The closed reason-code catalog (spec §A.5) and the reason-item wire shape (§A).

Each code maps to exactly one rule. A reason item is
``{code, rule_id, evidence_ids, message, source_url}``; ``message`` is at most
200 characters and never carries PII (callers pass ids, dates and counts only);
``source_url`` is the cited rule's first source URL or null. ``reason_lines`` render
as ``vi/{rule_id}/{code}: {message}`` (at most 400 characters).
"""

from __future__ import annotations

from typing import Iterable, Optional

CATALOG: dict[str, str] = {
    "SUBMISSION_UNKNOWN": "VI-20", "FACTS_MISMATCH": "VI-20", "DUPLICATE_POST": "VI-20", "POSTED_AT_MISMATCH": "VI-20",
    "NOT_CONNECTED": "VI-17", "CONNECTION_REVOKED": "VI-17", "AUTHOR_MISMATCH": "VI-13",
    "ACCOUNT_SHARED": "VI-13", "PLATFORM_NOT_PAYABLE": "VI-18", "PLATFORM_DISABLED": "VI-18",
    "ADAPTER_UNAVAILABLE": "VI-03", "QUOTA_EXHAUSTED": "VI-03", "NOT_YET_SETTLED": "VI-04",
    "LAG_MISMATCH": "VI-04", "MIN_LIVE_UNKNOWN": "VI-06", "SETTLEMENT_SNAPSHOT_MISSING": "VI-03",
    "METRIC_UNAVAILABLE": "VI-03", "DELETED_BEFORE_MIN_LIVE": "VI-06", "LIVENESS_GAP": "VI-06",
    "HASH_MISMATCH": "VI-07", "FINGERPRINT_UNAVAILABLE": "VI-07", "CAPTION_CHANGED": "VI-08",
    "STOLEN_MATCH": "VI-14", "STOLEN_CHECK_INCOMPLETE": "VI-14", "ANOMALY_HOLD": "VI-09",
    "INSUFFICIENT_SIGNAL": "VI-09", "BOUGHT_ENGAGEMENT": "VI-10", "PLATFORM_STRIPPED": "VI-10",
    "AGE_MINOR": "VI-11", "AGE_NOT_ASSURED": "VI-11", "AGE_METHOD_NOT_HIGHLY_EFFECTIVE": "VI-11",
    "DUPLICATE_IDENTITY": "VI-12", "COLLAB_POST": "VI-19", "RULE_NOT_IN_FORCE": "VI-21",
    "DEPENDENCY_UNAVAILABLE": "VI-21", "REVISED_DOWN": "VI-05", "VOIDED": "VI-10",
    "YT_DERIVED_USE_UNRESOLVED": "VI-15", "YT_AGGREGATION_UNRESOLVED": "VI-15",
    "STRIKE_STATUS_UNKNOWN": "VI-22", "RULES_NOT_IN_FORCE": "VI-00",
}
MESSAGE_MAX = 200
LINE_MAX = 400


class UnknownReason(ValueError):
    pass


def item(code: str, message: str, evidence_ids: Iterable[str] = (), rules: Optional[dict] = None) -> dict:
    """One reason item. ``rules`` (rule_id -> row) supplies the source URL; a code outside the catalog is a
    programming error (raises), never a silent pass."""
    if code not in CATALOG:
        raise UnknownReason(code)
    rid = CATALOG[code]
    row = (rules or {}).get(rid) or {}
    urls = row.get("source_urls") or []
    msg = " ".join(str(message).split())[:MESSAGE_MAX] or code.lower()
    return {"code": code, "rule_id": rid, "evidence_ids": sorted({e for e in evidence_ids if e})[:50],
            "message": msg, "source_url": urls[0] if urls else None}


def line(r: dict) -> str:
    return f"vi/{r['rule_id']}/{r['code']}: {r['message']}"[:LINE_MAX]


def lines(reasons: list[dict]) -> list[str]:
    return [line(r) for r in reasons]


def dedupe(reasons: list[dict]) -> list[dict]:
    """Stable, one item per (code, message); evidence ids merged."""
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
