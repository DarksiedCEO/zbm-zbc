"""Self-test of devtools/ci_annotate.py (the step-log -> GitHub annotation converter). Standard library only; CI runs it
in the `hygiene-static` job:

    python3 -B -m unittest devtools/test_ci_annotate.py -v

The sample logs below are trimmed copies of real output (pytest 9.1 with -rfEs and a faulthandler timeout, cargo
1.97's libtest, go 1.24 `go test -v`, Node 22's `node --test` TAP, unittest, devtools/hygiene_check.py run, a
department live_run.py, the war room report). Secret-shaped values are assembled at run time so the secret scan
never sees one in this file.
"""

from __future__ import annotations

import os
import random
import re
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "devtools"))

import ci_annotate as ca  # noqa: E402

CI = REPO / ".github" / "workflows" / "ci.yml"

PYTEST_LOG = """\
hygiene_check: suite python:demo-py in /w/services/demo-py
  private TMPDIR /runner/_temp/hyg-python-demo-py-x/tmp
  subreaper True
  command /usr/bin/python -c 'import sys; ...' -q -rfEs -p no:cacheprovider
.FETimeout (0:10:00)!
Thread 0x00007f56020756c0 (most recent call first):
  File "/w/services/demo-py/tests/test_x.py", line 8 in <lambda>
  File "/usr/lib/python3.13/threading.py", line 995 in run
  File "/usr/lib/python3.13/threading.py", line 1044 in _bootstrap_inner
  File "/usr/lib/python3.13/threading.py", line 1015 in _bootstrap

Thread 0x00007f56043e7080 (most recent call first):
  File "/usr/lib/python3.13/socket.py", line 295 in accept
  File "/w/services/demo-py/tests/test_x.py", line 9 in test_slow
  File "/venv/lib/python3.13/site-packages/_pytest/python.py", line 167 in pytest_pyfunc_call
  File "/venv/lib/python3.13/site-packages/pluggy/_callers.py", line 121 in _multicall
  File "/venv/lib/python3.13/site-packages/_pytest/main.py", line 384 in _main
.s                                                                    [100%]
==================================== ERRORS ====================================
__________________________ ERROR at setup of test_err __________________________
file /w/services/demo-py/tests/test_x.py, line 6
  def test_err(fixture_missing): pass
E       fixture 'fixture_missing' not found
>       available fixtures: capfd, capsys, tmp_path

/w/services/demo-py/tests/test_x.py:6
=================================== FAILURES ===================================
________________________________ TestCls.test_bad ________________________________

    def test_bad(self):
        x = {"a": 1}
>       assert x == {"a": 2}, "dicts differ"
E       AssertionError: dicts differ
E       assert {'a': 1} == {'a': 2}
E
E         Differing items:
E         {'a': 1} != {'a': 2}

tests/test_x.py:5: AssertionError
----------------------------- Captured log call ------------------------------
ERROR    root:test_x.py:4 a logged error that is not a test failure
============================= slowest 10 durations =============================
612.03s call     tests/test_x.py::test_slow
1.20s setup    tests/test_x.py::test_err

(3 durations < 0.005s hidden.  Use -vv to show these durations.)
=========================== short test summary info ============================
FAILED tests/test_x.py::TestCls::test_bad - AssertionError: dicts differ
ERROR tests/test_x.py::test_err
SKIPPED [1] tests/test_x.py:11: nope
1 failed, 2 passed, 1 skipped, 1 error in 613.70s (0:10:13)
hygiene_check: suite python:demo-py exited 1; tests counted: 5; skips: 1; hygiene violations: 1
HYGIENE R3-tmp python:demo-py: left in the private TMPDIR: pytest-of-runner
"""

CARGO_LOG = """\
\x1b[1m\x1b[92m     Running\x1b[0m unittests src/lib.rs (target/debug/deps/cprobe-58c3cfa458a1b080)

running 3 tests
test tests::ok ... ok
test tests::eq_fails ... FAILED
test tests::panics ... FAILED

failures:

---- tests::eq_fails stdout ----

thread 'tests::eq_fails' (491) panicked at src/lib.rs:4:29:
assertion `left == right` failed: math is off
  left: 2
 right: 3
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

---- tests::panics stdout ----

thread 'tests::panics' panicked at 'boom: 42', src/lib.rs:5:27


failures:
    tests::eq_fails
    tests::panics

test result: FAILED. 1 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

\x1b[1m\x1b[91merror\x1b[0m: test failed, to rerun pass `--lib`
"""

GO_LOG = """\
=== RUN   TestOK
--- PASS: TestOK (0.00s)
=== RUN   TestBad
    x_test.go:4: got 1 want 2
--- FAIL: TestBad (0.00s)
=== RUN   TestSub
=== RUN   TestSub/inner
    x_test.go:5: inner broke
--- FAIL: TestSub (0.00s)
    --- FAIL: TestSub/inner (0.00s)
FAIL
FAIL\tgprobe\t0.007s
FAIL
"""

NODE_LOG = """\
TAP version 13
# Subtest: adds
ok 1 - adds
  ---
  duration_ms: 1.370923
  ...
# Subtest: money rounds
not ok 2 - money rounds
  ---
  duration_ms: 1.76603
  location: '/w/apps/dashboard-ts/tests/money.test.ts:4:1'
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly equal:

    0.30000000000000004 !== 0.3
  code: 'ERR_ASSERTION'
  stack: |-
    TestContext.<anonymous> (file:///w/apps/dashboard-ts/tests/money.test.ts:4:35)
  ...
# Subtest: group
    # Subtest: inner bad
    not ok 1 - inner bad
      ---
      duration_ms: 0.329268
      location: '/w/apps/dashboard-ts/tests/auth.test.ts:7:11'
      failureType: 'testCodeFailure'
      error: 'session cookie missing'
      code: 'ERR_TEST_FAILURE'
      ...
    1..1
not ok 3 - group
  ---
  duration_ms: 1.638992
  location: '/w/apps/dashboard-ts/tests/auth.test.ts:5:1'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  ...
# Subtest: skipped
ok 4 - skipped # SKIP not here
  ---
  duration_ms: 2.451429
  ...
1..4
# tests 5
# pass 1
# fail 2
# cancelled 0
# skipped 1
"""

UNITTEST_LOG = """\
test_bad (test_u.T.test_bad) ... FAIL
test_ok (test_u.T.test_ok) ... ok

======================================================================
FAIL: test_bad (test_u.T.test_bad)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/w/devtools/test_u.py", line 4, in test_bad
    def test_bad(self): self.assertEqual("a", "b", "letters differ")
                        ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^
AssertionError: 'a' != 'b'
- a
+ b
 : letters differ

----------------------------------------------------------------------
Ran 2 tests in 0.002s

FAILED (failures=1)
"""

LIVE_LOG = """\
[    0.01] starting the real ledger-rust binary
[    1.20]   CHECK PASS: ledger verifies after the first commit
[    2.50]   CHECK FAIL: refund is refused without a second approver
::error title=sales-py live run::check failed: refund is refused without a second approver
[12:00:01] MISMATCH: payout held (observed: 'released')
[    3.00] 11/12 checks passed; FAILED: ['refund is refused without a second approver']
hygiene_check: suite live:sales-py exited 1; tests counted: None; skips: 0; hygiene violations: 0
"""

WARROOM_LOG = """\
# War room report (seed 1)

Gate: **FAIL**. LLM personas: not connected (deterministic personas only).

## sales-py

### New MUST failures (gate-blocking)

- `sales-py/opt-out#1.2@1` chain i10>i12: opt-out honoured within one reply

### Errors

- `sales-py/flood#0.0@1` chain i01: worker timed out

### Known failures

- `sales-py/known#0.1@1` chain i03 finding WR-F001: still failing
"""


def _rand(n: int, alphabet: str, seed: int) -> str:
    r = random.Random(seed)
    return "".join(r.choice(alphabet) for _ in range(n))


def parse(text: str) -> ca.Parser:
    return ca.parse_lines(text.splitlines(keepends=True))


def run(text: str, code: str = "1", suite: str = "python:demo-py", notice: bool = True) -> list[str]:
    return ca.build(parse(text), code, suite, notice)


def unescape(msg: str) -> str:
    return msg.replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")


def split(cmd: str) -> tuple[str, str, str]:
    m = re.fullmatch(r"::(error|warning|notice) title=([^:]*)::(.*)", cmd)
    assert m, f"not a well-formed workflow command: {cmd!r}"
    return m.group(1), m.group(2), unescape(m.group(3))


class Samples(unittest.TestCase):
    def test_pytest_failures_reasons_timeout_hygiene_and_summary(self):
        out = run(PYTEST_LOG)
        parts = [split(c) for c in out]
        self.assertTrue(all(lvl == "error" for lvl, _, _ in parts), out)
        self.assertTrue(all(t == "python%3Ademo-py" for _, t, _ in parts), out)
        msgs = [m for _, _, m in parts]
        bad = next(m for m in msgs if m.startswith("FAILED tests/test_x.py::TestCls::test_bad"))
        self.assertIn("assert {'a': 1} == {'a': 2}", bad)
        self.assertIn("at tests/test_x.py:5: AssertionError", bad)
        err = next(m for m in msgs if m.startswith("ERROR tests/test_x.py::test_err"))
        self.assertIn("fixture 'fixture_missing' not found", err)
        hyg = next(m for m in msgs if m.startswith("HYGIENE R3-tmp"))
        self.assertIn("pytest-of-runner", hyg)
        fh = next(m for m in msgs if "faulthandler" in m)
        self.assertIn("Timeout (0:10:00)!", fh)
        self.assertIn('running test entry: File "/w/services/demo-py/tests/test_x.py", line 9 in test_slow', fh)
        self.assertIn("socket.py", fh)                      # the innermost frame of the thread running the test
        self.assertIn("in <lambda>", fh)                    # ... and the other thread's
        self.assertIn('File "site-packages/_pytest/python.py"', fh)
        # the summary is the last error and carries the count line, the slowest test and the hygiene line
        last = msgs[-1]
        self.assertTrue(last.startswith("exit code 1; 3 failure(s) recognised; 1 faulthandler"), last)
        self.assertIn("1 failed, 2 passed, 1 skipped, 1 error in 613.70s (0:10:13)", last)
        self.assertIn("slowest: 612.03s call tests/test_x.py::test_slow", last)
        self.assertIn("hygiene_check: suite python:demo-py exited 1", last)
        # a captured log line starting with ERROR is not a failing test
        self.assertFalse(any("a logged error" in m.splitlines()[0] for m in msgs))

    def test_cargo(self):
        msgs = [split(c)[2] for c in run(CARGO_LOG, "101", "rust:ledger-rust")]
        self.assertEqual(len(msgs), 3, msgs)
        self.assertEqual(msgs[0].splitlines(), ["test tests::eq_fails ... FAILED",
                                                "assertion `left == right` failed: math is off",
                                                "  left: 2", " right: 3", "at src/lib.rs:4:29"])
        self.assertEqual(msgs[1].splitlines(), ["test tests::panics ... FAILED", "boom: 42", "at src/lib.rs:5:27"])
        self.assertIn("test result: FAILED. 1 passed; 2 failed", msgs[2])
        self.assertIn("error: test failed, to rerun pass `--lib`", msgs[2])   # ANSI colour stripped
        self.assertNotIn("\x1b", "".join(msgs))

    def test_go_parent_of_failed_subtest_is_dropped(self):
        msgs = [split(c)[2] for c in run(GO_LOG, "1", "go:orchestrator-go")]
        self.assertEqual(msgs[0].splitlines(), ["--- FAIL: TestBad (0.00s)", "x_test.go:4: got 1 want 2"])
        self.assertEqual(msgs[1].splitlines(), ["--- FAIL: TestSub/inner (0.00s)", "x_test.go:5: inner broke"])
        self.assertEqual(len(msgs), 3)
        self.assertIn("FAIL    gprobe    0.007s", msgs[2])

    def test_node_tap(self):
        msgs = [split(c)[2] for c in run(NODE_LOG, "1", "node:dashboard-ts")]
        self.assertEqual(len(msgs), 3, msgs)
        self.assertTrue(msgs[0].startswith("not ok - money rounds (/w/apps/dashboard-ts/tests/money.test.ts:4:1)"))
        self.assertIn("0.30000000000000004 !== 0.3", msgs[0])
        self.assertEqual(msgs[1].splitlines(), ["not ok - inner bad (/w/apps/dashboard-ts/tests/auth.test.ts:7:11)",
                                                "session cookie missing"])
        self.assertIn("node --test: tests 5, pass 1, fail 2", msgs[2])
        self.assertNotIn("group", "".join(msgs))            # failed only through its subtest

    def test_unittest(self):
        msgs = [split(c)[2] for c in run(UNITTEST_LOG, "1", "devtools:test_u")]
        self.assertEqual(msgs[0].splitlines(), ["FAIL: test_bad (test_u.T.test_bad)", "AssertionError: 'a' != 'b'",
                                                "- a", "+ b"])
        self.assertIn("FAILED (failures=1)", msgs[1])

    def test_live_run(self):
        msgs = [split(c)[2] for c in run(LIVE_LOG, "1", "live:sales-py")]
        self.assertEqual(msgs[0], "CHECK FAIL: refund is refused without a second approver")
        self.assertEqual(msgs[1], "MISMATCH: payout held (observed: 'released')")
        self.assertEqual(len(msgs), 3, msgs)                # the script's own ::error line is not repeated
        self.assertIn("11/12 checks passed", msgs[2])

    def test_warroom(self):
        msgs = [split(c)[2] for c in run(WARROOM_LOG, "1", "warroom:all")]
        self.assertEqual(msgs[0].splitlines(), ["war room: sales-py/opt-out#1.2@1",
                                                "chain i10>i12: opt-out honoured within one reply"])
        self.assertEqual(msgs[1].splitlines()[0], "war room: sales-py/flood#0.0@1")
        self.assertEqual(len(msgs), 3, msgs)                # known failures are not gate failures
        self.assertIn("Gate: **FAIL**", msgs[2])

    def test_unrecognised_failure_quotes_the_tail(self):
        text = "".join(f"progress line {i}\n" for i in range(200)) + "Traceback (most recent call last):\n" \
            "  File \"x.py\", line 1, in <module>\n" "OSError: [Errno 28] No space left on device\n" "Killed\n"
        (lvl, _, msg), = [split(c) for c in run(text, "137")]
        self.assertEqual(lvl, "error")
        self.assertTrue(msg.startswith("exit code 137"), msg)
        self.assertIn("no failing test recognised", msg)
        self.assertIn("uncaught exception: OSError: [Errno 28] No space left on device", msg)
        self.assertTrue(msg.endswith("| Killed"), msg)     # the END of the log survives the length cut
        self.assertNotIn("progress line 0\n", msg)
        self.assertLessEqual(len(msg), ca.MAX_MESSAGE + 1200 + 100)


class ExitZero(unittest.TestCase):
    def test_green_step_emits_only_a_notice(self):
        green = ("....  [100%]\n============================= slowest 10 durations ============================\n"
                 "41.00s call     tests/test_y.py::test_live\n\n"
                 "120 passed in 95.10s (0:01:35)\n"
                 "hygiene_check: suite python:demo-py exited 0; tests counted: 120; skips: 0; hygiene violations: 0\n")
        out = run(green, "0")
        self.assertEqual(len(out), 1)
        lvl, _, msg = split(out[0])
        self.assertEqual(lvl, "notice")
        self.assertEqual(msg.splitlines(), ["120 passed in 95.10s (0:01:35)",
                                            "hygiene_check: suite python:demo-py exited 0; tests counted: 120; "
                                            "skips: 0; hygiene violations: 0",
                                            "slowest: 41.00s call tests/test_y.py::test_live"])
        self.assertEqual(run(green, "0", notice=False), [])

    def test_exit_zero_never_errors_even_with_failure_lines(self):
        # e.g. a suite whose tests print expected-failure output and still pass
        out = run(PYTEST_LOG, "0")
        self.assertTrue(all(split(c)[0] == "notice" for c in out), out)
        self.assertEqual(run("", "0"), [])


class EmptyAndMissing(unittest.TestCase):
    def test_missing_log(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.log")
            self.assertEqual(ca.annotate(missing, "0", "s"), [])
            (lvl, _, msg), = [split(c) for c in ca.annotate(missing, "2", "s")]
            self.assertEqual(lvl, "error")
            self.assertIn("exit code 2; the step's log file was not written", msg)

    def test_empty_log(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.log"
            p.write_bytes(b"")
            self.assertEqual(ca.annotate(str(p), "0", "s"), [])
            (lvl, _, msg), = [split(c) for c in ca.annotate(str(p), "1", "s")]
            self.assertEqual(msg.splitlines(), ["exit code 1", "no failing test recognised in the log",
                                                "the log is empty"])

    def test_undecodable_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bin.log"
            p.write_bytes(b"\xff\xfe\x00junk\nFAILED tests/t.py::test_a - boom \xc3\x28\n")
            (first, _) = ca.annotate(str(p), "1", "s")
            self.assertTrue(split(first)[2].startswith("FAILED tests/t.py::test_a - boom"))


class EscapingAndInjection(unittest.TestCase):
    def test_escape_rules(self):
        self.assertEqual(ca.escape_data("100%\r\nnext"), "100%25%0D%0Anext")
        self.assertEqual(ca.escape_property("a:b,c%d\ne"), "a%3Ab%2Cc%25d%0Ae")
        self.assertEqual(ca.escape_data("%0A"), "%250A")       # a literal %0A in the log stays literal

    def test_log_cannot_start_a_command_or_break_properties(self):
        evil = ("____ test_x ____\n"
                "E   ::set-env name=PATH::/evil\n"
                "E   %0A::error::forged via an encoded newline\n"
                "E   ##[set-output name=x;]y\n"
                "E   line with a\rcarriage return\n"
                "==== short test summary info ====\n"
                "FAILED tests/t.py::test_x - ::add-mask::oops\n"
                "1 failed in 0.10s\n")
        out = run(evil, "1", "python:x, y::z")
        for cmd in out:
            # one physical line each, and only this script's own command at its start
            self.assertNotIn("\n", cmd)
            self.assertNotIn("\r", cmd)
            lvl, title, msg = split(cmd)
            self.assertEqual(title, "python%3Ax%2C y%3A%3Az")
            body = cmd.split("::", 2)[2]
            self.assertNotIn("%0A::", body)                     # no escaped line may begin with ::
            self.assertNotRegex(body, r"(^|%0A)\s*##\[")
            for ln in msg.splitlines():
                self.assertFalse(ln.lstrip().startswith("::"), ln)
                self.assertFalse(ln.lstrip().startswith("##["), ln)
        first = split(out[0])[2].splitlines()
        self.assertIn(": :set-env name=PATH::/evil", first)
        self.assertIn("%0A::error::forged via an encoded newline", first)   # still the literal text, inert
        self.assertIn("# #[set-output name=x;]y", first)

    def test_workflow_command_lines_in_the_log_are_not_repeated(self):
        out = run("::error title=x::already annotated\n::warning::w\n", "1")
        (lvl, _, msg), = [split(c) for c in out]
        self.assertNotIn("already annotated", msg.split("last", 1)[0])
        self.assertIn("| ::error title=x::already annotated", msg)     # quoted in the tail behind "| ": inert

    def test_a_bare_carriage_return_in_a_reason_keeps_the_failed_line(self):
        # fix wave I (AEGIS CI-L2): only the text after the last "\r" was kept, so the FAILED line was lost
        text = ("==== short test summary info ====\n"
                "FAILED tests/t.py::test_cr - AssertionError: assert 'a\rb' == 'ab'\n"
                "progress [ 10%]\rprogress [ 50%]\rprogress [100%]\n"
                "1 failed in 0.10s\n")
        # the lines as annotate() reads a log file: split on "\n" only (str.splitlines would split on "\r" itself)
        p = ca.parse_lines(ln + "\n" for ln in text.split("\n")[:-1])
        out = [split(c) for c in ca.build(p, "1", "python:demo-py", True)]
        lvl, _, msg = out[0]
        self.assertEqual(lvl, "error")
        self.assertTrue(msg.startswith("FAILED tests/t.py::test_cr - AssertionError: assert 'a"), msg)
        self.assertEqual(len(p.failures), 1, list(p.failures))
        tail = list(p.tail)
        self.assertIn("progress [100%]", tail)                              # the redraw the terminal showed
        self.assertNotIn("progress [ 10%]", tail)                           # ... and not the ones it overwrote
        with tempfile.TemporaryDirectory() as d:                            # and through the file path itself
            log = Path(d) / "step.log"
            log.write_bytes(text.encode())
            cmds = ca.annotate(str(log), "1", "python:demo-py")
        self.assertTrue(split(cmds[0])[2].startswith("FAILED tests/t.py::test_cr - "), cmds[0])
        for cmd in cmds:
            self.assertNotIn("\r", cmd)

    def test_control_characters_are_removed(self):
        (_, _, msg), _ = [split(c) for c in run("FAILED t.py::test_a - bell\x07 nul\x00 esc\x1b[31mred\n", "1")]
        self.assertEqual(msg, "FAILED t.py::test_a - bell? nul? escred")


class Caps(unittest.TestCase):
    def test_counts_and_order(self):
        text = "".join(f"FAILED tests/test_many.py::test_{i:03d} - AssertionError: n{i}\n" for i in range(60))
        text += "60 failed in 9.00s\n"
        out = run(text, "1")
        levels = [split(c)[0] for c in out]
        self.assertLessEqual(len(out), ca.MAX_ANNOTATIONS)
        self.assertEqual(levels.count("error"), ca.MAX_ERRORS)
        self.assertEqual(levels.count("warning"), ca.MAX_WARNINGS)
        self.assertEqual(levels[:ca.MAX_ERRORS], ["error"] * ca.MAX_ERRORS)   # errors first: they are what is kept
        msgs = [split(c)[2] for c in out]
        self.assertTrue(msgs[0].startswith("FAILED tests/test_many.py::test_000"))
        self.assertTrue(msgs[8].startswith("FAILED tests/test_many.py::test_008"))
        summary = msgs[ca.MAX_ERRORS - 1]
        self.assertTrue(summary.startswith("exit code 1; 60 failure(s) recognised"), summary)
        self.assertIn("60 failed in 9.00s", summary)
        self.assertIn("41 more failure(s) not annotated: FAILED tests/test_many.py::test_019", summary)
        self.assertTrue(msgs[ca.MAX_ERRORS].startswith("FAILED tests/test_many.py::test_009"))

    def test_line_and_message_length(self):
        long_reason = "x" * 5000
        text = f"FAILED t.py::test_a - {long_reason}\n" + "".join(f"E   {'y' * 400} {i}\n" for i in range(50))
        text = "____ test_a ____\n" + "".join(f"E   {'y' * 400} {i}\n" for i in range(50)) + \
            "==== short test summary info ====\n" + f"FAILED t.py::test_a - {long_reason}\n"
        for cmd in run(text, "1"):
            msg = split(cmd)[2]
            self.assertLessEqual(len(msg), ca.MAX_MESSAGE + 100)
            for ln in msg.splitlines():
                self.assertLessEqual(len(ln), ca.MAX_LINE)
        first = split(run(text, "1")[0])[2].splitlines()
        self.assertLessEqual(len(first), 1 + ca.MAX_REASON_LINES)

    def test_a_long_reason_cut_in_the_header_survives_in_its_e_line(self):
        reason = "AssertionError: baseline 74 MiB; per-round peaks [" + ", ".join(["209"] * 120) + "]; tail END"
        text = "____ test_n2 ____\nE   " + reason + "\nE   assert 25 < 24\n" \
            "==== short test summary info ====\n" f"FAILED tests/t.py::test_n2 - {reason}\n"
        msg = split(run(text, "1")[0])[2].splitlines()
        self.assertTrue(msg[0].endswith("…"), msg[0])          # the header line is cut ...
        self.assertTrue(msg[1].startswith("AssertionError: baseline 74 MiB"))   # ... its E line is quoted too
        self.assertIn("assert 25 < 24", msg)
        short = "____ test_s ____\nE   AssertionError: short\n==== short test summary info ====\n" \
            "FAILED tests/t.py::test_s - AssertionError: short\n"
        self.assertEqual(split(run(short, "1")[0])[2].splitlines(),
                         ["FAILED tests/t.py::test_s - AssertionError: short"])   # a short one is not repeated

    def test_a_timeout_dump_keeps_an_error_slot_among_many_failures(self):
        text = "".join(f"FAILED t.py::test_{i} - e\n" for i in range(30))
        text = "Timeout (0:10:00)!\nThread 0x01 (most recent call first):\n  File \"t.py\", line 3 in test_hang\n" + \
            "  File \"/v/site-packages/_pytest/python.py\", line 1 in pytest_pyfunc_call\n\n" + text
        msgs = [split(c) for c in run(text, "1")]
        errors = [m for lvl, _, m in msgs if lvl == "error"]
        self.assertEqual(len(errors), ca.MAX_ERRORS)
        self.assertTrue(any("Timeout (0:10:00)!" in m for m in errors))
        self.assertTrue(errors[-1].startswith("exit code 1"))


class Redaction(unittest.TestCase):
    def test_tokens_and_credentials(self):
        alnum = string.ascii_letters + string.digits
        gh = "gh" + "p_" + _rand(36, alnum, 1)
        stripe = "sk" + "_live_" + _rand(24, alnum, 2)
        jwt = "ey" + "J" + _rand(20, alnum, 3) + "." + _rand(30, alnum, 4) + "." + _rand(30, alnum, 5)
        b64 = _rand(40, alnum + "+/", 6) + "=="
        hexkey = _rand(64, "0123456789abcdef", 7)
        bearer = _rand(28, alnum, 8)
        pw = _rand(12, alnum, 9)
        secrets = [gh, stripe, jwt, b64, hexkey, bearer, pw]
        lines = [f"Authorization: Bearer {bearer}", f"curl -H 'X-Api-Key: {b64}'", f"token={gh}",
                 f"stripe key {stripe} rejected", f"id_token {jwt}", f"digest {hexkey}",
                 f'{{"password": "{pw}"}}', f"https://bot:{pw}@example.invalid/repo.git"]
        text = "".join(f"E   {ln}\n" for ln in lines)
        text = "____ test_a ____\n" + text + "==== short test summary info ====\nFAILED t.py::test_a - leaked\n"
        out = "\n".join(run(text, "1"))
        for s in secrets:
            self.assertNotIn(s, out)
            self.assertNotIn(s[:20], out)
        self.assertIn("[REDACTED]", out)

    def test_env_style_keys_with_an_underscore_before_the_secret_word(self):
        # fix wave I (AEGIS CI-L1): "_" is a word character, so the key's \b never matched "DB_PASSWORD"
        pw = "hunter2" + "hunter2"
        tok = "abcdef" + "abcdef"
        forms = [f"DB_PASSWORD={pw}", f"export DB_PASSWORD={pw}", f"DB_PASSWORD: {pw}", f'DB_PASSWORD="{pw}"',
                 f"'LEDGER_SERVICE_TOKEN': '{tok}'", f'"LEDGER_SERVICE_TOKEN": "{tok}"',
                 f"{{'LEDGER_SERVICE_TOKEN': '{tok}', 'DB_PASSWORD': '{pw}'}}",
                 f"environ({{'HOME': '/root', 'LEDGER_SERVICE_TOKEN': '{tok}', 'DB_PASSWORD': '{pw}'}})",
                 f"STRIPE_CLIENT_SECRET={tok}", f"X-Ledger-Api-Key: {tok}", f"GH_ACCESS_TOKEN = {tok}"]
        for line in forms:
            r = ca.redact(line)
            self.assertNotIn(pw, r, line)
            self.assertNotIn(tok, r, line)
            self.assertIn("[REDACTED]", r, line)
        self.assertEqual(ca.redact(f"{{'HOME': '/root', 'LEDGER_SERVICE_TOKEN': '{tok}'}}"),
                         "{'HOME': '/root', 'LEDGER_SERVICE_TOKEN': '[REDACTED]'}")      # the key and the rest are kept
        # end to end, from an E line of a failing test
        text = ("____ test_env ____\n" f"E   AssertionError: environ({{'DB_PASSWORD': '{pw}', 'LEDGER_SERVICE_TOKEN': "
                f"'{tok}'}})\n" "==== short test summary info ====\n" f"FAILED t.py::test_env - DB_PASSWORD={pw}\n")
        out = "\n".join(run(text, "1"))
        self.assertNotIn(pw, out)
        self.assertNotIn(tok, out)
        self.assertIn("DB_PASSWORD=[REDACTED]", out)

    def test_a_secret_word_inside_a_longer_word_is_not_a_key(self):
        for k in ("tokenizer=wordpiece", "passwords_checked: 1200", "mytoken=abcdefgh", "test_password_reset: passed"):
            self.assertEqual(ca.redact(k), k)

    def test_ordinary_text_is_kept(self):
        keep = ["tests/test_round22.py::test_live_tests_leave_git_status_unchanged",
                "services/delivery-py/tests/test_live_tracked_files.py:47",
                "AssertionError: expected 408 Request Timeout, got 200",
                "test_payout_is_held_until_the_second_approver_signs_off_today"]
        for k in keep:
            self.assertEqual(ca.redact(k), k)

    def test_redaction_before_cutting(self):
        secret = _rand(400, string.ascii_letters + string.digits, 10)
        c = ca.clean("value " + secret)
        self.assertNotIn(secret[:31], c)


class CommandLine(unittest.TestCase):
    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", str(REPO / "devtools" / "ci_annotate.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_cli_never_fails(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "t.log"
            log.write_text(CARGO_LOG)
            r = self._cli("--log", str(log), "--code", "101", "--suite", "rust:ledger-rust")
            self.assertEqual(r.returncode, 0)
            self.assertEqual(len(r.stdout.splitlines()), 3)
            self.assertTrue(all(ln.startswith("::error title=rust%3Aledger-rust::") for ln in r.stdout.splitlines()))
            self.assertEqual(self._cli("--bogus").returncode, 0)                        # usage error
            r = self._cli("--log", d, "--code", "1", "--suite", "s")                   # a directory, not a file
            self.assertEqual(r.returncode, 0)
            r = self._cli("--log", str(log), "--code", "", "--suite", "s")             # PIPESTATUS went missing
            self.assertEqual(r.returncode, 0)
            self.assertIn("exit code unknown", r.stdout)


class Workflow(unittest.TestCase):
    """Every test-running step of ci.yml tees its output and annotates, with the suite's exit code kept."""

    def _steps(self) -> list[tuple[str, str, str]]:
        """(name, the step's YAML text, its `run:` script) for every step of every job. A step starts at
        `      - ` and ends at the next line indented 6 or less."""
        steps, cur = [], None
        for line in CI.read_text().splitlines() + ["END"]:
            if cur is not None and line.strip() and len(line) - len(line.lstrip()) <= 6 and \
                    not line.startswith("        "):
                steps.append(cur)
                cur = None
            if line.startswith("      - "):
                cur = [line]
            elif cur is not None:
                cur.append(line)
        out = []
        for body in steps:
            m = re.match(r"^      - (?:name: (.*)|.*)$", body[0])
            name = (m.group(1) or body[0].strip()) if m else body[0].strip()
            script, in_run = [], False
            for ln in body:
                r = re.match(r"^\s+(?:- )?run: ?(.*)$", ln)
                if r:
                    in_run = True
                    if r.group(1) not in ("|", ">", ""):
                        script.append(r.group(1))
                    continue
                if in_run:
                    if ln.strip() and len(ln) - len(ln.lstrip()) <= 8:
                        in_run = False
                        continue
                    script.append(ln)
            out.append((name, "\n".join(body), "\n".join(script)))
        return out

    def test_every_test_step_is_wrapped(self):
        runners = re.compile(r"hygiene_check\.py (?:run|lint)|-m pytest|-m unittest|cargo test|go test|npm test")
        wrapped = []
        for name, body, script in self._steps():
            if not runners.search(script):
                continue
            wrapped.append(name)
            with self.subTest(step=name):
                self.assertRegex(body, r"\n        shell: bash\n")
                self.assertEqual(script.strip().splitlines()[0].strip(), "set +e")
                self.assertRegex(script, r'2>&1 \| tee "\$RUNNER_TEMP/[\w.-]+\.log"\n')
                log = re.search(r'tee "\$RUNNER_TEMP/([\w.-]+\.log)"', script).group(1)
                self.assertIn("\n          code=${PIPESTATUS[0]}\n", script)
                self.assertRegex(script, r'ci_annotate\.py" --log "\$RUNNER_TEMP/' + re.escape(log) +
                                 r'" --code "\$code"|ci_annotate\.py --log "\$RUNNER_TEMP/' + re.escape(log) +
                                 r'" --code "\$code"')
                self.assertTrue(script.rstrip().endswith('exit "$code"'), script)
                if "-m pytest" in script:
                    self.assertIn("-rfEs", script)
                    self.assertRegex(script, r"faulthandler_timeout=(\d+|\$FAULTHANDLER_TIMEOUT)")
        # python-tests, delivery-py, ledger-rust, orchestrator-go, dashboard-ts, live-runs, delivery-docker-live,
        # the war room's three, hygiene-static's three self-tests and its lint
        self.assertGreaterEqual(len(wrapped), 14, wrapped)


if __name__ == "__main__":
    unittest.main()
