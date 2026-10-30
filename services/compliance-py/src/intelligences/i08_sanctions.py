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

Client screening (ADR 0024, HR-14): no screening provider is wired, so Andre searches OFAC's Sanctions List Search
himself and RECORDS the search (``POST /compliance/v1/sanctions/attestations``, his token): date, list version,
lists, result and evidence hash -- never the name itself (its SHA-256 only). ``attestation_problem`` decides whether a
recorded search lets a client activation pass; any hit refuses and holds the subject permanently.
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


ATTESTATION_LISTS = ("SDN", "Consolidated")
ATTESTATION_RESULTS = ("no_match", "potential_match", "match")


def _param_int(params: dict, key: str) -> Optional[int]:
    v = params.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 3650 else None


def attestation_problem(att: Optional[dict], subject_id: str, now: datetime, params: dict, *, lane: Optional[str],
                        age_key: str = "max_age_days") -> Optional[str]:
    """None when the recorded OFAC search lets ``subject_id`` pass now on ``lane``; otherwise why not (fail closed).
    ``age_key``: ``max_age_days`` at activation, ``rescreen_after_days`` for a current activation. The record is
    tied to the subject AND the lane it was recorded for (AEGIS F-7): a ZBC brand search does not activate a ZBM
    client, and the other way round."""
    days = _param_int(params, age_key)
    lists = params.get("lists")
    if days is None or not isinstance(lists, list) or not lists:
        return "HR-14 parameters are invalid: blocked"
    if att is None:
        return "no OFAC search is recorded under this id"
    if att.get("subject_id") != subject_id:
        return "the OFAC search on record is for another subject"
    if lane is None or att.get("lane") != lane:
        return f"the OFAC search on record was recorded for lane {att.get('lane')}, not {lane}"
    missing = [x for x in lists if x not in (att.get("lists") or [])]
    if missing:
        return f"the OFAC search did not cover: {', '.join(missing)}"
    if att.get("result") != "no_match":
        return f"OFAC search result {att.get('result')}: refused (a hit is never investigated around)"
    try:
        at = parse_iso(att["searched_at"])
    except (KeyError, TypeError, ValueError):
        return "the OFAC search has no valid date"
    if at > now + timedelta(minutes=5):
        return "the OFAC search date is in the future"
    if at < now - timedelta(days=days):
        return f"the OFAC search is older than {days} days: re-screen required"
    return None
