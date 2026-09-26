"""
Intelligence 6 — Clipper Comms (spec §C.6, CN-15, CN-16, CN-26; Onboarding P7).

Decides which approved template version, on which channel, and WHEN: inside
the recipient-local quiet window (CN-15 ``quiet_window``, default
08:00-20:00, applied to every channel — spec choice) a message may go now;
outside it waits until the window next opens. No time zone → in-app only
(sent at once: it waits in the hub). Canadian clippers get in-app only while
counsel row CN-CQ-06 is open. Never free-writes text: bodies are rendered
only from typed variables (templates.py).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

NUMBER, NAME, ACTOR = 6, "Clipper Comms", "intel_06_comms"
_WINDOW = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])")


@lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    return frozenset(available_timezones())


def valid_time_zone(name: str) -> bool:
    if not isinstance(name, str) or len(name) > 64 or name not in _zones():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_window(w: str) -> tuple[int, int]:
    m = _WINDOW.fullmatch(w or "")
    if not m:
        raise ValueError("quiet window must be HH:MM-HH:MM")
    a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
    if a == b:
        raise ValueError("quiet window must not be empty")
    return a, b


def _inside(minute: int, a: int, b: int) -> bool:
    return a <= minute < b if a < b else (minute >= a or minute < b)


def inside_window(now_utc: datetime, tz: str, window: str) -> bool:
    a, b = parse_window(window)
    local = now_utc.astimezone(ZoneInfo(tz))
    return _inside(local.hour * 60 + local.minute, a, b)


def send_after(now_utc: datetime, tz: str, window: str) -> datetime:
    """``now`` when the recipient-local time is inside the window, else the next local window opening (UTC)."""
    a, b = parse_window(window)
    zone = ZoneInfo(tz)
    local = now_utc.astimezone(zone)
    if _inside(local.hour * 60 + local.minute, a, b):
        return now_utc
    for add in range(0, 3):
        day = (local + timedelta(days=add)).date()
        cand = datetime(day.year, day.month, day.day, a // 60, a % 60, tzinfo=zone)
        if cand > local:
            return cand.astimezone(timezone.utc)
    raise RuntimeError("no window opening found")   # unreachable for a non-empty window


def channel_for(template: dict, clipper: dict, ca_member_email_blocked: bool) -> tuple[Optional[str], str]:
    """(channel, why). Member messages: email when the template allows it and the clipper has a time zone and is
    not held to in-app by CN-CQ-06; otherwise in_app."""
    chans = template["channels"]
    tz = clipper.get("time_zone")
    if "email" in chans and tz and not (ca_member_email_blocked and clipper.get("declared_country") == "CA"):
        return "email", "email"
    if "in_app" in chans:
        if not tz:
            return "in_app", "no time zone: in-app only (CN-15)"
        return "in_app", "in-app only for Canadian clippers while CN-CQ-06 is open" if clipper.get("declared_country") == "CA" \
            else "in_app"
    return None, "the template has no channel for this recipient"
