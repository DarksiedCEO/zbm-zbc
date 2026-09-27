"""
The engine's own test execution (spec §C.8.4 steps 2, 4, 5; D7; SP-03; round 18 R1/R2): ``TestRunner`` runs the
seeded argv (no shell) inside the run's sandbox from a service directory, captures exit code and output, and computes
the verdict from artefacts the engine controls. Nothing the agent prints is ever a count.

Every seeded framework has an engine-owned verdict path (``engine/toolchains.py``): pytest (``--junitxml`` to an
engine path + ``--collect-only`` + summary + exit), go (``go test -json`` events + ``go test -json -list`` + package
results + exit), cargo (per-test lines + ``-- --list`` + per-binary result lines + exit, stable toolchain) and
node (``--test-reporter=junit`` to an engine path + the TAP stream + exit; Node has no collect-only). Any
disagreement is ``unknown``; ``unknown`` is never green and never a valid RED. A framework whose seed says
``verified: false`` (none shipped) stays ``unknown`` by construction.

pytest detail: every invocation carries ``-c <engine ini>`` (written by the engine under
``/mnt/user-data/workspace/.dlv-engine/``, ``addopts`` empty), ``--rootdir=<service dir>``, ``-o`` overrides for
``python_files``/``testpaths``/``pythonpath`` from the seed and ``-p no:cacheprovider``; the repository's
``pytest.ini``/``pyproject``/``setup.cfg``/``tox.ini`` are never read. Report files are read back with ``docker cp``
(the daemon, not a process in the box). cargo builds into an engine-owned ``--target-dir`` under the engine directory,
never the service's ``target/``.

Residual (accepted, ADR 0011): a test process runs as the same uid in the same container as the engine's files; the
cross-checks make forging expensive (a second process's listing, per-line multiplicities, package/binary results and
the exit code must all be forged together; per-ecosystem content rules in the seed refuse the cheap routes), and the
verification checkout (R1) runs the RED test file alone on base + src, where no other agent file exists.
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

from zbm_delivery.engine import parsers, toolchains
from zbm_delivery.policy import WORKSPACE
from zbm_delivery.ports import ExecResult

_TARGET_RE = re.compile(r"^[A-Za-z0-9_./\-]+::[A-Za-z_][A-Za-z0-9_:]*(?:\[[^\]\n]{1,120}\])?$")
_EXTS = r"(?:py|rs|go|ts|mts|cts|js|mjs|cjs)"
_NODE_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_./\-])((?:tests?/)?[A-Za-z0-9_./\-]+\." + _EXTS +
                           r"::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*(?:\[[^\]\n]{1,120}\])?)")
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
    junit_sha256: Optional[str] = None       # sha256 of the engine-read report file (junit for pytest/node)
    collected: Optional[int] = None
    cwd: str = ""
    extra: dict = field(default_factory=dict)


def node_id_in_text(text: str) -> Optional[str]:
    """The first test target (``<path>::<name>``, any seeded ecosystem's source extension) named in a finding's
    free text (R3: the finding's own reproduction), or None."""
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


def same_test(failed_name: str, target: str) -> bool:
    """Whether a failure name from a verified count set names the test ``<path>::<name>`` (a finding's
    reproduction): pytest keys are the node id itself; cargo keys are the libtest name alone; go keys are
    ``<pkg dir>::<TestName>``; node keys are the name, ``suite > name`` or ``name #n``."""
    path, _, name = target.partition("::")
    if not name:
        return False
    d = posixpath.dirname(path) or "."
    return (failed_name == target or failed_name == name or failed_name == f"{d}::{name}"
            or failed_name.endswith(" > " + name) or failed_name.startswith(name + " #")
            or failed_name.startswith(target + "[") or failed_name.startswith(target + "::"))


class TestRunner:  # noqa: N801
    __test__ = False  # not a pytest collection target

    def __init__(self, test_seed: dict, service: str, sandbox, worktree_path: str, cmd_timeout_s: int):
        self.seed = test_seed
        self.service = service
        self.sandbox = sandbox
        self.worktree = worktree_path
        self.cmd_timeout_s = cmd_timeout_s
        service_dir = os.path.join(worktree_path, "services", service)
        self.framework = self.detect(test_seed, service_dir)
        self.cwd = f"{WORKSPACE}/services/{service}"
        self.engine_dir = f"{ENGINE_DIR}/{secrets.token_hex(8)}"
        self._ini_written: set[str] = set()
        self._engine_dir_made = False
        self.toolchain = self._toolchain(service_dir)

    @staticmethod
    def detect(seed: dict, service_dir: str) -> str:
        """Framework by marker file inside the service directory (D7); ``npm`` needs the lockfile."""
        for name, fw in seed["frameworks"].items():
            if any(os.path.exists(os.path.join(service_dir, m)) for m in fw["markers"]):
                if all(os.path.exists(os.path.join(service_dir, r)) for r in fw.get("requires", [])):
                    return name
        raise RunnerRefused("no seeded test framework matches the service directory (D7: nothing else is run)")

    def _toolchain(self, service_dir: str) -> Optional[toolchains.Toolchain]:
        if not self.fw.get("verified"):
            return None
        if self.framework == "pytest":
            return toolchains.PytestToolchain(self.fw, service_dir, self._ini_path, self._ini_values)
        if self.framework == "go":
            return toolchains.GoToolchain(self.fw, service_dir)
        if self.framework == "cargo":
            return toolchains.CargoToolchain(self.fw, service_dir, self.engine_dir)
        if self.framework == "npm":
            return toolchains.NodeToolchain(self.fw, service_dir)
        raise RunnerRefused(f"seed marks {self.framework} verified but the engine has no adapter for it")

    @property
    def fw(self) -> dict:
        return self.seed["frameworks"][self.framework]

    @property
    def verified(self) -> bool:
        return self.toolchain is not None

    # --- argv -------------------------------------------------------------------------------------------------------

    def check_target(self, target: str) -> str:
        if not _TARGET_RE.fullmatch(target) or ".." in target.split("/") or target.startswith("/"):
            raise RunnerRefused("test target must be <path>::<name> relative to the service directory")
        return target

    def target_tokens(self, target: str) -> list[str]:
        return self.toolchain.target_tokens(target) if self.toolchain is not None else [target]

    def test_argv(self, target: str) -> list[str]:
        self.check_target(target)
        out: list[str] = []
        for a in self.fw["test"]:
            out += self.target_tokens(target) if a == "{target}" else [a]
        return out

    def example_test_argv(self) -> list[str]:
        """The targeted argv shown in the brief, with the seed's example target expanded (per ecosystem)."""
        example = self.fw.get("target_example") or "tests/test_<file>.py::test_<name>"
        out: list[str] = []
        for a in self.fw["test"]:
            out += (self.toolchain.target_tokens(example) if self.toolchain is not None else [example]) if a == "{target}" else [a]
        return out

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

    def _ensure_engine_dir(self) -> None:
        if self._engine_dir_made or not self.verified:
            return
        mk = self.sandbox.exec_argv(["mkdir", "-p", "--", self.engine_dir], cwd=WORKSPACE, timeout=30)
        if mk.exit_code != 0:
            raise RunnerRefused("could not create the engine directory in the sandbox")
        self._engine_dir_made = True

    def _ensure_ini(self, cwd: str) -> None:
        self._ensure_engine_dir()
        if cwd in self._ini_written or self.framework != "pytest" or not self.verified:
            return
        self.sandbox.put_bytes(self._ini_path(cwd), self.ini_text(cwd).encode("utf-8"))
        self._ini_written.add(cwd)

    # --- execution ----------------------------------------------------------------------------------------------------

    def _exec(self, argv: list[str], cwd: Optional[str] = None) -> TestRun:
        env = dict(self.seed.get("service_env") or {})
        cwd = cwd or self.cwd
        r: ExecResult = self.sandbox.exec_argv(argv, cwd=cwd, env=env, timeout=self.cmd_timeout_s)
        text = r.stdout.decode("utf-8", "replace") + (toolchains.STDERR_MARK + r.stderr.decode("utf-8", "replace") if r.stderr else "")
        return TestRun(argv=list(argv), exit=r.exit_code, output=text,
                       output_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                       timed_out=r.timed_out, truncated=r.truncated, cwd=cwd)

    def _collect(self, cwd: str, target: Optional[str]) -> tuple[Optional[toolchains.Listing], Optional[TestRun]]:
        """The ecosystem's collect-only equivalent, run separately (None for an ecosystem without one)."""
        argv = self.toolchain.collect_argv(cwd, target)
        if argv is None:
            return None, None
        t = self._exec(argv, cwd)
        return self.toolchain.parse_listing(t.output, t.exit, t.timed_out, t.truncated), t

    def _verified_run(self, base_argv: list[str], cwd: str, target: Optional[str]) -> TestRun:
        """One run with the engine's configuration; the verdict from the report + listing + transcript + exit."""
        tc = self.toolchain
        self._ensure_ini(cwd)
        report = f"{self.engine_dir}/run-{secrets.token_hex(6)}.{tc.report_ext}" if tc.report_ext else None
        argv = tc.run_argv(base_argv, cwd, report, target)
        tc.prepare(self.sandbox, cwd)
        t = self._exec(argv, cwd)
        report_text = None
        if report is not None:
            data = self.sandbox.get_bytes(report) if not t.timed_out else None
            if data is not None:
                report_text = data.decode("utf-8", "replace")
                t.junit_sha256 = hashlib.sha256(data).hexdigest()
        listing, ct = self._collect(cwd, target)
        if ct is not None:
            t.extra["collect_exit"] = ct.exit
            t.extra["collect_output_sha256"] = ct.output_sha256
        t.counts = tc.verify(report=report_text, output=t.output, listing=listing, exit_code=t.exit,
                             timed_out=t.timed_out, truncated=t.truncated)
        t.collected = t.counts.collected if t.counts.collected is not None else (listing.total if listing else None)
        if target:
            t.verdict = t.counts.verdict_for(tc.case_key(target), tc.case_under)
        else:
            t.verdict = "pass" if t.counts.ok and not t.counts.failed_names else ("fail" if t.counts.ok else "unknown")
        if report is not None:
            self.sandbox.exec_argv(["rm", "-f", "--", report], cwd=WORKSPACE, timeout=20)
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

    def denied_test_content(self, text: str) -> Optional[str]:
        """The first seeded ``test_content_deny`` rule (regex, per ecosystem) a test file's text matches, or None:
        the cheap structural refusals of the routes a test process could use to forge the ecosystem's transcript
        (a Go ``TestMain``/``os.Exit``/test2json ``\\x16`` frame; a Rust ``process::exit``/raw fd write; a Node
        ``process.exit``/serialized frame)."""
        for rule in self.fw.get("test_content_deny", []):
            if re.search(rule["pattern"], text):
                return rule["name"]
        return None
