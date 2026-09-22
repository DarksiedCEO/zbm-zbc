"""
Agent: Renewal-Never-Triggered Detection
Single job: flag subscriptions whose status is explicitly
lapsed_no_renewal_attempt — the renewal engine never fired at all.

Deliberately narrow: does NOT flag `cancelled` (a customer's own choice,
not a system failure) and does NOT flag `past_due` where a renewal
attempt actually happened but failed (a different leak category —
payment/dunning recovery — explicitly out of scope for Revenue Recovery
per the founder's original scope correction).
"""

from __future__ import annotations

from zbm_schema import (
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    LeakCategory,
    Subscription,
    SubscriptionStatus,
    ValueClassification,
)

AGENT_ID = "renewal-never-triggered-v1"


def detect(subscriptions: list[Subscription]) -> list[Finding]:
    findings: list[Finding] = []

    for sub in subscriptions:
        if sub.status != SubscriptionStatus.LAPSED_NO_RENEWAL_ATTEMPT:
            continue

        findings.append(
            Finding(
                finding_id=f"renew-{sub.subscription_id}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.RENEWAL_NEVER_TRIGGERED,
                entity_type="subscription",
                entity_id=sub.subscription_id,
                customer_id=sub.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Subscription (${sub.plan_price_usd:.2f}/{sub.renewal_interval_days}d) reached "
                    f"its renewal date ({sub.next_renewal_due_at.date()}) with no renewal attempt "
                    f"ever fired — a trigger failure, not a customer cancellation or a failed charge."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=sub.plan_price_usd,
                    classification=ValueClassification.OBSERVED,
                    confidence=DecisionConfidence.HIGH,
                ),
            )
        )

    return findings
