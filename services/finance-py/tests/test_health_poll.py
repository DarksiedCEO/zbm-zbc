"""AEGIS round 2 N-3: the heartbeat must read /health (not /livez) and alert when a service is non-ok for 10 minutes.
health_poll.py is the piece the ops-alerts job (fix wave C, ADR 0027) calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import health_poll as HP
from helpers import Harness

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _transport(routes: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        r = routes[host]
        if isinstance(r, Exception):
            raise r
        status, body = r
        return httpx.Response(status, json=body) if not isinstance(body, bytes) else httpx.Response(status, content=body)
    return httpx.MockTransport(handler)


def test_n3_readings_turn_every_non_ok_health_into_a_fixed_code():
    t = _transport({
        "ok": (200, {"status": "ok", "write_ok": True}),
        "maint": (503, {"status": "maintenance", "write_ok": False, "degraded_reasons": ["reconcile_mode"]}),
        "deg": (503, {"status": "degraded", "degraded_reasons": ["ledger_unreachable"]}),
        "held": (503, {"status": "starting", "degraded_reasons": ["interrupted_write_anchored"]}),
        "warn": (200, {"status": "ok", "warnings": ["rotation_expired"]}),
        "writes": (200, {"status": "ok", "write_ok": False}),
        "idleak": (503, {"status": "fin-je-abcdef0123 refused"}),
        "badwarn": (200, {"status": "ok", "warnings": ["Andre: rotate FIN_SERVICE_TOKEN now"]}),
        "warns": (200, {"status": "ok", "warnings": ["ledger_anchor_unkeyed", "rotation_expired"]}),
        "html": (502, b"<html>bad gateway</html>"),
        "down": httpx.ConnectError("refused"),
    })
    got = {r.name: (r.ok, r.code) for r in HP.poll({n: f"http://{n}:8080" for n in
                                                     ("ok", "maint", "deg", "held", "warn", "writes", "idleak",
                                                      "badwarn", "warns", "html", "down")}, t)}
    assert got == {"ok": (True, "ok"), "maint": (False, "maintenance"), "deg": (False, "degraded"),
                   "held": (False, "starting"), "warn": (False, "warning:rotation_expired"),
                   "writes": (False, "write_refused"), "idleak": (False, "bad_status"),
                   "badwarn": (False, "warning:bad_warning"), "html": (False, "http_502"),
                   "warns": (False, "warning:ledger_anchor_unkeyed,rotation_expired"),     # every code (N3-8)
                   "down": (False, "unreachable")}


def test_n3_an_alert_only_after_ten_minutes_non_ok_without_a_break():
    st: dict = {}
    bad, good = HP.Reading("legal", False, "maintenance"), HP.Reading("legal", True, "ok")
    assert HP.track(st, [bad], T0) == []
    assert HP.track(st, [bad], T0 + timedelta(minutes=9)) == []
    assert HP.track(st, [bad], T0 + timedelta(minutes=10)) == [("service_unhealthy", "legal", "maintenance")]
    assert HP.track(st, [good], T0 + timedelta(minutes=11)) == [] and "legal" not in st["health"]
    # the 10-minute clock starts again; 4 of the last 5 polls were non-ok, so it is flapping meanwhile (N3-8)
    assert HP.track(st, [bad], T0 + timedelta(minutes=12)) == [("service_flapping", "legal", "maintenance")]
    assert HP.track(st, [bad], T0 + timedelta(minutes=22)) == [("service_unhealthy", "legal", "maintenance")]
    assert HP.track(st, [], T0 + timedelta(minutes=23)) == [] and st["health"] == {}


def test_n3_a_forgotten_reconcile_mode_is_caught(tmp_path):
    """AEGIS A15: reconcile mode on a clean log answered /health 200 "ok", so `curl -f /health` passed forever."""
    x = Harness(data_dir=str(tmp_path / "d")).ready()
    x.svc.close()
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, fakes=x.f, env={"FIN_RECONCILE_MODE": "1"})

    def handler(req: httpx.Request) -> httpx.Response:
        r = y.client.get(req.url.path)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})
    (r,) = HP.poll({"finance": "http://finance.railway.internal:8080"}, httpx.MockTransport(handler))
    assert r == HP.Reading("finance", False, "maintenance")
    assert y.client.get("/livez").status_code == 200                           # Railway's healthcheck still passes


def test_n3_targets_come_from_one_variable_and_are_checked():
    env = {"OPS_HEALTH_URLS": "finance=http://finance.railway.internal:8080, ledger=http://ledger.railway.internal:8080/"}
    assert HP.targets_from_env(env) == {"finance": "http://finance.railway.internal:8080",
                                        "ledger": "http://ledger.railway.internal:8080"}
    assert HP.targets_from_env({}) == {}
    for bad in ("finance", "Finance=http://x:1", "a=http://x:1,a=http://y:1", "a=ftp://x", "a=http://u:p@x:1",
                "a=http://x:1/?t=1"):
        with pytest.raises(ValueError):
            HP.targets_from_env({"OPS_HEALTH_URLS": bad})


def test_n3_8_a_flapping_service_alerts():
    """AEGIS round 3 N3-8 (P1): unreachable on 2 of every 3 polls for 2 h never alerted (no 10-minute unbroken run).
    Non-ok in more than half of the polls over 30 minutes is `service_flapping`."""
    st: dict = {}
    out = []
    for k in range(24):                                          # every 5 minutes for 2 h
        ok = k % 3 == 0
        out += HP.track(st, [HP.Reading("finance", ok, "ok" if ok else "unreachable")], T0 + timedelta(minutes=5 * k))
    assert ("service_flapping", "finance", "unreachable") in out, out
    st = {}
    out = []
    for k in range(24):                                          # one blip in six: not flapping
        ok = k % 6 != 0
        out += HP.track(st, [HP.Reading("legal", ok, "ok" if ok else "http_503")], T0 + timedelta(minutes=5 * k))
    assert out == [], out


def test_n3_8_one_call_does_everything_and_an_empty_config_alerts(caplog):
    """AEGIS round 3 N3-8 (P2): an unset or empty OPS_HEALTH_URLS polled nothing, silently. `run()` is the whole call
    site for the ops-alerts job; an empty list, or one without a required service, is a warning and its own alert."""
    import logging
    st: dict = {}
    with caplog.at_level(logging.WARNING):
        got = HP.run(st, {}, T0)
    assert got == [("health_poll_unconfigured", "ops-alerts", "no_targets")]
    assert "OPS_HEALTH_URLS" in caplog.text
    t = _transport({"finance": (200, {"status": "ok"})})
    got = HP.run(st, {"OPS_HEALTH_URLS": "finance=http://finance:8410"}, T0, transport=t)
    assert sorted(got) == [("health_poll_unconfigured", n, "missing") for n in ("compliance", "ledger", "legal")]
    t = _transport({n: (200, {"status": "ok"}) for n in HP.REQUIRED})
    env = {"OPS_HEALTH_URLS": ",".join(f"{n}=http://{n}:80" for n in HP.REQUIRED)}
    assert HP.run(st, env, T0, transport=t) == []
    got = HP.run(st, {"OPS_HEALTH_URLS": "finance"}, T0)          # malformed: an alert, never an exception
    assert got == [("health_poll_unconfigured", "ops-alerts", "malformed")]
