"""
Ports to everything outside Influencer & Partnership Marketing (ADR 0015 decision 17). Each has a fail-closed stand-in
that says what is missing; /inf/v1/status lists which are wired. No provider or department client is built yet:
setting the switch that would select one refuses start (config.NOT_BUILT). No port is ever called with the service
lock held.

- ``EmailSender``: send one outreach email. Stand-in ``not_wired``: the message stays ``queued``, visibly.
- ``DmSender``: send one Andre-approved platform DM (Instagram, TikTok, X, YouTube). There is no DM provider at all:
  stand-in ``not_wired``, approved DMs stay ``queued``.
- ``DiscoverySource`` x2 (public profiles, the paid influencer database): fetch candidate profiles. Stand-in: raises
  ``NotWired`` (503 ``SOURCE_NOT_WIRED``). There is no scraping code in this service.
- ``LegalContracts`` (Legal 37): send the influencer agreement for a deal and read whether it is in force. Stand-in:
  ``unavailable`` (503 ``LEGAL_UNAVAILABLE``), so no contract can be sent and no deal becomes ``contracted``.
- ``FinancePayees`` (Finance 31): register the influencer as a payee from a tax REFERENCE (never a raw TIN), read the
  payee's verification (Stripe Connect KYC and TIN match happen at Finance and Stripe, never here) with Finance's
  per-PERSON key — an opaque keyed hash of the matched TIN, computed by Finance, so two payees with different references
  but one TIN are one person here (AEGIS R2-N2; the stand-in returns none) — and hand Finance a payout request. This service never calls Stripe. Stand-in: ``unavailable`` for every call (503
  ``FINANCE_UNAVAILABLE``): no payee is ever verified and no payout is ever requested. A real adapter must be
  idempotent on ``payee_id`` and ``payout_id`` (a retry after a lost answer sends the same request again).
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


class DmSender(Protocol):
    wired: bool

    def send(self, message_id: str, platform: str, handle: str, text: str) -> SendResult: ...


class NotWiredSender:
    wired = False

    def __init__(self, name: str):
        self.name = name

    def send(self, *a, **k) -> SendResult:
        return SendResult("not_wired")


class DiscoverySource(Protocol):
    wired: bool

    def fetch(self, query: dict, limit: int) -> list[dict]: ...


class NotWiredSource:
    wired = False

    def __init__(self, name: str):
        self.name = name

    def fetch(self, query: dict, limit: int) -> list[dict]:
        raise NotWired(self.name)


@dataclass(frozen=True)
class ContractSend:
    status: str                       # sent | refused | unavailable
    envelope_ref: Optional[str] = None


@dataclass(frozen=True)
class ContractCheck:
    status: str                       # in_force | not_in_force | unavailable


class LegalContracts(Protocol):
    wired: bool

    def send_contract(self, deal_id: str, influencer_id: str, deal_sha256: str, brief_sha256: str,
                      disclosure: str) -> ContractSend: ...

    def contract_status(self, deal_id: str, envelope_ref: str, deal_sha256: str) -> ContractCheck: ...


class NotWiredLegal:
    wired = False

    def send_contract(self, *a, **k) -> ContractSend:
        return ContractSend("unavailable")

    def contract_status(self, *a, **k) -> ContractCheck:
        return ContractCheck("unavailable")


@dataclass(frozen=True)
class PayeeAnswer:
    status: str                       # registered | refused | unavailable
    payee_ref: Optional[str] = None
    person_key: Optional[str] = None  # AEGIS R2-N2: Finance's opaque keyed hash of the MATCHED TIN (one per person)


@dataclass(frozen=True)
class PayeeStatus:
    status: str                       # verified | pending | refused | unavailable
    person_key: Optional[str] = None


@dataclass(frozen=True)
class PayoutAnswer:
    status: str                       # accepted | refused | unavailable
    finance_ref: Optional[str] = None


class FinancePayees(Protocol):
    wired: bool

    def register_payee(self, payee_id: str, tax_ref: str, tax_form: str, legal_form: str, country: str,
                       identity_ref: str) -> PayeeAnswer:
        """``identity_ref``: the creator's CONFIRMED identity (record id and the SHA-256 of the confirmed address) for
        Finance to match the payee's KYC against (AEGIS R1-M2)."""
        ...

    def payee_status(self, payee_ref: str) -> PayeeStatus: ...

    def request_payout(self, payout_id: str, payee_ref: str, amount: str, currency: str, brand: str,
                       deal_id: str) -> PayoutAnswer: ...


class NotWiredFinance:
    wired = False

    def register_payee(self, *a, **k) -> PayeeAnswer:
        return PayeeAnswer("unavailable")

    def payee_status(self, *a, **k) -> PayeeStatus:
        return PayeeStatus("unavailable")

    def request_payout(self, *a, **k) -> PayoutAnswer:
        return PayoutAnswer("unavailable")


@dataclass
class Ports:
    email: EmailSender
    dm: DmSender
    sources: dict
    legal: LegalContracts
    finance: FinancePayees

    @classmethod
    def default(cls) -> "Ports":
        return cls(NotWiredSender("email"), NotWiredSender("dm"),
                   {"public_profile": NotWiredSource("public_profile"),
                    "paid_database": NotWiredSource("paid_database")},
                   NotWiredLegal(), NotWiredFinance())

    def wired(self) -> dict:
        return {"email": self.email.wired, "dm": self.dm.wired,
                "public_profile": self.sources["public_profile"].wired,
                "paid_database": self.sources["paid_database"].wired,
                "legal": self.legal.wired, "finance": self.finance.wired}
