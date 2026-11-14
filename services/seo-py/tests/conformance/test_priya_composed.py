"""priya-spec/1 section 3 (CP-1..CP-4): composed pairs, built structurally (units and gaps; no gap longer than three
characters; none inside a word; a spelled transform's joiner replaced by a gap so a number word may touch gap
letters; the e-mail pair is b(a(value))). Every ordered pair a != b outside one family, each equally often, a quarter
in context. Bar: kept <= 1.0% overall and <= 5% for every ordered pair (owner Q-5 default). CI 4,000 cases (every pair
at least seven times); heavy 42,000."""

from __future__ import annotations

import collections

import priya_gen as g
import pytest
from priya_common import dropped

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
    worst = max(((kept[p] / total[p], p) for p in total), default=(0, None))
    assert sum(total.values()) == n
    assert k <= 0.01 * n, (k, n)
    assert worst[0] <= 0.05, worst
    return k, worst


@pytest.mark.conformance
def test_CP4_residual_ci_4000():
    k, _w = _cp(4000)
    assert k <= CP_KEPT_CI, k


@pytest.mark.conformance_heavy
def test_CP4_residual_heavy_42000():
    _cp(42000)


CP_KEPT_CI = 0          # measured at round 19 on the final code (ADR 0047)
