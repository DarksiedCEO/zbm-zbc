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
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from agents import (
    appointment_tracking,
    callback_orchestration,
    customer_dossier,
    followup_sequencing,
    missed_call_detection,
    resolution_writeback,
)
from fixtures_loader import load_appointments, load_call_events, load_dossiers
from fulfillment_schema import (
    Appointment,
    CallEvent,
    CustomerDossier,
    FollowUpTask,
    ResolutionRecord,
)
from integrations.sip_dialer import InMemorySipDialer, NotWiredSipDialer, SipDialerPort
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


_dialer: SipDialerPort = _build_dialer()
_system_of_record: SystemOfRecordPort = _build_system_of_record()
_dossiers: dict[str, CustomerDossier] = {}


class CallEventsRequest(BaseModel):
    call_events: list[CallEvent]


class AppointmentsRequest(BaseModel):
    appointments: list[Appointment]


class TasksResponse(BaseModel):
    tasks: list[FollowUpTask]


class DossierUpdateRequest(BaseModel):
    call_events: list[CallEvent] = []
    appointments: list[Appointment] = []


class DossiersResponse(BaseModel):
    dossiers: list[CustomerDossier]


class EscalateRequest(BaseModel):
    task: FollowUpTask


class OrchestrateRequest(BaseModel):
    tasks: list[FollowUpTask]
    phone_by_call_id: dict[str, str]
    line_by_call_id: dict[str, str] = {}
    now: datetime | None = None  # override for deterministic testing; defaults to real time


class TerminalEventIn(BaseModel):
    entity_type: str
    entity_id: str
    customer_id: str | None = None
    resolution_type: str


class ResolveRequest(BaseModel):
    events: list[TerminalEventIn]


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
    result = followup_sequencing.escalate(req.task)
    if result is not None:
        return {"next_task": result.model_dump(), "sequence_exhausted": False, "resolution": None}

    # Independent review finding (Sep 22 2026, CONFIRMED): a failed
    # human_handoff previously vanished here — next_task: null and
    # nothing else. Now: when the sequence is genuinely exhausted, that
    # is itself recorded as a resolution (NO_RESOLUTION — explicit, not
    # silence) and an attempted write-back, same as any other terminal
    # event, so it shows up wherever resolution records are reviewed.
    from fulfillment_schema import ResolutionType

    exhausted = followup_sequencing.is_sequence_exhausted(req.task)
    resolution = None
    if exhausted:
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
        resolution = record.model_dump()

    return {"next_task": None, "sequence_exhausted": exhausted, "resolution": resolution}


@app.post("/agents/callback-orchestration/run", dependencies=[Depends(require_auth)])
def run_callback_orchestration(req: OrchestrateRequest) -> dict:
    outcomes = callback_orchestration.orchestrate(
        req.tasks,
        _dialer,
        phone_by_call_id=req.phone_by_call_id,
        line_by_call_id=req.line_by_call_id,
        now=req.now,
    )
    return {
        "outcomes": [
            {
                "task_id": o.task.task_id,
                "attempted": o.attempted,
                "skip_reason": o.skip_reason,
                "dial_placed": o.dial_result.placed if o.dial_result else None,
                "sla_breached": o.sla_breached,
                "resulting_task_status": o.task.status.value,
            }
            for o in outcomes
        ]
    }


@app.post("/agents/customer-dossier/update", response_model=DossiersResponse, dependencies=[Depends(require_auth)])
def update_dossiers(req: DossierUpdateRequest) -> DossiersResponse:
    global _dossiers
    _dossiers = customer_dossier.build_or_update(_dossiers, req.call_events, req.appointments)
    return DossiersResponse(dossiers=list(_dossiers.values()))


@app.post("/agents/resolution-writeback/resolve", response_model=ResolveResponse, dependencies=[Depends(require_auth)])
def resolve_and_writeback(req: ResolveRequest) -> ResolveResponse:
    from fulfillment_schema import ResolutionType

    events = [
        TerminalEvent(
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            customer_id=e.customer_id,
            resolution_type=ResolutionType(e.resolution_type),
        )
        for e in req.events
    ]
    records = resolution_writeback.resolve_and_writeback(events, _system_of_record)
    return ResolveResponse(records=records)
