"""
Intelligence 5 — Control Monitor (spec A.4).

Each control green/red from pushed or computed results and its SLA
(controls.control_status), the trust-center view, and the deterministic
tests of the Compliance-owned controls. Never runs security tests
(Cybersecurity 22 owns them); never marks a control green without a
passing result.

Compliance-owned tests with NO evidence mechanism in this build (C-12
monthly counter attestations, C-15 owner action log, C-17 review log)
return ``fail`` whenever there is anything to evidence — fail closed; the
gap is listed in ADR 0006.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from clock import parse_iso
from register import effective_status, is_effective

NUMBER, NAME, ACTOR = 5, "Control Monitor", "intel_05_control_monitor"


def test_c01(rows: list[dict], open_reverify_targets: set[str], today: date) -> tuple[bool, str]:
    expired = [r["id"] for r in rows if r["status"] != "superseded" and effective_status(r, today) == "expired"]
    if expired:
        return False, f"{len(expired)} in-force row(s) expired"
    soon = [r["id"] for r in rows if r["status"] == "verified" and r.get("expires_at")
            and date.fromisoformat(r["expires_at"]) - timedelta(days=7) <= today]
    lacking = [i for i in soon if i not in open_reverify_targets]
    if lacking:
        return False, f"{len(lacking)} row(s) expire within 7 days without an open re-verification proposal"
    return True, "no expired rows; every row expiring within 7 days has an open re-verification proposal"


def test_c02(enabled: bool, last_cycle: dict | None, now: datetime) -> tuple[bool, str]:
    if not enabled:
        return False, "Change Watcher disabled (COMPLIANCE_WATCHER_ENABLED != 1)"
    if not last_cycle:
        return False, "Change Watcher has not run"
    if last_cycle.get("failed"):
        return False, f"{len(last_cycle['failed'])} source(s) failed in the last cycle"
    return True, "every enabled source polled successfully in the last cycle"


def test_c05(payout_rulings: list[dict], now: datetime) -> tuple[bool, str]:
    since = now - timedelta(days=7)
    lacking = [r["ruling_id"] for r in payout_rulings if r["allowed"] and parse_iso(r["evaluated_at"]) >= since
               and not r.get("disclosure_evidence_ref")]
    if lacking:
        return False, f"{len(lacking)} payout-allowed ruling(s) without disclosure evidence ids"
    return True, "every payout-allowed ruling in the trailing 7 days carries disclosure evidence ids"


def test_c15(rows: list[dict], today: date) -> tuple[bool, str]:
    upcoming = [r["id"] for r in rows if r["status"] != "superseded" and r.get("effective_date")
                and today < date.fromisoformat(r["effective_date"]) <= today + timedelta(days=60)]
    if upcoming:
        return False, f"{len(upcoming)} row(s) take effect within 60 days; no owner action log exists in this build"
    return True, "no row takes effect within the next 60 days"


def test_c12() -> tuple[bool, str]:
    return False, "no per-state consumer counter attestation has been received (no intake exists in this build)"


def test_c17() -> tuple[bool, str]:
    return False, "no organisation-level applicability review is logged (no review log exists in this build)"


__all__ = ["test_c01", "test_c02", "test_c05", "test_c12", "test_c15", "test_c17", "is_effective"]
