"""
Intelligence 6 — Change Watcher (spec C.4).

fetch (port) -> store raw-bytes SHA-256 and normalized-text SHA-256 ->
compare with the last snapshot of that source -> equal normalized hash =
no change (cosmetic discarded) -> classify -> draft a proposal into the
inbox. Deterministic classification:
- a FEED item matches rows when (agency slug, document type) are the
  source's AND a watch term of the row's domain appears in the item title
  or abstract; among the domains that match, only those with the most
  distinct matching terms are kept (ADR 0006 choice 12), and only rows
  whose jurisdiction falls under the source's jurisdictions;
- a PAGE source matches the rows whose ``source_url`` or
  ``additional_sources`` equal its URL, plus the rows its config maps.
The first fetch of a source is a baseline (no proposal). Unmatched items
are dropped and counted. A proposal marks the row ``unverified`` with the
new evidence attached — a changed rule is stale until Andre re-verifies it;
the proposal never changes the register by itself (only Andre's decision
does). Page text is data: injection patterns are counted and recorded,
never obeyed. Never crawls excluded sources (X, Meta: CQ-13); never logs in.
"""

from __future__ import annotations

import email.utils
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from fetcher import WATCH_TERMS, Source
from register import sha256_text
from textguard import has_control_chars, normalize, normalized_page_text

NUMBER, NAME, ACTOR = 6, "Change Watcher", "intel_06_change_watcher"

MAX_ITEMS = 500
_DOC_NUMBER = re.compile(r"/documents/\d{4}/\d{2}/\d{2}/(\d{4}-\d{4,6})/")
_EFFECTIVE = re.compile(r"(?i)\beffective(?: on| date:?)?\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
_MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"), start=1)}


class FeedParseError(Exception):
    pass


@dataclass(frozen=True)
class Item:
    key: str
    title: str
    abstract: str
    link: Optional[str]
    published: Optional[str]      # ISO date
    doc_number: Optional[str]
    effective_date: Optional[str]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _date_from_feed(v: str) -> Optional[str]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return email.utils.parsedate_to_datetime(v).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _effective(text: str) -> Optional[str]:
    m = _EFFECTIVE.search(text)
    if not m:
        return None
    try:
        mon, rest = m.group(1).split(" ", 1)
        day, year = rest.replace(",", "").split()
        return date(int(year), _MONTHS[mon], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


def _safe_url(u: str) -> Optional[str]:
    u = (u or "").strip()
    if u.startswith("https://") and len(u) <= 2048 and " " not in u and not has_control_chars(u):
        return u
    return None


def parse_feed(raw: bytes) -> list[Item]:
    """RSS 2.0 or Atom. DOCTYPE / ENTITY declarations are refused before parsing
    (no entity expansion of hostile feeds)."""
    head = raw[:4096].lower()
    if b"<!doctype" in raw.lower() or b"<!entity" in raw.lower() or b"<!doctype" in head:
        raise FeedParseError("feeds with DOCTYPE or ENTITY declarations are refused")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FeedParseError("feed is not well-formed XML") from exc
    items: list[Item] = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        kids = {_local(c.tag): c for c in el}

        def first(*names):
            # (an Element with no children is falsy: never chain them with `or`)
            for n in names:
                if kids.get(n) is not None:
                    return kids[n]
            return None
        title = _text(kids.get("title"))[:500]
        abstract = _text(first("description", "summary", "content"))[:4000]
        link_el = kids.get("link")
        link = None
        if link_el is not None:
            link = _safe_url(link_el.get("href") or _text(link_el))
        key = _text(first("guid", "id")) or link or sha256_text(title + abstract)
        published = _date_from_feed(_text(first("pubdate", "published", "updated")))
        m = _DOC_NUMBER.search(link or "")
        items.append(Item(key=key[:500], title=title, abstract=abstract, link=link, published=published,
                          doc_number=m.group(1) if m else None, effective_date=_effective(f"{title} {abstract}")))
        if len(items) >= MAX_ITEMS:
            break
    return items


def _term_hits(text: str, terms: tuple[str, ...]) -> set[str]:
    n = normalize(text)
    hits = set()
    for t in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(normalize(t)) + r"s?(?![a-z0-9])", n):
            hits.add(t)
    return hits


def classify_item(item: Item, source: Source, rows: list[dict]) -> list[dict]:
    text = f"{item.title} {item.abstract}"
    by_domain = {d: _term_hits(text, terms) for d, terms in WATCH_TERMS.items()}
    best = max((len(h) for h in by_domain.values()), default=0)
    if best == 0:
        return []
    domains = {d for d, h in by_domain.items() if len(h) == best}
    out = []
    for r in rows:
        if r["domain"] not in domains or r["status"] == "superseded":
            continue
        if not any(r["jurisdiction"] == j or r["jurisdiction"].startswith(j + "-") for j in source.jurisdictions):
            continue
        out.append(r)
    return out


def page_rows(source: Source, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r["status"] == "superseded":
            continue
        if r["id"] in source.row_ids or r.get("source_url") == source.url or source.url in (r.get("additional_sources") or []):
            out.append(r)
    return out


def first_difference_excerpt(old_text: str, new_text: str, limit: int = 2000) -> str:
    i = 0
    n = min(len(old_text), len(new_text))
    while i < n and old_text[i] == new_text[i]:
        i += 1
    start = max(0, i - 200)
    return new_text[start:start + limit]


def changed_row(row: dict, extra_source: Optional[str], note: str) -> dict:
    """The drafted row: unverified (stale until re-verified), with the new source attached."""
    sources = list(row.get("additional_sources") or [])
    if extra_source and extra_source != row.get("source_url") and extra_source not in sources and len(sources) < 20:
        sources.append(extra_source)
    return {**row, "status": "unverified", "verified_at": None, "expires_at": None,
            "additional_sources": sources, "effective_note": note[:300]}


SNAPSHOT_TEXT_MAX = 200_000


def snapshot(raw: bytes) -> dict:
    text = normalized_page_text(raw)
    return {"raw_sha256": hashlib.sha256(raw).hexdigest(), "normalized_sha256": sha256_text(text),
            "text": text[:SNAPSHOT_TEXT_MAX]}
