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


def access_diff(bot_fetch, human_fetch, bot_extract: Optional[dict], human_extract: Optional[dict]) -> dict:
    """SAME, BOT_DIFFERENTIAL or INCONCLUSIVE, with the differing signals."""
    if bot_fetch.state not in ("OK", "BLOCKED_BY_ROBOTS") or human_fetch.state != "OK":
        return {"state": "INCONCLUSIVE", "signals": [], "why": "a fetch failed (no claim either way)"}
    signals = []
    if bot_fetch.state == "BLOCKED_BY_ROBOTS":
        return {"state": "INCONCLUSIVE", "signals": [], "why": "robots.txt disallows our crawler on this path"}
    if bot_fetch.status != human_fetch.status:
        signals.append({"signal": "status", "bot": bot_fetch.status, "human": human_fetch.status})
    if (bot_fetch.final_url or "") != (human_fetch.final_url or ""):
        signals.append({"signal": "final_url"})
    if bot_extract is not None and human_extract is not None:
        for k in ("title", "canonicals", "robots_meta"):
            if bot_extract.get(k) != human_extract.get(k):
                signals.append({"signal": k})
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
