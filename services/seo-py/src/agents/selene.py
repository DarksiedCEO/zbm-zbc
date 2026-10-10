"""
Selene — crawlability (P1 technical, P2 crawler access). One task: can the engines' crawlers reach and index the
audited pages, as observed from outside?

Observes (all ``measured`` from this service's own fetches; nothing estimated):
  - robots.txt: fetched per RFC 9309; each bot family (agents/bots.py, versioned) evaluated against "/" and every
    audited path. A blocked AI crawler is reported as a POLICY FACT (decision WATCH: blocking may be intentional),
    a blocked search crawler as a defect;
  - each audited page: status code, redirect chain, X-Robots-Tag and meta robots, canonical (count, host, target
    status), noindex / canonical conflicts, content type;
  - bot-vs-browser access differential (UA-based only; see primitives/diff.py): the browser-identity fetch is made
    only where robots.txt allows this service's own crawler, and honours it;
  - fetch / render state (render is NOT_CONNECTED in Wave 1: raw HTML only).
Selene returns the page extracts too, so Delia, Roman and the entity check reuse them (one fetch per page).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from agents import bots
from envelope import envelope, finding
from primitives import Killed
from primitives import diff as diff_mod
from primitives import render as render_mod
from primitives.parse import parse_html

AGENT, TASK = "selene", "crawlability"
METHODOLOGY = ("Fetched robots.txt (RFC 9309: 4xx = no rules, 5xx/unreachable = everything disallowed) and each "
               "audited page with this service's identified crawler (ZBM-SEO-Audit), honouring robots.txt for its own "
               "token. Each bot family token was evaluated with RFC 9309 longest-match rules against '/' and every "
               "audited path; bot family list version {v}. Where robots.txt allows this service's crawler, pages were "
               "also fetched once with a browser User-Agent (honouring robots.txt for our crawler) to compare what is "
               "served (user-agent differential only). Raw HTML only; no JavaScript was executed.")
LIMITS = ["Observed from one network location at one time; a CDN or firewall may answer other clients differently.",
          "Bot access by VERIFIED IP range (how the major operators identify their crawlers) cannot be observed by "
          "changing the User-Agent; an IP-based block or cloak is not detected.",
          "robots.txt states a request to crawlers; it does not show whether any operator honours it.",
          "The bot family list is versioned data and is not complete.",
          "Rendering is not connected: content inserted by JavaScript was not seen."]
HTML_TYPES = ("text/html", "application/xhtml+xml")
CANONICAL_CHECK_MAX = 5


def _noindex(fetch, extract: Optional[dict]) -> bool:
    xr = (fetch.headers.get("x-robots-tag") or "").lower()
    if "noindex" in xr or "none" in [x.strip() for x in xr.split(",")]:
        return True
    if extract:
        for v in extract["robots_meta"].values():
            if "noindex" in v or "none" in v:
                return True
    return False


def run(ctx) -> tuple[dict, dict]:
    """Returns (envelope, pages) — ``pages``: path -> {"fetch": FetchResult, "extract": dict | None,
    "render": dict}."""
    findings, facts, pages = [], {"bot_families_version": bots.VERSION}, {}
    not_connected = set()
    try:
        ctx.guard(capability="fetch", provider="web")
        rb = ctx.fetcher.robots(ctx.origin, guard=ctx.guard)
        ctx.robots = rb
        ctx.robots_cache[_origin_key(ctx.origin)] = rb
        ctx.osei.observed("robots", ctx.origin + "/robots.txt", rb["status_class"])
        if rb["fetch"]["state"] == "KILLED":
            raise Killed(rb["fetch"]["detail"] or "KILLED")
        facts["robots"] = {"status_class": rb["status_class"], "fetch": rb["fetch"],
                           "text_sha256": rb.get("text_sha256"),
                           "sitemaps_declared": (rb["parsed"].sitemaps[:50] if rb["parsed"] else [])}
        findings += _robots_findings(ctx, rb, facts)
        fetched = 0
        canon_checked = 0
        for path in ctx.paths:
            url = ctx.url(path)
            f = ctx.fetcher.fetch(url, robots_cache=ctx.robots_cache, guard=ctx.guard)
            if f.state == "KILLED":
                raise Killed(f.detail or "KILLED")
            ctx.osei.observed("page", url, f.state)
            extract = None
            if f.state == "TOO_LARGE":
                ctx.osei.quarantine("page", "TOO_LARGE", url, None)
            if f.ok and f.content_type in HTML_TYPES and f.body is not None:
                extract = parse_html(f.text(), f.final_url or url)
            rstate = render_mod.render_state(f, extract, ctx.renderer, ctx.guard)
            if rstate["state"] == "RAW_OK_RENDER_NOT_CONNECTED":
                not_connected.add("render")
            pages[path] = {"fetch": f, "extract": extract, "render": {k: v for k, v in rstate.items()
                                                                     if k != "rendered_extract"}}
            if f.ok:
                fetched += 1
            page_findings, canon_checked = _page_findings(ctx, path, f, extract, canon_checked)
            findings += page_findings
            if f.ok and extract is not None:
                # AEGIS L3: the browser-identity comparison honours robots.txt for OUR crawler too (it is only made
                # where our crawler may fetch, and from the same robots cache)
                human = ctx.fetcher.fetch(url, ua="human", robots_cache=ctx.robots_cache, guard=ctx.guard)
                if human.state == "KILLED":
                    raise Killed(human.detail or "KILLED")
                hx = parse_html(human.text(), human.final_url or url) if human.ok and human.content_type in \
                    HTML_TYPES else None
                ad = diff_mod.access_diff(f, human, extract, hx)
                pages[path]["access_diff"] = ad
                if ad["state"] == "BOT_DIFFERENTIAL":
                    findings.append(finding("BOT_DIFFERENTIAL", "high", "measured", "TEST", url=url,
                                            capability="P2", detail={"signals": ad["signals"]}))
        facts["pages_requested"], facts["pages_fetched"] = len(ctx.paths), fetched
    except Killed as k:
        return envelope(AGENT, TASK, "KILLED", [], methodology=METHODOLOGY.format(v=bots.VERSION),
                        limitations=LIMITS, reason=k.code, facts=facts), pages
    if fetched == 0:
        outcome = "BLOCKED" if all(p["fetch"].state == "BLOCKED_BY_ROBOTS" for p in pages.values()) else "FAILED"
    else:
        outcome = "OK" if fetched == len(ctx.paths) else "PARTIAL"
    facts["render_states"] = {p: v["render"]["state"] for p, v in pages.items()}
    return envelope(AGENT, TASK, outcome, findings, methodology=METHODOLOGY.format(v=bots.VERSION),
                    limitations=LIMITS, not_connected=not_connected, facts=facts), pages


def _origin_key(origin: str) -> str:
    p = urlsplit(origin)
    return f"{p.scheme.lower()}://{(p.netloc or '').lower()}"


def _robots_findings(ctx, rb: dict, facts: dict) -> list:
    out = []
    url = ctx.origin + "/robots.txt"
    if rb["status_class"] in ("unreachable", "too_large"):
        out.append(finding("ROBOTS_UNREACHABLE", "critical", "measured", "ACT", url=url, capability="P2",
                           detail={"fetch_state": rb["fetch"]["state"], "status": rb["fetch"]["status"],
                                   "effect": "crawlers following RFC 9309 treat the whole site as disallowed"}))
        return out
    if rb["status_class"] == "unavailable":
        out.append(finding("ROBOTS_ABSENT", "info", "measured", "WATCH", url=url, capability="P2",
                           detail={"status": rb["fetch"]["status"], "effect": "no rules: everything allowed"}))
    parsed = rb["parsed"]
    matrix = {}
    for fam in bots.FAMILIES:
        per_path = {}
        for path in ["/"] + [p for p in ctx.paths if p != "/"]:
            d = parsed.decide(fam["token"], path) if parsed else {"allowed": rb["default_allow"], "group": "none",
                                                                    "rule": None}
            per_path[path] = d
        matrix[fam["token"]] = {"purpose": fam["purpose"], "operator": fam["operator"],
                                "paths": {p: {"allowed": d["allowed"], "group": d["group"]} for p, d in
                                          per_path.items()}}
        blocked = [p for p, d in per_path.items() if not d["allowed"]]
        if not blocked:
            continue
        rules = sorted({d["rule"] for d in per_path.values() if d["rule"] and not d["allowed"]})[:5]
        if fam["token"] in bots.SEARCH_TOKENS:
            out.append(finding("ROBOTS_BLOCKS_SEARCH_CRAWLER", "critical" if "/" in blocked else "high", "measured",
                               "ACT", url=url, capability="P2",
                               detail={"token": fam["token"], "blocked_paths": blocked[:20], "rules": rules}))
        else:
            out.append(finding("ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER", "info", "measured", "WATCH", url=url,
                               capability="P2",
                               detail={"token": fam["token"], "purpose": fam["purpose"], "blocked_paths": blocked[:20],
                                       "rules": rules, "note": "may be an intentional policy; a founder / client "
                                                               "decision, not a defect"}))
    facts["robots_matrix"] = matrix
    if parsed is not None and parsed.lines_ignored:
        out.append(finding("ROBOTS_LINES_IGNORED", "low", "measured", "WATCH", url=url, capability="P2",
                           detail={"lines_ignored": parsed.lines_ignored}))
    return out


def _page_findings(ctx, path: str, f, extract: Optional[dict], canon_checked: int) -> tuple[list, int]:
    url = ctx.url(path)
    out = []
    if f.state == "BLOCKED_BY_ROBOTS":
        out.append(finding("PAGE_BLOCKED_FOR_OUR_CRAWLER", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           detail={"note": "robots.txt disallows ZBM-SEO-Audit here; the page was not read"}))
        return out, canon_checked
    if not f.ok:
        out.append(finding("PAGE_UNREACHABLE", "high", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           detail={"fetch_state": f.state, "detail": f.detail}))
        return out, canon_checked
    if f.status >= 400:
        out.append(finding("PAGE_HTTP_ERROR", "high" if f.status >= 500 else "medium", "measured", "ACT", url=url,
                           detail={"status": f.status}))
    if f.redirects:
        chain = [r["status"] for r in f.redirects]
        sev = "medium" if len(chain) > 1 else "low"
        out.append(finding("REDIRECT_CHAIN" if len(chain) > 1 else "REDIRECTED", sev, "measured",
                           "ACT" if len(chain) > 1 else "WATCH", url=url,
                           detail={"statuses": chain, "hops": len(chain), "final_url": f.final_url,
                                   "temporary": any(s in (302, 303, 307) for s in chain)}))
    if f.content_type not in ("text/html", "application/xhtml+xml"):
        out.append(finding("PAGE_NOT_HTML", "low", "measured", "WATCH", url=url,
                           detail={"content_type": f.content_type}))
        return out, canon_checked
    noindex = _noindex(f, extract)
    if noindex:
        out.append(finding("NOINDEX", "high" if path == "/" else "medium", "measured", "TEST", url=url,
                           detail={"x_robots_tag": f.headers.get("x-robots-tag"),
                                   "robots_meta": extract["robots_meta"] if extract else {}}))
    if extract is None:
        return out, canon_checked
    canons = extract["canonicals"]
    if len(canons) > 1:
        out.append(finding("CANONICAL_MULTIPLE", "high", "measured", "ACT", url=url,
                           detail={"count": len(canons), "canonicals": canons[:10]}))
    elif len(canons) == 1:
        c = canons[0]
        host = urlsplit(c).hostname
        if not ctx.same_site(host):
            out.append(finding("CANONICAL_OTHER_HOST", "medium", "measured", "TEST", url=url,
                               detail={"canonical": c}))
        if noindex and c.rstrip("/") != (f.final_url or url).rstrip("/"):
            out.append(finding("NOINDEX_CANONICAL_CONFLICT", "high", "measured", "ACT", url=url,
                               detail={"canonical": c, "note": "noindex plus a canonical pointing elsewhere sends "
                                                               "contradictory signals"}))
        if c.rstrip("/") != (f.final_url or url).rstrip("/") and ctx.same_site(host) \
                and canon_checked < CANONICAL_CHECK_MAX:
            canon_checked += 1
            cf = ctx.fetcher.fetch(c, robots_cache=ctx.robots_cache, guard=ctx.guard)
            if cf.state == "KILLED":
                raise Killed(cf.detail or "KILLED")
            if not cf.ok or cf.status != 200 or cf.redirects:
                out.append(finding("CANONICAL_TARGET_NOT_200", "high", "measured", "ACT", url=url,
                                   detail={"canonical": c, "fetch_state": cf.state, "status": cf.status,
                                           "redirected": bool(cf.redirects)}))
    else:
        out.append(finding("CANONICAL_MISSING", "low", "measured", "WATCH", url=url))
    if extract.get("parse_error"):
        out.append(finding("HTML_PARSE_ERROR", "low", "measured", "WATCH", url=url,
                           detail={"error": extract["parse_error"]}))
    return out, canon_checked
