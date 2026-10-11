#!/usr/bin/env python3
"""Turn a CI test log into GitHub workflow-command annotations. Standard library only; Python 3.9+ syntax (the CI
steps run it with Python 3.12+, but the macOS runner's system python3 must never turn it into a crash).

Why: the job logs of this repository's Actions runs cannot be read from the environment the team works in (the log
blob storage answers 403), while a check run's annotations can:

    gh api repos/DarksiedCEO/zbm-zbc/check-runs/<job_id>/annotations

Each test step in .github/workflows/ci.yml tees the suite's full output to a file under $RUNNER_TEMP (the console
still gets every line) and then runs

    python3 devtools/ci_annotate.py --log "$RUNNER_TEMP/<name>.log" --code "$code" --suite "<suite>"

which prints, for a red step, one ``::error title=<suite>::...`` per failure it recognises — the failing test's id
and the first reason lines under it — then a summary. Recognised (best effort; an unknown format falls back to the
log's last lines):

  pytest       ``FAILED <nodeid> - <reason>`` / ``ERROR <nodeid>`` short-summary lines (the steps pass ``-rfEs``),
               the ``E   ...`` lines and the ``file.py:N: Error`` line of that test's section, the count line, the
               ``slowest durations`` section, and faulthandler timeout dumps (``Timeout (0:10:00)!`` then one stack
               per thread: the entry frame of the running test and the first frames of every thread);
  hygiene      ``HYGIENE <rule> <suite>: <detail>`` violations and the ``hygiene_check: suite ... exited N`` line;
  cargo        ``test <name> ... FAILED``, the panic message under ``thread '<name>' panicked at``, the
               ``test result: FAILED`` line, compile errors (``error[E....]``);
  go           ``--- FAIL: <name>`` with the ``x_test.go:N: ...`` lines printed while it ran, ``panic:``, data races;
  node --test  TAP ``not ok N - <name>`` with its ``error:`` and ``location:`` (parents that only failed because a
               subtest did are left out), the spec reporter's ``✖ <name>``, and the ``# tests / # fail`` counts;
  unittest     ``FAIL: <test> (<id>)`` / ``ERROR: ...`` and the exception line of its traceback, ``FAILED (...)``;
  live runs    ``CHECK FAIL: ...``, ``MISMATCH: ...``, ``RESULT: ...`` and ``N/M checks passed`` lines;
  war room     the entries under ``### New MUST failures`` / ``### Errors`` and the ``Gate: **FAIL**`` line.

Limits (GitHub keeps 10 error and 10 warning annotations per step and 50 annotations per job): at most 10
``::error`` lines — the failures first, a faulthandler timeout next, the summary always last among them — then up
to 10 ``::warning`` lines for further failures, and never more than ``MAX_ANNOTATIONS`` in all; failures beyond
that are named in the summary. Each quoted line is cut at ``MAX_LINE`` characters, each message at ``MAX_MESSAGE``.

Exit code 0: nothing but one ``::notice`` with the summary (count line, slowest test, hygiene line), unless
``--no-notice``. The script itself always exits 0 and never changes the step's result: the step exits with the
suite's own code.

The log is untrusted text. Every quoted line is redacted (Bearer/Basic/authorization values, ``key=value`` secrets,
URL credentials, well-known token shapes, and long high-entropy strings), stripped of control characters, has a
leading ``::`` / ``##[`` neutralised, and the whole message is escaped as GitHub requires (``%`` -> ``%25``, CR ->
``%0D``, LF -> ``%0A``; properties also ``:`` -> ``%3A``, ``,`` -> ``%2C``), so a log line can neither start a
workflow command of its own nor break out of the property list. Lines of the log that already are workflow
commands (a live run prints its own ``::error``) were acted on when they streamed to the console and are not
repeated.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter, deque

MAX_ANNOTATIONS = 40          # hard cap on lines this script prints for one step
MAX_ERRORS = 10               # GitHub keeps 10 error annotations per step
MAX_WARNINGS = 10             # ... and 10 warning annotations per step
MAX_TIMEOUT_ERRORS = 2        # error slots a faulthandler dump may take ahead of further failures
MAX_MESSAGE = 1800            # characters of one annotation message (before escaping)
MAX_LINE = 300                # characters of one quoted log line
MAX_REASON_LINES = 6          # reason lines quoted under one failure
MAX_SCAN_LINE = 20000         # a longer log line is cut before any pattern sees it
TAIL_LINES = 25               # last log lines quoted when no failure is recognised
MAX_FRAMES_MAIN = 6           # faulthandler: innermost frames of the thread running the test
MAX_FRAMES_OTHER = 3          # ... of every other thread
MAX_THREADS_OTHER = 4

# ============================================================================================ escaping / redaction

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def escape_data(s: str) -> str:
    """GitHub workflow-command message escaping."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(s: str) -> str:
    """GitHub workflow-command property escaping (a title, a file name)."""
    return escape_data(s).replace(":", "%3A").replace(",", "%2C")


def neutralise(line: str) -> str:
    """A quoted line must not look like a workflow command even on its own: ``::x`` -> ``: :x``, ``##[`` -> ``# #[``."""
    return re.sub(r"^(\s*)(?:::|##\[)", lambda m: m.group(1) + (": :" if m.group(0).endswith("::") else "# #["), line)


_SECRET_KEYS = (r"authorization|proxy-authorization|x-api-key|api[_-]?key|apikey|secret|client[_-]?secret|"
                r"password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token|"
                r"private[_-]?key|session[_-]?id|cookie|set-cookie|credentials?")
_REDACTIONS = (
    # scheme + credential: "Bearer abc...", "Basic dXNl...", "token ghp_..."
    (re.compile(r"(?i)\b(bearer|basic|digest|token)(\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1\2[REDACTED]"),
    # key: value / key=value for secret-looking keys (quoted or not, JSON or header style)
    (re.compile(r"(?i)(\b(?:" + _SECRET_KEYS + r")\b[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&)}\]]{4,})"), r"\1[REDACTED]"),
    # URL userinfo: https://user:pass@host
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[REDACTED]@"),
    # well-known token shapes
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{8,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
                r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"), "[REDACTED]"),
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _secretish(tok: str) -> bool:
    """A long run of token characters that looks random: 32+ hex digits (digests and keys alike — conservative), or
    upper case, lower case and digits mixed with high per-character entropy. Identifiers (``test_some_long_name``) and
    paths have no upper-case/digit mix and are kept."""
    core = tok.strip("=")
    if re.fullmatch(r"[0-9a-fA-F]{32,}", core):
        return True
    if not (re.search(r"[0-9]", core) and re.search(r"[a-z]", core) and re.search(r"[A-Z]", core)):
        return False
    return _entropy(core) >= 3.5


def redact(line: str) -> str:
    for rx, repl in _REDACTIONS:
        line = rx.sub(repl, line)
    return _LONG_TOKEN.sub(lambda m: "[REDACTED]" if _secretish(m.group(0)) else m.group(0), line)


def clean(line: str, limit: int = MAX_LINE) -> str:
    """One log line made safe to quote: control characters gone, redacted, neutralised, then cut (cut AFTER
    redaction, so a secret is never shortened below the length that marks it)."""
    line = CTRL_RE.sub("?", line.replace("\t", "    "))
    line = neutralise(redact(line))
    return line if len(line) <= limit else line[:limit - 1] + "…"


def short_path(s: str) -> str:
    """Shorter frames and locations: the checkout prefix and everything up to site-packages/ dropped."""
    ws = os.environ.get("GITHUB_WORKSPACE")
    if ws:
        s = s.replace(ws.rstrip("/") + "/", "")
    s = re.sub(r"(?:file://)?/\S*?/site-packages/", "site-packages/", s)
    return s


# ======================================================================================================= parsing

PREFIX_RE = re.compile(r"^\[\s*[\d.:]+\]\s")                      # live_run.py's "[   12.34] " / "[12:00:00] "
SECTION_RE = re.compile(r"^_{3,} (.+?) _{3,}$")
BANNER_RE = re.compile(r"^={3,} ?.*?={3,}$")
PYTEST_SUMMARY_LINE = re.compile(r"^(FAILED|ERROR) (?!\()(.+?)(?: - (.*))?$")
PYTEST_COUNTS = re.compile(r"^(?:=+ )?(\d+ (?:failed|passed|errors?|skipped|deselected|xfailed|xpassed|warnings?|"
                           r"rerun)(?:, \d+ \w+)*) in [\d.]+s(?: \([\d:.]+\))?(?: =+)?$")
PYTEST_NO_TESTS = re.compile(r"^(?:=+ )?(no tests ran|\d+ deselected) in [\d.]+s(?: =+)?$")
DURATIONS_HDR = re.compile(r"^=+ slowest (?:\d+ )?durations =+$")
DURATION_LINE = re.compile(r"^([\d.]+)s (call|setup|teardown)\s+(.+)$")
E_LINE = re.compile(r"^E(?:\s+(.*))?$")
LOC_LINE = re.compile(r"^(\S+\.py):(\d+): (\w[\w.]*)$")
FH_TIMEOUT = re.compile(r"Timeout \((\d+:\d\d:\d\d(?:\.\d+)?)\)!\s*$")
FH_THREAD = re.compile(r"^(?:Current thread|Thread) (0x[0-9a-fA-F]+)(.*)\(most recent call first\):\s*$")
FH_FRAME = re.compile(r"^  (?:File \"(.+)\", line (\d+) in (.+)|<no Python frame>|<.*>)$")
HYG_VIOLATION = re.compile(r"^HYGIENE (\S+) (\S+): (.*)$")
HYG_PLUGIN = re.compile(r"^HYGIENE: (.*)$")
HYG_SUMMARY = re.compile(r"^hygiene_check: suite (\S+) exited (-?\d+); .*$")
CARGO_FAILED = re.compile(r"^test (.+?) \.\.\. FAILED$")
CARGO_PANIC = re.compile(r"^thread '(.+?)'(?: \(\d+\))? panicked at (.*?):?$")
CARGO_RESULT = re.compile(r"^test result: FAILED\. .*$")
CARGO_ERROR = re.compile(r"^error(\[E\d{4}\])?: (.*)$")
GO_RUN = re.compile(r"^=== (?:RUN|CONT|PAUSE|NAME)\s+(\S+)$")
GO_FAIL = re.compile(r"^(\s*)--- FAIL: (\S+) \(([\d.]+s)\)$")
GO_MSG = re.compile(r"^\s+(\S+_test\.go:\d+: .*)$")
GO_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)\s+[\d.]+s$|^FAIL\s+(\S+) \[.+\]$")
GO_PANIC = re.compile(r"^panic: (.*)$")
GO_RACE = re.compile(r"^WARNING: DATA RACE$")
TAP_NOT_OK = re.compile(r"^(\s*)not ok \d+ - (.+?)$")
TAP_COUNT = re.compile(r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)$")
SPEC_FAIL = re.compile(r"^\s*✖ (.+?) \([\d.]+m?s\)$")
SPEC_COUNT = re.compile(r"^ℹ (tests|pass|fail|cancelled) (\d+)$")
UT_FAIL = re.compile(r"^(FAIL|ERROR): (.+?) \((.+)\)$")
UT_SUMMARY = re.compile(r"^FAILED \((.+)\)$")
UT_RAN = re.compile(r"^Ran \d+ tests? in [\d.]+s$")
LIVE_FAIL = re.compile(r"^\s*(CHECK FAIL: .*|MISMATCH: .*)$")
LIVE_RESULT = re.compile(r"^(RESULT: .*|\d+/\d+ checks passed.*)$")
WR_GATE = re.compile(r"^Gate: \*\*(PASS|FAIL)\*\*.*$")
WR_HEAD = re.compile(r"^### (.+)$")
WR_ITEM = re.compile(r"^- `([^`]+)` (.*)$")
TRACEBACK = "Traceback (most recent call last):"


class Failure:
    def __init__(self, key: str, header: str):
        self.key = key
        self.header = header
        self.reasons: list[str] = []

    def add(self, line: str) -> None:
        line = line.rstrip()
        if line and line not in self.reasons and len(self.reasons) < MAX_REASON_LINES:
            self.reasons.append(line)

    def lines(self) -> list[str]:
        return [self.header] + [r for r in self.reasons if r not in self.header]


class Parser:
    """One pass over the log. Keeps only what it may quote; the log itself is never held in memory."""

    def __init__(self) -> None:
        self.failures: dict[str, Failure] = {}
        self.timeouts: list[list[str]] = []
        self.summaries: list[str] = []
        self.notable: list[str] = []            # tracebacks' last lines, compile errors: quoted when nothing else is
        self.tail: deque = deque(maxlen=TAIL_LINES)
        self.slowest: tuple[float, str] | None = None
        self.passed_summary: list[str] = []
        # pytest sections
        self._section: str | None = None
        self._section_lines: dict[str, list[str]] = {}
        self._in_durations = False
        # faulthandler
        self._fh: dict | None = None
        # cargo panics waiting for their message
        self._panic: tuple[str, list[str]] | None = None
        self._panic_at = ""
        # go
        self._go_current: str | None = None
        self._go_msgs: dict[str, list[str]] = {}
        self._go_race: list[str] | None = None
        # node TAP block
        self._tap: dict | None = None
        self._tap_counts: dict[str, str] = {}
        # unittest
        self._ut: dict | None = None
        # war room
        self._wr_section = False
        # python traceback
        self._tb: list[str] | None = None

    # ------------------------------------------------------------------------------------------------- helpers
    def _fail(self, key: str, header: str) -> Failure:
        f = self.failures.get(key)
        if f is None:
            f = self.failures[key] = Failure(key, header)
        return f

    def _summary(self, line: str) -> None:
        if line not in self.summaries:
            self.summaries.append(line)

    # ---------------------------------------------------------------------------------------------------- feed
    def feed(self, raw: str) -> None:
        raw = raw.rstrip("\n").rstrip("\r")
        if "\r" in raw:                                  # progress redraws: the last segment is what was shown
            raw = raw.rsplit("\r", 1)[-1]
        line = ANSI_RE.sub("", raw[:MAX_SCAN_LINE])
        if line.strip():
            self.tail.append(line)
        if line.lstrip().startswith("::"):
            return                                       # already a workflow command; the runner acted on it
        line = PREFIX_RE.sub("", line)

        if self._fh is not None and self._feed_faulthandler(line):
            return
        m = FH_TIMEOUT.search(line)
        if m:
            self._fh = {"timeout": m.group(1), "threads": [], "current": None}
            return
        if self._panic is not None and self._feed_panic(line):
            return
        if self._tap is not None and self._feed_tap(line):
            return
        if self._ut is not None and self._feed_unittest(line):
            return
        if self._go_race is not None:
            if line.strip() and len(self._go_race) < MAX_REASON_LINES:
                self._go_race.append(line.strip())
                return
            self._fail(f"race#{len(self.failures)}", "WARNING: DATA RACE").reasons.extend(self._go_race)
            self._go_race = None
        if self._tb is not None:
            self._feed_traceback(line)

        self._feed_pytest(line)
        self._feed_other(line)

    # ------------------------------------------------------------------------------------------------- pytest
    def _feed_pytest(self, line: str) -> None:
        if DURATIONS_HDR.match(line):
            self._in_durations = True
            self._section = None
            return
        if self._in_durations:
            m = DURATION_LINE.match(line)
            if m:
                secs = float(m.group(1))
                if self.slowest is None or secs > self.slowest[0]:
                    self.slowest = (secs, f"{m.group(1)}s {m.group(2)} {m.group(3).strip()}")
                return
            if line.strip() and not line.startswith("("):
                self._in_durations = False
        m = SECTION_RE.match(line)
        if m:
            name = m.group(1)
            for p in ("ERROR at setup of ", "ERROR at teardown of ", "ERROR collecting "):
                if name.startswith(p):
                    name = name[len(p):]
            self._section = name
            self._section_lines.setdefault(name, [])
            return
        if BANNER_RE.match(line):
            self._section = None
            return
        if self._section is not None:
            store = self._section_lines[self._section]
            m = E_LINE.match(line)
            if m and len(store) < MAX_REASON_LINES:
                text = (m.group(1) or "").strip()
                if text:
                    store.append(text)
                return
            m = LOC_LINE.match(line)
            if m and len(store) < MAX_REASON_LINES + 1:
                store.append(f"at {line.strip()}")
                return
        m = PYTEST_SUMMARY_LINE.match(line)
        if m and not m.group(2)[:1].isspace() and ("::" in m.group(2) or m.group(2).endswith(".py")):
            nodeid = m.group(2).strip()
            f = self._fail(f"pytest:{nodeid}", f"{m.group(1)} {nodeid}" + (f" - {m.group(3)}" if m.group(3) else ""))
            f.pytest_nodeid = nodeid  # type: ignore[attr-defined]
            return
        m = PYTEST_COUNTS.match(line) or PYTEST_NO_TESTS.match(line)
        if m:
            self._summary(line.strip("= ").strip())

    def _attach_pytest_sections(self) -> None:
        for f in self.failures.values():
            nodeid = getattr(f, "pytest_nodeid", None)
            if nodeid is None:
                continue
            path, _, rest = nodeid.partition("::")
            keys = [rest.replace("::", ".")] if rest else [path]
            if rest:
                keys.append(rest.rsplit("::", 1)[-1])
            for k in keys:
                if k in self._section_lines:
                    for r in self._section_lines[k]:
                        f.add(r)
                    break

    # ---------------------------------------------------------------------------------------------- faulthandler
    def _feed_faulthandler(self, line: str) -> bool:
        fh = self._fh
        assert fh is not None
        m = FH_THREAD.match(line)
        if m:
            fh["current"] = {"id": m.group(1), "label": m.group(2).strip(), "frames": []}
            fh["threads"].append(fh["current"])
            return True
        if FH_FRAME.match(line) and fh["current"] is not None:
            fh["current"]["frames"].append(line.strip())
            return True
        if not line.strip():
            return True
        self._close_faulthandler()
        return False

    def _close_faulthandler(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        out = [f"pytest faulthandler: Timeout ({fh['timeout']})! a test ran past faulthandler_timeout; stacks at "
               f"that moment (innermost frame first):"]
        threads = fh["threads"]
        main = next((t for t in threads if any("_pytest" in fr or "pluggy" in fr for fr in t["frames"])),
                    threads[-1] if threads else None)
        if main is not None:
            entry = None
            for i, fr in enumerate(main["frames"]):
                if "/_pytest/" in fr or "/pluggy/" in fr:
                    entry = main["frames"][i - 1] if i > 0 else None
                    break
            if entry:
                out.append(f"running test entry: {short_path(entry)}")
            out.append(f"thread {main['id']}{' ' + main['label'] if main['label'] else ''} (runs the test):")
            out += [f"  {short_path(fr)}" for fr in main["frames"][:MAX_FRAMES_MAIN]]
        others = [t for t in threads if t is not main]
        for t in others[:MAX_THREADS_OTHER]:
            out.append(f"thread {t['id']}{' ' + t['label'] if t['label'] else ''}:")
            out += [f"  {short_path(fr)}" for fr in t["frames"][:MAX_FRAMES_OTHER]]
        if len(others) > MAX_THREADS_OTHER:
            out.append(f"... {len(others) - MAX_THREADS_OTHER} more thread(s) in the log")
        self.timeouts.append(out)

    # ---------------------------------------------------------------------------------------------- cargo panic
    def _feed_panic(self, line: str) -> bool:
        assert self._panic is not None
        name, msg = self._panic
        s = line.rstrip()
        if s and not s.startswith(("note:", "stack backtrace:")) and len(msg) < MAX_REASON_LINES - 1:
            msg.append(s)
            return True
        self._close_panic()
        return False

    def _close_panic(self) -> None:
        if self._panic is None:
            return
        name, msg = self._panic
        self._panic = None
        f = self.failures.get(f"cargo:{name}")
        target = f if f is not None else self._fail(f"cargo-panic:{name}", f"thread '{name}' panicked")
        for r in msg + [self._panic_at]:
            target.add(r)

    # ------------------------------------------------------------------------------------------------- node TAP
    def _feed_tap(self, line: str) -> bool:
        t = self._tap
        assert t is not None
        ind = t["indent"]
        if re.match(r"^\s*\.\.\.\s*$", line) and len(line) - len(line.lstrip()) == ind + 2:
            self._close_tap()
            return True
        body_ind = len(line) - len(line.lstrip())
        if line.strip() and body_ind < ind + 2:
            self._close_tap()
            return False
        s = line.strip()
        if t["error_block"] is not None:
            if body_ind > ind + 2 and line.strip():
                if len(t["error_block"]) < MAX_REASON_LINES - 1:
                    t["error_block"].append(s)
                return True
            if not line.strip():
                return True
            t["error"] = t["error_block"]
            t["error_block"] = None
        m = re.match(r"^(\w+): (.*)$", s)
        if m:
            k, v = m.group(1), m.group(2)
            if k == "error":
                if v in ("|-", "|", ">-", ">", "|+"):
                    t["error_block"] = []
                else:
                    t["error"] = [v.strip("'\"")]
            elif k in ("location", "failureType"):
                t[k] = v.strip("'\"")
        return True

    def _close_tap(self) -> None:
        t = self._tap
        self._tap = None
        if t is None:
            return
        if t["error_block"] is not None:
            t["error"] = t["error_block"]
        if t.get("failureType") == "subtestsFailed":
            return
        loc = t.get("location")
        header = f"not ok - {t['name']}" + (f" ({short_path(loc)})" if loc else "")
        f = self._fail(f"node:{t['name']}:{loc}", header)
        for r in t.get("error") or []:
            f.add(r)

    # ------------------------------------------------------------------------------------------------ unittest
    def _feed_unittest(self, line: str) -> bool:
        """``FAIL: name (id)``, a ``-----`` rule, ``Traceback ...``, frames, the exception line(s), then the next
        ``=====`` / ``-----`` rule. Any other shape right after the ``FAIL:`` line was not unittest's: dropped."""
        u = self._ut
        assert u is not None
        s = line.rstrip()
        if u["stage"] == "sep":
            if s.startswith("-----"):
                u["stage"] = "tb"
                return True
            self._ut = None
            return False
        if u["stage"] == "tb":
            if s.startswith(TRACEBACK):
                u["stage"] = "body"
                return True
            if not s:
                return True
            # a failure without a traceback (subTest, a skip-style message): quote what is there
            u["stage"] = "body"
            u["exc"].append(s)
            return True
        if s.startswith(("=====", "-----")):
            self._close_unittest()
            return False
        if s.startswith(("During handling", "The above exception")) or s.startswith("  File "):
            u["exc"] = []
            return True
        if s and not s[0].isspace() and s != TRACEBACK and len(u["exc"]) < 4:
            u["exc"].append(s)
        return True

    def _close_unittest(self) -> None:
        u = self._ut
        self._ut = None
        if u is None or u["stage"] == "sep":
            return
        f = self._fail(f"unittest:{u['id']}", f"{u['kind']}: {u['name']} ({u['id']})")
        for r in u["exc"]:
            f.add(r)

    # ----------------------------------------------------------------------------------------------- traceback
    def _feed_traceback(self, line: str) -> None:
        tb = self._tb
        assert tb is not None
        s = line.rstrip()
        if s and not s[0].isspace() and not s.startswith(TRACEBACK):
            self._tb = None
            note = f"uncaught exception: {s}"
            if note not in self.notable:
                self.notable.append(note)

    # ------------------------------------------------------------------------------------------------- the rest
    def _feed_other(self, line: str) -> None:
        s = line.rstrip()
        if s == TRACEBACK:
            self._tb = []
            return
        m = HYG_VIOLATION.match(s) or HYG_PLUGIN.match(s)
        if m:
            self._fail(f"hygiene:{s}", s)
            return
        m = HYG_SUMMARY.match(s)
        if m:
            self._summary(s)
            return
        m = CARGO_FAILED.match(s)
        if m:
            self._fail(f"cargo:{m.group(1)}", s)
            return
        m = CARGO_PANIC.match(s)
        if m:
            self._close_panic()
            where = m.group(2)
            old = re.match(r"^'(.*)', (\S+)$", where)        # pre-1.73: panicked at 'msg', src/x.rs:1:2
            self._panic = (m.group(1), [old.group(1)] if old else [])
            self._panic_at = f"at {old.group(2) if old else where}"
            return
        if CARGO_RESULT.match(s):
            self._summary(s)
            return
        m = CARGO_ERROR.match(s)
        if m:
            if m.group(1) and len(self.notable) < 10:
                self.notable.append(s)
            elif s.startswith("error: test failed") or s.startswith("error: could not compile"):
                self._summary(s)
            return
        m = GO_RUN.match(s)
        if m:
            self._go_current = m.group(1)
            return
        m = GO_MSG.match(s)
        if m and self._go_current is not None:
            lst = self._go_msgs.setdefault(self._go_current, [])
            if len(lst) < MAX_REASON_LINES:
                lst.append(m.group(1))
            return
        m = GO_FAIL.match(s)
        if m:
            name = m.group(2)
            f = self._fail(f"go:{name}", f"--- FAIL: {name} ({m.group(3)})")
            for r in self._go_msgs.get(name, []):
                f.add(r)
            return
        m = GO_PKG_FAIL.match(s)
        if m:
            self._summary(s)
            return
        m = GO_PANIC.match(s)
        if m:
            self._fail(f"go-panic:{s}", s)
            return
        if GO_RACE.match(s):
            self._go_race = []
            return
        m = TAP_NOT_OK.match(line)
        if m and not re.search(r"\s#\s*(TODO|SKIP)\b", m.group(2)):
            self._tap = {"indent": len(m.group(1)), "name": m.group(2).strip(), "error": None, "error_block": None}
            return
        m = TAP_COUNT.match(s) or SPEC_COUNT.match(s)
        if m:
            self._tap_counts[m.group(1)] = m.group(2)
            return
        m = SPEC_FAIL.match(s)
        if m:
            self._fail(f"node-spec:{m.group(1)}", f"✖ {m.group(1)}")
            return
        m = UT_FAIL.match(s)
        if m:
            self._ut = {"kind": m.group(1), "name": m.group(2), "id": m.group(3), "stage": "sep", "exc": []}
            return
        m = UT_SUMMARY.match(s)
        if m:
            self._summary(s)
            return
        if UT_RAN.match(s):
            self._summary(s)
            return
        m = LIVE_FAIL.match(s)
        if m:
            self._fail(f"live:{m.group(1)}", m.group(1))
            return
        m = LIVE_RESULT.match(s)
        if m:
            self._summary(s)
            return
        m = WR_GATE.match(s)
        if m:
            self._summary(s)
            return
        m = WR_HEAD.match(s)
        if m:
            self._wr_section = m.group(1).startswith(("New MUST failures", "Errors"))
            return
        if s.startswith("#"):
            self._wr_section = False
        if self._wr_section:
            m = WR_ITEM.match(s)
            if m:
                self._fail(f"warroom:{m.group(1)}", f"war room: {m.group(1)}").add(m.group(2))

    # ---------------------------------------------------------------------------------------------------- end
    def finish(self) -> None:
        self._close_faulthandler()
        self._close_panic()
        self._close_tap()
        self._close_unittest()
        if self._go_race is not None:
            self._fail(f"race#{len(self.failures)}", "WARNING: DATA RACE").reasons.extend(self._go_race)
            self._go_race = None
        self._attach_pytest_sections()
        # a go parent test that failed only through its subtests adds nothing
        go = [k for k in self.failures if k.startswith("go:")]
        for k in go:
            name = k[3:]
            if not self.failures[k].reasons and any(o.startswith(f"go:{name}/") for o in go):
                del self.failures[k]
        if self._tap_counts:
            self._summary("node --test: " + ", ".join(f"{k} {v}" for k, v in self._tap_counts.items()))


# ===================================================================================================== emitting

def _message(lines: list[str], limit: int = MAX_MESSAGE) -> str:
    out: list[str] = []
    used = 0
    for i, ln in enumerate(lines):
        c = clean(ln)
        if used + len(c) + 1 > limit:
            out.append(f"… ({len(lines) - i} more line(s) in the log)")
            break
        out.append(c)
        used += len(c) + 1
    return "\n".join(out)


def _command(level: str, title: str, message: str) -> str:
    return f"::{level} title={escape_property(clean(title, 200))}::{escape_data(message)}"


def build(parser: Parser, code: str, suite: str, notice: bool = True) -> list[str]:
    """The workflow-command lines for this step, in the order they are printed."""
    ok = code == "0"
    summary = list(parser.summaries)
    if parser.slowest is not None:
        summary.append(f"slowest: {short_path(parser.slowest[1])}")
    if ok:
        if notice and summary:
            return [_command("notice", suite, _message(summary))]
        return []

    failures = list(parser.failures.values())
    timeouts = parser.timeouts
    errors: list[str] = []
    warnings: list[str] = []
    budget = MAX_ERRORS - 1                                  # the summary always gets the last error slot
    t_slots = min(len(timeouts), MAX_TIMEOUT_ERRORS, budget)
    f_slots = budget - t_slots
    for f in failures[:f_slots]:
        errors.append(_command("error", suite, _message(f.lines())))
    for t in timeouts[:t_slots]:
        errors.append(_command("error", suite, _message(t)))
    rest_items = [f.lines() for f in failures[f_slots:]] + list(timeouts[t_slots:])
    room = min(MAX_WARNINGS, MAX_ANNOTATIONS - len(errors) - 1)
    for item in rest_items[:max(room, 0)]:
        warnings.append(_command("warning", suite, _message(item)))
    unlisted = failures[f_slots + max(room, 0):] if len(rest_items) > max(room, 0) else []

    head = f"exit code {code}" + (f"; {len(failures)} failure(s) recognised" if failures else "") + \
        (f"; {len(timeouts)} faulthandler timeout dump(s)" if timeouts else "")
    lines = [head] + summary
    if unlisted:
        lines.append(f"{len(unlisted)} more failure(s) not annotated: " + "; ".join(f.header for f in unlisted))
    if failures or timeouts:
        errors.append(_command("error", suite, _message(lines)))
        return (errors + warnings)[:MAX_ANNOTATIONS]
    # Nothing recognised: the summary error carries the uncaught exceptions / compile errors seen and the LAST lines
    # of the log (the end is what matters, so the oldest tail lines are the ones dropped to fit).
    lines.append("no failing test recognised in the log")
    lines += parser.notable[:5]
    budget = MAX_MESSAGE + 1200 - sum(len(clean(x)) + 1 for x in lines)
    tail = [clean(f"| {x}", 200) for x in parser.tail]
    while tail and sum(len(x) + 1 for x in tail) > budget - 40:
        tail.pop(0)
    lines += [f"last {len(tail)} line(s) of the log:"] + tail if tail else ["the log is empty"]
    errors.append(_command("error", suite, _message(lines, MAX_MESSAGE + 1200)))
    return (errors + warnings)[:MAX_ANNOTATIONS]


def parse_lines(lines) -> Parser:
    p = Parser()
    for ln in lines:
        p.feed(ln)
    p.finish()
    return p


def annotate(log_path: str, code: str, suite: str, notice: bool = True) -> list[str]:
    if not os.path.isfile(log_path):
        if code == "0":
            return []
        return [_command("error", suite, _message([f"exit code {code}; the step's log file was not written "
                                                   f"({log_path})"]))]
    with open(log_path, "rb") as fh:
        p = parse_lines(b.decode("utf-8", errors="replace") for b in fh)
    return build(p, code, suite, notice)


def _norm_code(raw: str) -> str:
    raw = (raw or "").strip()
    return raw if re.fullmatch(r"-?\d{1,4}", raw) else f"unknown ({raw[:20]!r})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", required=True, help="the step's tee'd log file")
    ap.add_argument("--code", required=True, help="the suite's exit code (${PIPESTATUS[0]})")
    ap.add_argument("--suite", required=True, help="label used as every annotation's title")
    ap.add_argument("--no-notice", action="store_true", help="on exit code 0 print nothing at all")
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 0                                             # never fail the step, not even on a usage error
    try:
        out = annotate(a.log, _norm_code(a.code), a.suite, notice=not a.no_notice)
    except Exception as e:  # noqa: BLE001 - the step's result must never depend on this script
        out = [_command("warning", "ci_annotate", f"ci_annotate could not read the log: {type(e).__name__}")]
    try:
        sys.stdout.flush()
        sys.stdout.buffer.write("".join(line + "\n" for line in out).encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:  # noqa: BLE001 - a closed or odd stdout must not fail the step either
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
