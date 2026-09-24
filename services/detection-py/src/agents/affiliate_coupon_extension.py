"""
Agent: Affiliate & Coupon-Extension Leak Detection
Single job (agent doctrine): flag orders where an affiliate-linked coupon
was honored well outside any legitimate attribution window — the
"extended cookie" fraud pattern where an affiliate coupon/link is stretched
to claim commission on organic, unrelated purchases.

This agent does ONE thing: detect the anomaly and label it. It never
executes a fix (that's the Action Execution Agent's job) and never
retrains itself on feedback (Decision 3 — corroboration required).
"""

from __future__ import annotations

from zbm_schema import (
    format_money,
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    LeakCategory,
    Order,
    ValueClassification,
)

AGENT_ID = "affiliate-coupon-extension-v1"

# An affiliate click-to-order gap beyond this multiple of the order's own
# stated attribution window is treated as suspicious extension, not a
# legitimate late conversion. Kept as a named constant, not a magic number,
# so it's an explicit, reviewable policy rather than buried logic.
SUSPICIOUS_WINDOW_OVERRUN_MULTIPLIER = 2.0


def _hours_between(order: Order) -> float | None:
    if order.affiliate is None:
        return None
    delta = order.affiliate.order_timestamp - order.affiliate.click_timestamp
    return delta.total_seconds() / 3600


def detect(orders: list[Order]) -> list[Finding]:
    findings: list[Finding] = []

    for order in orders:
        if order.affiliate is None:
            continue  # no affiliate attribution on this order — not in scope for this agent

        elapsed_hours = _hours_between(order)
        window = order.affiliate.attribution_window_hours

        if elapsed_hours is None or elapsed_hours <= window:
            continue  # within window — legitimate attribution, not a leak

        overrun_ratio = elapsed_hours / window

        if overrun_ratio < SUSPICIOUS_WINDOW_OVERRUN_MULTIPLIER:
            # Outside the window, but not dramatically — genuinely ambiguous.
            # Failure Mode #3 safeguard: don't guess, say so.
            findings.append(
                Finding(
                    finding_id=f"aff-{order.order_id}",
                    agent_id=AGENT_ID,
                    leak_category=LeakCategory.AFFILIATE_COUPON_EXTENSION,
                    entity_type="order",
                    entity_id=order.order_id,
                    customer_id=order.customer_id,
                    cause_certainty=CauseCertainty.UNCERTAIN,
                    cause_description=(
                        f"Affiliate click-to-order gap ({elapsed_hours:.1f}h) exceeds the stated "
                        f"attribution window ({window}h) but not by enough to confidently call it "
                        f"fraud rather than a legitimate delayed purchase."
                    ),
                    recoverable_value=None,
                )
            )
            continue

        # Well outside window — confident finding.
        findings.append(
            Finding(
                finding_id=f"aff-{order.order_id}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.AFFILIATE_COUPON_EXTENSION,
                entity_type="order",
                entity_id=order.order_id,
                customer_id=order.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Affiliate commission honored {elapsed_hours:.1f}h after click, "
                    f"{overrun_ratio:.1f}x the stated {window}h attribution window — "
                    f"consistent with a stretched/extended affiliate cookie rather than "
                    f"a genuinely attributed purchase. Order value ${format_money(order.subtotal_usd)} "
                    f"at risk of unearned commission."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=order.subtotal_usd,
                    classification=ValueClassification.ATTRIBUTED,
                    confidence=DecisionConfidence.HIGH,
                ),
            )
        )

    return findings
