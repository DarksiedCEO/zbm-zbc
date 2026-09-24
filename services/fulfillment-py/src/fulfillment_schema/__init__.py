"""
fulfillment_schema — shared domain contracts for ZBM's Fulfillment
department (see docs/adr/0002-fulfillment-department-architecture.md).

Mirrors the architectural discipline set in Revenue Recovery 1A
(zbm_schema in services/detection-py):

  1. Every follow-up action carries a mandatory `purpose` and `channel`
     so a caller can never construct an ambiguous, untyped task.
  2. Every entity that can be "resolved" (a call, an appointment) is
     traceable to a `ResolutionRecord` with an explicit write-back
     status — silence is never treated as success (Numa-style
     completeness model).
  3. `CustomerDossier` is the single shared record a call, a follow-up,
     and an appointment all resolve back to (Beside-style memory) —
     there is no separate per-channel history object.

Platform-agnostic by design: no SIP-trunk-specific, carrier-specific, or
CRM-specific fields live here. Those attach at the integration seams in
`integrations/` (SipDialerPort, SystemOfRecordPort), never in this
schema.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Optional

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, StringConstraints

from fulfillment_schema.money import PositiveMoney


# ---------------------------------------------------------------------------
# Bounded field types (Sep 24 2026 audit). Before this, every string was
# unbounded, phone numbers were free text ("call me" was accepted and would
# have been handed to the dialer), and every datetime accepted a NAIVE
# value, which then crashed the agents' aware-vs-naive comparisons with an
# unhandled TypeError => HTTP 500. Now: naive datetimes are a 422.
# ---------------------------------------------------------------------------

_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"
EntityId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=_ID_PATTERN)]
# task ids are derived ("fu-" + call_id, then "-escN" per escalation), so
# they get headroom above EntityId instead of failing validation mid-agent.
TaskId = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=_ID_PATTERN)]
# E.164: "+", country code 1-9, up to 15 digits total. No spaces/dashes.
PhoneE164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{1,14}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=128)]

# Fix wave 1 (fuzz sweep): every datetime used to accept anything Python can
# represent, so a call event with started_at "9999-12-31T23:59:59+00:00" made
# missed-call detection compute started_at + 5 min -> OverflowError -> HTTP
# 500. Every datetime in this schema is now bounded to [2000-01-01,
# 2100-01-01) UTC, so no agent's timedelta arithmetic (at most days) can
# leave the representable range. The comparison itself cannot overflow into
# a 500 either: any OverflowError becomes the same validation error.
DATETIME_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
DATETIME_MAX_EXCLUSIVE = datetime(2100, 1, 1, tzinfo=timezone.utc)
# A deadline an agent DERIVES from an in-range event (missed-call due_at =
# started_at + at most 5 min) may land just past DATETIME_MAX_EXCLUSIVE; it
# must not fail validation inside the agent (that was itself a 500), so
# FollowUpTask.due_at gets one day of headroom. Still far from overflow.
DEADLINE_MAX_EXCLUSIVE = DATETIME_MAX_EXCLUSIVE + timedelta(days=1)


def _range_check(max_exclusive: datetime):
    def check(v: datetime) -> datetime:
        try:
            ok = DATETIME_MIN <= v < max_exclusive
        except OverflowError:
            ok = False
        if not ok:
            raise ValueError(
                f"datetime must be between {DATETIME_MIN.isoformat()} (inclusive) and "
                f"{max_exclusive.isoformat()} (exclusive)"
            )
        return v

    return check


BoundedAwareDatetime = Annotated[AwareDatetime, AfterValidator(_range_check(DATETIME_MAX_EXCLUSIVE))]
DeadlineDatetime = Annotated[AwareDatetime, AfterValidator(_range_check(DEADLINE_MAX_EXCLUSIVE))]


# ---------------------------------------------------------------------------
# Shared value labeling (same discipline as zbm_schema.LabeledValue —
# reused here for estimated job/lifetime value, never presented unlabeled)
# ---------------------------------------------------------------------------

class ValueConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LabeledValue(BaseModel):
    """A dollar amount that cannot exist without a confidence label attached.

    Independent review finding (Sep 22 2026, CONFIRMED): amount_usd was a
    float. No agent in this build currently populates this field (it's a
    stub for a future lifetime-value estimate), so today's actual risk
    was theoretical — but float is the wrong type for money regardless of
    current usage, and this exact class of bug is what a future agent
    would silently inherit. Fixed to Decimal, matching how a value that
    will eventually inform revenue decisions should be represented from
    the start, not retrofitted after it's wired into something real.

    Sep 24 2026 audit: brought to BUILD_CONTRACTS section 1 (half-up to
    0.01, positive check after rounding, two-decimal JSON string).

    Fix wave 1, F15: the Sep 24 version still parsed ANY string Decimal()
    understood and then rounded it, so "1e3", " 12.30 ", "012.30" were
    accepted and "12.345" became "12.35". Now `PositiveMoney`
    (fulfillment_schema/money.py): a string must be the canonical wire form
    (fixtures/money_vectors.json), a JSON number is refused when parsed from
    JSON text, and only computed Decimal/int values are rounded half-up.
    """
    model_config = {"frozen": True}

    amount_usd: PositiveMoney
    confidence: ValueConfidence


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, Enum):
    ANSWERED = "answered"
    MISSED = "missed"
    VOICEMAIL = "voicemail"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    FAILED = "failed"  # carrier/SIP-level failure, not a customer outcome


class CallEvent(BaseModel):
    call_id: EntityId
    customer_id: Optional[EntityId] = None  # None until dossier matching resolves the caller
    phone_number: PhoneE164
    direction: CallDirection
    status: CallStatus
    started_at: BoundedAwareDatetime
    ended_at: Optional[BoundedAwareDatetime] = None
    duration_seconds: int = Field(ge=0, le=86_400, default=0)
    voicemail_transcript: Optional[str] = Field(default=None, max_length=10_000)
    line_id: EntityId  # which configured SIP line/trunk this call rode in on

    @property
    def is_unresolved(self) -> bool:
        """A call the business did not actually connect on — the trigger
        condition for missed-call detection."""
        return self.status in (
            CallStatus.MISSED,
            CallStatus.VOICEMAIL,
            CallStatus.NO_ANSWER,
            CallStatus.BUSY,
        )


# ---------------------------------------------------------------------------
# Follow-up tasks — the actionable unit every agent in this department
# ultimately produces or consumes
# ---------------------------------------------------------------------------

class TaskPurpose(str, Enum):
    MISSED_CALL_CALLBACK = "missed_call_callback"
    ESCALATION = "escalation"
    APPOINTMENT_CONFIRMATION = "appointment_confirmation"
    COMPLETION_CHECK = "completion_check"
    REVIEW_REQUEST = "review_request"


class TaskChannel(str, Enum):
    CALL = "call"
    SMS = "sms"
    EMAIL = "email"
    HUMAN_HANDOFF = "human_handoff"


class TaskStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    COMPLETED = "completed"
    ESCALATED = "escalated"


class FollowUpTask(BaseModel):
    task_id: TaskId
    purpose: TaskPurpose
    channel: TaskChannel
    customer_id: Optional[EntityId] = None
    source_call_id: Optional[EntityId] = None  # the CallEvent that triggered this, if any
    source_appointment_id: Optional[EntityId] = None  # the Appointment this concerns, if any
    created_at: BoundedAwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    due_at: DeadlineDatetime
    attempt_number: int = Field(ge=1, le=100, default=1)
    status: TaskStatus = TaskStatus.PENDING
    reason: str = Field(min_length=1, max_length=2_000)  # human-readable: why this task exists


# ---------------------------------------------------------------------------
# Customer dossier — Beside-style single shared record
# ---------------------------------------------------------------------------

class CustomerDossier(BaseModel):
    customer_id: str
    name: Optional[str] = None
    phone_numbers: list[str] = Field(default_factory=list)
    first_contact_at: Optional[BoundedAwareDatetime] = None
    last_contact_at: Optional[BoundedAwareDatetime] = None
    call_history: list[str] = Field(default_factory=list)  # call_ids
    appointment_history: list[str] = Field(default_factory=list)  # appointment_ids
    open_task_ids: list[str] = Field(default_factory=list)
    lifetime_value: Optional[LabeledValue] = None
    tags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Appointments — Vocca-style completion tracking
# ---------------------------------------------------------------------------

class AppointmentStatus(str, Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"


class Appointment(BaseModel):
    appointment_id: EntityId
    customer_id: EntityId
    scheduled_at: BoundedAwareDatetime
    service_type: ShortText
    status: AppointmentStatus
    completion_confirmed_at: Optional[BoundedAwareDatetime] = None
    technician_id: Optional[EntityId] = None

    def is_overdue_for_confirmation(self, now: datetime) -> bool:
        """True when the scheduled time has passed but nothing ever moved
        the status off SCHEDULED/CONFIRMED — Vocca's core insight: a
        booked appointment with no completion signal is not a success,
        it's an open question."""
        return (
            self.status in (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)
            and self.scheduled_at < now
        )


# ---------------------------------------------------------------------------
# Resolution / write-back — Numa-style "resolve to completion" model
# ---------------------------------------------------------------------------

class ResolutionType(str, Enum):
    BOOKED = "booked"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    ESCALATED_TO_HUMAN = "escalated_to_human"
    CONFIRMED_COMPLETE = "confirmed_complete"
    NO_RESOLUTION = "no_resolution"  # explicit, not silence


class WriteBackStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"  # no system-of-record adapter wired for this client


class ResolutionRecord(BaseModel):
    resolution_id: str
    entity_type: str  # "call" | "appointment" | "task"
    entity_id: str
    customer_id: Optional[str] = None
    resolution_type: ResolutionType
    resolved_at: BoundedAwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    write_back_status: WriteBackStatus
    write_back_detail: Optional[str] = None
