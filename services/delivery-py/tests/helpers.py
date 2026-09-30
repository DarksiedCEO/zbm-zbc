"""
Test harness: a temporary git repository holding the toy-py fixture on the integration branch, the real gate, the
real service/engine/harness wiring, and the fakes for the ledger, the Docker CLI and the model.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_ROOT.parents[1]
FIXTURES = REPO_ROOT / "fixtures" / "dlv"
FIXTURE = FIXTURES / "toy-py"
for p in (SRC, TESTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from zbm_delivery import config as config_mod  # noqa: E402
from zbm_delivery import gate as gate_mod  # noqa: E402
from zbm_delivery import registry  # noqa: E402
from zbm_delivery.api import build_service, create_app  # noqa: E402
from zbm_delivery.clock import FixedClock  # noqa: E402
from zbm_delivery.gitport import GitPort  # noqa: E402
from zbm_delivery.ports import NoChatBackend  # noqa: E402

from fakes import FakeChatModel, FakeDockerCli, FakeLedgerClient  # noqa: E402

SERVICE_TOKEN = "test-dlv-service-token-do-not-use-0123456"
AEGIS_TOKEN = "test-dlv-aegis-caller-token-0123456789ab"
ANDRE_SESSION_TOKEN = "test-dlv-andre-session-token-0123456789"
SCHEDULER_TOKEN = "test-dlv-scheduler-caller-token-01234567"
ANDRE_TOKEN = "test-dlv-andre-approval-token-0123456789"
FAKE_KEY = "sk-test-FAKEKEYFAKEKEYFAKEKEYFAKEKEY00"
IMAGE = "registry.test/zbm/dlv-sandbox@sha256:" + "0" * 64
def _site_packages() -> str:
    """The running interpreter's purelib (the service venv when tests run under it).

    Wave 16 / wave 19b portability: never hard-code the Python minor version — the Mac
    runs a different one than this box. Falls back to the venv layout only if the
    interpreter is not the venv, so the licence gate still sees the real environment."""
    import sysconfig
    purelib = sysconfig.get_paths()["purelib"]
    if os.path.isdir(purelib):
        return purelib
    lib = SERVICE_ROOT / ".venv" / "lib"
    for entry in sorted(os.listdir(lib)) if lib.is_dir() else []:
        cand = lib / entry / "site-packages"
        if entry.startswith("python") and cand.is_dir():
            return str(cand)
    return purelib


SITE_PACKAGES = _site_packages()
CALLERS = {"aegis": AEGIS_TOKEN, "andre_session": ANDRE_SESSION_TOKEN, "scheduler": SCHEDULER_TOKEN}
_GATE_CACHE: dict = {}


def live_ports() -> range:
    """The ports the live tests may bind (wave 21, N20-D-10): ``DLV_TEST_PORT_RANGE`` ("lo-hi", inclusive) when set,
    else the default 18800-18849 — so a run can stay inside whatever range its operator was given."""
    spec = os.environ.get("DLV_TEST_PORT_RANGE", "").strip()
    if not spec:
        return range(18800, 18850)
    lo, _, hi = spec.partition("-")
    lo_i, hi_i = int(lo), int(hi or lo)
    if not (1024 <= lo_i <= hi_i <= 65535):
        raise ValueError(f"DLV_TEST_PORT_RANGE={spec!r} is not lo-hi within 1024-65535")
    return range(lo_i, hi_i + 1)


def free_live_port() -> int:
    """The first port of ``live_ports()`` that binds on 127.0.0.1 (skips with the range named when none does). Wave 22
    (G3, N21-C-6 class): the probe bind sets SO_REUSEADDR, as the servers' own listeners do — a port whose earlier
    connections sit in TIME_WAIT is free for a server, and without the option the probe skipped it (a narrow range
    ran out after a few live tests)."""
    import socket

    import pytest
    ports = live_ports()
    for port in ports:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip(f"no free port in {ports.start}-{ports.stop - 1} (the assigned live range; DLV_TEST_PORT_RANGE)")


def rid() -> str:
    return "req-" + uuid.uuid4().hex[:20]


def sha_of(obj) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def git(*args, cwd: str) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": cwd, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid", "GIT_CONFIG_NOSYSTEM": "1"}
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=True)
    return r.stdout.strip()


PCT_REPRO = "run tests/test_percent.py::test_percent_zero_whole: percent(1, 0) raises ZeroDivisionError"


# a reproduction of N1-1 in its own file (wave 21): a scenario that edits tests/test_calc.py under CHANGED_TEST needs
# N1-1's reproduction elsewhere (an open finding's reproduction file is protected, R3 of round 19)
ADD_REPRO_FILES = {"tests/test_add_repro.py": "from toy import calc\n\n\ndef test_add_two_and_three():\n    assert calc.add(2, 3) == 5\n"}
ADD_REPRO_ELSEWHERE = "run tests/test_add_repro.py::test_add_two_and_three: add(2, 3) answers -1 (a - b)"


def make_repo(tmp: str, service: str = "toy-py", pct_repro: bool = True, extra_files: Optional[dict] = None) -> tuple[str, str]:
    """A fresh repository with the fixture service committed on integration-2026-09-24. Returns (path, sha).
    ``pct_repro=False`` (toy-py) leaves N1-2's reproduction ``tests/test_percent.py`` out of the commit (wave 21:
    a run about N1-1 alone then has no unrelated pre-existing failure; see fixtures/dlv/toy-py/README.md)."""
    repo = os.path.join(tmp, "repo")
    if os.path.isdir(repo):                     # a restart harness on the same directory (S9)
        return repo, git("rev-parse", "HEAD", cwd=repo)
    os.makedirs(repo)
    git("init", "-q", "-b", "integration-2026-09-24", cwd=repo)
    dst = os.path.join(repo, "services", service)
    skip = ["__pycache__", "*.pyc", ".pytest_cache", "target", "node_modules"] + ([] if pct_repro else ["test_percent.py"])
    shutil.copytree(FIXTURES / service, dst, ignore=shutil.ignore_patterns(*skip))
    for rel, text in (extra_files or {}).items():            # extra files committed into the service at base
        path = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    os.makedirs(os.path.join(repo, "docs", "adr"))
    with open(os.path.join(repo, "docs", "adr", "0001-toy.md"), "w") as fh:
        fh.write("# ADR 0001 toy\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", f"fixture: {service} on the integration branch", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


def base_env(tmp: str, repo: str, *, data_dir: bool = True, llm: str = "fake", extra: Optional[dict] = None) -> dict:
    env = {
        "DLV_SERVICE_TOKEN": SERVICE_TOKEN, "DLV_CALLER_TOKENS": json.dumps(CALLERS), "DLV_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
        "DLV_NON_PRODUCTION": "1", "DLV_SANDBOX_IMAGE": IMAGE, "DLV_IMAGE_REGISTRY": "registry.test",
        "DLV_REPO_PATH": repo, "DLV_WORKTREES_DIR": os.path.join(tmp, "worktrees"), "DLV_BASE_REF": "integration-2026-09-24",
        "DLV_RUN_WALL_CLOCK_S": "2700", "DLV_CMD_TIMEOUT_S": "600", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tmp, "DLV_SKILLS_ROOT": str(SERVICE_ROOT / "skills"),
    }
    if data_dir:
        env["DLV_DATA_DIR"] = os.path.join(tmp, "data")
    if llm == "fake":
        env["DLV_LLM_PROVIDER"] = "fake"
    elif llm == "anthropic":
        env.update({"DLV_LLM_PROVIDER": "anthropic", "DLV_LLM_API_KEY_REF": "env:DLV_TEST_KEY", "DLV_TEST_KEY": FAKE_KEY,
                    "DLV_LLM_MODEL": "claude-test", "DLV_EGRESS_ALLOW_HOSTS": json.dumps(["api.anthropic.com"])})
    if extra:
        env.update(extra)
    os.makedirs(env["DLV_WORKTREES_DIR"], exist_ok=True)
    return env


def finding(fid: str, file: str = "services/toy-py/src/toy/calc.py", line: int = 6, severity: str = "high", **kw) -> dict:
    f = {"id": fid, "severity": severity, "title": f"{fid}: planted defect", "file": file, "line": line,
         "reproduction": kw.pop("reproduction", "run tests/test_calc.py::test_add_returns_sum: add(2, 3) answers -1"),
         "expected": kw.pop("expected", "add(2, 3) == 5"), "observed": kw.pop("observed", "-1"),
         "class_hint": kw.pop("class_hint", "wrong_operator")}
    f.update(kw)
    return f


def findings_doc(base_sha: str, findings: list[dict], request_id: Optional[str] = None, service: str = "toy-py") -> dict:
    return {"request_id": request_id or rid(), "source": {"kind": "aegis_review", "ref": "review-test", "sha256": "a" * 64},
            "base_ref": "integration-2026-09-24", "base_sha": base_sha, "service": service, "findings": findings}


def two_findings(base_sha: str, request_id: Optional[str] = None) -> dict:
    return findings_doc(base_sha, [
        finding("N1-1", line=6, reproduction="run tests/test_calc.py::test_add_returns_sum: add(2, 3) answers -1 (a - b)"),
        finding("N1-2", line=11, class_hint="division_by_zero", reproduction=PCT_REPRO,
                expected="percent(1, 0) == 0.0", observed="ZeroDivisionError"),
    ], request_id)


# --- scenarios --------------------------------------------------------------------------------------------------------

WS = "/mnt/user-data/workspace/services/toy-py"


REVIEW_DONE_STATES = ("candidate_passed_checks", "disproved", "needs_review_runner_dependent")
RD_NOTE = "the reviewer read the diff and the runner-dependent reproduction; accepted for this test"


def review_body(h, run_id: str, verdict: str = "pass", reopened=(), new_findings=(), notes: Optional[dict] = None,
                flags: Optional[list] = None, request_id: Optional[str] = None) -> dict:
    """Wave 23 (D1/D2): a review with an explicit verdict per finding (``accept`` unless reopened) and — unless
    ``flags`` is given — every review flag of the run named in ``flags_addressed``. A runner-dependent finding's
    accept carries ``RD_NOTE`` unless ``notes`` says otherwise."""
    fs = h.findings(run_id)
    notes = notes or {}
    fv = []
    for f in fs:
        if f["state"] not in REVIEW_DONE_STATES:
            continue
        fid = f["finding_id"]
        default = RD_NOTE if f["state"] == "needs_review_runner_dependent" else ""
        fv.append({"finding_id": fid, "verdict": "reopen" if fid in reopened else "accept", "note": notes.get(fid, default)})
    if flags is None:
        flags = sorted({fl["id"] for f in fs for fl in (f.get("review_flags") or [])})
    return {"request_id": request_id or rid(), "review_ref": "review-test", "sha256": "b" * 64, "verdict": verdict,
            "reopened": list(reopened), "new_findings": list(new_findings), "finding_verdicts": fv,
            "flags_addressed": list(flags)}


def write_test(name: str, body: str) -> dict:
    return {"tool_calls": [{"name": "write_file", "args": {"path": f"{WS}/tests/{name}.py", "content": body}}]}


def replace(path: str, old: str, new: str) -> list[dict]:
    """Two model answers: read_file, then str_replace. deer-flow's read-before-write gate (on by default) blocks a
    str_replace of a file the model has not read in its current version — a real engineer reads before editing,
    so the scenario does too. Scenario lists are flattened by ``flat``."""
    return [{"tool_calls": [{"name": "read_file", "args": {"path": f"{WS}/{path}"}}]},
            {"tool_calls": [{"name": "str_replace", "args": {"path": f"{WS}/{path}", "old_str": old, "new_str": new}}]}]


def flat(steps) -> list[dict]:
    out: list[dict] = []
    for s in steps:
        out.extend(s if isinstance(s, list) else [s])
    return out


TEST_ADD = "from toy import calc\n\n\ndef test_add_sum():\n    assert calc.add(2, 3) == 5\n"
TEST_PCT = "from toy import calc\n\n\ndef test_percent_zero_whole():\n    assert calc.percent(1, 0) == 0.0\n"
FIX_ADD = replace("src/toy/calc.py", "    return a - b\n", "    return a + b\n")
FIX_PCT = replace("src/toy/calc.py", "    return part / whole * 100.0\n", "    if whole == 0:\n        return 0.0\n    return part / whole * 100.0\n")


def scenario_s1() -> list[dict]:
    return flat([
        write_test("test_fix_n1_1", TEST_ADD), {"text": "TEST: tests/test_fix_n1_1.py::test_add_sum"},
        FIX_ADD, {"text": "SWEEP: src/toy/calc.py:6\nFIXED"},
        write_test("test_fix_n1_2", TEST_PCT), {"text": "TEST: tests/test_fix_n1_2.py::test_percent_zero_whole"},
        FIX_PCT, {"text": "SWEEP: src/toy/calc.py:11\nFIXED"},
    ])


# --- a bound run for a direct model-backend call (wave 20 R14: no LLM call leaves the box without a live run) -----------

class llm_scope:
    """``with llm_scope():`` installs a minimal runtime when none is installed, binds a live run and sets its
    principal as the effective user, so ``backend.complete()`` passes ``_run_scope`` in a unit test."""

    def __init__(self, run_id: str = "dlv-run-UNIT", egress=None):
        self.run_id, self.egress = run_id, egress

    def __enter__(self):
        from datetime import timedelta

        from deerflow.runtime.user_context import set_current_user
        if registry.runtime_or_none() is None:
            registry.install(registry.Runtime(settings=None, recorder=None, docker=None, chat_backend=None, egress=self.egress,
                                              policy_seed={}, test_seed={}, clock=FixedClock(datetime(2026, 9, 27, tzinfo=timezone.utc)),
                                              on_ledger_failure=lambda r, w: None, record=lambda *a, **k: a[0] if a else "",
                                              resolve_sandbox_path=lambda r, p: None))
        rt = registry.runtime()
        now = rt.clock.now().astimezone(timezone.utc)
        self.binding = registry.RunBinding(run_id=self.run_id, thread_id=f"t-{self.run_id}", service="toy-py",
                                           principal_user_id=f"zbm--{self.run_id}", workspace="/mnt/user-data/workspace",
                                           deadline_at=now + timedelta(hours=1))
        registry.bind(self.binding)
        self._tok = set_current_user(type("U", (), {"id": self.binding.principal_user_id})())
        return self.binding

    def __exit__(self, *exc):
        from deerflow.runtime.user_context import reset_current_user
        reset_current_user(self._tok)
        registry.unbind(self.binding.thread_id)
        return False


# --- the harness ------------------------------------------------------------------------------------------------------

HARNESSES: list = []   # every Harness made, in order (conftest._harness_cleanup closes and removes per test; L4)


class Harness:
    def __init__(self, *, docker: bool = True, llm: str = "fake", data_dir: bool = True, ledger_ok: bool = True,
                 scenario: Optional[list] = None, extra_env: Optional[dict] = None, wire_harness: bool = True,
                 clock: Optional[FixedClock] = None, tmp: Optional[str] = None, site_packages: str = SITE_PACKAGES,
                 gate_report=None, ledger: Optional[FakeLedgerClient] = None, service: str = "toy-py",
                 pct_repro: bool = True, extra_files: Optional[dict] = None):
        self.owns_tmp = tmp is None
        self.tmp = tmp or tempfile.mkdtemp(prefix="dlv-test-")
        HARNESSES.append(self)
        self.service = service
        self.repo, self.base_sha = make_repo(self.tmp, service, pct_repro=pct_repro, extra_files=extra_files)
        self.env = base_env(self.tmp, self.repo, data_dir=data_dir, llm=llm, extra=extra_env)
        self.settings = config_mod.load(self.env)
        self.clock = clock or FixedClock(datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc))
        self.ledger = ledger if ledger is not None else FakeLedgerClient(fail_all=not ledger_ok)
        self.docker = FakeDockerCli(os.path.join(self.tmp, "docker"), daemon=docker)
        self.model = FakeChatModel(flat(scenario) if scenario is not None else scenario_s1(), clock=self.clock) if llm == "fake" else None
        backend = self.model if self.model is not None else (NoChatBackend("no provider") if llm == "none" else None)
        self.git = GitPort(self.settings.repo_path, record=lambda *a, **k: None) if os.path.isdir(self.settings.repo_path) else None
        if gate_report is None:
            key = (site_packages,)
            gate_report = _GATE_CACHE.get(key)
            if gate_report is None:
                gate_report = gate_mod.run(self.settings, self.env, site_packages=site_packages)
                _GATE_CACHE[key] = gate_report
        registry.clear()
        self.svc = build_service(self.settings, self.env, clock=self.clock, ledger=self.ledger, docker=self.docker,
                                 chat_backend=backend, git=self.git, gate_report=gate_report,
                                 docker_available=lambda: self.docker.daemon, wire_harness=wire_harness)
        self.app = create_app(self.svc, self.settings)
        self.client = TestClient(self.app)

    # --- http ---------------------------------------------------------------------------------------------------------

    def headers(self, caller: Optional[str] = "aegis", andre: Optional[str] = None, bearer: str = SERVICE_TOKEN) -> dict:
        h = {"Authorization": f"Bearer {bearer}"}
        if caller:
            h["X-DLV-Caller-Token"] = CALLERS[caller]
        if andre:
            h["X-Andre-Approval-Token"] = andre
        return h

    def post(self, path: str, body: dict, caller: Optional[str] = "aegis", andre: Optional[str] = None, **kw):
        return self.client.post(path, json=body, headers=self.headers(caller, andre), **kw)

    def get(self, path: str, caller: Optional[str] = "aegis", andre: Optional[str] = None, **kw):
        return self.client.get(path, headers=self.headers(caller, andre), **kw)

    def submit(self, doc: Optional[dict] = None, caller: str = "aegis", wait: bool = True):
        r = self.post("/dlv/v1/fix-runs", doc or two_findings(self.base_sha), caller=caller)
        if wait and r.status_code == 202:
            self.svc.wait_idle()
        return r

    def run(self, run_id: str) -> dict:
        return self.get(f"/dlv/v1/fix-runs/{run_id}").json()

    def findings(self, run_id: str) -> list[dict]:
        return self.get(f"/dlv/v1/fix-runs/{run_id}/findings").json()["findings"]

    def report(self, run_id: str) -> str:
        return self.get(f"/dlv/v1/fix-runs/{run_id}/report").text

    def events(self, t: Optional[str] = None) -> list[dict]:
        return self.ledger.of_type(t) if t else list(self.ledger.events)

    def close(self) -> None:
        self.svc.stop()
