"""
onboarding_schema — domain contracts for the Onboarding department
(docs/adr/0004-onboarding-department-architecture.md).

Rules enforced here, by construction rather than convention:

1. LabeledValue (Revenue Recovery's rule, ADR 0001 Decision 6): no dollar
   figure exists without BOTH a value classification and a decision
   confidence. Onboarding keeps its own copy of the enums (same values as
   detection-py's ``zbm_schema``) because services do not import each
   other's packages; the values must stay identical.
2. Money is Decimal on the inside and a two-decimal string on the wire
   (BUILD_CONTRACTS.md section 1) — see ``money.py``.
3. NO SECRET FIELD EXISTS on any model in this package. Access grants are
   OAuth grant *metadata* only (platform, account id, role, type, dates).
   Every inbound model is ``extra="forbid"``, so a request that tries to
   smuggle ``password`` / ``access_token`` / ``refresh_token`` is rejected
   (and the API's validation handler never echoes the rejected input).
4. Identifiers that reach the ledger (client ids, creator ids, brand ids)
   match the ledger's ``subject_id`` pattern ``[A-Za-z0-9._:-]{1,128}``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import Enum
from typing import Annotated, Any, ClassVar, Optional

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from redaction import contains_credential, refuse_credentials, scrub_obj

from .money import (  # noqa: F401
    Money,
    PositiveMoney,
    PositiveWireMoney,
    WireMoney,
    client_stated_amount,
    money_str,
    parse_wire_money,
    to_money,
)


def _not_credential_like(v: str) -> str:
    # An identifier is echoed in responses, URLs and ledger entries. One that
    # looks like a credential is refused rather than echoed. The message
    # never repeats the value.
    if contains_credential(v):
        raise ValueError("identifier looks like a credential; refused")
    return v


SubjectId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$"), AfterValidator(_not_credential_like)
]
ShortText = Annotated[str, StringConstraints(max_length=500)]
LongText = Annotated[str, StringConstraints(max_length=20000)]
AccountId = Annotated[str, StringConstraints(min_length=1, max_length=128), AfterValidator(_not_credential_like)]


class Inbound(BaseModel):
    """Base for request-side models.

    - Unknown fields are rejected (so ``password`` / ``access_token`` cannot
      be smuggled in; the API's 422 handler never echoes the input).
    - A credential-shaped string anywhere in the incoming data is REFUSED
      (422 with ``redaction.CREDENTIAL_REFUSAL``, telling the client never to
      send credentials) instead of being stored and scrubbed later
      (fix wave 1, F10). Fields listed in ``SCRUB_ONLY_FIELDS`` carry
      third-party content the client did not type (a public web page, raw
      account-pull rows); they are scrubbed, not refused.
    - Every string is then still passed through ``redaction.scrub_obj``
      (second layer).
    """

    model_config = ConfigDict(extra="forbid")
    SCRUB_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _refuse_credentials_at_ingest(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for k, v in data.items():
            if k not in cls.SCRUB_ONLY_FIELDS:
                refuse_credentials(v)
        return scrub_obj(data)


class Outbound(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Labels (identical values to detection-py zbm_schema)
# ---------------------------------------------------------------------------


class ValueClassification(str, Enum):
    OBSERVED = "observed"
    ATTRIBUTED = "attributed"
    INCREMENTAL = "incremental"
    FINANCIALLY_VERIFIED = "financially_verified"


class DecisionConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


CONFIDENCE_RANK = {
    DecisionConfidence.LOW: 1,
    DecisionConfidence.MEDIUM: 2,
    DecisionConfidence.HIGH: 3,
    DecisionConfidence.VERY_HIGH: 4,
}


class LabeledValue(BaseModel):
    """A dollar amount that cannot exist without both labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_usd: PositiveMoney
    classification: ValueClassification
    confidence: DecisionConfidence

    def render(self) -> str:
        """The ONLY way a dollar figure is written into client-facing text.
        The guarantee/money filter rejects any ``$`` figure without this
        label suffix."""
        return (
            f"${money_str(self.amount_usd)} ({self.classification.value}, "
            f"{self.confidence.value} confidence)"
        )


# ---------------------------------------------------------------------------
# Lanes, platforms, profile
# ---------------------------------------------------------------------------


class Lane(str, Enum):
    CLIENT = "client"
    ZBC_CREATOR = "zbc_creator"
    ZBC_BRAND = "zbc_brand"


class Platform(str, Enum):
    GOOGLE_ADS = "google_ads"
    META = "meta"
    SHOPIFY = "shopify"
    TIKTOK = "tiktok"
    GOOGLE_TAG_MANAGER = "google_tag_manager"
    GOOGLE_ANALYTICS = "google_analytics"


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    CHAT = "chat"
    PHONE = "phone"


class Provenance(str, Enum):
    CONTRACT = "contract"
    CLIENT_CONFIRMED = "client_confirmed"
    ACCOUNT_PULL = "account_pull"
    CLIENT_STATED = "client_stated"
    WEBSITE = "website"
    INFERRED = "inferred"


class Person(Inbound):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    email: Annotated[str, StringConstraints(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")]
    role: Optional[ShortText] = None


class ProfileField(Outbound):
    name: str
    value: Any
    confidence: DecisionConfidence
    provenance: Provenance
    evidence: str
    observed_at: datetime
    conflict: bool = False
    conflicting_values: list[Any] = Field(default_factory=list)


class ProfileGap(Outbound):
    field: str
    priority: int
    reason: str  # "missing" | "low_confidence" | "conflict"
    prefill: Any = None  # P13: confirm, don't type


class ClientProfile(Outbound):
    client_id: str
    lane: Lane
    fields: dict[str, ProfileField]
    gaps: list[ProfileGap]
    dropped_fields: list[str] = Field(default_factory=list)  # minimum-data rule


# ---------------------------------------------------------------------------
# Access (metadata only — no secrets, ever)
# ---------------------------------------------------------------------------


class AccountType(str, Enum):
    BUSINESS = "business"
    PERSONAL = "personal"
    UNKNOWN = "unknown"


class AccessTier(str, Enum):
    OAUTH = "tier1_oauth"
    VAULT = "tier2_vault"
    PASSWORD_LAST_RESORT = "tier3_password"


class AccessGrantIn(Inbound):
    """OAuth / delegated-access grant METADATA. There is deliberately no
    field that can hold a token, password or secret."""

    platform: Platform
    account_id: AccountId
    account_name: Optional[ShortText] = None
    account_type: AccountType
    granted_role: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    granted_by_email: Optional[ShortText] = None
    account_last_activity_at: Optional[AwareDatetime] = None
    scopes: list[Annotated[str, StringConstraints(max_length=128)]] = Field(default_factory=list)
    job: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "audit"


class AccessProblem(Outbound):
    code: str  # personal_account | stale_account | insufficient_role | unknown_account_type | live_check_*
    fix_instruction: str


class AccessVerification(Outbound):
    platform: Platform
    account_id: str
    metadata_ok: bool
    live_verified: bool
    usable: bool
    problems: list[AccessProblem]
    client_message: str


class PermissionReceipt(Outbound):
    platform: Platform
    can_see: list[str]
    cannot_see: list[str]
    how_to_revoke: list[str]
    text: str


# ---------------------------------------------------------------------------
# Findings from Revenue Recovery (consumed, never re-derived)
# ---------------------------------------------------------------------------


class ConsumedLabeledValue(LabeledValue):
    """A labeled value as it arrives from Revenue Recovery: the amount must
    be the canonical contract string (section 1), never rounded, never a
    JSON number, never an exponent (fix wave 1, F14)."""

    amount_usd: PositiveWireMoney


class ConsumedFinding(BaseModel):
    """A detection-py Finding as Onboarding consumes it. ``extra`` ignored
    so detection-py can add fields without breaking Onboarding."""

    model_config = ConfigDict(extra="ignore")

    finding_id: str
    agent_id: str
    leak_category: str
    entity_type: str
    entity_id: str
    customer_id: str
    cause_certainty: str  # "named" | "uncertain"
    cause_description: str
    recoverable_value: Optional[ConsumedLabeledValue] = None
    detected_at: Optional[datetime] = None
    double_count_risk: bool = False


class Baseline(Outbound):
    finding_count: int
    by_category: dict[str, int]
    # Totals are kept PER CLASSIFICATION — observed and attributed dollars
    # are never added together into one unlabeled number.
    totals_by_classification: dict[str, Money]
    uncertain_findings: list[str]
    double_count_entities: list[str]
    excluded_from_totals: list[str]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanItem(Outbound):
    topic: str
    service: str
    source: str  # "client" | "audit" | "both"
    client_rank: Optional[int] = None
    evidence_finding_ids: list[str] = Field(default_factory=list)
    value: Optional[LabeledValue] = None
    note: str = ""


class Disagreement(Outbound):
    topic: str
    client_view: str
    audit_view: str
    recommendation: str
    needs_client_choice: bool = True


class MergedPlan(Outbound):
    client_id: str
    items: list[PlanItem]  # client's order is preserved — never silently reordered
    disagreements: list[Disagreement]
    recommended_order: list[str]  # shown alongside, not applied
    summary_text: str


# ---------------------------------------------------------------------------
# Commitments, escalations, briefings
# ---------------------------------------------------------------------------


class CommitmentStatus(str, Enum):
    OPEN = "open"
    ON_TRACK = "on_track"  # owner confirmed it will be met
    KEPT = "kept"
    RESCHEDULED = "rescheduled"
    BREACHED = "breached"


class Commitment(Outbound):
    commitment_id: str
    client_id: str
    kind: str  # "escalation_callback" | "deliverable" | "callback"
    category: str
    text: str
    owner: str
    created_at: datetime
    due_at: datetime
    status: CommitmentStatus = CommitmentStatus.OPEN
    engaged: bool = False
    # True only once a nudge push to Andre was confirmed delivered (fix wave 2).
    andre_nudged: bool = False
    # Undelivered attempts of the current nudge; retried while below
    # OnboardingConfig.andre_nudge_max_attempts.
    andre_nudge_failures: int = 0
    # The breach nudge (sent when the time passed) was not delivered yet.
    breach_nudge_pending: bool = False
    client_warned: bool = False
    proposed_new_due_at: Optional[datetime] = None


class TriggerKind(str, Enum):
    HUMAN_REQUESTED = "human_requested"
    DEAL_SIZE = "deal_size"
    STUCK = "stuck"
    FRICTION = "friction"
    AUDIT_ANOMALY = "audit_anomaly"
    LOW_RECOMMEND_SCORE = "low_recommend_score"


# Locked spec: HARD = always escalate, no agent discretion. Everything else
# is SOFT: the agent makes ONE resolution attempt first.
HARD_TRIGGERS = frozenset({TriggerKind.HUMAN_REQUESTED, TriggerKind.DEAL_SIZE})
SOFT_TRIGGERS = frozenset({TriggerKind.STUCK, TriggerKind.FRICTION, TriggerKind.AUDIT_ANOMALY, TriggerKind.LOW_RECOMMEND_SCORE})


class EscalationDecisionKind(str, Enum):
    ESCALATE = "escalate"
    ATTEMPT_RESOLUTION = "attempt_resolution"
    NO_ACTION = "no_action"


class BriefingPack(Outbound):
    """Exactly the contents the locked spec lists — nothing more."""

    who_the_client_is: str
    what_the_account_pull_found: str
    what_the_client_said_they_care_about: str
    exactly_where_the_snag_is: str
    what_the_agent_already_tried: str
    sixty_second_summary: str
    the_one_decision: str
    recommended_action: str


class Escalation(Outbound):
    escalation_id: str
    client_id: str
    trigger: TriggerKind
    hard: bool
    reason: str
    snag: str
    attempted_resolution: Optional[str]
    raised_at: datetime
    briefing: BriefingPack
    push_delivered: bool
    push_detail: str
    push_attempts: int = 0  # initial push + tick retries (bounded; fix wave 3)
    client_commitment_text: str
    client_commitment_due_at: datetime
    client_message_status: str  # "released" | "held: ..."
    commitment_id: Optional[str]
    acknowledged_at: Optional[datetime] = None
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class GateResult(Outbound):
    gate: str  # "contract_14" | "compliance_15"
    passed: bool
    unmet: list[str]
    drift: list[str] = Field(default_factory=list)


class ActivationDecision(Outbound):
    subject_id: str
    lane: Lane
    activated: bool
    contract: Optional[GateResult]
    compliance: GateResult
    unmet: list[str]  # union, each prefixed with its gate
    handoff: Optional[dict] = None


# ---------------------------------------------------------------------------
# Contract terms (as read from contract storage)
# ---------------------------------------------------------------------------


class ContractTerms(Inbound):
    client_id: SubjectId
    signed: bool
    signed_at: Optional[AwareDatetime] = None
    start_date: date
    end_date: Optional[date] = None
    services: list[str]  # e.g. ["revenue_recovery", "digital_advertising"]
    allowed_commitment_categories: list[str] = Field(default_factory=lambda: ["callback", "report", "audit"])
    monthly_spend_cap_usd: Optional[Money] = None
    ccpa_cpra_clause_present: bool = False


# ---------------------------------------------------------------------------
# ZBC creators / brands
# ---------------------------------------------------------------------------


class ClipperApplication(Inbound):
    """A ZBC clipper application. There is deliberately NO guardian /
    parental-consent field (18+ is written in stone, no guardian process)
    and NO tax-identifier field: the W-9 itself lives with ZBC payouts/tax;
    Onboarding only records that it was received (P8, minimum data)."""

    creator_id: SubjectId
    legal_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    date_of_birth: Optional[date] = None
    # There is deliberately NO application-date field: age is computed
    # against the SERVER's clock (fix wave 1, F1). A request that carries
    # ``applied_on`` (or any other unknown field) is rejected with 422.
    time_zone: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "America/Los_Angeles"
    platforms: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list)
    follower_count: int = Field(ge=0)
    avg_engagement_rate: float = Field(ge=0, le=1)  # a ratio, not money
    follower_growth_30d_ratio: float = Field(ge=0, default=0.0)
    fake_follower_ratio: Optional[float] = Field(default=None, ge=0, le=1)
    engagement_pod_signal: float = Field(ge=0, le=1, default=0.0)
    brand_safety_flags: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list)
    content_history_posts: int = Field(ge=0, default=0)
    bio: Annotated[str, StringConstraints(max_length=5000)] = ""
    network_fit_tags: list[Annotated[str, StringConstraints(max_length=64)]] = Field(default_factory=list)
    w9_received: bool = False  # P8: collected at signup; the form itself is not stored here
    creator_agreement_signed: bool = False
    disclosure_training_completed: bool = False  # P9


class VettingOutcome(str, Enum):
    APPROVE = "approve"
    DECLINE = "decline"
    SEND_TO_ANDRE = "send_to_andre"
    INCOMPLETE = "incomplete"


class VettingDecision(Outbound):
    creator_id: str
    outcome: VettingOutcome
    reasons: list[str]
    injection_flags: list[str] = Field(default_factory=list)


class DistributionModel(str, Enum):
    RENTED = "rented"
    OWNED = "owned"


class CampaignPlan(Outbound):
    brand_id: str
    campaign_id: str
    distribution: DistributionModel
    owned_addon_offered: bool
    owned_addon_selected: bool
    owned_addon_note: str
    proving_campaign: dict
    success_measures: list[str]
    requires_per_campaign_approval: bool = True
    approved: bool = False
    scale_allowed: bool = False


class DomainEvent(Outbound):
    """An event published to the shared bus (spec: "Publish events to a
    shared bus"). The bus itself is not built; see integrations.bus."""

    event_type: str
    subject_id: str
    payload: dict
    at: datetime


class LedgerEventRecord(Outbound):
    event_id: str
    department: str
    event_type: str
    actor: str
    subject_id: str
    payload_sha256: str
    summary: str
