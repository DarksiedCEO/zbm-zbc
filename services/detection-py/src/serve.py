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

from graceful_close import DRAIN_MAX_BYTES, DRAIN_TIMEOUT_S, GracefulCloseMixin, drains_max_from_env  # noqa: F401

from api import MAX_HEADER_BYTES

# uvicorn has no request-head timeout: a client that sends its request line
# and headers one byte at a time (slowloris) held a connection open for as
# long as it kept trickling (measured: still open after 20 s). The event loop
# was not blocked, but every such connection is held forever.
REQUEST_HEAD_TIMEOUT_S = 10.0


# Graceful close (fix wave 21, L1) with the wave-22 bounds (G5/G6: bounded reads
# through one shared buffer; the concurrency slot and the answered request's
# buffered body released before the drain; the drain discards in that buffer; at
# most DRAINS_MAX (DETECTION_DRAINS_MAX, default 512) drains at once) — the module shared
# byte-for-byte by the ten Python services (src/graceful_close.py).
DRAINS_MAX: int = drains_max_from_env("DETECTION_DRAINS_MAX")


class _HeadDeadlineH11Protocol(GracefulCloseMixin, H11Protocol):
    """uvicorn's h11 protocol plus a request-head deadline: a connection
    whose next request head has not fully arrived within
    REQUEST_HEAD_TIMEOUT_S (counted from connect, or from the end of the
    previous response) is closed. The body has its own deadline
    (api.BODY_READ_TIMEOUT_S); idle keep-alive stays uvicorn's 5 s."""

    drains_max = DRAINS_MAX

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
