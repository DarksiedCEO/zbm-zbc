"""
Primitive: parse. HTML -> a bounded, plain-data extract (stdlib ``html.parser``: tolerant of malformed markup, never
executes anything). JSON-LD blocks are parsed and validated against this service's own rule table for common
schema.org types (``SCHEMA_RULES``, versioned) — that is NOT Google's Rich Results Test and is never presented as
one.

Every string taken from the page is crawled content: it is DATA. The extract keeps bounded copies so a report can
quote them (through envelope.observed), and nothing here or downstream acts on what the text says.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit

MAX_LINKS = 2000
MAX_HEADINGS = 300
MAX_JSONLD_BLOCKS = 30
MAX_JSONLD_BYTES = 256 * 1024
MAX_TEXT = 200_000
STR_MAX = 500
SCHEMA_RULES_VERSION = "2026-10-09.1"
# type -> (required properties, recommended properties). Source note: drawn from schema.org type definitions and
# the search engines' public structured-data documentation as read on the version date; NOT complete, and a
# rule here is this service's heuristic, not a search engine's requirement.
SCHEMA_RULES = {
    "Organization": (("name",), ("url", "logo", "sameAs")),
    "LocalBusiness": (("name", "address"), ("telephone", "url", "openingHoursSpecification", "geo")),
    "ProfessionalService": (("name", "address"), ("telephone", "url")),
    "Store": (("name", "address"), ("telephone", "url")),
    "WebSite": (("name", "url"), ()),
    "WebPage": ((), ("name",)),
    "Article": (("headline",), ("author", "datePublished", "image")),
    "BlogPosting": (("headline",), ("author", "datePublished", "image")),
    "NewsArticle": (("headline",), ("author", "datePublished", "image")),
    "Product": (("name",), ("offers", "aggregateRating", "review", "image")),
    "FAQPage": (("mainEntity",), ()),
    "BreadcrumbList": (("itemListElement",), ()),
    "PostalAddress": ((), ("streetAddress", "addressLocality", "addressRegion", "postalCode")),
}
LOCAL_BUSINESS_TYPES = ("LocalBusiness", "ProfessionalService", "Store")
ORG_TYPES = ("Organization",) + LOCAL_BUSINESS_TYPES


def _s(v: Optional[str], n: int = STR_MAX) -> Optional[str]:
    if v is None:
        return None
    v = " ".join(str(v).split())
    return v[:n]


class _Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title: Optional[str] = None
        self._in_title = False
        self.metas: list = []
        self.links_rel: list = []
        self.headings: list = []
        self._heading: Optional[list] = None
        self.anchors: list = []
        self._anchor: Optional[dict] = None
        self.scripts_ld: list = []
        self._ld: Optional[list] = None
        self._skip = 0                       # inside script / style / noscript / template
        self.text_parts: list = []
        self.text_len = 0
        self.html_lang: Optional[str] = None
        self.elements = 0
        self.script_count = 0

    def handle_starttag(self, tag, attrs):
        self.elements += 1
        a = {k.lower(): (v or "") for k, v in attrs if k}
        if tag == "html" and self.html_lang is None:
            self.html_lang = _s(a.get("lang"), 35)
        elif tag == "title" and self.title is None:
            self._in_title = True
            self.title = ""
        elif tag == "meta":
            self.metas.append({k: _s(v) for k, v in a.items() if k in ("name", "property", "content", "http-equiv",
                                                                       "charset")})
        elif tag == "link":
            if len(self.links_rel) < MAX_LINKS:
                self.links_rel.append({"rel": _s(a.get("rel", ""), 100).lower(), "href": _s(a.get("href"), 2000),
                                       "hreflang": _s(a.get("hreflang"), 35)})
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading = [int(tag[1]), ""]
        elif tag == "a":
            self._anchor = {"href": _s(a.get("href"), 2000), "rel": _s(a.get("rel", ""), 100).lower(), "text": ""}
        elif tag == "script":
            self.script_count += 1
            self._skip += 1
            if a.get("type", "").strip().lower() == "application/ld+json":
                self._ld = []
        elif tag in ("style", "noscript", "template"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading is not None:
            if len(self.headings) < MAX_HEADINGS:
                self.headings.append({"level": self._heading[0], "text": _s(self._heading[1], 300)})
            self._heading = None
        elif tag == "a" and self._anchor is not None:
            if len(self.anchors) < MAX_LINKS:
                self._anchor["text"] = _s(self._anchor["text"], 200)
                self.anchors.append(self._anchor)
            self._anchor = None
        elif tag == "script":
            self._skip = max(0, self._skip - 1)
            if self._ld is not None:
                if len(self.scripts_ld) < MAX_JSONLD_BLOCKS:
                    self.scripts_ld.append("".join(self._ld)[:MAX_JSONLD_BYTES + 1])
                self._ld = None
        elif tag in ("style", "noscript", "template"):
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if self._skip:
            return
        if self._in_title and self.title is not None and len(self.title) < 2000:
            self.title += data
        if self._heading is not None and len(self._heading[1]) < 2000:
            self._heading[1] += data
        if self._anchor is not None and len(self._anchor["text"]) < 1000:
            self._anchor["text"] += data
        if self.text_len < MAX_TEXT:
            self.text_parts.append(data)
            self.text_len += len(data)


def parse_html(html: str, base_url: str) -> dict:
    """The page extract. Never raises on malformed input: a parser failure is reported in ``parse_error``."""
    p = _Extractor()
    error = None
    try:
        p.feed(html)
        p.close()
    except Exception as exc:                       # html.parser is tolerant; anything left is reported, not raised
        error = type(exc).__name__
    metas = p.metas
    robots_meta = {}
    description = None
    for mt in metas:
        name = (mt.get("name") or "").lower()
        if name in ("robots", "googlebot", "bingbot", "gptbot", "claudebot", "perplexitybot") and mt.get("content"):
            robots_meta[name] = [x.strip().lower() for x in mt["content"].split(",") if x.strip()][:20]
        elif name == "description" and description is None:
            description = mt.get("content")
    canonicals = [lk["href"] for lk in p.links_rel if "canonical" in lk["rel"].split() and lk["href"]]
    hreflang = [{"hreflang": lk["hreflang"], "href": _abs(base_url, lk["href"])} for lk in p.links_rel
                if "alternate" in lk["rel"].split() and lk["hreflang"] and lk["href"]]
    links = []
    for an in p.anchors:
        href = an["href"]
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        u = _abs(base_url, href)
        if u:
            links.append({"url": u, "nofollow": "nofollow" in an["rel"].split(), "text": an["text"]})
    text = " ".join(" ".join(p.text_parts).split())
    return {
        "title": _s(p.title, 1000), "description": description, "lang": p.html_lang,
        "robots_meta": robots_meta, "canonicals": [_abs(base_url, c) for c in canonicals][:10],
        "hreflang": hreflang[:200], "headings": p.headings, "links": links,
        "jsonld_raw_count": len(p.scripts_ld), "jsonld": [validate_jsonld(b) for b in p.scripts_ld],
        "text_length": len(text), "text_sample": text[:20_000], "elements": p.elements,
        "scripts": p.script_count, "parse_error": error,
        "viewport": any((m.get("name") or "").lower() == "viewport" for m in metas),
    }


def _abs(base: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    try:
        u = urljoin(base, href.strip())
        parts = urlsplit(u)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    return u.split("#", 1)[0][:2000]


# ------------------------------------------------------------------------------------------- JSON-LD

def _types(node: dict) -> list:
    t = node.get("@type")
    if isinstance(t, str):
        return [t.split("/")[-1].split(":")[-1]]
    if isinstance(t, list):
        return [x.split("/")[-1].split(":")[-1] for x in t if isinstance(x, str)][:10]
    return []


def _context_ok(ctx) -> bool:
    if isinstance(ctx, str):
        return bool(re.fullmatch(r"https?://schema\.org/?", ctx.strip()))
    if isinstance(ctx, list):
        return any(_context_ok(c) for c in ctx)
    if isinstance(ctx, dict):
        return _context_ok(ctx.get("@vocab"))
    return False


def _nodes(doc) -> list:
    """Top-level nodes of a JSON-LD document (object, array, or @graph)."""
    if isinstance(doc, list):
        return [n for n in doc if isinstance(n, dict)][:100]
    if isinstance(doc, dict):
        if isinstance(doc.get("@graph"), list):
            return [{**n, "@context": n.get("@context", doc.get("@context"))} for n in doc["@graph"]
                    if isinstance(n, dict)][:100]
        return [doc]
    return []


def _present(v) -> bool:
    return v not in (None, "", [], {})


def validate_jsonld(raw: str) -> dict:
    """One ``<script type="application/ld+json">`` block -> {valid_json, nodes: [{types, issues, props}]}."""
    if len(raw) > MAX_JSONLD_BYTES:
        return {"valid_json": False, "error": "BLOCK_TOO_LARGE", "nodes": []}
    try:
        doc = json.loads(raw.strip().removeprefix("<!--").removesuffix("-->"))
    except (ValueError, RecursionError):
        return {"valid_json": False, "error": "JSON_INVALID", "nodes": []}
    out = []
    for n in _nodes(doc):
        types = _types(n)
        issues = []
        if not _context_ok(n.get("@context")):
            issues.append({"code": "CONTEXT_NOT_SCHEMA_ORG", "level": "error"})
        if not types:
            issues.append({"code": "TYPE_MISSING", "level": "error"})
        for t in types:
            req, rec = SCHEMA_RULES.get(t, ((), ()))
            issues += [{"code": "REQUIRED_MISSING", "level": "error", "type": t, "property": pr}
                       for pr in req if not _present(n.get(pr))]
            issues += [{"code": "RECOMMENDED_MISSING", "level": "warning", "type": t, "property": pr}
                       for pr in rec if not _present(n.get(pr))]
        addr = n.get("address")
        if isinstance(addr, dict) and any(t in LOCAL_BUSINESS_TYPES for t in types):
            for pr in ("streetAddress", "addressLocality", "addressRegion"):
                if not _present(addr.get(pr)):
                    issues.append({"code": "ADDRESS_PART_MISSING", "level": "warning", "property": pr})
        out.append({"types": types, "known_types": [t for t in types if t in SCHEMA_RULES], "issues": issues[:50],
                    "props": _entity_props(n)})
    return {"valid_json": True, "error": None, "nodes": out[:100]}


def _entity_props(n: dict) -> dict:
    """The entity fields the consistency check and Roman use (bounded strings only)."""
    def s(v):
        return _s(v, 300) if isinstance(v, (str, int, float)) else None
    addr = n.get("address") if isinstance(n.get("address"), dict) else {}
    same_as = n.get("sameAs")
    same_as = [x for x in (same_as if isinstance(same_as, list) else [same_as]) if isinstance(x, str)][:20]
    return {"name": s(n.get("name")), "url": s(n.get("url")), "telephone": s(n.get("telephone")),
            "street_address": s(addr.get("streetAddress")), "locality": s(addr.get("addressLocality")),
            "region": s(addr.get("addressRegion")), "postal_code": s(addr.get("postalCode")),
            "same_as": [_s(x, 300) for x in same_as]}
