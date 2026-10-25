"""Shopify orders -> the platform-agnostic ``zbm_schema.Order`` (ADR 0001 decision 5; ADR 0020 section 6).

Minimisation (Shopify protected customer data, ADR 0020 S11): the connector never asks for a customer, a name, an
email, a phone number, an address, a note or an IP, so none can be stored. Every order gets the same
``customer_id`` (``CUSTOMER_NOT_COLLECTED``): the agents that read this data never need to link orders to a person.
What is kept per order: its Shopify numeric id (as ``shopify-order-<id>``), created time, financial status, line
items (SKU, unit price, quantity) and discount codes with their values and the amount Shopify recorded for each —
the inputs of the discount-misuse agent.

Conservative by rule: an order whose discounts the agent's model would misstate is EXCLUDED and counted, never
reshaped into something the store did not record. Excluded: test and cancelled orders; orders not paid; orders with
more line items or discount applications than one page holds; codes on shipping, codes on selected items only
(ENTITLED/EXPLICIT), fixed-amount codes applied to each item (EACH), zero-value codes; non-USD money; codes the schema
cannot hold.

H-1 (final go-live AEGIS, Oct 25 2026). Each code's dollar amount is what Shopify RECORDED for it (the sum of the
line items' ``discountAllocations`` that point at the code's discount application ``index``), never a
recomputation from the code's nominal value. Shopify's allocations include amounts on refunded and removed units,
and an automatic, manual or script discount changes what each code was applied to, so no exact per-code attribution
exists for: an order with any non-code discount application (``non_code_discount_present``), a partially refunded
order (``partially_refunded``), an order with any refund recorded (``refund_recorded``), and an order whose line
quantities were reduced by a refund or an edit (``line_items_refunded_or_removed``). Those orders are excluded and
counted; the report lists them as left out.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import ValidationError

from zbm_schema import DiscountApplication, Order, OrderLineItem

from . import shopify_models as m

CUSTOMER_NOT_COLLECTED = "not-collected"
SOURCE_PLATFORM = "shopify"
# PARTIALLY_REFUNDED is not analysed (H-1): the refunded units' discounts are still in the recorded allocations.
ANALYSED_STATUSES = frozenset({"PAID", "PARTIALLY_PAID"})

# Which Revenue Recovery agents this data source feeds, and why the others get nothing from it. A report must
# never present "no finding" from an agent that had no data as a clean result.
AGENT_COVERAGE = {
    "discount-misuse-v1": (True, "orders with their discount codes are read from the store"),
    "affiliate-coupon-extension-v1": (False, "affiliate clicks and commission rates are not in Shopify order data; "
                                             "no affiliate source is connected"),
    "abandoned-cart-coverage-v1": (False, "abandoned checkouts are not read: they hold customer contact details, "
                                          "and whether a recovery message was sent is not in the order data"),
    "renewal-never-triggered-v1": (False, "subscription contracts are not read"),
    "server-side-attribution-v1": (False, "no server-side tracking source is connected"),
    "cross-channel-attribution-v1": (False, "no marketing-touchpoint source is connected"),
    "platform-integration-v1": (False, "no platform-connection status source is connected"),
    "contract-pricing-term-drift-v1": (False, "no contract or invoice source is connected"),
}


def _usd(money: m.MoneyV2) -> Optional[Decimal]:
    if money.currencyCode != "USD":
        return None
    try:
        d = Decimal(money.amount)
    except InvalidOperation:
        return None
    if d < 0 or d.as_tuple().exponent < -2:
        return None
    return d.quantize(Decimal("0.01"))


def _discount(app: m.DiscountApplicationNode) -> tuple[Optional[DiscountApplication], Optional[str]]:
    """(the code, None) or (None, the exclusion reason for the whole order)."""
    if app.targetType != "LINE_ITEM":
        return None, "shipping_discount_code_not_modelled"
    if app.targetSelection != "ALL":
        return None, "targeted_discount_code_not_modelled"
    code = app.code or ""
    if not 1 <= len(code) <= 64 or not code.isprintable():
        return None, "discount_code_format"
    if isinstance(app.value, m.PercentValue):
        if app.value.percentage <= 0:
            return None, "zero_value_code"
        return DiscountApplication(code=code, percent_off=app.value.percentage), None
    if app.allocationMethod == "EACH":
        return None, "per_item_fixed_discount_not_modelled"
    amount = _usd(m.MoneyV2(amount=app.value.amount, currencyCode=app.value.currencyCode))
    if amount is None:
        return None, "money_format_or_currency"
    if amount <= 0:
        return None, "zero_value_code"
    return DiscountApplication(code=code, amount_off_usd=amount), None


def _recorded_by_index(n: m.OrderNode) -> tuple[Optional[dict], Optional[str]]:
    """({application index: the USD amount Shopify recorded for it on this order}, None) or (None, the exclusion
    reason). Every line's allocations count, including lines dropped as zero-value."""
    known = {app.index for app in n.discountApplications.nodes}
    if len(known) != len(n.discountApplications.nodes):
        return None, "discount_allocation_unmatched"
    by_index: dict = {}
    for li in n.lineItems.nodes:
        for al in li.discountAllocations:
            idx = al.discountApplication.index
            if idx not in known:
                return None, "discount_allocation_unmatched"
            amount = _usd(al.allocatedAmountSet.shopMoney)
            if amount is None:
                return None, "money_format_or_currency"
            by_index[idx] = by_index.get(idx, Decimal("0.00")) + amount
    return by_index, None


def translate(nodes: list) -> tuple[list[Order], dict]:
    """(orders the agents can read, aggregate counts). Nothing per-order is kept beyond the Order itself."""
    orders: list[Order] = []
    excluded: Counter = Counter()
    zero_lines = non_code_discounts = 0
    seen: set = set()
    for n in nodes:
        oid = f"shopify-order-{n.legacyResourceId}"
        if oid in seen:
            excluded["duplicate_in_read"] += 1
            continue
        seen.add(oid)
        if n.test:
            excluded["test_order"] += 1
            continue
        if n.cancelledAt is not None:
            excluded["cancelled"] += 1
            continue
        if n.currencyCode != "USD":
            excluded["money_format_or_currency"] += 1
            continue
        if n.displayFinancialStatus == "PARTIALLY_REFUNDED":
            excluded["partially_refunded"] += 1
            continue
        if n.displayFinancialStatus not in ANALYSED_STATUSES:
            excluded[f"financial_status_{(n.displayFinancialStatus or 'unknown').lower()}"] += 1
            continue
        refunded = _usd(n.totalRefundedSet.shopMoney)
        if refunded is None:
            excluded["money_format_or_currency"] += 1
            continue
        if refunded > 0:
            excluded["refund_recorded"] += 1
            continue
        if n.lineItems.pageInfo.hasNextPage:
            excluded["line_items_over_cap"] += 1
            continue
        if n.discountApplications.pageInfo.hasNextPage:
            excluded["discount_applications_over_cap"] += 1
            continue
        if any(li.currentQuantity != li.quantity for li in n.lineItems.nodes):
            excluded["line_items_refunded_or_removed"] += 1
            continue
        non_code = sum(1 for app in n.discountApplications.nodes if app.typename != "DiscountCodeApplication")
        if non_code:
            non_code_discounts += non_code
            excluded["non_code_discount_present"] += 1
            continue
        reason = None
        lines: list[OrderLineItem] = []
        for li in n.lineItems.nodes:
            price = _usd(li.originalUnitPriceSet.shopMoney)
            if price is None:
                reason = "money_format_or_currency"
                break
            if price <= 0 or li.quantity <= 0:
                zero_lines += 1
                continue
            sku = li.sku if li.sku and len(li.sku) <= 64 and li.sku.isprintable() else ""
            lines.append(OrderLineItem(sku=sku, unit_price_usd=price, quantity=li.quantity))
        recorded: dict = {}
        if reason is None:
            recorded, reason = _recorded_by_index(n)
        discounts: list[DiscountApplication] = []
        if reason is None:
            for app in n.discountApplications.nodes:
                d, why = _discount(app)
                if why:
                    reason = why
                    break
                discounts.append(d.model_copy(update={"recorded_usd": recorded.get(app.index, Decimal("0.00"))}))
        if reason is None:
            subtotal = sum((li.unit_price_usd * li.quantity for li in lines), Decimal("0.00"))
            if sum((d.recorded_usd for d in discounts), Decimal("0.00")) > subtotal:
                reason = "recorded_discount_exceeds_subtotal"
        if reason:
            excluded[reason] += 1
            continue
        try:
            orders.append(Order(order_id=oid, customer_id=CUSTOMER_NOT_COLLECTED, placed_at=n.createdAt,
                                status=n.displayFinancialStatus.lower(), line_items=lines, discounts=discounts,
                                affiliate=None, source_platform=SOURCE_PLATFORM, recovery_attempted=False))
        except (ValidationError, ValueError):
            excluded["schema_refused"] += 1
    counts = {
        "orders_read": len(nodes),
        "orders_analysed": len(orders),
        "orders_excluded": dict(sorted(excluded.items())),
        "orders_with_two_or_more_codes": sum(1 for o in orders if len(o.discounts) > 1),
        "line_items_dropped_zero_value": zero_lines,
        # Non-code discount applications seen on orders excluded as non_code_discount_present.
        "non_code_discounts_left_out": non_code_discounts,
    }
    return orders, counts


def agent_coverage() -> dict:
    return {a: {"covered": c, "why": why} for a, (c, why) in AGENT_COVERAGE.items()}
