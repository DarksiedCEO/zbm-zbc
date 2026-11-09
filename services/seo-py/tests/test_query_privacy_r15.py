"""Review round 15 (search_performance 2026-11-08.15): THE RULE — the card (Luhn 13-19 + issuer prefix), phone
(NANP / E.164) and SSN detectors always run on the raw normalised text and its de-separated digit runs, with NO
exemption of any kind; exemptions (dates, times, years, versions, ZIP + year) only suppress the generic rules. A
committed property test checks it: for every fake card / phone / SSN, every exemption shape and every placement
(prefix, suffix, embedded), the query is dropped. All values are public test numbers, 555 numbers or documented
example SSNs, built from parts."""

from __future__ import annotations

import itertools

import pytest

from agents import search_performance as sp

J = "".join
CARDS = [J(("4111", "1111", "1111", "1111")), J(("5555", "5555", "5555", "4444")), J(("2223", "0031", "2200", "3222")),
         J(("3782", "822463", "10005")), J(("6011", "1111", "1111", "1117"))]
PHONES = [J(("202", "555", "0100")), J(("555", "010", "0199")), J(("312", "555", "0142"))]
SSNS = [J(("123", "45", "6789")), J(("078", "05", "1120"))]


def _formats(v: str):
    """The value as people write it: plain, and grouped with each common separator."""
    if len(v) == 9:                                         # SSN
        groups = [v[:3], v[3:5], v[5:]]
    elif len(v) == 10:                                      # phone
        groups = [v[:3], v[3:6], v[6:]]
    else:                                                   # card
        groups = [v[i:i + 4] for i in range(0, len(v), 4)]
    yield v
    for sep in (" ", "-", ".", "/"):
        yield sep.join(groups)


KEYWORDS = ["v", "version", "build", "ios", "fw", "firmware", "model", "sku", "rev", "rfc", "release", "kernel"]
# every exemption shape the generic rules know
EXEMPTION_SHAPES = ["2025-11-08", "11/08/2025", "08.11.2025", "2025 01 01", "20251108", "2025-11-08 10am",
                    "2025-11-08T09:30", "11/10/2025 3:30", "7pm", "2024", "2023 2024", "2024-2025", "1960s",
                    "version 1.2.3", "firmware 2.3.11.4", "rfc 9110", "90210", "90210 2024"]


@pytest.mark.parametrize("kw,value", list(itertools.product(KEYWORDS, CARDS + PHONES + SSNS)))
def test_a_keyword_never_lets_a_card_phone_or_ssn_through(kw, value):
    for f in _formats(value):
        assert sp.classify_query(f"{kw} {f}", ["acme"]) == "pii_suspect", f"{kw} {f}"
        assert sp.classify_query(f"{kw}{f}", ["acme"]) == "pii_suspect", f"{kw}{f}"


def _placements(shape: str, value: str):
    yield f"{shape} {value}"
    yield f"{value} {shape}"
    yield f"{shape} {value} {shape}"
    yield f"hotel {shape} {value} deals"


def test_property_no_exemption_shape_ever_changes_a_drop():
    failures = []
    for value in CARDS + PHONES + SSNS:
        for f in _formats(value):
            assert sp.classify_query(f, ["acme"]) == "pii_suspect", f
            for shape in EXEMPTION_SHAPES:
                for q in _placements(shape, f):
                    if sp.classify_query(q, ["acme"]) != "pii_suspect":
                        failures.append(q)
    assert failures == [], failures[:10]


DATE_SPLIT_DROP = [
    "2025/5/5 0100 2024", "2025.5.5 0100 2024", "2025-5-5 0100 2024", "2025 550100 2024", "2024 2025 550100",
    "2025/5/5 0199", "2 1909/9/9 99", "22230 03/12/2003 222", "2025-11-08 202 555 0100", "1 23 45 67 89 2024",
]
DATE_TIME_KEEP = [
    "11/8/2025 7pm", "2025-11-08 10am", "exam 12/15/2025 9am", "dentist 11/10/2025 3:30",
    "2025-11-08T09:30 meeting", "2025-11-08 09:30 utc", "10/31/2025 halloween 7pm", "flight 2025-12-24 6:45am",
    "meeting 2025-11-08 14:00", "2025-11-08 10:30:00z log", "1999 2000 y2k", "90210 2024 restaurants",
]


@pytest.mark.parametrize("q", DATE_SPLIT_DROP)
def test_date_split_and_year_word_cases_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


@pytest.mark.parametrize("q", DATE_TIME_KEEP)
def test_date_and_time_queries_are_kept(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


def test_detectors_are_called_on_the_raw_text_before_any_exemption():
    raw = "version " + J(("4111", "1111", "1111", "1111"))
    assert sp.pii_detectors(raw)
    assert not sp.pii_detectors("2025-11-08 10am") and not sp.pii_detectors("2001 2002 2003 2004")
    assert sp.card_iin(J(("2223", "0031", "2200", "3222"))) and not sp.card_iin("2001200220032004")
