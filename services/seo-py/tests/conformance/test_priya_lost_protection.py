"""priya-spec/1 section 4 (LP-1..LP-3): no query dropped by 644b4901 OR by 65f8b92a on the personal-data corpora is
kept now, case by case. LP-1 BASE is FROZEN: ``data/baseline/<corpus>.<release>.bits.gz`` (two bits per case: raw
text dropped, row dropped - raw or stored key), written once by ``devtools/conformance/calibrate.py`` from ``git archive`` exports of
the two releases (SUITE_PLAN C-1), so these tests need no git history. The corpora are ``corpora.PERSONAL``: the
builder's matrix (CI and heavy draws), boundary set, composed pairs and the round-10 attack corpus, and the
reviewers' corpora (priya-spec/1.1 C-5). CI: every CI corpus whole and the CI stride of the heavy ones; heavy:
everything. The heavy lane also re-derives the bits of the CI corpora from git history when it is present."""

from __future__ import annotations

import gzip
import json
import subprocess
import types
from pathlib import Path

import corpora
import pytest
from priya_common import dropped

DATA = Path(__file__).resolve().parent / "data"
REPO = Path(__file__).resolve().parents[4]


def _bits(name: str, rev: str) -> bytes:
    path = DATA / "baseline" / f"{name}.{rev}.bits.gz"
    return gzip.decompress(path.read_bytes())


def _dropped_at(raw: bytes, i: int) -> bool:
    return bool((raw[(2 * i) // 8] >> ((2 * i) % 8)) & 1 or (raw[(2 * i + 1) // 8] >> ((2 * i + 1) % 8)) & 1)


def _lost(name: str, heavy: bool) -> dict:
    m = json.loads((DATA / "manifest.json").read_text())
    n = m["corpora"][name]["n"]
    bases = [_bits(name, r) for r in corpora.REVISIONS]
    assert all(len(b) == (2 * n + 7) // 8 for b in bases), name
    rows = enumerate(corpora.rows(name)) if heavy else ((i, (k, q)) for i, k, q in corpora.ci_rows(name))
    lost, seen = {}, 0
    for i, (key, q) in rows:
        seen += 1
        if any(_dropped_at(b, i) for b in bases):
            if not dropped(q):
                lost[key] = lost.get(key, 0) + 1
    assert seen and (seen == n or not heavy), (name, seen, n)
    return lost


def _none_lost(names, heavy: bool) -> None:
    lost = {n: _lost(n, heavy) for n in names}
    assert all(v == {} for v in lost.values()), {n: v for n, v in lost.items() if v}      # case ids and counts only


REVIEWER = ("rv18_p1", "rv18_bnd", "rv18_p2v2", "rv19_m1", "rv19_b19", "rv19_cp19", "rv19_cpx19")


@pytest.mark.conformance
def test_LP1_LP2_matrix_ci_draw():
    _none_lost(["matrix5"], False)


@pytest.mark.conformance
def test_LP1_LP2_boundary_set():
    _none_lost(["boundary"], False)


@pytest.mark.conformance
def test_LP1_LP2_composed_ci_and_attack_corpus():
    _none_lost(["composed4000", "attack676"], False)


@pytest.mark.conformance
def test_LP1_LP2_reviewer_corpora_ci():
    """priya-spec/1.1 C-5: the reviewers' corpora are personal-data corpora of LP-1 too (their CI stride)."""
    _none_lost(REVIEWER, False)


@pytest.mark.conformance_heavy
def test_LP1_LP2_heavy_draws():
    _none_lost(["matrix200", "composed42000", "attack40672"], True)


@pytest.mark.conformance_heavy
def test_LP1_LP2_reviewer_corpora_heavy():
    _none_lost(REVIEWER, True)


@pytest.mark.conformance
def test_LP1_every_personal_corpus_has_frozen_bits():
    assert set(REVIEWER) | {"matrix5", "boundary", "composed4000", "attack676", "matrix200", "composed42000",
                            "attack40672"} == set(corpora.PERSONAL)
    for n in corpora.PERSONAL:
        for r in corpora.REVISIONS:
            assert (DATA / "baseline" / f"{n}.{r}.bits.gz").is_file(), (n, r)


def _git_base(rev: str):
    try:
        src = subprocess.run(["git", "show", f"{rev}:services/seo-py/src/agents/search_performance.py"], cwd=REPO,
                             capture_output=True, check=True, timeout=60).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        pytest.skip(f"git history of {rev[:8]} not available (the frozen bits stand)")
    mod = types.ModuleType(f"search_performance_{rev[:8]}")
    exec(compile(src, f"<{rev[:8]}>", "exec"), mod.__dict__)
    return mod


BASES = tuple(corpora.REVISIONS.values())
_CACHE: dict = {}


def _base(rev: str):
    """The release's Priya decision read from git history (heavy lane only: the A/B of PF-1 and the re-derivation
    below): raw text, then the key as round 10's ``clean_text`` makes it."""
    if rev not in _CACHE:
        mod = _git_base(rev)

        def drops(q: str) -> bool:
            key = " ".join(mod.clean_text(q or "", keep_script_marks=True).casefold().split())
            return mod.classify_query(q, ["acme"]) == "pii_suspect" or mod.classify_query(key, ["acme"]) == \
                "pii_suspect"
        _CACHE[rev] = drops
    return _CACHE[rev]


@pytest.mark.conformance_heavy
def test_LP1_frozen_bits_rederived_from_git_history_raw_lane():
    """The raw-text lane of the frozen bits equals each release's classifier read from git history (the key lane
    needs the release's priya.normalize and is re-derived by calibrate.py)."""
    for short, full in corpora.REVISIONS.items():
        mod = _git_base(full)
        for name in ("boundary", "attack676", "rv19_b19"):
            raw = _bits(name, short)
            for i, (_k, q) in enumerate(corpora.rows(name)):
                assert bool((raw[(2 * i) // 8] >> ((2 * i) % 8)) & 1) == (
                    mod.classify_query(q, ["acme"]) == "pii_suspect"), (short, name, i)


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
