"""core-spec/1 PF-1..PF-4. CI: a linearity smoke of every PF-3 shape at 12k / 24k / 48k characters, both lanes (12k ->
48k <= 9x, <= 2 s at 48k). Heavy (the gate): ``tools/pf.py`` pf1 (in-process A/B vs ffd28ca8, two runs in
opposite orders, a third run decides a cell they disagree on: core-spec/2), pf2 (cold start, 24 interleaved processes, median <= 1.10x), pf3 (2k..64k, doubling <= 2.5x from 8k,
<= 1.0 s and <= 2.5x ffd28ca8 at 64k)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conformance import perf_shapes as P

pytestmark = pytest.mark.conformance
TOOL = Path(__file__).resolve().parent / "tools" / "pf.py"


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def test_core_PF_3_linearity_smoke(sg):
    """Every PF-3 shape at 12k / 24k / 48k characters, both lanes: the two doublings 12k -> 48k at most 9x together
    (3.0x each on average; a quadratic shape gives 16x), judged past timer noise (10 ms), <= 2 s at 48k. A cell over
    the bound is measured again (best of 9) before it counts: a box loaded by other jobs moves single timings by
    2-4x (measured), not a shape's growth over two doublings."""
    sizes = (6144, 12288, 24576, 49152)                       # (the 6k pass warms the tables; not judged)
    res = P.linearity(sg, sizes, k=3)
    bad = {}
    for shape, lanes in res.items():
        for lane, (_w, a, _b, c) in lanes.items():
            if (c > 0.01 and c / max(a, 1e-4) > 9.0) or c > 2.0:
                fn = sg.check if lane == "strict" else sg.client_view_findings
                a, c = P.best(fn, P.SHAPES[shape](sizes[1]), 9), P.best(fn, P.SHAPES[shape](sizes[3]), 9)
                if (c > 0.01 and c / max(a, 1e-4) > 9.0) or c > 2.0:
                    bad[f"{shape}|{lane}"] = (round(a, 4), round(c, 4))
    worst = max(t for lanes in res.values() for ts in lanes.values() for t in ts)
    print(f"PF-3 smoke: shapes={len(res)} worst 48k={worst:.3f}s over the bound={bad}")
    assert len(res) >= 17 and not bad


def test_core_PF_4_the_perf_test_file_is_in_the_suite():
    assert (Path(__file__).resolve().parents[1] / "test_secret_guard_perf.py").exists()


def _tool(*args) -> dict:
    p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=6 * 3600)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout)


@pytest.mark.conformance_heavy
def test_core_PF_1_per_language_cost_vs_ffd28ca8():
    res = _tool("pf1")
    print(f"PF-1: {res}")
    assert res["fail"] == [] and res["fail_both_runs"] == []


def test_core_PF_1_third_run_rule():
    """core-spec/2 PF-1: two runs agree -> their verdict; they disagree -> a third run is required and decides."""
    from conformance.tools import pf
    v = pf.pf1_verdict({"a|cv": {"build_first": 0.2, "ffd_first": 0.3}, "b|cv": {"build_first": 0.01, "ffd_first": 0.0},
                        "c|cv": {"build_first": 0.2, "ffd_first": 0.05}, "d|cv": {"build_first": 0.2, "ffd_first": 0.0,
                        "third": 0.03}, "e|cv": {"build_first": 0.0, "ffd_first": 0.12, "third": 0.11}})
    assert v == {"fails": ["a|cv", "e|cv"], "need_third": ["c|cv"], "decided_by_third": ["d|cv", "e|cv"]}


def test_core_PF_3_has_the_three_core2_cv4_shapes():
    from agents import cv_shape
    for name in ("cv4_one_char_fragments", "cv4_fragment_word_alternating", "cv4_24char_fragments"):
        t = P.SHAPES[name](65536)
        assert len(t) == 65536 and any(c in cv_shape.J for c in t)
    one = P.SHAPES["cv4_one_char_fragments"](4096)
    assert [len(p) for p in one.replace("\t", " ").split()] and all(
        len(x) == 1 for x in "".join(" " if c in cv_shape.J else c for c in one).split())
    assert all(len(x) == 24 for x in P.SHAPES["cv4_24char_fragments"](2400).split("-")[:-1])


@pytest.mark.conformance_heavy
def test_core_PF_2_cold_start():
    res = _tool("pf2")
    print(f"PF-2: {res}")
    assert res["ratio"] <= 1.10


@pytest.mark.conformance_heavy
def test_core_PF_3_linearity_to_64k():
    res = _tool("pf3")
    print(f"PF-3: fails={res['fails']}")
    assert res["fails"] == []
