"""
Fix wave 23: /health latency under 16 concurrent worst-case batches (LOW-C,
test_request_limits_live.test_health_latency_bound_under_16_concurrent_worst_case_batches)
measured 0.50-0.63 s against its 0.5 s bound on the w23 box. The time was the
server's (time to first byte, prober in its own process), and it was not one
long block of the event loop (its lag stayed <= 0.13 s): it was the GIL convoy —
each time the loop gave the GIL up for a syscall it waited up to a 5 ms switch
slice to get it back behind the ~0.5 s parse. serve.py now sets a 1 ms switch
interval, as onboarding-py and the other serve.py launchers have since fix
wave 7 (NEW-5). These pin the setting; the live test pins its effect.
"""

from __future__ import annotations

import importlib
import sys

import pytest

import serve


def test_the_launcher_sets_a_1ms_switch_interval_before_serving(monkeypatch):
    assert serve.SWITCH_INTERVAL_S == 0.001
    seen = {}
    before = sys.getswitchinterval()
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: seen.setdefault("interval", sys.getswitchinterval()))
    monkeypatch.setattr(sys, "argv", ["serve.py", "--port", "1"])
    try:
        serve.main()
    finally:
        sys.setswitchinterval(before)
    assert seen["interval"] == pytest.approx(0.001)


def test_the_switch_interval_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DETECTION_SWITCH_INTERVAL_SECONDS", "0.002")
    assert serve._switch_interval_from_env() == 0.002


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "1", "fast"])
def test_an_invalid_switch_interval_is_refused_at_start(monkeypatch, raw):
    monkeypatch.setenv("DETECTION_SWITCH_INTERVAL_SECONDS", raw)
    with pytest.raises(RuntimeError, match="DETECTION_SWITCH_INTERVAL_SECONDS"):
        importlib.reload(serve)
    monkeypatch.delenv("DETECTION_SWITCH_INTERVAL_SECONDS")
    importlib.reload(serve)
