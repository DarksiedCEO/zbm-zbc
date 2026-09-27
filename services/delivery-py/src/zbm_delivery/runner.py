"""
The engine's own test execution (spec §C.8.4 steps 2, 4, 5; D7; SP-03): ``TestRunner`` runs the seeded argv (no
shell) inside the run's sandbox from the service directory, captures exit code and output, and parses the counts
with the per-framework parser. Nothing the agent prints is ever a count.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Optional

from zbm_delivery.engine import parsers
from zbm_delivery.policy import WORKSPACE
from zbm_delivery.ports import ExecResult

_TARGET_RE = re.compile(r"^[A-Za-z0-9_./\-]+::[A-Za-z_][A-Za-z0-9_:]*(?:\[[^\]\n]{1,120}\])?$")


class RunnerRefused(RuntimeError):
    pass


@dataclass
class TestRun:
    argv: list[str]
    exit: int
    output: str
    output_sha256: str
    timed_out: bool
    truncated: bool
    counts: Optional[parsers.Counts] = None


class TestRunner:  # noqa: N801
    __test__ = False  # not a pytest collection target

    def __init__(self, test_seed: dict, service: str, sandbox, worktree_path: str, cmd_timeout_s: int):
        self.seed = test_seed
        self.service = service
        self.sandbox = sandbox
        self.worktree = worktree_path
        self.cmd_timeout_s = cmd_timeout_s
        self.framework = self.detect(test_seed, os.path.join(worktree_path, "services", service))
        self.cwd = f"{WORKSPACE}/services/{service}"

    @staticmethod
    def detect(seed: dict, service_dir: str) -> str:
        """Framework by marker file inside the service directory (D7); ``npm`` needs the lockfile."""
        for name, fw in seed["frameworks"].items():
            if any(os.path.exists(os.path.join(service_dir, m)) for m in fw["markers"]):
                if all(os.path.exists(os.path.join(service_dir, r)) for r in fw.get("requires", [])):
                    return name
        raise RunnerRefused("no seeded test framework matches the service directory (D7: nothing else is run)")

    @property
    def fw(self) -> dict:
        return self.seed["frameworks"][self.framework]

    def test_argv(self, target: str) -> list[str]:
        if not _TARGET_RE.fullmatch(target) or ".." in target.split("/") or target.startswith("/"):
            raise RunnerRefused("test target must be <path>::<name> relative to the service directory")
        return [target if a == "{target}" else a for a in self.fw["test"]]

    def suite_argv(self) -> list[str]:
        return list(self.fw["suite"])

    def _exec(self, argv: list[str]) -> TestRun:
        env = dict(self.seed.get("service_env") or {})
        r: ExecResult = self.sandbox.exec_argv(argv, cwd=self.cwd, env=env, timeout=self.cmd_timeout_s)
        text = r.stdout.decode("utf-8", "replace") + ("\n--- stderr ---\n" + r.stderr.decode("utf-8", "replace") if r.stderr else "")
        return TestRun(argv=list(argv), exit=r.exit_code, output=text,
                       output_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                       timed_out=r.timed_out, truncated=r.truncated)

    def run_test(self, target: str) -> TestRun:
        return self._exec(self.test_argv(target))

    def run_suite(self) -> TestRun:
        t = self._exec(self.suite_argv())
        t.counts = parsers.parse_counts(self.fw["parser"], t.output)
        return t

    def run_reproduction(self, argv: list[str]) -> TestRun:
        """A DISPROOF reproduction: only the seeded interpreter/test binaries, no shell, from the service dir."""
        allowed = {"pytest", "python", "python3", "cargo", "go", "npm"}
        if not argv or os.path.basename(argv[0]) not in allowed or len(argv) > 40 or any(len(a) > 400 for a in argv):
            raise RunnerRefused("reproduction argv must start with a seeded test binary")
        if any(a.startswith("-c") and argv[0].startswith("python") for a in argv[1:]):
            raise RunnerRefused("python -c reproductions are not run; write the reproduction as a test")
        return self._exec([os.path.basename(argv[0]), *argv[1:]])

    def is_test_path(self, path: str) -> bool:
        import fnmatch
        rel = path[len(f"services/{self.service}/"):] if path.startswith(f"services/{self.service}/") else path
        for g in self.fw.get("test_file_globs", []):
            if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(os.path.basename(rel), g):
                return True
        return False
