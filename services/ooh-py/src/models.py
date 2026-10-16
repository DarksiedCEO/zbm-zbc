"""Request bodies (strict: unknown fields refused, no coercion, frozen; seo-py's Strict base). Responses are plain dicts
built by the service. api.py also refuses personal-data keys anywhere in a body before it is parsed.

Money is a string with exactly two decimals (ADR 0003), never a float. Times are RFC 3339 with an offset."""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

import policy

REQUEST_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TENANT = r"^[a-z][a-z0-9-]{1,39}$"
AMOUNT = r"^(0|[1-9][0-9]{0,11})\.[0-9]{2}$"
PCT = r"^(0|[1-9][0-9]{0,2})\.[0-9]{2}$"
REF = r"^[A-Za-z0-9._:-]{1,100}$"             # an external reference (document id, bank transfer id, vendor ref)
VENDOR = r"^[a-z0-9][a-z0-9-]{1,63}$"
CODE = r"^[A-Z_]{3,40}$"
ID = r"^ooh-[a-z]{3}-[0-9a-f]{40}$"
DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
TIME = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$"
DOC_MAX = 4000


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


RequestId = Annotated[StrictStr, Field(pattern=REQUEST_ID)]
Amount = Annotated[StrictStr, Field(pattern=AMOUNT)]
Ref = Annotated[StrictStr, Field(pattern=REF)]
Id = Annotated[StrictStr, Field(pattern=ID)]
Time = Annotated[StrictStr, Field(pattern=TIME)]
Version = Annotated[StrictInt, Field(ge=1, le=10_000_000)]


class RequestOnly(Strict):
    request_id: RequestId


class TenantCreate(Strict):
    request_id: RequestId
    tenant_id: Annotated[StrictStr, Field(pattern=TENANT)]


class SwitchSet(Strict):
    request_id: RequestId
    switch: Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,20}(:[a-z0-9_-]{1,40})?$")]
    engaged: StrictBool


class CampaignCreate(Strict):
    request_id: RequestId
    name: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9 ._&'-]{2,120}$")]
    markets: Annotated[list[Annotated[StrictStr, Field(pattern=r"^[a-z0-9-]{2,60}$")]], Field(min_length=1,
                                                                                             max_length=20)]


class Transition(Strict):
    request_id: RequestId
    to: Literal[policy.ALL_STATES]                                   # type: ignore[valid-type]
    expected_state: Literal[policy.ALL_STATES]                       # type: ignore[valid-type]
    expected_version: Version
    reason: Annotated[StrictStr, Field(pattern=CODE)]


class InventoryAdd(Strict):
    request_id: RequestId
    vendor: Annotated[StrictStr, Field(pattern=VENDOR)]
    kind: Literal["static", "digital"]
    market: Annotated[StrictStr, Field(pattern=r"^[a-z0-9-]{2,60}$")]
    vendor_unit_ref: Ref
    flight_start: Optional[Annotated[StrictStr, Field(pattern=DATE)]] = None   # with flight_end, or neither yet
    flight_end: Optional[Annotated[StrictStr, Field(pattern=DATE)]] = None
    # vendor-supplied description: untrusted data, kept bounded and marked, never read as an instruction (section 52)
    vendor_text: Optional[Annotated[StrictStr, Field(max_length=DOC_MAX)]] = None


class FlightSet(Strict):
    request_id: RequestId
    expected_version: Version
    flight_start: Annotated[StrictStr, Field(pattern=DATE)]
    flight_end: Annotated[StrictStr, Field(pattern=DATE)]


class DistinctAck(Strict):
    """Andre's acknowledgement that a vendor whose name normalises onto others' is a different vendor (AEGIS R3)."""
    vendor_ids: Annotated[list[Annotated[StrictStr, Field(pattern=VENDOR)]], Field(min_length=1, max_length=10)]
    reason: Literal[policy.VENDOR_DISTINCT_REASONS]                  # type: ignore[valid-type]


class VendorCreate(Strict):
    """Andre registers a vendor; every unit, quote and authorization names one by its id."""
    request_id: RequestId
    vendor_id: Annotated[StrictStr, Field(pattern=VENDOR)]
    legal_name: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9 .,&'()-]{2,120}$")]
    distinct_from_ack: Optional[DistinctAck] = None


class AvailabilityMove(Strict):
    request_id: RequestId
    to: Literal[policy.AVAILABILITY_STATES]                          # type: ignore[valid-type]
    expected_state: Literal[policy.AVAILABILITY_STATES]              # type: ignore[valid-type]
    expected_version: Version
    vendor_ref: Optional[Ref] = None
    hold_expires_at: Optional[Time] = None


class PriceRecord(Strict):
    request_id: RequestId
    price_type: Literal[policy.PRICE_TYPES]                          # type: ignore[valid-type]
    amount: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    vendor: Optional[Annotated[StrictStr, Field(pattern=VENDOR)]] = None
    inventory_ids: Annotated[list[Id], Field(max_length=200)] = []
    quote_refs: Annotated[list[Id], Field(max_length=200)] = []     # a sell price names the quotes it marks up
    evidence_class: Optional[Literal[policy.EVIDENCE_CLASSES]] = None  # type: ignore[valid-type]
    valid_until: Optional[Time] = None
    source_ref: Optional[Ref] = None
    vendor_text: Optional[Annotated[StrictStr, Field(max_length=DOC_MAX)]] = None


class CommercialModelSet(Strict):
    request_id: RequestId
    expected_version: Version
    principal_agent_state: Literal[policy.PRINCIPAL_AGENT_STATES]    # type: ignore[valid-type]
    pricing_method: Literal[policy.PRICING_METHODS]                  # type: ignore[valid-type]
    markup_pct: Annotated[StrictStr, Field(pattern=PCT)]             # required: no default is ever applied
    markup_disclosed: Literal[True]
    markup_basis_ref: Ref                                            # the evidence the markup was set from
    presentation: Literal[policy.PRESENTATIONS]                      # type: ignore[valid-type]
    cancellation_terms_ref: Ref


class ClientApproval(Strict):
    request_id: RequestId
    expected_version: Version
    sell_price_id: Id
    client_approval_doc_ref: Ref


class FundsVerify(Strict):
    request_id: RequestId
    expected_version: Version
    amount: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    method: Literal[policy.FUNDS_METHODS]                            # type: ignore[valid-type]
    evidence_ref: Ref                                                # e.g. the bank's transfer reference
    cleared: Literal[True]                                           # Andre attests the funds are CLEARED


class FundsReconfirm(Strict):
    """Andre re-confirms that cleared funds already recorded cover the re-approved amount (no new money, no new
    reference: AEGIS M3)."""
    request_id: RequestId
    expected_version: Version
    approved_amount: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]


class Authorize(Strict):
    """Section 12 OOH_PURCHASE_AUTHORIZATION, as Andre signs it. ``funding_state`` is computed, never supplied."""
    request_id: RequestId
    expected_version: Version
    inventory_ids: Annotated[list[Id], Field(min_length=1, max_length=200)]
    vendor: Annotated[StrictStr, Field(pattern=VENDOR)]
    exact_quote_refs: Annotated[list[Id], Field(min_length=1, max_length=200)]
    authorized_amount: Amount
    authorized_currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    client_approval_ref: Id
    commercial_terms_ref: Id
    cancellation_terms_ref: Ref
    signer: Literal["andre"]
    authority_scope: Annotated[StrictStr, Field(pattern=CODE)]
    expires_at: Time


class Submit(Strict):
    """What the submitter believes it is sending; every field must equal the authorization's (section 50 binding)."""
    request_id: RequestId
    vendor: Annotated[StrictStr, Field(pattern=VENDOR)]
    amount: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    quote_refs: Annotated[list[Id], Field(min_length=1, max_length=200)]
    campaign_version: Version
    channel: Literal[policy.SUBMISSION_CHANNELS]                     # type: ignore[valid-type]


class Outcome(Strict):
    request_id: RequestId
    outcome: Literal[policy.OUTCOMES]                                # type: ignore[valid-type]
    vendor_ref: Ref                                                  # the vendor's confirmation or rejection reference
