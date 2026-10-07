"""
Agent: Discount Misuse Detection
Single job: flag orders where more than one discount code was applied to
a single order — store policy (encoded here as the agent's rule) is one
code per order. Stacking beyond that is the leak: revenue given away
beyond what any single authorized promotion intended.

E-4 (Oct 6 2026): the recoverable amount is the EXCESS — what the stacked
codes gave away beyond what the single most valuable code on the order would
have given on its own. It used to be the whole combined discount, which
also claimed the discount the customer was entitled to (probe P3: $100
order, 10% + 5% codes -> $14.50 claimed; the leak is $4.50). Each code is
assumed to be individually valid, the reading most favourable to the
customer, so the figure is a floor of the leak under the one-code policy.
"""

from __future__ import annotations

from decimal import Decimal

from zbm_schema import (
    EntityType,
    EvidenceClass,
    format_money,
    new_finding,
    money_context,
    percent_of,
    quantize_money,
    CauseCertainty,
    DecisionConfidence,
    DiscountApplication,
    Finding,
    LabeledValue,
    LeakCategory,
    Order,
    ValueClassification,
)

AGENT_ID = "discount-misuse-v1"
METHODOLOGY_ID = "disc_excess_best_code"
METHODOLOGY = (
    "Discount given by all stacked codes, applied in order against the running subtotal, minus the "
    "discount the single most valuable code on the order would have given alone on the full subtotal. "
    "Every code is assumed individually valid, so this is the least the one-code policy was breached by. "
    "Exact cent arithmetic on the recorded order."
)


def _effective_discount_usd(order: Order) -> Decimal:
    """
    The combined dollar value given away by all discounts on the order,
    applied sequentially against the running subtotal — the same way most
    cart engines actually stack percentage discounts.

    Exact Decimal arithmetic (README gap #6): each discount line is
    computed and quantized to cents (ROUND_HALF_UP) at the moment it is
    computed, exactly as a cart engine would record a per-line discount,
    and the running remainder is reduced by that recorded cent amount. No
    binary-float intermediate exists anywhere in this function.
    """
    with money_context():
        remaining = order.subtotal_usd
        total_given = Decimal("0.00")
        for d in order.discounts:
            if d.percent_off is not None:
                given = percent_of(remaining, d.percent_off)
            else:
                given = min(d.amount_off_usd, remaining)
            total_given += given
            remaining -= given
        # total_given <= subtotal <= MAX_MONEY: every step stays in range.
        return quantize_money(total_given)


def _single_code_discount_usd(order: Order, d: DiscountApplication) -> Decimal:
    """What one code would have given on its own, against the full subtotal."""
    with money_context():
        if d.percent_off is not None:
            return percent_of(order.subtotal_usd, d.percent_off)
        return quantize_money(min(d.amount_off_usd, order.subtotal_usd))


def detect(orders: list[Order], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if len(order.discounts) <= 1:
            continue  # zero or one discount code is within policy — not a leak

        given_away = _effective_discount_usd(order)
        best_single = max(_single_code_discount_usd(order, d) for d in order.discounts)
        with money_context():
            # Never negative for valid input (stacking gives at least what any
            # one of its codes gives alone); max() guards cent rounding.
            excess = quantize_money(max(given_away - best_single, Decimal(0)))
        if excess <= 0:
            # N6 (AEGIS round 2): stacked codes that gave away nothing after
            # cent rounding (0% codes, 10% of $0.01, no line items) leak no
            # revenue. ADR 0001 "Zero-value findings": no finding — a 0.00
            # figure cannot be labeled (LabeledValue is positive-only), and
            # constructing one used to raise inside this loop -> HTTP 500.
            # E-4: the same holds when the stack gave nothing beyond the best
            # single code (e.g. a second 0% code, or a first code that already
            # took the whole subtotal).
            continue
        codes = ", ".join(d.code for d in order.discounts)

        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.DISCOUNT_MISUSE,
                entity_type=EntityType.ORDER,
                entity_id=order.order_id,
                customer_id=order.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"{len(order.discounts)} discount codes stacked on one order ({codes}), "
                    f"against single-code policy. The stack gave away ${format_money(given_away)}; the most "
                    f"valuable single code alone would have given ${format_money(best_single)}, so "
                    f"${format_money(excess)} was given away beyond any one authorized code."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=excess,
                    classification=ValueClassification.OBSERVED,
                    # Was VERY_HIGH. The arithmetic is exact, but the one-code
                    # policy is this agent's assumption, not read from the
                    # store's own promotion rules.
                    confidence=DecisionConfidence.HIGH,
                ),
                evidence_class=EvidenceClass.OBSERVED,
                methodology_id=METHODOLOGY_ID,
                methodology=METHODOLOGY,
            )
        )

    return findings
