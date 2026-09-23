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

from pydantic import BaseModel, Field, field_validator

from zbm_schema import to_money


class ServerSideAttributionEvent(BaseModel):
    """One order's client-side (pixel) vs server-side confirmed attribution."""
    order_id: str
    channel: str
    order_value_usd: Decimal = Field(gt=0)
    pixel_attributed: bool  # did client-side pixel tracking record this conversion?
    server_confirmed: bool  # did server-side tracking independently confirm the order happened?

    @field_validator("order_value_usd", mode="before")
    @classmethod
    def _money(cls, v):
        return to_money(v)


class ChannelTouchpoint(BaseModel):
    order_id: str
    channel: str
    touchpoint_sequence: int = Field(ge=1)  # 1 = first touch, higher = later
    is_paid_channel: bool
    is_credited_conversion_channel: bool  # the channel the store's current (last-click) model credits


class PlatformConnectionStatus(BaseModel):
    client_id: str
    platform: str
    client_reports_using_it: bool  # client told onboarding they use this platform
    integration_connected: bool  # ZBM actually has a working data connection to it


class ContractTermType(str, Enum):
    MINIMUM_SPEND = "minimum_spend"
    ESCALATOR = "escalator"
    OVERAGE_RATE = "overage_rate"


class ContractTerm(BaseModel):
    term_id: str  # unique per contract line — a client can have multiple terms of the same type/period
    client_id: str
    term_type: ContractTermType
    contracted_value_usd: Decimal = Field(gt=0)
    actual_billed_value_usd: Decimal = Field(ge=0)
    period_label: str  # e.g. "2026-06"

    @field_validator("contracted_value_usd", "actual_billed_value_usd", mode="before")
    @classmethod
    def _money(cls, v):
        return to_money(v)

    @property
    def drift_usd(self) -> Decimal:
        return to_money(self.contracted_value_usd - self.actual_billed_value_usd)
