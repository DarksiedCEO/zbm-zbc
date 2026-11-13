"""
The one URL rule (AEGIS Wave-1 certification M-S2, M-S4, M-D3, M-O3): every crawled or declared URL or path that
enters an envelope, a report, a quarantine / observation record, a log record or an API response goes through
``safe_url`` / ``safe_path`` here first. What is kept:

  scheme      lower-cased; ``http`` / ``https`` (any other scheme is kept as its name only, e.g. ``javascript:``)
  host        lower-cased, an IDN as punycode, userinfo dropped, a port kept only when it is not the scheme's default
  path        TEMPLATED (``template_path``): identifier-looking segments replaced by a typed placeholder; at most
              TEMPLATE_MAX characters
  query       never (``query_present`` says whether there was one, for a finding that needs it)
  fragment    never

Comparisons a finding needs (same site, canonical on another host, redirects, robots rules, sitemap membership) are
made on the raw values in memory; only the safe form is stored. Ordinary readable paths stay readable: a segment
that is a plain word, or a slug of plain words (``how-to-choose-running-shoes``), is kept as written.

Template rules (``TEMPLATE_RULES``), per segment, judged on its lookalike fold (the repo's shared table):
an e-mail in any spelling -> {email}; UUID -> {uuid}; 7+ digits in total -> {number}; only digits -> {id}; a file
name with a non-code extension -> {file}.ext; a parameter segment (``;`` matrix parameters, ``,`` or ``=``) ->
``{param}`` (the part before a ``;`` judged on its own); a slug of plain words (letters, at most 4 digits) is kept;
16+ hex, 20+ token characters, 8+ characters mixing letters and 2+ digits (in a stored crawled URL also 10+ with
one digit), or over 40 characters -> {token}; any run of 4+ digits left -> {id}; short numeric segments next to each
other with 7+ digits together -> {number}. A segment of letters only that is not slug-shaped is kept by these rules
(a random all-letter token cannot be told from a word by its shape): see the word rule below.

Round 2 (AEGIS Wave-1 certification round 2 M5; decision 2026-11-09 "split storage from display"): a segment the
rules above would keep readable is kept ONLY when it is ordinary (``words=True``, the stored form of crawled URLs
and of log path templates): every word in it (split on ``- _ . ~ +``, case changes and letter / digit boundaries;
1-4 digit numbers and single ASCII letters are words) is in the bundled ordinary-word list ``path_words.txt``
(wordfreq top-N of the 18 languages used elsewhere in seo-py plus English, minus given names, surnames and common
secret words, plus the exempt names that are also common words; the file header states sources, versions and
counts), and the segment does not follow a person or secret context segment (``/user/<x>``, ``/author/<x>``,
``/token/<x>``, ``/reset/<x>``: ``CONTEXT_NAME`` / ``CONTEXT_SECRET``). Anything else is stored as a keyed digest
token ``{seg:xxxxxxxx}`` (HMAC-SHA-256 with the run's per-tenant path key, ``keyed()``; stable within a tenant so
reports can still count and group), or ``{seg}`` when no key is set. Already-safe placeholders are kept as they
are. Disclosed residual: a name, or a secret word, that is also a common word (``[exempt]`` in the data file) stays
readable outside a context segment.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import unicodedata
from contextvars import ContextVar
from typing import Optional
from urllib.parse import unquote, urlsplit

import lookalikes

PATH_MAX = 2000
TEMPLATE_MAX = 300
_EMAIL = re.compile(r"[^/@\s]{1,64}@[^/@\s]{1,255}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_DIGITS = re.compile(r"\d{4,}")
_TOKENISH = re.compile(r"[A-Za-z0-9_\-=+.~]{20,}")
_AT_WORD = re.compile(r"[(\[{<]\s*at\s*[)\]}>]|\s+at\s+|%40|&#0*64;|&commat;|\bat\b(?=[^/]*\bdot\b)", re.I)
_FILE = re.compile(r"(.+)\.([A-Za-z0-9]{1,5})")
# a slug piece: a plain word (lower-case, Capitalised, or an upper-case word of at most 5 letters), or 1-4 digits
_WORD_PIECE = re.compile(r"[a-z]{1,20}|[A-Z][a-z]{1,19}|[A-Z]{1,5}|[0-9]{1,4}")
_CONSONANT_RUN = re.compile(r"[bcdfghjklmnpqrstvwxz]{6,}", re.I)
SLUG_MAX = 100
STATIC_EXT = frozenset({"css", "js", "mjs", "map", "svg", "ico", "woff", "woff2", "ttf", "eot", "otf", "xml", "txt",
                        "html", "htm", "php", "asp", "aspx", "jsp", "json", "webmanifest", "xhtml", "rss", "atom"})
_FOLD = str.maketrans({"\uff20": "@", "\ufe6b": "@", "\u24d0": "a"})
TEMPLATE_RULES = ("invisible characters removed; decode once; NFKC-normalise and fold confusable at-signs; drop query "
                  "and fragment; per segment, judged on its lookalike fold (the repo's shared table): "
                  "an e-mail in any spelling ('@', '(at)', '[at]', ' at ... dot', %40) -> {email}; UUID -> {uuid}; "
                  "7+ digits in total, whatever separates them -> {number}; only digits -> {id}; a file name with "
                  "a non-code extension -> {file}.ext; a parameter segment (';' matrix parameters, ',' or '=') -> "
                  "{param} (the part before ';' judged on its own); a slug of plain words (at most 4 digits) is "
                  "kept; 16+ hex, 20+ token characters, 8+ characters mixing letters and 2+ digits (10+ with 1 "
                  "digit in a stored crawled URL), or over 40 characters -> {token}; any run of 4+ digits left -> "
                  "{id}; short numeric segments next to each other with 7+ digits together -> {number}")
_LOOKALIKE = lookalikes.Table()

# ------------------------------------------------------------------------------------- ordinary words (round 2 M5)

WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "path_words.txt")
CONTEXT_NAME = frozenset({"user", "users", "u", "profile", "profiles", "people", "person", "persons", "author",
                          "authors", "member", "members", "team", "staff", "customer", "customers", "employee",
                          "employees", "patient", "patients", "client", "clients", "contact", "contacts"})
CONTEXT_SECRET = frozenset({"token", "tokens", "key", "keys", "apikey", "api-key", "api_key", "secret", "secrets",
                            "password", "passwords", "passwd", "pwd", "reset", "reset-password", "session",
                            "sessions", "sid", "otp", "invite", "invites", "invitation", "invitations", "verify",
                            "verification", "confirm", "confirmation", "activate", "activation", "unsubscribe",
                            "magic", "magic-link", "auth"})
_WORDS: Optional[frozenset] = None
_EXEMPT: Optional[frozenset] = None
_KEY: ContextVar = ContextVar("seo_path_key", default=None)
_PLACEHOLDER = re.compile(r"\{[a-z]+(?::[0-9a-f]{8})?\}(?:\.[a-z0-9]{1,5})?(?:;\{param\})?")
_SPLIT = re.compile(r"[-_.~+;,=\s]+")
_PH_ANY = re.compile(r"\{[a-z]+(?::[0-9a-f]{8})?\}")
_CAMEL = re.compile(r"(?<=[a-z\u00df-\u00ff])(?=[A-Z\u00c0-\u00de])|(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])")


def _load_words() -> tuple[frozenset, frozenset]:
    global _WORDS, _EXEMPT
    if _WORDS is None:
        ordinary, exempt, section = set(), set(), None
        with open(WORDS_FILE, encoding="ascii") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if line.startswith("["):
                    section = line
                    continue
                w = line.encode("ascii").decode("unicode_escape") if "\\" in line else line
                (ordinary if section == "[ordinary]" else exempt).add(w)
        _WORDS, _EXEMPT = frozenset(ordinary), frozenset(exempt)
    return _WORDS, _EXEMPT


def tenant_key(base: Optional[bytes], tenant_id: str) -> Optional[bytes]:
    """The per-tenant path key: HMAC of the service's log hash key with the tenant id (None without a base key)."""
    if not base:
        return None
    return hmac.new(base, b"seo-path-key\x00" + tenant_id.encode("utf-8"), hashlib.sha256).digest()


@contextlib.contextmanager
def keyed(key: Optional[bytes]):
    """Within this block the stored forms digest non-ordinary segments with ``key`` (``{seg:xxxxxxxx}``)."""
    tok = _KEY.set(key)
    try:
        yield
    finally:
        _KEY.reset(tok)


def digest(value: str, purpose: str = "seg") -> Optional[str]:
    """8 hex characters of HMAC-SHA-256(key, purpose NUL value), or None when no key is set."""
    key = _KEY.get()
    if not key:
        return None
    msg = purpose.encode("ascii") + b"\x00" + value.encode("utf-8", "surrogatepass")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:8]


def seg_token(seg: str) -> str:
    d = digest(seg)
    return "{seg:" + d + "}" if d else "{seg}"


def words_of(seg: str) -> list:
    """The words of a segment: split on - _ . ~ + and white space, on case changes and on letter / digit
    boundaries; NFC, case-folded."""
    out = []
    for part in _SPLIT.split(seg):
        for w in _CAMEL.split(part):
            if w:
                out.append(unicodedata.normalize("NFC", w).casefold())
    return out


def ordinary_segment(seg: str) -> bool:
    """Every word of ``seg`` is ordinary: in the bundled list, or a 1-4 digit number, or a single ASCII letter."""
    words, _ = _load_words()
    ws = words_of(seg)
    if not ws:
        return True
    for w in ws:
        if w.isdigit():
            if len(w) <= 4 and w.isascii():
                continue
            return False
        if len(w) == 1 and w.isascii() and w.isalpha():   # a single Latin letter; a CJK character may be a name
            continue
        if w not in words:
            return False
    return True


def _digits(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _slug(seg: str) -> bool:
    """A slug of plain words joined by ``-`` / ``_`` (``how-to-choose-running-shoes``, ``best-shoes-2024``): kept
    readable. Every piece is a word-like run of letters (a vowel, no run of 6+ consonants) or 1-4 digits, at most 4
    digits in all, at least one word, at most SLUG_MAX characters."""
    if len(seg) > SLUG_MAX:
        return False
    stem, dot, ext = seg.rpartition(".")
    if dot and ext.lower() in STATIC_EXT:           # how-to-choose-running-shoes.html
        seg = stem
    pieces = re.split(r"[-_]", seg)
    if len(pieces) < 2 or any(not p for p in pieces) or _digits(seg) > 4:
        return False
    words = 0
    for p in pieces:
        if not p.isascii():
            # round 2: a word in another script (or with accents) is a slug piece too; whether it may be stored
            # readable is the word rule's decision (``ordinary_segment``)
            if len(p) > 30 or not all(unicodedata.category(c)[0] in "LM" for c in p):
                return False
            words += 1
            continue
        if not _WORD_PIECE.fullmatch(p):
            return False
        if p.isdigit():
            continue
        if len(p) > 3 and (not re.search(r"[aeiouy]", p, re.I) or _CONSONANT_RUN.search(p)):
            return False
        words += 1
    return words > 0


def _kind(seg: str, raw: Optional[str] = None, strict: bool = False) -> Optional[str]:
    """The placeholder for a segment judged on its fold ``seg``; ``raw`` (as written, NFKC) decides the slug rule,
    which reads letter case (an upper-case word of more than 5 letters is not a slug word)."""
    if "@" in seg or _EMAIL.search(seg) or _AT_WORD.search(seg):
        return "{email}"
    if _UUID.fullmatch(seg):
        return "{uuid}"
    if _digits(seg) >= 7:                      # phone numbers, SSNs, card numbers, long ids, across any separator
        return "{number}"
    if seg.isdigit():
        return "{id}"
    if "," in seg or "=" in seg:               # M-S5: a key / value or list segment is a parameter, whatever its digits
        return "{param}"
    m = _FILE.fullmatch(seg)
    if m and m.group(2).lower() not in STATIC_EXT:
        return "{file}." + m.group(2).lower()  # a document or image file name may name a person
    if _slug(raw if raw is not None else seg):
        return None
    stem = seg.rsplit(".", 1)[0] if "." in seg and seg.rsplit(".", 1)[1].lower() in STATIC_EXT else seg
    if _HEX.fullmatch(seg) or len(seg) > 40 or _TOKENISH.fullmatch(seg) or (
            len(seg) >= 8 and _digits(seg) >= 2 and any(c.isalpha() for c in seg)) or (
            strict and len(stem) >= 10 and _digits(stem) >= 1 and any(c.isalpha() for c in stem)):
        return "{token}"
    return None


def _segment(seg: str, strict: bool = False) -> str:
    """The placeholder for one path segment, or the segment itself when nothing in it looks identifying. Judged on
    the segment's lookalike fold (war room, ADR 0017 W3-3); a segment kept is kept as written (NFKC, invisible
    characters removed), never as its fold. A ``;`` starts matrix parameters: the part before it is judged on its
    own and every parameter becomes ``{param}`` (M-S5)."""
    if not seg:
        return ""
    if _PLACEHOLDER.fullmatch(seg):            # an already-safe placeholder (a stored form read again) is kept
        return seg
    folded = _LOOKALIKE.fold_cased(seg)
    if ";" in folded:
        if "@" in folded or _EMAIL.search(folded) or _AT_WORD.search(folded):
            return "{email}"
        if ";" not in seg:                     # a lookalike of ';': the whole segment is a parameter
            return "{param}"
        base = seg.split(";", 1)[0]
        head = _segment(base, strict) if base else ""
        return head + ";{param}"
    kind = _kind(folded, seg, strict)
    if kind is not None:
        return kind
    return seg[:SLUG_MAX] if _slug(seg) else _DIGITS.sub("{id}", seg)[:40]


def _path(target: str) -> Optional[str]:
    try:
        p = urlsplit(target).path if "://" in target else target.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        return None
    p = unquote(p)[:PATH_MAX]
    if not p.startswith("/") or any(ord(c) < 0x20 or ord(c) == 0x7F for c in p):
        return None
    return p


def template_path(target: str, strict: bool = False, words: bool = False) -> Optional[str]:
    """A path with every identifier-looking part replaced by a typed placeholder; None when not a path.
    NFKC-normalised and confusables folded first (fullwidth digits and at-signs become ASCII). ``strict`` (the stored
    form of crawled URLs, ``safe_path``) also templates a 10+ character segment with a single digit; the Search
    Console templates keep their pinned rules. ``words`` (stored crawled URLs and log path templates, round 2 M5): a
    segment left readable must be ordinary (module doc), else it becomes ``{seg:xxxxxxxx}``."""
    if not isinstance(target, str):
        return None
    # war room (W3-3): invisible characters are removed BEFORE the one decode
    p = _path(lookalikes.strip_invisible(target))
    if p is None:
        return None
    p = lookalikes.strip_invisible(unicodedata.normalize("NFKC", p)).translate(_FOLD)
    segs = p.split("/")[1:]
    out = [_segment(s, strict) for s in segs]
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
    if words:
        for k, seg in enumerate(segs):
            o = out[k]
            if not seg or _PLACEHOLDER.fullmatch(o):
                continue                                # wholly a placeholder already
            prev = segs[k - 1].casefold() if k else ""
            # what is still readable in the segment (placeholders taken out) must be ordinary
            if prev in CONTEXT_NAME or prev in CONTEXT_SECRET or seg.startswith("~") \
                    or not ordinary_segment(_PH_ANY.sub(" ", o)):
                out[k] = seg_token(seg)
    return ("/" + "/".join(out))[:TEMPLATE_MAX]


def safe_path(path) -> Optional[str]:
    """A crawled / declared path (query and fragment dropped) in its stored form; ``{unreadable}`` when it is not a
    path; None for None."""
    if path is None:
        return None
    t = template_path(path, strict=True, words=True) if isinstance(path, str) else None
    return t if t is not None else "{unreadable}"


_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,15}")
_HOST_LDH = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.?")
_DEFAULT_PORT = {"http": 80, "https": 443}


def _host(raw: Optional[str]) -> str:
    if not raw:
        return ""
    h = raw.lower().rstrip(".")
    if ":" in h:                                   # an IPv6 literal (urlsplit gives it without brackets)
        return "[" + h.split("%", 1)[0] + "]" if re.fullmatch(r"[0-9a-f:.%a-z]{2,60}", h) else "{host}"
    try:
        h = h.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return "{host}"
    return h if len(h) <= 253 and _HOST_LDH.fullmatch(h) else "{host}"


def safe_url(url) -> Optional[str]:
    """The stored form of a crawled or declared URL: scheme://host[:port]/templated-path; never a query, a
    fragment or userinfo. None for None; ``{unreadable}`` for something that is not a URL."""
    if url is None:
        return None
    if not isinstance(url, str):
        return "{unreadable}"
    u = url.strip()
    try:
        p = urlsplit(u)
        host, port = p.hostname, p.port
    except ValueError:
        return "{unreadable}"
    scheme = (p.scheme or "").lower()
    if not _SCHEME.fullmatch(scheme):
        return safe_path(u) if u.startswith("/") else "{unreadable}"
    if scheme not in _DEFAULT_PORT:
        return scheme + ":"
    h = _host(host)
    if not h:
        return scheme + "://{host}"
    port_s = f":{port}" if port is not None and port != _DEFAULT_PORT[scheme] else ""
    return f"{scheme}://{h}{port_s}{safe_path(p.path or '/')}"


def query_present(url) -> bool:
    try:
        return bool(isinstance(url, str) and urlsplit(url).query)
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
    "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/x-icon", "image/avif"})
_TOP = frozenset({"text", "application", "image", "audio", "video", "font", "multipart", "message", "model"})


def safe_ctype(ctype) -> Optional[str]:
    """A Content-Type's media type as stored: kept when it is one of the common types (``MEDIA_TYPES``); else its
    top-level type with ``{other}`` (``text/{other}``), or ``{other}``. A crawled header is not free text."""
    if ctype is None:
        return None
    if not isinstance(ctype, str):
        return "{other}"
    if ctype in MEDIA_TYPES:
        return ctype
    top = ctype.split("/", 1)[0]
    return f"{top}/{{other}}" if top in _TOP and "/" in ctype else "{other}"


# X-Robots-Tag / meta robots directives (Google's and Bing's documentation; Wave-1 list). A value is stored only as
# these words (with a known bot token prefix where one was given); anything else becomes {other}.
ROBOTS_DIRECTIVES = frozenset({"all", "index", "noindex", "follow", "nofollow", "none", "noarchive", "nocache",
                               "nosnippet", "notranslate", "noimageindex", "nositelinkssearchbox",
                               "indexifembedded", "noodp", "noydir", "noai", "noimageai", "unavailable_after",
                               "max-snippet", "max-image-preview", "max-video-preview"})
_DIRECTIVE_VALUE = re.compile(r"-1|[0-9]{1,6}|none|standard|large")


_EXTRA_BOTS = ("googlebot-news", "googlebot-image", "googlebot-video", "adsbot-google", "mediapartners-google",
               "msnbot", "bingpreview", "yandex", "baiduspider", "duckduckbot", "robots")


def known_bots() -> frozenset:
    """Bot names an X-Robots-Tag / meta robots prefix is kept as (lower-cased): the versioned bot families
    (agents/bots.py) plus a few common search-crawler names; any other prefix is stored as {bot}."""
    global _KNOWN
    if _KNOWN is None:
        from agents import bots                  # versioned data; imported here to keep primitives import-light
        _KNOWN = frozenset({f["token"].lower() for f in bots.FAMILIES} | set(_EXTRA_BOTS))
    return _KNOWN


_KNOWN: Optional[frozenset] = None


def robots_directives(value) -> list:
    """X-Robots-Tag (or meta robots) text -> [(bot token lower-cased or None, directive)] in order; a directive this
    service does not know is ``{other}``; a bot name is kept lower-cased only when it is a known one (``known_bots``),
    else ``{bot}``."""
    out = []
    if not isinstance(value, str):
        return out
    bot = None
    for item in value.split(",")[:50]:
        it = item.strip().lower()
        if not it:
            continue
        head, sep, rest = it.partition(":")
        head = head.strip()
        if sep and head not in ROBOTS_DIRECTIVES:
            # "<bot>: directive" (Google: a user-agent prefix applies to the directives after it)
            bot = head if head in known_bots() else "{bot}"
            it, head, sep, rest = rest.strip(), *rest.strip().partition(":")
            head = head.strip()
        if head in ROBOTS_DIRECTIVES:
            v = rest.strip()
            if head == "unavailable_after" and sep:
                out.append((bot, "unavailable_after:{date}"))
            elif sep and head.startswith("max-"):
                out.append((bot, f"{head}:{v}" if _DIRECTIVE_VALUE.fullmatch(v) else f"{head}:{{other}}"))
            else:
                out.append((bot, head))
        elif it:
            out.append((bot, "{other}"))
    return out


def safe_x_robots(value) -> Optional[str]:
    """The stored form of an X-Robots-Tag header: known directives (and plain bot tokens) only."""
    if value is None:
        return None
    parts = [f"{b}: {d}" if b else d for b, d in robots_directives(value)]
    return ", ".join(parts)[:300]
