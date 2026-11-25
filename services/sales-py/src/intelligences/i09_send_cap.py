"""Sending pace for the outreach domain: the warm-up schedule and the daily cap (ADR 0013 decision 10).

Decides: how many cold emails the outreach domain may send on a given UTC day. The step and the daily counts are kept
PER outreach domain (AEGIS S1-M2): a new domain starts the schedule from day 1. Day 1 is the first day that domain
sent anything; the schedule only moves forward one step on a day the ``warmup-reset`` job advances it, and holds
(does not advance) while yesterday's complaint rate was above 0.10% or its hard-bounce rate above 5% (integer
arithmetic). 0.10% is Google's sender guideline: "Keep spam rates reported in Postmaster Tools below 0.10% and avoid
ever reaching a spam rate of 0.30% or higher" (support.google.com/mail/answer/81126, read 2026-11-25). The earlier
0.3% hold let a domain keep climbing at the rate Gmail treats as the hard limit. The day's cap is min(schedule step, SALES_DAILY_SEND_CAP). Never: sends."""

from __future__ import annotations

NUMBER = 9
NAME = "send_pace"
DECIDES = "today's cold-email cap for the outreach domain"

COMPLAINT_PER_TEN_THOUSAND = 10  # 0.10% (Google sender guidelines)
BOUNCE_PER_HUNDRED = 5           # 5%


def cap(schedule: tuple, step: int, ceiling: int) -> int:
    return min(schedule[min(max(step, 0), len(schedule) - 1)], ceiling)


def may_advance(sent: int, complaints: int, hard_bounces: int) -> bool:
    if sent == 0:
        return False
    if complaints * 10000 > sent * COMPLAINT_PER_TEN_THOUSAND:
        return False
    if hard_bounces * 100 > sent * BOUNCE_PER_HUNDRED:
        return False
    return True
