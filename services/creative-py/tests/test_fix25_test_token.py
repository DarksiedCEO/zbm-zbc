"""
Fix wave 25 (scout A D1/F7/O8/C7/P2): tests/conftest.py set the service token
with os.environ.setdefault, so an operator shell that already exports
CREATIVE_SERVICE_TOKEN — what the root README tells an operator to do before running
the service — made every in-process test authenticate with the TEST token
against a service that read the OPERATOR's: reproduced in detection-py,
6 failed / 10 passed in test_api.py + test_auth.py. The test process now
always runs with its own test-only token, whatever the shell exports (the
operator's value is never needed by a test: live-server tests pass theirs
explicitly).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def test_the_suite_uses_its_own_token_even_when_the_shell_exports_one():
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "CREATIVE_SERVICE_TOKEN": "an-operator-secret-from-the-shell"}
    out = subprocess.run([sys.executable, "-c", "import os, conftest; print(os.environ['CREATIVE_SERVICE_TOKEN'])"],
                         cwd=TESTS, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "test-shared-secret-do-not-use-in-production", out.stdout[-500:]
