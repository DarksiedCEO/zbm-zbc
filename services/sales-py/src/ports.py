"""
Ports to everything outside Sales (ADR 0013 decision 17). Each has a fail-closed stand-in that says what is missing;
/sales/v1/status lists which are wired. No provider or department client is built yet: setting the switch that would
select one refuses start (config.NOT_BUILT).

- ``EmailSender``, ``SmsSender``, ``VoiceDialer``: send one message / place one call. Stand-in: ``not_wired`` — the
  message stays ``queued``, visibly, and nothing is recorded as sent.
- ``LeadSource`` x2 (public data, paid provider): fetch candidate leads. Stand-in: raises ``NotWired``.
- ``OnboardingHandoff`` (create the client) and ``FinanceHandoff`` (draft the first invoice) for a won deal.
  Stand-in: ``unavailable`` — the hand-off stays ``pending_delivery`` and the ``handoff-retry`` job retries it. A
  real adapter must be idempotent on ``handoff_id`` (a retry after a lost answer sends it again).
- ``LegalContracts``: is a client_msa / order form in force for this account (Legal 37)? Stand-in: ``unavailable``,
  so no proposal can be sent (``LEGAL_UNAVAILABLE``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


class NotWired(Exception):
    pass


@dataclass(frozen=True)
class SendResult:
    status: str                       # accepted | failed | not_wired
    provider_ref: Optional[str] = None


class EmailSender(Protocol):
    wired: bool

    def send(self, message_id: str, to: str, msg: dict) -> SendResult: ...


class SmsSender(Protocol):
    wired: bool

    def send(self, message_id: str, to: str, msg: dict) -> SendResult: ...


class VoiceDialer(Protocol):
    wired: bool

    def send(self, message_id: str, to: str, msg: dict) -> SendResult: ...


class NotWiredSender:
    wired = False

    def __init__(self, name: str):
        self.name = name

    def send(self, message_id, to, msg) -> SendResult:
        return SendResult("not_wired")


class LeadSource(Protocol):
    wired: bool

    def fetch(self, limit: int) -> list[dict]: ...


class NotWiredSource:
    wired = False

    def __init__(self, name: str):
        self.name = name

    def fetch(self, limit: int) -> list[dict]:
        raise NotWired(self.name)


@dataclass(frozen=True)
class Delivery:
    status: str                       # delivered | refused | unavailable
    reference: Optional[str] = None


class Handoff(Protocol):
    wired: bool

    def deliver(self, handoff_id: str, payload: dict) -> Delivery: ...


class NotWiredHandoff:
    wired = False

    def deliver(self, handoff_id, payload) -> Delivery:
        return Delivery("unavailable")


@dataclass(frozen=True)
class ContractCheck:
    status: str                       # in_force | not_in_force | unavailable


class LegalContracts(Protocol):
    wired: bool

    def in_force(self, account_id: str, contract_kind: str, contract_ref: str) -> ContractCheck: ...


class NotWiredLegal:
    wired = False

    def in_force(self, account_id, contract_kind, contract_ref) -> ContractCheck:
        return ContractCheck("unavailable")


@dataclass
class Ports:
    email: EmailSender
    sms: SmsSender
    voice: VoiceDialer
    sources: dict
    onboarding: Handoff
    finance: Handoff
    legal: LegalContracts

    @classmethod
    def default(cls) -> "Ports":
        return cls(NotWiredSender("email"), NotWiredSender("sms"), NotWiredSender("voice"),
                   {"public_data": NotWiredSource("public_data"), "paid_provider": NotWiredSource("paid_provider")},
                   NotWiredHandoff(), NotWiredHandoff(), NotWiredLegal())

    def wired(self) -> dict:
        return {"email": self.email.wired, "sms": self.sms.wired, "voice": self.voice.wired,
                "public_data": self.sources["public_data"].wired, "paid_provider": self.sources["paid_provider"].wired,
                "onboarding": self.onboarding.wired, "finance": self.finance.wired, "legal": self.legal.wired}
