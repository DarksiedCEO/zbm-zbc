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
``/mnt/user-data/workspace/.dlv-engine/``, ``addopts`` empty), ``--rootdir=.`` (the service directory the engine runs in, as the process resolves it: the physical path; wave 21, N20-D-4), ``-o`` overrides for
``python_files``/``testpaths``/``pythonpath`` from the seed and ``-p no:cacheprovider``; the repository's
``pytest.ini``/``pyproject``/``setup.cfg``/``tox.ini`` are never read. Report files are read back with ``docker cp``
(the daemon, not a process in the box). cargo builds into an engine-owned ``--target-dir`` under the engine directory,
never the service's ``target/``.

Round 19 (R1, R4): a ``TestRunner`` holds no sandbox — every run takes the FRESH engine container it runs in
(``run_test(box, …)`` / ``run_suite(box, …)``): the engine directory, the ini and the engine-owned pytest plugin
(``adapters/tools/zbm_engine_plugin.py``, hash pinned here) are written into that container by the engine before the run,
and nothing the agent's process could have left behind exists there. pytest runs with ``--disable-plugin-autoload``
and ``-p zbm_engine_plugin`` (the engine directory first on ``pythonpath`` so nothing in the tree shadows it); the
plugin's record (``<junitxml>.zbm.json``) must agree with the junit file case by case or the result is ``unknown``.

Residual (accepted, ADR 0011): a test process runs as the same uid as the engine's files in the (engine-owned)
container; the cross-checks make forging expensive (a second process's listing, per-line multiplicities,
package/binary results, the exit code and — for pytest — the plugin's four-position record must all be forged
together; per-ecosystem content rules in the seed refuse the cheap routes).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import re
import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional

from zbm_delivery.engine import parsers, toolchains
from zbm_delivery.policy import WORKSPACE
from zbm_delivery.ports import ExecResult

_TARGET_RE = re.compile(r"^[A-Za-z0-9_./\-]+::[A-Za-z_][A-Za-z0-9_:]*(?:\[[^\]\n]{1,120}\])?$")
_EXTS = r"(?:py|rs|go|ts|mts|cts|js|mjs|cjs)"
_NODE_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_./\-])((?:tests?/)?[A-Za-z0-9_./\-]+\." + _EXTS +
                           r"::[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*(?:\[[^\]\n]{1,120}\])?)")
ENGINE_DIR = f"{WORKSPACE}/.dlv-engine"
PLUGIN_NAME = "zbm_engine_plugin"
PLUGIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters", "tools", f"{PLUGIN_NAME}.py")
PLUGIN_SHA256 = "05ab874b93ce2a69b543c580bce4efd8ffdc22622552d43ac938f5e67040d845"
# wave 22 (G1(b), N21-D-1): the runner-independent re-execution of a finding's reproduction (read-only at /mnt/dlv)
STANDALONE_NAME = "zbm_standalone_runner.py"
STANDALONE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapters", "tools", STANDALONE_NAME)
STANDALONE_SHA256 = "40c50dea1ce106c49021c18cb034c2ccc8a7fb745c5c0300fa73ded0d6eeb5b8"
STANDALONE_EXIT = {0: "pass", 1: "fail", 3: "runner_dependent"}
# the names a scrubbed-environment re-run unsets for the toolchains without a standalone runner (go, cargo, node):
# every name the container env file can carry that says "a CI/test run", plus the common CI markers
SCRUB_ENV_NAMES = ("CI", "CONTINUOUS_INTEGRATION", "BUILD_NUMBER", "RUN_ID", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE",
                   "JENKINS_URL", "TEAMCITY_VERSION", "TF_BUILD", "RUST_TEST_THREADS", "RUST_TEST_NOCAPTURE", "GOFLAGS")


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
    verdicts: dict = field(default_factory=dict)   # per target, for a multi-target run


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


# wave 21 (R1, N20-D-3): the source files each seeded ecosystem runs tests from (a reproduction must name one)
REPRO_SUFFIXES = {"pytest": (".py",), "cargo": (".rs",), "go": ("_test.go",),
                  "npm": (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs")}


def detect_framework(seed: dict, exists: Callable[[str], bool]) -> Optional[str]:
    """The seeded framework whose marker files exist (``exists(<path relative to the service directory>)``), in
    seed order, else None (D7). ``TestRunner.detect`` is this over the worktree's service directory."""
    for name, fw in seed["frameworks"].items():
        if any(exists(m) for m in fw["markers"]):
            if all(exists(r) for r in fw.get("requires", [])):
                return name
    return None


def reproduction_problem(seed: dict, text: str, exists: Callable[[str], bool],
                         read: Callable[[str], Optional[str]]) -> tuple[Optional[str], Optional[str]]:
    """``(node id, None)`` when the finding's ``reproduction`` text names a test the service's toolchain can run
    on the base tree, else ``(node id or None, why not)`` (wave 21, R1: a prose reproduction is refused at
    ingestion). ``exists``/``read`` see the BASE commit's service directory. Resolvable means: a ``<path>::<name>``
    node id in the text; a seeded framework detected at base; the path a source file of that ecosystem's test
    runner (``REPRO_SUFFIXES``; not test infrastructure), present at base; and the test's own name (the last ``::``
    part, without a ``[param]``) appearing as an identifier in that file. That is what the seeded ``{target}``
    argv selects (``engine/toolchains.py``); whether the test FAILS on base is the engine's RED/reverted runs."""
    node = node_id_in_text(text)
    if node is None:
        return None, "the reproduction names no test node id (<path>::<name> relative to the service directory)"
    if not _TARGET_RE.fullmatch(node) or ".." in node.split("/") or node.startswith("/"):
        return node, "the node id is not a plain <path>::<name> relative to the service directory"
    fw_name = detect_framework(seed, exists)
    if fw_name is None:
        return node, "no seeded test framework matches the service directory at the base commit"
    fw = seed["frameworks"][fw_name]
    path, _, name = node.partition("::")
    if not path.endswith(REPRO_SUFFIXES.get(fw_name, ())):
        return node, f"{path} is not a {fw_name} test source ({', '.join(REPRO_SUFFIXES.get(fw_name, ()))})"
    base = posixpath.basename(path)
    if any(TestRunner._match(path, g) or fnmatch.fnmatch(base, g) for g in fw.get("test_infra_globs", [])):
        return node, f"{path} is test infrastructure, not a test"
    body = read(path)
    if body is None:
        return node, f"{path} is not a file of the base commit"
    func = name.split("[", 1)[0].rsplit("::", 1)[-1]
    if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(func) + r"(?![A-Za-z0-9_])", body):
        return node, f"{func} does not occur in {path} at the base commit"
    return node, None


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

    def __init__(self, test_seed: dict, service: str, worktree_path: str, cmd_timeout_s: int):
        self.seed = test_seed
        self.service = service
        self.worktree = worktree_path
        self.cmd_timeout_s = cmd_timeout_s
        service_dir = os.path.join(worktree_path, "services", service)
        self.framework = self.detect(test_seed, service_dir)
        self.cwd = f"{WORKSPACE}/services/{service}"
        self.engine_dir = f"{ENGINE_DIR}/{secrets.token_hex(8)}"
        self._prepared: set[str] = set()          # container names whose engine directory is in place
        self.toolchain = self._toolchain(service_dir)

    @staticmethod
    def detect(seed: dict, service_dir: str) -> str:
        """Framework by marker file inside the service directory (D7); ``npm`` needs the lockfile."""
        name = detect_framework(seed, lambda rel: os.path.exists(os.path.join(service_dir, rel)))
        if name is None:
            raise RunnerRefused("no seeded test framework matches the service directory (D7: nothing else is run)")
        return name

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
        """The seed's ini values with ``pythonpath`` made ABSOLUTE under ``cwd`` (pytest resolves that ``paths``-typed
        value against the ini file's directory, which is the engine directory, never the service); the engine
        directory comes FIRST on ``pythonpath`` so ``-p zbm_engine_plugin`` can only resolve to the engine's copy (R4).
        ``testpaths`` stays RELATIVE (wave 21, N20-D-4): it is an ``args``-typed value pytest globs against the
        process's working directory — the service directory, as the process resolves it — and uses only when that
        directory is the rootdir (``--rootdir=.``). Made absolute, a working directory reached through a symlink gave
        collected paths outside the rootdir (``../../<link>/...`` node ids) and every suite verdict was unknown."""
        ini = dict(self.fw.get("ini") or {})
        if "pythonpath" in ini:
            ini["pythonpath"] = " ".join(posixpath.normpath(posixpath.join(cwd, part)) for part in str(ini["pythonpath"]).split())
        if "testpaths" in ini:
            parts = str(ini["testpaths"]).split()
            if any(p.startswith("/") or ".." in p.split("/") for p in parts):
                raise RunnerRefused("seed testpaths must be relative to the service directory")
            ini["testpaths"] = " ".join(parts)
        ini["pythonpath"] = (self.engine_dir + " " + ini["pythonpath"]).strip() if ini.get("pythonpath") else self.engine_dir
        return ini

    def ini_text(self, cwd: str) -> str:
        ini = self._ini_values(cwd)
        lines = ["[pytest]", "addopts ="]
        for k in sorted(ini):
            lines.append(f"{k} = {ini[k]}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def plugin_bytes() -> bytes:
        """The engine-owned pytest plugin, verified against its pin (R4: a modified plugin never ships)."""
        with open(PLUGIN_PATH, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != PLUGIN_SHA256:
            raise RunnerRefused("engine/zbm_engine_plugin.py does not match its pinned hash (R4)")
        return data

    def prepare_box(self, box, cwd: str) -> None:
        """Put the engine directory, the ini and the pytest plugin into a FRESH engine container (once per box)."""
        if not self.verified or box.container in self._prepared:
            return
        mk = box.exec_argv(["mkdir", "-p", "--", self.engine_dir], cwd=WORKSPACE, timeout=30)
        if mk.exit_code != 0:
            raise RunnerRefused("could not create the engine directory in the engine container")
        if self.framework == "pytest":
            box.put_bytes(self._ini_path(cwd), self.ini_text(cwd).encode("utf-8"), contained=False)
            box.put_bytes(f"{self.engine_dir}/{PLUGIN_NAME}.py", self.plugin_bytes(), contained=False)
        self._prepared.add(box.container)

    # --- execution ----------------------------------------------------------------------------------------------------

    def _exec(self, box, argv: list[str], cwd: Optional[str] = None) -> TestRun:
        env = dict(self.seed.get("service_env") or {})
        cwd = cwd or self.cwd
        r: ExecResult = box.exec_argv(argv, cwd=cwd, env=env, timeout=self.cmd_timeout_s)
        text = r.stdout.decode("utf-8", "replace") + (toolchains.STDERR_MARK + r.stderr.decode("utf-8", "replace") if r.stderr else "")
        return TestRun(argv=list(argv), exit=r.exit_code, output=text,
                       output_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                       timed_out=r.timed_out, truncated=r.truncated, cwd=cwd)

    def _collect(self, box, cwd: str, targets: list[str], prefix: tuple = ()) -> tuple[Optional[toolchains.Listing], Optional[TestRun]]:
        """The ecosystem's collect-only equivalent, run separately (None for an ecosystem without one)."""
        argv = self.toolchain.collect_argv(cwd, targets[0] if len(targets) == 1 else None)
        if argv is None:
            return None, None
        if len(targets) > 1:
            argv = argv + list(targets)
        t = self._exec(box, list(prefix) + argv, cwd)
        return self.toolchain.parse_listing(t.output, t.exit, t.timed_out, t.truncated), t

    def _verified_run(self, box, base_argv: list[str], cwd: str, targets: list[str], prefix: tuple = ()) -> TestRun:
        """One run with the engine's configuration in ``box``; the verdict from the report + listing + transcript
        + exit (+ the plugin record for pytest). ``targets``: none for the suite, one for RED/GREEN/checkouts, several
        for the src-only check (every one gets its own verdict in ``TestRun.verdicts``)."""
        tc = self.toolchain
        self.prepare_box(box, cwd)
        report = f"{self.engine_dir}/run-{secrets.token_hex(6)}.{tc.report_ext}" if tc.report_ext else None
        argv = tc.run_argv(base_argv, cwd, report, targets[0] if len(targets) == 1 else None)
        if len(targets) > 1:
            argv = argv + list(targets)
        tc.prepare(box, cwd)
        t = self._exec(box, list(prefix) + argv, cwd)
        report_text = None
        extra_text = None
        if report is not None:
            data = box.get_bytes(report) if not t.timed_out else None
            if data is not None:
                report_text = data.decode("utf-8", "replace")
                t.junit_sha256 = hashlib.sha256(data).hexdigest()
            if tc.record_suffix and not t.timed_out:
                rec = box.get_bytes(report + tc.record_suffix)
                if rec is not None:
                    extra_text = rec.decode("utf-8", "replace")
                    t.extra["plugin_record_sha256"] = hashlib.sha256(rec).hexdigest()
        listing, ct = self._collect(box, cwd, targets, prefix)
        if ct is not None:
            t.extra["collect_exit"] = ct.exit
            t.extra["collect_output_sha256"] = ct.output_sha256
        t.counts = tc.verify(report=report_text, output=t.output, listing=listing, exit_code=t.exit,
                             timed_out=t.timed_out, truncated=t.truncated, record=extra_text,
                             engine=self.engine_dir)
        t.collected = t.counts.collected if t.counts.collected is not None else (listing.total if listing else None)
        if targets:
            for tg in targets:
                t.verdicts[tg] = t.counts.verdict_for(tc.case_key(tg), tc.case_under)
            t.verdict = t.verdicts[targets[0]] if len(targets) == 1 else (
                "pass" if all(v == "pass" for v in t.verdicts.values()) else
                ("fail" if any(v == "fail" for v in t.verdicts.values()) and not any(v == "unknown" for v in t.verdicts.values()) else "unknown"))
        else:
            t.verdict = "pass" if t.counts.ok and not t.counts.failed_names else ("fail" if t.counts.ok else "unknown")
        return t

    def run_test(self, box, target: str, cwd: Optional[str] = None) -> TestRun:
        argv = self.test_argv(target)
        if self.verified:
            return self._verified_run(box, argv, cwd or self.cwd, [target])
        t = self._exec(box, argv, cwd)
        t.counts = parsers.parse_counts(self.fw["parser"], t.output)
        t.verdict = "unknown"
        return t

    def run_tests(self, box, targets: list[str], cwd: Optional[str] = None) -> TestRun:
        """Several targets in ONE run (the src-only check): the seeded targeted argv with every target appended."""
        for tg in targets:
            self.check_target(tg)
        if not targets:
            raise RunnerRefused("no targets")
        if len(targets) == 1:
            return self.run_test(box, targets[0], cwd)
        if not self.verified or self.framework != "pytest":
            raise RunnerRefused("multi-target runs are built for pytest only")
        argv = self.test_argv(targets[0])
        return self._verified_run(box, argv[:-1], cwd or self.cwd, targets)

    # --- runner-independent re-execution (wave 22, G1(b)) -----------------------------------------------------------

    @staticmethod
    def standalone_bytes() -> bytes:
        """The standalone runner, verified against its pin (a modified runner never runs)."""
        with open(STANDALONE_PATH, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != STANDALONE_SHA256:
            raise RunnerRefused("adapters/tools/zbm_standalone_runner.py does not match its pinned hash (G1)")
        return data

    def _standalone_paths(self, cwd: str) -> list[str]:
        """The seed's ``pythonpath`` entries made absolute under ``cwd`` (never the engine directory)."""
        raw = str((self.fw.get("ini") or {}).get("pythonpath") or "")
        return [posixpath.normpath(posixpath.join(cwd, part)) for part in raw.split()]

    def run_standalone(self, box, target: str, cwd: Optional[str] = None) -> TestRun:
        """G1(b): run the test function ``target`` OUTSIDE pytest in ``box`` (a fresh engine container): the pinned
        standalone runner from the read-only tools mount, ``python3 -I``, pytest not importable, CI/PYTEST*/TEST*
        scrubbed from the environment, the request (with a nonce) on stdin. The verdict comes from the runner's
        report file (read back with ``docker cp``) and must agree with the exit code: ``pass`` / ``fail`` /
        ``runner_dependent``; anything else is ``unknown``. pytest services only (``RunnerRefused`` otherwise)."""
        if self.framework != "pytest" or not self.verified:
            raise RunnerRefused("the standalone runner is built for pytest services")
        self.check_target(target)
        cwd = cwd or self.cwd
        self.standalone_bytes()                                  # the pin, before anything runs
        self.prepare_box(box, cwd)
        from zbm_delivery.adapters.sandbox import TOOLS_MOUNT
        script = f"{TOOLS_MOUNT}/{STANDALONE_NAME}"
        nonce = secrets.token_hex(16)
        report = f"{self.engine_dir}/solo-{secrets.token_hex(8)}.json"
        path, _, name = target.partition("::")
        request = {"nonce": nonce, "report": report, "service_dir": cwd, "paths": self._standalone_paths(cwd),
                   "test_file": posixpath.join(cwd, path), "test": name}
        argv = ["python3", "-I", script]
        r: ExecResult = box.exec_argv(argv, cwd=cwd, env={}, timeout=self.cmd_timeout_s,
                                      stdin=json.dumps(request).encode("utf-8"))
        text = r.stdout.decode("utf-8", "replace") + (toolchains.STDERR_MARK + r.stderr.decode("utf-8", "replace") if r.stderr else "")
        t = TestRun(argv=argv + [target], exit=r.exit_code, output=text,
                    output_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                    timed_out=r.timed_out, truncated=r.truncated, cwd=cwd)
        data = box.get_bytes(report) if not r.timed_out else None
        rec: dict = {}
        if data is not None:
            t.output += "\n[engine: the standalone runner's report] " + data.decode("utf-8", "replace")[:4000] + "\n"
            t.output_sha256 = hashlib.sha256(t.output.encode("utf-8", "surrogatepass")).hexdigest()
            t.junit_sha256 = hashlib.sha256(data).hexdigest()
            try:
                rec = json.loads(data.decode("utf-8"))
            except ValueError:
                rec = {}
        claimed = rec.get("verdict") if isinstance(rec, dict) else None
        agree = (isinstance(rec, dict) and rec.get("nonce") == nonce and claimed in STANDALONE_EXIT.values()
                 and STANDALONE_EXIT.get(r.exit_code) == claimed and not r.timed_out and not r.truncated)
        t.verdict = claimed if agree else "unknown"
        why = str(rec.get("why") or "") if isinstance(rec, dict) else ""
        t.extra["standalone"] = {"why": (why if agree else f"no agreeing report (exit {r.exit_code}, report "
                                                              f"{'present' if data is not None else 'missing'})")[:400],
                                 "conftest": list(rec.get("conftest") or [])[:10] if agree else [],
                                 "blocked_imports": list(rec.get("blocked_imports") or [])[:10] if agree else [],
                                 # wave 23 (D3): where each refused pytest import came from (test / src / lib side)
                                 "blocked_from": [{"name": str(b.get("name", ""))[:80], "side": str(b.get("side", "")),
                                                   "file": str(b.get("file", ""))[:200]}
                                                  for b in (rec.get("blocked_from") or [])[:10] if isinstance(b, dict)]
                                                 if agree else [],
                                 "report_sha256": t.junit_sha256}
        return t

    def run_scrubbed(self, box, target: str, cwd: Optional[str] = None) -> TestRun:
        """G1(b) for go/cargo/node (no standalone runner exists): the seeded targeted run through the verified
        toolchain with ``SCRUB_ENV_NAMES`` unset (``env -u``). The same test runner runs, so this defeats only an
        environment-conditional fix; the toolchain's own detection hooks (Go ``testing.Testing()``, Node's
        ``NODE_TEST_CONTEXT``, the libtest harness's arguments) remain — ``src_content_deny`` refuses their cheap
        spellings (the residual, stated in ADR 0011)."""
        if self.framework == "pytest":
            raise RunnerRefused("pytest services use the standalone runner")
        prefix = ["env"] + [x for n in SCRUB_ENV_NAMES for x in ("-u", n)]
        argv = self.test_argv(target)
        if self.verified:
            return self._verified_run(box, argv, cwd or self.cwd, [target], prefix=prefix)
        t = self._exec(box, prefix + argv, cwd)
        t.counts = parsers.parse_counts(self.fw["parser"], t.output)
        t.verdict = "unknown"
        return t

    def run_suite(self, box, cwd: Optional[str] = None) -> TestRun:
        if self.verified:
            return self._verified_run(box, self.suite_argv(), cwd or self.cwd, [])
        t = self._exec(box, self.suite_argv(), cwd)
        t.counts = parsers.parse_counts(self.fw["parser"], t.output)
        t.verdict = "unknown"
        return t

    def target_for_case(self, case_key: str) -> Optional[str]:
        """The ``<path>::<name>`` target that re-runs one verified case key, or None when the ecosystem's key does
        not name a file the seeded argv can select (pytest: the node id itself; go: ``<dir>::<Test>`` → a file in
        that package directory; cargo/node: the bare name cannot be mapped back to a file — None, stated)."""
        if self.framework == "pytest":
            path = case_key.split("::", 1)[0]
            return case_key if "::" in case_key and _TARGET_RE.fullmatch(case_key.split("[", 1)[0]) and not path.startswith("/") else None
        if self.framework == "go" and "::" in case_key:
            d, name = case_key.split("::", 1)
            if "/" in name:
                return None                                  # a sub-test: its parent is the listed test
            return f"{d}/{name}_test.go::{name}" if d != "." else f"{name}_test.go::{name}"
        return None

    def reproduction_target(self, finding: dict) -> Optional[str]:
        """R3: the finding's own reproduction as a seeded argv target, or None (not machine-runnable)."""
        return node_id_in_text(finding.get("reproduction") or "")

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

    def denied_src_content(self, text: str) -> Optional[str]:
        """The first seeded ``src_content_deny`` rule a changed SOURCE file matches (R4, cheap layer: a source
        module that reaches for pytest's plugin manager or walks the heap for it)."""
        for rule in self.fw.get("src_content_deny", []):
            if re.search(rule["pattern"], text):
                return rule["name"]
        return None

    def denied_test_content(self, text: str) -> Optional[str]:
        """The first seeded ``test_content_deny`` rule (regex, per ecosystem) a test file's text matches, or None:
        the cheap structural refusals of the routes a test process could use to forge the ecosystem's transcript
        (a Go ``TestMain``/``os.Exit``/test2json ``\\x16`` frame; a Rust ``process::exit``/raw fd write; a Node
        ``process.exit``/serialized frame)."""
        for rule in self.fw.get("test_content_deny", []):
            if re.search(rule["pattern"], text):
                return rule["name"]
        return None
