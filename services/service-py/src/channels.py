"""
Intelligence I4 — the outbound channel rules (ADR 0014 decisions 5-7). Deterministic, one job: may this message go
out on this channel to this contact NOW, and if not, why (a reason code)?

- SMS: only to a contact with a recorded, unrevoked EXPRESS SMS consent (consent registry), and only between 08:00
  and 21:00 in the recipient's own time zone; an unknown or invalid time zone refuses (we never guess). Outside the
  window a message waits (``QUIET_HOURS``); it is never sent early.
- Email: a reply on a ticket the contact opened is allowed unless they revoked email; anything proactive (check-in,
  survey, offer, renewal) needs a recorded, unrevoked email consent.
- Chat: messages into the contact's own portal / site thread; no consent record needed.
- Phone: never an outbound channel here (no voice provider).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

QUIET_START_HOUR = 8       # 08:00 local: first minute SMS may go
QUIET_END_HOUR = 21        # 21:00 local: first minute SMS may NOT go
STOP_WORDS = ("stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout", "opt out")


def zone(tz: Optional[str]) -> Optional[ZoneInfo]:
    if not tz or not isinstance(tz, str) or len(tz) > 64:
        return None
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def within_sms_hours(now: datetime, tz: Optional[str]) -> Optional[bool]:
    """True / False, or None when the time zone is unknown (the caller refuses)."""
    z = zone(tz)
    if z is None:
        return None
    local = now.astimezone(z)
    return QUIET_START_HOUR <= local.hour < QUIET_END_HOUR


def is_stop(text: str) -> bool:
    """The whole message is one opt-out keyword (CTIA practice), case and punctuation ignored."""
    t = " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())
    return t in STOP_WORDS


def check(channel: str, contact: dict, consent_active, proactive: bool, now: datetime) -> Optional[str]:
    """None when allowed now; else a reason code. ``consent_active(channel) -> bool``."""
    if channel == "sms":
        if not contact.get("phone"):
            return "NO_ADDRESS"
        if not consent_active("sms"):
            return "SMS_CONSENT_REQUIRED"
        ok = within_sms_hours(now, contact.get("timezone"))
        if ok is None:
            return "TIMEZONE_UNKNOWN"
        return None if ok else "QUIET_HOURS"
    if channel == "email":
        if not contact.get("email"):
            return "NO_ADDRESS"
        if contact.get("email_revoked"):
            return "EMAIL_CONSENT_REVOKED"
        if proactive and not consent_active("email"):
            return "EMAIL_CONSENT_REQUIRED"
        return None
    if channel == "chat":
        return None
    return "CHANNEL_NOT_OUTBOUND"
