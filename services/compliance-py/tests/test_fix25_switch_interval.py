"""
Fix wave 25 (scout C5-3; the class of detection-py's wave-24 F3 and wave-25 H5):
COMPLIANCE_SWITCH_INTERVAL_SECONDS accepted any positive number (3600 s, 1e-9 s;
and nan passed the `<= 0` check), and the interval was never checked in force.
Now only [100 us, 50 ms] starts (unset or empty: 1 ms), serve.run() checks the
interval in force in whole microseconds (CPython truncates to the microsecond:
0.0001 reads back as 9.999999999999999e-05) and prints it before serving.

The launcher module is imported in a child process with the setting under test
(it is read at import), and serve.run() is driven there with uvicorn.run
replaced by a stub, so nothing binds a port.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
CHILD = (
    "import sys, uvicorn, serve\n"
    "uvicorn.run = lambda app, **kw: print('served at', round(sys.getswitchinterval() * 1e6), 'us', flush=True)\n"
    "serve.run(object(), host='127.0.0.1', port=1)\n"
)


def _child(raw: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("COMPLIANCE_SWITCH_INTERVAL_SECONDS", None)
    if raw is not None:
        env["COMPLIANCE_SWITCH_INTERVAL_SECONDS"] = raw
    return subprocess.run([sys.executable, "-c", CHILD], cwd=SRC, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("raw,us", [(None, 1000), ("", 1000), ("0.0001", 100), ("0.05", 50_000), (" 0.002 ", 2000)])
def test_the_launcher_serves_with_the_interval_in_force_it_reports(raw, us):
    out = _child(raw)
    assert out.returncode == 0, out.stderr[-2000:]
    assert f"compliance-py: GIL switch interval in force: {us} us" in out.stderr, out.stderr[-2000:]
    assert f"served at {us} us" in out.stdout, out.stdout[-500:]


@pytest.mark.parametrize("raw", ["0.0000999", "0.0500001", "0.5", "3600", "1e-9", "0", "-0.001", "nan", "inf", "abc"])
def test_the_launcher_refuses_an_interval_outside_the_range(raw):
    out = _child(raw)
    assert out.returncode != 0 and "served at" not in out.stdout, (out.stdout[-500:], out.stderr[-2000:])
    assert "COMPLIANCE_SWITCH_INTERVAL_SECONDS" in out.stderr and "refuses to start" in out.stderr, out.stderr[-2000:]


@pytest.mark.parametrize("value,us", [(0.0001, 100), (0.05, 50_000), (0.00015, 150), (0.0123456789, 12_345)])
def test_the_interval_in_force_is_compared_in_whole_microseconds(value, us):
    import serve

    before = sys.getswitchinterval()
    try:
        assert serve.apply_switch_interval(value) == us
    finally:
        sys.setswitchinterval(before)


def test_an_interval_not_in_force_refuses(monkeypatch):
    import serve

    before = sys.getswitchinterval()
    try:
        monkeypatch.setattr(sys, "getswitchinterval", lambda: 0.005)       # e.g. a runtime that ignores the setter
        with pytest.raises(RuntimeError, match="refuses to start"):
            serve.apply_switch_interval(0.001)
    finally:
        monkeypatch.undo()
        sys.setswitchinterval(before)
