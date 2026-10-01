"""
Fix wave 25 — AEGIS round 24, fulfillment (H1, H4).

H1 (N24-S-1, N24-S-2): wave 24 made the request-body memory bound structural
(every body byte inside the one 64 MiB budget, N24-S-3 PASS) by pausing the
protocol as soon as it buffered ANY body byte the app had not asked for. The
cost: every 16 KiB read waited for a round trip through the app (wake the app,
take the chunk, cover it, ask again, resume reading, the next loop pass), so
under CPU contention a body arrived more slowly, stalled bodies were cut
later and the 128-sender test missed its settle bound (7/60). Now the app
reserves up to `_READ_GRANT_BYTES` (64 KiB, uvicorn's own high-water mark)
AHEAD of what it has taken, from free budget only and never while another body
waits for the budget (`_BodyHold.grant`, `_InFlightBytes.try_reserve`), and
the protocol keeps reading while the bytes it buffers plus the bytes the app
has taken are below the bytes covered. Covered bytes stream; at most one read
past them is ever buffered (the wave-24 bound is unchanged).

H3 (N24-S-4): the derived bound left out the parsed model. A valid 4 MiB
CallEventsRequest whose transcripts each end in one astral character parses to
a ~21 MB model (CPython stores the whole string at 4 bytes a character, plus a
cached UTF-8 copy), and it was held — through the agent's work — after the
body's budget bytes had gone back; models of consecutive requests coexisted
uncounted. Now each model's measured size (`api._retained_bytes`) is counted
in the budget from the end of its parse until it is dropped, the wait for it
inside the parse slot.

H4 (N24-S-12): the rules that judge a client by time counted wall time spent
waiting for its bytes, including time this process could not run at all. A
client sending steadily while the server was stopped for 6 s was answered
408 "stalled" (3/3), for 3 s 408 "cannot complete" (2/3). They now run on the
time the event loop was able to run (`http_limits.LoopLag`); the hard
deadlines stay wall-clock.

Ports: FULFILLMENT_TEST_PORT_RANGE when set.
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import socket
import sys
import time

import h11
import pytest
import uvicorn
from uvicorn.server import ServerState

from conftest import TEST_SERVICE_TOKEN
from test_fix24_body_memory_accounted import _Transport
from test_fix5_http_limits_live import _health, _start, _stop
from test_fix8_n7_2_body_prealloc import DETECT, _Client
from test_live_server import TOKEN

import api
import http_limits
from graceful_close import READ_BUFFER_BYTES

KIB = 1024
MIB = 1024 * KIB


# --- H1: read-ahead grants -----------------------------------------------------------------------------

class _Hold:
    """The two numbers the protocol reads from api._BodyHold."""

    def __init__(self, covered: int, taken: int = 0) -> None:
        self.covered, self.taken = covered, taken


def _protocol(app):
    config = uvicorn.Config(app=app, http=http_limits.DeadlineH11Protocol, lifespan="off",
                            h11_max_incomplete_event_size=http_limits.MAX_HEADER_BYTES)
    config.load()
    proto = http_limits.DeadlineH11Protocol(config=config, server_state=ServerState(), app_state={})
    transport = _Transport()
    proto.connection_made(transport)
    return proto, transport


def _head(n: int) -> bytes:
    return (f"POST {DETECT} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TEST_SERVICE_TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {n}\r\n\r\n").encode()


def _pump(proto, transport, stream: bytes, sent: int) -> int:
    """Hand the protocol reads of at most READ_BUFFER_BYTES while it is reading; returns the bytes handed."""
    reader = transport.protocol
    while sent < len(stream) and not transport.paused and not transport.closed:
        buf = reader.get_buffer(-1)
        n = min(len(buf), len(stream) - sent)
        buf[:n] = stream[sent:sent + n]
        reader.buffer_updated(n)
        sent += n
    return sent


@pytest.mark.parametrize("covered", [40 * KIB, 64 * KIB])
def test_the_protocol_reads_covered_body_bytes_and_at_most_one_read_past_them(covered):
    """An app that has covered `covered` body bytes (at most a grant, 64 KiB) and taken none: the protocol reads
    them without the app asking (wave 24 paused after the first read whatever was covered) and stops within one
    read past them. (Nothing covered: one read, wave 24's
    test_uvicorn_buffers_at_most_one_read_of_a_body_the_app_has_not_asked_for. More than 64 KiB covered: uvicorn's
    own high-water mark stops it first.)"""
    started = asyncio.Event()

    async def app(scope, receive, send):
        scope[http_limits.BODY_HOLD_SCOPE_KEY] = _Hold(covered)
        started.set()
        await asyncio.sleep(3600)

    async def scenario():
        proto, transport = _protocol(app)
        body = 1 * MIB
        stream = _head(body) + b" " * body
        sent = _pump(proto, transport, stream, 0)       # the head (and the first read): the app is not running yet
        await asyncio.wait_for(started.wait(), 5)
        proto.flow.resume_reading()                      # as the app's first receive() does
        sent = _pump(proto, transport, stream, sent)
        buffered, paused = len(proto.cycle.body), transport.paused
        for task in list(proto.tasks):
            task.cancel()
        await asyncio.gather(*proto.tasks, return_exceptions=True)
        return buffered, paused

    buffered, paused = asyncio.run(scenario())
    assert paused, "reading was never paused"
    assert buffered >= covered, f"stopped at {buffered} buffered bytes with {covered} covered"
    assert buffered < covered + READ_BUFFER_BYTES, f"{buffered} buffered: more than one read past {covered}"


def _app_round_trips(body_len: int, shared_free: bool) -> tuple[int, int, int]:
    """The real app (api.app) behind the real protocol; a client that sends as fast as it is read. Returns (times
    the protocol was resumed by an app that found it paused, the most body bytes ever in this process past the
    covered ones — taken by the app or buffered by the protocol —, the status). `shared_free=False`: every shared
    byte is held by someone else for 0.3 s first. The derived bound (ADR 0002, wave 24, unchanged): past the
    covered bytes, at most one read in the app's hand while it waits to cover it, and one read in the protocol's
    buffer (uvicorn's receive() resumes reading whenever the app asks) — under 2 x READ_BUFFER_BYTES."""

    async def scenario():
        lanes = api._lanes()
        if not shared_free:
            await lanes.inflight.reserve(lanes.inflight.limit)
            asyncio.get_running_loop().call_later(0.3, lanes.inflight.release, lanes.inflight.limit)
        proto, transport = _protocol(api.app)
        resumes = 0
        orig_resume = transport.resume_reading

        def resume():
            nonlocal resumes
            resumes += transport.paused
            orig_resume()

        transport.resume_reading = resume
        payload = b'{"call_events":[' + b" " * (body_len - 18) + b"]}"
        stream = _head(len(payload)) + payload
        sent, worst = 0, 0
        t_end = time.monotonic() + 30
        while time.monotonic() < t_end and not transport.closed and not transport.out:
            sent = _pump(proto, transport, stream, sent)
            cyc = proto.cycle
            hold = cyc.scope.get(api._HOLD_SCOPE_KEY) if cyc is not None else None
            if hold is not None and proto.conn.their_state is h11.SEND_BODY:    # while the body is being read
                taken = getattr(hold, "taken", None)                             # (wave 24's hold has no count)
                if taken is not None:
                    worst = max(worst, taken + len(cyc.body) - hold.covered)
            await asyncio.sleep(0)
        await asyncio.gather(*proto.tasks, return_exceptions=True)
        status = int(bytes(transport.out)[9:12] or 0)
        return resumes, worst, status

    return asyncio.run(scenario())


def test_a_body_streams_into_covered_budget_without_a_round_trip_per_read():
    """N24-S-2's mechanism: 4 MiB through the real protocol and app. Wave 24: the app had to wake and ask again for
    every 16 KiB read (~256 resumes of a paused socket). Now ~one per 64 KiB grant, and never more than one read
    beyond the covered bytes is buffered."""
    resumes, worst, status = _app_round_trips(4 * MIB, shared_free=True)
    assert status in (200, 422), status
    assert resumes <= 4 * MIB // (64 * KIB) + 8, f"{resumes} round trips for 4 MiB"   # one per 64 KiB grant
    assert worst < 2 * READ_BUFFER_BYTES, f"{worst} bytes past the covered ones"


def test_with_the_budget_held_by_others_reading_stops_at_the_covered_bytes_and_resumes_when_freed():
    resumes, worst, status = _app_round_trips(1 * MIB, shared_free=False)
    assert status in (200, 422), status
    assert worst < 2 * READ_BUFFER_BYTES, f"{worst} bytes past the covered ones"


def test_grants_come_only_from_free_budget_and_never_ahead_of_a_waiting_body(monkeypatch):
    assert api._READ_GRANT_BYTES == 64 * KIB                    # uvicorn's own high-water mark
    assert http_limits.BODY_HOLD_SCOPE_KEY == api._HOLD_SCOPE_KEY
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 1.0)

    async def scenario():
        lanes = api._lanes()
        pool = lanes.inflight
        a = api._BodyAccount(asyncio.get_running_loop())
        assert pool.try_reserve(100, a) == 100 and a.held == 100
        assert pool.try_reserve(pool.limit, a) == pool.limit - 100          # only what is free
        assert pool.try_reserve(1, a) == 0
        pool.release(pool.limit, a)
        await pool.reserve(pool.limit - 10)
        waiter = asyncio.ensure_future(pool.reserve(50))                  # a body waiting for bytes
        await asyncio.sleep(0.01)
        pool.release(20)                                                  # 30 free, the waiter still short
        got = pool.try_reserve(30)                                        # a grant may not take them
        pool.release(pool.limit - 30)
        await waiter
        used = pool.used
        pool.release(50)
        return got, used, pool.used

    got, used, after = asyncio.run(scenario())
    assert got == 0, "a read-ahead grant took bytes a waiting body needed"
    assert used == 50 and after == 0, (used, after)


def test_a_grant_past_the_end_of_a_chunked_body_is_given_back_when_it_completes():
    async def scenario():
        lanes = api._lanes()
        c = _Client(content_length=None)
        task = asyncio.ensure_future(c.run())
        await c.feed(b'{"call_events":[' + b" " * (100 * KIB))
        await asyncio.sleep(0.05)
        held_mid = (lanes.small_reserve.used, lanes.inflight.used)
        await c.feed(b"]}", more=False)
        await asyncio.wait_for(task, 10)
        return held_mid, (lanes.small_reserve.used, lanes.inflight.used), c.status

    (small, shared), after, status = asyncio.run(scenario())
    assert status == 200
    received = 16 + 100 * KIB
    assert small == api._SMALL_BODY_BYTES
    assert received - api._SMALL_BODY_BYTES <= shared <= received + api._READ_GRANT_BYTES - api._SMALL_BODY_BYTES
    assert after == (0, 0), after


# --- H3: the parsed model is counted until it is dropped --------------------------------------------

def _astral_detect_body(events: int = 1000, limit: int = 4 * MIB) -> bytes:
    """The largest valid CallEventsRequest of `events` events under `limit` whose transcripts each end in one
    astral character (the worst expansion found: ~5x)."""
    import json
    def build(tlen):
        ev = {"call_id": "c{}", "phone_number": "+14155550100", "direction": "inbound", "status": "voicemail",
              "started_at": "2026-09-01T10:00:00Z", "voicemail_transcript": "a" * (tlen - 1) + "\U0001F600",
              "line_id": "l1"}
        one = json.dumps(ev, ensure_ascii=False)
        return ('{"call_events":[' + ",".join(one.replace('"c{}"', f'"c{i}"') for i in range(events)) + "]}").encode()
    lo, hi = 1, 10_000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if len(build(mid)) <= limit else (lo, mid - 1)
    return build(lo)


def _run_detect(body: bytes, during_work=None):
    """POST `body` to detect in process; `during_work(lanes)` is called (on the loop) while the agent's work runs.
    Returns (status, what during_work returned, (small_reserve.used, inflight.used) after)."""
    import threading
    release = threading.Event()
    inside = threading.Event()
    orig = api._detect_missed_calls

    def slow_work(req):
        inside.set()
        release.wait(10)
        return orig(req)

    async def scenario():
        lanes = api._lanes()
        api._detect_missed_calls = slow_work
        try:
            c = _Client(content_length=len(body))
            task = asyncio.ensure_future(c.run())
            for i in range(0, len(body), 64 * KIB):
                await c.feed(body[i:i + 64 * KIB], more=i + 64 * KIB < len(body))
            seen = None
            loop = asyncio.get_running_loop()
            end = loop.time() + 10
            while not inside.is_set() and not task.done() and loop.time() < end:
                await asyncio.sleep(0.01)
            if inside.is_set() and during_work is not None:
                seen = during_work(lanes)
            release.set()
            await asyncio.wait_for(task, 20)
            return c.status, seen, (lanes.small_reserve.used, lanes.inflight.used)
        finally:
            api._detect_missed_calls = orig
            release.set()

    return asyncio.run(scenario())


def test_a_parsed_model_is_counted_in_the_budget_until_it_is_dropped():
    """The AEGIS N24-S-4 body: while the agent works on its ~21 MB model, the budget holds at least the model's
    measured size (b51f307: nothing — the hold went back when the parse ended)."""
    import gc
    import tracemalloc
    body = _astral_detect_body()
    gc.collect()
    tracemalloc.start()
    try:
        kept = api.CallEventsRequest.model_validate_json(body)
        model, _ = tracemalloc.get_traced_memory()          # what the model holds, measured independently
    finally:
        tracemalloc.stop()
    del kept
    status, held, after = _run_detect(body, lambda lanes: lanes.small_reserve.used + lanes.inflight.used)
    assert status == 200
    assert model > 5 * len(body), (model, len(body))
    assert held is not None and held >= model, f"{held} bytes counted while a {model}-byte model was held"
    assert after == (0, 0), after


def test_a_model_the_budget_cannot_cover_is_refused_inside_the_parse_slot(monkeypatch):
    """Room for the 4 MiB body but not for its ~21 MB model: 503 after the budget wait (b51f307: 200 — the model was
    never counted), the parse slot is free again and nothing is left counted."""
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 0.3)
    body = _astral_detect_body()

    async def scenario():
        lanes = api._lanes()
        other = lanes.inflight.limit - 6 * MIB
        await lanes.inflight.reserve(other)                   # someone else's bodies
        c = _Client(content_length=len(body))
        task = asyncio.ensure_future(c.run())
        for i in range(0, len(body), 64 * KIB):
            await c.feed(body[i:i + 64 * KIB], more=i + 64 * KIB < len(body))
        await asyncio.wait_for(task, 20)
        state = (lanes.inflight.used - other, lanes.small_reserve.used, lanes.large._value)
        lanes.inflight.release(other)
        return c, state

    c, (left, small, slots) = asyncio.run(scenario())
    assert c.status == 503, (c.status, c.body[:200])
    assert "in-flight" in c.body.decode()
    assert (left, small, slots) == (0, 0, api._LARGE_LANE_SLOTS), (left, small, slots)


@pytest.mark.parametrize("limit,events", [(4 * MIB, 1000), (64 * KIB, 200)])
def test_the_model_measure_is_not_below_what_the_model_holds(limit, events):
    """_retained_bytes against tracemalloc (what validate_json left allocated) on the worst bodies found."""
    import gc
    import tracemalloc
    body = _astral_detect_body(events, limit)
    api.CallEventsRequest.model_validate_json(body)            # warm any caches first
    gc.collect()
    tracemalloc.start()
    try:
        model = api.CallEventsRequest.model_validate_json(body)
        traced, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    counted = api._retained_bytes(model)
    assert counted >= traced * 0.97, (counted, traced)
    assert counted <= traced * 1.25, (counted, traced)        # and not so far above that it starves the budget


# --- H4: loop lag is not the client's time -------------------------------------------------------------

@pytest.fixture(scope="module")
def server():
    proc, port = _start()
    yield proc, port
    _stop(proc)


def _send_paced(port: int, size: int, step: int, every: float, freeze=None, stop_after: int | None = None,
                freeze_at: float = 1.0):
    """Declare `size` bytes and send `step` bytes every `every` s (reading while sending); `freeze()` is called
    once, `freeze_at` s in. `stop_after`: stop sending after that many bytes (a real stall). Returns (status,
    seconds)."""
    body = b'{"call_events":[' + b" " * (size - 18) + b"]}"
    head = (f"POST {DETECT} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {size}\r\n\r\n").encode()
    import threading
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    timer = threading.Timer(freeze_at, freeze) if freeze is not None else None
    try:
        s.sendall(head)
        t0, sent, nxt, buf = time.monotonic(), 0, time.monotonic(), b""
        if timer is not None:
            timer.start()                       # on its own clock, whatever the sending loop is doing
        while time.monotonic() - t0 < 45:
            r, _, _ = select.select([s], [], [], 0.02)
            if r:
                chunk = s.recv(65536)
                if not chunk:
                    return (buf[9:12].decode() or "closed"), time.monotonic() - t0
                buf += chunk
                if b"\r\n\r\n" in buf:
                    return buf[9:12].decode(), time.monotonic() - t0
            limit = len(body) if stop_after is None else stop_after
            if sent < limit and time.monotonic() >= nxt:
                piece = body[sent:min(sent + step, limit)]
                s.sendall(piece)
                sent += len(piece)
                nxt += every
        return "timeout", time.monotonic() - t0
    finally:
        if timer is not None:
            timer.cancel()
        s.close()


def _stopper(pid: int, seconds: float):
    def run():
        os.kill(pid, signal.SIGSTOP)
        try:
            time.sleep(seconds)
        finally:
            os.kill(pid, signal.SIGCONT)
    return run


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="needs POSIX job-control signals")
def test_a_client_that_keeps_sending_while_the_server_cannot_run_is_not_judged_stalled(server):
    """The reviewer's N24-S-12 question, answered by experiment: 512 KiB as 32 KiB every 0.5 s (64 KiB/s, far above
    every floor), and the SERVER stopped for 6 s, 2 s into the body; the client keeps sending into the socket.
    Before: 408 "stalled for 5s" at 8.0 s, 3/3 (b51f307)."""
    proc, port = server
    status, took = _send_paced(port, 512 * KIB, 32 * KIB, 0.5, freeze=_stopper(proc.pid, 6.0), freeze_at=2.0)
    assert status == "200", (status, took)
    assert _health(port)[0] == 200


def test_the_arrival_projection_does_not_divide_by_time_the_loop_could_not_run(monkeypatch):
    """Rule (c), in process: a 1 MiB body, 16 KiB delivered, then the event loop cannot run for 1.5 s while the
    client's next chunks arrive (queued from another thread, as the kernel queues them). Before: the projection
    divided 32 KiB by ~1.6 s of "waiting for the client" and refused at once — 408 "cannot complete within the 30s
    body deadline" — although the client was sending at its pace all along."""
    import threading

    monkeypatch.setattr(api, "_BODY_MIN_RATE_GRACE_S", 1.0)

    async def scenario():
        loop = asyncio.get_running_loop()
        size = 1 * MIB
        body = b'{"call_events":[' + b" " * (size - 18) + b"]}"
        c = _Client(content_length=size)
        task = asyncio.ensure_future(c.run())
        await c.feed(body[:16 * KIB])
        await asyncio.sleep(0.1)

        def client_keeps_sending():                       # while the loop is blocked
            for i in range(1, 4):
                time.sleep(0.3)
                loop.call_soon_threadsafe(c.queue.put_nowait, {"type": "http.request", "more_body": True,
                                                               "body": body[16 * KIB * i:16 * KIB * (i + 1)]})

        th = threading.Thread(target=client_keeps_sending)
        th.start()
        loop.call_soon(time.sleep, 1.5)                   # the loop cannot run
        await asyncio.sleep(0.01)
        th.join()
        await c.feed(body[64 * KIB:], more=False)         # and the rest at once
        await asyncio.wait_for(task, 15)
        return c

    c = asyncio.run(scenario())
    assert c.status == 200, (c.status, c.body[:200])


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="needs POSIX job-control signals")
def test_a_client_that_really_stalls_is_still_cut_on_the_loops_running_time(server):
    """The other side: a client that sends 64 KiB and then nothing is cut by the stall rule after the grace of
    RUNNING time — with the server stopped for 2 s on the way, at about grace + 2 s, well before the deadline."""
    proc, port = server
    status, took = _send_paced(port, 1 * MIB, 64 * KIB, 0.1, freeze=_stopper(proc.pid, 2.0), stop_after=64 * KIB)
    grace = http_limits.BODY_MIN_RATE_GRACE_S
    assert status == "408", (status, took)
    assert grace + 1.5 <= took <= grace + 2.0 + 3.0, took


def test_loop_lag_counts_only_time_the_loop_was_behind():
    async def scenario():
        loop = asyncio.get_running_loop()
        lag = http_limits.loop_lag(loop)
        lag.hold()
        try:
            await asyncio.sleep(0.3)
            quiet = lag.lost
            loop.call_soon(time.sleep, 0.6)              # the loop cannot run for 0.6 s
            await asyncio.sleep(0.3)
            behind = lag.lost - quiet
        finally:
            lag.drop()
        return quiet, behind, lag._handle

    quiet, behind, handle = asyncio.run(scenario())
    assert quiet < 0.25, quiet                         # (a loaded box can make even a quiet loop late)
    assert 0.4 <= behind <= 0.6 + 0.3, behind
    assert handle is None, "the tick keeps running with nobody holding it"
