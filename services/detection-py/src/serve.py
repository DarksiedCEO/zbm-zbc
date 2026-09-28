"""
The supported way to run detection-py: `cd src && python3 serve.py [--host H] [--port P]`.

Fix wave 1 (Sep 24 2026, sweep of the "no body-size limit" finding): the
previous launch command, `python3 -m uvicorn api:app`, picks uvicorn's
httptools parser (installed by uvicorn[standard]), which puts NO limit on
the size of a request head — a request with a 20 MB header was read into
memory and answered 200. This launcher pins uvicorn's h11 parser with
h11_max_incomplete_event_size = api.MAX_HEADER_BYTES, so an oversized
request line or header block is refused (400) while it is still being read,
and adds a request-head deadline (REQUEST_HEAD_TIMEOUT_S) that uvicorn lacks.
Body limits are enforced by api._BodyLimitMiddleware under any launcher.
"""

from __future__ import annotations

import argparse

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from api import MAX_HEADER_BYTES

# uvicorn has no request-head timeout: a client that sends its request line
# and headers one byte at a time (slowloris) held a connection open for as
# long as it kept trickling (measured: still open after 20 s). The event loop
# was not blocked, but every such connection is held forever.
REQUEST_HEAD_TIMEOUT_S = 10.0


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
    previous response) is closed. The body has its own deadline
    (api.BODY_READ_TIMEOUT_S); idle keep-alive stays uvicorn's 5 s."""

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the detection-py REST service.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        "api:app",
        host=args.host,
        port=args.port,
        http=_HeadDeadlineH11Protocol,
        h11_max_incomplete_event_size=MAX_HEADER_BYTES,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
