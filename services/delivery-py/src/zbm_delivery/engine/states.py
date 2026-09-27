"""
Run and finding states (spec §B.2, §B.3) and the invariants checked on every transition. There is no state named
``parked``, ``deferred``, ``minor``, ``ruling`` or ``skipped`` (test G4 parses these enums): a finding leaves a run
only as ``fixed`` (RED→GREEN recorded by the engine) or ``disproved`` (a reproduction the engine ran), or the run
ends ``failed`` with the finding ``blocked``.
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


FINDING_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"red", "disproved", "blocked"},
    "red": {"red", "green", "disproved", "blocked"},
    "green": {"swept", "red", "blocked"},
    "swept": {"fixed", "red", "blocked"},
    "fixed": {"reviewed", "queued"},          # ``queued`` only through a review that reopens it (§C.8.7)
    "disproved": {"reviewed", "queued"},
    "reviewed": set(),
    "blocked": {"queued"},
}


def run_transition_problem(frm: str, to: str) -> Optional[str]:
    if to not in RUN_TRANSITIONS.get(frm, set()):
        return f"run cannot move from {frm} to {to}"
    return None


def finding_transition_problem(rec: dict, to: str, *, red_test_name: Optional[str] = None,
                               suite_after_commit: bool = False) -> Optional[str]:
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
    if to == "fixed":
        if not rec.get("green") or rec["green"].get("exit") != 0 or rec["green"].get("verdict", "pass") != "pass":
            return "fixed requires green"
        rc = rec.get("revert_check") or {}
        if not rc or rc.get("exit") == 0 or rc.get("verdict", "fail") != "fail":
            return "fixed requires a revert check that failed with the fix reverted"
        v = rec.get("verification") or {}
        if v and (v.get("verification_checkout", {}).get("verdict") != "pass" or v.get("reverted_checkout", {}).get("verdict") != "fail"):
            return "fixed requires the verification checkout to pass and the reverted checkout to fail (R1)"
        if not rec.get("sweep"):
            return "fixed requires a sweep record"
        if not suite_after_commit:
            return "fixed requires a suite run after the commit"
        if rec.get("suite_failures"):
            return "fixed requires a green suite (failures remain)"
    if to == "disproved":
        d = rec.get("disproof")
        if not d or not d.get("reproduction_argv") or d.get("output_sha256") is None or not d.get("statement_sha256"):
            return "disproved requires a reproduction the engine ran and a written statement"
        if d.get("verdict", "pass") != "pass":
            return "disproved requires the finding's reproduction to pass on the base tree (R3)"
    if to == "blocked" and not rec.get("reasons"):
        return "blocked requires a reason"
    return None
