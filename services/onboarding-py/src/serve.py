"""
Hardened uvicorn launcher for onboarding-py (fix wave 5, NEW-3). The
supported way to run the service is still ``cd src && python3 -m api``;
``api.main()`` calls ``run()`` here.

The finding: ``api.main()`` ran default uvicorn, which picks the httptools
parser (installed by uvicorn[standard]). It puts NO limit on the size of a
request head — a 100-200 MB header was buffered, unauthenticated (RSS
74 -> 347 MB) — and uvicorn has no request-head timeout, so idle and
half-sent connections were never closed.

Same approach as services/detection-py/src/serve.py:
  - uvicorn's h11 parser with ``h11_max_incomplete_event_size`` =
    MAX_HEADER_BYTES (16 KiB): an oversized request line / header block is
    refused while it is still being read (the connection is answered 400 or
    closed), never buffered;
  - a request-head deadline (REQUEST_HEAD_TIMEOUT_S), counted from connect
    and again from the end of every response: a connection that has not
    delivered a complete request head in time is closed — an idle socket
    that never sends a byte, a half-sent head, a head trickled a byte at a
    time;
  - keep-alive idle timeout (KEEP_ALIVE_TIMEOUT_S, uvicorn's own);
  - ``limit_concurrency`` (LIMIT_CONCURRENCY): beyond that many open
    connections + in-flight requests, uvicorn answers 503 at once instead of
    taking on more work.
The body has its own deadline and size cap in ``api.InputLimits``
(``body_read_timeout_seconds``, ``max_body_bytes``), under any launcher.

Tuning (env, read at start): ONBOARDING_REQUEST_HEAD_TIMEOUT_SECONDS
(default 10), ONBOARDING_KEEP_ALIVE_TIMEOUT_SECONDS (default 5),
ONBOARDING_LIMIT_CONCURRENCY (default 128),
ONBOARDING_SWITCH_INTERVAL_SECONDS (default 0.001).

The switch interval (fix wave 7, NEW-5): the interpreter lets a thread hold
the GIL for ``sys.getswitchinterval()`` (5 ms by default) before another
thread that wants it is served. A body scan is one CPU-bound thread for up
to ~1 s, and a light request needs the GIL several times on its way through
(the event loop parses it, a worker checks the body, the handler runs, the
loop writes the response), so with 5 ms slices a 40-byte message beside a
416 KB scan was p50 49-67 ms; at 1 ms it is 12-25 ms (test_fix_wave7.py,
live). The scan pays for the extra switches: measured +3-4% of CPU with
three chatty threads beside it, nothing when it runs alone.
"""

from __future__ import annotations

import os
import sys

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from graceful_close import DRAIN_MAX_BYTES, DRAIN_TIMEOUT_S, GracefulCloseMixin, drains_max_from_env  # noqa: F401


def _positive(name: str, default: float, conv=float):
    raw = os.environ.get(name)
    if not raw:
        return default
    value = conv(raw)
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


MAX_HEADER_BYTES = 16 * 1024
REQUEST_HEAD_TIMEOUT_S: float = _positive("ONBOARDING_REQUEST_HEAD_TIMEOUT_SECONDS", 10.0)
KEEP_ALIVE_TIMEOUT_S: int = _positive("ONBOARDING_KEEP_ALIVE_TIMEOUT_SECONDS", 5, int)
LIMIT_CONCURRENCY: int = _positive("ONBOARDING_LIMIT_CONCURRENCY", 128, int)
SWITCH_INTERVAL_S: float = _positive("ONBOARDING_SWITCH_INTERVAL_SECONDS", 0.001)


# Graceful close (fix wave 21, L1) with the wave-22 bounds (G5/G6: bounded reads
# through one shared buffer; the concurrency slot and the answered request's
# buffered body released before the drain; the drain discards in that buffer; at
# most DRAINS_MAX (ONBOARDING_DRAINS_MAX, default 512) drains at once) — the module shared
# byte-for-byte by the ten Python services (src/graceful_close.py).
DRAINS_MAX: int = drains_max_from_env("ONBOARDING_DRAINS_MAX")


class HeadDeadlineH11Protocol(GracefulCloseMixin, H11Protocol):
    """uvicorn's h11 protocol plus a request-head deadline: a connection
    whose next request head has not fully arrived within
    REQUEST_HEAD_TIMEOUT_S (counted from connect, or from the end of the
    previous response) is closed."""

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


def uvicorn_kwargs() -> dict:
    return {
        "http": HeadDeadlineH11Protocol,
        "h11_max_incomplete_event_size": MAX_HEADER_BYTES,
        "timeout_keep_alive": KEEP_ALIVE_TIMEOUT_S,
        "limit_concurrency": LIMIT_CONCURRENCY,
    }


def run(app, host: str, port: int) -> None:
    sys.setswitchinterval(SWITCH_INTERVAL_S)
    uvicorn.run(app, host=host, port=port, **uvicorn_kwargs())
