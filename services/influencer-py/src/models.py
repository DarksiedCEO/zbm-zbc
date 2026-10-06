"""Request bodies (strict: unknown fields refused, no coercion, frozen). Responses are plain dicts built by the
service. Money is a canonical string, checked again by money.py where it is used (a JSON number is refused)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from intelligences import i03_fit, i08_disclosure


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
Money = Annotated[StrictStr, Field(max_length=18)]
Email = Annotated[StrictStr, Field(min_length=3, max_length=254)]
Brand = Literal["zbm", "zbc"]
Platform = Literal["instagram", "tiktok", "x", "youtube"]
Niche = Literal[i03_fit.NICHES]  # type: ignore[valid-type]
FollowerBand = Literal[tuple(i03_fit.FOLLOWER_BANDS)]  # type: ignore[valid-type]
EngagementBand = Literal[tuple(i03_fit.ENGAGEMENT_BANDS)]  # type: ignore[valid-type]
Country = Annotated[StrictStr, Field(pattern=r"^[A-Z]{2}$")]
Disclosure = Literal[i08_disclosure.ALL_DISCLOSURES]  # type: ignore[valid-type]


def _text(max_len: int):
    return Annotated[StrictStr, Field(min_length=1, max_length=max_len), AfterValidator(_printable_lines)]


class HandleIn(Strict):
    platform: Platform
    handle: Annotated[StrictStr, Field(min_length=1, max_length=64)]


class ApplicationIn(Strict):
    """The creator application form (hub). ``adult_18_plus`` must be exactly true (checked by the service, so a
    missing or false value gets its own code)."""
    request_id: Id
    display_name: Name
    email: Email
    handles: Annotated[list[HandleIn], Field(max_length=8)] = []
    niches: Annotated[list[Niche], Field(max_length=6)] = []
    follower_band: Optional[FollowerBand] = None
    engagement_band: Optional[EngagementBand] = None
    country: Optional[Country] = None
    adult_18_plus: Optional[StrictBool] = None
    attestation_text_version: Id
    attestation_text_sha256: Hex64


class ProspectIn(Strict):
    """A profile a person at Andre's console researched (dashboard). Never attested: no deal until the creator applies."""
    request_id: Id
    display_name: Name
    email: Optional[Email] = None
    handles: Annotated[list[HandleIn], Field(min_length=1, max_length=8)]
    niches: Annotated[list[Niche], Field(max_length=6)] = []
    follower_band: Optional[FollowerBand] = None
    engagement_band: Optional[EngagementBand] = None
    country: Optional[Country] = None
    evidence_ref: Id


class Confirm(Strict):
    request_id: Id
    token: Annotated[StrictStr, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")]


class MinorReview(Strict):
    request_id: Id
    decision: Literal["confirm_minor", "not_a_minor"]


class DiscoveryImport(Strict):
    request_id: Id
    source: Literal["public_profile", "paid_database"]
    niches: Annotated[list[Niche], Field(max_length=6)] = []
    limit: Annotated[StrictInt, Field(ge=1, le=200)] = 50


class FirstName(Strict):
    request_id: Id
    first_name: Annotated[StrictStr, Field(min_length=1, max_length=40), AfterValidator(_printable)]


class Suppress(Strict):
    request_id: Id
    influencer_id: Optional[Id] = None
    email: Optional[Email] = None
    handle: Optional[HandleIn] = None
    reason: Literal["manual"] = "manual"


class Unsubscribe(Strict):
    request_id: Id
    token: Annotated[StrictStr, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")]


class TemplateCreate(Strict):
    request_id: Id
    brand: Brand
    name: Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")]
    subject: ShortText
    body: _text(5000)


class TemplateVersion(Strict):
    request_id: Id
    subject: ShortText
    body: _text(5000)


class Approve(Strict):
    """Andre approves exactly the content whose SHA-256 he names."""
    request_id: Id
    content_sha256: Hex64


class RequestOnly(Strict):
    request_id: Id


class EmailOutreach(Strict):
    request_id: Id
    influencer_id: Id
    template_id: Id
    version: Annotated[StrictInt, Field(ge=1, le=10_000)]


class DmDraft(Strict):
    request_id: Id
    influencer_id: Id
    platform: Platform
    brand: Brand
    text: _text(1000)


class EmailEvent(Strict):
    request_id: Id
    message_id: Id
    event: Literal["delivered", "soft_bounce", "hard_bounce", "complaint"]


class ReplyIn(BaseModel):
    """A reply relayed from the email provider or a platform. NOTHING a provider sends may get a reply refused (AEGIS
    R1-M4, R2-N3): every field is optional and of any JSON type, unknown fields are ignored (never stored), and the
    service reads what it can — a text of any length is truncated before it is classified and hashed, a channel it does
    not know is ``other``, a missing or unreadable request id is replaced by the SHA-256 of the body. The tax-id scan
    does not run on this body: its text, addresses, handles and message id are never stored raw (only hashes, and a
    message id only when it is one of ours)."""
    model_config = ConfigDict(extra="ignore", frozen=True)
    request_id: Any = None
    channel: Any = None
    message_id: Any = None
    from_email: Any = None
    from_handle: Any = None
    text: Any = None


class HoldDecision(Strict):
    request_id: Id
    decision: Literal["continue", "opt_out"]


class PartnerIn(Strict):
    name: ShortText
    ref: Id


class CampaignCreate(Strict):
    request_id: Id
    brand: Brand
    name: ShortText
    kind: Literal["influencer", "co_marketing"]
    partner: Optional[PartnerIn] = None
    objective: Optional[ShortText] = None


class BriefCreate(Strict):
    request_id: Id
    campaign_id: Id
    title: ShortText
    text: _text(8000)
    disclosure: Disclosure


class DeliverableIn(Strict):
    platform: Platform
    kind: Literal["post", "story", "reel", "short", "video", "live", "thread"]
    quantity: Annotated[StrictInt, Field(ge=1, le=20)]


class DealCreate(Strict):
    request_id: Id
    influencer_id: Id
    campaign_id: Id
    brief_id: Id
    deliverables: Annotated[list[DeliverableIn], Field(min_length=1, max_length=10)]
    fee: Money
    product_value: Money = "0.00"
    currency: Literal["USD"] = "USD"


class ContentSubmit(Strict):
    request_id: Id
    deal_id: Id
    platform: Platform
    caption: _text(2200)
    media_sha256: Annotated[list[Hex64], Field(min_length=1, max_length=10)]
    platform_label_on: StrictBool


class ContentLive(Strict):
    request_id: Id
    content_sha256: Hex64
    post_ref: Id


class TaxProfile(Strict):
    """A REFERENCE to tax information held at Finance / Stripe / the vault — never the number itself."""
    request_id: Id
    influencer_id: Id
    tax_form: Literal["w9", "w8ben", "w8bene"]
    # AEGIS R1-M3 / R2-L-b: provider formats only — a Stripe connected account, or a vault reference in an alphabet
    # with NO digits (26 lowercase letters), so its shape can never hide a tax id; that it EXISTS is Finance's to confirm
    tax_ref: Annotated[StrictStr, Field(pattern=r"^(stripe:acct_[A-Za-z0-9]{16,64}|vault:[a-z]{26})$")]
    legal_form: Literal["individual", "entity"]
    country: Country


class PayoutRequest(Strict):
    request_id: Id
    deal_id: Id
    amount: Money
    content_ids: Annotated[list[Id], Field(min_length=1, max_length=20)]
