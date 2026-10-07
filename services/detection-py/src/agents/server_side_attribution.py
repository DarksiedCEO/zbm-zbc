"""
Agent E: Server-Side Attribution Agent (Hyros-style).
Single job: flag orders the server confirmed as real but that client-side
pixel tracking never recorded — the iOS/ad-blocker loss pattern. This is
lost VISIBILITY (which channel earned credit), not lost revenue itself —
the order happened and was paid either way.

E-4 (Oct 6 2026): the agent used to put the whole order value into
recoverable_value as OBSERVED/HIGH (probe P4: $250.00 "recoverable" on a
$250 order) although, as this docstring always said, no revenue was lost.
Nothing here is recoverable money; the value of fixing the gap (better ad
optimization) is not measurable from this data. The finding now claims no
dollar figure (evidence UNKNOWN) and its text states no amount, so the
HIGH-confidence dollar claim is gone entirely.
"""

from __future__ import annotations

from zbm_schema import (
    CauseCertainty,
    EntityType,
    EvidenceClass,
    Finding,
    LeakCategory,
    new_finding,
)
from zbm_schema.tier2 import ServerSideAttributionEvent

AGENT_ID = "server-side-attribution-v1"
METHODOLOGY_ID = "ssa_visibility_gap"
METHODOLOGY = (
    "No dollar figure: the order was real and paid, so no revenue was lost. The gap is attribution "
    "visibility; its value (better channel optimization) is not measurable from this data."
)


def detect(events: list[ServerSideAttributionEvent], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for event in events:
        if not (event.server_confirmed and not event.pixel_attributed):
            continue  # only the "server says real, pixel missed it" case is this agent's job

        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.SERVER_SIDE_ATTRIBUTION_GAP,
                entity_type=EntityType.ORDER,
                entity_id=event.order_id,
                customer_id="unknown",  # this event stream doesn't carry customer_id; correlation still keys on the order
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Server confirmed a real order on {event.channel}, but client-side pixel tracking "
                    f"never recorded it — likely iOS/ad-blocker loss. The revenue was received; what is "
                    f"missing is the channel's attribution credit, so no recoverable amount is claimed."
                ),
                recoverable_value=None,
                evidence_class=EvidenceClass.UNKNOWN,
                methodology_id=METHODOLOGY_ID,
                methodology=METHODOLOGY,
            )
        )

    return findings
