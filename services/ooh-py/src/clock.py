"""Injectable clock so expiry, SLA and freshness logic is testable on a fixed date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().astimezone(timezone.utc).date()


@dataclass
class FixedClock:
    """A settable clock (tests, and the live demo scripts never use it)."""

    at: datetime

    def now(self) -> datetime:
        return self.at

    def today(self) -> date:
        return self.at.astimezone(timezone.utc).date()  # a UTC date whatever tz ``at`` carries (AEGIS N14-14)

    def advance(self, **kw) -> None:
        self.at = self.at + timedelta(**kw)


def iso(dt: datetime) -> str:
    """RFC 3339, UTC, second precision (what every record carries)."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an RFC 3339 timestamp that carries an offset; naive -> ValueError."""
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("timestamp must be an RFC 3339 string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp must carry a UTC offset")
    return dt.astimezone(timezone.utc)
