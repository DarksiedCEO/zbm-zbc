"""
Ports to everything outside New Business Development (ADR 0016 decision 22). Each has a fail-closed stand-in that
says what is missing; /nbd/v1/status lists which are wired. No provider or department client is built yet: setting
the switch that would select one refuses start (config.NOT_BUILT). A port that raises is treated as unavailable and
its exception text is dropped. No port is ever called with the service lock held.

- ``EmailSender``: send one outreach email. Stand-in: ``not_wired`` — the message stays ``queued``, visibly, and
  nothing is recorded as sent.
- ``SubmissionPort``: deliver one Andre-approved bid / RFP / RFQ response or pitch, and reconcile one whose outcome
  is unknown (``submission_status``). Stand-in: ``not_wired`` — the submission stays ``queued`` (re-checked, deadline
  included, every time the job looks at it); its status answer is ``unknown``.
- ``BidSource``: fetch bid opportunities from a portal. Stand-in raises ``NotWired``; no fetching code exists here.
- ``Handoff`` x2 (Onboarding: create the client; Finance: draft the first invoice) for a won pursuit. Stand-in:
  ``unavailable`` — the hand-off stays ``pending_delivery``, retried by ``handoff-retry``. A real adapter must be
  idempotent on ``handoff_id``.
- ``FinancePayouts``: hand one partner payout request to Finance (31), which owns payees, tax checks and the Stripe
  rail; this service never calls Stripe. Stand-in: ``not_wired`` — the payout stays ``queued``.
- ``LegalAgreements`` (legal_client.py): hand a partner agreement / NDA / white-label contract to Legal (37), and ask
  whether one is in force. Stand-in: ``unavailable`` — refused ``LEGAL_UNAVAILABLE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

NOT_WIRED = "not_wired"


class NotWired(Exception):
    pass


@dataclass(frozen=True)
class SendResult:
    status: str                       # accepted | failed | not_wired
    provider_ref: Optional[str] = None


class EmailSender(Protocol):
    wired: bool

    def send(self, message_id: str, to: str, msg: dict) -> SendResult: ...


class NotWiredSender:
    wired = False

    def __init__(self, name: str):
        self.name = name

    def send(self, message_id, to, msg) -> SendResult:
        return SendResult(NOT_WIRED)


class SubmissionPort(Protocol):
    wired: bool

    def submit(self, submission_id: str, pursuit_id: str, content_sha256: str, text: str) -> SendResult:
        """``accepted`` (with a provider reference) = delivered; ``refused`` = certainly not delivered; anything else
        (or an exception) is an unknown outcome: the submission stays ``sending`` and is reconciled."""
        ...

    def submission_status(self, submission_id: str) -> SendResult:
        """Reconcile one submission whose outcome is unknown: ``accepted`` (with a reference), ``refused``, or
        anything else = still unknown."""
        ...


class NotWiredSubmission:
    wired = False

    def submit(self, submission_id, pursuit_id, content_sha256, text) -> SendResult:
        return SendResult(NOT_WIRED)

    def submission_status(self, submission_id) -> SendResult:
        return SendResult("unknown")


class BidSource(Protocol):
    wired: bool

    def fetch(self, limit: int) -> list[dict]: ...


class NotWiredBidSource:
    wired = False

    def fetch(self, limit: int) -> list[dict]:
        raise NotWired("bid_source")


@dataclass(frozen=True)
class Delivery:
    status: str                       # delivered | refused | unavailable | not_wired
    reference: Optional[str] = None


class Handoff(Protocol):
    wired: bool

    def deliver(self, handoff_id: str, payload: dict) -> Delivery: ...


class NotWiredHandoff:
    wired = False

    def deliver(self, handoff_id, payload) -> Delivery:
        return Delivery("unavailable")


class FinancePayouts(Protocol):
    wired: bool

    def request_payout(self, payout_id: str, payload: dict) -> Delivery:
        """``payload``: ids, the finance payee ref, the tax reference, the amount (string) and the deal; Finance
        decides, gates and pays (its own maker/checker). ``delivered`` = Finance took the request; ``refused`` =
        Finance certainly did not take it; anything else (or an exception) is an unknown outcome."""
        ...

    def payout_status(self, payout_id: str) -> Delivery:
        """Reconcile one request whose outcome is unknown: ``with_finance`` (with Finance's reference), ``refused``,
        or anything else = still unknown (the payout stays ``sending``)."""
        ...


class NotWiredPayouts:
    wired = False

    def request_payout(self, payout_id, payload) -> Delivery:
        return Delivery(NOT_WIRED)

    def payout_status(self, payout_id) -> Delivery:
        return Delivery("unknown")


@dataclass
class Ports:
    email: EmailSender
    submission: SubmissionPort
    bid_source: BidSource
    onboarding: Handoff
    finance: Handoff
    payouts: FinancePayouts
    legal: object

    @classmethod
    def default(cls) -> "Ports":
        from legal_client import NotWiredLegal
        return cls(NotWiredSender("email"), NotWiredSubmission(), NotWiredBidSource(), NotWiredHandoff(),
                   NotWiredHandoff(), NotWiredPayouts(), NotWiredLegal())

    def wired(self) -> dict:
        return {k: bool(getattr(getattr(self, k), "wired", False))
                for k in ("email", "submission", "bid_source", "onboarding", "finance", "payouts", "legal")}
