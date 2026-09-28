"""Fix wave 21, lead rulings L2 (reviewer-authored reproduction tests) and L4 (no temp-dir leak)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import json

import pytest

from helpers import SERVICE_ROOT, Harness, finding, findings_doc, flat, replace, rid, scenario_s1, write_test

from zbm_delivery import policy  # noqa: E402
from zbm_delivery.policy import WORKSPACE  # noqa: E402

RT_PATH = "tests/test_review_clamp.py"
RT_NODE = f"{RT_PATH}::test_clamp_rejects_inverted_bounds"
RT_CONTENT = ("import pytest\n\nfrom toy import calc\n\n\ndef test_clamp_rejects_inverted_bounds():\n"
              "    with pytest.raises(ValueError):\n        calc.clamp(5, 3, 0)\n")
FIX_CLAMP = replace("src/toy/calc.py", "    return max(lo, min(hi, x))\n",
                    "    if lo > hi:\n        raise ValueError(\"lo > hi\")\n    return max(lo, min(hi, x))\n")
AGENT_RED = write_test("test_fix_n2_1", "import pytest\n\nfrom toy import calc\n\n\ndef test_clamp_inverted():\n"
                                        "    with pytest.raises(ValueError):\n        calc.clamp(5, 3, 0)\n")


def _n21(**kw) -> dict:
    return finding("N2-1", line=15, class_hint="argument_validation",
                   reproduction=kw.pop("reproduction", f"run {RT_NODE}: clamp(5, 3, 0) answers 3 silently"),
                   expected="ValueError for lo > hi", observed="3",
                   reproduction_test=kw.pop("reproduction_test", {"path": RT_PATH, "content": RT_CONTENT}), **kw)


def _review(new_findings, reopened=()) -> dict:
    return {"request_id": rid(), "review_ref": "review-l2", "sha256": "c" * 64, "verdict": "fail",
            "reopened": list(reopened), "new_findings": new_findings}


def _whys(h: Harness) -> list[str]:
    return [e["payload"].get("why") for e in h.events("round_failed")]


def _first_run_awaiting_review(h: Harness) -> str:
    run_id = h.submit().json()["run_id"]
    assert h.run(run_id)["status"] == "awaiting_review", h.run(run_id)["reasons"]
    return run_id


# ====================================================================== L2: the review route under R1

def test_l2_new_finding_at_review_with_a_reviewer_test_goes_red_to_fixed_honestly():
    """A defect with NO test at the run's head (clamp accepts lo > hi) raised at review with a reviewer-authored
    reproduction: RED on the head at ingestion, then the child run's agent fixes the source and the finding is
    fixed ONLY through that test (verification pass, reverted fail). The test is recorded as reviewer-authored
    and is never committed by the engine."""
    scenario = scenario_s1() + flat([AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
                                     FIX_CLAMP, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}])
    h = Harness(scenario=scenario)
    try:
        run_id = _first_run_awaiting_review(h)
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", _review([_n21()]))
        assert r.status_code == 200, r.text
        child_id = r.json()["next_run_id"]
        h.svc.wait_idle()
        child = h.run(child_id)
        assert child["status"] == "awaiting_review", child["reasons"]
        f = {x["finding_id"]: x for x in h.findings(child_id)}["N2-1"]
        sha = hashlib.sha256(RT_CONTENT.encode()).hexdigest()
        assert f["state"] == "fixed" and f["reviewer_test"] == {"path": RT_PATH, "author": "reviewer", "sha256": sha}, f
        rc = f["repro_check"]
        assert rc["target"] == RT_NODE and rc["verification"]["verdict"] == "pass" and rc["reverted"]["verdict"] == "fail", rc
        received = [e["payload"] for e in h.events("fix_run_received") if e["payload"]["run_id"] == child_id]
        assert received[0]["reviewer_tests"] == [{"finding_id": "N2-1", "path": RT_PATH, "sha256": sha, "author": "reviewer"}]
        # the admission RED run happened on the head (the child's base), under its own admission id
        assert not h.events("fix_run_refused")
        red = [e["payload"] for e in h.events("reproduction_red_checked")]
        head = h.run(run_id)["commits"][-1]["sha"]
        assert len(red) == 1 and red[0]["verdict"] == "fail" and red[0]["base_sha"] == head and red[0]["target"] == RT_NODE, red
        assert red[0]["test_sha256"] == sha and red[0]["admission_id"] != child_id
        # never committed: the commit holds the agent's test and the fix, not the reviewer's file
        files = [p for c in child["commits"] for p in c["files"]]
        assert f"services/toy-py/{RT_PATH}" not in files and "services/toy-py/src/toy/calc.py" in files, files
        assert not os.path.exists(os.path.join(child["worktree_path"], "services/toy-py", RT_PATH))
    finally:
        h.close()


def test_l2_the_agent_writing_the_reviewer_test_path_is_denied():
    """The agent 'fixes' the finding by overwriting the reviewer's test with one that passes: any diff at that
    path is changed_test_denied (it is not in the worktree, so the file is new there — still refused)."""
    forged = "def test_clamp_rejects_inverted_bounds():\n    assert True\n"
    scenario = scenario_s1() + flat([
        AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
        write_test("test_review_clamp", forged), FIX_CLAMP, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}])
    h = Harness(scenario=scenario, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = _first_run_awaiting_review(h)
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", _review([_n21()]))
        assert r.status_code == 200, r.text
        child_id = r.json()["next_run_id"]
        h.svc.wait_idle()
        f = {x["finding_id"]: x for x in h.findings(child_id)}["N2-1"]
        assert f["state"] != "fixed", f
        denied = [e["payload"] for e in h.events("round_failed")
                  if e["payload"].get("why") == "changed_test_denied" and e["payload"].get("finding_id") == "N2-1"]
        assert denied and denied[0]["reviewer_authored"] is True and denied[0]["targets"] == [f"services/toy-py/{RT_PATH}"], denied
        assert not h.run(child_id)["commits"]
    finally:
        h.close()


def test_l2_a_reviewer_test_that_passes_on_its_base_is_refused_at_ingestion():
    """A reviewer test that already passes proves nothing: 422 reproduction_not_red, nothing created, the refusal
    (with the verdict and output digest) on the ledger — for a findings document and for a failing review."""
    passing = ("from toy import calc\n\n\ndef test_clamp_rejects_inverted_bounds():\n"
               "    assert calc.clamp(5, 0, 10) == 5\n")
    h = Harness(scenario=scenario_s1())
    try:
        doc = findings_doc(h.base_sha, [_n21(reproduction_test={"path": RT_PATH, "content": passing})])
        r = h.submit(doc)
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "reproduction_not_red" and r.json()["finding_id"] == "N2-1", r.json()
        assert h.svc.runs == {} and not h.events("fix_run_received")
        ref = [e["payload"] for e in h.events("fix_run_refused")]
        assert ref and ref[-1]["code"] == "REPRODUCTION_NOT_RED" and ref[-1]["verdict"] == "pass" and ref[-1]["output_sha256"], ref
        # the review route: the run stays awaiting_review, no review recorded
        run_id = _first_run_awaiting_review(h)
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", _review([_n21(reproduction_test={"path": RT_PATH, "content": passing})]))
        assert r.status_code == 422 and r.json()["code"] == "reproduction_not_red", r.text
        assert h.run(run_id)["status"] == "awaiting_review" and not h.events("fix_run_reviewed")
    finally:
        h.close()


@pytest.mark.parametrize("rt,repro,why", [
    ({"path": "tests/test_calc.py", "content": RT_CONTENT}, "run tests/test_calc.py::test_clamp_rejects_inverted_bounds",
     "already a file of the base commit"),
    ({"path": RT_PATH, "content": RT_CONTENT}, "run tests/test_calc.py::test_add_returns_sum: add", "not a test of reproduction_test.path"),
    ({"path": RT_PATH, "content": RT_CONTENT}, f"run {RT_PATH}::test_other: x", "does not occur in"),
    ({"path": RT_PATH, "content": RT_CONTENT.replace("calc.clamp(5, 3, 0)", "calc.clamp(5, 3, 0)\n    import os\n    os._exit(1)")},
     f"run {RT_NODE}", "test content rule"),
    ({"path": "conftest.py", "content": "def test_x():\n    pass\n"}, "run conftest.py::test_x", "test infrastructure"),
], ids=["existing-file", "node-elsewhere", "name-missing", "content-rule", "test-infra"])
def test_l2_a_reviewer_test_must_be_a_new_runnable_test_of_the_named_node(rt, repro, why):
    h = Harness(scenario=[], pct_repro=False)
    try:
        r = h.submit(findings_doc(h.base_sha, [_n21(reproduction=repro, reproduction_test=rt)]))
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "reproduction_not_runnable" and why in r.json()["detail"], r.json()
        assert h.svc.runs == {}
    finally:
        h.close()


@pytest.mark.parametrize("rt", [
    {"path": "../x/test_a.py", "content": "x"}, {"path": "/abs/test_a.py", "content": "x"},
    {"path": RT_PATH, "content": "a\x1bb"}, {"path": RT_PATH, "content": "x" * (64 * 1024 + 1)},
    {"path": RT_PATH, "content": "x", "author": "agent"},
], ids=["dotdot", "absolute", "control-char", "too-long", "extra-field"])
def test_l2_reviewer_test_schema_edge(rt):
    h = Harness(scenario=[], pct_repro=False)
    try:
        r = h.submit(findings_doc(h.base_sha, [_n21(reproduction_test=rt)]))
        assert r.status_code == 422 and h.svc.runs == {}, r.text
    finally:
        h.close()


# ====================================================================== L4: no temp-dir leak

def test_l4_a_test_session_leaves_no_new_directory_in_the_temp_dir(tmp_path):
    """A child pytest session (harness tests: repositories, engine trees, the git isolation dir, pytest's own
    tmp_path) with a fresh TMPDIR leaves it empty except the deliberately shared Go build cache."""
    tmpdir = tmp_path / "t"
    tmpdir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["TMPDIR"] = str(tmpdir)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_cert_scenarios.py::test_s1_clean_loop_reaches_awaiting_review",
                        "tests/test_round20.py::test_n20_d4_pytest_rootdir_is_the_process_working_directory"],
                       cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    left = sorted(p.name for p in tmpdir.iterdir())
    assert left in ([], ["dlv-test-gocache"]), left


# ====================================================================== L3: R4 stays strict

@pytest.mark.parametrize("cmd", ["cat x | sed 's/a/b/'", "grep y x | awk '{print $1}'", "sed -n 1p <<< 'a'"])
def test_l3_harmless_pipes_into_text_tools_are_refused_and_the_answer_names_the_file_tools(cmd):
    """Lead ruling L3: fail closed. A harmless `| sed`/`| awk` is refused like `| bash`; the refusal tells the
    engineer to use the file tools, and the engineer's system prompt says so up front."""
    seed = json.load(open(SERVICE_ROOT / "seed" / "tool_policy_seed.json"))
    v = policy.classify(seed, "bash", {"command": cmd}, policy.Context(service="toy-py", workspace=WORKSPACE,
                                                                        evidence_root="/data/evidence"))
    assert v.deny and "pipe_to_interpreter" in v.message and "str_replace" in v.message, (cmd, v)
    system = (SERVICE_ROOT / "prompts" / "engine.system.md").read_text()
    assert "| sed" in system and "even when harmless" in system
