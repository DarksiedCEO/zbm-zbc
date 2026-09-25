"""
Fix wave 8, N7-2 (MED-HIGH, CONFIRMED; a fix wave 7 regression): the body
buffer was pre-allocated from the client's Content-Length, so idle
connections pinned memory at zero bandwidth.

The finding (AEGIS `ful_idle7b.py`): 128 connections each sending a request
head with `Content-Length: 4194304` plus ONE byte took RSS from 55 to 569 MB,
held until the 30 s body deadline. `api._off_loop` did
`body = bytearray(declared or 0)` — 4 MiB memset per connection before a
single body byte had arrived.

What must hold (in-process against the ASGI app with hand-driven chunk
delivery, and live against the real `python3 -m api` over TCP):
  - nothing is allocated ahead of the bytes received: the buffer grows with
    the data (never from a client-declared size), so the AEGIS scenario
    keeps RSS growth small (< 20 MB for 128 connections);
  - the bytes actually buffered by all in-flight bodies share one budget
    (`_INFLIGHT_BODY_BYTES`): with it exhausted by stalled senders, a request
    that cannot buffer its next chunk within `_INFLIGHT_WAIT_S` is answered
    503 + Retry-After, and admitted again once the bytes are released;
  - a body that trickles is cut by a minimum-throughput rule (< 1 KiB/s after
    the first 5 s → 408), not held until the 30 s deadline — measured on
    time spent waiting for the client, so a request stalled by the service's
    own budget wait is not blamed;
  - legitimate large batches still succeed; the wave-7 lane tests still pass.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 20720-20739).
"""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import time
import tracemalloc

import pytest

from conftest import TEST_SERVICE_TOKEN
from test_fix4_limits import MAX_BATCHES
from test_fix5_http_limits_live import _health, _start as _start_quiet, _stop
from test_live_server import TOKEN

import api
import http_limits

MIB = 1024 * 1024
DETECT = "/agents/missed-call-detection/detect"
ORCHESTRATE = "/agents/callback-orchestration/run"
HEADERS = [(b"authorization", f"Bearer {TEST_SERVICE_TOKEN}".encode()), (b"content-type", b"application/json")]


# --- an ASGI request whose body chunks are delivered on demand -------------------

class _Client:
    """Drives api.app directly: chunks go in through `feed`, the response
    (status, headers, body) comes out of `run`. The request stays open
    (`more_body`) until `finish` or `disconnect`."""

    def __init__(self, path: str = DETECT, content_length: int | None = None, headers=None):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""
        hdrs = list(HEADERS if headers is None else headers)
        if content_length is not None:
            hdrs.append((b"content-length", str(content_length).encode()))
        self.scope = {"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
                      "raw_path": path.encode(), "query_string": b"", "headers": hdrs, "client": ("127.0.0.1", 1),
                      "server": ("127.0.0.1", 80)}

    async def feed(self, data: bytes, more: bool = True) -> None:
        await self.queue.put({"type": "http.request", "body": data, "more_body": more})

    async def finish(self) -> None:
        await self.feed(b"", more=False)

    async def disconnect(self) -> None:
        await self.queue.put({"type": "http.disconnect"})

    async def _receive(self):
        return await self.queue.get()

    async def _send(self, message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    async def run(self) -> "_Client":
        await api.app(self.scope, self._receive, self._send)
        return self


def _event() -> bytes:
    return json.dumps({"call_events": [{
        "call_id": "c1", "phone_number": "+15550100", "direction": "inbound", "status": "voicemail",
        "started_at": "2026-09-22T12:00:00Z", "line_id": "l1",
    }]}).encode()


# --- (1) nothing is allocated ahead of the bytes received ---------------------------

def test_declared_content_length_does_not_preallocate_the_body_buffer():
    """32 requests declare a 4 MiB body and send one byte each, then stall.
    Before: 32 x bytearray(4 MiB) = 128 MiB allocated (and memset — resident)
    while nothing had arrived (the large-lane byte budget admits ~8 per second,
    so 21 MiB was traced at 0.3 s). Now the traced heap grows by ~1 MiB: the
    32 requests' own objects, no body buffers."""
    async def scenario():
        clients = [_Client(content_length=4 * MIB) for _ in range(32)]
        tracemalloc.start()
        try:
            base, _ = tracemalloc.get_traced_memory()
            tasks = [asyncio.ensure_future(c.run()) for c in clients]
            for c in clients:
                await c.feed(b"{")
            await asyncio.sleep(0.3)  # every request has read its one byte and is waiting for more
            held, _ = tracemalloc.get_traced_memory()
            for c in clients:
                await c.disconnect()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            tracemalloc.stop()
        return held - base

    grown = asyncio.run(scenario())
    assert grown < 4 * MIB, f"{grown / MIB:.1f} MiB allocated for 32 one-byte bodies declaring 4 MiB each"


def test_no_allocation_in_api_is_driven_by_a_client_declared_size():
    """The sweep: the only place a client-declared size could size an
    allocation is the body buffer; it must not (`bytearray(declared`)."""
    src = open(api.__file__).read()
    assert "bytearray(declared" not in src
    assert "bytearray(0)" not in src or "bytearray()" in src


@pytest.mark.parametrize("declared", [5, 18, 100, None])
def test_body_is_parsed_exactly_as_sent_whatever_the_declared_length(declared):
    """Still true without the pre-sized buffer: understated, overstated or
    missing Content-Length — the parse sees exactly the bytes sent."""
    async def scenario():
        c = _Client(content_length=declared)
        task = asyncio.ensure_future(c.run())
        body = b'{"call_events":[]}'
        await c.feed(body[:7])
        await c.feed(body[7:], more=False)
        return await task

    c = asyncio.run(scenario())
    assert c.status == 200, c.body[:200]
    assert json.loads(c.body) == {"tasks": []}


def test_a_large_body_delivered_in_small_chunks_is_reassembled_and_accepted():
    async def scenario():
        c = _Client(content_length=len(big))
        task = asyncio.ensure_future(c.run())
        for i in range(0, len(big), 4096):
            await c.feed(big[i:i + 4096])
        await c.finish()
        return await task

    big = json.dumps(MAX_BATCHES[DETECT]).encode()
    assert len(big) > api._SMALL_BODY_BYTES
    c = asyncio.run(scenario())
    assert c.status == 200, c.body[:200]
    assert "tasks" in json.loads(c.body)


# --- (2) one budget for the bytes actually buffered, across connections ------------

def test_stalled_bodies_exhaust_the_inflight_budget_and_the_next_chunk_is_503_until_released(monkeypatch):
    chunk = b" " * 16 * 1024
    tail = b'{"call_events":[]}'
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", 2 * (len(chunk) + len(tail)))  # exactly two whole bodies
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 0.2)

    async def scenario():
        stalled = [_Client(content_length=len(chunk) + 18) for _ in range(2)]
        stalled_tasks = [asyncio.ensure_future(c.run()) for c in stalled]
        for c in stalled:
            await c.feed(chunk)  # buffered; the budget is now full
        await asyncio.sleep(0.05)
        third = _Client(content_length=len(chunk) + 18)
        third_task = asyncio.ensure_future(third.run())
        t0 = time.perf_counter()
        await third.feed(chunk)
        await third_task
        refused_after = time.perf_counter() - t0
        # release: the stalled senders finish, their bytes are given back
        for c in stalled:
            await c.feed(b'{"call_events":[]}', more=False)
        await asyncio.gather(*stalled_tasks)
        fourth = _Client(content_length=len(chunk) + 18)
        fourth_task = asyncio.ensure_future(fourth.run())
        await fourth.feed(chunk)
        await fourth.feed(b'{"call_events":[]}', more=False)
        await fourth_task
        return stalled, third, refused_after, fourth

    stalled, third, refused_after, fourth = asyncio.run(scenario())
    assert third.status == 503, (third.status, third.body[:200])
    assert third.headers.get("retry-after") == "1"
    assert "in-flight" in third.body.decode()
    assert 0.15 < refused_after < 0.6, refused_after
    assert [c.status for c in stalled] == [200, 200], [c.body[:100] for c in stalled]
    assert fourth.status == 200, fourth.body[:200]


def test_inflight_budget_is_released_when_a_sender_disconnects_mid_body(monkeypatch):
    chunk = b" " * 8 * 1024
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", len(chunk) + 18)  # exactly one whole body
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 0.2)

    async def scenario():
        a = _Client(content_length=len(chunk) + 18)
        a_task = asyncio.ensure_future(a.run())
        await a.feed(chunk)
        await asyncio.sleep(0.05)
        await a.disconnect()
        await asyncio.gather(a_task, return_exceptions=True)
        b = _Client(content_length=len(chunk) + 18)
        b_task = asyncio.ensure_future(b.run())
        await b.feed(chunk)
        await b.feed(b'{"call_events":[]}', more=False)
        await b_task
        return b

    b = asyncio.run(scenario())
    assert b.status == 200, b.body[:200]


def test_inflight_budget_and_throughput_rule_are_the_documented_ones():
    assert api._INFLIGHT_BODY_BYTES == 64 * MIB
    assert api._INFLIGHT_BODY_BYTES == api._LARGE_BYTES_PER_S * api._LARGE_WAIT_S
    assert api._INFLIGHT_WAIT_S == 2.0
    assert http_limits.BODY_MIN_BYTES_PER_S == 1024
    assert http_limits.BODY_MIN_RATE_GRACE_S == 5.0
    assert http_limits.LIMIT_CONCURRENCY * api._MAX_BODY_BYTES > api._INFLIGHT_BODY_BYTES  # the budget binds


# --- (3) a trickling body is cut by the minimum-throughput rule ---------------------

def test_trickling_body_is_408_after_the_grace_period_not_at_the_deadline(monkeypatch):
    monkeypatch.setattr(api, "_BODY_READ_TIMEOUT_S", 6.0)
    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 0.5)
    monkeypatch.setattr(api, "_BODY_MIN_BYTES_PER_S", 1024)

    async def scenario():
        c = _Client(content_length=100_000)
        task = asyncio.ensure_future(c.run())
        t0 = time.perf_counter()
        await c.feed(b"{")
        while not task.done() and time.perf_counter() - t0 < 5.0:
            await asyncio.sleep(0.1)
            await c.feed(b" ")  # 10 B/s: far below 1 KiB/s
        return c, time.perf_counter() - t0, task.done()

    c, took, done = asyncio.run(scenario())
    assert done, "the trickling body was not cut"
    assert c.status == 408, (c.status, c.body[:200])
    assert "bytes/s" in c.body.decode()
    assert 0.4 < took < 1.5, f"cut after {took:.2f}s (grace 0.5 s, deadline 6 s)"


def test_front_loaded_body_that_then_stalls_is_408_after_the_grace_period(monkeypatch):
    """Per-chunk progress: 200 KiB sent at once earns no credit for a stall.
    An average-rate rule alone would have let this one sit for the deadline."""
    monkeypatch.setattr(api, "_BODY_READ_TIMEOUT_S", 6.0)
    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 0.5)
    monkeypatch.setattr(api, "_BODY_MIN_BYTES_PER_S", 1024)

    async def scenario():
        c = _Client(content_length=4 * MIB)
        task = asyncio.ensure_future(c.run())
        t0 = time.perf_counter()
        await c.feed(b'{"call_events":[' + b" " * 200 * 1024)
        await asyncio.wait_for(task, 5.0)
        return c, time.perf_counter() - t0

    c, took = asyncio.run(scenario())
    assert c.status == 408, (c.status, c.body[:200])
    assert "stalled" in c.body.decode()
    assert 0.4 < took < 1.5, f"cut after {took:.2f}s (grace 0.5 s, deadline 6 s)"


def test_a_body_arriving_at_or_above_the_minimum_rate_is_not_cut(monkeypatch):
    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 0.2)
    monkeypatch.setattr(api, "_BODY_MIN_BYTES_PER_S", 1024)

    async def scenario():
        payload = b'{"call_events":[]' + b" " * 6000 + b"}"  # ~6 KB over ~1.2 s = ~5 KiB/s
        c = _Client(content_length=len(payload))
        task = asyncio.ensure_future(c.run())
        for i in range(0, len(payload), 500):
            await c.feed(payload[i:i + 500])
            await asyncio.sleep(0.1)
        await c.finish()
        return await task

    c = asyncio.run(scenario())
    assert c.status == 200, (c.status, c.body[:200])


def test_time_spent_waiting_for_the_inflight_budget_is_not_charged_to_the_client(monkeypatch):
    """The service's own wait must not turn into a 408 for a fast client
    (the budget is held directly here: a stalled request holding it would
    itself be cut by the stall rule and release it)."""
    chunk = b" " * 8 * 1024
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", len(chunk) + 18)  # exactly one whole body
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 3.0)
    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 0.3)
    monkeypatch.setattr(api, "_BODY_MIN_BYTES_PER_S", 1024)

    async def scenario():
        inflight = api._lanes().inflight
        await inflight.reserve(inflight.limit)  # someone else's bytes fill the budget
        fast = _Client(content_length=len(chunk) + 18)
        fast_task = asyncio.ensure_future(fast.run())
        await fast.feed(chunk)  # cannot be buffered: waits for the budget
        await fast.feed(b'{"call_events":[]}', more=False)  # already sent everything
        await asyncio.sleep(0.8)  # longer than the grace period, all of it the service's wait
        assert not fast_task.done(), (fast.status, fast.body[:200])
        inflight.release(inflight.limit)
        await fast_task
        return fast

    fast = asyncio.run(scenario())
    assert fast.status == 200, (fast.status, fast.body[:200])


# --- the real process: the AEGIS scenario, a trickle, and legit large batches -------

@pytest.fixture(scope="module")
def server():
    proc, port = _start_quiet()
    yield proc, port
    _stop(proc)


def _rss_mib(pid: int) -> int:
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    raise AssertionError("no VmRSS")


def _head(content_length: int) -> bytes:
    return (f"POST {DETECT} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n\r\n").encode()


def test_live_128_idle_connections_declaring_4mib_each_do_not_pin_memory(server):
    """AEGIS ful_idle7b: head + Content-Length: 4194304 + one byte, held.
    Before: RSS 55 -> 569 MB (the large-lane byte budget admitted 8 such
    requests per second, each allocating 4 MiB on admission: +80 MiB after
    2 s here, +160 after 4 s, all 128 by 16 s). Now: growth < 20 MB."""
    proc, port = server
    base = _rss_mib(proc.pid)
    socks = []
    try:
        for _ in range(http_limits.LIMIT_CONCURRENCY):
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
            s.sendall(_head(4 * MIB) + b"{")
            socks.append(s)
        time.sleep(4.0)
        held = _rss_mib(proc.pid)
    finally:
        for s in socks:
            s.close()
    time.sleep(1.0)
    after = _rss_mib(proc.pid)
    print(f"RSS base {base} held {held} after-close {after} MiB")
    assert held - base < 20, f"RSS grew {held - base} MiB with {len(socks)} idle bodies"
    assert _health(port)[0] == 200


def test_live_trickling_body_is_cut_within_the_grace_period_not_the_30s_deadline(server):
    _, port = server
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    t0 = time.monotonic()
    got = b""
    try:
        s.sendall(_head(100_000) + b"{")
        while time.monotonic() - t0 < http_limits.BODY_MIN_RATE_GRACE_S + 4:
            try:
                s.sendall(b" ")  # 20 B/s
            except (BrokenPipeError, ConnectionResetError):
                break
            s.settimeout(0.05)
            try:
                chunk = s.recv(65536)
                if chunk == b"":
                    break
                got += chunk
                if b"\r\n\r\n" in got:
                    break
            except (TimeoutError, socket.timeout, BlockingIOError):
                pass
            except ConnectionResetError:
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - t0
    finally:
        s.close()
    assert got.startswith(b"HTTP/1.1 408"), got[:120]
    assert elapsed < http_limits.BODY_MIN_RATE_GRACE_S + 2.5, f"trickle held {elapsed:.1f}s"
    assert elapsed >= http_limits.BODY_MIN_RATE_GRACE_S - 0.5, f"cut too early at {elapsed:.1f}s"


def test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut(server):
    """Real bytes, then a stall: before, 128 x 4 MiB pinned for 30 s. Now at
    most _INFLIGHT_BODY_BYTES (64 MiB) is buffered (the rest 503), and the
    stalled bodies are cut by the throughput rule within the grace period, so
    RSS is back near baseline well before the 30 s deadline."""
    import threading
    proc, port = server
    base = _rss_mib(proc.pid)
    payload = b'{"call_events":[' + b" " * (3_900_000 - 16)
    socks, codes, lock = [], {}, threading.Lock()

    def sender():
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=30)
            with lock:
                socks.append(s)
            s.sendall(_head(4 * MIB) + payload)
            s.settimeout(20)
            try:
                got = s.recv(64)[9:12].decode() or "closed"
            except OSError as exc:
                got = type(exc).__name__
        except OSError as exc:
            got = "connect:" + type(exc).__name__
        with lock:
            codes[got] = codes.get(got, 0) + 1

    threads = [threading.Thread(target=sender) for _ in range(http_limits.LIMIT_CONCURRENCY)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    peak = base
    samples = []
    while any(t.is_alive() for t in threads) and time.monotonic() - t0 < 25:
        time.sleep(0.5)
        r = _rss_mib(proc.pid)
        peak = max(peak, r)
        samples.append((round(time.monotonic() - t0, 1), r))
    for t in threads:
        t.join(timeout=5)
    for s in socks:
        s.close()
    settled_at = next((at for at, r in samples if at > 2 and r - base < 24), None)
    print(f"codes {codes}; RSS base {base} peak {peak}; samples {samples}")
    # the budget, plus uvicorn's own per-connection buffers (<= 64 KiB x 128) and the
    # 503'd bodies' drains — measured +84 MiB (base 54, peak 138)
    assert peak - base < api._INFLIGHT_BODY_BYTES // MIB + 32, f"RSS grew {peak - base} MiB"
    assert codes.get("408", 0) + codes.get("503", 0) == len(threads), codes  # none held silently
    assert codes.get("408", 0) > 0, codes  # the admitted, stalled ones were cut
    # admitted bodies are cut at the app's grace; the 503'd senders' unread
    # bytes go when the protocol's (grace + BODY_DEADLINE_GRACE_S) closes them
    bound = http_limits.BODY_MIN_RATE_GRACE_S + http_limits.BODY_DEADLINE_GRACE_S + 3
    assert settled_at is not None and settled_at < bound, samples


def test_live_unread_trickling_body_is_closed_by_the_protocol_within_the_grace_period(server):
    """No token: the app answers 401 and never reads the body. The protocol's
    own throughput rule (judged BODY_DEADLINE_GRACE_S after the app's, so an
    app that is reading writes its 408 first) closes the socket, not the
    30 s + 5 s deadline."""
    _, port = server
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall((f"POST {DETECT} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               f"Content-Length: 100000\r\n\r\n").encode() + b"{")
    t0 = time.monotonic()
    bound = http_limits.BODY_MIN_RATE_GRACE_S + http_limits.BODY_DEADLINE_GRACE_S + 3.0
    closed = False
    try:
        while time.monotonic() - t0 < bound + 5:
            try:
                s.sendall(b" ")
            except (BrokenPipeError, ConnectionResetError):
                closed = True
                break
            s.settimeout(0.05)
            try:
                if s.recv(65536) == b"":
                    closed = True
                    break
            except (TimeoutError, socket.timeout):
                pass
            except ConnectionResetError:
                closed = True
                break
            time.sleep(0.2)
        elapsed = time.monotonic() - t0
    finally:
        s.close()
    assert closed, "unread trickling body was not closed"
    assert elapsed < bound, f"unread trickle held {elapsed:.1f}s"


def _request(port, method, path, body=None, timeout=60):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, body=body, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def test_live_legit_large_batches_still_succeed(server):
    _, port = server
    for route, payload in MAX_BATCHES.items():
        body = json.dumps(payload).encode()
        status_code, data = _request(port, "POST", route, body)
        assert status_code == 200, (route, status_code, data[:200])


def test_live_fast_large_body_sent_in_small_writes_is_accepted(server):
    """A client writing a 3.5 MB body in 4 KiB writes with tiny pauses is
    still far above 1 KiB/s and must be accepted whole."""
    _, port = server
    body = json.dumps(MAX_BATCHES[ORCHESTRATE]).encode()
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        s.sendall(_head(len(body)).replace(DETECT.encode(), ORCHESTRATE.encode()))
        for i in range(0, len(body), 64 * 1024):
            s.sendall(body[i:i + 64 * 1024])
            time.sleep(0.002)
        got = b""
        while b"\r\n\r\n" not in got:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
    finally:
        s.close()
    assert got.startswith(b"HTTP/1.1 200"), got[:120]
