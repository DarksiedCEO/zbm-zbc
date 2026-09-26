"""
Ports to the departments and providers Clipper Network depends on (spec §E
"Thin clients", §I, BUILD_CONTRACTS §0).

House rule: a department that does not answer is reached only through an
interface whose default answers "not allowed yet" — never "fine". Every
``NotBuilt*`` / ``NotWired*`` class below is that default and is the ONLY
implementation in src/ besides the HTTP thin clients (``httpclients.py``,
V&I, Compliance and Creative, wired only when fully configured). Passing
fakes live in tests/fakes.py (guardrail G3 parses src/ for them).

Every call through a port is recorded on the ledger first as
``crossing_<port>_requested`` (service.PortCalls); an answer with
``available=False`` — or a port that raises — is an unmet
``DEPENDENCY_UNAVAILABLE:<port>``, never a pass.

CN never trusts another service's boolean about a clipper (§0.3): it asks
the owning department — V&I (age, identity, connections, integrity,
strikes, certifications), Compliance (jurisdiction, activation), Finance
(tax form on file, rate card, open items), Legal (agreement version),
Creative (rulebook, kit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

NOT_ALLOWED_YET = "not allowed yet"


# --- Verification and Integrity ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgeAnswer:
    available: bool
    status: str = "unknown"                 # adult | minor | unknown
    attestation_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class IdentityAnswer:
    available: bool
    status: str = "incomplete"              # clear | duplicate | incomplete
    finding_ids: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Connection:
    connection_id: str
    platform: str
    status: str                             # pending | active | revoked | expired | refused


@dataclass(frozen=True)
class ConnectionsAnswer:
    available: bool
    connections: tuple[Connection, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class StartAnswer:
    available: bool
    started: bool = False
    connection_id: Optional[str] = None
    authorization_url: Optional[str] = None
    state_expires_at: Optional[str] = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompleteAnswer:
    available: bool
    connection_id: Optional[str] = None
    status: str = "refused"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ack:
    available: bool
    ok: bool = False
    reference: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class IntegrityAnswer:
    available: bool
    clear: bool = False                     # no open S3 and no hold on the identity
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Strike:
    strike_id: str
    clipper_id: str
    strike_class: str                       # S1 | S2 | S3
    status: str                             # active | expired | overturned
    rule_id: str                            # the V&I rule id
    finding_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    issued_at: str
    subject_refs: tuple[str, ...] = ()      # submission ids the findings are about (clip flags)


@dataclass(frozen=True)
class StrikeFeed:
    available: bool
    strikes: tuple[Strike, ...] = ()
    next_cursor: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class Finding:
    finding_id: str
    clipper_id: str
    kind: str
    status: str                             # open | upheld | overturned
    evidence_ids: tuple[str, ...]
    subject_ref: Optional[str] = None


@dataclass(frozen=True)
class FindingAnswer:
    available: bool
    finding: Optional[Finding] = None       # None = V&I does not know this id
    reason: str = ""


@dataclass(frozen=True)
class Certification:
    certification_id: str
    submission_id: str
    campaign_id: str
    platform: str
    status: str                             # pending | certified | not_certified | revised | voided
    certified_views: Optional[int]
    revision_watch_end: Optional[str] = None


@dataclass(frozen=True)
class CertificationsAnswer:
    available: bool
    certifications: tuple[Certification, ...] = ()
    reason: str = ""


class VerificationIntegrityPort(Protocol):
    def age_subject(self, clipper_id: str) -> AgeAnswer: ...

    def age_check(self, request_id: str, clipper_id: str, dob: str, dob_field_neutral: bool, method: str,
                  provider_session_ref: str) -> AgeAnswer: ...

    def identity_check(self, request_id: str, clipper_id: str, email: str) -> IdentityAnswer: ...

    def connections(self, clipper_id: str) -> ConnectionsAnswer: ...

    def connection_start(self, request_id: str, clipper_id: str, platform: str, redirect_uri: str) -> StartAnswer: ...

    def connection_complete(self, request_id: str, state: str, code: str) -> CompleteAnswer: ...

    def connection_revoke(self, request_id: str, connection_id: str) -> Ack: ...

    def integrity(self, clipper_id: str) -> IntegrityAnswer: ...

    def strikes(self, cursor: Optional[str]) -> StrikeFeed: ...

    def finding(self, finding_id: str) -> FindingAnswer: ...

    def certifications(self, clipper_id: str) -> CertificationsAnswer: ...

    def ban(self, request_id: str, clipper_id: str, cn_decision_id: str, approved_at: str,
            andre_token: Optional[str]) -> Ack:
        """V&I's ban route needs Andre's own approval token (AEGIS N16-2): CN passes through the exact token it
        received on Andre's ban-decision request, in memory only; without one the ban is never sent."""
        ...


class NotBuiltVerificationIntegrity:
    REASON = "Verification and Integrity is not reachable from Clipper Network: not allowed yet"

    def age_subject(self, clipper_id):
        return AgeAnswer(False, reason=self.REASON)

    def age_check(self, request_id, clipper_id, dob, dob_field_neutral, method, provider_session_ref):
        return AgeAnswer(False, reason=self.REASON)

    def identity_check(self, request_id, clipper_id, email):
        return IdentityAnswer(False, reason=self.REASON)

    def connections(self, clipper_id):
        return ConnectionsAnswer(False, reason=self.REASON)

    def connection_start(self, request_id, clipper_id, platform, redirect_uri):
        return StartAnswer(False, reasons=(self.REASON,))

    def connection_complete(self, request_id, state, code):
        return CompleteAnswer(False, reasons=(self.REASON,))

    def connection_revoke(self, request_id, connection_id):
        return Ack(False, reason=self.REASON)

    def integrity(self, clipper_id):
        return IntegrityAnswer(False, reasons=(self.REASON,))

    def strikes(self, cursor):
        return StrikeFeed(False, reason=self.REASON)

    def finding(self, finding_id):
        return FindingAnswer(False, reason=self.REASON)

    def certifications(self, clipper_id):
        return CertificationsAnswer(False, reason=self.REASON)

    def ban(self, request_id, clipper_id, cn_decision_id, approved_at, andre_token):
        return Ack(False, reason=self.REASON)


# --- Compliance (38) ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JurisdictionAnswer:
    available: bool
    jurisdiction_class: str = "refuse"      # operate | conditional | refuse
    resolution_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class ComplianceRuling:
    available: bool
    allowed: bool = False
    ruling_id: Optional[str] = None
    unmet_lines: tuple[str, ...] = ()
    reason: str = ""


class CompliancePort(Protocol):
    def resolve_person(self, request_id: str, declared_country: str, declared_region: Optional[str], attested: bool,
                       attestation_ref: str) -> JurisdictionAnswer: ...

    def creator_activation(self, request_id: str, clipper_id: str, facts: dict) -> ComplianceRuling: ...

    def latest_activation(self, lane: str, subject_id: str) -> ComplianceRuling: ...

    def review_email_campaign(self, request_id: str, subject_id: str, facts: dict) -> ComplianceRuling: ...


class NotBuiltCompliance:
    REASON = "Compliance (38) is not reachable from Clipper Network: not allowed yet"

    def resolve_person(self, request_id, declared_country, declared_region, attested, attestation_ref):
        return JurisdictionAnswer(False, reason=self.REASON)

    def creator_activation(self, request_id, clipper_id, facts):
        return ComplianceRuling(False, reason=self.REASON)

    def latest_activation(self, lane, subject_id):
        return ComplianceRuling(False, reason=self.REASON)

    def review_email_campaign(self, request_id, subject_id, facts):
        return ComplianceRuling(False, reason=self.REASON)


# --- Creative Production -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RulebookAnswer:
    available: bool
    live_version: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class KitAnswer:
    available: bool
    kit_id: Optional[str] = None
    rulebook_version: Optional[int] = None
    status: str = "unknown"                 # signed | draft | unknown
    kit_sha256: Optional[str] = None        # SHA-256 of the kit exactly as read (CN never edits it)
    reason: str = ""


class CreativePort(Protocol):
    def live_rulebook(self, campaign_id: str) -> RulebookAnswer: ...

    def kit(self, campaign_id: str) -> KitAnswer: ...


class NotBuiltCreative:
    REASON = "Creative Production is not reachable from Clipper Network: not allowed yet"

    def live_rulebook(self, campaign_id):
        return RulebookAnswer(False, reason=self.REASON)

    def kit(self, campaign_id):
        return KitAnswer(False, reason=self.REASON)


# --- Finance (31) --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaxAnswer:
    available: bool
    form_on_file: bool = False              # the ONLY tax fact CN ever keeps (at decision time)
    reason: str = ""


@dataclass(frozen=True)
class RateCardAnswer:
    available: bool
    published: bool = False
    doc_id: Optional[str] = None
    version: Optional[str] = None
    sha256: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class OpenItemsAnswer:
    available: bool
    state: str = "unknown"                  # none | open | unknown
    reason: str = ""


class FinancePort(Protocol):
    def tax_status(self, clipper_id: str) -> TaxAnswer: ...

    def rate_card(self, doc_id: str, version: str) -> RateCardAnswer: ...

    def open_items(self, clipper_id: str) -> OpenItemsAnswer: ...

    def notify_offboarding(self, clipper_id: str, offboarding_id: str) -> Ack: ...


class NotBuiltFinance31:
    REASON = "Finance (31) is not built yet: not allowed yet"

    def tax_status(self, clipper_id):
        return TaxAnswer(False, reason=self.REASON)

    def rate_card(self, doc_id, version):
        return RateCardAnswer(False, reason=self.REASON)

    def open_items(self, clipper_id):
        return OpenItemsAnswer(False, reason=self.REASON)

    def notify_offboarding(self, clipper_id, offboarding_id):
        return Ack(False, reason=self.REASON)


# --- Legal (37) ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DocVersionAnswer:
    available: bool
    version: Optional[str] = None
    doc_sha256: Optional[str] = None
    reason: str = ""


class LegalPort(Protocol):
    def current_version(self, doc_id: str) -> DocVersionAnswer: ...


class NotBuiltLegal37:
    def current_version(self, doc_id):
        return DocVersionAnswer(False, reason="Legal (37) is not built yet: no current version, not allowed yet")


# --- People (43): delegates for disputes ---------------------------------------------------------------------


@dataclass(frozen=True)
class DelegateAnswer:
    available: bool
    active: bool = False
    reason: str = ""


class PeoplePort(Protocol):
    def delegate_active(self, name: str) -> DelegateAnswer: ...


class NotBuiltPeople43:
    def delegate_active(self, name):
        return DelegateAnswer(False, reason="People (43) is not built yet: no delegate roster, Andre only")


# --- Messaging provider, hub, push -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SendAnswer:
    delivered: bool
    provider_ref: Optional[str] = None
    reason: str = ""


class MessagingProvider(Protocol):
    def send(self, message_id: str, channel: str, recipient: str, body: str) -> SendAnswer: ...


class NotWiredMessagingProvider:
    """CN_MESSAGE_PROVIDER unset: nothing is delivered (recorded as ``not_delivered``)."""

    def send(self, message_id, channel, recipient, body):
        return SendAnswer(False, reason="no messaging provider is configured: not delivered")


class HubPort(Protocol):
    def revoke_session(self, clipper_id: str) -> Ack: ...


class NotBuiltHub:
    def revoke_session(self, clipper_id):
        return Ack(False, reason="the clipper hub is not built: no session to revoke there")


class PushPort(Protocol):
    def push(self, topic: str, briefing: dict) -> Ack: ...


class NotWiredPush:
    def push(self, topic, briefing):
        return Ack(False, reason="push channel to Andre's phone is not wired: not delivered")


@dataclass
class Ports:
    vi: VerificationIntegrityPort = field(default_factory=NotBuiltVerificationIntegrity)
    compliance: CompliancePort = field(default_factory=NotBuiltCompliance)
    creative: CreativePort = field(default_factory=NotBuiltCreative)
    finance: FinancePort = field(default_factory=NotBuiltFinance31)
    legal: LegalPort = field(default_factory=NotBuiltLegal37)
    people: PeoplePort = field(default_factory=NotBuiltPeople43)
    messaging: MessagingProvider = field(default_factory=NotWiredMessagingProvider)
    hub: HubPort = field(default_factory=NotBuiltHub)
    push: PushPort = field(default_factory=NotWiredPush)


STAND_INS = (NotBuiltVerificationIntegrity, NotBuiltCompliance, NotBuiltCreative, NotBuiltFinance31, NotBuiltLegal37,
             NotBuiltPeople43, NotWiredMessagingProvider, NotBuiltHub, NotWiredPush)
