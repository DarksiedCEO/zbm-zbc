"""Request bodies (strict: unknown fields refused, no coercion, frozen; service-py's Strict base). Responses are plain
dicts built by the service. No model has a date of birth, government id, tax id, card, bank or phone field; api.py
also refuses such keys anywhere in a body before it is parsed. Money is a canonical two-decimal JSON STRING; a float
or an int in a money field is refused 422 (strict mode: no coercion)."""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from clock import parse_iso


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _printable(v: str) -> str:
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in v):
        raise ValueError("control characters are refused")
    if not v.strip():
        raise ValueError("must not be blank")
    return v


def _no_braces(v: str) -> str:
    if "{" in v or "}" in v:
        raise ValueError("braces are refused (they are template syntax)")
    return v


def _multiline(v: str) -> str:
    if any((ord(c) < 0x20 and c not in "\n\r\t") or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in v):
        raise ValueError("control characters are refused")
    if not v.strip():
        raise ValueError("must not be blank")
    return v


def _ts(v: str) -> str:
    parse_iso(v)
    return v


def _unique(v: list) -> list:
    if len(set(v)) != len(v):
        raise ValueError("items must be unique")
    return v


Id = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
NbId = Annotated[StrictStr, Field(pattern=r"^nb-[a-z]{3}-[0-9a-f]{40}$")]
Brand = Literal["zbm", "zbc"]
Slug = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{2,59}$")]
Ref = Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,20}:[A-Za-z0-9._-]{1,100}$")]
Domain = Annotated[StrictStr, Field(max_length=253,
                                    pattern=r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")]
Email = Annotated[StrictStr, Field(min_length=3, max_length=254)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=120), AfterValidator(_printable), AfterValidator(_no_braces)]
MergeName = Annotated[StrictStr, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9 .'&-]{1,40}$")]
Title = Annotated[StrictStr, Field(min_length=1, max_length=200), AfterValidator(_printable)]
Text = Annotated[StrictStr, Field(min_length=1, max_length=20000), AfterValidator(_multiline)]
ShortText = Annotated[StrictStr, Field(min_length=1, max_length=2000), AfterValidator(_multiline)]
Subject = Annotated[StrictStr, Field(min_length=1, max_length=200), AfterValidator(_printable)]
Timestamp = Annotated[StrictStr, Field(max_length=40), AfterValidator(_ts)]
Money = Annotated[StrictStr, Field(max_length=18, pattern=r"^(0|[1-9][0-9]{0,14})\.[0-9]{2}$")]
Pct = Annotated[StrictStr, Field(max_length=6, pattern=r"^(0|[1-9][0-9]?|100)\.[0-9]{2}$")]
Sha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Version = Annotated[StrictInt, Field(ge=1, le=100_000)]
TaxRef = Annotated[StrictStr, Field(max_length=80)]
FinanceRef = Annotated[StrictStr, Field(pattern=r"^fin:[A-Za-z0-9._-]{4,120}$")]


class RequestOnly(Strict):
    request_id: Id


# --------------------------------------------------------------------------------------------------- pursuits

class Counterparty(Strict):
    ref: Ref
    name: Name
    domain: Domain


class ChecklistRequest(Strict):
    code: Annotated[StrictStr, Field(pattern=r"^[a-z_]{3,60}$")]
    label: Optional[Title] = None


PursuitKind = Literal["rfp", "rfq", "enterprise_bid", "government_bid", "formal_pitch"]


class PursuitCreate(Strict):
    request_id: Id
    brand: Brand
    kind: PursuitKind
    title: Title
    counterparty: Counterparty
    value: Money
    deadline: Optional[Timestamp] = None
    notes: Optional[ShortText] = None
    source_ref: Optional[Ref] = None
    checklist: list[ChecklistRequest] = Field(default_factory=list, max_length=50)


class ValueSet(Strict):
    request_id: Id
    value: Money


class DeadlineSet(Strict):
    request_id: Id
    deadline: Timestamp


YesNo = Literal["yes", "no", "unknown"]


class Qualification(Strict):
    request_id: Id
    scope_fit: YesNo
    capacity: YesNo
    deadline_feasible: YesNo
    compliance_feasible: YesNo
    relationship: YesNo
    price_competitive: YesNo
    payment_terms_acceptable: YesNo


class BidDecision(Strict):
    request_id: Id
    decision: Literal["bid", "no_bid"]
    qualification_sha256: Sha256


class Attest(Strict):
    request_id: Id
    item_sha256: Sha256


class DealApproval(Strict):
    request_id: Id
    binding_sha256: Sha256


class Lost(Strict):
    request_id: Id
    reason_code: Literal["price", "scope", "incumbent", "timing", "no_decision", "disqualified", "relationship",
                         "other"]


class Import(Strict):
    request_id: Id
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20


class AgreementRequest(Strict):
    request_id: Id
    kind: Literal["referral_agreement", "alliance_agreement", "white_label_agreement", "nda"]


# --------------------------------------------------------------------------------------------------- blocks, responses

class BlockCreate(Strict):
    request_id: Id
    block_key: Slug
    brand: Literal["zbm", "zbc", "both"]
    title: Title
    text: Text


class BlockVersion(Strict):
    request_id: Id
    title: Title
    text: Text


class Approve(Strict):
    request_id: Id
    content_sha256: Sha256


class BlockPart(Strict):
    block_id: NbId
    version: Version


class CustomPart(Strict):
    custom: Text


Part = Union[BlockPart, CustomPart]


class ResponseCreate(Strict):
    request_id: Id
    pursuit_id: NbId
    parts: list[Part] = Field(min_length=1, max_length=120)


class ResponseVersion(Strict):
    request_id: Id
    parts: list[Part] = Field(min_length=1, max_length=120)


class ResponseApprove(Strict):
    request_id: Id
    version: Version
    content_sha256: Sha256
    acknowledged_flags: Annotated[list[Literal["GIFT", "LOBBYING", "CONFLICT_OF_INTEREST", "CONTINGENT_FEE"]],
                                  AfterValidator(_unique)] = Field(default_factory=list, max_length=4)


class ResponseSubmit(Strict):
    request_id: Id
    version: Version
    content_sha256: Sha256


# --------------------------------------------------------------------------------------------------- partners

class PartnerCreate(Strict):
    request_id: Id
    partner_key: Slug
    kind: Literal["referral", "agency_alliance", "white_label"]
    brands: Annotated[list[Brand], AfterValidator(_unique)] = Field(min_length=1, max_length=2)
    name: Name
    domain: Domain
    notes: Optional[ShortText] = None


class RatePropose(Strict):
    request_id: Id
    version: Version
    rate_pct: Pct


class RateApprove(Strict):
    request_id: Id
    version: Version
    binding_sha256: Sha256


class PayeeSet(Strict):
    request_id: Id
    finance_payee_ref: FinanceRef
    tax_info_ref: TaxRef


class PartnerDealCreate(Strict):
    request_id: Id
    partner_id: NbId
    brand: Brand
    counterparty: Counterparty
    deal_value: Money
    notes: Optional[ShortText] = None


class DealValueSet(Strict):
    request_id: Id
    deal_value: Money


class PartnerDealWon(Strict):
    request_id: Id
    agreement_kind: Literal["referral_agreement", "alliance_agreement", "white_label_agreement"]


class FinanceEvent(Strict):
    request_id: Id
    finance_event_id: Annotated[StrictStr, Field(pattern=r"^fin:[A-Za-z0-9._-]{4,120}$")]
    deal_id: NbId
    kind: Literal["payment", "refund", "chargeback"]
    amount: Money
    currency: Literal["USD"]


class PayoutPaid(Strict):
    request_id: Id
    finance_ref: FinanceRef


# --------------------------------------------------------------------------------------------------- outreach

class ContactCreate(Strict):
    request_id: Id
    brand: Brand
    email: Email
    name: Name
    partner_id: Optional[NbId] = None
    pursuit_id: Optional[NbId] = None


class MergeFields(Strict):
    request_id: Id
    first_name: Optional[MergeName] = None
    company: Optional[MergeName] = None


class TemplateCreate(Strict):
    request_id: Id
    template_key: Slug
    brand: Brand
    subject: Subject
    body: Text


class TemplateVersion(Strict):
    request_id: Id
    subject: Subject
    body: Text


class QueueEmail(Strict):
    request_id: Id
    contact_id: NbId
    template_id: NbId
    version: Version
    content_sha256: Sha256


class EmailEvent(Strict):
    request_id: Id
    message_id: NbId
    event: Literal["delivered", "soft_bounce", "hard_bounce", "complaint"]


class Reply(Strict):
    request_id: Id
    message_id: Optional[NbId] = None
    from_email: Optional[Email] = None
    text: Annotated[StrictStr, Field(min_length=1, max_length=20000)]


class Unsubscribe(Strict):
    request_id: Id
    token: Annotated[StrictStr, Field(pattern=r"^nb-msg-[0-9a-f]{40}\.[0-9a-f]{32}$")]


class Suppress(Strict):
    request_id: Id
    contact_id: Optional[NbId] = None
    email: Optional[Email] = None


class HoldDecision(Strict):
    request_id: Id
    decision: Literal["resume", "opt_out"]

