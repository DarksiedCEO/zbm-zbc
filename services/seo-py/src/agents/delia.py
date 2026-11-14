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
from envelope import envelope
from envelope import finding as _finding
from primitives import Killed
from primitives import codes
from primitives import fetch as fetch_mod
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


def finding(*a, url: Optional[str] = None, **k) -> dict:
    """A finding whose URL is in its stored form (spec B-CD-03)."""
    return _finding(*a, url=su.safe_url(url), **k)


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


# B-DE-03 / Q-07: a sitemap is read up to the sitemaps.org limit of 52,428,800 bytes UNCOMPRESSED, streamed: the
# decoded bytes go through an incremental expat parser and only the locs / lastmods are kept (bounded), so memory stays
# within the request bound whatever the file size.
PROTOCOL_MAX_BYTES = 52_428_800
# B-DE-03 (rv-wave1v2 #3): the wire cap sits above the protocol limit, so a plain body over the limit is measured by
# the reader (decoded size > limit -> SITEMAP_TOO_LARGE ACT), never cut by the fetch first (-> UNREADABLE IE); gzip
# adds at most a few octets per 64 KiB, so a gzip body over this on the wire decodes over the limit too
WIRE_MAX = PROTOCOL_MAX_BYTES + 1024 * 1024
DECODE_STEP = fetch_mod.DECODE_STEP          # A-11 (rv-wave1v2 #4): the most one zlib call returns
_TAIL = 16


class _Stop(fetch_mod.SinkStop):
    pass


class _SitemapReader:
    """One sitemap body, streamed: wire size and SHA-256 (``size_basis: wire``), bounded gzip decoding, XML 1.0
    encoding detection, the DOCTYPE / ENTITY text check on the DECODED text, and one expat pass with DTDs refused.
    ``state``: None (read), "TOO_LARGE" (decoded size over the protocol limit), "DECOMPRESS_FAILED", "DTD",
    "MALFORMED"."""

    def __init__(self, gz: bool, kept_max: int):
        self.gz, self.kept_max = gz, kept_max
        self.wire = hashlib.sha256()
        self.wire_n = 0
        self.decoded = hashlib.sha256()
        self.n = 0
        self.state: Optional[str] = None
        self.z = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
        self.sniff: Optional[bytes] = None if gz else b""
        self.head = b""
        self.enc: Optional[str] = None
        self.text_dec = None
        self.u32 = None
        self.tail = ""
        self.p = None
        self.root: Optional[tuple] = None
        self.depth = 0
        self.cur: Optional[str] = None
        self.item: dict = {}
        self.items: list = []                 # (loc, lastmod or None) of urlset / (loc, None) of sitemapindex
        self.count = 0

    # ------------------------------------------------------------------ the fetch sink
    def feed(self, chunk: bytes) -> None:
        if self.state is not None:
            raise _Stop()
        self.wire.update(chunk)
        self.wire_n += len(chunk)
        if self.sniff is not None:
            # a gzip body is known by its name (.gz) or by its first two octets (served as application/gzip, or
            # with no type at all): no XML document starts with 0x1f 0x8b
            self.sniff += chunk
            if len(self.sniff) < 2 and chunk:
                return
            chunk, self.sniff = self.sniff, None
            if chunk[:2] == b"\x1f\x8b":
                self.z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        if self.z is None:
            self._decoded(chunk)
            return
        buf = chunk
        try:
            while True:
                # bounded output per call (max_length), streamed on to the parser; zlib's held output is drained
                out = self.z.decompress(buf, min(DECODE_STEP, PROTOCOL_MAX_BYTES - self.n + 1))
                self._decoded(out)
                buf = self.z.unconsumed_tail
                if not self.z.eof:
                    if not buf and len(out) < DECODE_STEP:
                        break
                    continue
                rest = self.z.unused_data
                if not rest:
                    break
                self.z = zlib.decompressobj(16 + zlib.MAX_WBITS)
                buf = rest
        except zlib.error:
            self.state = "DECOMPRESS_FAILED"
            raise _Stop() from None

    def finish(self) -> None:
        try:
            self._finish()
        except _Stop:
            pass

    def _finish(self) -> None:
        if self.state is not None:
            return
        if self.sniff:                              # a one-octet body: not gzip
            chunk, self.sniff = self.sniff, None
            self._decoded(chunk)
        if self.z is not None and not self.z.eof:
            self.state = "DECOMPRESS_FAILED"        # a truncated gzip (12 bytes of a member) is not a sitemap
            return
        if self.p is None:
            self._start(force=True)
        if self.state is None and self.p is not None:
            self._xml(b"", final=True)

    # ------------------------------------------------------------------ decoded bytes
    def _decoded(self, b: bytes) -> None:
        if not b:
            return
        self.n += len(b)
        self.decoded.update(b)                      # B-OS-01: the hash is of every byte counted in ``n``
        if self.n > PROTOCOL_MAX_BYTES:
            self.state = "TOO_LARGE"
            raise _Stop()
        if self.p is None:
            self.head += b
            if len(self.head) < 256:
                return
            self._start()
            return
        self._xml(b)

    def _start(self, force: bool = False) -> None:
        body = self.head
        self.head = b""
        if not body and force:
            self.state = "MALFORMED"
            return
        self.enc = xml_encoding(body)
        try:
            self.text_dec = codecs.getincrementaldecoder(self.enc)(errors="replace")
        except LookupError:
            self.text_dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        if self.enc.startswith(("utf-32", "utf-16")):
            # expat reads no UTF-32, and refuses a UTF-16 document whose declaration names the encoding in a
            # spelling it does not know ("utf-16-le"): both are decoded here and handed on as UTF-8, the
            # declaration dropped (it names the old encoding)
            self.u32 = codecs.getincrementaldecoder(self.enc)(errors="strict")
        p = expat.ParserCreate(namespace_separator=" ")
        p.StartDoctypeDeclHandler = _refuse
        p.EntityDeclHandler = _refuse
        p.UnparsedEntityDeclHandler = _refuse
        p.ExternalEntityRefHandler = _refuse
        p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        p.StartElementHandler = self._open
        p.EndElementHandler = self._close
        p.CharacterDataHandler = self._chars
        p.buffer_text = True
        self.p = p
        self._xml(body, first=True)

    def _xml(self, b: bytes, first: bool = False, final: bool = False) -> None:
        # the DOCTYPE / ENTITY declaration check on the DECODED text (AEGIS Wave-1 M-D1), across chunk ends
        t = self.text_dec.decode(b, final=final)
        low = (self.tail + t).lower()
        if "<!doctype" in low or "<!entity" in low:
            self.state = "DTD"
            raise _Stop()
        self.tail = low[-_TAIL:]
        data = b
        if self.u32 is not None:                # expat cannot read UTF-32: handed on as UTF-8, declaration dropped
            try:
                txt = self.u32.decode(b, final=final)
            except UnicodeDecodeError:
                self.state = "MALFORMED"
                raise _Stop() from None
            if first:
                txt = _DECL.sub("", txt.lstrip("\ufeff"), count=1)
            data = txt.encode("utf-8")
        try:
            self.p.Parse(data, final)
        except _DTDRefused:
            self.state = "DTD"
            raise _Stop() from None
        except (expat.ExpatError, ValueError, RecursionError):
            self.state = "MALFORMED"
            raise _Stop() from None

    # ------------------------------------------------------------------ expat events
    def _open(self, name, attrs) -> None:
        self.depth += 1
        if self.root is None:
            ns, _, local = name.rpartition(" ")
            self.root = (ns, local)
            return
        local = name.rpartition(" ")[2]
        if self.depth == 2:
            self.item = {}
        elif self.depth == 3 and local in ("loc", "lastmod"):
            self.cur = local
            self.item[local] = ""

    def _chars(self, data) -> None:
        if self.cur is not None and len(self.item.get(self.cur, "")) < 4096:
            self.item[self.cur] += data

    def _close(self, name) -> None:
        if self.depth == 3:
            self.cur = None
        elif self.depth == 2:
            loc = (self.item.get("loc") or "").strip()
            if loc:
                self.count += 1
                if len(self.items) < self.kept_max:
                    lm = self.item.get("lastmod")
                    self.items.append((loc, lm.strip() if lm and lm.strip() else None))
        self.depth -= 1


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
    """The run inside the stored-form context (primitives/codes.py ``within``: the service sets its own)."""
    with codes.within(getattr(ctx, "domain", None)):
        return _run(ctx)


def _run(ctx) -> dict:
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
        kept_left = max(0, URLS_KEPT_MAX - len(urls))
        gz_name = sm.split("?", 1)[0].lower().endswith(".gz")
        reader = _SitemapReader(gz_name, kept_left)
        f = ctx.fetcher.fetch(sm, robots_cache=ctx.robots_cache, guard=ctx.guard, sink=reader.feed,
                              max_bytes=WIRE_MAX, accept="application/xml,text/xml;q=0.9,*/*;q=0.5")
        if f.state == "KILLED":
            raise Killed(f.detail or "KILLED")
        ctx.osei.observed("sitemap", sm, f.state)
        if not f.ok or f.status != 200:
            failures += 1                                    # B-DE-02 / B-DE-16: counted, the outcome is PARTIAL
            missing = f.ok and f.status in (404, 410)        # NIT: 410 Gone is a missing sitemap too
            if f.state == "TOO_LARGE":
                ctx.osei.quarantine("sitemap", "TOO_LARGE", sm, None, partial=f.partial)
            out.append(finding("SITEMAP_MISSING" if missing else "SITEMAP_UNREADABLE",
                               "medium", "measured", "ACT" if missing else "INSUFFICIENT_EVIDENCE",
                               url=su.safe_url(sm), capability="P3", detail={"fetch_state": f.state,
                                                                             "status": f.status}))
            continue
        reader.finish()
        if reader.state == "TOO_LARGE":
            # B-OS-01 (rv-wave1v2 #5): the decoded bytes read before the stop, their real size and SHA-256
            cl = f.headers.get("content-length") or ""
            ctx.osei.quarantine("sitemap", "TOO_LARGE", sm, None, partial={
                "bytes_read": reader.n, "sha256": reader.decoded.hexdigest(),
                "declared": int(cl) if cl.isdigit() and len(cl) < 19 else None})
            out.append(finding("SITEMAP_TOO_LARGE", "medium", "measured", "ACT", url=su.safe_url(sm), capability="P3",
                               detail={"limit": PROTOCOL_MAX_BYTES, "note": "the sitemaps.org protocol limits a "
                                       "sitemap to 52,428,800 bytes uncompressed; this one is larger"}))
            continue
        if reader.state == "DECOMPRESS_FAILED":
            ctx.osei.quarantine("sitemap", "DECOMPRESS_FAILED", sm, None, size=reader.wire_n,
                                digest=reader.wire.hexdigest(), basis="wire")
            out.append(finding("SITEMAP_DECOMPRESS_FAILED", "medium", "measured", "ACT", url=su.safe_url(sm),
                               capability="P3"))
            continue
        if reader.state in ("DTD", "MALFORMED"):
            reason = "XML_DOCTYPE_REFUSED" if reader.state == "DTD" else "XML_MALFORMED"
            q = ctx.osei.quarantine("sitemap", reason, sm, None, size=reader.n, digest=reader.decoded.hexdigest(),
                                    basis="decoded")
            out.append(finding("SITEMAP_QUARANTINED", "high", "measured", "ACT", url=su.safe_url(sm), capability="P3",
                               detail={"why": q["reason"]}))
            continue
        read += 1
        ns, name = reader.root or ("", "")
        if ns != NS:
            out.append(finding("SITEMAP_NAMESPACE", "low", "measured", "ACT", url=su.safe_url(sm), capability="P3",
                               detail={"namespace": codes.code("ns", ns) if ns else ""}))
        if name == "sitemapindex":
            for u, _ in reader.items:
                if not ctx.same_site(host_of(u)):
                    children_cross_host += 1          # NIT: counted, not dropped silently
                elif children_followed < MAX_CHILDREN:
                    queue.append(u)
                    children_followed += 1
                else:
                    children_over_cap += 1
            children_over_cap += max(0, reader.count - len(reader.items))
            continue
        if name != "urlset":
            out.append(finding("SITEMAP_ROOT_UNKNOWN", "medium", "measured", "ACT", url=su.safe_url(sm),
                               capability="P3", detail={"root": name if name in ROOT_NAMES else
                                                        codes.code("root", name)}))
            continue
        file_urls = reader.count
        for u, lm in reader.items:
            urls.append((u, sm))
            if lm is not None:
                lastmods.append((lm, sm))
        total_urls += file_urls
        if file_urls > MAX_URLS_PER_FILE:
            out.append(finding("SITEMAP_TOO_MANY_URLS", "high", "measured", "ACT", url=su.safe_url(sm),
                               capability="P3", detail={"urls": file_urls, "limit": MAX_URLS_PER_FILE}))
    # round 2: what was read is disclosed, never implied complete (sitemaps attempted vs read; URLs listed vs kept)
    facts.update(sitemaps_read=read, sitemap_fetch_failures=failures, sitemap_urls=total_urls,
                 sitemaps_attempted=len(seen), sitemap_urls_examined=len(urls), sitemap_urls_examined_max=URLS_KEPT_MAX,
                 sitemap_children_followed=children_followed, sitemap_children_cross_host=children_cross_host,
                 sitemap_children_over_cap=children_over_cap)
    templates, sample_codes = [], []
    for u, _ in urls:
        if len(templates) >= SAMPLE_PATHS:
            break
        p = split_url(u)
        if p is not None and p.scheme in ("http", "https") and ctx.same_site(p.hostname):
            exact = su.decoded_path(p.path or "/")
            if exact is None:
                continue
            templates.append(su.safe_path(exact))
            sample_codes.append(codes.code("path", exact))
    # for the log task's "important but uncrawled" (B-LG-05): stored forms to show and the code of each whole path
    # (keyed per tenant, B-CD-01) to match a log line's path, coded the same way in memory
    facts["sitemap_paths_sample"] = templates
    facts["sitemap_paths_sample_codes"] = sample_codes
    out += _url_checks(ctx, urls, facts)
    out += _lastmod_checks(ctx, lastmods)
    return out


def _url_checks(ctx, urls: list, facts: Optional[dict] = None) -> list:
    out, bad_abs, other_host, blocked = [], [], [], []
    parsed = ctx.robots.get("parsed") if ctx.robots else None
    work = robots_mod.Work(URL_CHECK_WORK)
    checked = 0
    sampled = 0     # B-DE-11 (rv-wave1v2 #2): the sample is the first BLOCK_SAMPLE same-site, valid URLs
    for u, sm in urls:
        p = split_url(u)
        if p is None or p.scheme not in ("http", "https") or not p.hostname:
            bad_abs.append(u)
            continue
        if not ctx.same_site(p.hostname):
            other_host.append(u)
            continue
        if parsed is not None and sampled < BLOCK_SAMPLE:
            sampled += 1
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
    if facts is not None:
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
            invalid.append(codes.code("date", v))
            continue
        values.append((v, dt))
        if dt > now + timedelta(days=1):
            future.append(codes.code("date", v))
    if invalid:
        out.append(finding("SITEMAP_LASTMOD_INVALID", "low", "measured", "ACT", capability="P3",
                           detail={"count": len(invalid), "examples": invalid[:5]}))
    if future:
        out.append(finding("SITEMAP_LASTMOD_FUTURE", "low", "measured", "ACT", capability="P3",
                           detail={"count": len(future), "examples": future[:5]}))
    if len(values) >= 10 and len({v for v, _ in values}) == 1:
        out.append(finding("SITEMAP_LASTMOD_ALL_IDENTICAL", "low", "inferred", "TEST", capability="P3",
                           detail={"count": len(values), "value": codes.code("date", values[0][0]),
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
