"""core-spec/2 ST-1 / ST-2: the strict lane is advisory and the SP matrix is REPORTED only.

These tests check that the report job runs and that its table is complete and self-consistent. They never assert
a read or a no-read of any cell (ST-2: not a pass/fail row). Failures print ids and counts only."""
import inspect
import json

import pytest

from conformance import core_matrix as M
from conformance import sp_report as SR

pytestmark = pytest.mark.conformance

SAMPLE_SP = {1, 25, 39}           # a whole-word row, an in-text row and the own-separator row (small sample)


@pytest.fixture(scope="module")
def refs():
    return SR.ref_guards(SR.REFS)


@pytest.fixture(scope="module")
def sample():
    return list(M.ci_cells(per_triple=1, only_sp=SAMPLE_SP))


@pytest.fixture(scope="module")
def res(sample, refs):
    return SR.table(sample, SR.build_guard(), refs)


def test_core_ST_1_strict_api_unchanged_for_callers():
    """ST-1: the strict-lane API stays callable as before (names and parameters)."""
    from agents import secret_guard as sg
    for name in ("check", "_inspect", "is_clearly_safe", "redact_text", "guard_record", "label_only"):
        assert callable(getattr(sg, name)), name
    assert list(inspect.signature(sg.check).parameters)[:1] == ["text"]
    assert sg.check("plain prose about a garden") != "a_label"


def test_core_ST_2_report_rows_cover_every_sampled_triple_and_lane(sample, res):
    keys = {f"{c.eid}|SP{c.sp:02d}|SEP-{c.sep_class}|{lane}" for c in sample for lane in SR.LANES}
    print(f"ST-2 sample: cells={len(sample)} rows={len(res['rows'])}")
    assert sample and set(res["rows"]) == keys


def test_core_ST_2_report_counts_are_self_consistent(sample, res, refs):
    """cells = R + N + X + O; reads <= cells; per reference: read@build - read@ref = NEW - LOST; totals = sum."""
    for k, r in res["rows"].items():
        assert r["cells"] == r["R"] + r["N"] + r["X"] + r["O"], k
        assert 0 <= r["read"]["build"] <= r["cells"], k
        for sha, sg in refs.items():
            if sg is None or r["read"].get(sha) == "absent":
                assert sha not in r["lost"] and sha not in r["new"], k
                continue
            assert r["read"]["build"] - r["read"][sha] == r["new"][sha] - r["lost"][sha], (k, sha)
    for lane in SR.LANES:
        rows = [r for k, r in res["rows"].items() if k.endswith("|" + lane)]
        assert res["totals"][lane]["cells"] == sum(r["cells"] for r in rows) == len(sample)
        assert res["totals"][lane]["read"]["build"] == sum(r["read"]["build"] for r in rows)
    present = [s for s, g in refs.items() if g is not None]
    print(f"ST-2 refs present={present} totals=" + json.dumps(
        {ln: {x: res["totals"][ln][x] for x in ("read", "lost", "new")} for ln in SR.LANES}, sort_keys=True))


def test_core_ST_2_job_writes_the_table_and_never_a_cell_text(tmp_path, sample):
    """The CLI writes sp_report.json + sp_report.md (ids and counts only)."""
    assert SR.main(["--out", str(tmp_path), "--per-triple", "1", "--only-sp", "39", "--refs", ""]) == 0
    md = (tmp_path / "sp_report.md").read_text(encoding="ascii")
    js = json.loads((tmp_path / "sp_report.json").read_text(encoding="ascii"))
    assert md.startswith("# SP matrix report") and "TOTAL strict" in md and "TOTAL cv" in md
    assert js["meta"]["spec"] == "core-spec/2 ST-2" and js["rows"]
    for c in sample:
        if c.sp == 39:
            assert c.text not in md and c.text not in json.dumps(js, ensure_ascii=False), c.key()


def test_core_ST_2_the_matrix_is_not_a_pass_fail_row():
    """ST-2: the report job holds no assertion and no exit status from a read."""
    src = inspect.getsource(SR)
    assert "assert " not in src and "sys.exit(1)" not in src
