"""
Search-truth drift (spec; Wave 2 stage 2) — Osei's comparison of two audit runs of the same target.

Every finding is keyed by (agent, code, url, and the detail fields that name WHAT it is about: token, field, engine,
prompt index). A key present in only one run, or present in both with a different severity or decision, is a change,
and every change gets exactly one class by the FIRST rule below that its evidence supports. A class is never
assigned without the evidence listed with it; UNKNOWN is a valid answer and is used whenever no rule's evidence holds.

  R1 TOOL_FAILURE       the agent's outcome, or the page's fetch state, is not OK in either run (or differs): the
                        observation failed, so the change says nothing about the site
  R2 MODEL_DRIFT        (answer engines) the prompt set hash, or the model versions the engine reported, differ
  R3 SAMPLING_NOISE     (answer engines) same prompt set and model versions, and the 95% intervals of both the
                        citation and the mention rate overlap between the runs
  R4 SURFACE_DRIFT      (answer engines) same prompt set and model versions, intervals do not overlap: the answer
                        surface changed (consistent with it; no claim about why)
  R5 REAL_CHANGE        (pages) the page's content fingerprint, served status, final URL or X-Robots-Tag differs;
                        (robots.txt) its SHA-256 differs; (sitemaps / llms.txt) the observed counts or states differ
  R6 MEASUREMENT_ERROR  the page / robots.txt is byte-for-byte the same by fingerprint, but the measuring rules or
                        the reference changed (bot family list, structured-data rule table, canonical entity record
                        version): the finding moved because the ruler moved
  R7 UNKNOWN            none of the above holds
"""

from __future__ import annotations

from typing import Optional

AGENT, TASK = "osei", "search_truth_drift"
RULES_VERSION = "2026-10-10.1"
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
            return "MODEL_DRIFT", {"rule": "R2", "prompt_set_before": psa, "prompt_set_after": psb}
        qa, qb = _prompt(ea, d["engine"], d["prompt_index"]), _prompt(eb, d["engine"], d["prompt_index"])
        if qa is None or qb is None:
            return "UNKNOWN", {"rule": "R7", "why": "the prompt's samples are missing from one run"}
        if qa["model_versions"] != qb["model_versions"]:
            return "MODEL_DRIFT", {"rule": "R2", "models_before": qa["model_versions"],
                                   "models_after": qb["model_versions"]}
        oc, om = _overlap(qa["citation_ci95"], qb["citation_ci95"]), _overlap(qa["mention_ci95"], qb["mention_ci95"])
        ev = {"citation_ci95": [qa["citation_ci95"], qb["citation_ci95"]],
              "mention_ci95": [qa["mention_ci95"], qb["mention_ci95"]]}
        if oc is None or om is None:
            return "UNKNOWN", {"rule": "R7", "why": "an interval is missing (too few usable answers)", **ev}
        if oc and om:
            return "SAMPLING_NOISE", {"rule": "R3", **ev}
        return "SURFACE_DRIFT", {"rule": "R4", **ev}
    if pa is not None and pb is not None:
        A, B = a["pages"][pa], b["pages"][pb]
        served = {k: (A["fetch"].get(k), B["fetch"].get(k)) for k in ("status", "final_url", "x_robots_tag")
                  if A["fetch"].get(k) != B["fetch"].get(k)}
        fa, fb = A.get("fingerprint"), B.get("fingerprint")
        if fa is not None and fb is not None:
            changed = sorted(k for k in fb if fa.get(k) != fb.get(k))
            if changed or served:
                return "REAL_CHANGE", {"rule": "R5", "changed_fields": changed, "served_changed": served}
            moved = _ruler(a, b, agent)
            if moved:
                return "MEASUREMENT_ERROR", {"rule": "R6", "page_unchanged": True, "changed": moved}
        elif served:
            return "REAL_CHANGE", {"rule": "R5", "served_changed": served}
        return "UNKNOWN", {"rule": "R7", "why": "same page, same rules; no evidence for a cause"}
    if agent == "selene" and url and url.endswith("/robots.txt"):
        ra = ((ea.get("facts") or {}).get("robots") or {}).get("text_sha256")
        rb = ((eb.get("facts") or {}).get("robots") or {}).get("text_sha256")
        if ra is not None and rb is not None:
            if ra != rb:
                return "REAL_CHANGE", {"rule": "R5", "robots_sha256": [ra, rb]}
            moved = _ruler(a, b, agent)
            if moved:
                return "MEASUREMENT_ERROR", {"rule": "R6", "robots_unchanged": True, "changed": moved}
        return "UNKNOWN", {"rule": "R7", "why": "no robots.txt hash in one run, or nothing changed"}
    if agent == "delia":
        keys = ("sitemaps_read", "sitemap_urls", "sitemap_fetch_failures", "llms_txt")
        fa, fb = ea.get("facts") or {}, eb.get("facts") or {}
        diff = {k: [fa.get(k), fb.get(k)] for k in keys if fa.get(k) != fb.get(k)}
        if diff:
            return "REAL_CHANGE", {"rule": "R5", "observed_changed": diff}
        return "UNKNOWN", {"rule": "R7", "why": "the sitemap and llms.txt observations are the same"}
    return "UNKNOWN", {"rule": "R7", "why": "no rule applies to this finding"}


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
            "rules": "first matching rule R1..R7 (agents/drift.py); UNKNOWN when no rule's evidence holds"}


def _count(changes: list, pred) -> dict:
    out: dict = {}
    for c in changes:
        if pred(c):
            out[c["agent"]] = out.get(c["agent"], 0) + 1
    return out
