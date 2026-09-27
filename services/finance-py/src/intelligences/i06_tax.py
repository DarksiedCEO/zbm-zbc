"""
Intelligence 6 — Tax (Finance spec §B.7, §C.6; FIN-11, FIN-12, FIN-24). Pure judgment on the tax agent's answer and
the records Finance keeps. Finance never stores a TIN, never stores a form image and never files anything itself.

Gate 3: W-9 on file with tin_match = matched; or W-8BEN/W-8BEN-E on file, w8_current and the outside-US attestation.
Anything else blocks under policy ``block``; ``withhold_24`` (unmatched W-9 paid net of 24%) exists only while
counsel row FIN-CQ-06 is verified (config refuses start otherwise).
Business days for the B-notice timers: Monday-Friday (US federal holidays are NOT modelled — ADR 0009 choice).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import money as M
import reasons as R

NUMBER, NAME, ACTOR = 6, "Tax", "intel_06_tax"
LA = ZoneInfo("America/Los_Angeles")
BACKUP_PCT = 24


def tax_year(at: datetime) -> int:
    return at.astimezone(LA).year


def add_business_days(d: date, n: int) -> date:
    x = d
    while n > 0:
        x += timedelta(days=1)
        if x.weekday() < 5:
            n -= 1
    return x


def business_days_between(a: date, b: date) -> int:
    n, x = 0, a
    while x < b:
        x += timedelta(days=1)
        if x.weekday() < 5:
            n += 1
    return n


def gate(ans, policy: str) -> tuple[list[dict], bool]:
    """(reasons, withhold_for_unmatched). ``ans`` is the tax agent's answer (never a caller's)."""
    if not ans.available:
        return [R.item("TAX_NOT_READY", "tax agent unavailable (stand-in): no W-9/W-8 status",
                       obligation_id="US-IRS-TIN")], False
    if not ans.form_on_file or ans.form_kind not in ("w9", "w8ben", "w8bene"):
        return [R.item("TAX_NOT_READY", "no W-9 or W-8 on file", obligation_id="US-IRS-TIN")], False
    if ans.form_kind == "w9":
        if ans.tin_match == "matched":
            return [], False
        if ans.tin_match in ("mismatched", "pending") and policy == "withhold_24":
            return [], True
        return [R.item("TAX_NOT_READY", f"W-9 TIN match is {ans.tin_match}", obligation_id="US-IRS-TIN")], False
    out = []
    if ans.w8_current is not True:
        out.append(R.item("TAX_NOT_READY", "W-8 not current per the tax agent", obligation_id="US-IRS-W8-VALID"))
    if ans.services_outside_us_attested is not True:
        out.append(R.item("TAX_NOT_READY", "no outside-US services attestation (US-performed work is blocked)",
                          obligation_id="US-IRS-FOREIGN"))
    return out, False


def obligations_for(ans) -> tuple:
    if ans.available and ans.form_kind in ("w8ben", "w8bene"):
        return ("US-IRS-FOREIGN", "US-IRS-W8-VALID")
    return ("US-IRS-TIN", "US-IRS-BWH")


def withholding(net_before: str) -> str:
    return M.fmt(M.pct(M.D(net_before), BACKUP_PCT))


def b_notice_timers(received_on: date) -> dict:
    return {"cp2100_received_on": received_on.isoformat(),
            "first_b_notice_due": add_business_days(received_on, 15).isoformat(),
            "withholding_start_by": add_business_days(received_on, 30).isoformat()}


def overdue_timers(records: list[dict], today: date) -> list[str]:
    """Payees whose B-notice timer is past due without the notice sent / withholding started."""
    out = []
    for r in records:
        b = r.get("b_notice") or {}
        if not b:
            continue
        if not b.get("first_b_notice_sent_on") and date.fromisoformat(b["first_b_notice_due"]) < today:
            out.append(r["payee_id"])
        elif not (r.get("backup_withholding") or {}).get("flag") and date.fromisoformat(b["withholding_start_by"]) < today:
            out.append(r["payee_id"])
    return sorted(set(out))
