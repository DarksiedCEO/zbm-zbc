"""The confirmation email port (clickwrap text v1, Part D; ADR 0028).

No mail provider is wired in this service. Finance has a client-mail adapter on another branch (ADR 0026); when one
is chosen for this service, it implements ``ConfirmationMailer`` and is passed to ``create_app``. Until then the
stand-in sends nothing and says so (``send`` returns False), the confirmation page does not claim an email was sent,
and Andre sends the Part D email by hand (docs/runbooks/client-acceptance.md, step 5).

Contract for a real adapter:
- ``send`` returns True only when the provider accepted the message for delivery;
- it passes ``message.idempotency_key`` (the invitation id) to the provider as its de-duplication key, so a retried
  submission never reaches the client as a second email;
- it times out within 10 seconds and never raises (the acceptance is already recorded when it runs);
- the attachments are the documents' exact registered UTF-8 text (their SHA-256 equals the fingerprints listed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    content_type: str = "text/markdown; charset=utf-8"


@dataclass(frozen=True)
class ConfirmationMessage:
    to: str
    subject: str
    body: str
    attachments: tuple[Attachment, ...]
    idempotency_key: str


class ConfirmationMailer(Protocol):
    def send(self, message: ConfirmationMessage) -> bool: ...


class NotWiredMailer:
    """No mail provider is chosen or wired: nothing is sent."""

    def send(self, message: ConfirmationMessage) -> bool:
        return False
