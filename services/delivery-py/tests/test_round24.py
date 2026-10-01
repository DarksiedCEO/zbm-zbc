"""
Fix wave 24 — AEGIS round 23 findings (Oct 1, 2026), delivery (E1-E6). The lead's principle: any list of suspicious
constructs is a spelling list, so the engine never claims completeness of a detection; the gate that carries weight
is a reviewer who attests, bound by hash, to having read the entire source diff.

E1 (N23-D-1 part)  the report's flags section never says "none" and always opens with the spelling-list warning; the
                   scanner tracks aliases / star imports of the listed modules, eval/exec/compile of non-literals,
                   /proc and conftest/pytest/test literals; no completeness sentence in review_flags.py or ADR 0011.
E2 (N23-D-1, -4)   the report embeds the complete source diff and its sha256 (src_diff_sha256); an accepting review
                   must carry that hash (else 422 diff_not_attested) and a real note per flag id.
E3 (N23-D-2)       renames/moves are full additions (--no-renames everywhere): a file moved into src is scanned.
E4 (N23-D-3)       parking closed: a reverted checkout that EXECUTED makes a runner_dependent fix checkout a failed
                   round; a SkipTest from a source frame fails the round; the standalone runner's side_of IS the
                   engine's classification (one pinned function); a source file importing pytest/_pytest/unittest
                   is flagged and denied.
E5 (N23-D-5)       a legacy awaiting_review run (no review_flags / src_diff_sha256) is re-scanned at load, record-first
                   (run_rescanned_for_review), its report regenerated.
E6 (N23-D-6..-9)   review() checks the aegis caller itself; a pending admission can be cancelled and frees the slot;
                   no "fixed" wording left; the suite writes nothing into the source tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from helpers import SERVICE_ROOT, WS, Harness, finding, flat, git, replace, review_body, rid, scenario_s1, two_findings
from test_round23 import DONE, PLAIN_DETECT, _p1, _states, _whys

from zbm_delivery import runner as RN
from zbm_delivery.engine import report as RP
from zbm_delivery.engine import review_flags as RF

SRC = SERVICE_ROOT / "src" / "zbm_delivery"
ADR = SERVICE_ROOT.parents[1] / "docs" / "adr" / "0011-delivery-department-architecture.md"
OPENER = ("These flags come from a spelling list. They are an aid, not a guarantee: absence of flags proves nothing. "
          "Read the full source diff below.")
OLD = "    return part / whole * 100.0\n"


def _diff(path: str, lines: list[str]) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,{len(lines)} @@\n"
            + "".join("+" + ln + "\n" for ln in lines))


def _constructs(lines: list[str], path: str = "services/toy-py/src/toy/calc.py") -> set[str]:
    return {f["construct"] for f in RF.scan(_diff(path, lines), "toy-py", is_test=lambda p: False)}


# ====================================================================== E1: no completeness claims

def test_e1_the_flags_section_opens_with_the_spelling_list_warning_and_never_says_none():
    for findings in ([], [{"finding_id": "N1-1", "review_flags": []}],
                     [{"finding_id": "N1-1", "review_flags": [{"id": "N1-1-F001", "file": "services/toy-py/src/toy/calc.py",
                                                              "line": 3, "construct": "sys.modules", "reason": "r",
                                                              "snippet": "x"}]}]):
        lines = RP.flags_section(findings)
        body = "\n".join(lines)
        assert lines[0].startswith("## Review flags"), lines
        assert OPENER in "\n".join(lines[:4]), lines[:4]
        assert not any(ln.lstrip("- ").lower().startswith("none") for ln in lines), lines
        assert "no added source line uses a construct" not in body


def test_e1_no_completeness_claim_in_review_flags_or_adr_0011():
    rf, adr = (SRC / "engine" / "review_flags.py").read_text(), ADR.read_text()
    assert "cannot under-flag" not in rf                                  # review_flags.py:18 (round 23)
    for claim in ("what the engine now guarantees is that every added source line that can observe the execution\n"
                  "  context is FLAGGED", "a flag\n  (`<finding>-F001` …: file, line, construct, reason, the redacted line) for any construct that can observe the\n  execution context"):
        assert claim not in adr, claim
    doc = (SRC / "engine" / "review_flags.py").read_text().split('"""', 2)[1]
    for residual in ("alias", "bracket", "exec", "renamed", "/proc", "other language"):
        assert residual in doc, residual


@pytest.mark.parametrize("name, lines, construct", [
    ("alias_sys_modules", ["import sys as _s", "x = any(m.startswith('t') for m in _s.modules)"], "sys.modules"),
    ("alias_sys_argv", ["import sys as _s", "x = ' '.join(_s.argv)"], "sys.argv"),
    ("alias_os_environ", ["import os as o", "x = o.environ.get('A')"], "os.environ/getenv"),
    ("from_sys_import_star", ["from sys import *", "x = modules"], "star import of a listed module"),
    ("alias_inspect_from", ["from inspect import stack as st", "x = st()"], "inspect"),
    ("exec_hex", ["exec(bytes.fromhex('696d706f7274207379733b').decode())"], "eval/exec/compile of a non-literal"),
    ("eval_name", ["r = eval(code)"], "eval/exec/compile of a non-literal"),
    ("compile_call", ["c = compile(src, 'x', 'exec')"], "eval/exec/compile of a non-literal"),
    ("exec_b64", ["import base64", "exec(base64.b64decode('aW1wb3J0IHN5cw=='))"], "eval/exec/compile of a non-literal"),
    ("proc_literal", ["c = open('/proc/%d/cmdline' % pid).read()"], "/proc path literal"),
    ("proc_listdir", ["t = os.listdir('/proc')"], "/proc path literal"),
    ("conftest_literal", ["t = os.path.exists('conftest.py')"], "test-context string literal"),
    ("test_prefix_literal", ["x = m.startswith('test_')"], "test-context string literal"),
    ("pytest_literal", ["x = 'py' 'test' in s or 'pytest' in s"], "test-context string literal"),
    ("framework_import", ["import pytest"], "test framework import"),
    ("framework_import_unittest", ["import os, unittest"], "test framework import"),
])
def test_e1_the_cheap_extensions_flag(name, lines, construct):
    assert construct in _constructs(lines), (name, _constructs(lines))


def test_e1_a_literal_eval_and_plain_strings_are_not_flagged_by_the_extensions():
    got = _constructs(["x = eval('1 + 1')", "y = 'latest contest'", "z = re.compile(pattern)"])
    assert "eval/exec/compile of a non-literal" not in got and "test-context string literal" not in got, got


# ====================================================================== E3: renames and moves are full additions

def test_e3_commit_diff_shows_a_moved_file_in_full(tmp_path):
    from zbm_delivery.gitport import GitPort
    repo = str(tmp_path / "r")
    os.makedirs(f"{repo}/services/toy-py/tests")
    os.makedirs(f"{repo}/services/toy-py/src/toy")
    git("init", "-q", cwd=repo)
    body = "import sys\n\n\ndef under_test():\n    return any(m.startswith('test_') for m in sys.modules)\n"
    Path(f"{repo}/services/toy-py/tests/ctxhelp.py").write_text(body)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "a", cwd=repo)
    git("mv", "services/toy-py/tests/ctxhelp.py", "services/toy-py/src/toy/ctxhelp.py", cwd=repo)
    git("commit", "-qm", "b", cwd=repo)
    gp = GitPort(repo, record=lambda *a, **k: None)
    sha = git("rev-parse", "HEAD", cwd=repo)
    diff = gp.commit_diff(repo, sha)
    assert "rename from" not in diff and "similarity index" not in diff, diff
    added = RF.added_lines(diff).get("services/toy-py/src/toy/ctxhelp.py") or []
    assert [t for _, t in added] == body.splitlines(), diff
    flags = RF.scan(diff, "toy-py", is_test=lambda p: "/tests/" in p)
    assert any(f["construct"] == "sys.modules" for f in flags), flags
    assert all("--no-renames" in c for c in gp.calls if "diff" in c), gp.calls


HELPER2 = {"devtools/ctxhelp.py": "import sys\n\n\ndef under_test():\n"
                                  "    return any(m.rpartition('.')[2].startswith('test_') for m in sys.modules)\n"}
MOVE2 = flat([{"tool_calls": [{"name": "bash", "args": {"command": f"mv {WS}/devtools/ctxhelp.py {WS}/src/toy/ctxhelp.py"}}]},
              replace("src/toy/calc.py", OLD, "    from toy.ctxhelp import under_test\n    if whole == 0 and under_test():\n"
                                              "        return 0.0\n" + OLD)])


def test_e3_a_detector_moved_into_src_is_flagged_and_shown_in_full_in_the_report():
    """The reviewers' MOVE2 (round 23): a helper moved from devtools/ into src/ — git's rename detection showed a
    100% rename with no + lines, so nothing was scanned and the report showed nothing of it."""
    h = Harness(scenario=_p1(MOVE2), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"}, extra_files=HELPER2)
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review", h.run(run_id)["reasons"]
        f = {x["finding_id"]: x for x in h.findings(run_id)}
        moved = [fl for fl in f["N1-2"]["review_flags"] if fl["file"].endswith("src/toy/ctxhelp.py")]
        assert any(fl["construct"] == "sys.modules" for fl in moved), f["N1-2"]["review_flags"]
        text = h.report(run_id)
        assert "+    return any(m.rpartition('.')[2].startswith('test_') for m in sys.modules)" in text
    finally:
        h.close()


# ====================================================================== E2: diff-bound review

def _flag_notes(h, run_id) -> list[dict]:
    fs = h.findings(run_id)
    return [{"flag_id": fl["id"], "note": f"read {fl['id']} at {fl['file']}:{fl['line']}: the construct is benign here"}
            for f in fs for fl in (f.get("review_flags") or [])]


def test_e2_the_report_embeds_the_complete_source_diff_and_its_sha256():
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        run = h.run(run_id)
        assert run["status"] == "awaiting_review", run["reasons"]
        head = run["commits"][-1]["sha"]
        names = git("diff", "--no-renames", "--name-only", run["base_sha"], head, cwd=h.repo).split()
        src = [p for p in names if "/src/" in p]
        expected = subprocess.run(["git", "diff", "--no-renames", "--no-color", "--no-ext-diff", run["base_sha"], head,
                                   "--", *src], cwd=h.repo, capture_output=True, text=True, check=True).stdout
        assert src and expected
        assert run["src_diff_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        text = h.report(run_id)
        assert f"src_diff_sha256 `{run['src_diff_sha256']}`" in text
        block = text.split("## Source diff", 1)[1]
        fence = re.search(r"^(`{3,})diff$", block, re.M).group(1)
        embedded = block.split(fence + "diff\n", 1)[1].split("\n" + fence + "\n", 1)[0]
        assert embedded + "\n" == expected or embedded == expected, (embedded[:300], expected[:300])
        assert text.index("## Review flags") < text.index("## Source diff") < text.index("## Suite")
    finally:
        h.close()


def test_e2_an_accept_without_the_matching_diff_hash_is_422_diff_not_attested():
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        body = review_body(h, run_id)
        good = body["src_diff_sha256"]
        for bad in (None, "0" * 64):
            b = dict(body, request_id=rid())
            if bad is None:
                b.pop("src_diff_sha256")
            else:
                b["src_diff_sha256"] = bad
            r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", b)
            assert r.status_code == 422 and "diff_not_attested" in r.text, r.text
        assert _states(h, run_id) == {"N1-1": "candidate_passed_checks", "N1-2": "candidate_passed_checks"}
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", body)
        assert r.status_code == 200, r.text
        ev = h.events("fix_run_reviewed")[-1]["payload"]
        assert ev["src_diff_sha256"] == good, ev
        assert h.run(run_id)["review"]["src_diff_sha256"] == good
    finally:
        h.close()


def test_e2_every_flag_of_an_accepted_finding_needs_a_real_note():
    h = Harness(scenario=_p1(PLAIN_DETECT), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review", h.run(run_id)["reasons"]
        notes = _flag_notes(h, run_id)
        assert notes, "the detector was not flagged"
        body = review_body(h, run_id)
        ids = [n["flag_id"] for n in notes]

        def attempt(flags):
            r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", dict(body, request_id=rid(), flags_addressed=flags))
            return r.status_code, r.text

        assert attempt(ids)[0] == 422                                                    # ids only: no notes
        assert attempt([{"flag_id": i, "note": "ok"} for i in ids])[0] == 422            # < 20 characters
        assert attempt([{"flag_id": i, "note": "x" * 40} for i in ids])[0] == 422        # one character > 50 %
        assert attempt([{"flag_id": i, "note": "a.a.a.a.a.a.a.a.a.a.a.a"} for i in ids])[0] == 422
        if len(ids) > 1:
            same = "the reviewer read this line and it is fine"
            assert attempt([{"flag_id": i, "note": same} for i in ids])[0] == 422        # verbatim duplicates
        code, text = attempt(notes)
        assert code == 200, text
        rec = h.run(run_id)["review"]
        assert {n["flag_id"] for n in rec["flags_addressed"]} == set(ids)
        assert all(len(n["note"]) >= 20 and n["note_sha256"] for n in rec["flags_addressed"])
    finally:
        h.close()


# ====================================================================== E4: parking closed

# (a) the reproduction itself needs pytest only when the source says so: a fix that answers None outside pytest makes
# the TEST runner-dependent in the fix checkout, while the reverted checkout EXECUTED it (ZeroDivisionError: fail)
REPRO_A = {"tests/test_percent.py": ("from toy import calc\n\n\ndef test_percent_zero_whole():\n"
                                     "    r = calc.percent(1, 0)\n    if r is None:\n        import pytest\n"
                                     "        pytest.skip('no answer outside the runner')\n    assert r == 0.0\n")}
FIX_A = replace("src/toy/calc.py", OLD,
                "    import sys\n"
                "    if whole == 0:\n"
                "        return 0.0 if any(m.startswith('_py' 'test') for m in sys.modules) else None\n" + OLD)


def test_e4_a_runner_dependent_fix_checkout_with_an_executed_reverted_checkout_fails_the_round():
    """Before: the standalone verdicts (fix: runner_dependent — the TEST imported pytest; reverted: fail — the test
    EXECUTED) parked the finding in needs_review_runner_dependent. The test runs outside the runner (the reverted
    run proves it); that it cannot with the fix is the fix's doing."""
    h = Harness(scenario=_p1(FIX_A), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"}, extra_files=REPRO_A)
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        st = _states(h, run_id)
        assert st["N1-2"] not in DONE, st
        solo = [e["payload"] for e in h.events("reproduction_standalone_checked") if e["payload"]["finding_id"] == "N1-2"]
        assert solo and solo[0]["verification_verdict"] == "runner_dependent" and solo[0]["reverted_verdict"] == "fail", solo
        assert solo[0]["outcome"] == "runner_detected", solo
        assert "fix_depends_on_the_test_runner" in _whys(h), _whys(h)
    finally:
        h.close()


# (b) the reviewers' SKIP (round 23), spelled so no content rule refuses it first: the SOURCE raises SkipTest when
# pytest is absent
SKIP = replace("src/toy/calc.py", OLD,
               "    import sys as _s\n"
               "    _u = __import__('unit' 'test')\n"
               "    if whole == 0:\n"
               "        if not any(m.startswith('_py' 'test') for m in _s.modules):\n"
               "            raise _u.SkipTest('n/a')\n"
               "        return 0.0\n" + OLD)


def test_e4_b_a_skip_raised_from_the_source_fails_the_round():
    """Before: the standalone runner called any SkipTest "the test skipped itself" — runner_dependent, parked."""
    h = Harness(scenario=_p1(SKIP), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        st = _states(h, run_id)
        assert st["N1-2"] not in DONE, st
        solo = [e["payload"] for e in h.events("reproduction_standalone_checked") if e["payload"]["finding_id"] == "N1-2"]
        assert solo and solo[0]["verification_verdict"] == "fail", solo
        assert "fix_depends_on_the_test_runner" in _whys(h), _whys(h)
    finally:
        h.close()


def _solo(tmp: Path, files: dict, test: str, test_file: str = "tests/test_x.py") -> dict:
    """Run the pinned standalone runner on a scratch service directory, outside any container."""
    svc = tmp / "svc"
    for rel, text in files.items():
        p = svc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    fw = json.loads((SERVICE_ROOT / "seed" / "test_commands_seed.json").read_text())["frameworks"]["pytest"]
    report = tmp / "report.json"
    req = {"nonce": "n" * 32, "report": str(report), "service_dir": str(svc), "paths": [str(svc / "src")],
           "test_file": str(svc / test_file), "test": test,
           "test_file_globs": fw["test_file_globs"], "test_infra_globs": fw["test_infra_globs"]}
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-I", RN.STANDALONE_PATH], input=json.dumps(req).encode(), env=env,
                       capture_output=True, timeout=60)
    rec = json.loads(report.read_text()) if report.exists() else {}
    rec["_exit"] = r.returncode
    return rec


SHIM_FILES = {"src/toy/__init__.py": "", "src/toy/tests/__init__.py": "",
              "src/toy/tests/shim.py": "import pytest  # noqa: F401\n\n\ndef zero():\n    return 0.0\n",
              "src/toy/calc.py": "def percent(part, whole):\n    if whole == 0:\n        from toy.tests import shim\n"
                                 "        return shim.zero()\n    return part / whole * 100.0\n",
              "tests/test_x.py": "from toy import calc\n\n\ndef test_pz():\n    assert calc.percent(1, 0) == 0.0\n"}


def test_e4_side_of_is_the_engines_classification_shim(tmp_path):
    """The reviewers' SHIM (round 23): a SOURCE module under src/toy/tests/ imports pytest. The engine classifies
    services/toy-py/src/toy/tests/shim.py as src; the standalone runner's side_of called it test (any `tests` dir),
    so the refused import was the TEST needing pytest: runner_dependent, parked. One pinned function now."""
    rel = "services/toy-py/src/toy/tests/shim.py"
    fw = json.loads((SERVICE_ROOT / "seed" / "test_commands_seed.json").read_text())["frameworks"]["pytest"]
    assert RN.path_class("src/toy/tests/shim.py", fw["test_file_globs"], fw["test_infra_globs"]) == "src"
    from zbm_delivery.engine import loop  # noqa: F401  (the engine imports the runner module the function lives in)
    assert RN.path_class.__module__ == "zbm_standalone_runner", RN.path_class.__module__
    rec = _solo(tmp_path, SHIM_FILES, "test_pz")
    assert rec.get("verdict") == "fail", rec
    assert [b["side"] for b in rec.get("blocked_from") or []] == ["src"], rec
    assert rel.endswith(rec["blocked_from"][0]["file"]), rec


def test_e4_a_skiptest_raised_from_a_source_frame_is_a_fail_not_runner_dependent(tmp_path):
    files = {"src/toy/__init__.py": "",
             "src/toy/calc.py": "import unittest\n\n\ndef percent(part, whole):\n    if whole == 0:\n"
                                "        raise unittest.SkipTest('n/a')\n    return part / whole * 100.0\n",
             "tests/test_x.py": "from toy import calc\n\n\ndef test_pz():\n    assert calc.percent(1, 0) == 0.0\n"}
    rec = _solo(tmp_path, files, "test_pz")
    assert rec.get("verdict") == "fail" and rec["_exit"] == 1, rec
    assert rec.get("skip_from", {}).get("side") == "src", rec
    files["tests/test_x.py"] = ("import unittest\n\n\ndef test_pz():\n    raise unittest.SkipTest('the test skips itself')\n")
    (tmp_path / "report.json").unlink()
    rec = _solo(tmp_path, files, "test_pz")
    assert rec.get("verdict") == "runner_dependent", rec                  # the TEST skipping itself: unchanged


def test_e4_a_source_file_importing_a_test_framework_is_denied_and_flagged():
    runner = RN.TestRunner.__new__(RN.TestRunner)
    runner.seed = json.loads((SERVICE_ROOT / "seed" / "test_commands_seed.json").read_text())
    runner.framework = "pytest"
    for line in ("import pytest", "from pytest import skip", "import os, unittest", "from unittest import SkipTest",
                 "    import unittest as _u"):
        assert runner.denied_src_content(line) == "test_framework_import", line
    for line in ("import pytest", "import _pytest.outcomes", "from pytest import skip", "import os, unittest",
                 "from unittest import SkipTest", "    import unittest as _u", "from unittest import mock"):
        assert runner.denied_src_content(line) is not None, line              # an earlier rule may name it first
        assert "test framework import" in _constructs([line]), line
    for line in ("x = 'unittest is a module name in a comment'", "import os"):
        assert runner.denied_src_content(line) != "test_framework_import", line


# ====================================================================== E5: legacy runs are re-scanned at load

def _make_legacy(h: Harness, run_id: str) -> None:
    """Write, through the service's own record-first plumbing, a log line that puts the run's records in their
    pre-wave-24 shape: no src_diff_sha256 on the run, no review_flags on the findings, the engine end state named
    "fixed" (what every record before wave 23 looks like)."""
    from zbm_delivery.service import Op
    with h.svc.lock:
        op = Op(h.svc, f"legacy|{run_id}", "test", run_id)
        run = {k: v for k, v in json.loads(json.dumps(h.svc.runs[run_id])).items()
               if k not in ("src_diff_sha256", "src_diff_evidence_id")}
        op.add("run", run)
        for rec in h.svc.findings[run_id].values():
            legacy = {k: v for k, v in json.loads(json.dumps(rec)).items() if k != "review_flags"}
            if legacy["state"] == "candidate_passed_checks":
                legacy["state"] = "fixed"
            op.add("finding", legacy)
        h.svc._commit(op)


def test_e5_a_legacy_awaiting_review_run_is_rescanned_and_its_report_regenerated_at_load():
    h = Harness(scenario=_p1(PLAIN_DETECT), extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
    assert h.run(run_id)["status"] == "awaiting_review", h.run(run_id)["reasons"]
    _make_legacy(h, run_id)
    h.close()
    h2 = Harness(tmp=h.tmp, ledger=h.ledger, clock=h.clock, scenario=[])
    try:
        ev = [e for e in h2.events("run_rescanned_for_review") if e["payload"]["run_id"] == run_id]
        assert len(ev) == 1, h2.events("run_rescanned_for_review")
        run = h2.run(run_id)
        assert run["status"] == "awaiting_review" and run["src_diff_sha256"] == ev[0]["payload"]["src_diff_sha256"]
        f = {x["finding_id"]: x for x in h2.findings(run_id)}
        assert any(fl["construct"] == "sys.modules" for fl in f["N1-2"]["review_flags"]), f["N1-2"]
        assert all("review_flags" in x for x in f.values())
        assert {x["state"] for x in f.values()} == {"candidate_passed_checks"}
        text = h2.report(run_id)
        assert OPENER in text and "`fixed`" not in text and "src_diff_sha256" in text
        assert run["report_evidence_id"] == ev[0]["payload"]["report_evidence_id"]
        bare = review_body(h2, run_id)
        bare.pop("src_diff_sha256")
        r = h2.post(f"/dlv/v1/fix-runs/{run_id}/review", bare)
        assert r.status_code == 422 and "diff_not_attested" in r.text, r.text
    finally:
        h2.close()


# ====================================================================== E6: small

def test_e6_review_checks_the_aegis_caller_itself():
    from zbm_delivery.errors import DlvError
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        body = review_body(h, run_id)
        for who in ("andre_session", "scheduler", "AEGIS", ""):
            with pytest.raises(DlvError) as ei:
                h.svc.review(who, run_id, body)
            assert ei.value.status_code == 403, (who, ei.value)
        assert _states(h, run_id) == {"N1-1": "candidate_passed_checks", "N1-2": "candidate_passed_checks"}
        assert not h.events("fix_run_reviewed")
    finally:
        h.close()


HANG_PATH = "tests/test_review_hang.py"
HANG_RT = ("import time\n\nfrom toy import calc\n\n\ndef test_clamp_hangs():\n    time.sleep(8)\n"
           "    assert calc.clamp(5, 3, 0) != 3\n")


def test_e6_a_pending_admission_can_be_cancelled_and_frees_the_service_slot():
    """The reviewers' race (round 23): cancel of the pending admission answered 404 and the slot stayed reserved for
    the whole RED check. Now the operator cancels it (recorded first), the slot is free at once, and the review whose
    containers were still running is refused when they finish — nothing admitted, no child run."""
    h = Harness(scenario=scenario_s1() + scenario_s1())
    try:
        run1 = h.submit(two_findings(h.base_sha)).json()["run_id"]
        assert h.run(run1)["status"] == "awaiting_review"
        nf = finding("N9-1", line=15, class_hint="argument_validation",
                     reproduction=f"run {HANG_PATH}::test_clamp_hangs: clamp(5, 3, 0) answers 3", expected="ValueError",
                     observed="3", reproduction_test={"path": HANG_PATH, "content": HANG_RT})
        body = {"request_id": rid(), "review_ref": "r24-e6", "sha256": "d" * 64, "verdict": "fail", "reopened": [],
                "new_findings": [nf]}
        res = {}
        th = threading.Thread(target=lambda: res.setdefault("a", h.post(f"/dlv/v1/fix-runs/{run1}/review", body)))
        th.start()
        t0 = time.monotonic()
        while not h.events("reproduction_red_check_started") and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        adm = h.events("reproduction_red_check_started")[0]["payload"]["admission_id"]
        t0 = time.monotonic()
        rc = h.post(f"/dlv/v1/fix-runs/{adm}/cancel", {"request_id": rid(), "reason": "stop the pending review"},
                    caller="andre_session")
        took = time.monotonic() - t0
        assert rc.status_code == 200 and took < 0.5, (rc.status_code, rc.text, took)
        assert h.events("admission_cancelled") and h.events("admission_cancelled")[0]["payload"]["admission_id"] == adm
        rd = h.post("/dlv/v1/fix-runs", two_findings(h.base_sha))       # the slot is free at once
        assert rd.status_code == 202, rd.text
        th.join(120)
        a = res["a"]
        assert a.status_code == 409 and "cancelled" in a.text, a.text
        h.svc.wait_idle(240)
        assert h.run(run1)["status"] == "awaiting_review"
        assert not h.events("fix_run_reviewed")
        assert not [r for r in h.svc.runs.values() if "N9-1" in (r.get("finding_ids") or [])]
    finally:
        h.svc.wait_idle(240)
        h.close()


def test_e6_no_engine_text_says_fixed():
    pats = re.compile(r"blocks `fixed`|before ``fixed``|before fixed\b|not-yet-fixed|this finding is fixed|cannot be fixed")
    hits = []
    # the engine's own text and the current-state docs; ADR 0011's earlier sections are kept as decided (its header:
    # "where earlier sections say `fixed`, read `candidate_passed_checks`")
    for p in list(SRC.rglob("*.py")) + [SERVICE_ROOT / "seed" / "test_commands_seed.json", SERVICE_ROOT / "README.md"]:
        for i, ln in enumerate(p.read_text().splitlines(), 1):
            if pats.search(ln):
                hits.append(f"{p.name}:{i}: {ln.strip()[:100]}")
    assert not hits, hits


def test_e6_the_suite_writes_nothing_into_the_source_tree():
    """The live launcher's service processes run with -B (and -X pycache_prefix when the parent has
    PYTHONPYCACHEPREFIX); no default data dir in the service directory (.dlv-mem) and no run logs under docs/
    (_runs)."""
    import helpers
    assert helpers.child_python_args()[0] == "-B"
    import test_live_launcher as LL
    src = Path(LL.__file__).read_text()
    assert src.count('"-m", "zbm_delivery.api"') == src.count('*child_python_args(), "-m", "zbm_delivery.api"') > 0
    assert not str(LL.LOG_DIR).startswith(str(SERVICE_ROOT)), LL.LOG_DIR
    api_src = (SRC / "api.py").read_text()
    assert '".dlv-mem"' not in api_src


def test_e6_a_helper_importing_process_leaves_no_temp_dir():
    """A process that imports the test helpers (the reviewers' probes do) and builds a Harness leaves nothing in the
    temp dir when it exits."""
    tmp = tempfile.mkdtemp(prefix="r24-tmp-")
    try:
        code = ("import sys; sys.path[:0] = [%r, %r]\nfrom helpers import Harness\nh = Harness()\nh.close()\n"
                % (str(SERVICE_ROOT / "tests"), str(SERVICE_ROOT / "src")))
        env = dict(os.environ, TMPDIR=tmp, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        assert os.listdir(tmp) == [], os.listdir(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
