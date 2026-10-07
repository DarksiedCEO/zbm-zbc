"""
Correlation utility — Decision 3 / Failure Mode #2 enforcement.

Given the combined Finding output of multiple agents, detects when more
than one agent has claimed the SAME entity before any dollar value is
summed or presented. This is a detection/flagging utility, not a resolver —
deciding how to value an overlapping order (sum both? take the larger?
something else) is a policy decision for the orchestration/valuation layer,
deliberately not made here.

Revenue Recovery fix wave (Oct 6 2026):
  - E-3: the entity is (client_id, entity_type, entity_id), not entity_id
    alone. Two clients' "ord_1", or an order and a subscription that happen
    to share an id, are different entities and never overlap.
  - E-10: an overlap needs MORE THAN ONE DISTINCT AGENT. The same agent
    reporting one entity twice (duplicate input rows, probe P8) is not two
    agents double-counting it.

The result is keyed by Finding.correlation_key ("client|entity_type|entity_id";
"|" is in none of the three charsets). The function is a pure partition by
key, so running it over any partition of the findings that keeps each key's
findings together gives exactly the same answer — which is how orchestrator-go
batches it past the 1,000-findings request cap (E-6).
"""

from __future__ import annotations

from collections import defaultdict

from zbm_schema import Finding


def find_overlapping_entities(findings: list[Finding]) -> dict[str, list[Finding]]:
    """
    Returns {correlation_key: [finding, ...]} for every entity claimed by
    more than one distinct agent. Entities claimed by exactly one agent are
    not included — this function's whole job is surfacing the overlap case,
    not restating the non-overlapping majority.
    """
    by_entity: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_entity[f.correlation_key].append(f)

    return {
        key: entity_findings
        for key, entity_findings in by_entity.items()
        if len({f.agent_id for f in entity_findings}) > 1
    }
