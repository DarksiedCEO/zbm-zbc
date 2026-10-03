"""Self-test of devtools/hygiene_check.py (fix wave 25, R-HYGIENE): every rule FAILS on a planted violation and the
clean probe passes. Standard library only (unittest); CI runs it in the `hygiene-static` job:

    python3 -B -m unittest devtools/test_hygiene_check.py -v

Each case copies devtools/ into a fresh throwaway git repository (in a private temp dir), plants one violation
there, and runs the checker from THAT copy, so the real checkout is never touched. The Python-suite path (the
pytest plugin) is exercised by every CI Python job; here the dynamic rules run through `--kind none` / `--kind go`
with small shell commands, which reach the same code; the `Pytest` cases run a planted pytest suite through the
plugin (pytest must be importable by this interpreter — CI installs the services' pinned pytest).

Fix wave 25 (E-C, after review of the E0 commit): pytest-kind cases, a plain background child, counts written and
checked, cargo/node parsing, expected skips, other-language and named-constant L1/L2, wrapped-line and
commit-pinned L3, a graceful_close.py that differs from its pin."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The macOS process path, forced on Linux (fix wave 26a, W26-1): no child subreaper, and the process table read with
# `ps -axo ...` instead of /proc — what every macOS CI job does. Done by patching the checker module from outside
# (the same two names exist in every version of the checker), so this harness also runs the pre-fix checker.
PORTABLE_HARNESS = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hygiene_check as h
if sys.argv[2] != "--keep-subreaper":
    h._set_subreaper = lambda: False
else:
    del sys.argv[2]
class _P(type(Path())):
    def exists(self, *a, **k):
        return False if str(self) == "/proc/self/stat" else super().exists(*a, **k)
h.Path = _P
sys.exit(h.main(sys.argv[2:]))
"""


class _Repo:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hyg-selftest-"))
        self.root = self.tmp / "repo"
        self.work = self.tmp / "work"
        # R3's "system temp dir" for these runs (fix wave 26b, W26-ST): a private directory, so an entry another
        # process creates in the real /tmp during a case cannot fail it; one case still runs against the real /tmp
        self.systmp = self.tmp / "systmp"
        self.work.mkdir()
        self.root.mkdir()
        self.systmp.mkdir()
        shutil.copytree(HERE, self.root / "devtools", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "README.md").write_text("probe\n")
        (self.root / ".gitignore").write_text("*.pyc\n")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "test-counts.md").write_text(
            "# Test counts (generated)\n\n| Suite | Tests | Counted by |\n|---|---|---|\n| `go:probe` | 1 | go test -v |\n")
        allow = json.loads((self.root / "devtools" / "hygiene_allowlist.json").read_text())
        allow["allow"] = []
        allow["expected_skips"] = {}
        (self.root / "devtools" / "hygiene_allowlist.json").write_text(json.dumps(allow))
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")

    def git(self, *a):
        subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True)

    def run(self, *args) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(self.root / "devtools" / "hygiene_check.py"), *args],
                           cwd=self.root, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, RUNNER_TEMP=str(self.work)))
        return r.returncode, r.stdout + r.stderr

    def run_portable(self, *args) -> tuple[int, str]:
        r = subprocess.run([sys.executable, "-c", PORTABLE_HARNESS, str(self.root / "devtools"), *args],
                           cwd=self.root, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, RUNNER_TEMP=str(self.work)))
        return r.returncode, r.stdout + r.stderr

    def suite(self, shell: str, kind: str = "none", *extra, portable: bool = False,
              real_tmp: bool = False) -> tuple[int, str]:
        systmp = () if real_tmp else ("--system-tmp", str(self.systmp))
        return (self.run_portable if portable else self.run)(
            "run", "--suite", "go:probe", "--kind", kind, "--work-dir", str(self.work),
            "--tmp-ignore", r"^claude-[0-9a-f]+-cwd$", *systmp, *extra, "--", "/bin/sh", "-c", shell)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Dynamic(unittest.TestCase):
    def setUp(self):
        self.r = _Repo()

    def tearDown(self):
        self.r.close()

    def test_clean_passes(self):
        rc, out = self.r.suite("true")
        self.assertEqual(rc, 0, out)
        self.assertIn("hygiene violations: 0", out)

    def test_r1_a_tracked_file_changed(self):
        rc, out = self.r.suite("echo planted >> README.md")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R1-tracked", out)

    def test_r1_a_file_already_dirty_changed_again(self):
        (self.r.root / "README.md").write_text("dirty before the run\n")      # a local checkout may be dirty
        rc, out = self.r.suite("true")
        self.assertEqual(rc, 0, out)                                          # left as it was: not the suite's
        rc, out = self.r.suite("echo planted >> README.md")                   # same status line, new content
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R1-tracked go:probe: README.md (already changed before the run) was changed again", out)

    def test_r2_a_new_ignored_file(self):
        rc, out = self.r.suite("echo x > planted.pyc")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R2-ignored", out)
        # planted.pyc now exists BEFORE the next run (not new); an allowlisted path may appear
        rc, out = self.r.suite("echo x > allowed.pyc", "none", "--allow-ignored", "allowed.pyc")
        self.assertEqual(rc, 0, out)

    def test_r3_left_in_the_private_tmpdir(self):
        rc, out = self.r.suite('mktemp "$TMPDIR/planted.XXXX" >/dev/null')
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R3-tmp", out)
        self.assertIn("private TMPDIR", out)

    def test_r3_new_entry_in_system_tmp(self):
        # the real /tmp: only this case's own entry is asserted, so other processes' entries cannot fail it
        name = f"zbm-hyg-selftest-{uuid.uuid4().hex}"
        try:
            rc, out = self.r.suite(f"touch /tmp/{name}", real_tmp=True)
            self.assertEqual(rc, 1, out)
            self.assertIn(f"HYGIENE R3-tmp go:probe: new entry in /tmp during the run: {name}", out)
        finally:
            Path(f"/tmp/{name}").unlink(missing_ok=True)

    def test_r3_new_entry_in_the_given_system_tmp(self):
        rc, out = self.r.suite(f'touch "{self.r.systmp}/planted"')
        self.assertEqual(rc, 1, out)
        self.assertIn(f"HYGIENE R3-tmp go:probe: new entry in {self.r.systmp} during the run: planted", out)

    def test_r3_cases_do_not_watch_the_real_tmp(self):
        # W26-ST (AEGIS r26, review26/logs/selftest-tmpnoise-*): any foreign /tmp entry made during a run failed the
        # cases that expect a clean result. The cases now watch a private system tmp; an entry in the real /tmp
        # (planted here from inside the probe, the same as another process making one) is not theirs to report.
        name = f"zbm-hyg-selftest-{uuid.uuid4().hex}"
        try:
            rc, out = self.r.suite(f"touch /tmp/{name}")
            self.assertEqual(rc, 0, out)
            self.assertIn("hygiene violations: 0", out)
        finally:
            Path(f"/tmp/{name}").unlink(missing_ok=True)

    def test_r4_a_process_outlives_the_suite(self):
        # double fork + its own session + an empty environment
        rc, out = self.r.suite("env -i /bin/sh -c 'setsid sleep 300 >/dev/null 2>&1 &'")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R4-procs", out)
        self.assertIn("sleep 300", out)

    def test_r4_a_plain_background_child(self):
        rc, out = self.r.suite("sleep 301 >/dev/null 2>&1 &")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R4-procs", out)
        self.assertIn("sleep 301", out)

    def test_r4_report_names_pid_ppid_pgid_stat_and_the_full_command(self):
        rc, out = self.r.suite("sleep 304 >/dev/null 2>&1 &")
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"HYGIENE R4-procs go:probe: pid \d+ \(ppid \d+, pgid \d+, stat \w+\) still alive: "
                              r"command .*sleep 304")   # before its exec the child's command line is still sh's

    def test_r4_portable_path_a_clean_suite_passes(self):
        """W26-1: on the non-subreaper path (macOS) the checker's own `ps` helper was reported as the suite's."""
        if shutil.which("ps") is None:
            self.fail("ps is required by this self-test")
        rc, out = self.r.suite("true", portable=True)
        self.assertIn("subreaper False", out)
        self.assertNotIn("ps -axo", out)
        self.assertEqual(rc, 0, out)
        self.assertIn("hygiene violations: 0", out)

    def test_r4_portable_path_a_real_leftover_still_fails(self):
        rc, out = self.r.suite("sleep 305 >/dev/null 2>&1 &", portable=True)
        self.assertIn("subreaper False", out)
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"HYGIENE R4-procs go:probe: pid \d+ \(ppid \d+, pgid \d+, stat \w+\) still alive: "
                              r"command .*sleep 305")
        self.assertNotIn("ps -axo", out)

    def test_r4_ps_table_with_a_subreaper_does_not_report_its_own_ps(self):
        # the ps helper is excluded by its pid even where this checker's children ARE walked (a subreaper without
        # /proc): the two guards are independent
        rc, out = self.r.run_portable("--keep-subreaper", "run", "--suite", "go:probe", "--kind", "none",
                                      "--work-dir", str(self.r.work), "--tmp-ignore", r"^claude-[0-9a-f]+-cwd$",
                                      "--system-tmp", str(self.r.systmp), "--", "/bin/sh", "-c", "true")
        self.assertIn("subreaper True", out)
        self.assertNotIn("ps -axo", out)
        self.assertEqual(rc, 0, out)

    def test_r4_portable_path_a_new_session_child_that_keeps_the_marker_is_found(self):
        # R26-1 (AEGIS r26): without a subreaper (macOS) an orphan that leaves the suite's process group — setsid,
        # setpgid, a double fork; delivery-py's fakes start children with start_new_session=True — escaped, because
        # the run marker was read from /proc only. The process table's environments are now read on that path too
        # (macOS: sysctl KERN_PROCARGS2, this user's non-platform processes), so a child that kept its environment is
        # found by the marker. Python, not /bin/sleep: macOS withholds a platform binary's environment.
        child = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(306)'], "
                 "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)")
        rc, out = self.r.suite(f'"{sys.executable}" -c "{child}"', portable=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R4-procs", out)
        self.assertIn("time.sleep(306)", out)

    def test_r4_portable_path_a_leftover_grandchild_still_fails(self):
        # a child of a background subshell: found as a descendant of the suite's process group
        rc, out = self.r.suite("( sh -c 'sleep 306' & wait ) >/dev/null 2>&1 &", portable=True)
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"still alive: command .*sleep 306")
        self.assertNotIn("ps -axo", out)

    def test_r6_write_then_check(self):
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n=== RUN   TestB\n=== RUN   TestB/sub\n'", "go", "--counts", "write")
        self.assertEqual(rc, 0, out)
        self.assertIn("| `go:probe` | 2 | go test -v |", (self.r.root / "docs" / "test-counts.md").read_text())
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n=== RUN   TestB\n'", "go")
        self.assertEqual(rc, 0, out)

    def _platform_only(self, row: int, entries: list) -> None:
        """docs row for go:probe = ``row`` (cargo-counted) and the suite's platform_only_tests = ``entries``."""
        allow = self.r.root / "devtools" / "hygiene_allowlist.json"
        d = json.loads(allow.read_text())
        d["platform_only_tests"] = {"go:probe": entries}
        allow.write_text(json.dumps(d))
        (self.r.root / "docs" / "test-counts.md").write_text(
            f"| Suite | Tests | Counted by |\n|---|---|---|\n| `go:probe` | {row} | cargo test |\n")
        self.r.git("add", "-A")
        self.r.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "platform-only")

    @staticmethod
    def _cargo(*names: str) -> str:
        lines = "".join(f"echo 'test {n} ... ok'; " for n in names)
        return lines + f"echo 'test result: ok. {len(names)} passed; 0 failed; 0 ignored; 0 measured; 0 filtered out'"

    LINUX_ONLY = [{"test": "f5_linux_only", "only_on": ["linux"], "why": "probe: #[cfg(target_os = \"linux\")]"}]

    def test_r6_platform_only_test_absent_on_another_os_passes_and_is_stated(self):
        """W26-5: on macOS ledger-rust counted one test fewer and R6 said nothing; now the summary names it."""
        self._platform_only(3, self.LINUX_ONLY)
        rc, out = self.r.suite(self._cargo("a", "b"), "cargo", "--count-os", "darwin")
        self.assertEqual(rc, 0, out)
        self.assertIn("tests counted: 2 (1 platform-only test(s) not on darwin: f5_linux_only); "
                      "docs/test-counts.md row 3", out)
        rc, out = self.r.suite(self._cargo("a", "b", "f5_linux_only"), "cargo", "--count-os", "linux")
        self.assertEqual(rc, 0, out)
        self.assertIn("tests counted: 3; skips: 0", out)

    def test_r6_platform_only_test_running_where_it_should_not_fails(self):
        """The masking case: the platform gate removed (the test runs on darwin) while another test is lost — the
        count alone still matches the per-OS number; the name check does not."""
        self._platform_only(3, self.LINUX_ONLY)
        rc, out = self.r.suite(self._cargo("a", "f5_linux_only"), "cargo", "--count-os", "darwin")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R6-counts go:probe: platform-only test f5_linux_only (only on linux) ran on darwin", out)

    def test_r6_platform_only_test_missing_where_it_should_run_fails(self):
        self._platform_only(3, self.LINUX_ONLY)
        rc, out = self.r.suite(self._cargo("a", "b", "c"), "cargo", "--count-os", "linux")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R6-counts go:probe: platform-only test f5_linux_only (only on linux) did not run "
                      "on linux", out)

    def test_r6_a_lost_test_on_another_os_still_fails(self):
        self._platform_only(3, self.LINUX_ONLY)
        rc, out = self.r.suite(self._cargo("a"), "cargo", "--count-os", "darwin")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R6-counts go:probe: docs/test-counts.md says 3 (expected 2 on darwin (1 platform-only "
                      "test(s) not on darwin: f5_linux_only)), this run counted 1", out)

    def test_r6_write_on_another_os_counts_the_absent_platform_only_test(self):
        self._platform_only(9, self.LINUX_ONLY)
        rc, out = self.r.suite(self._cargo("a", "b"), "cargo", "--count-os", "darwin", "--counts", "write")
        self.assertEqual(rc, 0, out)
        self.assertIn("| `go:probe` | 3 | cargo test | `f5_linux_only` only on linux |",
                      (self.r.root / "docs" / "test-counts.md").read_text())

    def test_r6_platform_only_entries_need_named_tests(self):
        self._platform_only(2, self.LINUX_ONLY)
        rc, out = self.r.suite(r"printf '# tests 2\n'", "node", "--count-os", "darwin")
        self.assertEqual(rc, 1, out)
        self.assertIn("platform_only_tests are listed but a node run's output names no tests", out)

    def test_counts_check_platform_column_must_match_the_allowlist(self):
        allow = self.r.root / "devtools" / "hygiene_allowlist.json"
        d = json.loads(allow.read_text())
        d["platform_only_tests"] = {"rust:ledger-rust": self.LINUX_ONLY}
        d["count_os_delta"] = {"rust:ledger-rust": {"darwin": {"delta": -1, "why": "old"}}}
        allow.write_text(json.dumps(d))
        (self.r.root / "docs" / "test-counts.md").write_text(
            "| Suite | Tests | Counted by | Platform-only tests |\n|---|---|---|---|\n"
            "| `rust:ledger-rust` | 114 | cargo test | — |\n")
        rc, out = self.r.run("counts", "--check")
        self.assertEqual(rc, 1, out)
        self.assertIn("row `rust:ledger-rust` platform-only column is '—', the allowlist says "
                      "'`f5_linux_only` only on linux'", out)
        self.assertIn("allowlist key count_os_delta is no longer read", out)

    def test_cargo_counts_and_ignored_tests(self):
        line = "test result: ok. {} passed; 0 failed; {} ignored; 0 measured; 0 filtered out"
        self.r.git("rm", "-q", "--cached", "docs/test-counts.md")
        (self.r.root / "docs" / "test-counts.md").write_text(
            "| Suite | Tests | Counted by |\n|---|---|---|\n| `go:probe` | 3 | cargo test |\n")
        self.r.git("add", "-A")
        self.r.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rows")
        rc, out = self.r.suite(f"echo '{line.format(2, 0)}'; echo '{line.format(1, 0)}'", "cargo")
        self.assertEqual(rc, 0, out)
        rc, out = self.r.suite(f"echo '{line.format(2, 1)}'", "cargo")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R5-skips go:probe: unexpected ignored", out)

    def test_node_skips(self):
        rc, out = self.r.suite(r"printf '# tests 1\n# skipped 1\n'", "node")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R5-skips go:probe: unexpected skip: (node): 1 skipped", out)

    def test_an_expected_skip_on_the_allowlist_passes(self):
        allow = self.r.root / "devtools" / "hygiene_allowlist.json"
        d = json.loads(allow.read_text())
        d["expected_skips"] = {"go:probe": [{"reason_regex": "^go test SKIP$", "why": "probe"}]}
        allow.write_text(json.dumps(d))
        self.r.git("add", "-A")
        self.r.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "allow")
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n--- SKIP: TestA (0.00s)\n'", "go")
        self.assertEqual(rc, 0, out)

    def test_the_suites_own_exit_status_wins(self):
        rc, out = self.r.suite("exit 7")
        self.assertEqual(rc, 7, out)

    def test_r5_an_unexpected_skip_and_r6_a_count_mismatch(self):
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n--- SKIP: TestA (0.00s)\n=== RUN   TestB\n'", "go")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R5-skips go:probe: unexpected skip: TestA", out)
        self.assertIn("HYGIENE R6-counts go:probe: docs/test-counts.md says 1, this run counted 2", out)
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n'", "go")
        self.assertEqual(rc, 0, out)


class Pytest(unittest.TestCase):
    """The Python path: `--kind pytest` loads devtools/pytest_plugin/zbm_pytest_hygiene.py into the suite's own
    interpreter. Needs pytest in THIS interpreter (CI's hygiene-static job installs the services' pinned pytest);
    without it these cases FAIL, they never skip."""

    def setUp(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec("pytest"), "pytest is required by this self-test")
        self.r = _Repo()
        self.t = self.r.root / "services" / "probe-py" / "tests"
        self.t.mkdir(parents=True)
        (self.r.root / "docs" / "test-counts.md").write_text(
            "| Suite | Tests | Counted by |\n|---|---|---|\n| `python:probe-py` | 2 | pytest collection |\n")
        self.r.git("add", "-A")
        self.r.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rows")

    def tearDown(self):
        self.r.close()

    def suite(self, body: str, *extra) -> tuple[int, str]:
        (self.t / "test_probe.py").write_text(body)
        self.r.git("add", "-A")
        self.r.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "probe")
        return self.r.run("run", "--suite", "python:probe-py", "--kind", "pytest", "--cwd", "services/probe-py",
                          "--work-dir", str(self.r.work), "--tmp-ignore", r"^claude-[0-9a-f]+-cwd$",
                          "--system-tmp", str(self.r.systmp), *extra,
                          "--", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")

    def test_clean_passes_and_tmp_path_is_not_retained(self):
        rc, out = self.suite("def test_a(tmp_path):\n    (tmp_path / 'x').write_text('x')\n\ndef test_b():\n    pass\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("tests counted: 2; skips: 0; hygiene violations: 0", out)

    def test_skip_xfail_and_count(self):
        rc, out = self.suite("import pytest\n\ndef test_a():\n    pytest.skip('planted reason')\n\n"
                             "@pytest.mark.xfail(reason='planted xfail')\ndef test_b():\n    assert 0\n\n"
                             "def test_c():\n    pass\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R5-skips python:probe-py: unexpected skip: tests/test_probe.py::test_a: planted reason", out)
        self.assertIn("HYGIENE R5-skips python:probe-py: unexpected xfail: tests/test_probe.py::test_b: planted xfail", out)
        self.assertIn("HYGIENE R6-counts python:probe-py: docs/test-counts.md says 2, this run counted 3", out)

    def test_a_file_left_in_the_private_tmpdir(self):
        rc, out = self.suite("import tempfile\n\ndef test_a():\n    tempfile.mkstemp(prefix='planted-')\n\n"
                             "def test_b():\n    pass\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R3-tmp python:probe-py: left in the private TMPDIR: planted-", out)

    def test_a_child_left_running_and_a_tracked_file_changed(self):
        rc, out = self.suite("import subprocess, pathlib\n\ndef test_a():\n"
                             "    subprocess.Popen(['sleep', '302'])\n\n"
                             "def test_b():\n    pathlib.Path('tests/test_probe.py').write_text('# changed\\n')\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R4-procs", out)
        self.assertIn("sleep 302", out)
        self.assertIn("HYGIENE R1-tracked", out)

    def test_a_failing_suite_fails_whatever_the_hygiene(self):
        rc, out = self.suite("def test_a():\n    assert 0\n\ndef test_b():\n    pass\n")
        self.assertEqual(rc, 1, out)
        self.assertIn("hygiene violations: 0", out)


class DocumentedCommands(unittest.TestCase):
    def test_every_documented_unittest_command_for_devtools_writes_no_bytecode(self):
        # F-11 (AEGIS r25): the documented unittest command for this file, run without PYTHONDONTWRITEBYTECODE, left
        # devtools/__pycache__/ in the checkout (the .pyc is written when unittest imports the module, before any of
        # its code runs, so the module cannot prevent it). Every place that tells someone to run one says `-B`.
        import re
        root = HERE.parent
        files = [root / "README.md", root / "docs" / "ci.md", root / ".github" / "workflows" / "ci.yml",
                 *sorted(HERE.glob("test_*.py"))]
        bad = []
        for f in files:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                for m in re.finditer(r"python3?((?:\s+-\S+)*)\s+-m\s+unittest\s+devtools/", line):
                    if "-B" not in m.group(1).split():
                        bad.append(f"{f.relative_to(root)}:{i}: {line.strip()[:120]}")
        self.assertEqual(bad, [])


class RepoIgnoreRules(unittest.TestCase):
    def test_runtime_ledgers_are_ignored_and_a_jsonl_fixture_anywhere_is_not(self):
        # C5-8b (scout C): `*.jsonl` was ignored repo-wide with one exception (ledger-rust's fixtures), so a JSONL
        # fixture added to any other service was silently un-addable. The rule is now the names the services write.
        root = HERE.parent

        def ignored(path: str) -> bool:
            return subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=root).returncode == 0

        for runtime in ("services/ledger-rust/ledger_data/ledger.jsonl", "ledger.jsonl", "x/ledger.jsonl.torn-123",
                        "data/dlv_log.jsonl", "d/fin_log.jsonl", "d/vi_log.jsonl", "d/legal_log.jsonl",
                        "d/cn_log.jsonl", "d/compliance_log.jsonl", "d/fin_log.jsonl.torn-9"):
            self.assertTrue(ignored(runtime), runtime)
        for fixture in ("services/creative-py/tests/fixtures/events.jsonl", "fixtures/sample.jsonl",
                        "services/ledger-rust/tests/fixtures/legacy_ledger_v1.jsonl"):
            self.assertFalse(ignored(fixture), fixture)


class Static(unittest.TestCase):
    def setUp(self):
        self.r = _Repo()
        t = self.r.root / "services" / "probe-py" / "tests"
        t.mkdir(parents=True)
        self.t = t

    def tearDown(self):
        self.r.close()

    def lint(self):
        self.r.git("add", "-A")
        return self.r.run("lint")

    def test_clean_passes(self):
        (self.t / "test_ok.py").write_text("import time\n\ndef test_x():\n    t0 = time.monotonic()\n"
                                           "    assert time.monotonic() - t0 >= 0\n")
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)

    def test_l1_l2_planted(self):
        (self.t / "test_bad.py").write_text(
            "import socket, time\n\ndef test_a():\n    t0 = time.perf_counter()\n    took = time.perf_counter() - t0\n"
            "    assert took < 1.0\n\ndef test_b():\n    s = socket.socket()\n    s.bind(('127.0.0.1', 20111))\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L1-wallclock services/probe-py/tests/test_bad.py:6", out)
        self.assertIn("L2-ports services/probe-py/tests/test_bad.py:10", out)

    def test_l1_l2_other_languages_planted(self):
        rs = self.r.root / "services" / "ledger-rust" / "tests"
        rs.mkdir(parents=True)
        (rs / "probe.rs").write_text(
            "const PROMPT: Duration = Duration::from_secs(1);\nfn a() {\n    assert!(t.elapsed() < Duration::from_secs(1));\n}\n"
            "fn b() {\n    assert!(elapsed < PROMPT);\n}\nfn c() {\n    let s = \"127.0.0.1:20111\";\n}\n"
            "fn d() {\n    assert!(elapsed >= PROMPT);\n}\n")
        go = self.r.root / "services" / "probe-go"
        go.mkdir(parents=True)
        (go / "x_test.go").write_text("package x\n\nconst bound = 2 * time.Second\n\nfunc TestA(t *testing.T) {\n"
                                       "\tif elapsed > bound {\n\t}\n\tport := 19970\n}\n")
        ts = self.r.root / "apps" / "dashboard-ts" / "tests"
        ts.mkdir(parents=True)
        (ts / "x.test.mjs").write_text("test('a', () => {\n  assert(Date.now() - t0 < 500);\n  server.listen(20172);\n});\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        for where in ("L1-wallclock services/ledger-rust/tests/probe.rs:3", "L1-wallclock services/ledger-rust/tests/probe.rs:6",
                      "L2-ports services/ledger-rust/tests/probe.rs:9", "L1-wallclock services/probe-go/x_test.go:6",
                      "L2-ports services/probe-go/x_test.go:8", "L1-wallclock apps/dashboard-ts/tests/x.test.mjs:2",
                      "L2-ports apps/dashboard-ts/tests/x.test.mjs:3"):
            self.assertIn(where, out)
        self.assertNotIn("probe.rs:12", out)            # a lower bound is not flagged

    def test_l1_other_python_spellings_of_the_bound(self):
        (self.t / "test_d.py").write_text(
            "import time, unittest, pytest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
            "        t0 = time.monotonic()\n        self.assertTrue(time.monotonic() - t0 < 1.0)\n"
            "    def test_b(self):\n        t0 = time.monotonic()\n        self.assertFalse(time.monotonic() - t0 > 1.0)\n\n"
            "def test_c():\n    t0 = time.perf_counter()\n    took = time.perf_counter() - t0\n    if took > 2:\n"
            "        pytest.fail('slow')\n\ndef test_d():\n    t0 = time.monotonic()\n"
            "    if time.monotonic() - t0 > 2:\n        raise AssertionError('slow')\n\n"
            "def test_ok():\n    t0 = time.monotonic()\n    if time.monotonic() - t0 > 2:\n        print('slow')\n"
            "    assert time.monotonic() - t0 >= 0\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        for ln in (6, 9, 14, 19):
            self.assertIn(f"L1-wallclock services/probe-py/tests/test_d.py:{ln}:", out)
        self.assertNotIn("test_d.py:23", out)           # a branch that does not fail the test is not a bound
        self.assertNotIn("test_d.py:25", out)           # a lower bound is not flagged

    def test_l1_a_named_python_constant_is_a_literal(self):
        (self.t / "test_c.py").write_text("import time\nLIMIT = 2.0\n\ndef test_x():\n    t0 = time.monotonic()\n"
                                           "    assert time.monotonic() - t0 < LIMIT\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L1-wallclock services/probe-py/tests/test_c.py:6", out)

    def test_l1_allowlisted_with_a_reason_passes_and_strict_flags_a_stale_entry(self):
        (self.t / "test_bad.py").write_text("import time\n\ndef test_a():\n    t0 = time.monotonic()\n"
                                             "    assert time.monotonic() - t0 < 1\n")
        allow = self.r.root / "devtools" / "hygiene_allowlist.json"
        d = json.loads(allow.read_text())
        d["allow"] = [{"rule": "L1-wallclock", "path": "services/probe-py/tests/test_bad.py", "function": "test_a",
                       "reason": "probe"},
                      {"rule": "L1-wallclock", "path": "services/probe-py/tests/gone.py", "reason": "stale"}]
        allow.write_text(json.dumps(d))
        self.assertEqual(self.lint()[0], 0)
        self.r.git("add", "-A")
        rc, out = self.r.run("lint", "--strict-allowlist")
        self.assertEqual(rc, 1, out)
        self.assertIn("L9-allowlist services/probe-py/tests/gone.py", out)
        d["allow"][0]["reason"] = ""                     # an entry without a reason allows nothing
        allow.write_text(json.dumps(d))
        self.assertEqual(self.lint()[0], 1)

    def test_l3_a_count_wrapped_across_lines_and_a_commit_pinned_one(self):
        (self.r.root / "README.md").write_text("Run `cargo test`: 84\n  passed.\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L3-counts README.md:1", out)
        head = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=self.r.root, capture_output=True,
                              text=True, check=True).stdout.strip()
        (self.r.root / "README.md").write_text(f"At {head}: `cargo test`: 84\n  passed.\n")
        self.assertEqual(self.lint()[0], 0)

    def test_l3_a_count_pinned_by_an_all_digit_commit_id(self):
        # W26-ST (AEGIS r26, review26/logs/wst.log): ids made only of digits were skipped, so ~1 commit in 27 (a
        # 7-char short id with no a-f) could not pin a count, and the case above failed whenever HEAD was one. Make
        # such a commit deterministically: plumbing commits until one's 7-char id is all digits (p ~ 0.037 each; a
        # miss in 3000 tries is ~1e-49).
        tree = subprocess.run(["git", "write-tree"], cwd=self.r.root, capture_output=True, text=True,
                              check=True).stdout.strip()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        digits = None
        for i in range(3000):
            c = subprocess.run(["git", "commit-tree", tree, "-m", f"probe {i}"], cwd=self.r.root, env=env,
                               capture_output=True, text=True, check=True).stdout.strip()
            if c[:7].isdigit():
                digits = c[:7]
                break
        self.assertIsNotNone(digits)
        self.r.git("update-ref", "refs/heads/all-digit", c)                # reachable, as a real commit would be
        (self.r.root / "README.md").write_text(f"At {digits}: `cargo test`: 84\n  passed.\n")
        rc, out = self.lint()
        self.assertEqual(rc, 0, out)
        # a digit run that is NOT a commit still pins nothing
        (self.r.root / "README.md").write_text("At 1048576: `cargo test`: 84\n  passed.\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L3-counts README.md:1", out)

    def test_strict_allowlist_flags_an_expected_skip_no_source_can_produce(self):
        # W25-EB-R3: `lint --strict-allowlist` checked only L1/L2 entries, so a stale expected-skip entry (delivery-py's
        # "inner half of test_g3_dlv_test_port_range_set_outside_reaches_the_live_tests", no such skip since d0876fd)
        # stayed. An expected skip must now be producible: its reason regex matches a string literal (or an
        # f-string's constant text) somewhere in that suite's sources.
        allow_path = self.r.root / "devtools" / "hygiene_allowlist.json"
        d = json.loads(allow_path.read_text())
        d["expected_skips"] = {"python:probe-py": [{"reason_regex": "^planted reason: (?:a|b)$", "why": "t"},
                                                   {"reason_regex": "^built at run time ", "why": "t"}]}
        allow_path.write_text(json.dumps(d))
        (self.t / "test_s.py").write_text("import pytest\n\ndef test_a():\n    pytest.skip('planted reason: a')\n\n"
                                          "def test_b(x=1):\n    pytest.skip(f'built at run time {x}')\n")
        self.r.git("add", "-A")
        rc, out = self.r.run("lint", "--strict-allowlist")
        self.assertEqual(rc, 0, out)
        (self.t / "test_s.py").write_text("import pytest\n\ndef test_a():\n    pytest.skip('planted reason: a')\n")
        self.r.git("add", "-A")
        rc, out = self.r.run("lint", "--strict-allowlist")
        self.assertEqual(rc, 1, out)
        self.assertIn("L9-allowlist devtools/hygiene_allowlist.json: expected skip of python:probe-py matches no "
                      "skip reason in services/probe-py: '^built at run time '", out)
        self.assertNotIn("planted reason", out)
        rc, out = self.r.run("lint")                   # only --strict-allowlist reads them
        self.assertEqual(rc, 0, out)

    def test_l4_graceful_close_differs_from_its_pin(self):
        for svc, body in (("a-py", "X = 1\n"), ("b-py", "X = 2\n")):
            (self.r.root / "services" / svc / "src").mkdir(parents=True)
            (self.r.root / "services" / svc / "src" / "graceful_close.py").write_text(body)
            (self.r.root / "services" / svc / "tests").mkdir(parents=True)
            import hashlib
            pin = hashlib.sha256(b"X = 1\n").hexdigest()
            (self.r.root / "services" / svc / "tests" / "test_live_graceful_close_module.py").write_text(
                f'PINNED_SHA256 = "{pin}"\n')
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L4-shared services/b-py/src/graceful_close.py: differs from the pinned graceful_close.py", out)
        self.assertNotIn("services/a-py/src/graceful_close.py", out)

    def test_l3_planted(self):
        (self.r.root / "README.md").write_text("The suite has 123 tests.\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L3-counts README.md:1", out)

    def test_l4_planted(self):
        for svc in ("a-py", "b-py"):
            d = self.r.root / "services" / svc / "tests"
            d.mkdir(parents=True)
            (d / "_procinfo.py").write_text("X = 1\n" if svc == "a-py" else "X = 2\n")
        rc, out = self.lint()
        self.assertEqual(rc, 1, out)
        self.assertIn("L4-shared services/b-py/tests/_procinfo.py", out)


if __name__ == "__main__":
    unittest.main()
