"""
Agent G: Platform Integration Agent (Triple Whale-style coverage check).
Single job: flag platforms a client reports using but that have no
working ZBM data integration — a visibility gap, since every other
detection agent is blind on a platform with no connection. No dollar
value is claimed (this is a coverage gap, not a transaction), which is
why Finding.recoverable_value is legitimately None here.

Revenue Recovery fix wave (Oct 6 2026):
  - E-3: the client is now Finding.client_id and the entity is the platform
    itself. The old id "platform-{client_id}-{platform}" made client "a-b" +
    platform "c" and client "a" + platform "b-c" the same finding (probe P2).
  - Tenant: a status row whose client_id is not the scan's client is refused.
  - E-4 review: no dollar figure — unchanged; labeled UNKNOWN.
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
from zbm_schema.tier2 import PlatformConnectionStatus

AGENT_ID = "platform-integration-v1"
METHODOLOGY_ID = "platform_coverage_gap"
METHODOLOGY = (
    "No dollar figure: a missing integration is a visibility gap — every other agent is blind on this "
    "platform — not a transaction with a value."
)


def detect(statuses: list[PlatformConnectionStatus], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for status in statuses:
        if status.client_id != client_id:
            raise ValueError(
                f"platform status for {status.platform} belongs to client {status.client_id}, "
                f"not to the scanned client {client_id}"
            )
        if not (status.client_reports_using_it and not status.integration_connected):
            continue

        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.PLATFORM_INTEGRATION_GAP,
                entity_type=EntityType.PLATFORM,
                entity_id=status.platform,
                customer_id=status.client_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Client reports using {status.platform}, but no working ZBM integration "
                    f"exists for it — every Revenue Recovery agent is blind to this platform's "
                    f"orders, discounts, abandoned carts, and renewals until it's connected."
                ),
                recoverable_value=None,
                evidence_class=EvidenceClass.UNKNOWN,
                methodology_id=METHODOLOGY_ID,
                methodology=METHODOLOGY,
            )
        )

    return findings
