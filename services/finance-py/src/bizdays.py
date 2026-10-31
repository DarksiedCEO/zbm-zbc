"""
US business days on the Federal Reserve's holiday schedule (founder ask, Oct 31 2026; MSA: "a business day is a weekday
that is not a United States federal holiday"; refund policy: refunds within 10 business days, acknowledgements within
2, billing disputes within 15).

Computed in code for any year from 1986 on (no seeded list that can run out):

- the 11 legal public holidays of 5 U.S.C. 6103(a): New Year's Day, Birthday of Martin Luther King, Jr. (3rd Monday of
  January), Washington's Birthday (3rd Monday of February), Memorial Day (last Monday of May), Juneteenth National
  Independence Day (June 19, from 2022 on the Federal Reserve's schedule), Independence Day, Labor Day (1st Monday of
  September), Columbus Day (2nd Monday of October), Veterans Day (November 11), Thanksgiving Day (4th Thursday of
  November), Christmas Day;
- observance as the Federal Reserve Banks close: a holiday that falls on a SUNDAY is observed the Monday after; a holiday
  that falls on a SATURDAY is not moved (the Reserve Banks are open the Friday before; the federal-employee rule of
  5 U.S.C. 6103(b), which moves it to the Friday, is NOT used here).

Inauguration Day (a federal-employee holiday in the Washington area only) is not a bank holiday and is not counted.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

FIRST_YEAR = 1986          # the first year all of today's Monday holidays, MLK Day included, were observed
JUNETEENTH_FROM = 2022     # first year on the Federal Reserve's schedule (the law was signed June 17, 2021)


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


@lru_cache(maxsize=256)
def _bank(year: int) -> tuple:
    out = []
    for d, name in _federal(year):
        if d.weekday() == 6:                     # Sunday: observed the Monday after
            out.append((d + timedelta(days=1), name))
        elif d.weekday() == 5:                   # Saturday: not moved (Reserve Banks open the Friday before)
            continue
        else:
            out.append((d, name))
    return tuple(sorted(out))


def bank_holidays(year: int) -> dict:
    """The weekdays of ``year`` on which the Federal Reserve Banks are closed for a holiday."""
    return dict(_bank(year))


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in bank_holidays(d.year)


def add_business_days(day: date, n: int) -> date:
    """The ``n``-th business day after ``day`` (``day`` itself never counts; 0 is ``day``). Weekends and Federal
    Reserve bank holidays are skipped."""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("n must be a whole number of business days, 0 or more")
    d = day
    while n > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d
