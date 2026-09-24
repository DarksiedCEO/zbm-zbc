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
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Literal, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
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


class _BodyTooLarge(Exception):
    pass


class _BodyTimeout(Exception):
    pass


def _refusal(code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail}, headers={"Connection": "close"})


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
            await _refusal(431, f"request head exceeds {_MAX_HEADER_BYTES} bytes")(scope, receive, send)
            return
        limit = _MAX_BODY_BYTES
        for name, value in scope["headers"]:
            if name == b"content-length":
                if not value.isdigit():
                    await JSONResponse(status_code=400, content={"detail": "invalid Content-Length"},
                                       headers={"Connection": "close"})(scope, receive, send)
                    return
                if int(value) > limit:
                    await _too_large_response()(scope, receive, send)
                    return
        received = 0
        started = False
        body_done = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BODY_READ_TIMEOUT_S

        async def limited_receive() -> Message:
            nonlocal received, body_done
            if body_done:
                return await receive()  # e.g. waiting for http.disconnect: no deadline
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), remaining)
            except TimeoutError:
                raise _BodyTimeout() from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
                if not message.get("more_body", False):
                    body_done = True
            else:
                body_done = True
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if started:
                raise
            await _too_large_response()(scope, receive, send)
        except _BodyTimeout:
            if started:
                raise
            await _refusal(408, f"request body not received within {_BODY_READ_TIMEOUT_S:g}s")(scope, receive, send)


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


def _parse(model: type[_M], body: bytes) -> _M:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise RequestValidationError(
            [{"loc": ("body", *e["loc"]), "type": e["type"], "msg": e["msg"]} for e in exc.errors(include_url=False)]
        ) from None


def _render(result: Any) -> Response:
    # The same encoder FastAPI applied to these return values, run here so the
    # json.dumps happens in the worker thread, not on the event loop.
    return JSONResponse(content=jsonable_encoder(result))


async def _off_loop(request: Request, model: type[_M], work: Callable[[_M], Any]) -> Response:
    if not _is_json_content_type(request.headers.get("content-type")):
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="expected application/json")
    body = await request.body()  # bounded by BodySizeLimitMiddleware
    return await run_in_threadpool(lambda: _render(work(_parse(model, body))))


@app.exception_handler(RequestValidationError)
async def _validation_error_without_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Sep 24 2026 audit: FastAPI's default 422 body echoes each error's
    # `input` (and `ctx`). For a missing field, `input` is the whole
    # submitted object — so a malformed call event echoed the caller's
    # phone number and voicemail transcript back in the error, and into
    # any proxy or client log that records error bodies. Keep only
    # location, type and message: enough to fix the request, no payload.
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": [{"loc": e.get("loc"), "type": e.get("type"), "msg": e.get("msg")} for e in exc.errors()]},
    )


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

_MAX_BATCH = 1000
TimezoneName = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class _Req(BaseModel):
    # Unknown fields are an error, not silently ignored — this is what
    # makes a stale client still sending `now` get a 422 instead of
    # believing its clock override worked.
    model_config = ConfigDict(extra="forbid")


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
    phone_by_call_id: dict[EntityId, PhoneE164] = Field(max_length=_MAX_BATCH)
    line_by_call_id: dict[EntityId, EntityId] = Field(default_factory=dict, max_length=_MAX_BATCH)
    # Recipient IANA time zone per call (e.g. "America/Chicago"). A call
    # with no entry here is not dialed — fail closed. Fix wave 1, F3: this
    # is a CLAIM, checked against the number by the gate (recipient_zones.py);
    # it can narrow the window, never widen it.
    timezone_by_call_id: dict[EntityId, TimezoneName] = Field(default_factory=dict, max_length=_MAX_BATCH)


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

def main() -> None:
    import uvicorn

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
