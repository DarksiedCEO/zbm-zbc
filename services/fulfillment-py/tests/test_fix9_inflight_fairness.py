"""
Fix wave 9 — two questions AEGIS round 8 could not finish about the wave-8
body handling (in-flight byte budget 64 MiB; minimum throughput 1 KiB/s after
a 5 s grace; 503 + Retry-After when the budget is exhausted).

Q1. Can an authenticated client exhaust the in-flight budget and starve
    legitimate clients while staying above the 1 KiB/s floor?
    Measured on the real launcher before this fix (scratch harness, 25-30 s
    runs; legit small `detect` sequential every 0.1 s, legit large batch every
    1 s):
      - N senders at 2 KiB/s, Content-Length 4 MiB: N=16 and N=64 did NOT
        starve anyone (small p50 8 ms, p99 65-68 ms; large 18/18 200): at
        2 KiB/s a body holds <= 60 KiB by the 30 s deadline, 64 x 60 KiB is
        far below 64 MiB. N=128 starved everyone — but through uvicorn's
        connection limit (LIMIT_CONCURRENCY 128: 183/184 legit small 503
        from uvicorn before any app code runs), not the byte budget; see the
        README for that residual limit.
      - Front-loaded senders (declare 4 MiB, send 1 MiB at once, then
        2 KiB/s — above the floor, and the throughput rule credits the
        front-load for ~1000 s): N=64 held the budget for the whole 30 s.
        Legit small `detect` p99 1 989 ms (waiting on the shared budget,
        the 2 s refusal edge; 77 requests in 24 s instead of ~220), legit
        large batches 3/7 200. 3.9 MB front-loads with N=64: large 1/6.
    So: yes. Fixed three ways (api.py), each tested here:
      (1) small bodies never draw on the shared budget — the first
          _SMALL_BODY_BYTES of every body come from a reserve sized
          LIMIT_CONCURRENCY x _SMALL_BODY_BYTES, which the real launcher's
          concurrency limit makes impossible to exhaust;
      (2) every body is charged bytes x seconds for the shared bytes it holds
          while the service waits on the client; when the shared budget is
          contended, the in-flight body with the largest charge, if it is at
          least _PREEMPT_BYTE_SECONDS (4 MiB·s: a max body arriving at
          >= 2 MiB/s never reaches it), is cut with 408 so the newcomer is
          not refused;
      (3) a body whose declared size cannot arrive by the deadline at its
          observed rate is refused with 408 as soon as that is known (from
          the 5 s grace on), with a message telling the client to split.
      Plus: bytes a large body declared but never sent are refunded to the
      large-lane byte-rate budget (otherwise (3) — cutting slow senders early
      — would let them drain that budget faster and 503 legit batches).

Q2. Do legitimate slow mobile uploads of a max body succeed? No: a 4 MiB body
    at 64 KiB/s needs 64 s; the deadline is 30 s. Decision: the 30 s deadline
    stays (a longer one is more time for every slow sender to hold memory);
    a body must arrive at >= size / 30 s — 136.5 KiB/s for a 4 MiB body —
    and one that cannot is told so at ~5 s (408, "split the batch into
    requests of at most N bytes") instead of being cut at 30 s. Tested at
    the boundary on the real launcher.

Ports: FULFILLMENT_TEST_PORT_RANGE when set (this wave: 20920-20939).
"""

from __future__ import annotations

import asyncio
import json
import socket
import statistics
import threading
import time

import pytest

from test_fix4_limits import MAX_BATCHES
from test_fix5_http_limits_live import _start as _start_quiet, _stop
from test_fix8_n7_2_body_prealloc import DETECT, ORCHESTRATE, _Client, _event, _until
from test_live_server import TOKEN

import api
import http_limits

KIB = 1024
MIB = 1024 * 1024


def _large_body(size: int) -> bytes:
    """Valid `detect` JSON of exactly `size` bytes (padding is whitespace)."""
    head = b'{"call_events":[]'
    return head + b" " * (size - len(head) - 1) + b"}"


# --- (1) small bodies never wait for the shared budget --------------------------

def test_small_body_is_not_blocked_by_an_exhausted_shared_budget(monkeypatch):
    """Before: every chunk of every body reserved from the one shared budget,
    so with it held by large senders a 200-byte `detect` waited
    _INFLIGHT_WAIT_S and got 503 (live: p99 1.99 s, the refusal edge)."""
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 0.5)

    async def scenario():
        inflight = api._lanes().inflight
        await inflight.reserve(inflight.limit)  # large bodies hold every shared byte
        body = _event()
        c = _Client(content_length=len(body))
        t0 = time.perf_counter()
        task = asyncio.ensure_future(c.run())
        await c.feed(body, more=False)
        await task
        took = time.perf_counter() - t0
        inflight.release(inflight.limit)
        return c, took

    c, took = asyncio.run(scenario())
    assert c.status == 200, (c.status, c.body[:200])
    assert took < 0.3, f"small body waited {took:.2f}s for the shared budget"


def test_first_small_body_bytes_of_a_large_body_come_from_the_reserve(monkeypatch):
    """A large body draws on the shared budget only past _SMALL_BODY_BYTES."""
    async def scenario():
        lanes = api._lanes()
        body = _large_body(200 * KIB)
        c = _Client(content_length=len(body))
        task = asyncio.ensure_future(c.run())
        await c.feed(body[:150 * KIB])
        await _until(lambda: lanes.inflight.used >= 150 * KIB - api._SMALL_BODY_BYTES)   # fix wave 25: not sleep(0.05)
        seen = (lanes.small_reserve.used, lanes.inflight.used)
        await c.feed(body[150 * KIB:], more=False)
        await task
        return c, seen, (lanes.small_reserve.used, lanes.inflight.used)

    c, seen, after = asyncio.run(scenario())
    assert c.status == 200, c.body[:200]
    # Fix wave 25, H1: the shared pool also holds the read-ahead grant the body
    # reserved before asking for more (<= _READ_GRANT_BYTES, never past the
    # declared length) — reserved from the shared pool too, never the reserve.
    assert seen[0] == api._SMALL_BODY_BYTES, seen
    assert 150 * KIB - api._SMALL_BODY_BYTES <= seen[1] <= min(150 * KIB + api._READ_GRANT_BYTES, 200 * KIB) - api._SMALL_BODY_BYTES, seen
    assert after == (0, 0), after


def test_small_reserve_is_sized_so_the_real_launcher_cannot_exhaust_it():
    assert api._SMALL_RESERVE_BYTES == http_limits.LIMIT_CONCURRENCY * api._SMALL_BODY_BYTES


# --- (2) time-weighted charge: slow holders are preempted under contention -----

def _contended(monkeypatch, preempt_after_s: float):
    shared = 192 * KIB
    monkeypatch.setattr(api, "_INFLIGHT_BODY_BYTES", api._SMALL_RESERVE_BYTES + shared)  # fix wave 24: the total includes the small reserve; `shared` is the shared pool
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 1.0)
    monkeypatch.setattr(api, "_PREEMPT_BYTE_SECONDS", shared * preempt_after_s)
    return shared


def test_slow_holder_of_the_shared_budget_is_preempted_for_a_newcomer(monkeypatch):
    """A slow sender that front-loaded enough to fill the shared budget and
    then trickles (above the floor) used to keep it for up to 30 s; a fast
    newcomer got 503. Now the holder's bytes x seconds pass the preemption
    threshold and, the moment someone is waiting, it is cut with 408."""
    shared = _contended(monkeypatch, preempt_after_s=0.2)

    async def scenario():
        holder = _Client(content_length=4 * MIB)
        h_task = asyncio.ensure_future(holder.run())
        await holder.feed(b'{"call_events":[' + b" " * (api._SMALL_BODY_BYTES + shared - 16))
        await asyncio.sleep(0.4)  # holds all of it: 192 KiB x 0.4 s > threshold
        body = _large_body(200 * KIB)
        newcomer = _Client(content_length=len(body))
        t0 = time.perf_counter()
        n_task = asyncio.ensure_future(newcomer.run())
        await newcomer.feed(body, more=False)
        await asyncio.wait_for(n_task, 5)
        took = time.perf_counter() - t0
        await asyncio.wait_for(h_task, 5)
        return holder, newcomer, took

    holder, newcomer, took = asyncio.run(scenario())
    assert newcomer.status == 200, (newcomer.status, newcomer.body[:200])
    assert took < 0.5, took
    assert holder.status == 408, (holder.status, holder.body[:200])
    assert "preempted" in holder.body.decode()


def test_fast_holder_below_the_threshold_is_not_preempted(monkeypatch):
    """The guarantee's other side: a body that has not reached the
    threshold keeps its bytes; the newcomer waits (and is 503'd if the wait
    runs out), exactly as before."""
    shared = _contended(monkeypatch, preempt_after_s=10.0)

    async def scenario():
        holder = _Client(content_length=api._SMALL_BODY_BYTES + shared)
        h_task = asyncio.ensure_future(holder.run())
        payload = _large_body(api._SMALL_BODY_BYTES + shared)
        await holder.feed(payload[:-1])
        await asyncio.sleep(0.1)
        body = _large_body(200 * KIB)
        newcomer = _Client(content_length=len(body))
        n_task = asyncio.ensure_future(newcomer.run())
        await newcomer.feed(body, more=False)
        await asyncio.sleep(0.3)
        await holder.feed(payload[-1:], more=False)
        await asyncio.wait_for(h_task, 5)
        await asyncio.wait_for(n_task, 5)
        return holder, newcomer

    holder, newcomer = asyncio.run(scenario())
    assert holder.status == 200, (holder.status, holder.body[:200])
    assert newcomer.status == 200, (newcomer.status, newcomer.body[:200])  # admitted once the holder finished


def test_time_the_service_makes_a_body_wait_is_not_charged(monkeypatch):
    """Only time spent waiting on the CLIENT accrues bytes x seconds: a body
    stalled by the service's own budget wait must not become preemptible."""
    shared = _contended(monkeypatch, preempt_after_s=0.2)

    async def scenario():
        lanes = api._lanes()
        body = _large_body(api._SMALL_BODY_BYTES + shared // 2 + 16 * KIB)
        a = _Client(content_length=len(body))
        a_task = asyncio.ensure_future(a.run())
        await lanes.inflight.reserve(shared // 2)  # someone else holds half
        await a.feed(body, more=False)  # all sent: a now waits on the SERVICE for 16 KiB
        await asyncio.sleep(0.6)  # a holds 96 KiB x 0.6 s of service wait
        accounts = [acc for acc in lanes.inflight.accounts if acc is not None]
        charged = [acc.charge(asyncio.get_running_loop().time()) for acc in accounts]
        lanes.inflight.release(shared // 2)
        await asyncio.wait_for(a_task, 5)
        return a, charged

    a, charged = asyncio.run(scenario())
    assert a.status == 200, (a.status, a.body[:200])
    assert charged and max(charged) < 0.2 * 192 * KIB * 0.5, charged


# --- (3) a body that cannot arrive by the deadline at its rate is told so early ----

def _paced(c: _Client, body: bytes, rate: float, step: float = 0.02):
    async def go():
        n = max(1, int(rate * step))
        t0 = asyncio.get_running_loop().time()
        for i in range(0, len(body), n):
            await c.feed(body[i:i + n], more=i + n < len(body))
            delay = t0 + (i + n) / rate - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
    return go()


@pytest.mark.parametrize("factor, expect", [(1.3, 200), (0.75, 408)])
def test_projected_arrival_after_the_deadline_is_refused_at_the_grace(monkeypatch, factor, expect):
    """Scaled boundary (deadline 3 s, grace 0.5 s): a 300 kB body needs
    100 kB/s. At 1.3x it is accepted; at 0.75x it is refused at the grace
    (~0.5 s, not at the 3 s deadline) with a message saying how to split."""
    monkeypatch.setattr(api, "_BODY_READ_TIMEOUT_S", 3.0)
    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 0.5)
    body = _large_body(300_000)
    need = len(body) / 3.0

    async def scenario():
        c = _Client(content_length=len(body))
        task = asyncio.ensure_future(c.run())
        t0 = time.perf_counter()
        sender = asyncio.ensure_future(_paced(c, body, need * factor))
        await asyncio.wait_for(task, 6)
        took = time.perf_counter() - t0
        sender.cancel()
        return c, took

    c, took = asyncio.run(scenario())
    assert c.status == expect, (c.status, c.body[:300], took)
    if expect == 408:
        detail = json.loads(c.body)["detail"]
        assert "split the batch" in detail and "bytes/s" in detail, detail
        assert took < 1.2, f"refused after {took:.2f}s: should be at the 0.5 s grace, not the deadline"


def test_projection_needs_a_declared_length():
    """Chunked bodies have no size to project; they are bounded by the
    throughput floor, the deadline and preemption (2)."""
    src = open(api.__file__).read()
    assert "_BodyWontArrive" in src


# --- new defect found here: the large-lane admission window ran from arrival ---------

@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_large_body_that_takes_longer_than_the_admission_wait_to_upload_is_accepted(declared):
    """Found while assessing Q2: `_off_loop` measured the large lane's 2 s
    admission window (_LARGE_WAIT_S) from the request's ARRIVAL and applied
    it to the parse-slot wait after the body was complete (and, for a
    chunked body, to every pay-as-it-streams take). So every large body that
    took more than 2 s to upload was refused 503 once it had arrived whole —
    live, a 4 MiB body at 160 KiB/s: 503 at 26 s; in-process here, 503 at
    ~2.3 s. The window now runs from when each wait starts."""
    body = _large_body(600_000)

    async def scenario():
        c = _Client(content_length=len(body) if declared else None)
        task = asyncio.ensure_future(c.run())
        sender = asyncio.ensure_future(_paced(c, body, len(body) / 2.6))  # 2.6 s, ~225 kB/s
        await asyncio.wait_for(task, 10)
        sender.cancel()
        return c

    c = asyncio.run(scenario())
    assert c.status == 200, (c.status, c.body[:300])


# --- the large-lane byte-rate budget is refunded for bytes never sent ---------------

def test_declared_but_unsent_bytes_are_refunded_to_the_large_lane_budget(monkeypatch):
    monkeypatch.setattr(api, "_LARGE_BYTES_PER_S", 1)  # no refill during the test
    monkeypatch.setattr(api, "_BODY_READ_TIMEOUT_S", 0.5)

    async def scenario():
        lanes = api._lanes()
        c = _Client(content_length=4 * MIB)
        task = asyncio.ensure_future(c.run())
        await c.feed(b'{"call_events":[' + b" " * 1000)
        await asyncio.wait_for(task, 5)  # cut at the deadline having sent ~1 KB
        return c, lanes.budget.tokens

    c, tokens = asyncio.run(scenario())
    assert c.status == 408, c.body[:200]
    assert tokens >= api._LARGE_BURST_BYTES - 2 * KIB, f"{(api._LARGE_BURST_BYTES - tokens) / MIB:.2f} MiB never refunded"


# --- the real launcher --------------------------------------------------------------

@pytest.fixture(scope="module")
def server():
    proc, port = _start_quiet()
    yield proc, port
    _stop(proc)


def _head(path: str, content_length: int) -> bytes:
    return (f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {content_length}\r\n\r\n").encode()


def _status_of(s: socket.socket) -> tuple[int | None, bytes]:
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        while len(rest) < length:
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
    except OSError:
        pass
    return (int(data[9:12]) if data.startswith(b"HTTP/1.1 ") else None), data


def _paced_upload(port: int, path: str, body: bytes, rate: float) -> tuple[int | None, float, bytes]:
    """Send `body` at `rate` bytes/s (10 ms steps); stop as soon as the
    service answers. Returns (status, seconds, raw response)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=45)
    t0 = time.monotonic()
    try:
        s.sendall(_head(path, len(body)))
        step = max(1, int(rate * 0.01))
        sent = 0
        s.setblocking(False)
        while sent < len(body):
            try:
                s.recv(1, socket.MSG_PEEK)
                break  # answered early (or closed)
            except BlockingIOError:
                pass
            except OSError:
                break
            try:
                sent += s.send(body[sent:sent + step])
            except BlockingIOError:
                pass
            except OSError:
                break
            delay = t0 + sent / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        s.setblocking(True)
        s.settimeout(45)
        code, raw = _status_of(s)
        return code, time.monotonic() - t0, raw
    finally:
        s.close()


def _max_legal_body() -> bytes:
    """A real `detect` batch as close to the 4 MiB limit as it gets: 1000
    worst-case events with voicemail transcripts filling the rest."""
    payload = json.loads(json.dumps(MAX_BATCHES[DETECT]))
    base = len(json.dumps(payload).encode())
    per = (api._MAX_BODY_BYTES - base - 8192) // 1000 + 2  # replaces `null` with `"v..."`: + per - 2 bytes each
    for ev in payload["call_events"]:
        ev["voicemail_transcript"] = "v" * per
    body = json.dumps(payload).encode()
    assert api._MAX_BODY_BYTES - 16 * KIB < len(body) <= api._MAX_BODY_BYTES, len(body)
    return body


def test_live_q2_max_body_boundary_rate(server):
    """A max legal body (~4 MiB) must arrive within the 30 s deadline: it
    needs size / 30 s (~136 KiB/s). Both sides at once on the real launcher:
    at 1.15x that rate it is accepted; at 64 KiB/s (the poor-mobile case,
    which would need 64 s) and at 0.85x it is refused at ~5 s (the grace),
    not at 30 s, with a 408 telling the client how far to split."""
    _, port = server
    body = _max_legal_body()
    need = len(body) / http_limits.BODY_READ_TIMEOUT_S
    results = {}

    def run(name, rate):
        results[name] = _paced_upload(port, DETECT, body, rate)

    threads = [threading.Thread(target=run, args=a) for a in
               (("pass_1.15x", need * 1.15), ("refuse_0.85x", need * 0.85), ("mobile_64KiB/s", 64 * KIB))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    summary = {k: (v[0], round(v[1], 1)) for k, v in results.items()}
    print(f"body {len(body)} bytes, needs {need / KIB:.1f} KiB/s: {summary}")
    code, took, raw = results["pass_1.15x"]
    assert code == 200, (summary, raw[:300])
    assert took < http_limits.BODY_READ_TIMEOUT_S, summary
    for name in ("refuse_0.85x", "mobile_64KiB/s"):
        code, took, raw = results[name]
        assert code == 408, (name, summary, raw[:300])
        assert b"split the batch" in raw, raw[:400]
        assert took < http_limits.BODY_MIN_RATE_GRACE_S + 2.0, (name, summary)


def _legit_probe(port: int, stop: float, small: list, large: list) -> None:
    small_body = _event()
    large_body = json.dumps(MAX_BATCHES[ORCHESTRATE]).encode()  # 3.57 MB
    next_large = time.monotonic()
    while time.monotonic() < stop:
        t0 = time.monotonic()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                s.sendall(_head(DETECT, len(small_body)) + small_body)
                code, _ = _status_of(s)
        except OSError as exc:
            code = type(exc).__name__
        small.append((code, time.monotonic() - t0))
        if time.monotonic() >= next_large:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=20) as s:
                    s.sendall(_head(ORCHESTRATE, len(large_body)) + large_body)
                    code, _ = _status_of(s)
            except OSError as exc:
                code = type(exc).__name__
            large.append(code)
            next_large = time.monotonic() + 1.0
        time.sleep(0.1)


def _slow_senders(port: int, n: int, front: int, rate: int, stop: float, counts: dict) -> list[threading.Thread]:
    """n authenticated senders, each: declare 4 MiB, send `front` bytes at
    once, then `rate` bytes/s (above the 1 KiB/s floor) until answered;
    reconnect and repeat until `stop`."""
    lock = threading.Lock()

    def one():
        while time.monotonic() < stop:
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
            except OSError:
                time.sleep(0.2)
                continue
            outcome = "held"
            try:
                s.sendall(_head(DETECT, 4 * MIB) + b'{"call_events":[' + b" " * max(0, front - 16))
                s.setblocking(False)
                while time.monotonic() < stop:
                    try:
                        data = s.recv(64)
                        outcome = data[9:12].decode() if data.startswith(b"HTTP/1.1 ") else "closed"
                        break
                    except BlockingIOError:
                        pass
                    try:
                        s.send(b" " * (rate // 4))
                    except BlockingIOError:
                        pass
                    time.sleep(0.25)
            except OSError as exc:
                outcome = type(exc).__name__
            finally:
                s.close()
            with lock:
                counts[outcome] = counts.get(outcome, 0) + 1

    threads = [threading.Thread(target=one, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    return threads


@pytest.mark.parametrize("n, front", [(64, 2 * KIB), (64, 1 * MIB)], ids=["2KiBps", "1MiB-front-then-2KiBps"])
def test_live_q1_slow_senders_above_the_floor_do_not_starve_legit_clients(server, n, front):
    """N=64 senders at 2 KiB/s (above the floor, never cut by it), plain and
    front-loaded. Before the fix the front-loaded run gave legit small
    `detect` p99 ~2 s (the 2 s refusal edge) and legit large batches 3/7.
    Now: small requests never touch the shared budget; slow holders are
    preempted (or refused by the arrival projection) when a legit large
    batch needs the bytes."""
    _, port = server
    counts: dict = {}
    t0 = time.monotonic()
    stop = t0 + 18.0
    senders = _slow_senders(port, n, front, 2 * KIB, stop, counts)
    time.sleep(6.0)  # past the 5 s grace: the budget is held as long as it ever will be
    small, large = [], []
    _legit_probe(port, stop, small, large)
    for t in senders:
        t.join(10)
    ok = sorted(dt for code, dt in small if code == 200)
    p50 = statistics.median(ok) * 1000 if ok else None
    p99 = ok[int(0.99 * (len(ok) - 1))] * 1000 if ok else None
    print(f"n={n} front={front}: small {len(ok)}/{len(small)} 200, p50 {p50:.1f} ms p99 {p99:.1f} ms; "
          f"large {large}; senders {counts}")
    assert len(ok) == len(small), [c for c, _ in small if c != 200][:10]
    assert p99 < 500, p99
    assert large and large.count(200) >= len(large) - 1, large
