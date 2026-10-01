"""
Fix wave 24, F5 (AEGIS N23-S-9): the live servers (and every other Python child
process) the suite starts built their environment from scratch — PATH,
PYTHONPATH, the token — so a run with PYTHONDONTWRITEBYTECODE /
PYTHONPYCACHEPREFIX set (the operator's way to keep build output out of the
worktree) still had every child `python3 -m api` write `src/__pycache__/`.
Every child environment now goes through `conftest.child_env()`, which
carries both (and PYTHONDONTWRITEBYTECODE=1 even when the parent did not set
it: a test child never writes bytecode into the source tree).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import conftest

TESTS = Path(__file__).resolve().parent


def test_child_env_carries_the_bytecode_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path))
    env = conftest.child_env()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONPYCACHEPREFIX"] == str(tmp_path)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX")
    assert "PYTHONPYCACHEPREFIX" not in conftest.child_env()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/<pid>/environ (Linux only)")
def test_live_server_runs_with_the_bytecode_settings(monkeypatch, tmp_path):
    from test_fix5_http_limits_live import _start, _stop
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path))
    proc, _port = _start()
    try:
        environ = Path(f"/proc/{proc.pid}/environ").read_bytes().split(b"\0")
    finally:
        _stop(proc)
    assert b"PYTHONDONTWRITEBYTECODE=1" in environ, environ
    assert f"PYTHONPYCACHEPREFIX={tmp_path}".encode() in environ, environ


def test_every_child_environment_built_from_scratch_goes_through_child_env():
    """The sweep: an env dict that names PYTHONPATH (a child Python process)
    must include **child_env() — or start from os.environ, which carries the
    parent's settings."""
    missing = []
    for path in sorted(TESTS.glob("*.py")):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if re.search(r'"PYTHONPATH":\s*str\(SRC\)', line):
                window = "\n".join(lines[max(0, i - 1):i + 3])
                if "child_env()" not in window:
                    missing.append(f"{path.name}:{i + 1}")
    assert not missing, missing
