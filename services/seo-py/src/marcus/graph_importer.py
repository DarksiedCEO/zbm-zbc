"""The Graph Importer (``cc_graph_importer``, proposed name Gideon, name pending Q-MP-1): import the host / domain graph
rank files of one release (marcus-pipeline-spec/1 B-MP-40..43, CLARIFICATIONS v1.1 GP-3).

A rank file is tab-separated ``harmonicc_pos, harmonicc_val, pr_pos, pr_val, host_rev`` under a ``#`` header line
(host in reversed-label notation: ``com.example.www`` is ``www.example.com``). Strict: a row that is not five fields,
a rank that is not a positive integer or exceeds the node count (the file's row count), a value that is not a finite
non-negative number, a host that is not lower-case labels, or a duplicate of a kept host is quarantined
``SOURCE_ROW_INVALID`` (counted, the import continues); a missing or other header quarantines the file
(``SOURCE_SCHEMA_UNKNOWN``). Only the hosts (host graph) or domains (domain graph) present in the current snapshot's
slices are kept (path B), streaming: memory is bounded by the kept set, never by the file (B-MP-41). The duplicate check
covers the kept hosts: they are the only rows the output holds. The output ``marcus.graph_ranks/1`` is encrypted at
rest (index_store.py). These are Common Crawl's published ranks; never a vendor metric, never rescaled."""

from __future__ import annotations

import math
import re

from marcus import index_builder, release_watcher

AGENT = "cc_graph_importer"
CONTRACT = "marcus.graph_ranks/1"
HEADER = ("harmonicc_pos", "harmonicc_val", "pr_pos", "pr_val", "host_rev")
DOC_KEYS = frozenset(("contract", "release", "kind", "nodes", "snapshot_id", "rows"))
RANK_KEYS = frozenset(("hc_rank", "pr_rank"))
NODES_MAX = 10 ** 11
LINE_MAX = 1024
_LABELS = re.compile(r"[a-z0-9_-]{1,63}(\.[a-z0-9_-]{1,63}){1,126}")
_CHECK_EVERY = 65536
_NUM = re.compile(r"(0|[1-9][0-9]{0,30})(\.[0-9]{1,30})?([eE][-+]?[0-9]{1,3})?")


class SourceInvalid(Exception):
    def __init__(self, code: str = "SOURCE_SCHEMA_UNKNOWN"):
        super().__init__(code)
        self.code = code


class Counts(dict):
    def __missing__(self, key):
        return 0


def unreverse(host_rev: str) -> str:
    return ".".join(reversed(host_rev.split(".")))


def rank_path(release_id: str, kind: str) -> str:
    part = "host" if kind == "host_graph" else "domain"
    return f"projects/hyperlinkgraph/{release_id}/{part}/{release_id}-{part}-ranks.txt.gz"


def _rank(v: str, nodes: int):
    if not v.isdigit() or not v.isascii() or v[0] == "0" or len(v) > 12:
        return None
    n = int(v)
    return n if n <= nodes else None


def _value(v: str) -> bool:
    """A finite non-negative decimal (no sign, no NaN or infinity spelling)."""
    return _NUM.fullmatch(v) is not None and math.isfinite(float(v))


def header_ok(line: str) -> bool:
    return line.startswith("#") and tuple(line[1:].rstrip("\n").split("\t")) == HEADER


def parse_ranks(lines, keep, counts: Counts, nodes: int, check=None) -> tuple:
    """(nodes, {host: {hc_rank, pr_rank}}) over ``lines`` (str, with or without newlines), keeping the hosts of
    ``keep``. ``nodes`` is the file's data-row count (the caller counts it in a first pass)."""
    it = iter(lines)
    first = next(it, None)
    if not isinstance(first, str) or not header_ok(first):
        raise SourceInvalid("SOURCE_SCHEMA_UNKNOWN")
    rows: dict = {}
    n = 0
    for line in it:
        n += 1
        if check is not None and n % _CHECK_EVERY == 0:
            check()
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        ok = len(parts) == 5 and len(line) <= LINE_MAX
        if ok:
            hc, hv, pr, pv, hr = parts
            ok = (_rank(hc, nodes) is not None and _rank(pr, nodes) is not None and _value(hv) and _value(pv)
                  and _LABELS.fullmatch(hr) is not None)
        if not ok:
            counts["rows_invalid"] += 1
            counts["quarantine:SOURCE_ROW_INVALID"] += 1
            continue
        host = unreverse(hr)
        if host in keep:
            if host in rows:
                counts["rows_duplicate"] += 1
                counts["quarantine:SOURCE_ROW_INVALID"] += 1
                continue
            rows[host] = {"hc_rank": int(hc), "pr_rank": int(pr)}
    return nodes, rows


def validate_graph(doc) -> dict:
    """``marcus.graph_ranks/1`` as consumers read it, or ``index_builder.ContractInvalid`` (refused whole)."""
    try:
        ok = (isinstance(doc, dict) and set(doc) == DOC_KEYS and doc["contract"] == CONTRACT
              and release_watcher.valid_graph_id(doc["release"]) and doc["kind"] in release_watcher.GRAPH_KINDS
              and type(doc["nodes"]) is int and 0 <= doc["nodes"] <= NODES_MAX
              and isinstance(doc["snapshot_id"], str) and index_builder.SNAPSHOT_ID.fullmatch(doc["snapshot_id"])
              and isinstance(doc["rows"], dict))
        if not ok:
            raise index_builder.ContractInvalid()
        for h, v in doc["rows"].items():
            if not (isinstance(h, str) and _LABELS.fullmatch(h) and isinstance(v, dict) and set(v) == RANK_KEYS
                    and all(type(v[k]) is int and 1 <= v[k] <= doc["nodes"] for k in RANK_KEYS)):
                raise index_builder.ContractInvalid()
    except (KeyError, TypeError, AttributeError):
        raise index_builder.ContractInvalid() from None
    return doc
