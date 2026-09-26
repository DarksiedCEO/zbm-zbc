"""
Request models (strict: unknown fields refused -> 422, so ``age_verified``,
``guardian_consent``, ``w9_on_file`` or any other caller boolean is refused
at the edge — CN never accepts a caller's word for age, tax or compliance).

No model carries a money field, an amount, a rate or a float (guardrail G1):
``rate_card_ref`` is ``{finance_doc_id, version, sha256}`` and ``view_terms``
are integers (views, not money).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from clock import parse_iso
from jurisdictions import is_known, is_known_country, is_known_subdivision
from textguard import display_name_problem, has_control_chars, is_email, money_or_earnings

AGE_METHODS = ("open_banking", "photo_id_match", "facial_age_estimation", "mobile_operator", "credit_card",
               "digital_identity", "email_age_estimation")   # Compliance C.1 AGE_METHODS
PLATFORMS = ("youtube", "tiktok", "instagram", "x")
APPLICATION_CHANNELS = ("inbound_form", "referral", "discord_server_post", "email_opt_in")


def _no_control(v: str) -> str:
    if has_control_chars(v):
        raise ValueError("control characters are not accepted")
    return v


def _no_control_ml(v: str) -> str:
    if has_control_chars(v, allow_newlines=True):
        raise ValueError("control characters are not accepted")
    return v


def _email(v: str) -> str:
    if not is_email(v):
        raise ValueError("not an email address")
    return v


def _ts(v: str) -> str:
    try:
        parse_iso(v)
    except ValueError:
        raise ValueError("must be an RFC 3339 timestamp with a UTC offset") from None
    return v


def _display_name(v: str) -> str:
    why = display_name_problem(v)
    if why:
        raise ValueError(f"display_name: {why}")
    return v


def _no_money(v: str) -> str:
    if money_or_earnings(v):
        raise ValueError("money values and earnings claims are not accepted here (CN-26)")
    return v


def _bounded_json(v: Optional[dict]) -> Optional[dict]:
    import json

    if v is not None and len(json.dumps(v, separators=(",", ":"), default=str)) > 8192:
        raise ValueError("larger than 8 KB")
    return v


Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Email = Annotated[str, Field(min_length=3, max_length=254), AfterValidator(_email)]
Ts = Annotated[str, Field(max_length=40), AfterValidator(_ts)]
Iso2 = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
Region = Annotated[str, Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{1,3}$")]
Code = Annotated[str, Field(pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")]
Tz = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_+\-/]{1,64}$")]
Note = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_no_control)]
Tier = Literal["T0", "T1", "T2", "T3"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRequest(Strict):
    request_id: Id


# --- recruiting ----------------------------------------------------------------------------------------------

class OptInRequest(Strict):
    request_id: Id
    email: Email
    recipient_country: Iso2
    time_zone: Tz
    consent_text_sha256: Sha
    source_form_id: Id
    captured_at: Ts
    age_18_plus_confirmed: StrictBool

    @model_validator(mode="after")
    def _check(self):
        if not is_known_country(self.recipient_country):
            raise ValueError("recipient_country is not a known ISO 3166-1 code")
        if self.age_18_plus_confirmed is not True:
            raise ValueError("recruiting never targets minors: the opt-in form must confirm 18+ (CN-08)")
        return self


class OptOutRequest(Strict):
    request_id: Id
    email: Email


class RecruitingCampaignRequest(Strict):
    request_id: Id
    channel: Literal["email_opt_in", "discord_server_post"]          # SMS / Reddit / X do not exist here (CN-09)
    template_id: Literal["recruiting_invite"]
    recipients: list[Annotated[str, Field(min_length=1, max_length=254), AfterValidator(_no_control)]] = \
        Field(default_factory=list, max_length=1000)
    discord_server_ref: Optional[Id] = None


# --- application and the relays -------------------------------------------------------------------------------

class ApplicationRequest(Strict):
    request_id: Id
    display_name: Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_no_control),
                            AfterValidator(_display_name)]          # AEGIS N16-11
    email: Email
    declared_country: Iso2
    declared_region: Optional[Region] = None
    jurisdiction_attested: StrictBool
    time_zone: Optional[Tz] = None
    channel: Literal[APPLICATION_CHANNELS]  # type: ignore[valid-type]
    referrer_clipper_id: Optional[Id] = None
    opt_in_record_id: Optional[Id] = None
    declared_18_plus: StrictBool            # recorded; NEVER counts as age assurance (CN-01)
    sag_aftra_member: StrictBool
    statement: Optional[Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_no_control_ml)]] = None

    @model_validator(mode="after")
    def _codes(self):
        if not is_known_country(self.declared_country):
            raise ValueError("declared_country is not a known ISO 3166-1 alpha-2 code")
        if self.declared_region is not None and (not is_known_subdivision(self.declared_region)
                                                 or not self.declared_region.startswith(self.declared_country + "-")):
            raise ValueError("declared_region is not a known ISO 3166-2 code of that country")
        if self.channel == "email_opt_in" and not self.opt_in_record_id:
            raise ValueError("channel email_opt_in needs opt_in_record_id")
        if self.channel == "referral" and not self.referrer_clipper_id:
            raise ValueError("channel referral needs referrer_clipper_id")
        return self


class ConnectionStartRequest(Strict):
    request_id: Id
    platform: Literal[PLATFORMS]  # type: ignore[valid-type]
    redirect_uri: Annotated[str, Field(max_length=512, pattern=r"^https://[^\s]{1,500}$")]
    handle: Optional[Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_no_control)]] = None


class ConnectionCompleteRequest(Strict):
    request_id: Id
    state: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9._~:-]{1,512}$")]
    code: Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[\x21-\x7e]{1,2048}$")]


class AgeCheckRequest(Strict):
    request_id: Id
    dob: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]      # relayed to V&I, never stored or logged
    dob_field_neutral: StrictBool
    method: Literal[AGE_METHODS]  # type: ignore[valid-type]
    provider_session_ref: Id


class AgreementAcceptanceRequest(Strict):
    request_id: Id
    doc_id: Literal["clipper_agreement"]
    version: Id
    doc_sha256: Sha
    presented_sha256: Sha
    method: Literal["clickwrap_unticked_box"]
    box_ticked: StrictBool
    session_ref: Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_no_control)]


class TrainingRequest(Strict):
    request_id: Id
    training_version: Id
    attested: StrictBool


# --- campaigns --------------------------------------------------------------------------------------------------

class ViewTerms(Strict):
    min_views_to_review: StrictInt = Field(ge=0, le=10**9)
    max_paid_views_per_clip: StrictInt = Field(ge=1, le=10**12)


class RateCardRef(Strict):
    finance_doc_id: Id
    version: Id
    sha256: Sha


class NetworkConfigRequest(Strict):
    request_id: Id
    min_tier: Tier
    platforms: list[Literal[PLATFORMS]] = Field(min_length=1, max_length=4)  # type: ignore[valid-type]
    clipper_jurisdictions: list[Code] = Field(min_length=1, max_length=60)
    max_clippers: StrictInt = Field(ge=1, le=100_000)
    max_submissions_per_clipper: StrictInt = Field(ge=1, le=10_000)
    view_terms: ViewTerms
    rate_card_ref: RateCardRef
    rate_card_effective_at: Ts
    opens_at: Ts
    closes_at: Ts

    @model_validator(mode="after")
    def _check(self):
        if len(set(self.platforms)) != len(self.platforms) or len(set(self.clipper_jurisdictions)) != len(self.clipper_jurisdictions):
            raise ValueError("platforms and clipper_jurisdictions must not repeat")
        bad = [c for c in self.clipper_jurisdictions if not is_known(c)]
        if bad:
            raise ValueError("clipper_jurisdictions holds a code that is not a known ISO 3166 code")
        if parse_iso(self.opens_at) >= parse_iso(self.closes_at):
            raise ValueError("opens_at must be before closes_at")
        if self.view_terms.min_views_to_review > self.view_terms.max_paid_views_per_clip:
            raise ValueError("min_views_to_review exceeds max_paid_views_per_clip")
        return self


class AnnouncementRequest(Strict):
    request_id: Id
    version: StrictInt = Field(ge=1, le=1_000_000)
    facts: Annotated[dict[str, Any], AfterValidator(_bounded_json)] = Field(default_factory=dict)


class EnrolmentRequest(Strict):
    request_id: Id
    clipper_id: Id


class KitAckRequest(Strict):
    request_id: Id
    kit_delivery_id: Id
    kit_sha256: Sha
    rulebook_version: StrictInt = Field(ge=1, le=1_000_000)
    rate_card_version: Id
    rulebook_received: StrictBool
    disclosure_section_received: StrictBool


# --- register ---------------------------------------------------------------------------------------------------

class RuleProposalRequest(Strict):
    request_id: Id
    kind: Literal["new", "amend", "retire", "counsel_memo"]
    target_id: Optional[Annotated[str, Field(pattern=r"^CN-[A-Z0-9-]{2,12}$")]] = None
    rule: Optional[dict[str, Any]] = None
    memo: Optional[dict[str, Any]] = None


class TemplateProposalRequest(Strict):
    request_id: Id
    kind: Literal["new", "amend", "retire"]
    target_id: Optional[Annotated[str, Field(pattern=r"^[a-z_]{1,40}$")]] = None
    template: Optional[dict[str, Any]] = None


class Decision(Strict):
    proposal_id: Id
    content_sha256: Sha
    decision: Literal["approve", "reject"]
    note: Optional[Note] = None
    acknowledge_weakening: StrictBool = False


class DecisionsRequest(Strict):
    request_id: Id
    decisions: list[Decision] = Field(min_length=1, max_length=100)


# --- disputes, discipline, tiers, offboarding --------------------------------------------------------------------

class DisputeRequest(Strict):
    request_id: Id
    clipper_id: Id
    notice_message_id: Id
    subject_kind: Literal["clip_flag", "vi_finding", "strike", "ban", "suspension", "tier", "enrolment", "admission"]
    subject_ref: Id
    statement: Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(_no_control_ml)]
    evidence_refs: list[Id] = Field(default_factory=list, max_length=20)


class DisputeOutcomeRequest(Strict):
    request_id: Id
    outcome: Literal["appeal_granted", "appeal_denied"]
    note: Note


class BanDecisionRequest(Strict):
    request_id: Id
    proposal_id: Id
    decision: Literal["approve", "reject"]
    note: Note


class TierNominationRequest(Strict):
    request_id: Id
    nominate: StrictBool


class OffboardingRequest(Strict):
    request_id: Id
    trigger: Literal["clipper_request", "andre_decision"]
    keep_connections_until_settlement: Optional[StrictBool] = None


class ReconcileRequest(Strict):
    request_id: Id
    head_sha256: Sha
    void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)
    void_event_ids: list[Id] = Field(max_length=10_000)


REQUEST_MODELS = (RunRequest, OptInRequest, OptOutRequest, RecruitingCampaignRequest, ApplicationRequest,
                  ConnectionStartRequest, ConnectionCompleteRequest, AgeCheckRequest, AgreementAcceptanceRequest,
                  TrainingRequest, ViewTerms, RateCardRef, NetworkConfigRequest, AnnouncementRequest, EnrolmentRequest,
                  KitAckRequest, RuleProposalRequest, TemplateProposalRequest, Decision, DecisionsRequest,
                  DisputeRequest, DisputeOutcomeRequest, BanDecisionRequest, TierNominationRequest,
                  OffboardingRequest, ReconcileRequest)
