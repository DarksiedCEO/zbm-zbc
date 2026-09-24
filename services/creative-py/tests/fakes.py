"""
TEST-ONLY passing fakes for departments that don't exist yet.

They live here, not in src/, so no production code path can be wired to a
department that says "fine". src/shared/departments.py has only the
fail-closed stand-ins.
"""

from shared.departments import CommissionReceipt, GateResult, VerificationAttestation


class PassingCompliance:
    def review(self, subject_kind, subject_id, facts):
        return GateResult("compliance_38", True, "test fake: allowed", reference=f"c38-{subject_id}")


class PassingVerification:
    def __init__(self, verified_views: int = 125000):
        self.verified_views = verified_views

    def attest_clip(self, submission_id, facts):
        return VerificationAttestation(submission_id, True, "test fake: verified", f"vi-{submission_id}",
                                       {"verified_views": self.verified_views})

    def attest_result(self, result_id, facts):
        return VerificationAttestation(result_id, True, "test fake: verified", f"vi-{result_id}",
                                       {"verified_views": self.verified_views})


class PassingLegal:
    def signoff(self, topic, subject_id, facts):
        return GateResult("legal_37", True, "test fake: signed off", reference=f"l37-{subject_id}")


class AcceptingCreativeAgents:
    def commission(self, request):
        return CommissionReceipt(request.request_id, request.agent, True, "test fake: accepted", f"ext-{request.request_id}")
