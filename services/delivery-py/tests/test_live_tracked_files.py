"""Wave 21 (AEGIS N20-D-1): the suite's live tests never rewrite a tracked file. Every green run used to overwrite
the committed ``docs/evidence/dept28/live-launcher-run.log`` (a tracked evidence file changed by running the tests).
This runs the live modules in a child pytest and compares ``git status --porcelain`` (untracked files included)
before and after: a tracked file modified, or an untracked file left behind outside the ignored ``_runs/``
directory, fails. A ``test_live_*`` module: the conftest's no-network guard exempts it (its child talks to
127.0.0.1 on the live port range only)."""

from __future__ import annotations

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


def test_live_tests_leave_git_status_unchanged():
    before = _status()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", *LIVE_MODULES],
                       cwd=str(SERVICE_ROOT), env=env, capture_output=True, text=True, timeout=1800)
    after = _status()
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    changed = sorted(set(after.splitlines()) ^ set(before.splitlines()))
    assert not changed, "the live tests changed the working tree:\n" + "\n".join(changed)
