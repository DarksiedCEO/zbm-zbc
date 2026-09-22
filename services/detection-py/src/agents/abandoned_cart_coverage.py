"""
Agent: Abandoned-Cart Coverage Detection
Single job: flag abandoned-cart orders where NO recovery attempt
(email/SMS flow) ever fired — a gap in the store's own recovery
coverage, not a judgment on whether the recovery would have worked.

Explicitly does NOT flag abandoned carts where recovery_attempted=True —
that's the store's recovery system doing its job; flagging it would be a
false positive against a working safeguard (this distinction was the
whole reason the fixture pool has a control case for it).
"""

from __future__ import annotations

from zbm_schema import (
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    LeakCategory,
    Order,
    ValueClassification,
)

AGENT_ID = "abandoned-cart-coverage-v1"


def detect(orders: list[Order]) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if order.status != "abandoned_cart":
            continue
        if order.recovery_attempted:
            continue  # coverage worked as intended — not a leak

        findings.append(
            Finding(
                finding_id=f"cart-{order.order_id}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.ABANDONED_CART_COVERAGE,
                entity_type="order",
                entity_id=order.order_id,
                customer_id=order.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Order abandoned at checkout (${order.subtotal_usd:.2f} cart value) with no "
                    f"recovery email/SMS ever triggered — a gap in recovery-flow coverage, not a "
                    f"claim about whether recovery would have converted."
                ),
                recoverable_value=LabeledValue(
                    # Cart value is OBSERVED (the amount is real); whether it converts if recovered
                    # is not — so this is an INCREMENTAL opportunity estimate, not a certain figure,
                    # with confidence set accordingly rather than overstated.
                    amount_usd=order.subtotal_usd,
                    classification=ValueClassification.INCREMENTAL,
                    confidence=DecisionConfidence.MEDIUM,
                ),
            )
        )

    return findings
