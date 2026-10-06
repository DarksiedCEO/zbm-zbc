"""
Intelligence I3 — the account health score (ADR 0014 decision 18). Deterministic, one job: score one account from
its signals, integers only, and say why. Start at 100, subtract the documented points below, clamp to 0..100.
A signal that cannot be read (port not wired, no data) costs a small fixed amount and is listed as ``unknown`` —
never treated as fine. Each score lists every contributing signal: code, points, source.

| Signal | Source | Points |
|---|---|---|
| results trend | Revenue Recovery / detection metrics port | down -25, flat -10, up 0, unknown -5 |
| payment status | Finance (31) port | failed -30, late -15, current 0, unknown -5 |
| portal login recency | `hub` events posted here | none ever or >30 days -15, 15..30 days -8, <=14 days 0 |
| complaints, last 30 days | this service (tickets triaged complaint) | -10 each, at most -30 |
| open escalations | this service | -5 each, at most -15 |
| latest NPS answer | this service | 0..6 (detractor) -20, 7..8 (passive) -5, 9..10 0, none 0 |
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

WEIGHTS = {
    "results": {"down": -25, "flat": -10, "up": 0, "unknown": -5},
    "payment": {"failed": -30, "late": -15, "current": 0, "unknown": -5},
    "login": {"stale": -15, "aging": -8, "recent": 0},
    "complaint_each": -10, "complaint_cap": -30,
    "escalation_each": -5, "escalation_cap": -15,
    "nps": {"detractor": -20, "passive": -5, "promoter": 0},
}


def nps_band(score: int) -> str:
    return "detractor" if score <= 6 else ("passive" if score <= 8 else "promoter")


def compute(now: datetime, trend: Optional[str], payment: Optional[str], last_login: Optional[datetime],
            complaints_30d: int, open_escalations: int, latest_nps: Optional[int]) -> dict:
    sig = []

    def add(code: str, points: int, source: str):
        sig.append({"signal": code, "points": points, "source": source})

    t = trend if trend in ("up", "flat", "down") else "unknown"
    add(f"results_{t}", WEIGHTS["results"][t], "results_port")
    p = payment if payment in ("current", "late", "failed") else "unknown"
    add(f"payment_{p}", WEIGHTS["payment"][p], "finance_port")
    if last_login is None:
        add("login_never", WEIGHTS["login"]["stale"], "hub_events")
    else:
        days = (now - last_login).days
        band = "recent" if days <= 14 else ("aging" if days <= 30 else "stale")
        add(f"login_{band}", WEIGHTS["login"][band], "hub_events")
    if complaints_30d:
        add("complaints_30d", max(WEIGHTS["complaint_cap"], WEIGHTS["complaint_each"] * complaints_30d), "tickets")
    if open_escalations:
        add("open_escalations", max(WEIGHTS["escalation_cap"], WEIGHTS["escalation_each"] * open_escalations),
            "tickets")
    if latest_nps is not None:
        band = nps_band(latest_nps)
        add(f"nps_{band}", WEIGHTS["nps"][band], "nps")
    score = max(0, min(100, 100 + sum(s["points"] for s in sig)))
    return {"score": score, "signals": sig}
