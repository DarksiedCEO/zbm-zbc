"""The Link Extractor (``cc_link_extractor``, proposed name Esther, name pending Q-MP-1): turn WAT records into
normalised, personal-data-scrubbed link rows (marcus-pipeline-spec/1 B-MP-20..27, CLARIFICATIONS v1.1 GP-2, GP-5).

  - B-MP-20: one WAT JSON record a line (standard library only); only ``WARC-Type == "response"`` records of HTML
    pages with link metadata are selected; a line that is not valid UTF-8 JSON (duplicate keys and NaN included),
    deeper than 64 levels or larger than 16 MiB is skipped and counted; an identical record twice in one file is
    counted once (``records_duplicate``);
  - B-MP-21: per link the source (the record's target URI), the target (resolved against ``Head.Base`` or the page),
    the anchor text, the element / attribute path and ``rel``; only http / https targets (others counted by scheme in
    a closed set of buckets); at most 10,000 links a page (the excess counted);
  - B-MP-22: ``normalise``: scheme and host lower case, IDNA (the host rule of ``agents/domain_match.py``), trailing
    dot and default port removed, dot segments removed, fragment dropped, tracking parameters dropped (the core URL
    lane's list: ``secret_guard.TRACKING_PARAMS`` and ``utm_*``, ADR 0050 U-1), user info NEVER kept, more than 2,048
    characters dropped and counted;
  - B-MP-23: ``scrub_url`` / ``scrub_anchor`` run BEFORE ``make_row``: every path segment and query value the core
    URL lane reads (``secret_guard.inspect_url``) becomes its placeholder (``{secret}`` / ``{withheld}``), every one
    ``prompt_guard.personal_data`` reads becomes ``{withheld}``; a host with a finding withholds the link (counted);
    an anchor with a secret or personal-data finding becomes ``{withheld}``; anchors are NFC, without control or
    format characters, bounded to 256 characters. The wave1 placeholders ``{email} {number} {uuid} {token}`` come
    from ``primitives/codes.py`` (GP-5, CL-0): BLOCKED until it is on the base (``_wave1_placeholder``);
  - B-MP-24: ``rel`` is ``UNKNOWN`` unless the caller says rel capture is VERIFIED (a recorded Phase-0 spike);
  - B-MP-25: deterministic (same lines, same rows, same order; ``row_hash`` is stable).
Anchors are data: nothing in one is ever read as an instruction (AC-3)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Optional
from urllib.parse import quote, unquote, unquote_plus, urljoin, urlsplit

from agents import domain_match, prompt_guard, secret_guard

AGENT = "cc_link_extractor"
MAX_DEPTH = 64
MAX_RECORD = 16 * 1024 * 1024
MAX_LINKS = 10_000
MAX_URL = 2048
ANCHOR_MAX = 256
ROW_KEYS = ("src", "dst", "anchor", "path", "rel")
REL_TOKENS = frozenset(("follow", "nofollow", "sponsored", "ugc", "noopener", "noreferrer", "external"))
SCHEME_BUCKETS = ("mailto", "javascript", "ftp", "tel", "data", "file")
WITHHELD = "{withheld}"
_ELEM = re.compile(r"[A-Za-z][A-Za-z0-9]{0,15}@/[a-z][a-z0-9_-]{0,31}")
_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}:")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"


class Counts(dict):
    """Named counters (machine names only); a missing name reads 0."""

    def __missing__(self, key):
        return 0


def _bad_text(s: str) -> bool:
    return any(ord(c) < 0x21 or ord(c) == 0x7F or c == "\\" for c in s)


def _remove_dots(path: str) -> str:
    """RFC 3986 5.2.4 remove_dot_segments."""
    out: list = []
    segs = path.split("/")
    for i, s in enumerate(segs):
        if s == ".":
            if i == len(segs) - 1:
                out.append("")
            continue
        if s == "..":
            if len(out) > 1:
                out.pop()
            if i == len(segs) - 1:
                out.append("")
            continue
        out.append(s)
    res = "/".join(out)
    return res if res.startswith("/") else "/" + res


def _tracking(key: str) -> bool:
    k = unquote_plus(key).lower()
    return k in secret_guard.TRACKING_PARAMS or k.startswith("utm_")


def normalise(url, base: Optional[str] = None) -> tuple:
    """(the normalised absolute URL, None) or (None, the counter naming why it was dropped)."""
    if not isinstance(url, str):
        return None, "urls_invalid"
    if len(url) > MAX_URL:
        return None, "urls_too_long"
    url = url.strip(" ")
    if not url or _bad_text(url):
        return None, "urls_invalid"
    m = _SCHEME.match(url)
    if m is None:
        if not base:
            return None, "urls_invalid"
        try:
            url = urljoin(base, url)
        except ValueError:
            return None, "urls_invalid"
        m = _SCHEME.match(url)
        if m is None:
            return None, "urls_invalid"
    scheme = m.group(0)[:-1].lower()
    if scheme not in ("http", "https"):
        return None, "links_scheme:" + (scheme if scheme in SCHEME_BUCKETS else "other")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None, "urls_invalid"
    netloc = p.netloc.rsplit("@", 1)[-1]                  # user info is NEVER kept
    raw_host = netloc.rsplit(":", 1)[0] if ":" in netloc and not netloc.endswith("]") else netloc
    host = domain_match._ascii(unquote(raw_host)) if raw_host and "%" not in raw_host else None
    if host is None or not all(_LABEL.fullmatch(lb) for lb in host.split(".")):
        return None, "urls_invalid"
    if port == 0:
        return None, "urls_invalid"
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None                                       # the default port is removed
    path = _remove_dots(quote(p.path or "/", safe=_PATH_SAFE))
    pieces = [x for x in (p.query or "").split("&") if x and not _tracking(x.split("=", 1)[0])]
    query = quote("&".join(pieces), safe=_QUERY_SAFE)
    out = f"{scheme}://{host}" + (f":{port}" if port else "") + path + (f"?{query}" if query else "")
    if len(out) > MAX_URL:
        return None, "urls_too_long"
    return out, None


def _wave1_placeholder(segment: str) -> Optional[str]:
    """GP-5 / CL-0: the wave1 placeholder rules (``{email}``, ``{number}``, ``{uuid}``, ``{token}``) live in
    ``primitives/codes.py``, which is not on this base. BLOCKED: no local re-implementation (a second implementation
    would be a second store-codes model); the conformance tests of these placeholders are strict xfail."""
    return None


def _pd(text: str) -> bool:
    try:
        return prompt_guard.personal_data(text) is not None
    except Exception:  # noqa: BLE001 - fail closed: a reader that fails withholds
        return True


def _ph(kind: Optional[str]) -> str:
    return secret_guard.URL_PLACEHOLDER.get(kind, WITHHELD)


def scrub_url(url: str) -> Optional[str]:
    """A normalised URL with every part the core URL lane or ``personal_data`` reads replaced by its placeholder; None
    when the host itself has a finding (the link is withheld whole)."""
    u = secret_guard.inspect_url(url)
    if u is None or not u["host"] or u["host_kind"] is not None or u["userinfo"] or _pd(u["host"]):
        return None
    segs = []
    for seg, kind in u["segments"]:
        if kind is not None:
            segs.append(_ph(kind))
        elif seg and _pd(unquote(seg)):
            segs.append(WITHHELD)
        else:
            segs.append(_wave1_placeholder(seg) or seg)
    p = urlsplit(url)
    pieces = [x for x in (p.query or "").split("&") if x]
    query = ""
    if pieces:
        if len(pieces) != len(u["query"]):
            query = WITHHELD
        else:
            out = []
            for piece, (k, v, kind, _trk) in zip(pieces, u["query"]):
                kraw = piece.split("=", 1)[0]
                if _pd(k) or (kind is not None and secret_guard.check(k) is not None):
                    out.append(f"{_ph(kind)}={_ph(kind)}")
                elif kind is not None:
                    out.append(f"{kraw}={_ph(kind)}")
                elif v and _pd(v):
                    out.append(f"{kraw}={WITHHELD}")
                else:
                    out.append(piece)
            query = "&".join(out)
    netloc = p.netloc
    return f"{p.scheme}://{netloc}" + "/".join(segs) + (f"?{query}" if query else "")


def anchor_rule(text) -> Optional[str]:
    """The rule that withholds ``text`` as an anchor (``core_check:<check>`` / ``personal:<kind>``), or None."""
    if not isinstance(text, str) or not text:
        return None
    clean = _clean_anchor(text)
    for t in (text, clean):
        try:
            name = secret_guard.check(t)
        except Exception:  # noqa: BLE001 - fail closed
            return "core_check:error"
        if name is not None:
            return f"core_check:{name}"
        try:
            kind = prompt_guard.personal_data(t)
        except Exception:  # noqa: BLE001 - fail closed
            return "personal:error"
        if kind is not None:
            return f"personal:{kind}"
    return None


def _clean_anchor(text: str) -> str:
    t = unicodedata.normalize("NFC", text)
    return "".join(c for c in t if unicodedata.category(c) not in ("Cc", "Cf", "Cs"))


def scrub_anchor(text) -> str:
    if not isinstance(text, str) or not text:
        return ""
    if anchor_rule(text) is not None:
        return WITHHELD
    return _clean_anchor(text)[:ANCHOR_MAX]


def make_row(src: str, dst: str, anchor: str, path: str, rel: str) -> dict:
    """The one constructor of a link row: its arguments are already normalised and scrubbed (B-MP-23)."""
    return {"src": src, "dst": dst, "anchor": anchor, "path": path, "rel": rel}


def row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps([row[k] for k in ROW_KEYS], separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def _rel(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(t for t in value.lower().split() if t in REL_TOKENS)


def _no_dups(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _refuse_constant(_c):
    raise ValueError("non-finite number")


def _depth_ok(obj) -> bool:
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if isinstance(o, (dict, list)):
            if d > MAX_DEPTH:
                return False
            stack.extend((v, d + 1) for v in (o.values() if isinstance(o, dict) else o))
    return True


def parse_record(line, counts: Counts):
    """The record as a dict, or None (counted)."""
    if not isinstance(line, (bytes, bytearray)) or len(line) > MAX_RECORD:
        counts["records_too_large"] += 1
        return None
    try:
        obj = json.loads(bytes(line).decode("utf-8"), object_pairs_hook=_no_dups, parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        counts["records_malformed"] += 1
        return None
    if not _depth_ok(obj):
        counts["records_malformed"] += 1
        return None
    return obj


def _get(o, *keys):
    for k in keys:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def page_links(obj, counts: Counts):
    """(source URI, base, links) of a selected record, or None (counted ``records_skipped``)."""
    whm = _get(obj, "Envelope", "WARC-Header-Metadata")
    html = _get(obj, "Envelope", "Payload-Metadata", "HTTP-Response-Metadata", "HTML-Metadata")
    if not isinstance(whm, dict) or whm.get("WARC-Type") != "response" or not isinstance(html, dict):
        counts["records_skipped"] += 1
        return None
    uri, links = whm.get("WARC-Target-URI"), html.get("Links")
    if not isinstance(uri, str) or not isinstance(links, list):
        counts["records_skipped"] += 1
        return None
    base = _get(html, "Head", "Base")
    return uri, base if isinstance(base, str) else None, links


def extract_lines(lines, counts: Counts, rel_verified: bool = False, on_page=None, check=None):
    """Rows for every line (bytes, or the fetcher's TOO_LARGE marker). ``on_page(scrubbed source URL)`` is called for
    every selected page whose source URL is kept (the crawled-page evidence of B-MP-31); ``check`` between records."""
    seen = set()
    for line in lines:
        if check is not None:
            check()
        if isinstance(line, str):
            counts["records"] += 1
            counts["records_too_large"] += 1
            continue
        if not line:
            continue
        counts["records"] += 1
        digest = hashlib.sha256(line).digest()
        if digest in seen:
            counts["records_duplicate"] += 1
            continue
        obj = parse_record(line, counts)
        if obj is None:
            continue
        seen.add(digest)
        got = page_links(obj, counts)
        if got is None:
            continue
        counts["records_selected"] += 1
        uri, base, links = got
        src_norm, why = normalise(uri)
        src = scrub_url(src_norm) if src_norm is not None else None
        if src is None:
            counts["sources_" + ("withheld" if src_norm is not None else "invalid")] += 1
            continue
        if on_page is not None:
            on_page(src)
        b, _ = normalise(base) if base else (None, None)
        yield from _rows(src, b or src_norm, links, counts, rel_verified)


def _rows(src, base, links, counts, rel_verified):
    if len(links) > MAX_LINKS:
        counts["links_over_cap"] += len(links) - MAX_LINKS
    for ln in links[:MAX_LINKS]:
        counts["links"] += 1
        if not isinstance(ln, dict) or not isinstance(ln.get("url"), str):
            counts["links_dropped"] += 1
            continue
        dst_norm, why = normalise(ln["url"], base)
        if dst_norm is None:
            counts[why] += 1
            continue
        dst = scrub_url(dst_norm)
        if dst is None:
            counts["urls_withheld"] += 1
            continue
        elem = ln.get("path")
        path = elem if isinstance(elem, str) and _ELEM.fullmatch(elem) else "OTHER"
        rel = _rel(ln.get("rel")) if rel_verified else "UNKNOWN"
        yield make_row(src, dst, scrub_anchor(ln.get("text")), path, rel)
