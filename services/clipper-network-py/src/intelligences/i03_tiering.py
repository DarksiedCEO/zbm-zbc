"""
Intelligence 3 — Tiering (spec §C.3, CN-10, CN-11).

Tier from verified outcomes only: V&I certifications in ``certified`` or
``revised`` (their CURRENT ``certified_views`` — a revision replaces the
value, nothing else changes) and the strike mirror. Never touches a count or
a rate (CN-11). T3 only by Andre's nomination on top of T2. An active S2
caps the tier at T0; an S3 suspends (Discipline) and caps at T0 too.
Numbers are CN-10 parameters (spec defaults, not sourced figures);
platform anchors are off (``platform_anchors`` false).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Iterable, Optional

from clock import parse_iso
from ports import Certification

NUMBER, NAME, ACTOR = 3, "Tiering", "intel_03_tiering"
TIERS = ("T0", "T1", "T2", "T3")


def rank(t: str) -> int:
    return TIERS.index(t)


def compute(certs: Iterable[Certification], strikes: list[dict], admitted_at: str, now: datetime, params: dict,
            nominated: bool) -> tuple[str, dict, list[str]]:
    counted = sorted((c for c in certs if c.status in ("certified", "revised") and isinstance(c.certified_views, int)),
                     key=lambda c: c.certification_id)
    ids = [c.certification_id for c in counted]
    days = (now - parse_iso(admitted_at)).days
    active = [s for s in strikes if s["status"] == "active"]
    s1 = sum(1 for s in active if s["class"] == "S1")
    s2 = any(s["class"] == "S2" for s in active)
    s3 = any(s["class"] == "S3" for s in active)
    t1p, t2p = params["t1"], params["t2"]
    excl = set(t2p["median_exclude_platforms"])
    median_pool = [c.certified_views for c in counted if c.platform not in excl]
    median = int(statistics.median(median_pool)) if median_pool else 0
    campaigns = len({c.campaign_id for c in counted})
    window = now - timedelta(days=t2p["no_upheld_strike_days"])
    recent_upheld = any(s["status"] != "overturned" and parse_iso(s["issued_at"]) >= window for s in strikes)
    t1 = (len(counted) >= t1p["min_certified_clips"] and days >= t1p["min_days_since_admission"]
          and not s2 and not s3 and s1 <= t1p["max_active_s1"])
    t2 = (t1 and len(counted) >= t2p["min_certified_clips"] and campaigns >= t2p["min_campaigns"]
          and median >= t2p["min_median_certified_views"] and days >= t2p["min_days_since_admission"]
          and not recent_upheld)
    t3 = t2 and nominated and params["t3"]["requires_andre_nomination"]
    tier = "T3" if t3 else "T2" if t2 else "T1" if t1 else "T0"
    if s2 or s3:
        tier = "T0"
    inputs = {"certified_clips": len(counted), "campaigns": campaigns, "median_certified_views": median,
              "median_pool": len(median_pool), "days_since_admission": days, "active_s1": s1, "active_s2": s2,
              "active_s3": s3, "upheld_strike_in_window": recent_upheld, "nominated": nominated}
    return tier, inputs, ids


def cap(tier: Optional[str], params: dict) -> int:
    return params["max_active_enrolments"][tier or "T0"]
