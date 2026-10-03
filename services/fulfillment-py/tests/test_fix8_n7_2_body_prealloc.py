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
import ctypes
import http.client
import json
import os
import socket
import sys
import time
import tracemalloc

import pytest

from _procinfo import rss_mib
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


async def _until(predicate, timeout: float = 10.0) -> None:
    """Fix wave 25 (AEGIS N24-S-12): wait for a state of the app, not for a
    wall-clock interval. These in-process tests used to `sleep(0.05)` and
    assume the app had got there — each request crosses the thread pool (the
    sync auth dependency) first, so on a starved box it had not, and a later
    request took the budget the test meant for an earlier one."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError(f"the app did not reach the expected state within {timeout:g}s")
        await asyncio.sleep(0.01)


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
            # fix wave 25 (scout A F4): until every request has TAKEN its one byte (its queue is empty) or been
            # answered — it slept 0.3 s, and a request that had not read its byte yet allocated nothing for it, so
            # the measurement could pass without measuring (the large lane admits ~8 bodies a second)
            await _until(lambda: all(c.queue.empty() or c.status is not None for c in clients), timeout=30)
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
    # Fix wave 9: bodies' first _SMALL_BODY_BYTES come from a separate reserve,
    # so these 8-16 KiB bodies would never touch the shared budget; count them
    # as large (past 1 KiB) so the shared budget is what is exercised here.
    monkeypatch.setattr(api, "_SMALL_BODY_BYTES", 1024)
    chunk = b" " * 16 * 1024
    tail = b'{"call_events":[]}'
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", api._SMALL_RESERVE_BYTES + 2 * (len(chunk) + len(tail)))  # shared pool: exactly two whole bodies (fix wave 24: the total includes the small reserve)
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 0.2)

    async def scenario():
        lanes = api._lanes()
        stalled = [_Client(content_length=len(chunk) + 18) for _ in range(2)]
        stalled_tasks = [asyncio.ensure_future(c.run()) for c in stalled]
        for c in stalled:
            await c.feed(chunk)  # buffered; the budget is now full
        # fix wave 25: until both stalled bodies' bytes are counted (it was sleep(0.05): N24-S-12, module run B#9)
        await _until(lambda: lanes.inflight.used >= 2 * (len(chunk) - api._SMALL_BODY_BYTES))
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
    # fix wave 25 (scout A F3; R-HYGIENE L1): it waited for the budget before refusing (the lower bound cannot flake
    # under load); the upper bound (< 0.6 s) measured the box, and the 503 "in-flight" above is the refusal itself.
    assert refused_after > 0.15, refused_after
    assert [c.status for c in stalled] == [200, 200], [c.body[:100] for c in stalled]
    assert fourth.status == 200, fourth.body[:200]


def test_inflight_budget_is_released_when_a_sender_disconnects_mid_body(monkeypatch):
    # Fix wave 9: bodies' first _SMALL_BODY_BYTES come from a separate reserve,
    # so these 8-16 KiB bodies would never touch the shared budget; count them
    # as large (past 1 KiB) so the shared budget is what is exercised here.
    monkeypatch.setattr(api, "_SMALL_BODY_BYTES", 1024)
    chunk = b" " * 8 * 1024
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", api._SMALL_RESERVE_BYTES + len(chunk) + 18)  # shared pool: exactly one whole body (fix wave 24: the total includes the small reserve)
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
    # fix wave 25 (E-A successor, R-HYGIENE L1): was `0.4 < took < 1.5`. The rate rule's 408 (above) is not the 6 s
    # deadline's ("not received within 6s"); it can fire only after the 0.5 s grace (load only delays it)
    assert "not received within" not in c.body.decode(), c.body[:200]
    assert took > 0.4, f"cut after {took:.2f}s (grace 0.5 s)"


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
    # fix wave 25 (E-A successor, R-HYGIENE L1): was `0.4 < took < 1.5`; "stalled" is the stall rule's 408, not the
    # 6 s deadline's, and `wait_for(task, 5.0)` above already fails a body held to the deadline
    assert took > 0.4, f"cut after {took:.2f}s (grace 0.5 s)"


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
    # Fix wave 9: bodies' first _SMALL_BODY_BYTES come from a separate reserve,
    # so these 8-16 KiB bodies would never touch the shared budget; count them
    # as large (past 1 KiB) so the shared budget is what is exercised here.
    monkeypatch.setattr(api, "_SMALL_BODY_BYTES", 1024)
    chunk = b" " * 8 * 1024
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", api._SMALL_RESERVE_BYTES + len(chunk) + 18)  # shared pool: exactly one whole body (fix wave 24: the total includes the small reserve)
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

# Fix wave 26b (CI3-6; R26-5's W26-EA-1 hypothesis, confirmed by CI #3 macos-26 and on this build box, an M4 Pro):
# on macOS BOTH phys_footprint and `ps` RSS stayed at their peak for the whole 13 s settle bound (CI: 47 -> 123 MiB
# and 63 -> 138; here 49 -> 128 and 64 -> 142). The cause is libmalloc's large-allocation cache: a freed 3.9 MB body
# buffer is kept by the allocator for reuse, still charged to the process. Measured with 20 such buffers built from
# 64 KiB chunks and freed: footprint 87 MiB held, 87 after the free, 87 after malloc_zone_pressure_relief() (it
# released 0); with MallocLargeCache=0, 82 held and 6 after the free (MallocMediumZone=0 changes nothing). So the
# settle check read the allocator's cache, not what the server holds. glibc unmaps blocks this large at free, which
# is why Linux settles (W26-2c: 7.0-7.5 s, with or without _malloc_trim). The server under THIS module's memory tests
# therefore starts with libmalloc's large cache off on macOS — read by libmalloc at process start, so it has to be in
# the launch environment — and the footprint then measures the server's own frees, as VmRSS does on Linux. The
# bounds are unchanged; production runs on Linux.
_ALLOCATOR_ENV = {"MallocLargeCache": "0"} if sys.platform == "darwin" else {}


@pytest.fixture(scope="module")
def server():
    proc, port = _start_quiet(env_extra=_ALLOCATOR_ENV)
    yield proc, port
    _stop(proc)


# Fix wave 16: portable (Linux /proc, macOS/BSD ps); measures only the server pid.
_rss_mib = rss_mib


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


def _send_reading(s: socket.socket, data: bytes, idle_timeout: float, answer: dict | None = None) -> str:
    """Send ``data`` while reading; returns the answer's status code once the
    answer is complete to its Content-Length, "closed" on EOF with no answer,
    or the error that ended the exchange before a complete answer. After the
    last byte is sent the client waits up to ``idle_timeout`` for the answer.
    When ``answer`` is given, ``answer["head"]`` receives the answer's raw head
    and ``answer["sent"]`` the bytes sent (fix wave 23: a keep-alive caller
    decides from the head whether the connection can be reused)."""
    import select
    s.setblocking(False)
    sent, buf, last = 0, b"", time.monotonic()
    while time.monotonic() - last < idle_timeout:
        r, w, _ = select.select([s], [s] if sent < len(data) else [], [], 0.5)
        if r:
            try:
                chunk = s.recv(65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                return type(exc).__name__
            if not chunk:
                return buf[9:12].decode() or "closed"
            buf += chunk
            head, sep, body = buf.partition(b"\r\n\r\n")
            if sep:
                cl = [int(ln.split(b":", 1)[1]) for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length:")]
                if not cl or len(body) >= cl[0]:
                    if answer is not None:
                        answer["head"], answer["sent"] = head, sent
                    return buf[9:12].decode()
        elif w:
            try:
                sent += s.send(data[sent:sent + 65536])
                last = time.monotonic()
            except BlockingIOError:
                pass
            except OSError:
                sent = len(data)              # the server stopped reading: read what it answered
    return "timeout"


# Fix wave 25, H2 (AEGIS N24-S-13): the settle check of the 128-sender test
# could pass without measuring anything — "settled" was any sample after 2 s
# under base + 24 MiB, so a run whose sender threads were slow to start (a
# loaded box) settled at 2.6 s with growth 22 MiB, before the senders had
# built any memory. The check now starts only once memory has reached its
# peak phase (a sample at base + _PEAK_FLOOR_MIB or more), and a run that never
# got there measured nothing: it fails as INVALID, never passes.
_PEAK_FLOOR_MIB = 32   # half the 64 MiB budget the ~20 admitted 3.9 MB bodies fill
_SETTLED_MIB = 24


def _settled_at(samples: list[tuple[float, int]], base: int) -> tuple[float | None, bool]:
    """(the first sample time under base + _SETTLED_MIB AFTER the first sample
    at base + _PEAK_FLOOR_MIB or more — None if none yet; whether that floor
    has been reached)."""
    reached = False
    for at, rss in samples:
        if rss - base >= _PEAK_FLOOR_MIB:
            reached = True
        elif reached and rss - base < _SETTLED_MIB:
            return at, True
    return None, reached


def _hwm_reset(pid: int) -> bool:
    """Linux: reset the process's peak RSS (VmHWM) to its current RSS; False
    where that is not available (the sampled peak is used instead)."""
    try:
        with open(f"/proc/{pid}/clear_refs", "w") as fh:
            fh.write("5")
        return True
    except OSError:
        return False


def _hwm_mib(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


# Fix wave 26 (W26-2c; CI #2: this module's 128-sender test failed on macos-26 with its samples flat at
# "... (12.5, 138), (13.0, 138)]": the bound (13 s) elapsed with RSS still at its peak, so `settled_at` was None;
# the rest of its line was not captured). The settle check assumes that memory the server frees leaves the number
# it reads. On Linux it does, without help: with `_malloc_trim` disabled (ADR 0002 Decision 21's glibc-only call)
# the test still settles at 7.0-7.5 s, as with it (measured, w26-reports/E-A logs; glibc serves blocks this large
# by mmap and unmaps them at free). What macOS's allocator does at free is the open question; the hypothesis
# (NOT measured -- no Mac here) is that it keeps them, marked reusable (madvise MADV_FREE_REUSABLE): such pages stay in the
# resident size `ps -o rss` reports until the kernel reclaims them, but leave the process's physical footprint,
# the kernel's count of the memory charged to it (what jetsam limits and Activity Monitor use). On macOS the test
# therefore reads `phys_footprint` (proc_pid_rusage, RUSAGE_INFO_V0; same user, no privilege needed) and prints the
# `ps` RSS series beside it, so the next Mac run shows which held. The bounds are unchanged: growth <
# _INFLIGHT_BODY_BYTES + 32 MiB and settle to base + 24 MiB within 13 s, derived from what the server holds (the
# 64 MiB budget, uvicorn's per-connection buffers, the 503'd bodies' drains, one parse; ADR 0002) -- on macOS
# that is its footprint, not pages it has handed back. Linux keeps VmRSS and the kernel peak (VmHWM), unchanged.
# Elsewhere (no footprint): `ps` RSS, as before.


class _RusageInfoV0(ctypes.Structure):
    """`struct rusage_info_v0` of macOS <sys/resource.h> (proc_pid_rusage flavor RUSAGE_INFO_V0 = 0)."""

    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups", "ri_pageins", "ri_wired_size",
        "ri_resident_size", "ri_phys_footprint", "ri_proc_start_abstime", "ri_proc_exit_abstime")]


def _darwin_rusage(pid: int) -> _RusageInfoV0 | None:
    """macOS: proc_pid_rusage(pid, RUSAGE_INFO_V0); None anywhere else or if the call fails."""
    if sys.platform != "darwin":
        return None
    try:
        fn = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).proc_pid_rusage
    except (OSError, AttributeError):
        return None
    fn.argtypes, fn.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p], ctypes.c_int
    info = _RusageInfoV0()
    return info if fn(pid, 0, ctypes.byref(info)) == 0 else None


def _held_metric() -> str:
    """Which memory figure `_held_mib` reads on this machine (printed with every 128-sender run)."""
    if sys.platform.startswith("linux"):
        return "VmRSS"
    return "phys_footprint" if _darwin_rusage(os.getpid()) is not None else "ps rss"


def _held_mib(pid: int) -> int:
    """The memory the server holds, in MiB: Linux VmRSS (unchanged), macOS phys_footprint, else `ps` RSS. Where the
    footprint is readable at all (this process), failing to read the server's is an error, never a silent RSS."""
    if _held_metric() == "phys_footprint":
        info = _darwin_rusage(pid)
        assert info is not None, f"proc_pid_rusage({pid}) failed: errno {ctypes.get_errno()}"
        return info.ri_phys_footprint // MIB
    return _rss_mib(pid)


def test_held_memory_metric_is_rss_on_linux_and_the_darwin_struct_matches_sys_resource_h():
    """Fix wave 26 (W26-2c): Linux reads exactly what it read before (VmRSS); the macOS struct has the layout of
    <sys/resource.h> (16-byte uuid, then uint64 fields; phys_footprint is the 8th, at byte 72; 96 bytes)."""
    assert ctypes.sizeof(_RusageInfoV0) == 96 and _RusageInfoV0.ri_phys_footprint.offset == 72
    assert _RusageInfoV0.ri_resident_size.offset == 64
    if sys.platform.startswith("linux"):
        assert _held_metric() == "VmRSS" and _darwin_rusage(os.getpid()) is None
        assert abs(_held_mib(os.getpid()) - _rss_mib(os.getpid())) <= 1
    elif sys.platform == "darwin":
        info = _darwin_rusage(os.getpid())
        assert info is not None and _held_metric() == "phys_footprint"
        # sanity against ps: the resident size the struct reports is ps's RSS (same pages, same moment +- churn)
        assert abs(info.ri_resident_size // MIB - _rss_mib(os.getpid())) <= 8, (info.ri_resident_size, _rss_mib(os.getpid()))
        assert 0 < info.ri_phys_footprint // MIB < 4096


def test_held_memory_on_the_darwin_path_is_the_footprint_and_a_failed_read_is_an_error(monkeypatch):
    """Fix wave 26 (W26-2c), the macOS path driven on any OS through a stand-in libSystem: the struct is filled by
    the same `proc_pid_rusage(pid, 0, byref(info))` call, `_held_mib` returns the footprint (not the resident
    size), and when the server's pid cannot be read the test errors instead of falling back to RSS."""
    calls = []

    class FakeProcPidRusage:
        def __call__(self, pid, flavor, ref):
            calls.append((pid, flavor, self.argtypes, self.restype))
            if pid != os.getpid() and pid != 4242:
                return -1
            info = ref._obj
            info.ri_resident_size, info.ri_phys_footprint = 700 * MIB, 123 * MIB + 5
            return 0

    class FakeLib:
        proc_pid_rusage = FakeProcPidRusage()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda path, use_errno=False: FakeLib() if path == "/usr/lib/libSystem.B.dylib" else None)
    assert _held_metric() == "phys_footprint"
    assert _held_mib(4242) == 123
    assert calls[-1] == (4242, 0, [ctypes.c_int, ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
    with pytest.raises(AssertionError, match="proc_pid_rusage"):
        _held_mib(4243)


def test_the_settle_check_waits_for_the_peak_phase_and_a_run_without_one_is_invalid():
    """N24-S-13, the reviewer's run A single #5 — base 53 MiB, samples [(2.0, 71), (2.6, 75)] (its log): the old
    check said settled_at 2.6 s with growth 22 MiB; it measured nothing. Now: no settle before the floor, and the
    run is reported invalid."""
    vacuous = [(2.0, 71), (2.6, 75)]
    old = next((at for at, r in vacuous if at > 2 and r - 53 < 24), None)
    assert old == 2.6                                         # what the old rule concluded
    assert _settled_at(vacuous, 53) == (None, False)          # not settled, and not a valid run
    real = [(1.0, 53 + 9), (2.0, 53 + 59), (6.0, 53 + 64), (11.1, 53 + 36), (12.2, 53 + 2)]
    assert _settled_at(real, 53) == (12.2, True)
    assert _settled_at(real[:3], 53) == (None, True)          # peak reached, not yet settled


def _runqueue_wait_s(pid: int) -> float | None:
    """Linux: seconds the process's main thread (its event loop) has spent RUNNABLE but not running — waiting for a
    CPU (/proc/<pid>/task/<pid>/schedstat, field 2, ns). None where that is not available."""
    try:
        with open(f"/proc/{pid}/task/{pid}/schedstat") as fh:
            return int(fh.read().split()[1]) / 1e9
    except (OSError, IndexError, ValueError):
        return None


def _senders_on_one_thread(port: int, data: bytes, n: int, idle_timeout: float, every: float, tick) -> list[dict]:
    """Fix wave 25 (H1/H2; AEGIS N24-S-1, -13): the 128 senders of the test below, driven by ONE thread through a
    selector — each exactly `_send_reading`'s client (sends while reading, stops sending at the answer, reads it to
    Content-Length, waits `idle_timeout` after its last byte out) — with `tick(elapsed)` called every `every` s
    (the RSS sampler; it returns False to stop sampling). The test used to run 128 Python threads plus a sampler
    thread: on a loaded 2-CPU box that client process was the bottleneck — the sampler's first 0.5 s sleep came back
    after 1.3-1.7 s typically, 2.9 s and 4.7 s at worst (wave 25, campaign B), and in exactly those runs the senders
    got their bytes out late, so stalled bodies were cut late and the settle bound measured the CLIENT (the one
    module failure: samples from 4.7 s, settled None). One thread with no GIL to share drives the same scenario
    as specified. Returns per sender: code, connected, last_send (s since start; None if nothing went out) and
    answered."""
    import errno
    import selectors

    sel = selectors.DefaultSelector()
    t0 = time.monotonic()
    recs = []

    def finish(rec, code):
        rec["code"], rec["answered"] = code, time.monotonic() - t0
        sel.unregister(rec["sock"])
        rec["sock"].close()

    for _ in range(n):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        rec = {"sock": sock, "sent": 0, "buf": b"", "last": time.monotonic(), "code": None, "connected": None,
               "last_send": None, "answered": None}
        recs.append(rec)
        err = sock.connect_ex(("127.0.0.1", port))
        sel.register(sock, selectors.EVENT_READ | selectors.EVENT_WRITE, rec)
        if err not in (0, errno.EINPROGRESS):
            finish(rec, "connect:" + errno.errorcode.get(err, str(err)))
    next_tick, sampling = t0 + every, True
    while sampling or any(r["code"] is None for r in recs):
        events = sel.select(max(0.0, min(next_tick - time.monotonic(), 0.1)) if sampling else 0.1)
        now = time.monotonic()
        for key, mask in events:
            rec, sock = key.data, key.fileobj
            if rec["code"] is not None:
                continue
            if rec["connected"] is None:
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err:
                    finish(rec, "connect:" + errno.errorcode.get(err, str(err)))
                    continue
                rec["connected"], rec["last"] = now - t0, now
            if mask & selectors.EVENT_READ:
                try:
                    chunk = sock.recv(65536)
                except BlockingIOError:
                    chunk = None
                except OSError as exc:
                    finish(rec, type(exc).__name__)
                    continue
                if chunk == b"":
                    finish(rec, rec["buf"][9:12].decode() or "closed")
                    continue
                if chunk:
                    rec["buf"] += chunk
                    head, sep, body = rec["buf"].partition(b"\r\n\r\n")
                    if sep:
                        cl = [int(ln.split(b":", 1)[1]) for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length:")]
                        if not cl or len(body) >= cl[0]:
                            finish(rec, rec["buf"][9:12].decode())
                            continue
            if mask & selectors.EVENT_WRITE and rec["sent"] < len(data):
                try:
                    rec["sent"] += sock.send(data[rec["sent"]:rec["sent"] + 65536])
                    rec["last"], rec["last_send"] = now, now - t0
                except BlockingIOError:
                    pass
                except OSError:
                    rec["sent"] = len(data)        # the server stopped reading: read what it answered
                if rec["sent"] >= len(data):
                    sel.modify(sock, selectors.EVENT_READ, rec)
        for rec in recs:
            if rec["code"] is None and now - rec["last"] >= idle_timeout:
                finish(rec, "timeout")
        if sampling and now >= next_tick:
            next_tick += every
            sampling = tick(now - t0)
    sel.close()
    return recs


# Fix wave 26b (OPEN F-3, AEGIS r25 `invalid_sigstop.log`): the test below ran LIMIT_CONCURRENCY (128) senders, and
# uvicorn refuses a request head with 503 when, at the moment it parses it, `len(connections) >= limit_concurrency` --
# the connection being parsed counts itself. Normally the heads are parsed as the connections arrive, so every sender
# but the last finds fewer than 128 open and is admitted (the 128th gets uvicorn's 503 -- one sender of the 128 was
# always refused that way). When the server is descheduled across the connect phase, all 128 are accepted before
# any head is parsed: every one then finds 128 open and the WHOLE burst is 503 at once (AEGIS r25 on Linux, 128/128;
# the same SIGSTOP experiment on macOS refused 1 of 128 -- the kernel handed the connections over in parts), no body is read, memory never
# reaches the peak phase and the run is INVALID (it fails as such; it never passes). That is the product's behaviour
# (pinned by tests/test_fix26b_request_memory.py::test_live_a_burst_at_uvicorns_limit_accepted_while_the_server_
# cannot_run_is_refused_one_fewer_is_served; http_limits and ADR 0002 "Fix wave 26b"), not what this test measures.
# So the test runs one sender fewer than uvicorn's limit: however the server is scheduled, the open connections never
# reach it, every head reaches the app, and the in-flight budget and the large lane decide -- the scenario as
# specified (the same load: in a normal run the 128th sender was refused before its body).
_SENDERS = http_limits.LIMIT_CONCURRENCY - 1


def test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut(server):
    """Real bytes, then a stall: before, 128 x 4 MiB pinned for 30 s. Now at
    most _INFLIGHT_BODY_BYTES (64 MiB) is buffered (the rest 503), and the
    stalled bodies are cut by the throughput rule within the grace period, so
    RSS is back near baseline well before the 30 s deadline."""
    proc, port = server
    base = _held_mib(proc.pid)  # fix wave 26 (W26-2c): VmRSS on Linux (as before), phys_footprint on macOS
    rss_base = _rss_mib(proc.pid)
    hwm_ok = _hwm_reset(proc.pid)
    payload = b'{"call_events":[' + b" " * (3_900_000 - 16)
    # Fix wave 21 (lead ruling L1): each sender sends WHILE reading, stops
    # sending at the answer and reads it to Content-Length (a reset after a
    # complete answer is the server's documented behaviour: its drain after
    # the answer is bounded, 64 KiB / 1 s). The old client wrote the whole
    # 3.9 MB with a blocking sendall before reading anything; answered
    # early (uvicorn's limit_concurrency 503), it could still have MBs
    # unsent when the bounded drain ended, and was reset mid-send without
    # ever reading the 503 (w21 logs/L1-proof-probe.log: 20/20 reset at the
    # 64 KiB bound, 20/20 clean with an 8 MiB bound or a reading client).
    # Fix wave 25: all 128 of them on one thread (_senders_on_one_thread).
    # Fix wave 26b (OPEN F-3): 127 of them, _SENDERS -- see there.
    n = _SENDERS
    # admitted bodies are cut at the app's grace; the 503'd senders' unread
    # bytes go when the protocol's (grace + BODY_DEADLINE_GRACE_S) closes them
    bound = http_limits.BODY_MIN_RATE_GRACE_S + http_limits.BODY_DEADLINE_GRACE_S + 3
    peak = base
    samples: list[tuple[float, int]] = []
    rss_samples: list[tuple[float, int]] = []  # fix wave 26: printed where the held metric is not RSS (macOS)
    state = {"settled_at": None, "reached": False}
    # fix wave 25 (E-A): the server main thread's run-queue wait during the burst is PRINTED (diagnostics: a slow
    # server and a starved one read differently); the settle bound itself is fixed and is not extended by it.
    wait0 = _runqueue_wait_s(proc.pid)

    # Fix wave 21 (AEGIS N20-M-5): sampling runs until RSS settles or the bound
    # elapses, whatever the senders are doing (it used to stop as soon as every
    # sender had its answer). Fix wave 25, H2: "settled" only counts after the
    # peak phase (_settled_at). The 96 MiB growth bound below is unchanged (N20-M-4).
    metric = _held_metric()

    def tick(elapsed: float) -> bool:
        nonlocal peak
        r = _held_mib(proc.pid)
        peak = max(peak, r)
        samples.append((round(elapsed, 1), r))
        if metric != "VmRSS":
            rss_samples.append((round(elapsed, 1), _rss_mib(proc.pid)))
        state["settled_at"], state["reached"] = _settled_at(samples, base)
        return state["settled_at"] is None and elapsed < bound

    recs = _senders_on_one_thread(port, _head(4 * MIB) + payload, n, 20, 0.5, tick)
    settled_at, reached = state["settled_at"], state["reached"]
    wait1 = _runqueue_wait_s(proc.pid)
    rq = "n/a" if wait0 is None or wait1 is None else f"{wait1 - wait0:.2f} s"
    codes: dict[str, int] = {}
    for r in recs:
        codes[r["code"]] = codes.get(r["code"], 0) + 1
    hwm = _hwm_mib(proc.pid) if hwm_ok else None
    # fix wave 25: when the clients connected, last got bytes out and were answered — a starved client (late
    # connects / sends) and a slow server (late answers after the last byte) read differently here
    q = lambda v: f"{min(v):.1f}/{sorted(v)[len(v) // 2]:.1f}/{max(v):.1f}" if v else "-"
    cut = [r for r in recs if r["code"] == "408"]
    clients = (f"connected {q([r['connected'] for r in recs if r['connected'] is not None])} s; "
               f"408 last byte {q([r['last_send'] for r in cut if r['last_send'] is not None])} s, "
               f"answered {q([r['answered'] for r in cut])} s; "
               f"503 answered {q([r['answered'] for r in recs if r['code'] == '503'])} s (min/median/max)")
    # the whole line, flushed, before any assertion: a failing run (e.g. on a Mac runner) keeps its evidence
    line = (f"codes {codes}; memory metric {metric}; base {base} peak {peak} growth {peak - base} MiB; hwm_growth "
            f"{None if hwm is None else hwm - base} MiB; peak phase reached {reached}; settled_at {settled_at} "
            f"(bound {bound}); server main-thread run-queue wait {rq}; clients: {clients}; "
            f"samples {samples}"
            + (f"; ps RSS base {rss_base}, samples {rss_samples}" if rss_samples else ""))
    print(line, flush=True)
    if not reached:
        pytest.fail(f"INVALID run: memory never reached base + {_PEAK_FLOOR_MIB} MiB, so nothing was measured -- {line}")
    # the budget, plus uvicorn's own per-connection buffers (<= 64 KiB x 128) and the
    # 503'd bodies' drains — measured +84 MiB (base 54, peak 138). Fix wave 25, H2:
    # the kernel's peak RSS where it is available (the 0.5 s sampler can miss a peak).
    growth = (hwm if hwm is not None else peak) - base
    assert growth < api._INFLIGHT_BODY_BYTES // MIB + 32, f"RSS grew {growth} MiB -- {line}"
    assert codes.get("408", 0) + codes.get("503", 0) == n, line  # none held silently
    assert codes.get("408", 0) > 0, line  # the admitted, stalled ones were cut
    assert settled_at is not None and settled_at < bound, line


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
