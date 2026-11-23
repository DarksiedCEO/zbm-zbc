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

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

from agents import delia as delia_mod
from agents import CRAWL_DELAY_CAP, DEADLINE_REASON, GONE_STATUSES, DeadlineReached, bots, host_of, split_url
from agents.prompt_guard import personal_data
from envelope import envelope, finding
from primitives import Killed
from primitives import diff as diff_mod
from primitives import fetch as fetch_mod
from primitives import render as render_mod
from primitives.parse import parse_html

AGENT, TASK = "selene", "crawlability"
METHODOLOGY = ("Fetched robots.txt (RFC 9309: 4xx = no rules, 5xx/unreachable = everything disallowed) and each "
               "audited page with this service's identified crawler (ZBM-SEO-Audit), honouring robots.txt for its own "
               "token. Each bot family token was evaluated with RFC 9309 longest-match rules against '/' and every "
               "audited path; bot family list version {v}. Where robots.txt allows this service's crawler, pages were "
               "also fetched once with a browser User-Agent (honouring robots.txt for our crawler) to compare what is "
               "served (user-agent differential only); a difference is reported only when it holds on a repeat "
               "request by our crawler. Same-site pages linked from the requested pages (one level, no query strings, "
               "robots.txt honoured) were read too, up to the per-audit page limit. Raw HTML only; no JavaScript was "
               "executed.")
LIMITS = ["Observed from one network location at one time; a CDN or firewall may answer other clients differently.",
          "Bot access by VERIFIED IP range (how the major operators identify their crawlers) cannot be observed by "
          "changing the User-Agent; an IP-based block or cloak is not detected.",
          "robots.txt states a request to crawlers; it does not show whether any operator honours it.",
          "The bot family list is versioned data and is not complete.",
          "Rendering is not connected: content inserted by JavaScript was not seen."]
HTML_TYPES = ("text/html", "application/xhtml+xml")
CANONICAL_CHECK_MAX = 5
REFUSED_STATUSES = (401, 403, 405, 429)
SERVICE_BUSY = "SERVICE_BUSY"          # our own capacity or safety limit stopped a request (never about the site)


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
        ctx.polite()
        t0 = ctx.monotonic()
        rb = ctx.fetcher.robots(ctx.origin, guard=ctx.guard)
        ctx.observe(t0)
        if rb.get("status_class") == "not_checked":
            # round-4 review: our own capacity limit, not the site. robots.txt is neither cached nor treated as
            # unreachable; nothing else is read, and the area is FAILED with an honest "our service was busy"
            facts["robots"] = {"status_class": "not_checked", "fetch": rb["fetch"]}
            return envelope(AGENT, TASK, "FAILED", [], methodology=METHODOLOGY.format(v=bots.VERSION),
                            limitations=LIMITS, reason=SERVICE_BUSY, facts=facts), pages
        ctx.robots = rb
        ctx.robots_cache[_origin_key(ctx.origin)] = rb
        ctx.osei.observed("robots", ctx.origin + "/robots.txt", rb["status_class"])
        if rb["fetch"]["state"] == "KILLED":
            raise Killed(rb["fetch"]["detail"] or "KILLED")
        facts["robots"] = {"status_class": rb["status_class"], "fetch": rb["fetch"],
                           "text_sha256": rb.get("text_sha256"),
                           "sitemaps_declared": (rb["parsed"].sitemaps[:50] if rb["parsed"] else [])}
        findings += _robots_findings(ctx, rb, facts)
        state = {"fetched": 0, "canon_checked": 0}
        try:
            for path in ctx.paths:
                findings += _read_page(ctx, path, pages, state, facts, not_connected)
        except DeadlineReached:
            facts["deadline_reached"] = True
        # Page discovery (bounded): when the request named fewer pages than the per-audit cap, same-site pages linked
        # from the requested pages are read too, one level deep, in document order, up to the cap. Every discovered
        # path goes through the same fetcher (SSRF guard, robots.txt for our crawler, byte / time / redirect budgets)
        discovered = discover_paths(ctx, pages)
        asked = ctx.robots_crawl_delay(fetch_mod.PRODUCT_TOKEN)
        facts["crawl_delay_seconds"] = ctx.crawl_delay()
        if discovered and asked is not None and asked > CRAWL_DELAY_CAP:
            # robots.txt asks for more time between requests than an audit may take: only the requested pages
            facts["discovery_stopped"] = "crawl_delay_above_cap"
            discovered = []
        if ctx.deadline_hit:
            _cut(facts, "discovery", len(discovered))
            discovered = []
        read = []
        started = ctx.monotonic()
        # discovered pages leave room before the audit deadline for Delia's sitemap and llms.txt requests
        # re-review item 3: the reserve is counted in expected request time (the politeness wait or the slowest
        # response seen so far), plus one worst-case fetch, so Delia's requests keep their room on a slow site
        horizon = (ctx.deadline - DELIA_RESERVE_REQUESTS * ctx.per_request_estimate() - ctx.fetch_reserve_s
                   if ctx.deadline is not None else started + DISCOVERY_TIME_BUDGET_S)
        budget = REQUESTS_PER_PAGE * max(ctx.max_pages, len(ctx.paths)) + CANONICAL_CHECK_MAX + 2
        for path in discovered:
            # politeness and a bounded audit: a discovered page is read only while the request budget and the time
            # budget allow its worst case (crawler, browser comparison, repeat)
            if ctx.requests + REQUESTS_PER_PAGE > budget:
                facts["discovery_stopped"] = "request_budget"
                break
            if ctx.monotonic() + REQUESTS_PER_PAGE * ctx.per_request_estimate() > min(
                    horizon, started + DISCOVERY_TIME_BUDGET_S):
                facts["discovery_stopped"] = "time_budget"
                _cut(facts, "discovery", len(discovered) - len(read))
                break
            read.append(path)
            try:
                findings += _read_page(ctx, path, pages, state, facts, not_connected)
            except DeadlineReached:
                facts["deadline_reached"] = True
                facts["discovery_stopped"] = "deadline"
                read.pop()
                _cut(facts, "discovery", len(discovered) - len(read))
                break
        facts["pages_discovered"] = read
        facts["requests"] = ctx.requests
        fetched = state["fetched"]
        facts["pages_requested"], facts["pages_fetched"] = len(ctx.paths), fetched
    except DeadlineReached:
        # not even robots.txt could be asked in time: nothing observed
        return envelope(AGENT, TASK, "FAILED", findings, methodology=METHODOLOGY.format(v=bots.VERSION),
                        limitations=LIMITS, reason=DEADLINE_REASON, facts=facts), pages
    except Killed as k:
        return envelope(AGENT, TASK, "KILLED", [], methodology=METHODOLOGY.format(v=bots.VERSION),
                        limitations=LIMITS, reason=k.code, facts=facts), pages
    fetched = sum(1 for p in pages.values() if p["fetch"].ok and not p.get("refused"))
    if facts.get("deadline_reached") and any(p not in pages for p in ctx.paths):
        # the deadline stopped the requested pages: never OK; what was read is reported
        facts["render_states"] = {p: v["render"]["state"] for p, v in pages.items()}
        return envelope(AGENT, TASK, "PARTIAL" if fetched else "FAILED", findings,
                        methodology=METHODOLOGY.format(v=bots.VERSION), limitations=LIMITS,
                        not_connected=not_connected, facts=facts, reason=DEADLINE_REASON), pages
    cut = facts.get("deadline_cut") or {}
    if fetched and (cut.get("comparisons") or cut.get("canonical_checks")) \
            and not (facts.get("deadline_reached") and any(p not in pages for p in ctx.paths)):
        facts["render_states"] = {p: v["render"]["state"] for p, v in pages.items()}
        return envelope(AGENT, TASK, "PARTIAL", findings, methodology=METHODOLOGY.format(v=bots.VERSION),
                        limitations=LIMITS, not_connected=not_connected, facts=facts, reason=DEADLINE_REASON), pages
    if fetched == 0:
        outcome = "BLOCKED" if all(p["fetch"].state == "BLOCKED_BY_ROBOTS" or p.get("refused")
                                   for p in pages.values()) else "FAILED"
    else:
        outcome = "OK" if fetched == len(pages) else "PARTIAL"
    facts["render_states"] = {p: v["render"]["state"] for p, v in pages.items()}
    reason = SERVICE_BUSY if outcome != "OK" and facts.get("service_busy_pages") else None
    return envelope(AGENT, TASK, outcome, findings, methodology=METHODOLOGY.format(v=bots.VERSION),
                    limitations=LIMITS, not_connected=not_connected, facts=facts, reason=reason), pages


# Discovery: what a linked path must look like to be read. Only page-like paths (no file extension, or an HTML one);
# no query string (faceted, session and tracking URLs are not pages to audit); never an action-like path.
DISCOVERY_EXTENSIONS = ("", ".html", ".htm", ".php", ".asp", ".aspx")
# a path segment (percent-decoded, lower-cased) is skipped when it, its stem without extension, or any of its
# hyphen / underscore / dot separated words is one of these (account, sign-in, delete, API and admin areas)
_DISCOVERY_SKIP_SEGMENTS = frozenset({
    "login", "log-in", "logout", "log-out", "signin", "sign-in", "signout", "sign-out", "signup", "sign-up",
    "register", "password", "passwd", "unsubscribe", "cart", "checkout", "basket", "admin", "administrator",
    "wp-admin", "wp-login", "wp-json", "xmlrpc", "account", "accounts", "myaccount", "my-account", "delete",
    "destroy", "api", "graphql", "cgi-bin", "cdn-cgi", "auth", "oauth"})
# the most one page costs: our crawler, its canonical target, the browser comparison, the repeat request
REQUESTS_PER_PAGE = 4
DELIA_RESERVE_REQUESTS = delia_mod.MAX_REQUESTS   # Delia's true maximum, kept free of discovery
DISCOVERY_TIME_BUDGET_S = 120.0        # discovered pages stop once the next could take the audit past this
_DISCOVERY_PATH = re.compile(r"/[\x21-\x7e]{0,500}")


def discover_paths(ctx, pages: dict) -> list:
    """Same-site paths linked from the pages already read, bounded by ``ctx.max_pages`` (requested + discovered).
    Deterministic: the requested pages in request order, their links in document order. A path robots.txt disallows
    for our crawler, a nofollow link, a query string, a non-page file, an action-like path and a path that looks like
    personal data are skipped (never fetched)."""
    room = (ctx.max_pages or 0) - len(pages)
    if room <= 0:
        return []
    rb = ctx.robots or {}
    parsed = rb.get("parsed")
    if parsed is None and not rb.get("default_allow"):
        return []                                    # robots.txt unreadable: nothing beyond what was asked for
    seen = {p.rstrip("/") or "/" for p in pages}
    out = []
    for path in list(ctx.paths):
        pg = pages.get(path) or {}
        x = pg.get("extract")
        if x is None or not pg["fetch"].ok:
            continue
        for link in x.get("links") or []:
            if len(out) >= room:
                return out
            cand = _candidate(ctx, link)
            if cand is None:
                continue
            key = cand.rstrip("/") or "/"
            if key in seen:
                continue
            allowed = parsed.allowed(fetch_mod.PRODUCT_TOKEN, cand) if parsed is not None else rb["default_allow"]
            if not allowed:
                continue
            seen.add(key)
            out.append(cand)
    return out


def _candidate(ctx, link) -> Optional[str]:
    if not isinstance(link, dict) or link.get("nofollow") or not isinstance(link.get("url"), str):
        return None
    p = split_url(link["url"])
    if p is None or p.scheme not in ("http", "https") or p.query or p.port not in (None, 80, 443) \
            or p.username or p.password or not ctx.same_site(p.hostname):
        return None
    path = p.path or "/"
    if not _DISCOVERY_PATH.fullmatch(path) or path.startswith("//") or "\\" in path or "#" in path or ";" in path:
        return None
    dec = unquote(path)
    # judged on the decoded path (review of PR 47, L5: /%61dmin is /admin); a still-encoded path after one decode
    # (double encoding) or a decoded control / space / backslash character is refused outright
    if "%" in dec or any(ord(c) < 0x21 or c in "\\;" or ord(c) == 0x7f for c in dec):
        return None
    segs = [s for s in dec.lower().split("/") if s]
    for seg in segs:
        if seg.startswith("."):                       # dot-folders and dot-files: /.git/config, /.well-known, /.env
            return None
        stem = seg.rsplit(".", 1)[0]
        if {seg, stem, *re.split(r"[-_.+~]", seg)} & _DISCOVERY_SKIP_SEGMENTS:
            return None
    last = segs[-1] if segs and not dec.endswith("/") else ""
    ext = last[last.rfind("."):] if "." in last else ""
    if ext not in DISCOVERY_EXTENSIONS:
        return None
    if personal_data(dec) is not None:
        return None
    return path


def _read_page(ctx, path: str, pages: dict, state: dict, facts: dict, not_connected: set) -> list:
    """Fetch and judge one page (requested or discovered); fills ``pages[path]``."""
    findings = []
    url = ctx.url(path)
    f = _fetch(ctx, url)
    ctx.osei.observed("page", url, f.state)
    extract = None
    if f.state == "TOO_LARGE":
        ctx.osei.quarantine("page", "TOO_LARGE", url, None)
    if f.ok and f.content_type in HTML_TYPES and f.body is not None:
        extract = parse_html(f.text(), f.final_url or url)
    rstate = render_mod.render_state(f, extract, ctx.renderer, ctx.guard)
    if rstate["state"] == "RAW_OK_RENDER_NOT_CONNECTED":
        not_connected.add("render")
    # an error page (4xx / 5xx) is not the page: Roman and the entity check never judge its content
    pages[path] = {"fetch": f, "extract": extract if f.ok and (f.status or 0) < 400 else None,
                   "render": {k: v for k, v in rstate.items() if k != "rendered_extract"}}
    if f.ok and f.status in REFUSED_STATUSES:
        pages[path]["refused"] = True
    if f.ok:
        state["fetched"] += 1
    page_findings, state["canon_checked"] = _page_findings(ctx, path, f, extract, state["canon_checked"], facts)
    findings += page_findings
    if f.ok and extract is not None:
        # AEGIS L3: the browser-identity comparison honours robots.txt for OUR crawler too (it is only made
        # where our crawler may fetch, and from the same robots cache)
        try:
            human = _fetch(ctx, url, ua="human")
        except DeadlineReached:
            pages[path]["access_diff"] = {"state": "INCONCLUSIVE", "signals": [], "why": "audit time limit reached",
                                          "reason": DEADLINE_REASON}
            _cut(facts, "comparisons")
            return findings
        hx = _extract_of(human, url)
        ad = diff_mod.access_diff(f, human, extract, hx)
        if ad["state"] == "BOT_DIFFERENTIAL":
            # Gate 4: a differential is reported only when it is stable. Our crawler fetches the page once more;
            # a signal that also differs between our own two fetches is the page changing between requests, not
            # the site treating crawlers differently, and is dropped. Nothing stable left: INCONCLUSIVE, never a
            # finding.
            try:
                again = _fetch(ctx, url)
            except DeadlineReached:
                again = None
                _cut(facts, "comparisons")
            repeat = diff_mod.access_diff(f, again, extract, _extract_of(again, url)) \
                if again is not None and again.ok else None
            ad = diff_mod.confirm(ad, repeat)
        pages[path]["access_diff"] = ad
        if ad["state"] == "CRAWLER_REFUSED":
            # H3: not cloaking, not an error visitors see: one note that the site refuses unknown crawlers
            findings = [x for x in findings if not (x["url"] == url and x["code"] in (
                "PAGE_HTTP_ERROR", "PAGE_HTTP_ERROR_OTHER", "PAGE_ACCESS_REFUSED"))]
            findings.append(finding("PAGE_ACCESS_REFUSED", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                                    capability="P2", detail={"status": f.status, "browser_served": True}))
            pages[path]["refused"] = True
        if ad["state"] == "BOT_DIFFERENTIAL":
            findings.append(finding("BOT_DIFFERENTIAL", "high", "measured", "TEST", url=url,
                                    capability="P2", detail={"signals": ad["signals"], "confirmed": True}))
    return findings


def _cut(facts: dict, what: str, n: int = 1) -> None:
    """Work the audit time limit (or the discovery time budget that protects it) left undone, by kind:
    ``discovery`` (linked pages not read), ``comparisons`` (crawler / browser comparisons), ``canonical_checks``."""
    if n > 0:
        c = facts.setdefault("deadline_cut", {})
        c[what] = c.get(what, 0) + n


def _fetch(ctx, url: str, **kw):
    """Every Selene request after robots.txt: politeness wait first, then the one fetcher (SSRF guard, robots.txt for
    our crawler, byte / time / redirect budgets)."""
    ctx.polite()
    t0 = ctx.monotonic()
    f = ctx.fetcher.fetch(url, robots_cache=ctx.robots_cache, guard=ctx.guard, **kw)
    ctx.observe(t0)
    if f.state == "KILLED":
        raise Killed(f.detail or "KILLED")
    return f


def _extract_of(f, url: str) -> Optional[dict]:
    return parse_html(f.text(), f.final_url or url) if f.ok and f.content_type in HTML_TYPES else None


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


def same_url(a: Optional[str], b: Optional[str]) -> bool:
    """Two absolute URLs name the same page: scheme and host compared without case, the default port and one
    trailing slash ignored (a canonical written as ``https://Example.com:443/a/`` is ``https://example.com/a``)."""
    pa, pb = split_url(a), split_url(b)
    if pa is None or pb is None:
        return (a or "").rstrip("/") == (b or "").rstrip("/")

    def key(p):
        port = p.port if p.port not in (None, {"http": 80, "https": 443}.get(p.scheme.lower())) else None
        return (p.scheme.lower(), (p.hostname or "").rstrip("."), port, (p.path or "/").rstrip("/") or "/", p.query)
    return key(pa) == key(pb)


def _page_findings(ctx, path: str, f, extract: Optional[dict], canon_checked: int,
                   facts: Optional[dict] = None) -> tuple[list, int]:
    url = ctx.url(path)
    out = []
    if f.state == "BLOCKED_BY_ROBOTS":
        out.append(finding("PAGE_BLOCKED_FOR_OUR_CRAWLER", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           detail={"note": "robots.txt disallows ZBM-SEO-Audit here; the page was not read"}))
        return out, canon_checked
    if f.state == "RESOURCE_LIMIT":
        # our capacity or safety limit, not the site: recorded, never a finding about the client's page
        if facts is not None:
            facts.setdefault("service_busy_pages", []).append(path)
        return out, canon_checked
    if not f.ok:
        out.append(finding("PAGE_UNREACHABLE", "high", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           detail={"fetch_state": f.state, "detail": f.detail}))
        return out, canon_checked
    if f.status in REFUSED_STATUSES:
        # H2: refused or rate-limited (401 / 403 / 405 / 429) says nothing about what visitors get: inconclusive,
        # info only, never in the executive summary; the page's content is not judged
        out.append(finding("PAGE_ACCESS_REFUSED", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url,
                           capability="P2", detail={"status": f.status}))
        return out, canon_checked
    if f.status in GONE_STATUSES or f.status >= 500:
        out.append(finding("PAGE_HTTP_ERROR", "high" if f.status >= 500 else "medium", "measured", "ACT", url=url,
                           detail={"status": f.status}))
    elif f.status >= 400:
        out.append(finding("PAGE_HTTP_ERROR_OTHER", "low", "measured", "WATCH", url=url, detail={"status": f.status}))
    if f.redirects:
        chain = [r["status"] for r in f.redirects]
        sev = "medium" if len(chain) > 1 else "low"
        out.append(finding("REDIRECT_CHAIN" if len(chain) > 1 else "REDIRECTED", sev, "measured",
                           "ACT" if len(chain) > 1 else "WATCH", url=url,
                           detail={"statuses": chain, "hops": len(chain), "final_url": f.final_url,
                                   "temporary": any(s in (302, 303, 307) for s in chain)}))
    if f.status >= 400:
        return out, canon_checked                      # an error page's canonical / noindex are not the page's
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
    invalid = extract.get("canonicals_invalid") or 0
    total = (extract.get("canonicals_valid", len(canons)) or 0) + invalid
    if invalid:
        # WR-F009: a canonical that is not an http(s) URL this service can read (javascript:, a broken IPv6 literal);
        # reported whenever there is one (AEGIS SEO Wave 3 M3), beside the other canonical findings
        out.append(finding("CANONICAL_INVALID", "medium", "measured", "ACT", url=url, detail={"count": invalid}))
    if total > 1:
        # M3: an invalid canonical still counts: one valid plus one invalid is two canonicals
        out.append(finding("CANONICAL_MULTIPLE", "high", "measured", "ACT", url=url,
                           detail={"count": total, "invalid": invalid, "canonicals": canons[:10]}))
    elif len(canons) == 1:
        c = canons[0]
        host = host_of(c)
        if not ctx.same_site(host):
            out.append(finding("CANONICAL_OTHER_HOST", "medium", "measured", "TEST", url=url,
                               detail={"canonical": c}))
        if noindex and not same_url(c, f.final_url or url):
            out.append(finding("NOINDEX_CANONICAL_CONFLICT", "high", "measured", "ACT", url=url,
                               detail={"canonical": c, "note": "noindex plus a canonical pointing elsewhere sends "
                                                               "contradictory signals"}))
        if not same_url(c, f.final_url or url) and ctx.same_site(host) \
                and canon_checked < CANONICAL_CHECK_MAX:
            canon_checked += 1
            try:
                cf = _fetch(ctx, c)
            except DeadlineReached:
                cf = fetch_mod.FetchResult(url=c, state="DEADLINE")
                if facts is not None:
                    _cut(facts, "canonical_checks")
            if not cf.ok:
                # Gate 4: our own request for the canonical target failed (timeout, refused, robots.txt for our
                # crawler). That says nothing about what the target answers: inconclusive, recorded as a fact, never
                # a finding
                if facts is not None:
                    facts.setdefault("canonical_checks_inconclusive", []).append(
                        {"path": path, "fetch_state": cf.state})
            elif cf.status != 200 or cf.redirects:
                out.append(finding("CANONICAL_TARGET_NOT_200", "high", "measured", "ACT", url=url,
                                   detail={"canonical": c, "fetch_state": cf.state, "status": cf.status,
                                           "redirected": bool(cf.redirects)}))
    elif not invalid:
        out.append(finding("CANONICAL_MISSING", "low", "measured", "WATCH", url=url))
    if extract.get("parse_error"):
        out.append(finding("HTML_PARSE_ERROR", "low", "measured", "WATCH", url=url,
                           detail={"error": extract["parse_error"]}))
    return out, canon_checked
