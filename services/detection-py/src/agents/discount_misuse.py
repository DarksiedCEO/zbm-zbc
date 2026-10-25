"""
Agent: Discount Misuse Detection
Single job: flag orders where more than one discount code was applied to
a single order, and put a dollar figure on what the stack gave away beyond
the single most valuable code.

E-4 (Oct 6 2026): the figure is the EXCESS — what the stacked codes gave
away beyond what the single most valuable code on the order would have
given on its own (probe P3: $100 order, 10% + 5% codes -> the excess is
$4.50, not the whole $14.50).

H-1 (final go-live AEGIS, Oct 25 2026):
  - When the source recorded what each code gave (Shopify: the line items'
    discountAllocations, store_connector.translate), those recorded amounts
    are the stack's total. They are never recomputed from the codes'
    nominal values against the full price (Shopify computes two percentage
    order codes on the same subtotal, and applies order codes after product
    and automatic discounts; a recomputation misstates both).
  - The "one code per order" policy is this agent's ASSUMPTION, not
    something read from the store. Shopify only lets codes combine when the
    store's own discount settings allow it, so a stack on a Shopify order is
    not an observed violation. Every finding is therefore ESTIMATED
    (labels attributed/medium, the most zbm_schema allows ESTIMATED
    evidence), and its cause states the assumption next to the figure. It
    is never OBSERVED, so the client report never marks it quotable.
  - The subtrahend is the most any one code could have given ALONE on the
    full subtotal (never less than what the store recorded for it), so the
    excess is a floor under the stated assumption.
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
from zbm_schema.limits import CAUSE_DESCRIPTION_MAX_CHARS

AGENT_ID = "discount-misuse-v1"
METHODOLOGY_ID = "disc_excess_best_code"
METHODOLOGY_ID_RECORDED = "disc_excess_recorded"
ASSUMPTION = (
    "Assumption, not an observed breach: this counts as a leak only if the store intends at most one code per "
    "order. Shopify combines codes only when each discount's combination setting allows it, so the store's own "
    "settings permitted this stack."
)
METHODOLOGY = (
    "Discount given by all stacked codes, applied in order against the running subtotal (the source recorded no "
    "per-code amounts), minus the discount the single most valuable code would have given alone on the full "
    "subtotal. Exact cent arithmetic. ESTIMATED: a leak only under the assumed one-code-per-order policy."
)
METHODOLOGY_RECORDED = (
    "Sum of the discount the store recorded for each stacked code on this order (Shopify's per-line discount "
    "allocations), minus the most the single most valuable code could have given alone on the full subtotal. "
    "Only orders whose discounts are all codes, with no refund and no reduced line, are analysed. Exact cents. "
    "ESTIMATED: a leak only under the assumed one-code-per-order policy, which the store's settings did not enforce."
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


def _cause(order: Order, given_away: Decimal, best_single: Decimal, excess: Decimal, recorded: bool) -> str:
    given = (f"The store recorded ${format_money(given_away)} of discount from them" if recorded else
             f"Applied in turn they give ${format_money(given_away)}")
    rest = (f"; the most valuable code alone would have given at most ${format_money(best_single)}, so "
            f"${format_money(excess)} was given beyond one code. {ASSUMPTION}")
    codes = ", ".join(d.code for d in order.discounts)
    text = f"{len(order.discounts)} discount codes on one order ({codes}). {given}{rest}"
    if len(text) > CAUSE_DESCRIPTION_MAX_CHARS:
        text = f"{len(order.discounts)} discount codes on one order. {given}{rest}"
    return text


def detect(orders: list[Order], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if len(order.discounts) <= 1:
            continue  # zero or one discount code is within policy — not a leak

        recorded_amounts = [d.recorded_usd for d in order.discounts]
        if all(r is not None for r in recorded_amounts):
            recorded = True
            with money_context():
                given_away = quantize_money(sum(recorded_amounts, Decimal("0.00")))
                # Never less than what the store recorded for that code (H-1: the excess stays a floor).
                best_single = max(max(_single_code_discount_usd(order, d), d.recorded_usd) for d in order.discounts)
        elif any(r is not None for r in recorded_amounts):
            continue  # some codes recorded and some not: no exact figure exists, so no finding
        else:
            recorded = False
            given_away = _effective_discount_usd(order)
            best_single = max(_single_code_discount_usd(order, d) for d in order.discounts)
        with money_context():
            # max() guards cent rounding and recorded amounts below the
            # best code's stand-alone value.
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

        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.DISCOUNT_MISUSE,
                entity_type=EntityType.ORDER,
                entity_id=order.order_id,
                customer_id=order.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=_cause(order, given_away, best_single, excess, recorded),
                recoverable_value=LabeledValue(
                    amount_usd=excess,
                    # H-1: was OBSERVED/HIGH. The arithmetic is exact, but the
                    # one-code policy is this agent's assumption (see module
                    # docstring), so the figure is ESTIMATED and its labels
                    # are capped at attributed/medium (zbm_schema M2).
                    classification=ValueClassification.ATTRIBUTED,
                    confidence=DecisionConfidence.MEDIUM,
                ),
                evidence_class=EvidenceClass.ESTIMATED,
                methodology_id=METHODOLOGY_ID_RECORDED if recorded else METHODOLOGY_ID,
                methodology=METHODOLOGY_RECORDED if recorded else METHODOLOGY,
            )
        )

    return findings
