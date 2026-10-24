"""
Search-truth drift (spec; Wave 2 stage 2) — Osei's comparison of two audit runs of the same target.

Every finding is keyed by (agent, code, url, and the detail fields that name WHAT it is about: token, field, engine,
prompt index). A key present in only one run, or present in both with a different severity or decision, is a change,
and every change gets exactly one class by the FIRST rule below that its evidence supports. A class is never
assigned without the evidence listed with it; UNKNOWN is a valid answer and is used whenever no rule's evidence holds.

  R1 TOOL_FAILURE       the agent's outcome, or the page's fetch state, is not OK in either run (or differs): the
                        observation failed, so the change says nothing about the site
  R2 MEASUREMENT_ERROR  the ruler moved and the thing measured did not: the bot family list, the structured-data
                        rule table, the canonical entity record version, or (answer engines) the prompt set changed,
                        while the page / robots.txt is the same by fingerprint
  R3 UNKNOWN (confound) the ruler moved AND the thing measured changed: no single cause can be claimed (AEGIS
                        1472041 M4: never REAL_CHANGE with a confound)
  R4 MODEL_DRIFT        (answer engines) same prompt set, the model versions the engine reported differ
  R5 SAMPLING_NOISE     (answer engines) same prompt set and model versions, and the 95% intervals of both the
                        citation and the mention rate overlap between the runs
  R6 SURFACE_DRIFT      (answer engines) same prompt set and model versions, intervals do not overlap: the answer
                        surface changed (consistent with it; no claim about why)
  R7 REAL_CHANGE        the ruler did not move and (pages) the content fingerprint, served status, final URL or
                        X-Robots-Tag differs; (robots.txt) its SHA-256 differs; (sitemaps / llms.txt) the observed
                        counts or states differ
  R8 UNKNOWN            none of the above holds
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

AGENT, TASK = "osei", "search_truth_drift"
RULES_VERSION = "2026-10-10.2"   # .2: ruler changes are checked before real change
CLASSES = ("REAL_CHANGE", "MEASUREMENT_ERROR", "MODEL_DRIFT", "SURFACE_DRIFT", "SAMPLING_NOISE", "TOOL_FAILURE",
           "UNKNOWN")
KEY_DETAIL = ("token", "field", "engine", "prompt_index")
OVERTURNING = ("MEASUREMENT_ERROR", "SAMPLING_NOISE")   # a vanished finding that was never real (TOOL_FAILURE: no info)


class NotComparable(Exception):
    pass


def key(agent: str, f: dict) -> str:
    d = f.get("detail") or {}
    parts = [agent, f["code"], f.get("url") or "-"] + [f"{k}={d[k]}" for k in KEY_DETAIL if k in d]
    return "|".join(str(p) for p in parts)


def _index(report: dict) -> dict:
    out = {}
    for e in report["agents"]:
        for f in e["findings"]:
            out.setdefault(key(e["agent"], f), (e["agent"], f))
    return out


def _agents(report: dict) -> dict:
    return {e["agent"]: e for e in report["agents"]}


def _path_of(report: dict, url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    prefix = f"{report['scheme']}://{report['domain']}"
    if url.startswith(prefix):
        p = url[len(prefix):] or "/"
        return p if p in report.get("pages", {}) else None
    return None


def _overlap(a: Optional[list], b: Optional[list]) -> Optional[bool]:
    if not a or not b:
        return None
    return a[0] <= b[1] and b[0] <= a[1]


def _prompt(env: Optional[dict], engine: str, idx: int) -> Optional[dict]:
    try:
        return env["facts"]["engines"][engine]["prompts"][idx]
    except (KeyError, IndexError, TypeError):
        return None


def classify(agent: str, f: dict, a: dict, b: dict) -> tuple[str, dict]:
    """(class, evidence) for one changed finding; ``a`` older report, ``b`` newer."""
    ea, eb = _agents(a).get(agent), _agents(b).get(agent)
    oa, ob = (ea or {}).get("outcome"), (eb or {}).get("outcome")
    ok = ("OK", "PARTIAL")
    if oa not in ok or ob not in ok:
        return "TOOL_FAILURE", {"rule": "R1", "outcome_before": oa, "outcome_after": ob}
    url = f.get("url")
    pa, pb = _path_of(a, url), _path_of(b, url)
    if pa is not None and pb is not None:
        sa, sb = a["pages"][pa]["fetch"]["state"], b["pages"][pb]["fetch"]["state"]
        if sa != "OK" or sb != "OK":
            return "TOOL_FAILURE", {"rule": "R1", "fetch_state_before": sa, "fetch_state_after": sb}
    d = f.get("detail") or {}
    if agent == "callum" and "engine" in d and "prompt_index" in d:
        psa, psb = (a.get("prompt_set") or {}).get("sha256"), (b.get("prompt_set") or {}).get("sha256")
        if psa != psb:
            return "MEASUREMENT_ERROR", {"rule": "R2", "prompt_set_before": psa, "prompt_set_after": psb}
        qa, qb = _prompt(ea, d["engine"], d["prompt_index"]), _prompt(eb, d["engine"], d["prompt_index"])
        if qa is None or qb is None:
            return "UNKNOWN", {"rule": "R8", "why": "the prompt's samples are missing from one run"}
        if qa["model_versions"] != qb["model_versions"]:
            return "MODEL_DRIFT", {"rule": "R4", "models_before": qa["model_versions"],
                                   "models_after": qb["model_versions"]}
        oc, om = _overlap(qa["citation_ci95"], qb["citation_ci95"]), _overlap(qa["mention_ci95"], qb["mention_ci95"])
        ev = {"citation_ci95": [qa["citation_ci95"], qb["citation_ci95"]],
              "mention_ci95": [qa["mention_ci95"], qb["mention_ci95"]]}
        if oc is None or om is None:
            return "UNKNOWN", {"rule": "R8", "why": "an interval is missing (too few usable answers)", **ev}
        if oc and om:
            return "SAMPLING_NOISE", {"rule": "R5", **ev}
        return "SURFACE_DRIFT", {"rule": "R6", **ev}
    if pa is not None and pb is not None:
        A, B = a["pages"][pa], b["pages"][pb]
        served = {k: (A["fetch"].get(k), B["fetch"].get(k)) for k in ("status", "final_url", "x_robots_tag")
                  if A["fetch"].get(k) != B["fetch"].get(k)}
        fa, fb = A.get("fingerprint"), B.get("fingerprint")
        moved = _ruler(a, b, agent)
        changed = sorted(k for k in fb if fa.get(k) != fb.get(k)) if fa is not None and fb is not None else None
        page_moved = bool(changed) or bool(served)
        if moved and page_moved:
            return "UNKNOWN", {"rule": "R3", "why": "the rules and the page both changed", "changed": moved,
                               "changed_fields": changed, "served_changed": served}
        if moved and changed is not None:
            return "MEASUREMENT_ERROR", {"rule": "R2", "page_unchanged": True, "changed": moved}
        if page_moved:
            return "REAL_CHANGE", {"rule": "R7", "changed_fields": changed, "served_changed": served}
        return "UNKNOWN", {"rule": "R8", "why": "same page, same rules; no evidence for a cause"}
    if agent == "selene" and url and url.endswith("/robots.txt"):
        ra = ((ea.get("facts") or {}).get("robots") or {}).get("text_sha256")
        rb = ((eb.get("facts") or {}).get("robots") or {}).get("text_sha256")
        if ra is not None and rb is not None:
            moved = _ruler(a, b, agent)
            if moved and ra != rb:
                return "UNKNOWN", {"rule": "R3", "why": "the rules and robots.txt both changed", "changed": moved,
                                   "robots_sha256": [ra, rb]}
            if moved:
                return "MEASUREMENT_ERROR", {"rule": "R2", "robots_unchanged": True, "changed": moved}
            if ra != rb:
                return "REAL_CHANGE", {"rule": "R7", "robots_sha256": [ra, rb]}
        return "UNKNOWN", {"rule": "R8", "why": "no robots.txt hash in one run, or nothing changed"}
    if agent == "delia":
        keys = ("sitemaps_read", "sitemap_urls", "sitemap_fetch_failures", "llms_txt")
        fa, fb = ea.get("facts") or {}, eb.get("facts") or {}
        diff = {k: [fa.get(k), fb.get(k)] for k in keys if fa.get(k) != fb.get(k)}
        if diff:
            return "REAL_CHANGE", {"rule": "R7", "observed_changed": diff}
        return "UNKNOWN", {"rule": "R8", "why": "the sitemap and llms.txt observations are the same"}
    return "UNKNOWN", {"rule": "R8", "why": "no rule applies to this finding"}


def _ruler(a: dict, b: dict, agent: str) -> dict:
    va, vb = a.get("versions") or {}, b.get("versions") or {}
    moved = {k: [va.get(k), vb.get(k)] for k in ("bot_families", "schema_rules") if va.get(k) != vb.get(k)}
    if agent == "entity_check":
        ea, eb = _agents(a)["entity_check"]["facts"], _agents(b)["entity_check"]["facts"]
        if ea.get("entity_version") != eb.get("entity_version"):
            moved["entity_record_version"] = [ea.get("entity_version"), eb.get("entity_version")]
    return moved


def compare(a: dict, b: dict) -> dict:
    """``a`` the older completed report, ``b`` the newer, same tenant and domain."""
    if a["tenant_id"] != b["tenant_id"] or a["domain"] != b["domain"] or a["scheme"] != b["scheme"]:
        raise NotComparable("different tenant or target")
    ia, ib = _index(a), _index(b)
    changes, unchanged, persisted = [], 0, {}
    for k in sorted(set(ia) | set(ib)):
        fa, fb = ia.get(k), ib.get(k)
        if fa and fb and (fa[1]["severity"], fa[1]["decision"]) == (fb[1]["severity"], fb[1]["decision"]):
            unchanged += 1
            persisted[fa[0]] = persisted.get(fa[0], 0) + 1
            continue
        agent, f = fb or fa
        change = "APPEARED" if fa is None else "DISAPPEARED" if fb is None else "CHANGED"
        cls, ev = classify(agent, f, a, b)
        changes.append({"key": k, "agent": agent, "code": f["code"], "url": f.get("url"), "change": change,
                        "class": cls, "evidence": ev})
    counts = {c: 0 for c in CLASSES}
    for c in changes:
        counts[c["class"]] += 1
    return {"agent": AGENT, "task": TASK, "rules_version": RULES_VERSION, "older": a["audit_id"],
            "newer": b["audit_id"], "changes": changes, "counts": counts, "unchanged": unchanged,
            "persisted_by_agent": persisted,
            "overturned_by_agent": _count(changes, lambda c: c["change"] == "DISAPPEARED"
                                          and c["class"] in OVERTURNING),
            "rules": "first matching rule R1..R8 (agents/drift.py); UNKNOWN when no rule's evidence holds",
            "search_performance": compare_search_performance(a, b)}


# Search performance (first-party, Search Console; agents/search_performance.py). Window totals of clicks and
# impressions are compared metric by metric; each change gets one class by the first rule whose evidence holds:
#   SP0 NOT_CONNECTED      either run had no Search Console to read (no key, no binding, or a report from before the
#                          section existed): nothing was measured, which is not a failure of the tool
#   SP1 TOOL_FAILURE       either run tried and did not read (KILLED, BLOCKED, FAILED, RESTRICTED, or no totals)
#   SP2 MEASUREMENT_ERROR  the ruler moved: another property, window length, data state, search type or rules version
#   SP3 overlapping windows (``windows_overlap_days`` o of d days). Search Analytics final data for a shared day is
#       the same in both reads, so the difference of the totals is exactly (new-only days) - (old-only days): two
#       Poisson counts over d - o days each. Under "no change" (one daily rate for both windows) its variance is
#       estimated by (before + after) * (d - o) / d, and the band is twice its square root (o = 0 gives the plain
#       2 * sqrt(before + after)). With fewer than SP_MIN_FRESH_DAYS non-overlapping days the comparison is skipped
#       (class UNKNOWN): a day or two of fresh data is dominated by day-of-week effects and the band says nothing.
#       Inside the band: SAMPLING_NOISE.
#   SP4 REAL_CHANGE        outside that band: an observed change in first-party totals (no cause is claimed)
# Only what survives retention is used (property, window, totals, versions), so the API and the scheduler classify a
# pair the same way before and after the breakdowns expire. CTR and position are derived and shown before / after
# only. Whatever the class, the decision stays INSUFFICIENT_EVIDENCE until revenue is connected (the Goodhart pairing):
# a search metric alone never justifies ACT. Caveat: real counts are over-dispersed relative to Poisson, so the band
# is narrow and REAL_CHANGE means "outside Poisson noise", not "significant".
SP_RULES_VERSION = "2026-10-24.2"            # .2 (AEGIS gl-gsc L3): SP0, the overlap rule, expired sections compared
SP_MIN_FRESH_DAYS = 7
SP_CLASSES = ("NOT_CONNECTED", "TOOL_FAILURE", "MEASUREMENT_ERROR", "SAMPLING_NOISE", "REAL_CHANGE", "UNKNOWN")
_SP_RULER = ("property", "days", "data_state", "search_type", "version")
_SP_NOT_CONNECTED = frozenset({"GSC_NOT_CONNECTED", "GSC_PROPERTY_UNBOUND"})


def _sp_ruler(sec: dict) -> dict:
    w = sec.get("window") or {}
    return {"property": sec.get("property"), "days": w.get("days"), "data_state": w.get("data_state"),
            "search_type": w.get("search_type"), "version": (sec.get("versions") or {}).get("search_performance")}


def _sp_not_connected(block: Optional[dict]) -> bool:
    return block is None or block.get("outcome") == "NOT_CONNECTED" or block.get("reason") in _SP_NOT_CONNECTED


def _sp_overlap(wa: dict, wb: dict) -> Optional[int]:
    try:
        o = (min(date.fromisoformat(wa["end"]), date.fromisoformat(wb["end"]))
             - max(date.fromisoformat(wa["start"]), date.fromisoformat(wb["start"]))).days + 1
    except (KeyError, TypeError, ValueError):
        return None
    return max(0, o)


def compare_search_performance(a: dict, b: dict) -> dict | None:
    ba, bb = a.get("search_performance"), b.get("search_performance")
    if ba is None and bb is None:
        return None
    sa, sb = (ba or {}).get("section"), (bb or {}).get("section")
    base = {"rules_version": SP_RULES_VERSION, "decision": "INSUFFICIENT_EVIDENCE",
            "pairing": "revenue NOT_CONNECTED: no decision is drawn from search metrics alone", "metrics": {}}
    ev = {"before": {k: (ba or {}).get(k) for k in ("outcome", "reason")},
          "after": {k: (bb or {}).get(k) for k in ("outcome", "reason")}}
    if _sp_not_connected(ba) or _sp_not_connected(bb):
        base["metrics"] = {m: {"class": "NOT_CONNECTED", "evidence": {"rule": "SP0", **ev}}
                           for m in ("clicks", "impressions")}
        return base
    if ba.get("outcome") != "OK" or bb.get("outcome") != "OK" or not sa or not sb \
            or not isinstance(sa.get("totals"), dict) or not isinstance(sb.get("totals"), dict):
        base["metrics"] = {m: {"class": "TOOL_FAILURE", "evidence": {"rule": "SP1", **ev}}
                           for m in ("clicks", "impressions")}
        return base
    ra, rb = _sp_ruler(sa), _sp_ruler(sb)
    moved = {k: [ra[k], rb[k]] for k in _SP_RULER if ra[k] != rb[k]}
    ta, tb = sa["totals"], sb["totals"]
    overlap = _sp_overlap(sa.get("window") or {}, sb.get("window") or {})
    base["windows_overlap_days"] = overlap
    base["derived"] = {k: [ta.get(k), tb.get(k)] for k in ("ctr", "position")}
    days = ra["days"]
    for m in ("clicks", "impressions"):
        x, y = ta[m], tb[m]
        if moved:
            base["metrics"][m] = {"class": "MEASUREMENT_ERROR", "before": x, "after": y,
                                  "evidence": {"rule": "SP2", "changed": moved}}
            continue
        if overlap is None or not isinstance(days, int) or days < 1:
            base["metrics"][m] = {"class": "UNKNOWN", "before": x, "after": y,
                                  "evidence": {"rule": "SP3", "why": "the windows cannot be compared"}}
            continue
        fresh = days - overlap
        if fresh < SP_MIN_FRESH_DAYS:
            base["metrics"][m] = {"class": "UNKNOWN", "before": x, "after": y,
                                  "evidence": {"rule": "SP3", "windows_overlap_days": overlap, "fresh_days": fresh,
                                               "why": f"fewer than {SP_MIN_FRESH_DAYS} non-overlapping days: "
                                                      "not compared"}}
            continue
        band = 2 * math.sqrt((x + y) * fresh / days)
        cls, rule = ("SAMPLING_NOISE", "SP3") if abs(y - x) <= band else ("REAL_CHANGE", "SP4")
        base["metrics"][m] = {"class": cls, "before": x, "after": y,
                              "evidence": {"rule": rule, "difference": y - x, "band": round(band, 2),
                                           "windows_overlap_days": overlap, "fresh_days": fresh}}
    return base


def _count(changes: list, pred) -> dict:
    out: dict = {}
    for c in changes:
        if pred(c):
            out[c["agent"]] = out.get(c["agent"], 0) + 1
    return out
