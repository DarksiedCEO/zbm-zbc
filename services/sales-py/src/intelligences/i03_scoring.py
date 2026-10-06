"""Lead qualification: a deterministic fit + intent score with named rules (ADR 0013 decision 8).

Decides: fit (0..50), intent (0..50), the total, a grade and the qualification status, and lists every rule that
fired. Never: uses a model, a person's protected traits, or anything not in the lead's own signals and evidence.

Fit (cap 50)
  F1  company domain (not a free-mail provider)                         +10
  F2  employees  1-10 +3 · 11-50 +8 · 51-200 +12 · 201-1000 +15 · 1000+ +15
  F3  revenue    under_1m +2 · 1m_10m +8 · 10m_50m +12 · 50m+ +15
  F4  industry in the routed brand's ideal-customer list                +10
  F5  monthly ad spend  under_5k +2 · 5k_25k +5 · 25k_100k +8 · 100k+ +10
Intent (cap 50)
  I1  source  inbound +15 · referral +15 · partner +10 · paid_provider +5 · public_data +0
  I2  a Revenue Recovery scan with at least one finding                 +15
  I3  asked for a call                                                  +15
  I4  timeline  now +10 · 30_days +8 · 90_days +4
  I5  stated a budget                                                   +5
  I6  each further piece of evidence on the same lead (repeat inquiry)   +5, at most +10
Grade: A >= 60, B >= 40, C >= 20, else D. A and B are ``qualified``, C ``nurture``, D ``new``.
"""

from __future__ import annotations

NUMBER = 3
NAME = "lead_scoring"
DECIDES = "fit + intent score, grade and qualification status"

EMPLOYEES = {"1-10": 3, "11-50": 8, "51-200": 12, "201-1000": 15, "1000+": 15}
REVENUE = {"under_1m": 2, "1m_10m": 8, "10m_50m": 12, "50m+": 15}
AD_SPEND = {"none": 0, "under_5k": 2, "5k_25k": 5, "25k_100k": 8, "100k+": 10}
SOURCE = {"inbound": 15, "referral": 15, "partner": 10, "paid_provider": 5, "public_data": 0}
TIMELINE = {"now": 10, "30_days": 8, "90_days": 4, "later": 0}
ICP = {"zbm": frozenset({"ecommerce", "retail", "dtc_brand", "restaurant", "local_services", "b2b_services"}),
       "zbc": frozenset({"entertainment", "music", "creator", "sports", "dtc_brand", "ecommerce"})}
INDUSTRIES = ("ecommerce", "retail", "dtc_brand", "restaurant", "local_services", "b2b_services", "entertainment",
              "music", "creator", "sports", "other")


def score(source: str, brand: str, signals: dict, has_company_domain: bool, evidence: list[dict]) -> dict:
    fired: list[str] = []
    fit = 0
    if has_company_domain:
        fit += 10
        fired.append("F1")
    for rule, table, key in (("F2", EMPLOYEES, "employees_band"), ("F3", REVENUE, "revenue_band"),
                             ("F5", AD_SPEND, "monthly_ad_spend_band")):
        pts = table.get(signals.get(key) or "", 0)
        if pts:
            fit += pts
            fired.append(rule)
    if signals.get("industry") in ICP.get(brand, ()):
        fit += 10
        fired.append("F4")
    intent = SOURCE.get(source, 0)
    if intent:
        fired.append("I1")
    if any(e.get("kind") == "rr_scan" and (e.get("scan_findings") or 0) >= 1 for e in evidence):
        intent += 15
        fired.append("I2")
    if signals.get("requested_call") is True:
        intent += 15
        fired.append("I3")
    t = TIMELINE.get(signals.get("timeline") or "", 0)
    if t:
        intent += t
        fired.append("I4")
    if signals.get("budget_stated") is True:
        intent += 5
        fired.append("I5")
    extra = min(10, 5 * max(0, len(evidence) - 1))
    if extra:
        intent += extra
        fired.append("I6")
    fit, intent = min(fit, 50), min(intent, 50)
    total = fit + intent
    grade = "A" if total >= 60 else "B" if total >= 40 else "C" if total >= 20 else "D"
    status = {"A": "qualified", "B": "qualified", "C": "nurture", "D": "new"}[grade]
    return {"fit": fit, "intent": intent, "total": total, "grade": grade, "status": status, "rules": fired}
