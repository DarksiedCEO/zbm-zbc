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

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Optional

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, field_validator


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

MONEY_QUANTUM = Decimal("0.01")


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

    Sep 24 2026 audit: brought to BUILD_CONTRACTS section 1. Previously
    quantized with the default context rounding (ROUND_HALF_EVEN, so
    "0.125" -> "0.12", "1.005" -> "1.00") and checked `gt=0` BEFORE
    rounding, so "0.004" became a positive-only amount of "0.00". Now:
    float input only via str(value), ROUND_HALF_UP to 0.01, and the
    positive check runs on the rounded value. Serializes as a two-decimal
    JSON string ("12.30").
    """
    model_config = {"frozen": True}

    amount_usd: Decimal
    confidence: ValueConfidence

    @field_validator("amount_usd", mode="before")
    @classmethod
    def _to_decimal(cls, v: object) -> Decimal:
        if isinstance(v, bool):
            raise ValueError("amount_usd must be a number, not a boolean")
        if isinstance(v, float):
            v = str(v)  # never Decimal(float): 1.005 must stay 1.005
        if isinstance(v, (int, str, Decimal)):
            try:
                d = Decimal(v)
            except InvalidOperation:
                raise ValueError("amount_usd is not a valid decimal amount") from None
        else:
            raise ValueError("amount_usd must be a string, int, float or Decimal")
        if not d.is_finite():
            raise ValueError("amount_usd must be finite")
        try:
            q = d.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        except InvalidOperation:  # more digits than the 28-digit context holds
            raise ValueError("amount_usd is out of range") from None
        if q <= 0:
            raise ValueError("amount_usd must be positive after rounding to 0.01")
        return q


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
    started_at: AwareDatetime
    ended_at: Optional[AwareDatetime] = None
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
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    due_at: AwareDatetime
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
    first_contact_at: Optional[AwareDatetime] = None
    last_contact_at: Optional[AwareDatetime] = None
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
    scheduled_at: AwareDatetime
    service_type: ShortText
    status: AppointmentStatus
    completion_confirmed_at: Optional[AwareDatetime] = None
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
    resolved_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    write_back_status: WriteBackStatus
    write_back_detail: Optional[str] = None
