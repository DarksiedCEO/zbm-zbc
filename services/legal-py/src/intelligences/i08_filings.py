"""
Intelligence 8 — Filings Calendar (Legal spec §B.9, §C.8). Decides windows, alerts and lapse status; never files
or pays anything (Andre does, then records it).

| kind | calendar |
| dmca_agent_designation | expires_on = filed_on + 3 years (LG-11 parameter); alert LEGAL_FILING_ALERT_DAYS (60) |
| tm_application | filed_on; office-action deadlines entered by Andre (window_closes) |
| tm_statement_of_use | deadline from the notice of allowance, entered (window_closes) |
| tm_section_8, tm_section_15 | window = registration + 5y .. + 6y ("at years five to six", LR) |
| tm_section_9 | window = registration + 9y .. + 10y ("at year ten", LR; the exact opening is UNVERIFIED) |
| sos_statement_of_information | biennial six-month window ending with the formation month (January -> Aug 1 .. Jan 31) |
| fbn_statement, insurance_policy_notice | dates entered by Andre (rules UNVERIFIED) |
A deadline passed without ``filed`` -> ``lapsed`` (Andre alerted, a matter opened). Fees are cited only where
verified; the rest say UNVERIFIED. Fees are references, not money fields (spec §E: no other money on the wire).
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Optional

NUMBER, NAME, ACTOR = 8, "Filings Calendar", "intel_08_filings"
KINDS = ("dmca_agent_designation", "tm_application", "tm_statement_of_use", "tm_section_8", "tm_section_9",
         "tm_section_15", "sos_statement_of_information", "fbn_statement", "insurance_policy_notice")
TRADEMARK_KINDS = ("tm_application", "tm_statement_of_use", "tm_section_8", "tm_section_9", "tm_section_15")
FEES = {
    "dmca_agent_designation": "USD 6 per designation, amendment or resubmission (https://www.copyright.gov/dmca-directory/faq.html)",
    "tm_application": "USD 350 per class; surcharges USD 100/200/200 (https://www.uspto.gov/trademarks/fees-payment-information/summary-2025-trademark-fee-changes)",
    "tm_statement_of_use": "USD 150 per class (USPTO 2025 fee summary)",
    "tm_section_8": "USD 325 per class (USPTO 2025 fee summary)",
    "tm_section_9": "USD 325 per class (USPTO 2025 fee summary)",
    "tm_section_15": "USD 250 per class (USPTO 2025 fee summary)",
}


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:              # 29 February -> 28 February
        return d.replace(year=d.year + n, day=28)


def sos_window(formation_month: int, due_year: int) -> tuple[date, date]:
    """Six-month window ending with the formation month of ``due_year`` (SOS: January -> Aug 1 .. Jan 31)."""
    end = date(due_year, formation_month, calendar.monthrange(due_year, formation_month)[1])
    m, y = formation_month - 5, due_year
    if m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, 1), end


def compute(kind: str, f: dict, dmca_years: int) -> dict:
    """Dates for a filing from the entered fields. Raises ValueError when a required field is missing."""
    def d(k) -> Optional[date]:
        return date.fromisoformat(f[k]) if f.get(k) else None
    opens, closes, expires = d("window_opens"), d("window_closes"), d("expires_on")
    if kind == "dmca_agent_designation":
        if f.get("filed_on"):
            expires = add_years(d("filed_on"), dmca_years)
    elif kind in ("tm_section_8", "tm_section_15", "tm_section_9"):
        reg = d("registration_date")
        if reg is None:
            raise ValueError(f"{kind} needs registration_date")
        a, b = (9, 10) if kind == "tm_section_9" else (5, 6)
        opens, closes = add_years(reg, a), add_years(reg, b)
    elif kind == "sos_statement_of_information":
        if not f.get("formation_month") or not f.get("due_year"):
            raise ValueError("sos_statement_of_information needs formation_month and due_year")
        opens, closes = sos_window(int(f["formation_month"]), int(f["due_year"]))
    elif kind in ("tm_statement_of_use", "fbn_statement", "insurance_policy_notice") and closes is None and \
            expires is None:
        raise ValueError(f"{kind} needs window_closes or expires_on (entered by Andre)")
    return {"window_opens": opens.isoformat() if opens else None,
            "window_closes": closes.isoformat() if closes else None,
            "expires_on": expires.isoformat() if expires else None,
            "fee_reference": FEES.get(kind, "UNVERIFIED")}


def deadline(fl: dict) -> Optional[date]:
    if fl["status"] == "filed":
        return date.fromisoformat(fl["expires_on"]) if fl.get("expires_on") else None
    v = fl.get("window_closes") or fl.get("expires_on")
    return date.fromisoformat(v) if v else None


def evaluate(fl: dict, today: date) -> list[str]:
    """Events due today for one filing (each at most once, tracked by the caller): window_open, due_soon, lapsed."""
    out = []
    if fl["status"] == "lapsed":
        return out
    dl = deadline(fl)
    if fl["status"] in ("not_started", "ready") and fl.get("window_opens") and not fl.get("announced_open") and \
            date.fromisoformat(fl["window_opens"]) <= today:
        out.append("window_open")
    if dl is None:
        return out
    lapsed = today >= dl if fl["status"] == "filed" else today > dl
    if lapsed:
        out.append("lapsed")
    elif not fl.get("announced_due") and today >= dl - timedelta(days=fl["alert_lead_days"]):
        out.append("due_soon")
    return out
