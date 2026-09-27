"""
Outbound ports (Legal spec §E.2). House rule (BUILD_CONTRACTS §0): a department or provider that does not exist
yet is reached only through an interface whose default stand-in answers "not allowed yet" / "not delivered" /
"unavailable" — never "fine". Every class here is such a stand-in; the passing fakes live in tests/fakes.py
only (spec G2: no passing fake is importable from ``src/``).

Every port call is recorded on the ledger first (``crossing_<port>_requested``, ids and hashes only) by the
service; an exception from a port is read as its fail-closed answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class Delivery:
    """Answer of every notify-style port: delivered or not (a stand-in is always not)."""

    delivered: bool
    reason: str = ""
    reference: Optional[str] = None


NOT_BUILT = "not built yet: not delivered"


# --- Compliance (38) -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProposalAnswer:
    """``status``: created (201/200), refused (4xx: Compliance read it and said no), unavailable (transport,
    5xx, timeout, unwired: retried by the proposal-delivery job)."""

    status: str
    compliance_proposal_id: Optional[str] = None
    http_status: Optional[int] = None


@dataclass(frozen=True)
class ComplianceRow:
    available: bool
    obligation_id: str = ""
    effective_status: Optional[str] = None
    register_version: Optional[int] = None


class CompliancePort(Protocol):
    def create_proposal(self, request_id: str, body: dict) -> ProposalAnswer: ...

    def row(self, obligation_id: str) -> ComplianceRow: ...


class NotWiredCompliance:
    """LEGAL_COMPLIANCE_URL / _TOKEN / _CALLER_TOKEN unset."""

    def create_proposal(self, request_id, body):
        return ProposalAnswer("unavailable")

    def row(self, obligation_id):
        return ComplianceRow(False, obligation_id)


# --- E-sign provider -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvelopeAnswer:
    available: bool
    envelope_id: Optional[str] = None


class ESignProvider(Protocol):
    def create_envelope(self, doc_id: str, version: str, doc_sha256: str, signer_refs: list[str]) -> EnvelopeAnswer: ...

    def status(self, envelope_id: str) -> Optional[str]: ...

    def certificate(self, envelope_id: str) -> Optional[bytes]: ...


class NotWiredESignProvider:
    def create_envelope(self, doc_id, version, doc_sha256, signer_refs):
        return EnvelopeAnswer(False)

    def status(self, envelope_id):
        return None

    def certificate(self, envelope_id):
        return None


# --- Counsel channel (D1: engagement TBD) -----------------------------------------------------------------------

class CounselChannel(Protocol):
    def deliver(self, package: dict) -> Delivery: ...


class NotWiredCounselChannel:
    def deliver(self, package):
        return Delivery(False, "no counsel engaged (LEGAL_COUNSEL_CHANNEL unset): not delivered")


# --- Cybersecurity (22): auto-delete freeze --------------------------------------------------------------------

class Cybersecurity22Port(Protocol):
    def freeze(self, hold_id: str, systems: list[str], subject_refs: list[str]) -> Delivery: ...


class NotBuiltCybersecurity22:
    def freeze(self, hold_id, systems, subject_refs):
        return Delivery(False, "Cybersecurity (22) is not built: not frozen")


# --- People (43), Clipper Network, Creative, Finance, Onboarding, V&I, push -----------------------------------------

class NotifyPort(Protocol):
    def notify(self, kind: str, subject_id: str, payload: dict) -> Delivery: ...


@dataclass
class NotBuiltDepartment:
    name: str

    def notify(self, kind, subject_id, payload):
        return Delivery(False, f"{self.name} {NOT_BUILT}")


class NotWiredPush:
    def notify(self, kind, subject_id, payload):
        return Delivery(False, "Andre's push channel is not wired: not delivered")


@dataclass
class Ports:
    compliance: object = field(default_factory=NotWiredCompliance)
    esign: object = field(default_factory=NotWiredESignProvider)
    counsel: object = field(default_factory=NotWiredCounselChannel)
    cybersecurity_22: object = field(default_factory=NotBuiltCybersecurity22)
    people_43: object = field(default_factory=lambda: NotBuiltDepartment("People (43)"))
    clipper_network: object = field(default_factory=lambda: NotBuiltDepartment("Clipper Network"))
    creative_production: object = field(default_factory=lambda: NotBuiltDepartment("Creative Production"))
    finance_31: object = field(default_factory=lambda: NotBuiltDepartment("Finance (31)"))
    onboarding: object = field(default_factory=lambda: NotBuiltDepartment("Onboarding"))
    verification_integrity: object = field(default_factory=lambda: NotBuiltDepartment("Verification and Integrity"))
    push: object = field(default_factory=NotWiredPush)


STAND_INS = (NotWiredCompliance, NotWiredESignProvider, NotWiredCounselChannel, NotBuiltCybersecurity22,
             NotBuiltDepartment, NotWiredPush)
