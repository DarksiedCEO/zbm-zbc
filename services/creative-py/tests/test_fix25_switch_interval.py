"""
Fix wave 25 (scout C5-2; the class FIX_WAVE_23b item 2 ruled a PRODUCT defect in
detection-py): `python3 serve.py` never set the GIL switch interval, so the
event loop waited up to CPython's default 5 ms slice for the GIL behind any
CPU-bound handler each time it gave it up for a syscall. The launcher now sets
1 ms; CREATIVE_SWITCH_INTERVAL_SECONDS overrides it only within [100 us, 50 ms]
(as detection-py), the interval in force is compared in whole microseconds and
printed before serving.

These start the REAL launcher. A server counts as this test's own only once its
own stderr says it is running on the port it was given (another process could
answer /health on a port picked free a moment earlier).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from conftest import SRC, TEST_SERVICE_TOKEN, free_port


def _launch(raw: str | None, timeout: float = 20.0) -> tuple[int | None, str]:
    """(the /health status from OUR child once it announced its bind, or None if it exited first; its output)."""
    port = free_port()
    env = {**os.environ, "CREATIVE_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "CREATIVE_PORT": str(port),
           "PYTHONDONTWRITEBYTECODE": "1"}
    for k in ("LEDGER_SERVICE_URL", "LEDGER_SERVICE_TOKEN", "CREATIVE_SWITCH_INTERVAL_SECONDS"):
        env.pop(k, None)
    if raw is not None:
        env["CREATIVE_SWITCH_INTERVAL_SECONDS"] = raw
    with tempfile.TemporaryFile(mode="w+b") as out:
        proc = subprocess.Popen([sys.executable, "serve.py"], cwd=SRC, env=env, stdout=out, stderr=subprocess.STDOUT)
        status = None
        try:
            deadline = time.monotonic() + timeout
            announced = f"Uvicorn running on http://127.0.0.1:{port}".encode()
            while time.monotonic() < deadline and proc.poll() is None:
                if announced in os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0):
                    try:
                        status = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code
                    except httpx.HTTPError:
                        time.sleep(0.1)
                        continue
                    if proc.poll() is None:
                        break
                    status = None
                time.sleep(0.05)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        out.seek(0)
        return status, out.read().decode(errors="replace")


@pytest.mark.parametrize("raw,us", [(None, 1000), ("0.0001", 100), ("0.05", 50_000), (" 0.002 ", 2000)])
def test_the_launcher_serves_with_the_interval_in_force_it_reports(raw, us):
    status, out = _launch(raw)
    assert status == 200, out[-2000:]
    assert f"creative-py: GIL switch interval in force: {us} us" in out, out[-2000:]


@pytest.mark.parametrize("raw", ["0.0000999", "0.0500001", "0.5", "0", "-0.001", "nan", "inf", "abc"])
def test_the_launcher_refuses_an_interval_outside_the_range(raw):
    status, out = _launch(raw, timeout=15)
    assert status is None, out[-2000:]
    assert "CREATIVE_SWITCH_INTERVAL_SECONDS" in out and "Uvicorn running on" not in out, out[-2000:]


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
        with pytest.raises(SystemExit, match="refuses to start"):
            serve.apply_switch_interval(0.001)
    finally:
        monkeypatch.undo()
        sys.setswitchinterval(before)
