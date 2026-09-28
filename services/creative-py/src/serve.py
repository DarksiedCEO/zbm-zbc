"""
Entry point: `python3 serve.py` (from services/creative-py/src).

Binds 127.0.0.1 by default — never 0.0.0.0 by default (house rule; same
class of bug fixed in ledger-rust and orchestrator-go). Override with
CREATIVE_BIND_ADDR; port from CREATIVE_PORT (default 8300).

Fix wave 5 (AEGIS round 4, NEW-3; the same fix as detection-py's serve.py):
`uvicorn.run("api:app")` picked uvicorn's httptools parser (installed by
uvicorn[standard]), which puts NO limit on a request head — a 100-200 MB
header, sent before any authentication, was buffered in full (RSS 107 ->
220 MB) — and uvicorn has no request-head deadline, so idle and
partial-head sockets stayed open indefinitely. Now:
  - uvicorn's h11 parser with h11_max_incomplete_event_size =
    api.MAX_HEADER_BYTES (16 KiB): an oversized request line / header block
    is refused (400) while it is still being read;
  - a request-head deadline, REQUEST_HEAD_TIMEOUT_S (10 s), counted from
    connect or from the end of the previous response: a connection that
    sends nothing, a partial head, or a byte-at-a-time head is closed;
  - idle keep-alive closed after KEEP_ALIVE_TIMEOUT_S (5 s);
  - at most MAX_CONCURRENCY (CREATIVE_MAX_CONCURRENCY, default 256)
    connections + in-flight requests at once (uvicorn limit_concurrency):
    beyond it a request is answered 503 at once. Held sockets are closed by
    the deadlines above, so they free their slots.
The body has its own limits in api.BodyLimit (1 MiB, 30 s -> 408).
"""

from __future__ import annotations

import os

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from api import MAX_HEADER_BYTES

REQUEST_HEAD_TIMEOUT_S = 10.0
KEEP_ALIVE_TIMEOUT_S = 5
DEFAULT_MAX_CONCURRENCY = 256


# Fix wave 21 (lead ruling L1; the ledger-rust N20-M-1 class). uvicorn closes a
# connection it has answered with ``transport.close()`` at once — after a
# ``Connection: close`` answer (its own limit_concurrency 503, a 400 it
# writes itself, an app answer that closes) and on every deadline. Closing a
# socket that still holds unread request bytes makes the kernel send RST
# instead of FIN; a client still sending its body then fails with
# ECONNRESET/EPIPE and may never read the answer that was written. Every close
# is now graceful: FIN once the answer is flushed (``write_eof``), then the
# client's remaining bytes are read and discarded — at most DRAIN_MAX_BYTES,
# for at most DRAIN_TIMEOUT_S — then the socket is closed. A client that keeps
# sending past either bound still gets the kernel's RST, by design (the drain
# is bounded, like every other resource here).
DRAIN_MAX_BYTES = 64 * 1024
DRAIN_TIMEOUT_S = 1.0


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


class GracefulCloseMixin:
    """Mix in BEFORE uvicorn's H11Protocol: every close becomes FIN + a
    bounded drain + close (see DRAIN_MAX_BYTES / DRAIN_TIMEOUT_S)."""

    _closing = False
    _raw_transport = None
    _drain_timer = None
    _drained = 0

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self._raw_transport = transport
        super().connection_made(_GracefulTransport(transport, self))

    def _graceful_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        raw = self._raw_transport
        if raw is None or raw.is_closing():
            return
        try:
            if not raw.can_write_eof():
                raw.close()
                return
            raw.write_eof()          # FIN after the buffered answer is flushed
            raw.resume_reading()     # flow control may have paused it; the drain must read
        except (OSError, RuntimeError):
            raw.close()
            return
        self._drain_timer = self.loop.call_later(DRAIN_TIMEOUT_S, self._drain_over)

    def _drain_over(self) -> None:
        if self._drain_timer is not None:
            self._drain_timer.cancel()
            self._drain_timer = None
        if self._raw_transport is not None and not self._raw_transport.is_closing():
            self._raw_transport.close()

    def _drain(self, data: bytes) -> None:
        self._drained += len(data)
        if self._drained > DRAIN_MAX_BYTES:
            self._drain_over()

    def data_received(self, data: bytes) -> None:
        if self._closing:
            self._drain(data)
            return
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
        super().connection_lost(exc)


class _HeadDeadlineH11Protocol(GracefulCloseMixin, H11Protocol):
    """uvicorn's h11 protocol plus a request-head deadline: a connection
    whose next request head has not fully arrived within
    REQUEST_HEAD_TIMEOUT_S (counted from connect, or from the end of the
    previous response) is closed. (Copied from detection-py/src/serve.py —
    services don't import each other.)"""

    _head_timer = None
    _head_cycle = None

    def _arm_head_deadline(self) -> None:
        self._disarm_head_deadline()
        self._head_cycle = self.cycle
        self._head_timer = self.loop.call_later(REQUEST_HEAD_TIMEOUT_S, self._head_deadline_passed)

    def _disarm_head_deadline(self) -> None:
        if self._head_timer is not None:
            self._head_timer.cancel()
            self._head_timer = None

    def _head_deadline_passed(self) -> None:
        self._head_timer = None
        if not self.transport.is_closing():
            self.transport.close()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._arm_head_deadline()

    def handle_events(self) -> None:
        super().handle_events()
        # A new RequestResponseCycle means a complete request head was parsed.
        if self._head_timer is not None and self.cycle is not self._head_cycle:
            self._disarm_head_deadline()

    def on_response_complete(self) -> None:
        # Armed first: the parent may parse a pipelined request head right
        # away (via handle_events above), which disarms it again.
        self._arm_head_deadline()
        super().on_response_complete()

    def connection_lost(self, exc) -> None:
        self._disarm_head_deadline()
        super().connection_lost(exc)


def max_concurrency() -> int:
    raw = os.environ.get("CREATIVE_MAX_CONCURRENCY", str(DEFAULT_MAX_CONCURRENCY))
    try:
        n = int(raw)
    except ValueError:
        raise SystemExit(f"CREATIVE_MAX_CONCURRENCY must be a positive integer, got {raw!r}") from None
    if n < 1:
        raise SystemExit(f"CREATIVE_MAX_CONCURRENCY must be a positive integer, got {raw!r}")
    return n


def main() -> None:
    host = os.environ.get("CREATIVE_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("CREATIVE_PORT", "8300"))
    uvicorn.run(
        "api:app",
        host=host,
        port=port,
        log_level="info",
        http=_HeadDeadlineH11Protocol,
        h11_max_incomplete_event_size=MAX_HEADER_BYTES,
        timeout_keep_alive=KEEP_ALIVE_TIMEOUT_S,
        limit_concurrency=max_concurrency(),
    )


if __name__ == "__main__":
    main()
