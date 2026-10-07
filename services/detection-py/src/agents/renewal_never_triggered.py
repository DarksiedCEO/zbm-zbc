"""
Agent: Renewal-Never-Triggered Detection
Single job: flag subscriptions whose status is explicitly
lapsed_no_renewal_attempt — the renewal engine never fired at all.

Deliberately narrow: does NOT flag `cancelled` (a customer's own choice,
not a system failure) and does NOT flag `past_due` where a renewal
attempt actually happened but failed (a different leak category —
payment/dunning recovery — explicitly out of scope for Revenue Recovery
per the founder's original scope correction).

Revenue Recovery fix wave (Oct 6 2026):
  - E-13: the status alone used to decide, so a subscription "lapsed" with a
    renewal due in 2099 was reported as a missed renewal (probe P11). The
    renewal must now actually be due: next_renewal_due_at <= as_of, where
    as_of is the scan's instant, passed in (never read from the clock here,
    so a scan is reproducible). Both are timezone-aware (probe P12).
  - E-3: the missed renewal's due date is the finding's period, so the same
    subscription missing a later renewal is a different finding.
  - E-4 review: one cycle at plan price is the charge that should have been
    attempted — not inflated, unchanged. It is ESTIMATED, not OBSERVED:
    whether that charge would have succeeded is not in the data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from zbm_schema import (
    format_money,
    CauseCertainty,
    DecisionConfidence,
    EntityType,
    EvidenceClass,
    Finding,
    LabeledValue,
    LeakCategory,
    Subscription,
    SubscriptionStatus,
    ValueClassification,
    new_finding,
)

AGENT_ID = "renewal-never-triggered-v1"
METHODOLOGY_ID = "renewal_one_cycle"
METHODOLOGY = (
    "One renewal cycle at the subscription's plan price: the charge that should have been attempted on "
    "the due date. Estimated: assumes the charge would have succeeded; not adjusted for payment failure "
    "or churn, and later missed cycles are not added."
)


def detect(subscriptions: list[Subscription], *, client_id: str, as_of: datetime) -> list[Finding]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")

    findings: list[Finding] = []

    for sub in subscriptions:
        if sub.status != SubscriptionStatus.LAPSED_NO_RENEWAL_ATTEMPT:
            continue
        if sub.next_renewal_due_at > as_of:
            continue  # not due yet as of this scan — nothing has been missed

        due_utc = sub.next_renewal_due_at.astimezone(timezone.utc)
        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.RENEWAL_NEVER_TRIGGERED,
                entity_type=EntityType.SUBSCRIPTION,
                entity_id=sub.subscription_id,
                period_label=due_utc.date().isoformat(),
                customer_id=sub.customer_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Subscription (${format_money(sub.plan_price_usd)}/{sub.renewal_interval_days}d) reached "
                    f"its renewal date ({due_utc.date()} UTC) with no renewal attempt "
                    f"ever fired — a trigger failure, not a customer cancellation or a failed charge."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=sub.plan_price_usd,
                    classification=ValueClassification.OBSERVED,
                    confidence=DecisionConfidence.HIGH,
                ),
                evidence_class=EvidenceClass.ESTIMATED,
                methodology_id=METHODOLOGY_ID,
                methodology=METHODOLOGY,
            )
        )

    return findings
