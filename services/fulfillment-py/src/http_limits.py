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
                                 api._BodyWontArrive, judged on the same
                                 waiting-on-the-client clock (fix wave 10,
                                 N9-6); not applied here to
                                 unread bodies, which buffer nothing.)
  LIMIT_CONCURRENCY      128     uvicorn's limit: at or above this many open
                                 connections or in-flight requests, a new
                                 request gets 503. Bounds concurrent request
                                 bodies (each <= 4 MiB, api._MAX_BODY_BYTES) to
                                 128; the bytes actually buffered by them are
                                 bounded further by api._INFLIGHT_BODY_BYTES
                                 (64 MiB, fix wave 8; since fix wave 24 every
                                 body byte the process holds, the small reserve
                                 included).
  body reads             reads of READ_BUFFER_BYTES (16 KiB), and only
                                 into in-flight budget the app has already
                                 reserved for this body (fix wave 24, F1; fix
                                 wave 25, H1): the protocol stops reading a body
                                 once the bytes it has buffered for the app plus
                                 the bytes the app has taken reach the bytes the
                                 app has covered (api._BodyHold, found under
                                 BODY_HOLD_SCOPE_KEY in the request's scope).
                                 The app reserves up to api._READ_GRANT_BYTES
                                 (64 KiB) ahead of what it has taken whenever
                                 the budget has them free, so a body streams as
                                 it did under uvicorn alone (which read up to
                                 64 KiB + a read ahead per connection, outside
                                 the budget); with no grant it is one read per
                                 ask (wave 24). At most one read past the
                                 covered bytes is ever buffered.
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

import asyncio
import math
import os
import sys
import weakref

import h11
from uvicorn.protocols.http.h11_impl import H11Protocol

import launch_guard
from graceful_close import (  # noqa: F401
    DRAIN_MAX_BYTES, DRAIN_TIMEOUT_S, READ_BUFFER_BYTES, GracefulCloseMixin, drains_max_from_env,
)

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
# Fix wave 25, H1: where the app puts the in-flight budget it holds for the body
# (api._BodyHold: `covered`, the body bytes reserved; `taken`, the body bytes the
# app has taken from this protocol). Read by DeadlineH11Protocol.handle_events.
BODY_HOLD_SCOPE_KEY = "fulfillment.body_hold"

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


# Fix wave 25 (scout C5-2; FIX_WAVE_23b item 2 ruled the same gap in detection-py
# a product defect): the GIL switch interval. A thread holds the GIL for
# sys.getswitchinterval() (CPython's default 5 ms) before a thread that wants it
# is served; a 4 MiB parse (run_in_threadpool) is CPU-bound for tens of ms, and
# the event loop gives the GIL up on every syscall (accept, recv, send) and
# waited up to a full slice to get it back each time — the latency of every
# small request and /health behind a large parse or a junk flood. `python3 -m
# api` never set it. Now 1 ms, as every other launcher in this repo;
# FULFILLMENT_SWITCH_INTERVAL_SECONDS overrides it only within [100 us, 50 ms]
# (detection-py's range, fix wave 24 F3: 0.5 s would let a thread hold the GIL
# half a second, 1e-7 s is a switch storm), read at import (refuses startup, as
# FULFILLMENT_BODY_READ_TIMEOUT_S), and the launcher checks the interval in
# force in whole microseconds (CPython truncates 1e6 x the value: 0.0001 reads
# back as 9.999999999999999e-05 — detection-py's wave-25 H5) and prints it.
SWITCH_INTERVAL_DEFAULT_S = launch_guard.SWITCH_INTERVAL_DEFAULT_S
SWITCH_INTERVAL_MIN_US = launch_guard.SWITCH_INTERVAL_MIN_US
SWITCH_INTERVAL_MAX_US = launch_guard.SWITCH_INTERVAL_MAX_US
_SWITCH_ENV = "FULFILLMENT_SWITCH_INTERVAL_SECONDS"


# Fix wave 26b (scout C5-3): the check itself is now the one shared by every launcher (src/launch_guard.py,
# byte-identical in each service); these names stay for this service's callers and tests.


def load_switch_interval() -> float:
    """SWITCH_INTERVAL_DEFAULT_S, or FULFILLMENT_SWITCH_INTERVAL_SECONDS
    (unset or empty: the default). Raises RuntimeError (refuse startup) unless
    100 us <= value <= 50 ms."""
    return launch_guard.switch_interval_from_env(_SWITCH_ENV, SWITCH_INTERVAL_DEFAULT_S)


def apply_switch_interval(value: float) -> int:
    """Sets the interval and returns the one in force in whole microseconds
    (as CPython keeps it); RuntimeError unless it is within [MIN_US, MAX_US]
    and is `value` to within the microsecond CPython truncates."""
    return launch_guard.apply_switch_interval(value, _SWITCH_ENV)


# Fix wave 25, H4 (AEGIS N24-S-12): the rules that judge a CLIENT by time —
# the app's stall, trickle and arrival rules (api.BodySizeLimitMiddleware) and
# this protocol's own stall and rate rules for unread bodies — counted every
# second of wall time spent waiting for the client's bytes, including time the
# event loop could not run at all. Measured (wave 25, h4_freeze.py): a client
# sending 32 KiB every 0.5 s, the server process stopped for 6 s or 9 s mid-body
# -> 408 "stalled for 5s" 3/3 and 3/3; stopped for 3 s -> 408 "cannot complete"
# 2/3 (the arrival projection divided the bytes by a wait that included the
# stop). The client had sent all along; its bytes sat in the kernel. LoopLag
# measures the time the loop was behind: a timer every LOOP_LAG_TICK_S, and
# whatever it fires later than LOOP_LAG_SLACK_S past its time is added to
# `lost`. The client-judging clocks subtract the `lost` that accrued while they
# ran. The hard deadlines (BODY_READ_TIMEOUT_S, the protocol's +grace,
# REQUEST_HEAD_TIMEOUT_S) stay wall-clock bounds. The timer runs only while
# some body is being judged (`hold`/`drop`).
LOOP_LAG_TICK_S = 0.05
LOOP_LAG_SLACK_S = 0.05


class LoopLag:
    """Seconds the event loop has been behind (`lost`), measured while held."""

    __slots__ = ("loop", "lost", "users", "_when", "_handle", "__weakref__")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.lost = 0.0
        self.users = 0
        self._when = 0.0
        self._handle: asyncio.TimerHandle | None = None

    def hold(self) -> None:
        self.users += 1
        if self._handle is None:
            self._arm()

    def drop(self) -> None:
        self.users -= 1
        if self.users <= 0:
            self.users = 0
            if self._handle is not None:
                self._handle.cancel()
                self._handle = None

    def _arm(self) -> None:
        self._when = self.loop.time() + LOOP_LAG_TICK_S
        self._handle = self.loop.call_at(self._when, self._tick)

    def _tick(self) -> None:
        late = self.loop.time() - self._when
        if late > LOOP_LAG_SLACK_S:
            self.lost += late - LOOP_LAG_SLACK_S
        self._arm()


_loop_lags: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, LoopLag]" = weakref.WeakKeyDictionary()


def loop_lag(loop: asyncio.AbstractEventLoop) -> LoopLag:
    lag = _loop_lags.get(loop)
    if lag is None:
        lag = _loop_lags[loop] = LoopLag(loop)
    return lag


# Graceful close (fix wave 21, L1) with the wave-22 bounds (G5/G6: the slot and the
# answered request's buffered body are released before the drain; the drain reads
# into one small shared buffer; at most DRAINS_MAX drains at once) — the module
# shared byte-for-byte by the ten Python services (src/graceful_close.py).
DRAINS_MAX = drains_max_from_env("FULFILLMENT_DRAINS_MAX")


class DeadlineH11Protocol(GracefulCloseMixin, H11Protocol):
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
    drains_max = DRAINS_MAX

    _deadline_timer = None
    _deadline_state = None
    _over_cap = False
    _body_started = 0.0
    _body_last = 0.0  # when the last body bytes arrived
    _body_bytes = 0
    _body_deadline = 0.0
    _lag: LoopLag | None = None       # held while a body is judged (fix wave 25, H4)
    _body_started_lost = 0.0
    _body_last_lost = 0.0

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
        if self._closing:            # graceful close: the drainer reads from here on (L1, G5)
            return
        if self._over_cap:
            self._cancel_deadline()
            if not self.transport.is_closing():
                self.transport.close()
            return
        if self._deadline_state is h11.SEND_BODY:
            self._body_bytes += len(data)
            self._body_last = self.loop.time()
            if self._lag is not None:
                self._body_last_lost = self._lag.lost
        super().data_received(data)

    def handle_events(self) -> None:
        super().handle_events()
        # Fix wave 24, F1 (AEGIS N23-S-1): a body is read only into budget the app
        # holds for it. uvicorn keeps reading a body until its buffer passes 64 KiB
        # (its high-water mark) whether or not the app has asked — up to 64 KiB
        # + one read per connection, outside the app's in-flight budget.
        # Fix wave 25, H1 (AEGIS N24-S-1/-2): wave 24 paused as soon as the buffer
        # held anything, so every 16 KiB read waited for a round trip through the
        # app (wake the app, take the chunk, cover it, ask again, resume, next
        # loop pass) — under CPU contention per-body intake slowed and stalled
        # bodies were cut later (settle 11.6-15.4 s against 11.3-12.5 s). Now the
        # app reserves up to 64 KiB AHEAD of what it has taken whenever the
        # budget has them free (api._BodyHold.grant), and reading pauses only
        # when the bytes buffered here plus the bytes the app has taken reach
        # the bytes it has covered: covered bytes stream without a round trip,
        # and at most one read (READ_BUFFER_BYTES, 16 KiB) past them is ever
        # buffered. No hold in the scope (the app has not started yet, or
        # another app): paused as soon as anything is buffered, as in wave 24.
        cycle = self.cycle
        if cycle is not None and cycle.body and not cycle.response_complete:
            hold = cycle.scope.get(BODY_HOLD_SCOPE_KEY)
            if hold is None or hold.taken + len(cycle.body) >= hold.covered:
                self.flow.pause_reading()
        self._update_deadline()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        self._update_deadline()

    def connection_lost(self, exc) -> None:
        self._cancel_deadline()
        self._drop_lag()
        super().connection_lost(exc)

    # --

    def _drop_lag(self) -> None:
        if self._lag is not None:
            self._lag.drop()
            self._lag = None

    def _update_deadline(self) -> None:
        if self.transport is None or self.transport.is_closing():
            self._cancel_deadline()
            self._drop_lag()
            return
        state = self.conn.their_state
        if state is self._deadline_state:
            return
        self._cancel_deadline()
        self._drop_lag()
        self._deadline_state = state
        if state is h11.IDLE:
            timeout = REQUEST_HEAD_TIMEOUT_S
        elif state is h11.SEND_BODY:
            self._lag = loop_lag(self.loop)
            self._lag.hold()
            self._body_started = self._body_last = self.loop.time()
            self._body_started_lost = self._body_last_lost = self._lag.lost
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
        # given the bytes so far. Fix wave 25, H4: the stall and the rate are
        # judged on the time the loop was able to run (LoopLag), not wall time;
        # the hard deadline stays wall-clock.
        self._deadline_timer = None
        now = self.loop.time()
        lost = self._lag.lost if self._lag is not None else 0.0
        elapsed = max(0.0, now - self._body_started - (lost - self._body_started_lost))
        stalled = max(0.0, now - self._body_last - (lost - self._body_last_lost))
        grace = BODY_MIN_RATE_GRACE_S + BODY_DEADLINE_GRACE_S
        if now >= self._body_deadline or stalled >= grace or (
            elapsed >= grace and self._body_bytes < BODY_MIN_BYTES_PER_S * elapsed
        ):
            self._deadline_passed()
            return
        due = max(grace, self._body_bytes / BODY_MIN_BYTES_PER_S) - elapsed
        wake = min(due, grace - stalled, self._body_deadline - now)
        self._deadline_timer = self.loop.call_later(max(0.05, wake), self._body_check)

    def _cancel_deadline(self) -> None:
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None

    def _deadline_passed(self) -> None:
        self._deadline_timer = None
        if not self.transport.is_closing():
            self.transport.close()
