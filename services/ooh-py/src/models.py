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
    """A campaign unit is one face of the registry (OOH-2) for one flight: vendor, format, market and the vendor's
    face reference come from the face, never from this body."""
    request_id: RequestId
    face_id: Id
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
    """A unit's availability move. HOLD_CONFIRMED is never reached here (policy.AVAILABILITY_ANDRE_ONLY), so the
    vendor's hold reference and expiry are not fields of this body: they travel as hold evidence."""
    request_id: RequestId
    to: Literal[policy.AVAILABILITY_STATES]                          # type: ignore[valid-type]
    expected_state: Literal[policy.AVAILABILITY_STATES]              # type: ignore[valid-type]
    expected_version: Version


class HoldEvidence(Strict):
    """The vendor's hold evidence, attached by an agent (or the dashboard) for Andre's decision. It confirms nothing."""
    request_id: RequestId
    expected_version: Version                                        # the unit's version
    vendor_ref: Ref                                                  # the vendor's hold reference
    hold_expires_at: Time                                            # the expiry the vendor states
    evidence_doc_ref: Ref                                            # where the vendor's message or document is kept
    vendor_text: Optional[Annotated[StrictStr, Field(max_length=DOC_MAX)]] = None


class HoldDecision(Strict):
    """Andre's decision on ONE named hold-evidence record: CONFIRM sets HOLD_CONFIRMED; REJECT names a reason."""
    request_id: RequestId
    expected_version: Version                                        # the unit's version
    evidence_id: Id
    decision: Literal[policy.HOLD_DECISIONS]                         # type: ignore[valid-type]
    reject_reason: Optional[Literal[policy.HOLD_REJECT_REASONS]] = None  # type: ignore[valid-type]


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


class PrincipalAgentDetermination(Strict):
    """Andre records the CPA's or counsel's written confirmation of whether ZBM buys media as principal or as agent
    (department-wide). UNDETERMINED withdraws a determination."""
    request_id: RequestId
    determination: Literal[policy.PRINCIPAL_AGENT_STATES]           # type: ignore[valid-type]
    confirmed_by: Literal[policy.PRINCIPAL_AGENT_CONFIRMERS]         # type: ignore[valid-type]
    confirmation_ref: Ref                                            # the confirmation letter / memo reference


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


class FinanceVerification(Strict):
    """Finance (31) verifies that cleared client funds cover the current client approval (locked, Oct 16 round 2)."""
    request_id: RequestId
    approval_id: Id                                                  # the client approval it verifies
    verified_amount: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    finance_ref: Ref                                                 # Finance's own verification reference
    cleared: Literal[True]


class FundsReconfirm(Strict):
    """Andre re-confirms that cleared funds already recorded cover the re-approved amount (no new money, no new
    reference: AEGIS M3)."""
    request_id: RequestId
    expected_version: Version
    approved_amount: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]


class Authorize(Strict):
    """Section 12 OOH_PURCHASE_AUTHORIZATION, as Andre signs it. ``funding_state`` is computed, never supplied.
    ``contract_id``: the vendor contract whose terms Andre approved (OOH-4)."""
    request_id: RequestId
    expected_version: Version
    contract_id: Id
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


# ---------------------------------------------------------------------------------------------- OOH-2 registry

MARKET = r"^[a-z0-9-]{2,60}$"


class ReadinessSet(Strict):
    """One of a market's four readiness flags. MET needs Andre; NOT_MET (the safe direction) any preparer."""
    request_id: RequestId
    flag: Literal[policy.READINESS_FLAGS]                            # type: ignore[valid-type]
    value: Literal[policy.READINESS_VALUES]                          # type: ignore[valid-type]
    evidence_ref: Ref


class SourceRecord(Strict):
    """A named source of inventory facts: a vendor quote or a rate card, as received."""
    request_id: RequestId
    kind: Literal[policy.SOURCE_KINDS + policy.SOURCE_KINDS_NOT_CONNECTED]   # type: ignore[valid-type]
    vendor: Annotated[StrictStr, Field(pattern=VENDOR)]
    doc_ref: Ref
    dated: Time                                                      # the date the source states (not in the future)
    vendor_text: Optional[Annotated[StrictStr, Field(max_length=DOC_MAX)]] = None


class FaceCreate(Strict):
    """A face enters the registry from a named source: its identity (vendor, the vendor's reference, format,
    market) is that source's."""
    request_id: RequestId
    source_id: Id
    vendor_face_ref: Ref
    format: Literal[policy.FORMATS]                                  # type: ignore[valid-type]
    market: Annotated[StrictStr, Field(pattern=MARKET)]


class AttributeSet(Strict):
    """One attribute value as a named source states it (its timestamp is the source's)."""
    request_id: RequestId
    expected_version: Version
    attribute: Annotated[StrictStr, Field(pattern=r"^[a-z_]{2,40}$")]
    value: Annotated[StrictStr, Field(min_length=1, max_length=300)]
    source_id: Id


class Brief(Strict):
    """The client's brief for a campaign (OOH-3): what the plan must fit."""
    request_id: RequestId
    expected_version: Version
    budget_cap: Amount                                               # the client's budget, all-in
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    formats: Annotated[list[Literal[policy.FORMATS]], Field(min_length=1, max_length=4)]  # type: ignore[valid-type]
    flight_window_start: Annotated[StrictStr, Field(pattern=DATE)]
    flight_window_end: Annotated[StrictStr, Field(pattern=DATE)]
    brief_doc_ref: Ref                                               # the client's brief document


class Estimate(Strict):
    request_id: RequestId
    method: Literal[policy.ESTIMATE_METHODS]                         # type: ignore[valid-type]


class MarkupEvidence(Strict):
    """Evidence a deal's markup is set from; the commercial model's markup_basis_ref names one of these."""
    request_id: RequestId
    kind: Literal[policy.MARKUP_EVIDENCE_KINDS]                      # type: ignore[valid-type]
    doc_ref: Ref


class ContractDraft(Strict):
    """A vendor contract (insertion order) as the vendor's document states it, drafted by a preparer."""
    request_id: RequestId
    vendor: Annotated[StrictStr, Field(pattern=VENDOR)]
    quote_refs: Annotated[list[Id], Field(min_length=1, max_length=200)]
    total: Amount
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
    io_doc_ref: Ref                                                  # the vendor's insertion order / contract
    cancellation_terms_ref: Ref
    payment_terms_days: Annotated[StrictInt, Field(ge=0, le=policy.CONTRACT_PAYMENT_TERMS_MAX_DAYS)]
    vendor_payment_method: Literal[policy.VENDOR_PAYMENT_METHODS]     # type: ignore[valid-type]
    vendor_text: Optional[Annotated[StrictStr, Field(max_length=DOC_MAX)]] = None


class ContractApproval(Strict):
    """Andre approves the terms of one named contract draft."""
    request_id: RequestId
    expected_version: Version                                        # the campaign's


class PortSync(Strict):
    request_id: RequestId
    vendor: Annotated[StrictStr, Field(pattern=VENDOR)]


class AvailsRecord(Strict):
    """The vendor's answer on a face for a flight, from a named source."""
    request_id: RequestId
    source_id: Id
    flight_start: Annotated[StrictStr, Field(pattern=DATE)]
    flight_end: Annotated[StrictStr, Field(pattern=DATE)]
    answer: Literal[policy.AVAILS_ANSWERS]                           # type: ignore[valid-type]
    valid_until: Optional[Time] = None
