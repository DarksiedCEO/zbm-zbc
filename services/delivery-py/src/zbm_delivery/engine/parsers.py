"""
Parsers (spec §C.8.4 steps 1, 3, 7, 8; §C.8.5): the engineer's reply lines (strict regexes — anything else fails
the round) and the per-framework suite output parsers (pytest summary line, ``cargo test`` result lines, ``go test``
ok/FAIL per package, npm/jest summary). Counts come from the RUNNER's captured output, never from agent text.

Round 18 R2: for pytest the summary line is a CROSS-CHECK only. The verdict is ``verified_counts`` — the junit file
the engine asked for (``--junitxml`` to an engine-chosen path, read back by the engine) against the collect-only
count, the summary line and the exit code. Any disagreement, a missing or unparseable junit, exit 5 (nothing
collected), exit 124 / a timeout or a truncated capture is ``status == "unknown"``; ``unknown`` never satisfies
green, a failed RED, or ``fixed``. The go / cargo / node verdicts live in ``engine/toolchains.py`` and use the
same rule; ``parse_junit_node`` here reads Node's junit reporter (no file attribute, suites nest).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

_TEST_LINE = re.compile(r"^TEST:\s*(?P<path>(?:services/[a-z0-9\-]+/)?[A-Za-z0-9_./\-]+?)::(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*(?:\[[^\]\n]{1,120}\])?)\s*$", re.M)
_FIXED_LINE = re.compile(r"^FIXED\s*$", re.M)
_SWEEP_LINE = re.compile(r"^SWEEP:\s*(?P<file>[A-Za-z0-9_./\-]+):(?P<line>[0-9]{1,7})\s*$", re.M)
_CHANGED_TEST = re.compile(r"^CHANGED_TEST:\s*(?P<path>[A-Za-z0-9_./\-]+)\s+[—-]+\s+(?P<why>\S.{0,400})$", re.M)
_DISPROOF = re.compile(r"^DISPROOF:\s*(?P<argv>\S.{0,500})$", re.M)
_BLOCKED = re.compile(r"^BLOCKED:\s*(?P<why>\S.{0,500})$", re.M)


@dataclass
class Reply:
    test: Optional[tuple[str, str]] = None
    fixed: bool = False
    sweep: list[tuple[str, int]] = field(default_factory=list)
    changed_tests: dict[str, str] = field(default_factory=dict)
    disproof: Optional[list[str]] = None
    disproof_statement: str = ""
    blocked: Optional[str] = None


def service_relative(path: str, service: str) -> str:
    prefix = f"services/{service}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def parse_reply(text: str, service: str) -> Reply:
    r = Reply()
    if not isinstance(text, str):
        return r
    m = _TEST_LINE.search(text)
    if m:
        path = service_relative(m.group("path"), service)
        if ".." not in path.split("/") and not path.startswith("/"):
            r.test = (path, m.group("name"))
    r.fixed = bool(_FIXED_LINE.search(text))
    for m in _SWEEP_LINE.finditer(text):
        r.sweep.append((service_relative(m.group("file"), service), int(m.group("line"))))
    for m in _CHANGED_TEST.finditer(text):
        r.changed_tests[service_relative(m.group("path"), service)] = m.group("why").strip()
    m = _DISPROOF.search(text)
    if m:
        import shlex
        try:
            argv = shlex.split(m.group("argv"))
        except ValueError:
            argv = []
        if argv:
            r.disproof = argv
            r.disproof_statement = text[m.end():].strip()
    m = _BLOCKED.search(text)
    if m:
        r.blocked = m.group("why").strip()
    return r


# --- suite output -----------------------------------------------------------------------------------------------------

@dataclass
class Counts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_names: list[str] = field(default_factory=list)
    parsed: bool = False
    status: str = "unknown"                  # "ok" only when every cross-check agreed (R2)
    why: str = "not verified"
    source: str = "none"                     # junit | summary | none
    collected: Optional[int] = None
    cases: dict = field(default_factory=dict)   # node id -> pass | fail | error | skip (junit only)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def verdict_for(self, target: str, under=None) -> str:
        """pass | fail | unknown for one case key: an ``unknown`` count set answers unknown; a target absent from the
        report answers unknown (it did not run); skipped answers unknown (nothing was proven). ``under(key,
        target)`` names the cases that belong to the target when it has no case of its own (default: pytest's
        parametrised ``target[...]`` and class-scoped ``target::...`` ids); every one of them must agree."""
        if not self.ok:
            return "unknown"
        out = self.cases.get(target)
        if out is None:
            if under is None:
                def under(k, t):
                    return k.startswith(t + "[") or k.startswith(t + "::")
            outs = [v for k, v in self.cases.items() if under(k, target)]
            if not outs:
                return "unknown"
            if all(v == "pass" for v in outs):
                return "pass"
            return "fail" if any(v in ("fail", "error") for v in outs) else "unknown"
        return {"pass": "pass", "fail": "fail", "error": "fail"}.get(out, "unknown")

    def as_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed, "errors": self.errors, "skips": self.skipped,
                "failed_names": sorted(self.failed_names)[:200], "parsed": self.parsed, "status": self.status,
                "why": self.why, "source": self.source, "collected": self.collected}


# verbose: "===== 1 failed, 2 passed in 0.03s =====" · quiet (-q): "1 failed, 2 passed in 0.03s"
_PYTEST_SUMMARY = re.compile(r"^(?:=+ )?(?P<body>(?:[0-9]+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)(?:, )?)+) in [0-9.]+s(?: \([^)]*\))?(?: =+)?\s*$", re.M)
_PYTEST_PART = re.compile(r"(?P<n>[0-9]+) (?P<kind>passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings?)")
_PYTEST_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (?P<node>[^\s]+)", re.M)


def parse_pytest(output: str) -> Counts:
    c = Counts()
    last = None
    for m in _PYTEST_SUMMARY.finditer(output):
        last = m
    if last is None:
        if re.search(r"^no tests ran", output, re.M) or "no tests ran" in output:
            c.parsed = True
        return c
    for part in _PYTEST_PART.finditer(last.group("body")):
        n, kind = int(part.group("n")), part.group("kind")
        if kind == "passed":
            c.passed += n
        elif kind == "failed":
            c.failed += n
        elif kind in ("error", "errors"):
            c.errors += n
        elif kind.startswith("skip"):
            c.skipped += n
    c.failed_names = sorted({m.group("node") for m in _PYTEST_FAILED_LINE.finditer(output)})
    c.parsed = True
    return c


_CARGO_RESULT = re.compile(r"^test result: (?P<status>ok|FAILED)\. (?P<p>[0-9]+) passed; (?P<f>[0-9]+) failed; (?P<i>[0-9]+) ignored", re.M)
_CARGO_FAILED = re.compile(r"^test (?P<name>\S+) \.\.\. FAILED", re.M)


def parse_cargo(output: str) -> Counts:
    c = Counts()
    for m in _CARGO_RESULT.finditer(output):
        c.passed += int(m.group("p"))
        c.failed += int(m.group("f"))
        c.skipped += int(m.group("i"))
        c.parsed = True
    c.failed_names = sorted({m.group("name") for m in _CARGO_FAILED.finditer(output)})
    return c


_GO_PKG = re.compile(r"^(?P<status>ok|FAIL|---\s+FAIL)\s+(?P<pkg>\S+)", re.M)
_GO_FAILED = re.compile(r"^--- FAIL: (?P<name>\S+)", re.M)
_GO_PASSED = re.compile(r"^--- PASS: (?P<name>\S+)", re.M)
_GO_SKIP = re.compile(r"^--- SKIP: (?P<name>\S+)", re.M)


def parse_go(output: str) -> Counts:
    c = Counts()
    c.failed_names = sorted({m.group("name") for m in _GO_FAILED.finditer(output)})
    c.failed = len(c.failed_names)
    c.passed = len({m.group("name") for m in _GO_PASSED.finditer(output)})
    c.skipped = len({m.group("name") for m in _GO_SKIP.finditer(output)})
    pkgs = list(_GO_PKG.finditer(output))
    if pkgs:
        c.parsed = True
        if not c.passed and not c.failed:
            c.passed = sum(1 for m in pkgs if m.group("status") == "ok")
            c.failed = sum(1 for m in pkgs if m.group("status").startswith("FAIL"))
    return c


_NPM_TESTS = re.compile(r"^Tests:\s+(?P<body>.*)$", re.M)
_NPM_PART = re.compile(r"(?P<n>[0-9]+) (?P<kind>passed|failed|skipped|todo|total)")


def parse_npm(output: str) -> Counts:
    c = Counts()
    matches = list(_NPM_TESTS.finditer(output))
    if not matches:
        return c
    m = matches[-1]
    for part in _NPM_PART.finditer(m.group("body")):
        n, kind = int(part.group("n")), part.group("kind")
        if kind == "passed":
            c.passed = n
        elif kind == "failed":
            c.failed = n
        elif kind.startswith("skip"):
            c.skipped = n
    c.failed_names = sorted({x.strip() for x in re.findall(r"^\s*✕ (.+?)(?: \([0-9]+ ms\))?$", output, re.M)})
    c.parsed = True
    return c


PARSERS = {"pytest": parse_pytest, "cargo": parse_cargo, "go": parse_go, "npm": parse_npm}


def parse_counts(framework: str, output: str) -> Counts:
    """Summary-only counts for a framework whose seed says ``verified: false`` (none in the shipped seed): never
    ``ok`` — a summary line alone is exactly what R2 forbids trusting."""
    c = PARSERS[framework](output)
    c.source = "summary" if c.parsed else "none"
    c.status, c.why = "unknown", "summary line only (no engine-owned report for this framework)"
    return c


# --- junit (R2) --------------------------------------------------------------------------------------------------------

def _node_id(tc) -> str:
    """xunit1: ``file`` + ``classname`` + ``name`` → pytest node id ``file::Class::name[param]``."""
    file = tc.get("file") or ""
    classname = tc.get("classname") or ""
    name = tc.get("name") or ""
    if not file:
        parts = classname.split(".")
        file = "/".join(parts[:-1] + [parts[-1] + ".py"]) if parts else ""
        return f"{file}::{name}"
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file.replace("/", ".")
    classes = classname[len(module) + 1:] if classname.startswith(module + ".") else ""
    mid = "::".join(x for x in classes.split(".") if x) if classes else ""
    return f"{file}::{mid}::{name}" if mid else f"{file}::{name}"


def parse_junit(xml_text: str) -> Optional[Counts]:
    """Counts and per-case outcomes from a pytest junit file (xunit1 family); None when unparseable."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return None
    c = Counts()
    cases = root.iter("testcase")
    n = 0
    for tc in cases:
        n += 1
        node = _node_id(tc)
        outcome = "pass"
        for child in tc:
            if child.tag == "failure":
                outcome = "fail"
            elif child.tag == "error":
                outcome = "error"
            elif child.tag.startswith("skip"):
                outcome = "skip" if outcome == "pass" else outcome
        prev = c.cases.get(node)
        # a node id seen twice (setup error + failure) keeps the worst outcome
        rank = {"pass": 0, "skip": 1, "fail": 2, "error": 3}
        if prev is None or rank[outcome] > rank[prev]:
            c.cases[node] = outcome
        if outcome == "pass":
            c.passed += 1
        elif outcome == "fail":
            c.failed += 1
        elif outcome == "error":
            c.errors += 1
        else:
            c.skipped += 1
    if n == 0 and root.tag not in ("testsuites", "testsuite"):
        return None
    c.failed_names = sorted(k for k, v in c.cases.items() if v in ("fail", "error"))
    c.parsed = True
    c.source = "junit"
    return c


_COLLECT_NODE = re.compile(r"^[^\s:]+::\S.*$", re.M)          # a parametrised id may contain spaces
_COLLECT_SUMMARY = re.compile(r"^(?P<n>[0-9]+) tests? collected", re.M)
_COLLECT_NONE = re.compile(r"^no tests collected|^no tests ran", re.M)


def parse_collected(output: str) -> Optional[int]:
    """The number of node ids ``--collect-only -q`` printed, cross-checked against its own ``N tests collected``
    line when present; None when the two disagree or nothing is recognisable."""
    ids = len(_COLLECT_NODE.findall(output))
    matches = list(_COLLECT_SUMMARY.finditer(output))
    m = matches[-1] if matches else None
    if m is not None:
        stated = int(m.group("n"))
        return ids if stated == ids else None
    if _COLLECT_NONE.search(output):
        return 0 if ids == 0 else None
    return ids if ids else None


def verified_counts(*, junit_xml: Optional[str], output: str, collected: Optional[int], exit_code: int,
                    timed_out: bool, truncated: bool) -> Counts:
    """The engine's verdict for one pytest run (R2). Every cross-check must agree or the result is ``unknown``."""
    summary = parse_pytest(output)
    junit = parse_junit(junit_xml) if junit_xml else None

    def unknown(why: str) -> Counts:
        c = junit if junit is not None else Counts()
        c.status, c.why = "unknown", why
        c.source = "junit" if junit is not None else ("summary" if summary.parsed else "none")
        c.collected = collected
        if junit is None and summary.parsed:
            c.passed, c.failed, c.errors, c.skipped = summary.passed, summary.failed, summary.errors, summary.skipped
            c.failed_names = list(summary.failed_names)
            c.parsed = True
        return c

    if timed_out or exit_code == 124:
        return unknown("timed out (exit 124)")
    if truncated:
        return unknown("captured output truncated")
    if junit is None:
        return unknown("junit report missing or unparseable")
    if exit_code == 5:
        return unknown("nothing collected (exit 5)")
    if collected is None:
        return unknown("collect-only count unavailable")
    total = junit.passed + junit.failed + junit.errors + junit.skipped
    if total != collected:
        return unknown(f"junit testcase count {total} != collected {collected}")
    if not summary.parsed:
        return unknown("no summary line to cross-check")
    if (summary.passed, summary.failed, summary.errors, summary.skipped) != (junit.passed, junit.failed, junit.errors, junit.skipped):
        return unknown("summary line disagrees with junit")
    if sorted(summary.failed_names) != sorted(n.split(" ")[0] for n in junit.failed_names):
        return unknown("summary FAILED lines disagree with junit")           # the summary cuts an id at its first space
    red = junit.failed + junit.errors > 0
    if exit_code == 0 and red:
        return unknown("exit 0 with failures in junit")
    if exit_code != 0 and not red and exit_code in (1,):
        return unknown("exit 1 with no failure in junit")
    if exit_code not in (0, 1):
        return unknown(f"unexpected exit code {exit_code}")
    junit.status, junit.why, junit.collected = "ok", "junit == collected == summary == exit", collected
    return junit


def parse_junit_node(xml_text: Optional[str]) -> Optional[Counts]:
    """Counts and per-case outcomes from Node's ``--test-reporter=junit`` file. Node writes no ``file`` attribute
    and nests ``describe`` blocks as ``testsuite`` elements, so a case is keyed ``suite > … > name`` (a top-level
    test is just ``name``); a name used twice keeps both cases (they are counted twice, as the TAP stream does).
    None when unparseable."""
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    c = Counts()
    seen: dict[str, int] = {}

    def walk(node, prefix: list[str]) -> None:
        for child in node:
            if child.tag == "testsuite":
                walk(child, prefix + [child.get("name") or ""])
            elif child.tag == "testcase":
                name = child.get("name") or ""
                key = " > ".join(prefix + [name]) if prefix else name
                outcome = "pass"
                for g in child:
                    if g.tag == "failure":
                        outcome = "fail"
                    elif g.tag == "error":
                        outcome = "error"
                    elif g.tag.startswith("skip"):
                        outcome = "skip" if outcome == "pass" else outcome
                n = seen.get(key, 0)
                seen[key] = n + 1
                c.cases[key if n == 0 else f"{key} #{n + 1}"] = outcome
                if outcome == "pass":
                    c.passed += 1
                elif outcome == "fail":
                    c.failed += 1
                elif outcome == "error":
                    c.errors += 1
                else:
                    c.skipped += 1

    walk(root, [])
    c.failed_names = sorted(k for k, v in c.cases.items() if v in ("fail", "error"))
    c.parsed = True
    c.source = "junit"
    return c


# --- the engine plugin's record (R4) -----------------------------------------------------------------------------------

# pytest's report outcomes mapped to the junit kinds this module uses (anything that is neither passed nor failed
# is pytest's third outcome, the one junit records as a <skipped> element)
_KIND = {"passed": "pass", "failed": "fail"}


def _kind(outcome) -> Optional[str]:
    if not isinstance(outcome, str) or not outcome:
        return None
    return _KIND.get(outcome, "skip")


def _record_case_outcome(phases: dict) -> Optional[str]:
    """junit's outcome for one case from the plugin's per-phase final outcomes (None when a phase is inconsistent)."""
    for ph in phases.values():
        raw, final, logged = _kind(ph.get("raw_outcome")), _kind(ph.get("final_outcome")), _kind(ph.get("logged_outcome"))
        if raw is None or final is None or logged != final:
            return None
        if ph.get("excinfo_none") != (raw == "pass"):
            return None
        if raw != final and not (ph.get("final_wasxfail") or ph.get("xfail_marked")):
            return None
    call = phases.get("call") or {}
    setup, teardown = phases.get("setup") or {}, phases.get("teardown") or {}
    if _kind(setup.get("final_outcome")) == "fail" or _kind(teardown.get("final_outcome")) == "fail":
        return "error"
    if _kind(call.get("final_outcome")) == "fail":
        return "fail"
    if any(_kind(ph.get("final_outcome")) == "skip" for ph in phases.values()):
        return "skip"
    if call.get("final_wasxfail") and _kind(call.get("final_outcome")) == "pass":
        return "skip"                  # xpass (non-strict): junit records a <skipped> element
    if setup and not call and _kind(setup.get("final_outcome")) == "pass":
        return "error"                 # a setup that passed with no call phase: not a shape pytest produces
    return "pass"


def plugin_record_problem(record_text: Optional[str], counts: Counts, *, engine_dir: str,
                          plugin_sha256: Optional[str] = None) -> Optional[str]:
    """Why the engine plugin's record does not confirm ``counts`` (a verified junit result), or None when it does:
    the record must exist, name the pinned plugin at the engine directory, show a started/collected/finished session
    with plugin autoload disabled and no violation, and every junit case must have a record whose four positions
    agree with each other and with junit's outcome (and vice versa)."""
    from zbm_delivery.runner import PLUGIN_NAME, PLUGIN_SHA256
    if not record_text:
        return "record missing"
    try:
        rec = json.loads(record_text)
    except ValueError:
        return "record unparseable"
    if not isinstance(rec, dict):
        return "record is not an object"
    if rec.get("plugin_sha256") != (plugin_sha256 or PLUGIN_SHA256):
        return "plugin hash does not match the pin"
    # the engine directory's nonce-named tail (the double maps the workspace to a host directory in file contents)
    tail = "/" + "/".join(engine_dir.rstrip("/").split("/")[-2:]) + f"/{PLUGIN_NAME}.py" if engine_dir else f"/{PLUGIN_NAME}.py"
    if not str(rec.get("plugin_file") or "").endswith(tail):
        return "plugin loaded from outside the engine directory"
    for flag in ("session_started", "collection_finished", "session_finished", "disable_plugin_autoload"):
        if rec.get(flag) is not True:
            return f"{flag} is not true"
    violations = rec.get("violations")
    if not isinstance(violations, list):
        return "violations missing"
    if violations:
        return "violation: " + str(violations[0])[:160]
    tests = rec.get("tests")
    if not isinstance(tests, dict):
        return "tests missing"
    if rec.get("collected") is not None and counts.collected is not None and rec["collected"] != counts.collected:
        return f"plugin collected {rec['collected']} != {counts.collected}"
    junit_ids = set(counts.cases)
    rec_ids = set(tests)
    if junit_ids != rec_ids:
        missing = sorted(junit_ids ^ rec_ids)[:3]
        return "junit cases and the plugin record name different tests: " + ", ".join(missing)
    for nodeid, phases in tests.items():
        if not isinstance(phases, dict) or "call" not in phases and "setup" not in phases:
            return f"{nodeid}: no phase recorded"
        out = _record_case_outcome(phases)
        if out is None:
            return f"{nodeid}: the four report positions disagree"
        if out != counts.cases.get(nodeid):
            return f"{nodeid}: plugin says {out}, junit says {counts.cases.get(nodeid)}"
    return None
