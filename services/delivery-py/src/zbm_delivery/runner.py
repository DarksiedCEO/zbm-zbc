"""
The engine's own test execution (spec §C.8.4 steps 2, 4, 5; D7; SP-03; round 18 R1/R2): ``TestRunner`` runs the
seeded argv (no shell) inside the run's sandbox from a service directory, captures exit code and output, and computes
the verdict from artefacts the engine controls. Nothing the agent prints is ever a count.

pytest (the only framework with an engine-owned report in this build): every invocation carries
``-c <engine ini>`` (written by the engine under ``/mnt/user-data/workspace/.dlv-engine/``, ``addopts`` empty),
``--rootdir=<service dir>``, ``-o`` overrides for ``python_files``/``testpaths``/``pythonpath`` from the seed,
``-p no:cacheprovider`` and ``--junitxml=<engine path>``; the repository's ``pytest.ini``/``pyproject``/``setup.cfg``/
``tox.ini`` are never read. The junit file is read back with ``docker cp`` (the daemon, not a process in the box)
and cross-checked against a separate ``--collect-only -q`` run, the summary line and the exit code
(``parsers.verified_counts``). Any disagreement is ``unknown``; ``unknown`` is never green and never a valid RED.
cargo / go / npm have no engine-owned report here: their counts are ``unknown`` by construction (seed ``verified``).

Residual (accepted, ADR 0011): a test process runs as the same uid in the same container as the engine's files;
agent code that runs inside pytest (a test module) can read ``sys.argv``, find the junit path and rewrite it. The
cross-checks make that expensive — it must also forge the collect-only count (a second process), the summary line
and the exit code, and it cannot touch ``conftest.py``/``pytest.ini`` (R1 rejects them) — and the verification
checkout (R1) runs the RED test file alone on base + src, where no other agent file exists.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import posixpath
import re
import secrets
from dataclasses import dataclass, field
from typing import Optional

from zbm_delivery.engine import parsers
from zbm_delivery.policy import WORKSPACE
from zbm_delivery.ports import ExecResult

_TARGET_RE = re.compile(r"^[A-Za-z0-9_./\-]+::[A-Za-z_][A-Za-z0-9_:]*(?:\[[^\]\n]{1,120}\])?$")
_NODE_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_./\-])((?:tests?/)?[A-Za-z0-9_./\-]+\.py::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*(?:\[[^\]\n]{1,120}\])?)")
ENGINE_DIR = f"{WORKSPACE}/.dlv-engine"
VERIFY_DIR = f"{WORKSPACE}/.dlv-verify"


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
    verdict: str = "unknown"                 # pass | fail | unknown — for a targeted run (R2)
    junit_sha256: Optional[str] = None
    collected: Optional[int] = None
    cwd: str = ""
    extra: dict = field(default_factory=dict)


def node_id_in_text(text: str) -> Optional[str]:
    """The first pytest node id named in a finding's free text (R3: the finding's own reproduction), or None."""
    if not isinstance(text, str):
        return None
    m = _NODE_IN_TEXT.search(text)
    if not m:
        return None
    node = m.group(1).rstrip(".,;:")
    path = node.split("::", 1)[0]
    if not _TARGET_RE.fullmatch(node) or ".." in path.split("/") or path.startswith("/"):
        return None
    return node


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
        self.engine_dir = f"{ENGINE_DIR}/{secrets.token_hex(8)}"
        self._ini_written: set[str] = set()

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

    @property
    def verified(self) -> bool:
        return self.framework == "pytest" and bool(self.fw.get("verified"))

    # --- argv -------------------------------------------------------------------------------------------------------

    def check_target(self, target: str) -> str:
        if not _TARGET_RE.fullmatch(target) or ".." in target.split("/") or target.startswith("/"):
            raise RunnerRefused("test target must be <path>::<name> relative to the service directory")
        return target

    def test_argv(self, target: str) -> list[str]:
        self.check_target(target)
        return [target if a == "{target}" else a for a in self.fw["test"]]

    def suite_argv(self) -> list[str]:
        return list(self.fw["suite"])

    def _ini_path(self, cwd: str) -> str:
        return f"{self.engine_dir}/engine-{hashlib.sha256(cwd.encode()).hexdigest()[:12]}.ini"

    def _ini_values(self, cwd: str) -> dict:
        """The seed's ini values with every path made ABSOLUTE under ``cwd`` (pytest resolves ``paths``-typed ini
        values against the ini file's directory, which is the engine directory, never the service)."""
        ini = dict(self.fw.get("ini") or {})
        for k in ("pythonpath", "testpaths"):
            if k in ini:
                ini[k] = " ".join(posixpath.normpath(posixpath.join(cwd, part)) for part in str(ini[k]).split())
        return ini

    def ini_text(self, cwd: str) -> str:
        ini = self._ini_values(cwd)
        lines = ["[pytest]", "addopts ="]
        for k in sorted(ini):
            lines.append(f"{k} = {ini[k]}")
        return "\n".join(lines) + "\n"

    def _engine_options(self, cwd: str, junit: str) -> list[str]:
        ini = self._ini_values(cwd)
        opts = ["-c", self._ini_path(cwd), f"--rootdir={cwd}", "-o", "addopts="]
        for k in ("python_files", "testpaths", "pythonpath"):
            if k in ini:
                opts += ["-o", f"{k}={ini[k]}"]
        opts.append(f"--junitxml={junit}")
        return opts

    def _ensure_ini(self, cwd: str) -> None:
        if cwd in self._ini_written or not self.verified:
            return
        mk = self.sandbox.exec_argv(["mkdir", "-p", "--", self.engine_dir], cwd=WORKSPACE, timeout=30)
        if mk.exit_code != 0:
            raise RunnerRefused("could not create the engine directory in the sandbox")
        self.sandbox.put_bytes(self._ini_path(cwd), self.ini_text(cwd).encode("utf-8"))
        self._ini_written.add(cwd)

    # --- execution ----------------------------------------------------------------------------------------------------

    def _exec(self, argv: list[str], cwd: Optional[str] = None) -> TestRun:
        env = dict(self.seed.get("service_env") or {})
        cwd = cwd or self.cwd
        r: ExecResult = self.sandbox.exec_argv(argv, cwd=cwd, env=env, timeout=self.cmd_timeout_s)
        text = r.stdout.decode("utf-8", "replace") + ("\n--- stderr ---\n" + r.stderr.decode("utf-8", "replace") if r.stderr else "")
        return TestRun(argv=list(argv), exit=r.exit_code, output=text,
                       output_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                       timed_out=r.timed_out, truncated=r.truncated, cwd=cwd)

    def _collect(self, cwd: str, target: Optional[str]) -> tuple[Optional[int], TestRun]:
        junit = f"{self.engine_dir}/collect-{secrets.token_hex(6)}.xml"
        argv = list(self.fw["collect"]) + self._engine_options(cwd, junit) + ([target] if target else [])
        t = self._exec(argv, cwd)
        if t.timed_out or t.truncated or t.exit not in (0, 5):
            return None, t
        return parsers.parse_collected(t.output), t

    def _verified_run(self, base_argv: list[str], cwd: str, target: Optional[str]) -> TestRun:
        """A pytest run with the engine's configuration; the verdict from junit + collect-only + summary + exit."""
        self._ensure_ini(cwd)
        junit = f"{self.engine_dir}/run-{secrets.token_hex(6)}.xml"
        argv = list(base_argv)
        # the target stays last; the engine options go before it
        if target is not None and argv and argv[-1] == target:
            argv = argv[:-1] + self._engine_options(cwd, junit) + [target]
        else:
            argv = argv + self._engine_options(cwd, junit)
        t = self._exec(argv, cwd)
        xml = None
        data = self.sandbox.get_bytes(junit) if not t.timed_out else None
        if data is not None:
            xml = data.decode("utf-8", "replace")
            t.junit_sha256 = hashlib.sha256(data).hexdigest()
        collected, ct = self._collect(cwd, target)
        t.collected = collected
        t.extra["collect_exit"] = ct.exit
        t.extra["collect_output_sha256"] = ct.output_sha256
        t.counts = parsers.verified_counts(junit_xml=xml, output=t.output, collected=collected, exit_code=t.exit,
                                           timed_out=t.timed_out, truncated=t.truncated)
        t.verdict = t.counts.verdict_for(target) if target else ("pass" if t.counts.ok and not t.counts.failed_names else
                                                                 ("fail" if t.counts.ok else "unknown"))
        self.sandbox.exec_argv(["rm", "-f", "--", junit], cwd=WORKSPACE, timeout=20)
        return t

    def run_test(self, target: str, cwd: Optional[str] = None) -> TestRun:
        argv = self.test_argv(target)
        if self.verified:
            return self._verified_run(argv, cwd or self.cwd, target)
        t = self._exec(argv, cwd)
        t.counts = parsers.parse_counts(self.fw["parser"], t.output)
        t.verdict = "unknown"
        return t

    def run_suite(self, cwd: Optional[str] = None) -> TestRun:
        if self.verified:
            return self._verified_run(self.suite_argv(), cwd or self.cwd, None)
        t = self._exec(self.suite_argv(), cwd)
        t.counts = parsers.parse_counts(self.fw["parser"], t.output)
        t.verdict = "unknown"
        return t

    def reproduction_target(self, finding: dict) -> Optional[str]:
        """R3: the finding's own reproduction as a seeded argv target, or None (not machine-runnable)."""
        return node_id_in_text(finding.get("reproduction") or "")

    # --- verification checkouts (R1) ----------------------------------------------------------------------------------

    def checkout_dir(self, tag: str) -> str:
        return f"{VERIFY_DIR}/{tag}-{secrets.token_hex(6)}/services/{self.service}"

    # --- path classes -------------------------------------------------------------------------------------------------

    def _rel(self, path: str) -> str:
        prefix = f"services/{self.service}/"
        return path[len(prefix):] if path.startswith(prefix) else path

    @staticmethod
    def _match(rel: str, g: str) -> bool:
        """fnmatch where ``**/`` also matches zero directories (``tests/**/*.rs`` matches ``tests/it.rs``)."""
        return fnmatch.fnmatch(rel, g) or ("**/" in g and fnmatch.fnmatch(rel, g.replace("**/", "")))

    def is_test_infra_path(self, path: str) -> bool:
        rel = self._rel(path)
        for g in self.fw.get("test_infra_globs", []):
            if self._match(rel, g) or fnmatch.fnmatch(posixpath.basename(rel), g):
                return True
        return False

    def is_test_path(self, path: str) -> bool:
        rel = self._rel(path)
        if self.is_test_infra_path(path):
            return False
        for g in self.fw.get("test_file_globs", []):
            if self._match(rel, g) or ("/" not in g and fnmatch.fnmatch(os.path.basename(rel), g)):
                return True
        return False

    def classify_paths(self, paths: list[str]) -> dict[str, list[str]]:
        """Every changed path into ``src`` / ``test`` / ``test_infra`` (R1)."""
        out: dict[str, list[str]] = {"src": [], "test": [], "test_infra": []}
        for p in sorted(set(paths)):
            if self.is_test_infra_path(p):
                out["test_infra"].append(p)
            elif self.is_test_path(p):
                out["test"].append(p)
            else:
                out["src"].append(p)
        return out
