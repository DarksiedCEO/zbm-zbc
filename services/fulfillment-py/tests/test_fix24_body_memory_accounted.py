"""
Fix wave 24, F1 (AEGIS N23-S-1): the request-body memory bound must hold by
construction, not by allocator luck.

The round-23 finding: the 128-sender test's 96 MiB growth bound failed 6/20
single runs and 3/10 module runs under three busy loops (growth up to
114 MiB). The in-flight budget (`_INFLIGHT_BODY_BYTES`, 64 MiB) counted only
the bytes the app had already taken into its own buffer, and only past each
body's first 64 KiB; outside it were (a) the small reserve (128 x 64 KiB =
8 MiB, a separate pool), (b) the body bytes uvicorn's protocol buffers before
the app reads them (`cycle.body`: up to its 64 KiB high-water mark plus the
read that crossed it, per connection), and (c) the chunk the app had just
received while it waited to reserve it.

What must hold now:
  - every body byte the app holds, small part included, is inside the ONE
    64 MiB budget (small reserve + shared pool == `_INFLIGHT_BODY_BYTES`);
  - a chunk is counted the moment the app receives it (the one in hand while
    the body waits for the budget included), and the body is not read further
    until the budget covers it: its next bytes wait in the socket, not here;
  - under the real launcher uvicorn's protocol buffers at most one read
    (`graceful_close.READ_BUFFER_BYTES`, 16 KiB) of a body the app has not asked
    for, not 64 KiB + a read.
"""

from __future__ import annotations

import asyncio

import uvicorn
from uvicorn.server import ServerState

from conftest import TEST_SERVICE_TOKEN
from test_fix8_n7_2_body_prealloc import DETECT, _Client, _until

import api
import http_limits
from graceful_close import READ_BUFFER_BYTES

KIB = 1024
MIB = 1024 * KIB


def test_every_body_byte_the_app_holds_is_inside_the_one_64_mib_budget():
    async def scenario():
        lanes = api._lanes()
        return lanes.small_reserve.limit, lanes.inflight.limit

    small, shared = asyncio.run(scenario())
    assert api._INFLIGHT_BODY_BYTES == 64 * MIB
    assert small == api._SMALL_RESERVE_BYTES == http_limits.LIMIT_CONCURRENCY * api._SMALL_BODY_BYTES
    assert small + shared == api._INFLIGHT_BODY_BYTES, (small, shared)


def test_a_chunk_in_hand_is_counted_and_the_next_one_is_not_read_until_covered(monkeypatch):
    """The shared pool is full; a large body has delivered its first 64 KiB
    (the small part) and the client has sent two more chunks. Before: the app
    took the first chunk off the connection and waited to reserve it — 128 KiB
    in memory and in no count, for up to _INFLIGHT_WAIT_S. Now it is counted
    the moment it is received (the pool's `over`: bytes in memory waiting to
    be covered) and the body is not read further until the budget covers it:
    the second chunk stays with the client."""
    monkeypatch.setattr(api, "_INFLIGHT_WAIT_S", 1.0)

    async def scenario():
        lanes = api._lanes()
        inflight = lanes.inflight
        await inflight.reserve(inflight.limit)  # someone else's bodies hold every shared byte
        c = _Client(content_length=api._SMALL_BODY_BYTES + 256 * KIB)
        task = asyncio.ensure_future(c.run())
        await c.feed(b'{"call_events":[' + b" " * (api._SMALL_BODY_BYTES - 16))
        await asyncio.sleep(0.1)
        await c.feed(b" " * 128 * KIB)
        await c.feed(b" " * 128 * KIB)
        # fix wave 25: until the chunk is in hand (on a starved box 0.3 s did not always get the app there), then
        # long enough for a wrong implementation to take the second one too
        await _until(lambda: getattr(inflight, "over", 0) > 0)
        await asyncio.sleep(0.3)
        seen = (getattr(inflight, "over", 0), c.queue.qsize())
        inflight.release(inflight.limit)
        await c.feed(b"]}", more=False)
        await asyncio.wait_for(task, 10)
        return seen, c

    (over, unread), c = asyncio.run(scenario())
    assert over == 128 * KIB, f"the chunk in hand is not counted ({over} bytes counted as waiting to be covered)"
    assert unread == 1, f"{2 - unread} chunk(s) taken off the connection while the budget could not cover them"
    assert c.status in (200, 422), (c.status, c.body[:200])


class _Transport(asyncio.Transport):
    """Enough of a socket transport for uvicorn's protocol, recording whether reading is paused."""

    def __init__(self):
        super().__init__()
        self.paused = False
        self.closed = False
        self.out = bytearray()
        self.protocol = None

    def get_extra_info(self, name, default=None):
        return {"sockname": ("127.0.0.1", 8091), "peername": ("127.0.0.1", 40000)}.get(name, default)

    def set_protocol(self, protocol):
        self.protocol = protocol

    def get_protocol(self):
        return self.protocol

    def pause_reading(self):
        self.paused = True

    def resume_reading(self):
        self.paused = False

    def is_reading(self):
        return not self.paused

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    def write(self, data):
        self.out += data

    def can_write_eof(self):
        return True

    def write_eof(self):
        pass

    def get_write_buffer_size(self):
        return 0


def test_uvicorn_buffers_at_most_one_read_of_a_body_the_app_has_not_asked_for():
    """The real launcher's protocol, a client that sends its body as fast as
    it is read, and an app that has not asked for the body yet (it is waiting
    for the large lane, say). Before: uvicorn kept reading until its buffer
    passed 64 KiB (its high-water mark) — up to 64 KiB + one read per
    connection, none of it in the budget. Now one read at most."""
    started = asyncio.Event()

    async def slow_app(scope, receive, send):
        started.set()
        await asyncio.sleep(3600)

    async def scenario():
        config = uvicorn.Config(app=slow_app, http=http_limits.DeadlineH11Protocol, lifespan="off",
                                h11_max_incomplete_event_size=http_limits.MAX_HEADER_BYTES)
        config.load()
        proto = http_limits.DeadlineH11Protocol(config=config, server_state=ServerState(), app_state={})
        transport = _Transport()
        proto.connection_made(transport)
        body_len = 1 * MIB
        head = (f"POST {DETECT} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TEST_SERVICE_TOKEN}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {body_len}\r\n\r\n").encode()
        stream = head + b" " * body_len
        reader = transport.protocol          # graceful_close's BufferedProtocol in front of uvicorn's
        sent = 0
        while sent < len(stream) and not transport.paused and not transport.closed:
            buf = reader.get_buffer(-1)
            n = min(len(buf), len(stream) - sent)
            buf[:n] = stream[sent:sent + n]
            reader.buffer_updated(n)
            sent += n
            await asyncio.sleep(0)           # let the app task start
        await asyncio.wait_for(started.wait(), 5)
        buffered, paused = len(proto.cycle.body), transport.paused
        for task in list(proto.tasks):     # (the cancelled app answers 500, which resumes reading)
            task.cancel()
        await asyncio.gather(*proto.tasks, return_exceptions=True)
        return buffered, sent - len(head), paused

    buffered, body_read, paused = asyncio.run(scenario())
    assert paused, "reading was never paused"
    assert buffered <= READ_BUFFER_BYTES, (
        f"uvicorn buffered {buffered} body bytes ({body_read} read) for an app that never asked for them")

