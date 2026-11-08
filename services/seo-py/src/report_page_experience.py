"""
Lena's area of the client report (ADR 0050): the Chrome UX Report section of a completed audit as rows of
closed-vocabulary strings, and its Markdown and HTML renderings. Kept beside client_report.py (whose rules apply
unchanged: deterministic, escaped for the output format, nothing added or scored); every value shown is a number from
the recorded section, formatted here, or a fixed phrase.
"""

from __future__ import annotations

from typing import Any, Optional

from client_report import DASH, _DAY, _Ctx, _num, h, md, md_code, safe_token

FIELD_SHORT = ("LCP", "INP", "CLS", "FCP", "TTFB")
FIELD_DEVICES = (("PHONE", "phone"), ("DESKTOP", "desktop"), ("TABLET", "tablet"))
_RATING = {"GOOD": "good", "NEEDS_IMPROVEMENT": "needs improvement", "POOR": "poor"}
_CWV = {"PASSED": "passed", "FAILED": "not passed", "INSUFFICIENT_FIELD_DATA": "not enough field data"}
_TREND = {"IMPROVING": "improving", "WORSENING": "worsening", "NO_CLEAR_CHANGE": "no clear change",
          "INSUFFICIENT_HISTORY": "not enough history"}
_ENTRY = {"INSUFFICIENT_FIELD_DATA": "no field data (too few visits)", "NOT_QUERIED": "not asked",
          "FAILED": "not read"}
FIELD_EVIDENCE = ("OBSERVED by Google (Chrome UX Report field data from real Chrome users), not by this audit's own "
                  "requests; each value is the 75th percentile over the collection period shown")


def field_value(m: Any) -> str:
    if not isinstance(m, dict):
        return "no data"
    if m.get("state") == "MALFORMED":
        return "set aside (malformed)"
    if m.get("state") != "OK":
        return "no data"
    v, unit, r = m.get("p75"), m.get("unit"), _RATING.get(m.get("rating"))
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 <= v <= 1e9 or r is None:
        return "no data"
    shown = f"{v:.2f}" if unit == "unitless" else f"{int(round(v)):,} ms"
    return f"{shown} ({r})"


def field_period(first: Any, last: Any) -> Optional[str]:
    if isinstance(first, str) and _DAY.fullmatch(first) and isinstance(last, str) and _DAY.fullmatch(last):
        return f"{first} to {last}"
    return None


def field_freshness(sec: Any) -> str:
    lasts = []
    for t in (sec.get("targets") or []) if isinstance(sec, dict) else []:
        ffs = t.get("form_factors") if isinstance(t, dict) else None
        for ff in (ffs.values() if isinstance(ffs, dict) else []):
            cur = ff.get("current") if isinstance(ff, dict) else None
            p = cur.get("collection_period") if isinstance(cur, dict) else None
            if isinstance(p, dict) and isinstance(p.get("last"), str) and _DAY.fullmatch(p["last"]):
                lasts.append(p["last"])
    if not lasts:
        return "no Chrome UX Report data in this report"
    return (f"Google field data for the 28 days up to {max(lasts)} (Chrome UX Report; about two days behind; "
            "refreshed by Google daily)")


def field_block(env: Optional[dict], block: Optional[dict], ctx: _Ctx) -> Optional[dict]:
    """Lena's CrUX section as rows of closed-vocabulary strings: per target and device, the p75 of each metric with
    its rating, the Core Web Vitals assessment and the collection period; and the trend of LCP / INP / CLS. Targets
    are only the audited origin and the audited paths (anything else in the section is not shown)."""
    if env is None or env.get("outcome") not in ("OK", "PARTIAL") or not isinstance(block, dict):
        return None
    sec = block.get("section")
    if not isinstance(sec, dict) or not isinstance(sec.get("targets"), list):
        return None
    origin = f"{ctx.scheme}://{ctx.domain}"
    rows, trends = [], []
    for t in sec["targets"][:1 + len(ctx.paths)]:
        if not isinstance(t, dict) or not isinstance(t.get("form_factors"), dict):
            continue
        if t.get("kind") == "origin" and t.get("target") == origin:
            label = "whole site (origin)"
        elif t.get("kind") == "url" and isinstance(t.get("target"), str) and t["target"].startswith(origin) \
                and t["target"][len(origin):] in ctx.paths:
            label = ctx.path(t["target"][len(origin):])
        else:
            continue
        for ff, dev in FIELD_DEVICES:
            e = t["form_factors"].get(ff)
            e = e if isinstance(e, dict) else {}
            cur = e.get("current") if isinstance(e.get("current"), dict) else {}
            if cur.get("state") == "OK":
                ms = cur.get("metrics") if isinstance(cur.get("metrics"), dict) else {}
                cw = cur.get("core_web_vitals") if isinstance(cur.get("core_web_vitals"), dict) else {}
                p = cur.get("collection_period") if isinstance(cur.get("collection_period"), dict) else {}
                rows.append({"target": label, "device": dev, "values": [field_value(ms.get(k)) for k in FIELD_SHORT],
                             "cwv": _CWV.get(cw.get("assessment"), "not enough field data"),
                             "period": field_period(p.get("first"), p.get("last")) or "unknown",
                             "stale": cur.get("stale") is True})
            else:
                rows.append({"target": label, "device": dev, "values": None,
                             "cwv": _ENTRY.get(cur.get("state"), "not read"), "period": DASH, "stale": False})
            hist = e.get("history") if isinstance(e.get("history"), dict) else {}
            cps = hist.get("collection_periods")
            if hist.get("state") == "OK" and isinstance(cps, list) and cps and all(isinstance(c, dict) for c in cps):
                hm = hist.get("metrics") if isinstance(hist.get("metrics"), dict) else {}
                cells = []
                for k in ("LCP", "INP", "CLS"):
                    m = hm.get(k)
                    tr = m.get("trend") if isinstance(m, dict) and m.get("state") == "OK" else None
                    cells.append(_TREND.get(tr.get("direction") if isinstance(tr, dict) else None, "no data"))
                trends.append({"target": label, "device": dev, "cells": cells, "periods": _num(len(cps)) or "0",
                               "span": field_period(cps[0].get("first"), cps[-1].get("last")) or "unknown"})
    return {"rows": rows, "trends": trends, "thresholds_version": safe_token(sec.get("thresholds_version"))}


def md_field(b: dict) -> list:
    def tgt(x):
        return md_code(x) if x.startswith("/") else md(x)
    L = [f"Evidence: {md(FIELD_EVIDENCE)}. Ratings use web.dev's thresholds (version "
         f"{md_code(b['thresholds_version'])}): good at or below the good limit, poor above the poor limit.", "",
         "| Page | Device | " + " | ".join(FIELD_SHORT) + " | Core Web Vitals | Collection period |",
         "|---|---|" + "---|" * len(FIELD_SHORT) + "---|---|"]
    for r in b["rows"]:
        vals = r["values"] or [DASH] * len(FIELD_SHORT)
        period = r["period"] + (" (stale)" if r["stale"] else "")
        L.append(f"| {tgt(r['target'])} | {md(r['device'])} | " + " | ".join(md(v) for v in vals)
                 + f" | {md(r['cwv'])} | {md(period)} |")
    L += ["", "No field data means Google has too few real-user visits to report; it is not a fast or a slow result.",
          ""]
    if b["trends"]:
        L += ["**Trend over the weekly history** (descriptive; the periods overlap; no forecast):", "",
              "| Page | Device | LCP | INP | CLS | Periods |", "|---|---|---|---|---|---|"]
        L += [f"| {tgt(t['target'])} | {md(t['device'])} | " + " | ".join(md(c) for c in t["cells"])
              + f" | {md(t['periods'])} ({md(t['span'])}) |" for t in b["trends"]]
        L.append("")
    return L


def h_field(b: dict) -> list:
    o = [f"<p>Evidence: {h(FIELD_EVIDENCE)}. Ratings use web.dev's thresholds (version "
         f"<code>{h(b['thresholds_version'])}</code>): good at or below the good limit, poor above the poor limit.</p>",
         "<table><thead><tr><th>Page</th><th>Device</th>" + "".join(f"<th>{k}</th>" for k in FIELD_SHORT)
         + "<th>Core Web Vitals</th><th>Collection period</th></tr></thead><tbody>"]
    for r in b["rows"]:
        vals = r["values"] or [DASH] * len(FIELD_SHORT)
        period = r["period"] + (" (stale)" if r["stale"] else "")
        o.append(f"<tr><td>{h(r['target'])}</td><td>{h(r['device'])}</td>"
                 + "".join(f"<td>{h(v)}</td>" for v in vals) + f"<td>{h(r['cwv'])}</td><td>{h(period)}</td></tr>")
    o += ["</tbody></table>",
          "<p>No field data means Google has too few real-user visits to report; it is not a fast or a slow "
          "result.</p>"]
    if b["trends"]:
        o += ["<h3>Trend over the weekly history (descriptive; the periods overlap; no forecast)</h3>",
              "<table><thead><tr><th>Page</th><th>Device</th><th>LCP</th><th>INP</th><th>CLS</th><th>Periods</th>"
              "</tr></thead><tbody>"]
        o += [f"<tr><td>{h(t['target'])}</td><td>{h(t['device'])}</td>"
              + "".join(f"<td>{h(c)}</td>" for c in t["cells"]) + f"<td>{h(t['periods'])} ({h(t['span'])})</td></tr>"
              for t in b["trends"]]
        o.append("</tbody></table>")
    return o
