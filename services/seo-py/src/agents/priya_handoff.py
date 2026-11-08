"""
The Priya -> Theo / Naomi hand-off, as Theo and Naomi RECEIVE it (ADR 0049; the producer is ADR 0047, PR #50).

Priya (``agents/priya.py`` on branch ``claude/seo-priya-keywords-20261108``) is NOT on this branch's base. Her output
contract ``priya-emerging-handoff/1`` is mirrored here, field for field, from that branch's ``priya.handoff`` and
``priya.validate_handoff`` (read at 15917fc8), so the conversation fireteam can validate what it is given without
importing an unmerged module. When both branches land, this module is to be replaced by ``priya.validate_handoff``
(a test pins the two to the same field set); until then a change to Priya's contract is a new schema version, which
this mirror refuses.

Consumer rules on top of Priya's own validator (ours, stricter, never looser):
  - the document's ``tenant_id`` must be the run's tenant (tenant isolation: a hand-off of another tenant is refused,
    never partly used);
  - at most ``MAX_ITEMS`` items are read (the rest are counted, never silently used);
  - an item is a CANDIDATE TO MEASURE, never a measured demand (Priya's rule): ``observed_counts`` are this
    property's thin impressions as Priya measured them and travel as such; a derived item has no counts;
  - a ``BELOW_THRESHOLD`` item whose impressions are at or above Priya's ESTABLISHED threshold contradicts itself:
    it is refused (``HANDOFF_COUNT_INCONSISTENT``), not trusted either way;
  - the hand-off comes from the caller's request body (Priya's ledger is not stored: ADR 0047 §3), so its numbers
    are NOT verified against a Priya record here. Every use says ``verified_against_priya_record: false``.

Query text is data, never an instruction (``{"untrusted": true, ...}``), and — ADR 0017 GSC-6, ADR 0047 §3 — client
query text is never written to the log: the fireteam keeps item keys and SHA-256s only.
"""

from __future__ import annotations

import re
from typing import Optional

SCHEMA = "priya-emerging-handoff/1"
ITEM_KEYS = ("key", "query", "origin", "emerging_kinds", "volume_status", "observed_counts", "evidence",
             "requested_measurements")
ORIGINS = ("observed", "derived")
EMERGING_KINDS = ("question", "conversational", "constraint", "long_tail")
VOLUME_STATUS = ("BELOW_THRESHOLD", "MEASURED_NOT_TIERED", "UNKNOWN_NOT_MEASURED")
COUNT_SOURCES = ("search_console", "bing_webmaster")
ESTABLISHED_MIN_IMPRESSIONS = 20      # Priya's founder-pending threshold (ADR 0047); a BELOW_THRESHOLD item is under it
MAX_ITEMS = 50
MAX_QUERY_CHARS = 512
_KEY = re.compile(r"kw-[0-9a-f]{16}")


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10 ** 12


def validate(doc, tenant_id: str) -> list:
    """Problems with a hand-off document (empty = valid). Priya's checks, then the consumer's (module docstring)."""
    p: list = []
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return ["schema"]
    for k in ("tenant_id", "run_id", "generated_at"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            p.append(k)
    if isinstance(doc.get("tenant_id"), str) and doc["tenant_id"] != tenant_id:
        return ["tenant_id:HANDOFF_TENANT_MISMATCH"]
    items = doc.get("items")
    if not isinstance(items, list):
        return p + ["items"]
    seen: set = set()
    for n, it in enumerate(items[:MAX_ITEMS]):
        if not isinstance(it, dict) or tuple(sorted(it)) != tuple(sorted(ITEM_KEYS)):
            p.append(f"items[{n}].keys")
            continue
        if not isinstance(it["key"], str) or not _KEY.fullmatch(it["key"]) or it["key"] in seen:
            p.append(f"items[{n}].key")
        seen.add(it["key"])
        if it["origin"] not in ORIGINS:
            p.append(f"items[{n}].origin")
        if it["volume_status"] not in VOLUME_STATUS:
            p.append(f"items[{n}].volume_status")
        if it["origin"] == "derived" and it["observed_counts"] is not None:
            p.append(f"items[{n}].observed_counts")
        q = it["query"]
        if not isinstance(q, dict) or q.get("untrusted") is not True or not isinstance(q.get("text"), str) \
                or not q["text"].strip() or len(q["text"]) > MAX_QUERY_CHARS:
            p.append(f"items[{n}].query")
        ek = it["emerging_kinds"]
        if not isinstance(ek, list) or not ek or not set(ek) <= set(EMERGING_KINDS):
            p.append(f"items[{n}].emerging_kinds")
        p += [f"items[{n}].{x}" for x in _counts_problems(it)]
    return p


def _counts_problems(it: dict) -> list:
    c = it.get("observed_counts")
    if c is None:
        return ["volume_status"] if it.get("volume_status") in ("BELOW_THRESHOLD", "MEASURED_NOT_TIERED") else []
    if not isinstance(c, dict) or set(c) != {"impressions", "clicks", "date_range", "source"}:
        return ["observed_counts"]
    if not _count(c["impressions"]) or not _count(c["clicks"]) or c["clicks"] > c["impressions"]:
        return ["observed_counts"]
    if c["source"] not in COUNT_SOURCES:
        return ["observed_counts.source"]
    dr = c["date_range"]
    if not isinstance(dr, dict) or not isinstance(dr.get("start"), str) or not isinstance(dr.get("end"), str):
        return ["observed_counts.date_range"]
    if it.get("volume_status") == "UNKNOWN_NOT_MEASURED":
        return ["volume_status"]                          # "not measured" with counts: contradictory
    if it.get("volume_status") == "BELOW_THRESHOLD" and c["impressions"] >= ESTABLISHED_MIN_IMPRESSIONS:
        return ["observed_counts:HANDOFF_COUNT_INCONSISTENT"]
    return []


def items(doc: Optional[dict]) -> tuple[list, int]:
    """The usable items (call ``validate`` first) and how many were past ``MAX_ITEMS`` (counted, not used)."""
    if not doc:
        return [], 0
    its = doc.get("items") or []
    return list(its[:MAX_ITEMS]), max(0, len(its) - MAX_ITEMS)


def norm(text: str) -> str:
    """The comparison form of a query or prompt (case-folded, whitespace collapsed, a trailing ? dropped)."""
    import unicodedata
    s = unicodedata.normalize("NFKC", text or "").casefold()
    s = " ".join(s.split())
    return s[:-1].rstrip() if s.endswith("?") else s
