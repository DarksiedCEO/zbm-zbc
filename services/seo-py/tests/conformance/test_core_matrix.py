"""core-spec/1 section 2: the label matrix (SP-01..40, SEP-1..5, M-1..M-6, P-1, P-3), both lanes.

CI lane: the stratified sample (M-4: 10 cells of every (entry, SP, SEP class) triple not X in both lanes), judged once
per module and read by one test per spec item. Heavy lane: every form of every class (``core_matrix.all_cells``).
Failures print ids and counts only."""
import json
import os

import pytest

from conformance import conftest as C
from conformance import core_matrix as M
from conformance import core_ref as R

pytestmark = pytest.mark.conformance


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


@pytest.fixture(scope="module")
def ci(sg):
    """The CI sample, every cell judged in both lanes once: [(cell, strict_read, cv_read, cv_masked)]."""
    out = []
    for c in M.ci_cells():
        cv = sg.client_view_findings(c.text)
        out.append((c, sg.check(c.text) == "a_label", "label_value" in cv, bool(cv)))
    return out


def _misses(rows) -> dict:
    out: dict = {}
    for c, s, cv, masked in rows:
        for lane, v, got in (("strict", c.strict, s), ("cv", c.cv, cv)):
            if (v == "R" and not got) or (v == "N" and got):
                k = f"{c.eid}|SP{c.sp:02d}|SEP-{c.sep_class}|{lane}|{v}"
                out[k] = out.get(k, 0) + 1
    return out


def _report(name, rows) -> dict:
    counts: dict = {}
    for c, s, cv, masked in rows:
        for lane, v, got in (("strict", c.strict, s), ("cv", c.cv, cv)):
            k = f"{lane}:{v.split(':')[0]}:{int(got)}"
            counts[k] = counts.get(k, 0) + 1
    miss = _misses(rows)
    print(f"{name}: cells={len(rows)} counts={dict(sorted(counts.items()))} misses={sum(miss.values())}")
    return miss


SP_IDS = [f"SP-{i:02d}" for i in range(1, 41)]


@pytest.mark.parametrize("item", SP_IDS)
def test_core_SP_class_read_as_required_ci(ci, item):
    """SP-01..SP-40: every R cell of the class read, every N cell not read, in both lanes (M-6, zero tolerance)."""
    sp = int(item[3:])
    rows = [r for r in ci if r[0].sp == sp]
    if all(v.startswith("X") for v in M.SP_VERDICT[sp]):
        # (a row that is a declared residual in both lanes holds no judged cell: sized by test_core_residuals.py)
        assert not rows
        print(f"{item}: residual {M.SP_VERDICT[sp]} in both lanes, not judged")
        return
    assert rows, f"{item}: no cell in the CI sample"
    miss = _report(item, rows)
    assert not miss, f"{item}: {sum(miss.values())} misses: {dict(sorted(miss.items())[:40])}"


SEP_IDS = ["SEP-1", "SEP-2", "SEP-3a", "SEP-3b", "SEP-4", "SEP-4b"]


@pytest.mark.parametrize("item", SEP_IDS)
def test_core_SEP_separator_read_as_required_ci(ci, item):
    """SEP-1..SEP-4b: the cells of the separator class (all SP rows)."""
    sc = item[4:]
    rows = [r for r in ci if r[0].sep_class == sc or (sc == "1" and r[0].sep_class == "own")]
    assert rows
    miss = _report(item, rows)
    assert not miss, f"{item}: {sum(miss.values())} misses: {dict(sorted(miss.items())[:40])}"


def test_core_SEP_5_glued_value_is_open():
    """SEP-5 (R-o3): a value glued to the label with no separator is O in both lanes: no cell of the matrix requires
    it (reported in the heavy lane, never judged)."""
    assert "5" not in M.SEP_CLASSES


def test_core_M_2_bare_label_words(ci):
    """M-2: a bare whole-word label (SP-01..24, 32) is STRICT R and CV N; bare in-word forms are O."""
    rows = [r for r in ci if r[0].sep_class == "bare"]
    assert {r[0].strict for r in rows if r[0].sp in M.BARE_WHOLE} <= {"R", "X:d3", "X:d5", "X:b", "X:d6"}
    assert {r[0].cv for r in rows if r[0].sp in M.BARE_WHOLE} <= {"N", "X:b", "X:d6"}
    miss = _report("M-2", rows)
    assert not miss, miss


def test_core_M_1_every_row_applies_where_meaningful():
    """M-1: the applicability table: every SP row holds at least one entry, every entry of E holds rows, and every
    applicable (entry, SP) has forms (the generator's own table; adding a class or entry is a spec change)."""
    per_sp: dict = {}
    per_e: dict = {}
    for eid, e, kind, _sc, _g in R.entries():
        for sp in range(1, 41):
            if M.applies(eid, kind, sp, e):
                n = len(M.forms(eid, sp))
                per_sp[sp] = per_sp.get(sp, 0) + n
                per_e[eid] = per_e.get(eid, 0) + n
    print(f"M-1: forms per SP {dict(sorted(per_sp.items()))}")
    assert sorted(per_sp) == list(range(1, 41)) and all(per_sp.values())
    assert len(per_e) == R.ENTRY_COUNT and all(per_e.values())


def test_core_M_4_ci_sample_is_stratified(ci):
    """M-4: >= 10 cells per (entry, SP, SEP class) triple that is not X (all triples smaller than 10: every cell)."""
    per: dict = {}
    for c, *_ in ci:
        per[(c.eid, c.sp, c.sep_class)] = per.get((c.eid, c.sp, c.sep_class), 0) + 1
    print(f"M-4: triples={len(per)} cells={len(ci)}")
    assert len(ci) >= 30000 and min(per.values()) >= 1
    assert sum(1 for v in per.values() if v >= 10) / len(per) > 0.95


def test_core_M_5_verdicts_are_stated_by_rule():
    """M-5: the required verdicts come from the spec tables (``core_matrix.SP_VERDICT`` / ``verdict``), never from
    a release: the generator and the verdict rule read no guard module."""
    import inspect

    from conformance import core_forms
    for mod in (M, core_forms, R):
        src = inspect.getsource(mod)
        assert "secret_guard" not in src.replace("``agents.secret_guard``", "").replace("agents.secret_guard", "")
    assert len(M.SP_VERDICT) == 40


def test_core_M_6_P_1_P_3_zero_misses_in_the_whole_sample(ci):
    """M-6 / P-1 / P-3: zero misses over the whole CI sample, every lane (no percentage)."""
    miss = _report("M-6", ci)
    assert not miss, f"{sum(miss.values())} misses"


def test_core_manifest_ci_stream_is_frozen():
    """SUITE_PLAN 0: the CI sample's stream hash equals the committed manifest (corpus drift guard)."""
    from conformance import corpora as C
    man = json.loads((M.DATA / "manifest.json").read_text(encoding="ascii"))
    assert man["spec"] == "core-spec/1"
    assert M.stream_hash(M.ci_cells()) == man["core_matrix_ci"]
    for name, fn in (("neutral", C.neutral), ("fuzz", C.fuzz), ("hosts", C.hosts), ("real_hosts", C.real_hosts),
                     ("urls", C.urls), ("machine", C.machine), ("wsample", C.wsample)):
        assert C.stream_hash(fn()) == man[name], name


@pytest.mark.conformance_heavy
def test_core_matrix_heavy_every_form(sg):
    """The heavy lane: every form of every class x every SEP class (sharded by CONFORMANCE_SHARD)."""
    i, n = C.shard()
    res = M.judge(sg, M.all_cells(shard=(i, n)))
    print(f"heavy matrix shard {i}/{n}: {res['counts']} misses={sum(res['misses'].values())}")
    out = os.environ.get("CONFORMANCE_OUT")
    if out:
        with open(out, "w", encoding="ascii") as f:
            json.dump(res, f, sort_keys=True)
    assert not res["misses"], dict(sorted(res["misses"].items())[:60])
