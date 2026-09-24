"""
Agent E: Server-Side Attribution Agent (Hyros-style).
Single job: flag orders the server confirmed as real but that client-side
pixel tracking never recorded — the iOS/ad-blocker loss pattern. This is
lost VISIBILITY (which channel earned credit), not lost revenue itself —
the order happened either way — so the dollar value is labeled OBSERVED
(the order is real) but the leak is about attribution blindness, not a
missing dollar.
"""

from __future__ import annotations

from zbm_schema import (
    format_money,
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    LeakCategory,
    ValueClassification,
)
from zbm_schema.tier2 import ServerSideAttributionEvent

AGENT_ID = "server-side-attribution-v1"


def detect(events: list[ServerSideAttributionEvent]) -> list[Finding]:
    findings: list[Finding] = []

    for event in events:
        if not (event.server_confirmed and not event.pixel_attributed):
            continue  # only the "server says real, pixel missed it" case is this agent's job

        findings.append(
            Finding(
                finding_id=f"ssa-{event.order_id}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.SERVER_SIDE_ATTRIBUTION_GAP,
                entity_type="order",
                entity_id=event.order_id,
                customer_id="unknown",  # this event stream doesn't carry customer_id; correlation still keys on order_id
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Server confirmed a real ${format_money(event.order_value_usd)} order on {event.channel}, "
                    f"but client-side pixel tracking never recorded it — likely iOS/ad-blocker loss. "
                    f"Channel is real revenue with no attribution credit."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=event.order_value_usd,
                    classification=ValueClassification.OBSERVED,
                    confidence=DecisionConfidence.HIGH,
                ),
            )
        )

    return findings
