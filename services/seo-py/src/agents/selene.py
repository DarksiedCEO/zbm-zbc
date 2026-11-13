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

from agents import bots, host_of
from envelope import envelope, finding
from primitives import Killed
from primitives import diff as diff_mod
from primitives import render as render_mod
from primitives import robots as robots_mod
from primitives.parse import parse_html
from primitives.safe_url import robots_directives, safe_ctype, safe_path, safe_url, safe_urls, safe_x_robots

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
# AEGIS Wave-1 M-S1, round 2 M2: the bot family x path robots.txt matrix of one run is bounded by this COUNTED work
# (primitives/robots.py ``Work``, no clock); decisions left once it is spent are reported undecided
# (ROBOTS_CHECK_INCOMPLETE, outcome PARTIAL), never as allowed or blocked.
MATRIX_WORK = robots_mod.STEP_WORK_UNITS


def noindex_for(fetch, extract: Optional[dict], token: str) -> bool:
    """Whether the page asks ``token``'s crawler not to index it (AEGIS Wave-1 S-S2): an X-Robots-Tag directive
    applies to every crawler unless it follows a ``<bot>:`` prefix, and then only to that bot; a meta robots tag
    applies to every crawler when its name is ``robots``, else only to the crawler it names."""
    t = token.lower()
    for bot, d in robots_directives(fetch.headers.get("x-robots-tag")):
        if d in ("noindex", "none") and (bot is None or bot == t):
            return True
    if extract:
        for name, v in extract["robots_meta"].items():
            if name in ("robots", t) and ("noindex" in v or "none" in v):
                return True
    return False


def noindex_engines(fetch, extract: Optional[dict]) -> list:
    """The answer / search engines whose crawler the page asks not to index it (bots.ENGINE_SEARCH_BOT)."""
    return [e for e, tok in bots.ENGINE_SEARCH_BOT.items() if noindex_for(fetch, extract, tok)]


def noindex_bots(fetch, extract: Optional[dict]) -> list:
    """Every bot family token (agents/bots.py) the page asks not to index it, each scoped to that bot (round 2 NIT:
    a ``GPTBot`` meta noindex is reported for GPTBot, not for every engine and not nowhere)."""
    return [f["token"] for f in bots.FAMILIES if noindex_for(fetch, extract, f["token"])]


def _safe_meta(robots_meta: dict) -> dict:
    return {name: [d for _, d in robots_directives(", ".join(v))] for name, v in robots_meta.items()}


def _safe_rule(rule: Optional[str]) -> Optional[str]:
    """A robots.txt rule (crawled text; always a rule as written in the file) as stored: its kind and its templated
    pattern (primitives/safe_url.py)."""
    if not rule:
        return rule
    kind, _, pat = rule.partition(": ")
    if not pat:
        return kind + ": "
    lead = pat.startswith("/")
    t = safe_path(pat if lead else "/" + pat) or ""
    return f"{kind}: {t if lead else t[1:]}"


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
        if rb["status_class"] == "too_large":
            ctx.osei.quarantine("robots", "TOO_LARGE", ctx.origin + "/robots.txt", None, partial=rb.get("partial"))
        sm = rb["parsed"].sitemaps if rb["parsed"] else []
        facts["robots"] = {"status_class": rb["status_class"], "fetch": rb["fetch"],
                           "text_sha256": rb.get("text_sha256"),
                           "sitemaps_declared": safe_urls(sm, 50), "sitemaps_declared_count": len(sm)}
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
                ctx.osei.quarantine("page", "TOO_LARGE", url, None, partial=f.partial)
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
                ctx.osei.observed("page_browser_ua", url, human.state)
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
        states = {p["fetch"].state for p in pages.values()}
        # round 2 M3: this service's own refusal (counted work spent) is never "blocked"
        outcome = "BLOCKED" if states == {"BLOCKED_BY_ROBOTS"} else \
            "INSUFFICIENT_EVIDENCE" if states and states <= {"BLOCKED_BY_ROBOTS", "ROBOTS_CHECK_INCOMPLETE"} else "FAILED"
    else:
        outcome = "OK" if fetched == len(ctx.paths) and not facts.get("robots_matrix_undecided") else "PARTIAL"
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
        # too_large: kept from round 1 for a body this service could not read at all; robots.txt is now read up to
        # the RFC 9309 parse limit (fetch ``truncate_at``), so a large file is truncated, not too_large
        out.append(finding("ROBOTS_UNREACHABLE", "critical", "measured", "ACT", url=url, capability="P2",
                           detail={"fetch_state": rb["fetch"]["state"], "status": rb["fetch"]["status"],
                                   "effect": "crawlers following RFC 9309 treat the whole site as disallowed"}))
        return out
    if rb["status_class"] == "unavailable":
        out.append(finding("ROBOTS_ABSENT", "info", "measured", "WATCH", url=url, capability="P2",
                           detail={"status": rb["fetch"]["status"], "effect": "no rules: everything allowed"}))
    parsed = rb["parsed"]
    matrix = {}
    work = robots_mod.Work(MATRIX_WORK)
    approximate = bool(parsed is not None and parsed.truncated)
    for fam in bots.FAMILIES:
        per_path = {}
        for path in ["/"] + [p for p in ctx.paths if p != "/"]:
            d = parsed.decide(fam["token"], path, work) if parsed else {"allowed": rb["default_allow"],
                                                                        "group": "none", "rule": None}
            per_path[path] = d
        matrix[fam["token"]] = {"purpose": fam["purpose"], "operator": fam["operator"],
                                "paths": {p: {"allowed": d["allowed"], "group": d["group"]} for p, d in
                                          per_path.items()}}
        # S11: an undecided path (None) is never counted as blocked
        blocked = [p for p, d in per_path.items() if d["allowed"] is False]
        if not blocked:
            continue
        rules = sorted({_safe_rule(d["rule"]) for d in per_path.values() if d["rule"] and d["allowed"] is False})[:5]
        if approximate:
            # round 2 M3: a decision from a file cut at the parse limit is approximate: never a measured block
            out.append(finding("ROBOTS_BLOCK_APPROXIMATE", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                               capability="P2",
                               detail={"token": fam["token"], "blocked_paths": blocked[:20], "rules": rules,
                                       "cap": "robots_bytes", "cap_value": robots_mod.MAX_BYTES,
                                       "note": "robots.txt is longer than the parse limit; rules past it were not "
                                               "read, so this decision may differ for a crawler that reads more"}))
        elif fam["token"] in bots.SEARCH_TOKENS:
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
    facts["robots_matrix_work"] = {"units": work.units, "limit": work.limit}
    if work.undecided:
        facts["robots_matrix_undecided"] = work.undecided
        out.append(finding("ROBOTS_CHECK_INCOMPLETE", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           capability="P2", detail={"undecided": work.undecided, "cap": "step_work_units",
                                                    "cap_value": work.limit,
                                                    "note": "robots.txt decisions left undecided when this run's "
                                                            "counted work was spent (no claim either way)"}))
    if parsed is not None and parsed.lines_ignored:
        out.append(finding("ROBOTS_LINES_IGNORED", "low", "measured", "WATCH", url=url, capability="P2",
                           detail={"lines_ignored": parsed.lines_ignored}))
    if parsed is not None and parsed.limits_exceeded():
        # round 2 NIT: a file over the parse limit is never parsed silently
        out.append(finding("ROBOTS_LIMITS_EXCEEDED", "medium", "measured", "WATCH", url=url, capability="P2",
                           detail={**parsed.limits(), "effect": "robots.txt is longer than 500 KiB: it was read up "
                                   "to that limit (RFC 9309 2.5; Google documents the same limit) and rules past it "
                                   "were not read; decisions from it are reported as approximate"}))
        facts["robots"]["truncated"] = True
    if parsed is not None and parsed.bom_stripped:
        facts["robots"]["bom_stripped"] = True
    return out


def _page_findings(ctx, path: str, f, extract: Optional[dict], canon_checked: int) -> tuple[list, int]:
    url = ctx.url(path)
    out = []
    if f.state == "BLOCKED_BY_ROBOTS":
        why = (f.robots or {}).get("reason")
        note = "robots.txt could not be fetched (RFC 9309: everything disallowed); the page was not read" \
            if why == "unreachable" else "robots.txt disallows ZBM-SEO-Audit here; the page was not read"
        out.append(finding("PAGE_BLOCKED_FOR_OUR_CRAWLER", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           detail={"reason": why or "rule", "note": note}))
        return out, canon_checked
    if f.state == "ROBOTS_CHECK_INCOMPLETE":
        # round 2 M2/M3: this service's own refusal, with its own code; it says nothing about robots.txt
        out.append(finding("PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE", "info", "measured", "INSUFFICIENT_EVIDENCE",
                           url=url, detail={"reason": (f.robots or {}).get("reason"),
                                            "note": "this service could not complete its own robots.txt check "
                                                    "within its bounds, so it did not read the page; no claim "
                                                    "about robots.txt"}))
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
                           detail={"statuses": chain, "hops": len(chain), "final_url": safe_url(f.final_url),
                                   "temporary": any(s in (302, 303, 307) for s in chain)}))
    if f.content_type not in ("text/html", "application/xhtml+xml"):
        out.append(finding("PAGE_NOT_HTML", "low", "measured", "WATCH", url=url,
                           detail={"content_type": safe_ctype(f.content_type)}))
        return out, canon_checked
    engines = noindex_engines(f, extract)
    noindex = bool(engines)
    nbots = noindex_bots(f, extract)
    if noindex:
        out.append(finding("NOINDEX", "high" if path == "/" else "medium", "measured", "TEST", url=url,
                           detail={"engines": engines, "bots": nbots,
                                   "x_robots_tag": safe_x_robots(f.headers.get("x-robots-tag")),
                                   "robots_meta": _safe_meta(extract["robots_meta"]) if extract else {}}))
    elif nbots:
        # round 2 NIT: a noindex aimed only at crawlers that are no engine's search crawler (GPTBot, CCBot, ...):
        # reported, scoped to those bots
        out.append(finding("NOINDEX_FOR_BOT", "info", "measured", "WATCH", url=url,
                           detail={"bots": nbots, "x_robots_tag": safe_x_robots(f.headers.get("x-robots-tag")),
                                   "robots_meta": _safe_meta(extract["robots_meta"]) if extract else {},
                                   "note": "applies only to the named crawlers; may be an intentional policy"}))
    if extract is None:
        return out, canon_checked
    canons = extract["canonicals"]
    invalid = extract.get("canonicals_invalid") or 0
    total = (extract.get("canonicals_valid", len(canons)) or 0) + invalid
    if invalid:
        # WR-F009: a canonical that is not an http(s) URL this service can read (javascript:, a broken IPv6 literal);
        # reported whenever there is one (AEGIS SEO Wave 3 M3), beside the other canonical findings
        out.append(finding("CANONICAL_INVALID", "medium", "measured", "ACT", url=url, detail={"count": invalid}))
    if total > 1:
        # M3: an invalid canonical still counts: one valid plus one invalid is two canonicals
        out.append(finding("CANONICAL_MULTIPLE", "high", "measured", "ACT", url=url,
                           detail={"count": total, "invalid": invalid, "canonicals": safe_urls(canons, 10)}))
    elif len(canons) == 1:
        c = canons[0]
        host = host_of(c)
        if not ctx.same_site(host):
            out.append(finding("CANONICAL_OTHER_HOST", "medium", "measured", "TEST", url=url,
                               detail={"canonical": safe_url(c)}))
        if noindex and c.rstrip("/") != (f.final_url or url).rstrip("/"):
            out.append(finding("NOINDEX_CANONICAL_CONFLICT", "high", "measured", "ACT", url=url,
                               detail={"canonical": safe_url(c), "note": "noindex plus a canonical pointing "
                                                                         "elsewhere sends contradictory signals"}))
        if c.rstrip("/") != (f.final_url or url).rstrip("/") and ctx.same_site(host) \
                and canon_checked < CANONICAL_CHECK_MAX:
            canon_checked += 1
            cf = ctx.fetcher.fetch(c, robots_cache=ctx.robots_cache, guard=ctx.guard)
            if cf.state == "KILLED":
                raise Killed(cf.detail or "KILLED")
            ctx.osei.observed("page_canonical_target", c, cf.state)
            if not cf.ok:
                # round 2: our own fetch failed or was refused: nothing measured about the target
                out.append(finding("CANONICAL_TARGET_UNCHECKED", "info", "measured", "INSUFFICIENT_EVIDENCE",
                                   url=url, detail={"canonical": safe_url(c), "fetch_state": cf.state}))
            elif cf.status != 200 or cf.redirects:
                out.append(finding("CANONICAL_TARGET_NOT_200", "high", "measured", "ACT", url=url,
                                   detail={"canonical": safe_url(c), "fetch_state": cf.state, "status": cf.status,
                                           "redirected": bool(cf.redirects)}))
    elif not invalid:
        out.append(finding("CANONICAL_MISSING", "low", "measured", "WATCH", url=url))
    if extract.get("parse_error"):
        out.append(finding("HTML_PARSE_ERROR", "low", "measured", "WATCH", url=url,
                           detail={"error": extract["parse_error"]}))
    return out, canon_checked
