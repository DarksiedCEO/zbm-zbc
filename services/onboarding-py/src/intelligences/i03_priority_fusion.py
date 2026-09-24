"""
Intelligence 3 — Priority Fusion.

Decides: ONE merged plan from the audit findings plus the client's stated
priorities ("what we found + what you told us matters").

Rules:
- The client's priority order is the plan order. Always. Audit-only topics
  are appended after the client's, never inserted above them.
- On disagreement, both views are shown side by side with a
  recommendation and ``needs_client_choice=True``. The recommended order is
  a separate field; it is never applied without the client choosing it.
  Disagreement means either (a) the audit's strongest-evidence topic is
  missing from the client's list, or (b) the client's #1 has no audit
  evidence while another topic has high/very-high confidence evidence.
- Evidence strength of a topic = (best decision confidence, number of
  findings, largest single labeled value). Dollar values are never summed
  across findings here; a plan item carries at most the single largest
  LabeledValue behind it, with its labels.
- Findings flagged as double-count risk are excluded.
"""

from __future__ import annotations

from decimal import Decimal

from guardrails import check_outbound
from onboarding_schema import (
    CONFIDENCE_RANK,
    ConsumedFinding,
    DecisionConfidence,
    Disagreement,
    LabeledValue,
    MergedPlan,
    PlanItem,
)

from ._status import PHASE1_STATUS

NUMBER = 3
NAME = "Priority Fusion"
PHASE = 1
STATUS = PHASE1_STATUS

CATEGORY_TOPIC = {
    "affiliate_coupon_extension": "affiliate_leakage",
    "discount_misuse": "discount_abuse",
    "abandoned_cart_coverage": "abandoned_carts",
    "renewal_never_triggered": "renewals",
    "server_side_attribution_gap": "tracking",
    "platform_integration_gap": "tracking",
    "cross_channel_misattribution_risk": "attribution",
    "contract_pricing_term_drift": "contract_pricing",
}
TOPIC_SERVICE = {
    "affiliate_leakage": "revenue_recovery",
    "discount_abuse": "revenue_recovery",
    "abandoned_carts": "revenue_recovery",
    "renewals": "revenue_recovery",
    "tracking": "revenue_recovery",
    "attribution": "revenue_recovery",
    "contract_pricing": "revenue_recovery",
    "ad_spend_waste": "digital_advertising",
    "lead_volume": "digital_advertising",
    "new_customers": "digital_advertising",
    "brand_awareness": "out_of_home",
    "local_visibility": "out_of_home",
}


def _strength(fs: list[ConsumedFinding]) -> tuple[int, int, Decimal]:
    best_conf = max((CONFIDENCE_RANK[f.recoverable_value.confidence] for f in fs if f.recoverable_value), default=0)
    biggest = max((f.recoverable_value.amount_usd for f in fs if f.recoverable_value), default=Decimal("0"))
    return best_conf, len(fs), biggest


def _largest_value(fs: list[ConsumedFinding]) -> LabeledValue | None:
    vals = [f.recoverable_value for f in fs if f.recoverable_value]
    return max(vals, key=lambda v: (CONFIDENCE_RANK[v.confidence], v.amount_usd)) if vals else None


def _pretty(topic: str) -> str:
    return topic.replace("_", " ")


def fuse(client_id: str, findings: list[ConsumedFinding], client_priorities: list[str]) -> MergedPlan:
    usable = [f for f in findings if not f.double_count_risk]
    by_topic: dict[str, list[ConsumedFinding]] = {}
    for f in usable:
        by_topic.setdefault(CATEGORY_TOPIC.get(f.leak_category, f.leak_category), []).append(f)

    seen: list[str] = []
    for p in client_priorities:
        if p not in seen:
            seen.append(p)
    client_priorities = seen

    items: list[PlanItem] = []
    for rank, topic in enumerate(client_priorities, start=1):
        fs = by_topic.get(topic, [])
        items.append(PlanItem(
            topic=topic,
            service=TOPIC_SERVICE.get(topic, "unassigned"),
            source="both" if fs else "client",
            client_rank=rank,
            evidence_finding_ids=[f.finding_id for f in fs],
            value=_largest_value(fs),
            note="" if fs else "No audit evidence on this yet; we'll look at it specifically.",
        ))
    audit_only = sorted((t for t in by_topic if t not in client_priorities), key=lambda t: _strength(by_topic[t]), reverse=True)
    for topic in audit_only:
        fs = by_topic[topic]
        items.append(PlanItem(
            topic=topic, service=TOPIC_SERVICE.get(topic, "unassigned"), source="audit",
            evidence_finding_ids=[f.finding_id for f in fs], value=_largest_value(fs),
            note="Found in the audit; you didn't list it. Added after your priorities.",
        ))

    disagreements: list[Disagreement] = []
    strongest = max(by_topic, key=lambda t: _strength(by_topic[t])) if by_topic else None
    if strongest and strongest not in client_priorities:
        v = _largest_value(by_topic[strongest])
        disagreements.append(Disagreement(
            topic=strongest,
            client_view=f"You didn't list {_pretty(strongest)} as a priority.",
            audit_view=(
                f"The audit's strongest evidence is {_pretty(strongest)}: {len(by_topic[strongest])} finding(s)"
                + (f", largest single item {v.render()}" if v else "") + "."
            ),
            recommendation=(
                f"We recommend looking at {_pretty(strongest)} early. Your order stays as you set it unless you choose to change it."
            ),
        ))
    if client_priorities and not by_topic.get(client_priorities[0]):
        strong_other = [t for t in by_topic if _strength(by_topic[t])[0] >= CONFIDENCE_RANK[DecisionConfidence.HIGH] and t != client_priorities[0]]
        if strong_other:
            top = max(strong_other, key=lambda t: _strength(by_topic[t]))
            if not any(d.topic == top for d in disagreements):
                disagreements.append(Disagreement(
                    topic=client_priorities[0],
                    client_view=f"Your #1 priority is {_pretty(client_priorities[0])}.",
                    audit_view=f"The account data doesn't show evidence there yet; it does show high-confidence evidence on {_pretty(top)}.",
                    recommendation=f"Keep {_pretty(client_priorities[0])} first and run {_pretty(top)} alongside it. Your call.",
                ))

    recommended = sorted(
        [i.topic for i in items],
        key=lambda t: (-(_strength(by_topic[t])[0] if t in by_topic else 0),
                       client_priorities.index(t) if t in client_priorities else 999),
    )
    lines = ["Here's your plan: what we found, plus what you told us matters, in your order."]
    for i in items:
        lines.append(f"- {_pretty(i.topic)} ({i.source})" + (f": largest single item {i.value.render()}" if i.value else ""))
    if disagreements:
        lines.append("Where the data and your priorities differ, we've shown both and a recommendation; nothing changes unless you choose.")
    return MergedPlan(
        client_id=client_id, items=items, disagreements=disagreements,
        recommended_order=recommended, summary_text=check_outbound("\n".join(lines)),
    )

