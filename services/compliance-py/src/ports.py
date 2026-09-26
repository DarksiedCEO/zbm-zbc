"""
Ports to departments and providers that do not exist yet (spec §0.13, §I).

House rule: a department that is not built is reached only through an
interface whose default answers "not allowed yet" — never "fine". Every
``NotBuilt*`` / ``NotWired*`` class below is that default and is the ONLY
implementation in src/. Passing fakes live in tests/fakes.py, so no
production path can be wired to a department that says "fine" (H.28).

Every call through a port is recorded on the ledger first as
``crossing_<port>_requested`` (service.RecordedPorts), and an answer with
``available=False`` is an unmet ``dependency_unavailable:<port>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

NOT_ALLOWED_YET = "not allowed yet"


# --- Verification and Integrity ------------------------------------------------

@dataclass(frozen=True)
class AgeAttestation:
    available: bool
    status: str = "unknown"            # adult | minor | unknown
    attestation_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class ClipAttestation:
    """HR-13 verified-view attestation for one clip."""

    available: bool
    verified_views: bool = False           # platform-reported at settlement, after platform filtering
    anomaly_screen_passed: bool = False
    purchased_engagement: bool = True
    still_live_at_minimum_period: bool = False
    copyright_strike: bool = True
    attestation_id: Optional[str] = None
    reason: str = ""


class VerificationIntegrityPort(Protocol):
    def age_attestation(self, attestation_id: str) -> AgeAttestation: ...

    def attest_clip(self, submission_id: str, post_ref: str, platform: str, posted_at: str,
                    settlement_lag_days: int) -> ClipAttestation: ...


class NotBuiltVerificationIntegrity:
    REASON = "Verification and Integrity is not built yet: not allowed yet"

    def age_attestation(self, attestation_id):
        return AgeAttestation(False, reason=self.REASON)

    def attest_clip(self, submission_id, post_ref, platform, posted_at, settlement_lag_days):
        return ClipAttestation(False, reason=self.REASON)


# --- Finance (31) -----------------------------------------------------------------

@dataclass(frozen=True)
class TaxStatus:
    available: bool
    form_kind: Optional[str] = None        # w9 | w8ben | w8bene
    form_on_file: bool = False
    tin_match: Optional[bool] = None
    w8_current: Optional[bool] = None
    services_outside_us_attested: Optional[bool] = None
    reason: str = ""


@dataclass(frozen=True)
class RailStatus:
    available: bool
    status: str = "unknown"                # verified | pending | restricted | unknown
    payouts_enabled: bool = False
    reason: str = ""


class Finance31Port(Protocol):
    def tax_status(self, payee_id: str) -> TaxStatus: ...

    def rail_status(self, payee_id: str) -> RailStatus: ...


class NotBuiltFinance31:
    REASON = "Finance (31) is not built yet: not allowed yet"

    def tax_status(self, payee_id):
        return TaxStatus(False, reason=self.REASON)

    def rail_status(self, payee_id):
        return RailStatus(False, reason=self.REASON)


# --- Legal (37) --------------------------------------------------------------------

@dataclass(frozen=True)
class DocVersion:
    available: bool
    current_version: Optional[str] = None
    reason: str = ""


class Legal37Port(Protocol):
    def current_version(self, doc_id: str) -> DocVersion: ...


class NotBuiltLegal37:
    def current_version(self, doc_id):
        return DocVersion(False, reason="Legal (37) is not built yet: not allowed yet")


# --- Sanctions screening provider (spec C.6) -----------------------------------------

@dataclass(frozen=True)
class ScreenAnswer:
    result: str                              # clear | potential_match | match | unavailable
    list_version: Optional[str] = None
    screened_at: Optional[str] = None        # RFC 3339
    provider_ref: Optional[str] = None


class SanctionsScreeningProvider(Protocol):
    def screen(self, legal_name: str, aliases: list[str], dob: Optional[str], country: str,
               region: Optional[str]) -> ScreenAnswer: ...

    def current_list_version(self) -> Optional[str]: ...


class NotWiredSanctionsProvider:
    """COMPLIANCE_SANCTIONS_PROVIDER unset: every screen is ``unavailable``."""

    def screen(self, legal_name, aliases, dob, country, region):
        return ScreenAnswer("unavailable")

    def current_list_version(self):
        return None


# --- Accessibility checker (spec C.8) -------------------------------------------------

@dataclass(frozen=True)
class A11yAnswer:
    available: bool
    passed: bool = False
    standard: str = "WCAG 2.1 AA"
    violations_count: int = 0
    report_sha256: Optional[str] = None
    tool: Optional[str] = None
    tool_version: Optional[str] = None
    checked_at: Optional[str] = None
    covers_captions: bool = False            # provider covers WCAG 1.2.x (captions) for video
    overlay_scripts_disabled: bool = False   # scan ran with overlay widgets disabled


class AccessibilityChecker(Protocol):
    def check(self, asset_ref: str, asset_type: str, content_sha256: str) -> A11yAnswer: ...


class NotWiredAccessibilityChecker:
    """COMPLIANCE_A11Y_PROVIDER unset: every check is unavailable."""

    def check(self, asset_ref, asset_type, content_sha256):
        return A11yAnswer(False)


STAND_INS = (NotBuiltVerificationIntegrity, NotBuiltFinance31, NotBuiltLegal37, NotWiredSanctionsProvider,
             NotWiredAccessibilityChecker)
