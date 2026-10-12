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


HANG_GUARD_S = 120.0   # waits for an EVENT (the bind announcement or the child's exit); never a measurement
PORT_ATTEMPTS = 5


def _launch(raw: str | None) -> tuple[int | None, str]:
    """(the /health status from OUR child once it announced its bind, or None if it exited first; its output).

    fix-flakes-h: ends on events only — the child announcing its own bind, or exiting — with a generous hang guard
    (it was a 15-20 s deadline, which a slow runner's cold start could reach: the refusal check runs only after
    `import api`); and a child that lost its picked port to another process (exited with "address already in use"
    before announcing) is retried on another port, as conftest.start_serve does, instead of reading as "refused"."""
    for _ in range(PORT_ATTEMPTS):
        status, out, lost_port = _launch_once(raw)
        if not lost_port:
            return status, out
    raise RuntimeError(f"the launcher lost its port {PORT_ATTEMPTS} times: {out[-2000:]}")


def _launch_once(raw: str | None) -> tuple[int | None, str, bool]:
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
            deadline = time.monotonic() + HANG_GUARD_S
            announced = f"Uvicorn running on http://127.0.0.1:{port}".encode()
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("the launcher neither announced its bind nor exited within the hang guard: "
                                       + os.pread(out.fileno(), 4000, 0).decode(errors="replace"))
                if announced in os.pread(out.fileno(), os.fstat(out.fileno()).st_size, 0):
                    try:
                        status = httpx.get(f"http://127.0.0.1:{port}/health", timeout=30).status_code
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
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        out.seek(0)
        text = out.read().decode(errors="replace")
    lost_port = status is None and "Uvicorn running on" not in text and "address already in use" in text.lower()
    return status, text, lost_port


@pytest.mark.parametrize("raw,us", [(None, 1000), ("0.0001", 100), ("0.05", 50_000), (" 0.002 ", 2000)])
def test_the_launcher_serves_with_the_interval_in_force_it_reports(raw, us):
    status, out = _launch(raw)
    assert status == 200, out[-2000:]
    assert f"creative-py: GIL switch interval in force: {us} us" in out, out[-2000:]


@pytest.mark.parametrize("raw", ["0.0000999", "0.0500001", "0.5", "0", "-0.001", "nan", "inf", "abc"])
def test_the_launcher_refuses_an_interval_outside_the_range(raw):
    status, out = _launch(raw)
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
