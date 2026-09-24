"""
MessageSenderPort — the seam any future SMS or email sender must implement
(fix wave 1, F3).

HONEST STATUS: no SMS or email sender exists in this repository.
followup_sequencing and appointment_tracking create SMS/EMAIL tasks, but
nothing sends them. The audit found those tasks are created without any
calling-hours check. Creating a task is not contacting anyone (a task
created at 02:00 may properly go out at 09:00), so the check belongs where
contact happens. This port puts it there before any sender exists: `send`
receives a ContactAuthorization from OutboundContactGate, never an address
or number, and must obtain the recipient only via
`authorization.redeem(TaskChannel.SMS | TaskChannel.EMAIL)` immediately
before sending. redeem() enforces the recipient-local window (in every zone
the customer's number could be in) and the per-number/per-customer attempt
limits shared with calls, and raises ContactRefused otherwise.

Email: the schema has no email address yet. The gate still requires the
customer's phone number for an EMAIL authorization, because the number is
the only thing the recipient's time zone can be checked against; a future
email sender resolves the address from authorization.customer_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from outbound_gate import ContactAuthorization


@dataclass(frozen=True)
class SendResult:
    message_id: str
    sent: bool
    detail: str
    sent_at: datetime


class MessageSenderPort(Protocol):
    def send(self, authorization: ContactAuthorization, body: str) -> SendResult:
        """Must call authorization.redeem(<channel>) to obtain the recipient,
        immediately before sending, and must not send if it raises."""
        ...


class NotWiredMessageSender:
    """Default: fails loudly, never redeems (so no attempt is consumed)."""

    def send(self, authorization: ContactAuthorization, body: str) -> SendResult:
        raise NotImplementedError(
            "No MessageSenderPort implementation is configured; SMS/email sending is not wired."
        )
