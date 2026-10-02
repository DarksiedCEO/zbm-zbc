"""
Run and finding states (spec §B.2, §B.3) and the invariants checked on every transition. There is no state named
``parked``, ``deferred``, ``minor``, ``ruling`` or ``skipped`` (test G4 parses these enums).

Wave 23 (founder design change D1, Sep 30 2026 — honest claims): the ENGINE never asserts that a finding is fixed. A
finding leaves a run only as ``candidate_passed_checks`` (every check the engine runs passed: RED→GREEN, the
verification/reverted/single-file checkouts, the reproduction under the runner AND outside it, the suite on the
committed tree — necessary, not sufficient), ``disproved`` (a reproduction the engine ran) or
``needs_review_runner_dependent`` (every check passed under the runner but the TEST itself cannot run outside it:
committed, listed under the report's flags), or the run ends ``failed`` with the finding ``blocked``. ``accepted`` is
set by nothing but an AEGIS review (``POST /fix-runs/{id}/review``, caller ``aegis``) with an explicit per-finding
verdict (``DeliveryService.review``; ``review_transition_problem``) — the engine's ``finding_transition_problem``
refuses it; a review that reopens a finding moves it to ``reopened`` (the child run carries it on). A finding
carrying a reviewer-authored reproduction reaches none of the three engine end states without its admission RED
check's ``reproduction_red_checked`` event id (wave 22, G2).

Legacy names (records written before wave 23) are read through ``normalize_finding_state``: ``fixed`` →
``candidate_passed_checks``. ``reviewed`` (the pre-wave-23 review outcome, pass or reopen alike) is kept as a
read-only, terminal legacy state: nothing moves into it any more.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class RunStatus(str, Enum):
    received = "received"
    preparing = "preparing"
    running = "running"
    suite = "suite"
    reporting = "reporting"
    awaiting_review = "awaiting_review"
    reviewed_pass = "reviewed_pass"
    reviewed_fail = "reviewed_fail"
    failed = "failed"


RUN_TRANSITIONS: dict[str, set[str]] = {
    "received": {"preparing", "failed"},
    "preparing": {"running", "failed"},
    "running": {"suite", "reporting", "failed"},
    "suite": {"running", "reporting", "failed"},
    "reporting": {"awaiting_review", "failed"},
    "awaiting_review": {"reviewed_pass", "reviewed_fail", "failed"},
    "reviewed_pass": set(),
    "reviewed_fail": set(),
    "failed": set(),
}
TERMINAL = {"reviewed_pass", "reviewed_fail", "failed"}
LIVE = {"running", "suite"}


class FindingState(str, Enum):
    queued = "queued"
    red = "red"
    green = "green"
    swept = "swept"
    candidate_passed_checks = "candidate_passed_checks"
    disproved = "disproved"
    blocked = "blocked"
    # wave 22 (G1(b)) / wave 23 (D3): every check passed under the test runner, but the finding's reproduction TEST
    # cannot be executed outside it (it imports pytest, takes fixtures, is parametrized, ...): committed on the
    # branch, listed under the report's flags; only a review with a per-finding note accepts it
    needs_review_runner_dependent = "needs_review_runner_dependent"
    # wave 23 (D1): set ONLY by an AEGIS review with an explicit per-finding verdict
    accepted = "accepted"
    reopened = "reopened"
    # legacy (pre-wave-23 review outcome); read-only, terminal, nothing moves into it
    reviewed = "reviewed"


# records written before wave 23 name the engine's end state "fixed"; every read maps it (D1 migration alias)
LEGACY_FINDING_STATES = {"fixed": "candidate_passed_checks"}
# the engine's end states for a finding of a run that reaches awaiting_review
ENGINE_DONE_STATES = ("candidate_passed_checks", "disproved", "needs_review_runner_dependent")
# the states only a review sets
REVIEW_STATES = ("accepted", "reopened")


def normalize_finding_state(state):
    """The wave-23 name of a finding state read from a record (``fixed`` → ``candidate_passed_checks``)."""
    return LEGACY_FINDING_STATES.get(state, state)


FINDING_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"red", "disproved", "blocked"},
    "red": {"red", "green", "disproved", "blocked"},
    "green": {"swept", "red", "blocked"},
    "swept": {"candidate_passed_checks", "needs_review_runner_dependent", "red", "blocked"},
    # the engine's end states move only through a review (``review_transition_problem``): accepted or reopened
    "candidate_passed_checks": {"accepted", "reopened"},
    "disproved": {"accepted", "reopened"},
    "needs_review_runner_dependent": {"accepted", "reopened"},
    "accepted": set(),
    "reopened": set(),
    "reviewed": set(),
    "blocked": {"queued"},
}


def run_transition_problem(frm: str, to: str) -> Optional[str]:
    if to not in RUN_TRANSITIONS.get(frm, set()):
        return f"run cannot move from {frm} to {to}"
    return None


def review_transition_problem(rec: dict, to: str) -> Optional[str]:
    """Wave 23 (D1): the only route to ``accepted`` / ``reopened`` — an AEGIS review's per-finding verdict. ``rec`` is
    the finding record before the review."""
    frm = normalize_finding_state(rec.get("state"))
    if to not in REVIEW_STATES:
        return f"a review moves a finding only to {' or '.join(REVIEW_STATES)}"
    if to not in FINDING_TRANSITIONS.get(frm, set()):
        return f"finding cannot move from {frm} to {to}"
    return None


def finding_transition_problem(rec: dict, to: str, *, red_test_name: Optional[str] = None) -> Optional[str]:
    """§B.3 invariants for the ENGINE's transitions. ``rec`` is the finding record before the transition. The engine
    never reaches ``accepted`` or ``reopened`` (D1: only a review does)."""
    frm = normalize_finding_state(rec.get("state"))
    if to in REVIEW_STATES:
        return f"{to} is set only by an AEGIS review with a per-finding verdict (D1), never by the engine"
    if to not in FINDING_TRANSITIONS.get(frm, set()):
        return f"finding cannot move from {frm} to {to}"
    if to == "green":
        red = rec.get("red")
        if not red or red.get("exit") == 0 or red.get("verdict", "fail") != "fail":
            return "green requires a prior red with exit != 0 and a verified failing verdict"
        if red_test_name is not None and red.get("test_name") != red_test_name:
            return "green requires the same test_name as the red"
        green = rec.get("green") or {}
        if green.get("verdict", "pass") != "pass":
            return "green requires a verified passing verdict (R2: unknown is never green)"
    if to in ENGINE_DONE_STATES:
        rt = rec.get("reviewer_test")
        if rt and not rt.get("red_checked_event_id"):
            # wave 22 (G2, N21-D-2): a reviewer-authored reproduction must have been run RED at admission, recorded
            return f"{to} requires the reviewer-authored reproduction's admission RED check (reproduction_red_checked)"
    if to in ("candidate_passed_checks", "needs_review_runner_dependent"):
        if not rec.get("green") or rec["green"].get("exit") != 0 or rec["green"].get("verdict", "pass") != "pass":
            return f"{to} requires green"
        rc = rec.get("revert_check") or {}
        if not rc or rc.get("exit") == 0 or rc.get("verdict", "fail") != "fail":
            return f"{to} requires a revert check that failed with the fix reverted"
        v = rec.get("verification") or {}
        if not v:
            return f"{to} requires a verification record (R1; N19-E-6)"
        if v.get("verification_checkout", {}).get("verdict") != "pass" or v.get("reverted_checkout", {}).get("verdict") != "fail":
            return f"{to} requires the verification checkout to pass and the reverted checkout to fail (R1)"
        if not rec.get("finding_file_hunk"):
            return f"{to} requires the finding's file to carry a hunk of the fix (R2)"
        sf = rec.get("single_file_revert") or {}
        if sf.get("verdict") != "fail" or sf.get("file") != rec.get("file"):
            return f"{to} requires the single-file revert of the finding's file to fail the RED test (R2)"
        rp = rec.get("repro_check")
        if not rp or rp.get("verification", {}).get("verdict") != "pass" or rp.get("reverted", {}).get("verdict") != "fail":
            # wave 21 (R1): a reproduction record is REQUIRED — there is no end state without the finding's own test
            return f"{to} requires the finding's reproduction to pass with the fix and fail without it (R2; wave 21: always)"
        so = rec.get("src_only_check")
        if so is not None and so.get("verdict") != "pass":
            return f"{to} requires the claimed baseline failures to pass on base + the source changes alone"
        if not rec.get("sweep"):
            return f"{to} requires a sweep record"
        if not rec.get("suite_tree_sha256") or rec.get("suite_tree_sha256") != rec.get("commit_tree_sha256"):
            return f"{to} requires the suite to have run on the committed tree (tree digests must match; N19-E-6)"
        if rec.get("suite_failures"):
            return f"{to} requires a green suite (failures remain)"
        if rec.get("outcome_regressions"):
            return f"{to} requires no outcome regression against the baseline (R3)"
        sc = rec.get("standalone_check") or {}
        want = "confirmed" if to == "candidate_passed_checks" else "runner_dependent"
        if sc.get("outcome") != want or sc.get("target") != rp.get("target"):
            # wave 22 (G1(b), N21-D-1): the reproduction re-run outside the test runner — pass with the fix, fail
            # reverted — is what separates candidate_passed_checks from runner-dependent; nothing else reaches either
            return (f"{to} requires the finding's reproduction re-run outside the test runner with outcome {want} "
                    "(G1: runner-independent re-execution)")
    if to == "disproved":
        d = rec.get("disproof")
        if not d or not d.get("reproduction_argv") or d.get("output_sha256") is None or not d.get("statement_sha256"):
            return "disproved requires a reproduction the engine ran and a written statement"
        if d.get("verdict", "pass") != "pass":
            return "disproved requires the finding's reproduction to pass on the base tree (R3)"
    if to == "blocked" and not rec.get("reasons"):
        return "blocked requires a reason"
    return None
