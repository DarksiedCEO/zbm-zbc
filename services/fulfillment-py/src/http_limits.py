"""
Transport limits for the supported entrypoint, `python3 -m api` (fix wave 5,
NEW-3, MED, CONFIRMED). Mirrors services/detection-py/src/serve.py.

The finding: `python3 -m api` ran uvicorn with its defaults — the httptools
parser (installed by uvicorn[standard]), which has NO request-head size
limit, and no request-head or request-body deadline. Without the service
token, one 100-200 MB header was buffered in full (RSS 53 -> 415 MB); 10/10
idle and partial-head sockets were still open after 60 s; a slow-drip body
held its connection indefinitely (after an early 401 every trickled byte
even reset uvicorn's keep-alive timer).

The limits, all enforced before routing and auth:

  MAX_HEADER_BYTES       16 KiB  request line + headers. uvicorn's h11 parser
                                 with h11_max_incomplete_event_size refuses a
                                 longer head (400, connection closed) while it
                                 is being read — at most one recv buffer past
                                 the cap is ever held. api.BodySizeLimitMiddleware
                                 re-checks it (431) under any other launcher.
  REQUEST_HEAD_TIMEOUT_S 10 s    from connect (or from the end of the previous
                                 response) until the request head is complete;
                                 covers idle-from-connect and slowloris heads.
  KEEP_ALIVE_TIMEOUT_S   5 s     idle keep-alive between requests (uvicorn's).
  BODY_READ_TIMEOUT_S    30 s    from the end of the head until the body is
                                 complete. The app answers 408 when it is
                                 reading (api.BodySizeLimitMiddleware); this
                                 protocol closes the connection at the same
                                 deadline + BODY_DEADLINE_GRACE_S whether or not
                                 the app reads (e.g. after an early 401).
                                 FULFILLMENT_BODY_READ_TIMEOUT_S may narrow it
                                 (0 < value <= 30); anything else refuses startup.
  LIMIT_CONCURRENCY      128     uvicorn's limit: at or above this many open
                                 connections or in-flight requests, a new
                                 request gets 503. Bounds concurrent request
                                 bodies (each <= 4 MiB, api._MAX_BODY_BYTES):
                                 worst case ~512 MiB of in-flight body bytes.
  MAX_OPEN_CONNECTIONS   256     hard cap on held sockets: uvicorn's
                                 limit_concurrency still accepts and holds
                                 connections, so a connection made while this
                                 many are open is closed at once.

Trade-off (bounded, not free): a client holding >= LIMIT_CONCURRENCY sockets
makes the service answer 503 (including /health) until those sockets are
closed — at most REQUEST_HEAD_TIMEOUT_S for sockets that never send a head —
and cannot make it hold more than MAX_OPEN_CONNECTIONS sockets or more than
MAX_HEADER_BYTES of head per socket. Before, the same client could hold every
file descriptor and unbounded memory indefinitely.
"""

from __future__ import annotations

import math
import os

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol

MAX_HEADER_BYTES = 16 * 1024
REQUEST_HEAD_TIMEOUT_S = 10.0
KEEP_ALIVE_TIMEOUT_S = 5
BODY_READ_TIMEOUT_S = 30.0
BODY_DEADLINE_GRACE_S = 5.0
LIMIT_CONCURRENCY = 128
MAX_OPEN_CONNECTIONS = 256


def load_body_read_timeout() -> float:
    """BODY_READ_TIMEOUT_S, or a narrower FULFILLMENT_BODY_READ_TIMEOUT_S.
    Raises RuntimeError (refuse startup) on anything invalid or wider."""
    raw = os.environ.get("FULFILLMENT_BODY_READ_TIMEOUT_S")
    if raw is None:
        return BODY_READ_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and 0 < value <= BODY_READ_TIMEOUT_S):
        raise RuntimeError(
            f"FULFILLMENT_BODY_READ_TIMEOUT_S={raw!r} is invalid: expected seconds, "
            f"0 < value <= {BODY_READ_TIMEOUT_S:g} (may only narrow). This service refuses to start."
        )
    return value


class DeadlineH11Protocol(H11Protocol):
    """uvicorn's h11 protocol plus a request-head deadline, a request-body
    deadline and a hard cap on open connections. The deadline in force is
    derived from h11's view of the client after every parse step:
      their_state IDLE      -> waiting for a head:  REQUEST_HEAD_TIMEOUT_S
      their_state SEND_BODY -> waiting for a body:  body timeout + grace
      anything else         -> request fully received: no deadline (the
                               app's work and uvicorn's keep-alive govern)
    A deadline is (re)armed only when the state changes, so trickled bytes
    never extend it."""

    body_timeout_s: float = BODY_READ_TIMEOUT_S  # set by api.main()

    _deadline_timer = None
    _deadline_state = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        if len(self.connections) > MAX_OPEN_CONNECTIONS:
            self.connections.discard(self)
            transport.abort()
            return
        self._update_deadline()

    def handle_events(self) -> None:
        super().handle_events()
        self._update_deadline()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        self._update_deadline()

    def connection_lost(self, exc) -> None:
        self._cancel_deadline()
        super().connection_lost(exc)

    # --

    def _update_deadline(self) -> None:
        if self.transport is None or self.transport.is_closing():
            self._cancel_deadline()
            return
        state = self.conn.their_state
        if state is self._deadline_state:
            return
        self._cancel_deadline()
        self._deadline_state = state
        if state is h11.IDLE:
            timeout = REQUEST_HEAD_TIMEOUT_S
        elif state is h11.SEND_BODY:
            timeout = self.body_timeout_s + BODY_DEADLINE_GRACE_S
        else:
            return
        self._deadline_timer = self.loop.call_later(timeout, self._deadline_passed)

    def _cancel_deadline(self) -> None:
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None

    def _deadline_passed(self) -> None:
        self._deadline_timer = None
        if not self.transport.is_closing():
            self.transport.close()
