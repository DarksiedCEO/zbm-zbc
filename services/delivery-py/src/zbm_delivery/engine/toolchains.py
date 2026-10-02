"""
Per-ecosystem result adapters for the engine's verdict pipeline (spec §C.8.4 steps 2, 4, 5; D7; round 18 R2; the
cargo/go/npm limitation of ADR 0011 removed). One ``Toolchain`` per seeded framework turns the SEEDED argv into the
engine's own invocation, runs the ecosystem's collect-only equivalent, and computes ``Counts`` from artefacts the
engine controls. The discipline is the pytest one: nothing the agent's process prints is ever a count on its own —
every number must agree with a second, independent source or the result is ``unknown``.

- **pytest** (unchanged from wave 19): ``--junitxml`` to an engine path, cross-checked against ``--collect-only -q``,
  the summary line and the exit code.
- **go**: ``go test -json`` events (``pass``/``fail``/``skip`` per package+test, framed by ``test2json`` — a test's
  own prints arrive as ``output`` events, never as results) cross-checked against ``go test -json -list`` (the names
  the test binaries themselves enumerate, per package), the package-level events and the exit code. Exactly one
  terminal event per listed ``Test*`` function; a ``build-fail``, a package that fails with no failed test (a panic
  outside a test) or a name with an event but no listing is ``unknown``.
- **cargo**: stable toolchain only (``-Z unstable-options --format json`` needs nightly and is not used). ``cargo
  test --locked --offline --no-fail-fast`` per-test lines ``test <name> ... ok|FAILED|ignored`` cross-checked against
  ``cargo test … -- --list`` (every ``<name>: test`` line, per test binary, doc-tests included), the ``running N
  tests`` and ``test result: …`` lines of EVERY binary (their number must equal the number of binaries the listing
  saw) and the exit code (0 ⇔ no failure, else 101). A name printed twice (a forged line next to libtest's real
  one) is ``unknown``.
- **node** (``node --test``): ``--test-reporter=junit --test-reporter-destination=<engine path>`` (read back by the
  engine) cross-checked against the TAP reporter on stdout (``# tests/pass/fail/skipped/todo/cancelled`` and the
  top-level ``ok``/``not ok`` lines) and the exit code. Node 22 has no collect-only mechanism (no dry run, no list;
  ``--test-only`` runs ``only`` tests) — stated, not hidden: the two reporters are produced by the runner process
  from the same event stream, so the cross-check binds the file to the process's transcript and the exit code,
  not to an independent enumeration. Zero tests is ``unknown``.

The target grammar is the same for every ecosystem: ``<path>::<name>`` relative to the service directory. The
adapter maps it to the ecosystem's selector (``-run '^Name$' ./dir``; ``--test <bin> -- <name> --exact`` / ``--lib``
/ ``--bin``; ``--test-name-pattern='^name$' <file>``; the pytest node id) and to the key the parsed cases use.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Optional

from zbm_delivery.engine import parsers
from zbm_delivery.engine.parsers import Counts

_GO_TEST_NAME = re.compile(r"^Test[A-Z0-9_][A-Za-z0-9_]*$|^Test$")
_GO_LIST_LINE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\n?$")
_GO_MODULE = re.compile(r"^module\s+(?P<mod>\S+)\s*$", re.M)

_CARGO_LIST_LINE = re.compile(r"^(?P<name>\S.*?): (?P<kind>test|bench)$", re.M)
_CARGO_LIST_SUMMARY = re.compile(r"^(?P<n>[0-9]+) tests?, (?P<b>[0-9]+) benchmarks?$", re.M)
_CARGO_TEST_LINE = re.compile(r"^test (?P<name>\S.*?) \.\.\. (?P<st>ok|FAILED|ignored)(?:, .*)?$", re.M)
_CARGO_RUNNING = re.compile(r"^running (?P<n>[0-9]+) tests?$", re.M)
_CARGO_RESULT = re.compile(r"^test result: (?P<st>ok|FAILED)\. (?P<p>[0-9]+) passed; (?P<f>[0-9]+) failed; (?P<i>[0-9]+) ignored; "
                           r"(?P<m>[0-9]+) measured; (?P<fo>[0-9]+) filtered out", re.M)

_TAP_SUMMARY = re.compile(r"^# (?P<k>tests|suites|pass|fail|cancelled|skipped|todo) (?P<n>[0-9]+)$", re.M)
_TAP_TOP = re.compile(r"^(?P<st>ok|not ok) (?P<n>[0-9]+) - (?P<name>.*?)(?: # (?:SKIP|TODO).*)?$", re.M)
_TAP_PLAN = re.compile(r"^1\.\.(?P<n>[0-9]+)$", re.M)

STDERR_MARK = "\n--- stderr ---\n"


def split_streams(output: str) -> tuple[str, str]:
    """The runner joins stdout and stderr with a marker; the adapters that parse a stream need them apart."""
    if STDERR_MARK in output:
        out, err = output.split(STDERR_MARK, 1)
        return out, err
    return output, ""


@dataclass
class Listing:
    """What the collect-only equivalent enumerated: the total the run must account for, plus the structure."""
    total: int
    names: list[str] = field(default_factory=list)        # multiset of names (cargo) / case keys (go)
    by_package: dict = field(default_factory=dict)        # go: rel dir -> [names]
    binaries: int = 0                                     # cargo: number of test binaries the listing printed
    ok: bool = True
    why: str = ""


# ====================================================================== base

class Toolchain:
    name = "none"
    report_ext: Optional[str] = None       # the engine-read report file's extension, None when the ecosystem has none
    record_suffix: Optional[str] = None    # a second engine-read file next to the report (pytest: the plugin record)
    has_listing = True

    def __init__(self, fw: dict, service_dir: str):
        self.fw = fw
        self.service_dir = service_dir     # host path of services/<service> (marker files are read here, once)

    # --- targets ------------------------------------------------------------------------------------------------------

    def target_tokens(self, target: str) -> list[str]:
        """The argv tokens the seeded ``{target}`` placeholder expands to for ``<path>::<name>``."""
        return [target]

    def case_key(self, target: str) -> str:
        """The key ``Counts.cases`` uses for this target."""
        return target

    @staticmethod
    def case_under(key: str, target_key: str) -> bool:
        """Whether a case with no exact key belongs to the target (``Counts.verdict_for``)."""
        return key.startswith(target_key + "[") or key.startswith(target_key + "::")

    # --- argv ---------------------------------------------------------------------------------------------------------

    def prepare(self, sandbox, cwd: str) -> None:
        """Anything the ecosystem needs done in the box before an engine run (default nothing)."""

    def engine_options(self, cwd: str, report: Optional[str]) -> list[str]:
        return []

    def run_argv(self, base_argv: list[str], cwd: str, report: Optional[str], target: Optional[str]) -> list[str]:
        """The engine's invocation: the seeded argv (target expanded) with the engine options in the right place."""
        raise NotImplementedError

    def collect_argv(self, cwd: str, target: Optional[str]) -> Optional[list[str]]:
        return None

    def parse_listing(self, output: str, exit_code: int, timed_out: bool, truncated: bool) -> Optional[Listing]:
        return None

    # --- verdict ------------------------------------------------------------------------------------------------------

    def verify(self, *, report: Optional[str], output: str, listing: Optional[Listing], exit_code: int, timed_out: bool,
               truncated: bool, record: Optional[str] = None, engine: Optional[str] = None) -> Counts:
        raise NotImplementedError

    @staticmethod
    def _unknown(why: str, base: Optional[Counts] = None, source: str = "none") -> Counts:
        c = base if base is not None else Counts()
        c.status, c.why = "unknown", why
        if base is None:
            c.source = source
        return c


# ====================================================================== pytest (wave 19, unchanged semantics)

class PytestToolchain(Toolchain):
    name = "pytest"
    report_ext = "xml"
    record_suffix = ".zbm.json"            # written by the engine-owned plugin next to the junit file (R4)

    def __init__(self, fw: dict, service_dir: str, ini_path_for, ini_values_for):
        super().__init__(fw, service_dir)
        self._ini_path = ini_path_for
        self._ini_values = ini_values_for

    def engine_options(self, cwd: str, report: Optional[str]) -> list[str]:
        ini = self._ini_values(cwd)
        # wave 21 (N20-D-4): ``--rootdir=.`` — the process's own working directory as pytest resolves it
        # (``os.getcwd()``: the PHYSICAL path), never the path as spelled. The engine always runs pytest with its
        # working directory set to ``cwd``. With the spelled path, a cwd reached through a symlink gave pytest a
        # rootdir the collected files were not under (``../../<link>/...`` node ids in the terminal summary, other
        # ids in junit) and every verdict was ``unknown`` (fail closed, but no run could pass on such a host).
        opts = ["-c", self._ini_path(cwd), "--rootdir=.", "-o", "addopts="]
        for k in ("python_files", "testpaths", "pythonpath"):
            if k in ini:
                opts += ["-o", f"{k}={ini[k]}"]
        if report:
            opts.append(f"--junitxml={report}")
        # R4: nothing plugs in from a distribution entry point; the engine's own plugin is loaded by name and
        # resolves to the engine directory (first on pythonpath) — nothing in the tree can shadow it
        opts += ["--disable-plugin-autoload", "-p", "zbm_engine_plugin"]
        return opts

    def run_argv(self, base_argv, cwd, report, target):
        argv = list(base_argv)
        if target is not None and argv and argv[-1] == target:       # the target stays last; the options go before it
            return argv[:-1] + self.engine_options(cwd, report) + [target]
        return argv + self.engine_options(cwd, report)

    def collect_argv(self, cwd, target):
        return list(self.fw["collect"]) + self.engine_options(cwd, None) + ([target] if target else [])

    def parse_listing(self, output, exit_code, timed_out, truncated):
        if timed_out or truncated or exit_code not in (0, 5):
            return None
        n = parsers.parse_collected(output)
        return None if n is None else Listing(total=n)

    def verify(self, *, report, output, listing, exit_code, timed_out, truncated, record=None, engine=None):
        c = parsers.verified_counts(junit_xml=report, output=output, collected=listing.total if listing else None,
                                    exit_code=exit_code, timed_out=timed_out, truncated=truncated)
        if not c.ok:
            return c
        problem = parsers.plugin_record_problem(record, c, engine_dir=engine or "")
        if problem:
            c.status, c.why = "unknown", f"engine plugin record: {problem}"
        return c


# ====================================================================== go

class GoToolchain(Toolchain):
    name = "go"
    report_ext = None            # the report IS the -json event stream captured by the engine (test2json-framed)

    def __init__(self, fw: dict, service_dir: str):
        super().__init__(fw, service_dir)
        self.module = self._module_path(service_dir)

    @staticmethod
    def _module_path(service_dir: str) -> str:
        try:
            with open(posixpath.join(service_dir, "go.mod"), "r", encoding="utf-8", errors="replace") as fh:
                m = _GO_MODULE.search(fh.read())
        except OSError:
            m = None
        return m.group("mod") if m else ""

    def rel_dir(self, import_path: str) -> str:
        if import_path == self.module:
            return "."
        if self.module and import_path.startswith(self.module + "/"):
            return import_path[len(self.module) + 1:]
        return import_path

    @staticmethod
    def _split(target: str) -> tuple[str, str]:
        path, _, name = target.partition("::")
        d = posixpath.dirname(path) or "."
        return d, name

    def target_tokens(self, target):
        d, name = self._split(target)
        return ["-run", f"^{re.escape(name)}$", "." if d == "." else f"./{d}"]

    def case_key(self, target):
        d, name = self._split(target)
        return f"{d}::{name}"

    @staticmethod
    def case_under(key, target_key):
        return False                       # a Go test always has its own terminal event; subtests never stand in

    def run_argv(self, base_argv, cwd, report, target):
        return list(base_argv)

    def collect_argv(self, cwd, target):
        if target is None:
            return list(self.fw["collect"]) + ["-list", ".*", "./..."]
        d, name = self._split(target)
        return list(self.fw["collect"]) + ["-list", f"^{re.escape(name)}$", "." if d == "." else f"./{d}"]

    @staticmethod
    def _events(output: str) -> Optional[list[dict]]:
        """Every stdout line of ``go test -json`` as an event; None when a line is not an event (the tool itself
        never prints anything else on stdout — a test's prints are wrapped as ``output`` events by test2json)."""
        out, _ = split_streams(output)
        events = []
        for ln in out.splitlines():
            if not ln.strip():
                continue
            try:
                ev = json.loads(ln)
            except ValueError:
                return None
            if not isinstance(ev, dict) or "Action" not in ev:
                return None
            events.append(ev)
        return events

    def parse_listing(self, output, exit_code, timed_out, truncated):
        if timed_out or truncated or exit_code != 0:
            return None
        events = self._events(output)
        if events is None:
            return None
        by_pkg: dict[str, list[str]] = {}
        packages: set[str] = set()
        for ev in events:
            if ev["Action"] == "build-fail":
                return None
            pkg = ev.get("Package")
            if not pkg:
                continue
            packages.add(pkg)
            if ev["Action"] == "output" and not ev.get("Test"):
                text = ev.get("Output") or ""
                m = _GO_LIST_LINE.match(text)
                if m and _GO_TEST_NAME.match(m.group("name")):
                    by_pkg.setdefault(self.rel_dir(pkg), []).append(m.group("name"))
        for pkg in packages:
            by_pkg.setdefault(self.rel_dir(pkg), [])
        names = [f"{d}::{n}" for d in sorted(by_pkg) for n in by_pkg[d]]
        if len(set(names)) != len(names):
            return None                                    # the same function listed twice: not a Go package
        return Listing(total=len(names), names=names, by_package=by_pkg)

    def verify(self, *, report, output, listing, exit_code, timed_out, truncated, record=None, engine=None):
        if timed_out or exit_code == 124:
            return self._unknown("timed out (exit 124)")
        if truncated:
            return self._unknown("captured output truncated")
        if listing is None:
            return self._unknown("go test -list count unavailable")
        if listing.total == 0:
            return self._unknown("nothing collected (go test -list found no Test function)")
        events = self._events(output)
        if events is None:
            return self._unknown("go test -json stream unparseable")
        c = Counts(source="go-json", collected=listing.total)
        terminal: dict[str, list[str]] = {}
        pkg_state: dict[str, list[str]] = {}
        for ev in events:
            if ev["Action"] == "build-fail":
                return self._unknown("build failed", c, "go-json")
            action = ev["Action"]
            if action not in ("pass", "fail", "skip"):
                continue
            pkg = ev.get("Package") or ""
            d = self.rel_dir(pkg)
            test = ev.get("Test")
            if not test:
                pkg_state.setdefault(d, []).append(action)
                continue
            terminal.setdefault(f"{d}::{test}", []).append(action)
        # exactly one terminal event per listed Test function; no Test* event without a listing
        for key in listing.names:
            outs = terminal.get(key, [])
            if len(outs) != 1:
                return self._unknown(f"{key}: {len(outs)} terminal event(s) for a listed test (expected exactly 1)", c, "go-json")
        listed = set(listing.names)
        for key, outs in terminal.items():
            d, _, test = key.partition("::")
            top = test.split("/", 1)[0]
            if _GO_TEST_NAME.match(top) and f"{d}::{top}" not in listed:
                return self._unknown(f"{key}: a result for a test the listing never enumerated", c, "go-json")
            if len(outs) != 1:
                return self._unknown(f"{key}: {len(outs)} terminal events", c, "go-json")
            c.cases[key] = {"pass": "pass", "fail": "fail", "skip": "skip"}[outs[0]]
        # package-level events: one per listed package; consistent with the tests inside it
        for d, names in listing.by_package.items():
            states = pkg_state.get(d, [])
            if len(states) != 1:
                return self._unknown(f"package {d}: {len(states)} package-level result(s) (expected exactly 1)", c, "go-json")
            failed_inside = any(c.cases.get(f"{d}::{n}") == "fail" for n in names)
            if states[0] == "fail" and not failed_inside:
                return self._unknown(f"package {d} failed with no failed test (panic or build failure)", c, "go-json")
            if states[0] == "pass" and failed_inside:
                return self._unknown(f"package {d} passed with a failed test inside it", c, "go-json")
            if states[0] == "skip" and names:
                return self._unknown(f"package {d} reported no test files but the listing has {len(names)}", c, "go-json")
        for d in pkg_state:
            if d not in listing.by_package:
                return self._unknown(f"package {d}: a result for a package the listing never saw", c, "go-json")
        for key in listing.names:
            out = c.cases[key]
            if out == "pass":
                c.passed += 1
            elif out == "fail":
                c.failed += 1
            else:
                c.skipped += 1
        c.failed_names = sorted(k for k in listing.names if c.cases[k] == "fail")
        c.parsed = True
        red = c.failed > 0
        if exit_code == 0 and red:
            return self._unknown("exit 0 with a failed test", c, "go-json")
        if exit_code == 1 and not red:
            return self._unknown("exit 1 with no failed test", c, "go-json")
        if exit_code not in (0, 1):
            return self._unknown(f"unexpected exit code {exit_code}", c, "go-json")
        c.status, c.why = "ok", "go test -json events == go test -list == package results == exit"
        return c


# ====================================================================== cargo

class CargoToolchain(Toolchain):
    name = "cargo"
    report_ext = None            # stable cargo has no machine-readable report; the transcript is cross-checked per line

    @staticmethod
    def _selector(path: str) -> list[str]:
        """The cargo target-selection flags for the file the test lives in (one test binary at a time)."""
        parts = path.split("/")
        if parts[0] == "tests" and len(parts) >= 2:
            name = parts[1][:-3] if len(parts) == 2 and parts[1].endswith(".rs") else parts[1]
            return ["--test", name]
        if parts[:2] == ["src", "bin"] and len(parts) >= 3:
            name = parts[2][:-3] if len(parts) == 3 and parts[2].endswith(".rs") else parts[2]
            return ["--bin", name]
        if path == "src/main.rs":
            return ["--bins"]
        return ["--lib"]

    def target_tokens(self, target):
        path, _, name = target.partition("::")
        return self._selector(path) + ["--", name, "--exact"]

    def case_key(self, target):
        return target.partition("::")[2]

    @staticmethod
    def case_under(key, target_key):
        return False                       # --exact selects one libtest name

    def __init__(self, fw: dict, service_dir: str, engine_dir: str):
        super().__init__(fw, service_dir)
        self.engine_dir = engine_dir

    def target_dir(self, cwd: str) -> str:
        """An engine-owned build directory PER CHECKOUT. Never shared: cargo's freshness check is mtime-based and
        its metadata hash does not separate two copies of the same package at different paths, so a shared
        directory hands the reverted checkout the binary the verification checkout just built (seen in the
        smoke run: the reverted tree "passed" with the fixed artefact). The price is a full rebuild per
        checkout; the alternative is a verdict from a stale artefact."""
        return f"{self.engine_dir}/target-{hashlib.sha256(cwd.encode()).hexdigest()[:12]}"

    def engine_options(self, cwd, report):
        return ["--target-dir", self.target_dir(cwd)]

    def prepare(self, sandbox, cwd):
        """cargo decides "fresh" by comparing source mtimes with its last build: a file the agent wrote in the same
        second as the previous engine build would be taken as unchanged and the OLD binary would answer for it.
        The engine never trusts that clock: every file of the tree is touched before each run (a package rebuild,
        seconds; dependencies live outside the tree and stay cached)."""
        r = sandbox.exec_argv(["find", ".", "-path", "./target", "-prune", "-o", "-type", "f", "-exec", "touch", "--", "{}", "+"],
                              cwd=cwd, timeout=120)
        if r.exit_code != 0:
            raise RuntimeError("could not touch the cargo tree before the engine run")

    def run_argv(self, base_argv, cwd, report, target):
        argv = list(base_argv)
        opts = self.engine_options(cwd, report)
        if "--" in argv:
            i = argv.index("--")
            return argv[:i] + opts + argv[i:]
        return argv + opts

    def collect_argv(self, cwd, target):
        argv = list(self.fw["collect"]) + self.engine_options(cwd, None)
        if target is not None:
            argv += self.target_tokens(target)
            return argv + ["--list"]
        return argv + ["--", "--list"]

    def parse_listing(self, output, exit_code, timed_out, truncated):
        if timed_out or truncated or exit_code != 0:
            return None
        out, _ = split_streams(output)
        names = [m.group("name") for m in _CARGO_LIST_LINE.finditer(out) if m.group("kind") == "test"]
        summaries = [int(m.group("n")) for m in _CARGO_LIST_SUMMARY.finditer(out)]
        if not summaries or sum(summaries) != len(names):
            return None
        return Listing(total=len(names), names=sorted(names), binaries=len(summaries))

    def verify(self, *, report, output, listing, exit_code, timed_out, truncated, record=None, engine=None):
        if timed_out or exit_code == 124:
            return self._unknown("timed out (exit 124)")
        if truncated:
            return self._unknown("captured output truncated")
        if listing is None:
            return self._unknown("cargo test -- --list count unavailable")
        if listing.total == 0:
            return self._unknown("nothing collected (cargo test -- --list enumerated no test)")
        out, _ = split_streams(output)
        c = Counts(source="cargo-transcript", collected=listing.total)
        lines = [(m.group("name"), m.group("st")) for m in _CARGO_TEST_LINE.finditer(out)]
        names = sorted(n for n, _ in lines)
        if names != listing.names:
            extra = sorted(set(names) - set(listing.names))
            dup = sorted({n for n in names if names.count(n) > 1})
            why = ("per-test lines disagree with the listing"
                   + (f"; unlisted: {', '.join(extra[:5])}" if extra else "")
                   + (f"; printed twice: {', '.join(dup[:5])}" if dup else ""))
            return self._unknown(why, c, "cargo-transcript")
        results = list(_CARGO_RESULT.finditer(out))
        running = [int(m.group("n")) for m in _CARGO_RUNNING.finditer(out)]
        if len(results) != listing.binaries or len(running) != listing.binaries:
            return self._unknown(f"{len(results)} result line(s) / {len(running)} 'running' line(s) for {listing.binaries} test binaries",
                                 c, "cargo-transcript")
        if sum(running) != listing.total:
            return self._unknown(f"'running N tests' totals {sum(running)} != listed {listing.total}", c, "cargo-transcript")
        p = sum(int(m.group("p")) for m in results)
        f = sum(int(m.group("f")) for m in results)
        i = sum(int(m.group("i")) for m in results)
        m_ = sum(int(m.group("m")) for m in results)
        by_st = {"ok": 0, "FAILED": 0, "ignored": 0}
        for name, st in lines:
            by_st[st] += 1
            c.cases[name] = {"ok": "pass", "FAILED": "fail", "ignored": "skip"}[st]
        if (p, f, i) != (by_st["ok"], by_st["FAILED"], by_st["ignored"]) or m_ != 0:
            return self._unknown("test result lines disagree with the per-test lines", c, "cargo-transcript")
        if any(m.group("st") == "FAILED" for m in results) != (f > 0):
            return self._unknown("a test result status disagrees with its failed count", c, "cargo-transcript")
        c.passed, c.failed, c.skipped = p, f, i
        c.failed_names = sorted(n for n, st in lines if st == "FAILED")
        c.parsed = True
        if exit_code == 0 and f > 0:
            return self._unknown("exit 0 with a failed test", c, "cargo-transcript")
        if exit_code == 101 and f == 0:
            return self._unknown("exit 101 with no failed test (compile error or harness abort)", c, "cargo-transcript")
        if exit_code not in (0, 101):
            return self._unknown(f"unexpected exit code {exit_code}", c, "cargo-transcript")
        c.status, c.why = "ok", "per-test lines == --list == result lines per binary == exit"
        return c


# ====================================================================== node --test

class NodeToolchain(Toolchain):
    name = "npm"
    report_ext = "xml"
    has_listing = False

    @staticmethod
    def _split(target: str) -> tuple[str, str]:
        path, _, name = target.partition("::")
        return path, name

    def target_tokens(self, target):
        path, name = self._split(target)
        return [f"--test-name-pattern=^{re.escape(name)}$", path]

    def case_key(self, target):
        return self._split(target)[1]

    @staticmethod
    def case_under(key, target_key):
        # --test-name-pattern='^name$' matches the name at any nesting depth; a duplicate name is a second case
        return key.startswith(target_key + " #") or key.endswith(" > " + target_key) or (" > " + target_key + " #") in key

    def engine_options(self, cwd, report):
        return ["--test-reporter=junit", f"--test-reporter-destination={report}",
                "--test-reporter=tap", "--test-reporter-destination=stdout"]

    def run_argv(self, base_argv, cwd, report, target):
        argv = list(base_argv)
        i = argv.index("--test") + 1 if "--test" in argv else len(argv)
        return argv[:i] + self.engine_options(cwd, report) + argv[i:]

    def verify(self, *, report, output, listing, exit_code, timed_out, truncated, record=None, engine=None):
        if timed_out or exit_code == 124:
            return self._unknown("timed out (exit 124)")
        if truncated:
            return self._unknown("captured output truncated")
        junit = parsers.parse_junit_node(report) if report else None
        if junit is None:
            return self._unknown("junit report missing or unparseable")
        out, _ = split_streams(output)
        tap = {("skip" if m.group("k").startswith("skip") else m.group("k")): int(m.group("n")) for m in _TAP_SUMMARY.finditer(out)}
        junit.collected = tap.get("tests")
        if not all(k in tap for k in ("tests", "pass", "fail", "cancelled", "skip", "todo")):
            return self._unknown("no TAP summary to cross-check", junit, "junit")
        total = junit.passed + junit.failed + junit.errors + junit.skipped
        if total == 0 or tap["tests"] == 0:
            return self._unknown("nothing collected (0 tests)", junit, "junit")
        if total != tap["tests"]:
            return self._unknown(f"junit testcase count {total} != TAP tests {tap['tests']}", junit, "junit")
        if (junit.passed, junit.failed + junit.errors, junit.skipped) != (tap["pass"], tap["fail"] + tap["cancelled"], tap["skip"] + tap["todo"]):
            return self._unknown("TAP summary disagrees with junit", junit, "junit")
        plan = [int(m.group("n")) for m in _TAP_PLAN.finditer(out)]
        top = list(_TAP_TOP.finditer(out))
        if len(plan) != 1 or plan[0] != len(top):
            return self._unknown("TAP plan disagrees with the top-level result lines", junit, "junit")
        if sum(1 for m in top if m.group("st") == "not ok") != len([k for k, v in junit.cases.items() if v in ("fail", "error") and " > " not in k]):
            return self._unknown("TAP top-level failures disagree with junit", junit, "junit")
        red = junit.failed + junit.errors > 0
        if exit_code == 0 and red:
            return self._unknown("exit 0 with a failure in junit", junit, "junit")
        if exit_code == 1 and not red:
            return self._unknown("exit 1 with no failure in junit", junit, "junit")
        if exit_code not in (0, 1):
            return self._unknown(f"unexpected exit code {exit_code}", junit, "junit")
        junit.status, junit.why = "ok", "junit == TAP summary == TAP lines == exit (node has no collect-only; stated)"
        return junit
