"""Review round 14 (search_performance 2026-11-08.14): a calendar date is exempt from the digit rules ONLY when it
stands alone, so a date can never hide a card, phone or SSN ("22230 03/12/2003 222" holds the documented Mastercard
2-series test number 2223003122003222); and the round's over-drops are fixed or disclosed. Every value is a public
test number or a 555 / documented example, built from parts."""

from __future__ import annotations

import re

import pytest

from agents import search_performance as sp

J = "".join
MC2 = J(("2223", "0031", "2200", "3222"))

DATE_HIDES_VALUE_DROP = [
    "22230 03/12/2003 222", "22230.03.12.2003.222", "22230-03-12-2003-222", "22230/03/12/2003/222",
    "22230 03.12.2003 222", "22230 03-12-2003 222", "2025/5/5 0199", "2025.5.5 0100", "2 1909/9/9 99",
    "2025-01-01 0199 555", "0199 2025/5/5", "555 2025-01-01 0199",
]
ROUND14_KEEP = [
    "2025-11-08/2025-11-09 hotel", "25.12.2024-01.01.2025 urlaub", "2001 2002 2003 2004 2005 honda",
    "1960s 1970s music", "2025 01 01 holiday", "20251108 log", "firmware 2.3.11.4 router",
    "version 10.0.19045.3803", "rfc 9110 2022", "1980s 1990s 2000s hits", "windows 10.0.22631 update",
    "ios 17.1.2 battery", "2024/12/25 2025/01/01 hotel", "2025-12-24/2025-12-26 christmas", "iphone 2023 2024",
]
# disclosed over-drop (privacy first): a ZIP-sized number next to one year has the digits of an SSN regrouped 5 + 4
ZIP_YEAR_OVERDROP = ["90210 2024 restaurants", "10001 2025 apartments"]


@pytest.mark.parametrize("q", DATE_HIDES_VALUE_DROP)
def test_a_date_never_hides_a_card_phone_or_ssn(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


@pytest.mark.parametrize("q", ROUND14_KEEP)
def test_round14_dates_years_versions_and_decades_are_kept(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


@pytest.mark.parametrize("q", ZIP_YEAR_OVERDROP)
def test_zip_plus_year_stays_dropped_because_it_could_be_a_regrouped_ssn(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q
    assert sp.classify_query(J(("12345", " ", "2019")), ["acme"]) == "pii_suspect"


def _date_forms(d: str):
    for ml in (1, 2):
        for dl in (1, 2):
            if len(d) != 4 + ml + dl:
                continue
            for order in ("ymd", "mdy", "dmy"):
                lens = {"y": 4, "m": ml, "d": dl}
                parts, i = {}, 0
                for k in order:
                    parts[k] = d[i:i + lens[k]]
                    i += lens[k]
                if not re.fullmatch(r"(19|20)\d\d", parts["y"]) or not 1 <= int(parts["m"]) <= 12 \
                        or not 1 <= int(parts["d"]) <= 31:
                    continue
                for sep in "/.- ":
                    yield sep.join(parts[k] for k in order)


def _date_hidden_cases():
    values = [MC2, J(("4111", "1111", "1111", "1111")), J(("4012", "8888", "8888", "1881")),
              J(("3782", "822463", "10005")), J(("555", "010", "0199")), J(("202", "555", "0100")),
              J(("123", "45", "6789"))]
    for v in values:
        for i in range(len(v)):
            for n in (6, 7, 8):
                sub = v[i:i + n]
                if len(sub) < n:
                    continue
                for form in _date_forms(sub):
                    for glue in (" ", ".", "-", "/", " x "):
                        yield glue.join(x for x in (v[:i], form, v[i + n:]) if x)


def test_every_date_shaped_window_of_a_value_still_drops():
    cases = list(_date_hidden_cases())
    kept = [c for c in cases if sp.classify_query(c, ["acme"]) != "pii_suspect"]
    assert len(cases) >= 150 and kept == [], kept[:5]


def test_only_dates_that_stand_alone_are_exempt():
    assert sp.isolated_dates_removed("2025-01-01 hotel").startswith("#")
    assert "03" in sp.isolated_dates_removed("22230 03/12/2003 222")        # next to other digits: kept in
