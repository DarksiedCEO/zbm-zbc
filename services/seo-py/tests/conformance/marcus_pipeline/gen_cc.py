"""Generated Common Crawl fixtures with TRUTH (marcus-pipeline-spec/1 D-MP-02: no real CC data is committed; the suite
generates every file at run time, seeded).

Fixture contracts (CLARIFICATIONS v1.1): GP-1 the release lists (``collinfo.json``: the CC index collection list
``[{id, name, timegate, cdx-api}]``; ``graphinfo.json``: ``[{id, kind, published}]``), GP-2 the WAT record
(``Envelope.WARC-Header-Metadata.{WARC-Type, WARC-Target-URI}``,
``Envelope.Payload-Metadata.HTTP-Response-Metadata.HTML-Metadata.{Head.Base?, Links: [{path, url, text?, rel?}]}``),
GP-3 the rank files (tab-separated ``harmonicc_pos, harmonicc_val, pr_pos, pr_val, host_rev`` with a ``#`` header).

The expected rows are built from the generator's own link model (components), never by calling the code under test:
the oracle of B-MP-21 / O-MP-1 (SUITE_PLAN 2.2, backlink oracle)."""

from __future__ import annotations

import gzip
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

CRAWLS = ("CC-MAIN-2026-40", "CC-MAIN-2026-44")
GRAPH = "cc-main-2026-jul-aug-sep"
WORDS = ("news", "blog", "post", "guide", "review", "shop", "store", "product", "category", "forum", "thread", "topic",
         "directory", "listing", "local", "garden", "kitchen", "travel", "music", "sports", "health", "home", "tools",
         "camera", "coffee", "bike", "repair", "plumbing", "roofing", "design", "studio", "market", "about", "team",
         "contact", "events", "archive", "tips", "best", "compare", "prices", "deals", "summer", "winter", "classic")
ANCHORS = ("read more", "our partner", "visit the shop", "great guide to gardens", "click here", "website",
           "the best coffee in town", "home", "next page", "local plumbing help", "see the review", "camera deals",
           "Acme", "Globex tools", "official site", "learn about roofing", "photo", "a studio worth visiting",
           "forum thread", "summer events", "contact us", "browse the directory", "classic bikes", "")
ELEMS = ("A@/href", "A@/href", "A@/href", "IMG@/src", "LINK@/href", "AREA@/href")
KINDS = ("news", "blog", "directory", "forum", "shop")


def crawl_item(cid: str) -> dict:
    return {"id": cid, "name": f"Index {cid}", "timegate": f"https://index.commoncrawl.org/{cid}/",
            "cdx-api": f"https://index.commoncrawl.org/{cid}-index"}


def graph_item(gid: str = GRAPH, kind: str = "host_graph", published: str = "2026-10-20") -> dict:
    return {"id": gid, "kind": kind, "published": published}


def wat_path(cid: str, i: int, seg: int = 1) -> str:
    return (f"crawl-data/{cid}/segments/17{seg:011d}.0/wat/"
            f"CC-MAIN-20261001000000-20261001010000-{i:05d}.warc.wat.gz")


@dataclass
class Link:
    host: str
    segs: tuple
    query: tuple = ()
    anchor: str = ""
    elem: str = "A@/href"
    rel: str = ""
    scheme: str = "https"
    relative: bool = False          # written as a path-relative reference (resolved against the page / its base)
    noise: tuple = ()               # raw-only decorations the normaliser must remove: upper, port, dot, frag, track

    def expected(self) -> str:
        q = "&".join(f"{k}={v}" for k, v in self.query)
        return f"{self.scheme}://{self.host}/" + "/".join(self.segs) + (f"?{q}" if q else "")

    def raw(self) -> str:
        host = self.host.upper() if "upper" in self.noise else self.host
        if "dot" in self.noise:
            host += "."
        if "port" in self.noise:
            host += ":443" if self.scheme == "https" else ":80"
        pairs = list(self.query)
        if "track" in self.noise:
            pairs = [("utm_source", "newsletter")] + pairs + [("gclid", "abcdef")]
        q = "&".join(f"{k}={v}" for k, v in pairs)
        frag = "#section" if "frag" in self.noise else ""
        if self.relative:
            return "/" + "/".join(self.segs) + (f"?{q}" if q else "") + frag
        return f"{self.scheme}://{host}/" + "/".join(self.segs) + (f"?{q}" if q else "") + frag


@dataclass
class Page:
    host: str
    segs: tuple
    links: list = field(default_factory=list)
    base: str = ""                  # Head.Base (absolute), when the page declares one

    def url(self) -> str:
        return f"https://{self.host}/" + "/".join(self.segs)

    def record(self) -> dict:
        links = []
        for ln in self.links:
            d = {"path": ln.elem, "url": ln.raw()}
            if ln.anchor:
                d["text"] = ln.anchor
            if ln.rel:
                d["rel"] = ln.rel
            links.append(d)
        html = {"Links": links}
        if self.base:
            html["Head"] = {"Base": self.base}
        return {"Envelope": {"WARC-Header-Metadata": {"WARC-Type": "response", "WARC-Target-URI": self.url()},
                             "Payload-Metadata": {"HTTP-Response-Metadata": {"HTML-Metadata": html}}}}

    def truth(self) -> list:
        """The rows the extractor must produce for this page, in order (rel UNKNOWN: rel capture is UNVERIFIED)."""
        out = []
        for ln in self.links:
            dst = ln.expected()
            if ln.relative:
                b = self.base or self.url()
                dst = b.split("://", 1)[0] + "://" + b.split("://", 1)[1].split("/", 1)[0] + dst.split(ln.host, 1)[1]
            out.append({"src": self.url(), "dst": dst, "anchor": ln.anchor, "path": ln.elem, "rel": "UNKNOWN"})
        return out


def line(rec) -> bytes:
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gz(lines, members: int = 1) -> bytes:
    data = b"".join(x + b"\n" for x in lines)
    if members <= 1:
        return gzip.compress(data, mtime=0)
    step = max(1, len(data) // members)
    return b"".join(gzip.compress(data[i:i + step], mtime=0) for i in range(0, len(data), step))


def slug(rng, k=None) -> tuple:
    return tuple(rng.choice(WORDS) for _ in range(k or rng.randint(1, 4)))


def host(rng, tld: str = "example") -> str:
    return f"{rng.choice(WORDS)}{rng.randint(1, 999)}.{rng.choice(WORDS)}.{tld}"


def page(rng, targets=("acme.test", "globex.test"), n_links=None, kind=None) -> Page:
    """A realistic page (news, blog, directory, forum, shop): links to the targets, their subdomains, other sites,
    relative links, decorated raw forms. Every link is well formed (O-MP-1)."""
    kind = kind or rng.choice(KINDS)
    p = Page(host(rng), (kind,) + slug(rng))
    if rng.random() < 0.1:
        p.base = f"https://{p.host}/"
    for _ in range(n_links if n_links is not None else rng.randint(1, 12)):
        r = rng.random()
        if r < 0.45:
            h = rng.choice(targets)
            h = rng.choice((h, f"www.{h}", f"shop.{h}", h))
        elif r < 0.6:
            h = p.host
        else:
            h = host(rng, rng.choice(("example", "test", "org")))
        q = tuple((rng.choice(("page", "sort", "q")), rng.choice(WORDS)) for _ in range(rng.randint(0, 2)))
        q = tuple(dict(q).items())
        noise = tuple(n for n in ("upper", "port", "dot", "frag", "track") if rng.random() < 0.12)
        ln = Link(h, slug(rng), q, rng.choice(ANCHORS), rng.choice(ELEMS), noise=noise,
                  scheme="https" if rng.random() < 0.9 else "http")
        if h == p.host and rng.random() < 0.5 and not ln.noise:
            ln.relative, ln.scheme = True, "https"
        p.links.append(ln)
    return p


def corpus(seed: int, n_files: int, pages_per_file: int = 25, targets=("acme.test", "globex.test")):
    """(files: [[Page]], all_truth: [row]) for one crawl."""
    rng = random.Random(seed)
    files = [[page(rng, targets) for _ in range(pages_per_file)] for _ in range(n_files)]
    return files, [r for f in files for p in f for r in p.truth()]


def write_crawl(root: Path, cid: str, files, extra_lines=None) -> list:
    """Write ``files`` ([[Page]] or [[bytes line]]) as the crawl's WAT files and its ``wat.paths``; returns paths."""
    paths = []
    for i, f in enumerate(files):
        p = wat_path(cid, i)
        lines = [x if isinstance(x, bytes) else line(x.record()) for x in f]
        if extra_lines and i in extra_lines:
            lines = extra_lines[i](lines)
        dst = root / p
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(lines if isinstance(lines, bytes) else gz(lines))
        paths.append(p)
    (root / "crawl-data" / cid).mkdir(parents=True, exist_ok=True)
    (root / "crawl-data" / cid / "wat.paths").write_text("".join(x + "\n" for x in paths))
    return paths


def write_releases(root: Path, crawls=CRAWLS, graphs=None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "collinfo.json").write_text(json.dumps([crawl_item(c) for c in crawls]))
    graphs = [graph_item(GRAPH, "host_graph"), graph_item(GRAPH, "domain_graph")] if graphs is None else graphs
    (root / "graphinfo.json").write_text(json.dumps(graphs))


# ---------------------------------------------------------------------------------------------- graph rank files

RANK_HEADER = "#harmonicc_pos\tharmonicc_val\tpr_pos\tpr_val\thost_rev"


def rev(h: str) -> str:
    return ".".join(reversed(h.split(".")))


def rank_rows(hosts, seed: int = 3) -> list:
    """(hc_pos, hc_val, pr_pos, pr_val, host) for ``hosts``: hc positions 1..n in order, pr a seeded permutation."""
    rng = random.Random(seed)
    n = len(hosts)
    pr = list(range(1, n + 1))
    rng.shuffle(pr)
    return [(i + 1, f"{1.0e7 / (i + 1):.4f}", pr[i], f"{1.0 / pr[i]:.6e}", h) for i, h in enumerate(hosts)]


def rank_text(rows, header: bool = True) -> str:
    out = [RANK_HEADER] if header else []
    out += [f"{a}\t{b}\t{c}\t{d}\t{rev(h)}" for a, b, c, d, h in rows]
    return "".join(x + "\n" for x in out)


def rank_path(gid: str, kind: str) -> str:
    part = "host" if kind == "host_graph" else "domain"
    return f"projects/hyperlinkgraph/{gid}/{part}/{gid}-{part}-ranks.txt.gz"


def write_ranks(root: Path, gid: str, kind: str, text: str) -> Path:
    p = root / rank_path(gid, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
    return p


# ---------------------------------------------------------------------------------------------- default fixture

def default_fixture(root: Path, seed: int = 11, files: int = 2, pages: int = 12) -> dict:
    """Both crawls (``files`` WAT files of ``pages`` pages each), the graph release (host + domain rank files over
    every host the corpus links from or to). Returns {crawl id: (paths, truth rows)}."""
    root.mkdir(parents=True, exist_ok=True)
    write_releases(root)
    out, hosts = {}, set()
    for j, cid in enumerate(CRAWLS):
        fl, truth = corpus(seed + j, files, pages)
        out[cid] = (write_crawl(root, cid, fl), truth)
        for r in truth:
            hosts.add(r["src"].split("/")[2])
            hosts.add(r["dst"].split("/")[2])
    hs = sorted(hosts)
    write_ranks(root, GRAPH, "host_graph", rank_text(rank_rows(hs)))
    doms = sorted({".".join(h.split(".")[-2:]) for h in hs})
    write_ranks(root, GRAPH, "domain_graph", rank_text(rank_rows(doms, seed=4)))
    return out


# ---------------------------------------------------------------------------------------------- hostile WAT (A-MP-01)

def _good(rng):
    return line(page(rng, n_links=3).record())


def hostile_cases(seed: int, n: int) -> list:
    """(name, file bytes, expectation): ``quarantine:<code>`` (the whole file) or ``skip:<counter>`` (records skipped
    and counted, the file's good records kept). Classes: gzip bomb, multi-member, truncated, trailing garbage,
    invalid UTF-8, JSON depth 10^5, an over-long line, duplicate records, NUL, not JSON, NaN, duplicate keys."""
    rng = random.Random(seed)
    makers = [
        ("bomb", lambda: _bomb(rng), "quarantine:TOO_LARGE"),
        ("multi_member", lambda: gz([_good(rng) for _ in range(6)], members=3), "ok"),
        ("truncated", lambda: gz([_good(rng) for _ in range(4)])[:-rng.randint(9, 30)], "quarantine:DECOMPRESS_FAILED"),
        ("corrupt", lambda: _flip(gz([_good(rng) for _ in range(4)]), rng), "quarantine:DECOMPRESS_FAILED"),
        ("trailing_garbage", lambda: gz([_good(rng)]) + bytes(rng.randrange(1, 256) for _ in range(rng.randint(2, 40))),
         "quarantine:PROTOCOL_ERROR"),
        ("invalid_utf8", lambda: gz([_good(rng), b'{"Envelope": "\xff\xfe"}', _good(rng)]), "skip:records_malformed"),
        ("deep", lambda: gz([_good(rng), b"[" * 100_000 + b"]" * 100_000, _good(rng)]), "skip:records_malformed"),
        ("deep65", lambda: gz([_good(rng), _deep_object(65), _good(rng)]), "skip:records_malformed"),
        ("long_line", lambda: gz([_good(rng), b'{"a":"' + b"x" * (17 * 1024 * 1024) + b'"}', _good(rng)]),
         "skip:records_too_large"),
        ("duplicate", lambda: _dup(rng), "skip:records_duplicate"),
        ("nul", lambda: gz([_good(rng), b'{"a":1}\x00{"b":2}', _good(rng)]), "skip:records_malformed"),
        ("not_json", lambda: gz([_good(rng), b"WARC/1.0 header line", _good(rng)]), "skip:records_malformed"),
        ("nan", lambda: gz([_good(rng), b'{"a": NaN}', _good(rng)]), "skip:records_malformed"),
        ("dup_keys", lambda: gz([_good(rng), b'{"Envelope": {}, "Envelope": {}}', _good(rng)]), "skip:records_malformed"),
        ("empty_file", lambda: b"", "quarantine:DECOMPRESS_FAILED"),
        ("not_gzip", lambda: b"plain text, not gzip\n", "quarantine:DECOMPRESS_FAILED"),
    ]
    out = []
    for i in range(n):
        name, mk, want = makers[i % len(makers)]
        out.append((f"{name}#{i}", mk(), want))
    return out


def _deep_object(depth: int) -> bytes:
    return b'{"a":' * depth + b"1" + b"}" * depth


def _flip(data: bytes, rng) -> bytes:
    b = bytearray(data)
    for _ in range(4):
        i = rng.randrange(12, max(13, len(b) - 9))
        b[i] ^= 0xFF
    return bytes(b)


def _dup(rng) -> bytes:
    g = _good(rng)
    return gz([g, g, _good(rng)])


_BOMB: dict = {}


def _bomb(rng, decoded: int = 2 * 1024 ** 3 + 1024 * 1024, chunk: int = 8 * 1024 * 1024) -> bytes:
    """A gzip member decoding to more than the 2 GiB budget, built in bounded memory (zeros compress ~1000:1); built
    once per process (the same bytes for every case: the class, not the value, is what is tested)."""
    if decoded in _BOMB:
        return _BOMB[decoded]
    import zlib
    c = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    zero = bytes(chunk)
    parts, n = [], 0
    while n < decoded:
        parts.append(c.compress(zero))
        n += chunk
    parts.append(c.flush())
    _BOMB[decoded] = b"".join(parts)
    return _BOMB[decoded]


def big_line_file(path: Path, size: int) -> None:
    """A gzip file holding one record line of ``size`` bytes, streamed to disk (the 1 GiB line of A-MP-01)."""
    with gzip.open(path, "wb", compresslevel=1) as f:
        f.write(b'{"a":"')
        left, chunk = size, b"x" * (8 * 1024 * 1024)
        while left > 0:
            f.write(chunk[:min(left, len(chunk))])
            left -= len(chunk)
        f.write(b'"}\n')
