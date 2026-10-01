"""Self-test of devtools/hygiene_check.py (fix wave 25, R-HYGIENE): every rule FAILS on a planted violation and the
clean probe passes. Standard library only (unittest); CI runs it in the `hygiene-static` job:

    python3 -m unittest devtools/test_hygiene_check.py -v

Each case copies devtools/ into a fresh throwaway git repository (in a private temp dir), plants one violation
there, and runs the checker from THAT copy, so the real checkout is never touched. The Python-suite path (the
pytest plugin) is exercised by every CI Python job; here the dynamic rules run through `--kind none` / `--kind go`
with small shell commands, which reach the same code."""
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


class _Repo:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hyg-selftest-"))
        self.root = self.tmp / "repo"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.root.mkdir()
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

    def suite(self, shell: str, kind: str = "none", *extra) -> tuple[int, str]:
        return self.run("run", "--suite", "go:probe", "--kind", kind, "--work-dir", str(self.work),
                        "--tmp-ignore", r"^claude-[0-9a-f]+-cwd$", *extra, "--", "/bin/sh", "-c", shell)

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
        name = f"zbm-hyg-selftest-{uuid.uuid4().hex}"
        try:
            rc, out = self.r.suite(f"touch /tmp/{name}")
            self.assertEqual(rc, 1, out)
            self.assertIn(f"HYGIENE R3-tmp go:probe: new entry in /tmp during the run: {name}", out)
        finally:
            Path(f"/tmp/{name}").unlink(missing_ok=True)

    def test_r4_a_process_outlives_the_suite(self):
        # double fork + its own session + an empty environment
        rc, out = self.r.suite("env -i /bin/sh -c 'setsid sleep 300 >/dev/null 2>&1 &'")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R4-procs", out)
        self.assertIn("sleep 300", out)

    def test_r5_an_unexpected_skip_and_r6_a_count_mismatch(self):
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n--- SKIP: TestA (0.00s)\n=== RUN   TestB\n'", "go")
        self.assertEqual(rc, 1, out)
        self.assertIn("HYGIENE R5-skips go:probe: unexpected skip: TestA", out)
        self.assertIn("HYGIENE R6-counts go:probe: docs/test-counts.md says 1, this run counted 2", out)
        rc, out = self.r.suite(r"printf '=== RUN   TestA\n'", "go")
        self.assertEqual(rc, 0, out)


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
