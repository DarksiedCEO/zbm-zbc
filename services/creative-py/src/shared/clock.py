"""Injectable clock so expiry logic is testable on a fixed date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().date()


@dataclass
class FixedClock:
    at: datetime

    def now(self) -> datetime:
        return self.at

    def today(self) -> date:
        return self.at.date()
