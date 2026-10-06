"""Sending pace for the outreach domain: the warm-up schedule and the daily cap (ADR 0013 decision 10).

Decides: how many cold emails the outreach domain may send on a given UTC day. The step and the daily counts are kept
PER outreach domain (AEGIS S1-M2): a new domain starts the schedule from day 1. Day 1 is the first day that domain
sent anything; the schedule only moves forward one step on a day the ``warmup-reset`` job advances it, and holds
(does not advance) while yesterday's complaint rate was above 0.3% or its hard-bounce rate above 5% (integer
arithmetic). The day's cap is min(schedule step, SALES_DAILY_SEND_CAP). Never: sends."""

from __future__ import annotations

NUMBER = 9
NAME = "send_pace"
DECIDES = "today's cold-email cap for the outreach domain"

COMPLAINT_PER_THOUSAND = 3       # 0.3%
BOUNCE_PER_HUNDRED = 5           # 5%


def cap(schedule: tuple, step: int, ceiling: int) -> int:
    return min(schedule[min(max(step, 0), len(schedule) - 1)], ceiling)


def may_advance(sent: int, complaints: int, hard_bounces: int) -> bool:
    if sent == 0:
        return False
    if complaints * 1000 > sent * COMPLAINT_PER_THOUSAND:
        return False
    if hard_bounces * 100 > sent * BOUNCE_PER_HUNDRED:
        return False
    return True
