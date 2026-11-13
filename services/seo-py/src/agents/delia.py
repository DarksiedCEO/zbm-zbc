"""
Delia — sitemaps and llms.txt (P3 machine readability). One task: can machines discover the site's URLs, and does
the site publish the machine-oriented files it claims to?

Sitemaps (sitemaps.org protocol 0.9): discovered from robots.txt ``Sitemap:`` lines (same site only), else
``/sitemap.xml``; ``urlset`` and ``sitemapindex`` (children followed, bounded); gzip sitemaps decompressed with a
bound. Validated: namespace, ``loc`` absolute and on this site, ``lastmod`` W3C date-time and not in the future,
identical ``lastmod`` everywhere (often a generator stamping "now", which tells crawlers nothing), the 50,000-URL
per-file limit, URLs listed but disallowed for Googlebot. XML with a DOCTYPE or entity declaration is quarantined
unparsed (no entity expansion, ever), in whatever encoding it comes (AEGIS Wave-1 M-D1): the encoding is detected
(byte order mark, UTF-16 / UTF-32 byte pattern, XML declaration) and the DECODED text is checked, and the parse
itself runs on an expat parser whose DOCTYPE and entity-declaration handlers refuse (DTDs forbidden), so an
encoding expat reads natively but this check did not foresee cannot expand an entity either.

Every crawled URL, path, namespace, element name and lastmod value is stored in its safe form only
(primitives/safe_url.py; lastmod examples as dates or as their character shape); comparisons (same site, robots
rules, sitemap membership) are made on the raw values in memory. The sitemap path sample the log task matches
against is kept as templates (for display) and keyed digests (``path_digest``), never as raw paths.

llms.txt: described honestly — an EMERGING, NON-STANDARD convention (a community proposal, llmstxt.org); no search
or answer engine has committed publicly to reading it as far as this service's authors know, so its absence is NOT
a defect (decision WATCH), and a malformed one is a low-severity TEST.
"""

from __future__ import annotations

import codecs
import hashlib
import re
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.parsers import expat

from agents import host_of, split_url
from agents.logs import path_digest
from envelope import envelope, finding
from primitives import Killed
from primitives import robots as robots_mod
from primitives import safe_url as su

AGENT, TASK = "delia", "sitemaps_and_llms_txt"
NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_URLS_PER_FILE = 50_000
MAX_CHILDREN = 10
DECOMPRESSED_MAX = 8 * 1024 * 1024
BLOCK_SAMPLE = 500
SAMPLE_PATHS = 500
URLS_KEPT_MAX = 100_000
# AEGIS Wave-1 M-S1 / M-D2, round 2 M2: the listed-URL robots.txt check of one run is bounded by this COUNTED work
# (primitives/robots.py ``Work``; no clock: a slow site never spends it); URLs left once it is spent are counted
# ``robots_unchecked`` and the outcome is PARTIAL.
URL_CHECK_WORK = robots_mod.STEP_WORK_UNITS
ROOT_NAMES = ("urlset", "sitemapindex", "rss", "feed", "html", "channel")
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
# [0-9], not \d: a lastmod in full-width (or any non-ASCII) digits is not a W3C date-time (AEGIS Wave-1 NIT)
_W3C = re.compile(r"[0-9]{4}(-[0-9]{2}(-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?"
                  r"(Z|[+-][0-9]{2}:[0-9]{2}))?)?)?")
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


_BOMS = ((codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF8, "utf-8"),
         (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"))
_PATTERNS = ((b"\x00\x00\x00<", "utf-32-be"), (b"<\x00\x00\x00", "utf-32-le"), (b"\x00<\x00?", "utf-16-be"),
             (b"<\x00?\x00", "utf-16-le"))
_DECL_ENC = re.compile(rb"^\s*<\?xml[^>]{0,200}?encoding\s*=\s*[\"']([A-Za-z0-9._-]{1,40})[\"']")


def xml_encoding(body: bytes) -> str:
    """The encoding of an XML document as the XML 1.0 spec (appendix F) detects it: a byte order mark, else the
    byte pattern of ``<?`` in UTF-32 / UTF-16, else the XML declaration's ``encoding``, else UTF-8."""
    for bom, enc in _BOMS:
        if body.startswith(bom):
            return enc
    for pat, enc in _PATTERNS:
        if body.startswith(pat):
            return enc
    m = _DECL_ENC.match(body[:256])
    if m:
        name = m.group(1).decode("ascii").lower()
        try:
            return codecs.lookup(name).name
        except LookupError:
            return "utf-8"
    return "utf-8"


def _declares_dtd(body: bytes) -> bool:
    """DOCTYPE or ENTITY anywhere in the DECODED text (AEGIS Wave-1 M-D1: a UTF-16 document hid both from a byte
    search)."""
    enc = xml_encoding(body)
    try:
        text = body.decode(enc, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    low = text.lower()
    return "<!doctype" in low or "<!entity" in low


class _DTDRefused(Exception):
    pass


def _refuse(*_args) -> None:
    raise _DTDRefused()


def _no_dtd_scan(body: bytes) -> Optional[str]:
    """One expat pass with DTDs forbidden: None when the document is well-formed and declares no DOCTYPE / entity,
    "dtd" when it declares one, "malformed" when expat cannot read it. expat decodes it itself (BOM, declaration)."""
    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = _refuse
    p.EntityDeclHandler = _refuse
    p.UnparsedEntityDeclHandler = _refuse
    p.ExternalEntityRefHandler = _refuse
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        p.Parse(body, True)
    except _DTDRefused:
        return "dtd"
    except (expat.ExpatError, ValueError, RecursionError):
        return "malformed"
    return None


_DECL = re.compile(r"^\s*<\?xml[^>]{0,200}?\?>")


def _utf32_to_utf8(body: bytes) -> Optional[bytes]:
    """expat cannot read UTF-32 (round 2 NIT): a UTF-32 document is decoded here and handed on as UTF-8 with its
    XML declaration dropped (it names the old encoding). None when it is not valid UTF-32."""
    enc = xml_encoding(body)
    if not enc.startswith("utf-32"):
        return body
    try:
        text = body.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None
    return _DECL.sub("", text.lstrip("\ufeff"), count=1).encode("utf-8")


def _xml(ctx, kind: str, url: str, body: bytes):
    """Parse sitemap XML or quarantine it. Returns (root element or None, the quarantine record or None)."""
    if _declares_dtd(body):
        return None, ctx.osei.quarantine(kind, "XML_DOCTYPE_REFUSED", url, body)
    original, body = body, _utf32_to_utf8(body)
    if body is None:
        return None, ctx.osei.quarantine(kind, "XML_MALFORMED", url, original)
    if _declares_dtd(body):
        return None, ctx.osei.quarantine(kind, "XML_DOCTYPE_REFUSED", url, body)
    scan = _no_dtd_scan(body)
    if scan == "dtd":
        return None, ctx.osei.quarantine(kind, "XML_DOCTYPE_REFUSED", url, original)
    if scan == "malformed":
        return None, ctx.osei.quarantine(kind, "XML_MALFORMED", url, original)
    try:
        return ET.fromstring(body), None
    except (ET.ParseError, ValueError, RecursionError):
        return None, ctx.osei.quarantine(kind, "XML_MALFORMED", url, original)


def _shape(v: str) -> str:
    """A crawled value's character shape (digits -> 9, letters -> a, others kept if ASCII punctuation): how a
    malformed lastmod looked, without its content."""
    out = []
    for c in v[:40]:
        out.append("9" if c in "0123456789" else "a" if c.isalpha() else c if c in "-:.+TZ " else "?")
    return "".join(out)


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
    complete = not facts.get("sitemap_fetch_failures") and not facts.get("robots_unchecked")
    outcome = "OK" if read_any and complete else ("PARTIAL" if read_any else "FAILED")
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
    same = [u for u in declared if ctx.same_site(host_of(u))]
    cross = [u for u in declared if u not in same]
    facts["sitemaps_declared"] = su.safe_urls(declared, 50)
    facts["sitemaps_declared_count"] = len(declared)
    facts["sitemaps_cross_host_not_read"] = su.safe_urls(cross, 50)
    facts["sitemaps_cross_host_count"] = len(cross)
    queue = same[:MAX_CHILDREN] or [ctx.url("/sitemap.xml")]
    seen, urls, lastmods, read, failures, total_urls = set(), [], [], 0, 0, 0
    children_followed = children_cross_host = children_over_cap = 0
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
                ctx.osei.quarantine("sitemap", "TOO_LARGE", sm, None, partial=f.partial)
            missing = f.ok and f.status in (404, 410)        # NIT: 410 Gone is a missing sitemap too
            out.append(finding("SITEMAP_MISSING" if missing else "SITEMAP_UNREADABLE",
                               "medium", "measured", "ACT" if missing else "INSUFFICIENT_EVIDENCE",
                               url=su.safe_url(sm), capability="P3", detail={"fetch_state": f.state,
                                                                             "status": f.status}))
            continue
        body = f.body or b""
        if sm.endswith(".gz") or f.content_type in ("application/gzip", "application/x-gzip"):
            body = _decompress(body)
            if body is None:
                ctx.osei.quarantine("sitemap", "DECOMPRESS_FAILED", sm, f.body)
                out.append(finding("SITEMAP_DECOMPRESS_FAILED", "medium", "measured", "ACT", url=su.safe_url(sm),
                                   capability="P3"))
                continue
        root, q = _xml(ctx, "sitemap", sm, body)
        if root is None:
            out.append(finding("SITEMAP_QUARANTINED", "high", "measured", "ACT", url=su.safe_url(sm), capability="P3",
                               detail={"why": q["reason"]}))
            continue
        read += 1
        ns, name = _local(root.tag)
        if ns != NS:
            out.append(finding("SITEMAP_NAMESPACE", "low", "measured", "ACT", url=su.safe_url(sm), capability="P3",
                               detail={"namespace": su.safe_url(ns) if ns else ""}))
        if name == "sitemapindex":
            for child in root:
                loc = child.find(f"{{{ns}}}loc") if ns else child.find("loc")
                if loc is not None and loc.text:
                    u = loc.text.strip()
                    if not ctx.same_site(host_of(u)):
                        children_cross_host += 1          # NIT: counted, not dropped silently
                    elif children_followed < MAX_CHILDREN:
                        queue.append(u)
                        children_followed += 1
                    else:
                        children_over_cap += 1
            continue
        if name != "urlset":
            out.append(finding("SITEMAP_ROOT_UNKNOWN", "medium", "measured", "ACT", url=su.safe_url(sm),
                               capability="P3", detail={"root": name if name in ROOT_NAMES else "{other}"}))
            continue
        file_urls = 0
        for child in root:
            loc = child.find(f"{{{ns}}}loc") if ns else child.find("loc")
            lm = child.find(f"{{{ns}}}lastmod") if ns else child.find("lastmod")
            if loc is None or not (loc.text or "").strip():
                continue
            file_urls += 1
            if len(urls) < URLS_KEPT_MAX:
                urls.append((loc.text.strip(), sm))
            if lm is not None and lm.text:
                lastmods.append((lm.text.strip(), sm))
        total_urls += file_urls
        if file_urls > MAX_URLS_PER_FILE:
            out.append(finding("SITEMAP_TOO_MANY_URLS", "high", "measured", "ACT", url=su.safe_url(sm),
                               capability="P3", detail={"urls": file_urls, "limit": MAX_URLS_PER_FILE}))
    # round 2: what was read is disclosed, never implied complete (sitemaps attempted vs read; URLs listed vs kept)
    facts.update(sitemaps_read=read, sitemap_fetch_failures=failures, sitemap_urls=total_urls,
                 sitemaps_attempted=len(seen), sitemap_urls_examined=len(urls), sitemap_urls_examined_max=URLS_KEPT_MAX,
                 sitemap_children_followed=children_followed, sitemap_children_cross_host=children_cross_host,
                 sitemap_children_over_cap=children_over_cap)
    templates, digests = [], []
    key = getattr(ctx, "path_key", None)
    for u, _ in urls:
        if len(templates) >= SAMPLE_PATHS:
            break
        p = split_url(u)
        if p is not None and p.scheme in ("http", "https") and ctx.same_site(p.hostname):
            exact = su._path(p.path or "/")
            if exact is None:
                continue
            templates.append(su.safe_path(exact))
            if key:
                digests.append(path_digest(key, exact))
    # for the log task's "important but uncrawled" (Wave 2): templates to show, keyed digests to match (M-S3/M-S4)
    facts["sitemap_paths_sample"] = templates
    facts["sitemap_paths_sample_digests"] = {"key_fp": hashlib.sha256(key).hexdigest()[:16],
                                             "digests": digests} if key else None
    out += _url_checks(ctx, urls, facts)
    out += _lastmod_checks(ctx, lastmods)
    return out


def _url_checks(ctx, urls: list, facts: Optional[dict] = None) -> list:
    out, bad_abs, other_host, blocked = [], [], [], []
    parsed = ctx.robots.get("parsed") if ctx.robots else None
    work = robots_mod.Work(URL_CHECK_WORK)
    checked = 0
    for i, (u, sm) in enumerate(urls):
        p = split_url(u)
        if p is None or p.scheme not in ("http", "https") or not p.hostname:
            bad_abs.append(u)
            continue
        if not ctx.same_site(p.hostname):
            other_host.append(u)
            continue
        if parsed is not None and i < BLOCK_SAMPLE:
            path = (p.path or "/") + (f"?{p.query}" if p.query else "")
            verdict = parsed.allowed("Googlebot", path, work)
            if verdict is not None:
                checked += 1
            if verdict is False:
                blocked.append(u)
    if facts is not None:
        facts["robots_checked"] = checked
        facts["robots_check_sample_max"] = BLOCK_SAMPLE
        facts["robots_check_work"] = {"units": work.units, "limit": work.limit}
    if facts is not None and work.undecided:
        facts["robots_unchecked"] = work.undecided
    if bad_abs:
        out.append(finding("SITEMAP_LOC_NOT_ABSOLUTE", "medium", "measured", "ACT", capability="P3",
                           detail={"count": len(bad_abs), "examples": su.safe_urls(bad_abs, 5)}))
    if other_host:
        out.append(finding("SITEMAP_LOC_OTHER_HOST", "medium", "measured", "ACT", capability="P3",
                           detail={"count": len(other_host), "examples": su.safe_urls(other_host, 5)}))
    if blocked:
        out.append(finding("SITEMAP_LISTS_ROBOTS_BLOCKED_URL", "medium", "measured", "ACT", capability="P3",
                           detail={"count": len(blocked), "examples": su.safe_urls(blocked, 5),
                                   "checked_for": "Googlebot"}))
    if facts is not None and work.undecided:
        out.append(finding("SITEMAP_ROBOTS_CHECK_INCOMPLETE", "info", "measured", "INSUFFICIENT_EVIDENCE",
                           capability="P3", detail={"undecided": work.undecided, "cap": "step_work_units",
                                                    "cap_value": work.limit,
                                                    "note": "listed URLs not checked against robots.txt within "
                                                            "this run's counted work (no claim either way)"}))
    return out


def _lastmod_checks(ctx, lastmods: list) -> list:
    out, invalid, future = [], [], []
    now = ctx.clock.now()
    values = []
    for v, sm in lastmods:
        dt = _parse_lastmod(v)
        if dt is None:
            invalid.append(_shape(v))
            continue
        values.append((v, dt))
        if dt > now + timedelta(days=1):
            future.append(dt.date().isoformat())
    if invalid:
        out.append(finding("SITEMAP_LASTMOD_INVALID", "low", "measured", "ACT", capability="P3",
                           detail={"count": len(invalid), "examples_shape": invalid[:5]}))
    if future:
        out.append(finding("SITEMAP_LASTMOD_FUTURE", "low", "measured", "ACT", capability="P3",
                           detail={"count": len(future), "examples": future[:5]}))
    if len(values) >= 10 and len({v for v, _ in values}) == 1:
        out.append(finding("SITEMAP_LASTMOD_ALL_IDENTICAL", "low", "inferred", "TEST", capability="P3",
                           detail={"count": len(values), "value": values[0][1].date().isoformat(),
                                   "note": "every URL carries the same lastmod; it likely does not reflect real "
                                           "changes (inferred, not measured)"}))
    return out


def check_llms_txt(text: str) -> dict:
    """Shape check against the llmstxt.org proposal: an H1 first, optional '>' summary, H2 sections whose list items
    are markdown links. Returns {"issues": [...], "sections": n, "links": n}."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln.strip()]
    issues, sections, links, bad_links = [], 0, 0, 0
    has_h1 = bool(body) and bool(re.match(r"^#\s+\S", body[0])) and not body[0].startswith("##")
    if not has_h1:
        issues.append("NO_H1_TITLE")
    # NIT: without an H1 the first line is read like any other (an H2 there is a section)
    for ln in (body[1:] if has_h1 else body):
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
        if f.state == "TOO_LARGE":
            ctx.osei.quarantine("llms_txt", "TOO_LARGE", url, None, partial=f.partial)
        return [finding("LLMS_TXT_UNREADABLE", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url, capability="P3",
                        detail={"fetch_state": f.state, "note": note})]
    if f.status >= 500:
        # NIT: a server error says nothing about whether the file exists: unreadable, not absent
        facts["llms_txt"] = {"state": "unreadable", "fetch_state": f.state, "status": f.status}
        return [finding("LLMS_TXT_UNREADABLE", "info", "measured", "INSUFFICIENT_EVIDENCE", url=url, capability="P3",
                        detail={"fetch_state": f.state, "status": f.status, "note": note})]
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
    facts["llms_txt"] = {"state": "present", "content_type": su.safe_ctype(f.content_type), **chk}
    out = []
    if chk["issues"]:
        out.append(finding("LLMS_TXT_FORMAT", "low", "measured", "TEST", url=url, capability="P3",
                           detail={"issues": chk["issues"], "note": note}))
    if f.content_type not in ("text/plain", "text/markdown"):
        out.append(finding("LLMS_TXT_CONTENT_TYPE", "low", "measured", "TEST", url=url, capability="P3",
                           detail={"content_type": su.safe_ctype(f.content_type), "note": note}))
    return out
