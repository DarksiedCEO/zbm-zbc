"""
Intelligence 8 — Sanctions Screen (spec C.6).

The provider port screens; this module decides what a stored screen means:
``clear`` / ``potential_match`` (hold, Andre releases) / ``match``
(permanent block; no API release) / ``unavailable`` (block). A screen is
FRESH when ``screened_at >= now - COMPLIANCE_SANCTIONS_FRESHNESS_DAYS`` and
its ``list_version`` equals the provider's current list version (as last
refreshed by control C-04). DOB goes to the provider only: it is never
stored or logged. The payout gate never calls the provider (no PII in
payout facts); a stale screen blocks with "re-screen required".
Never does: clear a potential match itself; store DOB.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from clock import parse_iso

NUMBER, NAME, ACTOR = 8, "Sanctions Screen", "intel_08_sanctions"


def freshness_problem(screen: Optional[dict], now: datetime, freshness_days: int,
                      current_list_version: Optional[str]) -> Optional[str]:
    if screen is None:
        return "no sanctions screen on record"
    result = screen.get("result")
    if result == "unavailable":
        return "sanctions screening provider unavailable: no screen result"
    if result == "match":
        return "sanctions match: permanent block (counsel)"
    if result == "potential_match":
        return "potential sanctions match: held for Andre's review"
    if result != "clear":
        return "sanctions screen not clear"
    try:
        at = parse_iso(screen["screened_at"])
    except (KeyError, ValueError):
        return "sanctions screen has no valid screening time"
    if at < now - timedelta(days=freshness_days):
        return f"sanctions screen older than {freshness_days} day(s): re-screen required"
    if at > now + timedelta(minutes=5):
        return "sanctions screen time is in the future: re-screen required"
    if current_list_version is None:
        return "current sanctions list version unknown (control C-04 has not refreshed it): re-screen required"
    if screen.get("list_version") != current_list_version:
        return "sanctions screen ran against an old list version: re-screen required"
    return None
