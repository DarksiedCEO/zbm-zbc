"""Fixture audit views for the client-report tests (client_report.py): hand-built with the service's own envelope and
finding helpers so their shape is the recorded report's, with fixed ids and times so the golden files are stable.

  full()           every section checked, findings in each, Search Console read, an answer engine sampled
  not_connected()  nothing connected: no fetcher, Search Console NOT_CONNECTED, no prompt set, Naomi NOT_CONNECTED
  empty()          every crawl section checked with no findings; Search Console unbound; engines NOT_CONNECTED
  hostile()        crawled text with HTML / Markdown injection, personal data and lookalike tricks in every field the
                   renderer reads; prompt and answer needles that must never appear
  long()           the bounds: 25 long paths, hundreds of findings, very long strings
"""

from __future__ import annotations

import copy

from envelope import envelope, finding, observed

AID = "seo-aud-" + "a" * 40
RSHA = "b" * 64
T_START, T_END = "2026-10-24T09:00:00Z", "2026-10-24T09:02:30Z"
PS = {"prompt_set_id": "seo-pst-" + "c" * 40, "name": "core", "version": 2, "sha256": "d" * 64}
TIERS = {"robots": 1, "sitemap": 1, "llms_txt": 7, "page": 7, "structured_data": 7, "ai_probe": 7, "entity_record": 180}
PROMPT_NEEDLE = "PROMPTNEEDLE who is the best rug cleaner"
ANSWER_NEEDLE = "ANSWERNEEDLE"
QUERY_NEEDLE = "QUERYNEEDLE"
HOSTILE = ("<script>alert(1)</script>](javascript:alert(1)) **bold** <img src=x onerror=alert(1)> | --- | "
           "[x](http://evil.test) ![i](http://evil.test/i.png) <!-- c --> # heading `tick` jane.doe@example.com")
PII = ("jane.doe@example.com", "555-010-0199", "5550100199")


def _view(domain: str, paths: list, agents: list, *, scheme: str = "https", search=None, not_connected=(),
          limitations=None, outcome: str = "PARTIAL", quarantined=()) -> dict:
    report = {
        "report_version": "seo-audit-report/1", "audit_id": AID, "tenant_id": "acme", "domain": domain,
        "scheme": scheme, "paths": list(paths), "started_at": T_START, "finished_at": T_END, "outcome": outcome,
        "agents": agents, "not_connected": sorted(not_connected),
        "limitations": limitations if limitations is not None else [
            "Wave 1 reads public pages only; nothing here is paired with traffic, conversions or revenue (credit is "
            "not causation).", "No single visibility score is produced."],
        "data_hygiene": {"quarantined": list(quarantined), "observations": [], "refresh_tiers_days": dict(TIERS)},
        "search_performance": search,
        "versions": {"bot_families": "2026-10-10.1", "schema_rules": "2026-10-09.1", "report": "seo-audit-report/1"},
    }
    return {"audit_id": AID, "tenant_id": "acme", "status": "completed", "report_sha256": RSHA, "report": report,
            "domain": domain, "completed_at": T_END}


def _gsc_ok(prop="sc-domain:acme.test", expired=False) -> tuple[dict, dict]:
    section = {
        "property": prop,
        "window": {"start": "2026-09-24", "end": "2026-10-21", "days": 28, "timezone": "America/Los_Angeles",
                   "data_state": "final", "search_type": "web", "lag_days": 3},
        "totals": {"clicks": 140, "impressions": 1400, "ctr": 0.1, "position": 7.5, "aggregation": "byProperty"},
        "by_query_class": {"classes": {"pii_suspect": {"clicks": 2, "impressions": 5, "ctr": 0.4, "position": 1.0},
                                       "brand": {"clicks": 20, "impressions": 100, "ctr": 0.2, "position": 2.0},
                                       "question": {"clicks": 10, "impressions": 150, "ctr": 0.0667, "position": 8.0},
                                       "local": {"clicks": 5, "impressions": 60, "ctr": 0.0833, "position": 5.0},
                                       "commercial": {"clicks": 0, "impressions": 0, "ctr": None, "position": None},
                                       "other": {"clicks": 3, "impressions": 40, "ctr": 0.075, "position": 11.0}},
                           "unattributed": {"clicks": 100, "impressions": 1045, "why": "anonymised"},
                           "query_text_kept": False},
        "by_page_template": {"templates": [{"template": "/blog/{slug}", "clicks": 40, "impressions": 500, "ctr": 0.08,
                                            "position": 6.0},
                                           {"template": "{other}", "clicks": 3, "impressions": 30, "ctr": 0.1,
                                            "position": 9.0}]},
        "evidence": {"evidence_class": "OBSERVED"}, "headline_score": None,
    }
    if expired:
        section = {k: section[k] for k in ("property", "window", "totals")} | {"expired": True}
    env = envelope("osei", "search_performance", "OK", [], methodology="Search Analytics for the bound property.",
                   limitations=["Google returns top rows only."], facts={"state": "OK", "requests": 3})
    return env, {"outcome": "OK", "reason": None, "read_started": True, "section": section}


def _gsc_off(reason="GSC_NOT_CONNECTED") -> tuple[dict, dict]:
    env = envelope("osei", "search_performance", "NOT_CONNECTED", [], reason=reason,
                   methodology="Search Analytics for the bound property.", not_connected={"first_party:search_console"})
    return env, {"outcome": "NOT_CONNECTED", "reason": reason, "read_started": False, "section": None}


def _engine(name="perplexity", prompts=2, prompt_text=PROMPT_NEEDLE) -> dict:
    out = []
    for i in range(prompts):
        out.append({"prompt_index": i, "prompt": observed(f"{prompt_text} {i}", 500), "samples": 5, "answers": 5,
                    "refusals": 0, "errors": 0, "class": "OPPORTUNITY" if i else "STRENGTH",
                    "answers_sha256": ["e" * 64], "other_sources": [{"host": "yelp.com", "answers": 3,
                                                                     "competitor": False}]})
    return {"engine": name, "outcome": "OK", "reason": None, "prompts": out, "prompts_not_asked": 0,
            "model_versions": ["sonar@?"], "usage": {"spend_usd": "0.25"}, "drift": {}}


def _naomi() -> dict:
    return envelope("naomi", "prompt_volume", "NOT_CONNECTED", [], methodology="no volume source",
                    not_connected={"prompt_volume"}, reason="no prompt-volume source is connected")


def full() -> dict:
    paths = ["/", "/about", "/pricing"]
    u = "https://acme.test"
    selene = envelope("selene", "crawlability", "OK", [
        finding("ROBOTS_BLOCKS_SEARCH_CRAWLER", "high", "measured", "ACT", url=u + "/robots.txt", capability="P2",
                detail={"token": "Bingbot", "blocked_paths": ["/pricing"], "rules": ["Disallow: /pricing"]}),
        finding("ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER", "info", "measured", "WATCH", url=u + "/robots.txt",
                capability="P2", detail={"token": "GPTBot", "purpose": "ai_training", "blocked_paths": ["/"],
                                         "rules": ["Disallow: /"]}),
        finding("REDIRECT_CHAIN", "medium", "measured", "ACT", url=u + "/about",
                detail={"statuses": [301, 302], "hops": 2, "temporary": True, "final_url": u + "/about-us"}),
        finding("NOINDEX", "medium", "measured", "TEST", url=u + "/pricing", detail={"x_robots_tag": "noindex"}),
        finding("CANONICAL_MISSING", "low", "measured", "WATCH", url=u + "/"),
        finding("CANONICAL_MISSING", "low", "measured", "WATCH", url=u + "/pricing"),
    ], methodology="Fetched robots.txt and each audited page (bot family list 2026-10-10.1).",
        limitations=["Observed from one network location at one time."], facts={"pages_fetched": 3})
    delia = envelope("delia", "sitemaps_and_llms_txt", "OK", [
        finding("SITEMAP_LOC_OTHER_HOST", "medium", "measured", "ACT", capability="P3",
                detail={"count": 2, "examples": ["https://cdn.example/a", "https://cdn.example/b"]}),
        finding("SITEMAP_LASTMOD_ALL_IDENTICAL", "low", "inferred", "TEST", capability="P3",
                detail={"count": 40, "value": "2026-01-01"}),
        finding("LLMS_TXT_ABSENT", "info", "measured", "WATCH", url=u + "/llms.txt", capability="P3",
                detail={"status": 404}),
    ], methodology="Sitemaps from robots.txt Sitemap lines.", limitations=["llms.txt is non-standard."])
    roman = envelope("roman", "content_structure", "OK", [
        finding("TITLE_LENGTH", "low", "measured", "TEST", url=u + "/", capability="P3",
                detail={"length": 80, "heuristic_range": [15, 65]}),
        finding("STRUCTURED_DATA_ERRORS", "medium", "measured", "ACT", url=u + "/about", capability="P3",
                detail={"issues": [{"code": "REQUIRED_MISSING", "level": "error", "type": "LocalBusiness",
                                    "property": "address"}]}),
        finding("H1_COUNT", "medium", "measured", "ACT", url=u + "/pricing", capability="P3", detail={"h1_count": 0}),
    ], methodology="Raw HTML parsed; structured data validated (rules 2026-10-09.1).",
        limitations=["Heuristic checklist, uncalibrated."])
    entity = envelope("entity_check", "on_site_entity_consistency", "OK", [
        finding("ENTITY_FIELD_MISMATCH", "high", "measured", "ACT", url=u + "/", capability="P6",
                detail={"field": "telephone", "canonical": "(562) 248-6617",
                        "on_site": observed("(562) 555-0100")}),
    ], methodology="Organization / LocalBusiness JSON-LD compared with the canonical entity record.",
        limitations=["Only the audited pages were read."])
    callum = envelope("callum", "citation_sources", "OK", [
        finding("CITATION_STRENGTH", "info", "measured", "WATCH", capability="P4",
                detail={"engine": "perplexity", "prompt_index": 0, "usable_answers": 5, "citation_rate": 0.8,
                        "citation_ci95": [0.3755, 0.9638], "mention_rate": 1.0, "mention_ci95": [0.5655, 1.0]}),
        finding("CITATION_OPPORTUNITY", "medium", "measured", "TEST", capability="P4",
                detail={"engine": "perplexity", "prompt_index": 1, "usable_answers": 5, "citation_rate": 0.0,
                        "other_sources": [{"host": "yelp.com", "answers": 3, "competitor": False},
                                          {"host": "rival.test", "answers": 2, "competitor": True}]}),
        finding("ENGINE_COST_ABOVE_RESERVE", "medium", "measured", "WATCH", capability="P4",
                detail={"engine": "perplexity", "usage": {"spend_usd": "9.99"}}),
    ], methodology="Each prompt of prompt set core v2 sent 5 times per engine.",
        limitations=["One sample of N answers at one time from one account."],
        not_connected={"answer_engine:openai"},
        facts={"prompt_set": PS, "samples_per_prompt": 5, "engines": {"perplexity": _engine()}})
    gsc_env, gsc_block = _gsc_ok()
    hygiene = envelope("osei", "data_hygiene", "OK", [], methodology="quarantine and freshness")
    return _view("acme.test", paths, [selene, delia, roman, entity, callum, _naomi(), gsc_env, hygiene],
                 search=gsc_block, not_connected=("render", "prompt_volume", "answer_engine:openai",
                                                  "first_party:analytics", "first_party:crm"),
                 quarantined=[{"kind": "sitemap", "reason": "XML_DOCTYPE_REFUSED", "url": "https://acme.test/s.xml",
                               "bytes": 10, "sha256": "f" * 64}])


def not_connected() -> dict:
    selene = envelope("selene", "crawlability", "NOT_CONNECTED", [], methodology="no fetcher is wired",
                      not_connected={"fetch"}, reason="the web fetcher port is not connected")
    entity = envelope("entity_check", "on_site_entity_consistency", "INSUFFICIENT_EVIDENCE", [],
                      methodology="Organization / LocalBusiness JSON-LD compared with the canonical entity record.")
    callum = envelope("callum", "citation_sources", "INSUFFICIENT_EVIDENCE", [], reason="NO_PROMPT_SET",
                      methodology="No prompt set was given for this audit; no engine was asked anything.")
    gsc_env, gsc_block = _gsc_off()
    return _view("acme.test", ["/"], [selene, entity, callum, _naomi(), gsc_env], search=gsc_block,
                 outcome="NOT_CONNECTED", not_connected=("fetch", "render", "prompt_volume",
                                                         "first_party:search_console", "answer_engine:perplexity"))


def empty() -> dict:
    ok = dict(methodology="checked", limitations=["One location, one time."])
    agents = [envelope("selene", "crawlability", "OK", [], **ok),
              envelope("delia", "sitemaps_and_llms_txt", "OK", [], **ok),
              envelope("roman", "content_structure", "OK", [], **ok),
              envelope("entity_check", "on_site_entity_consistency", "OK", [], **ok),
              envelope("callum", "citation_sources", "NOT_CONNECTED", [], methodology="no engine connected",
                       not_connected={"answer_engine:perplexity"}, reason="no answer-engine port is connected",
                       facts={"prompt_set": PS, "engines": {"perplexity": "NOT_CONNECTED"}}),
              _naomi()]
    gsc_env, gsc_block = _gsc_off("GSC_PROPERTY_UNBOUND")
    return _view("acme.test", ["/"], agents + [gsc_env], search=gsc_block, outcome="PARTIAL",
                 not_connected=("render", "prompt_volume", "answer_engine:perplexity", "first_party:search_console"))


def hostile() -> dict:
    u = "https://acme.test"
    evil_url = "https://acme.test/<script>alert(1)</script>/jane.doe@example.com/555-010-0199?t=" + HOSTILE
    selene = envelope("selene", "crawlability", "OK", [
        finding("CANONICAL_OTHER_HOST", "medium", "measured", "TEST", url=u + "/",
                detail={"canonical": "http://evil.test/[x](javascript:alert(1))"}),
        finding("CANONICAL_MULTIPLE", "high", "measured", "ACT", url=u + "/",
                detail={"count": 3, "invalid": 1, "canonicals": [evil_url, "javascript:alert(1)",
                                                                 "https://xn--80ak6aa92e.com/<b>"]}),
        finding("REDIRECTED", "low", "measured", "WATCH", url=evil_url,
                detail={"statuses": [301], "temporary": False, "final_url": "data:text/html,<script>x</script>"}),
        finding("ROBOTS_BLOCKS_SEARCH_CRAWLER", "critical", "measured", "ACT", url=u + "/robots.txt",
                capability="P2", detail={"token": "Googlebot<script>", "blocked_paths": ["/", evil_url],
                                         "rules": [HOSTILE, "Disallow: /u/jane.doe@example.com"]}),
        finding("PAGE_NOT_HTML", "low", "measured", "WATCH", url=u + "/", detail={"content_type": HOSTILE}),
        finding("SOME_FUTURE_RULE", "low", "measured", "WATCH", url=u + "/", detail={"secret": HOSTILE, "n": 3}),
        finding("ESTIMATED_THING", "low", "estimated", "TEST", url=u + "/", detail={}),
    ], methodology="Fetched robots.txt " + HOSTILE, limitations=[HOSTILE])
    delia = envelope("delia", "sitemaps_and_llms_txt", "OK", [
        finding("SITEMAP_LISTS_ROBOTS_BLOCKED_URL", "medium", "measured", "ACT", capability="P3",
                detail={"count": 1, "examples": [evil_url]}),
        finding("SITEMAP_NAMESPACE", "low", "measured", "ACT", url=evil_url, capability="P3",
                detail={"namespace": HOSTILE}),
        finding("LLMS_TXT_FORMAT", "low", "measured", "TEST", url=u + "/llms.txt", capability="P3",
                detail={"issues": [HOSTILE, {"code": "<b>x</b>"}]}),
    ], methodology="Sitemaps.")
    entity = envelope("entity_check", "on_site_entity_consistency", "OK", [
        finding("ENTITY_FIELD_MISMATCH", "high", "measured", "ACT", url=u + "/", capability="P6",
                detail={"field": "telephone", "canonical": "555-010-0199", "on_site": observed(HOSTILE)}),
    ], methodology="entity")
    eng = _engine("perplexity", 1)
    eng["prompts"][0]["other_sources"] = [{"host": "evil.test<script>", "answers": 1}]
    callum = envelope("callum", "citation_sources", "OK", [
        finding("CITATION_OPPORTUNITY", "medium", "measured", "TEST", capability="P4",
                detail={"engine": "perplexity", "prompt_index": 0, "usable_answers": 5,
                        "other_sources": [{"host": "evil.test<script>", "answers": 1},
                                          {"host": "jane.doe@example.com", "answers": 1}],
                        "answer": ANSWER_NEEDLE, "prompt": PROMPT_NEEDLE}),
    ], methodology="Each prompt of prompt set core<script> v2 sent 5 times per engine.",
        facts={"prompt_set": PS, "samples_per_prompt": 5, "engines": {"perplexity": eng, "<b>": {"outcome": "OK"}}})
    gsc_env, gsc_block = _gsc_ok()
    gsc_block["section"]["by_page_template"]["templates"].insert(
        0, {"template": "/u/jane.doe@example.com/<script>", "clicks": 1, "impressions": 2, "ctr": 0.5})
    gsc_block["section"]["by_query_class"]["sample"] = QUERY_NEEDLE
    return _view("acme.test", ["/"], [selene, delia, entity, callum, _naomi(), gsc_env], search=gsc_block,
                 not_connected=("render", HOSTILE), limitations=[HOSTILE])


def long() -> dict:
    paths = ["/" + "p" * 480 + f"/{i:02d}" for i in range(25)]
    u = "https://acme.test"
    findings = []
    for i in range(400):
        p = paths[i % 25]
        findings.append(finding("HEADING_LEVEL_SKIP", "low", "measured", "TEST", url=u + p, capability="P3",
                                detail={"skips": ["h1>h3 " + "x" * 5000] * 50}))
    roman = envelope("roman", "content_structure", "OK", findings + [
        finding("TITLE_MISSING", "high", "measured", "ACT", url=u + "/" + "q" * 9000, capability="P3")],
        methodology="m" * 10_000, limitations=["l" * 10_000] * 100)
    delia = envelope("delia", "sitemaps_and_llms_txt", "OK", [
        finding("SITEMAP_LOC_OTHER_HOST", "medium", "measured", "ACT", capability="P3",
                detail={"count": 100_000, "examples": ["https://" + "h" * 60 + f".example/{'z' * 4000}"] * 500}),
    ], methodology="Sitemaps.")
    return _view("acme.test", paths, [roman, delia], not_connected=("render",))


def with_expired_gsc() -> dict:
    v = full()
    env, block = _gsc_ok(expired=True)
    v["report"]["search_performance"] = block
    return v


FIXTURES = {"full": full, "not_connected": not_connected, "empty": empty, "hostile": hostile, "long": long}


def fresh(name: str) -> dict:
    return copy.deepcopy(FIXTURES[name]())
