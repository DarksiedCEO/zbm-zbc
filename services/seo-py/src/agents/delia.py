"""
Delia — sitemaps and llms.txt (P3 machine readability). One task: can machines discover the site's URLs, and does
the site publish the machine-oriented files it claims to?

Sitemaps (sitemaps.org protocol 0.9): discovered from robots.txt ``Sitemap:`` lines (same site only), else
``/sitemap.xml``; ``urlset`` and ``sitemapindex`` (children followed, bounded); gzip sitemaps decompressed with a
bound. Validated: namespace, ``loc`` absolute and on this site, ``lastmod`` W3C date-time and not in the future,
identical ``lastmod`` everywhere (often a generator stamping "now", which tells crawlers nothing), the 50,000-URL
per-file limit, URLs listed but disallowed for Googlebot. XML with a DOCTYPE or entity declaration is quarantined
unparsed (no entity expansion, ever).

llms.txt: described honestly — an EMERGING, NON-STANDARD convention (a community proposal, llmstxt.org); no search
or answer engine has committed publicly to reading it as far as this service's authors know, so its absence is NOT
a defect (decision WATCH), and a malformed one is a low-severity TEST.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from envelope import envelope, finding
from primitives import Killed

AGENT, TASK = "delia", "sitemaps_and_llms_txt"
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_URLS_PER_FILE = 50_000
MAX_CHILDREN = 10
DECOMPRESSED_MAX = 8 * 1024 * 1024
BLOCK_SAMPLE = 500
SAMPLE_PATHS = 500
METHODOLOGY = ("Sitemaps discovered from robots.txt Sitemap lines on this site, else /sitemap.xml; parsed as "
               "sitemaps.org 0.9 (urlset / sitemapindex, children followed up to {c}); XML with DOCTYPE or ENTITY "
               "declarations quarantined unparsed. lastmod checked as W3C date-time against the audit clock. Up to "
               "{s} listed URLs checked against robots.txt for Googlebot. llms.txt fetched from the site root and "
               "checked against the llmstxt.org proposal's shape (H1 title, optional blockquote, H2 sections of "
               "markdown links).")
LIMITS = ["llms.txt is an emerging, non-standard convention; its presence or absence is not known to affect any "
          "engine's ranking or answers.",
          "Only sitemaps on this site were read; cross-host sitemaps are listed but not fetched.",
          "Listed URLs were not fetched individually (status codes of sitemap URLs are not checked in Wave 1)."]
_W3C = re.compile(r"\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?)?)?")
_LLMS_LINK = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\(([^)\s]+)\)(?::\s*(.*))?$")


def _parse_lastmod(v: str) -> Optional[datetime]:
    if not _W3C.fullmatch(v):
        return None
    try:
        if "T" in v:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        parts = [int(x) for x in v.split("-")] + [1, 1]
        return datetime(parts[0], parts[1], parts[2], tzinfo=timezone.utc)
    except ValueError:
        return None


def _decompress(body: bytes) -> Optional[bytes]:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(body, DECOMPRESSED_MAX + 1)
    except zlib.error:
        return None
    return None if len(out) > DECOMPRESSED_MAX else out


def _xml(ctx, kind: str, url: str, body: bytes):
    """Parse sitemap XML or quarantine it. Returns the root element or None."""
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        ctx.osei.quarantine(kind, "XML_DOCTYPE_REFUSED", url, body)
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        ctx.osei.quarantine(kind, "XML_MALFORMED", url, body)
        return None


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def run(ctx) -> dict:
    findings, facts, not_connected = [], {}, set()
    try:
        ctx.guard(capability="fetch", provider="web")
        findings += _sitemaps(ctx, facts)
        findings += _llms(ctx, facts)
    except Killed as k:
        return envelope(AGENT, TASK, "KILLED", [], methodology=METHODOLOGY.format(c=MAX_CHILDREN, s=BLOCK_SAMPLE),
                        limitations=LIMITS, reason=k.code, facts=facts)
    read_any = facts.get("sitemaps_read", 0) > 0 or facts.get("llms_txt", {}).get("state") in ("present", "absent")
    outcome = "OK" if read_any and not facts.get("sitemap_fetch_failures") else ("PARTIAL" if read_any else "FAILED")
    return envelope(AGENT, TASK, outcome, findings, methodology=METHODOLOGY.format(c=MAX_CHILDREN, s=BLOCK_SAMPLE),
                    limitations=LIMITS, not_connected=not_connected, facts=facts)


def _fetch(ctx, url: str, accept: str):
    f = ctx.fetcher.fetch(url, robots_cache=ctx.robots_cache, guard=ctx.guard, accept=accept)
    if f.state == "KILLED":
        raise Killed(f.detail or "KILLED")
    return f


def _sitemaps(ctx, facts: dict) -> list:
    out = []
    declared = list(ctx.robots["parsed"].sitemaps) if ctx.robots and ctx.robots.get("parsed") else []
    same = [u for u in declared if ctx.same_site(urlsplit(u).hostname)]
    facts["sitemaps_declared"] = declared[:50]
    facts["sitemaps_cross_host_not_read"] = [u for u in declared if u not in same][:50]
    queue = same[:MAX_CHILDREN] or [ctx.url("/sitemap.xml")]
    seen, urls, lastmods, read, failures = set(), [], [], 0, 0
    children_followed = 0
    while queue:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        f = _fetch(ctx, sm, "application/xml,text/xml;q=0.9,*/*;q=0.5")
        ctx.osei.observed("sitemap", sm, f.state)
        if not f.ok or f.status != 200:
            failures += 1
            if f.state == "TOO_LARGE":
                ctx.osei.quarantine("sitemap", "TOO_LARGE", sm, None)
            out.append(finding("SITEMAP_MISSING" if f.ok and f.status == 404 else "SITEMAP_UNREADABLE",
                               "medium", "measured", "ACT" if f.ok and f.status == 404 else "INSUFFICIENT_EVIDENCE",
                               url=sm, capability="P3", detail={"fetch_state": f.state, "status": f.status}))
            continue
        body = f.body or b""
        if sm.endswith(".gz") or f.content_type in ("application/gzip", "application/x-gzip"):
            body = _decompress(body)
            if body is None:
                ctx.osei.quarantine("sitemap", "DECOMPRESS_FAILED", sm, f.body)
                out.append(finding("SITEMAP_DECOMPRESS_FAILED", "medium", "measured", "ACT", url=sm, capability="P3"))
                continue
        root = _xml(ctx, "sitemap", sm, body)
        if root is None:
            out.append(finding("SITEMAP_QUARANTINED", "high", "measured", "ACT", url=sm, capability="P3",
                               detail={"why": ctx.osei.quarantined[-1]["reason"]}))
            continue
        read += 1
        ns, name = _local(root.tag)
        if ns != NS:
            out.append(finding("SITEMAP_NAMESPACE", "low", "measured", "ACT", url=sm, capability="P3",
                               detail={"namespace": ns[:200]}))
        if name == "sitemapindex":
            for child in root:
                loc = child.find(f"{{{ns}}}loc") if ns else child.find("loc")
                if loc is not None and loc.text and children_followed < MAX_CHILDREN:
                    u = loc.text.strip()
                    if ctx.same_site(urlsplit(u).hostname):
                        queue.append(u)
                        children_followed += 1
            continue
        if name != "urlset":
            out.append(finding("SITEMAP_ROOT_UNKNOWN", "medium", "measured", "ACT", url=sm, capability="P3",
                               detail={"root": name[:60]}))
            continue
        file_urls = 0
        for child in root:
            loc = child.find(f"{{{ns}}}loc") if ns else child.find("loc")
            lm = child.find(f"{{{ns}}}lastmod") if ns else child.find("lastmod")
            if loc is None or not (loc.text or "").strip():
                continue
            file_urls += 1
            if len(urls) < 100_000:
                urls.append((loc.text.strip(), sm))
            if lm is not None and lm.text:
                lastmods.append((lm.text.strip(), sm))
        if file_urls > MAX_URLS_PER_FILE:
            out.append(finding("SITEMAP_TOO_MANY_URLS", "high", "measured", "ACT", url=sm, capability="P3",
                               detail={"urls": file_urls, "limit": MAX_URLS_PER_FILE}))
    facts.update(sitemaps_read=read, sitemap_fetch_failures=failures, sitemap_urls=len(urls),
                 sitemap_children_followed=children_followed)
    sample = []
    for u, _ in urls:
        p = urlsplit(u)
        if p.scheme in ("http", "https") and ctx.same_site(p.hostname) and len(sample) < SAMPLE_PATHS:
            sample.append((p.path or "/")[:300])
    facts["sitemap_paths_sample"] = sample          # for the log task's "important but uncrawled" (Wave 2)
    out += _url_checks(ctx, urls)
    out += _lastmod_checks(ctx, lastmods)
    return out


def _url_checks(ctx, urls: list) -> list:
    out, bad_abs, other_host, blocked = [], [], [], []
    parsed = ctx.robots.get("parsed") if ctx.robots else None
    for i, (u, sm) in enumerate(urls):
        p = urlsplit(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            bad_abs.append(u[:300])
            continue
        if not ctx.same_site(p.hostname):
            other_host.append(u[:300])
            continue
        if parsed is not None and i < BLOCK_SAMPLE:
            path = (p.path or "/") + (f"?{p.query}" if p.query else "")
            if not parsed.allowed("Googlebot", path):
                blocked.append(u[:300])
    if bad_abs:
        out.append(finding("SITEMAP_LOC_NOT_ABSOLUTE", "medium", "measured", "ACT", capability="P3",
                           detail={"count": len(bad_abs), "examples": bad_abs[:5]}))
    if other_host:
        out.append(finding("SITEMAP_LOC_OTHER_HOST", "medium", "measured", "ACT", capability="P3",
                           detail={"count": len(other_host), "examples": other_host[:5]}))
    if blocked:
        out.append(finding("SITEMAP_LISTS_ROBOTS_BLOCKED_URL", "medium", "measured", "ACT", capability="P3",
                           detail={"count": len(blocked), "examples": blocked[:5], "checked_for": "Googlebot"}))
    return out


def _lastmod_checks(ctx, lastmods: list) -> list:
    out, invalid, future = [], [], []
    now = ctx.clock.now()
    values = []
    for v, sm in lastmods:
        dt = _parse_lastmod(v)
        if dt is None:
            invalid.append(v[:60])
            continue
        values.append(v)
        if dt > now + timedelta(days=1):
            future.append(v[:60])
    if invalid:
        out.append(finding("SITEMAP_LASTMOD_INVALID", "low", "measured", "ACT", capability="P3",
                           detail={"count": len(invalid), "examples": invalid[:5]}))
    if future:
        out.append(finding("SITEMAP_LASTMOD_FUTURE", "low", "measured", "ACT", capability="P3",
                           detail={"count": len(future), "examples": future[:5]}))
    if len(values) >= 10 and len(set(values)) == 1:
        out.append(finding("SITEMAP_LASTMOD_ALL_IDENTICAL", "low", "inferred", "TEST", capability="P3",
                           detail={"count": len(values), "value": values[0][:60],
                                   "note": "every URL carries the same lastmod; it likely does not reflect real "
                                           "changes (inferred, not measured)"}))
    return out


def check_llms_txt(text: str) -> dict:
    """Shape check against the llmstxt.org proposal: an H1 first, optional '>' summary, H2 sections whose list items
    are markdown links. Returns {"issues": [...], "sections": n, "links": n}."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln.strip()]
    issues, sections, links, bad_links = [], 0, 0, 0
    if not body or not re.match(r"^#\s+\S", body[0]) or body[0].startswith("##"):
        issues.append("NO_H1_TITLE")
    for ln in body[1:]:
        if ln.startswith("## "):
            sections += 1
        elif ln.startswith("# "):
            issues.append("MORE_THAN_ONE_H1")
        m = _LLMS_LINK.match(ln)
        if m:
            links += 1
            if not re.match(r"^https?://", m.group(2)):
                bad_links += 1
    if sections == 0:
        issues.append("NO_H2_SECTIONS")
    if links == 0:
        issues.append("NO_LINK_LISTS")
    if bad_links:
        issues.append("RELATIVE_OR_NON_HTTP_LINKS")
    return {"issues": sorted(set(issues)), "sections": sections, "links": links, "relative_links": bad_links}


def _llms(ctx, facts: dict) -> list:
    url = ctx.url("/llms.txt")
    f = _fetch(ctx, url, "text/plain,text/markdown;q=0.9,*/*;q=0.5")
    ctx.osei.observed("llms_txt", url, f.state)
    note = "llms.txt is an emerging, non-standard convention (llmstxt.org proposal)"
    if not f.ok:
        facts["llms_txt"] = {"state": "unreadable", "fetch_state": f.state}
        return [finding("LLMS_TXT_UNREADABLE", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url, capability="P3",
                        detail={"fetch_state": f.state, "note": note})]
    if f.status != 200:
        facts["llms_txt"] = {"state": "absent", "status": f.status}
        return [finding("LLMS_TXT_ABSENT", "info", "measured", "WATCH", url=url, capability="P3",
                        detail={"status": f.status, "note": note + "; absence is not a defect"})]
    try:
        text = (f.body or b"").decode("utf-8")
    except UnicodeDecodeError:
        ctx.osei.quarantine("llms_txt", "NOT_UTF8", url, f.body)
        facts["llms_txt"] = {"state": "present", "quarantined": True}
        return [finding("LLMS_TXT_NOT_UTF8", "low", "measured", "TEST", url=url, capability="P3",
                        detail={"note": note})]
    chk = check_llms_txt(text)
    facts["llms_txt"] = {"state": "present", "content_type": f.content_type, **chk}
    out = []
    if chk["issues"]:
        out.append(finding("LLMS_TXT_FORMAT", "low", "measured", "TEST", url=url, capability="P3",
                           detail={"issues": chk["issues"], "note": note}))
    if f.content_type not in ("text/plain", "text/markdown"):
        out.append(finding("LLMS_TXT_CONTENT_TYPE", "low", "measured", "TEST", url=url, capability="P3",
                           detail={"content_type": f.content_type, "note": note}))
    return out
