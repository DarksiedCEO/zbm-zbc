"""
Intelligence 6 — Engagement Anomaly (spec §C.6, VI-09): hold | no_hold + reasons. Never decides payout and
never releases its own hold (only Andre or a People-43-confirmed delegate does, §C.6).

Numeric thresholds are internal policy — "no primary source publishes numeric thresholds" (research KQ4) —
so every one is config (VI_ANOM_*). Each signal is ``fired | clear | not_applicable | unavailable |
insufficient_history | not_evaluated``; ``unavailable`` on any applicable signal → hold INSUFFICIENT_SIGNAL.
The decision-rate analogue (after MRC) = applicable signals evaluated / applicable signals.

YouTube (R2): a signal that divides or combines API values (velocity, like rate, geography share, cap
proximity) is computed only with VI_YT_DERIVED_SIGNALS=1; otherwise it is ``not_evaluated`` and the clip
holds YT_DERIVED_USE_UNRESOLVED. YouTube baselines never mix clips posted on both sides of Aug 24, 2026.
Ratios use exact fractions (no floats).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from statistics import median_low
from typing import Optional

from platforms import YT_VIEW_DEFINITION_CHANGE
from reasons import item

NUMBER, NAME, ACTOR = 6, "Engagement Anomaly", "intel_06_engagement_anomaly"
SIGNALS = ("velocity", "near_zero_engagement", "watch_time", "geography", "cap_proximity", "platform_stripping")
DAY_MIN_S, DAY_MAX_S = 20 * 3600, 28 * 3600


@dataclass
class ScreenInput:
    platform: str
    create_time: int
    views_series: list                      # [(epoch seconds, views)], ascending
    views: Optional[int]
    likes: Optional[int]
    avg_view_percentage: Optional[int]
    reels_avg_watch_time_ms: Optional[int]
    duration_ms: Optional[int]
    country_views: dict = field(default_factory=dict)
    target_countries: Optional[list] = None
    baseline_peaks: list = field(default_factory=list)   # peak 24-h gains of the clipper's certified clips
    cap_available: bool = False
    cap: Optional[int] = None
    baseline: str = "clipper"               # "clipper" (its own certified clips) | "platform" (a new identity)


def peak_daily_gain(series: list) -> Optional[int]:
    best = None
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if DAY_MIN_S <= t1 - t0 <= DAY_MAX_S:
            g = v1 - v0
            best = g if best is None else max(best, g)
    return best


def same_era(platform: str, a: int, b: int) -> bool:
    if platform != "youtube":
        return True
    cut = YT_VIEW_DEFINITION_CHANGE.timestamp()
    return (a >= cut) == (b >= cut)


def screen(x: ScreenInput, cfg, rules: dict, evidence: tuple = ()) -> dict:
    yt_blocked = x.platform == "youtube" and not cfg.yt_derived_signals
    sig: dict[str, dict] = {}

    def put(name, status, detail=""):
        sig[name] = {"status": status, "detail": detail[:160]}

    # velocity (all platforms, from the daily snapshots)
    if yt_blocked:
        put("velocity", "not_evaluated", "YouTube derived signals off (VI-CQ-01)")
    else:
        peak = peak_daily_gain(x.views_series)
        if peak is None:
            put("velocity", "unavailable", "no pair of daily view snapshots ~24 h apart")
        elif len(x.baseline_peaks) < cfg.anom_min_history:
            # bug sweep C: the service falls back to the platform's certified clips for a new identity, so this is
            # reached only while the PLATFORM itself has too little certified history (bootstrap)
            put("velocity", "insufficient_history", f"{len(x.baseline_peaks)} certified clip(s) in the {x.baseline} "
                "baseline")
        else:
            base = max(median_low(x.baseline_peaks), 1)
            fired = peak > cfg.anom_velocity_multiple * base
            put("velocity", "fired" if fired else "clear", f"peak 24-h gain {peak} vs {cfg.anom_velocity_multiple} x "
                f"{x.baseline} median {base}")
    # near-zero engagement
    if yt_blocked:
        put("near_zero_engagement", "not_evaluated", "YouTube derived signals off (VI-CQ-01)")
    elif x.views is None:
        put("near_zero_engagement", "unavailable", "views missing")
    elif x.views < cfg.anom_ratio_min_views:
        put("near_zero_engagement", "clear", f"views {x.views} below {cfg.anom_ratio_min_views}")
    elif x.likes is None:
        put("near_zero_engagement", "unavailable", "likes missing")
    else:
        fired = Fraction(x.likes, x.views) < cfg.anom_min_like_rate
        put("near_zero_engagement", "fired" if fired else "clear", f"likes {x.likes} / views {x.views}")
    # watch time (YouTube averageViewPercentage; Instagram reels average watch time when the duration is known)
    if x.platform == "youtube":
        if x.views is not None and x.views < cfg.anom_ratio_min_views:
            put("watch_time", "clear", "below the view floor")
        elif x.avg_view_percentage is None:
            put("watch_time", "unavailable", "averageViewPercentage missing")
        else:
            put("watch_time", "fired" if x.avg_view_percentage < cfg.anom_min_avg_view_pct else "clear",
                f"average view percentage {x.avg_view_percentage}")
    elif x.platform == "instagram" and x.duration_ms:
        if x.views is not None and x.views < cfg.anom_ratio_min_views:
            put("watch_time", "clear", "below the view floor")
        elif x.reels_avg_watch_time_ms is None:
            put("watch_time", "unavailable", "ig_reels_avg_watch_time missing")
        else:
            pct = Fraction(x.reels_avg_watch_time_ms * 100, x.duration_ms)
            put("watch_time", "fired" if pct < cfg.anom_min_avg_view_pct else "clear", "reels average watch time")
    else:
        put("watch_time", "not_applicable", f"{x.platform}: no watch-time data (or no duration)")
    # geography (YouTube Analytics country only)
    if x.platform != "youtube":
        put("geography", "not_applicable", f"{x.platform}: no geography data")
    elif yt_blocked:
        put("geography", "not_evaluated", "YouTube derived signals off (VI-CQ-01)")
    elif not x.target_countries or not x.country_views:
        put("geography", "unavailable", "campaign targets or country split missing")
    else:
        total = sum(x.country_views.values())
        off = sum(v for c, v in x.country_views.items() if c not in set(x.target_countries))
        fired = total > 0 and Fraction(off, total) > cfg.anom_off_target_share
        put("geography", "fired" if fired else "clear", f"{off} of {total} views outside the targets")
    # payout-threshold proximity (campaign view cap from Clipper Network)
    if yt_blocked:
        put("cap_proximity", "not_evaluated", "YouTube derived signals off (VI-CQ-01)")
    elif not x.cap_available:
        put("cap_proximity", "unavailable", "Clipper Network view cap unavailable")
    elif x.cap is None:
        put("cap_proximity", "not_applicable", "campaign has no view-denominated cap")
    elif x.views is None:
        put("cap_proximity", "unavailable", "views missing")
    else:
        # bug sweep C: landing at or just above the cap fires; so does OVERSHOOTING it (views past cap + margin were
        # "clear" before: a clip that blew straight through the payout cap was the least suspicious of all)
        fired = x.views >= x.cap
        put("cap_proximity", "fired" if fired else "clear",
            f"views {x.views} vs cap {x.cap}" + (" (overshoot)" if x.views > x.cap + x.cap * cfg.anom_cap_proximity
                                                 else ""))
    put("platform_stripping", "not_applicable", "evaluated by the revision watch (C.2)")

    applicable = [n for n, s in sig.items() if s["status"] != "not_applicable"]
    evaluated = [n for n in applicable if s_ok(sig[n]["status"])]
    reasons = []
    fired = [n for n in applicable if sig[n]["status"] == "fired"]
    unavailable = [n for n in applicable if sig[n]["status"] == "unavailable"]
    if fired:
        reasons.append(item("ANOMALY_HOLD", "anomaly signal(s) fired: " + ", ".join(fired), evidence, rules))
    if unavailable:
        reasons.append(item("INSUFFICIENT_SIGNAL", "signal(s) unavailable: " + ", ".join(unavailable), evidence, rules))
    if yt_blocked:
        reasons.append(item("YT_DERIVED_USE_UNRESOLVED", "YouTube derived anomaly signals are off pending VI-CQ-01",
                            evidence, rules))
    return {"decision": "hold" if reasons else "no_hold", "signals": sig, "reasons": reasons,
            "decision_rate": {"evaluated": len(evaluated), "applicable": len(applicable)}}


def s_ok(status: str) -> bool:
    return status in ("fired", "clear", "insufficient_history")


def baseline_ok(platform: str, create_time: int, other_create_time: int) -> bool:
    return same_era(platform, create_time, other_create_time)


def now_epoch(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())
