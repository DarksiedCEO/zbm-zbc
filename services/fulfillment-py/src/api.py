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

import hmac
import os
import threading
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agents import (
    appointment_tracking,
    callback_orchestration,
    customer_dossier,
    followup_sequencing,
    missed_call_detection,
    resolution_writeback,
)
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

# In-memory state. FastAPI runs these sync handlers in a thread pool, so
# every read-modify-write below is under a lock (Sep 24 2026 audit: four
# concurrent dossier updates kept one customer and silently dropped three).
_dossiers: dict[str, CustomerDossier] = {}
_dossiers_lock = threading.Lock()
# task_ids this process has already handed to the dialer. Closes README
# gap 6 for the life of the process only — lost on restart (no datastore).
_attempted_task_ids: set[str] = set()
_dial_lock = threading.Lock()
# exhausted-escalation task_id -> the NO_RESOLUTION record already made
# for it, so a retried request returns the same record (process lifetime).
_exhausted_resolutions: dict[str, ResolutionRecord] = {}
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
def health() -> dict:
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

@app.post("/agents/missed-call-detection/detect", response_model=TasksResponse, dependencies=[Depends(require_auth)])
def detect_missed_calls(req: CallEventsRequest) -> TasksResponse:
    return TasksResponse(tasks=missed_call_detection.detect(req.call_events))


@app.post("/agents/appointment-tracking/detect", response_model=TasksResponse, dependencies=[Depends(require_auth)])
def detect_overdue_appointments(req: AppointmentsRequest) -> TasksResponse:
    return TasksResponse(tasks=appointment_tracking.find_overdue(req.appointments))


@app.post("/agents/followup-sequencing/escalate", dependencies=[Depends(require_auth)])
def escalate_task(req: EscalateRequest) -> dict:
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
            record = _exhausted_resolutions.get(req.task.task_id)
            if record is None:
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
                _exhausted_resolutions[req.task.task_id] = record
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
def run_callback_orchestration(req: OrchestrateRequest) -> dict:
    # Sep 24 2026 audit: (1) the clock is the server's, never the caller's;
    # (2) a task_id this process already handed to the dialer is never
    # dialed again, even if the caller resubmits it still PENDING — a
    # convenience only since fix wave 1 F3: task ids are caller-chosen, so
    # the real redial limit is the gate's per-number/per-customer limit; (3) the
    # check-then-dial is serialized so concurrent requests for the same
    # task can't both pass the check. Serializing dials is a throughput
    # cost accepted deliberately: correctness over speed for outbound calls.
    with _dial_lock:
        fresh = [t for t in req.tasks if t.task_id not in _attempted_task_ids]
        repeats = [
            t for t in req.tasks
            if t.task_id in _attempted_task_ids
            and t.channel == TaskChannel.CALL and t.status == TaskStatus.PENDING
        ]
        outcomes = callback_orchestration.orchestrate(
            fresh,
            _dialer,
            phone_by_call_id=req.phone_by_call_id,
            line_by_call_id=req.line_by_call_id,
            timezone_by_call_id=req.timezone_by_call_id,
            gate=_GATE,
            now=_now(),
        )
        for o in outcomes:
            if o.attempted:
                _attempted_task_ids.add(o.task.task_id)

    rows = [_outcome_row(o) for o in outcomes]
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
    return {"outcomes": rows}


@app.post("/agents/customer-dossier/update", response_model=DossiersResponse, dependencies=[Depends(require_auth)])
def update_dossiers(req: DossierUpdateRequest) -> DossiersResponse:
    global _dossiers
    with _dossiers_lock:
        _dossiers = customer_dossier.build_or_update(_dossiers, req.call_events, req.appointments)
        snapshot = list(_dossiers.values())
    return DossiersResponse(dossiers=snapshot)


@app.post("/agents/resolution-writeback/resolve", response_model=ResolveResponse, dependencies=[Depends(require_auth)])
def resolve_and_writeback(req: ResolveRequest) -> ResolveResponse:
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

def main() -> None:
    import uvicorn

    host = os.environ.get("FULFILLMENT_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("FULFILLMENT_PORT", "8091"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
