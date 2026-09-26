"""
Intelligence 1 — Platform Metrics (spec §C.1): fetch through the right adapter, snapshot, quota. It never
invents, smooths or interpolates a value and never calls a platform that is not enabled.

Only the adapter creates a snapshot (§B.2); no route accepts one. A snapshot is one metric value exactly as
the platform returned it, with the SHA-256 of the raw response (the body itself is never stored).

Quota (per platform, per UTC day unless stated): YouTube units refused beyond VI_YT_DAILY_UNITS minus the
reserve VI_YT_RESERVE_UNITS — settlement fetches (priority 0) may use the reserve, nothing else may (ADR 0007
choice 13); TikTok a 60-second sliding window of VI_TT_MAX_PER_MIN (< 600) calls; Instagram at most
VI_IG_MAX_RPS calls per second; X at most VI_X_MONTHLY_RESOURCE_BUDGET owned reads per calendar month (0 =
none); TikTok oEmbed one call per clip per day and VI_OEMBED_MAX_RPS. A 429 backs off 1, 2, 4 … 60 s and is
recorded as ``platform_rate_limited``. A job skipped for quota records QUOTA_EXHAUSTED for that day.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

NUMBER, NAME, ACTOR = 1, "Platform Metrics", "intel_01_platform_metrics"
PRIORITY = {"settlement": 0, "approval": 0, "liveness": 1, "revision": 2, "metrics": 3, "anomaly": 3}
BACKOFF_MAX_S = 60


class QuotaBook:
    """In-memory counters rebuilt from the log's ``quota`` records (per platform and UTC day)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.yt_units: dict[str, int] = {}            # UTC date -> units spent
        self.tt_calls: deque = deque()                # epoch seconds of TikTok calls (60 s window)
        self.ig_last: dict[int, int] = {}             # epoch second -> calls
        self.x_month: dict[str, int] = {}             # YYYY-MM -> owned reads
        self.backoff_until: dict[str, float] = {}     # platform -> epoch seconds
        self.backoff_step: dict[str, int] = {}
        self.oembed_day: set[tuple[str, str]] = set()  # (submission id, UTC date)
        self.oembed_sec: dict[int, int] = {}

    def allow(self, platform: str, purpose: str, now: datetime) -> Optional[str]:
        """None when the call may go ahead; otherwise why not (a QUOTA_EXHAUSTED message)."""
        t = now.timestamp()
        if self.backoff_until.get(platform, 0) > t:
            return f"{platform} rate limited: backing off"
        day = now.astimezone(timezone.utc).date().isoformat()
        if platform == "youtube":
            cap = self.cfg.yt_daily_units - (0 if PRIORITY.get(purpose, 3) == 0 else self.cfg.yt_reserve_units)
            if self.yt_units.get(day, 0) + 1 > cap:
                return f"YouTube daily units exhausted for {purpose} ({self.yt_units.get(day, 0)} of {cap})"
        elif platform == "tiktok":
            while self.tt_calls and self.tt_calls[0] <= t - 60:
                self.tt_calls.popleft()
            if len(self.tt_calls) >= self.cfg.tt_max_per_min:
                return f"TikTok {self.cfg.tt_max_per_min}/min window full"
        elif platform == "instagram":
            if self.ig_last.get(int(t), 0) >= self.cfg.ig_max_rps:
                return "Instagram per-second budget used"
        elif platform == "x":
            month = day[:7]
            if self.x_month.get(month, 0) + 1 > self.cfg.x_monthly_resource_budget:
                return "X monthly owned-read budget exhausted (VI_X_MONTHLY_RESOURCE_BUDGET)"
        return None

    def spend(self, platform: str, units: int, at: datetime) -> None:
        t = at.timestamp()
        day = at.astimezone(timezone.utc).date().isoformat()
        if platform == "youtube":
            self.yt_units[day] = self.yt_units.get(day, 0) + max(units, 1)   # invalid requests cost >= 1 unit
        elif platform == "tiktok":
            self.tt_calls.append(t)
        elif platform == "instagram":
            self.ig_last = {k: v for k, v in self.ig_last.items() if k > int(t) - 5}
            self.ig_last[int(t)] = self.ig_last.get(int(t), 0) + 1
        elif platform == "x":
            self.x_month[day[:7]] = self.x_month.get(day[:7], 0) + max(units, 1)

    def rate_limited(self, platform: str, at: datetime) -> int:
        step = min(self.backoff_step.get(platform, 0) + 1, 7)
        self.backoff_step[platform] = step
        wait = min(2 ** (step - 1), BACKOFF_MAX_S)
        self.backoff_until[platform] = at.timestamp() + wait
        return wait

    def ok(self, platform: str) -> None:
        self.backoff_step.pop(platform, None)

    def oembed_allow(self, submission_id: str, now: datetime) -> bool:
        day = now.astimezone(timezone.utc).date().isoformat()
        sec = int(now.timestamp())
        if (submission_id, day) in self.oembed_day or self.oembed_sec.get(sec, 0) >= self.cfg.oembed_max_rps:
            return False
        return True

    def oembed_spend(self, submission_id: str, now: datetime) -> None:
        self.oembed_day.add((submission_id, now.astimezone(timezone.utc).date().isoformat()))
        sec = int(now.timestamp())
        self.oembed_sec = {k: v for k, v in self.oembed_sec.items() if k > sec - 5}
        self.oembed_sec[sec] = self.oembed_sec.get(sec, 0) + 1


def snapshot(snapshot_id: str, connection_id: str, submission_id: str, platform: str, video_id_sha256: Optional[str],
             metric: str, value: int, dimension: Optional[dict], fetched_at: str, source_endpoint: str,
             source_sha: Optional[str], adapter_version: str, retention_until: str, retention_rule: str) -> dict:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("a snapshot value is a non-negative integer, exactly as the platform returned it")
    return {"snapshot_id": snapshot_id, "connection_id": connection_id, "submission_id": submission_id,
            "platform": platform, "video_id_sha256": video_id_sha256, "metric": metric, "dimension": dimension,
            "value": value, "fetched_at": fetched_at, "source_endpoint": source_endpoint,
            "source_response_sha256": source_sha, "adapter_version": adapter_version,
            "retention_until": retention_until, "retention_rule": retention_rule}


def retention_until(fetched: datetime, days: int) -> str:
    return (fetched.astimezone(timezone.utc) + timedelta(days=days)).date().isoformat()
