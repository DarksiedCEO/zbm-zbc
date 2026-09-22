"""
Agent G: Platform Integration Agent (Triple Whale-style coverage check).
Single job: flag platforms a client reports using but that have no
working ZBM data integration — a visibility gap, since every other
detection agent is blind on a platform with no connection. No dollar
value is claimed (this is a coverage gap, not a transaction), which is
why Finding.recoverable_value is legitimately None here.
"""

from __future__ import annotations

from zbm_schema import CauseCertainty, Finding, LeakCategory
from zbm_schema.tier2 import PlatformConnectionStatus

AGENT_ID = "platform-integration-v1"


def detect(statuses: list[PlatformConnectionStatus]) -> list[Finding]:
    findings: list[Finding] = []

    for status in statuses:
        if not (status.client_reports_using_it and not status.integration_connected):
            continue

        findings.append(
            Finding(
                finding_id=f"platform-{status.client_id}-{status.platform}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.PLATFORM_INTEGRATION_GAP,
                entity_type="platform",
                entity_id=f"{status.client_id}:{status.platform}",
                customer_id=status.client_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Client reports using {status.platform}, but no working ZBM integration "
                    f"exists for it — every Revenue Recovery agent is blind to this platform's "
                    f"orders, discounts, abandoned carts, and renewals until it's connected."
                ),
                recoverable_value=None,
            )
        )

    return findings
