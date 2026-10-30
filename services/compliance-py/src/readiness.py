"""
Readiness report (ADR 0024): which rules are expired and which gates they affect, what blocks every gate, and the
rule set a plain US web client's activation runs through (SEO audit or Revenue Recovery on a Shopify or web store),
with the dates its rules expire. Pure functions of the register version in force, the control states and the date:
nothing here is recorded, nothing is decided (the gates decide).

A rule that is expired keeps blocking every gate it feeds when it applies (engine.evaluate): nothing here extends a
verification. The report only says which ones a US web client would meet, so Andre re-verifies those first.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from clock import iso
from intelligences.engine import Ctx, base_match
from register import effective_status, is_effective

# A plain US web client at activation: lane client, platform web, a US state, no activation flag set (the client
# lane's fixed flags as i02_activation_gate fixes them).
US_WEB_CLIENT_FLAGS = {"political_content": False, "child_directed": False, "health_data_shared": False,
                       "biz_opp_client": False, "audience_data_sale": False, "marketplace": False,
                       "pooled_client_funds": False, "claims_present": False, "health_or_earnings_claim": False,
                       "entity_payee": False, "foreign_payee": False}
EXPIRING_WINDOW_DAYS = 60


def _applies_to_us_web_client(row: dict, today: date) -> bool:
    if "activation" not in row["gates"] or row["status"] == "superseded" or not is_effective(row, today):
        return False
    ctx = Ctx(gate="activation", subject_id="readiness", lane="client", platforms={"web"},
              jurisdictions={"US-CA", "US"}, flags=dict(US_WEB_CLIENT_FLAGS))
    aw = row.get("applies_when") or {}
    if not base_match(aw, ctx):
        return False
    if aw.get("flags_any") and not any(US_WEB_CLIENT_FLAGS.get(f) for f in aw["flags_any"]):
        return False
    if aw.get("flags_none") and any(US_WEB_CLIENT_FLAGS.get(f) for f in aw["flags_none"]):
        return False
    return True


def compute(rows: Optional[list[dict]], version: Optional[int], seed_pinned: bool, now: datetime,
            control_blockers: list[str]) -> dict:
    """``control_blockers``: ``control_red:<id>: <reason>`` for each red control that blocks client activation."""
    today = now.astimezone(timezone.utc).date()
    out = {"as_of": iso(now), "register_version": version, "seed_pinned": seed_pinned,
           "gate_blockers": [], "expired": [], "expiring_within_60_days": [],
           "us_web_client_activation": {"rows": [], "blocking_now": [], "expiring": []}}
    if rows is None:
        out["gate_blockers"] = ["register_not_in_force"]
        return out
    out["gate_blockers"] = list(control_blockers)
    web = [r for r in rows if _applies_to_us_web_client(r, today)]
    web_ids = {r["id"] for r in web}
    for r in sorted(rows, key=lambda x: x["id"]):
        if r["status"] == "superseded":
            continue
        st = effective_status(r, today)
        if st == "expired":
            out["expired"].append({"id": r["id"], "title": r["title"], "expires_at": r["expires_at"],
                                   "gates": list(r["gates"]), "applies_when": r.get("applies_when") or {},
                                   "blocks_us_web_client_activation": r["id"] in web_ids})
        elif st == "verified" and r.get("expires_at") and \
                date.fromisoformat(r["expires_at"]) <= today + timedelta(days=EXPIRING_WINDOW_DAYS):
            out["expiring_within_60_days"].append(r["id"])
    w = out["us_web_client_activation"]
    w["rows"] = sorted(web_ids)
    w["blocking_now"] = sorted(r["id"] for r in web if effective_status(r, today) != "verified")
    w["expiring"] = sorted(({"id": r["id"], "expires_at": r["expires_at"], "source_kind": r["source_kind"],
                             "source_url": r.get("source_url")}
                            for r in web if r.get("expires_at") and effective_status(r, today) == "verified"),
                           key=lambda x: (x["expires_at"], x["id"]))
    return out


def summary_lines(r: dict) -> list[str]:
    """What the service prints at start-up and after each scheduled run (stdout; no personal data)."""
    lines = [f"compliance readiness {r['as_of']}: register "
             + (f"v{r['register_version']} in force (seed pinned: {'yes' if r['seed_pinned'] else 'NO'})"
                if r["register_version"] is not None else "not in force (Andre has not approved the seed)")]
    lines.append("gates closed by: " + "; ".join(r["gate_blockers"]) if r["gate_blockers"]
                 else "gates: no register-wide or control blocker")
    lines.append(f"expired: {len(r['expired'])} rule(s)")
    for e in r["expired"]:
        tail = " -- blocks a US web client's activation" if e["blocks_us_web_client_activation"] else ""
        lines.append(f"  - {e['id']} (expired {e['expires_at']}; gates {', '.join(e['gates'])}){tail}")
    w = r["us_web_client_activation"]
    lines.append("US web client activation: " + ("BLOCKED by " + ", ".join(w["blocking_now"]) if w["blocking_now"]
                                                 else "not blocked by any rule"))
    for e in w["expiring"]:
        lines.append(f"  - {e['id']} expires {e['expires_at']} (re-verify and have Andre approve before then)")
    return lines


__all__ = ["compute", "summary_lines", "US_WEB_CLIENT_FLAGS"]
