"""
Intelligence 7 — Dispute Desk (spec §C.7, CN-18).

Decides admissibility, routing and SLA — never the outcome (a human: Andre,
or a delegate only when People (43) confirms one). One appeal per flagged
clip and one per org-wide ban; the filing window runs from the notice
(CN-18 ``appeal_window_days``); the SLA counts business days (Mon-Fri; no
holiday calendar in this build — spec choice) and pushes Andre on
``sla_push_business_day``. Disputes about a V&I finding or hold are decided
at V&I; CN reads the outcome.
"""

from __future__ import annotations

from datetime import date, timedelta

NUMBER, NAME, ACTOR = 7, "Dispute Desk", "intel_07_disputes"
ROUTE = {"clip_flag": "verification_integrity", "vi_finding": "verification_integrity", "strike": "verification_integrity",
         "ban": "clipper_network", "suspension": "clipper_network", "tier": "clipper_network",
         "enrolment": "clipper_network", "admission": "clipper_network"}
ONE_PER_SUBJECT = ("clip_flag", "ban", "strike", "vi_finding", "suspension", "tier", "enrolment", "admission")
NOTICE_TEMPLATES = {"clip_flag": ("clip_flagged", "strike_notice"), "strike": ("strike_notice",),
                    "vi_finding": ("clip_flagged", "strike_notice"), "ban": ("ban_notice",),
                    "suspension": ("suspension_notice",), "tier": ("admission_decision",),
                    "enrolment": ("admission_decision",), "admission": ("admission_decision",)}


def add_business_days(d: date, n: int) -> date:
    cur = d
    left = n
    while left > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


def business_days_between(a: date, b: date) -> int:
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n
