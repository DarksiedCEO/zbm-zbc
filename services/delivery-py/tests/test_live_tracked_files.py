"""Wave 21 (AEGIS N20-D-1): the suite's live tests never rewrite a tracked file. Every green run used to overwrite
the committed ``docs/evidence/dept28/live-launcher-run.log`` (a tracked evidence file changed by running the tests).
This runs the live modules in a child pytest and compares ``git status --porcelain`` (untracked files included)
before and after: a tracked file modified, or an untracked file left behind, fails (since wave 24 the live log goes
to the session's temp dir; nothing in the tree is exempt). Wave 25 (scout B Low): a file that was ALREADY dirty or
untracked and is rewritten shows the same porcelain line before and after, so the content of every path the
status lists is compared too. A ``test_live_*`` module: the conftest's no-network guard exempts it (its child talks to
127.0.0.1 on the live port range only)."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

from helpers import REPO_ROOT, SERVICE_ROOT

LIVE_MODULES = ("tests/test_live_launcher.py", "tests/test_live_round19.py", "tests/test_live_docker.py")


def _status() -> str:
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=all"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        pytest.skip(f"not a git checkout (git status exit {r.returncode}): nothing to compare")
    return r.stdout


def _contents(status: str) -> dict:
    """sha256 of every file the porcelain status lists (a rename's new path; a directory's files)."""
    out = {}
    for line in status.splitlines():
        path = REPO_ROOT / line[3:].split(" -> ")[-1].strip('"')
        for f in ([path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else []):
            out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def test_live_tests_leave_git_status_unchanged():
    before = _status()
    before_contents = _contents(before)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", *LIVE_MODULES],
                       cwd=str(SERVICE_ROOT), env=env, capture_output=True, text=True, timeout=1800)
    after = _status()
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    changed = sorted(set(after.splitlines()) ^ set(before.splitlines()))
    assert not changed, "the live tests changed the working tree:\n" + "\n".join(changed)
    rewritten = sorted(p for p, h in _contents(after).items() if before_contents.get(p) != h)
    assert not rewritten, "the live tests rewrote files that were already dirty or untracked:\n" + "\n".join(rewritten)
