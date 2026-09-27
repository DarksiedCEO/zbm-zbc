"""
Business-day arithmetic (Legal spec §C.7, spec choice): a business day is Monday-Friday excluding the US federal
holidays in ``seed/us_federal_holidays.json`` (observed dates, 2026-2030). The statute's own business-day
definition is not in the research; counsel confirms before first use. The day of receipt is not counted. A date
whose count would leave the seeded range raises ``HolidaysUnknown`` (fail closed: never guess a holiday).
"""

from __future__ import annotations

import json
from datetime import date, timedelta


class HolidaysUnknown(ValueError):
    pass


class BusinessCalendar:
    def __init__(self, raw: bytes):
        doc = json.loads(raw)
        self.first = date(doc["first_year"], 1, 1)
        self.last = date(doc["last_year"], 12, 31)
        self.holidays = frozenset(date.fromisoformat(h["date"]) for h in doc["holidays"])

    def is_business_day(self, d: date) -> bool:
        if not self.first <= d <= self.last:
            raise HolidaysUnknown(f"{d.isoformat()} is outside the seeded holiday list "
                                  f"({self.first.year}-{self.last.year})")
        return d.weekday() < 5 and d not in self.holidays

    def add(self, start: date, n: int) -> date:
        """The n-th business day after ``start`` (``start`` itself not counted); n >= 1. Negative n counts
        backwards (the n-th business day before ``start``)."""
        if n == 0:
            return start
        step = 1 if n > 0 else -1
        d, k = start, 0
        while k < abs(n):
            d += timedelta(days=step)
            if self.is_business_day(d):
                k += 1
        return d

    def restore_window(self, received: date, lo: int = 10, hi: int = 14) -> tuple[date, date]:
        """§512(g)(2): restore not less than ``lo`` nor more than ``hi`` business days after receipt."""
        return self.add(received, lo), self.add(received, hi)
