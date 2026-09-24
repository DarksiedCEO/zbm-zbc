"""
Payout eligibility — a gate aggregator, NOT an intelligence, and NOT money.

Spec: "NOTHING is paid until Clip Review, Verification and Integrity, and
Compliance (38) all pass." This module answers one question — may this
clip be handed to Finance (31) for payment consideration? — and nothing
else. It carries no amounts, rates or currency; Creative never touches
money (Finance pays, from verified views, outside this service).

eligible = Clip Review outcome is "pass"
           AND Verification and Integrity attests the clip (verified views,
               bot screen, stolen-clip check, minimum days live)
           AND Compliance (38) allows it.
Each failing condition is a named blocker. Today both departments are
fail-closed stand-ins, so eligibility is ALWAYS false with both named.
All three gates are consulted every time (no short-circuit), so the
blocker list is complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.departments import Compliance38Port, GateResult, VerificationAttestation, VerificationIntegrityPort
from zbc.clip_review import ClipReviewDecision, ClipSubmission


@dataclass(frozen=True)
class PayoutEligibility:
    submission_id: str
    eligible: bool
    blockers: tuple[str, ...]
    clip_review_outcome: str
    verification: VerificationAttestation
    compliance: GateResult
    note: str = "Eligibility only: Creative sets no amounts; Finance (31) pays from verified views."

    def as_dict(self) -> dict:
        return {
            "submission_id": self.submission_id, "eligible": self.eligible, "blockers": list(self.blockers),
            "clip_review_outcome": self.clip_review_outcome,
            "verification": {"verified": self.verification.verified, "reason": self.verification.reason,
                             "attestation_id": self.verification.attestation_id},
            "compliance": {"allowed": self.compliance.allowed, "reason": self.compliance.reason,
                           "reference": self.compliance.reference},
            "note": self.note,
        }


def evaluate(sub: ClipSubmission, decision: ClipReviewDecision, verification: VerificationIntegrityPort,
             compliance: Compliance38Port) -> PayoutEligibility:
    blockers: list[str] = []
    if decision.outcome != "pass":
        blockers.append(f"clip_review: outcome is {decision.outcome!r}, not 'pass'")
    facts = {"campaign_id": sub.campaign_id, "rulebook_version": sub.rulebook_version, "post_ref": sub.post_ref,
             "clipper_id": sub.clipper_id, "posted_at": sub.posted_at.isoformat()}
    att = verification.attest_clip(sub.submission_id, facts)
    if not att.verified:
        blockers.append(f"verification_and_integrity: {att.reason}")
    gate = compliance.review("zbc_clip", sub.submission_id, {**facts, "clip_review": decision.outcome})
    if not gate.allowed:
        blockers.append(f"compliance_38: {gate.reason}")
    return PayoutEligibility(sub.submission_id, not blockers, tuple(blockers), decision.outcome, att, gate)
