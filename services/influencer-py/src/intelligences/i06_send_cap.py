"""Sending pace for the outreach domain (ADR 0015 decision 10).

Decides: whether one more outreach email may go out today (UTC day) from the outreach domain: at most
``INF_DAILY_SEND_CAP`` (default 50, at most 200) per domain per day. Influencer outreach is low volume, so there is no
warm-up schedule (sales-py's is for cold B2B volume); a new outreach domain starts its own count. Messages over the cap
stay queued for the next day. Never: sends."""

from __future__ import annotations

NUMBER = 6
NAME = "send_pace"
DECIDES = "whether one more outreach email may go out today"


def may_send(sent_today: int, cap: int) -> bool:
    return sent_today < cap
