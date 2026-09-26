"""
Intelligence 4 — Liveness Monitor (spec §C.4): live | gone | private | unknown per UTC day. A missing check
is never treated as live.

Required days: every UTC date after the posting date through the date of ``min_live_end`` (ADR 0007 choice
12); the settlement fetch (at ``settle_at`` ≥ ``min_live_end``) is the closing check. Any ``gone``/``private``
day before ``min_live_end`` → DELETED_BEFORE_MIN_LIVE; a past required day with no ``live`` /
``live_public_fallback`` state is a gap; more than VI_LIVENESS_MAX_GAP_DAYS gaps → LIVENESS_GAP. A day proven
only by the TikTok oEmbed fallback counts at most VI_OEMBED_MAX_CONSECUTIVE_DAYS days in a row; the rest are
gaps. A day skipped for quota is ``unknown`` with cause QUOTA_EXHAUSTED (then VI-06 decides: a gap).

``copyright_strike`` (§C.4.3, VI-22): false only when the clip was live every required day, Legal 37's
takedown intake answered with none, no rights restriction came back, AND rule VI-22 is verified by Andre.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from reasons import item

NUMBER, NAME, ACTOR = 4, "Liveness Monitor", "intel_04_liveness"
OK_STATES = ("live", "live_public_fallback")
STATES = ("live", "live_public_fallback", "gone", "private", "unknown")


def required_days(create_time: int, min_live_end: datetime) -> list[date]:
    start = datetime.fromtimestamp(create_time, timezone.utc).date() + timedelta(days=1)
    end = min_live_end.astimezone(timezone.utc).date()
    out, d = [], start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def evaluate(days: list[date], states: dict, today: date, *, max_gap_days: int, oembed_max_consecutive: int,
             rules: dict, evidence_for=lambda d: ()) -> tuple[str, list[dict]]:
    """``states``: date -> {"state", "cause", "check_id"}. Returns (outcome, reasons): outcome ``pass`` when every
    required day is past and fine, ``pending`` while required days remain, ``fail`` otherwise."""
    reasons = []
    gaps: list[date] = []
    streak = 0
    for d in days:
        st = states.get(d)
        s = st["state"] if st else None
        if s in ("gone", "private"):
            reasons.append(item("DELETED_BEFORE_MIN_LIVE", f"post {s} on {d.isoformat()} (day {days.index(d) + 1} of "
                                f"{len(days)})", evidence_for(d), rules))
            streak = 0
            continue
        if s == "live_public_fallback":
            streak += 1
            if streak > oembed_max_consecutive:
                gaps.append(d)
            continue
        streak = 0
        if s == "live":
            continue
        if d < today or (st is not None and s == "unknown"):
            gaps.append(d)
    if len(gaps) > max_gap_days:
        causes = sorted({(states.get(d) or {}).get("cause") or "no check" for d in gaps})
        reasons.append(item("LIVENESS_GAP", f"{len(gaps)} day(s) without liveness evidence (first "
                            f"{gaps[0].isoformat()}; {', '.join(causes)[:80]})", [e for d in gaps for e in evidence_for(d)],
                            rules))
    if reasons:
        return "fail", reasons
    if days and days[-1] >= today and not all((states.get(d) or {}).get("state") in OK_STATES for d in days):
        return "pending", []
    return "pass", []


def copyright_strike(liveness_outcome: str, takedown_available: bool, takedown_notices: int,
                     rights_restricted: Optional[bool], vi22_verified: bool, rules: dict,
                     evidence: tuple = ()) -> tuple[bool, list[dict]]:
    if (liveness_outcome == "pass" and takedown_available and takedown_notices == 0 and rights_restricted is False
            and vi22_verified):
        return False, []
    why = []
    if not vi22_verified:
        why.append("Andre has not ruled that the VI-22 basis suffices")
    if not takedown_available:
        why.append("Legal 37 takedown intake unavailable")
    elif takedown_notices:
        why.append(f"{takedown_notices} takedown notice(s)")
    if liveness_outcome != "pass":
        why.append("not live every required day")
    if rights_restricted is not False:
        why.append("rights-restriction status unknown" if rights_restricted is None else "rights restriction reported")
    return True, [item("STRIKE_STATUS_UNKNOWN", "copyright strike not excluded: " + "; ".join(why), evidence, rules)]
