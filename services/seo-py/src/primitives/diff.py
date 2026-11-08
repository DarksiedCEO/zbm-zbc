"""
Primitives: diff, access-diff, link-diff, change-detect. Pure functions over extracts (no I/O).

access-diff compares what this crawler (identified bot UA) was served with what a browser identity was served. It
detects user-agent-based differences only: a site that serves bots differently by verified IP range (as the major
AI and search crawlers' operators publish) is NOT detectable this way, and the report says so.

change-detect separates "the page changed" from "we could not see it": a failed fetch is TOOL_FAILURE, never a
change (search-truth drift: real change vs measurement error vs tool failure).
"""

from __future__ import annotations

import difflib
import hashlib
from typing import Optional
from urllib.parse import urlsplit

DIFF_MAX_LINES = 2000
TEXT_RATIO_MIN = 0.7          # access-diff: visible text length ratio below this is a differential


def _h(s) -> str:
    return hashlib.sha256(repr(s).encode("utf-8", "surrogatepass")).hexdigest()


def diff(a: str, b: str) -> dict:
    """Line diff summary of two texts (counts and hashes only; no content)."""
    la, lb = a.splitlines()[:DIFF_MAX_LINES], b.splitlines()[:DIFF_MAX_LINES]
    sm = difflib.SequenceMatcher(a=la, b=lb, autojunk=False)
    added = removed = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed += i2 - i1
        if op in ("replace", "insert"):
            added += j2 - j1
    return {"changed": a != b, "sha256_a": _h(a), "sha256_b": _h(b), "lines_added": added, "lines_removed": removed,
            "similarity": round(sm.ratio(), 4)}


def link_diff(a: list, b: list) -> dict:
    sa = {x["url"] if isinstance(x, dict) else x for x in a}
    sb = {x["url"] if isinstance(x, dict) else x for x in b}
    return {"added": sorted(sb - sa)[:200], "removed": sorted(sa - sb)[:200], "added_count": len(sb - sa),
            "removed_count": len(sa - sb), "common": len(sa & sb)}


def fingerprint(extract: dict) -> dict:
    """The fields change-detect compares."""
    return {"title": _h(extract.get("title")), "canonicals": _h(extract.get("canonicals")),
            "robots_meta": _h(extract.get("robots_meta")), "headings": _h(extract.get("headings")),
            "jsonld": _h(extract.get("jsonld")), "text": _h(extract.get("text_sample")),
            "links": _h(sorted(x["url"] for x in extract.get("links") or []))}


def change_detect(previous: Optional[dict], current: Optional[dict], fetch_state: str) -> dict:
    """``previous`` / ``current``: fingerprints. CHANGED, UNCHANGED, BASELINE (nothing to compare with) or
    TOOL_FAILURE (the fetch failed: no claim about the page)."""
    if fetch_state != "OK" or current is None:
        return {"state": "TOOL_FAILURE", "changed_fields": []}
    if previous is None:
        return {"state": "BASELINE", "changed_fields": []}
    changed = sorted(k for k in current if current.get(k) != previous.get(k))
    return {"state": "CHANGED" if changed else "UNCHANGED", "changed_fields": changed}


# A browser-identity request answered with one of these while our crawler was served the page is a bot-protection
# challenge or refusal of the (datacenter) browser request, not a page served differently to crawlers: INCONCLUSIVE.
CHALLENGE_STATUSES = (401, 403, 405, 429, 503)


def _norm_url(u) -> str:
    """A URL for comparison: scheme and host without case, default port and one trailing slash ignored."""
    if not isinstance(u, str):
        return ""
    try:
        p = urlsplit(u.strip())
        port = p.port
    except ValueError:
        return u.strip().rstrip("/")
    scheme = p.scheme.lower()
    host = (p.hostname or "").rstrip(".")
    netloc = host + (f":{port}" if port not in (None, {"http": 80, "https": 443}.get(scheme)) else "")
    return f"{scheme}://{netloc}{(p.path or '/').rstrip('/') or '/'}" + (f"?{p.query}" if p.query else "")


def _norm_urls(urls) -> list:
    return sorted({_norm_url(u) for u in urls or []})


def access_diff(bot_fetch, human_fetch, bot_extract: Optional[dict], human_extract: Optional[dict]) -> dict:
    """SAME, BOT_DIFFERENTIAL or INCONCLUSIVE, with the differing signals."""
    if bot_fetch.state not in ("OK", "BLOCKED_BY_ROBOTS") or human_fetch.state != "OK":
        return {"state": "INCONCLUSIVE", "signals": [], "why": "a fetch failed (no claim either way)"}
    signals = []
    if bot_fetch.state == "BLOCKED_BY_ROBOTS":
        return {"state": "INCONCLUSIVE", "signals": [], "why": "robots.txt disallows our crawler on this path"}
    if bot_fetch.status in CHALLENGE_STATUSES and human_fetch.status in CHALLENGE_STATUSES:
        return {"state": "INCONCLUSIVE", "signals": [], "why": "both requests were refused or challenged"}
    if bot_fetch.status in CHALLENGE_STATUSES and (human_fetch.status or 0) < 400:
        # Review of PR 47 (H3): our crawler refused while a browser identity is served is the site refusing UNKNOWN
        # crawlers (bot protection), not cloaking and not an error visitors see. Whether the major search and AI
        # crawlers (verified by IP) are refused too cannot be told from here.
        return {"state": "CRAWLER_REFUSED", "signals": [], "bot_status": bot_fetch.status,
                "why": "the site refused our crawler but served a browser identity; whether verified search and AI "
                       "crawlers are refused cannot be observed from here"}
    if human_fetch.status in CHALLENGE_STATUSES and (bot_fetch.status or 0) < 400:
        return {"state": "INCONCLUSIVE", "signals": [],
                "why": "the browser-identity request was challenged or refused (bot protection); no claim either way"}
    if bot_fetch.status != human_fetch.status:
        signals.append({"signal": "status", "bot": bot_fetch.status, "human": human_fetch.status})
    if _norm_url(bot_fetch.final_url) != _norm_url(human_fetch.final_url):
        signals.append({"signal": "final_url"})
    if bot_extract is not None and human_extract is not None:
        if bot_extract.get("title") != human_extract.get("title"):
            signals.append({"signal": "title"})
        if _norm_urls(bot_extract.get("canonicals")) != _norm_urls(human_extract.get("canonicals")):
            signals.append({"signal": "canonicals"})
        if bot_extract.get("robots_meta") != human_extract.get("robots_meta"):
            signals.append({"signal": "robots_meta"})
        if [x["text"] for x in bot_extract["headings"] if x["level"] == 1] != \
                [x["text"] for x in human_extract["headings"] if x["level"] == 1]:
            signals.append({"signal": "h1"})
        a, b = bot_extract.get("text_length", 0), human_extract.get("text_length", 0)
        if max(a, b) > 0 and min(a, b) / max(a, b) < TEXT_RATIO_MIN:
            signals.append({"signal": "text_length", "bot": a, "human": b})
        ld = link_diff(bot_extract.get("links") or [], human_extract.get("links") or [])
        total = ld["common"] + ld["added_count"] + ld["removed_count"]
        if total and (ld["added_count"] + ld["removed_count"]) / total > 1 - TEXT_RATIO_MIN:
            signals.append({"signal": "links", "only_human": ld["added_count"], "only_bot": ld["removed_count"]})
    return {"state": "BOT_DIFFERENTIAL" if signals else "SAME", "signals": signals}


def confirm(first: dict, repeat: Optional[dict]) -> dict:
    """A BOT_DIFFERENTIAL kept only for the signals that are stable. ``repeat`` is ``access_diff`` between our
    crawler's first fetch and a second fetch by our crawler (None when that fetch failed). A signal that also differs
    between our own two fetches is the page changing between requests (rotating content, timestamps, A/B tests), not
    crawler-specific serving, and is dropped. No repeat, or nothing stable left: INCONCLUSIVE (no claim either way)."""
    if first.get("state") != "BOT_DIFFERENTIAL":
        return first
    if repeat is None or repeat.get("state") == "INCONCLUSIVE":
        return {"state": "INCONCLUSIVE", "signals": [], "why": "the repeat request by our crawler failed; the "
                "difference could not be confirmed"}
    unstable = {s["signal"] for s in repeat.get("signals") or []}
    stable = [s for s in first.get("signals") or [] if s["signal"] not in unstable]
    if not stable:
        return {"state": "INCONCLUSIVE", "signals": [], "unstable": sorted(unstable),
                "why": "the page differed between two requests by our crawler as well; not crawler-specific"}
    return {"state": "BOT_DIFFERENTIAL", "signals": stable, "confirmed": True,
            "unstable": sorted(unstable)}
