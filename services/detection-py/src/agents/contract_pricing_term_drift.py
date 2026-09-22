"""
Agent H: Contract & Pricing-Term Drift Agent.
Single job: for clients with custom contract terms (minimums, escalators,
overage rates), flag where actual billing drifted from the contracted
term in the client's favor at ZBM's expense — specifically, billed value
falling short of a contracted minimum. Billing ABOVE a minimum is not
drift (a floor being exceeded is fine); only a shortfall against the
contracted commitment is a leak.
"""

from __future__ import annotations

from zbm_schema import (
    CauseCertainty,
    DecisionConfidence,
    Finding,
    LabeledValue,
    LeakCategory,
    ValueClassification,
)
from zbm_schema.tier2 import ContractTerm, ContractTermType

AGENT_ID = "contract-pricing-term-drift-v1"


def detect(terms: list[ContractTerm]) -> list[Finding]:
    findings: list[Finding] = []

    for term in terms:
        if term.term_type != ContractTermType.MINIMUM_SPEND:
            # Escalator/overage drift detection follows the same shape but needs
            # its own directionality rules — deliberately not built tonight
            # rather than guessing at logic for a case with no fixture behind it.
            continue

        if term.drift_usd <= 0:
            continue  # billed at or above the contracted minimum — not a leak

        findings.append(
            Finding(
                finding_id=f"contract-{term.client_id}-{term.period_label}",
                agent_id=AGENT_ID,
                leak_category=LeakCategory.CONTRACT_PRICING_TERM_DRIFT,
                entity_type="contract_term",
                entity_id=term.term_id,
                customer_id=term.client_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Contracted minimum spend of ${term.contracted_value_usd:.2f} for "
                    f"{term.period_label} was not enforced — only ${term.actual_billed_value_usd:.2f} "
                    f"was actually billed, a ${term.drift_usd:.2f} shortfall against the client's "
                    f"own agreed commitment."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=term.drift_usd,
                    classification=ValueClassification.OBSERVED,
                    confidence=DecisionConfidence.VERY_HIGH,
                ),
            )
        )

    return findings
