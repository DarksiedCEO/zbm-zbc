"""
Fix wave 25 (scout C5-2; the class FIX_WAVE_23b item 2 ruled a PRODUCT defect in
detection-py): `python3 -m api` never set the GIL switch interval, so the event
loop waited up to CPython's default 5 ms slice to get the GIL back from a
CPU-bound parse in the threadpool every time it gave it up for a syscall —
the latency of every small request and /health behind a 4 MiB parse or a junk
flood. The launcher now sets 1 ms (FULFILLMENT_SWITCH_INTERVAL_SECONDS
overrides it, only within [100 us, 50 ms], as detection-py), checks the
interval actually in force in whole microseconds (CPython keeps it truncated
to the microsecond: 0.0001 reads back as 9.999999999999999e-05) and prints it.

These start the REAL launcher (`python3 -m api`, tests/test_live_server._start).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

import http_limits
from conftest import child_env
from test_live_server import SRC, TOKEN, _free_port, _start, _stop, _LOGS


def _log_text(proc) -> str:
    log = _LOGS[proc.pid]
    fd = log.fileno()
    return os.pread(fd, os.fstat(fd).st_size, 0).decode(errors="replace")


@pytest.mark.parametrize("raw,us", [(None, 1000), ("0.0001", 100), ("0.05", 50_000), (" 0.002 ", 2000)])
def test_the_launcher_serves_with_the_interval_in_force_it_reports(raw, us):
    env = {} if raw is None else {"FULFILLMENT_SWITCH_INTERVAL_SECONDS": raw}
    proc, _, _ = _start(env)
    try:
        text = _log_text(proc)
    finally:
        _stop(proc)
    assert f"fulfillment-py: GIL switch interval in force: {us} us" in text, text[-2000:]


@pytest.mark.parametrize("raw", ["0.0000999", "0.0500001", "0.5", "0", "-0.001", "nan", "inf", "abc"])
def test_the_launcher_refuses_an_interval_outside_the_range(raw):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(SRC), **child_env(),
           "FULFILLMENT_SERVICE_TOKEN": TOKEN, "FULFILLMENT_PORT": str(_free_port()),
           "FULFILLMENT_SWITCH_INTERVAL_SECONDS": raw}
    with tempfile.TemporaryFile(mode="w+b") as log:
        proc = subprocess.Popen([sys.executable, "-m", "api"], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            rc = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        log.seek(0)
        text = log.read().decode(errors="replace")
    assert rc != 0, text[-2000:]
    assert "FULFILLMENT_SWITCH_INTERVAL_SECONDS" in text and "Uvicorn running on" not in text, text[-2000:]


@pytest.mark.parametrize("value,us", [(0.0001, 100), (0.05, 50_000), (0.00015, 150), (0.0123456789, 12_345)])
def test_the_interval_in_force_is_compared_in_whole_microseconds(value, us):
    before = sys.getswitchinterval()
    try:
        assert http_limits.apply_switch_interval(value) == us
    finally:
        sys.setswitchinterval(before)


def test_an_interval_not_in_force_refuses(monkeypatch):
    before = sys.getswitchinterval()
    try:
        monkeypatch.setattr(sys, "getswitchinterval", lambda: 0.005)       # e.g. a runtime that ignores the setter
        with pytest.raises(RuntimeError, match="refuses to start"):
            http_limits.apply_switch_interval(0.001)
    finally:
        monkeypatch.undo()
        sys.setswitchinterval(before)


def test_the_value_is_read_once_at_import_like_the_body_timeout():
    # unset or empty -> the default; the range is the documented one
    assert http_limits.SWITCH_INTERVAL_DEFAULT_S == 0.001
    assert (http_limits.SWITCH_INTERVAL_MIN_US, http_limits.SWITCH_INTERVAL_MAX_US) == (100, 50_000)
