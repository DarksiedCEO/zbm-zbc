"""
Agent H: Contract & Pricing-Term Drift Agent.
Single job: for clients with custom contract terms (minimums, escalators,
overage rates), flag where actual billing drifted from the contracted
term in the client's favor at ZBM's expense — specifically, billed value
falling short of a contracted minimum. Billing ABOVE a minimum is not
drift (a floor being exceeded is fine); only a shortfall against the
contracted commitment is a leak.

Revenue Recovery fix wave (Oct 6 2026):
  - E-3: the finding is per contract TERM and period. Its id used to be
    "contract-{client_id}-{period_label}", so two minimum-spend terms of one
    client in one period collided (probe P1) and one silently replaced the
    other downstream.
  - Tenant: a term belongs to exactly one client. A term whose client_id is
    not the scan's client is refused (422 for that item), never reported
    under the wrong tenant.
  - E-4 review: the figure (contracted minimum minus amount billed) is exact
    arithmetic on the contract and the invoice — OBSERVED, unchanged.
"""

from __future__ import annotations

from zbm_schema import (
    format_money,
    CauseCertainty,
    DecisionConfidence,
    EntityType,
    EvidenceClass,
    Finding,
    LabeledValue,
    LeakCategory,
    ValueClassification,
    new_finding,
)
from zbm_schema.tier2 import ContractTerm, ContractTermType

AGENT_ID = "contract-pricing-term-drift-v1"
METHODOLOGY_ID = "contract_min_shortfall"
METHODOLOGY = (
    "Contracted minimum spend for the period minus the amount actually billed for it, exact cent "
    "arithmetic on the contract term and the billing record."
)


def detect(terms: list[ContractTerm], *, client_id: str) -> list[Finding]:
    findings: list[Finding] = []

    for term in terms:
        if term.client_id != client_id:
            raise ValueError(
                f"contract term {term.term_id} belongs to client {term.client_id}, "
                f"not to the scanned client {client_id}"
            )
        if term.term_type != ContractTermType.MINIMUM_SPEND:
            # Escalator/overage drift detection follows the same shape but needs
            # its own directionality rules — deliberately not built tonight
            # rather than guessing at logic for a case with no fixture behind it.
            continue

        if term.drift_usd <= 0:
            continue  # billed at or above the contracted minimum — not a leak

        findings.append(
            new_finding(
                client_id=client_id,
                agent_id=AGENT_ID,
                leak_category=LeakCategory.CONTRACT_PRICING_TERM_DRIFT,
                entity_type=EntityType.CONTRACT_TERM,
                entity_id=term.term_id,
                period_label=term.period_label,
                customer_id=term.client_id,
                cause_certainty=CauseCertainty.NAMED,
                cause_description=(
                    f"Contracted minimum spend of ${format_money(term.contracted_value_usd)} for "
                    f"{term.period_label} was not enforced — only ${format_money(term.actual_billed_value_usd)} "
                    f"was actually billed, a ${format_money(term.drift_usd)} shortfall against the client's "
                    f"own agreed commitment."
                ),
                recoverable_value=LabeledValue(
                    amount_usd=term.drift_usd,
                    classification=ValueClassification.OBSERVED,
                    confidence=DecisionConfidence.VERY_HIGH,
                ),
                evidence_class=EvidenceClass.OBSERVED,
                methodology_id=METHODOLOGY_ID,
                methodology=METHODOLOGY,
            )
        )

    return findings
