"""
The stored form of every crawled or declared URL, path and header value (spec wave1-spec/1 B-CD-03 / B-CD-04, D-01;
store codes, primitives/codes.py). Every crawled or declared URL or path that enters an envelope, a report, a
quarantine / observation record, a log record or an API response goes through ``safe_url`` / ``safe_path`` here.
There is no dictionary: readability comes from the closed allowlist (D-01) only.

URL (B-CD-03)
  scheme  ``http`` / ``https`` readable; any other: the name from the closed list ``javascript data file ftp mailto
          tel`` or ``other``, then ``:`` and nothing else
  host    the tenant's registered domains and their ``www.`` twin readable (D-01 H); any other host, the tenant's
          other subdomains included, ``{c:host:..}`` of the whole lower-cased IDNA host; unparseable ``{host}``
  port    the number, only when it is not the scheme's default
  path    per B-CD-04 below
  never   userinfo, query, fragment (``query_present`` / ``fragment_present`` say whether there was one)

PATH (B-CD-04): decoded once (invisible characters removed, NFKC, confusable at-signs folded), then per ``/``
segment: empty -> empty; a well-known file name (D-01 W) -> as written; a value-destroying shape -> its placeholder
(``TEMPLATE_RULES``); anything else -> ``{c:seg:..}``. At most TEMPLATE_MAX characters (a longer stored form is the
code of the whole path, ``/{c:path:..}``).

Comparisons a finding needs (same site, canonical on another host, redirects, robots rules, sitemap membership) are
made on the raw values in memory; only the stored form is kept.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import unquote, urlsplit

import lookalikes
from primitives import codes

PATH_MAX = 2000
TEMPLATE_MAX = 300
_EMAIL = re.compile(r"[^/@\s]{1,64}@[^/@\s]{1,255}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_DIGITS = re.compile(r"\d{4,}")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-=+.~]{20,}")
_AT_WORD = re.compile(r"[(\[{<]\s*at\s*[)\]}>]|\s+at\s+|%40|&#0*64;|&commat;|\bat\b(?=[^/]*\bdot\b)", re.I)
_FILE = re.compile(r"(.+)\.([A-Za-z0-9]{1,5})")
# the closed extension list of ``{file}.<ext>`` (B-CD-04): web assets and document / media types
STATIC_EXT = frozenset({"css", "js", "mjs", "map", "svg", "ico", "woff", "woff2", "ttf", "eot", "otf", "xml", "txt",
                        "html", "htm", "php", "asp", "aspx", "jsp", "json", "webmanifest", "xhtml", "rss", "atom"})
DOC_EXT = frozenset({"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "csv", "png",
                     "jpg", "jpeg", "gif", "webp", "avif", "bmp", "tif", "tiff", "mp3", "mp4", "webm", "mov", "avi",
                     "wav", "ogg", "zip", "gz", "tar", "epub"})
FILE_EXT = STATIC_EXT | DOC_EXT
_FOLD = str.maketrans({"\uff20": "@", "\ufe6b": "@", "\u24d0": "a"})
TEMPLATE_RULES = ("invisible characters removed; decode once; NFKC-normalise and fold confusable at-signs; drop query "
                  "and fragment; per segment: a well-known file name (robots.txt, sitemap names, ...) is kept; "
                  "an e-mail in any spelling ('@', '(at)', '[at]', ' at ... dot', %40) -> {email}; ';' matrix "
                  "parameters -> {param} (the part before ';' judged on its own); UUID -> {uuid}; 7+ digits in "
                  "total -> {number}; only digits -> {id}; ',' or '=' -> {param}; a file name with an extension of "
                  "the closed list -> {file}.ext; 16+ hex, 20+ token characters, 8+ characters mixing letters and 2+ "
                  "digits, 10+ with one digit, or over 40 characters -> {token}; anything else -> a keyed code "
                  "{c:seg:...} (the owner's display shows the value); short numeric segments next to each other "
                  "with 7+ digits together -> {number}")
_LOOKALIKE = lookalikes.Table()
_PLACEHOLDER = re.compile(r"(?:\{(?:email|uuid|number|id|param|token|unreadable|host)\}|\{file\}\.[a-z0-9]{1,5}|"
                          r"\{c:[a-z]+:[0-9a-f]{16}\})(?:;\{param\})?")


def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _token(seg: str) -> bool:
    stem = seg.rsplit(".", 1)[0] if "." in seg and seg.rsplit(".", 1)[1].lower() in FILE_EXT else seg
    letters = any(c.isalpha() for c in seg)
    return bool(_HEX.fullmatch(seg) or len(seg) > 40 or _TOKENISH.fullmatch(seg) or
                (len(seg) >= 8 and _digits(seg) >= 2 and letters) or
                (len(stem) >= 10 and _digits(stem) >= 1 and any(c.isalpha() for c in stem)))


def _kind(seg: str) -> Optional[str]:
    """The placeholder for a segment judged on its lookalike fold ``seg``, or None (the segment is coded)."""
    if _UUID.fullmatch(seg):
        return "{uuid}"
    if _digits(seg) >= 7:                      # phone numbers, SSNs, card numbers, long ids, across any separator
        return "{number}"
    if seg.isdigit():
        return "{id}"
    if "," in seg or "=" in seg:               # M-S5: a key / value or list segment is a parameter
        return "{param}"
    m = _FILE.fullmatch(seg)
    if m and m.group(2).lower() in FILE_EXT:
        return "{file}." + m.group(2).lower()
    if _token(seg):
        return "{token}"
    return None


def _segment(seg: str) -> str:
    """The stored form of one decoded path segment (B-CD-04)."""
    if not seg:
        return ""
    if _PLACEHOLDER.fullmatch(seg):            # an already-stored form read again is kept
        return seg
    if codes.well_known(seg):
        return seg
    folded = _LOOKALIKE.fold_cased(seg)
    if "@" in folded or _EMAIL.search(folded) or _AT_WORD.search(folded):
        return "{email}"
    if ";" in folded:
        if ";" not in seg:                     # a lookalike of ';': the whole segment is a parameter
            return "{param}"
        base = seg.split(";", 1)[0]
        return (_segment(base) if base else "") + ";{param}"
    kind = _kind(folded)
    if kind is not None:
        return kind
    return codes.code("seg", seg)


def _path(target: str) -> Optional[str]:
    """The decoded path of a request target or URL (query and fragment dropped); None when it is not a path."""
    try:
        p = urlsplit(target).path if "://" in target else target.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        return None
    p = unquote(p)[:PATH_MAX]
    if not p.startswith("/") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in p):
        return None
    return p


def decoded_path(target) -> Optional[str]:
    """The path a stored form is made from (decoded once, invisibles removed, NFKC, at-signs folded): what the log
    task codes to match the sitemap sample (B-LG-05)."""
    if not isinstance(target, str):
        return None
    p = _path(lookalikes.strip_invisible(target))
    if p is None:
        return None
    return lookalikes.strip_invisible(unicodedata.normalize("NFKC", p)).translate(_FOLD)


def template_path(target, strict: bool = True, words: bool = False) -> Optional[str]:
    """A path in its stored form (B-CD-04); None when it is not a path. ``strict`` / ``words`` are accepted for the
    callers of the round-2 signature and change nothing (one rule)."""
    p = decoded_path(target)
    if p is None:
        return None
    segs = p.split("/")[1:]
    out = [_segment(s) for s in segs]
    # digits split over several short numeric segments (/123/45/6789) count together
    i = 0
    while i < len(segs):
        j = i
        while j < len(segs) and segs[j].isdigit() and out[j] == "{id}":
            j += 1
        if j - i >= 2 and sum(_digits(segs[k]) for k in range(i, j)) >= 7:
            for k in range(i, j):
                out[k] = "{number}"
        i = max(j, i + 1)
    t = "/" + "/".join(out)
    if len(t) > TEMPLATE_MAX:
        return "/" + codes.code("path", p)
    return t


def safe_path(path) -> Optional[str]:
    """A crawled / declared path (query and fragment dropped) in its stored form; ``{unreadable}`` when it is not a
    path; None for None."""
    if path is None:
        return None
    t = template_path(path) if isinstance(path, str) else None
    return t if t is not None else "{unreadable}"


_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,15}")
_HOST_LDH = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*\.?")
_DEFAULT_PORT = {"http": 80, "https": 443}
OTHER_SCHEMES = ("javascript", "data", "file", "ftp", "mailto", "tel")


def safe_host(raw) -> str:
    """D-01 H: an own host readable (lower-case IDNA, no trailing dot); any other host ``{c:host:..}`` of the whole
    lower-cased IDNA host (an IP literal included); ``{host}`` when it cannot be parsed."""
    if not raw or not isinstance(raw, str):
        return "{host}"
    if _PLACEHOLDER.fullmatch(raw):                # an already-stored form read again is kept
        return raw
    h = raw.strip().lower().rstrip(".")
    if ":" in h:                                   # an IPv6 literal (urlsplit gives it without brackets)
        return codes.code("host", h) if re.fullmatch(r"[0-9a-f:.%a-z]{2,60}", h) else "{host}"
    try:
        h = h.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return "{host}"
    if not (len(h) <= 253 and _HOST_LDH.fullmatch(h)):
        return "{host}"
    return h if codes.own_host(h) else codes.code("host", h)


def safe_url(url) -> Optional[str]:
    """The stored form of a crawled or declared URL (B-CD-03); None for None; ``{unreadable}`` for something that
    is not a URL; a bare path is stored as a path."""
    if url is None:
        return None
    if not isinstance(url, str):
        return "{unreadable}"
    u = url.strip()
    try:
        p = urlsplit(u)
        host = p.hostname
        port = p.port
    except ValueError:
        return "{unreadable}"
    scheme = (p.scheme or "").lower()
    if not scheme:
        return safe_path(u) if u.startswith("/") else "{unreadable}"
    if not _SCHEME.fullmatch(scheme):
        return "other:"
    if scheme not in _DEFAULT_PORT:
        return (scheme if scheme in OTHER_SCHEMES else "other") + ":"
    if not host:
        return scheme + "://{host}"
    h = safe_host(host)
    if ":" in host and not h.startswith("{"):
        h = "[" + h + "]"
    port_s = f":{port}" if port is not None and port != _DEFAULT_PORT[scheme] else ""
    return f"{scheme}://{h}{port_s}{safe_path(p.path or '/')}"


def query_present(url) -> bool:
    try:
        return bool(isinstance(url, str) and urlsplit(url).query)
    except ValueError:
        return False


def fragment_present(url) -> bool:
    try:
        return bool(isinstance(url, str) and urlsplit(url).fragment)
    except ValueError:
        return False


def safe_urls(urls, limit: int) -> list:
    return [safe_url(u) for u in list(urls)[:limit]]


# ------------------------------------------------------------------------------------------- other crawled values

MEDIA_TYPES = frozenset({
    "text/html", "text/plain", "text/markdown", "text/x-markdown", "text/xml", "text/css", "text/csv",
    "text/javascript", "application/xhtml+xml", "application/xml", "application/json", "application/ld+json",
    "application/javascript", "application/rss+xml", "application/atom+xml", "application/gzip",
    "application/x-gzip", "application/octet-stream", "application/pdf", "application/zip", "image/png",
    "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/x-icon", "image/avif", "video/mp4",
    "audio/mpeg"})


def safe_ctype(ctype) -> Optional[str]:
    """A Content-Type's media type as stored: readable when it is on the closed ``MEDIA_TYPES`` list, else
    ``{c:ctype:..}``."""
    if ctype is None:
        return None
    if not isinstance(ctype, str):
        return "{unreadable}"
    return ctype if ctype in MEDIA_TYPES else codes.code("ctype", ctype)


# X-Robots-Tag / meta robots directives (Google's and Bing's documentation; Wave-1 list). A directive is stored only
# from this list (with a known bot token prefix where one was given); anything else is coded (D-01 V).
ROBOTS_DIRECTIVES = frozenset({"all", "index", "noindex", "follow", "nofollow", "none", "noarchive", "nocache",
                               "nosnippet", "notranslate", "noimageindex", "nositelinkssearchbox",
                               "indexifembedded", "noodp", "noydir", "noai", "noimageai", "unavailable_after",
                               "max-snippet", "max-image-preview", "max-video-preview"})
_DIRECTIVE_VALUE = re.compile(r"-1|[0-9]{1,6}|none|standard|large")
_EXTRA_BOTS = ("googlebot-news", "googlebot-image", "googlebot-video", "adsbot-google", "mediapartners-google",
               "msnbot", "bingpreview", "yandex", "baiduspider", "duckduckbot", "robots")
_KNOWN: Optional[frozenset] = None
# the separator between several X-Robots-Tag header lines kept by the fetcher (a newline never occurs in a header
# value): each line is read on its own, so a user-agent prefix in one never reaches into the next
HEADER_SEP = "\n"


def known_bots() -> frozenset:
    """Bot names an X-Robots-Tag / meta robots prefix is kept as (lower-cased): the versioned bot families
    (agents/bots.py) plus a few common search-crawler names; any other prefix is coded."""
    global _KNOWN
    if _KNOWN is None:
        from agents import bots                  # versioned data; imported here to keep primitives import-light
        _KNOWN = frozenset({f["token"].lower() for f in bots.FAMILIES} | set(_EXTRA_BOTS))
    return _KNOWN


def robots_rules(value) -> list:
    """X-Robots-Tag (or meta robots) text -> [(bot lower-cased or None, directive lower-cased, value or None)] in
    order, RAW (for decisions in memory). Several header lines (``HEADER_SEP``) are independent."""
    out = []
    if not isinstance(value, str):
        return out
    for line in value.split(HEADER_SEP)[:50]:
        bot = None
        for item in line.split(",")[:50]:
            it = item.strip()
            if not it:
                continue
            head, sep, rest = it.partition(":")
            h = head.strip().lower()
            if sep and h not in ROBOTS_DIRECTIVES:
                bot = h                            # "<bot>: directive": applies to the directives after it
                it = rest.strip()
                head, sep, rest = it.partition(":")
                h = head.strip().lower()
            if not h:
                continue
            out.append((bot, h, rest.strip() if sep else None))
    return out


def robots_directives(value) -> list:
    """The STORED form of ``robots_rules``: [(bot or None, directive)]; a bot name is kept lower-cased only when it
    is a known one (``known_bots``), else coded; a directive or a value off the closed lists is coded."""
    out = []
    for bot, d, v in robots_rules(value):
        b = None if bot is None else (bot if bot in known_bots() else codes.code("text", bot))
        if d in ROBOTS_DIRECTIVES:
            if d == "unavailable_after" and v is not None:
                out.append((b, "unavailable_after:" + codes.code("date", v)))
            elif v is not None and d.startswith("max-"):
                out.append((b, f"{d}:{v}" if _DIRECTIVE_VALUE.fullmatch(v) else f"{d}:" + codes.code("text", v)))
            else:
                out.append((b, d))
        else:
            out.append((b, codes.code("text", d if v is None else f"{d}:{v}")))
    return out


def safe_x_robots(value) -> Optional[str]:
    """The stored form of the X-Robots-Tag header line(s): known directives and bot tokens, everything else coded."""
    if value is None:
        return None
    parts = [f"{b}: {d}" if b else d for b, d in robots_directives(value)]
    s = ", ".join(parts)
    return s if len(s) <= 600 else s[:600].rsplit(", ", 1)[0]
