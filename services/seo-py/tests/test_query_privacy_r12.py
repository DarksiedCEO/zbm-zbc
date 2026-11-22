"""Review round 12 (search_performance 2026-11-08.12): the composed attack corpus over EVERY ordered pair of
obfuscations (40,672 cases since round 16; part of the default, counted suite — about 10-20 s), and the round's four leak classes:
filler digit runs on the homoglyph view, one spelled word in a 9+ digit run, homoglyph digits with spelled words,
and spelled words with filler or interleaving inside them."""

from __future__ import annotations

import attack_corpus as ac
import pytest

from agents import search_performance as sp

ROUND12_DROP = [
    "lZ34 x S67B x 9", "SSSO x lOOl x 99", "\u2478 \u2478 \u2478 0 \u2474 zero 0 \u2474 \u247c \u247c",
    "four l l l l l l l l l l l l l l l", "cuatro l l l l l l l l l l l l l l l",
    "fxoxuxrx xoxnxex xoxnxex xoxnxex xoxnxex xoxnxex xoxnxex xoxnxex",
    "oxnxex xtxwxox xtxhxrxexex xfxoxuxrx xfxixvxex xsxixxx xsxexvxexnx xexixgxhxtx xnxixnxex",
    "five x  fiv x e fi x ve z x ero  x one  x zero x  zer x o on x e ni x ne",
    "cuat-ab-ro u-ab-no u-ab-no u-ab-no u-ab-no u-ab-no u-ab-no u-ab-no",
    "1 tw-ab-o 3 -ab-four-ab- 5 s-ab-ix 7-ab- eig-ab-ht 9",
]


@pytest.mark.parametrize("q", ROUND12_DROP)
def test_round12_leak_classes_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", repr(q)


def test_all_composed_pairs_drop():
    allc = list(ac.composed_cases())
    kept = [(b, n) for b, n, t in allc if sp.classify_query(t, ["acme"]) != "pii_suspect"]
    assert len(allc) == 40672, len(allc)        # round 16: eleven Unicode separators added
    assert kept == [], kept
