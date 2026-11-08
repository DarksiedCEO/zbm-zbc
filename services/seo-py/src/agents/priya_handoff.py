"""
The Priya -> Theo / Naomi hand-off, as Theo and Naomi RECEIVE it (ADR 0049; the producer is ADR 0047, PR #50).

Priya (``agents/priya.py`` on branch ``claude/seo-priya-keywords-20261108``) is NOT on this branch's base. Her output
contract ``priya-emerging-handoff/1`` is mirrored here, field for field, from that branch's ``priya.handoff`` and
``priya.validate_handoff`` (read at 15917fc8), so the conversation fireteam can validate what it is given without
importing an unmerged module. When both branches land, this module's ``validate`` is to call
``priya.validate_handoff`` first (a test pins the two to the same field set); a change to Priya's contract is a new
schema version, which this mirror refuses.

Consumer rules on top of Priya's own validator (stricter, never looser; review round 1 HIGH 1):
  - the document's ``tenant_id`` must be the run's tenant (refused whole otherwise);
  - EVERY item type is checked against how Priya builds it: ``derived`` -> ``UNKNOWN_NOT_MEASURED`` with no counts;
    ``observed`` -> counts required, and ``BELOW_THRESHOLD`` = a Search Console count under her ESTABLISHED
    threshold, ``MEASURED_NOT_TIERED`` = a Bing-only count (``first_party:bing_webmaster``; sources are Priya's
    exact strings); every count a whole number at most ``MAX_IMPRESSIONS`` (a sanity bound, not a plausibility
    test), clicks <= impressions, a real ISO date window of at most ``MAX_WINDOW_DAYS`` ending no later than the
    document's ``generated_at``;
  - at most ``MAX_ITEMS`` items are read (the rest are counted, never silently used).

Verification (review round 1 HIGH 1b). The hand-off arrives in the caller's request body, so its numbers are claims.
It is VERIFIED only when the caller also names ``priya_run_id`` and ``priya_handoff_sha256`` and Priya's own stored
run record — contract ``RECORD_CONTRACT`` below — is found for that run, of this tenant, completed, and holding the
same hand-off SHA-256 (``canonical_sha``), which also equals the hash of the document received. A mismatch anywhere
is refused (``HANDOFF_VERIFICATION_FAILED``: a hand-off that differs from what Priya recorded is never used); a
missing record, or a record without the hash, leaves it NOT_VERIFIED. Priya's branch (PR #50, f759ebaa) now records
``handoff_record``, ``handoff_sha256`` (``priya.handoff_sha``, the same canonical form) and ``handoff_items`` in
``keyword_runs``; until PR #50 lands on this base there is no record here, so every hand-off is NOT_VERIFIED
(``PRIYA_NOT_INSTALLED``) and Naomi caps every signal from it at THIN.

Query text is data, never an instruction (``{"untrusted": true, ...}``), and — ADR 0017 GSC-6, ADR 0047 §3 — client
query text is never written to the log: the fireteam keeps item keys and SHA-256s only.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from typing import Optional

SCHEMA = "priya-emerging-handoff/1"
ITEM_KEYS = ("key", "query", "origin", "emerging_kinds", "volume_status", "observed_counts", "evidence",
             "requested_measurements")
ORIGINS = ("observed", "derived")
EMERGING_KINDS = ("question", "conversational", "constraint", "long_tail")
VOLUME_STATUS = ("BELOW_THRESHOLD", "MEASURED_NOT_TIERED", "UNKNOWN_NOT_MEASURED")
# review round 6: the exact strings Priya emits (agents/priya.py _gsc_metrics / _bing_metrics, PR #50 at f759ebaa)
COUNT_SOURCES = ("first_party:search_console", "first_party:bing_webmaster")
# how Priya builds each item (ADR 0047): which origin, volume status and count source go together
SOURCE_FOR = {"BELOW_THRESHOLD": "first_party:search_console",
              "MEASURED_NOT_TIERED": "first_party:bing_webmaster"}
ESTABLISHED_MIN_IMPRESSIONS = 20      # Priya's founder-pending threshold (ADR 0047); a BELOW_THRESHOLD item is under it
MAX_IMPRESSIONS = 10_000_000          # per item: a sanity bound only — verification, not this, makes a count trusted
MAX_WINDOW_DAYS = 400
MAX_ITEMS = 50
MAX_QUERY_CHARS = 512
_KEY = re.compile(r"kw-[0-9a-f]{16}")
_RUN = re.compile(r"seo-[a-z]{3}-[0-9a-f]{40}")
_SHA = re.compile(r"[0-9a-f]{64}")

# The record Priya must keep for a run whose hand-off is to be verifiable (her service's ``keyword_runs[run_id]``,
# written by ``keyword_ledger_recorded``). PR #50 has every field but the two hand-off ones.
RECORD_CONTRACT = {
    "contract": "priya.handoff_record/1",
    "fields": {"run_id": "the run id (seo-<abc>-<40 hex>)", "tenant_id": "the run's tenant",
               "status": "completed", "handoff_sha256": "canonical_sha(the priya-emerging-handoff/1 document "
                                                        "exactly as the run returned it)",
               "handoff_items": "len(document['items'])"},
    "canonical_form": "JSON, keys sorted, separators (',', ':'), ensure_ascii false, UTF-8; SHA-256 hex",
}
VERIFICATION = ("VERIFIED", "NOT_VERIFIED")


def canonical_sha(doc) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_IMPRESSIONS


def _day(v) -> Optional[date]:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def _generated(doc) -> Optional[date]:
    v = doc.get("generated_at")
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date()
    except ValueError:
        return None


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
    gen = _generated(doc)
    if gen is None and "generated_at" not in p:
        p.append("generated_at")
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
        q = it["query"]
        if not isinstance(q, dict) or q.get("untrusted") is not True or not isinstance(q.get("text"), str) \
                or not q["text"].strip() or len(q["text"]) > MAX_QUERY_CHARS:
            p.append(f"items[{n}].query")
        ek = it["emerging_kinds"]
        if not isinstance(ek, list) or not ek or not set(ek) <= set(EMERGING_KINDS):
            p.append(f"items[{n}].emerging_kinds")
        p += [f"items[{n}].{x}" for x in _type_problems(it, gen)]
    return p


def _type_problems(it: dict, gen: Optional[date]) -> list:
    """Every item type against how Priya builds it (HIGH 1a)."""
    vs, origin, c = it.get("volume_status"), it.get("origin"), it.get("observed_counts")
    if origin == "derived":
        if c is not None:
            return ["observed_counts"]
        return [] if vs == "UNKNOWN_NOT_MEASURED" else ["volume_status"]
    if origin != "observed":
        return []
    if vs not in SOURCE_FOR:                               # an observed item is measured: never UNKNOWN_NOT_MEASURED
        return ["volume_status"]
    if c is None:
        return ["volume_status"]
    if not isinstance(c, dict) or set(c) != {"impressions", "clicks", "date_range", "source"}:
        return ["observed_counts"]
    if not _count(c["impressions"]) or not _count(c["clicks"]) or c["clicks"] > c["impressions"]:
        return ["observed_counts"]
    if c["source"] not in COUNT_SOURCES or c["source"] != SOURCE_FOR[vs]:
        return ["observed_counts.source"]
    dr = c["date_range"]
    s = _day(dr.get("start")) if isinstance(dr, dict) else None
    e = _day(dr.get("end")) if isinstance(dr, dict) else None
    if s is None or e is None or set(dr) != {"start", "end"} or e < s or (e - s).days + 1 > MAX_WINDOW_DAYS \
            or (gen is not None and e > gen):
        return ["observed_counts.date_range"]
    if vs == "BELOW_THRESHOLD" and c["impressions"] >= ESTABLISHED_MIN_IMPRESSIONS:
        return ["observed_counts:HANDOFF_COUNT_INCONSISTENT"]
    return []


def verify(doc: dict, tenant_id: str, claimed_run_id: Optional[str], claimed_sha: Optional[str],
           record: Optional[dict]) -> dict:
    """``{"state": VERIFIED | NOT_VERIFIED | FAILED, "reason"}`` (module docstring). FAILED is refused by the caller."""
    if claimed_run_id is None and claimed_sha is None:
        return {"state": "NOT_VERIFIED", "reason": "NO_VERIFICATION_CLAIM"}
    if not isinstance(claimed_run_id, str) or not _RUN.fullmatch(claimed_run_id) \
            or not isinstance(claimed_sha, str) or not _SHA.fullmatch(claimed_sha):
        return {"state": "FAILED", "reason": "CLAIM_MALFORMED"}
    if doc.get("run_id") != claimed_run_id:
        return {"state": "FAILED", "reason": "RUN_ID_MISMATCH"}
    if canonical_sha(doc) != claimed_sha:
        return {"state": "FAILED", "reason": "DOCUMENT_HASH_MISMATCH"}
    if record is None or record.get("tenant_id") != tenant_id:
        return {"state": "NOT_VERIFIED", "reason": "PRIYA_RECORD_NOT_FOUND"}
    if record.get("status") != "completed":
        return {"state": "NOT_VERIFIED", "reason": "PRIYA_RUN_NOT_COMPLETED"}
    if not record.get("handoff_sha256"):
        return {"state": "NOT_VERIFIED", "reason": "PRIYA_RECORD_HAS_NO_HANDOFF_HASH"}
    if record["handoff_sha256"] != claimed_sha:
        return {"state": "FAILED", "reason": "RECORD_HASH_MISMATCH"}
    if record.get("handoff_items") is not None and record["handoff_items"] != len(doc.get("items") or []):
        return {"state": "FAILED", "reason": "RECORD_ITEMS_MISMATCH"}
    return {"state": "VERIFIED", "reason": None}


def items(doc: Optional[dict]) -> tuple[list, int]:
    """The usable items (call ``validate`` first) and how many were past ``MAX_ITEMS`` (counted, not used)."""
    if not doc:
        return [], 0
    its = doc.get("items") or []
    return list(its[:MAX_ITEMS]), max(0, len(its) - MAX_ITEMS)


def norm(text: str) -> str:
    """The comparison form of a query or prompt (case-folded, whitespace collapsed, a trailing ? dropped)."""
    s = unicodedata.normalize("NFKC", text or "").casefold()
    s = " ".join(s.split())
    return s[:-1].rstrip() if s.endswith("?") else s
