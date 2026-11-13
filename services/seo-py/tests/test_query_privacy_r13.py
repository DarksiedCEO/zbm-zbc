"""Review round 13 (search_performance 2026-11-08.13): year and date queries are kept. Round 12 let ONE spelled
number word count in a run of 9+ tokens, and found that word inside ordinary words ("ph-one", reversed "ho" in
hotel / honda), so "iphone 2023 2024" and "2024-12-25 hotel deals" were dropped. Now one word counts only as a
whole word with 9+ real digits, after the year-list and date exemptions; calendar dates in every common order
(2025/11/08, 11/08/2025, 08.11.2025, 12-25-2024) are never digit runs, cards or filler runs."""

from __future__ import annotations

import itertools

import pytest

from conformance.oracle import priya_list

from agents import search_performance as sp

YEAR_DATE_KEEP = [
    # the reviewer's examples
    "2023 2024 honda civic", "2024 2025 home sales",
    "2024-12-25 hotel deals", "2025-01-01 hours", "2010 2020 ohio state", "2025/11/08 hotel",
    "2025-01-01 holidays",
    # year pairs / ranges + products, model years, seasons
    # round 18: "1999 2000 y2k" moved to test_query_privacy_r18.LIST_MANDATED_DROP (its digits and the "y" gap spell
    # a valid SSN stream: closed list item 1)
    "2019 2020 2021 toyota camry", "2022 vs 2023 honda accord", "2024 to 2025 tax brackets",
    "2020-2021 nba season", "2023/2024 premier league", "2024-25 school year calendar", "2018 2019 ford f150 parts",
    "2021 2022 2023 subaru outback", "1990 2000 movies",
    "2024 2025 world cup qualifiers", "nfl 2024 2025 schedule", "2023 2024 ohio state football",
    "2025 2026 holiday hours", "best laptops 2024 2025", "honda civic 2016 2017 2018", "2015-2020 jeep wrangler",
    # dates in every common order + common words
    "11/08/2025 hotel deals", "08.11.2025 hours", "12-25-2024 holiday hours", "2025-01-01 2025-12-31 calendar",
    "2024/12/25 2025/01/01 hotel", "hotel 2025-07-04", "1/1/2025 new year", "12/31/2024 party",
    "31.12.2024 silvester", "2025-03-15 tax deadline", "flights 2025-06-01 2025-06-15", "holidays 25/12/2025",
    "someone like you 2011 2012", "money heist 2017 2021", "oh my god 2023 2024",
]


@pytest.mark.parametrize("q", YEAR_DATE_KEEP)
def test_year_and_date_queries_are_kept(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


# priya-spec/1 (D14, G-7): moved here from YEAR_DATE_KEEP - the independent oracle of the conformance suite labels
# each list-mandated (one number word, whole or inside a word, among years spells a valid SSN across gaps of at most
# three characters; THE RULE has no year exemption)
LIST_MANDATED_R19 = ["iphone 2023 2024", "best phone 2025 2026", "2024-2025 one piece", "2024 2025 phone deals",
                     "2025 2026 one punch man", "the one 2001 2002"]


@pytest.mark.parametrize("q", LIST_MANDATED_R19)
def test_list_mandated_year_queries_drop(q):
    assert priya_list.mandated(q), q
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


def _probe():
    words = ["iphone", "best phone", "honda civic", "home sales", "hotel deals", "hours", "ohio state", "one piece",
             "hotel", "holidays", "nba season", "toyota camry", "tax brackets", "calendar", "phone deals",
             "honda accord", "world cup", "school year", "movies", "holiday hours", "ohio football",
             "one punch man", "money heist", "honey prices", "someone like you", "the one", "phone plans",
             "hot sauce", "photo contest", "oh my god"]
    years = ["2023 2024", "2024 2025", "2025 2026", "2010 2020", "2019 2020 2021", "2024-2025", "2023/2024",
             "1999 2000", "2024 to 2025", "2020-2021 2022"]
    dates = ["2024-12-25", "2025-01-01", "2025/11/08", "11/08/2025", "08.11.2025", "2025-01-01 2025-12-31",
             "12-25-2024", "2024/12/25 2025/01/01"]
    for y, w in itertools.chain(itertools.product(years, words), itertools.product(dates, words)):
        yield f"{y} {w}"
        yield f"{w} {y}"


def test_year_date_probe_has_no_over_drop():
    qs = list(_probe())
    dropped = [q for q in qs if sp.classify_query(q, ["acme"]) == "pii_suspect"]
    # priya-spec/1 (G-7): no drop but those the independent oracle labels list-mandated (a number word among years)
    mandated = [q for q in qs if priya_list.mandated(q)]
    assert len(qs) == 1080 and dropped == mandated, [q for q in dropped if q not in mandated][:10]


@pytest.mark.parametrize("q", ["4111 1111 1111 1111", "4000 2019 2020 2021", "123-45-6789", "call 555 010 0199",
                               "four l l l l l l l l l l l l l l l", "(5) (5) (5) 0 (1) zero 0 (1) (9) (9)",
                               "4111/1111/1111/1111", "2025/11/08 4111 1111 1111 1111"])
def test_cards_ssns_and_phones_still_drop_next_to_dates(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


def test_one_spelled_word_inside_another_word_never_counts():
    assert sp.spelled_digits("iphone 2023 2024") == "" and sp.spelled_digits("2024 2025 hotel") == ""
    assert sp.spelled_digits("phone 1 2 3 4 5 6 7 8") == ""          # "one" only inside "phone"

