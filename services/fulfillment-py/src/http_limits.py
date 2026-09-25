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
  BODY_MIN_BYTES_PER_S   1 KiB/s minimum body throughput (fix wave 8, N7-2):
  BODY_MIN_RATE_GRACE_S  5 s     a body that sends nothing for
                                 BODY_MIN_RATE_GRACE_S (a stall), or that after
                                 BODY_MIN_RATE_GRACE_S of waiting has delivered
                                 fewer than BODY_MIN_BYTES_PER_S x seconds-waited
                                 bytes (a trickle), is cut — 408 while the app
                                 is reading it
                                 (api.BodySizeLimitMiddleware, measured on the
                                 time spent waiting for the client only); when
                                 the app is not (after an early 401, say) this
                                 protocol closes the connection on the same
                                 rule judged BODY_DEADLINE_GRACE_S later (so the
                                 app's 408 is written first). Before, one byte
                                 per 20 s kept a body alive for the whole 30 s
                                 deadline. (Fix wave 9: the app also refuses,
                                 at the grace, a declared body that cannot
                                 arrive by the deadline at its observed rate —
                                 api._BodyWontArrive; not applied here to
                                 unread bodies, which buffer nothing.)
  LIMIT_CONCURRENCY      128     uvicorn's limit: at or above this many open
                                 connections or in-flight requests, a new
                                 request gets 503. Bounds concurrent request
                                 bodies (each <= 4 MiB, api._MAX_BODY_BYTES) to
                                 128; the bytes actually buffered by them are
                                 bounded further by api._INFLIGHT_BODY_BYTES
                                 (64 MiB, fix wave 8).
  MAX_OPEN_CONNECTIONS   256     hard cap on held sockets: uvicorn's
                                 limit_concurrency still accepts and holds
                                 connections, so a connection made while this
                                 many are open is answered with a minimal 503
                                 (Connection: close) and closed as soon as its
                                 request bytes arrive, or after
                                 OVER_CAP_CLOSE_S if none do. It is never
                                 counted as held. (Fix wave 6, N7: it used to
                                 be aborted without a response — curl 000.)
  OVER_CAP_CLOSE_S       1 s     see above.

Trade-off (bounded, not free), exactly:
  - with >= LIMIT_CONCURRENCY (128) and < MAX_OPEN_CONNECTIONS (256) sockets
    held, every NEW request — /health included — is answered 503 by uvicorn
    for as long as they are held: at most REQUEST_HEAD_TIMEOUT_S (10 s) for
    sockets that never send a head, KEEP_ALIVE_TIMEOUT_S after a response
    for idle keep-alives, BODY_READ_TIMEOUT_S + BODY_DEADLINE_GRACE_S for
    sockets that never finish a body; a client that keeps sending complete
    requests on 128 sockets keeps the service at 503 for others while it does;
  - a connection made while MAX_OPEN_CONNECTIONS are held gets a minimal 503
    and is closed (see above), so /health from a fresh connection is 503, not
    a reset, in that state as well;
  - a client cannot make the service hold more than MAX_OPEN_CONNECTIONS
    sockets, or more than MAX_HEADER_BYTES of head per socket. Before, the
    same client could hold every file descriptor and unbounded memory
    indefinitely.
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
BODY_MIN_BYTES_PER_S = 1024
BODY_MIN_RATE_GRACE_S = 5.0
LIMIT_CONCURRENCY = 128
MAX_OPEN_CONNECTIONS = 256
OVER_CAP_CLOSE_S = 1.0

_OVER_CAP_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Length: 19\r\n"
    b"Connection: close\r\n"
    b"Retry-After: 1\r\n"
    b"\r\n"
    b"Service Unavailable"
)


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
    never extend it. While waiting for a body (fix wave 8, N7-2) the bytes
    received are counted and, from BODY_MIN_RATE_GRACE_S + BODY_DEADLINE_GRACE_S
    on, fewer than BODY_MIN_BYTES_PER_S per second closes the connection
    before the deadline: the app is not necessarily reading (an early 401
    answers without the body), and this is the only deadline such a body
    has. The grace is the app's plus BODY_DEADLINE_GRACE_S so that a body the
    app IS reading gets its 408 written before the socket is closed."""

    body_timeout_s: float = BODY_READ_TIMEOUT_S  # set by api.main()

    _deadline_timer = None
    _deadline_state = None
    _over_cap = False
    _body_started = 0.0
    _body_last = 0.0  # when the last body bytes arrived
    _body_bytes = 0
    _body_deadline = 0.0

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        if len(self.connections) > MAX_OPEN_CONNECTIONS:
            # Fix wave 6, N7: answer, don't abort. The 503 is written now; the
            # socket is closed once the client's request bytes have arrived
            # (closing with unread bytes in the socket makes the kernel send
            # RST and the client may never see the 503) or after
            # OVER_CAP_CLOSE_S, whichever is first. Not counted as held.
            self.connections.discard(self)
            self._over_cap = True
            transport.write(_OVER_CAP_RESPONSE)
            self._deadline_timer = self.loop.call_later(OVER_CAP_CLOSE_S, self._deadline_passed)
            return
        self._update_deadline()

    def data_received(self, data: bytes) -> None:
        if self._over_cap:
            self._cancel_deadline()
            if not self.transport.is_closing():
                self.transport.close()
            return
        if self._deadline_state is h11.SEND_BODY:
            self._body_bytes += len(data)
            self._body_last = self.loop.time()
        super().data_received(data)

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
            self._body_started = self._body_last = self.loop.time()
            self._body_bytes = 0
            self._body_deadline = self._body_started + self.body_timeout_s + BODY_DEADLINE_GRACE_S
            self._body_check()
            return
        else:
            return
        self._deadline_timer = self.loop.call_later(timeout, self._deadline_passed)

    def _body_check(self) -> None:
        # Close at the hard deadline, when the body has stalled for the grace
        # period, or when it has fallen below the minimum rate after the grace
        # period; otherwise sleep until the earliest moment any could be true
        # given the bytes so far.
        self._deadline_timer = None
        now = self.loop.time()
        elapsed = now - self._body_started
        grace = BODY_MIN_RATE_GRACE_S + BODY_DEADLINE_GRACE_S
        if now >= self._body_deadline or now - self._body_last >= grace or (
            elapsed >= grace and self._body_bytes < BODY_MIN_BYTES_PER_S * elapsed
        ):
            self._deadline_passed()
            return
        due = self._body_started + max(grace, self._body_bytes / BODY_MIN_BYTES_PER_S)
        wake = min(due, self._body_last + grace, self._body_deadline)
        self._deadline_timer = self.loop.call_later(max(0.05, wake - now), self._body_check)

    def _cancel_deadline(self) -> None:
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None

    def _deadline_passed(self) -> None:
        self._deadline_timer = None
        if not self.transport.is_closing():
            self.transport.close()
