"""
Correlation utility — Decision 3 / Failure Mode #2 enforcement.

Given the combined Finding output of multiple agents, detects when more
than one agent has claimed the SAME entity_id (order/subscription) before
any dollar value is summed or presented. This is a detection/flagging
utility, not a resolver — deciding how to value an overlapping order
(sum both? take the larger? something else) is a policy decision for the
orchestration/valuation layer, deliberately not made here.
"""

from __future__ import annotations

from collections import defaultdict

from zbm_schema import Finding


def find_overlapping_entities(findings: list[Finding]) -> dict[str, list[Finding]]:
    """
    Returns {entity_id: [finding, finding, ...]} for every entity_id
    claimed by MORE than one agent. Entities claimed by exactly one
    agent are not included — this function's whole job is surfacing
    the overlap case, not restating the non-overlapping majority.
    """
    by_entity: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_entity[f.entity_id].append(f)

    return {
        entity_id: entity_findings
        for entity_id, entity_findings in by_entity.items()
        if len(entity_findings) > 1
    }
