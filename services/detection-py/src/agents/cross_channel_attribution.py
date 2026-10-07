"""
Agent F: Cross-Channel Attribution Modeling Agent (Northbeam-style).
Single job: flag orders where a PAID first-touch channel was overridden
by last-click credit to a later, unpaid touchpoint — a pattern consistent
with under-crediting paid channels that likely started the journey.

Deliberately conservative (Decision 4 — phased build, simple first): this
agent does NOT run real multi-touch attribution modeling yet, and does
NOT claim a dollar figure for the miscount, because it cannot actually
compute what the "correct" credit split would be without that model.
Per Failure Mode #3, it names the pattern and stops at cause_certainty
UNCERTAIN with no recoverable_value, rather than fabricate a number.

E-4 review (Oct 6 2026): no dollar figure — unchanged; now labeled UNKNOWN
with its methodology note. A future multi-touch model's figure would be
MODELED, never OBSERVED.
"""

from __future__ import annotations

from collections import defaultdict

from zbm_schema import (
    CauseCertainty,
    EntityType,
    EvidenceClass,
    Finding,
    LeakCategory,
    new_finding,
)
from zbm_schema.tier2 import ChannelTouchpoint

AGENT_ID = "cross-channel-attribution-v1"
METHODOLOGY_ID = "xchan_no_model"
METHODOLOGY = (
    "No dollar figure: how much credit the paid first touch should have received needs a multi-touch "
    "attribution model, which is not built. The finding names the pattern only."
)


def detect(touchpoints: list[ChannelTouchpoint], *, client_id: str) -> list[Finding]:
    by_order: dict[str, list[ChannelTouchpoint]] = defaultdict(list)
    for tp in touchpoints:
        by_order[tp.order_id].append(tp)

    findings: list[Finding] = []

    for order_id, tps in by_order.items():
        if len(tps) < 2:
            continue  # single-touch orders have nothing to misattribute

        tps_sorted = sorted(tps, key=lambda t: t.touchpoint_sequence)
        first_touch = tps_sorted[0]
        credited = next((t for t in tps_sorted if t.is_credited_conversion_channel), None)

        if credited is None:
            continue  # no credited channel recorded — not this agent's finding to make

        if first_touch.is_paid_channel and credited.channel != first_touch.channel:
            findings.append(
                new_finding(
                    client_id=client_id,
                    agent_id=AGENT_ID,
                    leak_category=LeakCategory.CROSS_CHANNEL_MISATTRIBUTION_RISK,
                    entity_type=EntityType.ORDER,
                    entity_id=order_id,
                    customer_id="unknown",
                    cause_certainty=CauseCertainty.UNCERTAIN,
                    cause_description=(
                        f"Paid channel '{first_touch.channel}' was the first touchpoint, but "
                        f"last-click credit went to '{credited.channel}' instead. Consistent with "
                        f"under-crediting the paid channel that likely started this journey — "
                        f"a real multi-touch model (not yet built) would be needed to confirm the "
                        f"correct split, so no dollar figure is claimed here."
                    ),
                    recoverable_value=None,
                    evidence_class=EvidenceClass.UNKNOWN,
                    methodology_id=METHODOLOGY_ID,
                    methodology=METHODOLOGY,
                )
            )

    return findings
