"""Request bodies (strict: unknown fields refused, no coercion, frozen; service-py's Strict base). Responses are plain
dicts built by the service. No model has a date of birth, government id, tax id, card, bank or phone field; api.py
also refuses such keys anywhere in a body before it is parsed. Money is a canonical two-decimal JSON STRING; a float
or an int in a money field is refused 422 (strict mode: no coercion)."""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, BeforeValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr

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


def _no_tax_id_shape(v: str) -> str:
    """A human-entered key, ref, name or domain is SCANNED, not capped (AEGIS round 2 N1): refused when, NFKC-
    normalised with every separator dropped, it holds a run of exactly nine digits (an SSN / ITIN / EIN), or any other
    i12 tax-id shape. A longer run (a CRM id such as ``hubspot:12345678901``) is allowed."""
    from intelligences import i12_tax_refs
    if i12_tax_refs.raw_tax_id(v):
        raise ValueError("looks like a taxpayer id")
    return v


def _max8_digits(v: str) -> str:
    """At most eight digits in total: a payee reference cannot carry a nine-digit id in any arrangement."""
    if sum(c.isdigit() for c in v) > 8:
        raise ValueError("at most eight digits")
    return v


def _idna_domain(v: str) -> str:
    """AEGIS round 2 L4: a domain is IDNA-encoded before anything else (``münchen.de`` is ``xn--mnchen-3ya.de``),
    lower-cased, then checked against the ASCII shape."""
    from intelligences import i02_identity
    d = i02_identity.domain(v)
    if d is None or len(d) > 253:
        raise ValueError("not a domain name")
    return d


def _ts(v: str) -> str:
    parse_iso(v)
    return v


def _unique(v: list) -> list:
    if len(set(v)) != len(v):
        raise ValueError("items must be unique")
    return v


Id = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
# AEGIS round 2 N1: a request id is opaque and of one fixed shape — a UUID (with or without hyphens) or 16..64
# lowercase hex characters — so it can never be a field anything is typed into (no digit counting needed)
def _lower(v):
    """AEGIS round 3 Info: a UUID / hex request id in any case is lower-cased BEFORE the shape check and before the
    request key is built, so ``ABCD...`` and ``abcd...`` are one request."""
    return v.lower() if isinstance(v, str) else v


RequestId = Annotated[StrictStr, BeforeValidator(_lower), Field(pattern=r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
                                               r"[0-9a-f]{16,64})$")]
# finance-py's OWN generated ids (services/finance-py/src/models.py OWN_ID_RE: ``service.rid`` is
# fin-<prefix>-<26 Crockford base32>, ``ledger.derived_id`` is fin-<prefix>-<40 hex>), exactly
FinanceId = Annotated[StrictStr, Field(pattern=r"^fin-[a-z][a-z0-9]{0,7}-(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{40})$")]
NbId = Annotated[StrictStr, Field(pattern=r"^nb-[a-z]{3}-[0-9a-f]{40}$")]
Brand = Literal["zbm", "zbc"]
Slug = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9-]{2,59}$"), AfterValidator(_no_tax_id_shape)]
Ref = Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,20}:[A-Za-z0-9._-]{1,100}$"), AfterValidator(_no_tax_id_shape)]
Domain = Annotated[StrictStr, Field(min_length=3, max_length=253), AfterValidator(_idna_domain),
                   AfterValidator(_no_tax_id_shape)]
Email = Annotated[StrictStr, Field(min_length=3, max_length=254)]
Name = Annotated[StrictStr, Field(min_length=1, max_length=120), AfterValidator(_printable), AfterValidator(_no_braces),
                 AfterValidator(_no_tax_id_shape)]
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
PayeeRef = Annotated[StrictStr, Field(pattern=r"^fin:[A-Za-z0-9._-]{4,120}$"), AfterValidator(_max8_digits)]


class RequestOnly(Strict):
    request_id: RequestId


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
    request_id: RequestId
    brand: Brand
    kind: PursuitKind
    title: Title
    counterparty: Counterparty
    value: Money
    deadline: Optional[Timestamp] = None
    notes: Optional[ShortText] = None
    source_ref: Optional[Ref] = None
    checklist: list[ChecklistRequest] = Field(default_factory=list, max_length=50)


class ChecklistExtend(Strict):
    request_id: RequestId
    items: list[ChecklistRequest] = Field(min_length=1, max_length=20)


class ValueSet(Strict):
    request_id: RequestId
    value: Money


class DeadlineSet(Strict):
    request_id: RequestId
    deadline: Timestamp


YesNo = Literal["yes", "no", "unknown"]


class Qualification(Strict):
    request_id: RequestId
    scope_fit: YesNo
    capacity: YesNo
    deadline_feasible: YesNo
    compliance_feasible: YesNo
    relationship: YesNo
    price_competitive: YesNo
    payment_terms_acceptable: YesNo


class BidDecision(Strict):
    request_id: RequestId
    decision: Literal["bid", "no_bid"]
    qualification_sha256: Sha256


class Attest(Strict):
    request_id: RequestId
    item_sha256: Sha256


class DealApproval(Strict):
    request_id: RequestId
    binding_sha256: Sha256


class Lost(Strict):
    request_id: RequestId
    reason_code: Literal["price", "scope", "incumbent", "timing", "no_decision", "disqualified", "relationship",
                         "other"]


class Import(Strict):
    request_id: RequestId
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20


class AgreementRequest(Strict):
    request_id: RequestId
    kind: Literal["referral_agreement", "alliance_agreement", "white_label_agreement", "nda"]


# --------------------------------------------------------------------------------------------------- blocks, responses

class BlockCreate(Strict):
    request_id: RequestId
    block_key: Slug
    brand: Literal["zbm", "zbc", "both"]
    title: Title
    text: Text


class BlockVersion(Strict):
    request_id: RequestId
    title: Title
    text: Text


class Approve(Strict):
    request_id: RequestId
    content_sha256: Sha256


class BlockPart(Strict):
    block_id: NbId
    version: Version


class CustomPart(Strict):
    custom: Text


Part = Union[BlockPart, CustomPart]


class ResponseCreate(Strict):
    request_id: RequestId
    pursuit_id: NbId
    parts: list[Part] = Field(min_length=1, max_length=120)


class ResponseVersion(Strict):
    request_id: RequestId
    parts: list[Part] = Field(min_length=1, max_length=120)


class ResponseApprove(Strict):
    request_id: RequestId
    version: Version
    content_sha256: Sha256
    acknowledged_flags: Annotated[list[Literal["GIFT", "LOBBYING", "CONFLICT_OF_INTEREST", "CONTINGENT_FEE"]],
                                  AfterValidator(_unique)] = Field(default_factory=list, max_length=4)


class ResponseSubmit(Strict):
    request_id: RequestId
    version: Version
    content_sha256: Sha256


# --------------------------------------------------------------------------------------------------- partners

# Partner records carry NO free text (no notes field): tax information can only ever arrive as a reference
class PartnerCreate(Strict):
    request_id: RequestId
    partner_key: Slug
    kind: Literal["referral", "agency_alliance", "white_label"]
    brands: Annotated[list[Brand], AfterValidator(_unique)] = Field(min_length=1, max_length=2)
    name: Name
    domain: Domain


class RatePropose(Strict):
    request_id: RequestId
    version: Version
    rate_pct: Pct


class RateApprove(Strict):
    request_id: RequestId
    version: Version
    binding_sha256: Sha256


class PayeeSet(Strict):
    request_id: RequestId
    finance_payee_ref: PayeeRef
    tax_info_ref: TaxRef


class PartnerDealCreate(Strict):
    request_id: RequestId
    partner_id: NbId
    brand: Brand
    counterparty: Counterparty
    deal_value: Money


class DealValueSet(Strict):
    request_id: RequestId
    deal_value: Money


class PartnerDealWon(Strict):
    request_id: RequestId
    agreement_kind: Literal["referral_agreement", "alliance_agreement", "white_label_agreement"]


class FinanceEvent(Strict):
    request_id: RequestId
    finance_event_id: FinanceId
    deal_id: NbId
    kind: Literal["payment", "refund", "chargeback"]
    amount: Money
    currency: Literal["USD"]


class SubmissionReconcile(Strict):
    request_id: RequestId
    outcome: Literal["delivered", "not_delivered"]
    state_sha256: Sha256


class PayoutReconcile(Strict):
    request_id: RequestId
    outcome: Literal["paid", "not_paid"]
    state_sha256: Sha256


class PayoutPaid(Strict):
    request_id: RequestId
    finance_ref: FinanceId


# --------------------------------------------------------------------------------------------------- outreach

class ContactCreate(Strict):
    request_id: RequestId
    brand: Brand
    email: Email
    name: Name
    partner_id: Optional[NbId] = None
    pursuit_id: Optional[NbId] = None


class MergeFields(Strict):
    request_id: RequestId
    first_name: Optional[MergeName] = None
    company: Optional[MergeName] = None


class TemplateCreate(Strict):
    request_id: RequestId
    template_key: Slug
    brand: Brand
    subject: Subject
    body: Text


class TemplateVersion(Strict):
    request_id: RequestId
    subject: Subject
    body: Text


class QueueEmail(Strict):
    request_id: RequestId
    contact_id: NbId
    template_id: NbId
    version: Version
    content_sha256: Sha256


class EmailEvent(Strict):
    request_id: RequestId
    message_id: NbId
    event: Literal["delivered", "soft_bounce", "hard_bounce", "complaint"]


class Reply(Strict):
    request_id: RequestId
    message_id: Optional[NbId] = None
    from_email: Optional[Annotated[StrictStr, Field(min_length=1, max_length=1000)]] = None   # never refuses (H1)
    text: Annotated[StrictStr, Field(min_length=1, max_length=20000)]


class Unsubscribe(Strict):
    request_id: RequestId
    token: Annotated[StrictStr, Field(pattern=r"^nb-msg-[0-9a-f]{40}\.[0-9a-f]{32}$")]


class Suppress(Strict):
    request_id: RequestId
    contact_id: Optional[NbId] = None
    email: Optional[Email] = None


class HoldRef(Strict):
    hold_id: NbId
    reply_id: NbId


class HoldDecision(Strict):
    """AEGIS round 4: Andre names exactly the holds he decides, each with its reply, and the hash over that set and
    the action."""
    request_id: RequestId
    decision: Literal["resume", "opt_out"]
    holds: list[HoldRef] = Field(min_length=1, max_length=200)
    decision_sha256: Sha256

