"""
Roman — content structure and machine readability (P3). One task: is each audited page's structure and markup
legible to machines, as observed in the raw HTML?

Per page: title, meta description, H1 count, heading-level skips, document language, structured data (JSON-LD
validity against parse.SCHEMA_RULES, versioned), entity markup (Organization / LocalBusiness with sameAs), how much
text is in the raw HTML (engines' crawlers that do not run JavaScript see only that).

Per engine: a HEURISTIC READINESS CHECKLIST — which checks apply to that engine, and which passed. It is labelled
``calibrated: false`` with its methodology, it is not a visibility measurement, and it is never combined into one
number across engines (spec: no single visibility score unless its methodology is calibrated).
"""

from __future__ import annotations

from agents import bots
from envelope import envelope, finding, observed
from primitives.parse import ORG_TYPES, SCHEMA_RULES_VERSION

AGENT, TASK = "roman", "content_structure"
TITLE_MAX, TITLE_MIN = 60, 10
RAW_TEXT_MIN = 500
ENGINES = ("google", "bing", "openai", "anthropic", "perplexity")
# check -> engines it applies to (heuristic; see METHODOLOGY)
CHECK_APPLIES = {
    "crawler_allowed": ENGINES, "indexable": ENGINES, "title_present": ENGINES, "single_h1": ENGINES,
    "structured_data_valid": ("google", "bing"), "entity_markup": ENGINES, "text_in_raw_html": ENGINES,
    "lang_declared": ENGINES,
}
METHODOLOGY = ("Raw HTML of each audited page parsed (no JavaScript). Structured data validated against this "
               "service's rule table version {v} (schema.org-derived; not any engine's validator). Per-engine "
               "readiness is a HEURISTIC CHECKLIST: 'crawler_allowed' uses the engine's search/answer crawler token "
               "({bots}) against robots.txt; 'text_in_raw_html' requires at least {n} characters of visible text "
               "without rendering; 'structured_data_valid' is applied to Google and Bing only, whose documentation "
               "describes using structured data; the others apply to every engine. It is NOT calibrated against "
               "any engine's behaviour, NOT a visibility measurement, and NOT combined into a score.")
LIMITS = ["Heuristic checklist, uncalibrated: passing every check does not mean an engine will show or cite the page.",
          "Only the audited pages were read; site-wide content structure is not assessed.",
          "Rendering is not connected: markup inserted by JavaScript was not seen."]


def run(ctx, pages: dict) -> dict:
    findings, per_page, readiness = [], {}, {}
    entity_seen = False
    for path, pg in pages.items():
        x, f = pg.get("extract"), pg["fetch"]
        url = ctx.url(path)
        if x is None:
            continue
        h1 = [h for h in x["headings"] if h["level"] == 1]
        skips = _skips(x["headings"])
        sd_errors = [i for b in x["jsonld"] for n in b["nodes"] for i in n["issues"] if i["level"] == "error"]
        sd_warn = [i for b in x["jsonld"] for n in b["nodes"] for i in n["issues"] if i["level"] == "warning"]
        sd_invalid = [b for b in x["jsonld"] if not b["valid_json"]]
        types = sorted({t for b in x["jsonld"] for n in b["nodes"] for t in n["types"]})
        org_nodes = [n for b in x["jsonld"] for n in b["nodes"] if set(n["types"]) & set(ORG_TYPES)]
        entity = bool(org_nodes)
        entity_seen |= entity
        per_page[path] = {"title": observed(x["title"]), "title_length": len(x["title"] or ""),
                          "description_present": bool(x["description"]), "h1_count": len(h1),
                          "heading_skips": skips, "lang": x["lang"], "jsonld_blocks": len(x["jsonld"]),
                          "schema_types": types[:30], "text_length_raw": x["text_length"]}
        if not x["title"]:
            findings.append(finding("TITLE_MISSING", "high", "measured", "ACT", url=url, capability="P3"))
        elif len(x["title"]) > TITLE_MAX or len(x["title"]) < TITLE_MIN:
            findings.append(finding("TITLE_LENGTH", "low", "measured", "TEST", url=url, capability="P3",
                                    detail={"length": len(x["title"]), "heuristic_range": [TITLE_MIN, TITLE_MAX]}))
        if not x["description"]:
            findings.append(finding("META_DESCRIPTION_MISSING", "low", "measured", "TEST", url=url, capability="P3"))
        if len(h1) != 1:
            findings.append(finding("H1_COUNT", "medium", "measured", "ACT", url=url, capability="P3",
                                    detail={"h1_count": len(h1)}))
        if skips:
            findings.append(finding("HEADING_LEVEL_SKIP", "low", "measured", "TEST", url=url, capability="P3",
                                    detail={"skips": skips[:10]}))
        if not x["lang"]:
            findings.append(finding("LANG_MISSING", "low", "measured", "ACT", url=url, capability="P3"))
        if sd_invalid:
            ctx.osei.quarantine("structured_data", "JSONLD_INVALID", url, None)
            findings.append(finding("STRUCTURED_DATA_INVALID_JSON", "medium", "measured", "ACT", url=url,
                                    capability="P3", detail={"blocks": len(sd_invalid)}))
        if sd_errors:
            findings.append(finding("STRUCTURED_DATA_ERRORS", "medium", "measured", "ACT", url=url, capability="P3",
                                    detail={"issues": sd_errors[:20], "rules_version": SCHEMA_RULES_VERSION}))
        if sd_warn:
            findings.append(finding("STRUCTURED_DATA_RECOMMENDED_MISSING", "low", "measured", "TEST", url=url,
                                    capability="P3", detail={"issues": sd_warn[:20],
                                                             "rules_version": SCHEMA_RULES_VERSION}))
        if not x["jsonld"]:
            findings.append(finding("STRUCTURED_DATA_ABSENT", "medium" if path == "/" else "low", "measured", "TEST",
                                    url=url, capability="P3"))
        if org_nodes and not any(n["props"]["same_as"] for n in org_nodes):
            findings.append(finding("ENTITY_SAMEAS_MISSING", "low", "measured", "TEST", url=url, capability="P3"))
        if x["text_length"] < RAW_TEXT_MIN:
            findings.append(finding("LITTLE_TEXT_IN_RAW_HTML", "medium", "measured", "TEST", url=url,
                                    capability="P3", detail={"characters": x["text_length"], "heuristic_min":
                                                             RAW_TEXT_MIN, "render_state": pg["render"]["state"]}))
        readiness[path] = _readiness(ctx, path, f, x, h1, sd_errors, sd_invalid, entity)
    if pages and not entity_seen and "/" in pages and pages["/"].get("extract") is not None:
        findings.append(finding("ENTITY_MARKUP_ABSENT", "medium", "measured", "TEST", url=ctx.url("/"),
                                capability="P3", detail={"note": "no Organization / LocalBusiness node on any "
                                                                 "audited page"}))
    read = sum(1 for p in pages.values() if p.get("extract") is not None)
    outcome = "OK" if read and read == len(pages) else ("PARTIAL" if read else "INSUFFICIENT_EVIDENCE")
    methodology = METHODOLOGY.format(v=SCHEMA_RULES_VERSION, n=RAW_TEXT_MIN,
                                     bots=", ".join(f"{e}: {t}" for e, t in bots.ENGINE_SEARCH_BOT.items()))
    return envelope(AGENT, TASK, outcome, findings, methodology=methodology, limitations=LIMITS,
                    not_connected={"render"}, facts={"pages": per_page, "engine_readiness_heuristic": readiness})


def _skips(headings: list) -> list:
    out, prev = [], 0
    for h in headings:
        if prev and h["level"] > prev + 1:
            out.append([prev, h["level"]])
        prev = h["level"]
    return out


def _readiness(ctx, path, f, x, h1, sd_errors, sd_invalid, entity) -> dict:
    rb = ctx.robots or {}
    parsed = rb.get("parsed")
    noindex = any("noindex" in v or "none" in v for v in x["robots_meta"].values()) or \
        "noindex" in (f.headers.get("x-robots-tag") or "").lower()
    out = {}
    for eng in ENGINES:
        token = bots.ENGINE_SEARCH_BOT[eng]
        allowed = parsed.allowed(token, path) if parsed else bool(rb.get("default_allow"))
        results = {
            "crawler_allowed": allowed, "indexable": not noindex, "title_present": bool(x["title"]),
            "single_h1": len(h1) == 1, "structured_data_valid": bool(x["jsonld"]) and not sd_errors and not sd_invalid,
            "entity_markup": entity, "text_in_raw_html": x["text_length"] >= RAW_TEXT_MIN,
            "lang_declared": bool(x["lang"]),
        }
        checks = {k: ("pass" if v else "fail") if eng in CHECK_APPLIES[k] else "n/a" for k, v in results.items()}
        out[eng] = {"checks": checks, "passed": sum(1 for v in checks.values() if v == "pass"),
                    "applicable": sum(1 for v in checks.values() if v != "n/a"), "heuristic": True,
                    "calibrated": False, "crawler_token": token,
                    "label": "heuristic readiness checklist; not a calibrated score; not a visibility measurement"}
    return out
