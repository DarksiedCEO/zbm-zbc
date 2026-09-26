"""
Intelligence 9 — Strike Ledger (spec §C.9): strike class from upheld findings; ban recommendation; ban
propagation after Clipper Network reports Andre's decision. It never imposes a ban.

| upheld finding                                                     | class | automatic effect (V&I)             |
|--------------------------------------------------------------------|-------|------------------------------------|
| deleted before min live; caption changed                           | S1    | clip not certified                 |
| hash mismatch; stolen content upheld                               | S2    | —                                  |
| bought engagement; platform stripped (auto-upheld); account shared / duplicate identity upheld | S3 | hold every open certification of the clipper; ban_recommended |
| minor                                                              | none  | removal (age)                      |

Escalation: 3 active S1 within 90 days → S2; 2 active S2 within 180 days → S3. Expiry: S1 90 d, S2 180 d,
S3 never. A ban is never automatic (spec choice): it takes effect only through ``POST /vi/v1/bans`` with
Clipper Network's decision AND Andre's approval token.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

NUMBER, NAME, ACTOR = 9, "Strike Ledger", "intel_09_strike_ledger"
EXPIRY_DAYS = {"S1": 90, "S2": 180, "S3": None}
AUTO_UPHELD = ("integrity_fail", "platform_stripped")      # objective platform evidence (ADR 0007 choice 16)


def strike_class(finding: dict) -> Optional[str]:
    kind, code = finding["kind"], finding.get("code")
    if kind == "minor":
        return None
    if kind == "integrity_fail":
        return "S2" if code == "HASH_MISMATCH" else "S1"
    if kind == "stolen_content":
        return "S2"
    if kind in ("bought_engagement", "platform_stripped", "account_shared", "duplicate_identity"):
        return "S3"
    return None


def expires_at(cls: str, issued: datetime) -> Optional[datetime]:
    days = EXPIRY_DAYS[cls]
    return None if days is None else issued + timedelta(days=days)


def is_active(strike: dict, now: datetime, parse) -> bool:
    if strike["status"] != "active":
        return False
    return strike["expires_at"] is None or parse(strike["expires_at"]) > now


def escalation(strikes: list[dict], now: datetime, parse) -> Optional[tuple[str, list[str]]]:
    """(new class, strike ids it escalates) when the active non-escalated strikes cross a threshold."""
    active = [s for s in strikes if is_active(s, now, parse) and not s.get("escalated_into")]
    s1 = [s for s in active if s["class"] == "S1" and parse(s["issued_at"]) > now - timedelta(days=90)]
    if len(s1) >= 3:
        return "S2", [s["strike_id"] for s in sorted(s1, key=lambda s: s["issued_at"])[:3]]
    s2 = [s for s in active if s["class"] == "S2" and parse(s["issued_at"]) > now - timedelta(days=180)]
    if len(s2) >= 2:
        return "S3", [s["strike_id"] for s in sorted(s2, key=lambda s: s["issued_at"])[:2]]
    return None
