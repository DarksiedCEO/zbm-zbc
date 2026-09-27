"""
Intelligence 4 — Payout Run (Finance spec §B.5, §C.4; FIN-07..FIN-10, FIN-15). Maker and release worker. Pure
helpers here: the item identity, the rail idempotency key, the content hash Andre approves, velocity/exposure limits.
The service runs every gate (all of them, every time — no short-circuit) and never approves; release may only
narrow a batch (exclude an item), never add one or raise an amount.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

import money as M

NUMBER, NAME, ACTOR = 4, "Payout Run", "intel_04_payout_run"
LIVE_ITEM = ("proposed", "approved", "submitting", "submitted", "paid", "netted")    # a payable here is spoken for
DEAD_ITEM = ("failed", "returned", "cancelled", "excluded_at_release")               # its payables are accrued again
RETRY_WINDOW_H = 23
GATES = {1: "certification", 2: "compliance", 3: "tax", 4: "ofac", 5: "rail", 6: "payee_hold", 7: "minimum",
         8: "limits", 9: "reconciliation", 10: "treasury", 11: "controls", 12: "rules"}
RUN_BLOCKING = (9, 10, 11)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def idempotency_key(batch_id: str, payee_id: str, period: str) -> str:
    """FIN-09 / §B.5: SHA-256(zbc|payout|{batch_id}|{payee_id}|{period})."""
    return hashlib.sha256(f"zbc|payout|{batch_id}|{payee_id}|{period}".encode("utf-8")).hexdigest()


def iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def content_sha256(items: list[dict], totals: dict, gate_inputs_sha256: str) -> str:
    core = [{k: it[k] for k in ("item_id", "payee_id", "payable_ids", "gross", "netted", "withheld", "net",
                                "idempotency_key")} for it in items]
    return sha({"items": core, "totals": totals, "gate_inputs_sha256": gate_inputs_sha256})


def totals(items: list[dict]) -> dict:
    return {"gross": M.fmt(M.total(i["gross"] for i in items)), "withheld": M.fmt(M.total(i["withheld"] for i in items)),
            "netted": M.fmt(M.total(i["netted"] for i in items)), "net": M.fmt(M.total(i["net"] for i in items)),
            "count": len(items)}


def limit_breaches(net: Decimal, first_payout: bool, rolling_30d: Decimal, cfg) -> list[str]:
    out = []
    if net > cfg.limit_payee_run:
        out.append(f"net {M.fmt(net)} above the per-payee per-run limit {M.fmt(cfg.limit_payee_run)}")
    if M.q(rolling_30d + net) > cfg.limit_payee_30d:
        out.append(f"rolling 30-day total would be {M.fmt(M.q(rolling_30d + net))} (limit {M.fmt(cfg.limit_payee_30d)})")
    if first_payout and net > cfg.limit_first_payout:
        out.append(f"first payout {M.fmt(net)} above the first-payout limit {M.fmt(cfg.limit_first_payout)}")
    return out
