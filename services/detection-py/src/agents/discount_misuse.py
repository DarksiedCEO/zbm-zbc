"""
Agent: Discount Misuse Detection
Single job: flag orders where more than one discount code was applied to
a single order — store policy (encoded here as the agent's rule) is one
code per order. Stacking beyond that is the leak: revenue given away
beyond what any single authorized promotion intended.
"""

from __future__ import annotations

from decimal import Decimal

from zbm_schema import (
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    LeakCategory,
    Order,
    ValueClassification,
    to_money,
)

AGENT_ID = "discount-misuse-v1"


def _effective_discount_usd(order: Order) -> Decimal:
    """
    Approximates the combined dollar value given away by all discounts on
    the order, applied sequentially against the running subtotal — the
    same way most cart engines actually stack percentage discounts.
    """
    remaining = order.subtotal_usd
    total_given = Decimal("0")
    for d in order.discounts:
        if d.percent_off is not None:
            # percent_off is a plain float ratio (not currency); convert via
            # str() so it doesn't drag binary-float imprecision into the
            # Decimal money arithmetic (see to_money's own docstring).
            given = remaining * (Decimal(str(d.percent_off)) / Decimal("100"))
        else:
            given = min(d.amount_off_usd, remaining)
        total_given += given
        remaining -= given
    return to_money(total_given)


def detect(orders: list[Order]) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if len(order.discounts) <= 1:
            continue  # zero or one discount code is within policy — not a leak

        given_away = _effective_discount_usd(order)
        codes = ", ".join(d.code for d in order.discounts)

        findings.append(
            Finding(
                finding_id=f"disc-{order.order_id}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.DISCOUNT_MISUSE,
                entity_type="order",
                entity_id=order.order_id,
                customer_id=order.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"{len(order.discounts)} discount codes stacked on one order ({codes}), "
                    f"against single-code policy. Combined discount value ${given_away:.2f} "
                    f"exceeds what any one authorized code should have given away."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=given_away,
                    classification=ValueClassification.OBSERVED,
                    confidence=DecisionConfidence.VERY_HIGH,
                ),
            )
        )

    return findings
