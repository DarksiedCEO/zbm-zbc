"""Fix wave 23 (AEGIS round 22, delivery N22-D-2..8; the founder-approved design change D1-D3, Sep 30 2026).

D1 honest claims: the engine's terminal per-finding state is ``candidate_passed_checks`` (never "fixed"); only an
AEGIS review with an explicit per-finding verdict makes a finding ``accepted``; the report header says in plain words
that passed checks are necessary, not sufficient, and that the diff has not been reviewed.
D2 flag, don't chase: every added/changed SOURCE line that can observe the execution context is a ``review_flags``
entry (file:line, reason), at the top of the report, in the ledger, and a review that accepts a finding must name
each of its flag ids in ``flags_addressed``.
D3 nothing parked: a standalone run that executed and returned a verdict is authoritative (a conftest on the path does
not turn a ``fail`` into "runner dependent"); a pytest import blocked from a SOURCE frame fails the round
(``fix_imports_test_runner``); ``needs_review_runner_dependent`` is a flag and needs a review note to be accepted.
B1 a failing review is refused 409 BEFORE anything is recorded when another run of the service is in flight.
B2 the admission RED check never holds the service lock while a container runs (a pending admission reserves the
service's run slot instead).
B3 ``wait_idle`` waits for the engine thread to settle, not only for a terminal status.
B4 the kill of an engine container that started after a cancel is recorded first (``sandbox_kill_requested``); an
unrecordable kill still happens and marks the run ``unrecorded_failure``.
B5 the references in docstrings name files that exist.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

import pytest

from helpers import (SERVICE_ROOT, Harness, finding, findings_doc, flat, git, replace, review_body, rid, scenario_s1,
                     two_findings, write_test)
from test_round21 import AGENT_RED, FIX_CLAMP, RT_PATH, _n21

from zbm_delivery.engine import states
from zbm_delivery.errors import Conflict

HEADER = "Checks passed are necessary, not sufficient. This diff has not been reviewed."
DONE = ("candidate_passed_checks", "disproved", "needs_review_runner_dependent")
TEST_ADD = "from toy import calc\n\n\ndef test_add_sum():\n    assert calc.add(2, 3) == 5\n"
FIX_ADD = replace("src/toy/calc.py", "    return a - b\n", "    return a + b\n")
TEST_PCT = "from toy import calc\n\n\ndef test_percent_zero():\n    assert calc.percent(1, 0) == 0.0\n"
CONFTEST = {"tests/conftest.py": "# shared test configuration (no fixtures)\n"}
PLAIN_RT = ("from toy import calc\n\n\ndef test_clamp_rejects_inverted_bounds():\n    try:\n        calc.clamp(5, 3, 0)\n"
            "    except ValueError:\n        return\n    raise AssertionError('clamp(5, 3, 0) did not raise')\n")


def _p1(fix) -> list:
    return flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
                 FIX_ADD, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"}, write_test("test_fix_n1_2", TEST_PCT),
                 {"text": "TEST: tests/test_fix_n1_2.py::test_percent_zero"}, fix, {"text": "SWEEP: src/toy/calc.py:11\nFIXED"},
                 {"text": "FIXED"}])


def _whys(h: Harness) -> list:
    return [e["payload"].get("why") for e in h.events("round_failed")]


def _states(h: Harness, run_id: str) -> dict:
    return {f["finding_id"]: f["state"] for f in h.findings(run_id)}


# ====================================================================== D1: honest claims

def test_d1_the_engine_never_claims_fixed_and_the_report_says_so_in_plain_words():
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "awaiting_review", run["reasons"]
        assert _states(h, run_id) == {"N1-1": "candidate_passed_checks", "N1-2": "candidate_passed_checks"}
        text = h.report(run_id)
        head = text.split("\n## ", 1)[0]
        assert HEADER in head, head
        assert "`fixed`" not in text and "NOT fixed" not in text
        moved = [e["payload"]["to"] for e in h.events("finding_state_changed")]
        assert "fixed" not in moved and moved.count("candidate_passed_checks") == 2, moved
    finally:
        h.close()


def test_d1_only_an_aegis_review_with_a_verdict_per_finding_accepts():
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        bare = {"request_id": rid(), "review_ref": "r23-d1", "sha256": "d" * 64, "verdict": "pass"}
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", bare)
        assert r.status_code == 422, r.text                               # no per-finding verdicts: refused
        body = review_body(h, run_id)
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", body, caller="andre_session")
        assert r.status_code == 403, r.text                               # only the aegis caller reviews
        partial = dict(body, request_id=rid(), finding_verdicts=body["finding_verdicts"][:1])
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", partial)
        assert r.status_code == 422 and "N1-2" in r.text, r.text          # every finding needs its verdict
        assert _states(h, run_id) == {"N1-1": "candidate_passed_checks", "N1-2": "candidate_passed_checks"}
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", body)
        assert r.status_code == 200, r.text
        assert _states(h, run_id) == {"N1-1": "accepted", "N1-2": "accepted"}
        ev = h.events("fix_run_reviewed")[-1]["payload"]
        assert sorted(ev["accepted"]) == ["N1-1", "N1-2"] and ev["verdict"] == "pass", ev
    finally:
        h.close()


def test_d1_nothing_but_the_review_reaches_accepted():
    for frm in ("swept", "candidate_passed_checks", "disproved", "needs_review_runner_dependent"):
        assert states.finding_transition_problem({"state": frm}, "accepted") is not None, frm
    assert "accepted" in {m.value for m in states.FindingState}
    assert "fixed" not in {m.value for m in states.FindingState}
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        # the engine-facing transition refuses it whatever the run state (here: not live → Conflict / refused)
        with pytest.raises(Conflict):
            h.svc.finding_transition(run_id, "N1-1", "accepted", {}, [])
        assert _states(h, run_id)["N1-1"] == "candidate_passed_checks"
    finally:
        h.close()


def test_d1_a_legacy_fixed_record_reads_as_candidate_passed_checks():
    assert states.normalize_finding_state("fixed") == "candidate_passed_checks"
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        rec = dict(h.svc.findings[run_id]["N1-1"], state="fixed")
        h.svc._apply("finding", rec)                                     # an old log line replayed at start-up
        assert h.svc.findings_get_one(run_id, "N1-1")["state"] == "candidate_passed_checks"
        assert _states(h, run_id)["N1-1"] == "candidate_passed_checks"
    finally:
        h.close()


def test_d1_a_failing_review_accepts_what_it_accepts_and_reopens_the_rest():
    h = Harness(scenario=scenario_s1() + flat([write_test("test_fix_n1_2b", TEST_PCT.replace("test_percent_zero", "test_pz")),
                                               {"text": "TEST: tests/test_fix_n1_2b.py::test_pz"}, {"text": "BLOCKED: stop"}]))
    try:
        run_id = h.submit().json()["run_id"]
        body = review_body(h, run_id, verdict="fail", reopened=["N1-2"])
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", body)
        assert r.status_code == 200 and r.json()["next_run_id"], r.text
        assert _states(h, run_id) == {"N1-1": "accepted", "N1-2": "reopened"}
        h.svc.wait_idle()
    finally:
        h.close()


# ====================================================================== D2: flag, don't chase

PLAIN_DETECT = replace("src/toy/calc.py", "    return part / whole * 100.0\n",
                       "    import sys\n"
                       "    if whole == 0 and any(m.rpartition('.')[2].startswith('test_') for m in sys.modules):\n"
                       "        return 0.0\n"
                       "    return part / whole * 100.0\n")


def test_d2_a_detector_that_passes_every_check_is_flagged_at_the_top_and_blocks_an_unaddressed_accept():
    """The reviewers' B4 (round 22): a plain "a test_* module is loaded" detector passes under pytest AND in the
    standalone run. The engine does not chase it; it flags it."""
    h = Harness(scenario=_p1(PLAIN_DETECT), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"}, extra_files=CONFTEST)
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review", h.run(run_id)["reasons"]
        f = {x["finding_id"]: x for x in h.findings(run_id)}
        flags = f["N1-2"]["review_flags"]
        assert any(fl["construct"] == "sys.modules" and fl["file"] == "services/toy-py/src/toy/calc.py" for fl in flags), flags
        assert all(re.fullmatch(r"N1-2-F[0-9]{3}", fl["id"]) and fl["line"] > 0 and fl["reason"] for fl in flags), flags
        assert f["N1-1"]["review_flags"] == []
        text = h.report(run_id)
        first_result = re.search(r"[0-9]+ passed|verdict pass", text)
        assert first_result and text.index("## Review flags") < min(first_result.start(), text.index("## Suite")), text[:2000]
        for fl in flags:
            assert fl["id"] in text.split("## Suite", 1)[0]
        ev = [e["payload"] for e in h.events("review_flags_recorded") if e["payload"]["finding_id"] == "N1-2"]
        assert ev and {x["id"] for x in ev[-1]["flags"]} == {x["id"] for x in flags}, ev
        body = review_body(h, run_id, flags=[])
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", body)
        assert r.status_code == 422 and flags[0]["id"] in r.text, r.text
        body = review_body(h, run_id, flags=[fl["id"] for fl in flags] + ["N1-2-F999"])
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", body)
        assert r.status_code == 422 and "N1-2-F999" in r.text, r.text      # an unknown flag id is refused
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", review_body(h, run_id))
        assert r.status_code == 200 and _states(h, run_id) == {"N1-1": "accepted", "N1-2": "accepted"}, r.text
        rv = h.events("fix_run_reviewed")[-1]["payload"]
        assert sorted(rv["flags_addressed"]) == sorted(fl["id"] for fl in flags), rv
    finally:
        h.close()


SCAN_DIFF = """diff --git a/services/toy-py/src/toy/calc.py b/services/toy-py/src/toy/calc.py
--- a/services/toy-py/src/toy/calc.py
+++ b/services/toy-py/src/toy/calc.py
@@ -1,2 +1,24 @@
 import os
+import sys
+x = sys.modules
+y = sys.argv
+z = sys.flags.isolated
+f = sys._getframe(1)
+import inspect
+import traceback
+e = os.environ.get("A")
+g = os.getenv("B")
+m = __import__("sys")
+import importlib
+gl = globals()
+vv = vars(m)
+ga = getattr(sys, "path")
+if __name__ == "__main__": pass
+import atexit
+import signal
+t = threading.enumerate()
+o = gc.get_objects()
+import builtins
+n = "py" + "test"
+j = "".join(("py", "test"))
+ok = 1 + 2
diff --git a/services/toy-py/tests/test_x.py b/services/toy-py/tests/test_x.py
--- /dev/null
+++ b/services/toy-py/tests/test_x.py
@@ -0,0 +1,2 @@
+import sys
+assert sys.modules
diff --git a/services/toy-py/src/toy/main.go b/services/toy-py/src/toy/main.go
--- a/services/toy-py/src/toy/main.go
+++ b/services/toy-py/src/toy/main.go
@@ -1,1 +1,4 @@
 package main
+var a = os.Args
+var b = os.Getenv("CI")
+var c = testing.Testing()
diff --git a/services/toy-py/src/toy/lib.rs b/services/toy-py/src/toy/lib.rs
--- a/services/toy-py/src/toy/lib.rs
+++ b/services/toy-py/src/toy/lib.rs
@@ -1,1 +1,3 @@
 fn a() {}
+fn b() -> bool { cfg!(test) }
+fn c() { let _ = std::env::var("CI"); }
diff --git a/services/toy-py/src/toy/x.js b/services/toy-py/src/toy/x.js
--- a/services/toy-py/src/toy/x.js
+++ b/services/toy-py/src/toy/x.js
@@ -1,1 +1,4 @@
 const a = 1;
+const b = process.argv;
+const c = process.env.CI;
+const d = require.main === module;
"""


def test_d2_the_scanner_flags_every_listed_construct_on_source_lines_only():
    from zbm_delivery.engine import review_flags as RF
    flags = RF.scan(SCAN_DIFF, "toy-py", is_test=lambda p: "/tests/" in p or p.rsplit("/", 1)[-1].startswith("test_"))
    by_line = {}
    for fl in flags:
        by_line.setdefault((fl["file"].rsplit("/", 1)[-1], fl["line"]), set()).add(fl["construct"])
    py = {ln: c for (f, ln), c in by_line.items() if f == "calc.py"}
    want = {3: "sys.modules", 4: "sys.argv", 5: "sys.flags", 6: "sys._getframe", 7: "inspect", 8: "traceback",
            9: "os.environ/getenv", 10: "os.environ/getenv", 11: "__import__/importlib", 12: "__import__/importlib",
            13: "globals()/vars()", 14: "globals()/vars()", 15: "getattr on a module", 16: "__main__", 17: "atexit",
            18: "signal", 19: "threading.enumerate", 20: "gc.get_objects", 21: "builtins",
            22: "string concatenation forming an identifier", 23: "string concatenation forming an identifier"}
    for ln, c in want.items():
        assert c in py.get(ln, set()), (ln, c, py.get(ln))
    assert 24 not in py and 1 not in py and 2 not in py                    # plain arithmetic; context lines; `import sys`
    assert not any(f == "test_x.py" for (f, _) in by_line)                  # test files are never flagged
    assert by_line[("main.go", 2)] >= {"os.Args"} and by_line[("main.go", 3)] >= {"os.Getenv"}
    assert by_line[("main.go", 4)] >= {"testing.Testing()"}
    assert by_line[("lib.rs", 2)] >= {"cfg!(test)"} and by_line[("lib.rs", 3)] >= {"std::env"}
    assert by_line[("x.js", 2)] >= {"process.argv"} and by_line[("x.js", 3)] >= {"process.env"}
    assert by_line[("x.js", 4)] >= {"require.main"}
    ids = [fl["id"] for fl in RF.number(flags, "N1-1")]
    assert len(ids) == len(set(ids)) and ids[0] == "N1-1-F001"


# ====================================================================== D3: nothing parked

OBF_ENV_FIX = replace("src/toy/calc.py", "    return max(lo, min(hi, x))\n",
                      "    if lo > hi and __import__('os').environ.get('PY' + 'TES' + 'T_CURRENT_TEST'):\n"
                      "        raise ValueError('lo > hi')\n    return max(lo, min(hi, x))\n")


def test_d3_an_executed_standalone_fail_is_authoritative_even_with_a_conftest():
    """The reviewers' C (round 22): an env-conditional fix, a plain reviewer test, a base conftest.py — the standalone
    run EXECUTED the test and it failed with the fix. Before: parked in needs_review_runner_dependent."""
    scen = scenario_s1() + flat([AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
                                 OBF_ENV_FIX, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}, {"text": "FIXED"}])
    h = Harness(scenario=scen, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"}, extra_files=CONFTEST)
    try:
        doc = two_findings(h.base_sha)
        doc["findings"].append(_n21(reproduction_test={"path": RT_PATH, "content": PLAIN_RT}))
        run_id = h.submit(doc).json()["run_id"]
        st = _states(h, run_id)
        assert st["N2-1"] not in DONE, st
        assert "fix_depends_on_the_test_runner" in _whys(h), _whys(h)
        assert h.run(run_id)["status"] == "failed"
    finally:
        h.close()


PARK_PCT_FIX = replace("src/toy/calc.py", "    return part / whole * 100.0\n",
                       "    if whole == 0:\n        __import__('py' + 'test')\n        return 0.0\n"
                       "    return part / whole * 100.0\n")


def test_d3_a_pytest_import_blocked_from_a_source_frame_fails_the_round():
    """The reviewers' E (round 22): the SOURCE imports pytest on the fixed path; the standalone runner's import hook
    refuses it. Before: classified "the test needs pytest" and parked."""
    h = Harness(scenario=_p1(PARK_PCT_FIX), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        st = _states(h, run_id)
        assert st["N1-2"] not in DONE, st
        assert "fix_imports_test_runner" in _whys(h), _whys(h)
        solo = [e["payload"] for e in h.events("reproduction_standalone_checked") if e["payload"]["finding_id"] == "N1-2"]
        assert solo and solo[-1]["outcome"] == "runner_detected", solo
    finally:
        h.close()


def test_d3_runner_dependent_is_a_flag_and_needs_a_review_note():
    """A reviewer test that imports pytest (the test itself needs the runner): the finding ends
    needs_review_runner_dependent, listed under the report's flags, and an accept without a note is refused."""
    scen = scenario_s1() + flat([AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
                                 FIX_CLAMP, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}])
    h = Harness(scenario=scen)
    try:
        doc = two_findings(h.base_sha)
        doc["findings"].append(_n21())                                      # RT_CONTENT: `import pytest` in the test
        run_id = h.submit(doc).json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review", h.run(run_id)["reasons"]
        f = {x["finding_id"]: x for x in h.findings(run_id)}
        assert f["N2-1"]["state"] == "needs_review_runner_dependent"
        rd = [fl for fl in f["N2-1"]["review_flags"] if fl["construct"] == "runner_dependent_reproduction"]
        assert len(rd) == 1 and rd[0]["id"] == "N2-1-RD", f["N2-1"]["review_flags"]
        top = h.report(run_id).split("## Suite", 1)[0]
        assert "N2-1-RD" in top, top
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", review_body(h, run_id, notes={"N2-1": ""}))
        assert r.status_code == 422 and "N2-1" in r.text and "note" in r.text, r.text
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review",
                   review_body(h, run_id, notes={"N2-1": "read the diff: clamp raises for lo > hi in every caller"}))
        assert r.status_code == 200 and _states(h, run_id)["N2-1"] == "accepted", r.text
    finally:
        h.close()


# ====================================================================== B1: no orphaned review

def test_b1_a_failing_review_while_another_run_is_in_flight_records_nothing():
    """The reviewers' test_r22_lost: the review used to be recorded reviewed_fail and its findings left with no run."""
    h = Harness(scenario=scenario_s1() + scenario_s1() + scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        gate = threading.Event()
        real = h.docker.run

        def slow(argv, **kw):                     # hold run2 in flight until the review has been answered
            a = [str(x) for x in argv]
            if a[:1] == ["run"] and "-suite-" in a[a.index("--name") + 1] and run1 not in a[a.index("--name") + 1]:
                gate.wait(60)
            return real(argv, **kw)
        h.docker.run = slow
        run2 = h.submit(two_findings(h.base_sha), wait=False).json()["run_id"]
        body = {"request_id": rid(), "review_ref": "r23-b1", "sha256": "e" * 64, "verdict": "fail", "reopened": ["N1-1"],
                "new_findings": [finding("N9-1", line=15, reproduction="run tests/test_calc.py::test_add_returns_sum: x",
                                         expected="y", observed="z")]}
        r = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert r.status_code == 409 and "RUN_IN_PROGRESS" in r.text, r.text
        assert h.run(run1)["status"] == "awaiting_review" and not h.events("fix_run_reviewed")
        replay = h.post(f"/dlv/v1/fix-runs/{run1}/review", body)
        assert replay.status_code == 409, replay.text
        gate.set()
        h.svc.wait_idle(240)
        assert h.run(run2)["status"] in ("awaiting_review", "failed")
        again = h.post(f"/dlv/v1/fix-runs/{run1}/review", dict(body, request_id=rid()))
        assert again.status_code == 200 and again.json()["next_run_id"], again.text
        holders = [x["run_id"] for x in h.svc.runs.values() if "N9-1" in (x.get("finding_ids") or [])]
        assert holders == [again.json()["next_run_id"]], holders
        assert len(h.events("fix_run_reviewed")) == 1
        h.svc.wait_idle(240)
    finally:
        h.close()


# ====================================================================== B2: no container under the service lock

HANG_S = 15
HANG_RT = ("import time\n\nfrom toy import calc\n\n\ndef test_clamp_hangs():\n"
           f"    time.sleep({HANG_S})\n    assert calc.clamp(5, 3, 0) != 3\n")
HANG_PATH = "tests/test_review_hang.py"


def _second_service(h: Harness) -> str:
    """Commit a copy of toy-py as services/toy2-py on the base branch; returns the new base sha."""
    shutil.copytree(os.path.join(h.repo, "services", "toy-py"), os.path.join(h.repo, "services", "toy2-py"))
    git("add", "-A", cwd=h.repo)
    git("commit", "-q", "-m", "fixture: toy2-py", cwd=h.repo)
    return git("rev-parse", "HEAD", cwd=h.repo)


def _wait_for(pred, stall_s: float = 60.0) -> bool:
    """Poll ``pred`` until it holds; False after ``stall_s`` (a bound on a stall, never a measured speed)."""
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > stall_s:
            return False
        time.sleep(0.02)
    return True


def test_b2_the_red_admission_check_never_holds_the_service_lock_across_a_container():
    """Wave 25 (scout B M2): ordered by state, not by seven latencies under 0.25 s and a `sleep(1.0)`. The admission's
    container is HELD (its first exec waits on a gate the test opens) and every probe must answer while it is held: a
    service lock held across the container would keep them waiting until the gate opened."""
    h = Harness(scenario=scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]              # toy-py, awaiting review
        base2 = _second_service(h)
        held, entered = threading.Event(), threading.Event()
        in_box, gate = threading.Event(), threading.Event()
        real = h.docker.run

        def hold(argv, **kw):                     # toy2-py's run waits inside its first engine container start
            a = [str(x) for x in argv]
            if a[:1] == ["run"] and "-suite-" in a[a.index("--name") + 1] and run1 not in a[a.index("--name") + 1]:
                entered.set()
                held.wait(90)
            if a[:1] == ["exec"] and any("-admission-" in x for x in a):
                in_box.set()                      # the admission's RED container is running ...
                gate.wait(120)                    # ... and stays so until the probes are done
            return real(argv, **kw)
        h.docker.run = hold
        doc2 = findings_doc(base2, [finding("N1-1", file="services/toy2-py/src/toy/calc.py")], service="toy2-py")
        run2 = h.post("/dlv/v1/fix-runs", doc2).json()["run_id"]
        assert entered.wait(60)
        docA = findings_doc(h.base_sha, [finding("N2-1", line=15, class_hint="argument_validation",
                                                  reproduction=f"run {HANG_PATH}::test_clamp_hangs: clamp(5, 3, 0) answers 3",
                                                  expected="ValueError", observed="3",
                                                  reproduction_test={"path": HANG_PATH, "content": HANG_RT})])
        res = {}
        th = threading.Thread(target=lambda: res.update(r=h.post("/dlv/v1/fix-runs", docA)))
        th.start()
        assert in_box.wait(60), "the admission's container never ran"
        assert _wait_for(lambda: any(e["event_type"] == "engine_box_started" and e["payload"].get("tag") == "admission"
                                     for e in h.ledger.events))
        p = {}

        def probes():
            p["g1"] = h.get(f"/dlv/v1/fix-runs/{run1}")
            p["g2"] = h.get(f"/dlv/v1/fix-runs/{run2}")
            p["c2"] = h.post(f"/dlv/v1/fix-runs/{run2}/cancel", {"request_id": rid(), "reason": "operator stop"})
            p["c1"] = h.post(f"/dlv/v1/fix-runs/{run1}/cancel", {"request_id": rid(), "reason": "stop"})
            p["hl"] = h.client.get("/health")
            # the pending admission is in flight for toy-py: a second document and a failing review are refused at once
            p["d2"] = h.post("/dlv/v1/fix-runs", two_findings(h.base_sha))
            p["rv"] = h.post(f"/dlv/v1/fix-runs/{run1}/review", {
                "request_id": rid(), "review_ref": "r23-b2", "sha256": "f" * 64, "verdict": "fail", "reopened": ["N1-1"],
                "new_findings": []})
        pt = threading.Thread(target=probes)
        pt.start()
        pt.join(60)                                                               # a bound on a stall only
        answered_while_held = not pt.is_alive() and not gate.is_set()
        still_running = th.is_alive()
        gate.set()
        held.set()
        pt.join(120)
        th.join(120)
        print("B2 admit", res["r"].status_code)
        assert answered_while_held, f"a probe waited behind the admission's running container: answered {sorted(p)}"
        g1, g2, c2, c1, hl, d2, rv = (p[k] for k in ("g1", "g2", "c2", "c1", "hl", "d2", "rv"))
        assert still_running, "the admission finished before the probes: the probes proved nothing"
        assert (g1.status_code, g2.status_code, c2.status_code, c1.status_code, hl.status_code) == (200, 200, 200, 409, 200)
        assert c2.json()["status"] == "failed"
        assert d2.status_code == 409 and "RUN_IN_PROGRESS" in d2.text, d2.text
        assert rv.status_code == 409 and "RUN_IN_PROGRESS" in rv.text, rv.text
        assert res["r"].status_code == 202, res["r"].text
        started = [e for e in h.events("reproduction_red_check_started")]
        assert len(started) == 1 and started[0]["payload"]["service"] == "toy-py"
        h.svc.wait_idle(240)
    finally:
        gate.set()
        held.set()
        h.close()


# ====================================================================== B3: wait_idle waits for the engine thread

def test_b3_wait_idle_returns_only_after_the_engine_thread_settled():
    scen = flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"}, FIX_ADD,
                 {"text": "SWEEP: src/toy/calc.py:6\nFIXED"}])
    h = Harness(scenario=scen)
    try:
        box, fired = {}, {"n": 0}
        real = h.docker.run

        def run(argv, **kw):
            a = [str(x) for x in argv]
            if (not fired["n"] and a[:1] == ["run"] and "-verify-" in a[a.index("--name") + 1] and box.get("id")
                    and any(e["event_type"] == "test_run" and e["payload"].get("phase") == "green" for e in h.ledger.events)):
                fired["n"] += 1
                h.svc.cancel("aegis", box["id"], {"request_id": rid(), "reason": "stop"})
                time.sleep(1.5)                   # the engine thread is still inside `docker run` after the status is failed
            return real(argv, **kw)
        h.docker.run = run
        box["id"] = h.post("/dlv/v1/fix-runs", findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        h.svc.wait_idle(120)
        seq = [e["event_type"] for e in h.events()]
        assert fired["n"] == 1
        after = seq[seq.index("fix_run_cancelled") + 1:]
        assert "engine_box_killed_after_cancel" in after, after
        assert h.svc._queue.unfinished_tasks == 0
    finally:
        h.close()


# ====================================================================== B4: kill-after-cancel is record-first

def _cancel_at_verify_start(h: Harness, box: dict, fired: dict, order: list):
    real = h.docker.run

    def run(argv, **kw):
        a = [str(x) for x in argv]
        if (not fired["n"] and a[:1] == ["run"] and "-verify-" in a[a.index("--name") + 1] and box.get("id")
                and any(e["event_type"] == "test_run" and e["payload"].get("phase") == "green" for e in h.ledger.events)):
            fired["n"] += 1
            fired["name"] = a[a.index("--name") + 1]
            h.svc.cancel("aegis", box["id"], {"request_id": rid(), "reason": "stop"})
        if a[:1] == ["kill"]:
            mine = [e["event_type"] for e in h.ledger.events if a[-1] in json.dumps(e["payload"])]
            order.append((a[-1], mine[-1:]))
        return real(argv, **kw)
    h.docker.run = run


def test_b4_the_kill_of_a_box_that_started_after_cancel_is_recorded_first():
    scen = flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"}, FIX_ADD,
                 {"text": "SWEEP: src/toy/calc.py:6\nFIXED"}])
    h = Harness(scenario=scen)
    try:
        box, fired, order = {}, {"n": 0}, []
        _cancel_at_verify_start(h, box, fired, order)
        box["id"] = h.post("/dlv/v1/fix-runs", findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        h.svc.wait_idle(120)
        kills = [o for o in order if o[0] == fired["name"]]
        assert fired["n"] == 1 and kills == [(fired["name"], ["sandbox_kill_requested"])], order
        req = [e["payload"] for e in h.events("sandbox_kill_requested") if e["payload"].get("container") == fired["name"]]
        assert req and req[0].get("why") == "started_after_cancel", req
        done = [e["payload"] for e in h.events("engine_box_killed_after_cancel")]
        assert done and done[0]["container"] == fired["name"] and "exit" in done[0], done
        assert not h.run(box["id"]).get("unrecorded_failure")
    finally:
        h.close()


def test_b4_an_unrecordable_kill_still_kills_and_marks_the_run_unrecorded_failure():
    scen = flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"}, FIX_ADD,
                 {"text": "SWEEP: src/toy/calc.py:6\nFIXED"}])
    h = Harness(scenario=scen)
    try:
        box, fired, order = {}, {"n": 0}, []
        _cancel_at_verify_start(h, box, fired, order)
        h.ledger.fail_on_type = "sandbox_kill_requested"
        box["id"] = h.post("/dlv/v1/fix-runs", findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        h.svc.wait_idle(120)
        assert fired["n"] == 1 and any(o[0] == fired["name"] for o in order), order     # safety wins: killed anyway
        assert fired["name"] not in h.docker.containers
        run = h.svc.run_get(box["id"])
        assert run["status"] == "failed" and run.get("unrecorded_failure") is True, run
        assert any(r["code"] == "LEDGER_UNAVAILABLE" for r in run["reasons"]), run["reasons"]
    finally:
        h.close()


# ====================================================================== B5: docstring references exist

def test_b5_file_references_in_the_modules_name_files_that_exist():
    src = SERVICE_ROOT / "src" / "zbm_delivery"
    repo = SERVICE_ROOT.parent.parent
    seen = 0
    for mod in ("graceful_close.py", "serve.py"):
        text = (src / mod).read_text(encoding="utf-8")
        for svc, ref in re.findall(r"(services/[a-z0-9-]+/)?(?:tests/)?\b(test_[a-z0-9_]+\.py)\b", text):
            seen += 1
            where = repo / svc / "tests" / ref if svc else SERVICE_ROOT / "tests" / ref
            assert where.is_file(), (mod, svc, ref)
    assert seen >= 2
