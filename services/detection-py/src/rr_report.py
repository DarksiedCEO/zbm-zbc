"""Revenue Recovery findings report for a client, from one orchestrator-go scan result (ADR 0020 section 8).

Input: the JSON body of ``POST /revenue-recovery/scan`` for a connected store (``data_source`` ``shopify:<snapshot>``,
with ``store_coverage``). Output: a structured report (dict) and its Markdown rendering.

The existing Revenue Recovery rules apply (ADR 0001 "Evidence class and methodology", AEGIS M1/M2/N4):
  - every dollar figure carries its evidence class; OBSERVED figures are read off the store's own records with exact
    arithmetic and are the only ones marked quotable; ESTIMATED figures read "est." and say what they assume;
    UNKNOWN findings carry no dollar figure and the text gives none;
  - there is no headline score and no grand total: figures of different evidence classes are never added together,
    and findings on one order from two agents are flagged, not summed (ADR 0001 decision 3);
  - an agent that had no data from this source is listed as NOT COVERED with the reason — its silence is never shown
    as a clean result.
Projections are ESTIMATED by construction (an observed window extended to a year, with the assumption stated); they
are not findings and are not on the ledger.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

AGENT_TITLES = {
    "discount-misuse-v1": "Stacked discount codes",
    "affiliate-coupon-extension-v1": "Affiliate commission on coupon-driven orders",
    "abandoned-cart-coverage-v1": "Abandoned carts without a recovery flow",
    "renewal-never-triggered-v1": "Subscription renewals never triggered",
    "server-side-attribution-v1": "Orders invisible to server-side attribution",
    "cross-channel-attribution-v1": "Cross-channel attribution gaps",
    "platform-integration-v1": "Platform integration gaps",
    "contract-pricing-term-drift-v1": "Contract pricing drift",
}
MIN_PROJECTION_WINDOW_DAYS = 14
CENT = Decimal("0.01")


class ReportInputError(ValueError):
    pass


def _money(s: str) -> Decimal:
    d = Decimal(s)
    if d.as_tuple().exponent < -2:
        raise ReportInputError("money with more than two decimals")
    return d


def _fmt(d: Decimal) -> str:
    return f"${d.quantize(CENT, ROUND_HALF_UP):,.2f}"


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


_MD_SPECIAL = set("\\`*_[]<>|~&")


def _md(text: str) -> str:
    """Text from the store (a discount code inside a finding's cause) as literal Markdown: no link, image, HTML,
    emphasis or table cell can be smuggled into the client's report."""
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in str(text)).replace("\n", " ").replace("\r", " ")


def _window_days(window: dict, taken: datetime | None) -> tuple[int | None, str | None]:
    """(days, basis) of the window the observed figures cover (AEGIS golive-rr M2).

    Not truncated: every order in the requested lookback was read, so the window is the lookback itself
    (``requested_start`` to the snapshot time, else ``lookback_days``) however few orders it held: measuring from the
    oldest order would shrink the window and inflate a yearly projection. Truncated: older orders were not read, so
    the figures cover only back to the oldest order read."""
    if not taken:
        return None, None
    if window.get("truncated_by"):
        oldest = _dt(window.get("oldest_order_read"))
        if not oldest:
            return None, None
        return max(1, round((taken - oldest).total_seconds() / 86400)), "oldest_order_read"
    start = _dt(window.get("requested_start"))
    if start:
        return max(1, round((taken - start).total_seconds() / 86400)), "requested_window"
    lb = window.get("lookback_days")
    if isinstance(lb, int) and not isinstance(lb, bool) and lb >= 1:
        return lb, "requested_window"
    return None, None


def build_report(scan: dict) -> dict:
    src = scan.get("data_source") or ""
    cov = scan.get("store_coverage")
    if not src.startswith("shopify:") or not isinstance(cov, dict) or scan.get("non_live_data_source") is not False:
        raise ReportInputError("not a scan of a connected store (fixture scans are never reported to a client)")
    client = scan["client_id"]
    findings = scan.get("findings") or []
    overlap_entities = {k.split("|", 2)[2] for k in (scan.get("overlapping_claims") or {})}
    rows, by_agent = [], defaultdict(list)
    for f in findings:
        if f.get("client_id") != client:
            raise ReportInputError("a finding belongs to another client")
        rv = f.get("recoverable_value")
        ev = f["evidence_class"]
        if (rv is None) != (ev == "UNKNOWN"):
            raise ReportInputError("evidence class and figure disagree")
        amount = _money(rv["amount_usd"]) if rv else None
        row = {"finding_id": f["finding_id"], "agent_id": f["agent_id"], "entity_id": f["entity_id"],
               "evidence_class": ev, "amount_usd": str(amount) if amount is not None else None,
               "classification": rv["classification"] if rv else None,
               "confidence": rv["confidence"] if rv else None, "cause": f["cause_description"],
               "methodology": f["methodology"], "quotable": ev == "OBSERVED" and amount is not None,
               "also_claimed_by_another_agent": f["entity_id"] in overlap_entities}
        rows.append(row)
        by_agent[f["agent_id"]].append(row)

    window = cov.get("window") or {}
    taken = _dt(cov.get("taken_at"))
    window_days, window_basis = _window_days(window, taken)

    agents = []
    projections = []
    order = list(AGENT_TITLES)
    listed = sorted((cov.get("agents") or {}).items(),
                    key=lambda kv: (not kv[1]["covered"], order.index(kv[0]) if kv[0] in order else len(order), kv[0]))
    for agent_id, c in listed:
        a_rows = by_agent.get(agent_id, [])
        observed = [Decimal(r["amount_usd"]) for r in a_rows if r["evidence_class"] == "OBSERVED"]
        estimated = [Decimal(r["amount_usd"]) for r in a_rows if r["evidence_class"] in ("ESTIMATED", "MODELED")]
        entry = {"agent_id": agent_id, "title": AGENT_TITLES.get(agent_id, agent_id), "covered": bool(c["covered"]),
                 "why": c["why"], "findings": len(a_rows),
                 "observed_usd_in_window": str(sum(observed, Decimal(0)).quantize(CENT)) if observed else None,
                 "estimated_usd_in_window": str(sum(estimated, Decimal(0)).quantize(CENT)) if estimated else None,
                 "findings_without_figure": sum(1 for r in a_rows if r["evidence_class"] == "UNKNOWN")}
        agents.append(entry)
        if observed and window_days and window_days >= MIN_PROJECTION_WINDOW_DAYS:
            total = sum(observed, Decimal(0))
            if window_basis == "requested_window":
                basis = (f"{_fmt(total)} observed in the {window_days}-day window read (every order in it was read) "
                         f"x 365/{window_days}")
            else:
                basis = (f"{_fmt(total)} observed over the {window_days} days back to the oldest order read (the read "
                         f"stopped at the {str(window.get('truncated_by')).replace('_', ' ')} limit) "
                         f"x 365/{window_days}")
            projections.append({
                "agent_id": agent_id, "evidence_class": "ESTIMATED", "confidence": "low",
                "amount_usd": str((total * Decimal(365) / Decimal(window_days)).quantize(CENT, ROUND_HALF_UP)),
                "basis": basis,
                "assumption": ("the store keeps the same order volume, the same codes and the same stacking rate for "
                               "a year; nothing in the data shows that it will"),
            })
    unknown_agents = sorted(set(by_agent) - {a["agent_id"] for a in agents})
    if unknown_agents:
        raise ReportInputError("findings from an agent the coverage note does not list")
    return {
        "report_version": "rr-client-report-1",
        "client_id": client, "shop": cov.get("shop"), "scan_id": scan["scan_id"], "as_of": scan["as_of"],
        "data_source": src, "snapshot_id": scan.get("store_snapshot_id"), "api_version": cov.get("api_version"),
        "window": window, "window_days": window_days, "window_basis": window_basis, "counts": cov.get("counts") or {},
        "agents": agents, "findings": rows, "projections": projections,
        "overlapping_entities": sorted(overlap_entities),
        "evidence_counts": {k: sum(1 for r in rows if r["evidence_class"] == k)
                            for k in ("OBSERVED", "ESTIMATED", "MODELED", "UNKNOWN")},
        "customer_data": cov.get("customer_data"),
        "ledger": {"scan_events": scan.get("ledger_events_created", 0) + scan.get("ledger_events_already_present", 0),
                   "verified": bool((scan.get("ledger_verify") or {}).get("valid"))},
    }


def _amount_text(r: dict) -> str:
    if r["amount_usd"] is None:
        return "no dollar figure"
    a = _fmt(Decimal(r["amount_usd"]))
    return a if r["evidence_class"] == "OBSERVED" else f"est. {a}"


def render_markdown(rep: dict) -> str:
    w = rep["window"]
    c = rep["counts"]
    if rep.get("window_basis") == "requested_window":
        scope_line = (f"- Window read: every order created from {w.get('requested_start') or '—'} to the snapshot "
                      f"({rep['window_days'] or 0} days; lookback asked: {w.get('lookback_days')} days")
    else:
        scope_line = (f"- Orders created from {w.get('oldest_order_read') or '—'} to {w.get('newest_order_read') or '—'} "
                      f"({rep['window_days'] or 0} days; lookback asked: {w.get('lookback_days')} days")
    L = [f"# Revenue Recovery findings — {rep['shop']}.myshopify.com", "",
         f"Client `{rep['client_id']}` · scan `{rep['scan_id']}` · as of {rep['as_of']} · "
         f"store snapshot `{rep['snapshot_id']}` (Shopify Admin API {rep['api_version']})", "",
         "## How to read this report", "",
         "- **OBSERVED** figures are read off your store's own order records with exact arithmetic. They are the only "
         "figures we quote as facts.",
         "- **est.** figures are ESTIMATED: observed inputs plus an assumption we state next to them.",
         "- **No dollar figure** means we found the gap but the data cannot support an amount.",
         "- There is no overall score and no grand total: figures of different kinds are never added together, and "
         "an order flagged by two checks is marked, not counted twice.", "",
         "## What we looked at", "",
         f"{scope_line}"
         f"{', limited to 60 days by the granted scopes' if w.get('lookback_limited_by_scope') else ''}"
         f"{'; read stopped at the ' + w['truncated_by'].replace('_', ' ') + ' limit, so older orders were not read' if w.get('truncated_by') else ''}).",
         *([f"- Orders found in the window are dated {w.get('oldest_order_read')} to {w.get('newest_order_read')}."]
           if rep.get("window_basis") == "requested_window" and w.get("oldest_order_read") else []),
         f"- {c.get('orders_read', 0)} orders read; {c.get('orders_analysed', 0)} analysed; "
         f"{sum((c.get('orders_excluded') or {}).values())} left out (below).", ""]
    if c.get("orders_excluded"):
        L += ["| Left out because | Orders |", "|---|---|"]
        L += [f"| {k.replace('_', ' ')} | {v} |" for k, v in c["orders_excluded"].items()]
        L.append("")
    L += ["## Checks run", "", "| Check | Covered by this store's data | Findings | Observed in window |", "|---|---|---|---|"]
    for a in rep["agents"]:
        cov = "yes" if a["covered"] else f"NOT COVERED — {_md(a['why'])}"
        obs = _fmt(Decimal(a["observed_usd_in_window"])) if a["observed_usd_in_window"] else "—"
        L.append(f"| {a['title']} | {cov} | {a['findings'] if a['covered'] else '—'} | {obs} |")
    L += ["", "A check marked NOT COVERED had no data from this source: it found nothing because it could not look, "
          "not because there is nothing there.", "", "## Findings", ""]
    if not rep["findings"]:
        L += ["No findings in the covered checks for this window.", ""]
    for r in rep["findings"]:
        L += [f"### {AGENT_TITLES.get(r['agent_id'], r['agent_id'])} — order `{r['entity_id']}`", "",
              f"- **{_amount_text(r)}** · evidence {r['evidence_class']}"
              + (f" · labels {r['classification']}/{r['confidence']}" if r["classification"] else "")
              + (" · quotable" if r["quotable"] else ""),
              f"- {_md(r['cause'])}",
              f"- How: {_md(r['methodology'])}"]
        if r["also_claimed_by_another_agent"]:
            L.append("- Also flagged by another check: do not add the two figures together.")
        L.append("")
    if rep["projections"]:
        L += ["## Projections (ESTIMATED — not findings)", ""]
        for p in rep["projections"]:
            L += [f"- {AGENT_TITLES.get(p['agent_id'], p['agent_id'])}: **est. {_fmt(Decimal(p['amount_usd']))} a "
                  f"year** ({p['basis']}). Assumes {p['assumption']}. Confidence: {p['confidence']}.", ""]
    L += ["## Your customers' data", "",
          f"- {rep['customer_data']}. Orders are identified by their Shopify order ID only.",
          "- The read was read-only: the connection had no write access, and we refuse any credential that has it.", "",
          "## Evidence", "",
          f"- Scan recorded on ZBM's tamper-evident ledger ({rep['ledger']['scan_events']} events; chain verified: "
          f"{'yes' if rep['ledger']['verified'] else 'NO'}), tied to the store read by snapshot `{rep['snapshot_id']}`.",
          ""]
    return "\n".join(L)


def report_from_file(path: str) -> tuple[dict, str]:
    with open(path, "rb") as f:
        scan = json.loads(f.read())
    rep = build_report(scan)
    return rep, render_markdown(rep)
