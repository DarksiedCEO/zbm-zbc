"""priya-spec/1 section 3 (CP-1..CP-3, kept by priya-spec/2 0.2; CP-4 now Z-PROMOTED): composed pairs, built structurally (units and gaps; no gap longer than three
characters; none inside a word; a spelled transform's joiner replaced by a gap so a number word may touch gap
letters; the e-mail pair is b(a(value))). Every ordered pair a != b outside one family, each equally often, a quarter
in context. Bar (priya-spec/2 section 4 Z-PROMOTED, 0.2 CP-4 REPLACED): 0 READABLE overall and in EVERY ordered pair;
the v1 caps (1.0% overall, 5% per ordered pair, v1.1 C-3) are removed. CI 4,000 cases (every pair at least seven
times); heavy 42,000."""

from __future__ import annotations

import collections

import priya_gen as g
import pytest
from priya_common import dropped, readable_report

DIGIT_FAMILIES_EXCLUDED = {("gap", "gap"), ("spelled", "spelled"), ("glyph", "glyph")}


@pytest.mark.conformance
def test_CP1_transforms_per_class():
    assert len(g.DTX2) == 12 and set(g.DTX2) == {"gap_ascii", "gap_nfkc", "gap_other", "gap_sep", "script_digits",
                                                 "fullwidth", "zw_bidi", "spelled_en", "spelled_es", "spelled_run",
                                                 "spelled_hyphen", "spelled_mixed"}
    classes = {c for c, _a, _b in g.cp_pairs()}
    assert classes == {"C1", "C2", "C3", "C4", "C5", "C6"}


@pytest.mark.conformance
def test_CP2_every_ordered_pair_outside_one_family():
    pairs = g.cp_pairs()
    assert len(pairs) == len(set(pairs))
    for c in ("C1", "C2", "C3", "C5"):
        mine = {(a, b) for cc, a, b in pairs if cc == c}
        want = {(a, b) for a in g.DTX2 for b in g.DTX2
                if a != b and (g.FAMILY[a], g.FAMILY[b]) not in DIGIT_FAMILIES_EXCLUDED}
        assert mine == want, c


@pytest.mark.conformance
def test_CP3_structural_no_gap_over_three_and_none_inside_a_word():
    import random
    for c in ("C1", "C2", "C3", "C5"):
        for i in range(300):
            rng = random.Random(f"cp3:{c}:{i}")
            lead, units, gaps = g._cp_start(rng, c, g.DIGIT_GEN[c](rng))
            for name in (g.DTX2[i % 12], g.DTX2[(i * 5 + 3) % 12]):
                lead, units, gaps = g._cp_apply(rng, name, lead, units, gaps)
            assert all(len(x.strip("".join(g.rc.INVISIBLE))) <= 3 for x in gaps), (c, i)
            assert len(gaps) == len(units) - 1


def _cp(n: int):
    total, kept = collections.Counter(), collections.Counter()
    for c, name, q in g.composed(n):
        total[(c, name)] += 1
        kept[(c, name)] += not dropped(q)
    k = sum(kept.values())
    readable_report(f"CP composed{n}", n, k)
    assert sum(total.values()) == n
    pairs = {p: c for p, c in kept.items() if c}                    # spec 4: every cell, every ordered pair = 0
    assert k == 0 and not pairs, (k, n, pairs)                      # pair ids and counts only
    return k


@pytest.mark.conformance
def test_CP4_residual_ci_4000():
    assert _cp(4000) == CP_KEPT_CI


@pytest.mark.conformance_heavy
def test_CP4_residual_heavy_42000():
    _cp(42000)


CP_KEPT_CI = 0          # priya-spec/2 section 4: Z-PROMOTED = 0 (no residual on a frozen CP corpus; section 9: P-r11 removed)
