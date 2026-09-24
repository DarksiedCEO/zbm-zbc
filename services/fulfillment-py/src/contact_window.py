"""
Outbound-contact quiet hours — a HARD rule, not a preference (Sep 24 2026
audit, see README "Audit, Sep 24 2026").

An automated system must not call or text a person at night in THEIR
local time (TCPA-style calling-hours rule: no earlier than 08:00, no
later than 21:00, recipient's local time). Before this module, the only
check was `8 <= now.hour < 20` against a UTC clock, which dialed a Los
Angeles caller at 02:00 local.

Rules enforced here:
  * The window is evaluated in the RECIPIENT's IANA time zone, via
    zoneinfo (so DST is handled by the tz database, not by us).
  * Unknown, empty or invalid time zone => not allowed (fail closed).
  * Naive `now` => ValueError (never guess what clock a naive time is on).
  * Start inclusive, end exclusive: 08:00:00 allowed, 21:00:00 not.
  * The window is configurable (FULFILLMENT_CONTACT_WINDOW="HH:MM-HH:MM")
    but can only be NARROWED inside 08:00-21:00, never widened, and must
    be non-empty. A bad value refuses service startup.

Fix wave 1, F3: `allows()` answers "is it daytime in THIS zone"; it does
not decide which zone. Callers must not use it directly for contact
decisions. Every outbound contact (call, SMS, email) is authorized by
OutboundContactGate (src/outbound_gate.py), which applies this window in
every zone the NUMBER could be in (src/recipient_zones.py) plus the
claimed zone, adds per-number attempt limits, and is the only source of
the ContactAuthorization a dialer or message sender needs to reach anyone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HARD_EARLIEST = time(8, 0)
HARD_LATEST = time(21, 0)

_WINDOW_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])$")


@lru_cache(maxsize=1024)
def resolve_timezone(name: str | None) -> ZoneInfo | None:
    """Returns the ZoneInfo, or None for anything unknown/invalid (callers
    treat None as "do not contact").

    Memoized, bounded (fix wave 4): zoneinfo itself keeps only 8 zones
    strongly cached and the gate checks up to 44 per +1 number, so every
    check re-read tz files from disk (~4.2 s for one 1000-task batch, under
    the dial lock). A cached None is still None: fail closed is unchanged."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


@dataclass(frozen=True)
class ContactWindow:
    start: time
    end: time

    def __post_init__(self) -> None:
        if not (HARD_EARLIEST <= self.start < self.end <= HARD_LATEST):
            raise ValueError(
                f"contact window {self.start:%H:%M}-{self.end:%H:%M} must be non-empty and "
                f"inside {HARD_EARLIEST:%H:%M}-{HARD_LATEST:%H:%M} recipient local time"
            )

    @classmethod
    def default(cls) -> "ContactWindow":
        return cls(HARD_EARLIEST, HARD_LATEST)

    @classmethod
    def from_hm(cls, sh: int, sm: int, eh: int, em: int) -> "ContactWindow":
        return cls(time(sh, sm), time(eh, em))

    def describe(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M} recipient local time"

    def allows(self, now: datetime, recipient_tz: str | None) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("contact window check requires a timezone-aware `now`")
        tz = resolve_timezone(recipient_tz)
        if tz is None:
            return False
        local = now.astimezone(tz).time()
        return self.start <= local < self.end


def parse_contact_window(value: str) -> ContactWindow:
    m = _WINDOW_RE.match(value.strip())
    if not m:
        raise ValueError(f"contact window {value!r} is not in HH:MM-HH:MM form")
    sh, sm, eh, em = (int(g) for g in m.groups())
    return ContactWindow.from_hm(sh, sm, eh, em)
