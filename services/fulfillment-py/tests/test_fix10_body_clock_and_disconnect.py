"""
Fix wave 10 (AEGIS round 9), fulfillment-py.

N9-6 — the early 408 ("the declared body cannot arrive by the deadline at
    its rate", fix wave 9) mixed two clocks: it measured the rate on the
    time the service spent WAITING on the client (`seen = received /
    waited`) and then compared `now + (declared - received) / seen` with the
    wall-clock `deadline`. The two differ whenever the service itself holds
    the request (large-lane admission, a wait for in-flight bytes): that
    time left the wall budget but not the rate's denominator, while the bytes
    the client sent meanwhile were still unread. A client uploading fast
    enough was refused 408 "send faster" for time the service spent. Now the
    rule is on one clock — the waiting clock (see api.py and the README).

N9-8 — a client that disconnects mid-body raised starlette's ClientDisconnect
    out of the app: uvicorn logged "Exception in ASGI application" with a
    ~60-line traceback per disconnect (round 9 logs: 77 in one run). Now an
    expected disconnect logs exactly one structured warning line (route,
    bytes received, declared, elapsed) with no traceback; any other
    exception is untouched and still logs its traceback.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 18550-18599).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

import pytest
from starlette.requests import ClientDisconnect

from test_fix8_n7_2_body_prealloc import DETECT, _Client, _event
from test_fix9_inflight_fairness import _large_body, _paced
from test_live_server import SRC, TOKEN, _free_port

import api
from conftest import child_env

KIB = 1024


# --- N9-6: the arrival projection is on ONE clock ------------------------------

def _hold_scenario(monkeypatch, factor: float, hold_at: float, hold_for: float):
    """Scaled boundary (deadline 3 s, grace 0.5 s): a 300 kB body needs
    100 kB/s. The client sends at `factor` x that, steadily, whatever the
    service does. At `hold_at` s someone else takes the rest of the shared
    in-flight budget, so the SERVICE stops reading this body (its next chunk
    waits for in-flight bytes) for `hold_for` s — the client's bytes queue up
    meanwhile, exactly as they do in the socket buffers. From then on the
    time the service waited on the client is `hold_for` s short of the
    elapsed time."""
    shared = 256 * KIB
    monkeypatch.setattr(api, "_BODY_READ_TIMEOUT_S", 3.0)
    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 0.5)
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", api._SMALL_RESERVE_BYTES + shared)  # fix wave 24: the total includes the small reserve; `shared` is the shared pool
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 2.0)
    monkeypatch.setattr(api, "_PREEMPT_BYTE_SECONDS", 1e12)  # no preemption in this test
    body = _large_body(300_000)
    need = len(body) / 3.0

    async def scenario():
        lanes = api._lanes()
        c = _Client(content_length=len(body))
        task = asyncio.ensure_future(c.run())
        t0 = asyncio.get_running_loop().time()
        sender = asyncio.ensure_future(_paced(c, body, need * factor))
        done, _ = await asyncio.wait({task}, timeout=hold_at)
        taken = 0
        if not done:
            taken = lanes.inflight.limit - lanes.inflight.used
            await lanes.inflight.reserve(taken)  # the service now holds this body
            await asyncio.wait({task}, timeout=hold_for)
            lanes.inflight.release(taken)
        await asyncio.wait_for(task, 6)
        took = asyncio.get_running_loop().time() - t0
        sender.cancel()
        return c, took, taken

    return asyncio.run(scenario())


def test_n9_6_time_the_service_holds_a_body_is_not_charged_to_the_projection(monkeypatch):
    """Fails on ea708a1: 408 "cannot complete within the 3s body deadline"
    at ~2.1 s, reporting a rate of ~130 kB/s — above the 100 kB/s the body
    needs — for a client whose last byte was sent at 2.3 s. The 1.3 s the
    service held the body had left the wall-clock budget but not the rate's
    denominator, and the ~170 kB the client sent during it were unread."""
    c, took, taken = _hold_scenario(monkeypatch, factor=1.3, hold_at=0.8, hold_for=1.3)
    assert taken > 0, "the hold never happened (body finished early?)"
    assert c.status == 200, (c.status, c.body[:400], round(took, 2))
    # fix wave 25 (E-A successor, R-HYGIENE L1): `took < 3.0` dropped — the 3 s body deadline is a hard wall-clock
    # deadline in the middleware, so a 200 is already "answered before the deadline"


def test_n9_6_a_slow_client_is_still_refused_at_the_grace_despite_a_service_hold(monkeypatch):
    """The other direction: a service hold must not let a body that is too
    slow on its own escape the rule. 0.8x the needed rate: refused 408 at the
    grace (0.5 s of waiting), before and regardless of the hold, and the
    detail's numbers agree with the refusal (reported rate < needed rate)."""
    c, took, _ = _hold_scenario(monkeypatch, factor=0.8, hold_at=0.8, hold_for=1.3)
    assert c.status == 408, (c.status, c.body[:400])
    detail = json.loads(c.body)["detail"]
    assert "cannot complete" in detail and "split the batch" in detail, detail
    rate = float(re.search(r"arriving at ~(\d+) bytes/s", detail).group(1))
    needs = float(re.search(r"needs >= (\d+) bytes/s", detail).group(1))
    assert rate < needs, detail
    # fix wave 25 (E-A successor, R-HYGIENE L1): was also `took < 1.2` (wall clock). The refusal is rule (c)'s own
    # ("cannot complete", rate < needs above), which can fire only once 0.5 s of waiting is spent; load only delays it
    assert took >= 0.5, f"refused after {took:.2f}s: before the 0.5 s grace"


@pytest.mark.parametrize("factor, expect", [(1.1, 200), (0.9, 408)])
def test_n9_6_without_a_hold_the_boundary_is_unchanged(monkeypatch, factor, expect):
    """No service hold (waited ~= elapsed): 1.1x arrives, 0.9x is refused at
    the grace — the fix wave 9 behaviour, closer to the boundary than the
    existing 1.3x / 0.75x test."""
    c, took, taken = _hold_scenario(monkeypatch, factor=factor, hold_at=10.0, hold_for=0.0)
    assert c.status == expect, (c.status, c.body[:400], round(took, 2))
    if expect == 408:
        assert took >= 0.5, took  # fix wave 25 (R-HYGIENE L1): was `took < 1.2`; rule (c) below, after the grace
        detail = json.loads(c.body)["detail"]
        assert "cannot complete" in detail, detail
        rate = float(re.search(r"arriving at ~(\d+) bytes/s", detail).group(1))
        needs = float(re.search(r"needs >= (\d+) bytes/s", detail).group(1))
        assert rate < needs, detail


# --- N9-8: an expected client disconnect is one line, not a traceback ------------

def _fulfillment_records(caplog):
    return [r for r in caplog.records if r.name == "fulfillment"]


def test_n9_8_client_disconnect_mid_body_logs_one_structured_line(caplog):
    """Fails on ea708a1: ClientDisconnect propagates out of the app (uvicorn
    then logs "Exception in ASGI application" and the traceback)."""
    caplog.set_level(logging.INFO, logger="fulfillment")

    async def scenario():
        c = _Client(content_length=5000)
        task = asyncio.ensure_future(c.run())
        await c.feed(b'{"call_events":[' + b" " * 1200)
        await asyncio.sleep(0.2)
        await c.disconnect()
        await asyncio.wait_for(task, 5)  # must not raise
        return c

    t0 = time.monotonic()
    c = asyncio.run(scenario())
    ran = time.monotonic() - t0
    assert c.status is None, "nothing is sent to a client that has gone"
    records = _fulfillment_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in records]
    rec = records[0]
    assert rec.levelno in (logging.INFO, logging.WARNING), rec.levelname
    assert rec.exc_info is None and rec.exc_text is None and rec.stack_info is None
    msg = rec.getMessage()
    assert "\n" not in msg, msg
    assert f"route={DETECT}" in msg, msg
    assert "bytes_received=1216" in msg, msg
    assert "declared=5000" in msg, msg
    elapsed = float(re.search(r"elapsed_s=([0-9.]+)", msg).group(1))
    # fix wave 25 (E-A successor, R-HYGIENE L1): was `0.15 < elapsed < 2.0`. The logged elapsed is this request's:
    # at least the 0.2 s the client waited before leaving, at most the time the whole scenario ran
    assert 0.15 < elapsed <= ran + 0.01, (msg, ran)


def test_n9_8_disconnect_before_any_body_byte_is_also_one_line(caplog):
    caplog.set_level(logging.INFO, logger="fulfillment")

    async def scenario():
        c = _Client(content_length=100)
        task = asyncio.ensure_future(c.run())
        await c.disconnect()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    records = _fulfillment_records(caplog)
    assert len(records) == 1 and "bytes_received=0" in records[0].getMessage(), [r.getMessage() for r in records]
    assert records[0].exc_info is None


def test_n9_8_a_client_disconnect_the_client_did_not_make_still_propagates(monkeypatch, caplog):
    """Only a disconnect the middleware actually saw is expected. A
    ClientDisconnect raised with the client still connected is a bug, and
    must surface as an exception (so the launcher logs its traceback)."""
    caplog.set_level(logging.INFO, logger="fulfillment")

    async def boom(*_a, **_k):
        raise ClientDisconnect()

    monkeypatch.setattr(api, "_off_loop", boom)

    async def scenario():
        body = _event()
        c = _Client(content_length=len(body))
        await c.feed(body, more=False)
        await c.run()

    with pytest.raises(ClientDisconnect):
        asyncio.run(scenario())
    assert _fulfillment_records(caplog) == []


def test_n9_8_other_exceptions_are_untouched(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="fulfillment")

    def boom(*_a, **_k):
        raise RuntimeError("not a disconnect")

    monkeypatch.setattr(api, "_parse", boom)

    async def scenario():
        body = _event()
        c = _Client(content_length=len(body))
        await c.feed(body, more=False)
        await c.run()

    with pytest.raises(RuntimeError, match="not a disconnect"):
        asyncio.run(scenario())
    assert _fulfillment_records(caplog) == []


# --- N9-8 on the real launcher: what the operator's log actually shows ------------

_LAUNCH = (
    "import api\n"
    "real = api._parse\n"
    "def _parse(model, body):\n"
    "    if b'EXPLODE' in body:\n"
    "        raise RuntimeError('deliberate test failure EXPLODE')\n"
    "    return real(model, body)\n"
    "api._parse = _parse\n"
    "api.main()\n"
)


def _head(path: str, content_length: int) -> bytes:
    return (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n\r\n").encode()


@pytest.fixture(scope="module")
def logged_server():
    port = _free_port()
    log = tempfile.NamedTemporaryFile(prefix="ful-fix10-", suffix=".log", delete=False)
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SRC), **child_env(), "FULFILLMENT_SERVICE_TOKEN": TOKEN,
           "FULFILLMENT_PORT": str(port), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-c", _LAUNCH], env=env, cwd=str(SRC), stdout=log, stderr=log)
    try:
        # Fix wave 25 (scout A F5): the answer on the port is this child's only once the child itself has logged its
        # bind ("Uvicorn running on", as test_live_server._start since wave 25); monotonic deadline.
        deadline = time.monotonic() + 15
        while True:
            assert proc.poll() is None, open(log.name).read()[-2000:]
            assert time.monotonic() < deadline, "service did not start: " + open(log.name).read()[-2000:]
            if f"Uvicorn running on http://127.0.0.1:{port}" not in open(log.name, errors="replace").read():
                time.sleep(0.05)
                continue
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                conn.request("GET", "/health")
                if conn.getresponse().status == 200:
                    break
            except OSError:
                time.sleep(0.1)
        yield port, log.name
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()                       # fix wave 22: the log file never outlives the module (it used to be
        os.unlink(log.name)               # left in TMPDIR on every run)


def _log_since(path: str, offset: int) -> str:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read().decode(errors="replace")


def _settle(path: str, needle: str, offset: int, port: int, timeout: float = 5.0) -> str:
    """The log from `offset` once `needle` is in it AND everything logged in the same event-loop step is too.
    Fix wave 25 (scout A F4): it slept a fixed 0.3 s after the needle and the callers then asserted that NO
    traceback followed — on a loaded box a traceback written later than that was simply not seen. The needle and
    a traceback that would follow it are written in one step of the server's event loop (no await between the
    middleware's log line and uvicorn's handler for an escaping exception), so one GET /health answered AFTER the
    needle appeared is a barrier: the loop has finished that step before it served the probe."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if needle in _log_since(path, offset):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/health")
            assert conn.getresponse().status == 200
            conn.close()
            return _log_since(path, offset)
        time.sleep(0.05)
    return _log_since(path, offset)


def test_n9_8_live_disconnect_mid_body_is_one_line_without_traceback(logged_server):
    port, log = logged_server
    start = len(open(log, "rb").read())
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.sendall(_head(DETECT, 200_000) + b'{"call_events":[' + b" " * 30_000)
        time.sleep(0.5)
    text = _settle(log, "client disconnected", start, port)
    lines = [ln for ln in text.splitlines() if "client disconnected" in ln]
    assert len(lines) == 1, text[-3000:]
    assert f"route={DETECT}" in lines[0] and "bytes_received=" in lines[0] and "elapsed_s=" in lines[0], lines
    assert "Traceback" not in text and "Exception in ASGI application" not in text, text[-3000:]


def test_n9_8_live_unexpected_exception_still_logs_a_traceback(logged_server):
    port, log = logged_server
    start = len(open(log, "rb").read())
    body = json.dumps({"call_events": [], "EXPLODE": 1}).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(_head(DETECT, len(body)) + body)
        status = s.recv(64)
    assert status.startswith(b"HTTP/1.1 500"), status
    text = _settle(log, "deliberate test failure EXPLODE", start, port)
    assert "Traceback" in text and "RuntimeError: deliberate test failure EXPLODE" in text, text[-3000:]
    assert "client disconnected" not in text, text[-3000:]
