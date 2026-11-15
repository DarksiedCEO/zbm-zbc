"""The Release Watcher (``cc_release_watcher``, proposed name Ruth, name pending Q-MP-1): notice a new Common Crawl
crawl or web-graph release and record it (marcus-pipeline-spec/1 B-MP-01..04, CLARIFICATIONS v1.1 GP-1).

The release list is read through the ``cc_releases`` port. v1: a fixture directory (``SEO_MARCUS_FIXTURE_DIR``, tests
only) holding ``collinfo.json`` (the CC index collection list ``[{id, name, timegate, cdx-api}]``) and
``graphinfo.json`` (``[{id, kind: host_graph | domain_graph, published}]``). The live port is ``config.NOT_BUILT``
(``SEO_CC_RELEASES_ENABLED``); its v2 budget is one request a day. Validation is strict: any other shape quarantines
the whole list (``SOURCE_SCHEMA_UNKNOWN``). This service never fetches data."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Optional
from urllib.parse import urlsplit

AGENT = "cc_release_watcher"
LIVE_REQUESTS_PER_DAY = 1                  # B-MP-03: the v2 live port's budget (specified now, built later)
ALLOWED_HOSTS = frozenset(("index.commoncrawl.org", "data.commoncrawl.org"))
CRAWL_ID = re.compile(r"CC-MAIN-(\d{4})-(\d{2})")
GRAPH_ID = re.compile(r"cc-main-[a-z0-9]{1,12}(-[a-z0-9]{1,12}){0,7}")
GRAPH_ID_MAX = 64
GRAPH_KINDS = ("host_graph", "domain_graph")
KINDS = ("crawl",) + GRAPH_KINDS
CRAWL_KEYS = frozenset(("id", "name", "timegate", "cdx-api"))
GRAPH_KEYS = frozenset(("id", "kind", "published"))
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 5000
NAME_MAX = 200
URL_MAX = 300
DOC_SOURCE = "CLARIFICATIONS-v1.1-GP-1"     # the fixture contract (the CC index collection list shape)
READ_DATE = "2026-11-13"
CRAWLS_FILE, GRAPHS_FILE = "collinfo.json", "graphinfo.json"


class SourceInvalid(Exception):
    def __init__(self, code: str = "SOURCE_SCHEMA_UNKNOWN"):
        super().__init__(code)
        self.code = code


def valid_crawl_id(s) -> bool:
    m = CRAWL_ID.fullmatch(s) if isinstance(s, str) else None
    if m is None:
        return False
    year, week = int(m.group(1)), int(m.group(2))
    if not 2008 <= year <= 2099 or week < 1:
        return False
    return week <= date(year, 12, 28).isocalendar()[1]          # 52 or 53 ISO weeks in that year


def valid_graph_id(s) -> bool:
    return isinstance(s, str) and len(s) <= GRAPH_ID_MAX and GRAPH_ID.fullmatch(s) is not None


def _cc_url(u) -> bool:
    if not isinstance(u, str) or len(u) > URL_MAX or not u.isascii() or any(c.isspace() for c in u):
        return False
    try:
        p = urlsplit(u)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and p.hostname in ALLOWED_HOSTS and port is None and "@" not in p.netloc
            and p.netloc == p.hostname and not p.query and not p.fragment)


def _date(s) -> bool:
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True


def _dup_refused(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise SourceInvalid()
        out[k] = v
    return out


def _load(raw) -> list:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_LIST_BYTES:
        raise SourceInvalid()
    try:
        text = bytes(raw).decode("utf-8")
        doc = json.loads(text, object_pairs_hook=_dup_refused, parse_constant=lambda c: (_ for _ in ()).throw(
            SourceInvalid()))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SourceInvalid() from None
    if not isinstance(doc, list) or len(doc) > MAX_ITEMS:
        raise SourceInvalid()
    return doc


def parse(crawls_raw, graphs_raw) -> list:
    """[{id, kind, published}] sorted by (kind, id), or ``SourceInvalid`` for the whole list. A crawl's published date
    is None: the GP-1 shape carries none (the crawl id names its ISO year and week)."""
    out, seen = [], set()
    for item in _load(crawls_raw):
        if not isinstance(item, dict) or set(item) != CRAWL_KEYS or not valid_crawl_id(item["id"]):
            raise SourceInvalid()
        name = item["name"]
        if not isinstance(name, str) or not name or len(name) > NAME_MAX or not name.isprintable():
            raise SourceInvalid()
        if not _cc_url(item["timegate"]) or not _cc_url(item["cdx-api"]):
            raise SourceInvalid()
        if ("crawl", item["id"]) in seen:
            raise SourceInvalid()
        seen.add(("crawl", item["id"]))
        out.append({"id": item["id"], "kind": "crawl", "published": None})
    for item in _load(graphs_raw):
        if not isinstance(item, dict) or set(item) != GRAPH_KEYS or not valid_graph_id(item["id"]):
            raise SourceInvalid()
        if item["kind"] not in GRAPH_KINDS or not _date(item["published"]):
            raise SourceInvalid()
        if (item["kind"], item["id"]) in seen:
            raise SourceInvalid()
        seen.add((item["kind"], item["id"]))
        out.append({"id": item["id"], "kind": item["kind"], "published": item["published"]})
    return sorted(out, key=lambda r: (r["kind"], r["id"]))


def _read_capped(path: str) -> Optional[bytes]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            return fh.read(MAX_LIST_BYTES + 1)
    except OSError:
        return None


class FixtureReleases:
    """The v1 ``cc_releases`` port: the fixture directory's two list files (tests only). Names its documentation
    source and read date (CB-13)."""

    connected = True
    doc_source = DOC_SOURCE
    read_date = READ_DATE

    def __init__(self, root: str):
        self.root = root

    def read(self) -> tuple:
        """(crawl list bytes, graph list bytes); a missing file reads as an empty value (quarantined by ``parse``)."""
        a = _read_capped(os.path.join(self.root, CRAWLS_FILE))
        b = _read_capped(os.path.join(self.root, GRAPHS_FILE))
        return (a if a is not None else b"", b if b is not None else b"")


class NotConnectedReleases:
    connected = False
    doc_source = None
    read_date = None

    def read(self) -> tuple:
        raise RuntimeError("the cc_releases port is not connected")
