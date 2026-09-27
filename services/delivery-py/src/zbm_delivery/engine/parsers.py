"""
Parsers (spec §C.8.4 steps 1, 3, 7, 8; §C.8.5): the engineer's reply lines (strict regexes — anything else fails
the round) and the per-framework suite output parsers (pytest summary line, ``cargo test`` result lines, ``go test``
ok/FAIL per package, npm/jest summary). Counts come from the RUNNER's captured output, never from agent text.
"""

from __future__ import annotations

import re
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

    def as_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed, "errors": self.errors, "skips": self.skipped,
                "failed_names": sorted(self.failed_names)[:200], "parsed": self.parsed}


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
    return PARSERS[framework](output)
