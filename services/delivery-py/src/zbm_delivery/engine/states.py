"""
Run and finding states (spec §B.2, §B.3) and the invariants checked on every transition. There is no state named
``parked``, ``deferred``, ``minor``, ``ruling`` or ``skipped`` (test G4 parses these enums): a finding leaves a run
only as ``fixed`` (RED→GREEN recorded by the engine, and — wave 22, G1 — its reproduction confirmed outside the test
runner), ``disproved`` (a reproduction the engine ran) or ``needs_review_runner_dependent`` (every check of ``fixed``
passed under the runner but the reproduction cannot run outside it: committed, NOT fixed, listed for the reviewer),
or the run ends ``failed`` with the finding ``blocked``. A finding carrying a reviewer-authored reproduction reaches
none of the three without its admission RED check's ``reproduction_red_checked`` event id (wave 22, G2).
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
    reviewed = "reviewed"
    fixed = "fixed"
    disproved = "disproved"
    blocked = "blocked"
    # wave 22 (G1(b)): every check a fix needs passed under the test runner, but the finding's reproduction could not
    # be confirmed OUTSIDE it (the test needs pytest, or a conftest the standalone run cannot provide): committed on
    # the branch, NOT fixed — a human reviews it (review pass → reviewed; review fail → reopened)
    needs_review_runner_dependent = "needs_review_runner_dependent"


FINDING_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"red", "disproved", "blocked"},
    "red": {"red", "green", "disproved", "blocked"},
    "green": {"swept", "red", "blocked"},
    "swept": {"fixed", "needs_review_runner_dependent", "red", "blocked"},
    "fixed": {"reviewed", "queued"},          # ``queued`` only through a review that reopens it (§C.8.7)
    "disproved": {"reviewed", "queued"},
    "needs_review_runner_dependent": {"reviewed", "queued"},
    "reviewed": set(),
    "blocked": {"queued"},
}


def run_transition_problem(frm: str, to: str) -> Optional[str]:
    if to not in RUN_TRANSITIONS.get(frm, set()):
        return f"run cannot move from {frm} to {to}"
    return None


def finding_transition_problem(rec: dict, to: str, *, red_test_name: Optional[str] = None) -> Optional[str]:
    """§B.3 invariants. ``rec`` is the finding record before the transition."""
    frm = rec.get("state")
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
    if to in ("fixed", "disproved", "needs_review_runner_dependent"):
        rt = rec.get("reviewer_test")
        if rt and not rt.get("red_checked_event_id"):
            # wave 22 (G2, N21-D-2): a reviewer-authored reproduction must have been run RED at admission, recorded
            return f"{to} requires the reviewer-authored reproduction's admission RED check (reproduction_red_checked)"
    if to in ("fixed", "needs_review_runner_dependent"):
        if not rec.get("green") or rec["green"].get("exit") != 0 or rec["green"].get("verdict", "pass") != "pass":
            return "fixed requires green"
        rc = rec.get("revert_check") or {}
        if not rc or rc.get("exit") == 0 or rc.get("verdict", "fail") != "fail":
            return "fixed requires a revert check that failed with the fix reverted"
        v = rec.get("verification") or {}
        if not v:
            return "fixed requires a verification record (R1; N19-E-6)"
        if v.get("verification_checkout", {}).get("verdict") != "pass" or v.get("reverted_checkout", {}).get("verdict") != "fail":
            return "fixed requires the verification checkout to pass and the reverted checkout to fail (R1)"
        if not rec.get("finding_file_hunk"):
            return "fixed requires the finding's file to carry a hunk of the fix (R2)"
        sf = rec.get("single_file_revert") or {}
        if sf.get("verdict") != "fail" or sf.get("file") != rec.get("file"):
            return "fixed requires the single-file revert of the finding's file to fail the RED test (R2)"
        rp = rec.get("repro_check")
        if not rp or rp.get("verification", {}).get("verdict") != "pass" or rp.get("reverted", {}).get("verdict") != "fail":
            # wave 21 (R1): a reproduction record is REQUIRED — there is no fixed without the finding's own test
            return "fixed requires the finding's reproduction to pass with the fix and fail without it (R2; wave 21: always)"
        so = rec.get("src_only_check")
        if so is not None and so.get("verdict") != "pass":
            return "fixed requires the claimed baseline failures to pass on base + the source changes alone"
        if not rec.get("sweep"):
            return "fixed requires a sweep record"
        if not rec.get("suite_tree_sha256") or rec.get("suite_tree_sha256") != rec.get("commit_tree_sha256"):
            return "fixed requires the suite to have run on the committed tree (tree digests must match; N19-E-6)"
        if rec.get("suite_failures"):
            return "fixed requires a green suite (failures remain)"
        if rec.get("outcome_regressions"):
            return "fixed requires no outcome regression against the baseline (R3)"
        sc = rec.get("standalone_check") or {}
        want = "confirmed" if to == "fixed" else "runner_dependent"
        if sc.get("outcome") != want or sc.get("target") != rp.get("target"):
            # wave 22 (G1(b), N21-D-1): the reproduction re-run outside the test runner — pass with the fix, fail
            # reverted — is what separates fixed from runner-dependent; nothing else reaches either state
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
