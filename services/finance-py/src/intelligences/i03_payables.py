"""
Intelligence 3 — Payables Accrual (Finance spec §B.4, §C.3). Pure judgment: from a V&I certification, an allowed
Compliance payout ruling, the campaign's commercial profile and the Andre-published rate card effective at the clip's
posting time, compute the payable. It never accepts a count, an amount or a rate from a caller.

    paid_views     = min(certified_views, max_paid_views_per_clip)
    amount         = quantize(paid_views x creator_rate[tier] / 1000, 0.01, HALF_UP)   -- ONE rounding (R1)
    revenue_amount = quantize(paid_views x client_rate / 1000, 0.01, HALF_UP)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import money as M
import reasons as R
from clock import parse_iso

NUMBER, NAME, ACTOR = 3, "Payables Accrual", "intel_03_payables"
FORBIDDEN_KEY_RE = r"(?i)(views|count|metric|amount|rate)"
TIERS = ("T0", "T1", "T2", "T3")


def pick_rate_card(versions: list[dict], create_time: Optional[str]) -> Optional[dict]:
    """The latest PUBLISHED version with effective_at <= the clip's posting time (CN-17)."""
    if not create_time:
        return None
    try:
        posted = parse_iso(create_time)
    except ValueError:
        return None
    ok = [v for v in versions if v["status"] == "published" and parse_iso(v["effective_at"]) <= posted]
    return max(ok, key=lambda v: (parse_iso(v["effective_at"]), v["version"])) if ok else None


def tiered(card: dict) -> bool:
    rates = card["creator_rate_per_1000"]
    return len({rates[t] for t in TIERS}) > 1


def compute(certified_views: int, card: dict, tier: str, client_rate: str) -> dict:
    paid = min(int(certified_views), int(card["max_paid_views_per_clip"]))
    rate = M.D(card["creator_rate_per_1000"][tier])
    amount = M.payable_amount(paid, rate)
    revenue = M.payable_amount(paid, M.D(client_rate))
    return {"paid_views": paid, "rate_per_1000": M.fmt(rate), "amount": M.fmt(amount),
            "client_rate_per_1000": M.fmt(M.D(client_rate)), "revenue_amount": M.fmt(revenue)}


def cert_reasons(cert, submission_id: str) -> list[dict]:
    """FIN-04 step 1: the certification as V&I answers it."""
    if not cert.available:
        return [R.item("DEPENDENCY_UNAVAILABLE:verification_integrity",
                       f"V&I certification for {submission_id} unavailable", rule="FIN-04")]
    out = []
    if cert.submission_id != submission_id:
        out.append(R.item("NOT_CERTIFIED", "V&I answered for a different submission"))
    if cert.status not in ("certified", "revised"):
        out.append(R.item("NOT_CERTIFIED", f"V&I status is {str(cert.status)[:20]}", [cert.certification_id or ""]))
    if cert.open_finding:
        out.append(R.item("NOT_CERTIFIED", "V&I has an open finding or hold on this clip", [cert.certification_id or ""]))
    if not isinstance(cert.certified_views, int) or isinstance(cert.certified_views, bool) or cert.certified_views < 0:
        out.append(R.item("NOT_CERTIFIED", "V&I gave no certified view count"))
    if not cert.certification_id or not cert.campaign_id or not cert.clipper_id or not cert.create_time:
        out.append(R.item("NOT_CERTIFIED", "V&I certification incomplete (id, campaign, clipper or posting time)"))
    return out


def ruling_reasons(ruling, submission_id: str, cert_time: Optional[str], ruling_id: Optional[str]) -> list[dict]:
    """FIN-04 step 2: Compliance payout ruling for this subject, gate payout, allowed, not older than the cert."""
    if not ruling_id:
        return [R.item("COMPLIANCE_NOT_ALLOWED", "the handoff names no Compliance payout ruling")]
    if not ruling.available:
        return [R.item("DEPENDENCY_UNAVAILABLE:compliance_38", f"Compliance ruling {ruling_id[:60]} unavailable",
                       rule="FIN-04")]
    out = []
    if ruling.ruling_id != ruling_id:
        out.append(R.item("COMPLIANCE_NOT_ALLOWED", "Compliance answered for a different ruling"))
    if ruling.gate != "payout":
        out.append(R.item("COMPLIANCE_NOT_ALLOWED", f"ruling gate is {str(ruling.gate)[:20]}, not payout", [ruling_id]))
    if ruling.subject_id != submission_id:
        out.append(R.item("COMPLIANCE_NOT_ALLOWED", "ruling is about a different subject", [ruling_id]))
    if not ruling.allowed:
        out.append(R.item("COMPLIANCE_NOT_ALLOWED", "Compliance payout ruling is not allowed", [ruling_id]))
    try:
        ev = parse_iso(ruling.evaluated_at) if ruling.evaluated_at else None
        ct = parse_iso(cert_time) if cert_time else None
    except ValueError:
        ev = ct = None
    if ev is None or (ct is not None and ev < ct):
        out.append(R.item("COMPLIANCE_NOT_ALLOWED", "ruling is older than the certification (re-rule after it)",
                          [ruling_id]))
    return out


def new_paid(certified_views: int, deltas: list[int], cap: int) -> int:
    return min(max(0, int(certified_views) + sum(int(d) for d in deltas)), int(cap))


def when(dt: datetime) -> str:
    return dt.isoformat()
