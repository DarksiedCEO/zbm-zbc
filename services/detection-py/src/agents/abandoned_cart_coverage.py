"""
Agent: Abandoned-Cart Coverage Detection
Single job: flag abandoned-cart orders where NO recovery attempt
(email/SMS flow) ever fired — a gap in the store's own recovery
coverage, not a judgment on whether the recovery would have worked.

Explicitly does NOT flag abandoned carts where recovery_attempted=True —
that's the store's recovery system doing its job; flagging it would be a
false positive against a working safeguard (this distinction was the
whole reason the fixture pool has a control case for it).

E-4 review (Oct 6 2026): this agent used to claim the WHOLE cart value as
recoverable (INCREMENTAL/MEDIUM). Only the share of abandoned carts that a
recovery flow actually wins back is recoverable, and no measured recovery
rate for the store exists in the data this agent sees — so the full cart
value overstated the figure by roughly the inverse of that rate. The finding
now claims no dollar figure (evidence UNKNOWN). A store-measured recovery
rate is the input that would make an ESTIMATED figure possible.

E-11: status is normalized (limits.Slug), so "Abandoned_Cart" is no longer a
silent miss (probe P6).
"""

from __future__ import annotations

from zbm_schema import (
    ORDER_STATUS_ABANDONED_CART,
    CauseCertainty,
    EntityType,
    EvidenceClass,
    Finding,
    LeakCategory,
    Order,
    new_finding,
)

AGENT_ID = "abandoned-cart-coverage-v1"
METHODOLOGY_ID = "cart_no_recovery_rate"
METHODOLOGY = (
    "No dollar figure: the recoverable share of an abandoned cart is the cart value times the rate at which "
    "a recovery flow wins carts back, and no measured recovery rate for this store is available. The cart "
    "value is the ceiling of the opportunity, not a recoverable amount."
)


def detect(orders: list[Order], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if order.status != ORDER_STATUS_ABANDONED_CART:
            continue
        if order.recovery_attempted:
            continue  # coverage worked as intended — not a leak
        if order.subtotal_usd <= 0:
            # N6 sweep: a valid order may have no line items (subtotal 0.00).
            # Nothing to recover -> no finding (ADR 0001 "Zero-value
            # findings").
            continue

        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.ABANDONED_CART_COVERAGE,
                entity_type=EntityType.ORDER,
                entity_id=order.order_id,
                customer_id=order.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                # No dollar figure in the text either: the Hallucination
                # Agent rejects a stated figure with no recoverable_value.
                cause_description=(
                    "Order abandoned at checkout with no recovery email/SMS ever triggered — a gap in "
                    "recovery-flow coverage. No recoverable amount is claimed: what a recovery flow would "
                    "win back depends on a recovery rate this store has not measured."
                ),
                recoverable_value=None,
                evidence_class=EvidenceClass.UNKNOWN,
                methodology_id=METHODOLOGY_ID,
                methodology=METHODOLOGY,
            )
        )

    return findings
