"""Request bodies (strict: unknown fields refused, no coercion, frozen). Responses are plain dicts built by
service.py. No model has a date of birth, government id or payment field; api.py also refuses such keys anywhere in
a body before it is parsed (legal-py's FORBIDDEN_KEYS)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StrictStr

from clock import parse_iso


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _printable(v: str) -> str:
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in v):
        raise ValueError("control characters are refused")
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


def _date(v: str) -> str:
    date.fromisoformat(v)
    return v


def _ts(v: str) -> str:
    parse_iso(v)
    return v


def _unique(v: list) -> list:
    if len(set(v)) != len(v):
        raise ValueError("items must be unique")
    return v


Id = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
SvId = Annotated[StrictStr, Field(pattern=r"^sv-[a-z]{3}-[0-9a-f]{40}$")]
Brand = Literal["zbm", "zbc"]
ContactRef = Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,20}:[A-Za-z0-9._-]{1,100}$")]
AccountId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
Email = Annotated[StrictStr, Field(max_length=254,
                                   pattern=r"^[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63}){1,8}$")]
Phone = Annotated[StrictStr, Field(pattern=r"^\+[1-9][0-9]{7,14}$")]
Tz = Annotated[StrictStr, Field(pattern=r"^[A-Za-z][A-Za-z0-9_+/-]{0,63}$")]
Name = Annotated[StrictStr, Field(min_length=1, max_length=80), AfterValidator(_printable), AfterValidator(_no_braces)]
Text = Annotated[StrictStr, Field(min_length=1, max_length=20000), AfterValidator(_multiline)]
ShortText = Annotated[StrictStr, Field(min_length=1, max_length=2000), AfterValidator(_multiline)]
SmsText = Annotated[StrictStr, Field(min_length=1, max_length=1600), AfterValidator(_multiline)]
Subject = Annotated[StrictStr, Field(min_length=1, max_length=300), AfterValidator(_printable)]
Title = Annotated[StrictStr, Field(min_length=1, max_length=120), AfterValidator(_printable)]
Timestamp = Annotated[StrictStr, Field(max_length=40), AfterValidator(_ts)]
DateStr = Annotated[StrictStr, Field(pattern=r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$"), AfterValidator(_date)]
ItemId = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_-]{2,59}$")]
Term = Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9' -]{0,59}$")]
Phrase = Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9' -]{2,119}$")]
Money = Annotated[StrictStr, Field(pattern=r"^(0|[1-9][0-9]{0,7})\.[0-9]{2}$")]
Sha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Ref = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:/-]{1,200}$")]
OutChannel = Literal["email", "sms", "chat"]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class RequestOnly(Strict):
    request_id: Id


class ContactSave(Strict):
    request_id: Id
    brand: Brand
    contact_ref: ContactRef
    account_id: Optional[AccountId] = None
    email: Optional[Email] = None
    phone: Optional[Phone] = None
    timezone: Optional[Tz] = None
    display_name: Optional[Name] = None


class ConsentRecord(Strict):
    request_id: Id
    contact_id: SvId
    channel: Literal["sms", "email"]
    source: Literal["portal_form", "site_form", "onboarding_form", "signed_agreement", "recorded_call"]
    # V2-H2: the number (sms) or email address the consent was given for; it must be the contact's current one
    address: Annotated[StrictStr, Field(max_length=254, pattern=r"^(\+[1-9][0-9]{7,14}|[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}"
                                                                r"(\.[a-z0-9-]{1,63}){1,8})$")]
    consent_text: ShortText                     # the exact words the contact agreed to (stored once, by hash)
    captured_at: Timestamp
    express: Literal[True]                      # an express, affirmative consent; nothing implied is recorded


class ConsentRevoke(Strict):
    request_id: Id
    contact_id: SvId
    channel: Literal["sms", "email"]


class ChatIn(Strict):
    request_id: Id
    brand: Brand
    contact_ref: ContactRef
    ticket_id: Optional[SvId] = None
    text: Text


class EmailIn(Strict):
    request_id: Id
    brand: Brand
    to_address: Email
    from_address: Email
    subject: Optional[Subject] = None
    ticket_id: Optional[SvId] = None
    text: Text


class SmsIn(Strict):
    request_id: Id
    brand: Brand
    to_number: Phone
    from_number: Phone
    text: SmsText


class CallIn(Strict):
    request_id: Id
    brand: Brand
    from_number: Phone
    started_at: Timestamp
    duration_seconds: Annotated[StrictInt, Field(ge=0, le=86400)]
    outcome: Literal["answered", "missed", "voicemail"]
    voicemail_ref: Optional[Ref] = None
    transcript_ref: Optional[Ref] = None


class CallRoute(Strict):
    brand: Brand


class RoutingSet(Strict):
    request_id: Id
    timezone: Tz
    days: Annotated[list[Day], Field(min_length=1, max_length=7), AfterValidator(_unique)]
    open_hour: Annotated[StrictInt, Field(ge=0, le=23)]
    close_hour: Annotated[StrictInt, Field(ge=1, le=24)]
    in_hours: Literal["ring_andre", "voicemail"]


class Reply(Strict):
    request_id: Id
    text: Text
    channel: Optional[OutChannel] = None


class StatusSet(Strict):
    request_id: Id
    status: Literal["open", "pending_customer", "escalated", "resolved", "closed"]


class PrioritySet(Strict):
    request_id: Id
    priority: Literal["p1", "p2", "p3", "p4"]


class KbRules(Strict):
    all: Annotated[list[Term], Field(max_length=10), AfterValidator(_unique)] = []
    any: Annotated[list[Term], Field(max_length=20), AfterValidator(_unique)] = []
    min_any: Annotated[StrictInt, Field(ge=1, le=10)] = 1
    phrases: Annotated[list[Phrase], Field(max_length=10), AfterValidator(_unique)] = []
    exclude: Annotated[list[Term], Field(max_length=20), AfterValidator(_unique)] = []


class ArticleSave(Strict):
    request_id: Id
    item_id: ItemId
    brands: Annotated[list[Brand], Field(min_length=1, max_length=2), AfterValidator(_unique)]
    channels: Annotated[list[OutChannel], Field(min_length=1, max_length=3), AfterValidator(_unique)]
    title: Title
    answer: ShortText
    rules: KbRules
    # V2-H1: words a routine question may also contain (approved with the article); denied words are refused
    vocabulary: Annotated[list[Term], Field(max_length=40), AfterValidator(_unique)] = []


class TemplateSave(Strict):
    request_id: Id
    item_id: ItemId
    purpose: Literal["check_in", "nps_survey", "offer"]
    brand: Brand
    channels: Annotated[list[OutChannel], Field(min_length=1, max_length=3), AfterValidator(_unique)]
    text: ShortText


class OfferSave(Strict):
    request_id: Id
    item_id: ItemId
    brand: Brand
    title: Title
    terms: ShortText
    price: Money                                 # a Decimal string, never a float
    currency: Literal["USD"] = "USD"


class Approve(Strict):
    request_id: Id
    version: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    content_sha256: Sha256


class AccountSave(Strict):
    request_id: Id
    account_id: AccountId
    brand: Brand
    primary_contact_id: Optional[SvId] = None
    contract_end: Optional[DateStr] = None


class AccountEvent(Strict):
    request_id: Id
    kind: Literal["portal_login"]
    occurred_at: Timestamp


class OfferSelect(Strict):
    request_id: Id
    offer_id: ItemId


class PlanClose(Strict):
    request_id: Id
    outcome: Literal["saved", "lost", "cancelled"]


class SurveySend(Strict):
    request_id: Id
    account_id: AccountId
    channel: OutChannel


class NpsResponse(Strict):
    request_id: Id
    survey_id: SvId
    score: Annotated[StrictInt, Field(ge=0, le=10)]
    comment: Optional[ShortText] = None


class ResolveHeld(Strict):
    request_id: Id
    outcome: Literal["sent", "requeue", "cancel"]
