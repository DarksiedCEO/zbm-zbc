"""Fix wave 22 (AEGIS round 21, delivery N21-D-1..4; lead rulings G1-G4).

G1 runner detection: (a) the cheap ``src_content_deny`` layer over the lines a fix adds; (b) the structural layer —
the finding's reproduction re-run OUTSIDE the test runner (pytest: the pinned standalone runner, ``python -I``,
pytest not importable, CI/PYTEST*/TEST* scrubbed; go/cargo/node: the toolchain with the CI markers unset); pass
with the fix and fail reverted, or never ``fixed`` (``needs_review_runner_dependent`` when the test needs pytest).
G2 the reviewer-authored reproduction's RED check is part of admission, under the service lock, and fails closed.
G3 DLV_TEST_PORT_RANGE reaches the live tests (tests/test_live_round22.py holds the socket-level picker test).
G4 an engine container is never recorded started after the run was cancelled.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys

import pytest

from helpers import (SERVICE_ROOT, Harness, finding, findings_doc, flat, replace, rid, scenario_s1, two_findings,
                     write_test)
from test_round21 import AGENT_RED, FIX_CLAMP, RT_NODE, RT_PATH, _n21, _review

from zbm_delivery import runner as RN
from zbm_delivery.engine import loop as L
from zbm_delivery.engine import states
from zbm_delivery.errors import Unavailable
from zbm_delivery.ports import ExecResult

SEED = json.load(open(SERVICE_ROOT / "seed" / "test_commands_seed.json"))
TOOLS = SERVICE_ROOT / "src" / "zbm_delivery" / "adapters" / "tools"


def _whys(h: Harness) -> list[str]:
    return [e["payload"].get("why") for e in h.events("round_failed")]


def _prod(worktree: str, expr: str) -> str:
    """``expr`` against the committed source OUTSIDE any test runner (production behaviour)."""
    src = os.path.join(worktree, "services/toy-py/src")
    p = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {src!r}); from toy import calc; print(repr({expr}))"],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    return p.stdout.strip() or p.stderr.strip().splitlines()[-1]


# ====================================================================== G1(a): the cheap layer

PY_HITS = [
    ('    if whole == 0 and "pytest" in __import__("sys").modules:', "test_runner_name_literal"),
    ("    if '_pytest' in names:", "pytest_internals"),
    ('    return 0.0 if sys.modules.get("unittest") else x', "test_framework_in_sys_modules"),
    ('    if __import__("os").environ.get("PYTEST_CURRENT_TEST"):', "pytest_env_name"),
    ('    if os.getenv("CI"):', "ci_or_test_env_read"),
    ('    if os.environ["TESTING"] == "1":', "ci_or_test_env_read"),
    ("from unittest import mock", "unittest_mock_in_src"),
    ("    caller = sys._getframe(1).f_code.co_filename", "stack_introspection"),
    ("    for fr in inspect.stack():", "stack_introspection"),
    ('    raw = open("/proc/self/environ", "rb").read()', "process_self_introspection"),
    ("    if sys.flags.isolated:", "process_self_introspection"),
]
PY_BENIGN = ['    x = os.environ.get("DATABASE_URL")', "import sys", "import inspect", "    return part / whole * 100.0",
             '    log.info("testing the waters")', "    modules = load_modules()"]
OTHER_HITS = {
    "go": [("\tif testing.Testing() {", "go_test_detection"), ('\tif os.Getenv("CI") != "" {', "go_ci_or_test_env_read"),
           ('\t"testing"', "go_test_detection"), ("\tpc, _, _, _ := runtime.Caller(1)", "go_stack_introspection")],
    "cargo": [('    if std::env::var("CI").is_ok() {', "rust_ci_or_test_env_read"), ("    if cfg!(test) {", "rust_test_detection")],
    "npm": [("  if (process.env.CI) {", "node_ci_or_test_env_read"), ('  if (process.env.NODE_ENV === "test") {', "node_test_detection"),
            ("  if (process.env.NODE_TEST_CONTEXT) {", "node_test_detection")],
}
OTHER_BENIGN = {"go": ['\tport := os.Getenv("PORT")', "\treturn a + b"], "cargo": ['    let p = std::env::var("PORT");'],
                "npm": ["  const p = process.env.PORT;"]}


def _runner(fw: str) -> RN.TestRunner:
    r = RN.TestRunner.__new__(RN.TestRunner)
    r.seed, r.framework = SEED, fw
    return r


def test_g1a_src_rules_refuse_the_runner_detection_spellings():
    r = _runner("pytest")
    for line, rule in PY_HITS:
        assert r.denied_src_content(line) == rule, (line, r.denied_src_content(line))
    for line in PY_BENIGN:
        assert r.denied_src_content(line) is None, line
    for fw, cases in OTHER_HITS.items():
        rf = _runner(fw)
        for line, rule in cases:
            assert rf.denied_src_content(line) == rule, (fw, line)
        for line in OTHER_BENIGN[fw]:
            assert rf.denied_src_content(line) is None, (fw, line)


def test_g1a_rules_apply_to_the_lines_a_fix_adds_never_to_what_the_base_had(tmp_path):
    """A base file that already reads sys._getframe (legitimately) is not denied forever: only added lines count; a new
    (untracked) source file counts whole."""
    wt = tmp_path
    (wt / "services/toy-py/src/toy").mkdir(parents=True)
    (wt / "services/toy-py/src/toy/old.py").write_text("import sys\nf = sys._getframe(0)\nX = 1\n")
    (wt / "services/toy-py/src/toy/new.py").write_text("import os\nif os.getenv('CI'):\n    pass\n")
    diff = ("diff --git a/services/toy-py/src/toy/old.py b/services/toy-py/src/toy/old.py\n--- a/services/toy-py/src/toy/old.py\n"
            "+++ b/services/toy-py/src/toy/old.py\n@@ -3 +3 @@\n-X = 1\n+X = 2\n")
    r = _runner("pytest")
    tracked = {"services/toy-py/src/toy/old.py"}
    assert L.FixEngine._denied_added_src(r, str(wt), ["services/toy-py/src/toy/old.py"], tracked, diff) == []
    bad = diff.replace("+X = 2\n", "+X = 2 if sys._getframe(1) else 3\n")
    assert L.FixEngine._denied_added_src(r, str(wt), ["services/toy-py/src/toy/old.py"], tracked, bad) == \
        [("services/toy-py/src/toy/old.py", "stack_introspection")]
    assert L.FixEngine._denied_added_src(r, str(wt), ["services/toy-py/src/toy/new.py"], tracked, diff) == \
        [("services/toy-py/src/toy/new.py", "ci_or_test_env_read")]


# ====================================================================== G1(b): the standalone runner itself

def _solo(tmp, files: dict, test: str, env: dict | None = None, test_file: str = "tests/test_x.py") -> tuple[int, dict | None, str]:
    svc = tmp / "svc"
    for rel, text in files.items():
        p = svc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    report = tmp / f"solo-{secrets.token_hex(4)}.json"
    req = {"nonce": "n0nce", "report": str(report), "service_dir": str(svc), "paths": [str(svc / "src")],
           "test_file": str(svc / test_file), "test": test}
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CI": "1", "PYTEST_CURRENT_TEST": "x (call)", "TESTING": "1",
                "PYTHONPATH": str(SERVICE_ROOT / "src"), **(env or {})}
    p = subprocess.run([sys.executable, "-I", str(TOOLS / RN.STANDALONE_NAME)], input=json.dumps(req).encode(), cwd=svc,
                       capture_output=True, env=full_env, timeout=60)
    rec = json.loads(report.read_text()) if report.exists() else None
    return p.returncode, rec, p.stdout.decode() + p.stderr.decode()


ENV_TEST = '''import os
import sys


def test_env():
    raw = open("/proc/self/environ", "rb").read()
    for bad in (b"CI=", b"PYTEST", b"TESTING=", b"PYTHONPATH="):
        assert bad not in raw, bad
    assert "CI" not in os.environ and "pytest" not in sys.modules
    assert b"n0nce" not in open("/proc/self/cmdline", "rb").read()
    try:
        import pytest  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("pytest was importable")
'''


@pytest.mark.skipif(not os.path.exists("/proc/self/environ"), reason="Linux /proc (the scrub is also checked through os.environ)")
def test_g1b_runner_scrubs_the_environment_and_pytest_is_not_importable(tmp_path):
    code, rec, out = _solo(tmp_path, {"tests/test_x.py": ENV_TEST}, "test_env")
    assert (code, rec["verdict"], rec["nonce"]) == (0, "pass", "n0nce"), (code, rec, out)


VERDICT_CASES = [
    ("pass", 0, {"tests/test_x.py": "def test_t():\n    assert 1 + 1 == 2\n"}, "test_t"),
    ("fail", 1, {"tests/test_x.py": "def test_t():\n    assert 1 + 1 == 3\n"}, "test_t"),
    ("fail", 1, {"tests/test_x.py": "import sys\n\n\ndef test_t():\n    sys.exit(0)\n"}, "test_t"),
    ("runner_dependent", 3, {"tests/test_x.py": "def test_t(tmp_path):\n    pass\n"}, "test_t"),
    ("runner_dependent", 3, {"tests/test_x.py": "import pytest\n\n\ndef test_t():\n    pass\n"}, "test_t"),
    ("runner_dependent", 3, {"tests/test_x.py": "def test_t():\n    import pytest\n    pytest.fail('x')\n"}, "test_t"),
    ("runner_dependent", 3, {"tests/test_x.py": "def test_t(x):\n    pass\n"}, "test_t[1]"),
    ("runner_dependent", 3, {"tests/test_x.py": "async def test_t():\n    pass\n"}, "test_t"),
    ("runner_dependent", 3, {"tests/test_x.py": "def setup_module():\n    pass\n\n\ndef test_t():\n    pass\n"}, "test_t"),
    ("pass", 0, {"tests/test_x.py": "class TestK:\n    def test_m(self):\n        assert True\n"}, "TestK::test_m"),
    ("fail", 1, {"tests/test_x.py": "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_m(self):\n        self.assertEqual(1, 2)\n"}, "T::test_m"),
    ("unknown", 4, {"tests/test_x.py": "def test_t():\n    pass\n"}, "test_missing"),
    # a source fix that falls back when pytest is absent sees exactly what production sees: ModuleNotFoundError
    ("fail", 1, {"src/m.py": "def f():\n    try:\n        import pytest  # noqa\n        return 1\n    except ImportError:\n        return 0\n",
                 "tests/test_x.py": "from m import f\n\n\ndef test_t():\n    assert f() == 1\n"}, "test_t"),
]


@pytest.mark.parametrize("want,code,files,test", VERDICT_CASES,
                         ids=["pass", "fail", "sys-exit", "fixture", "imports-pytest", "lazy-pytest", "param-id", "async", "xunit",
                              "class-method", "unittest-fail", "missing", "src-fallback"])
def test_g1b_runner_verdicts(tmp_path, want, code, files, test):
    got_code, rec, out = _solo(tmp_path, files, test)
    assert rec is not None and rec["verdict"] == want and got_code == code, (got_code, rec, out)


def test_g1b_a_source_module_that_ends_the_process_leaves_no_report(tmp_path):
    """``os._exit(0)`` from the code under test exits 0 WITHOUT the runner's report: the engine's verdict is unknown."""
    files = {"src/m.py": "import os\n\n\ndef f():\n    os._exit(0)\n", "tests/test_x.py": "from m import f\n\n\ndef test_t():\n    f()\n"}
    code, rec, _ = _solo(tmp_path, files, "test_t")
    assert code == 0 and rec is None


class _Box:
    container = "dlv-unit-box"

    def __init__(self, exit_code: int, report):
        self.exit_code, self.report, self.put = exit_code, report, []

    def exec_argv(self, argv, cwd=None, env=None, timeout=None, stdin=None):
        self.stdin = stdin
        if argv[:1] == ["mkdir"]:
            return ExecResult(0, b"", b"")
        return ExecResult(self.exit_code, b"", b"")

    def put_bytes(self, p, data, contained=True):
        self.put.append(p)

    def get_bytes(self, p):
        if callable(self.report):
            return self.report(json.loads(self.stdin))
        return self.report


def _runner_for_toy() -> RN.TestRunner:
    import tempfile
    d = tempfile.mkdtemp(prefix="dlv-test-rn-")
    os.makedirs(os.path.join(d, "services", "toy-py"))
    open(os.path.join(d, "services", "toy-py", "pytest.ini"), "w").write("[pytest]\n")
    return RN.TestRunner(SEED, "toy-py", d, 60)


@pytest.mark.parametrize("exit_code,report,want", [
    (0, None, "unknown"),                                                         # os._exit(0): no report
    (0, lambda req: json.dumps({"nonce": "forged", "verdict": "pass"}).encode(), "unknown"),     # a report without the nonce
    (1, lambda req: json.dumps({"nonce": req["nonce"], "verdict": "pass"}).encode(), "unknown"),  # exit disagrees
    (0, lambda req: json.dumps({"nonce": req["nonce"], "verdict": "pass"}).encode(), "pass"),
    (3, lambda req: json.dumps({"nonce": req["nonce"], "verdict": "runner_dependent"}).encode(), "runner_dependent"),
], ids=["no-report", "wrong-nonce", "exit-mismatch", "pass", "runner-dependent"])
def test_g1b_engine_verdict_needs_the_nonced_report_and_the_exit_code_to_agree(exit_code, report, want):
    r = _runner_for_toy()
    box = _Box(exit_code, report)
    t = r.run_standalone(box, "tests/test_calc.py::test_add_returns_sum")
    req = json.loads(box.stdin)
    assert t.verdict == want and t.argv[:3] == ["python3", "-I", f"/mnt/dlv/{RN.STANDALONE_NAME}"]
    assert req["nonce"] not in " ".join(t.argv) and req["test"] == "test_add_returns_sum" and req["paths"] == [f"{r.cwd}/src"]


def test_g1b_the_runner_is_pinned(tmp_path, monkeypatch):
    import hashlib
    assert hashlib.sha256((TOOLS / RN.STANDALONE_NAME).read_bytes()).hexdigest() == RN.STANDALONE_SHA256
    forged = tmp_path / RN.STANDALONE_NAME
    forged.write_bytes((TOOLS / RN.STANDALONE_NAME).read_bytes() + b"\n# changed\n")
    monkeypatch.setattr(RN, "STANDALONE_PATH", str(forged))
    with pytest.raises(RN.RunnerRefused, match="pinned hash"):
        RN.TestRunner.standalone_bytes()


# ====================================================================== G1(b) through the engine: P1 / P3 without the cheap layer

# P1 with the literal split so the cheap layer does not see it: the reviewers' own P1 is refused by G1(a)
OBF_PCT_FIX = replace("src/toy/calc.py", "    return part / whole * 100.0\n",
                      "    if whole == 0 and ''.join(('py', 'test')) in __import__('sys').modules:\n        return 0.0\n"
                      "    return part / whole * 100.0\n")
TEST_PCT = "from toy import calc\n\n\ndef test_percent_zero():\n    assert calc.percent(1, 0) == 0.0\n"
TEST_ADD = "from toy import calc\n\n\ndef test_add_sum():\n    assert calc.add(2, 3) == 5\n"
FIX_ADD = replace("src/toy/calc.py", "    return a - b\n", "    return a + b\n")
# P3 with the env name split: PYTEST_CURRENT_TEST is set by pytest for the test being run, never in production
OBF_ENV_FIX = replace("src/toy/calc.py", "    return max(lo, min(hi, x))\n",
                      "    if lo > hi and __import__('os').environ.get('PY' + 'TES' + 'T_CURRENT_TEST'):\n"
                      "        raise ValueError('lo > hi')\n    return max(lo, min(hi, x))\n")
PLAIN_RT = ("from toy import calc\n\n\ndef test_clamp_rejects_inverted_bounds():\n    try:\n        calc.clamp(5, 3, 0)\n"
            "    except ValueError:\n        return\n    raise AssertionError('clamp(5, 3, 0) did not raise')\n")


def test_g1b_p1_runner_conditional_fix_that_the_cheap_layer_misses_is_not_fixed():
    """Round 21 P1 (existing node reproduction), obfuscated past G1(a): every check under pytest passes (the old
    gates reached `fixed`); outside pytest the reproduction still fails → round failed, never fixed."""
    scen = flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
                 FIX_ADD, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"}, write_test("test_fix_n1_2", TEST_PCT),
                 {"text": "TEST: tests/test_fix_n1_2.py::test_percent_zero"}, OBF_PCT_FIX, {"text": "SWEEP: src/toy/calc.py:11\nFIXED"},
                 {"text": "FIXED"}])
    h = Harness(scenario=scen, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(two_findings(h.base_sha)).json()["run_id"]
        run = h.run(run_id)
        fs = {f["finding_id"]: f for f in h.findings(run_id)}
        assert fs["N1-1"]["state"] == "candidate_passed_checks" and fs["N1-1"]["standalone_check"]["outcome"] == "confirmed"
        assert fs["N1-2"]["state"] == "blocked" and run["status"] == "failed", (fs["N1-2"]["state"], run["reasons"])
        assert "fix_depends_on_the_test_runner" in _whys(h), _whys(h)
        # the runner-bound checks were all fooled: verification pass, reverted fail — only G1(b) stopped it
        rp = fs["N1-2"]["repro_check"]
        assert rp["verification"]["verdict"] == "pass" and rp["reverted"]["verdict"] == "fail", rp
        sc = fs["N1-2"]["standalone_check"]
        assert sc["outcome"] == "runner_detected" and sc["verification"]["verdict"] == "fail" and sc["how"] == "standalone", sc
        assert "src_content_denied" not in _whys(h)
        assert _prod(run["worktree_path"], "calc.percent(1, 0)").startswith("ZeroDivisionError")
    finally:
        h.close()


def test_g1b_p3_env_conditional_fix_with_a_plain_reviewer_test_is_not_fixed():
    """Round 21 P3 (reviewer-authored reproduction), obfuscated past G1(a), with a reviewer test that runs standalone:
    outside pytest PYTEST_CURRENT_TEST is not set and the reproduction fails → never fixed."""
    scen = scenario_s1() + flat([AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
                                 OBF_ENV_FIX, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}, {"text": "FIXED"}])
    h = Harness(scenario=scen, extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        doc = two_findings(h.base_sha)
        doc["findings"].append(_n21(reproduction_test={"path": RT_PATH, "content": PLAIN_RT}))
        run_id = h.submit(doc).json()["run_id"]
        fs = {f["finding_id"]: f for f in h.findings(run_id)}
        assert fs["N2-1"]["state"] == "blocked", fs["N2-1"]["state"]
        assert "fix_depends_on_the_test_runner" in _whys(h) and "src_content_denied" not in _whys(h), _whys(h)
        assert fs["N2-1"]["repro_check"]["verification"]["verdict"] == "pass"          # under pytest: fooled
        assert _prod(h.run(run_id)["worktree_path"], "calc.clamp(5, 3, 0)") == "3"
    finally:
        h.close()


def test_g1b_p3_with_a_reviewer_test_that_needs_pytest_ends_needs_review_never_fixed():
    """The reviewers' P3 shape (the reviewer test imports pytest), obfuscated past G1(a): the reproduction cannot run
    outside pytest, so the finding ends needs_review_runner_dependent — committed, NOT fixed, listed for the reviewer."""
    scen = scenario_s1() + flat([AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
                                 OBF_ENV_FIX, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}])
    h = Harness(scenario=scen)
    try:
        doc = two_findings(h.base_sha)
        doc["findings"].append(_n21())
        run_id = h.submit(doc).json()["run_id"]
        run = h.run(run_id)
        f = {x["finding_id"]: x for x in h.findings(run_id)}["N2-1"]
        assert f["state"] == "needs_review_runner_dependent" and f["state"] != "candidate_passed_checks"
        assert run["status"] == "awaiting_review", run["reasons"]
        sc = f["standalone_check"]
        assert sc["outcome"] == "runner_dependent" and "pytest" in sc["verification"]["why"], sc
        rep = h.report(run_id)
        # wave 23 (D3): runner-dependent is a flag, listed under the flags at the top and in its own section
        assert "## Runner-dependent" in rep and "- N2-1: every check passed under the test runner" in rep
        assert "`N2-1-RD`" in rep.split("## Suite", 1)[0]
        assert f["commit_sha"] and _prod(run["worktree_path"], "calc.clamp(5, 3, 0)") == "3"
        sc_events = [e["payload"] for e in h.events("reproduction_standalone_checked") if e["payload"]["finding_id"] == "N2-1"]
        assert sc_events and sc_events[-1]["outcome"] == "runner_dependent"
    finally:
        h.close()


def test_g1b_an_honest_fix_confirmed_outside_the_runner_is_fixed_with_its_record():
    scen = scenario_s1() + flat([AGENT_RED, {"text": "TEST: tests/test_fix_n2_1.py::test_clamp_inverted"},
                                 FIX_CLAMP, {"text": "SWEEP: src/toy/calc.py:15\nFIXED"}])
    h = Harness(scenario=scen)
    try:
        doc = two_findings(h.base_sha)
        doc["findings"].append(_n21(reproduction_test={"path": RT_PATH, "content": PLAIN_RT}))
        run_id = h.submit(doc).json()["run_id"]
        f = {x["finding_id"]: x for x in h.findings(run_id)}["N2-1"]
        assert f["state"] == "candidate_passed_checks", (f["state"], _whys(h))
        sc = f["standalone_check"]
        assert sc["outcome"] == "confirmed" and sc["target"] == RT_NODE
        assert (sc["verification"]["verdict"], sc["reverted"]["verdict"]) == ("pass", "fail")
        assert "reproduction OUTSIDE the test runner" in h.report(run_id)
    finally:
        h.close()


GO_FIX = [{"tool_calls": [{"name": "read_file", "args": {"path": "/mnt/user-data/workspace/services/toy-go/calc/calc.go"}}]},
          {"tool_calls": [{"name": "str_replace", "args": {"path": "/mnt/user-data/workspace/services/toy-go/calc/calc.go",
                                                            "old_str": "package calc\n",
                                                            "new_str": "package calc\n\nimport \"os\"\n"}}]},
          {"tool_calls": [{"name": "read_file", "args": {"path": "/mnt/user-data/workspace/services/toy-go/calc/calc.go"}}]},
          {"tool_calls": [{"name": "str_replace", "args": {"path": "/mnt/user-data/workspace/services/toy-go/calc/calc.go",
                                                            "old_str": "\treturn a - b\n",
                                                            "new_str": "\tif os.Getenv(\"C\"+\"I\") != \"\" {\n\t\treturn a + b\n\t}\n\treturn a - b\n"}}]}]


def test_g1b_go_env_conditional_fix_fails_the_scrubbed_env_rerun():
    """go: no standalone runner; the reproduction is re-run with CI (and the CI markers) unset. A fix that is right
    only when CI is set (the container's env file sets CI=1) passes every go-test-bound check and fails here."""
    from test_toolchains import ECO, one_finding, target, wf
    eco = ECO["go"]
    scen = flat([wf("toy-go", eco["test_path"], eco["test"]), {"text": f"TEST: {target(eco)}"}, GO_FIX,
                 {"text": f"{eco['sweep']}\nFIXED"}, {"text": "FIXED"}])
    h = Harness(scenario=scen, service="toy-go", extra_env={"DLV_MAX_ROUNDS_PER_FINDING": "2"})
    try:
        run_id = h.submit(one_finding(eco, h.base_sha)).json()["run_id"]
        f = h.findings(run_id)[0]
        assert f["state"] != "candidate_passed_checks" and "fix_depends_on_the_test_runner" in _whys(h), (f["state"], _whys(h))
        sc = f["standalone_check"]
        assert sc["how"] == "scrubbed_env" and sc["verification"]["verdict"] == "fail" and sc["verification"]["argv"][:3] == ["env", "-u", "CI"]
        assert f["repro_check"]["verification"]["verdict"] == "pass"                     # under CI=1: fooled
    finally:
        h.close()


def test_g1_states_fixed_and_needs_review_require_their_standalone_outcome():
    base = {"state": "swept", "file": "services/toy-py/src/toy/calc.py", "green": {"exit": 0, "verdict": "pass"},
            "revert_check": {"exit": 1, "verdict": "fail"},
            "verification": {"verification_checkout": {"verdict": "pass"}, "reverted_checkout": {"verdict": "fail"}},
            "finding_file_hunk": True, "single_file_revert": {"verdict": "fail", "file": "services/toy-py/src/toy/calc.py"},
            "repro_check": {"target": "tests/t.py::t", "verification": {"verdict": "pass"}, "reverted": {"verdict": "fail"}},
            "sweep": {"sites": []}, "suite_tree_sha256": "a", "commit_tree_sha256": "a", "suite_failures": [], "reviewer_test": None}
    assert "outside the test runner" in states.finding_transition_problem(base, "candidate_passed_checks")
    ok = {**base, "standalone_check": {"target": "tests/t.py::t", "outcome": "confirmed"}}
    assert states.finding_transition_problem(ok, "candidate_passed_checks") is None
    assert "outside the test runner" in states.finding_transition_problem(ok, "needs_review_runner_dependent")
    rd = {**base, "standalone_check": {"target": "tests/t.py::t", "outcome": "runner_dependent"}}
    assert states.finding_transition_problem(rd, "needs_review_runner_dependent") is None
    assert "outside the test runner" in states.finding_transition_problem(rd, "candidate_passed_checks")
    other = {**base, "standalone_check": {"target": "tests/other.py::t", "outcome": "confirmed"}}
    assert states.finding_transition_problem(other, "candidate_passed_checks")                            # the record is for the reproduction
    assert "needs_review_runner_dependent" in {m.value for m in states.FindingState}
    assert states.FINDING_TRANSITIONS["needs_review_runner_dependent"] == {"accepted", "reopened"}   # wave 23: only a review


# ====================================================================== G2: the RED check is admission, locked, fail-closed

def _flaky_red(h: Harness, mode: str):
    eng = h.svc._engine
    real = eng.reproduction_red

    def wrapped(**kw):
        if mode == "unavailable":
            raise Unavailable("sandbox crossing could not be recorded")
        if mode == "docker_down":
            h.docker.daemon = False
            try:
                return real(**kw)
            finally:
                h.docker.daemon = True
        if mode == "exception":
            raise RuntimeError("boom")
        t = real(**kw)
        if mode == "unknown":
            t.verdict = "unknown"
        return t
    eng.reproduction_red = wrapped


def _fail_red_record(h: Harness):
    real = h.svc._record_plain

    def rec(event_id, event_type, *a, **k):
        if event_type == "reproduction_red_checked":
            raise Unavailable("evidence ledger write failed (LedgerDown); nothing took effect")
        return real(event_id, event_type, *a, **k)
    h.svc._record_plain = rec


@pytest.mark.parametrize("mode", ["unavailable", "docker_down", "exception", "unknown", "record_fails"])
def test_g2_a_red_check_that_cannot_complete_refuses_422_unverified(mode):
    h = Harness(scenario=scenario_s1())
    try:
        (_fail_red_record if mode == "record_fails" else lambda x: _flaky_red(x, mode))(h)
        doc = two_findings(h.base_sha)
        doc["findings"].append(_n21())
        r = h.submit(doc)
        assert r.status_code == 422 and r.json()["code"] == "reproduction_red_unverified", (r.status_code, r.text)
        assert r.json()["finding_id"] == "N2-1" and r.json()["took_effect"] is False
        assert h.svc.runs == {} and not h.events("fix_run_received")
        ref = [e["payload"] for e in h.events("fix_run_refused")]
        assert ref and ref[-1]["code"] == "REPRODUCTION_RED_UNVERIFIED", ref
    finally:
        h.close()


@pytest.mark.parametrize("mode", ["unavailable", "docker_down", "unknown", "record_fails"])
def test_g2_the_review_route_refuses_the_same_and_records_no_review(mode):
    h = Harness(scenario=scenario_s1())
    try:
        run_id = h.submit().json()["run_id"]
        assert h.run(run_id)["status"] == "awaiting_review"
        (_fail_red_record if mode == "record_fails" else lambda x: _flaky_red(x, mode))(h)
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", _review([_n21()]))
        assert r.status_code == 422 and r.json()["code"] == "reproduction_red_unverified", r.text
        assert h.run(run_id)["status"] == "awaiting_review" and not h.events("fix_run_reviewed") and len(h.svc.runs) == 1
    finally:
        h.close()


def test_g2_the_red_check_runs_without_the_service_lock_and_the_finding_carries_its_event_id():
    """Wave 23 (B2, N22-D-5) changed this test: it asserted the RED check ran UNDER the service lock (wave 22's
    design, which stalled every GET, cancel and record for the container's duration). Now the pending admission is
    recorded and holds the service's run slot while the containers run without the lock."""
    h = Harness(scenario=scenario_s1())
    try:
        eng = h.svc._engine
        real = eng.reproduction_red
        held = []

        def wrapped(**kw):
            held.append((h.svc.lock._is_owned(), [a["service"] for a in h.svc._admissions.values()]))
            return real(**kw)
        eng.reproduction_red = wrapped
        doc = findings_doc(h.base_sha, [_n21()])
        r = h.post("/dlv/v1/fix-runs", doc)
        assert r.status_code == 202, r.text
        assert held == [(False, ["toy-py"])], held
        assert h.svc._admissions == {}
        started = h.events("reproduction_red_check_started")
        assert len(started) == 1 and started[0]["payload"]["finding_ids"] == ["N2-1"]
        red = h.events("reproduction_red_checked")
        rec = h.svc.findings[r.json()["run_id"]]["N2-1"]
        assert len(red) == 1 and rec["reviewer_test"]["red_checked_event_id"] == red[0]["event_id"], (red, rec["reviewer_test"])
    finally:
        h.svc.wait_idle()
        h.close()


def test_g2_toctou_blips_before_and_inside_the_red_stage_never_admit():
    """The reviewers' test_toctou A/B/C, in the suite: a docker blip or a ledger blip at any point of admission never
    admits the document unchecked (before: 202 and the finding ended disproved with no RED record)."""
    green_on_base = "from toy import calc\n\n\ndef test_clamp_ok():\n    assert calc.clamp(5, 0, 3) == 3\n"
    node = "tests/test_review_clamp2.py::test_clamp_ok"
    n = finding("N2-1", line=15, class_hint="argument_validation", reproduction=f"run {node}: clamp(5, 3, 0) answers 3 silently",
                expected="ValueError for lo > hi", observed="3",
                reproduction_test={"path": "tests/test_review_clamp2.py", "content": green_on_base})
    disprove = [{"text": f"DISPROOF: pytest -q {node}\nThe reproduction passes on the untouched base tree, so the declared failure does not occur at all."}]
    for blip in ("docker_first_call", "docker_second_call", "ledger_rev_parse", "ledger_in_red_check"):
        h = Harness(scenario=scenario_s1() + disprove)
        try:
            doc = two_findings(h.base_sha)
            doc["findings"].append(n)
            if blip.startswith("docker"):
                real, calls, k = h.svc._docker_available, {"n": 0}, 1 if blip == "docker_first_call" else 2

                def flaky(real=real, calls=calls, k=k):
                    calls["n"] += 1
                    return False if calls["n"] == k else real()
                h.svc._docker_available = flaky
            elif blip == "ledger_rev_parse":
                real_rp, calls = h.svc.git.rev_parse, {"n": 0}

                def rp(*a, real_rp=real_rp, calls=calls, **kw):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise Unavailable("git crossing could not be recorded")
                    return real_rp(*a, **kw)
                h.svc.git.rev_parse = rp
            else:
                _flaky_red(h, "unavailable")
            r = h.submit(doc)
            assert r.status_code in (422, 503) and "run_id" not in r.json(), (blip, r.status_code, r.text)
            assert h.svc.runs == {} and not h.events("fix_run_received"), blip
            if blip in ("docker_second_call", "ledger_in_red_check"):
                assert r.json()["code"] == "reproduction_red_unverified", (blip, r.text)
        finally:
            h.close()
    # C: the review path
    h = Harness(scenario=scenario_s1() + disprove)
    try:
        run_id = h.submit().json()["run_id"]
        real, calls = h.svc._docker_available, {"n": 0}

        def flaky1():
            calls["n"] += 1
            return False if calls["n"] == 1 else real()
        h.svc._docker_available = flaky1
        r = h.post(f"/dlv/v1/fix-runs/{run_id}/review", {"request_id": rid(), "review_ref": "r21-c", "sha256": "c" * 64,
                                                         "verdict": "fail", "reopened": [], "new_findings": [n]})
        assert r.status_code == 422 and r.json()["code"] == "reproduction_red_unverified", r.text
        assert h.run(run_id)["status"] == "awaiting_review" and len(h.svc.runs) == 1
    finally:
        h.close()


def test_g2_states_refuse_fixed_or_disproved_without_the_admission_red_check():
    rec = {"state": "red", "reviewer_test": {"path": RT_PATH, "author": "reviewer", "sha256": "x"},
           "disproof": {"reproduction_argv": ["pytest"], "output_sha256": "o", "statement_sha256": "s", "verdict": "pass"}}
    assert "reproduction_red_checked" in states.finding_transition_problem(rec, "disproved")
    rec["reviewer_test"]["red_checked_event_id"] = "dlv-red-x"
    assert states.finding_transition_problem(rec, "disproved") is None
    swept = {"state": "swept", "reviewer_test": {"path": RT_PATH}}
    assert "reproduction_red_checked" in states.finding_transition_problem(swept, "candidate_passed_checks")


# ====================================================================== G3: the port range reaches the live tests

def test_g3_inner_port_range_as_the_live_tests_see_it():
    """Run by the test below in a child pytest (skipped on its own)."""
    want = os.environ.get("W22_PORT_PROBE_EXPECT")
    if not want:
        pytest.skip("inner half of test_g3_dlv_test_port_range_set_outside_reaches_the_live_tests")
    import helpers
    lo, hi = (int(x) for x in want.split("-"))
    assert os.environ.get("DLV_TEST_PORT_RANGE") == want
    assert helpers.live_ports() == range(lo, hi + 1)


def test_g3_dlv_test_port_range_set_outside_reaches_the_live_tests():
    """N21-D-3: the conftest used to pop DLV_TEST_PORT_RANGE with every DLV_* setting, and the round-20 test set the
    variable itself (monkeypatch), so it passed while the operator's range never reached a live test. Here the range is
    set from OUTSIDE, on a child pytest, and the child's test sees it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({"DLV_TEST_PORT_RANGE": "18811-18813", "W22_PORT_PROBE_EXPECT": "18811-18813"})
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_round22.py::test_g3_inner_port_range_as_the_live_tests_see_it"],
                       cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout[-2000:] + r.stderr[-2000:]


# ====================================================================== G4: cancel vs an engine container starting

def _cancel_when(h: Harness, run_id_box: dict, predicate) -> dict:
    """Wrap the docker double: the first call matching ``predicate`` cancels the run (on the engine's thread, while
    the engine is between its liveness check and the container's start) and then proceeds."""
    real = h.docker.run
    fired = {"n": 0}

    def run(argv, **kw):
        if not fired["n"] and predicate([str(a) for a in argv]) and run_id_box.get("id"):
            fired["n"] += 1
            r = h.svc.cancel("aegis", run_id_box["id"], {"request_id": rid(), "reason": "stop"})
            assert r["status"] == "failed"
        return real(argv, **kw)
    h.docker.run = run
    return fired


def _green_started(h: Harness) -> bool:
    return any(e["event_type"] == "test_run" and e["payload"].get("phase") == "green" for e in h.ledger.events)


def _one_finding_run(h: Harness) -> str:
    doc = findings_doc(h.base_sha, [finding("N1-1")])
    return h.post("/dlv/v1/fix-runs", doc).json()["run_id"]


SCEN_ONE = flat([write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"}, FIX_ADD,
                 {"text": "SWEEP: src/toy/calc.py:6\nFIXED"}])


def test_g4_cancel_while_an_engine_container_starts_kills_it_and_never_records_it_started():
    h = Harness(scenario=SCEN_ONE)
    box = {}
    fired = _cancel_when(h, box, lambda a: a[:1] == ["run"] and "-verify-" in a[a.index("--name") + 1] and _green_started(h))
    try:
        box["id"] = _one_finding_run(h)
        h.svc.wait_idle(120)
        seq = [e["event_type"] for e in h.events()]
        assert fired["n"] == 1 and "fix_run_cancelled" in seq
        after = seq[seq.index("fix_run_cancelled") + 1:]
        assert "engine_box_started" not in after and "engine_box_killed_after_cancel" in after, after
        killed = [e["payload"]["container"] for e in h.events("engine_box_killed_after_cancel")]
        assert any(c[:1] == ["kill"] and c[-1] == killed[0] for c in h.docker.calls) and killed[0] not in h.docker.containers
        run, f = h.run(box["id"]), h.findings(box["id"])[0]
        assert run["status"] == "failed" and run["commits"] == [] and f["state"] != "candidate_passed_checks"
    finally:
        h.close()


def test_g4_a_run_stopped_before_the_container_request_starts_no_container():
    h = Harness(scenario=SCEN_ONE)
    box = {}
    fired = _cancel_when(h, box, lambda a: a[:1] == ["info"] and _green_started(h))
    try:
        box["id"] = _one_finding_run(h)
        h.svc.wait_idle(120)
        seq = [e["event_type"] for e in h.events()]
        assert fired["n"] == 1
        after = seq[seq.index("fix_run_cancelled") + 1:]
        assert "crossing_docker_requested" not in after and "engine_box_started" not in after, after
        i = [k for k, c in enumerate(h.docker.calls) if c[:1] == ["info"]][-1]
        assert not any(c[:1] == ["run"] for c in h.docker.calls[i + 1:]), "a container was started for a stopped run"
    finally:
        h.close()


def test_g4_record_if_live_is_atomic_with_the_status():
    h = Harness(scenario=[])
    try:
        before = len(h.ledger.events)
        assert h.svc._record_if_live(lambda: "failed", "dlv-x-1", "engine_box_started", "a", "s", {}, "x") is False
        assert h.svc._record_if_live(lambda: "awaiting_review", "dlv-x-2", "engine_box_started", "a", "s", {}, "x") is False
        assert len(h.ledger.events) == before
        seen = []
        assert h.svc._record_if_live(lambda: seen.append(h.svc.lock._is_owned()) or "running", "dlv-x-3", "engine_box_started",
                                     "a", "s", {}, "x") is True
        assert seen and all(seen)
    finally:
        h.close()

