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

from graceful_close import DRAIN_MAX_BYTES, DRAIN_TIMEOUT_S, GracefulCloseMixin, drains_max_from_env  # noqa: F401

from api import MAX_HEADER_BYTES

REQUEST_HEAD_TIMEOUT_S = 10.0
KEEP_ALIVE_TIMEOUT_S = 5
DEFAULT_MAX_CONCURRENCY = 256


# Graceful close (fix wave 21, L1) with the wave-22 bounds (G5/G6: bounded reads
# through one shared buffer; the concurrency slot and the answered request's
# buffered body released before the drain; the drain discards in that buffer; at
# most DRAINS_MAX (CREATIVE_DRAINS_MAX, default 512) drains at once) — the module shared
# byte-for-byte by the ten Python services (src/graceful_close.py).
DRAINS_MAX: int = drains_max_from_env("CREATIVE_DRAINS_MAX")


class _HeadDeadlineH11Protocol(GracefulCloseMixin, H11Protocol):
    """uvicorn's h11 protocol plus a request-head deadline: a connection
    whose next request head has not fully arrived within
    REQUEST_HEAD_TIMEOUT_S (counted from connect, or from the end of the
    previous response) is closed. (Copied from detection-py/src/serve.py —
    services don't import each other.)"""

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
