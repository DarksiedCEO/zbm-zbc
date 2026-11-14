"""
Roman — content structure and machine readability (P3). One task: is each audited page's structure and markup
legible to machines, as observed in the raw HTML?

Per page: title, meta description, H1 count, heading-level skips, document language, structured data (JSON-LD
validity against parse.SCHEMA_RULES, versioned), entity markup (Organization / LocalBusiness with sameAs), how much
text is in the raw HTML (engines' crawlers that do not run JavaScript see only that).

What is stored about a page's text (AEGIS Wave-1 M-R1; the shared redaction core is not on this base): the TITLE
TEXT IS WITHHELD - only measured attributes are kept (length in characters, empty, same as the H1, over TITLE_MAX,
under TITLE_MIN). ``lang`` is kept only as a valid BCP 47 tag reduced to language[-script][-region] (else
``invalid``, counted); an ``@type`` is kept only when it is a schema.org type name in the bundled list
(agents/schema_types.py; else counted ``unknown_type``). Readiness ``indexable`` is per engine: a noindex aimed at
another crawler does not count against this one (S-R2).

Per engine: a HEURISTIC READINESS CHECKLIST — which checks apply to that engine, and which passed. It is labelled
``calibrated: false`` with its methodology, it is not a visibility measurement, and it is never combined into one
number across engines (spec: no single visibility score unless its methodology is calibrated).
"""

from __future__ import annotations

import re

from typing import Optional

from agents import bots
from agents.selene import noindex_for
from envelope import envelope
from envelope import finding as _finding
from primitives import codes
from primitives import robots as robots_mod
from primitives.parse import LOCAL_BUSINESS_TYPES, ORG_TYPES, SCHEMA_RULES, SCHEMA_RULES_VERSION
from primitives.safe_url import safe_path, safe_url

AGENT, TASK = "roman", "content_structure"


def finding(*a, url: Optional[str] = None, **k) -> dict:
    """A finding whose URL is in its stored form (spec B-CD-03)."""
    return _finding(*a, url=safe_url(url), **k)



TITLE_MAX, TITLE_MIN = 60, 10
READINESS_WORK = robots_mod.STEP_WORK_UNITS        # counted work for the per-engine crawler_allowed checks of a run
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
          "Rendering is not connected: markup inserted by JavaScript was not seen.",
          "Page titles are measured (length, empty, same as the H1) but their text is not stored or shown."]
# BCP 47 (RFC 5646) language tag, the common forms: language (2-3 letters, optional extlang), script, region,
# variants, extensions, private use. Only language[-script][-region] is stored.
_BCP47 = re.compile(r"(?P<lang>[A-Za-z]{2,3})(?:-[A-Za-z]{3}){0,3}(?P<script>-[A-Za-z]{4})?"
                    r"(?P<region>-(?:[A-Za-z]{2}|[0-9]{3}))?(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
                    r"(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*(?:-[Xx](?:-[A-Za-z0-9]{1,8})+)?")
# RFC 5646 2.1: a private-use tag (``x-default``) and the grandfathered tags (section 2.2.8: the irregular ones and
# the regular ones, valid only as whole tags) are valid tags with no language[-script][-region] to keep: each is
# stored as its code (D-01: not on the allowlist), never as LANG_INVALID (rv-wave1v2 #6)
_PRIVATE_USE = re.compile(r"[Xx](?:-[A-Za-z0-9]{1,8})+")
GRANDFATHERED = frozenset(x.lower() for x in (
    "en-GB-oed", "i-ami", "i-bnn", "i-default", "i-enochian", "i-hak", "i-klingon", "i-lux", "i-mingo", "i-navajo",
    "i-pwn", "i-tao", "i-tay", "i-tsu", "sgn-BE-FR", "sgn-BE-NL", "sgn-CH-DE",
    "art-lojban", "cel-gaulish", "no-bok", "no-nyn", "zh-guoyu", "zh-hakka", "zh-min", "zh-min-nan", "zh-xiang"))


def _repeats(low: str) -> bool:
    """RFC 5646 2.2.5 / 2.2.6: the same variant twice, or the same extension singleton twice, makes a tag invalid
    (``de-1996-1996``, ``en-a-bbb-a-ccc``); nothing after the private-use ``x`` is compared."""
    variants, singletons, in_ext = set(), set(), False
    for sub in low.split("-")[1:]:
        if len(sub) == 1:
            if sub == "x":
                break
            if sub in singletons:
                return True
            singletons.add(sub)
            in_ext = True
        elif not in_ext and (len(sub) >= 5 or (len(sub) == 4 and sub[0].isdigit())):
            if sub in variants:
                return True
            variants.add(sub)
    return False


# Subtag validity (spec B-RO-05 / D-01 L, Q-05): the CLDR validity data (Unicode-3.0), bundled as
# primitives/cldr_validity.txt (derived by devtools/conformance/cldr_extract.py; THIRD_PARTY_NOTICES).
_CLDR: dict = {}


def cldr(kind: str) -> frozenset:
    if not _CLDR:
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "primitives",
                            "cldr_validity.txt")
        with open(path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                k, *vals = line.split()
                _CLDR[k] = frozenset(vals)
    return _CLDR.get(kind, frozenset())


def script_ok(code: str) -> bool:
    return code.lower() in cldr("script")


def safe_lang(v):
    """A page's ``lang`` as stored (B-RO-05): None when absent (or only white space), ``invalid`` when it is not a
    BCP 47 tag or its language subtag is not valid in CLDR, else language[-Script][-REGION] normalised (``en-US``,
    ``zh-Hant-TW``), a script or region subtag that is not valid in CLDR left out; a private-use or grandfathered
    tag (valid, nothing of it on the allowlist) is its ``{c:lang:..}`` code."""
    if not v or not v.strip():
        return None
    t = v.strip()
    low = t.lower()
    if low in GRANDFATHERED or _PRIVATE_USE.fullmatch(t):
        return codes.code("lang", low)
    m = _BCP47.fullmatch(t)
    if not m or _repeats(low):
        return "invalid"
    lang = m.group("lang").lower()
    if lang not in cldr("language"):
        return "invalid"
    out = lang
    if m.group("script") and script_ok(m.group("script")[1:]):
        out += "-" + m.group("script")[1:].title()
    if m.group("region") and m.group("region")[1:].lower() in cldr("region"):
        out += "-" + m.group("region")[1:].upper()
    return out


# D-01 T / Q-04: an @type is readable only when this service's own rule table names it (no schema.org vocabulary is
# bundled); any other is counted ``unknown_type`` and, where named, coded
TYPE_ALLOWLIST = frozenset(SCHEMA_RULES) | frozenset(ORG_TYPES) | frozenset(LOCAL_BUSINESS_TYPES)


def _types(x: dict) -> tuple[list, int, list]:
    """(allowlisted type names on the page, sorted; how many @type values were not; the codes of those)."""
    known, unknown, coded = set(), 0, []
    for b in x["jsonld"]:
        for n in b["nodes"]:
            for t in n["types"]:
                if t in TYPE_ALLOWLIST:
                    known.add(t)
                else:
                    unknown += 1
                    if len(coded) < 10:
                        coded.append(codes.code("type", t))
    return sorted(known), unknown, coded


def run(ctx, pages: dict) -> dict:
    """The run inside the stored-form context (primitives/codes.py ``within``: the service sets its own)."""
    with codes.within(getattr(ctx, "domain", None)):
        return _run(ctx, pages)


def _run(ctx, pages: dict) -> dict:
    findings, per_page, readiness = [], {}, {}
    work = robots_mod.Work(READINESS_WORK)
    entity_seen = False
    lang_invalid = 0
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
        types, unknown_types, types_coded = _types(x)
        org_nodes = [n for b in x["jsonld"] for n in b["nodes"] if set(n["types"]) & set(ORG_TYPES)]
        entity = bool(org_nodes)
        entity_seen |= entity
        title = " ".join((x["title"] or "").split())        # B-RO-01 / B-CD-05: measured, never stored
        lang = safe_lang(x["lang"])
        if lang == "invalid":
            lang_invalid += 1
        per_page[safe_path(path)] = {"title_withheld": True, "title_length": len(title), "title_empty": not title,
                          "title_same_as_h1": bool(title) and any(" ".join((h["text"] or "").split()) ==
                                                                  " ".join(title.split()) for h in h1),
                          "title_over_max": len(title) > TITLE_MAX, "title_under_min": 0 < len(title) < TITLE_MIN,
                          "description_present": bool(x["description"]), "h1_count": len(h1),
                          "heading_skips": skips, "lang": lang, "jsonld_blocks": len(x["jsonld"]),
                          "schema_types": types[:30], "unknown_type": unknown_types, "unknown_types": types_coded,
                          "text_length_raw": x["text_length"]}
        if not title:
            findings.append(finding("TITLE_MISSING", "high", "measured", "ACT", url=url, capability="P3"))
        elif len(title) > TITLE_MAX or len(title) < TITLE_MIN:
            findings.append(finding("TITLE_LENGTH", "low", "measured", "TEST", url=url, capability="P3",
                                    detail={"length": len(title), "heuristic_range": [TITLE_MIN, TITLE_MAX]}))
        if not x["description"]:
            findings.append(finding("META_DESCRIPTION_MISSING", "low", "measured", "TEST", url=url, capability="P3"))
        if len(h1) != 1:
            findings.append(finding("H1_COUNT", "medium", "measured", "ACT", url=url, capability="P3",
                                    detail={"h1_count": len(h1)}))
        if skips:
            findings.append(finding("HEADING_LEVEL_SKIP", "low", "measured", "TEST", url=url, capability="P3",
                                    detail={"skips": skips[:10]}))
        if not (x["lang"] or "").strip():                 # R18: lang="" (or only white space) is missing
            findings.append(finding("LANG_MISSING", "low", "measured", "ACT", url=url, capability="P3"))
        elif lang == "invalid":
            # B-RO-05 / Q-12: a lang that is not a valid BCP 47 tag (or whose language subtag CLDR does not know)
            findings.append(finding("LANG_INVALID", "low", "measured", "ACT", url=url, capability="P3",
                                    detail={"lang": codes.code("lang", x["lang"].strip())}))
        too_large = [b for b in sd_invalid if b.get("truncated")]
        bad_json = [b for b in sd_invalid if not b.get("truncated")]
        for b in too_large:
            # round 2 Osei M2: a block over the size bound was not parsed: its own reason, the prefix's size and
            # hash labelled as such, and the whole block's size
            ctx.osei.quarantine("structured_data", "JSONLD_TOO_LARGE", url, None, size=b.get("bytes"),
                                digest=b.get("sha256"), basis="block_prefix", total=b.get("total_bytes"))
        for b in bad_json:
            ctx.osei.quarantine("structured_data", "JSONLD_INVALID", url, None, size=b.get("bytes"),
                                digest=b.get("sha256"), basis="decoded")
        if bad_json:
            findings.append(finding("STRUCTURED_DATA_INVALID_JSON", "medium", "measured", "ACT", url=url,
                                    capability="P3", detail={"blocks": len(bad_json)}))
        if too_large:
            findings.append(finding("STRUCTURED_DATA_BLOCK_TOO_LARGE", "medium", "measured", "TEST", url=url,
                                    capability="P3", detail={"blocks": len(too_large),
                                                             "total_bytes": [b.get("total_bytes") for b in too_large],
                                                             "note": "not parsed: over this service's size bound"}))
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
        readiness[safe_path(path)] = _readiness(ctx, path, f, x, h1, sd_errors, sd_invalid, entity, work)
    if pages and not entity_seen and "/" in pages and pages["/"].get("extract") is not None:
        findings.append(finding("ENTITY_MARKUP_ABSENT", "medium", "measured", "TEST", url=ctx.url("/"),
                                capability="P3", detail={"note": "no Organization / LocalBusiness node on any "
                                                                 "audited page"}))
    read = sum(1 for p in pages.values() if p.get("extract") is not None)
    outcome = "OK" if read and read == len(pages) else ("PARTIAL" if read else "INSUFFICIENT_EVIDENCE")
    methodology = METHODOLOGY.format(v=SCHEMA_RULES_VERSION, n=RAW_TEXT_MIN,
                                     bots=", ".join(f"{e}: {t}" for e, t in bots.ENGINE_SEARCH_BOT.items()))
    return envelope(AGENT, TASK, outcome, findings, methodology=methodology, limitations=LIMITS,
                    not_connected={"render"}, facts={"pages": per_page, "engine_readiness_heuristic": readiness,
                                                     "lang_invalid": lang_invalid,
                                                     "type_allowlist": "rule table " + SCHEMA_RULES_VERSION})


def _skips(headings: list) -> list:
    out, prev = [], 0
    for h in headings:
        if prev and h["level"] > prev + 1:
            out.append([prev, h["level"]])
        prev = h["level"]
    return out


def _readiness(ctx, path, f, x, h1, sd_errors, sd_invalid, entity, work=None) -> dict:
    rb = ctx.robots or {}
    parsed = rb.get("parsed")
    out = {}
    for eng in ENGINES:
        token = bots.ENGINE_SEARCH_BOT[eng]
        if parsed:
            d = parsed.decide(token, path, work)
            # R13: an undecided or approximate decision (counted work spent, file cut at the parse limit) is
            # neither a pass nor a fail
            allowed = d["allowed"] if d.get("complete", True) else None
        else:
            allowed = bool(rb.get("default_allow"))
        results = {
            "crawler_allowed": allowed, "indexable": not noindex_for(f, x, token),
            "title_present": bool(x["title"]),
            "single_h1": len(h1) == 1, "structured_data_valid": bool(x["jsonld"]) and not sd_errors and not sd_invalid,
            "entity_markup": entity, "text_in_raw_html": x["text_length"] >= RAW_TEXT_MIN,
            "lang_declared": safe_lang(x["lang"]) not in (None, "invalid"),
        }
        checks = {k: ("undecided" if v is None else "pass" if v else "fail") if eng in CHECK_APPLIES[k] else "n/a"
                  for k, v in results.items()}
        out[eng] = {"checks": checks, "passed": sum(1 for v in checks.values() if v == "pass"),
                    "applicable": sum(1 for v in checks.values() if v != "n/a"),
                    "undecided": sum(1 for v in checks.values() if v == "undecided"), "heuristic": True,
                    "calibrated": False, "crawler_token": token,
                    "label": "heuristic readiness checklist; not a calibrated score; not a visibility measurement"}
    return out
