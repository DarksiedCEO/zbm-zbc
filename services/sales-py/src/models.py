"""Request bodies (strict: unknown fields refused, no coercion, frozen). Responses are plain dicts built by the
service. Money and percentages are canonical strings, checked again by money.py where they are used."""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from intelligences import i01_intake, i03_scoring, i04_routing, i05_suppression, i06_consent


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _printable(v: str) -> str:
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in v):
        raise ValueError("control characters are refused")
    return v


def _printable_lines(v: str) -> str:
    if any((ord(c) < 0x20 and c not in "\n\t") or 0x7F <= ord(c) <= 0x9F for c in v):
        raise ValueError("control characters are refused")
    return v


Id = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Hex64 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[StrictStr, Field(min_length=1, max_length=100), AfterValidator(_printable)]
ShortText = Annotated[StrictStr, Field(min_length=1, max_length=200), AfterValidator(_printable)]
Note = Annotated[StrictStr, Field(min_length=1, max_length=500), AfterValidator(_printable)]
Money = Annotated[StrictStr, Field(max_length=18)]
Pct = Annotated[StrictStr, Field(max_length=6)]
Timestamp = Annotated[StrictStr, Field(pattern=r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]"
                                              r"(\.[0-9]{1,6})?(Z|[+-][0-2][0-9]:[0-5][0-9])$")]
Brand = Literal["zbm", "zbc"]
ProductLine = Literal[i04_routing.ALL_LINES]  # type: ignore[valid-type]
Industry = Literal[i03_scoring.INDUSTRIES]  # type: ignore[valid-type]
Stage = Literal["new", "qualified", "meeting", "proposal", "negotiation", "closed_lost"]


class ContactIn(Strict):
    name: Name
    email: Optional[Annotated[StrictStr, Field(max_length=254)]] = None
    phone: Optional[Annotated[StrictStr, Field(max_length=32)]] = None
    title: Optional[Name] = None
    time_zone: Optional[Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$",
                                                   max_length=64)]] = None


class AccountIn(Strict):
    name: ShortText
    domain: Optional[Annotated[StrictStr, Field(max_length=253)]] = None
    industry: Optional[Industry] = None
    employees_band: Optional[Literal[tuple(i03_scoring.EMPLOYEES)]] = None  # type: ignore[valid-type]
    revenue_band: Optional[Literal[tuple(i03_scoring.REVENUE)]] = None  # type: ignore[valid-type]


class EvidenceIn(Strict):
    kind: Literal["site_form", "zbc_campaign_inquiry", "rr_scan", "referral_note", "partner_note"]
    ref: Id
    captured_at: Timestamp
    form_version: Optional[Id] = None
    scan_findings: Optional[Annotated[StrictInt, Field(ge=0, le=10_000)]] = None


class SignalsIn(Strict):
    monthly_ad_spend_band: Optional[Literal[tuple(i03_scoring.AD_SPEND)]] = None  # type: ignore[valid-type]
    timeline: Optional[Literal[tuple(i03_scoring.TIMELINE)]] = None  # type: ignore[valid-type]
    requested_call: StrictBool = False
    budget_stated: StrictBool = False


class ReferrerIn(Strict):
    name: Name
    ref: Id


class LeadIn(Strict):
    request_id: Id
    source: Literal[i01_intake.API_SOURCES]  # type: ignore[valid-type]
    brand: Optional[Brand] = None
    product_interest: Annotated[list[ProductLine], Field(max_length=10)] = []
    contact: ContactIn
    account: Optional[AccountIn] = None
    evidence: EvidenceIn
    signals: SignalsIn = SignalsIn()
    referrer: Optional[ReferrerIn] = None


class ImportEvidence(Strict):
    kind: Literal["public_record", "provider_record"]
    ref: Id
    captured_at: Timestamp


class ImportedLead(Strict):
    """One record a public-data or paid-provider port returns; validated like an API lead."""
    brand: Optional[Brand] = None
    product_interest: Annotated[list[ProductLine], Field(max_length=10)] = []
    contact: ContactIn
    account: Optional[AccountIn] = None
    evidence: ImportEvidence
    signals: SignalsIn = SignalsIn()


class LeadImport(Strict):
    request_id: Id
    source: Literal["public_data", "paid_provider"]
    limit: Annotated[StrictInt, Field(ge=1, le=500)] = 50


class Owner(Strict):
    request_id: Id
    owner: Id


class Disqualify(Strict):
    request_id: Id
    reason_code: Literal["not_a_fit", "no_budget", "duplicate", "unreachable", "competitor", "other"]


class Convert(Strict):
    request_id: Id
    owner: Optional[Id] = None


class TimeZoneSet(Strict):
    request_id: Id
    time_zone: Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$", max_length=64)]


class DisplayName(Strict):
    request_id: Id
    display_name: Annotated[StrictStr, Field(min_length=1, max_length=40)]


class FirstName(Strict):
    request_id: Id
    first_name: Annotated[StrictStr, Field(min_length=1, max_length=40)]


class StageSet(Strict):
    request_id: Id
    stage: Stage


class ActivityIn(Strict):
    request_id: Id
    target_kind: Literal["lead", "opportunity", "account"]
    target_id: Id
    kind: Literal["note", "call_logged", "meeting_booked", "meeting_held"]
    note: Optional[Note] = None


class TaskClose(Strict):
    request_id: Id
    outcome: Literal["done", "dismissed"]


class HoldDecision(Strict):
    request_id: Id
    decision: Literal["not_an_opt_out", "opt_out"]


class ConsentGrant(Strict):
    request_id: Id
    contact_id: Id
    channel: Literal[i06_consent.CHANNELS]  # type: ignore[valid-type]
    brand: Brand
    source: Literal[i06_consent.SOURCES]  # type: ignore[valid-type]
    captured_at: Timestamp
    consent_text_version: Id
    consent_text_sha256: Hex64


class ConsentRevoke(Strict):
    request_id: Id
    contact_id: Optional[Id] = None
    phone: Optional[Annotated[StrictStr, Field(max_length=32)]] = None
    source: Literal["reply", "form", "call", "manual"]


class Suppress(Strict):
    request_id: Id
    contact_id: Optional[Id] = None
    email: Optional[Annotated[StrictStr, Field(max_length=254)]] = None
    phone: Optional[Annotated[StrictStr, Field(max_length=32)]] = None
    reason: Literal[i05_suppression.REASONS]  # type: ignore[valid-type]


class Unsubscribe(Strict):
    request_id: Id
    token: Annotated[StrictStr, Field(pattern=r"^sl-msg-[0-9a-f]{40}\.[0-9a-f]{32}$")]


Body = Annotated[StrictStr, Field(min_length=1, max_length=5000), AfterValidator(_printable_lines)]
Subject = Annotated[StrictStr, Field(min_length=1, max_length=120), AfterValidator(_printable)]


class TemplateCreate(Strict):
    request_id: Id
    brand: Brand
    channel: Literal["email", "sms"]
    name: Annotated[StrictStr, Field(pattern=r"^[a-z0-9_]{1,40}$")]
    subject: Optional[Subject] = None
    body: Body


class TemplateEdit(Strict):
    request_id: Id
    subject: Optional[Subject] = None
    body: Body


class HashApproval(Strict):
    request_id: Id
    content_sha256: Hex64


class OutreachMessage(Strict):
    request_id: Id
    contact_id: Id
    template_id: Id
    version: Annotated[StrictInt, Field(ge=1, le=10_000)]


class OutreachVoice(Strict):
    request_id: Id
    contact_id: Id
    brand: Brand
    purpose: Literal["follow_up", "book_call"]


class MessageCancel(Strict):
    request_id: Id


class EmailEvent(Strict):
    request_id: Id
    message_id: Id
    event: Literal["delivered", "soft_bounce", "hard_bounce", "complaint"]


class ReplyIn(Strict):
    request_id: Id
    channel: Literal["email", "sms", "voice"]
    message_id: Optional[Id] = None
    from_email: Optional[Annotated[StrictStr, Field(max_length=254)]] = None
    from_phone: Optional[Annotated[StrictStr, Field(max_length=32)]] = None
    text: Annotated[StrictStr, Field(min_length=0, max_length=10_000)]   # an empty (media-only) reply still holds


class PriceApprove(Strict):
    request_id: Id
    version: Annotated[StrictInt, Field(ge=1, le=10_000)]
    price: Optional[Money] = None
    markup_pct: Optional[Pct] = None


class PriceWithdraw(Strict):
    request_id: Id
    version: Annotated[StrictInt, Field(ge=1, le=10_000)]


class ProposalLineIn(Strict):
    line_id: Annotated[StrictStr, Field(pattern=r"^(zbm|zbc)\.[a-z0-9_]{1,60}$")]
    quantity: Annotated[StrictInt, Field(ge=1, le=100_000)] = 1
    media_cost: Optional[Money] = None
    markup_pct: Optional[Pct] = None


class ProposalCreate(Strict):
    request_id: Id
    opportunity_id: Id
    lines: Annotated[list[ProposalLineIn], Field(min_length=1, max_length=20)]
    discount: Money = "0.00"
    custom_terms: Optional[Annotated[StrictStr, Field(min_length=1, max_length=2000),
                                     AfterValidator(_printable_lines)]] = None


class ProposalSend(Strict):
    request_id: Id
    contract_kind: Literal["client_msa", "order_form"]
    contract_ref: Id


class Acceptance(Strict):
    kind: Literal["legal_acceptance", "esign_envelope"]
    ref: Id


class ProposalWon(Strict):
    """Won needs Andre (dashboard + his token) or the client's recorded acceptance, confirmed by Legal (S1-L1)."""
    request_id: Id
    acceptance: Optional[Acceptance] = None


class ProposalLost(Strict):
    request_id: Id
    reason_code: Literal["price", "timing", "competitor", "no_decision", "not_a_fit", "other"]


class JobRun(Strict):
    request_id: Id
