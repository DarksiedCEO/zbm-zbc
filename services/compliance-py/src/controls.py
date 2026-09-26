"""
Control catalog (spec A.4) and status arithmetic (intelligence 5).

Status is GREEN only when the last result is ``pass`` and
``now - last_passed_at <= sla_hours`` AND every feeding obligation is in
force in the register version in force; otherwise RED (never run = red,
feeding obligation unverified/expired = red ``obligation_not_in_force``).
The catalog changes only through an Andre-approved proposal of kind
``control``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from clock import parse_iso
from register import effective_status

OWNER_CALLERS = ("compliance", "andre", "site_owner", "people_43", "vendor_33", "finance_31", "cybersecurity_22",
                 "legal_37", "creative_production", "onboarding", "verification_integrity")


class ControlDef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    control_id: str = Field(pattern=r"^C-[0-9]{2,3}$")
    title: str = Field(min_length=1, max_length=200)
    owner_department: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    owner_intelligence: Optional[str] = Field(default=None, pattern=r"^i[0-9]{2}_[a-z_]{1,40}$")
    test: str = Field(min_length=1, max_length=600)
    evidence: str = Field(min_length=1, max_length=600)
    sla_hours: int = Field(ge=1, le=24 * 400)
    blocks_gates: list[str] = Field(max_length=3)
    obligation_ids: list[str] = Field(min_length=1, max_length=30)

    @field_validator("blocks_gates")
    @classmethod
    def _gates(cls, v):
        for g in v:
            if g not in ("activation", "payout", "publish"):
                raise ValueError("blocks_gates entries must be activation, payout or publish")
        return v

    @field_validator("obligation_ids")
    @classmethod
    def _ids(cls, v):
        for s in v:
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,39}\*?", s):
                raise ValueError("obligation ids (a trailing * is a prefix wildcard)")
        return v


def _c(cid, title, owner, intel, test, evidence, sla, blocks, obligations) -> dict:
    return ControlDef(control_id=cid, title=title, owner_department=owner, owner_intelligence=intel, test=test,
                      evidence=evidence, sla_hours=sla, blocks_gates=list(blocks), obligation_ids=list(obligations)).model_dump()


CQ_ALL = [f"CQ-{i:02d}" for i in range(1, 15)]

SEED_CONTROLS: list[dict] = [
    _c("C-01", "Register freshness", "compliance", "i01_register",
       "No in-force row expired; every row expiring within 7 days has an open re-verification proposal",
       "Computed by Compliance from the register version in force and the inbox", 24, (), ("PRG-ECCP", "HR-04")),
    _c("C-02", "Change Watcher", "compliance", "i06_change_watcher",
       "Every enabled source polled successfully in the last cycle", "Watcher cycle record", 26, (), ("PRG-ECCP",)),
    _c("C-03", "Weekly triage", "andre", None, "No inbox proposal older than 7 days undecided",
       "Andre's weekly triage result", 168, (), ("HR-04",)),
    _c("C-04", "Sanctions list current", "compliance", "i08_sanctions",
       "Provider list version refreshed <= 24 h", "Provider list version and refresh time", 24,
       ("activation", "payout"), ("US-OFAC-02",)),
    _c("C-05", "Disclosure monitoring log", "compliance", "i09_disclosure",
       "Every payout-allowed ruling in the trailing 7 days carries disclosure evidence ids",
       "Computed from stored payout rulings", 24, (), ("US-FTC-FAQ-01", "US-FTC-255-01")),
    _c("C-06", "Accessibility re-scan on deploy", "site_owner", None,
       "Every deployed agency-run site/portal content hash has a passed check", "Deploy list with content hashes", 72, (),
       ("HR-09",)),
    _c("C-07", "Consent/GPC on agency-run sites", "site_owner", None,
       "Banner, opt-out and GPC honoring verified", "Site consent test results", 72, (), ("HR-10", "US-PRIV-GPC")),
    _c("C-08", "Policies acknowledged", "people_43", None,
       "Every operator with access acknowledged the current policy set", "Acknowledgment records (policies from legal_37)",
       168, (), ("HR-01",)),
    _c("C-09", "People on/off-boarding", "people_43", None,
       "Access granted only after acknowledgment; revoked <= 24 h after offboarding",
       "On/off-boarding records incl. Cybersecurity (22) revocation records", 24, (), ("HR-01",)),
    _c("C-10", "Vendor risk", "vendor_33", None,
       "Every vendor touching ZBM/ZBC data has a DPA on file and a security review <= 365 days old",
       "Vendor inventory with DPA and review dates", 168, (), ("HR-01",)),
    _c("C-11", "Evidence integrity", "compliance", "i11_evidence_audit",
       "Ledger /ledger/verify passes and the local register log chain verifies", "Verification results", 24,
       ("activation", "payout", "publish"), ("HR-03",)),
    _c("C-12", "Privacy thresholds", "compliance", "i05_control_monitor",
       "Per-state consumer counters below every threshold (monthly attestation from data owners)",
       "Monthly counter attestations", 720, (), ("US-PRIV-*", "US-CPPA-2025", "CQ-09")),
    _c("C-13", "Tax trackers", "finance_31", None,
       "1099-NEC/1099-K YTD trackers current; B-notice timers (15/30 business days) met", "Finance tracker export",
       168, (), ("US-IRS-1099NEC", "US-IRS-1099K", "US-IRS-BWH")),
    _c("C-14", "Security controls from Cybersecurity (22)", "cybersecurity_22", None,
       "Cybersecurity (22) test suite results ingested and passing", "Test suite results", 72, (), ("HR-01",)),
    _c("C-15", "Effective-date reminders", "compliance", "i05_control_monitor",
       "Every row with an effective date in the next 60 days has an owner action logged", "Owner action log", 168, (),
       ("US-FCC-TCPA-02", "US-STATE-AI-ACTS", "US-PRIV-PENDING")),
    _c("C-16", "Counsel queue", "andre", None, "Every counsel-only row has an open request to counsel logged by Andre",
       "Counsel request log", 720, (), CQ_ALL),
    _c("C-17", "Organisation-level applicability", "compliance", "i05_control_monitor",
       "GDPR/UK Art. 27, CA AI transparency, Safeguards-type reviews logged", "Review log", 720, (),
       ("EU-GDPR-27", "CQ-05", "US-CA-AI-TRANSPARENCY")),
]

INTERNAL_CONTROLS = ("C-01", "C-02", "C-04", "C-05", "C-11", "C-12", "C-15", "C-17")


def expand_obligations(ids: list[str], rows_by_id: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for i in ids:
        if i.endswith("*"):
            out.extend(sorted(r for r in rows_by_id if r.startswith(i[:-1])))
        else:
            out.append(i)
    return out


def control_status(defn: dict, state: dict, rows_by_id: Optional[dict[str, dict]], now: datetime) -> dict:
    """``state``: {last_result, last_passed_at, last_tested_at, ...}. Returns
    {status, reason, not_in_force}."""
    if rows_by_id is None:
        return {"status": "red", "reason": "register_not_in_force", "not_in_force": []}
    today = now.date()
    feeding = expand_obligations(defn["obligation_ids"], rows_by_id)
    missing_or_bad = []
    for oid in feeding:
        row = rows_by_id.get(oid)
        if row is None or effective_status(row, today) != "verified":
            missing_or_bad.append(oid)
    if missing_or_bad:
        return {"status": "red", "reason": "obligation_not_in_force", "not_in_force": missing_or_bad}
    if not state or state.get("last_result") is None:
        return {"status": "red", "reason": "never_run", "not_in_force": []}
    if state.get("last_result") != "pass" or not state.get("last_passed_at"):
        return {"status": "red", "reason": "last_result_fail", "not_in_force": []}
    if now - parse_iso(state["last_passed_at"]) > timedelta(hours=defn["sla_hours"]):
        return {"status": "red", "reason": "sla_expired", "not_in_force": []}
    return {"status": "green", "reason": "passing_within_sla", "not_in_force": []}


def public_view(defn: dict, status: dict, state: dict) -> dict[str, Any]:
    """Trust center: exactly {control_id, title, status, last_passed_at}."""
    return {"control_id": defn["control_id"], "title": defn["title"], "status": status["status"],
            "last_passed_at": (state or {}).get("last_passed_at")}
