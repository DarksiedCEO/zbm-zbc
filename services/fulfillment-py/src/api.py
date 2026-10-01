"""
REST surface for the Fulfillment department (ADR 0002). Same discipline
as services/detection-py/src/api.py: thin request/response marshaling
around pure agent functions, fail-closed bearer-token auth on every
route except /health, built in from the first commit — not bolted on
after an independent review catches its absence.

Deliberately a single service this pass (ADR 0002, Decision 2) — no
separate orchestrator process. Endpoints call the real decision logic in
each agent directly; the SIP dialer and system-of-record ADAPTERS behind
those agents default to the honest not-wired/not-configured stand-ins,
never to something that reports success for a call that was never
placed or a write that never happened.

Independent review finding (Sep 22 2026, CRITICAL, CONFIRMED): this API
previously defaulted to InMemorySipDialer / InMemorySystemOfRecord — the
TEST DOUBLES meant only for the test suite — so a caller hitting
/agents/callback-orchestration/run or /agents/resolution-writeback/resolve
got back dial_placed=true / write_back_status="success" for a call that
was never placed and a write that never happened anywhere. That is
exactly the failure mode the "Known gaps" README section claims doesn't
happen. Fixed: the API now defaults to NotWiredSipDialer /
NotConfiguredSystemOfRecord — the same honest seams the agents' own
tests exercise — and only swaps in the in-memory test doubles when an
operator explicitly opts in via FULFILLMENT_SIP_DIALER=in_memory /
FULFILLMENT_SYSTEM_OF_RECORD=in_memory (for local demos only; a real
deployment sets neither and gets the honest default).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import weakref
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Literal, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from pydantic_core import PydanticCustomError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agents import (
    appointment_tracking,
    callback_orchestration,
    customer_dossier,
    followup_sequencing,
    missed_call_detection,
    resolution_writeback,
)
from bounded_state import BoundedExpiringMap
import http_limits
from contact_window import ContactWindow, parse_contact_window
from fixtures_loader import load_appointments, load_call_events, load_dossiers
from fulfillment_schema import (
    Appointment,
    CallEvent,
    CustomerDossier,
    EntityId,
    FollowUpTask,
    PhoneE164,
    ResolutionRecord,
    ResolutionType,
    TaskChannel,
    TaskId,
    TaskStatus,
)
from integrations.sip_dialer import InMemorySipDialer, NotWiredSipDialer, SipDialerPort
from outbound_gate import AttemptLimits, OutboundContactGate, parse_attempt_limits
from recipient_zones import parse_country_zones
from integrations.system_of_record import (
    InMemorySystemOfRecord,
    NotConfiguredSystemOfRecord,
    SystemOfRecordPort,
)
from agents.resolution_writeback import TerminalEvent

# --- auth ---------------------------------------------------------------
# Fail-closed by design, matching detection-py's ZBM_SERVICE_TOKEN
# pattern exactly (ADR 0002, Decision 5): this service refuses to start
# if no token is configured, rather than silently serving every endpoint
# unauthenticated.


def _load_required_token() -> str:
    token = os.environ.get("FULFILLMENT_SERVICE_TOKEN")
    if not token:
        raise RuntimeError(
            "FULFILLMENT_SERVICE_TOKEN is not set. This service refuses to "
            "start without an auth token configured (fail closed, not open). "
            "Set FULFILLMENT_SERVICE_TOKEN to a shared secret before starting "
            "fulfillment-py."
        )
    return token


_REQUIRED_TOKEN = _load_required_token()
_log = logging.getLogger("fulfillment")


def require_auth(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization.removeprefix("Bearer ")
    try:
        valid = hmac.compare_digest(supplied, _REQUIRED_TOKEN)
    except TypeError:
        # Independent review finding (Sep 22 2026, CONFIRMED): Python's
        # hmac.compare_digest raises TypeError on a non-ASCII str
        # comparison ("comparing strings with non-ASCII characters is not
        # supported"), which was previously unhandled here and surfaced
        # as an unauthenticated HTTP 500 instead of a 401 — a real
        # attacker-reachable crash, and a status code that leaks more
        # than "invalid token" should. Any input compare_digest can't
        # evaluate is, definitionally, not a valid token.
        valid = False
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = FastAPI(
    title="ZBM Fulfillment — Missed-Call, Follow-Up, Dossier & Completion Tracking",
    description=(
        "NON-LIVE: this service has no live SIP/LiveKit connection and no "
        "wired CRM/system-of-record by default. All data is either "
        "request-supplied or served from the fixtures/ pool for dev/testing "
        "only."
    ),
    version="0.1.0",
    # Independent review finding (Sep 22 2026, CONFIRMED): /docs, /redoc,
    # and /openapi.json were reachable with NO auth at all — not a secret
    # leak (no token/fixture data in the schema itself), but it exposes
    # every route name and shape to an unauthenticated caller, which is
    # unnecessary surface area for a service with no public-facing need
    # for interactive docs. Disabled outright rather than gated behind
    # auth, matching the fail-closed default elsewhere in this service.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# --- request body limit (fix wave 4) ------------------------------------------
# Before: no limit at all. A body of any size was read into memory and
# json-decoded; with the decode on the event loop (below), a 64 MiB body
# stalled /health for 3.3 s for every client of the process.
#
# Sized from the max batch (every list/map is capped at _MAX_BATCH = 1000):
# the largest legitimate body is a 1000-task orchestrate batch with every
# bounded field at its maximum and every task `reason` at its full 2,000
# ASCII chars — 3.57 MB (tests/test_fix4_limits.py::MAX_BATCHES builds each
# route's worst case and asserts it fits and is accepted). The one field that
# cannot be at its maximum across a full batch is voicemail_transcript
# (10,000 chars x 1000 = 10 MB): a 1000-event batch fits transcripts averaging
# ~3,400 ASCII chars; a larger one gets 413 and must be split.
_MAX_BODY_BYTES = 4 * 1024 * 1024
# Fix wave 5, NEW-3: head size and body read deadline, enforced here under
# any launcher (TestClient, a bare `uvicorn api:app`). The real head bound
# is in the parser and the hard connection deadlines are in the protocol:
# see http_limits.py, which `python3 -m api` runs (main() below).
_MAX_HEADER_BYTES = http_limits.MAX_HEADER_BYTES
_BODY_READ_TIMEOUT_S = http_limits.load_body_read_timeout()  # refuses startup if invalid
# Fix wave 8, N7-2: the deadline alone let one byte per 20 s hold a body (and
# whatever was buffered for it) for the whole 30 s. Now a body is answered 408
# when (a) a single wait for its next chunk reaches _BODY_MIN_RATE_GRACE_S
# (a stall — front-loading 3.9 MB and then sending nothing earns no credit)
# or (b) after _BODY_MIN_RATE_GRACE_S of waiting in total it has delivered
# fewer than _BODY_MIN_BYTES_PER_S per second waited (a trickle). The clock
# is the time spent WAITING for the client's bytes, not wall time: time the
# service itself spends holding a request (the in-flight byte budget below)
# is not charged to the client.
_BODY_MIN_BYTES_PER_S = http_limits.BODY_MIN_BYTES_PER_S
_BODY_MIN_RATE_GRACE_S = http_limits.BODY_MIN_RATE_GRACE_S
# Fix wave 9 (AEGIS round 8, Q2): a body must be able to arrive by the
# deadline. From the grace on, a body with a declared size whose remaining
# bytes cannot arrive by the deadline at the rate observed so far (bytes over
# time spent waiting for the client) is answered 408 at once, telling the
# client how far to split — not cut at 30 s after uploading most of it. The
# 30 s deadline is NOT raised for large bodies: a longer deadline is longer
# for every slow sender to hold memory. So a body needs >= size / 30 s: a
# maximum 4 MiB batch needs >= 136.5 KiB/s; at 64 KiB/s (poor mobile) it
# cannot arrive (64 s) and must be split into requests of <= ~1.9 MB.
# Fix wave 10, N9-6: the projection is on ONE clock, the time spent waiting
# for the client's bytes — refused iff that waiting plus the waiting the rest
# needs at the observed rate exceeds _BODY_READ_TIMEOUT_S (i.e. the observed
# rate x 30 s < the declared size). It used to take the rate on that clock and
# the remaining time on the wall clock (see BodySizeLimitMiddleware).


class _BodyTooLarge(Exception):
    pass


class _BodyTimeout(Exception):
    pass


class _BodyTooSlow(Exception):
    pass


class _BodyWontArrive(Exception):
    """Fix wave 9: the declared body cannot arrive by the deadline at its rate."""

    def __init__(self, declared: int, received: int, rate: float) -> None:
        self.declared, self.received, self.rate = declared, received, rate


class _BodyPreempted(Exception):
    """Fix wave 9: cut to give its shared in-flight bytes to a waiting body."""

    def __init__(self, byte_seconds: float) -> None:
        self.byte_seconds = byte_seconds


# Fix wave 9: per-request accounting of the shared in-flight bytes a body
# holds, shared between BodySizeLimitMiddleware (which knows when the service
# is waiting on the client and, since fix wave 24, reserves the bytes through
# _BodyHold) and _off_loop (which releases them once the body is parsed).
_ACCOUNT_SCOPE_KEY = "fulfillment.body_account"
_HOLD_SCOPE_KEY = http_limits.BODY_HOLD_SCOPE_KEY  # also read by the protocol (fix wave 25, H1)


class _BodyAccount:
    """`held`: shared in-flight bytes this body holds. `byte_seconds`: held x
    seconds, accrued ONLY while the service waits on the client (the service's
    own waits are not charged). `evicted` resolves when the body is
    preempted; `receiving` is False once the body is complete. Fix wave 25,
    H4: the seconds charged are the loop's running time (`lag`: time the event
    loop was behind is not the client's)."""

    __slots__ = ("held", "byte_seconds", "waiting_since", "lost_since", "lag", "receiving", "evicted")

    def __init__(self, loop: asyncio.AbstractEventLoop, lag: "http_limits.LoopLag | None" = None) -> None:
        self.held = 0
        self.byte_seconds = 0.0
        self.waiting_since: float | None = None
        self.lost_since = 0.0
        self.lag = lag
        self.receiving = True
        self.evicted: asyncio.Future = loop.create_future()

    def _lost(self) -> float:
        return self.lag.lost if self.lag is not None else 0.0

    def charge(self, now: float) -> float:
        if self.waiting_since is None:
            return self.byte_seconds
        return self.byte_seconds + self.held * max(0.0, now - self.waiting_since - (self._lost() - self.lost_since))

    def client_wait_started(self, now: float) -> None:
        self.waiting_since = now
        self.lost_since = self._lost()

    def client_wait_ended(self, now: float) -> None:
        self.byte_seconds = self.charge(now)
        self.waiting_since = None


class _BodyHold:
    """Fix wave 24, F1 (AEGIS N23-S-1): the in-flight budget bytes ONE request
    body holds — every byte received so far, each in exactly one pool: the
    first _SMALL_BODY_BYTES of the body in the small reserve, the rest in the
    shared pool (charged to the body's account, so they count for
    preemption). `cover(total)` reserves the bytes up to `total`, which are
    ALREADY in memory: while it waits (503 after _INFLIGHT_WAIT_S, or
    preempted) they are counted in the pool's `over`, and the body is not
    read further. `release()` gives everything back (idempotent). Created by
    BodySizeLimitMiddleware for every request; _off_loop releases it as soon
    as the parse has dropped the body, the middleware again when the request
    ends.

    Fix wave 25, H1 (AEGIS N24-S-1/-2): `grant(target)` reserves AHEAD of the
    bytes received, without waiting — only what the pools have free and only
    while no body is waiting for them (_InFlightBytes.try_reserve) — so the
    protocol can keep reading covered bytes without a round trip through the
    app per 16 KiB read (http_limits.DeadlineH11Protocol.handle_events reads
    `covered` and `taken`); `trim(total)` gives back whatever was reserved past
    `total` once the body is complete. The covered bytes are always a prefix
    of the body: the shared pool is drawn on only once the small reserve covers
    the body's first _SMALL_BODY_BYTES. `taken` is the body bytes the app has
    taken from the protocol, counted in the same task step in which the
    protocol's buffer is emptied (BodySizeLimitMiddleware), so `taken` + the
    bytes the protocol buffers is every body byte that has reached the app."""

    __slots__ = ("lanes", "account", "small", "shared", "taken")

    def __init__(self, lanes: "_Lanes", account: _BodyAccount) -> None:
        self.lanes, self.account = lanes, account
        self.small = 0
        self.shared = 0
        self.taken = 0

    @property
    def covered(self) -> int:
        return self.small + self.shared

    async def cover(self, total: int) -> None:
        small = min(total, _SMALL_BODY_BYTES)
        shared = total - small
        if small > self.small:
            await self.lanes.small_reserve.reserve(small - self.small, in_hand=True)
            self.small = small
        if shared > self.shared:
            await self.lanes.inflight.reserve(shared - self.shared, self.account, in_hand=True)
            self.shared = shared

    def grant(self, target: int) -> None:
        small = min(target, _SMALL_BODY_BYTES)
        if small > self.small:
            self.small += self.lanes.small_reserve.try_reserve(small - self.small)
        if self.small < _SMALL_BODY_BYTES:
            return                     # a prefix: nothing from the shared pool before the first 64 KiB is covered
        shared = target - _SMALL_BODY_BYTES
        if shared > self.shared:
            self.shared += self.lanes.inflight.try_reserve(shared - self.shared, self.account)

    def trim(self, total: int) -> None:
        small, shared = min(total, _SMALL_BODY_BYTES), max(0, total - _SMALL_BODY_BYTES)
        if self.shared > shared:
            self.lanes.inflight.release(self.shared - shared, self.account)
            self.shared = shared
        if self.small > small:
            self.lanes.small_reserve.release(self.small - small)
            self.small = small

    def release(self) -> None:
        if self.shared:
            self.lanes.inflight.release(self.shared, self.account)
            self.shared = 0
        if self.small:
            self.lanes.small_reserve.release(self.small)
            self.small = 0


# Fix wave 7 (NEW-4 sweep): a refusal decided from the request head alone
# (413 from Content-Length, 431, 400 bad Content-Length) costs the loop ~1 ms
# — accept, parse the head, answer, close — and nothing else, so a client
# that loops on it (the AEGIS probe with a 4.39 MB body: 3 944 attempts in
# 10 s from 8 senders, each answered 413 before auth) got ~400 attempts/s
# through and legit small requests went from 4 to 45 ms p50. The answer is
# held for _HEAD_REFUSAL_DELAY_S first: it costs the refused connection a
# slot (bounded by uvicorn's concurrency limit, the fix wave 5 trade-off)
# and caps such a client at 4 attempts/s per connection. Same probe after:
# legit p50 unchanged from baseline.
_HEAD_REFUSAL_DELAY_S = 0.25


def _refusal(code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail}, headers={"Connection": "close"})


async def _refuse_from_head(response: JSONResponse, scope: Scope, receive: Receive, send: Send) -> None:
    await asyncio.sleep(_HEAD_REFUSAL_DELAY_S)
    await response(scope, receive, send)


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        content={"detail": f"request body exceeds {_MAX_BODY_BYTES} bytes; split the batch"},
        headers={"Connection": "close"},
    )


class BodySizeLimitMiddleware:
    """413 from Content-Length before a byte of the body is read, and — for a
    chunked body or a lying client — as soon as the bytes received pass the
    limit while streaming. Runs before auth: refusing an oversized body costs
    nothing, and reading it is exactly the cost being refused.

    Fix wave 5, NEW-3: also 431 for a request head over _MAX_HEADER_BYTES
    (a re-check; `python3 -m api` refuses it in the parser first), and 408
    when the body has not fully arrived _BODY_READ_TIMEOUT_S after the
    request reached the app (the protocol in http_limits.py closes the
    connection shortly after, even if the app never reads the body)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        head = len(scope.get("raw_path") or b"") + len(scope.get("query_string") or b"")
        head += sum(len(k) + len(v) + 4 for k, v in scope["headers"])
        if head > _MAX_HEADER_BYTES:
            await _refuse_from_head(_refusal(431, f"request head exceeds {_MAX_HEADER_BYTES} bytes"), scope, receive, send)
            return
        limit = _MAX_BODY_BYTES
        declared: int | None = None
        for name, value in scope["headers"]:
            if name == b"content-length":
                if not value.isdigit():
                    await _refuse_from_head(_refusal(400, "invalid Content-Length"), scope, receive, send)
                    return
                if int(value) > limit:
                    await _refuse_from_head(_too_large_response(), scope, receive, send)
                    return
                declared = int(value)
        received = 0
        waited = 0.0  # time spent waiting for the client's bytes: the throughput rule's clock
        gap = 0.0  # of which, since the last chunk arrived
        pending: asyncio.Future | None = None  # one receive() in progress, kept across timeouts (never cancelled and re-issued)
        started = False
        body_done = False
        loop = asyncio.get_running_loop()
        budget = _BODY_READ_TIMEOUT_S
        entered = loop.time()
        deadline = entered + budget
        rate, grace = _BODY_MIN_BYTES_PER_S, _BODY_MIN_RATE_GRACE_S
        disconnected = False  # the client's http.disconnect reached the app (fix wave 10, N9-8)
        # Fix wave 25, H4 (AEGIS N24-S-12): every rule below that judges the
        # client by time — (a) stall, (b) trickle, (c) arrival, the preemption
        # charge — runs on the time spent waiting for the client's bytes MINUS
        # the time the event loop was behind meanwhile (http_limits.LoopLag):
        # bytes a client sent while this process could not run are not a stall.
        # The wall-clock deadline stays the hard bound.
        lag = http_limits.loop_lag(loop)
        lag.hold()
        account = _BodyAccount(loop, lag)
        scope[_ACCOUNT_SCOPE_KEY] = account
        # Fix wave 24, F1: every body byte this request holds is counted in the
        # one in-flight budget as it arrives, and the body is not read further
        # while the budget cannot cover it (_BodyHold, limited_receive below).
        lanes = _lanes()
        hold = _BodyHold(lanes, account)
        scope[_HOLD_SCOPE_KEY] = hold
        lanes.inflight.accounts.add(account)
        # Fix wave 25, H1: the most of this body there can be (a declared length
        # is exact: h11 reads no more as body), the end of every read-ahead grant.
        body_cap = limit if declared is None else declared

        async def taking_receive() -> Message:
            # Fix wave 25, H1: before asking, reserve what is free of the next
            # _READ_GRANT_BYTES (never waiting), so the protocol reads covered
            # bytes without a round trip through here per read; and again in the
            # same task step in which uvicorn empties its buffer into this message
            # (its receive() resumes reading before it returns what was buffered,
            # so the protocol finds the next window covered when it reads next).
            # `taken` moves in that same step: no body byte is ever in neither count.
            hold.grant(min(hold.taken + _READ_GRANT_BYTES, body_cap))
            message = await receive()
            if message["type"] == "http.request":
                hold.taken += len(message.get("body", b""))
                if message.get("more_body", False):
                    hold.grant(min(hold.taken + _READ_GRANT_BYTES, body_cap))
            return message

        def drop_pending() -> None:
            nonlocal pending
            if pending is not None:
                pending.cancel()
                pending = None

        async def limited_receive() -> Message:
            nonlocal received, body_done, waited, gap, pending, disconnected
            if body_done:
                message = await receive()  # e.g. waiting for http.disconnect: no deadline
                disconnected = disconnected or message["type"] == "http.disconnect"
                return message
            try:
                while True:
                    now = loop.time()
                    if account.evicted.done():
                        raise _BodyPreempted(account.byte_seconds)
                    if now >= deadline:
                        raise _BodyTimeout()
                    # (a) a stall: no chunk for `grace` of waiting; (b) a trickle: by the
                    # time `waited` reaches `due` the client must have delivered
                    # rate x due bytes (`due` moves out as bytes arrive).
                    due = max(grace, received / rate)
                    if gap >= grace or waited >= due:
                        raise _BodyTooSlow()
                    # (c) fix wave 9: the rest of a declared body cannot arrive by
                    # the deadline at the rate seen so far. Fix wave 10, N9-6: ONE
                    # clock, `waited` (time spent waiting on the client), for the
                    # rate AND the time: `waited` + the waiting the rest needs at
                    # that rate, against _BODY_READ_TIMEOUT_S — the most waiting
                    # any body can get, since waiting only happens before the
                    # deadline. It used to compare `now + rest / seen` with the
                    # wall-clock `deadline`: time the SERVICE held the body
                    # (admission, in-flight bytes) left the wall budget but not the
                    # rate's denominator, while the client's bytes sent meanwhile
                    # sat unread — a client fast enough was refused 408 "send
                    # faster" at the end of every service hold. Not wall time for
                    # both: the rule judges the client, and every other rule that
                    # judges the client ((a), (b), the preemption charge) is on
                    # this clock. The wall-clock deadline above stays the hard bound.
                    if declared is not None and waited >= grace and received < declared:
                        seen = received / waited
                        if seen <= 0 or waited + (declared - received) / seen > budget:
                            raise _BodyWontArrive(declared, received, seen)
                    if pending is None:
                        pending = asyncio.ensure_future(taking_receive())
                    account.client_wait_started(now)
                    lost0 = lag.lost
                    try:
                        done, _ = await asyncio.wait({pending, account.evicted}, return_when=asyncio.FIRST_COMPLETED,
                                                     timeout=min(deadline - now, grace - gap, due - waited))
                    finally:
                        account.client_wait_ended(loop.time())
                    spent = max(0.0, loop.time() - now - (lag.lost - lost0))  # fix wave 25, H4: not the loop's delay
                    waited += spent
                    gap += spent
                    if pending in done:
                        break
                message = pending.result()
                pending = None
                gap = 0.0
            except BaseException:
                drop_pending()
                raise
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
                # Fix wave 24, F1: the chunk is in this process now, so it is
                # counted now (the pool's `over` while it waits to be covered) and
                # the body is not read further until the budget covers it: the
                # wait (service time, not the client's) happens before the next
                # read. 503 after _INFLIGHT_WAIT_S, or 408 if this body is
                # preempted (_InFlightBytes.reserve).
                # Fix wave 25, H1: never past the body deadline — a wait that would
                # run past it is cut there (408), so every body is answered by the
                # deadline however many budget waits it met (each <= 2 s).
                left = deadline - loop.time()
                if left < _INFLIGHT_WAIT_S:
                    try:
                        await asyncio.wait_for(hold.cover(received), max(0.0, left))
                    except TimeoutError:
                        raise _BodyTimeout() from None
                else:
                    await hold.cover(received)
                if not message.get("more_body", False):
                    body_done = True
                    account.receiving = False  # complete: never preempted from here on
                    hold.trim(received)        # fix wave 25: a grant past the body's end goes back now
            else:
                body_done = True
                disconnected = disconnected or message["type"] == "http.disconnect"
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            try:
                await self.app(scope, limited_receive, tracking_send)
            except ClientDisconnect:
                # Fix wave 10, N9-8: a client that goes away mid-body is routine, not
                # a server error — it used to escape as "Exception in ASGI
                # application" with a ~60-line traceback per disconnect. One line,
                # no traceback; nothing to answer. Only when the disconnect really
                # reached the app: any other ClientDisconnect is a bug and propagates.
                if not disconnected:
                    raise
                _log.warning("client disconnected mid-body: route=%s bytes_received=%d declared=%s elapsed_s=%.3f",
                             _log_safe(scope.get("path", "")), received,
                             "none" if declared is None else declared, loop.time() - entered)
            except _BodyTooLarge:
                if started:
                    raise
                await _too_large_response()(scope, receive, send)
            except _BodyTimeout:
                if started:
                    raise
                await _refusal(408, f"request body not received within {_BODY_READ_TIMEOUT_S:g}s")(scope, receive, send)
            except _BodyTooSlow:
                if started:
                    raise
                await _refusal(408, f"request body stalled for {grace:g}s or slower than {rate} bytes/s after {grace:g}s")(scope, receive, send)
            except _BodyWontArrive as exc:
                if started:
                    raise
                await _refusal(408, _wont_arrive_detail(exc))(scope, receive, send)
            except _BodyPreempted as exc:
                if started:
                    raise
                await _refusal(408, _preempted_detail(exc))(scope, receive, send)
        finally:
            hold.release()            # whatever happened: the budget bytes go back (idempotent)
            account.receiving = False
            lanes.inflight.accounts.discard(account)
            lag.drop()


def _log_safe(text: str, limit: int = 200) -> str:
    # The path is the client's (percent-decoded: it can hold a newline).
    return text[:limit].encode("unicode_escape").decode("ascii")


def _wont_arrive_detail(exc: _BodyWontArrive) -> str:
    fits = min(_MAX_BODY_BYTES, int(exc.rate * _BODY_READ_TIMEOUT_S * 0.8))
    return (f"request body of {exc.declared} bytes is arriving at ~{exc.rate:.0f} bytes/s and cannot complete "
            f"within the {_BODY_READ_TIMEOUT_S:g}s body deadline (this size needs >= "
            f"{exc.declared / _BODY_READ_TIMEOUT_S:.0f} bytes/s); split the batch into requests of at most "
            f"~{fits} bytes at this rate, or send faster")


def _preempted_detail(exc: _BodyPreempted) -> str:
    return (f"request body preempted: request bodies share an in-flight budget of {_INFLIGHT_BODY_BYTES} bytes; "
            f"it was contended and this body had held the most of it for longest ({exc.byte_seconds:.0f} "
            f"byte-seconds, over {_PREEMPT_BYTE_SECONDS:.0f}); send faster or split the batch, then retry")


app.add_middleware(BodySizeLimitMiddleware)


# --- off-event-loop request handling (fix wave 4) -----------------------------
# Before: every POST route declared a pydantic body parameter, and FastAPI
# reads, json-decodes AND validates such a body on the event loop — only the
# handler function itself ran in the thread pool — and then serialized the
# response on the event loop too. Now no route declares a body parameter:
# the async route only awaits the (size-limited) body bytes, then ONE thread
# pool call parses + validates (pydantic's JSON parser, in JSON mode), runs
# the agent, and renders the response bytes. Side effect, intended: auth (a
# dependency) now runs before the body is read or parsed; an anonymous
# caller gets 401, not a JSON-parse 422.

_M = TypeVar("_M", bound=BaseModel)


def _is_json_content_type(value: str | None) -> bool:
    if not value:
        return True  # FastAPI's rule: no Content-Type is parsed as JSON
    main = value.split(";", 1)[0].strip().lower()
    return main == "application/json" or (main.startswith("application/") and main.endswith("+json"))


def _render(result: Any) -> Response:
    # The same encoder FastAPI applied to these return values, run here so the
    # json.dumps happens in the worker thread, not on the event loop.
    return JSONResponse(content=jsonable_encoder(result))


def _retained_bytes(obj: Any) -> int:
    """Fix wave 25, H3 (AEGIS N24-S-4): the memory a parsed request model holds
    — every object reachable through its fields, each counted once
    (sys.getsizeof: a str's cached UTF-8 copy included; a datetime's tzinfo),
    plus 1/16 for what no walk reaches (pydantic-core's own per-instance
    allocations). Shared and cached objects (small ints, None, interned
    strings) are counted as if they were the model's. Measured against
    tracemalloc (the allocations validate_json left live), the worst valid
    bodies found: 4 MiB CallEventsRequest of 1000 events whose 4009-char
    transcripts each end in an astral character — counted 22.74 MB, traced
    21.45 MB (5.1x the body); 64 KiB of 200 such events — counted 0.442 MB,
    traced 0.437-0.450 MB (6.7x). 2-3 ms for a 4 MiB model."""
    seen: set[int] = set()
    total = 0
    stack = [obj]
    size = sys.getsizeof
    while stack:
        o = stack.pop()
        if id(o) in seen:
            continue
        seen.add(id(o))
        total += size(o)
        if isinstance(o, BaseModel):
            stack.append(o.__dict__)
            for extra in (o.__pydantic_extra__, o.__pydantic_fields_set__, o.__pydantic_private__):
                if extra is not None:
                    stack.append(extra)
        elif isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
        elif isinstance(o, datetime) and o.tzinfo is not None:
            stack.append(o.tzinfo)
    return total + total // 16


def _parse_measured(model: type[_M], body: bytes | bytearray) -> tuple[_M | Response, int]:
    """_parse, and the bytes the parsed model holds (0 for a refusal) — in the
    same worker-thread call, inside the parse slot (fix wave 25, H3)."""
    parsed = _parse(model, body)
    return parsed, (0 if isinstance(parsed, Response) else _retained_bytes(parsed))


def _parse(model: type[_M], body: bytes | bytearray) -> _M | Response:
    # Parse + validate in a worker thread — and, on a validation failure,
    # render the bounded 422 right here (fix wave 6, N2), never on the loop.
    # Fix wave 7, NEW-4: the cheap shape pre-scan comes first, so a
    # structurally absurd body never reaches the full parse.
    shape = _json_shape(body)
    if shape.violation is not None:
        error_type, msg = shape.violation
        return _validation_error_response([{"loc": ("body",), "type": error_type, "msg": msg}], 1)
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        return _validation_error_response(
            ({"loc": ("body", *e["loc"]), "type": e["type"], "msg": e["msg"]} for e in _first_errors(exc)),
            exc.error_count(),
        )


# --- JSON shape pre-scan (fix wave 7, NEW-4, MED, CONFIRMED) ---------------------
# The full parse (jiter / pydantic-core) materializes every key and item as a
# Python object before any request model can refuse the body: a 3.4 MiB body
# of 255 000 unknown keys cost ~140 ms (300k one-key objects ~230 ms) to be
# told `too_many_fields`. Every route's largest legitimate body has ~19 000
# members (object keys + array items) in ~2 000 objects/arrays, three levels
# deep (tests/test_fix4_limits.py::MAX_BATCHES) — the caps below are sized
# from the batch contract (_MAX_BATCH) with more than 1.5x headroom.
#
# The pre-scan is byte-level and runs in C passes (bytes.translate / replace /
# count / split / join), never a per-token Python loop over the body:
#   1. keep only the bytes that matter (`"` `\` the escape chars `/bfnrtu`,
#      `{}[]:,`) — 4 MiB in ~3 ms, and the result is usually tiny;
#   2. delete `\\` pairs, then `\"`: in valid JSON a backslash is always
#      followed by one of the kept escape chars, so after this every `"` left
#      is a real string boundary (a run of n backslashes before a quote leaves
#      n mod 2, exactly as the escape grammar reads it);
#   3. the even-numbered pieces of a split on `"` are the bytes outside
#      strings (joined with a marker byte standing for each string); commas,
#      braces and brackets are counted there in C;
#   4. depth is walked in Python only over the brace/bracket bytes, and only
#      once the container cap has passed and the brackets balance — at most
#      2 x _MAX_JSON_CONTAINERS iterations.
# Before step 3 the quote count is checked: strings <= 2 x members + 1 in any
# JSON document, so more quotes than 2 x (2 x _MAX_JSON_MEMBERS + 1) is
# already a violation and the split (one bytes object per piece) is never
# made over more than that. Measured on every 4 MiB shape tried (all quotes,
# all backslashes, all escapes, all commas, 1M empty strings, 1M nested
# arrays, 4 MiB of closing brackets, the AEGIS bodies): 1-52 ms, the AEGIS
# bodies 3-11 ms (a loaded 2-core box; every figure is C time over <= 4 MiB).
#
# Exactness: `members` is commas-outside-strings + non-empty containers,
# equal to keys + items unless a container is empty (then an over-count of
# one per empty container; harmless — it only rejects LATER). `containers`
# and `depth` are exact for valid JSON. For invalid JSON the numbers mean
# nothing and the full parse's own `json_invalid` 422 answers, unless the
# pre-scan refuses first — either way a 422. A legitimate body is never
# refused: its strings are removed exactly, so nothing inside them counts.
_MAX_BATCH = 1000
_MAX_JSON_MEMBERS = 32 * _MAX_BATCH      # object keys + array items, whole document
_MAX_JSON_CONTAINERS = 4 * _MAX_BATCH    # objects + arrays, whole document
_MAX_JSON_DEPTH = 32                     # nesting (the root is depth 1); same cap as creative-py
_SHAPE_KEEP = b'"\\/bfnrtu{}[]:,'
_SHAPE_DELETE = bytes(sorted(set(range(256)) - set(_SHAPE_KEEP)))
_NOT_BRACKETS = bytes(sorted(set(range(256)) - set(b"{}[]")))
_OPEN_BRACKETS = frozenset(b"{[")


class _JsonShape:
    __slots__ = ("members", "containers", "depth", "violation")

    def __init__(self, members: int, containers: int, depth: int, violation: tuple[str, str] | None):
        self.members, self.containers, self.depth, self.violation = members, containers, depth, violation

    def __repr__(self) -> str:
        return f"_JsonShape(members={self.members}, containers={self.containers}, depth={self.depth}, violation={self.violation})"


def _json_shape(body: bytes | bytearray) -> _JsonShape:
    reduced = body.translate(None, _SHAPE_DELETE)
    if b"\\" in reduced:
        reduced = reduced.replace(b"\\\\", b"").replace(b'\\"', b"")
    quotes = reduced.count(b'"')
    if quotes > 2 * (2 * _MAX_JSON_MEMBERS + 1):
        return _JsonShape(0, 0, 0, _too_many_members())
    # Every string becomes one `s` (not a kept byte, so unambiguous): `["x"]`
    # must not read as the empty `[]`.
    outside = b"s".join(reduced.split(b'"')[0::2])
    containers = outside.count(b"{") + outside.count(b"[")
    if containers > _MAX_JSON_CONTAINERS:
        return _JsonShape(0, containers, 0, (
            "json_too_many_containers",
            f"JSON body has more than {_MAX_JSON_CONTAINERS} objects and arrays",
        ))
    members = outside.count(b",") + containers - outside.count(b"{}") - outside.count(b"[]")
    if members > _MAX_JSON_MEMBERS:
        return _JsonShape(members, containers, 0, _too_many_members())
    depth = max_depth = 0
    if outside.count(b"}") + outside.count(b"]") != containers:
        return _JsonShape(members, containers, 0, None)  # unbalanced: not JSON; the parser says so at once
    # Balanced, so at most 2 x _MAX_JSON_CONTAINERS bytes to walk here.
    for byte in outside.translate(None, _NOT_BRACKETS):
        if byte in _OPEN_BRACKETS:
            depth += 1
            if depth > max_depth:
                max_depth = depth
                if max_depth > _MAX_JSON_DEPTH:
                    return _JsonShape(members, containers, max_depth, (
                        "json_too_deep",
                        f"JSON body nests deeper than {_MAX_JSON_DEPTH} levels",
                    ))
        else:
            depth -= 1
    return _JsonShape(members, containers, max_depth, None)


def _too_many_members() -> tuple[str, str]:
    return "json_too_many_members", f"JSON body has more than {_MAX_JSON_MEMBERS} members (object keys and array items)"


# --- parse lanes (fix wave 6, N2; fix wave 7, NEW-4) -----------------------------
# Parsing runs in Rust (jiter / pydantic-core) holding the GIL for the whole
# body — the event loop cannot answer /health until it ends — so concurrent
# parses add no throughput, only more fully materialized bodies in memory (a
# 60k-key object is ~10 MB as Python objects; a 4 MiB body of ~260k keys
# ~45 MB) and a longer wait for the loop. Fix wave 6 therefore parsed ONE body
# at a time — and AEGIS (NEW-4) showed the cost: the slot was FIFO, so a
# 1.7 KB legitimate `detect` queued behind every 3.4 MiB junk body ahead of
# it (8 junk senders: p50 1.1 s; 32: 2.9-4.4 s; ~18 s at the concurrency
# limit). Now, size-aware admission:
#
#   small lane  bodies <= _SMALL_BODY_BYTES (64 KiB; the largest legitimate
#               single-record request is a few KB). Its own semaphore of
#               _SMALL_LANE_SLOTS: a small body never queues behind a large
#               one. A 64 KiB body parses in ~1-5 ms whatever it contains
#               (at most 32k members fit), so the lane bounds memory (4 x
#               64 KiB materialized) more than time. No byte budget: the
#               cost per small body is bounded by its size and the pre-scan.
#   large lane  bodies over 64 KiB. They draw on ONE byte budget, a token
#               bucket of _LARGE_BYTES_PER_S (32 MiB/s, burst
#               _LARGE_BURST_BYTES = 16 MiB): a request whose Content-Length
#               says it is large takes its whole size from the bucket BEFORE
#               its body is read, in arrival order (an asyncio.Lock is FIFO),
#               waiting while the bucket refills; a chunked body of unknown
#               length pays per 64 KiB as it streams. Admitted bodies then
#               parse _LARGE_LANE_SLOTS (1) at a time, FIFO. A request that
#               is not admitted within _LARGE_WAIT_S (2 s) of arriving, or
#               does not get the parse slot within _LARGE_WAIT_S of its body
#               being complete (fix wave 9: this window used to run from
#               arrival too, so any large body that took > 2 s to upload
#               was 503'd whole), is answered 503 + Retry-After: 1 (its
#               unread body is drained by uvicorn at the HTTP parser, ~2.5 ms
#               of loop time per 3.4 MiB, and the connection stays usable);
#               it must retry after Retry-After. Declared bytes that never
#               arrive are refunded to the budget (fix wave 9).
#
# Why a byte RATE and not only a slot: receiving a 3.4 MiB body costs the
# event loop ~10-13 ms of its own time (h11 buffering, flow control, copies)
# whatever the body contains, before any parse. Senders that loop on the
# response (the AEGIS probe, or any client that retries at once) send as fast
# as the service answers, so a cheaper refusal alone only raised the attempt
# rate (measured: an immediate 503 for the 9th concurrent large body took 32
# senders from 40 to 230 attempts/s and legit p50 from 36 to 344 ms). The
# budget caps the loop time spent receiving large bodies at ~13% whatever the
# number of senders (32 MiB/s x 13 ms per 3.4 MiB), leaving the loop free for
# small requests and /health; refused attempts cost their drain. Sized for
# legitimate use: 32 MiB/s is 9 maximum 1000-task batches per second, far
# above what the outbound gate lets this service act on.
#
# The guarantee, exactly (ADR 0002, Decision 21): a small body waits for at
# most _SMALL_LANE_SLOTS small parses ahead of it, never for a large one, and
# the loop spends at most the budget's share of its time receiving large
# bodies. What remains shared is the loop and the GIL: while a worker thread
# is inside one C/Rust call the loop waits for it, so a small request can
# still be delayed by one pre-scan or one bounded parse (tens of ms) and by
# the reading of one large body in progress — not by the queue of them. What
# is NOT solved: large bodies have no caller identity to rank on (one shared
# service token), so a legitimate large batch competes FIFO with junk for the
# budget and is 503'd like any other when more than ~2 s of large bodies (at
# 32 MiB/s, ~19 maximum-size ones) are queued ahead of it; it succeeds on a
# retry when its turn comes, and a client that keeps sending large bodies
# keeps everyone's large batches waiting for as long as it does. A flood of
# SMALL junk bodies competes fairly in the small lane (bounded per attempt,
# no budget) and can still load the loop. Requests that are refused early
# cost their body bytes on the wire but not in memory; admitted ones hold
# their bytes (<= 4 MiB each) until parsed. The agent work itself is not
# behind either lane.
_SMALL_BODY_BYTES = 64 * 1024
_SMALL_LANE_SLOTS = 4
_LARGE_LANE_SLOTS = 1
_LARGE_BYTES_PER_S = 32 * 1024 * 1024
_LARGE_BURST_BYTES = 16 * 1024 * 1024
_LARGE_WAIT_S = 2.0
# Idle memory (fix wave 7): the README claimed RSS "settles at 93 MB" after a
# burst of 3.9 MB bodies. Measured, it did not settle: 170 MB after 32 senders
# x 10 s and still 170 MB 15 s later (50 MB before). glibc's mmap threshold is
# dynamic — freeing an mmapped chunk raises it to that chunk's size — so after
# the first large body every later body buffer comes from the brk heap, where
# a freed block below the top is kept for reuse, not returned. That reuse is
# worth keeping (a fixed 128 KiB threshold made every large body ~50% more
# CPU in page faults: 19 vs 13 ms per 3.4 MiB); instead, _TRIM_IDLE_S after
# the last large parse finished, with no large body in flight, malloc_trim(0)
# hands the free pages back (a thread-pool call; ~15 ms for 130 MB, 0 ms when
# there is nothing to trim). Under a sustained flood RSS stays at its bounded
# peak (in-flight bodies + one parse); one idle second later it is back near
# baseline (measured: 50 -> 56 MB).
_TRIM_IDLE_S = 1.0
# Fix wave 8, N7-2 (a fix wave 7 regression): the body buffer was sized from
# Content-Length before a byte had arrived — bytearray(4 MiB), memset, per
# connection — so 128 connections sending a head and ONE byte pinned 512 MiB
# (AEGIS: RSS 55 -> 569 MB) for the 30 s body deadline at no bandwidth. Now
# nothing is allocated ahead of the bytes received: the buffer grows with the
# data (bytearray's own amortized growth, realloc/mremap — none of the grown
# pages are touched until bytes land in them, so resident memory tracks the
# bytes actually received; measured 0.13 ms per 3.4 MiB in 64 KiB chunks,
# against 0.32 ms for the pre-sized-and-memset buffer: the "~3 ms of realloc"
# fix wave 7 was avoiding was not what `+=` costs). (Fix wave 24, F1: "resident
# memory tracks the bytes received" held only roughly — growth headroom and
# realloc copies landing on reused heap pages made it ~1.16x — so the body is
# now kept as the chunks received and joined once in the parse slot; see
# _off_loop.) And the bytes buffered by
# ALL in-flight bodies share one
# budget, _INFLIGHT_BODY_BYTES: a request that cannot buffer its next chunk
# within _INFLIGHT_WAIT_S is answered 503 + Retry-After: 1 (uvicorn drains
# the rest at the parser, as for the large lane). Sized to the large lane's
# 2 s of budget at 32 MiB/s — 64 MiB — the most that can be admitted for
# parsing within the wait anyway; below it 128 x 4 MiB = 512 MiB was the
# only bound (http_limits.LIMIT_CONCURRENCY). A body that stalls after
# sending its bytes is cut by the throughput rule (_BODY_MIN_BYTES_PER_S,
# above) before the deadline, so a stalled sender holds its bytes ~5 s, not
# 30.
#
# Fix wave 24, F1 (AEGIS N23-S-1): the bound held only by allocator luck
# (growth 84-114 MiB against the 96 MiB bound, round 23) because the budget
# counted only part of the body memory: the bytes the app had taken past each
# body's first 64 KiB. Outside it were the small reserve (a separate 8 MiB
# pool), the body bytes uvicorn's protocol buffers before the app reads them
# (up to its 64 KiB high-water mark plus the read that crossed it, per
# connection; tracemalloc: 15.6 MiB in `cycle.body` across the 128-sender
# burst) and the chunk the app had just received while it waited to reserve
# it. Now _INFLIGHT_BODY_BYTES is the WHOLE budget and every request-body
# byte the process holds is in it:
#   - the small reserve is part of it (_SMALL_RESERVE_BYTES of the 64 MiB;
#     the shared pool is the rest, 56 MiB);
#   - a chunk is counted the moment the app receives it — the chunk in hand
#     while its body waits for the budget in the pool's `over` — and the body
#     is not read further until the budget covers it (BodySizeLimitMiddleware
#     via _BodyHold: the wait happens BEFORE the next read; 503 after
#     _INFLIGHT_WAIT_S, preemption as before). So `used` <= the limit and
#     `over` <= one chunk per body waiting;
#   - under the real launcher uvicorn reads a body only when the app asks,
#     one read of at most http_limits.READ_BUFFER_BYTES (16 KiB) at a time, so
#     that chunk is <= 16 KiB and at most one more read sits in uvicorn's
#     buffer per connection;
#   - drained bytes are never held (graceful_close reads them into one shared
#     16 KiB buffer and drops them).
# What is therefore outside the 64 MiB, as a fixed term (ADR 0002, Decision
# 21): <= 16 KiB per body waiting for the budget (<= LIMIT_CONCURRENCY) and
# <= one read of 16 KiB per connection in uvicorn's buffer (<=
# MAX_OPEN_CONNECTIONS) — 2 + 4 MiB at most — plus the per-connection
# objects and the allocator's own overhead, measured (see the ADR).
_INFLIGHT_BODY_BYTES = 64 * 1024 * 1024
_INFLIGHT_WAIT_S = 2.0
# Fix wave 25, H1 (AEGIS N24-S-1/-2): how far ahead of the bytes it has taken a
# body reserves budget before it asks for more (_BodyHold.grant, only from
# free bytes and only while no body waits for the budget). uvicorn's own
# high-water mark: covered bytes stream as they did before wave 24, and the
# memory bound is unchanged (the bytes are reserved BEFORE they are read).
_READ_GRANT_BYTES = 64 * 1024
# Fix wave 9 (AEGIS round 8, Q1). Measured on the real launcher before: 64
# authenticated senders that declared 4 MiB, sent 1 MiB at once and then
# 2 KiB/s — above the 1 KiB/s floor, which credits the front-load for ~1000 s
# — held the whole budget until the 30 s deadline; legit 200-byte `detect`
# p99 1.99 s (waiting on the budget, at the 2 s refusal edge) and legit large
# batches 3 of 7 refused. Plain 2 KiB/s senders (N = 16, 64) starved nobody:
# a body at 2 KiB/s holds <= 60 KiB by the deadline. Three changes:
#
#   small reserve   the first _SMALL_BODY_BYTES of EVERY body are reserved
#                   from their own pool, _SMALL_RESERVE_BYTES =
#                   LIMIT_CONCURRENCY x 64 KiB (8 MiB): under `python3 -m api`
#                   at most LIMIT_CONCURRENCY requests are in flight, so the
#                   pool cannot be exhausted and a small body never waits for
#                   bytes. Only bytes past 64 KiB draw on the shared budget.
#                   (Other launchers have no such limit; there the reserve
#                   can refuse like the shared budget: 503 after the wait.)
#   time-weighted   every body is charged the shared bytes it holds x the
#   charge          seconds the service spent waiting on ITS CLIENT
#                   (_BodyAccount; the service's own waits are not charged).
#                   When a body cannot reserve its next chunk, the body still
#                   being received with the largest charge — if that is at
#                   least _PREEMPT_BYTE_SECONDS and more than the waiter's own
#                   — is cut with 408 and its bytes go to the waiter. 4 MiB·s:
#                   a maximum 4 MiB body arriving at >= 2 MiB/s never reaches
#                   it; nothing is preempted unless someone is waiting.
#   arrival         see _BodyWontArrive (BodySizeLimitMiddleware): a declared
#   projection      body that cannot arrive by the deadline at its observed
#                   rate is refused at the grace, not held to the deadline.
#
# The guarantee, exactly (ADR 0002, Decision 23): a small body (<= 64 KiB)
# never waits for in-flight bytes under the real launcher. A large body is
# refused 503 for in-flight bytes only when every body holding them has
# accrued < 4 MiB·s since it started — so to keep the budget full against
# newcomers, bodies must turn over: N holders of 64 MiB / N each must each
# complete (or be cut) within 4 MiB·s / (64 MiB / N), i.e. an attacker needs
# a sustained 64 MiB x 64 MiB / (N x 4 MiB·s) = 1024 / N MiB/s of real
# upload bandwidth (8 MiB/s at N = 128, 16 MiB/s at N = 64) — the budget is
# bounded by bandwidth spent, not by time held. Limits: (a) a legitimate slow
# large body (a mobile client at a few hundred KiB/s) accrues byte-seconds as
# fast as an attacker's and IS preempted under contention (408, retry or
# split); (b) there is no caller identity (one service token), so this is
# not per-client fairness; (c) connection slots are a separate limit —
# LIMIT_CONCURRENCY (128) held connections, even at 1 KiB/s, still make
# uvicorn answer 503 to everyone (http_limits, fix wave 5 trade-off).
_SMALL_RESERVE_BYTES = http_limits.LIMIT_CONCURRENCY * _SMALL_BODY_BYTES  # part of _INFLIGHT_BODY_BYTES (fix wave 24)
_PREEMPT_BYTE_SECONDS = 4 * 1024 * 1024 * 1.0  # bytes x seconds


class _InFlightBytes:
    """Bytes buffered by request bodies being read, whole process (per loop):
    `reserve` waits (bounded) until `n` more fit — preempting, when an
    account is given, the heaviest preemptible holder (fix wave 9);
    `release` gives them back. Fix wave 25: `try_reserve` takes what is free
    now, never waits, and takes nothing while a reservation is waiting."""

    __slots__ = ("limit", "used", "over", "waiters", "accounts", "blocked")

    def __init__(self, limit: int) -> None:
        self.limit, self.used = limit, 0
        # Fix wave 24, F1: bytes ALREADY in memory waiting to be covered (a
        # chunk the app holds while its body waits for the budget, `in_hand`);
        # `used` <= `limit` always, `used + over` is what the pool's bodies hold.
        self.over = 0
        self.waiters: list[asyncio.Future] = []
        self.accounts: set[_BodyAccount] = set()
        self.blocked = 0  # reservations waiting for bytes (fix wave 25): read-ahead grants never overtake them

    def try_reserve(self, n: int, account: _BodyAccount | None = None) -> int:
        """Fix wave 25, H1: up to `n` bytes if they are free NOW, never
        waiting, and none while any reservation is waiting (a grant ahead of
        the bytes received must not take what a body holding received bytes
        waits for). Returns the bytes reserved (0..n)."""
        if n <= 0 or self.blocked:
            return 0
        got = min(n, self.limit - self.used)
        if got <= 0:
            return 0
        self.used += got
        if account is not None:
            account.held += got
        return got

    async def reserve(self, n: int, account: _BodyAccount | None = None, *, in_hand: bool = False) -> None:
        if n <= 0:
            return
        if in_hand:
            self.over += n
            try:
                await self.reserve(n, account)
            finally:
                self.over -= n
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _INFLIGHT_WAIT_S
        if self.used + n > self.limit:
            self.blocked += 1
            try:
                while self.used + n > self.limit:
                    if account is not None:
                        self._preempt_for(account, loop.time())
                    waiter = loop.create_future()
                    self.waiters.append(waiter)
                    wait_on = {waiter} if account is None else {waiter, account.evicted}
                    try:
                        done, _ = await asyncio.wait(wait_on, timeout=max(0.0, deadline - loop.time()),
                                                     return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        if waiter in self.waiters:
                            self.waiters.remove(waiter)
                    if account is not None and account.evicted.done():
                        raise _BodyPreempted(account.charge(loop.time()))
                    if not done:
                        raise _inflight_refused()
            finally:
                self.blocked -= 1
        self.used += n
        if account is not None:
            account.held += n

    def _preempt_for(self, waiter: _BodyAccount, now: float) -> None:
        victim, heaviest = None, 0.0
        for acc in self.accounts:
            if acc is waiter or not acc.receiving or acc.held <= 0:
                continue
            if acc.evicted.done():
                return  # one preemption at a time: its bytes are on their way back
            charge = acc.charge(now)
            if charge > heaviest:
                victim, heaviest = acc, charge
        if victim is not None and heaviest >= _PREEMPT_BYTE_SECONDS and heaviest > waiter.charge(now):
            victim.evicted.set_result(None)
            _log.warning("in-flight budget contended: preempted a body holding %d bytes (%.0f byte-seconds)",
                         victim.held, heaviest)

    def release(self, n: int, account: _BodyAccount | None = None) -> None:
        self.used -= n
        if account is not None:
            account.held -= n
        waiters, self.waiters = self.waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)


def _inflight_refused() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"request bodies being received share an in-flight budget of {_INFLIGHT_BODY_BYTES} bytes; "
               f"this one could not be buffered within {_INFLIGHT_WAIT_S:g}s — retry after Retry-After",
        headers={"Retry-After": "1"},
    )


class _ByteBudget:
    """Token bucket of bytes, taken in arrival order; the wait for a refill
    happens under the lock so nothing overtakes."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = float(rate), float(burst)
        self.tokens = self.burst
        self.updated: float | None = None
        self.lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        if self.updated is not None:
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def take(self, n: int) -> None:
        loop = asyncio.get_running_loop()
        async with self.lock:
            self._refill(loop.time())
            if self.tokens < n:
                await asyncio.sleep((n - self.tokens) / self.rate)
                self._refill(loop.time())
            self.tokens -= n

    def refund(self, n: int) -> None:
        # Fix wave 9: bytes taken for a declared size that never arrived cost
        # the loop nothing; without the refund, senders cut early (the
        # arrival projection) would drain the budget for the declared size.
        if n > 0:
            self.tokens = min(self.burst, self.tokens + n)


class _Lanes:
    __slots__ = ("small", "large", "budget", "inflight", "small_reserve", "large_in_flight", "trim_timer")

    def __init__(self) -> None:
        self.small = asyncio.Semaphore(_SMALL_LANE_SLOTS)
        self.large = asyncio.Semaphore(_LARGE_LANE_SLOTS)
        self.budget = _ByteBudget(_LARGE_BYTES_PER_S, _LARGE_BURST_BYTES)
        # fix wave 24, F1: ONE budget — the small reserve is carved out of it
        self.small_reserve = _InFlightBytes(_SMALL_RESERVE_BYTES)
        self.inflight = _InFlightBytes(_INFLIGHT_BODY_BYTES - _SMALL_RESERVE_BYTES)
        self.large_in_flight = 0
        self.trim_timer: asyncio.TimerHandle | None = None


_parse_lanes: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Lanes]" = weakref.WeakKeyDictionary()


def _lanes() -> _Lanes:
    loop = asyncio.get_running_loop()  # one set per loop: TestClient makes several
    lanes = _parse_lanes.get(loop)
    if lanes is None:
        lanes = _parse_lanes[loop] = _Lanes()
    return lanes


def _large_refused() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"large request bodies (over {_SMALL_BODY_BYTES} bytes) share a budget of {_LARGE_BYTES_PER_S} bytes/s "
               f"and one parse slot; this one was not admitted within {_LARGE_WAIT_S:g}s — retry after Retry-After",
        headers={"Retry-After": "1"},
    )


async def _within(deadline: float, awaitable: Any) -> None:
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(awaitable, max(0.0, deadline - loop.time()))
    except TimeoutError:
        raise _large_refused() from None


def _schedule_trim(lanes: _Lanes) -> None:
    loop = asyncio.get_running_loop()
    if lanes.trim_timer is not None:
        lanes.trim_timer.cancel()
    lanes.trim_timer = loop.call_later(_TRIM_IDLE_S, _trim_if_idle, loop, lanes)


def _trim_if_idle(loop: asyncio.AbstractEventLoop, lanes: _Lanes) -> None:
    lanes.trim_timer = None
    if lanes.large_in_flight == 0:
        loop.run_in_executor(None, _malloc_trim)


def _declared_length(request: Request) -> int | None:
    value = request.headers.get("content-length")
    return int(value) if value and value.isdigit() else None  # validated by BodySizeLimitMiddleware


async def _off_loop(request: Request, model: type[_M], work: Callable[[_M], Any]) -> Response:
    if not _is_json_content_type(request.headers.get("content-type")):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="expected application/json")
    lanes = _lanes()
    loop = asyncio.get_running_loop()
    declared = _declared_length(request)
    large = declared is not None and declared > _SMALL_BODY_BYTES
    if large:
        # before a byte of the body is read
        await _within(loop.time() + _LARGE_WAIT_S, lanes.budget.take(declared))
    lanes.large_in_flight += large
    hold: _BodyHold | None = request.scope.get(_HOLD_SCOPE_KEY)
    pos = 0
    chunks: list[bytes] | None = []
    parsed: Any = None
    try:
        # Fix wave 8, N7-2: nothing is allocated ahead of the bytes received
        # (NEVER sized from Content-Length, see _INFLIGHT_BODY_BYTES). Fix wave
        # 24, F1: every byte is counted in the in-flight budget as it arrives,
        # and the body is not read further while the budget cannot cover it
        # (BodySizeLimitMiddleware's _BodyHold — installed on `app`, so every
        # launcher has it). The chunks are KEPT AS RECEIVED (uvicorn's bytes
        # objects, exact size) and joined once, inside the parse slot: a
        # growing bytearray (the wave-8..23 buffer) realloc'd as the body
        # arrived, and its 12.5% growth headroom plus the copies left behind
        # in reused heap pages made resident memory ~1.16x the bytes counted
        # (measured, ADR 0002 "Fix wave 24") — memory the budget did not see.
        # The join costs one copy of ONE body at a time (the large lane has one
        # slot; small bodies are <= 64 KiB), a fixed term. Bounded by
        # BodySizeLimitMiddleware.
        charged = 0
        async for chunk in request.stream():
            chunks.append(chunk)
            pos += len(chunk)
            if not large and pos > _SMALL_BODY_BYTES:
                lanes.large_in_flight += 1
                large = True
            if large and declared is None and pos - charged >= _SMALL_BODY_BYTES:
                # unknown length: pay as it streams. Fix wave 9: each payment
                # gets its own admission window — the window used to run from
                # the request's arrival, so any chunked large body still
                # streaming 2 s after it arrived was refused 503.
                await _within(loop.time() + _LARGE_WAIT_S, lanes.budget.take(pos - charged))
                charged = pos
        lane = lanes.large if large else lanes.small
        if large:
            # Fix wave 9: the wait for the parse slot is measured from now,
            # when the body is complete — it used to be measured from the
            # request's arrival, so every large body that took more than
            # _LARGE_WAIT_S (2 s) to upload was refused 503 after arriving
            # whole (live: a 4 MiB body at 160 KiB/s, 503 at 26 s).
            await _within(loop.time() + _LARGE_WAIT_S, lane.acquire())
        else:
            await lane.acquire()
        try:
            body = b"".join(chunks)
            chunks = None                     # one copy from here on
            parsed, retained = await run_in_threadpool(_parse_measured, model, body)
            body = None
            if hold is not None and retained:
                # Fix wave 25, H3 (AEGIS N24-S-4): the parsed model is counted in
                # the budget from here until it is dropped — its measured size, in
                # place of the body's bytes (the joined copy is gone). A model larger
                # than the body (a 4 MiB body of transcripts each holding one astral
                # character is a 20 MiB model: CPython stores the whole string at 4
                # bytes a character, plus its UTF-8 copy) waits for the budget like
                # a chunk in hand — INSIDE the parse slot, so at most one model per
                # slot is ever uncounted; 503 after _INFLIGHT_WAIT_S. Models of
                # consecutive requests coexist while their work runs (the slot is
                # released before it): each is counted.
                try:
                    await hold.cover(retained)
                except BaseException:
                    parsed = None             # dropped now, not when the refusal's traceback goes
                    raise
                hold.trim(retained)
        finally:
            body = None  # noqa: F841
            lane.release()
    except BaseException:
        parsed = None
        raise
    finally:
        # Fix wave 24, F1: the chunks are dropped HERE, before the budget bytes
        # go back — rebinding the name frees them even when a traceback still
        # references this frame (a refusal raised above keeps the frame alive
        # while the middleware answers).
        chunks = None
        if hold is not None and not isinstance(parsed, BaseModel):
            hold.release()                    # no model: nothing more is held (a model's bytes go after its work)
        if large and declared is not None and declared > _SMALL_BODY_BYTES:
            lanes.budget.refund(declared - pos)  # declared but never received (fix wave 9)
        if large:
            lanes.large_in_flight -= 1
            _schedule_trim(lanes)
    if isinstance(parsed, Response):
        return parsed
    try:
        return await run_in_threadpool(lambda: _render(work(parsed)))
    finally:
        parsed = None                         # fix wave 25, H3: the model is dropped, then its bytes go back
        if hold is not None:
            hold.release()


# --- bounded 422 (fix wave 6, N2, MED, CONFIRMED) -----------------------------
# Sep 24 2026 audit (A8) stopped the 422 body echoing each error's `input`,
# but it still listed EVERY error in full. Against an `extra="forbid"` request
# model, a 600 KB body of 60 000 unknown keys produced 60 000
# `extra_forbidden` errors — a 5.4 MB response, built and serialized on the
# event loop; 20 concurrent senders took RSS from 52 to 547 MB and /health
# to 0.9 s. A single 1 MiB unknown key was echoed whole inside its `loc`.
#
# Now the 422 body is bounded whatever the request was: at most
# _MAX_REPORTED_ERRORS errors are listed (first ones, in pydantic's order)
# plus the honest total `error_count` (and `truncated: true` when they
# differ); each `loc` is cut to _MAX_LOC_DEPTH items of at most
# _MAX_LOC_ITEM_CHARS characters; `type` and `msg` are bounded; and the
# serialized body is kept under _MAX_422_BODY_BYTES by dropping trailing
# errors if the caps alone were not enough. The request models refuse a body
# with more unknown top-level keys than can be named with ONE error (see
# _Req) so pydantic never enumerates them. Anything FastAPI itself raises
# (RequestValidationError) renders through the same builder, in a sync
# handler, i.e. in the thread pool.
_MAX_REPORTED_ERRORS = 20
_MAX_LOC_DEPTH = 8
_MAX_LOC_ITEM_CHARS = 40
_MAX_TYPE_CHARS = 64
_MAX_MSG_CHARS = 200
_MAX_422_BODY_BYTES = 8 * 1024
_ELLIPSIS = "..."


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _bounded_loc(loc: Any) -> list[Any]:
    items = list(loc) if isinstance(loc, (list, tuple)) else [loc]
    out: list[Any] = [item if isinstance(item, int) and not isinstance(item, bool) else _clip(item, _MAX_LOC_ITEM_CHARS)
                      for item in items[:_MAX_LOC_DEPTH]]
    if len(items) > _MAX_LOC_DEPTH:
        out.append(_ELLIPSIS)
    return out


def _first_errors(exc: ValidationError) -> list[dict]:
    # `errors()` materializes every error (with `input`, a copy of the
    # offending value, unless told not to). The count is bounded upstream
    # (_Req caps unknown keys; every list/map is <= _MAX_BATCH), so this is
    # at most ~10k small dicts; only the first _MAX_REPORTED_ERRORS go on.
    return exc.errors(include_url=False, include_context=False, include_input=False)[:_MAX_REPORTED_ERRORS]


def _bounded_validation_body(errors: Any, total: int) -> bytes:
    detail = []
    for e in errors:
        if len(detail) >= _MAX_REPORTED_ERRORS:
            break
        detail.append({
            "loc": _bounded_loc(e.get("loc") or ()),
            "type": _clip(e.get("type") or "", _MAX_TYPE_CHARS),
            "msg": _clip(e.get("msg") or "", _MAX_MSG_CHARS),
        })
    total = max(int(total), len(detail))
    while True:
        content: dict[str, Any] = {"detail": detail, "error_count": total}
        if total > len(detail):
            content["truncated"] = True
        body = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) <= _MAX_422_BODY_BYTES or len(detail) <= 1:
            return body
        detail.pop()


def _validation_error_response(errors: Any, total: int) -> Response:
    return Response(
        content=_bounded_validation_body(errors, total),
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        media_type="application/json",
    )


@app.exception_handler(RequestValidationError)
def _validation_error_without_input(request: Request, exc: RequestValidationError) -> Response:
    # Sep 24 2026 audit: FastAPI's default 422 body echoes each error's
    # `input` (and `ctx`). For a missing field, `input` is the whole
    # submitted object — so a malformed call event echoed the caller's
    # phone number and voicemail transcript back in the error, and into
    # any proxy or client log that records error bodies. Keep only
    # location, type and message: enough to fix the request, no payload.
    # Fix wave 6: bounded (above), and a sync handler so Starlette runs it
    # in the thread pool. Bodies of the POST routes no longer come through
    # here (_parse answers them directly); this covers anything
    # FastAPI itself raises.
    errors = exc.errors()
    return _validation_error_response(errors, len(errors))


def _build_dialer() -> SipDialerPort:
    # Independent review finding (Sep 22 2026, CRITICAL, CONFIRMED): this
    # previously hardcoded InMemorySipDialer() — a TEST DOUBLE — as the
    # live API's dialer, so every callback request got back
    # dial_placed=true for a call that was never placed. Default is now
    # the honest NotWiredSipDialer; FULFILLMENT_SIP_DIALER=in_memory is
    # an explicit, named opt-in for local demos only.
    if os.environ.get("FULFILLMENT_SIP_DIALER") == "in_memory":
        return InMemorySipDialer()
    return NotWiredSipDialer()


def _build_system_of_record() -> SystemOfRecordPort:
    # Same finding as above, for write-back: default is the honest
    # NotConfiguredSystemOfRecord; FULFILLMENT_SYSTEM_OF_RECORD=in_memory
    # is the explicit opt-in for local demos only.
    if os.environ.get("FULFILLMENT_SYSTEM_OF_RECORD") == "in_memory":
        return InMemorySystemOfRecord()
    return NotConfiguredSystemOfRecord()


def _load_contact_window() -> ContactWindow:
    # Sep 24 2026 audit: outbound-contact quiet hours, recipient local time.
    # Narrowable via env, never widenable past 08:00-21:00; a malformed or
    # too-wide value refuses startup (fail closed), same as a missing token.
    raw = os.environ.get("FULFILLMENT_CONTACT_WINDOW")
    if raw is None:
        return ContactWindow.default()
    try:
        return parse_contact_window(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"FULFILLMENT_CONTACT_WINDOW is invalid ({exc}). Expected HH:MM-HH:MM "
            "inside 08:00-21:00. This service refuses to start rather than guess."
        ) from None


_CONTACT_WINDOW: ContactWindow = _load_contact_window()


def _load_attempt_limits() -> AttemptLimits:
    # Fix wave 1, F3: per-number/per-customer attempt limits. Narrow-only,
    # like the contact window; a bad value refuses startup.
    try:
        return parse_attempt_limits(
            os.environ.get("FULFILLMENT_CONTACT_MAX_ATTEMPTS_PER_24H"),
            os.environ.get("FULFILLMENT_CONTACT_MIN_SPACING_MINUTES"),
        )
    except ValueError as exc:
        raise RuntimeError(
            f"FULFILLMENT_CONTACT_MAX_ATTEMPTS_PER_24H / FULFILLMENT_CONTACT_MIN_SPACING_MINUTES "
            f"invalid ({exc}). This service refuses to start rather than guess."
        ) from None


def _load_country_zones() -> dict[str, tuple[str, ...]]:
    # Fix wave 1, F3: non-+1 numbers are never contacted unless a zone rule
    # for their country code is configured here. A bad value refuses startup.
    raw = os.environ.get("FULFILLMENT_COUNTRY_ZONES")
    if not raw:
        return {}
    try:
        return parse_country_zones(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"FULFILLMENT_COUNTRY_ZONES is invalid ({exc}). Expected e.g. "
            "'44=Europe/London;61=Australia/Perth,Australia/Sydney'. This service refuses to start."
        ) from None


def _now() -> datetime:
    """The ONLY clock the quiet-hours check sees. Sep 24 2026 audit: the
    orchestrate route used to accept `now` from the request body, letting
    any caller evaluate quiet hours against a time of its choosing. Tests
    monkeypatch this function instead."""
    return datetime.now(timezone.utc)


def _build_gate() -> OutboundContactGate:
    # The clock is looked up on every read (not bound once), so the gate
    # always reads the current module-level _now — per task, at dial time.
    return OutboundContactGate(
        window=_CONTACT_WINDOW,
        limits=_load_attempt_limits(),
        clock=lambda: _now(),
        country_zones=_load_country_zones(),
    )


# Fix wave 1, F3: the single place automated outbound contact is authorized.
_GATE: OutboundContactGate = _build_gate()


_dialer: SipDialerPort = _build_dialer()
_system_of_record: SystemOfRecordPort = _build_system_of_record()

# In-memory state. The agent work runs in the thread pool (_off_loop), so
# every read-modify-write below is under a lock (Sep 24 2026 audit: four
# concurrent dossier updates kept one customer and silently dropped three).
_dossiers: dict[str, CustomerDossier] = {}
_dossiers_lock = threading.Lock()
# Fix wave 1: the dossier store grew without limit (every customer ever
# seen, every call/appointment id). It is a record, not a 24h window, so
# nothing is evicted: at a cap the update is refused whole (503, nothing
# applied) rather than silently truncating a customer's history.
_MAX_DOSSIERS = 100_000
_MAX_DOSSIER_HISTORY = 10_000  # per dossier, per list (calls, appointments, numbers)
# Dedupe state (process lifetime, lost on restart — no datastore). Fix
# wave 1: both stores used to be a plain set/dict that grew forever. Each is
# now a BoundedExpiringMap: entries older than 24h are evicted (the gate's
# per-number limit is the real redial protection over any longer span), and
# at the hard cap with nothing expired the route FAILS CLOSED — no dial, no
# write-back — instead of forgetting what it already did.
_DEDUPE_TTL = timedelta(hours=24)
_DEDUPE_MAX_ENTRIES = 100_000


def _new_dedupe(max_entries: int = _DEDUPE_MAX_ENTRIES) -> BoundedExpiringMap:
    return BoundedExpiringMap(ttl=_DEDUPE_TTL, max_entries=max_entries)


# task_id -> True for task ids this process has already handed to the dialer
# (closes README gap 6 for 24h within the life of the process).
_attempted_task_ids: BoundedExpiringMap[str, bool] = _new_dedupe()
_dial_lock = threading.Lock()
# exhausted-escalation task_id -> the NO_RESOLUTION record already made
# for it, so a retried request returns the same record.
_exhausted_resolutions: BoundedExpiringMap[str, ResolutionRecord] = _new_dedupe()
_exhausted_lock = threading.Lock()

# _MAX_BATCH (1000) is defined with the JSON shape caps above.
TimezoneName = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class _Req(BaseModel):
    # Unknown fields are an error, not silently ignored — this is what
    # makes a stale client still sending `now` get a 422 instead of
    # believing its clock override worked.
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _refuse_unnameable_unknown_keys(cls, data: Any) -> Any:
        # Fix wave 6, N2: `extra="forbid"` reports one error PER unknown key,
        # so a body of 60 000 unknown keys made pydantic build 60 000
        # errors. Up to _MAX_REPORTED_ERRORS unknown keys are still named
        # (the 422 lists that many); beyond that the object is refused with
        # one error before any field is looked at.
        if isinstance(data, dict) and len(data) > len(cls.model_fields) + _MAX_REPORTED_ERRORS:
            raise PydanticCustomError(
                "too_many_fields",
                "object has {actual} keys; this request accepts at most {allowed} fields",
                {"actual": len(data), "allowed": len(cls.model_fields)},
            )
        return data


def _refuse_oversized_map(value: Any) -> Any:
    # Fix wave 6, N2: pydantic checks a LIST's max_length before validating
    # its items (one error), but validates every entry of a dict first — a
    # 4 MiB map of ~250 000 bad phone numbers produced 250 000 errors before
    # `too_long`. Refuse the size first, with pydantic's own error type.
    if isinstance(value, dict) and len(value) > _MAX_BATCH:
        raise PydanticCustomError(
            "too_long",
            "Dictionary should have at most {max_length} items after validation, not {actual_length}",
            {"field_type": "Dictionary", "max_length": _MAX_BATCH, "actual_length": len(value)},
        )
    return value


_K = TypeVar("_K")
_V = TypeVar("_V")
BoundedMap = Annotated[dict[_K, _V], BeforeValidator(_refuse_oversized_map)]


class CallEventsRequest(_Req):
    call_events: list[CallEvent] = Field(max_length=_MAX_BATCH)


class AppointmentsRequest(_Req):
    appointments: list[Appointment] = Field(max_length=_MAX_BATCH)


class TasksResponse(BaseModel):
    tasks: list[FollowUpTask]


class DossierUpdateRequest(_Req):
    call_events: list[CallEvent] = Field(default_factory=list, max_length=_MAX_BATCH)
    appointments: list[Appointment] = Field(default_factory=list, max_length=_MAX_BATCH)


class DossiersResponse(BaseModel):
    dossiers: list[CustomerDossier]


class EscalateRequest(_Req):
    task: FollowUpTask


class OrchestrateRequest(_Req):
    tasks: list[FollowUpTask] = Field(max_length=_MAX_BATCH)
    phone_by_call_id: BoundedMap[EntityId, PhoneE164] = Field(max_length=_MAX_BATCH)
    line_by_call_id: BoundedMap[EntityId, EntityId] = Field(default_factory=dict, max_length=_MAX_BATCH)
    # Recipient IANA time zone per call (e.g. "America/Chicago"). A call
    # with no entry here is not dialed — fail closed. Fix wave 1, F3: this
    # is a CLAIM, checked against the number by the gate (recipient_zones.py);
    # it can narrow the window, never widen it.
    timezone_by_call_id: BoundedMap[EntityId, TimezoneName] = Field(default_factory=dict, max_length=_MAX_BATCH)


class TerminalEventIn(_Req):
    entity_type: Literal["call", "appointment", "task"]
    entity_id: TaskId
    customer_id: EntityId | None = None
    resolution_type: ResolutionType


class ResolveRequest(_Req):
    events: list[TerminalEventIn] = Field(max_length=_MAX_BATCH)


class ResolveResponse(BaseModel):
    records: list[ResolutionRecord]


@app.get("/health")
async def health() -> dict:
    # A coroutine on purpose (fix wave 4): it does no work, so it must not
    # queue for a thread-pool slot behind batch requests.
    return {"status": "ok", "service": "fulfillment-py", "data_source": "non-live"}


# --- fixture endpoints (dev/test only, explicitly labeled) -----------------

@app.get("/fixtures/call-events", dependencies=[Depends(require_auth)])
def fixtures_call_events() -> list[CallEvent]:
    return load_call_events()


@app.get("/fixtures/appointments", dependencies=[Depends(require_auth)])
def fixtures_appointments() -> list[Appointment]:
    return load_appointments()


@app.get("/fixtures/dossiers", dependencies=[Depends(require_auth)])
def fixtures_dossiers() -> list[CustomerDossier]:
    return list(load_dossiers().values())


# --- agent endpoints ---------------------------------------------------------

@app.post("/agents/missed-call-detection/detect", dependencies=[Depends(require_auth)])
async def detect_missed_calls(request: Request) -> Response:
    return await _off_loop(request, CallEventsRequest, _detect_missed_calls)


def _detect_missed_calls(req: CallEventsRequest) -> TasksResponse:
    return TasksResponse(tasks=missed_call_detection.detect(req.call_events))


@app.post("/agents/appointment-tracking/detect", dependencies=[Depends(require_auth)])
async def detect_overdue_appointments(request: Request) -> Response:
    return await _off_loop(request, AppointmentsRequest, _detect_overdue_appointments)


def _detect_overdue_appointments(req: AppointmentsRequest) -> TasksResponse:
    return TasksResponse(tasks=appointment_tracking.find_overdue(req.appointments))


@app.post("/agents/followup-sequencing/escalate", dependencies=[Depends(require_auth)])
async def escalate_task(request: Request) -> Response:
    return await _off_loop(request, EscalateRequest, _escalate_task)


def _escalate_task(req: EscalateRequest) -> dict:
    try:
        result = followup_sequencing.escalate(req.task)
    except ValueError:
        # Sep 24 2026 audit: escalate() raises on a non-FAILED task by
        # design; that was unhandled here and surfaced as a 500.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"only FAILED tasks escalate; task status is {req.task.status.value!r}",
        ) from None
    if result is not None:
        return {"next_task": result.model_dump(), "sequence_exhausted": False, "resolution": None}

    # Independent review finding (Sep 22 2026, CONFIRMED): a failed
    # human_handoff previously vanished here — next_task: null and
    # nothing else. Now: when the sequence is genuinely exhausted, that
    # is itself recorded as a resolution (NO_RESOLUTION — explicit, not
    # silence) and an attempted write-back, same as any other terminal
    # event, so it shows up wherever resolution records are reviewed.
    exhausted = followup_sequencing.is_sequence_exhausted(req.task)
    resolution = None
    if exhausted:
        # Sep 24 2026 audit: a retried request for the same exhausted task
        # used to mint a second NO_RESOLUTION record and a second
        # write-back. Now the first record is returned (process lifetime).
        with _exhausted_lock:
            now = _now()
            record = _exhausted_resolutions.get(req.task.task_id, now)
            if record is None:
                if _exhausted_resolutions.room(now) < 1:
                    # Fail closed BEFORE the write-back: without room to
                    # remember this record, a retry would mint a duplicate.
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="exhausted-escalation dedupe state is full (fail closed); no record written — retry later",
                        headers={"Retry-After": "60"},
                    )
                [record] = resolution_writeback.resolve_and_writeback(
                    [
                        TerminalEvent(
                            entity_type="task",
                            entity_id=req.task.task_id,
                            customer_id=req.task.customer_id,
                            resolution_type=ResolutionType.NO_RESOLUTION,
                        )
                    ],
                    _system_of_record,
                )
                _exhausted_resolutions.put(req.task.task_id, record, now)
        resolution = record.model_dump()

    return {"next_task": None, "sequence_exhausted": exhausted, "resolution": resolution}


def _outcome_row(o: callback_orchestration.OrchestrationOutcome) -> dict:
    return {
        "task_id": o.task.task_id,
        "attempted": o.attempted,
        "skip_reason": o.skip_reason,
        "dial_placed": o.dial_result.placed if o.dial_result else None,
        "sla_breached": o.sla_breached,
        "resulting_task_status": o.task.status.value,
    }


@app.post("/agents/callback-orchestration/run", dependencies=[Depends(require_auth)])
async def run_callback_orchestration(request: Request) -> Response:
    return await _off_loop(request, OrchestrateRequest, _run_callback_orchestration)


def _run_callback_orchestration(req: OrchestrateRequest) -> dict:
    # Sep 24 2026 audit: (1) the clock is the server's, never the caller's;
    # (2) a task_id this process already handed to the dialer is never
    # dialed again, even if the caller resubmits it still PENDING — a
    # convenience only since fix wave 1 F3: task ids are caller-chosen, so
    # the real redial limit is the gate's per-number/per-customer limit; (3) the
    # check-then-dial is serialized so concurrent requests for the same
    # task can't both pass the check. Serializing dials is a throughput
    # cost accepted deliberately: correctness over speed for outbound calls.
    with _dial_lock:
        now = _now()
        fresh = [t for t in req.tasks if not _attempted_task_ids.contains(t.task_id, now)]
        repeats = [
            t for t in req.tasks
            if _attempted_task_ids.contains(t.task_id, now)
            and t.channel == TaskChannel.CALL and t.status == TaskStatus.PENDING
        ]
        # Fix wave 1: a task this service cannot remember having dialed is
        # not dialed (fail closed). Only as many distinct new ids as the
        # dedupe store has room for go on to the orchestrator.
        room = _attempted_task_ids.room(now)
        admitted: list[FollowUpTask] = []
        over_capacity: list[FollowUpTask] = []
        admitted_ids: set[str] = set()
        for t in fresh:
            if t.task_id in admitted_ids or len(admitted_ids) < room:
                admitted_ids.add(t.task_id)
                admitted.append(t)
            else:
                over_capacity.append(t)
        outcomes = callback_orchestration.orchestrate(
            admitted,
            _dialer,
            phone_by_call_id=req.phone_by_call_id,
            line_by_call_id=req.line_by_call_id,
            timezone_by_call_id=req.timezone_by_call_id,
            gate=_GATE,
            now=now,
        )
        for o in outcomes:
            if o.attempted:
                _attempted_task_ids.put(o.task.task_id, True, now)  # room reserved above

    rows = [_outcome_row(o) for o in outcomes]
    rows += [
        {
            "task_id": t.task_id,
            "attempted": False,
            "skip_reason": "dedupe state is full — not dialed (fail closed); retry later",
            "dial_placed": None,
            "sla_breached": False,
            "resulting_task_status": t.status.value,
        }
        for t in over_capacity
    ]
    rows += [
        {
            "task_id": t.task_id,
            "attempted": False,
            "skip_reason": "already attempted by this service — not redialed",
            "dial_placed": None,
            "sla_breached": False,
            "resulting_task_status": t.status.value,
        }
        for t in repeats
    ]
    capacity = _GATE.status()
    if capacity["near_capacity"] or capacity["new_key_budget_exhausted"] or capacity["new_key_burst_exhausted"]:
        _log.warning("outbound gate capacity alert: %s", capacity)
    return {"outcomes": rows, "gate": capacity}


@app.get("/gate/status", dependencies=[Depends(require_auth)])
def gate_status() -> dict:
    """Outbound gate capacity (fix wave 4): how full the tracked-number
    history is and how much of this hour's new-number budget is used. Alert
    on near_capacity / new_key_budget_exhausted — at_capacity means no NEW
    number or customer can be contacted until history expires (fail closed)."""
    return _GATE.status()


@app.post("/agents/customer-dossier/update", dependencies=[Depends(require_auth)])
async def update_dossiers(request: Request) -> Response:
    return await _off_loop(request, DossierUpdateRequest, _update_dossiers)


def _update_dossiers(req: DossierUpdateRequest) -> DossiersResponse:
    # Fix wave 4: this used to deep-copy the WHOLE store on every request and
    # return every customer's dossier to every caller (0.96 s for one update
    # against 20,000 small dossiers; up to the 100,000-customer cap). Now only
    # the dossiers this request touches are copied, checked and returned.
    # Stored dossiers are never mutated in place (apply_updates works on
    # copies), so the response can be rendered after the lock is released.
    with _dossiers_lock:
        changed = customer_dossier.apply_updates(_dossiers, req.call_events, req.appointments)
        new_customers = sum(1 for cid in changed if cid not in _dossiers)
        if len(_dossiers) + new_customers > _MAX_DOSSIERS:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"dossier store is full (max {_MAX_DOSSIERS} customers); update not applied (fail closed)",
                headers={"Retry-After": "60"},
            )
        if any(
            len(lst) > _MAX_DOSSIER_HISTORY
            for d in changed.values()
            for lst in (d.call_history, d.appointment_history, d.phone_numbers)
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"a dossier's history is full (max {_MAX_DOSSIER_HISTORY} entries per list); update not applied (fail closed)",
                headers={"Retry-After": "60"},
            )
        _dossiers.update(changed)
    return DossiersResponse(dossiers=list(changed.values()))


@app.post("/agents/resolution-writeback/resolve", dependencies=[Depends(require_auth)])
async def resolve_and_writeback(request: Request) -> Response:
    return await _off_loop(request, ResolveRequest, _resolve_and_writeback)


def _resolve_and_writeback(req: ResolveRequest) -> ResolveResponse:
    events = [
        TerminalEvent(
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            customer_id=e.customer_id,
            resolution_type=e.resolution_type,
        )
        for e in req.events
    ]
    records = resolution_writeback.resolve_and_writeback(events, _system_of_record)
    return ResolveResponse(records=records)


# --- entrypoint --------------------------------------------------------------
# Sep 24 2026 audit: this service had no entrypoint of its own; the README
# told people to run `uvicorn api:app --reload --port 8091`. uvicorn's own
# CLI default host is 127.0.0.1, so that was not an all-interfaces bind —
# but the bind address was not the service's decision and had no env
# override, unlike ledger-rust (LEDGER_BIND_ADDR) and orchestrator-go
# (ORCHESTRATOR_BIND_ADDR), and `--reload` is a dev file-watcher, not a
# way to run a service. `python3 -m api` now owns its bind: 127.0.0.1 by
# default, FULFILLMENT_BIND_ADDR to override, FULFILLMENT_PORT (8091).
#
# Fix wave 5, NEW-3: it also owns its transport limits. uvicorn's defaults
# (httptools parser, no head/body deadline) buffered a 150 MB header in full
# and held idle, partial-head and slow-body sockets forever, all without a
# token. It now runs the h11 parser with a 16 KiB head cap, head and body
# deadlines, and bounded connections — values and trade-offs in
# http_limits.py and the README ("Transport limits").

# Fix wave 6, N2: glibc gives each thread that allocates its own malloc arena,
# and a non-main arena keeps freed memory that is not at the top of its heap.
# Every thread that parses a body (jiter/pydantic-core allocate through the
# system allocator; so do dict tables and long strings) therefore kept tens of
# MB after its request was done: 20 concurrent 60k-key bodies left RSS at
# +102 MB with default arenas and +24 MB with one arena, same work. With the
# GIL serializing Python anyway, arena contention costs nothing here. Best
# effort and glibc only (`python3 -m api`); an operator-set MALLOC_ARENA_MAX
# wins; anything else keeps its allocator's defaults and logs a warning.
_MALLOC_ARENA_MAX = 1  # M_ARENA_MAX: every thread allocates from the main arena


def _libc():
    try:
        import ctypes

        return ctypes.CDLL("libc.so.6")
    except OSError:
        return None


_LIBC = _libc()


def _limit_malloc_arenas() -> bool:
    if os.environ.get("MALLOC_ARENA_MAX"):
        return True  # the operator chose; glibc read it at startup
    try:
        return bool(_LIBC.mallopt(-8, _MALLOC_ARENA_MAX))  # M_ARENA_MAX == -8
    except AttributeError:
        return False


def _malloc_trim() -> None:
    # Fix wave 7 (see _TRIM_IDLE_S): return freed heap pages to the OS once
    # no large body is being parsed. glibc only; a no-op elsewhere.
    try:
        _LIBC.malloc_trim(0)
    except AttributeError:
        pass


def main() -> None:
    import uvicorn

    if not _limit_malloc_arenas():
        _log.warning("could not limit malloc arenas (not glibc?); RSS after bursts of bad bodies may stay higher")
    host = os.environ.get("FULFILLMENT_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("FULFILLMENT_PORT", "8091"))
    http_limits.DeadlineH11Protocol.body_timeout_s = _BODY_READ_TIMEOUT_S
    uvicorn.run(
        app,
        host=host,
        port=port,
        http=http_limits.DeadlineH11Protocol,
        h11_max_incomplete_event_size=http_limits.MAX_HEADER_BYTES,
        timeout_keep_alive=http_limits.KEEP_ALIVE_TIMEOUT_S,
        limit_concurrency=http_limits.LIMIT_CONCURRENCY,
    )


if __name__ == "__main__":
    main()
