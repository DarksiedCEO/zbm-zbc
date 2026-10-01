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
    assert 0.15 < refused_after < 0.6, refused_after
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

@pytest.fixture(scope="module")
def server():
    proc, port = _start_quiet()
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


def test_live_128_senders_of_3_9mb_that_then_stall_are_bounded_by_the_inflight_budget_and_cut(server):
    """Real bytes, then a stall: before, 128 x 4 MiB pinned for 30 s. Now at
    most _INFLIGHT_BODY_BYTES (64 MiB) is buffered (the rest 503), and the
    stalled bodies are cut by the throughput rule within the grace period, so
    RSS is back near baseline well before the 30 s deadline."""
    import threading
    proc, port = server
    base = _rss_mib(proc.pid)
    hwm_ok = _hwm_reset(proc.pid)
    payload = b'{"call_events":[' + b" " * (3_900_000 - 16)
    socks, codes, lock = [], {}, threading.Lock()

    def sender():
        # Fix wave 21 (lead ruling L1): the client sends WHILE reading, stops
        # sending at the answer and reads it to Content-Length (a reset after a
        # complete answer is the server's documented behaviour: its drain after
        # the answer is bounded, 64 KiB / 1 s). The old client wrote the whole
        # 3.9 MB with a blocking sendall before reading anything; answered
        # early (uvicorn's limit_concurrency 503), it could still have MBs
        # unsent when the bounded drain ended, and was reset mid-send without
        # ever reading the 503 (w21 logs/L1-proof-probe.log: 20/20 reset at the
        # 64 KiB bound, 20/20 clean with an 8 MiB bound or a reading client).
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=30)
        except OSError as exc:
            got = "connect:" + type(exc).__name__
        else:
            with lock:
                socks.append(s)
            got = _send_reading(s, _head(4 * MIB) + payload, 20)
        with lock:
            codes[got] = codes.get(got, 0) + 1

    threads = [threading.Thread(target=sender) for _ in range(http_limits.LIMIT_CONCURRENCY)]
    # admitted bodies are cut at the app's grace; the 503'd senders' unread
    # bytes go when the protocol's (grace + BODY_DEADLINE_GRACE_S) closes them
    bound = http_limits.BODY_MIN_RATE_GRACE_S + http_limits.BODY_DEADLINE_GRACE_S + 3
    t0 = time.monotonic()
    for t in threads:
        t.start()
    peak = base
    samples = []
    settled_at = None
    # Fix wave 21 (AEGIS N20-M-5): sampling runs until RSS settles or the bound
    # elapses, whatever the sender threads are doing. It used to stop as soon
    # as every sender had its answer, which under load can be before the
    # server has released the bytes (settled_at None: a test defect, not a
    # server one). The 96 MiB growth bound below is unchanged (N20-M-4).
    reached = False
    while time.monotonic() - t0 < bound:
        time.sleep(0.5)
        r = _rss_mib(proc.pid)
        at = round(time.monotonic() - t0, 1)
        peak = max(peak, r)
        samples.append((at, r))
        settled_at, reached = _settled_at(samples, base)      # fix wave 25, H2: only after the peak phase
        if settled_at is not None:
            break
    for t in threads:
        t.join(timeout=20)
    for s in socks:
        s.close()
    hwm = _hwm_mib(proc.pid) if hwm_ok else None
    # the whole line, flushed, before any assertion: a failing run (e.g. on a Mac runner) keeps its evidence
    line = (f"codes {codes}; RSS base {base} peak {peak} growth {peak - base} MiB; hwm_growth "
            f"{None if hwm is None else hwm - base} MiB; peak phase reached {reached}; settled_at {settled_at} "
            f"(bound {bound}); samples {samples}")
    print(line, flush=True)
    if not reached:
        pytest.fail(f"INVALID run: memory never reached base + {_PEAK_FLOOR_MIB} MiB, so nothing was measured -- {line}")
    # the budget, plus uvicorn's own per-connection buffers (<= 64 KiB x 128) and the
    # 503'd bodies' drains — measured +84 MiB (base 54, peak 138). Fix wave 25, H2:
    # the kernel's peak RSS where it is available (the 0.5 s sampler can miss a peak).
    growth = (hwm if hwm is not None else peak) - base
    assert growth < api._INFLIGHT_BODY_BYTES // MIB + 32, f"RSS grew {growth} MiB -- {line}"
    assert codes.get("408", 0) + codes.get("503", 0) == len(threads), line  # none held silently
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
