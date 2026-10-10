"""
War room fixes (ADR 0018; devtools/warroom/findings.md): WR-F002, a money word in leetspeak AND spelled out letter by
letter got past the display-name filter (CN-26). The inputs are the war room's replay cases, copied.
"""

from __future__ import annotations

import pytest

from textguard import display_name_problem, fold_for_matching, money_or_earnings


@pytest.mark.parametrize("name", [
    "g u 4 r 4 n 7 3 e d",          # replay clipper-network-py/R0001
    "g ú a r a n t 3 e d",     # R0002
    "c 4 5 h",                      # R0003
    "C 4 S H",
    "e 4 r n",
    "c-4-5-h",
    "g.u.4.r.4.n.7.e.e.d",
    "p 4 1 d",
    "ｃ ４ ５ ｈ",  # full-width letters and digits, spaced
    "ᴄ ᴀ ѕ н",  # small capitals / Cyrillic, spaced
])
def test_wr_f002_spaced_leet_money_words_are_refused(name):
    assert money_or_earnings(name)
    assert display_name_problem(name) == "no money or earnings words (CN-26)"


@pytest.mark.parametrize("name", ["Agent 4 7", "Room 2 0 2 4", "J 4 5", "DJ K 4 T", "A J Smith", "Studio 5 4 3"])
def test_wr_f002_ordinary_names_with_spaced_digits_pass(name):
    assert money_or_earnings(name) == []
    assert display_name_problem(name) is None


def test_wr_f002_numbers_alone_are_not_read_as_letters():
    assert money_or_earnings("5 4 5 7") == []           # no letter in the run: digits stay digits


def test_shared_fold_removes_every_default_ignorable_and_reads_the_final_sigma():
    assert fold_for_matching("g᠋u️a឴rantee") == "guarantee"
    assert fold_for_matching("сαςh") == "cach"
