"""The Index Builder (``cc_index_builder``, proposed name Ivan, name pending Q-MP-1): build one snapshot - the link
slices for the registered target domains, sharded, encrypted, manifested (marcus-pipeline-spec/1 B-MP-30..36).

The pure rules live here; the job (record-first, kill switches, the staged files, the atomic publish, retention) is
svc_link_index.py.
  - B-MP-30: the targets are the registered domains of every tenant AT BUILD TIME (path B client slices); a row is
    kept when its target host is one of them or a subdomain (``agents/domain_match.py``: exact or subdomain; a
    LOOKALIKE never, it is only counted);
  - B-MP-32: one shard per target registrable domain (the bundled public suffix list), a large one split at 500,000
    rows; each shard encrypted at rest (index_store.py, GP-4) with its sha256 in the manifest;
  - B-MP-33: the manifest ``marcus.index_snapshot/1`` (this module's ``MANIFEST_KEYS``, validated strictly by
    ``validate_manifest`` for every consumer, CB-19) is written LAST, ``complete: true`` only after every shard was
    written, read back and its hash checked."""

from __future__ import annotations

import math
import re
from typing import Optional
from urllib.parse import urlsplit

from agents import domain_match
from marcus import release_watcher

AGENT = "cc_index_builder"
CONTRACT = "marcus.index_snapshot/1"
SOURCES_CONTRACT = "marcus.crawled_sources/1"
BUILDER_VERSION = "marcus-index-builder/1"
SHARD_MAX_ROWS = 500_000
LICENCE_ACK = "FIXTURES_ONLY"              # v1 reads fixtures only; CC's Terms of Use are the owner's call (Q-MA-1)
REL_CAPTURE = ("VERIFIED", "UNVERIFIED")
MANIFEST_KEYS = frozenset(("contract", "snapshot_id", "crawls", "built_at", "builder_version", "shards", "counts",
                           "licence_ack", "complete", "previous_snapshot_id", "crawled_sources", "key_id"))
SHARD_KEYS = frozenset(("shard_id", "sha256", "domain", "rows"))
VERSION_KEYS = frozenset(("version", "rel_capture", "spike_ref"))
SOURCES_KEYS = frozenset(("previous_snapshot_id", "sha256", "crawled", "not_crawled"))
SNAPSHOT_ID = re.compile(r"seo-snp-[0-9a-f]{40}")
SHARD_ID = re.compile(r"seo-shd-[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?Z")
KEY_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
COUNT_MAX = 10 ** 11


class ContractInvalid(Exception):
    pass


def registrable(host: str) -> Optional[str]:
    """The registrable domain of ``host`` (public suffix + one label), or None (a suffix itself, or not a host)."""
    labels = host.split(".") if isinstance(host, str) else []
    for i in range(1, len(labels)):
        if domain_match.is_public_suffix(".".join(labels[i:])):
            return ".".join(labels[i - 1:])
    return None


def canonical_domain(d) -> bool:
    """B-MP-22 (hosts are IDNA) for a manifest's shard domain, lead ruling MP-C4a 23:25 PT: accepted only in canonical
    form, ASCII lower-case A-labels exactly as domain_match's strict IDNA round trip makes them (labels 1-63, total
    <= 253, no empty label, no trailing dot). A U-label, upper case or a label the round trip changes is refused,
    never converted (fail closed, CB-19)."""
    return isinstance(d, str) and domain_match._ascii(d) == d


def targets(tenants: dict) -> dict:
    """{registered domain: its registrable domain} over every tenant's registered domains at build time."""
    out = {}
    for t in tenants.values():
        for d in t.get("domains") or ():
            r = registrable(d)
            if r is not None:
                out[d] = r
    return dict(sorted(out.items()))


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def match(host: str, tgt: dict) -> tuple:
    """(the shard key, False) when ``host`` is a target or a subdomain of one; (None, True) for a lookalike of one;
    (None, False) otherwise."""
    lookalike = False
    for d, r in tgt.items():
        m = domain_match.match(host, d)
        if m in (domain_match.EXACT, domain_match.SUBDOMAIN):
            return r, False
        if m == domain_match.LOOKALIKE:
            lookalike = True
    return None, lookalike


def split(rows_by_domain: dict) -> list:
    """[(domain, part index, rows)] in domain order, each part at most ``SHARD_MAX_ROWS`` rows."""
    out = []
    cap = SHARD_MAX_ROWS
    for dom in sorted(rows_by_domain):
        rows = rows_by_domain[dom]
        for k in range(0, len(rows), cap):             # a target with no rows has no shard
            out.append((dom, k // cap, rows[k:k + cap]))
    return out


def _int(v) -> bool:
    return type(v) is int and 0 <= v <= COUNT_MAX


def validate_manifest(doc) -> dict:
    """The manifest as producer and consumers read it, or ``ContractInvalid`` (refused whole, CB-19)."""
    try:
        ok = (isinstance(doc, dict) and set(doc) == MANIFEST_KEYS and doc["contract"] == CONTRACT
              and isinstance(doc["snapshot_id"], str) and SNAPSHOT_ID.fullmatch(doc["snapshot_id"]) is not None
              and isinstance(doc["crawls"], list) and 1 <= len(doc["crawls"]) <= 4
              and all(release_watcher.valid_crawl_id(c) for c in doc["crawls"])
              and len(set(doc["crawls"])) == len(doc["crawls"])
              and isinstance(doc["built_at"], str) and TIME.fullmatch(doc["built_at"]) is not None
              and isinstance(doc["builder_version"], dict) and set(doc["builder_version"]) == VERSION_KEYS
              and doc["builder_version"]["version"] == BUILDER_VERSION
              and doc["builder_version"]["rel_capture"] in REL_CAPTURE
              and (doc["builder_version"]["spike_ref"] is None or (isinstance(doc["builder_version"]["spike_ref"], str)
                                                                    and SHA256.fullmatch(
                                                                        doc["builder_version"]["spike_ref"])))
              and (doc["builder_version"]["rel_capture"] == "VERIFIED") == (doc["builder_version"]["spike_ref"]
                                                                            is not None)
              and doc["licence_ack"] == LICENCE_ACK and doc["complete"] is True
              and isinstance(doc["key_id"], str) and KEY_ID.fullmatch(doc["key_id"]) is not None
              and (doc["previous_snapshot_id"] is None or (isinstance(doc["previous_snapshot_id"], str)
                                                           and SNAPSHOT_ID.fullmatch(doc["previous_snapshot_id"])))
              and isinstance(doc["counts"], dict) and all(isinstance(k, str) and _int(v)
                                                          for k, v in doc["counts"].items())
              and isinstance(doc["shards"], list) and len(doc["shards"]) <= 100_000)
        if not ok:
            raise ContractInvalid()
        for s in doc["shards"]:
            if not (isinstance(s, dict) and set(s) == SHARD_KEYS and isinstance(s["shard_id"], str)
                    and SHARD_ID.fullmatch(s["shard_id"]) and isinstance(s["sha256"], str)
                    and SHA256.fullmatch(s["sha256"]) and canonical_domain(s["domain"])
                    and registrable(s["domain"]) == s["domain"] and _int(s["rows"])
                    and s["rows"] <= SHARD_MAX_ROWS):
                raise ContractInvalid()
        if len({s["shard_id"] for s in doc["shards"]}) != len(doc["shards"]):
            raise ContractInvalid()
        cs = doc["crawled_sources"]
        if not (isinstance(cs, dict) and set(cs) == SOURCES_KEYS and cs["previous_snapshot_id"] ==
                doc["previous_snapshot_id"] and isinstance(cs["sha256"], str) and SHA256.fullmatch(cs["sha256"])
                and _int(cs["crawled"]) and _int(cs["not_crawled"])):
            raise ContractInvalid()
    except (KeyError, TypeError, AttributeError):
        raise ContractInvalid() from None
    if any(isinstance(v, float) and not math.isfinite(v) for v in doc["counts"].values()):
        raise ContractInvalid()
    return doc
