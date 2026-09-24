"""
Interfaces to departments that do not exist yet, with fail-closed stand-ins.

House rule (BUILD_CONTRACTS section 0): a department that does not exist is
reached only through an interface whose default answers "not allowed yet",
never "fine". Every stand-in here does exactly that. There are NO passing
implementations in `src/` — test-only passing fakes live in tests/fakes.py
so a production code path can never be wired to one by accident.

- Compliance (38)            — hard gate after ZBM Quality and on ZBC payout.
- Verification and Integrity — view verification, bot screening, stolen-clip
                                detection, minimum-days-live; also attests
                                results before Creative Memory may learn.
- Legal (37)                 — sign-offs (AI generative fill on creator
                                footage, AGPL code, reuse bridge).
- Finance (31)               — pays. Creative never calls it; the port exists
                                so the boundary is explicit (see ADR 0005).
- Clipper Network            — receives rulebook-version announcements.
- Enigma / Phantom Canvas    — Andre's external creative agents (separate
                                repos). Creative only commissions through
                                this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class GateResult:
    department: str
    allowed: bool
    reason: str
    reference: str | None = None


# --- Compliance (38) ---------------------------------------------------------

class Compliance38Port(Protocol):
    def review(self, subject_kind: str, subject_id: str, facts: dict) -> GateResult: ...


class NotBuiltCompliance38:
    def review(self, subject_kind: str, subject_id: str, facts: dict) -> GateResult:
        return GateResult("compliance_38", False, "Compliance (38) is not built yet: not allowed yet")


# --- Verification and Integrity -----------------------------------------------

@dataclass(frozen=True)
class VerificationAttestation:
    subject_id: str
    verified: bool
    reason: str
    attestation_id: str | None = None
    checks: dict = field(default_factory=dict)


class VerificationIntegrityPort(Protocol):
    def attest_clip(self, submission_id: str, facts: dict) -> VerificationAttestation:
        """Views verified, bot screen, stolen-clip check, minimum days live."""

    def attest_result(self, result_id: str, facts: dict) -> VerificationAttestation:
        """Is a reported performance result real? Gate for Creative Memory."""


class NotBuiltVerificationIntegrity:
    REASON = "Verification and Integrity is not built yet: unverified, not allowed yet"

    def attest_clip(self, submission_id: str, facts: dict) -> VerificationAttestation:
        return VerificationAttestation(submission_id, False, self.REASON)

    def attest_result(self, result_id: str, facts: dict) -> VerificationAttestation:
        return VerificationAttestation(result_id, False, self.REASON)


# --- Legal (37) ----------------------------------------------------------------

class Legal37Port(Protocol):
    def signoff(self, topic: str, subject_id: str, facts: dict) -> GateResult: ...


class NotBuiltLegal37:
    def signoff(self, topic: str, subject_id: str, facts: dict) -> GateResult:
        return GateResult("legal_37", False, f"Legal (37) is not built yet: no sign-off for {topic!r}, not allowed yet")


# --- Finance (31) ----------------------------------------------------------------

class Finance31Port(Protocol):
    def accept_payout_handoff(self, submission_id: str, facts: dict) -> GateResult: ...


class NotBuiltFinance31:
    def accept_payout_handoff(self, submission_id: str, facts: dict) -> GateResult:
        return GateResult("finance_31", False, "Finance (31) is not built yet: not allowed yet")


# --- Clipper Network ---------------------------------------------------------------

class ClipperNetworkPort(Protocol):
    def announce_rulebook_version(self, campaign_id: str, version: int, facts: dict) -> GateResult: ...


class NotBuiltClipperNetwork:
    def announce_rulebook_version(self, campaign_id: str, version: int, facts: dict) -> GateResult:
        return GateResult("clipper_network", False, "Clipper Network is not built yet: announcement not delivered")


# --- Enigma / Phantom Canvas contract -----------------------------------------------

@dataclass(frozen=True)
class CommissionRequest:
    request_id: str
    requested_by_layer: str   # "zbm" | "zbc"
    agent: str                # "enigma" | "phantom_canvas"
    spec: dict


@dataclass(frozen=True)
class CommissionReceipt:
    request_id: str
    agent: str
    commissioned: bool
    reason: str
    external_ref: str | None = None


class CreativeAgentsPort(Protocol):
    def commission(self, request: CommissionRequest) -> CommissionReceipt: ...


class NotWiredCreativeAgents:
    def commission(self, request: CommissionRequest) -> CommissionReceipt:
        return CommissionReceipt(
            request.request_id, request.agent, False,
            f"{request.agent} contract endpoint is not wired (external repo, interface only): not commissioned",
        )


@dataclass
class Departments:
    compliance: Compliance38Port = field(default_factory=NotBuiltCompliance38)
    verification: VerificationIntegrityPort = field(default_factory=NotBuiltVerificationIntegrity)
    legal: Legal37Port = field(default_factory=NotBuiltLegal37)
    finance: Finance31Port = field(default_factory=NotBuiltFinance31)
    clipper_network: ClipperNetworkPort = field(default_factory=NotBuiltClipperNetwork)
    creative_agents: CreativeAgentsPort = field(default_factory=NotWiredCreativeAgents)
