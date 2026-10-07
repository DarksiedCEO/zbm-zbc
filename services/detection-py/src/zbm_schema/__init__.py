"""
zbm_schema — shared domain contracts for ZBM Revenue Recovery (1A: e-commerce).

This module is the architectural enforcement point for two founder-locked
safeguards (see docs/adr/0001-revenue-recovery-1a-architecture.md):

  1. No dollar figure may exist without BOTH a value classification
     (provenance) and a decision confidence (calibrated band). This is
     enforced by pydantic validation, not by UI convention — an
     unlabeled LabeledValue cannot be constructed. (Failure Mode #1)

  2. Every detection Finding must carry a shared entity reference
     (order_id) so a correlation/orchestration layer can detect two
     agents claiming credit for the same transaction before valuation.
     (Failure Mode #2 — double-counting)

Platform-agnostic by design: this schema has no Shopify/Amazon/TikTok-
specific fields. A per-platform translation layer (not yet built) is
responsible for converting a real store's native data into this shape.
Tonight's fixtures already speak this shape directly.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import AwareDatetime, BaseModel, Field, field_validator, model_validator

from zbm_schema.limits import (
    MAX_ATTRIBUTION_WINDOW_HOURS,
    MAX_DISCOUNTS_PER_ORDER,
    MAX_LINE_ITEMS_PER_ORDER,
    MAX_QUANTITY,
    MAX_RENEWAL_INTERVAL_DAYS,
    AgentId,
    CauseDescription,
    ClientId,
    DiscountCode,
    FindingRef,
    Id,
    Label,
    Methodology,
    MethodologyId,
    PeriodLabel,
    RatePercent,
    Sku,
    Slug,
    _normalize_slug,
)
from zbm_schema.money import (
    CENT as CENT,
    MAX_MONEY,
    Money as Money,
    PositiveMoney,
    MoneyRangeError as MoneyRangeError,
    format_money as format_money,
    money_context,
    percent_of as percent_of,
    quantize_money,
    to_money as to_money,
)


# ---------------------------------------------------------------------------
# Decision 2 — Confidence Label Split
# ---------------------------------------------------------------------------

class ValueClassification(str, Enum):
    """Provenance of a dollar figure — how directly it was observed."""
    OBSERVED = "observed"
    ATTRIBUTED = "attributed"
    INCREMENTAL = "incremental"
    FINANCIALLY_VERIFIED = "financially_verified"


class DecisionConfidence(str, Enum):
    """Calibrated confidence band, independent of value classification."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class LabeledValue(BaseModel):
    """
    A dollar amount that cannot exist without both labels attached.

    This is the architectural enforcement of Decision 2 + Failure Mode #1:
    there is no code path that produces a LabeledValue with a missing
    classification or confidence — pydantic raises on construction.
    """
    model_config = {"frozen": True}

    # Exact Decimal, cent-quantized half-up, positive-only; NaN/inf rejected
    # by zbm_schema.money.to_money. Serializes as a two-decimal JSON string.
    amount_usd: PositiveMoney = Field(description="Dollar amount, must be positive")
    classification: ValueClassification
    confidence: DecisionConfidence


# ---------------------------------------------------------------------------
# Failure Mode #3 — Misattribution safeguard
# ---------------------------------------------------------------------------

class CauseCertainty(str, Enum):
    """
    Whether the agent is naming a specific cause or admitting uncertainty.
    Below an agent's stated confidence threshold, the agent MUST emit
    UNCERTAIN rather than guess at a specific leak subtype.
    """
    NAMED = "named"
    UNCERTAIN = "uncertain"


# ---------------------------------------------------------------------------
# Platform-agnostic commerce entities (fixture / real-store shape)
# ---------------------------------------------------------------------------

class Customer(BaseModel):
    customer_id: Id
    email: str
    created_at: AwareDatetime
    lifetime_order_count: int = Field(ge=0)


class OrderLineItem(BaseModel):
    sku: Sku
    unit_price_usd: PositiveMoney
    quantity: int = Field(gt=0, le=MAX_QUANTITY)


class DiscountApplication(BaseModel):
    code: DiscountCode
    # percent_off is a percentage, not money — it stays a plain number on the
    # wire. Any money computed from it goes through zbm_schema.money.percent_of,
    # which converts it to Decimal via str() so the arithmetic stays exact.
    percent_off: Optional[float] = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    amount_off_usd: Optional[PositiveMoney] = None

    @model_validator(mode="after")
    def _one_discount_mode(self):
        if self.percent_off is None and self.amount_off_usd is None:
            raise ValueError("DiscountApplication needs percent_off or amount_off_usd")
        if self.percent_off is not None and self.amount_off_usd is not None:
            raise ValueError("DiscountApplication cannot set both percent_off and amount_off_usd")
        return self


class AffiliateAttribution(BaseModel):
    affiliate_id: Id
    # The affiliate program's commission rate for this affiliate, in percent
    # of the order subtotal, when known (E-4, Oct 6 2026). Without it the
    # commission actually at risk cannot be computed, and the affiliate agent
    # claims no dollar figure at all — it used to claim the whole order
    # subtotal (probe P5: a $400 order "at risk" when a 10% commission is $40).
    commission_rate_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    # Timezone required (fix wave 1): the affiliate agent subtracts these
    # two, and `aware - naive` raised TypeError -> 500. A naive timestamp is
    # also an ambiguous instant, so it is rejected at validation (422).
    click_timestamp: AwareDatetime
    order_timestamp: AwareDatetime
    attribution_window_hours: int = Field(gt=0, le=MAX_ATTRIBUTION_WINDOW_HOURS)

    @property
    def within_window(self) -> bool:
        elapsed_hours = (self.order_timestamp - self.click_timestamp).total_seconds() / 3600
        return 0 <= elapsed_hours <= self.attribution_window_hours


class Order(BaseModel):
    # Field limits: zbm_schema/limits.py (LOW-C, fix wave 1).
    order_id: Id
    customer_id: Id
    # Timezone required (E-13, Oct 6 2026): a naive timestamp is an ambiguous
    # instant (probe P12).
    placed_at: AwareDatetime
    # Normalized (E-11): "Abandoned_Cart" is abandoned_cart, see limits.Slug.
    status: Slug  # "completed" | "abandoned_cart" | "cancelled" | anything else the platform reports
    line_items: list[OrderLineItem] = Field(max_length=MAX_LINE_ITEMS_PER_ORDER)
    discounts: list[DiscountApplication] = Field(default_factory=list, max_length=MAX_DISCOUNTS_PER_ORDER)
    affiliate: Optional[AffiliateAttribution] = None
    source_platform: Label  # e.g. "shopify", "amazon", "tiktok_shop", "woocommerce" — informational only
    recovery_attempted: bool = False  # abandoned-cart recovery flow (email/SMS) fired for this order

    @model_validator(mode="after")
    def _subtotal_within_money_bound(self):
        # F14 (fix wave 1): each price is <= MAX_MONEY, but quantity is an
        # unbounded int, so a subtotal could leave the contract range (and
        # used to crash quantize with decimal.InvalidOperation -> 500). The
        # check runs in exact integer cents (Python ints never overflow or
        # round), so it cannot itself raise for any input that got this far.
        # Rejecting here makes it a 422, and guarantees subtotal_usd — and
        # every amount derived from it — stays within the contract bound.
        with money_context():  # scaleb honours the context; 17 digits fit exactly
            cents = sum(int(li.unit_price_usd.scaleb(2)) * li.quantity for li in self.line_items)
            max_cents = int(MAX_MONEY.scaleb(2))
        if cents > max_cents:
            raise ValueError(
                f"order subtotal exceeds the maximum money amount {MAX_MONEY} "
                f"(ADR 0003 section 1a: amounts are < 10^15 dollars)"
            )
        return self

    @property
    def subtotal_usd(self) -> Decimal:
        # Decimal * int is exact under MONEY_CONTEXT (the validator above
        # bounds the result to <= MAX_MONEY, 17 significant digits); the sum
        # is quantized once more to pin the invariant.
        with money_context():
            return quantize_money(sum((li.unit_price_usd * li.quantity for li in self.line_items), Decimal("0")))


ORDER_STATUS_ABANDONED_CART = "abandoned_cart"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    LAPSED_NO_RENEWAL_ATTEMPT = "lapsed_no_renewal_attempt"


class Subscription(BaseModel):
    subscription_id: Id
    customer_id: Id
    plan_price_usd: PositiveMoney
    renewal_interval_days: int = Field(gt=0, le=MAX_RENEWAL_INTERVAL_DAYS)
    # Timezone required (E-13): the renewal agent compares next_renewal_due_at
    # with the scan's as-of instant, which needs both to be real instants.
    last_renewal_at: Optional[AwareDatetime] = None
    next_renewal_due_at: AwareDatetime
    status: SubscriptionStatus

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value):
        # E-11: same normalization as limits.Slug, then the enum decides.
        return _normalize_slug(value)


# ---------------------------------------------------------------------------
# Detection output — every agent emits Finding objects in this shape
# ---------------------------------------------------------------------------

class LeakCategory(str, Enum):
    # Tier 1
    AFFILIATE_COUPON_EXTENSION = "affiliate_coupon_extension"
    DISCOUNT_MISUSE = "discount_misuse"
    ABANDONED_CART_COVERAGE = "abandoned_cart_coverage"
    RENEWAL_NEVER_TRIGGERED = "renewal_never_triggered"
    # Tier 2
    SERVER_SIDE_ATTRIBUTION_GAP = "server_side_attribution_gap"
    CROSS_CHANNEL_MISATTRIBUTION_RISK = "cross_channel_misattribution_risk"
    PLATFORM_INTEGRATION_GAP = "platform_integration_gap"
    CONTRACT_PRICING_TERM_DRIFT = "contract_pricing_term_drift"


class EntityType(str, Enum):
    """What a finding's entity_id names. Part of the finding identity and of
    the correlation key (client_id, entity_type, entity_id)."""
    ORDER = "order"
    SUBSCRIPTION = "subscription"
    CONTRACT_TERM = "contract_term"
    PLATFORM = "platform"


class EvidenceClass(str, Enum):
    """How a finding's dollar figure was obtained (E-4, Oct 6 2026). Every
    finding carries one, with a methodology note — ADR 0001 "Evidence class
    and methodology".

      OBSERVED  — read directly off recorded transactions/terms, exact
                  arithmetic only (e.g. a contracted minimum minus the
                  amount actually billed).
      ESTIMATED — observed inputs combined with a stated assumption (e.g.
                  subtotal x a known commission rate, assuming the
                  commission was paid; one missed renewal at plan price,
                  assuming the charge would have succeeded).
      MODELED   — produced by a statistical/attribution model. No agent
                  emits it yet; reserved so a model-derived figure can never
                  be passed off as ESTIMATED.
      UNKNOWN   — no defensible dollar figure exists; recoverable_value is
                  null. Required whenever recoverable_value is null.
    """
    OBSERVED = "OBSERVED"
    ESTIMATED = "ESTIMATED"
    MODELED = "MODELED"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# AEGIS M2 (Oct 7 2026): labels can never claim more than the evidence
# ---------------------------------------------------------------------------

# A value classification that says the figure was read off (or reconciled
# against) the records, and the confidence bands that say "act on this".
# Only OBSERVED evidence — exact arithmetic on recorded transactions/terms —
# supports them. An ESTIMATED or MODELED figure rests on an assumption the
# data cannot confirm, so it is at most ATTRIBUTED/INCREMENTAL and MEDIUM.
# (The renewal agent shipped ESTIMATED evidence labeled OBSERVED/HIGH.)
OBSERVATION_CLASSIFICATIONS = frozenset({ValueClassification.OBSERVED, ValueClassification.FINANCIALLY_VERIFIED})
HIGH_CONFIDENCES = frozenset({DecisionConfidence.HIGH, DecisionConfidence.VERY_HIGH})


def labels_exceed_evidence(evidence: EvidenceClass, value: LabeledValue | None) -> str | None:
    """Why `value`'s labels claim more than `evidence` supports, or None.
    orchestrator-go applies the same rule (ledger_record.go
    labelsExceedEvidence) before recording and when reading back."""
    if value is None or evidence == EvidenceClass.OBSERVED:
        return None
    if value.classification in OBSERVATION_CLASSIFICATIONS:
        return (f"classification {value.classification.value} needs OBSERVED evidence, "
                f"this figure is {evidence.value}")
    if value.confidence in HIGH_CONFIDENCES:
        return f"confidence {value.confidence.value} needs OBSERVED evidence, this figure is {evidence.value}"
    return None


class ValueBasis(BaseModel):
    """AEGIS L1 (Oct 7 2026): what a rate-derived dollar figure was computed
    from, recorded with the finding (and, by orchestrator-go, in the evidence
    ledger as an rr_value_basis event) so a quote can show — and anyone can
    recompute — amount = base_usd x rate_percent / 100, cent-rounded half-up.
    Today: the affiliate agent's commission (base = order subtotal, rate =
    the affiliate's commission rate). The Finding checks the arithmetic."""
    model_config = {"frozen": True}

    base_usd: PositiveMoney
    # A plain decimal string (no exponent), exactly the rate the figure used.
    rate_percent: RatePercent


def rate_percent_text(rate: float | Decimal) -> str:
    """The exact decimal text of a rate (15.0 -> "15", 12.5 -> "12.5",
    1e-05 -> "0.00001"); percent_of converts floats the same way (str())."""
    d = Decimal(str(rate)) if isinstance(rate, float) else Decimal(rate)
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


FINDING_ID_PREFIX = "rrf1-"


def compute_finding_id(client_id: str, agent_id: str, entity_type: str, entity_id: str,
                       period_label: str | None) -> str:
    """E-3 (Oct 6 2026): a finding's id is a hash of everything that makes it
    a distinct leak — tenant, agent, entity and period — so two clients'
    "ord_1" never collide, two contract terms of one client and period never
    collide (probe P1: both were "contract-client_b2-2026-06"), and
    "a-b"+"c" vs "a"+"b-c" can no longer concatenate to the same id (probe
    P2). Fields are joined with a newline, which no field's charset allows, so the
    preimage is unambiguous; a missing period is the empty string (no
    PeriodLabel can be empty). orchestrator-go recomputes this exactly
    (internal/orchestrator/finding_id.go) and refuses a finding whose id
    does not match. 160 bits of SHA-256."""
    preimage = "\n".join(("rrf1", client_id, agent_id, entity_type, entity_id, period_label or ""))
    return FINDING_ID_PREFIX + hashlib.sha256(preimage.encode("ascii")).hexdigest()[:40]


class Finding(BaseModel):
    """
    One agent's output for one entity of one client. (client_id,
    entity_type, entity_id) is the shared entity reference required for
    double-count correlation across agents (Failure Mode #2) — never
    optional. finding_id is derived from it (compute_finding_id) and checked.
    """
    finding_id: FindingRef
    client_id: ClientId  # the tenant (ZBM client) whose data produced this finding
    agent_id: AgentId
    leak_category: LeakCategory
    entity_type: EntityType
    entity_id: Id  # order_id, subscription_id, term_id or platform — the correlation key
    # The period this finding is about when the entity recurs (a contract
    # term's billing period, a subscription's missed renewal date); None for
    # one-off entities such as an order. Part of the finding identity.
    period_label: PeriodLabel | None = None
    customer_id: Id
    cause_certainty: CauseCertainty
    cause_description: CauseDescription
    recoverable_value: Optional[LabeledValue] = None
    # E-4: how the dollar figure (or its absence) was arrived at.
    evidence_class: EvidenceClass
    # L1: the base and rate a rate-derived figure was computed from (None for
    # figures not derived from a rate).
    value_basis: ValueBasis | None = None
    methodology_id: MethodologyId
    methodology: Methodology
    detected_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _uncertain_cause_has_no_confident_value(self):
        # A finding that admits cause uncertainty should not simultaneously
        # claim a "financially_verified"/"very_high" figure for it.
        if self.cause_certainty == CauseCertainty.UNCERTAIN and self.recoverable_value is not None:
            if self.recoverable_value.confidence in (DecisionConfidence.HIGH, DecisionConfidence.VERY_HIGH):
                raise ValueError(
                    "Finding cannot pair cause_certainty=UNCERTAIN with a HIGH/VERY_HIGH "
                    "decision confidence — Failure Mode #3 safeguard"
                )
        return self

    @model_validator(mode="after")
    def _evidence_class_matches_value(self):
        # A dollar figure always has a real evidence class, and UNKNOWN never
        # has a dollar figure — the two cannot disagree.
        if self.recoverable_value is None and self.evidence_class != EvidenceClass.UNKNOWN:
            raise ValueError("a finding with no recoverable_value must have evidence_class UNKNOWN")
        if self.recoverable_value is not None and self.evidence_class == EvidenceClass.UNKNOWN:
            raise ValueError("a finding with a recoverable_value cannot have evidence_class UNKNOWN")
        return self

    @model_validator(mode="after")
    def _labels_never_exceed_evidence(self):
        # M2: one rule for every agent, enforced on construction.
        reason = labels_exceed_evidence(self.evidence_class, self.recoverable_value)
        if reason:
            raise ValueError(f"recoverable_value labels claim more than the evidence supports: {reason}")
        return self

    @model_validator(mode="after")
    def _value_basis_reproduces_the_amount(self):
        if self.value_basis is None:
            return self
        if self.recoverable_value is None:
            raise ValueError("value_basis without a recoverable_value")
        expected = percent_of(self.value_basis.base_usd, Decimal(self.value_basis.rate_percent))
        if expected != self.recoverable_value.amount_usd:
            raise ValueError(
                f"recoverable_value {self.recoverable_value.amount_usd} is not value_basis "
                f"{self.value_basis.base_usd} x {self.value_basis.rate_percent}% ({expected})"
            )
        return self

    @model_validator(mode="after")
    def _finding_id_is_derived(self):
        expected = compute_finding_id(self.client_id, self.agent_id, self.entity_type.value,
                                      self.entity_id, self.period_label)
        if self.finding_id != expected:
            raise ValueError(
                "finding_id must be compute_finding_id(client_id, agent_id, entity_type, "
                "entity_id, period_label)"
            )
        return self

    @property
    def correlation_key(self) -> str:
        """(client_id, entity_type, entity_id) as one string. "|" is in none
        of the three charsets, so the key is unambiguous."""
        return f"{self.client_id}|{self.entity_type.value}|{self.entity_id}"


def new_finding(*, client_id: str, agent_id: str, entity_type: EntityType, entity_id: str,
                period_label: str | None = None, **fields) -> Finding:
    """Builds a Finding with its derived finding_id. Every agent uses this."""
    return Finding(
        finding_id=compute_finding_id(client_id, agent_id, entity_type.value, entity_id, period_label),
        client_id=client_id,
        agent_id=agent_id,
        entity_type=entity_type,
        entity_id=entity_id,
        period_label=period_label,
        **fields,
    )
