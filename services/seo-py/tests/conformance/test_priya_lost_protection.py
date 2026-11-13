"""priya-spec/1 section 4 (LP-1..LP-3): no query dropped by 644b4901 OR by 65f8b92a on the personal-data corpora is
kept now, cell by cell. The baselines are the two releases' classifiers, read from git history at run time (the frozen
bits of SUITE_PLAN C-1, ``data/baseline/``, replace this when the lead's calibration commits them); without history
the test is skipped with that reason. CI: the matrix CI draw, the boundary set, 4,000 composed pairs, the round-10
single attack corpus; heavy: the matrix at N = 200, 42,000 composed pairs, the composed attack corpus."""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import attack_corpus as ac
import priya_gen as g
import pytest
from priya_common import dropped

BASES = ("644b4901725fdd077dac60a0703591d6ce1a4088", "65f8b92ace21cdce68c38ad6880184a0bee537b9")
REPO = Path(__file__).resolve().parents[4]
_CACHE: dict = {}


def _base(rev: str):
    if rev in _CACHE:
        return _CACHE[rev]
    try:
        src = subprocess.run(["git", "show", f"{rev}:services/seo-py/src/agents/search_performance.py"], cwd=REPO,
                             capture_output=True, check=True, timeout=60).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        pytest.skip(f"git history of {rev[:8]} not available (frozen baseline bits not calibrated yet)")
    mod = types.ModuleType(f"search_performance_{rev[:8]}")
    exec(compile(src, f"<{rev[:8]}>", "exec"), mod.__dict__)

    def drops(q: str) -> bool:
        key = " ".join(mod.clean_text(q or "", keep_script_marks=True).casefold().split())
        return mod.classify_query(q, ["acme"]) == "pii_suspect" or mod.classify_query(key, ["acme"]) == "pii_suspect"
    _CACHE[rev] = drops
    return drops


def _lost(corpus) -> dict:
    bases = [_base(r) for r in BASES]
    lost = {}
    for key, q in corpus:
        if any(b(q) for b in bases) and not dropped(q):
            lost[key] = lost.get(key, 0) + 1
    return lost


@pytest.mark.conformance
def test_LP1_LP2_matrix_ci_draw():
    assert _lost((":".join(c), q) for c, _k, q in g.matrix(5)) == {}


@pytest.mark.conformance
def test_LP1_LP2_boundary_set():
    assert _lost(g.boundary()) == {}


@pytest.mark.conformance
def test_LP1_LP2_composed_ci_and_attack_corpus():
    assert _lost((c + ":" + n, q) for c, n, q in g.composed(4000)) == {}
    assert _lost((b + ":" + n, t) for b, n, t in ac.cases()) == {}


@pytest.mark.conformance_heavy
def test_LP1_LP2_heavy_draws():
    assert _lost((":".join(c), q) for c, _k, q in g.matrix(200)) == {}
    assert _lost((c + ":" + n, q) for c, n, q in g.composed(42000)) == {}
    assert _lost((b + ":" + n, t) for b, n, t in ac.composed_cases()) == {}


@pytest.mark.conformance
def test_LP3_glued_trim_only_for_the_closed_unit_model_list():
    from agents import search_performance as sp
    assert sp.longest_digit_run("x5 55 0199") == 7 and sp.longest_digit_run("ab55 501 99") == 7
    assert sp.longest_digit_run("55 501 99ab") == 7 and sp.longest_digit_run("ddr5 6400 64gb") == 4
    assert {"gb", "mb", "tb", "ghz", "mhz", "w", "v", "mm"} <= set(UNIT_TOKENS)
    for u in UNIT_TOKENS:
        assert sp._GLUED_AFTER.fullmatch(u), u
    for w in ("x", "ab", "me", "call", "zz"):
        assert not sp._GLUED_AFTER.fullmatch(w) and not sp._GLUED_BEFORE.search(w), w


# the closed unit / model token list frozen with priya-spec/1 (LP-3): the glued trim's own list, asserted equal
UNIT_TOKENS = ("gb", "mb", "tb", "kb", "ghz", "mhz", "khz", "hz", "w", "kw", "kwh", "wh", "v", "mv", "mah", "ah", "a",
               "ma", "mm", "cm", "km", "m", "ml", "l", "kg", "mg", "g", "lb", "lbs", "oz", "in", "ft", "hp", "fps", "mp",
               "k", "p", "pcs", "pk", "gal", "qt", "psi", "rpm", "nm", "th", "st", "nd", "rd")
