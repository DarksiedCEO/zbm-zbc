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

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from zbm_schema.limits import (
    MAX_ATTRIBUTION_WINDOW_HOURS,
    MAX_DISCOUNTS_PER_ORDER,
    MAX_LINE_ITEMS_PER_ORDER,
    MAX_QUANTITY,
    MAX_RENEWAL_INTERVAL_DAYS,
    AgentId,
    CauseDescription,
    DiscountCode,
    FindingRef,
    Id,
    Label,
    Sku,
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
    customer_id: str
    email: str
    created_at: datetime
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
    placed_at: datetime
    status: Label  # "completed" | "abandoned_cart" | "cancelled"
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
    last_renewal_at: Optional[datetime] = None
    next_renewal_due_at: datetime
    status: SubscriptionStatus


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


class Finding(BaseModel):
    """
    One agent's output for one entity. `order_id` (or `subscription_id`)
    is the shared entity reference required for double-count correlation
    across agents (Failure Mode #2) — never optional.
    """
    finding_id: FindingRef
    agent_id: AgentId
    leak_category: LeakCategory
    entity_type: Label  # "order" | "subscription"
    entity_id: FindingRef  # order_id or subscription_id — the correlation key
    customer_id: Id
    cause_certainty: CauseCertainty
    cause_description: CauseDescription
    recoverable_value: Optional[LabeledValue] = None
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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
