"""Business days skip US federal bank holidays (founder ask, Oct 31 2026; MSA 1.6 "a business day is a weekday that
is not a United States federal holiday"; refund policy: refunds within 10 business days).

The calendar is computed in code for any year (no seeded list that runs out), on the Federal Reserve's schedule: the
11 holidays of 5 U.S.C. 6103, a holiday on a Sunday is observed the Monday after, and a holiday on a Saturday is NOT
moved (the Reserve Banks are open the Friday before). Expected dates below are the Federal Reserve's published
holiday schedule for 2026-2028."""

from __future__ import annotations

from datetime import date

import pytest

import bizdays as B

FED_2026 = {
    date(2026, 1, 1): "new_years_day", date(2026, 1, 19): "birthday_of_martin_luther_king_jr",
    date(2026, 2, 16): "washingtons_birthday", date(2026, 5, 25): "memorial_day", date(2026, 6, 19): "juneteenth",
    # July 4, 2026 is a Saturday: not observed on Friday July 3 by the Federal Reserve
    date(2026, 9, 7): "labor_day", date(2026, 10, 12): "columbus_day",
    date(2026, 11, 11): "veterans_day", date(2026, 11, 26): "thanksgiving_day", date(2026, 12, 25): "christmas_day",
}
FED_2027 = {
    date(2027, 1, 1): "new_years_day", date(2027, 1, 18): "birthday_of_martin_luther_king_jr",
    date(2027, 2, 15): "washingtons_birthday", date(2027, 5, 31): "memorial_day",
    # June 19, 2027 is a Saturday (not moved); July 4, 2027 is a Sunday: observed Monday July 5
    date(2027, 7, 5): "independence_day", date(2027, 9, 6): "labor_day", date(2027, 10, 11): "columbus_day",
    date(2027, 11, 11): "veterans_day", date(2027, 11, 25): "thanksgiving_day",
    # December 25, 2027 is a Saturday: not moved
}
FED_2028 = {
    # January 1, 2028 is a Saturday: not moved (and not observed on Friday December 31, 2027)
    date(2028, 1, 17): "birthday_of_martin_luther_king_jr", date(2028, 2, 21): "washingtons_birthday",
    date(2028, 5, 29): "memorial_day", date(2028, 6, 19): "juneteenth", date(2028, 7, 4): "independence_day",
    date(2028, 9, 4): "labor_day", date(2028, 10, 9): "columbus_day",
    # November 11, 2028 is a Saturday: not moved
    date(2028, 11, 23): "thanksgiving_day", date(2028, 12, 25): "christmas_day",
}


@pytest.mark.parametrize("year,expected", [(2026, FED_2026), (2027, FED_2027), (2028, FED_2028)])
def test_the_closed_days_match_the_federal_reserve_schedule(year, expected):
    assert B.bank_holidays(year) == expected


@pytest.mark.parametrize("day", [date(2026, 7, 3), date(2027, 6, 18), date(2027, 12, 24), date(2027, 12, 31),
                                 date(2028, 11, 10)])
def test_a_saturday_holiday_leaves_the_friday_before_a_business_day(day):
    assert day.weekday() == 4 and B.is_business_day(day)


@pytest.mark.parametrize("day", [date(2027, 7, 5), date(2033, 12, 26), date(2022, 6, 20), date(2023, 1, 2)])
def test_a_sunday_holiday_closes_the_monday_after(day):
    assert day.weekday() == 0 and not B.is_business_day(day)


def test_weekends_and_holidays_are_not_business_days_and_other_weekdays_are():
    assert not B.is_business_day(date(2026, 10, 31))        # Saturday
    assert not B.is_business_day(date(2026, 11, 1))         # Sunday
    assert not B.is_business_day(date(2026, 11, 26))        # Thanksgiving
    assert B.is_business_day(date(2026, 11, 27))            # the Friday after Thanksgiving is open


def test_ten_business_days_skip_thanksgiving_and_christmas():
    # a refund due Monday Nov 23, 2026: Tue 24, Wed 25, (Thu 26 closed) Fri 27, Mon 30, Tue Dec 1 ... -> Dec 8
    assert B.add_business_days(date(2026, 11, 23), 10) == date(2026, 12, 8)
    # due Friday Dec 18, 2026: Mon 21 .. Thu 24, (Fri 25 closed), Mon 28 .. Thu 31, (Fri Jan 1 closed), Mon 4, Tue 5
    assert B.add_business_days(date(2026, 12, 18), 10) == date(2027, 1, 5)
    # the 2 business days of an acknowledgement around Juneteenth 2026 (a Friday)
    assert B.add_business_days(date(2026, 6, 18), 2) == date(2026, 6, 23)


def test_the_start_day_never_counts_and_zero_is_the_same_day():
    assert B.add_business_days(date(2026, 11, 26), 1) == date(2026, 11, 27)
    assert B.add_business_days(date(2026, 11, 26), 0) == date(2026, 11, 26)
    with pytest.raises(ValueError):
        B.add_business_days(date(2026, 11, 26), -1)


def test_every_year_has_the_same_eleven_holidays_before_observance():
    for year in range(2022, 2101):
        names = set(B.federal_holidays(year).values())
        assert len(names) == 11, year
        for d, name in B.bank_holidays(year).items():
            assert d.weekday() < 5 and d.year == year, (year, d, name)


def test_juneteenth_starts_in_2022_and_years_before_1986_are_refused():
    assert "juneteenth" not in B.federal_holidays(2021).values()
    assert "juneteenth" in B.federal_holidays(2022).values()
    with pytest.raises(ValueError):
        B.federal_holidays(1985)


def test_finance_counts_contract_business_days_with_bank_holidays():
    import svc_credits
    assert svc_credits.add_business_days(date(2026, 11, 23), 10) == date(2026, 12, 8)
    import svc_mail
    assert svc_mail.add_business_days is svc_credits.add_business_days
