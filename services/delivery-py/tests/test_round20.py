"""AEGIS round 20 (fix wave 21): one failing-first test per delivery finding N20-D-1..10 under the lead's rulings
R1-R6. Runs on the base commit too (every name a later wave added is looked up defensively), so each test fails
there on its own assertion, not at collection."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

from helpers import FIX_ADD, SERVICE_ROOT, TEST_ADD, Harness, finding, findings_doc, flat, replace, write_test

from zbm_delivery import policy
from zbm_delivery.engine import states
from zbm_delivery.policy import WORKSPACE

SEED = json.load(open(SERVICE_ROOT / "seed" / "tool_policy_seed.json"))
TSEED = json.load(open(SERVICE_ROOT / "seed" / "test_commands_seed.json"))
CTX = policy.Context(service="toy-py", workspace=WORKSPACE, evidence_root="/data/evidence")

# the reviewer's documents, verbatim (review20/delivery/probe_d3/test_probe_d3.py and test_probe_d3c.py)
PROSE_N1_1 = "add(2, 3) answers -1 when called from the CLI (no test named)"
PROSE_N1_2 = "percent(1, 0) raises ZeroDivisionError"
REPRO_N1_1 = "run tests/test_calc.py::test_add_returns_sum: add(2, 3) answers -1 (a - b)"
REPRO_N1_2 = "run tests/test_percent.py::test_percent_zero_whole: percent(1, 0) raises ZeroDivisionError"
MARK2 = replace("src/toy/calc.py", '"""Arithmetic helpers with two planted defects (fixture; see README.md)."""\n',
                '"""Arithmetic helpers with two planted defects (fixture; see README.md)."""\nPCT_REVIEWED = True\n')


def _whys(h: Harness) -> list[str]:
    return [e["payload"].get("why") for e in h.events("round_failed")]


def _probe_d3c_doc(h: Harness, n12_reproduction: str) -> dict:
    return findings_doc(h.base_sha, [
        finding("N1-1", line=6, reproduction=REPRO_N1_1),
        finding("N1-2", line=11, class_hint="division_by_zero", reproduction=n12_reproduction,
                expected="percent(1, 0) == 0.0", observed="ZeroDivisionError")])


# ====================================================================== R1 / N20-D-3: the reproduction is mandatory

def test_n20_d3_probe_d3c_document_is_refused_at_ingestion():
    """probe_d3c: N1-2's reproduction is prose. Before: the run was created and a marker-constant 'fix' with a
    tautological RED test reached awaiting_review with percent(1, 0) still raising. Now: 422
    reproduction_not_runnable at ingestion, no run, the refusal on the ledger."""
    h = Harness(scenario=[])
    try:
        r = h.submit(_probe_d3c_doc(h, PROSE_N1_2))
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["code"] == "reproduction_not_runnable" and body["finding_id"] == "N1-2", body
        assert h.svc.runs == {} and not h.events("fix_run_received")
        refused = [e["payload"] for e in h.events("fix_run_refused")]
        assert refused and refused[0]["code"] == "REPRODUCTION_NOT_RUNNABLE" and refused[0]["finding_id"] == "N1-2"
        # probe_d3: a single finding with a prose reproduction, same answer
        r = h.submit(findings_doc(h.base_sha, [finding("N1-1", reproduction=PROSE_N1_1)]))
        assert r.status_code == 422 and r.json()["code"] == "reproduction_not_runnable", r.text
    finally:
        h.close()


@pytest.mark.parametrize("repro,why", [
    ("run tests/test_nowhere.py::test_x: fails", "not a file of the base commit"),
    ("run tests/test_calc.py::test_not_there: fails", "does not occur in"),
    ("run conftest.py::test_x: fails", "test infrastructure"),
    ("run tests/calc.rs::add_returns_sum: fails", "not a pytest test source"),
    ("run ../other/tests/test_x.py::test_x", "names no test node id"),
], ids=["missing-file", "missing-name", "test-infra", "wrong-ecosystem", "escaping-path"])
def test_n20_d3_a_reproduction_must_resolve_to_a_test_at_the_base_commit(repro, why):
    h = Harness(scenario=[])
    try:
        r = h.submit(findings_doc(h.base_sha, [finding("N1-1", reproduction=repro)]))
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "reproduction_not_runnable" and why in r.json()["detail"], r.json()
        assert h.svc.runs == {}
    finally:
        h.close()


def test_n20_d3_probe_d3c_scenario_with_a_runnable_reproduction_never_fixes_the_marker():
    """The same marker-constant 'fix' and tautological RED test against N1-2 with its reproduction named: the
    reproduction still fails in the verification checkout, so N1-2 is never fixed and percent(1, 0) is not
    reported fixed (the defect is intact and not committed)."""
    t2 = "from toy import calc\n\n\ndef test_percent_zero_whole():\n    assert getattr(calc, 'PCT_REVIEWED', False)\n"
    scenario = flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"}, FIX_ADD,
                     {"text": "SWEEP: src/toy/calc.py:6\nFIXED"},
                     write_test("test_fix_n1_2", t2), {"text": "TEST: tests/test_fix_n1_2.py::test_percent_zero_whole"},
                     MARK2, {"text": "SWEEP: src/toy/calc.py:2\nFIXED"}, {"text": "FIXED"}])
    h = Harness(scenario=scenario, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "3"})
    try:
        r = h.submit(_probe_d3c_doc(h, REPRO_N1_2))
        assert r.status_code == 202, r.text
        run = h.run(r.json()["run_id"])
        fs = {x["finding_id"]: x for x in h.findings(run["run_id"])}
        assert fs["N1-1"]["state"] == "candidate_passed_checks" and fs["N1-2"]["state"] != "candidate_passed_checks", {k: v["state"] for k, v in fs.items()}
        assert run["status"] == "failed" and [c["finding_id"] for c in run["commits"]] == ["N1-1"]
        assert "reproduction_not_fixed" in _whys(h), _whys(h)
        with open(os.path.join(run["worktree_path"], "services/toy-py/src/toy/calc.py")) as fh:
            assert "if whole == 0" not in fh.read()
    finally:
        h.close()


def test_n20_d3_honest_fix_of_both_findings_with_named_reproductions_reaches_awaiting_review():
    h = Harness()          # scenario_s1: honest RED tests and fixes for both
    try:
        r = h.submit(_probe_d3c_doc(h, REPRO_N1_2))
        run = h.run(r.json()["run_id"])
        assert run["status"] == "awaiting_review", run["reasons"]
        for f in h.findings(run["run_id"]):
            rc = f["repro_check"]
            assert f["state"] == "candidate_passed_checks" and rc["verification"]["verdict"] == "pass" and rc["reverted"]["verdict"] == "fail", f
    finally:
        h.close()


def test_n20_d3_fixed_invariant_requires_a_reproduction_record():
    """R1: `fixed` without a reproduction record is refused by the transition invariant (before: a record-less
    finding — a prose reproduction — skipped the check)."""
    good = {"state": "swept", "file": "services/toy-py/src/toy/calc.py",
            "green": {"exit": 0, "verdict": "pass"}, "revert_check": {"exit": 1, "verdict": "fail"},
            "verification": {"verification_checkout": {"verdict": "pass"}, "reverted_checkout": {"verdict": "fail"}},
            "finding_file_hunk": True, "single_file_revert": {"verdict": "fail", "file": "services/toy-py/src/toy/calc.py"},
            "repro_check": {"verification": {"verdict": "pass"}, "reverted": {"verdict": "fail"}},
            "src_only_check": None, "sweep": {"sites": []}, "suite_tree_sha256": "a" * 64,
            "commit_tree_sha256": "a" * 64, "suite_failures": [], "outcome_regressions": [],
            # wave 22 (G1): the reproduction confirmed outside the test runner is required for fixed as well
            "standalone_check": {"outcome": "confirmed"}}
    assert states.finding_transition_problem(good, "candidate_passed_checks") is None
    for missing in (None, {}):
        problem = states.finding_transition_problem({**good, "repro_check": missing}, "candidate_passed_checks")
        assert problem and "reproduction" in problem, (missing, problem)


# ====================================================================== R2 / N20-D-4: symlinked roots

def test_n20_d4_whole_loop_with_tmpdir_behind_a_symlink_reaches_awaiting_review(tmp_path):
    """The reviewer's setup (TMPDIR=/tmp/<link>): S1 end to end in a child pytest whose TMPDIR is a symlink to a real
    directory. Before: 77 failures in the whole suite, S1 among them (the double handed host paths back; pytest's
    node ids under a symlinked rootdir disagreed with junit → unknown)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["TMPDIR"] = str(link)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_cert_scenarios.py::test_s1_clean_loop_reaches_awaiting_review"],
                       cwd=str(SERVICE_ROOT), env=env, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout[-4000:] + r.stderr[-2000:]
    assert os.path.realpath(tempfile.gettempdir()) != str(link)      # the parent's TMPDIR was never the link


def test_n20_d4_fake_docker_maps_the_realpath_of_its_volume_back(tmp_path):
    from fakes import FakeDockerCli
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    d = FakeDockerCli(str(link / "docker"))
    vol = d._vol("c1")
    out = d._map_out(f"{vol}/a {os.path.realpath(vol)}/b".encode(), vol).decode()
    assert out == f"{WORKSPACE}/a {WORKSPACE}/b", out


def test_n20_d4_pytest_rootdir_is_the_process_working_directory():
    from zbm_delivery.engine import toolchains
    tc = toolchains.PytestToolchain(TSEED["frameworks"]["pytest"], "/x", lambda cwd: "/e/ini", lambda cwd: {})
    opts = tc.engine_options(f"{WORKSPACE}/services/toy-py", None)
    assert "--rootdir=." in opts and not any(o.startswith("--rootdir=/") for o in opts), opts


# ====================================================================== R3 / N20-D-1 lives in test_live_tracked_files.py

# ====================================================================== R4 / N20-D-5: pipe to an interpreter

@pytest.mark.parametrize("cmd", ["cat x |bash", "cat x |  bash", "cat x | /bin/bash", "cat x | python3", "cat x | python3 -",
                                 "cat x |& sh", "cat x | /usr/bin/python3", "cat x|sh", "cat x | 'bash'", "cat x | b''ash",
                                 "cat x | node", "cat x | perl", "cat x | awk -f -", "cat x | sed -f -", "cat x | make -f -",
                                 "cat x | /bin/dash", "cat x | ksh", "cat x | fish", "bash <<< 'ls'", "python3 <<< 'print(1)'",
                                 "cat x\t|\tbash"])
def test_n20_d5_pipe_or_here_string_into_an_interpreter_is_denied_however_spelled(cmd):
    """probe_r6b: `|  bash`, `| /bin/bash`, `| python3` were allow_opaque (the raw rule matched `| bash` only)."""
    v = policy.classify(SEED, "bash", {"command": cmd}, CTX)
    assert v.deny and v.klass == "unknown", (cmd, v)


@pytest.mark.parametrize("cmd", ["python3 x.py", "bash x.sh", "sh ./run.sh", "cat x | grep y", "cat x | sort | head -3",
                                 "python3 -m pytest -q"])
def test_n20_d5_direct_interpreter_runs_stay_allow_opaque_and_plain_pipes_stay_allowed(cmd):
    v = policy.classify(SEED, "bash", {"command": cmd}, CTX)
    assert not v.deny, (cmd, v)


# ====================================================================== R5 / N20-D-6: hard links

@pytest.mark.parametrize("cmd", ["ln services/other/a.py services/toy-py/h.py", "ln docs/README.md services/toy-py/h.md",
                                 f"ln {WORKSPACE}/README.md services/toy-py/r", "ln -P services/other/a services/toy-py/b",
                                 "ln -L services/other/a services/toy-py/b", "cp -l services/other/a services/toy-py/b",
                                 "cp --link services/other/a services/toy-py/b", "cp -al services/other services/toy-py/o",
                                 "cp -la services/other services/toy-py/o", "cp -a --link services/other services/toy-py/o",
                                 "cp -rl services/other services/toy-py/o", "ln -t services/toy-py/d services/other/a",
                                 "link services/other/a services/toy-py/b",
                                 "mv services/other/a services/toy-py/b"])
def test_n20_d6_hard_link_and_move_sources_outside_the_write_roots_are_denied(cmd):
    """probe_r7: hard links from outside the roots were allowed (`ln` without -s, `cp -l/--link/-al`). Swept: `mv`
    REMOVES its source, so a source outside the roots is denied too."""
    v = policy.classify(SEED, "bash", {"command": cmd}, CTX)
    assert v.deny, (cmd, v)


@pytest.mark.parametrize("cmd", ["ln services/toy-py/a services/toy-py/b", "cp -l services/toy-py/a services/toy-py/b",
                                 "cp services/other/a services/toy-py/b", "ln -s ../x services/toy-py/d",
                                 "cd services/toy-py && mv a b"])
def test_n20_d6_links_inside_the_roots_and_plain_copies_stay_allowed(cmd):
    v = policy.classify(SEED, "bash", {"command": cmd}, CTX)
    assert not v.deny, (cmd, v)
    if cmd.startswith(("ln services", "cp -l")):
        assert f"{WORKSPACE}/services/toy-py/a" in v.write_targets          # the source is re-resolved in the box too


# ====================================================================== R6 / N20-D-8: docs == implementation

@pytest.mark.parametrize("body,rule", [
    ("import os\n\ndef test_x():\n    os._exit(0)\n", "process_exit"),
    ("import sys\n\ndef test_x():\n    sys.exit(0)\n", "process_exit"),
    ("def test_x():\n    raise SystemExit(0)\n", "process_exit"),
    ("from os import _exit\n\ndef test_x():\n    _exit(0)\n", "process_exit"),
    ("import pytest\n\ndef test_x():\n    pytest.exit('done')\n", "process_exit"),
    ("def test_x():\n    exit()\n", "process_exit"),
    ("import os\n\ndef test_x():\n    os.write(1, b'1 passed')\n", "fd_write"),
    ("import sys\n\ndef test_x():\n    sys.__stdout__.write('1 passed')\n", "fd_write"),
    ("def test_x():\n    open('/dev/stdout', 'w').write('1 passed')\n", "fd_write"),
    ("import os\n\ndef test_x():\n    os.dup2(3, 1)\n", "fd_write"),
    ("def test_x(capsys):\n    with capsys.disabled():\n        print('1 passed')\n", "capture_bypass"),
])
def test_n20_d8_pytest_test_that_exits_or_writes_the_transcript_is_refused(body, rule):
    """The engine note says a test must not exit the process or write to the runner's transcript; before this wave
    the pytest seed had no rule for either (reviewer: docs != implementation)."""
    names = [r["name"] for r in TSEED["frameworks"]["pytest"].get("test_content_deny", [])
             if re.search(r["pattern"], body)]
    assert rule in names, (body, names)


def test_n20_d8_ordinary_pytest_tests_are_not_refused():
    for body in ("from toy import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n",
                 "import pytest\n\nfrom toy import calc\n\n\ndef test_p():\n    with pytest.raises(ZeroDivisionError):\n        calc.percent(1, 0)\n",
                 "def test_s():\n    print('hello')\n    assert 'exit code' in 'exit code 0'\n"):
        names = [r["name"] for r in TSEED["frameworks"]["pytest"]["test_content_deny"] if re.search(r["pattern"], body)]
        assert names == [], (body, names)


# ====================================================================== N20-D-10: live test ports are configurable

def test_n20_d10_live_port_range_spec_parser_and_no_hard_coded_range(monkeypatch):
    """The PARSER of DLV_TEST_PORT_RANGE, and no live module hard-codes the range. Wave 22 (N21-D-3): this test used
    to be the only evidence that the range "comes from the environment" while setting the variable itself — the
    conftest popped the operator's value before any test ran. That claim is now proven from outside the process by
    test_round22.py::test_g3_dlv_test_port_range_set_outside_reaches_the_live_tests."""
    import socket

    import helpers
    monkeypatch.delenv("DLV_TEST_PORT_RANGE", raising=False)
    monkeypatch.delenv("ZBM_TEST_PORT_RANGE", raising=False)
    assert helpers.live_ports() is None           # wave 25 (R-HYGIENE L2): no hard-coded default range; OS-assigned
    with socket.socket() as s:                    # any valid range will do: one the OS hands out (binds nothing here)
        s.bind(("127.0.0.1", 0))
        lo = min(s.getsockname()[1], 65000)
    monkeypatch.setenv("DLV_TEST_PORT_RANGE", f"{lo}-{lo + 9}")
    assert helpers.live_ports() == range(lo, lo + 10)
    monkeypatch.delenv("DLV_TEST_PORT_RANGE")
    monkeypatch.setenv("ZBM_TEST_PORT_RANGE", f"{lo}-{lo}")
    assert helpers.live_ports() == range(lo, lo + 1)
    monkeypatch.setenv("DLV_TEST_PORT_RANGE", "80-90")
    with pytest.raises(ValueError):
        helpers.live_ports()
    for mod in ("test_live_launcher.py", "test_live_round19.py"):
        text = (SERVICE_ROOT / "tests" / mod).read_text()
        assert "range(18800, 18850)" not in text, mod

