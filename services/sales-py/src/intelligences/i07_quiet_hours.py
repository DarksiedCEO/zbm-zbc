"""Quiet hours: 8 am to 9 pm in the RECIPIENT's local time (TCPA 47 CFR 64.1200(c)(1); ADR 0013 decision 12).

Decides: whether a text or call may be placed now. The recipient's IANA time zone is required: unknown or invalid ->
refused (never the sender's zone, never a guess from the area code). Never: allows a call outside the window."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NUMBER = 7
NAME = "quiet_hours"
DECIDES = "whether now is inside 08:00-21:00 recipient-local"

START_HOUR = 8
END_HOUR = 21


def zone(name: Optional[str]) -> Optional[ZoneInfo]:
    if not name or not isinstance(name, str) or len(name) > 64 or name.startswith("/") or ".." in name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def allowed(now_utc: datetime, tz_name: Optional[str]) -> Optional[bool]:
    """True inside the window, False outside, None when the time zone is unknown (the caller refuses)."""
    z = zone(tz_name)
    if z is None:
        return None
    local = now_utc.astimezone(z)
    return START_HOUR <= local.hour < END_HOUR
