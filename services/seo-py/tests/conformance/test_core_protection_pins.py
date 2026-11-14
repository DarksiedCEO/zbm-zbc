"""core-spec/1 P-5 (the five MUST classes of review rv-sink8: SP-24, SP-25, SP-29, SP-34, SP-35) and P-6a..f (the six
pinned rules, each a minimal positive and negative case that kills the named mutant). Ids and counts only."""
import random

import pytest

from conformance import core_forms as F
from conformance import core_matrix as M
from conformance import core_ref as R

pytestmark = pytest.mark.conformance

S = {eid: e for eid, e, *_ in R.entries()}
GAP_VALUE = "Kx7#mQ2zB"


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def flagged(sg, t):
    return sg.check(t) == "a_label"


def masked(sg, t):
    return "label_value" in sg.client_view_findings(t)


def _class_cells(sp, cap=None, seed=0):
    """Every form of ``sp`` for its entries x SEP-1 / SEP-2 / SEP-3a (one rotating variant each), Unicode form and
    placement rotating; ``cap``: a seeded subset of the forms (CI lane), the heavy matrix holds them all."""
    out = []
    for eid, e, kind, _sc, _g in R.entries():
        if not M.applies(eid, kind, sp, e):
            continue
        fs = M.forms(eid, sp)
        idx = list(range(len(fs)))
        if cap is not None and len(idx) > cap:
            idx = sorted(random.Random(f"{seed}:{eid}:{sp}").sample(idx, cap))
        for k, f in enumerate(idx):
            for sc in ("1", "2", "3a"):
                seps = M.SEPS[sc]
                out.append(M._cell(eid, sp, sc, fs, f, (f + k) % len(seps), (f + k) % 3, (f + k) % 2))
    return out


def _judge(sg, cells):
    miss: dict = {}
    for c in cells:
        for lane, v, got in (("strict", c.strict, flagged(sg, c.text)), ("cv", c.cv, masked(sg, c.text))):
            if v == "R" and not got:
                k = f"{c.eid}|SP{c.sp:02d}|SEP-{c.sep_class}|{lane}"
                miss[k] = miss.get(k, 0) + 1
    return miss


@pytest.mark.parametrize("item,sp,cap", [("P-5.SP-24", 24, None), ("P-5.SP-34", 34, None), ("P-5.SP-35", 35, None),
                                         ("P-5.SP-25", 25, 60), ("P-5.SP-29", 29, 60)])
def test_core_P_5_must_classes_closed(sg, item, sp, cap):
    """P-5: the R cells of the five classes rv-sink8 found unread at 186471eb are read (CI: every form of SP-24 /
    34 / 35; a seeded 60 forms per entry of SP-25 and SP-29; the heavy matrix: all)."""
    cells = _class_cells(sp, cap)
    miss = _judge(sg, cells)
    r = sum(1 for c in cells for v in (c.strict, c.cv) if v == "R")
    print(f"{item}: cells={len(cells)} R={r} misses={sum(miss.values())}")
    assert not miss, dict(sorted(miss.items())[:40])


def _spelled(e, sep):
    return sep.join(F.units(e))


def test_core_P_6a_digits_between_spelled_units(sg):
    """P-6a (mutant ``strict_gap_no_digit``): a label spelled one unit at a time with a DIGIT between units is
    STRICT R; with a LETTER between units it is no label."""
    for eid in ("S00", "S12", "S21", "S22"):
        assert flagged(sg, _spelled(S[eid], "1") + ": " + GAP_VALUE), eid
        assert flagged(sg, _spelled(S[eid], "7") + " = " + GAP_VALUE), eid
    assert not flagged(sg, _spelled(S["S00"], "\u0445") + ": " + GAP_VALUE)        # a Cyrillic letter between


def test_core_P_6b_arabic_proclitic_with_an_f2_join(sg):
    """P-6b (mutant ``strict_no_proclitic``): S16 / S17 with a declared proclitic AND its words joined by an SP-23
    join is STRICT R; the same words joined by four non-letters are not a label join (P-6c)."""
    letters, n = R.AR_PROCLITICS
    for eid in ("S16", "S17"):
        a, b = S[eid].split(" ")
        for p in (letters[0], letters[2], letters[0] + letters[3]):
            for j in (".", "/", "1", ":"):
                assert flagged(sg, f"{p}{a}{j}{b}: {GAP_VALUE}"), (eid, ord(p[0]), j)
        assert not flagged(sg, f"{letters[0]}{a}..-.{b}: {GAP_VALUE}"), eid


def test_core_P_6c_the_join_limit(sg):
    """P-6c (mutant ``strict_join_1to4``): label words joined by 1-3 non-letters are STRICT R; by 4 or more they
    are not a label join."""
    for eid in ("S01", "S16", "S17", "S18", "S22"):
        a, b = S[eid].split(" ")
        for j in (".", "./", ".:.", "-1-", "__", "+"):
            assert flagged(sg, f"{a}{j}{b}: {GAP_VALUE}"), (eid, j)
        for j in ("....", "./.:", "-..-.", "__.._"):
            assert not flagged(sg, f"{a}{j}{b}: {GAP_VALUE}"), (eid, j)


def test_core_P_6d_the_value_minimum_after_a_gap(sg):
    """P-6d (mutant ``inword_gap_val_1``): an in-word label + a SEP-4 gap + a value-looking token of 4+ characters
    is STRICT R; of 1-3 characters it is not."""
    noun = {"S12": F.nouns("S12")[0], "S00": F.nouns("S00")[0], "S21": F.nouns("S21")[0]}
    for eid, n in noun.items():
        lab = n + S[eid]
        for gap in (" ", "\t", ", "):
            assert flagged(sg, f"{lab}{gap}Kx7#"), (eid, repr(gap))
            assert flagged(sg, f"{lab}{gap}Kx7#mQ2"), (eid, repr(gap))
            assert not flagged(sg, f"{lab}{gap}K7#"), (eid, repr(gap))
            assert not flagged(sg, f"{lab}{gap}7#"), (eid, repr(gap))


# (Hangul: the Han / kana two-character entries are read as substrings of the letters-only view by SP-31 at every
# release, so a letter before or after them cannot be observed by this guard: NOTES Q-P6E)
TWO_UNIT = ("S13", "S14", "S15")


def test_core_P_6e_two_unit_label_start_guard(sg):
    """P-6e (mutant ``strict_whole_lt2``): a two-unit label spelled with a separator is read as a whole word of its
    own; a letter joined BEFORE it makes it no label."""
    for eid in TWO_UNIT:
        e = S[eid]
        # (a letter of its script before any of the four; a letter of another script before "/" or "+": with "-" or
        # "." the word of its script is the label split once, SP-25)
        for before, seps in ((F.nouns(eid)[0][-1], ("-", ".", "/", "+")), ("x", ("/", "+"))):
            for s in seps:
                assert flagged(sg, f"{_spelled(e, s)}: {GAP_VALUE}"), (eid, s)
                assert not flagged(sg, f"{before}{_spelled(e, s)}: {GAP_VALUE}"), (eid, s)


def test_core_P_6f_two_unit_label_end_guard(sg):
    """P-6f (mutant ``strict_whole_no_end``): a letter joined AFTER a two-unit spelled label makes it no label."""
    for eid in TWO_UNIT:
        e = S[eid]
        for after, seps in ((F.nouns(eid)[1][0], ("-", ".", "/", "+")), ("x", ("/", "+"))):
            for s in seps:
                assert flagged(sg, f"{_spelled(e, s)} {GAP_VALUE}"), (eid, s)
                assert not flagged(sg, f"{_spelled(e, s)}{after}: {GAP_VALUE}"), (eid, s)
