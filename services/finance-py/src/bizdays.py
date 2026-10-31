"""
US business days (MSA 1, changelog 45: "a weekday that is not a United States federal legal public holiday, as observed
under 5 U.S.C. 6103 (a holiday falling on a Saturday is observed on the Friday before, and one falling on a Sunday on
the Monday after)"; refund policy: refunds within 10 business days, acknowledgements within 2, billing disputes
within 15).

Computed in code for any year from 1986 (no seeded list that can run out):

- the 11 legal public holidays of 5 U.S.C. 6103(a): New Year's Day, Birthday of Martin Luther King, Jr. (3rd Monday of
  January), Washington's Birthday (3rd Monday of February), Memorial Day (last Monday of May), Juneteenth National
  Independence Day (June 19, from 2022), Independence Day, Labor Day (1st Monday of September), Columbus Day (2nd
  Monday of October), Veterans Day (November 11), Thanksgiving Day (4th Thursday of November), Christmas Day;
- observance under 5 U.S.C. 6103(b) and Executive Order 11582: a holiday on a SATURDAY is observed the Friday before
  (so New Year's Day 2028, a Saturday, is observed Friday December 31, 2027, a day of 2027), a holiday on a SUNDAY the
  Monday after.

Inauguration Day (Washington-area federal employees only) is not counted. Legal (legal-py seed/us_federal_holidays.json)
uses the same rule for 2026-2030; tests/test_bizdays.py checks both give the same dates.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

FIRST_YEAR = 1986          # the first year all of today's Monday holidays, MLK Day included, were observed
JUNETEENTH_FROM = 2022     # first full year (the law was signed June 17, 2021)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Monday = 0) of the month; n = -1 is the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


@lru_cache(maxsize=256)
def _federal(year: int) -> tuple:
    if not isinstance(year, int) or isinstance(year, bool) or not FIRST_YEAR <= year <= 9998:
        raise ValueError(f"no holiday calendar for {year!r} (supported: {FIRST_YEAR}-9998)")
    days = [
        (date(year, 1, 1), "new_years_day"),
        (_nth_weekday(year, 1, 0, 3), "birthday_of_martin_luther_king_jr"),
        (_nth_weekday(year, 2, 0, 3), "washingtons_birthday"),
        (_nth_weekday(year, 5, 0, -1), "memorial_day"),
        (date(year, 7, 4), "independence_day"),
        (_nth_weekday(year, 9, 0, 1), "labor_day"),
        (_nth_weekday(year, 10, 0, 2), "columbus_day"),
        (date(year, 11, 11), "veterans_day"),
        (_nth_weekday(year, 11, 3, 4), "thanksgiving_day"),
        (date(year, 12, 25), "christmas_day"),
    ]
    if year >= JUNETEENTH_FROM:
        days.append((date(year, 6, 19), "juneteenth"))
    return tuple(sorted(days))


def federal_holidays(year: int) -> dict:
    """The legal public holidays of ``year`` on their actual dates (before any observance shift)."""
    return dict(_federal(year))


def _observe(d: date) -> date:
    if d.weekday() == 5:                         # Saturday: the Friday before
        return d - timedelta(days=1)
    if d.weekday() == 6:                         # Sunday: the Monday after
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=256)
def _observed(year: int) -> tuple:
    out = [(_observe(d), name) for d, name in _federal(year)]
    if year + 1 <= 9998:                         # next New Year's Day on a Saturday is observed on December 31
        nxt = date(year + 1, 1, 1)
        out.append((_observe(nxt), "new_years_day"))
    return tuple(sorted((d, n) for d, n in out if d.year == year))


def observed_holidays(year: int) -> dict:
    """The weekdays of ``year`` that are legal public holidays as observed under 5 U.S.C. 6103 (date -> name)."""
    return dict(_observed(year))


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in observed_holidays(d.year)


def add_business_days(day: date, n: int) -> date:
    """The ``n``-th business day after ``day`` (``day`` itself never counts; 0 is ``day``). Weekends and observed
    federal legal public holidays are skipped."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("n must be a whole number of business days, 0 or more")
    d = day
    while n > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d
