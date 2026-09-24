"""
Tier 2 entity models (server-side attribution, cross-channel touchpoints,
platform integration status, contract terms). Kept in a separate module
from the core Tier 1 commerce entities in __init__.py because these
describe a different layer (marketing/attribution/contract data, not
order/customer/subscription data) — Order/Customer/Subscription stay the
platform-agnostic e-commerce core; these are additive.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from zbm_schema.limits import MAX_TOUCHPOINT_SEQUENCE, Id, Label
from zbm_schema.money import Money, PositiveMoney, money_context, quantize_money


class ServerSideAttributionEvent(BaseModel):
    """One order's client-side (pixel) vs server-side confirmed attribution."""
    order_id: Id
    channel: Label
    order_value_usd: PositiveMoney
    pixel_attributed: bool  # did client-side pixel tracking record this conversion?
    server_confirmed: bool  # did server-side tracking independently confirm the order happened?


class ChannelTouchpoint(BaseModel):
    order_id: Id
    channel: Label
    touchpoint_sequence: int = Field(ge=1, le=MAX_TOUCHPOINT_SEQUENCE)  # 1 = first touch, higher = later
    is_paid_channel: bool
    is_credited_conversion_channel: bool  # the channel the store's current (last-click) model credits


class PlatformConnectionStatus(BaseModel):
    client_id: Id
    platform: Label
    client_reports_using_it: bool  # client told onboarding they use this platform
    integration_connected: bool  # ZBM actually has a working data connection to it


class ContractTermType(str, Enum):
    MINIMUM_SPEND = "minimum_spend"
    ESCALATOR = "escalator"
    OVERAGE_RATE = "overage_rate"


class ContractTerm(BaseModel):
    term_id: Id  # unique per contract line — a client can have multiple terms of the same type/period
    client_id: Id
    term_type: ContractTermType
    contracted_value_usd: PositiveMoney
    actual_billed_value_usd: Money
    period_label: Label  # e.g. "2026-06"

    @property
    def drift_usd(self) -> Decimal:
        # Exact Decimal subtraction; may be negative (billed above contract),
        # which callers treat as "no drift". Never serialized. Both operands
        # are in [0, MAX_MONEY], so the result is in range and exact.
        with money_context():
            return quantize_money(self.contracted_value_usd - self.actual_billed_value_usd)
