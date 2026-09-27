"""Scenario certification (spec §F S1-S12): the loop end to end with the deterministic fake model against the
toy-py fixture repository, through the real deer-flow harness, the real guardrail and the argv-level Docker double."""

from __future__ import annotations

import json
import os

import pytest

from helpers import (FIX_ADD, TEST_ADD, WS, Harness, finding, findings_doc, replace, rid, scenario_s1, two_findings,
                     write_test)


@pytest.fixture
def s1():
    h = Harness()
    yield h
    h.close()


def _run_events(h: Harness, run_id: str) -> list[dict]:
    return [e for e in h.ledger.events if e["subject_id"] == run_id or e["subject_id"].startswith(run_id + ":")]


def test_s1_clean_loop_reaches_awaiting_review(s1: Harness):
    h = s1
    r = h.submit()
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "received" and body["request_id"] and body["facts_sha256"]
    run_id = body["run_id"]
    run = h.run(run_id)
    assert run["status"] == "awaiting_review", run.get("reasons")
    # branch D8 in the temp repo: no fix<N>- branch existed → fix1-toy-py
    assert run["branch"] == "fix1-toy-py"
    assert os.path.isdir(run["worktree_path"])
    # suite before N-1 passed / 1 failed → after N passed
    before, after = run["suite"]["before"]["counts"], run["suite"]["after"]["counts"]
    assert (before["passed"], before["failed"]) == (2, 1) and before["failed_names"] == ["tests/test_calc.py::test_add_returns_sum"]
    assert (after["passed"], after["failed"]) == (5, 0)
    # per finding: RED (exit 1) then GREEN (exit 0), revert check failed-then-passed, sweep, commit, fixed
    fs = {f["finding_id"]: f for f in h.findings(run_id)}
    for fid in ("N1-1", "N1-2"):
        f = fs[fid]
        assert f["state"] == "fixed", f
        assert f["red"]["exit"] == 1 and f["green"]["exit"] == 0
        assert f["red"]["test_name"] == f["green"]["test_name"]
        assert f["revert_check"]["exit"] != 0 and f["revert_check"]["restored_exit"] == 0
        assert f["sweep"]["sites"] and f["commit_sha"] and f["commit_files"]
    assert len(run["commits"]) == 2
    # the commits are on the fix branch, with the attribution trailer
    log = h.git.log(run["worktree_path"], 5)
    assert log[:2] == [run["commits"][1]["sha"], run["commits"][0]["sha"]]
    import subprocess
    msg = subprocess.run(["git", "-C", run["worktree_path"], "log", "-1", "--format=%B"], capture_output=True, text=True).stdout
    assert "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" in msg and "Claude-Session:" in msg
    # every transition is a ledger event carrying the run id; the received event carries request_id + facts_sha256
    types = [e["event_type"] for e in _run_events(h, run_id)]
    for t in ("fix_run_received", "fix_run_started", "worktree_created", "sandbox_acquired", "suite_run", "brief_written",
              "finding_started", "test_run", "finding_state_changed", "commit_recorded", "report_written",
              "fix_run_awaiting_review", "tool_call_decided", "tool_result_recorded", "sandbox_exec_requested",
              "sandbox_exec_completed", "sandbox_released", "prompts_loaded"):
        assert t in types, t
    rcv = h.events("fix_run_received")[0]["payload"]
    assert rcv["request_id"] == body["request_id"] and rcv["facts_sha256"] == body["facts_sha256"]
    # the report cites every evidence id and carries the runner's counts
    report = h.report(run_id)
    for ev in run["evidence"]:
        if ev["evidence_id"] != run["report_evidence_id"]:          # the report cannot cite its own hash
            assert ev["evidence_id"] in report
    assert "5 passed / 0 failed" in report and "2 passed / 1 failed" in report
    # evidence files: content addressed, 0444, hash matches
    for ev in run["evidence"]:
        path = os.path.join(h.svc.evidence_root, run_id, ev["evidence_id"])
        assert os.path.isfile(path) and (os.stat(path).st_mode & 0o777) == 0o444
        r2 = h.get(f"/dlv/v1/fix-runs/{run_id}/evidence/{ev['evidence_id']}")
        assert r2.status_code == 200 and r2.headers["X-DLV-Evidence-SHA256"] == ev["sha256"]
    # the sandbox was destroyed after the run (docker rm -f + volume rm), never before the report
    calls = h.docker.calls
    idx_rm = next(i for i, c in enumerate(calls) if c[:2] == ["rm", "-f"])
    assert any(c[:2] == ["volume", "rm"] for c in calls[idx_rm:])
    assert run["sandbox"]["image_digest"] == "0" * 64


def test_s2_test_passing_on_unfixed_code_fails_the_round():
    bad_test = "from toy import calc\n\n\ndef test_add_sum():\n    assert calc.add(2, 2) == 0\n"   # passes on the bug
    scenario = [
        write_test("test_fix_n1_1", bad_test), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
        # the engineer rewrites its own test (read first: deer-flow's read-before-write gate) and names it again
        {"tool_calls": [{"name": "read_file", "args": {"path": f"{WS}/tests/test_fix_n1_1.py"}}]},
        write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
        FIX_ADD, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"},
    ]
    h = Harness(scenario=scenario)
    try:
        doc = findings_doc(h.base_sha, [finding("N1-1")])
        run_id = h.submit(doc).json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "awaiting_review", run["reasons"]
        f = h.findings(run_id)[0]
        assert f["state"] == "fixed" and f["rounds"] >= 2
        reds = [e for e in h.events("test_run") if e["payload"]["phase"] == "red"]
        assert [e["payload"]["exit"] for e in reds] == [0, 1]
        assert any(e["payload"].get("why") == "test_passes_on_unfixed_code" for e in h.events("round_failed"))
        # the engineer was told
        assert any("UNFIXED code and it PASSED" in m["content"] for t in h.model.calls for m in t.messages if m["role"] == "user")
    finally:
        h.close()


def test_s3_suite_failure_the_engineer_did_not_cause_blocks_fixed_and_fails_the_run():
    # the fix also breaks an unrelated existing test (clamp): the suite is red → the finding cannot reach fixed
    break_clamp = replace("src/toy/calc.py", "    return max(lo, min(hi, x))\n", "    return x\n")
    scenario = [
        write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
        FIX_ADD, break_clamp, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"},
        {"text": "FIXED"}, {"text": "FIXED"}, {"text": "FIXED"}, {"text": "FIXED"},
    ]
    h = Harness(scenario=scenario, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "3"})
    try:
        run_id = h.submit(findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "failed"
        f = h.findings(run_id)[0]
        assert f["state"] == "blocked"
        assert "tests/test_calc.py::test_clamp" in f["suite_failures"]
        assert "tests/test_calc.py::test_clamp" in run["new_defects"]
        assert run["commits"] == []
        assert any(e["event_type"] == "fix_run_failed" for e in h.ledger.events)
        # no report is written for a failed run; the run view names the new defect
        assert h.get(f"/dlv/v1/fix-runs/{run_id}/report").status_code == 404
    finally:
        h.close()


def test_s4_disproof_with_a_reproduction_the_engine_ran():
    """Round 18 R3 changed this scenario: the engine runs the FINDING's own reproduction (the node id named in the
    findings document, seeded argv) on the untouched base tree; the engineer's `DISPROOF:` argv is never run. The
    fixture's pre-existing failure belongs to N1-1, which is fixed first so suite.after is green."""
    statement = ("The finding claims percent(1, 4) answers 20.0. The reproduction it names is the existing test that asserts "
                 "percent(1, 4) == 25.0 and it passes on the untouched tree, so the observed value is 25.0.")
    scenario = [write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
                FIX_ADD, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"},
                {"text": "DISPROOF: pytest -q -p no:cacheprovider tests/test_calc.py::test_clamp\n" + statement}]
    h = Harness(scenario=scenario)
    try:
        doc = findings_doc(h.base_sha, [finding("N1-1"),
                                        finding("N1-2", line=11, reproduction="run tests/test_calc.py::test_percent_basic: percent(1, 4) answers 20.0",
                                                expected="25.0", observed="20.0", class_hint="wrong_result")])
        run_id = h.submit(doc).json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "awaiting_review", run["reasons"]
        f = {x["finding_id"]: x for x in h.findings(run_id)}["N1-2"]
        assert f["state"] == "disproved"
        assert f["disproof"]["exit"] == 0 and f["disproof"]["reproduction_argv"][0] == "pytest"
        assert "tests/test_calc.py::test_percent_basic" in f["disproof"]["reproduction_argv"]        # the finding's, not the agent's
        assert "tests/test_calc.py::test_clamp" not in f["disproof"]["reproduction_argv"]
        assert f["disproof"]["statement_sha256"] and f["disproof"]["evidence_id"] and f["disproof"]["verdict"] == "pass"
        assert any("disproof — verify" in r["message"] for r in f["reasons"])
        assert "DISPROOF — VERIFY" in h.report(run_id)
        assert any(e["payload"]["phase"] == "disproof" for e in h.events("test_run"))
    finally:
        h.close()


def test_s5_review_fail_reopens_and_opens_a_new_run_on_the_same_branch():
    scenario = scenario_s1() + [
        # rerun: N1-1 reopened (a stricter test), N2-1 new (clamp must handle lo > hi by swapping)
        write_test("test_fix_n1_1b", "from toy import calc\n\n\ndef test_add_neg():\n    assert calc.add(-2, -3) == -5\n"),
        {"text": "TEST: tests/test_fix_n1_1b.py::test_add_neg"},
        {"text": "BLOCKED: cannot reproduce — add(-2, -3) already answers -5 on this tree"},
        write_test("test_fix_n2_1", "import pytest\n\nfrom toy import calc\n\n\ndef test_clamp_rejects_inverted_bounds():\n"
                                    "    with pytest.raises(ValueError):\n        calc.clamp(5, 3, 0)\n"),
        {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_rejects_inverted_bounds"},
        replace("src/toy/calc.py", "    return max(lo, min(hi, x))\n", "    if lo > hi:\n        raise ValueError(\"lo > hi\")\n    return max(lo, min(hi, x))\n"),
        {"text": "SWEEP: src/toy/calc.py:15\nFIXED"},
    ]
    h = Harness(scenario=scenario)
    try:
        run_id = h.submit().json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review"
        review = {"request_id": rid(), "review_ref": "review-2", "sha256": "b" * 64, "verdict": "fail", "reopened": ["N1-1"],
                  "new_findings": [finding("N2-1", line=15, class_hint="argument_validation", reproduction="clamp(5, 3, 0) answers 3 silently",
                                           expected="ValueError for lo > hi", observed="3")]}
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", review)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "reviewed_fail" and body["next_run_id"]
        h.svc.wait_idle()
        child = h.run(body["next_run_id"])
        assert child["parent_run_id"] == run_id and child["branch"] == "fix1-toy-py"
        assert child["worktree_path"] == h.run(run_id)["worktree_path"]
        assert sorted(child["finding_ids"]) == ["N1-1", "N2-1"]
        # both findings entered the loop: N1-1's test passed on the (already fixed) tree, then the engineer replied
        # BLOCKED → the child run ends failed with N1-1 blocked and N2-1 fixed (no parking, §0.1.5)
        fs = {f["finding_id"]: f for f in h.findings(body["next_run_id"])}
        assert fs["N2-1"]["state"] == "fixed" and fs["N1-1"]["state"] == "blocked"
        assert child["status"] == "failed"
        # the review is recorded once, with request_id + facts_sha256; a replay returns the stored answer (A13)
        assert len(h.events("fix_run_reviewed")) == 1
        r2 = h.post(f"/dlv/v1/fix-runs/{run_id}/review", review)
        assert r2.status_code == 200 and r2.json() == body
        assert len(h.events("fix_run_reviewed")) == 1
        # a review for a run not awaiting review → 409
        r3 = h.post(f"/dlv/v1/fix-runs/{run_id}/review", {**review, "request_id": rid()})
        assert r3.status_code == 409
    finally:
        h.close()


def test_s5b_review_pass_is_terminal():
    h = Harness()
    try:
        run_id = h.submit().json()["run_id"]
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", {"request_id": rid(), "review_ref": "r", "sha256": "c" * 64, "verdict": "pass"})
        assert r.status_code == 200 and r.json()["status"] == "reviewed_pass" and r.json()["next_run_id"] is None
        assert all(f["state"] == "reviewed" for f in h.findings(run_id))
        assert h.post(f"/dlv/v1/fix-runs/{run_id}/cancel", {"request_id": rid(), "reason": "x"}).status_code == 409
    finally:
        h.close()


def test_s6_deadline_expiry_mid_round_fails_the_run_and_keeps_evidence():
    scenario = [write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
                {"advance_clock_s": 3000}, FIX_ADD, {"text": "FIXED"}]
    h = Harness(scenario=scenario)
    try:
        run_id = h.submit(findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "failed" and any(r["code"] == "DEADLINE" for r in run["reasons"])
        assert h.events("fix_run_deadline")
        assert h.findings(run_id)[0]["state"] == "red"
        for ev in run["evidence"]:
            assert os.path.isfile(os.path.join(h.svc.evidence_root, run_id, ev["evidence_id"]))
        assert any(c[:2] == ["rm", "-f"] for c in h.docker.calls) and h.events("sandbox_released")
        # after the deadline, tool calls were denied (the model's fix could not run)
        denied = [e for e in h.events("tool_call_decided") if e["payload"]["decision"] == "deny"]
        assert any(e["payload"]["code"] == "DEADLINE" for e in denied)
    finally:
        h.close()


def test_s7_docker_daemon_absent_refuses_before_any_worktree():
    h = Harness(docker=False)
    try:
        assert h.get("/health", caller=None).json()["sandbox"] == "unavailable"
        r = h.submit()
        assert r.status_code == 503 and r.json()["reasons"][0]["code"] == "SANDBOX_UNAVAILABLE"
        assert r.json()["reason_lines"][0].startswith("dlv/DLV-02/SANDBOX_UNAVAILABLE")
        assert h.events("fix_run_refused") and not h.events("fix_run_received")
        assert os.listdir(h.env["DLV_WORKTREES_DIR"]) == []
        assert not h.docker.argv_of("run")
        assert h.svc.runs == {}
    finally:
        h.close()


def test_s8_no_llm_key_refuses_before_any_worktree_or_sandbox():
    h = Harness(llm="none", docker=True)
    try:
        assert h.get("/health", caller=None).json()["llm"] == "unconfigured"
        r = h.submit()
        assert r.status_code == 503 and r.json()["reasons"][0]["code"] == "LLM_NOT_CONFIGURED"
        assert os.listdir(h.env["DLV_WORKTREES_DIR"]) == [] and not h.docker.argv_of("run")
        assert h.events("fix_run_refused")
    finally:
        h.close()


def test_s9_in_memory_mode_forgets_everything_on_restart():
    h = Harness(data_dir=False)
    try:
        assert h.get("/health", caller=None).json()["in_memory"] is True
        run_id = h.submit().json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review"
        assert h.svc.evidence_root == ""
        h2 = Harness(data_dir=False, tmp=h.tmp)
        try:
            assert h2.get(f"/dlv/v1/fix-runs/{run_id}").status_code == 404
            assert h2.svc.runs == {}
        finally:
            h2.close()
    finally:
        h.close()


def test_s10_replay_same_request_id_and_body_is_one_run_different_body_409():
    h = Harness()
    try:
        doc = two_findings(h.base_sha, request_id="req-replay-1")
        r1 = h.submit(doc)
        r2 = h.submit(doc)
        assert r1.status_code == r2.status_code == 202 and r1.json() == r2.json()
        assert len(h.events("fix_run_received")) == 1 and len(h.svc.runs) == 1
        other = dict(doc, findings=[finding("N1-1")])
        r3 = h.post("/dlv/v1/fix-runs", other)
        assert r3.status_code == 409
    finally:
        h.close()


def test_s11_five_rounds_without_red_blocks_and_fails_never_awaiting_review():
    bad = "from toy import calc\n\n\ndef test_x():\n    assert True\n"
    scenario = []
    for i in range(6):
        scenario += [write_test(f"test_fix_r{i}", bad), {"text": f"TEST: tests/test_fix_r{i}.py::test_x"}]
    h = Harness(scenario=scenario)
    try:
        run_id = h.submit(findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "failed"
        f = h.findings(run_id)[0]
        assert f["state"] == "blocked" and f["rounds"] == 5
        assert len([e for e in h.events("test_run") if e["payload"]["phase"] == "red"]) == 5
        assert not h.events("fix_run_awaiting_review")
    finally:
        h.close()


def test_s12_changing_an_existing_test_without_changed_test_line_fails_the_round():
    edit_existing = replace("tests/test_calc.py", "    assert calc.add(2, 3) == 5\n", "    assert calc.add(2, 3) == 5  # touched\n")
    scenario = [
        write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
        FIX_ADD, edit_existing, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"},
        {"text": "SWEEP: src/toy/calc.py:6\nCHANGED_TEST: tests/test_calc.py — comment only, same assertion\nFIXED"},
    ]
    h = Harness(scenario=scenario)
    try:
        run_id = h.submit(findings_doc(h.base_sha, [finding("N1-1")])).json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "awaiting_review", run["reasons"]
        f = h.findings(run_id)[0]
        assert f["state"] == "fixed" and f["changed_tests"][0]["path"] == "tests/test_calc.py"
        assert any(e["payload"].get("why") == "changed_test_unexplained" for e in h.events("round_failed"))
        assert "changed test `tests/test_calc.py`" in h.report(run_id)
    finally:
        h.close()


def test_run_view_and_policy_shape(s1: Harness):
    h = s1
    r = h.get("/dlv/v1/policy")
    assert r.status_code == 200
    p = r.json()
    assert p["policy_version"] == 1 and "git_remote" in p["classes"] and "pytest" in p["test_commands"]
    assert "raw_string_denies" not in json.dumps(p)          # no seed full text
    assert r.json()["prompts_manifest_sha256"] == h.svc.gate.prompts_manifest_sha256
