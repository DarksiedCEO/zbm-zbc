"""
Graceful close and bounded reads for uvicorn's h11 protocol — ONE module, byte-identical in all ten Python
services (fix wave 22, lead ruling G6; each service's tests/test_live_graceful_close_module.py pins this file's sha256
and compares it with the sibling copies it can see). Mix ``GracefulCloseMixin`` in BEFORE uvicorn's ``H11Protocol``.

Fix wave 21 (lead ruling L1; the ledger-rust N20-M-1 class): uvicorn closes a connection it has answered with
``transport.close()`` at once — after a ``Connection: close`` answer (its own limit_concurrency 503, a 400 it writes
itself, an app answer that closes) and on every deadline. Closing a socket that still holds unread request bytes
makes the kernel send RST instead of FIN; a client still sending its body then fails with ECONNRESET/EPIPE and may
never read the answer that was written. Every close is graceful: FIN once the answer is flushed (``write_eof``),
then the client's remaining bytes are read and discarded — at most DRAIN_MAX_BYTES, for at most DRAIN_TIMEOUT_S —
then the socket is closed. A client that keeps sending past either bound still gets the kernel's RST, by design.

Fix wave 22 (G5, G6; AEGIS N21-C-1, N21-C-3), mirroring ledger-rust:
  - every read goes through ONE buffer per event-loop thread (READ_BUFFER_BYTES, a ``BufferedProtocol`` in front of
    uvicorn's protocol): a read hands the parser at most READ_BUFFER_BYTES. The event loop's own reads were up to
    256 KiB, and uvicorn buffers a request's body up to its 64 KiB high-water mark PLUS the read that crossed it
    before it applies backpressure — measured (tracemalloc, the 128-sender scenario): ~150 KiB of unread body per
    connection answered 503 before its app ran, ~15 MiB across the burst, plus the receive-path copies of those
    buffers. The per-connection bound is now 64 KiB + READ_BUFFER_BYTES;
  - drained bytes are counted in that same buffer and discarded: no bytes object, no per-connection buffer;
  - the connection gives its concurrency slot back BEFORE it drains (uvicorn's limit_concurrency counts
    ``server_state.connections``; a draining socket is no longer being served), and the body uvicorn buffered for
    the request it answered is released then (nothing can read it any more);
  - at most ``drains_max`` connections of one server drain at once (DRAINS_MAX by default; a service's config may
    set it); past that an answered socket is closed at once (the pre-wave-21 behaviour, RST included).
"""

from __future__ import annotations

import asyncio
import os
import threading

DRAIN_MAX_BYTES = 64 * 1024
DRAIN_TIMEOUT_S = 1           # seconds; an int: no float literal in any service's src (finance-py's G1 guardrail)
DRAINS_MAX = 512
READ_BUFFER_BYTES = 16 * 1024

_tls = threading.local()


def drains_max_from_env(name: str, default: int = DRAINS_MAX) -> int:
    """``name`` from the environment as the concurrent-drain cap (a positive integer), else ``default``.
    Anything else refuses startup (RuntimeError)."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        raise RuntimeError(f"{name}={raw!r} is invalid: expected a positive integer (concurrent graceful-close drains)")
    return value


def read_buffer() -> memoryview:
    """This thread's one read buffer (an event loop's reads are synchronous: get_buffer, recv_into and
    buffer_updated run back to back, so the buffer is never shared by two reads in flight)."""
    buf = getattr(_tls, "buf", None)
    if buf is None:
        buf = _tls.buf = memoryview(bytearray(READ_BUFFER_BYTES))
    return buf


def draining(server_state) -> set:
    """The set of this server's connections currently draining (kept on uvicorn's ServerState)."""
    s = getattr(server_state, "_zbm_draining", None)
    if s is None:
        s = set()
        try:
            server_state._zbm_draining = s
        except AttributeError:
            pass
    return s


class _GracefulTransport:
    """The transport uvicorn sees: ``close()`` starts the graceful close;
    writes after it are dropped (as asyncio drops writes after close())."""

    __slots__ = ("_raw", "_proto")

    def __init__(self, raw, proto) -> None:
        self._raw = raw
        self._proto = proto

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def close(self) -> None:
        self._proto._graceful_close()

    def is_closing(self) -> bool:
        return self._proto._closing or self._raw.is_closing()

    def write(self, data) -> None:
        if not self._proto._closing:
            self._raw.write(data)

    def writelines(self, lines) -> None:
        if not self._proto._closing:
            self._raw.writelines(lines)


class _Reader(asyncio.BufferedProtocol):
    """The transport's protocol: reads into the thread's read buffer and hands the owner (uvicorn's protocol) a
    copy of what arrived; while the owner drains, counts and discards instead."""

    __slots__ = ("_owner", "drain_left")

    def __init__(self, owner) -> None:
        self._owner = owner
        self.drain_left = None

    def get_buffer(self, sizehint):
        return read_buffer()

    def buffer_updated(self, nbytes: int) -> None:
        if self.drain_left is not None:
            self.drain_left -= nbytes
            if self.drain_left < 0:
                self._owner._drain_over()
            return
        self._owner.data_received(bytes(read_buffer()[:nbytes]))

    def eof_received(self):
        return self._owner.eof_received()

    def pause_writing(self) -> None:
        self._owner.pause_writing()

    def resume_writing(self) -> None:
        self._owner.resume_writing()

    def connection_lost(self, exc) -> None:
        self._owner.connection_lost(exc)


class GracefulCloseMixin:
    """Mix in BEFORE uvicorn's H11Protocol: bounded reads, and every close becomes FIN + a bounded drain + close
    (see the module docstring)."""

    drains_max = DRAINS_MAX
    _closing = False
    _raw_transport = None
    _reader = None
    _drain_timer = None
    _draining_in = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self._raw_transport = transport
        super().connection_made(_GracefulTransport(transport, self))
        if not transport.is_closing():
            self._reader = _Reader(self)
            transport.set_protocol(self._reader)

    def _graceful_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        connections = getattr(self, "connections", None)
        if connections is not None:
            connections.discard(self)            # the slot is free before the drain (G6)
        cycle = getattr(self, "cycle", None)
        if cycle is not None and getattr(cycle, "response_complete", False):
            cycle.body = b""                     # answered: nothing reads that body any more (G5)
        raw = self._raw_transport
        if raw is None or raw.is_closing():
            return
        active = draining(getattr(self, "server_state", None))
        try:
            if self._reader is None or len(active) >= self.drains_max or not raw.can_write_eof():
                raw.close()                      # over the cap: closed at once (RST if bytes are unread)
                return
            raw.write_eof()                      # FIN after the buffered answer is flushed
            self._reader.drain_left = DRAIN_MAX_BYTES
            raw.resume_reading()                 # flow control may have paused it; the drain must read
        except (OSError, RuntimeError):
            raw.close()
            return
        active.add(self)
        self._draining_in = active
        self._drain_timer = self.loop.call_later(DRAIN_TIMEOUT_S, self._drain_over)

    def _drain_over(self) -> None:
        if self._drain_timer is not None:
            self._drain_timer.cancel()
            self._drain_timer = None
        if self._raw_transport is not None and not self._raw_transport.is_closing():
            self._raw_transport.close()

    def data_received(self, data: bytes) -> None:
        if self._closing:
            return                               # closing: nothing more is parsed or buffered
        super().data_received(data)

    def eof_received(self):
        if self._closing:
            self._drain_over()
            return None
        return super().eof_received()

    def connection_lost(self, exc) -> None:
        if self._drain_timer is not None:
            self._drain_timer.cancel()
            self._drain_timer = None
        if self._draining_in is not None:
            self._draining_in.discard(self)
            self._draining_in = None
        super().connection_lost(exc)
