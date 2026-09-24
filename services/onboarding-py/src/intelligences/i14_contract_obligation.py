"""
Intelligence 14 — Contract and Obligation.  PHASE 1 (activation gate).

Decides: whether a planned action or promise stays inside what the client
signed, and flags drift. It is one of the two ACTIVATION GATES (with 15):
nothing goes live until both pass.

Rules (client and brand lanes; terms come from contract storage — whose
location is undecided, so the default stand-in has no terms and the gate
FAILS CLOSED):
- No terms available => unmet ``contract_terms_available``.
- Not signed => unmet ``contract_signed``.
- Today outside [start_date, end_date] => unmet ``contract_in_term``.
- A merged-plan item whose service is not in the contract => DRIFT, and
  unmet ``plan_within_contract`` (work not paid for / not agreed).
- An open commitment whose category the contract does not allow => DRIFT,
  unmet ``commitments_within_contract``.
- P23: the contract must contain the CCPA/CPRA customer-data clause =>
  unmet ``p23_ccpa_cpra_clause_in_contract`` otherwise. (Counsel approval
  of the clause wording is a Compliance (15) requirement.)

Creator lane: there are no stored clipper contract terms yet; the gate
checks that the creator agreement was signed at application.

``check_action`` answers the same question for one planned action (a
service, a promise category, a spend amount) at any time.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from onboarding_schema import ContractTerms, GateResult

from ._status import PHASE1_STATUS

NUMBER = 14
NAME = "Contract and Obligation"
PHASE = 1
STATUS = PHASE1_STATUS
GATE = "contract_14"


def check_action(
    terms: Optional[ContractTerms], today: date, service: Optional[str] = None,
    commitment_category: Optional[str] = None, spend_usd: Optional[Decimal] = None,
) -> tuple[bool, list[str]]:
    if terms is None:
        return False, ["no contract terms available (contract storage location undecided): cannot confirm; ask Andre"]
    reasons = []
    if not terms.signed:
        reasons.append("contract not signed")
    if today < terms.start_date or (terms.end_date is not None and today > terms.end_date):
        reasons.append("outside the contract term")
    if service is not None and service not in terms.services:
        reasons.append(f"service '{service}' is not in the signed contract")
    if commitment_category is not None and commitment_category not in terms.allowed_commitment_categories:
        reasons.append(f"commitment category '{commitment_category}' is not something the contract covers")
    if spend_usd is not None:
        if terms.monthly_spend_cap_usd is None:
            reasons.append("contract sets no spend cap; spend needs Andre's explicit decision")
        elif spend_usd > terms.monthly_spend_cap_usd:
            reasons.append("spend is above the contract's monthly cap")
    return (not reasons), reasons


def client_gate(
    terms: Optional[ContractTerms], today: date, plan_services: list[str], open_commitment_categories: list[str]
) -> GateResult:
    if terms is None:
        return GateResult(gate=GATE, passed=False, unmet=[
            "contract_terms_available: no contract terms on file (contract storage location undecided; stand-in holds nothing)"
        ])
    unmet, drift = [], []
    if not terms.signed:
        unmet.append("contract_signed: contract is not signed")
    if today < terms.start_date or (terms.end_date is not None and today > terms.end_date):
        unmet.append("contract_in_term: today is outside the contract term")
    off_contract = sorted({s for s in plan_services if s not in terms.services})
    if off_contract:
        drift += [f"plan includes '{s}', which is not in the signed contract" for s in off_contract]
        unmet.append(f"plan_within_contract: plan drifts outside the contract ({', '.join(off_contract)})")
    bad_commitments = sorted({c for c in open_commitment_categories if c not in terms.allowed_commitment_categories})
    if bad_commitments:
        drift += [f"open commitment category '{c}' is not covered by the contract" for c in bad_commitments]
        unmet.append(f"commitments_within_contract: {', '.join(bad_commitments)}")
    if not terms.ccpa_cpra_clause_present:
        unmet.append("p23_ccpa_cpra_clause_in_contract: CCPA/CPRA customer-data clause missing from the contract")
    return GateResult(gate=GATE, passed=not unmet, unmet=unmet, drift=drift)


def creator_gate(agreement_signed: bool) -> GateResult:
    unmet = [] if agreement_signed else ["creator_agreement_signed: clipper agreement not signed"]
    return GateResult(gate=GATE, passed=not unmet, unmet=unmet)
