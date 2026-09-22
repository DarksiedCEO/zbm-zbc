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
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


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
    """
    model_config = {"frozen": True}

    amount_usd: Decimal = Field(gt=0)
    confidence: ValueConfidence

    @field_validator("amount_usd")
    @classmethod
    def _finite_amount(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("amount_usd must be a finite positive number")
        return v.quantize(Decimal("0.01"))


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
    call_id: str
    customer_id: Optional[str] = None  # None until dossier matching resolves the caller
    phone_number: str
    direction: CallDirection
    status: CallStatus
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_seconds: int = Field(ge=0, default=0)
    voicemail_transcript: Optional[str] = None
    line_id: str  # which configured SIP line/trunk this call rode in on

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
    task_id: str
    purpose: TaskPurpose
    channel: TaskChannel
    customer_id: Optional[str] = None
    source_call_id: Optional[str] = None  # the CallEvent that triggered this, if any
    source_appointment_id: Optional[str] = None  # the Appointment this concerns, if any
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    due_at: datetime
    attempt_number: int = Field(ge=1, default=1)
    status: TaskStatus = TaskStatus.PENDING
    reason: str  # human-readable: why this task exists


# ---------------------------------------------------------------------------
# Customer dossier — Beside-style single shared record
# ---------------------------------------------------------------------------

class CustomerDossier(BaseModel):
    customer_id: str
    name: Optional[str] = None
    phone_numbers: list[str] = Field(default_factory=list)
    first_contact_at: Optional[datetime] = None
    last_contact_at: Optional[datetime] = None
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
    appointment_id: str
    customer_id: str
    scheduled_at: datetime
    service_type: str
    status: AppointmentStatus
    completion_confirmed_at: Optional[datetime] = None
    technician_id: Optional[str] = None

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
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    write_back_status: WriteBackStatus
    write_back_detail: Optional[str] = None
