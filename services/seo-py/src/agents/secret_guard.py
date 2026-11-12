"""
Shared redaction core v2 for the SEO department (ADR 0050 v2, superseding the v1 text): WITHHOLD THE WHOLE FIELD UNLESS IT
IS CLEARLY SAFE. Every field bound for storage, a log or client output (answer excerpts, titles, hints, candidate
names, URL hosts / path segments / query pairs, any string swept by ``guard_record``) is

  1. NORMALISED for inspection (``views``): NFKC; Cf / Cc / Mn / Me / Co / Cn code points, variation selectors, tag
     characters and Hangul fillers removed; every Unicode space or line / paragraph separator read as one space;
     markdown emphasis, backticks and escapes removed (a view of its own) and real tags holding no address stripped
     (review 601e328a); bounded decoding (percent up to ``PERCENT_ROUNDS`` rounds, HTML
     entities, ``\\uXXXX`` / ``\\xXX`` escapes, base64- and hex-looking runs) for INSPECTION ONLY;
  2. SAFE-CHECKED on every view (``check``): (a) no credential label word from the closed multilingual list,
     (b) no ``scheme://user:pass@``, (c) no known strong-key prefix, (d) no long random-looking token, (e) no run of
     9+ digits through separators, (f) no e-mail address. The small safe-shape allowlist (UUID, ISBN, EAN-13, Amazon
     order id, ISO-8601 date-time, a commit SHA after the word "commit") is in ADR 0050 v2;
  3. WITHHELD WHOLE when any check fails: the text becomes ``[withheld]`` (a URL keeps scheme, host and the path
     segments that pass on their own). Nothing is ever cut out of the middle of a string.

Inputs over ``MAX_INPUT`` characters are withheld unread. Every step is linear in the input. The original text is
returned unchanged when it passes: normalised / decoded views are never emitted.

Public entry points (names kept from v1 so callers do not break):
  ``redact_text(text) -> (text or "[withheld]", findings)``   ``is_clearly_safe(text) -> bool``
  ``safe_url(url) -> str``   ``url_secret_segments(url) -> (bad indexes, other)``   ``url_has_secret(url) -> bool``
  ``guard_record(obj)``: fail-closed recursive sweep (dict keys and values, lists, tuples, sets, bytes, objects).

Section 5 (ADR 0051 job 3) is a SEPARATE, precise rule for the owner's decrypted view only
(``client_view_findings(text)``, ``client_view_url(url)``); it never decides what is logged, fetched or sent.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import re._parser as _sre_parse
import threading
import unicodedata
from urllib.parse import parse_qsl, unquote, unquote_plus, urlencode, urlsplit, urlunsplit

import lookalikes

WITHHELD = "[withheld]"
REDACTED = WITHHELD                      # v1 name; same marker
MAX_INPUT = 65536                        # characters; a longer field is withheld unread
URL_MAX_INPUT = 8192                     # characters; a longer URL is withheld unread (real citation URLs are short)
PERCENT_ROUNDS = 3
FRAGMENT_EXTRA_DECODE = 1                # review 08eea6e1 NIT: a URL fragment is decoded once more, as a path / query is
DECODE_RUNS_MAX = 64                     # base64 / hex runs decoded per view
DECODE_RUN_MAX = 4096                    # characters of one run decoded
DECODE_BOUNDARIES_MAX = 8                # "-" / "_" boundaries inside one base64 run also decoded from
GUARD_MAX_DEPTH = 16
GUARD_MAX_NODES = 200000
CHECKS = ("a_label", "b_userinfo", "c_prefix", "d_token", "e_digits", "f_email", "url_credential", "url_personal")

# ------------------------------------------------------------------------------------------------ 1. normalise

_FOLD = lookalikes.Table()
_HANGUL_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
_BLANKS = frozenset("\u2800\u180e")                       # braille blank, Mongolian vowel separator: read as space
# review c13529af MUST 2: every Unicode dash (hyphen, non-breaking hyphen, figure dash, en / em dash, horizontal bar,
# minus sign, small em dash, small / full-width hyphen-minus) reads as "-" in every view (so ``&hyphen;`` / ``&dash;``
# and a typed dash read as the ASCII hyphen the rules know)
_DASHES = frozenset("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d")
_DROP_CATS = frozenset(("Cf", "Cc", "Mn", "Me", "Co", "Cn", "Cs"))
_CMAP: dict = {}
_CMAP_MAX = 20000


_MAP_SPACES = True


def _map_char(c: str) -> str:
    r = _CMAP.get(c)
    if r is not None:
        return r
    if _MAP_SPACES and (c.isspace() or c in _BLANKS or unicodedata.category(c) in ("Zs", "Zl", "Zp")):
        r = " "                                            # every Unicode space, line / paragraph separator, NEL
    elif c in _DASHES:
        r = "-"
    elif c in _HANGUL_FILLERS:
        r = ""                                             # Hangul fillers (letters by category, invisible in use)
    elif unicodedata.category(c) in _DROP_CATS:
        # Cf format (zero-width, bidi, BOM, soft hyphen, TAG characters U+E0000-E007F), Cc control, Mn / Me marks
        # (combining grapheme joiner U+034F, VARIATION SELECTORS U+FE00-FE0F and U+E0100-E01EF), Co private use,
        # Cn unassigned, Cs surrogates
        r = ""
    else:
        r = c
    if len(_CMAP) < _CMAP_MAX:
        _CMAP[c] = r
    return r


_SPACES = re.compile(r" {2,}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _nfkc(t: str) -> str:
    return unicodedata.normalize("NFKC", t)


class _CharTable(dict):
    """``str.translate`` table filled on first use of each code point (bounded): one C-speed pass per text."""

    def __missing__(self, o: int) -> str:
        r = _map_char(chr(o))
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_TABLE = _CharTable()


def _map_chars(t: str) -> str:
    return t.translate(_TABLE)


def _collapse(t: str) -> str:
    return _SPACES.sub(" ", t).strip()


def _clean(text: str) -> str:
    """Step 1 without decoding: NFKC, the per-character map (spaces, invisibles, joiners, controls), NFKC again (a
    removed joiner can let characters compose), runs of spaces collapsed."""
    t = _nfkc(text)
    if not t.isascii() or _CONTROL.search(t):
        t = _nfkc(_map_chars(t))
    return _collapse(t)


def normalise(text: str) -> str:
    """The inspection form of ``text``: NFKC, invisible / joiner / control code points removed, every Unicode space
    and line break read as one space (step 1 of ADR 0050 v2). Never emitted."""
    return _clean(text or "")


# review 601e328a MUST 1: only a REAL tag (``<`` then a letter, ``/`` or ``!``) is stripped, and never one holding an
# ``@`` or an "at" word (``<name@domain.com>``, ``<name at domain dot com>`` are addresses, not tags); markdown is
# removed first, so ``<**name**@domain\.com>`` reads as an address. The markdown-only view (no tag strip) is a view of
# its own as well.
_TAG = re.compile(r"<[A-Za-z/!][^<>]{0,255}>")
_TAG_KEEP = re.compile(r"(?i)@|(?<![a-z])at(?![a-z])")
_MD = re.compile(r"[*`~\\]")


def _strip_md(t: str) -> str:
    """Markdown emphasis / backticks / strike / escapes removed, tags kept (inspection only)."""
    return _MD.sub("", t) if ("*" in t or "`" in t or "~" in t or "\\" in t) else t


def _strip_tags(t: str) -> str:
    if "<" not in t:
        return t
    keep: dict = {}                                        # tag -> kept or removed (a repeated tag is read once)

    def repl(m: re.Match) -> str:
        g = m.group(0)
        r = keep.get(g)
        if r is None:
            r = g if _TAG_KEEP.search(g) else ""
            if len(keep) < TOKEN_MEMO_MAX:
                keep[g] = r
        return r
    return _TAG.sub(repl, t)


def _strip_markup(t: str) -> str:
    """Markdown removed, then the real tags that hold no address (inspection only)."""
    return _strip_tags(_strip_md(t))


_ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


def _unescape(t: str) -> str:
    return _ESC.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), t) if "\\" in t else t


_B64_RUN = re.compile(r"[A-Za-z0-9+/_\-]{12,}={0,2}")
_HEX_RUN = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")


DECODE_TRIM = 6                          # bytes trimmed at each end of a decoded run (review 08eea6e1 / c13529af MUST 4;
#                                          c0c09bd1 MUST 4: up to six bytes, so a glued junk suffix / prefix of 6-8
#                                          base64 characters is trimmed off; bounded: (TRIM+1)^2 windows per run)
B64_OFFSETS = 9                          # a base64 run is decoded from offsets 0..B64_OFFSETS-1 (and its boundaries)
DECODE_EDGE = 128                        # characters of each trimmed / shifted edge kept in a run's edge view
DECODE_EDGES_MAX = 16                    # edge pieces kept per run (c0c09bd1: room for every head and tail trim)
DECODE_EDGE_TOTAL = 16384                # edge characters kept per decoded view (bounded; past it, main readings only)
DECODE_EDGE_MIN = 6                      # an edge piece shorter than this is not read (no finding fits in it)
# between two edge pieces: a 12-letter word (no digit run, filler-word bridge, number word or label crosses it)
_EDGE_JOIN = " qzqzqzqzqzqz "


class _InvisibleTable(dict):
    """``str.translate`` table: a format character (zero-width, bidi, soft hyphen, TAG characters...) or a Hangul
    filler removed (review c13529af MUST 4d: before the readable-ratio test, so padding cannot hide a payload)."""

    def __missing__(self, o: int) -> str | None:
        c = chr(o)
        r = None if (unicodedata.category(c) == "Cf" or c in _HANGUL_FILLERS) else c
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


class _UnreadableTable(dict):
    """``str.translate`` table keeping only the characters that are NOT readable (not printable and not a tab / line
    break): ``len(s.translate(...))`` counts them in one C-speed pass."""

    def __missing__(self, o: int) -> str | None:
        c = chr(o)
        r = None if (c.isprintable() or c in "\n\t\r") else c
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_INVISIBLE = _InvisibleTable()
_UNREADABLE = _UnreadableTable()


def _visible(s: str) -> str:
    return s if s.isascii() else s.translate(_INVISIBLE)


def _unreadable(s: str) -> int:
    return 0 if s.isprintable() else len(s.translate(_UNREADABLE))


def _readable(s: str) -> str | None:
    if not s:
        return None
    return s if _unreadable(s) <= 0.15 * len(s) else None


def _utf8(b: bytes) -> str | None:
    try:
        return _visible(b.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _windows(b: bytes) -> list:
    """Every readable window of ``b``: (lead, tail, text) for ``lead`` / ``tail`` bytes trimmed at each end (0 to
    ``DECODE_TRIM``; review c13529af MUST 4c: always tried, not only on a decode error), decoded as UTF-8, format
    characters removed, then the readable-ratio test. Linear: ``b`` is split at two character starts inside the trim
    zones, the middle is decoded and counted ONCE and only the few edge bytes are decoded per window."""
    n, tr = len(b), max(0, int(DECODE_TRIM))
    out: list = []
    if n <= 4 * (tr + 4):
        for lead in range(tr + 1):
            for tail in range(tr + 1):
                s = _utf8(b[lead:n - tail]) if n - tail - lead > 0 else None
                if s and _readable(s) is not None:
                    out.append((lead, tail, s))
        return out
    p = next((i for i in range(tr, tr + 4) if not 0x80 <= b[i] <= 0xBF), None)
    q = next((i for i in range(n - tr, n - tr - 4, -1) if i == n or not 0x80 <= b[i] <= 0xBF), None)
    if p is None or q is None:
        return out                                         # four continuation bytes in a row: no window decodes
    mid = _utf8(b[p:q])
    if mid is None:
        return out                                         # undecodable inside the body: no trim reaches it
    bad_mid = _unreadable(mid)
    heads = [_utf8(b[lead:p]) for lead in range(tr + 1)]
    tails = [_utf8(b[q:n - tail]) for tail in range(tr + 1)]
    for lead, h in enumerate(heads):
        if h is None:
            continue
        for tail, t in enumerate(tails):
            if t is None:
                continue
            size = len(h) + len(mid) + len(t)
            if size and _unreadable(h) + bad_mid + _unreadable(t) <= 0.15 * size:
                out.append((lead, tail, h + mid + t))
    return out


def _printable(b: bytes) -> str | None:
    """``b`` as readable UTF-8 text (the first readable window: fewest bytes trimmed at the start, then at the end),
    or None."""
    w = _windows(b)
    return w[0][2] if w else None


def _readings(b: bytes) -> tuple:
    """(main reading or None, edge pieces): the first readable window in full, and the first / last ``DECODE_EDGE``
    characters of every other readable window whose start / end differs from it (a junk byte that decoded is read
    both glued and trimmed off)."""
    w = _windows(b)
    if not w:
        return None, []
    main = w[0][2]
    edges: list = []
    for _lead, _tail, s in w[1:]:
        if not s.startswith(main[:DECODE_EDGE]):
            edges.append(s[:DECODE_EDGE])
        if not s.endswith(main[-DECODE_EDGE:]):
            edges.append(s[-DECODE_EDGE:])
    return main, edges


def _b64_starts(run: str) -> list:
    """Where a base64 run is decoded from (review adacb2ca MUST 4, review 08eea6e1 / c13529af MUST 4): offsets 0 to
    ``B64_OFFSETS - 1`` (0-8: a prefix of up to eight characters glued on: ``x`` + payload, ``abcd`` + payload,
    ``abcdefgh`` + payload) and just after each of its first ``DECODE_BOUNDARIES_MAX`` ``-`` / ``_`` (``ref-`` /
    ``x_``). Every start is decoded (no offset is skipped)."""
    starts = list(range(B64_OFFSETS))
    for i, c in enumerate(run):
        if c in "-_":
            if len(starts) >= B64_OFFSETS + DECODE_BOUNDARIES_MAX:
                break
            starts.append(i + 1)
    return sorted({s for s in starts if len(run) - s >= 8})


def _b64_decode(run: str) -> bytes:
    body = run.rstrip("=").replace("-", "+").replace("_", "/")
    body = body[: len(body) - len(body) % 4] if len(body) % 4 == 1 else body
    return base64.b64decode(body + "=" * (-len(body) % 4), validate=True)


def _shifted(prev: list, s: str) -> bool:
    """``s`` is an earlier main reading of the same run with a few leading characters removed (offset ``k + 4`` decodes
    the bytes of offset ``k`` past its first three): only its head is new."""
    return len(s) > 2 * DECODE_EDGE and any(0 <= len(p) - len(s) <= 16 and p.endswith(s[DECODE_EDGE:]) for p in prev)


def _decoded_runs(t: str, clean=_clean) -> list:
    """Base64- and hex-looking runs of ``t`` decoded (when they decode to readable text), for inspection only.
    Bounded: at most ``DECODE_RUNS_MAX`` runs of at most ``DECODE_RUN_MAX`` characters each; a base64 run is decoded
    from each of its ``_b64_starts`` (a constant number per run). Each run gives its main readings and ONE edge view
    (``_EDGE_JOIN``-joined, at most ``DECODE_EDGES_MAX`` pieces of ``DECODE_EDGE`` characters; at most
    ``DECODE_EDGE_TOTAL`` edge characters per call): the trimmed edges of every readable window, and the head of a
    reading that only repeats an earlier one shifted by whole quanta."""
    out: list = []
    budget = [DECODE_EDGE_TOTAL]
    n = 0

    def emit(mains: list, edges: list) -> None:
        for s in mains:
            out.append(clean(s))
        kept: list = []
        for e in edges:
            c = clean(e)
            if len(c) < DECODE_EDGE_MIN:
                continue                                   # c0c09bd1: a few junk characters carry no value
            if c not in kept and len(kept) < DECODE_EDGES_MAX and budget[0] >= len(c):
                budget[0] -= len(c)
                kept.append(c)
        if kept:
            out.append(_EDGE_JOIN.join(kept))
    for rx in (_HEX_RUN, _B64_RUN):
        for m in rx.finditer(t):
            n += 1
            if n > DECODE_RUNS_MAX:
                return out
            run = m.group(0)[:DECODE_RUN_MAX]
            if rx is _HEX_RUN:
                try:
                    b = bytes.fromhex(run if len(run) % 2 == 0 else run[:-1])
                except ValueError:
                    continue
                s, edges = _readings(b)
                if s:
                    emit([s], edges)
                continue
            mains: list = []
            edges: list = []
            for k in _b64_starts(run):
                try:
                    b = _b64_decode(run[k:])
                except (ValueError, binascii.Error):
                    continue
                s, more = _readings(b)
                if not s:
                    continue
                if _shifted(mains, s):
                    edges.append(s[:DECODE_EDGE])
                else:
                    mains.append(s)
                edges.extend(more)
            emit(mains, edges)
    return out


def views(text: str) -> list:
    """Every inspection view of ``text`` (see ``_tagged_views``)."""
    return [v for v, _ in _tagged_views(text)]


def _tagged_views(text: str, clean=_clean, fold: bool = True) -> list:
    """(view, folded) pairs. Every inspection view of ``text`` (step 1): the normalised form, the markup-stripped
    form, each percent / HTML-entity / escape decoding round (re-normalised), the decoded base64 / hex runs of each,
    and (``folded`` True, unless ``fold`` is False) the ASCII-folded form of each non-ASCII view. Distinct, in order;
    bounded in number and size. ``clean``: the normalisation (``_clean_marked`` gives the MARKED views the script
    labels spelled with a non-spacing mark are read on, section 2a)."""
    base = clean(text or "")
    out = [base]
    seen = {base}

    def add(v: str) -> None:
        if v not in seen:
            seen.add(v)
            out.append(v)
    add(_strip_md(base))
    add(_strip_markup(base))
    cur = base
    for _ in range(PERCENT_ROUNDS):
        nxt = clean(_unescape(_entities(_percent(cur))))      # escapes read before markup strips the backslash
        if nxt == cur:
            break
        add(nxt)
        add(_strip_md(nxt))
        add(_strip_markup(nxt))
        cur = nxt
    for v in out.copy():
        for d in (_decoded_runs(v) if clean is _clean else _decoded_runs(v, clean)):
            add(d)
            cur = d
            for _ in range(PERCENT_ROUNDS):               # review 601e328a MUST 5: every round, as for the text
                nxt = clean(_entities(_percent(cur)))
                if nxt == cur:
                    break
                cur = nxt
            add(_strip_md(cur))
            add(_strip_markup(cur))
    tagged = [(v, False) for v in out]
    if not fold:
        return tagged
    for v in out.copy():
        if not v.isascii():
            f = _ascii_fold(v)                             # r2 part 2: rules (b)-(f) also read the ASCII-folded form
            if f not in seen:
                seen.add(f)
                tagged.append((f, True))
    return tagged


class _DigitTable(dict):
    """``str.translate`` table: every non-ASCII Unicode decimal digit (Devanagari, Arabic-Indic, Bengali, Thai...)
    read as its ASCII digit (filled on first use, bounded)."""

    def __missing__(self, o: int) -> str:
        c = chr(o)
        d = None if c.isascii() else unicodedata.decimal(c, None)
        r = c if d is None else str(d)
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


class _ConfusableTable(dict):
    """``str.translate`` table: a non-ASCII letter the ``lookalikes`` table reads as ASCII letters (Cyrillic, Greek,
    Cherokee, Latin with marks...) becomes those letters, case kept (an upper-case lookalike reads upper case)."""

    def __missing__(self, o: int) -> str:
        c = chr(o)
        r = c
        if not c.isascii():
            up = c.lower() != c
            m = _FOLD.mapping.get(c) or (_FOLD.mapping.get(c.lower()) if up else None)
            if m and m.isascii() and m.isalpha():
                # the skeleton reads capital I as "l": an upper-case lookalike of it is "I"
                r = ("I" if m == "l" else m.upper()) if up else m
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_DIGIT_FOLD = _DigitTable()
_CONFUSABLE_FOLD = _ConfusableTable()


def _fold_digits(v: str) -> str:
    return v.translate(_DIGIT_FOLD)


def _fold_confusables(v: str) -> str:
    return v.translate(_CONFUSABLE_FOLD)


def _ascii_fold(v: str) -> str:
    """The folded inspection view: Unicode digits as ASCII digits, confusable letters as ASCII letters."""
    return _fold_confusables(_fold_digits(v))


def _percent(t: str) -> str:
    return unquote(t) if "%" in t else t


def _entities(t: str) -> str:
    return html.unescape(t) if "&" in t else t


decodings = views                        # v1 name used by callers; same function

# ------------------------------------------------------------------------------------------------ 2a. label words
# The CLOSED multilingual credential-label list (ADR 0050 v2 section 2a). The rule of reviews ffd28ca8 and cd0a6c7d:
# THE KEY OF AN ENTRY IS THE ENTRY READ BY THE SAME FUNCTION THAT READS THE TEXT, never a stronger one. The matching
# tables (``_JOINED_KEYS``, ``_PIECE_KEYS``, ``_SCRIPT_KEYS``) are made at import by passing each entry through the
# view its lane applies to a text (``_label_keys``); an entry no text could match stops the import.
#   Latin lists   the text is read through ``_label_norm`` (lookalikes folded, lower case, accents dropped), so is
#                 the entry; the client view's own cased view of the Latin labels is checked against it at import for
#                 every natural spelling of every entry (``_cv_latin_keys``).
#   script list   the text is read through the WORD VIEW (``_script_word_view``: the inspection view, each letter
#                 case folded with an ACCENT dropped, the tatweel dropped, every other character kept), so is each
#                 word of the entry. The kana voicing marks stay on their letter (review cd0a6c7d MUST A). An entry
#                 whose inspection view would drop a NON-SPACING mark it is spelled with (the Hindi virama, the Thai
#                 vowel and tone marks) is a MARKED entry: it is read, entry and text alike, on the marked views
#                 (``_marked_views``: the same views with the non-spacing marks of those scripts kept), never on a
#                 view without them (review cbdf5e78 MUST 2: with the Thai marks dropped, "quilt" + "code" read as
#                 the label; with the Hindi nasal dot dropped, "response" + "word" glued did).
#                 Review cbdf5e78 MUST 1, the BOUNDARY RULE (ADR 0051 decision 73): in a script that writes words
#                 apart (all but Han, kana, Thai, Lao, Khmer, Myanmar) a label is a SEQUENCE OF WHOLE WORDS: it
#                 starts at a word start and ends at a word end (a boundary is the text's start / end or any
#                 character that is not a letter or mark of the word's own script: a space, a separator, a digit,
#                 another script), each word of the label is one whole word of the text, and two words of a label
#                 stand apart by one space, "_" or "-" (or by nothing, for the labels declared ``glued``), never by
#                 another word. A declared STEM is followed in its own word by one of its declared endings only; a
#                 declared PROCLITIC letter may be written onto the first word. One pattern (``_SCRIPT_SEQ``) holds
#                 this rule and both lanes use it. Before, the letters-only view (spaces dropped) let a key be
#                 assembled from the letters of several words ("code" + "to" + "base-" read as the Greek label).
#                 The letters-only view is kept as a necessary PREFILTER only (every match of the rule leaves the
#                 key's letters in a row there).
# Before review ffd28ca8 an entry was compared as written with a text that had been folded (the Hindi and Thai labels
# never matched; the client view read the upper-case stroked L as an I); between ffd28ca8 and cd0a6c7d the entries
# were folded MORE than the text views fold (every mark dropped, the kana decomposed).
#   _JOINED  long labels found anywhere in the text with every non-letter removed, so "pass word", "pass_word",
#            "p.a.s.s.w.o.r.d", "api key", "DB_PASSWORD", "userPassword", "mot de passe" all read as the label;
#   _PIECES  short labels that must be a whole piece (pieces split at non-letters, camelCase and letter/digit
#            boundaries): "pass" in "DB_PASS", "auth" in "authToken", "pin", "sid", "token", "secret", "cookie";
#   _SCRIPT  labels in other scripts, written as their language writes them, words apart (``_SCRIPT_FORMS``: stems,
#            proclitics, glued words). Han, kana and Thai labels (scripts without word spacing) are matched as
#            substrings of the letters-only view; every other one by the boundary rule (above).
_JOINED = (
    "password", "passwd", "passcode", "passphrase", "apikey", "accesskey", "accesstoken", "authtoken",
    "secretkey", "privatekey", "clientsecret", "sessionid", "accesscode", "securitycode", "sessiontoken", "seedphrase",
    "recoveryphrase", "mnemonic", "credential", "connectionstring", "accountkey", "refreshtoken", "bearertoken",
    "sharedaccesssignature", "contrasena", "motdepasse", "passwort", "kennwort", "senha", "wachtwoord", "losenord",
    "adgangskode", "salasana", "haslo", "katasandi", "matkhau", "parolasi", "heslo", "jelszo", "lozinka",
    # review c0c09bd1 MUST 9: multi-word labels written with an apostrophe / hyphen (Italian "parola d'ordine",
    # Portuguese "palavra-passe", Catalan "contrasenya", French "phrase de passe"): read on the letters-only view, so
    # the letter-spaced, XML-tag and JSON-key forms read as the label too
    "paroladordine", "palavrapasse", "contrasenya", "phrasedepasse",
)
_PIECES = frozenset((
    "pass", "pwd", "pw", "pin", "secret", "secrets", "token", "tokens", "auth", "authorization", "authorisation",
    "bearer", "cookie", "cookies", "sid", "passwords", "credentials", "apikeys", "sig", "sifre", "parola", "clave",
    "chiave", "schlussel", "cle", "otp", "totp",
))
_SCRIPT = (
    "\u043f\u0430\u0440\u043e\u043b",                      # Russian / Ukrainian "parol'"
    "\u043a\u043b\u044e\u0447 api",
    "\u5bc6\u7801", "\u5bc6\u78bc", "\u53e3\u4ee4", "\u5bc6\u94a5", "\u79d8\u94a5", "\u91d1\u9470", "\u4ee4\u724c",
    "\u30d1\u30b9\u30ef\u30fc\u30c9", "\u6697\u8a3c\u756a\u53f7", "\u30c8\u30fc\u30af\u30f3",
    "\ube44\ubc00\ubc88\ud638", "\ube44\ubc88", "\uc554\ud638", "\ud1a0\ud070",
    "\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631", "\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0631",
    "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631",
    "\u092a\u093e\u0938\u0935\u0930\u094d\u0921", "\u0e23\u0e2b\u0e31\u0e2a\u0e1c\u0e48\u0e32\u0e19",
    # Greek "code" + the stem of "access" (its endings: ``_SCRIPT_FORMS``)
    "\u05e1\u05d9\u05e1\u05de\u05d4", "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2\u03b1\u03c3",
)
# How the words of a script label may be written (review cbdf5e78, ADR 0051 decision 73), by entry:
#   endings   the LAST word is a stem, followed in its own word by one of these endings ("" = none) and nothing
#             else (``None``, any letters of its script, is allowed by the builder but used by no entry). Read by
#             the word view, as the entry is. Without a declared list the Greek stem read "code accessible" ("code"
#             + a derivative of "access") as the label; Korean writes its particles onto the noun (a closed list).
#   prefix    (letters, n): up to n of these letters may be written onto the FIRST word (the Hebrew and Arabic
#             proclitics: "the", "and", "in", "to", "with"...)
#   glued     the words may also be written with nothing between them (the Persian non-joiner, which the inspection
#             view removes; a change of script). Arabic writes its two words apart: glued, "with the word" + "family" +
#             "secret" read as the label once the madda was folded (review cbdf5e78 S-2)
# Any other label word is one whole word.
_RU_ENDINGS = ("", "\u044c", "\u044f", "\u044e", "\u0435\u043c", "\u0435", "\u0438", "\u0435\u0439", "\u044f\u043c",
               "\u044f\u043c\u0438", "\u044f\u0445", "\u0456", "\u0456\u0432")             # the noun's cases (ru, uk)
# the Korean particles written onto a noun (a closed list: an open one read "encryption" as "cipher" + more)
_KO_PARTICLES = ("", "\uc774", "\uac00", "\uc740", "\ub294", "\uc744", "\ub97c", "\uc758", "\uc5d0", "\uc5d0\uc11c", "\ub85c",
                 "\uc73c\ub85c", "\uc640", "\uacfc", "\ub3c4", "\ub9cc", "\uae4c\uc9c0", "\ubd80\ud130", "\uc774\ub098", "\ub098",
                 "\ub791", "\uc774\ub791", "\ud558\uace0", "\uc5d0\uac8c", "\uc785\ub2c8\ub2e4", "\uc774\ub2e4", "\uc608\uc694",
                 "\uc774\uc5d0\uc694", "\ub4e4", "\ub4e4\uc744", "\ub4e4\uc774", "\ub4e4\uc740", "\ub4e4\uc758")
_SCRIPT_FORMS = {
    _SCRIPT[0]: {"endings": _RU_ENDINGS},
    _SCRIPT[1]: {"glued": True},
    "\ube44\ubc00\ubc88\ud638": {"endings": _KO_PARTICLES}, "\ube44\ubc88": {"endings": _KO_PARTICLES},
    "\uc554\ud638": {"endings": _KO_PARTICLES}, "\ud1a0\ud070": {"endings": _KO_PARTICLES},
    "\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631": {"prefix": ("\u0648\u0641\u0628\u0644\u0643", 2)},
    "\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0631": {"prefix": ("\u0648\u0641\u0628\u0644\u0643", 2)},
    "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631": {"glued": True},
    "\u05e1\u05d9\u05e1\u05de\u05d4": {"prefix": ("\u05d5\u05d4\u05d1\u05dc\u05de\u05e9\u05db", 3)},
    _SCRIPT[19]: {"endings": ("", "\u094b\u0902", "\u094d\u0938")},           # Hindi: oblique plural, "-s"
    _SCRIPT[-1]: {"endings": ("", "\u03b7", "\u03b7\u03c2", "\u03b5\u03c9\u03c2", "\u03b5\u03b9\u03c2", "\u03b5\u03c9\u03bd")},
}
# The scripts a label word may be in, by the first word of its letters' Unicode names, and their blocks: a word of a
# label ends where the letters and marks of its script end (``_word_class``).
_WORD_SCRIPTS = {
    "LATIN": ((0x41, 0x5a), (0x61, 0x7a), (0xc0, 0x24f), (0x1e00, 0x1eff)),
    "CYRILLIC": ((0x400, 0x52f), (0x1c80, 0x1c8f), (0x2de0, 0x2dff), (0xa640, 0xa69f)),
    "GREEK": ((0x370, 0x3ff), (0x1f00, 0x1fff)),
    "ARABIC": ((0x600, 0x6ff), (0x750, 0x77f), (0x870, 0x8ff), (0xfb50, 0xfdff), (0xfe70, 0xfeff)),
    "HEBREW": ((0x590, 0x5ff), (0xfb1d, 0xfb4f)),
    "DEVANAGARI": ((0x900, 0x97f), (0xa8e0, 0xa8ff)),
    "HANGUL": ((0x1100, 0x11ff), (0x3130, 0x318f), (0xa960, 0xa97f), (0xac00, 0xd7af), (0xd7b0, 0xd7ff)),
}
# scripts written without spaces between words: a label in one is a substring of the letters-only view
_UNSPACED_SCRIPTS = frozenset(("CJK", "HIRAGANA", "KATAKANA", "THAI", "LAO", "KHMER", "MYANMAR"))
# the blocks whose NON-SPACING marks the marked views keep (Devanagari, Thai, Lao, Myanmar, Khmer): in them a mark
# spells the word
_MARKED_BLOCKS = ((0x900, 0x97f), (0xa8e0, 0xa8ff), (0xe00, 0xe7f), (0xe80, 0xeff), (0x1000, 0x109f), (0x1780, 0x17ff))


_TATWEEL = "\u0640"                       # the Arabic elongation stroke: a letter by category, it spells nothing
# Canonical combining classes from this value up are ACCENTS (above, below, attached, the iota subscript: the Greek
# tonos and dialytika, the Cyrillic breve, the Arabic hamza above). The classes below it are fixed-position marks
# that are part of the letter (the nukta 7, the KANA VOICING MARKS 8, the virama 9, the Thai and Arabic vowel marks):
# a letter is never read without those.
ACCENT_CLASS_MIN = 200


class _LabelMarkTable(dict):
    """``str.translate`` table of the LATIN normal form: every combining mark (general category Mn, Mc, Me) and the
    Arabic tatweel removed (filled on first use, bounded). The Latin keys are ASCII letters and the Latin lanes read
    the letters of the form only, so a mark never stands between two letters of a Latin label."""

    def __missing__(self, o: int) -> str | None:
        c = chr(o)
        r = None if (unicodedata.category(c)[0] == "M" or c == _TATWEEL) else c
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_LABEL_MARKS = _LabelMarkTable()


class _RefoldTable(dict):
    """``str.translate`` table: the lookalike fold (``fold.fold_cased``) of ONE character, for the second pass of
    the Latin normal form. That pass reads a text that is already folded, NFKC and without marks, on which the
    fold works one character at a time, so one C-speed pass gives what a second ``fold_cased`` of the whole text
    gave at cd0a6c7d (measured there: the strict lane on Greek text cost a quarter more than at ffd28ca8, the whole
    fold being run twice on every text with an accent). Filled on first use, bounded; made for one fold object."""

    def __init__(self, fold):
        super().__init__()
        self.fold = fold

    def __missing__(self, o: int) -> str:
        r = self.fold.fold_cased(chr(o))
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_REFOLD: list = [None]


def _refold(g: str) -> str:
    """The lookalike fold of ``g`` (a text already folded once, its marks dropped), one character at a time."""
    t = _REFOLD[0]
    if t is None or t.fold is not _FOLD:
        t = _REFOLD[0] = _RefoldTable(_FOLD)               # (a test may put another fold in place)
    return g.translate(t)


def _script_fold(c: str) -> str:
    """ONE character of an inspection view as the script labels read it (lower case): case folded (the final sigma
    is a sigma), an ACCENT dropped from a precomposed letter (``ACCENT_CLASS_MIN``: the Greek tonos, which capitals
    are written without), every other precomposed letter left whole (a kana letter keeps its voicing mark, a
    Devanagari letter its nukta, a Hangul syllable its jamo), the tatweel and a combining mark standing alone read
    as nothing (a mark is not a letter; the inspection views have already removed the non-spacing ones, and an
    entry SPELLED with a spacing one is a word label, read on the view itself)."""
    if c == _TATWEEL or unicodedata.category(c)[0] == "M":
        return ""
    f = c.casefold()
    if f.isascii():
        return f
    d = "".join(x for x in unicodedata.normalize("NFD", f) if unicodedata.combining(x) < ACCENT_CLASS_MIN)
    return unicodedata.normalize("NFC", d)


class _ScriptFoldTable(dict):
    """``str.translate`` table of ``_script_fold`` (filled on first use, bounded)."""

    def __missing__(self, o: int) -> str:
        r = _script_fold(chr(o))
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_SCRIPT_FOLD = _ScriptFoldTable()


def _label_norm(s: str, latin: bool = True) -> str:
    """The normal form a credential label AND the text it is looked for in are both read in (lower case).
      ``latin=True``   (``_JOINED``, ``_PIECES``) invisible characters out and NFKC, case folded (``I`` with a dot
                       above, the final sigma and the sharp s included), lookalike letters read as the ASCII letters
                       they resemble (``lookalikes``), NFKD with the combining marks dropped, NFC, and the lookalikes
                       once more (a lookalike that carried a mark: a Greek letter with a tonos). The letters NFKD
                       does not decompose are folded by ``lookalikes``: the stroked l, o, d and the dotless i; the
                       sharp s reads ``ss`` by case folding. The ae / oe ligatures are left alone: no entry, and no
                       natural spelling of one, holds them (``tests/test_review_ffd28ca8.py`` asserts each).
                       The second lookalike pass reads only a word that MIXES scripts (``p\u03acssword``): in a
                       word written wholly in ONE script other than Latin a marked letter is a letter of that word
                       (review cbdf5e78 measurement: with every accented Greek letter read as Latin, ordinary Greek
                       words side by side spelled a Latin label across them, as "na rik egine" spelled "apikey";
                       ffd28ca8 read them as Greek), ``_one_script_spans``.
      ``latin=False``  (``_SCRIPT``) the inspection view of ``s`` (``normalise``), each character through
                       ``_script_fold``. The script is kept: no lookalike folding (a Cyrillic or Greek label is not
                       read as Latin letters), and no mark is dropped that the inspection views keep ON A LETTER."""
    if latin:
        if s.isascii():
            return s.lower()
        f = _FOLD.fold_cased(s)
        if not f.isascii():
            g = unicodedata.normalize("NFC", unicodedata.normalize("NFKD", f).translate(_LABEL_MARKS))
            if g != f and not g.isascii():
                # a lookalike that carried a mark (a Greek tonos): the second pass, but not on a word of one script
                one = globals().get("_ONE_SCRIPT_TEXT")
                if one is None or one.fullmatch(s) is None or _ONE_SCRIPT_ACROSS.search(s) is not None:
                    spans = _one_script_spans(s)
                    if not spans:
                        g = globals()["_refold"](g)
                    elif _mixed_outside(s, spans):
                        if globals()["_refold"](g) != g:
                            g = _refold_mixed(s, spans)
                # (else every word of one script: the first pass is the form, and the second is not run)
            f = g
        return f
    v = normalise(s)
    return v.lower() if v.isascii() else v.translate(_SCRIPT_FOLD)


class _ScriptTagTable(dict):
    """``str.translate`` table: each character as ONE tag character: a letter as the first letter of its script's
    name ("L" Latin, "G" Greek, "C" Cyrillic... a tag per script name, made on first use), a mark as "m", anything
    else as a space (filled on first use, bounded)."""
    names: dict = {}

    def __missing__(self, o: int) -> str:
        c = chr(o)
        if c.isalpha():
            n = _first_name(c)
            r = self.names.get(n)
            if r is None:
                r = "L" if n == "LATIN" else chr(0x4e00 + len(self.names))
                self.names[n] = r
        else:
            r = "m" if unicodedata.category(c)[0] == "M" else " "
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_SCRIPT_TAG = _ScriptTagTable()


_TAG_RUN = re.compile(r"([^ Lm])(?:\1|m)*")
# the runs ``_one_script_spans`` keeps, in one pass (review cbdf5e78 M6: the loop cost a Greek text 7%): a run of
# two letters or more of one script (with their marks), or a run of one letter that is a whole word
_ONE_SCRIPT_RUN = re.compile(r"([^ Lm])m*\1(?:\1|m)*|(?:(?<= )|^)[^ Lm]m*(?= |$)")


def _one_script_spans(s: str) -> list:
    """The (start, end) of every run of letters of ONE script other than Latin (its marks included) in ``s`` that is
    a whole word or two letters or more: the second lookalike pass leaves those letters alone. What it reads is a
    lone letter of another script standing in a word with letters of other scripts (``p\u03acssword``)."""
    return [m.span() for m in _ONE_SCRIPT_RUN.finditer(s.translate(_SCRIPT_TAG))]


def _one_script_spans_loop(s: str) -> list:
    """``_one_script_spans`` written as a loop over every run (``_TAG_RUN``): the definition the pattern
    ``_ONE_SCRIPT_RUN`` is tested against (``tests/test_review_cbdf5e78.py``)."""
    tags = s.translate(_SCRIPT_TAG)
    out: list = []
    n = len(tags)
    for m in _TAG_RUN.finditer(tags):
        a, b = m.span()
        if len(m.group(0).replace("m", "")) >= 2 or ((a == 0 or tags[a - 1] == " ") and (b == n or tags[b] == " ")):
            out.append((a, b))
    return out


# the ASCII characters Unicode calls case-ignorable: the lower-casing rule of the final sigma looks across them
_CASE_IGNORABLE_ASCII = frozenset("'.:^`")
# punctuation of other scripts that no fold step changes and composes with nothing, neither a letter nor
# case-ignorable (the no-break space reads as a space): outside the one-script words it changes nothing either
# (the danda, the Arabic comma, question mark and full stop, the Hebrew maqaf, guillemets, curly double quotes, the
# en and em dash, the ideographic comma and full stop)
_PLAIN_PUNCT = ("\u00a0\u00ab\u00bb\u0589\u05be\u05c3\u060c\u061b\u061f\u06d4\u0964\u0965\u2013\u2014\u201c\u201d"
                "\u3001\u3002")
_PLAIN_OUT = {ord(c): None for c in _PLAIN_PUNCT}


def _edge_cased(s: str, i: int, step: int, stop: int) -> bool:
    """Whether, from ``s[i]`` towards ``stop`` (``step`` 1 or -1), the first character that is not case-ignorable
    is a letter or lies past ``stop`` (in the next one-script word): the context of the final-sigma rule reaches
    across that edge of a word."""
    while i != stop:
        c = s[i]
        if c not in _CASE_IGNORABLE_ASCII:
            return c.isalpha()
        i += step
    return stop not in (-1, len(s))


def _mixed_outside(s: str, spans: list) -> bool:
    """Whether ``_refold_mixed`` must run: a part of ``s`` outside the one-script ``spans`` holds a character that is
    not ASCII nor plain punctuation (``_PLAIN_PUNCT``; a lone letter of another script, a mark on a Latin letter:
    only there can the second lookalike pass change anything), or a one-script word touches a letter (``user`` + a Greek word), alone or across
    case-ignorable characters (where folding the parts one by one and folding the whole differ: the final sigma).
    Otherwise ``_refold_mixed`` gives the first pass of the whole text, part for part (the fold reads one character
    at a time, a part ends where a run of letters and their marks ends, and no casing context crosses a part), so
    it is not run: the cost of an ordinary text of another script stays that of ffd28ca8 (review cbdf5e78 M6)."""
    at = 0
    n = len(s)
    for k, (a, b) in enumerate(spans):
        if not s[at:a].translate(_PLAIN_OUT).isascii() or (a > 0 and _edge_cased(s, a - 1, -1, at - 1)):
            return True
        nxt = spans[k + 1][0] if k + 1 < len(spans) else n
        if b < n and _edge_cased(s, b, 1, nxt):
            return True
        at = b
    return not s[at:].translate(_PLAIN_OUT).isascii()


def _refold_mixed(s: str, spans: list) -> str:
    """The Latin normal form of ``s`` with the second lookalike pass on every part but the one-script words
    (``spans``): each part through the same steps, the parts in order."""
    out: list = []
    at = 0
    for a, b in spans + [(len(s), len(s))]:
        for part, again in ((s[at:a], True), (s[a:b], False)):
            if not part:
                continue
            f = _FOLD.fold_cased(part)
            if not f.isascii():
                g = unicodedata.normalize("NFC", unicodedata.normalize("NFKD", f).translate(_LABEL_MARKS))
                f = globals()["_refold"](g) if (again and g != f and not g.isascii()) else g
            out.append(f)
        at = b
    return "".join(out)


def _word_fold(c: str) -> str:
    """ONE character of a view as the WORD VIEW reads it (lower case): a letter through ``_script_fold``, an ASCII
    character in lower case, the tatweel as nothing, a combining mark kept as it is (a view holds a spacing mark,
    and a marked view the non-spacing marks of ``_MARKED_BLOCKS``: they spell the word), anything else as it is."""
    if c == _TATWEEL:
        return ""
    if unicodedata.category(c)[0] == "M":
        return c
    return c.lower() if c.isascii() else _script_fold(c)


class _ScriptWordTable(dict):
    """``str.translate`` table of ``_word_fold`` (filled on first use, bounded): one C-speed pass gives the word view
    of a text, and ``_cv_script_ends`` maps a position in it back to the view as written."""

    def __missing__(self, o: int) -> str:
        r = _word_fold(chr(o))
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_SCRIPT_WORD = _ScriptWordTable()


def _script_word_view(v: str) -> str:
    """The word view of the view ``v``: what the script labels are matched on by the boundary rule (both lanes)."""
    return v.translate(_SCRIPT_WORD)


def _in_blocks(o: int, blocks) -> bool:
    return any(a <= o <= b for a, b in blocks)


class _MarkedTable(dict):
    """``str.translate`` table of the MARKED normalisation: the per-character map of ``_clean`` (``_map_char``), but a
    non-spacing mark of ``_MARKED_BLOCKS`` is kept (filled on first use, bounded). Any other mark, a variation
    selector or a joiner is removed as in every view."""

    def __missing__(self, o: int) -> str:
        c = chr(o)
        r = c if (unicodedata.category(c) == "Mn" and _in_blocks(o, _MARKED_BLOCKS)) else _map_char(c)
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_MARKED = _MarkedTable()


def _clean_marked(text: str) -> str:
    """``_clean`` keeping the non-spacing marks of ``_MARKED_BLOCKS``: NFKC, the per-character map, NFKC again, runs
    of spaces collapsed. Dropping those marks from it gives ``_clean`` of the same text."""
    t = _nfkc(text)
    if not t.isascii() or _CONTROL.search(t):
        t = _nfkc(t.translate(_MARKED))
    return _collapse(t)


def _marked_views(text: str) -> list:
    """The MARKED inspection views of ``text``: every view of ``_tagged_views`` (markup stripped, each decoding
    round, the decoded runs) made with ``_clean_marked``. Read for the marked script labels only."""
    return [v for v, _f in _tagged_views(text, _clean_marked, False)]


def _label_keys(name: str, entries, latin: bool, piece: bool = False) -> tuple:
    """The matching keys of one label list, in list order: every entry read by the function that reads the text.
      Latin list    the letters of ``_label_norm(entry)``;
      script list   the letters of the entry's inspection view (``_script_letters(normalise(entry))``): the
                    PREFILTER key (every match of the boundary rule leaves it in a row in the letters-only view).
                    The words the rule matches are ``_script_words``.
    An entry no text could match stops the import (so a new entry cannot be added in an unmatchable form): a
    Latin-list key must be ASCII letters, a script-list key must hold a non-ASCII letter and be the letters of the
    script normal form, a piece label must be one run of letters, every key has two letters or more, and reading a
    key again must give the key (the view is a fixed point)."""
    out: list = []
    for i, e in enumerate(entries):
        if latin:
            norm = _label_norm(e)
            k = "".join(c for c in norm if c.isalpha())
            ok = len(k) >= 2 and k.isascii() and (not piece or k == norm) and _label_norm(k) == k
        else:
            k = _script_letters(normalise(e))
            ok = (len(k) >= 2 and not k.isascii() and _script_letters(normalise(k)) == k
                  and k == "".join(c for c in _label_norm(e, False) if c.isalpha()))
        if not ok:
            raise ValueError(f"credential label list {name}: entry {i} cannot be matched after normalisation")
        if k not in out:
            out.append(k)
    return tuple(out)


def _first_name(c: str) -> str:
    """The script of a letter by its Unicode name ("CYRILLIC", "CJK", "KATAKANA" for the prolonged sound mark too)."""
    return unicodedata.name(c, "").split(" ")[0].split("-")[0]


def _one_script_text_rx() -> tuple:
    """(whole, across): a text of ASCII characters, plain punctuation (``_PLAIN_PUNCT``) and the letters of ONE other
    script (Greek, Cyrillic, Hebrew, Arabic, Devanagari; each letter followed by marks of its script or by
    combining diacritics, or not) is matched WHOLE by ``whole``; ``across`` finds a word of such a script that
    touches a letter, directly or across case-ignorable characters only (``'.:^`` and the grave): an ASCII letter
    on either side, or another word of the script after it. A text matched whole with nothing found across: every
    word of the script is a one-script word (``_one_script_spans``), nothing outside them is other than ASCII or
    plain punctuation, and no casing context crosses them (``_mixed_outside``), so ``_label_norm`` answers it in
    one C pass (review cbdf5e78 M6: the cost of ordinary text of another script). Any other text takes the spans;
    the answer is the same (``tests/test_review_cbdf5e78.py``)."""
    gap = "".join(re.escape(chr(o)) for o in range(128)) + "".join(re.escape(c) for c in _PLAIN_PUNCT)
    ign = "".join(re.escape(c) for c in sorted(_CASE_IGNORABLE_ASCII))
    alts, every = [], []
    for name, ranges in (("GREEK", ((0x370, 0x400), (0x1F00, 0x2000))),
                         ("CYRILLIC", ((0x400, 0x530), (0x1C80, 0x1C90), (0xA640, 0xA6A0))),
                         ("HEBREW", ((0x590, 0x600),)), ("ARABIC", ((0x600, 0x700), (0x750, 0x780))),
                         ("DEVANAGARI", ((0x900, 0x980),))):
        cps = [o for a, b in ranges for o in range(a, b)]
        letters = lookalikes.char_class([o for o in cps if chr(o).isalpha() and _first_name(chr(o)) == name])
        marks = lookalikes.char_class([o for o in cps if unicodedata.category(chr(o))[0] == "M"]) + "\\u0300-\\u036f"
        alts.append(f"(?:[{gap}]|[{letters}][{letters}{marks}]*+)*+")             # (possessive: linear)
        every.append((letters, marks))
    letter = "".join(x for x, _m in every)
    mark = "".join(m for _x, m in every)
    word_end = f"[{letter}][{mark}]*+"
    return re.compile("|".join(alts)), re.compile(f"{word_end}(?:[{ign}]++[{letter}]|[{ign}]*+[A-Za-z])"
                                                  f"|[A-Za-z][{ign}]*+[{letter}]")


_ONE_SCRIPT_TEXT, _ONE_SCRIPT_ACROSS = _one_script_text_rx()


def _marked_entry(e: str) -> bool:
    """The inspection view of the entry drops a non-spacing mark it is spelled with: it is read on the marked views."""
    return _clean_marked(e) != normalise(e)


def _script_words(name: str, entries) -> tuple:
    """How each script entry is matched, in list order: (marked, unspaced, words, scripts, endings, prefix, glued).
    ``words``: each word of the entry through the (marked) normalisation and the word view; ``scripts``: the script
    of each word (``_WORD_SCRIPTS``; an unspaced entry: its one script). Stops the import when a word is not a fixed
    point of the view, holds a character of another script or a separator, when an entry has no non-ASCII word, or
    when its declared forms do not fit it."""
    out: list = []
    for i, e in enumerate(entries):
        form = _SCRIPT_FORMS.get(e, {})
        marked = _marked_entry(e)
        clean = _clean_marked if marked else normalise
        words = tuple(_script_word_view(clean(w)) for w in e.split(" "))
        scripts = []
        ok = len(words) >= 1 and all(words) and any(not w.isascii() for w in words)
        for w in words:
            names = {_first_name(c) for c in w if c.isalpha()}
            sc = names.pop() if len(names) == 1 else ""
            ok = ok and sc != "" and _script_word_view(clean(w)) == w and all(
                c.isalpha() or unicodedata.category(c)[0] == "M" for c in w)
            ok = ok and (sc in _WORD_SCRIPTS or sc in _UNSPACED_SCRIPTS) and all(
                _first_name(c) == sc or unicodedata.category(c)[0] == "M" for c in w if c.isalpha())
            scripts.append(sc)
        unspaced = bool(scripts) and scripts[0] in _UNSPACED_SCRIPTS
        endings = form.get("endings", ("",))
        if endings is not None:
            endings = tuple(_script_word_view(clean(x)) for x in endings)
        prefix = form.get("prefix")
        if prefix is not None:
            prefix = (_script_word_view(clean(prefix[0])), prefix[1])
        ok = ok and (not unspaced or (len(words) == 1 and not form))
        ok = ok and (endings is None or all(c.isalpha() or unicodedata.category(c)[0] == "M"
                                            for x in endings for c in x))
        if not ok:
            raise ValueError(f"credential label list {name}: entry {i} cannot be matched after normalisation")
        out.append((marked, unspaced, words, tuple(scripts), endings, prefix, bool(form.get("glued"))))
    return tuple(out)


class _ScriptLettersTable(dict):
    """``str.translate`` table: each character of an inspection view becomes the letters of its script fold
    (``_script_fold``; nothing for a non-letter), so one C-speed pass gives the letters-only script view of a text
    (``tests/test_review_ffd28ca8.py`` asserts it is the letters of ``_label_norm(v, latin=False)``). Filled on first
    use, bounded."""

    def __missing__(self, o: int) -> str:
        r = "".join(x for x in _script_fold(chr(o)) if x.isalpha())
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_SCRIPT_LETTERS = _ScriptLettersTable()


def _script_letters(v: str) -> str:
    """The letters-only script view of the inspection view ``v``: what the letter keys of ``_SCRIPT`` are looked
    for in, and what makes them (``_label_keys``)."""
    return v.translate(_SCRIPT_LETTERS)


def _alternation(keys, between: str = "") -> re.Pattern | None:
    """The keys as one pattern (longest first), ``between`` allowed between two characters of a key; None when
    there is no key."""
    ks = sorted(keys, key=len, reverse=True)
    return re.compile("|".join(between.join(re.escape(c) for c in k) for k in ks)) if ks else None


_JOINED_KEYS = _label_keys("_JOINED", _JOINED, True)
_PIECE_KEYS = frozenset(_label_keys("_PIECES", sorted(_PIECES), True, piece=True))
_SCRIPT_KEYS = _label_keys("_SCRIPT", _SCRIPT, False)
_JOINED_RX = re.compile("|".join(sorted(_JOINED_KEYS, key=len, reverse=True)))
_WORD_CLASS: dict = {}


def _word_class(script: str) -> str:
    """The pattern class of the letters and marks of ``script`` (``_WORD_SCRIPTS``, made once): what a word of a
    label may not touch on either side. A space, a separator, a digit or a letter of another script ends it."""
    r = _WORD_CLASS.get(script)
    if r is None:
        parts: list = []
        for a, b in _WORD_SCRIPTS[script]:
            run = None
            for o in range(a, b + 2):
                inside = o <= b and unicodedata.category(chr(o))[0] in "LM"
                if inside and run is None:
                    run = o
                elif not inside and run is not None:
                    parts.append(f"\\u{run:04x}" + ("" if run == o - 1 else f"-\\u{o - 1:04x}"))
                    run = None
        r = _WORD_CLASS[script] = "[" + "".join(parts) + "]"
    return r


# a label spelled ONE UNIT AT A TIME, the same separator between every two units (review cbdf5e78 S-3): once, or
# twice where the view dropped a mark between them; a mark may also stay on its letter
_SPACERS = (" ", "_", "-", ".")


def _spelled(units: str, s: str) -> str:
    e = re.escape(s)
    return re.escape(units[0]) + "".join(
        (e + ("{0,2}" if unicodedata.category(u)[0] == "M" else "{1,2}")) + re.escape(u) for u in units[1:])


def _spelled_whole(units: str, s: str) -> str:
    """``units`` spelled one at a time with the separator ``s``, as a whole word: no letter right before or after
    it, nor a letter joined to it by ``s`` (``a-b`` is read; ``xa-b``, ``x-a-b``, ``a-by`` and ``a-b-y`` are not)."""
    e = re.escape(s)
    return f"(?<![^\\W\\d_])(?<![^\\W\\d_]{e}){_spelled(units, s)}(?![^\\W\\d_])(?!{e}[^\\W\\d_])"


def _ending_alt(endings, last: str, s: str | None) -> str:
    """What may follow a label's last word in its own word: an open stem, any letters of its script; a stem, one of
    its endings; any other word, nothing. ``s``: spelled with that separator."""
    if endings is None:
        return f"{last}*" if s is None else f"(?:{re.escape(s)}{{1,2}}{last})*"
    if s is None:
        alts = [re.escape(x) for x in endings]
    else:
        alts = ["".join(re.escape(s) + ("{0,2}" if unicodedata.category(u)[0] == "M" else "{1,2}") + re.escape(u)
                        for u in x) for x in endings]
    alts = sorted(set(alts), key=len, reverse=True)
    return "" if alts == [""] else "(?:" + "|".join(alts) + ")"


def _seq_alternatives(spell) -> list:
    """The pattern of one script entry over the word view: THE BOUNDARY RULE (section 2a) for a script that writes
    words apart, and the entry spelled one unit at a time with one separator. An unspaced entry: its letters in a
    row, a space or "_" allowed between two (as the client view always read them; the strict lane reads them on the
    letters-only view); a MARKED unspaced one (Thai) only as written, its marks on their letters, or spelled one
    unit at a time: in a text without word spaces nothing else tells it from the end of one word and the start of
    the next (review cbdf5e78 MUST 2)."""
    marked, unspaced, words, scripts, endings, prefix, glued = spell
    if unspaced:
        (key,) = words
        if not marked:
            # (and the whole entry spelled one letter at a time with "-" or ".": review of ced056c5)
            return ["[ _]?".join(re.escape(c) for c in key)] + [_spelled_whole(key, s) for s in "-."]
        return [re.escape(key)] + [_spelled(key, s) for s in _SPACERS]
    first, last = _word_class(scripts[0]), _word_class(scripts[-1])
    pre = "" if prefix is None else f"[{re.escape(prefix[0])}]{{0,{prefix[1]}}}"
    gap = "[ _\\-]?" if glued else "[ _\\-]"
    out = [f"(?<!{first}){pre}{gap.join(re.escape(w) for w in words)}{_ending_alt(endings, last, None)}(?!{last})"]
    units = "".join(words)
    out += [f"(?<!{first}){_spelled(units, s)}{_ending_alt(endings, last, s)}(?!{last})" for s in _SPACERS
            if len(units) >= 3 or s in " _"]
    # a two-letter label with a hyphen or a dot between its letters: only the WHOLE entry so spelled, as a word of
    # its own (no letter of its script before or after it, nor another letter joined by the same separator), no
    # ending (review of ced056c5: brief item 4, the spaced-letter form with one uniform separator)
    if len(units) < 3:
        out += [f"(?<!{first}){_spelled_whole(units, s)}(?!{last})" for s in "-."]
    return out


def _seq_pattern(spells) -> re.Pattern | None:
    """One pattern of every entry of ``spells`` (the longest words first)."""
    alts = [a for sp in sorted(spells, key=lambda x: -len("".join(x[2]))) for a in _seq_alternatives(sp)]
    return re.compile("|".join(alts)) if alts else None


_SCRIPT_SPELL = _script_words("_SCRIPT", _SCRIPT)
_SPELL_KEYS = tuple(zip(_SCRIPT_SPELL, (_script_letters(normalise(e)) for e in _SCRIPT)))
# the PREFILTERS (letters-only view; necessary): the letter keys of the entries read on the views, and of the marked
# entries (their letters without marks: a match on a marked view leaves them in a row in the view without marks)
_SCRIPT_RX = _alternation(k for sp, k in _SPELL_KEYS if not sp[0])
_MARKED_RX = _alternation(k for sp, k in _SPELL_KEYS if sp[0])
# the RULES: the strict lane and the client view both match ``_SCRIPT_SEQ`` (``_MARKED_SEQ`` on the marked views)
# on the word view; the strict lane also finds an unspaced (Han, kana) label anywhere in the letters-only view, as
# it always did
_SCRIPT_SEQ = _seq_pattern(sp for sp, _k in _SPELL_KEYS if not sp[0])
_MARKED_SEQ = _seq_pattern(sp for sp, _k in _SPELL_KEYS if sp[0])
_SCRIPT_UNSPACED_RX = _alternation(k for sp, k in _SPELL_KEYS if sp[1] and not sp[0])


class _LowerTable(dict):
    """``str.translate`` table: each character in lower case when that is ONE character, else as it is (filled on
    first use, bounded): the view as written, case aside."""

    def __missing__(self, o: int) -> str:
        c = chr(o)
        lo = c.lower()
        r = lo if len(lo) == 1 else c
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_LOWER = _LowerTable()


def _split_alternatives(entry: str, spell) -> list:
    """A label WORD written with ONE space or "_" put inside it (the no-break space or line break of a disguised
    label, which the views read as a space): read only letter for letter AS WRITTEN, case aside (the entry and its
    declared proclitics as listed, nothing folded, no ending: the entries and the full forms of ``_CV_SCRIPT_FULL``
    are the words). That is how both lanes of ffd28ca8 read every script label (its letters in a row, a space or "_"
    between two allowed; the client view needed the label to end before its separator), so it reads nothing
    ffd28ca8 did not. Not for an entry the word view reads otherwise than as written (the Greek one: its final
    sigma) nor for the marked ones."""
    marked, unspaced, words, scripts, _endings, _prefix, glued = spell
    raw = tuple(normalise(w).lower() for w in entry.split(" "))
    if marked or unspaced or raw != words:
        return []
    form = _SCRIPT_FORMS.get(entry, {})
    endings = ("",)
    prefix = form.get("prefix")
    first, last = _word_class(scripts[0]), _word_class(scripts[-1])
    pre = "" if prefix is None else f"[{re.escape(prefix[0])}]{{0,{prefix[1]}}}"
    gap = "[ _\\-]?" if glued else "[ _\\-]"
    tails = sorted(set(endings), key=len, reverse=True)
    ending_rx = "" if tails == [""] else "(?:" + "|".join(re.escape(t) for t in tails) + ")"
    out = []
    n = len(words)
    for i in range(n):
        forms = [words[i] + t for t in tails] if i == n - 1 else [words[i]]
        for f in forms:
            for k in range(1, len(f)):
                parts = [re.escape(x) for x in words]
                parts[i] = re.escape(f[:k]) + "[ _]" + re.escape(f[k:])
                body = gap.join(parts) + ("" if i == n - 1 else ending_rx)
                out.append(f"(?<!{first}){pre}{body}(?!{last})")
    return out


_SCRIPT_SPLIT = re.compile("|".join(a for e, (sp, _k) in zip(_SCRIPT, _SPELL_KEYS) for a in _split_alternatives(e, sp)))
_SPLIT_FULL: list = []                   # the full forms of section 5, added to ``_SCRIPT_SPLIT`` there
_PIECE_RX = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_A-Za-z]+")
_SIG_RX = re.compile(r"(?i)(?<![a-z])sig\s*[=:]")
_LETTER_RUN = re.compile(r"[^\W\d_]+")
_NO_PAIR_JOIN = frozenset(("sid", "sig", "cle", "pw"))  # "customer's ID", "Cle Elum": too common split
# "key" is an ordinary word ("the key takeaway"), so it is a label only when a value follows it: an explicit ":" / "="
# and a value, or a token-shaped value (letters and digits, 8+ characters) as the next word
_KEY_VALUE = re.compile(r"(?i)(?<![a-z])(?:keys?|cl[e\u00e9]s?|schl[u\u00fc]ssel)\s?(?:[:=]\s?[^\s]{6}"
                        r"|\s(?=[A-Za-z0-9+/=_.\-]{0,64}\d)(?=[0-9+/=_.\-]{0,64}[A-Za-z])[A-Za-z0-9+/=_.\-]{8})")


def _letters_view(v: str) -> str:
    """The letters of the Latin label normal form of ``v`` (``_label_norm``: lookalikes folded, lower case, marks
    dropped; Hangul syllables are kept whole)."""
    return _alpha(_label_norm(v))


def _script_hit(v: str) -> bool:
    """A ``_SCRIPT`` label (not a marked one: ``_marked_label``) in the inspection view ``v``. The letters-only view
    must hold a key (prefilter); then an unspaced label anywhere in it, or the boundary rule on the word view
    (``_SCRIPT_SEQ``, the pattern the client view reads too). Every entry holds a non-ASCII letter, so an ASCII text
    holds none."""
    if v.isascii():
        return False
    g = globals()
    letters = _script_letters(v)
    if g["_SCRIPT_RX"].search(letters) is None:
        return False
    unspaced = g["_SCRIPT_UNSPACED_RX"]
    if unspaced is not None and unspaced.search(letters) is not None:
        return True
    if g["_SCRIPT_SEQ"].search(_script_word_view(v)) is not None:
        return True
    return g["_SCRIPT_SPLIT"].search(v.translate(_LOWER)) is not None


# a character of the marked blocks: only such a character, in an inspection view, is a letter of a marked entry on
# the letters-only view (no other character folds to one; ``tests/test_review_cbdf5e78.py`` checks every code
# point), so a view without one is no candidate, decided in one C pass (review cbdf5e78 M6)
_MARKED_BLOCK_RX = re.compile("[" + "".join(f"\\u{a:04x}-\\u{b:04x}" for a, b in _MARKED_BLOCKS) + "]")


def _marked_candidate(v: str) -> bool:
    """The view ``v`` (marks dropped) holds the letters of a marked entry in a row: the marked views must be read."""
    rx = globals()["_MARKED_RX"]
    return (rx is not None and not v.isascii() and _MARKED_BLOCK_RX.search(v) is not None
            and rx.search(_script_letters(v)) is not None)


def _marked_hit(text: str) -> bool:
    """A marked ``_SCRIPT`` label (spelled with a non-spacing mark: the Hindi and Thai entries) on a marked view of
    ``text``, its marks as written (``_MARKED_SEQ`` on the word view, the pattern the client view reads too)."""
    seq = globals()["_MARKED_SEQ"]
    return seq is not None and any(not mv.isascii() and seq.search(_script_word_view(mv)) is not None
                                   for mv in _marked_views(text))


def _marked_label(text: str, v: str, memo: list) -> bool:
    """The strict lane's marked step for one view ``v`` of ``text``: the marked views are read (once per text, the
    answer kept in ``memo``) only when a view holds a marked entry's letters."""
    if memo[0] is None:
        if not globals()["_marked_candidate"](v):
            return False
        memo[0] = globals()["_marked_hit"](text)
    return memo[0]


class _AlphaTable(dict):
    """``str.translate`` table: every character that is not ``isalpha()`` removed (filled on first use, bounded)."""

    def __missing__(self, o: int) -> str | None:
        c = chr(o)
        r = c if c.isalpha() else None
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_ALPHA = _AlphaTable()


def _alpha(s: str) -> str:
    """Only the letters of ``s`` (``isalpha``), in one C-speed pass."""
    return s.translate(_ALPHA)


_FOLD_CACHE: dict = {}


def _fold_piece(p: str) -> str:
    if p.isascii():
        return p.lower()                                   # the confusable fold of ASCII letters is lower case
    f = _FOLD_CACHE.get(p)
    if f is not None:
        return f
    f = _fold_piece_slow(p)
    if len(_FOLD_CACHE) < _CMAP_MAX and len(p) <= 32:
        _FOLD_CACHE[p] = f
    return f


def _fold_piece_slow(p: str) -> str:
    return _label_norm(p)


PAIR_WINDOW = 64                         # characters searched on each side of a split short label


def _pair_has_separator(v: str, w: str, nxt: str, at: int = 0, end: int | None = None) -> bool:
    """``s id:`` / ``p w =``: the split short label is followed by an explicit ``:`` or ``=``. Only the window of
    ``PAIR_WINDOW`` characters around the pair (``v[at:end]``) is searched, so a text of many short pairs stays linear;
    a match inside the window is found exactly as a search of the whole text would find it."""
    end = len(v) if end is None else end
    win = v[max(0, at - PAIR_WINDOW):end + PAIR_WINDOW]
    return re.search(re.escape(w) + r"\W{1,3}" + re.escape(nxt) + r"\s{0,3}[:=]", win) is not None


def _single(t: str) -> bool:
    """A token of one character (a trailing ``:`` / ``=`` allowed), or of ``:`` / ``=`` only."""
    return len(t.rstrip(":=")) == 1 or (t != "" and not t.strip(":="))


def _singles_view(v: str) -> str:
    """``v`` with each run of single-character tokens (``_single``) joined; every other token keeps its spaces."""
    out: list = []
    run: list = []
    for t in v.split(" "):
        if _single(t):
            run.append(t)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(t)
    if run:
        out.append("".join(run))
    return " ".join(out)


def _spaced_label(v: str) -> bool:
    """Review 601e328a: a label spelled one character at a time with its own separator (``D B _ P A S S:``): the
    single characters (``_single``) joined, then read as pieces at the separator."""
    if "_" not in v and "-" not in v:
        return False
    chars = "".join(t if _single(t) else " " for t in v.split(" "))
    return any(_fold_words(x) in _PIECE_KEYS for x in _LETTER_RUN.findall(chars) if len(x) >= 3)


def _fold_words(*words) -> str:
    """Words of a text read one after the other, each through ``_fold_piece`` ON ITS OWN (review cbdf5e78: a split
    or letter-spaced label is read from words that are words of the text; folded as one string, a lone letter of
    another script among them was read through the second lookalike pass as if it stood inside a Latin word)."""
    return "".join(_fold_piece(w) for w in (words[0] if len(words) == 1 else words))


# every short / piece label is ASCII: when every LETTER of a view is ASCII (its pieces fold to lower case), a piece, a
# whole word, a split pair, a letter-spaced label all leave the label's letters contiguous among the view's letters, so
# when no label is there the per-word reads are skipped (review c13529af MUST 6: the same answer, linear in C)
_PIECES_IN = re.compile("|".join(sorted(_PIECE_KEYS, key=len, reverse=True)))


def _label_hit(v: str) -> bool:
    if _SIG_RX.search(v) or _KEY_VALUE.search(v):
        return True
    raw = _alpha(v)
    if raw.isascii() and not _PIECES_IN.search(raw.lower()):
        # every letter of the view is ASCII: no script label is in it (each key holds a non-ASCII letter)
        return _JOINED_RX.search(_letters_view(v)) is not None
    n = len(v)
    for m in _PIECE_RX.finditer(v):
        p = m.group(0)
        if len(p) > 16:
            continue
        f = _fold_piece(p)
        if f not in _PIECE_KEYS:
            continue
        if len(f) >= 5:
            return True                                    # secret, token, bearer, cookie...: any piece boundary
        # short labels (pass, pwd, pw, pin, sid, sig, otp, auth, cle) only as a whole word (delimited by a non
        # letter / digit, ``_`` included): a random id often holds "Pw" or "Sid" at a camel-case boundary
        a, b = m.span()
        if (a == 0 or not v[a - 1].isalnum()) and (b == n or not v[b].isalnum()):
            return True
    runs = list(_LETTER_RUN.finditer(v))
    words = [r.group(0) for r in runs]
    for i, w in enumerate(words):
        f = _fold_piece(w) if len(w) <= 16 else ""
        if f in _PIECE_KEYS and (len(f) >= 5 or w.isascii() is False):
            return True                                    # a whole word in any script (``\u015fifre``)
        if i + 1 < len(words):
            # a short label split by one space / NBSP / separator (``to ken``, ``P IN``): two adjacent short words
            nxt = words[i + 1]
            if len(w) + len(nxt) <= 16 and (min(len(w), len(nxt)) <= 3 or len(w) + len(nxt) >= 8):
                g = _fold_words(w, nxt)
                if g in _PIECE_KEYS and (g not in _NO_PAIR_JOIN or _pair_has_separator(v, w, nxt, runs[i].start(),
                                                                                    runs[i + 1].end())):
                    return True
    # a label spelled one letter at a time (``t o k e n``)
    singles = "".join(w if len(w) == 1 else " " for w in words)
    if any(_fold_words(x) in _PIECE_KEYS for x in singles.split() if len(x) >= 3):
        return True
    if globals()["_spaced_label"](v):
        return True
    if _JOINED_RX.search(_letters_view(v)):
        return True
    return _script_hit(v)


# Review c0c09bd1 MUST 2 / SHOULD: a URL path segment that is a credential LABEL WORD ALONE ("password", "api-key",
# "reset-password", "mot-de-passe"): after decoding, its letter pieces are only label words and the closed list of
# action words below, with no digit. Such a segment carries no value itself (it may be kept); the segment AFTER it
# carries the label's value ("/token/<v>", "/account/password/<v>") and is withheld unless it is an action word.
LABEL_ACTION_WORDS = frozenset((
    "reset", "forgot", "forgotten", "change", "changed", "update", "new", "lost", "recover", "recovery", "my", "your",
    "create", "manage", "setup", "set", "generate", "rotate", "revoke", "view", "show", "edit", "confirm", "verify",
    "settings", "help", "request", "send", "sent", "success", "done", "expired", "invalid", "required", "policy",
    "strength", "rules", "requirements", "index", "html", "htm", "php", "aspx", "the", "a", "an", "of", "and", "or",
))
_LABEL_WORDS = frozenset(_JOINED_KEYS) | _PIECE_KEYS | frozenset(_label_keys(
    "_LABEL_WORDS", ("apikey", "apikeys", "accesstokens", "authtokens"), True))
_LABEL_PIECE = re.compile(r"[a-z]+")


def label_only(seg, min_label: int = 1) -> bool:
    """``seg`` (a path segment, percent-decoded up to ``PERCENT_ROUNDS`` times) is a credential label word alone (its
    label part at least ``min_label`` letters)."""
    if not isinstance(seg, str) or not seg or len(seg) > 200:
        return False
    t = seg
    for _ in range(PERCENT_ROUNDS):
        u = unquote(t)
        if u == t:
            break
        t = u
    t = _label_norm(t)
    if _ANY_DIGIT.search(t):
        return False
    pieces = _LABEL_PIECE.findall(t)
    if not pieces or len(pieces) > 6 or "".join(pieces) != "".join(c for c in t if c.isalpha()):
        return False
    rest = "".join(p for p in pieces if p not in LABEL_ACTION_WORDS)
    return len(rest) >= max(1, min_label) and rest in globals()["_LABEL_WORDS"]


AFTER_LABEL_MIN = 5                      # a short label ("pin", "sid", "auth", "pass") is too often a route word


def after_label(segs: list, i: int) -> bool:
    """Segment ``i`` of a path follows a label-word-alone segment and is not itself an action word or a label word
    alone: it carries the label's value and is withheld (the strict lane) / masked (the client view)."""
    if i < 1 or not segs[i] or not label_only(segs[i - 1], AFTER_LABEL_MIN):
        return False
    low = unquote(segs[i]).lower()
    return low not in LABEL_ACTION_WORDS and not label_only(segs[i])


# ------------------------------------------------------------------------------------------------ 2b. URL userinfo

_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}:/{1,3}[^\s/?#@]{1,256}@|(?<![:/])//[^\s/?#@:]{1,128}:[^\s/?#@]{0,256}@")


def _userinfo_hit(v: str) -> bool:
    if "@" not in v:
        return False
    return bool(_USERINFO.search(v) or _USERINFO.search(v.replace(" ", "")))


# ------------------------------------------------------------------------------------------------ 2c. strong prefixes
# (provider, documented key shape), matched GLUED (no left boundary) on every view, on the view with every space
# removed, and on each separator-joined token. Each entry is a documented or partner-scanning format.
PROVIDERS = (
    ("aws_access_key_id", r"(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}"),
    ("openai_anthropic", r"sk-(?:proj-|svcacct-|admin-|ant-(?:api|admin|oat|sid)\d\d-)[A-Za-z0-9_\-]{12,}"),
    ("sk_generic", r"sk-[A-Za-z0-9_\-]{20,}"),
    ("github", r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("stripe", r"(?:(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}|whsec_[A-Za-z0-9]{16,})"),
    ("slack", (r"(?:xoxe\.xox[a-z]-[A-Za-z0-9\-]{10,}|xoxe-\d+-[A-Za-z0-9\-]{10,}|xox[a-z]-[A-Za-z0-9\-]{10,}"
               r"|hooks\.slack\.com/services/T[A-Za-z0-9_/]{8,})")),
    ("google", (r"(?:AIza[0-9A-Za-z_\-]{30,}|ya29\.[0-9A-Za-z_\-]{20,}|GOCSPX-[0-9A-Za-z_\-]{20,}"
                r"|1//0[0-9A-Za-z_\-]{30,})")),
    ("perplexity", r"pplx-[A-Za-z0-9]{16,}"),
    ("gitlab", r"(?:glpat|gldt|glrt|glptt|glsoat)-[A-Za-z0-9_\-]{20,}"),
    ("npm_pypi", r"(?:npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_\-]{40,})"),
    ("huggingface", r"hf_[A-Za-z0-9]{30,}"),
    ("sendgrid", r"SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    ("jwt", r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.?[A-Za-z0-9_\-]*"),
    ("shopify", r"shp(?:at|ss|ca|pa|tka)_[A-Fa-f0-9]{24,}"),
    ("amazon_lwa", r"amzn1\.(?:oa2-cs|application-oa2-client|sellerapps)\.[A-Za-z0-9.\-]{8,}"),
    ("azure", r"(?:AccountKey=[A-Za-z0-9+/=]{20,}|SharedAccessSignature|SharedAccessKey=|(?<![a-z])sv=20\d\d-\d\d-\d\d&)"),
    ("square", r"(?:sq0atp|sq0csp|sq0idp|EAAA)[-_][A-Za-z0-9_\-]{16,}"),
    ("vault", r"hv[sbr]\.[A-Za-z0-9_\-]{20,}"),
    ("discord", r"[MNO][A-Za-z0-9_\-]{23,27}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}"),
    ("mapbox", r"(?:sk|pk|tk)\.eyJ[A-Za-z0-9_\-]{10,}"),
    ("twilio", r"(?:SK|AC)[0-9a-f]{32}"),
    ("mailgun", r"key-[0-9a-zA-Z]{32}"),
    ("digitalocean", r"do[opr]_v1_[a-f0-9]{32,}"),
    ("misc_saas", (r"(?:lin_api_|secret_[A-Za-z0-9]{40}|ntn_|xai-|gsk_|r8_|dapi[a-f0-9]{32}|pat[A-Za-z0-9]{14}\."
                   r"|glc_|sntrys_|figd_|shpat_|rnd_|tskey-|pscale_tkn_|dp\.pt\.|fo1_|phc_|phx_|do[opr]_v1_)[A-Za-z0-9_\-]{8,}")),      # review 2b26d980 NIT: ``dop_v1_`` with any body (both lanes)
    ("db_uri", (r"(?i:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?|mssql|sqlserver|jdbc:[a-z]+)"
                r"://[^\s/@]{1,256}@")),
    ("pem_pgp", r"-----BEGIN [A-Z0-9 ]{0,40}(?:PRIVATE KEY|PGP|OPENSSH|ENCRYPTED|CERTIFICATE REQUEST)[A-Z0-9 ]{0,20}-----"),
)
_PREFIX_RX = re.compile("|".join(f"(?P<{n}>{rx})" for n, rx in PROVIDERS))
# families distinctive enough to match on the view with every space removed (a key split by spaces / line breaks);
# the short or word-like ones (bare sk-, JWT, Discord, Mailgun, Twilio, misc) are matched on the spaced views only
NOSPACE_FAMILIES = ("aws_access_key_id", "openai_anthropic", "github", "stripe", "slack", "google", "perplexity",
                    "gitlab", "npm_pypi", "huggingface", "shopify", "amazon_lwa", "azure", "square", "vault",
                    "mapbox", "digitalocean", "db_uri", "pem_pgp")
_PREFIX_NOSPACE_RX = re.compile("|".join(f"(?P<{n}>{rx})" for n, rx in PROVIDERS if n in NOSPACE_FAMILIES))
# one compiled pattern per family: each has a literal prefix the regex engine scans for quickly, while the combined
# alternation tries every family at every position (about 6x slower on a 64 KB field). These are only asked whether
# a family matches at all, so a family can be given a linear-time form that matches a text exactly when it does:
#   jwt  the first part runs to the end of its letters / digits run and needs a "." there, so a later "eyJ" in the same
#        run can only match when the first one does; starting at a run's start and committing (atomic group) to its
#        first "eyJ" stops every "eyJ" of a long run rescanning the run (quadratic: 1 s at 64 KB of "eyJeyJ...")
_LINEAR_FORMS = {"jwt": r"(?<![A-Za-z0-9_\-])(?>[A-Za-z0-9_\-]*?eyJ)[A-Za-z0-9_\-]{8,}+\.[A-Za-z0-9_\-]{8,}"}
_PREFIX_EACH = tuple((n, re.compile(_LINEAR_FORMS.get(n, rx))) for n, rx in PROVIDERS)
_PREFIX_EACH_NOSPACE = tuple((n, rx) for n, rx in _PREFIX_EACH if n in NOSPACE_FAMILIES)
_LONG = 256
# review c13529af MUST 6: a literal every match of the family holds (checked with ``in``, C speed, before the family's
# own scan; the case-insensitive ``db_uri`` alternation alone was half the cost of a 64 KB scan)
_PREFIX_MIN = min(_sre_parse.parse(rx).getwidth()[0] for _n, rx in PROVIDERS)   # 9 (a ``db_uri`` scheme)
_PREFIX_NEEDS = {"db_uri": ("://",), "jwt": ("eyJ",), "mapbox": ("eyJ",),
                 "azure": ("AccountKey=", "SharedAccessSignature", "SharedAccessKey=", "sv=20")}


def _prefix_found(s: str, nospace: bool = False) -> bool:
    """A strong-key prefix family matches in ``s`` (the bare ``sk-`` family only with a digit and a capital)."""
    if len(s) < _PREFIX_MIN:
        return False                                       # shorter than the shortest match of any family
    # one scan per family (each has a literal the engine searches for quickly; review c13529af MUST 6: faster than
    # the combined alternation at every length, 13 us against 34 us on a 52-character text)
    for n, rx in (_PREFIX_EACH_NOSPACE if nospace else _PREFIX_EACH):
        need = _PREFIX_NEEDS.get(n)
        if need is not None and not any(x in s for x in need):
            continue                                       # a literal every match holds is absent: no match
        for m in rx.finditer(s):
            if n != "sk_generic" or _sk_ok(m):
                return True
    return False
# the same AWS id family with separators and spaces removed (an id split unevenly by spaces / line breaks)
_AWS_JOINED = re.compile(r"(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}")
_NOSEP = re.compile(r"[\s.\-_/:]+")


def _sk_ok(m: re.Match) -> bool:
    # a bare ``sk-<body>`` must look like a key (a digit and an upper-case letter), so ``sk-ii-facial-essence`` is not
    body = m.group(0)
    return any(c.isdigit() for c in body) and any(c.isupper() for c in body)


def _prefix_hit(v: str) -> bool:
    if _prefix_found(v):
        return True
    if " " in v and _prefix_found(v.replace(" ", ""), nospace=True):
        return True
    return bool(_AWS_JOINED.search(_NOSEP.sub("", v)))


# ------------------------------------------------------------------------------------------------ safe shapes
# The SMALL disclosed allowlist (ADR 0050 v2 section 2d): occurrences are masked out before checks (d) and (e) only;
# checks (a), (b), (c) and (f) still run on the unmasked text.
_MASK = " \ue000\ue000\ue000\ue000 "   # private use: dropped from every view, so never typed
_B = r"(?<![A-Za-z0-9])"
_E = r"(?![A-Za-z0-9])"
_SAFE_SHAPES = re.compile(
    r"(?:(?P<geo>@[+-]?\d{1,2}\.\d{1,12},[+-]?\d{1,3}\.\d{1,12})|" + _B + r"(?:"   # geo: the "@" is its own boundary
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"|(?P<order>\d{3}-\d{7}-\d{7})"
    r"|(?P<ymd>(?:19|20)\d\d/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))"
    r"|(?P<iso>(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,9})?)?(?:Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)?)?)"
    r"|(?P<isbn13>(?:ISBN(?:-13)?:? ?)?97[89](?:[- ]?\d){10})"
    r"|(?P<isbn10>ISBN(?:-10)?:? ?\d(?:[- ]?\d){8}[- ]?[\dX])"
    r"|(?P<ean>\d{13})"
    r"|(?i:commit|commits|revision|sha)[ :#]{1,3}(?P<sha>[0-9a-f]{7,40})"
    r"))" + _E)


_DIGIT_CHARS = re.compile(r"[0-9]")
_ANY_DIGIT = re.compile(r"\d")


def _digits(s: str) -> list:
    return [int(c) for c in s if c.isdigit()]


def _ean_ok(s: str) -> bool:
    d = _digits(s)
    return len(d) == 13 and (sum(x * (3 if i % 2 else 1) for i, x in enumerate(d[:12])) + d[12]) % 10 == 0


def _isbn10_ok(s: str) -> bool:
    body = [c for c in s.upper().replace("ISBN-10", "").replace("ISBN", "") if c.isdigit() or c == "X"]
    if len(body) != 10 or "X" in body[:9]:
        return False
    vals = [10 if c == "X" else int(c) for c in body]
    return sum((10 - i) * x for i, x in enumerate(vals)) % 11 == 0


def _shape_ok(m: re.Match) -> bool:
    k = m.lastgroup
    if k == "isbn13":
        return _ean_ok(m.group(k))
    if k == "isbn10":
        return _isbn10_ok(m.group(k))
    if k == "ean":
        # review adacb2ca MUST 3: a 13-digit number with a card network's prefix that passes Luhn (a Visa) is never
        # the EAN safe shape, in either lane
        return _ean_ok(m.group(k)) and not _cv_card(m.group(k))
    if k == "geo":
        # a map's "@<lat>,<lng>" (signed decimals with a dot; review adacb2ca SHOULD 4), unless its digits read as a card
        lat, lng = m.group(k)[1:].split(",")
        return abs(float(lat)) <= 90 and abs(float(lng)) <= 180 and not _cv_card("".join(_DIGIT_CHARS.findall(m.group(k))))
    return True


# review c13529af MUST 3: a digit safe shape (EAN, ISBN, order number, date) is never applied to a run written as a
# phone number: right after "+" (``_B`` lets it through), or with a phone word ("tel:", "phone", "call", "text",
# "whatsapp", "mobile", "cell", "fax", "sms"...) within ``PHONE_CONTEXT`` characters before it. Both lanes.
PHONE_CONTEXT = 16
_DIGIT_SHAPES = frozenset(("order", "ymd", "iso", "isbn13", "isbn10", "ean"))
_PHONE_BEFORE = re.compile(r"(?i)\+[\s(]{0,3}$|(?<![a-z])(?:tel|tele|telephone|phone|phones|ph|call|calls|text|texts|txt"
                           r"|sms|whatsapp|wa|mobile|mob|cell|cellphone|fax|telefono|t\u00e9l\u00e9phone|handy)(?![a-z])")


def _phone_context(v: str, a: int) -> bool:
    return _PHONE_BEFORE.search(v, max(0, a - globals()["PHONE_CONTEXT"]), a) is not None


def _masked(v: str) -> str:
    """``v`` with every allowlisted safe shape replaced by a mark that no digit run or token can cross."""
    if not _ANY_DIGIT.search(v):
        return v                                           # every safe shape holds a decimal digit
    out, at = [], 0
    for m in _SAFE_SHAPES.finditer(v):
        if not _shape_ok(m):
            continue
        if m.lastgroup in _DIGIT_SHAPES and _phone_context(v, m.start()):
            continue                                       # a phone number, never a safe shape
        a, b = m.span(m.lastgroup) if m.lastgroup == "sha" else m.span()
        out.append(v[at:a])
        out.append(_MASK)
        at = b
    if not out:
        return v
    out.append(v[at:])
    return "".join(out)


# ------------------------------------------------------------------------------------------------ 2d. random tokens

_RUN = re.compile(r"[A-Za-z0-9+=]+")
_HEX32 = re.compile(r"[0-9a-fA-F]{32,}")
_TOK_SEP = re.compile(r"[.\-_/:]+")
_TOK_FILLER = re.compile(r"[^\w+=.\-/:]+")      # not a letter / digit / separator
_ALNUM = re.compile(r"[^\W_]")
_EDGE = "\"'()[]{}<>,;!?\u00a7\ue000"


_ASCII_UP, _ASCII_LO, _ASCII_DG = re.compile(r"[A-Z]"), re.compile(r"[a-z]"), re.compile(r"[0-9]")


def _classes(r: str) -> tuple:
    if r.isascii():                                        # the same answer in one C-speed scan per class
        return (_ASCII_UP.search(r) is not None, _ASCII_LO.search(r) is not None, _ASCII_DG.search(r) is not None)
    return (any(c.isupper() for c in r), any(c.islower() for c in r), any(c.isdigit() for c in r))


_LD_CHANGE = re.compile(r"(?<=[0-9])(?=[A-Za-z])|(?<=[A-Za-z])(?=[0-9])")
_CS_CHANGE = re.compile(r"(?<=[A-Z])(?=[a-z])|(?<=[a-z])(?=[A-Z])")


def _transitions(r: str) -> tuple:
    """(letter<->digit changes, upper<->lower changes) along ``r`` (an ASCII run is counted by two C-speed scans: the
    same counts, review c13529af MUST 6)."""
    if r.isascii():
        return len(_LD_CHANGE.findall(r)), len(_CS_CHANGE.findall(r))
    ld = cs = 0
    prev = None
    for c in r:
        k = "d" if c.isdigit() else ("u" if c.isupper() else ("l" if c.islower() else "o"))
        if prev is not None and k != prev:
            if "d" in (k, prev) and "o" not in (k, prev):
                ld += 1
            elif {k, prev} == {"u", "l"}:
                cs += 1
        prev = k
    return ld, cs


MIXED_MIN = 16                           # a run this long mixing upper case, lower case and digits is a key


def _random_run(r: str, joined: bool = False) -> bool:
    """One alphanumeric run (separators already removed) that looks like a key (check d). ``joined``: the run was
    put together from space-separated pieces (the case-only rule does not apply: ordinary words would trip it)."""
    n = len(r)
    if n < MIXED_MIN:
        return False
    up, lo, dg = _classes(r)
    if up and lo and dg and (n >= 20 or not joined):
        return True                                        # mixing case and digits
    if n >= 32 and _HEX32.search(r) and dg:
        return True                                        # >= 32 hex characters
    ld, cs = _transitions(r)
    if n >= 32 and dg and (up or lo) and ld >= 6:
        return True                                        # >= 32 base64 characters, letters and digits interleaved
    return not joined and n >= 24 and up and lo and cs >= 0.35 * n   # long mixed-case run, no words in it


def _tokenish(t: str) -> bool:
    """A space-separated piece that could be a fragment of a split key: alphanumeric, and holding a digit next to a
    letter or an upper-case letter after the first character."""
    if not t or not _RUN.fullmatch(t):
        return False
    up, lo, dg = _classes(t)
    return (dg and (up or lo)) or (lo and any(c.isupper() for c in t[1:]))


# a plain word piece: CamelCase / lower-case words with at most four trailing digits, a short acronym with at most four
# digits, or a short number. ``(?:[A-Z]?[a-z])++`` accepts exactly what ``(?:[A-Z]?[a-z]+)+`` does (every capital
# followed by a lower-case letter) but each character has one reading, so a long run followed by a rejected character
# (a fifth digit, a capital, an accented letter) fails in linear time instead of backtracking exponentially.
_PLAIN_WORD = re.compile(r"(?:[A-Z]?[a-z])++\d{0,4}|[A-Z]{2,6}\d{0,4}|\d{1,4}")


# the distinctive prefix families with their own separators removed, matched at the START of a token whose
# separators were all removed
_JOINED_PREFIX = re.compile(
    r"(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}|GOCSPX[A-Za-z0-9]{20,}"
    r"|AIza[A-Za-z0-9]{30,}|gh[pousr][A-Za-z0-9]{30,}|githubpat[A-Za-z0-9]{30,}|gl(?:pat|dt|rt)[A-Za-z0-9]{18,}"
    r"|xox[abposre][A-Za-z0-9]{15,}|shp(?:at|ss|ca|pa)[a-fA-F0-9]{28,}|sq0(?:atp|csp|idp)[A-Za-z0-9]{16,}"
    r"|hv[sbr][A-Za-z0-9]{20,}|pplx[A-Za-z0-9]{20,}|npm[A-Za-z0-9]{30,}|hf[A-Za-z0-9]{30,}"
    r"|sk(?:proj|ant|live|test|svcacct|admin)[A-Za-z0-9]{16,}|[rp]k(?:live|test)[A-Za-z0-9]{10,}|whsec[A-Za-z0-9]{20,}"
    r"|ya29[A-Za-z0-9]{20,}|amzn1oa2cs[A-Za-z0-9]{8,}|SG[A-Za-z0-9]{40,}|eyJ[A-Za-z0-9]{30,}"
    r"|do[opr]v1[A-Za-z0-9]{16,}")


def _nonword(pieces) -> str:
    """The pieces that are not plain words or short numbers, joined (what is left of a slug once its words are set
    aside: a key split into chunks keeps most of its length here, a slug keeps little)."""
    return "".join(x for x in pieces if x and not _PLAIN_WORD.fullmatch(x))


def _split_key(t: str, j: str) -> bool:
    """A key split by separators (``. - _ / :``) inside one token: 32+ hex characters (with a hex letter) once the
    separators are gone, or the NON-WORD pieces joined read as a key on their own (``Amazon-Report-2026-Fees`` keeps
    nothing; ``GOCSP-X-ISc-Gebcy-8F5n3`` keeps ``XISc8F5n3``...)."""
    for r in _RUN.findall(j):
        h = _HEX32.search(r)
        if h and any(c.isdigit() for c in h.group(0)) and any(c.isalpha() for c in h.group(0)):
            return True
    if _JOINED_PREFIX.match(j):
        return True                                        # a prefix key with separators put in (``GOCSP-X-...``)
    pieces = [x for x in _TOK_SEP.split(t) if x and not _PLAIN_WORD.fullmatch(x)]
    nw = "".join(pieces)
    return len(pieces) >= 2 and len(nw) >= MIXED_MIN and any(_random_run(r, joined=True) for r in _RUN.findall(nw))


def _prefix_after_separator(t: str) -> bool:
    """A prefix key whose BODY is split by separators (``hf_DN5.RpVI2X.QWhX1s``): the separators after the first
    (and after the second) one are removed and the prefix table matched again."""
    seps = [m.end() for m in _TOK_SEP.finditer(t)][:2]
    for k in seps:
        if k <= 12:
            t2 = t[:k] + _TOK_SEP.sub("", t[k:])
            if _prefix_found(t2):
                return True
    return False


TOKEN_MEMO_MAX = 4096                    # distinct tokens remembered per call (repeated words are checked once)
_FILLER_ONLY = ("filler",)


def _token_hit(v: str) -> bool:
    toks = v.split(" ")
    seen: dict = {}                                        # token -> (per-token hit, separators removed), this call
    chain: list = []
    same: list = []

    def chain_bad(parts: list) -> bool:
        if len(parts) < 2:
            return False
        nw = _nonword(parts)
        return len(nw) >= MIXED_MIN and _random_run(nw, joined=True)

    def same_bad(parts: list) -> bool:
        if len(parts) < 4:
            return False
        j = "".join(parts)                                 # short parts are ``_RUN`` pieces: ASCII
        return _DIGIT_CHARS.search(j) is not None and (_random_run(j, joined=True) or bool(_AWS_JOINED.search(j)))

    for raw in toks:
        got = seen.get(raw)
        if got is None:                                    # the per-token checks are a function of the token alone
            if raw and "\ue000" not in raw and not _ALNUM.search(raw):
                got = _FILLER_ONLY                         # r2 part 2: a filler-only piece (``,,,,``) pads, never splits
            else:
                t = raw.strip(_EDGE)
                if t:
                    t = _TOK_FILLER.sub("-", t)            # any other filler run inside a token is a separator
                j = _TOK_SEP.sub("", t)
                got = (any(_random_run(r) for r in _RUN.findall(t))
                       or (j != t and (_split_key(t, j) or _prefix_after_separator(t)))
                       or (j != t and _prefix_found(j)), j,  # (the last: a prefix key split by separators)
                       _tokenish(j), bool(j) and len(j) <= 4 and bool(_RUN.fullmatch(j)))
            if len(seen) < TOKEN_MEMO_MAX:
                seen[raw] = got
        if got is _FILLER_ONLY:
            continue
        if got[0]:
            return True
        j = got[1]
        # fragments of a key split by spaces / line breaks: consecutive token-like pieces, and runs of 3+ pieces of
        # one length (``AKIA IOSF ODNN 7EXA MPLE``, ``A K I A ...``)
        if got[2]:
            chain.append(j)
        elif chain:
            if len(chain) >= 2 and chain_bad(chain):
                return True
            chain = []
        short = got[3]
        if short and (not same or len(same[0]) == len(j)):
            same.append(j)
        else:
            if same_bad(same):
                return True
            same = [j] if short else []
    return chain_bad(chain) or same_bad(same)


# ------------------------------------------------------------------------------------------------ 2e. digit runs

# r2 part 2: every character that is not a letter or a digit is filler (a padded group ``1234____5678,,,,9012`` reads
# as one run); the safe-shape mark (U+E000) stays a barrier. ASCII digits only: Unicode digits are read through the
# folded view (``_ascii_fold``).
_DIGIT_FILLER = re.compile(r"[^\w\ue000]+|_+")
DIGIT_LETTER_BRIDGE = 3                  # letters allowed between two digit groups of one run (after filler is gone)
_DIGIT_RUN = re.compile(r"[0-9](?:[^\W\d_]{0,%d}[0-9])*" % DIGIT_LETTER_BRIDGE)
_ASCII_DIGITS = "0123456789"


# review adacb2ca MUST 2: between two digit groups, up to WORD_BRIDGE_WORDS letter-only words of at most
# WORD_BRIDGE_LETTERS letters each (with a little punctuation / space around them) are read as filler too:
# ``1234 then 5678``, ``1234 followed by 5678``, ``1234then5678``. Review 601e328a MUST 2: a group is 2-6 digits
# (``WORD_BRIDGE_GROUP``), so an SSN written 3-2-4 and an Amex written 4-6-5 with filler words are bridged; the card /
# SSN / 9+ digit rules then decide. Linear: every part of the pattern is bounded and a match starts only at the first
# digit of a group.
#
# Review 08eea6e1 MUST 3: a group is 1-8 digits (a card written 8-8 / 7-8 with filler words, a 1-digit tail group); a
# 1-DIGIT group bridges only when its run already holds ``BRIDGE_ONE_DIGIT_RUN`` digits (so "5 and 6" in prose is not
# glued). The bridge is a linear scan over the digit groups (``_bridge_sub``): ``_bridge_rx`` is the GAP between two
# groups, matched with ``fullmatch`` on that gap only (bounded pattern).
WORD_BRIDGE_WORDS = 2
WORD_BRIDGE_LETTERS = 10
WORD_BRIDGE_GROUP = (1, 8)
BRIDGE_ONE_DIGIT_RUN = 8
_BRIDGE_FILL = r"(?:[^\w\ue000]|_){0,4}"
_DIGIT_GROUP = re.compile(r"[0-9]+")
_SEP_GAP = re.compile(r"(?:[^\w\ue000]|_)+")
_HAS_LETTER = re.compile(r"[^\W\d_]")


def _bridge_rx(word: str) -> re.Pattern:
    """The gap between two digit groups that is bridged: up to ``WORD_BRIDGE_WORDS`` words matching ``word`` (and a
    little punctuation / space around them). Used with ``fullmatch`` on the gap."""
    return re.compile(_BRIDGE_FILL + r"(?:" + word + r"(?:[^\w\ue000]|_){1,4}){0,%d}" % (WORD_BRIDGE_WORDS - 1)
                      + word + _BRIDGE_FILL)


_WORD_BRIDGE = _bridge_rx(r"[^\W\d_]{1,%d}" % WORD_BRIDGE_LETTERS)          # the strict lane: any letter-only word
# the client view's SSN and phone rules (no check digit) bridge only these filler words (review 601e328a MUST 2 / 5)
CV_FILLER_WORDS = ("then", "next", "plus", "dash", "hyphen", "space", "followed", "by", "and", "comma", "dot", "slash",
                   "minus", "after")
_CV_WORD_BRIDGE = _bridge_rx(r"(?i:" + "|".join(CV_FILLER_WORDS) + r")(?![^\W\d_])")


def _bridge_sub(rx: re.Pattern, v: str, sep: str) -> str:
    """``v`` with every gap ``rx`` bridges between two digit groups of ``WORD_BRIDGE_GROUP`` digits replaced by
    ``sep`` (a 1-digit group only once its run holds ``BRIDGE_ONE_DIGIT_RUN`` digits; a run continues through bridged
    gaps and through pure filler). One pass over the groups; each gap is read by a bounded ``fullmatch``."""
    if not _HAS_LETTER.search(v):
        return v
    lo, hi = globals()["WORD_BRIDGE_GROUP"]
    need = globals()["BRIDGE_ONE_DIGIT_RUN"]
    out: list = []
    last = 0
    prev_end, prev_n, run = -1, 0, 0
    split = [None]                                         # any split filler word in v at all (read once, lazily)

    def split_gap(a: int, b: int) -> bool:
        if split[0] is None:
            split[0] = " " in v and globals()["_SPLIT_FILLER"].search(v) is not None
        return split[0] and _split_gap(rx, v, a, b)
    for g in _DIGIT_GROUP.finditer(v):
        a, n = g.start(), g.end() - g.start()
        if prev_end < 0:
            run = n
        elif _SEP_GAP.fullmatch(v, prev_end, a):
            run += n
        elif (lo <= prev_n <= hi and lo <= n <= hi and (min(prev_n, n) > 1 or run >= need)
              and (rx.fullmatch(v, prev_end, a) or split_gap(prev_end, a))):
            out.append(v[last:prev_end])
            out.append(sep)
            last = a
            run += n
        else:
            run = n
        prev_end, prev_n = g.end(), n
    if not out:
        return v
    out.append(v[last:])
    return "".join(out)


BRIDGE_GAP_REJOIN = 64                   # longest gap read again with its split filler words re-joined


def _split_gap(rx: re.Pattern, v: str, a: int, b: int) -> bool:
    """The gap ``v[a:b]`` bridges once a filler word split by one line break is re-joined (``fol\nlowed by``, review
    c13529af MUST 1). Bounded: only gaps of at most ``BRIDGE_GAP_REJOIN`` characters."""
    if b - a > BRIDGE_GAP_REJOIN:
        return False
    g = v[a:b]
    j = _rejoin(globals()["_SPLIT_FILLER"], g)
    return j != g and rx.fullmatch(j) is not None


def _unbridged(v: str, sep: str) -> str:
    """``v`` with every filler-word bridge between two digit groups replaced by ``sep``."""
    return _bridge_sub(globals()["_WORD_BRIDGE"], v, sep)


def _unbridged_fillers(v: str, sep: str) -> str:
    """``v`` with every bridge of listed filler words (``CV_FILLER_WORDS``) between two digit groups replaced by
    ``sep``: the same bridge, a closed word list (client view: the rules without a check digit)."""
    return _bridge_sub(globals()["_CV_WORD_BRIDGE"], v, sep)


# review c13529af MUST 1: a word split by ONE line break (read as a space) is re-joined when its halves form a listed
# word (number words, filler words, the Spanish / French spelled e-mail words)
def _split_rx(words) -> re.Pattern:
    """Every listed word written with ONE space inside it (``fo ur``), as a whole word, case-insensitive; grouped by
    first letter, so each position tries one branch."""
    alts = sorted({w[:i] + " " + w[i:] for w in words for i in range(1, len(w))}, key=len, reverse=True)
    by_first: dict = {}
    for a in alts:
        by_first.setdefault(a[0], []).append(a[1:])
    body = "|".join(re.escape(f) + "(?:" + "|".join(re.escape(x) for x in xs) + ")" for f, xs in sorted(by_first.items()))
    return re.compile(r"(?i)(?<![a-z])(?:" + body + r")(?![a-z])")


def _rejoin(rx: re.Pattern, v: str) -> str:
    """``v`` with every match of ``rx`` (a listed word split by one space) written without the space."""
    return rx.sub(lambda m: m.group(0).replace(" ", ""), v) if " " in v else v


# review 08eea6e1 NIT: a number spelled as words ("four one one one ...", "zero" / "oh" for 0) is read as digits in a
# digit view of its own when NUMBER_WORDS_MIN or more number words follow one another (separated by spaces, commas or
# hyphens); each word becomes its digit and the spaces between two such words are dropped (other punctuation kept).
NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9"}
NUMBER_WORDS_MIN = 9
_NW = "(?:" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + ")"
_NUMBER_WORD_RUN = re.compile(r"(?i)(?<![a-z])%s(?:[ ,\-]{1,3}+%s){%d,}(?![a-z])" % (_NW, _NW, NUMBER_WORDS_MIN - 1))
_NUMBER_WORD = re.compile(r"(?i)%s(?: ++(?=%s))?" % (_NW, _NW))


_SPLIT_NUMBER = _split_rx(NUMBER_WORDS)
_SPLIT_FILLER = _split_rx(CV_FILLER_WORDS)


def _number_word_view(v: str) -> str:
    """``v`` with every run of ``NUMBER_WORDS_MIN``+ number words written as digits (``v`` itself when none). A number
    word split by one line break (``fo\nur``) is re-joined first (review c13529af MUST 1)."""
    w = _rejoin(globals()["_SPLIT_NUMBER"], v)
    low = w.lower()
    if not any(x in low for x in ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "zero")):
        return v
    out = _NUMBER_WORD_RUN.sub(lambda m: _NUMBER_WORD.sub(lambda x: NUMBER_WORDS[x.group(0).strip().lower()],
                                                          m.group(0)), w)
    return v if out == w else out


def _digits_hit(v: str) -> bool:
    nw = _number_word_view(v)
    if nw != v and _digits_hit_on(nw):
        return True
    return _digits_hit_on(v)


def _digits_hit_on(v: str) -> bool:
    if sum(map(v.count, _ASCII_DIGITS)) < 9:
        return False
    compact = _DIGIT_FILLER.sub("", _unbridged(v, ""))
    for m in _DIGIT_RUN.finditer(compact):
        g = m.group(0)
        if len(g) >= 9 and sum(map(g.count, _ASCII_DIGITS)) >= 9:
            return True
    return False


# ------------------------------------------------------------------------------------------------ 2f. e-mail

_EMAIL_PLAIN = re.compile(r"[A-Za-z0-9._%+\-]@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}(?![A-Za-z])")
_EMAIL_BRACKET = re.compile(r"(?i)[a-z0-9._%+\-] ?[\[\(\{<] ?(?:a ?t|@) ?[\]\)\}>] ?[a-z0-9\-]{1,63}")   # "[a t]" too
_EMAIL_SPELLED = re.compile(r"(?i)[a-z0-9._%+\-] at [a-z0-9\-]{1,63}(?: ?[\[\(\{<]? ?dot ?[\]\)\}>]? ?[a-z0-9\-]{1,63}){1,4}"
                            r"(?![a-z])")
# ``_EMAIL_SPELLED`` is the specification; ``_spelled_email`` decides the same thing in linear time. The regex engine
# backtracks over every place a label could end before a glued "dot" (``x at dotdotdot...``: 0.4 s for one 400-character
# word, a minute for a 64 KB field); here every reachable position is carried at once as one bit of an integer (a
# set-of-states run of the same pattern), each step a few whole-text bit operations.
_SPELLED_CLASS = {"c": re.compile(r"(?i)[a-z0-9._%+\-]"), "l": re.compile(r"(?i)[a-z0-9\-]"),
                  "z": re.compile(r"(?i)[a-z]"), "a": re.compile(r"(?i)a"), "t": re.compile(r"(?i)t"),
                  "d": re.compile(r"(?i)d"), "o": re.compile(r"(?i)o"), " ": re.compile(" "),
                  "(": re.compile(r"[\[\(\{<]"), ")": re.compile(r"[\]\)\}>]")}


class _BitTable(dict):
    """``str.translate`` table: a code point becomes "1" when it matches ``rx``, else "0" (filled on first use)."""

    def __init__(self, rx: re.Pattern) -> None:
        super().__init__()
        self.rx = rx

    def __missing__(self, o: int) -> str:
        r = "1" if self.rx.fullmatch(chr(o)) else "0"
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_SPELLED_TABLES = {k: _BitTable(rx) for k, rx in _SPELLED_CLASS.items()}


def _spelled_email(v: str) -> bool:
    """``_EMAIL_SPELLED.search(v) is not None``, in linear time (bit ``i`` of a mask: position ``i`` of ``v``)."""
    m = {k: int(v.translate(t)[::-1] or "0", 2) for k, t in _SPELLED_TABLES.items()}
    sp, lab = m[" "], m["l"]

    def opt(s: int, k: str) -> int:                       # an optional character of class k
        return s | ((s & m[k]) << 1)

    def label(s: int) -> int:                            # [a-z0-9\-]{1,63}: every end position reachable from s
        ends = 0
        for _ in range(63):
            s = (s & lab) << 1
            if not s:
                break
            ends |= s
        return ends
    starts = m["c"] & (sp >> 1) & (m["a"] >> 2) & (m["t"] >> 3) & (sp >> 4)   # "<c> at " begins here
    ends = label(starts << 5)
    tail = ((1 << (len(v) + 1)) - 1) & ~m["z"]                                  # (?![a-z]): not a letter, or the end
    for _ in range(4):
        s = opt(opt(opt(ends, " "), "("), " ")
        s = ((((s & m["d"]) << 1) & m["o"]) << 1 & m["t"]) << 1                 # "dot"
        ends = label(opt(opt(opt(s, " "), ")"), " "))
        if ends & tail:
            return True
        if not ends:
            break
    return False


_TLDS = ("com", "net", "org", "io", "co", "us", "uk", "de", "fr", "ca", "au", "in", "jp", "es", "it", "nl", "edu",
         "gov", "info", "biz", "me", "ai", "app", "dev", "shop", "store", "xyz", "online", "site", "email", "mail",
         "ru", "br", "mx", "ch", "se", "no", "test", "example")
EMAIL_AT_MAX = 256
_LOCAL_CHAR = re.compile(r"[A-Za-z0-9._%+\-]")
_DOMAIN_END = re.compile(r"[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.(?:" + "|".join(_TLDS) + r")", re.IGNORECASE)
# review adacb2ca MUST 1: ``name at domain.tld`` with a literal dot (``domain.co.uk``; spaces around the dot tolerated:
# ``domain . com``, ``domain .com``, and ``domain. com`` when the next label is lower case, so a sentence end such as
# ``at noon. It`` is not read as one). After each word ``at`` preceded by a local-part character, the following text
# (dots re-joined) must read as a domain ending in a common top-level domain. Linear: the dots are re-joined once for
# the whole view, and each match reads only up to the next space.
_AT_WORD = re.compile(r"(?i)[A-Za-z0-9._%+\-][ \t\n]{1,3}at[ \t\n]{1,3}(?=[A-Za-z0-9])")
_DOT_JOIN = re.compile(r"[ \t]+\.[ \t]*(?=[A-Za-z0-9])|\.[ \t]+(?=[a-z0-9])")
_AT_DOMAIN = re.compile(_DOMAIN_END.pattern + r"(?![A-Za-z0-9\-])", re.IGNORECASE)


def _at_word_email(v: str) -> bool:
    low = v.lower()
    if "at" not in low or "." not in v:
        return False
    w = _DOT_JOIN.sub(".", v)
    return any(_AT_DOMAIN.match(w, m.end()) for m in _AT_WORD.finditer(w))


# review 601e328a MUST 3: an address spelled with letters spaced out (``l o c a l a t d o m a i n . c o m``) or with
# its words split mid-word (``dom ain DOT com``) is read on the NO-SPACE view anchored on the "at" / "dot" words: the
# text is read as words and anchors (a word "at" or "dot", also spelled one letter at a time, ``a t`` / ``d o t``);
# after each "at" anchor that follows a local-part character, the next words are joined with NO space (a "dot" anchor
# read as ".") one at a time, exactly as the split-"@" rule joins them (``_joins_to_domain``), until they read as a
# domain with a common top-level domain. The join stops at the next "at" anchor and at a sentence end (a word ending
# in "." before a capital), so a later sentence is not glued to the one before. Linear: one pass over the words, and
# each join reads at most ``JOIN_WORDS`` words / 255 characters.
JOIN_WORDS = 40
_ANCHORS = {"at": "at", "dot": "dot"}


def _joins_to_domain(words) -> int:
    """How many of the next words (at most ``JOIN_WORDS``, 255 characters), joined one at a time, first read as a
    domain with a common top-level domain (trailing punctuation ignored); 0 when they never do."""
    acc = ""
    for n, tok in enumerate(words[:JOIN_WORDS], 1):
        acc += tok
        if len(acc) > 255:
            return 0
        if _DOMAIN_END.fullmatch(acc.rstrip(".,;:!?)]}>\"'")):
            return n
    return 0


def _anchor_items(v: str) -> list:
    """``v`` as (kind, word) items: "at" / "dot" anchors, "end" (a sentence end) and "w" words."""
    toks = v.split(" ")
    out: list = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        k = t.lower()
        a = _ANCHORS.get(k)
        if a is None and len(t) == 1 and i + 1 < n:
            if k == "a" and toks[i + 1].lower() == "t":
                a, i = "at", i + 1
            elif k == "d" and i + 2 < n and toks[i + 1].lower() == "o" and toks[i + 2].lower() == "t":
                a, i = "dot", i + 2
        if a is not None:
            out.append((a, t))
        elif t:
            if out and out[-1][0] == "w" and out[-1][1].endswith(".") and t[:1].isupper():
                out.append(("end", ""))
            out.append(("w", t))
        i += 1
    return out


def _spaced_spelled_email(v: str) -> bool:
    return _spaced_join(v, frozenset())


# review 08eea6e1 SHOULD 5 (client view only): the split-word join stops at a common English word, so "at checkout on
# shop.com" is not glued into one domain; the strict lane joins through them (never loosened)
CV_JOIN_STOP = frozenset(("on", "at", "in", "the", "and", "for", "from", "with", "checkout", "today", "to", "of", "or",
                          "by", "now", "via"))


def _spaced_join(v: str, stop) -> bool:
    low = f" {v.lower()} "
    if " at " not in low and " a t " not in low:
        return False
    items = _anchor_items(v)
    for i, (k, t) in enumerate(items):
        if k != "at" or i == 0 or items[i - 1][0] != "w" or not _LOCAL_CHAR.fullmatch(items[i - 1][1][-1]):
            continue
        words: list = []
        for k2, t2 in items[i + 1:i + 1 + JOIN_WORDS]:
            if k2 in ("at", "end") or (stop and k2 == "w" and t2.lower().strip(".,;:!?)]}>\"'") in stop):
                break
            words.append("." if k2 == "dot" else t2)
        n = globals()["_joins_to_domain"](words)
        # only what the other spelled rules cannot read: a letter-spaced "a t", or two words glued (``dom ain``)
        if n and (len(t) == 1 or any(_ALNUM.search(a) and _ALNUM.search(b) for a, b in zip(words[:n - 1], words[1:n]))):
            return True
    return False


# review 08eea6e1 MUST 1: a DEFANGED dot ([dot] (dot) {dot} <dot> [.] (.) {.} <.>, any case, optional spaces; the NIT
# spellings [period] / [punto] / [point] too) is read as "." in an e-mail view of its own, so a real "@" or a spelled
# " at " followed by bracketed dots reads as an address (multi-label domains included)
_DEFANG_DOT = re.compile(r"(?i) ?[\[\(\{<] ?(?:d ?o ?t|\.|period|punto|point) ?[\]\)\}>] ?")


def _defanged(v: str) -> str:
    if "[" not in v and "(" not in v and "{" not in v and "<" not in v:
        return v
    return _DEFANG_DOT.sub(".", v)


# review 08eea6e1 MUST 2 / NIT: the spelled variants read as " at " / " dot " in an e-mail view of their own:
# local-at-domain-dot-tld (hyphens or underscores), " _at_ " / " _dot_ ", "at sign" / "at-sign", "period" for dot,
# Spanish "arroba" / "punto", French "arobase" / "point", a literal "@" (so "name@domain dot com" reads as spelled), and
# the glued "localATdomainDOTtld" (anchored on a case boundary: a lower-case letter or digit on both sides)
# Review c0c09bd1 MUST 3: "at symbol" / "at-symbol" (as "at sign"), a doubled "at at", and the common e-mail emoji
# (U+1F4E7 e-mail, U+2709 envelope, U+1F4E9 envelope with arrow) read as "@" (both lanes: the shared e-mail views)
EMAIL_EMOJI = "\U0001F4E7\u2709\U0001F4E9"
_AT_WORDS = r"(?:at[- ]?(?:sign|symbol)|at|arroba|arobase)"
_DOT_WORDS = r"(?:dot|period|punto|point)"
_VAR_AT = re.compile(r"(?i)(?<=[a-z0-9])[-_]%s[-_](?=[a-z0-9])|(?<![^ ])_%s_(?![^ ])|(?<![a-z])(?:at[- ](?:sign|symbol)"
                     r"|at at|arroba|arobase)(?![a-z])| ?[@%s] ?" % (_AT_WORDS, _AT_WORDS, EMAIL_EMOJI))
_VAR_DOT = re.compile(r"(?i)(?<=[a-z0-9])[-_]%s[-_](?=[a-z0-9])|(?<![^ ])_%s_(?![^ ])|(?<![a-z])(?:period|punto"
                      r"|point)(?![a-z])" % (_DOT_WORDS, _DOT_WORDS))
_GLUED_AT = re.compile(r"(?<=[a-z0-9])AT(?=[a-z0-9])")
_GLUED_DOT = re.compile(r"(?<=[a-z0-9])DOT(?=[a-z0-9])")
_VARIANT_HINTS = ("_at", "-at", "at_", "at-", "sign", "arroba", "arobase", "period", "punto", "point", "_dot", "-dot",
                  "symbol", "at at") + tuple(EMAIL_EMOJI)


# review c13529af MUST 1: a variant WRAPPED at a natural point (a line break, read as a space, before / after a hyphen
# or at an underscore: ``name-\nat-\ndomain``, ``name_\nat_``, ``at-\nsign``) reads as written unwrapped: in the
# variant view, spaces next to "-" / "_" are removed first; a Spanish / French spelled word split by a line break
# (``arro\nba``, ``pun\nto``) is re-joined when its halves form the word
_WRAP_SEP = re.compile(r" ?([-_]) ?")
# review c0c09bd1 MUST 6: the spaces around a "-" / "_" are removed ONLY next to an at / dot variant word (the word
# right before or right after the separator is one of these), so prose neighbours of an ordinary " - " are never glued
# onto an address
_WRAP_WORDS = frozenset(("at", "dot", "period", "punto", "point", "arroba", "arobase", "sign", "symbol"))
_WRAP_BEFORE = re.compile(r"[A-Za-z]{1,8}$")
_WRAP_AFTER = re.compile(r"[A-Za-z]{1,8}(?![A-Za-z])")
_SPLIT_WORDS = ("arroba", "arobase", "punto", "point", "period")


def _wrap_join(v: str) -> str:
    """``v`` with " - " / " _ " joined (spaces removed) only where the word touching the separator is an at / dot
    variant word (``_WRAP_WORDS``). Linear: each separator reads at most eight letters on each side."""
    def repl(m: re.Match) -> str:
        a, b = m.span()
        before = _WRAP_BEFORE.search(v, max(0, a - 8), a)
        after = _WRAP_AFTER.match(v, b)
        words = globals()["_WRAP_WORDS"]
        if (before is not None and before.group(0).lower() in words and (before.start() == 0 or not
                                                                          v[before.start() - 1].isalpha())) \
                or (after is not None and after.group(0).lower() in words):
            return m.group(1)
        return m.group(0)
    return _WRAP_SEP.sub(repl, v)


_SPLIT_VARIANT = _split_rx(_SPLIT_WORDS)


def _spelled_variants(v: str) -> str:
    """``v`` with the spelled e-mail variants written as " at " / " dot " (``v`` itself when none). Linear: a few
    bounded substitutions."""
    if " " in v:
        if "-" in v or "_" in v:
            v0 = v
            v = _wrap_join(v)
        else:
            v0 = v
        v = _rejoin(_SPLIT_VARIANT, v)
    else:
        v0 = v
    low = v.lower()
    if "@" not in v and "AT" not in v and "DOT" not in v and not any(h in low for h in _VARIANT_HINTS):
        return v0
    w = _GLUED_DOT.sub(" dot ", _GLUED_AT.sub(" at ", _VAR_DOT.sub(" dot ", _VAR_AT.sub(" at ", v))))
    return v0 if w == v else _collapse(w)


def _email_views(v: str) -> list:
    """``v``, its defanged-dot view and its spelled-variant view (distinct, in order)."""
    out = [v]
    d = _defanged(v)
    if d != v:
        out.append(d)
    s = _spelled_variants(d)
    if s not in out:
        out.append(s)
    return out


def _email_hit(v: str) -> bool:
    return any(_email_forms(x) for x in _email_views(v))


def _email_forms(v: str) -> bool:
    if "@" in v:
        w = re.sub(r" ?@ ?", "@", v)
        if _EMAIL_PLAIN.search(w):
            return True
        # an address split by spaces / line breaks: after each "@", the next words joined one at a time (at most 40
        # words / 255 characters) until they read as a domain with a common top-level domain; a local-part character
        # must precede the "@". More than EMAIL_AT_MAX "@" signs: withheld (fail closed).
        ats = 0
        for m in re.finditer("@", v):
            ats += 1
            if ats > EMAIL_AT_MAX:
                return True
            i = m.start() - 1
            while i >= 0 and v[i] == " " and m.start() - i < 8:
                i -= 1                                     # bounded look-back over spaces
            if i < 0 or not _LOCAL_CHAR.fullmatch(v[i]):
                continue
            if _joins_to_domain(v[m.end():m.end() + 1024].lstrip(" ").split(" ")):
                return True
    low = v.lower()
    if (("at" in low or "a t" in low or "@" in v) and ("[" in v or "(" in v or "{" in v or "<" in v)) \
            and _EMAIL_BRACKET.search(v):
        return True
    if _at_word_email(v):
        return True
    if " at " in low and "dot" in low and _spelled_email(v):
        return True
    return globals()["_spaced_spelled_email"](v)


# ------------------------------------------------------------------------------------------------ the check

# (check, function name): looked up at call time, so a test can disable one check and show another test fails
_CHECK_FUNCS = (("a_label", "_label_hit"), ("b_userinfo", "_userinfo_hit"), ("c_prefix", "_prefix_hit"))
WAIVABLE = frozenset(("d_token", "e_digits"))  # the only checks ever waived: (d) in a disclosed path position, (e) for a commerce query id
_URL_IN_TEXT = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'`]{1,4096}")
URL_TEXT_MAX = 64                        # URLs written in one field that are inspected; more: the field is withheld
URL_PARTS_MAX = 1024                     # URL parts (host labels, segments, query pairs, fragment) inspected per field


def _mask_clean_urls(m: str) -> tuple:
    """A URL written in the text is judged by the URL rules: one that passes every per-part check (with the disclosed
    URL positions) is masked out before checks (d) and (e), so a cited link does not withhold the prose around it,
    unless it passes ONLY through an exemption (a disclosed position, a commerce query id): that URL stays in the text
    and checks (d) / (e) read it; one that FAILS any part fails the whole text (``url_credential`` / ``url_personal``), since a
    URL rule (a host label, a split path) may see what the text checks do not. Checks (a)-(c) and (f) already ran on
    the unmasked text. More than ``URL_TEXT_MAX`` URLs or ``URL_PARTS_MAX`` URL parts in one field: ``"cap"`` (the
    field is withheld; fail closed, so a URL-dense text stays fast). Returns (masked text, failing check or None)."""
    low = m.lower()
    if "http" not in low and "www." not in low:
        return m, None
    n = 0
    for _ in _URL_IN_TEXT.finditer(m):
        n += 1
        if n > URL_TEXT_MAX:
            return m, "cap"
    worst: list = []
    budget = [URL_PARTS_MAX]

    def repl(x: re.Match) -> str:
        if budget[0] < 0:
            return x.group(0)                              # over the part cap: the field is withheld below
        u = x.group(0).rstrip(".,;:!?)]}")
        url = u if "://" in u else "https://" + u
        budget[0] -= _url_parts(url)
        if budget[0] < 0:
            return x.group(0)
        v, dependent = globals()["_url_judge"](url)
        if v is None:
            # a URL that passes only through an exemption stays in the text, so checks (d) / (e) still read it
            return x.group(0) if dependent else _MASK + x.group(0)[len(u):]
        worst.append(v)
        return x.group(0)
    out = _URL_IN_TEXT.sub(repl, m)
    if budget[0] < 0:
        return m, "cap"
    return out, ("url_credential" if CREDENTIAL in worst else ("url_personal" if worst else None))


def _abc(text: str) -> str | None:
    """Checks (a)-(c) only, on every view (a text glued from parts, where (d) / (e) would read words as one)."""
    marked: list = [None]
    for v, folded in _tagged_views(text):
        if not folded and _marked_label(text, v, marked):
            return "a_label"                               # (a) for the labels read on the marked views
        for name, fn in (_CHECK_FUNCS[1:] if folded else _CHECK_FUNCS):
            if globals()[fn](v):
                return name
    return None


def _check(text: str, waive=frozenset(), urls: bool = True) -> str | None:
    if len(text) > MAX_INPUT:
        return "cap"
    personal = None
    marked: list = [None]
    for v, folded in _tagged_views(text):
        if not v:
            continue
        if not folded and _marked_label(text, v, marked):
            return "a_label"                               # (a) for the labels read on the marked views (2a)
        # a folded view skips (a): the label rule folds confusables itself (``_letters_view`` / ``_fold_piece``)
        for name, fn in (_CHECK_FUNCS[1:] if folded else _CHECK_FUNCS):
            if globals()[fn](v):
                return name
        m = _masked(v)
        if urls:
            m, bad_url = _mask_clean_urls(m)
            if bad_url in ("url_credential", "cap"):
                return bad_url
            if bad_url and personal is None:
                personal = bad_url
        if "d_token" not in waive and _token_hit(m):
            return "d_token"
        if personal is None:
            if "e_digits" not in waive and _digits_hit(m):
                personal = "e_digits"
            elif _email_hit(v):
                personal = "f_email"
    return personal                                        # a credential failure on any view wins over personal data


# Review 2b26d980 SHOULD 1: ONE rule for a host name, in every lane. A text that is, whole, a syntactically valid host
# name (lower case, dotted labels, a TLD) is first judged by the host rule the sink guard defines (``sink_guard.
# host_refusal``, registered here at its import: this module cannot import it): a credential WORD in a label is a
# name (``1password.com``), a platform hash label is a name. Before, a tenant whose own domain held such a label had
# every URL of its site withheld (and its audits refused at the sink).
# Review 98b76cde (P1): the rule only ever ADDS host names. A host it refuses is judged by the checks that judged it
# before the rule existed (release 2b26d980: ``_check`` on the whole text; in a URL, on the host and on each label),
# so no host that release kept is withheld now. The rule holds no e-mail form: a sub-domain of a mailbox provider
# (``finance.yahoo.com``) and a host with an ``at`` label are host names, as they were.
HOST_RULE = None                         # (host) -> a reason name or None
HOST_LIKE = None                         # (text) -> the text has a host name's shape
_HOST_RULE_TRIED: list = []
# (an ``xn--`` label that does not decode to a label of letters is not a name: read as a token, so that the lanes
# agree on every host-shaped text; a host that fails on syntax or its TLD alone is left to the ordinary checks)
_HOST_CHECK = {"prefix": "c_prefix", "hex": "d_token", "token": "d_token", "idna": "d_token",
               "label_value": "a_label", "digits": "e_digits"}


def host_verdict(text) -> tuple:
    """(True, the failing check name or None) when ``text`` is a host name judged by the host rule; (False, None) when
    it is not one. Only ``(True, None)`` is a verdict (the host is kept): for every other answer the caller runs the
    ordinary checks, as release 2b26d980 did (review 98b76cde, P1)."""
    if len(text) > 253 or "." not in text:
        return False, None
    rule, like = globals()["HOST_RULE"], globals()["HOST_LIKE"]
    if rule is None and not _HOST_RULE_TRIED:
        _HOST_RULE_TRIED.append(True)                      # once: the same verdicts whichever module came first
        try:
            import sink_guard  # noqa: F401 - registers the rule (it imports this module, so it cannot be at the top)
        except Exception:  # noqa: BLE001 - the core used alone, without the service's sink guard
            pass
        rule, like = globals()["HOST_RULE"], globals()["HOST_LIKE"]
    if rule is None or like is None or not like(text):
        return False, None
    reason = rule(text)
    if reason is None:
        return True, None
    name = _HOST_CHECK.get(reason)
    return (True, name) if name is not None else (False, None)


def check(text, waive=frozenset()) -> str | None:
    """The failing check for ``text`` (``"cap"``, ``"type"``, ``"error"`` or one of ``CHECKS``; a credential check
    (a)-(d) is reported before a personal-data check (e)/(f)), or None when the text is clearly safe. Every check runs
    on every inspection view. ``waive``: checks a disclosed URL position waives (only (d) and (e)). Never raises."""
    if text is None:
        return None
    if not isinstance(text, str):
        return "type"
    if not text:
        return None
    try:
        applies, name = host_verdict(text)
        if applies and name is None:
            return None                                    # a host name the host rule takes
        return _check(text, frozenset(waive) & WAIVABLE)   # else the ordinary checks (review 98b76cde, P1)
    except Exception:  # noqa: BLE001 - fail closed: an input the checks cannot read is withheld
        return "error"


# Review c0c09bd1 (design change, Option B for operator text): operator free text (prompt-set prompts and context /
# negative terms, entity values / provenance, audit / schedule paths and queries, URL-prefix Search Console properties)
# is NOT refused for failing the strict check: it is stored in the tenant's vault and the log keeps only its vault id,
# a keyed HMAC and the strict check's flag. It is refused ONLY for what is never legitimate in it: a strong-key prefix,
# a PEM / PGP block, URL user info with a password (both lanes' rules on every view), and, in a URL that is fetched or
# queried, a value under a credential query key (``OPERATOR_QUERY_KEYS``, exact names).
OPERATOR_QUERY_KEYS = frozenset(("token", "key", "api_key", "apikey", "api-key", "secret", "sig", "signature",
                                 "password", "pwd", "passwd", "auth", "access_token", "client_secret", "id_token",
                                 "refresh_token", "sid", "sessionid", "session_id", "jsessionid", "phpsessid"))


def operator_refusal(text) -> str | None:
    """``c_prefix`` (a strong-key prefix or a PEM / PGP block), ``b_userinfo`` (``scheme://user:password@``), ``cap`` /
    ``type`` / ``error`` (fail closed), or None. Never raises."""
    if text is None:
        return None
    if not isinstance(text, str):
        return "type"
    if not text:
        return None
    if len(text) > MAX_INPUT:
        return "cap"
    try:
        for v, _folded in _tagged_views(text):
            if not v:
                continue
            if globals()["_userinfo_hit"](v):
                return "b_userinfo"
            if globals()["_prefix_hit"](v):
                return "c_prefix"
        cv = client_view_findings(text)
        if "strong_key" in cv:
            return "c_prefix"
        if "userinfo" in cv:
            return "b_userinfo"
        if {"cap", "error", "type"} & set(cv):
            return "error"
        return None
    except Exception:  # noqa: BLE001 - fail closed
        return "error"


def operator_url_refusal(url) -> str | None:
    """``operator_refusal`` on the URL (and its decoded query values), plus ``url_credential`` for a non-empty value
    under a credential query key (exact name, case-insensitive). Never raises."""
    r = operator_refusal(url)
    if r is not None:
        return r
    try:
        q = urlsplit(url).query
        for k, v in parse_qsl(q or "", keep_blank_values=True):
            if v and k.strip().lower() in OPERATOR_QUERY_KEYS:
                return "url_credential"
            r = operator_refusal(k) or operator_refusal(v)
            if r is not None:
                return r
        return None
    except Exception:  # noqa: BLE001 - fail closed
        return "error"


# Review c0c09bd1: an IDENTIFIER (tenant id, tenant domain, Finance client id, prompt-set name, competitor domain, an
# entity's source) is not prose: besides its own format it is refused when it holds a strong-key prefix / PEM / user
# info, a run of 9+ digits, an e-mail form, or fails the strict check at all.
_ID_DIGITS = re.compile(r"[0-9]{9,}")


# Review 2b26d980 SHOULD 9: an e-mail address written WITHOUT its "@" in an identifier, and prose folded with
# hyphens / dots into a tenant id or a Finance client id.
# mailbox providers (a closed list): ``<name>.<provider domain>`` is an e-mail address written without its "@"
MAILBOX_PROVIDERS = frozenset((
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.jp", "ymail.com", "outlook.com",
    "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.de", "gmx.net", "web.de", "mail.com", "mail.ru",
    "yandex.ru", "yandex.com", "zoho.com", "fastmail.com", "hey.com", "tutanota.com", "tuta.io", "qq.com",
    "163.com", "126.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "naver.com", "t-online.de"))
_MAILBOX_FOLDED = tuple(sorted({p.replace("-", ".") for p in MAILBOX_PROVIDERS}))   # ``t-online.de`` -> ``t.online.de``
_ID_SPLIT = re.compile(r"[-_.:=/\s]+")
_ID_MAILTO = re.compile(r"(?i)\s*(?:mailto(?![a-z0-9])|e-?mail\s?[:=])")
FOLDED_SEGMENTS_MIN = 6                  # "more than 5 segments" ...
FOLDED_WORDS_MIN = 4                     # ... of which 4+ are word-like: prose folded into an identifier
_FOLDED_WORD = re.compile(r"(?=[a-z]*[aeiouy])[a-z]{2,15}")


def atless_email(text) -> bool:
    """An e-mail address written without "@" in an identifier: ``mailto...``, ``email:...``, ``first.last.at.
    domain.tld`` (an ``at`` piece before a domain ending in a known TLD) or ``user.name.gmail.com`` (a closed list of
    mailbox providers' domains after a name), with ``.``, ``-`` or ``_`` as the separator. Never raises."""
    if not isinstance(text, str) or not text:
        return False
    try:
        low = text.lower()
        if _ID_MAILTO.match(low):
            return True
        parts = [x for x in _ID_SPLIT.split(low) if x]
        n = len(parts)
        for i in range(1, n - 2):
            if parts[i] == "at" and parts[-1] in _TLDS:
                return True
        folded = ".".join(parts)
        return any(folded.endswith("." + p) for p in _MAILBOX_FOLDED)
    except Exception:  # noqa: BLE001 - fail closed
        return True


def folded_prose(text) -> bool:
    """Prose folded into an identifier with hyphens / dots / underscores (a tenant id, a Finance client id): 6+
    segments of which 4+ are word-like (2..15 letters with a vowel). ``acme-co-uk-retail-west`` (5) is an id."""
    if not isinstance(text, str):
        return False
    parts = [x for x in _ID_SPLIT.split(text.lower()) if x]
    return len(parts) >= FOLDED_SEGMENTS_MIN and sum(1 for x in parts if _FOLDED_WORD.fullmatch(x)) >= FOLDED_WORDS_MIN


def email_form(text) -> bool:
    """An e-mail form as release 2b26d980 read one in an identifier: an "@", or a form the strict e-mail check hits
    (``first-last-at-domain.tld``: `` at `` between words once separators read as spaces). Never raises."""
    try:
        return isinstance(text, str) and ("@" in text or _email_hit(text))
    except Exception:  # noqa: BLE001 - fail closed
        return True


def identifier_refusal(text, atless: bool = True) -> str | None:
    """The reason an identifier is refused (a check name), or None. Never raises. ``atless=False``: without the
    "@"-less e-mail forms of review 2b26d980 (``atless_email``), which are for a tenant id and a Finance client id:
    a DOMAIN is never refused for being a mailbox provider's sub-domain or for an ``at`` label (review 98b76cde)."""
    if not isinstance(text, str) or not text:
        return "type"
    r = globals()["operator_refusal"](text)
    if r is not None:
        return r
    if _ID_DIGITS.search(text):
        return "e_digits"
    if "@" in text or _email_hit(text) or (atless and globals()["atless_email"](text)):
        return "f_email"
    return check(text)


def is_clearly_safe(text) -> bool:
    """True only when every check of ADR 0050 v2 passes on every inspection view of ``text``."""
    return check(text) is None


def redact_text(text) -> tuple:
    """STABLE PUBLIC ENTRY POINT (v1 name): ``(text, 0)`` when ``text`` is clearly safe, else ``("[withheld]", 1)``.
    The whole field is withheld; nothing is cut out of it. ``("", 0)`` for empty or non-string input."""
    if not isinstance(text, str) or not text:
        return "", 0
    return (text, 0) if is_clearly_safe(text) else (WITHHELD, 1)


redact = redact_text


def strong_secret(text) -> bool:
    """v1 name kept for callers: any check fails on ``text``."""
    return bool(text) and not is_clearly_safe(text)


has_secret = strong_secret


def _strong_spans(text: str) -> list:
    """v1 helper kept for TNZ (PR #53) until it switches to ``redact_text``: ``[(0, len(text))]`` when the text is
    not clearly safe (v2 never returns a partial span), else ``[]``."""
    return [] if is_clearly_safe(text or "") else [(0, len(text or ""))]

# ------------------------------------------------------------------------------------------------ URLs
# A URL is never checked as one string: scheme + host (each label) + each path segment + each query pair + the
# fragment are checked one by one, and only what passes is kept (step 3). Checks (a)-(c) run on every part with no
# exemption. The ONLY positional exemptions (ADR 0050 v2 section 3) waive checks (d) and (e), on these hosts and
# positions: a commit SHA on a code host; a Google Docs / Drive document id; a numeric item / post id on the listed
# hosts (never a 13-16 digit Luhn-valid number: a payment card stays withheld wherever it sits).

CREDENTIAL, PERSONAL = "credential", "personal"
_PERSONAL_CHECKS = frozenset(("e_digits", "f_email", "url_personal"))
CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gist.github.com",
                        "huggingface.co"})
# the hosts v1 exempted from its weak heuristics; v2 exempts NONE of them as a host (only the positions in
# ``POSITIONS``). Kept as a name so the property tests keep placing keys on every one of them.
ID_HOSTS = frozenset({
    "docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com", "forms.gle", "t.co", "bit.ly",
    "goo.gl", "tinyurl.com", "ow.ly", "lnkd.in", "buff.ly", "rebrand.ly", "is.gd", "youtu.be", "amzn.to",
    "shorturl.at", "cutt.ly", "fonts.gstatic.com", "open.spotify.com", "notion.so", "notion.site", "figma.com",
    "apnews.com", "facebook.com", "fb.com", "wixstatic.com", "wix.com", "squarespace-cdn.com", "squarespace.com",
    "ctfassets.net", "contentful.com", "gist.github.com", "huggingface.co", "loom.com", "canva.com", "zoom.us",
    "jsdelivr.net", "unpkg.com", "cloudfront.net", "googleusercontent.com", "ytimg.com", "maps.app.goo.gl"})
# THE DISCLOSED URL POSITIONS (ADR 0050 v2 section 3, r2 part 3a). A segment that fullmatches the shape, on one of the
# EXACT hosts listed (``canon_host``: ``www.`` dropped; no other subdomain counts), right after one of the listed path
# words, has check (d) waived. Checks (a), (b), (c), (e) and (f) still run on it: a prefix-shaped key or a card /
# long digit run in an exempt position is withheld as anywhere else. No position is valid on any host.
POSITION_WAIVE = frozenset(("d_token",))
POSITIONS = (
    # Google Drive / Docs file ids: Drive file / folder ids (33 chars from "1", 28 from "0B"), Docs ids (44 chars
    # from "1"); never 40 characters (the AWS secret length) and never all hex
    ("google_doc_id", ("drive.google.com", "docs.google.com"), ("d", "folders"),
     r"(?![0-9a-fA-F]+$)(?:1[A-Za-z0-9_\-]{32}|0B[A-Za-z0-9_\-]{26}|1[A-Za-z0-9_\-]{43})"),
    # a commit SHA (40 hex, or an abbreviated 7-12 hex, holding a letter) directly after ``/commit/``
    ("commit_sha", ("github.com", "gitlab.com", "bitbucket.org"), ("commit",),
     r"(?=[0-9]*[a-f])(?:[0-9a-f]{7,12}|[0-9a-f]{40})"),
    # a Canva design id: ``/design/<DA...>`` (the share-token segment after it is NOT exempt)
    ("canva_design", ("canva.com",), ("design",), r"DA[A-Za-z0-9_\-]{9}"),
    ("spotify_id", ("open.spotify.com",), ("episode", "track", "show", "playlist", "album", "artist"),
     r"[A-Za-z0-9]{22}"),
)
_POSITIONS = tuple((name, frozenset(hosts), frozenset(words), re.compile(rx)) for name, hosts, words, rx in POSITIONS)
# Gemini grounding redirects (agents/probes.py REDIRECTOR_HOSTS): the redirect id is NEVER stored or emitted (a
# stored form is ``<scheme>://<host>/grounding-api-redirect/[withheld]``); the raw URL may only be FETCHED, on this
# exact host, to resolve it (``url_has_secret`` waives check (d) on the id for that, and nothing else)
REDIRECT_HOST = "vertexaisearch.cloud.google.com"
REDIRECT_WORD = "grounding-api-redirect"
_REDIRECT_ID = re.compile(r"[A-Za-z0-9_\-]{1,2048}")
# query keys whose all-digit value (at most 20 digits) is a public commerce id, on the exact commerce hosts only:
# check (e) is waived for it, never for a Luhn-valid 12-19 digit number (a payment card) or a 9-digit run (an SSN)
QUERY_ID_KEYS = frozenset({"id", "variant", "item", "product"})
_AMAZON_TLDS = ("com", "ca", "com.mx", "com.br", "co.uk", "de", "fr", "it", "es", "nl", "se", "pl", "com.be",
                "com.tr", "ae", "sa", "eg", "in", "co.jp", "sg", "com.au", "cn")
COMMERCE_HOSTS = frozenset({"ebay.com", "etsy.com", "walmart.com", "target.com"}
                           | {"amazon." + t for t in _AMAZON_TLDS})
COMMERCE_SUFFIXES = (".myshopify.com",)
_QUERY_NUM = re.compile(r"\d{1,20}")
# exact path words that are a site's own structure (a constant cannot carry a value), kept although they are on the
# label list
STRUCTURAL_SEGMENTS = {"pinterest.com": frozenset({"pin"})}
TRACKING_PARAMS = frozenset({"gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "yclid", "twclid", "ttclid",
                             "igshid", "li_fat_id", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "_ga", "_gl",
                             "srsltid", "ref_src", "ref_url", "si", "epik", "sc_cid", "irclickid", "s_kwcid"})
URL_PLACEHOLDER = {CREDENTIAL: "{secret}", PERSONAL: "{withheld}"}


def canon_host(host: str) -> str:
    """Lower case, no trailing dot, no ``www.``."""
    h = (host or "").lower().rstrip(".")
    return h.removeprefix("www.")


def _host_in(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k.startswith("utm_")


def _kind(name: str | None) -> str | None:
    if name is None:
        return None
    return PERSONAL if name in _PERSONAL_CHECKS else CREDENTIAL


def _luhn(d: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(d)):
        x = int(c)
        if i % 2:
            x = x * 2 - 9 if x > 4 else x * 2
        total += x
    return total % 10 == 0


def position(host: str, segs: list, i: int) -> str | None:
    """The name of the disclosed URL position segment ``i`` of ``segs`` sits in on ``host`` (a ``canon_host``: only
    check (d) is waived there), or None. Exact hosts only."""
    seg = segs[i]
    prev = segs[i - 1] if i else ""
    g = globals()
    for name, hosts, words, rx in _POSITIONS:
        if not g["_position_host"](host, hosts) or prev not in words:
            continue
        if name == "canva_design" and not g["_canva_index_ok"](i):
            continue                                       # /design/<id>: the id is the second segment
        if rx.fullmatch(seg):
            return name
    return None


def _position_host(host: str, hosts) -> bool:
    return host in hosts                                   # exact (``canon_host``): no other subdomain


def _canva_index_ok(i: int) -> bool:
    return i == 2


def _commerce_host(host: str) -> bool:
    return host in COMMERCE_HOSTS or (host.endswith(COMMERCE_SUFFIXES) and host.count(".") >= 2)


def _query_waive(host: str, k: str, v: str) -> frozenset:
    """Check (e) is waived for a public numeric commerce id: an exact key, an exact commerce host, at most 20 digits,
    never a payment-card (Luhn 12-19 digits) or SSN (9 digits) shape."""
    g = globals()
    if k.lower() not in QUERY_ID_KEYS or not g["_commerce_host"](host) or not _QUERY_NUM.fullmatch(v):
        return frozenset()
    if g["_card_or_ssn"](v):
        return frozenset()
    return frozenset(("e_digits",))


def _card_or_ssn(v: str) -> bool:
    """An all-digit value shaped like a payment card (Luhn-valid, 12-19 digits) or an SSN (9 digits)."""
    return len(v) == 9 or (12 <= len(v) <= 19 and _luhn(v))


def _redirect_host(host: str) -> bool:
    """The exact grounding redirector host (no subdomain, no lookalike)."""
    return host.rstrip(".") == REDIRECT_HOST


def is_grounding_redirect(url) -> bool:
    """``url`` is a Gemini grounding redirect on the exact redirector host (its id is never stored)."""
    try:
        p = urlsplit(url) if isinstance(url, str) else None
        host = (p.hostname or "").rstrip(".") if p is not None else ""
    except ValueError:
        return False
    return globals()["_redirect_host"](host) and (p.path or "").split("/")[1:2] == [REDIRECT_WORD]


def redirect_withheld(url) -> str | None:
    """The only stored / emitted form of a grounding redirect: scheme + host + ``/grounding-api-redirect/[withheld]``
    (None when ``url`` is not one)."""
    if not is_grounding_redirect(url):
        return None
    scheme = (urlsplit(url).scheme or "").lower()
    return f"{scheme if scheme in ('http', 'https') else 'https'}://{REDIRECT_HOST}/{REDIRECT_WORD}/{WITHHELD}"


def _host_label_key(label: str) -> bool:
    """A host label that looks like a key: host names are case-insensitive, so the prefix families are matched on the
    upper-cased label too, and a long letters-and-digits label with digits interleaved is a key."""
    up = label.upper()
    if _prefix_found(up) or _AWS_JOINED.search(_NOSEP.sub("", up)):
        return True
    j = label.replace("-", "")
    ld, _ = _transitions(j)
    return len(j) >= 16 and any(c.isdigit() for c in j) and any(c.isalpha() for c in j) and ld >= 4


# raw format / control / separator characters in a URL part (bidi overrides hide a path's real reading)
_CONTROL_RX = re.compile("[\x00-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f"
                         "\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]")


def _url_parts(url: str) -> int:
    """About how many parts ``_inspect`` checks in ``url`` (never fewer): the host and each of its labels, the path
    segments, the query pairs and the fragment."""
    try:
        p = urlsplit(url)
    except ValueError:
        return 1
    q = p.query or ""
    return (2 + (p.netloc or "").count(".") + (p.path or "").count("/") + 1
            + ((q.count("&") + q.count(";") + 1) if q else 0) + (1 if p.fragment else 0))


def _inspect(url, urls: bool, fetch: bool = False) -> dict | None:
    """``fetch``: judge ``url`` for FETCHING (a grounding redirect id is checked with (d) waived, so the redirect can
    be resolved); otherwise for keeping (a grounding redirect id is never kept)."""
    if not isinstance(url, str) or len(url) > URL_MAX_INPUT:
        return None
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return None

    def chk(x: str, waive=frozenset()) -> str | None:
        try:
            if not x:
                return None
            name = _check(x, waive, urls=urls)
            if name is None and _CONTROL_RX.search(unquote(x)):
                return "control"                           # a bidi / invisible / control character: never kept
            return name
        except Exception:  # noqa: BLE001 - fail closed
            return "error"
    raw_host = re.sub(r":\d*$", "", (p.netloc or "").rsplit("@", 1)[-1])
    host_kind = None
    applies, name = globals()["host_verdict"](raw_host.lower())    # review 2b26d980: the one host rule
    if not (applies and name is None):
        # review 98b76cde (P1): a host the rule does not take is judged as before the rule (whole, then by label)
        for part in [raw_host] + raw_host.split("."):
            k = _kind(chk(part)) or (CREDENTIAL if part and _host_label_key(part) else None)
            if k is not None:
                host_kind = k
                break
    ch = canon_host(host)
    segs = (p.path or "").split("/")
    waived = [globals()["position"](ch, segs, i) if s else None for i, s in enumerate(segs)]
    redirect = globals()["_redirect_host"](host) and segs[1:2] == [REDIRECT_WORD]
    if redirect and fetch and len(segs) == 3 and _REDIRECT_ID.fullmatch(segs[2]):
        waived[2] = "grounding_redirect"                   # fetch only: the id is never kept (see below)
    verdicts = []
    dependent = False                                      # a part that passes ONLY because of an exemption
    for i, seg in enumerate(segs):
        if seg and any(_host_in(ch, (h,)) and seg in w for h, w in STRUCTURAL_SEGMENTS.items()):
            verdicts.append((seg, None))
            continue
        name = chk(seg, POSITION_WAIVE if waived[i] else frozenset())
        if name == "a_label" and not fetch and globals()["label_only"](seg):
            name = None                                    # review c0c09bd1: a label word alone carries no value
        k = _kind(name)
        if k is None and waived[i] and chk(seg) is not None:
            dependent = True
        if redirect and not fetch and i >= 2 and seg:
            k = k or PERSONAL                              # a grounding redirect id is never kept
        if k is None and not fetch and globals()["after_label"](segs, i):
            k = CREDENTIAL                                 # review c0c09bd1 SHOULD: the value after its label segment
        verdicts.append((seg, k))
    # a key or a number split across segments: the segments that passed alone (outside a disclosed position) read
    # together as one text; if that fails, every one of them holding a digit or an upper-case letter is withheld
    lone = [bool(s) and not fetch and globals()["label_only"](s) for s in segs]   # c0c09bd1: read apart
    kept = [unquote(s) for i, (s, k) in enumerate(verdicts) if s and k is None and not waived[i] and not lone[i]]
    if kept:
        cross = chk("/".join(kept)) or ("d_token" if _token_hit(_masked(_clean(" ".join(kept)))) else None)
        if cross is not None:
            kk = _kind(cross)
            verdicts = [(s, kk if (s and k is None and not waived[i] and any(c.isdigit() or c.isupper() for c in s))
                         else k) for i, (s, k) in enumerate(verdicts)]
    # checks (a)-(c) on the WHOLE path glued (a URI or prefix key spread over segments; a segment already withheld
    # for a credential is left out, one withheld for personal data is kept in): if that fails, every segment is
    # withheld, a disclosed position too
    glued = "".join(s if k != CREDENTIAL and not lone[i] else " " for i, (s, k) in enumerate(verdicts) if s)
    if glued.strip() and _abc(unquote(glued)):
        verdicts = [(s, CREDENTIAL if s else k) for s, k in verdicts]   # exempt positions included (rule (c))
    query = []
    for k, v in parse_qsl(p.query or "", keep_blank_values=True):
        waive = _query_waive(ch, k, v)
        kind = _kind(chk(k) or chk(v, waive) or chk(f"{k}={v}", waive))
        if kind is None and k.lower() in QUERY_ID_KEYS and _QUERY_NUM.fullmatch(v) and globals()["_card_or_ssn"](v):
            kind = PERSONAL                                # a card / SSN shape under an id key always fails
        if kind is None and waive and (chk(v) or chk(f"{k}={v}")):
            dependent = True
        query.append((k, v, kind, _tracking(k)))
    if p.query and not query:
        query.append(("", p.query, _kind(chk(p.query)), False))
    return {"scheme": (p.scheme or "").lower(), "host": host, "port": port, "host_kind": host_kind,
            "userinfo": "@" in (p.netloc or ""), "segments": verdicts, "positions": waived, "query": query,
            # review 08eea6e1 NIT: the fragment is read to the depth of a path / query value (one decode before the
            # check's own rounds)
            "fragment_kind": _kind(chk(p.fragment) or (chk(unquote(p.fragment)) if globals()["FRAGMENT_EXTRA_DECODE"]
                                                       else None)) if p.fragment else None,
            "exempt_dependent": dependent}


def inspect_url(url) -> dict | None:
    """The per-part verdict of ``url`` (None when it cannot be read): ``scheme``, ``host``, ``port``, ``host_kind``
    (None, ``credential`` or ``personal``: a failing host label withholds the whole URL), ``userinfo`` (present: never
    kept), ``segments`` [(segment, kind or None)], ``positions`` [disclosed position name or None], ``query``
    [(key, value, kind or None, is_tracking)], ``fragment_kind``. Tracking parameters get every check too (they are
    dropped before a fetch and never kept, but a key in one still marks the URL as never fetched)."""
    return _inspect(url, True)


def _worst(u: dict | None) -> str | None:
    if u is None or not u["host"]:
        return CREDENTIAL
    kinds = {u["host_kind"], u["fragment_kind"], CREDENTIAL if u["userinfo"] else None}
    kinds |= {k for _, k in u["segments"]} | {kind for _, _, kind, _ in u["query"]}
    return CREDENTIAL if CREDENTIAL in kinds else (PERSONAL if PERSONAL in kinds else None)


def _url_judge(url: str) -> tuple:
    """(verdict, exempt_dependent): the verdict is None when every part of ``url`` passes, else ``credential`` or
    ``personal`` (the worst part; a URL that cannot be read counts as ``credential``); ``exempt_dependent`` is True
    when some part passes only because of a disclosed position or a commerce query id."""
    u = _inspect(url, False)
    return _worst(u), bool(u and u["exempt_dependent"])


def _url_verdict(url: str) -> str | None:
    """None when every part of ``url`` passes; else ``credential`` or ``personal`` (the worst part). A URL that
    cannot be read counts as ``credential``."""
    return _url_judge(url)[0]


def safe_url(url) -> str:
    """What may be kept of ``url``: scheme + host (a failing host label withholds the whole URL) + only the path
    segments that pass on their own (a failing one becomes ``{secret}`` / ``{withheld}``); user info never; tracking
    parameters dropped; the rest of the query kept only when every key and value passes; the fragment kept only when
    it passes. ``[withheld]`` when the URL cannot be read."""
    u = inspect_url(url)
    if u is None or not u["host"] or u["host_kind"] is not None or check(u["scheme"]) is not None:
        return WITHHELD
    r = redirect_withheld(url)
    if r is not None:
        return r                                           # a grounding redirect id is never kept
    h = f"[{u['host']}]" if ":" in u["host"] else u["host"]
    netloc = h if u["port"] in (None, 80, 443) else f"{h}:{u['port']}"
    path = "/".join(URL_PLACEHOLDER[k] if k else s for s, k in u["segments"])
    q = [(k, v) for k, v, kind, trk in u["query"] if not trk]
    query = urlencode(q) if q and all(kind is None for _, _, kind, trk in u["query"] if not trk) else ""
    frag = urlsplit(url).fragment if u["fragment_kind"] is None else ""
    return urlunsplit((u["scheme"], netloc, path, query, frag))


def url_secret_segments(url) -> tuple:
    """v1 entry point: ``(indexes of path segments that fail a CREDENTIAL check (a)-(d), host / user info / query /
    fragment fail a credential check)``. Personal-data failures ((e), (f)) are reported by ``inspect_url``."""
    u = inspect_url(url)
    if u is None:
        return set(), True
    bad = {i for i, (_, k) in enumerate(u["segments"]) if k == CREDENTIAL}
    other = (u["host_kind"] == CREDENTIAL or u["userinfo"] or u["fragment_kind"] == CREDENTIAL
             or any(kind == CREDENTIAL for _, _, kind, _ in u["query"]))
    return bad, bool(other)


# ADR 0051 job 2 (Option B): only a CREDENTIAL finding blocks a fetch (sending a credential to a host is the harm); the
# r2 part 3a block on personal-data findings is reverted: a personal-data-shaped URL (an item id, a phone number in a
# path) is the client's own evidence, is fetched to classify it, and is never kept in the log (host only).
FETCH_BLOCKS = frozenset((CREDENTIAL,))


def url_has_secret(url) -> bool:
    """A URL with a CREDENTIAL failure in any part (tracking parameters included): never fetched (fail closed; a URL
    that cannot be read counts as a credential failure). A personal-data finding alone does not block the fetch. A
    grounding redirect on the exact redirector host is judged with check (d) waived on its id only, so it can be
    resolved; its id is still never kept."""
    return _worst(_inspect(url, True, fetch=True)) in FETCH_BLOCKS


def identity_query(url: str) -> list:
    """The query parameters that identify the page (every non-tracking parameter, sorted)."""
    try:
        pairs = parse_qsl(urlsplit(url).query or "", keep_blank_values=True)
    except ValueError:
        return []
    return sorted((k, v) for k, v in pairs if not _tracking(k))


def canonical_key(url: str) -> str:
    """The identity of a cited page (in memory only, hashed by the caller): scheme, canonical host, path and the
    identity-bearing query (tracking parameters and the fragment dropped)."""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return url or ""
    scheme = (p.scheme or "").lower()
    host = canon_host(p.hostname or "")
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, p.path or "/", urlencode(identity_query(url)), ""))


def fetch_url(url: str) -> str:
    """``url`` without its known tracking parameters (the URL a resolver fetches)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if not p.query:
        return url
    pairs = parse_qsl(p.query, keep_blank_values=True)
    if not any(_tracking(k) for k, _ in pairs):
        return url
    q = [(k, v) for k, v in pairs if not _tracking(k)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


# ------------------------------------------------------------------------------------------------ 4. guard_record
# Exact strings the services THEMSELVES emit that contain a label word (closed vocabulary: field names, flags,
# reason codes, placeholders). An exact match to a constant cannot carry a value, so it passes; everything else is
# checked. Placeholders inside a kept URL are blanked before its check.
VOCAB = frozenset({
    "secret_redacted", "SECRET_REDACTED", "NOT_FETCHED_SECRET", "CITED_URL_NOT_FETCHED", "credential", "personal",
    "withheld", "[withheld]", "{secret}", "{withheld}", "PERSONAL_DATA", "UNREADABLE", "max_tokens",
    "input_tokens", "output_tokens", "total_tokens", "token_usage", "tokens", "prompt_tokens", "completion_tokens",
})
_PLACEHOLDERS = re.compile(r"\{secret\}|\{withheld\}|\[withheld\]")
# keys whose values are machine values of one exact shape, produced by the service (hashes, timestamps)
_TYPED = (
    (re.compile(r"(?:.*_)?sha256$"), re.compile(r"[0-9a-f]{64}")),
    # the service's own ids: "seo-<kind>-<40 hex>" (prompt ids add ":v<version>:<index>")
    (re.compile(r"(?:.*_)?id$"), re.compile(r"seo-[a-z]{2,8}-[0-9a-f]{40}(?::v\d{1,6}:\d{1,6})?")),
    (re.compile(r"(?:.*_)?(?:at|due|time|date)$"),
     re.compile(r"(?:19|20)\d\d-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d{1,9})?)?(?:Z|[+-]\d\d:?\d\d)?)?")),
)


def _str_ok(s: str, key) -> bool:
    if s in VOCAB:
        return True
    if isinstance(key, str):
        for krx, vrx in _TYPED:
            if krx.fullmatch(key) and vrx.fullmatch(s):
                return True
    t = _PLACEHOLDERS.sub(" ", s) if ("{" in s or "[" in s) else s
    return is_clearly_safe(t)


class _Budget:
    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


# One function per branch (looked up at call time, so a test can disable one branch and show another test fails).

_URL_KEY = re.compile(r"(?:.*_)?url$")


def _guard_str(s: str, key):
    """A failing string becomes ``[withheld]``; under a ``url`` / ``*_url`` key it becomes None (a withheld URL is
    null, as ``stored_url`` writes it, so no consumer reads ``[withheld]`` as a URL)."""
    if _str_ok(s, key):
        return s
    return None if isinstance(key, str) and _URL_KEY.fullmatch(key) else WITHHELD


def _guard_bytes(b, key):
    s = bytes(b).decode("utf-8", "replace")
    return s if _str_ok(s, key) else WITHHELD


def _guard_key(k, i: int):
    """A dict key: numbers / booleans / None kept; anything else read as text and checked (a failing key becomes
    ``[withheld]#<n>`` so two withheld keys never collide)."""
    if k is None or isinstance(k, (bool, int, float)):
        return k
    ks = k if isinstance(k, str) else (bytes(k).decode("utf-8", "replace") if isinstance(k, (bytes, bytearray))
                                       else str(k))
    return ks if _str_ok(ks, None) else f"{WITHHELD}#{i}"


def _guard_dict(d: dict, depth: int, key, budget: _Budget) -> dict:
    out = {}
    for i, (k, v) in enumerate(d.items()):
        gk = globals()["_guard_key"](k, i)
        out[gk] = _guard(v, depth + 1, gk if isinstance(gk, str) else key, budget)
    return out


def _guard_seq(seq, depth: int, key, budget: _Budget) -> list:
    return [_guard(v, depth + 1, key, budget) for v in seq]


def _guard_set(st, depth: int, key, budget: _Budget) -> list:
    return sorted((_guard(v, depth + 1, key, budget) for v in st), key=repr)


def _guard_other(obj, key):
    s = str(obj)
    return s if _str_ok(s, key) else WITHHELD


def _guard(obj, depth: int, key, budget: _Budget):
    budget.n += 1
    if depth > GUARD_MAX_DEPTH or budget.n > GUARD_MAX_NODES:
        return WITHHELD
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    g = globals()
    if isinstance(obj, str):
        return g["_guard_str"](obj, key)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return g["_guard_bytes"](obj, key)
    if isinstance(obj, dict):
        return g["_guard_dict"](obj, depth, key, budget)
    if isinstance(obj, (list, tuple)):
        return g["_guard_seq"](obj, depth, key, budget)
    if isinstance(obj, (set, frozenset)):
        return g["_guard_set"](obj, depth, key, budget)
    return g["_guard_other"](obj, key)


def _on_guard_error(obj):
    return WITHHELD


def guard_record(obj):
    """FAIL-CLOSED sweep of anything bound for storage, a log or output (step 4): every dict KEY and value, list,
    tuple and set member is checked; bytes are decoded then checked; any other object is read with ``str()`` then
    checked. A string that fails is replaced by ``[withheld]`` (a failing key by ``[withheld]#<n>``); numbers, booleans
    and None are kept. Tuples and sets come back as lists. Bounded depth (``GUARD_MAX_DEPTH``) and size
    (``GUARD_MAX_NODES``): beyond them, ``[withheld]``. If anything raises, the WHOLE record is withheld: the return is
    then ``[withheld]`` (a string), which the caller must treat as a failed run."""
    try:
        return _guard(obj, 0, None, _Budget())
    except Exception:  # noqa: BLE001 - fail closed
        return globals()["_on_guard_error"](obj)


# ------------------------------------------------------------------------------------------------ 5. client view
# ADR 0051 job 3 (Option B): the OWNER'S decrypted view of its own citation evidence (behind tenant authentication)
# and its exports mask a field only on HIGH-PRECISION findings, read on the same inspection views as the strict lane
# (``_tagged_views``: NFKC, invisibles removed, Unicode digits and confusables folded, bounded decoding). The strict
# filter above is unchanged and stays the only filter for logs, outbound data and every cross-tenant surface; nothing
# here is ever used to decide what is logged, fetched or sent.
#   strong_key   a known strong-key prefix (the 2c table, also on the view with spaces removed and on each token with
#                its separators removed) or a PEM / PGP block
#   userinfo     ``scheme://user:password@``
#   label_value  a credential label word (the 2a list) FOLLOWED by a separator (``:`` ``=`` ``->`` an arrow ``|`` ``>``,
#                JSON forms, or `` is `` / `` - `` / `` would be `` ... with a value-like value) and a value of 4+
#                non-space characters; the bare word in prose is not masked
#   card         a Luhn-valid number with a card network's prefix and length (13-19 digits, any separators / padding)
#   ssn          a US SSN shape 3-2-4 with a valid area, group and serial
#   phone        E.164 (``+`` and 8-15 digits) or NANP written ``(xxx) xxx-xxxx``, ``xxx-xxx-xxxx``, ``xxx.xxx.xxxx``
#   email        an e-mail address, the spelled and bracketed at / dot forms included
# Bare unlabelled short values and random tokens without a label or a known prefix are NOT masked here (residual,
# ADR 0051 decision 13).

CLIENT_HIDDEN = "{hidden}"
CV_PERSONAL = frozenset(("card", "ssn", "phone", "email"))
CV_NUMBER_KINDS = ("card", "ssn", "phone")
CV_GROUPS_MAX = 2048                     # digit groups read per view; more: the field is masked (fail closed)
CV_URL_PARTS_MAX = 256                   # query pairs read per URL; more: the whole URL is masked (fail closed)


def _cv_letters(w: str) -> str:
    # review c0c09bd1 MUST 9: an apostrophe (straight or curly) may sit between a label's letters ("parola d'ordine")
    return "[ _.\\-'\u2019]{0,2}".join(f"[{c}{c.upper()}]" for c in w)


def _cv_label_rx() -> re.Pattern:
    """Every Latin label of the 2a list (the keys of ``_JOINED`` and ``_PIECES``: each entry through ``_label_norm``,
    so ASCII letters, none left out; ``_cv_latin_keys`` checks at import that the client view's own text view reads
    each entry as that key), letters optionally split by up to two separator characters, starting and ending at a
    piece boundary (a non-letter, or a camelCase change), grouped by first letter."""
    words = sorted(set(_JOINED_KEYS) | _PIECE_KEYS, key=len, reverse=True)
    by_first: dict = {}
    for w in words:
        by_first.setdefault(w[0], []).append(w)
    alts = [f"(?=[{f}{f.upper()}])(?:" + "|".join(_cv_letters(w) for w in ws) + ")" for f, ws in sorted(by_first.items())]
    return re.compile(r"(?:(?<![A-Za-z])|(?<=[a-z])(?=[A-Z]))(?:" + "|".join(alts) + r")(?:(?![A-Za-z])|(?=[A-Z][a-z]))")


_CV_LABEL = _cv_label_rx()
# Reviews ffd28ca8 / cd0a6c7d / cbdf5e78: a script label is matched by THE SAME pattern as in the strict lane
# (``_SCRIPT_SEQ`` on the word view, ``_MARKED_SEQ`` on the marked views: the boundary rule of section 2a; an unspaced
# label's letters may be joined by a space or "_", review c13529af S5; any case, review 98b76cde MUST 2), and what
# follows the label (its separator and value) is read on the view as written (``_cv_script_ends``). A stem's match
# reaches the end of the word that carries it (one of its declared endings), so the nominative forms below, and the
# other declared case forms, are read before their separator (before review cbdf5e78 only these two were: ADR 0051
# decision 71). The two full forms are kept as a check: each must be matched, whole, by both lanes at import.
_CV_SCRIPT_FULL = (
    "\u043f\u0430\u0440\u043e\u043b\u044c",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2",
)
# what follows a label: closing quotes / brackets / emphasis, then a separator, then opening quotes / brackets, then a
# value of 4+ characters (stopped by a space, a quote, a bracket, ``|`` ``,`` ``;`` ``&``). Review 98b76cde MUST 2: the
# words are matched in ANY case (`` Is ``, `` IS ``, `` Was ``, `` WOULD BE ``): a label and its value were shown
# to the owner when the verb between them had a capital
_CV_AFTER = re.compile(
    " ?[\"'\\]\\)>*`]{0,3} ?(?:(?P<p>:|=>|=|->|\u2192|\u21d2|\\||>)"
    "| (?:(?i:is)|-|\u2013|\u2014) |(?P<w> (?i:was|are|would be|will be|remains) ))"
    " ?[\"'`<\\[(*|]{0,3} ?(?P<val>[^\\s\"'`<>|,;&)\\]]{4,}|[^\\s\"'`<>|,;&)\\]]{1,3} [^\\s\"'`<>|,;&)\\]]{1,16})")


def _cv_valueish(val: str) -> bool:
    """After a natural-language separator beyond the listed ones (`` was ``, `` would be ``, `` remains ``...) the
    value must look like one: a digit or a symbol in it, or a capital after its first letter (``the password would be
    reset`` is prose). After the listed separators (``:`` ``=`` `` is `` `` - `` an arrow, JSON) any value of 4+
    characters counts."""
    return not val.isalpha() or any(c.isupper() for c in val[1:])


# The sharp s: NFKD does not decompose it and ``lookalikes`` reads it by case folding (``ss``), which the client
# view's cased table does not do. Review cd0a6c7d MUST B: the German password label written with it was shown.
_CV_SHARP_S = {"\u00df": "ss", "\u1e9e": "SS"}


class _CvLatinTable(dict):
    """``str.translate`` table: the client view's text view for the LATIN labels, one character at a time, case kept
    (its rules read case: a camelCase boundary before a label, a capital inside a value after `` was ``). Each
    character is read as the client view always read it: NFKD, the combining marks dropped, a lookalike letter as
    the ASCII letter it resembles in the character's own case (``_fold_confusables``).
    Two corrections, each a letter the strict lane's normal form (``_label_norm``) always read:
      review ffd28ca8 MUST 1  ``_fold_confusables`` reads EVERY upper-case letter whose fold is ``l`` as a capital
                              I. That is right for a lookalike from another script (the Greek iota, the Cyrillic
                              palochka); a LATIN letter L with a stroke or a bar is an L. Before, the Polish label
                              in upper case read ``H A S I O`` here and was shown;
      review cd0a6c7d MUST B  the sharp s reads ``ss`` and the capital sharp S ``SS`` (``_CV_SHARP_S``).
    The label keys and this view agree by construction: ``_cv_latin_keys`` runs every natural spelling of every
    Latin entry through this view at import and stops the import when the result is not the entry's key. Filled on
    first use, bounded."""

    def __missing__(self, o: int) -> str:
        c = chr(o)
        r = c
        if not c.isascii():
            r = _CV_SHARP_S.get(c)
            if r is None:
                r = _fold_confusables("".join(x for x in unicodedata.normalize("NFKD", c) if not unicodedata.combining(x)))
                if r == "I" and c.lower() != c and unicodedata.name(c, "").startswith("LATIN") and _label_norm(c) == "l":
                    r = "L"
        if len(self) < _CMAP_MAX:
            self[o] = r
        return r


_CV_LATIN_VIEW = _CvLatinTable()


def _cv_label_view(v: str) -> str:
    """``v`` as the client view reads it for a Latin label, case kept: ``contrase\u00f1a`` reads as ``contrasena``,
    the stroked L as ``L`` / ``l``, the sharp s as ``ss``, a lookalike letter as the ASCII letter it resembles."""
    return v.translate(_CV_LATIN_VIEW)


def _natural_spellings(e: str) -> tuple:
    """The spellings an entry is checked in at import: as listed, UPPER, Title, and, for an entry holding ``ss``,
    written with the sharp s in both cases (``pa\u00dfwort``, ``Pa\u00dfwort``, ``PA\u1e9eWORT``)."""
    forms = [e, e.upper(), e.title()]
    if "ss" in e.lower():
        sharp = e.lower().replace("ss", "\u00df")
        forms += [sharp, sharp.title(), e.upper().replace("SS", "\u1e9e")]
    return tuple(dict.fromkeys(forms))


def _cv_latin_keys(name: str, entries) -> tuple:
    """The keys of one Latin list AS THE CLIENT VIEW READS ITS ENTRIES: each entry, in every natural spelling
    (``_natural_spellings``), through the inspection view and ``_cv_label_view``, letters only, lower case. It must
    be the entry's key (``_label_norm``, the strict lane's form), and the strict lane must read the spelling as that
    key too: an entry the two lanes would read differently in any of them stops the import."""
    out: list = []
    for i, e in enumerate(entries):
        k = "".join(c for c in _label_norm(e) if c.isalpha())
        for form in _natural_spellings(e):
            v = normalise(form)
            if "".join(c for c in _cv_label_view(v) if c.isalpha()).lower() != k or _letters_view(v) != k:
                raise ValueError(f"credential label list {name}: entry {i} is read differently by the client view")
        if k not in out:
            out.append(k)
    return tuple(out)


_SPLIT_FULL[:] = [a for e, sp in zip(_CV_SCRIPT_FULL, _script_words("_CV_SCRIPT_FULL", _CV_SCRIPT_FULL))
                  for a in _split_alternatives(e, sp)]
_SCRIPT_SPLIT = re.compile(_SCRIPT_SPLIT.pattern + "".join("|" + a for a in _SPLIT_FULL))


def _cv_script_keys(name: str, entries) -> tuple:
    """The prefilter keys of one script list, each entry checked AS BOTH LANES READ IT (as listed, UPPER, Title): the
    word view of its inspection view (its marked view, for a marked entry) is matched WHOLE by the one pattern both
    lanes read (``_SCRIPT_SEQ`` / ``_MARKED_SEQ``), the strict lane flags it, and its letters are its prefilter key.
    An entry the lanes would read differently, or that the rule would not take whole, stops the import."""
    keys = _label_keys(name, entries, False)
    g = globals()
    for i, e in enumerate(entries):
        k = _script_letters(normalise(e))
        marked = _marked_entry(e)
        rx = g["_MARKED_SEQ"] if marked else g["_SCRIPT_SEQ"]
        for form in g["_natural_spellings"](e):
            v = _clean_marked(form) if marked else normalise(form)
            ok = (rx is not None and _script_letters(normalise(form)) == k
                  and rx.fullmatch(_script_word_view(v)) is not None
                  and (g["_marked_hit"](form) if marked else g["_script_hit"](v)))
            if not ok:
                raise ValueError(f"credential label list {name}: entry {i} is read differently by the client view")
    return keys


def _check_label_lists() -> None:
    """Run at import: every entry of every list, in its natural spellings, is read as its key by both lanes."""
    if _cv_latin_keys("_JOINED", _JOINED) != _JOINED_KEYS or \
            frozenset(_cv_latin_keys("_PIECES", sorted(_PIECES))) != _PIECE_KEYS:
        raise ValueError("credential label lists: the client view and the strict lane disagree on a key")
    if _cv_script_keys("_SCRIPT", _SCRIPT) != _SCRIPT_KEYS:
        raise ValueError("credential label lists: the client view and the strict lane disagree on a key")
    _cv_script_keys("_CV_SCRIPT_FULL", _CV_SCRIPT_FULL)


_check_label_lists()


def _cv_script_ends(v: str, sv: str, matches, table=None) -> list:
    """Where each match on the word view ``sv`` of ``v`` (made one character at a time by ``table``, the word view's
    by default) ends IN ``v`` (the view as written): what follows a label (its separator and value) is read on
    ``v``, exactly as it was before the label was looked for on the word view. One cursor walks ``v`` once
    (linear); the characters the view drops right after a label (a tatweel) are skipped. A match that ends inside
    the fold of one character is left out."""
    if sv == v:
        return [m.end() for m in matches]
    table = _SCRIPT_WORD if table is None else table
    out: list = []
    j = acc = 0
    n = len(v)
    for m in matches:
        e = m.end()
        while j < n and acc < e:
            acc += len(table[ord(v[j])])
            j += 1
        if acc != e:
            continue
        while j < n and not table[ord(v[j])]:
            j += 1
        out.append(j)
    return out


def _cv_script_value(v: str, rx, table=None) -> bool:
    """A script label that ``rx`` matches on the word view of ``v`` (or on ``v`` through ``table``), followed IN
    ``v`` by a separator and a value."""
    wv = _script_word_view(v) if table is None else v.translate(table)
    for end in _cv_script_ends(v, wv, rx.finditer(wv), table):
        a = _CV_AFTER.match(v, end)
        if a is not None and (a.group("w") is None or _cv_valueish(a.group("val"))):
            return True
    return False


def _cv_marked(text: str) -> bool:
    """The client view's marked step: a marked script label and its value on a marked view of ``text``."""
    rx = globals()["_MARKED_SEQ"]
    return rx is not None and any(not mv.isascii() and _cv_script_value(mv, rx) for mv in _marked_views(text))


# ``Bearer <token>`` with only a space (review adacb2ca SHOULD 3): a token of 12+ token characters holding a digit or a
# token symbol (``bearer instruments`` is prose)
_CV_BEARER = re.compile(r"(?i)(?<![a-z])bearer (?P<t>[A-Za-z0-9\-._~+/]{12,}=*)")


def _cv_label(v: str) -> bool:
    if "bearer" in v.lower():
        for m in _CV_BEARER.finditer(v):
            t = m.group("t")
            if any(c.isdigit() or c in "-._~+/=" for c in t):
                return True
    if v.isascii():
        forms: tuple = (v,)
    else:
        lv = _cv_label_view(v)
        forms = (v,) if lv == v else (v, lv)
    for w in forms:
        for m in _CV_LABEL.finditer(w):
            a = _CV_AFTER.match(w, m.end())
            if a is not None and (a.group("w") is None or _cv_valueish(a.group("val"))):
                return True
    g = globals()
    if v.isascii() or getattr(_CV_MEMO, "folded", False):
        return False                                       # (a script label is not read on an ASCII-folded view)
    if g["_SCRIPT_RX"].search(_script_letters(v)) is not None:
        # the strict lane's patterns (section 2a), then a value
        return _cv_script_value(v, g["_SCRIPT_SEQ"]) or _cv_script_value(v, g["_SCRIPT_SPLIT"], _LOWER)
    return False


_CV_MEMO = threading.local()


def _cv_prefix_found(s: str) -> bool:
    """``_prefix_found(s)`` (every family), remembered for the length of one client-view call (review c13529af MUST 6:
    a view, its space-removed form and its token joins are often identical across views; each is scanned once)."""
    memo = getattr(_CV_MEMO, "prefix", None)
    if memo is None:
        return _prefix_found(s)
    r = memo.get(s)
    if r is None:
        r = memo[s] = _prefix_found(s)
    return r


def _cv_strong(v: str) -> bool:
    """A strong-key prefix (2c) on the view, on the view with every space removed (every family: a superset of the
    strict lane's no-space families), the AWS id with separators removed, and on each token with its separators
    removed (a prefix key with separators put in: ``sk-pr-oj-...``)."""
    if _cv_prefix_found(v):
        return True
    if " " in v and _cv_prefix_found(v.replace(" ", "")):
        return True
    ns = _CV_NOSEP.sub("", v)
    if _AWS_JOINED.search(ns):
        return True
    if "BEGIN" in ns and _CV_PEM.search(ns):
        return True                                        # a PEM / PGP header with spaces or separators put in
    toks = [t for t in (x.strip(_EDGE) for x in v.split(" ") if len(x) >= 12) if _TOK_SEP.search(t)]
    if not toks:
        return False
    # every token with separators is read three ways, each as ONE text (one regex pass, not one per token): its
    # separators all removed, and removed only after its first / second separator (the prefix's own one kept)
    joined = " ".join(_TOK_SEP.sub("", t) for t in toks)
    if _CV_JOINED_START.search(joined) or _cv_prefix_found(joined):
        return True
    for k in (1, 2):
        if _cv_prefix_found(" ".join(_cv_after_sep(t, k) for t in toks)):
            return True
    for t in toks:
        if t[0] not in "MNO":
            continue
        if "-" in t and _PREFIX_DISCORD.search(t.replace("-", "")):
            return True                                    # a Discord token with dashes put in
        if t.count(".") >= 3 and _CV_DISCORD_JOINED.fullmatch(t.replace(".", "")) and all(_classes(t)):
            return True                                    # a Discord token with dots put in
    return False


def _cv_after_sep(t: str, k: int) -> str:
    """``t`` with every separator after its ``k``-th one removed (``_prefix_after_separator``), or "" when it has
    fewer or the ``k``-th ends past character 12."""
    ends = [m.end() for m in _TOK_SEP.finditer(t, 0, 64)][:k]
    if len(ends) < k or ends[k - 1] > 12:
        return ""
    e = ends[k - 1]
    return t[:e] + _TOK_SEP.sub("", t[e:])


_CV_JOINED_START = re.compile(r"(?<![^ ])(?:" + _JOINED_PREFIX.pattern + ")")


_CV_NOSEP = re.compile(r"[\s.\-_/:]+")
_CV_PEM = re.compile(r"BEGIN[A-Z0-9]{0,40}(?:PRIVATEKEY|PGP|OPENSSH|ENCRYPTED|CERTIFICATEREQUEST)")
_PREFIX_DISCORD = re.compile(dict(PROVIDERS)["discord"])
_CV_DISCORD_JOINED = re.compile(r"[MNO][A-Za-z0-9_\-]{56,120}")


_CV_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}://")
_CV_AUTHORITY = re.compile(r"[^\s/?#]{0,512}")


def _cv_userinfo(v: str) -> bool:
    """``scheme://user:password@`` (a password present; a bare ``user@`` is not a credential)."""
    if "@" not in v:
        return False
    for w in (v, v.replace(" ", "")) if " " in v else (v,):
        if "://" not in w:
            continue
        n = 0
        for m in _CV_SCHEME.finditer(w):
            n += 1
            if n > URL_TEXT_MAX:
                return True                                # fail closed
            auth = _CV_AUTHORITY.match(w, m.end()).group(0)
            ui, at, _host = auth.rpartition("@")
            if at and ":" in ui and ui.split(":", 1)[1]:
                return True
    return False


_CV_BRACKET_AT = re.compile(r"(?i)[a-z0-9._%+\-] ?[\[\(\{<] ?(?:a ?t|@) ?[\]\)\}>] ?")      # "[a t]" too
_CV_DOT = re.compile(r"(?i) ?[\[\(\{<] ?(?:d ?o ?t|\.) ?[\]\)\}>] ?| dot |\s?\.\s?")   # [dot] / [.] / dot / .
_CV_DOMAIN = re.compile(r"(?i)[a-z0-9\-]{1,63}+(?:\.[a-z0-9\-]{1,63}+){1,4}+(?![a-z0-9\-])")

# Client view only (review 601e328a MUST 4, owner decision): the SPELLED " at " forms (``name at domain.tld``,
# ``name at domain dot tld`` and the letter-spaced / split forms; never the literal "@" or the bracketed "[at]" /
# "(at)" forms) are NOT read as an e-mail only when BOTH hold:
#   1. the domain after "at" is one of the CLOSED list of large retailers / marketplaces / platforms below
#      (``CV_RETAILER_HOSTS``: the host itself or a subdomain of it, read with the e-mail rules' dot forms: ".",
#      " . ", " dot ", "[dot]"), and
#   2. the word before "at" is on the closed list of ordinary prose words (``CV_PROSE_BEFORE_AT``: "available at
#      amazon.com", "sold at target dot com"), written in lower case or capitalised (a sentence start).
# Review 08eea6e1 SHOULD 5 (owner decision) widens this for the spelled " at " form only: a prose-list word before "at"
# is prose for ANY domain (condition 1 dropped for it); "it" / "them" before "at" are prose only before a listed
# retailer (``CV_PRONOUNS_BEFORE_RETAILER``); a domain with a defanged dot (``[.]``, ``(dot)``) and the spelled
# variants (``-at-``, ``_at_``, "at sign", "arroba"...) are never exempt; the literal "@" and "[at]" never were.
# Role-mailbox words (``CV_ROLE_WORDS``: sales, support, info, shop, store, deals, offers, shipping, order, sale...)
# and pronouns (us, me, you, our, ...) are NOT on the prose list, so "support at amazon.com" and "shop at
# acmepets.com" are masked; any address on another domain is masked whatever word comes before it. Both lists are read
# at call time (``globals()``), so a test can change them and show the tests that rely on them fail.
_RETAILER_TLDS = {
    "amazon": ("com", "ca", "co.uk", "de", "fr", "it", "es", "nl", "se", "pl", "com.be", "com.mx", "com.br", "com.au",
               "co.jp", "in", "sg", "ae", "sa", "com.tr", "eg", "ie"),
    "ebay": ("com", "ca", "co.uk", "de", "fr", "it", "es", "com.au", "ie", "at", "ch", "nl", "pl", "be"),
    "walmart": ("com", "ca", "com.mx"), "bestbuy": ("com", "ca"), "costco": ("com", "ca", "co.uk", "com.au"),
    "homedepot": ("com", "ca"), "lowes": ("com", "ca"), "nordstrom": ("com", "ca"), "sephora": ("com", "ca", "fr"),
    "petsmart": ("com", "ca"), "wayfair": ("com", "ca", "co.uk", "de"), "ikea": ("com",), "apple": ("com",),
    "google": ("com", "co.uk", "ca", "de"), "newegg": ("com", "ca"), "gap": ("com", "ca"), "nike": ("com",),
    "adidas": ("com", "co.uk", "de", "ca"), "zalando": ("de", "co.uk", "fr", "it", "es", "nl"), "otto": ("de",),
    "rakuten": ("com", "co.jp"), "flipkart": ("com", "in"), "aliexpress": ("com", "us"), "shein": ("com", "co.uk"),
    "etsy": ("com",), "target": ("com",), "kohls": ("com",), "macys": ("com",), "ulta": ("com",), "chewy": ("com",),
    "petco": ("com",), "rei": ("com",), "shopify": ("com",), "tiktok": ("com",), "instagram": ("com",),
    "youtube": ("com",), "facebook": ("com",), "temu": ("com",), "bhphotovideo": ("com",),
    "dickssportinggoods": ("com",), "zappos": ("com",), "oldnavy": ("com",), "samsclub": ("com",), "cvs": ("com",),
    "walgreens": ("com",), "kroger": ("com",), "wholefoodsmarket": ("com",), "instacart": ("com",),
    "doordash": ("com",), "ubereats": ("com",), "alibaba": ("com",), "overstock": ("com",), "dell": ("com",),
    "argos": ("co.uk",), "bol": ("com",), "mercadolibre": ("com.mx", "com.ar", "com"), "tesco": ("com",),
    "staples": ("com",), "michaels": ("com",), "wish": ("com",), "poshmark": ("com",), "mercari": ("com",),
}
CV_RETAILER_HOSTS = frozenset(f"{name}.{tld}" for name, tlds in _RETAILER_TLDS.items() for tld in tlds)
CV_ROLE_WORDS = frozenset("""
sales support info information help helpdesk contact contacts admin administrator hello hi hey billing invoice invoices
accounts accounting orders order team press jobs careers career hr recruiting service services customerservice
care news newsletter mail email office marketing media returns return privacy legal security abuse feedback partners
partner shop shops store stores deal deals offer offers shipping sale merch pricing webmaster postmaster noreply
enquiries inquiries booking bookings events studio wholesale
i me my mine we us our ours you your yours they them their theirs he him his she her hers it its
""".split())
CV_PROSE_BEFORE_AT = frozenset("""
a about above across actually affordable again ago all almost alone already also always amazing an and another
any anyone anything anywhere appear appears are arrive arrived arriving as ask asked assembled available avoid
away back based be been being best better big both bought brand brands browse browsing built but buy buying
cart carried carries cheap cheaper cheapest check checked checkout clearance click come comes coming
compare compared cost costs could currently daily delivered directly discount
discounted discounts do does done down each early easily easy either else elsewhere employed even
eventually ever every everything everywhere exclusive exclusively expensive featured find finding first found
free fresh from fun gear get gets getting gift gifts go goes going gone good got great had happen happens has
have here hired holiday hosted how however in instead interested is items just kept last later
launched least less let lightly limited listed listing listings live lived located look looked looking lots
low lower lowest made make many marked more most much must near nearly never new next nice no not
nothing now of off offered offering often on once one online only open or ordered ordering
other others out over own paid pay people pick picked placed post posted pre price priced prices
product products published purchase purchased purchases purchasing put quality rated read
ready really receive received regular rest restocked retail retailer retailers review reviewed reviews right
same save saved say says see seen sell seller sellers selling sells shipped shopped shopper
shoppers shopping should shown sign signed similar since site sites so sold some someone something sometimes
soon spend spent start started starting starts still stock stocked stocks
such sure that the then there these thing things this those through today together too top
tried try trying twice two up usually very visit visited want wanted was way week well went were what
when where which while who why with work worked works worth would yesterday yet
customers shoppers buyers users reviewers people fans members visitors readers
day days weeks month months year years line lines
""".split())                             # review 08eea6e1 SHOULD 5: plural nouns ("Customers at x.com rate it ...")
_CV_TOKEN_AT = re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]++)([ \t\n]{1,3})(?:[Aa][Tt])(?=[ \t\n])")
CV_HOST_READ = 128
_CV_SPELLED_DOT = re.compile(r"(?i) ?[\[\(\{<] ?dot ?[\]\)\}>] ?| dot ")
_CV_HOST_START = re.compile(r"[ \t\n]*+([A-Za-z0-9\-]{1,63}+(?:\.[A-Za-z0-9\-]{1,63}+){0,8}+)")


def _cv_prose_word(tok: str) -> bool:
    words = globals()["CV_PROSE_BEFORE_AT"]
    if tok in words:
        return True
    return tok[:1].isupper() and tok[1:] == tok[1:].lower() and tok.lower() in words


def _cv_retailer_after(v: str, i: int) -> bool:
    """The domain written after the word "at" ending at ``i`` (read with the e-mail rules' dot forms) is a listed
    retailer host or a subdomain of one. Reads at most ``CV_HOST_READ`` characters (a listed host is at most 30)."""
    w = _DOT_JOIN.sub(".", _CV_SPELLED_DOT.sub(".", v[i:i + CV_HOST_READ]))
    m = _CV_HOST_START.match(w)
    if m is None:
        return False
    host = m.group(1).lower().strip(".")
    hosts = globals()["CV_RETAILER_HOSTS"]
    labels = host.split(".")
    return any(".".join(labels[k:]) in hosts for k in range(len(labels) - 1))


_CV_AT_SIGN = re.compile(r"(?i)[ \t\n]{1,3}sign(?![a-z])")
_CV_HOST_DEFANGED = re.compile(r"[ \t\n]*+[A-Za-z0-9\-]{1,63}+(?:[.\x01][A-Za-z0-9\-]{1,63}+){0,8}+")


def _cv_defanged_after(v: str, i: int) -> bool:
    """The domain written after the word "at" ending at ``i`` holds a defanged dot (``[.]``, ``(dot)``...): such a
    form is always masked (review 08eea6e1 SHOULD 5). Reads at most ``CV_HOST_READ`` characters."""
    w = v[i:i + CV_HOST_READ]
    if "[" not in w and "(" not in w and "{" not in w and "<" not in w:
        return False
    w = _DOT_JOIN.sub(".", _CV_SPELLED_DOT.sub(".", _DEFANG_DOT.sub("\x01", w)))
    m = _CV_HOST_DEFANGED.match(w)
    return m is not None and "\x01" in m.group(0)


# review 08eea6e1 SHOULD 5: "Buy it at amazon.com" / "Buy them at walmart dot com": these pronouns only before a listed
# retailer domain (every other pronoun and every role word stays masked everywhere)
CV_PRONOUNS_BEFORE_RETAILER = frozenset(("it", "them"))


def _cv_unprose_at(v: str) -> str:
    """``v`` with every spelled word "at" read as prose blanked to two NULs, so the spelled-at rules do not read it:
    after an ordinary prose word (``CV_PROSE_BEFORE_AT``) before ANY domain (review 08eea6e1 SHOULD 5, owner decision),
    or after "it" / "them" (``CV_PRONOUNS_BEFORE_RETAILER``) before a listed retailer domain (``CV_RETAILER_HOSTS``);
    never when the domain holds a defanged dot. Linear: a token starts only at the start of its run and is read
    possessively; each host read is bounded."""
    if "t" not in v and "T" not in v:
        return v

    def repl(m: re.Match) -> str:
        tok, j = m.group(1), m.end()
        if _cv_defanged_after(v, j) or _CV_AT_SIGN.match(v, j):
            return m.group(0)                              # a defanged dot or "at sign": never exempt
        if _cv_prose_word(tok) or (tok.lower() in globals()["CV_PRONOUNS_BEFORE_RETAILER"]
                                   and _cv_retailer_after(v, j)):
            return m.group(1) + m.group(2) + "\x00\x00"
        return m.group(0)
    return _CV_TOKEN_AT.sub(repl, v)


# Review c13529af UTILITY (owner decision, ADR 0051 decision 22): the client view does NOT read the PLAIN spelled form
# ``<word> at <domain>`` (one letters-only word, spaces, " at ", a domain written with literal dots and nothing spelled
# or defanged after it) as an e-mail: it is irreducibly ambiguous with prose ("Visit us at brand.com", "Get yours at
# shop.com", "Our team at brand.co.uk"). It still reads the literal "@", "[at]" / "(at)" / "{at}", defanged dots, the
# spelled "dot" ("x at y dot com"), the hyphen / underscore / glued variants, "at sign", the Spanish / French
# spellings, letter-spaced forms, and any spelled form whose local part holds a digit, ".", "_", "+" or "-". S1: a
# local part that is only digits and dots (a price: "$12.99 at", "12.99@") is never an e-mail here. The retailer /
# prose / role lists above (``CV_RETAILER_HOSTS``, ``CV_PROSE_BEFORE_AT``, ``CV_ROLE_WORDS``,
# ``CV_PRONOUNS_BEFORE_RETAILER``, ``_cv_unprose_at``) are now INERT: kept (tests still read them) but no rule calls
# them. The strict lane is unchanged (it withholds every one of these forms).
_CV_TOKEN_SEP = re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]++)(?:([ \t\n]{1,3})([Aa][Tt])(?=[ \t\n])|( ?)@)")
# Review c0c09bd1 MUST 5: the S1 price exemption is scoped. Only the PLAIN spelled " at " (never "@", "[at]", "(at)",
# "at sign", nor a domain written with a spelled / defanged dot) after a REAL price is exempt: a currency sign before
# it and / or a decimal part of exactly two digits, thousands commas allowed ("$12.99 at x.com", "$1,299 at x.com",
# "12.99 at x.com"); a bare count ("2 at x.com"), a digit-only local part before "@" and every other form are masked.
_CV_PRICE = re.compile(r"(?:[$\u00a2\u00a3\u00a5\u20ac] ?)?[0-9]{1,3}(?:,[0-9]{3})++(?:\.[0-9]{2})?|"
                       r"(?:[$\u00a2\u00a3\u00a5\u20ac] ?)?[0-9]++(?:\.[0-9]{2})?")
_CV_CURRENCY = frozenset("$\u00a2\u00a3\u00a5\u20ac")
CV_PRICE_READ = 24
_CV_WORD = re.compile(r"[A-Za-z]+")
# review c0c09bd1 SHOULD (owner view utility): a code / model number before the plain " at " ("use code SAVE10 at
# brand.com", "the X200 at amazon.com"): 2-12 characters of upper-case letters and digits, at least one of each, no
# lower case. A lower-case handle with digits ("jsmith42") stays masked.
_CV_CODE = re.compile(r"(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]{2,12}")
_CV_PLAIN_HOST = re.compile(r"[ \t\n]{1,3}[A-Za-z0-9\-]{1,63}+(?:\.[A-Za-z0-9\-]{1,63}+)++(?![A-Za-z0-9\-])")
_CV_HOST_GOES_ON = re.compile(r"(?i) ?[\[\(\{<] ?(?:d ?o ?t|\.|period|punto|point) ?[\]\)\}>]"
                              r"| ?(?:dot|period|punto|point)(?![a-z]) ?[a-z0-9]")


def _cv_real_price(v: str, s0: int, e0: int) -> bool:
    """The digits ``v[s0:e0]`` (a local-part token) end a real price: read back at most ``CV_PRICE_READ`` characters,
    the price must end exactly at ``e0``, start at a non-price character, and carry a currency sign or a two-digit
    decimal part."""
    a = max(0, s0 - CV_PRICE_READ)
    best = None
    for m in _CV_PRICE.finditer(v, a, e0):
        if m.end() == e0 and (m.start() == 0 or not (v[m.start() - 1].isalnum() or v[m.start() - 1] in ".,")):
            best = m
            break
    if best is None:
        return False
    t = best.group(0)
    return t[0] in _CV_CURRENCY or (len(t) > 3 and t[-3] == "." and t[-2:].isdigit())


def _cv_plain_at(v: str) -> str:
    """``v`` with every "at" the client view does not read as an e-mail blanked to NULs (decision 22, PROSE only: never
    inside a URL, review c0c09bd1): " at " after one letters-only word (not letter-spaced) or a code / model number
    before a plainly dotted domain, and the plain " at " after a real price (``_cv_real_price``). Linear: a token starts
    only at the start of its run and is read possessively; each host / price read is bounded."""
    if getattr(_CV_MEMO, "url", False):
        return v                                           # a URL part is never prose (review c0c09bd1 SHOULD)
    if "t" not in v and "T" not in v:
        return v

    def repl(m: re.Match) -> str:
        tok = m.group(1)
        if not m.group(3):
            return m.group(0)                              # "@": never exempt
        s0 = m.start(1)
        h = _CV_PLAIN_HOST.match(v, m.end(), m.end() + CV_HOST_READ)
        if h is None or _CV_HOST_GOES_ON.match(v, h.end()):
            return m.group(0)                              # not a plainly dotted domain after it
        if tok.replace(".", "").replace(",", "").isdigit():
            if globals()["_cv_real_price"](v, s0, m.end(1)):
                return tok + m.group(2) + "\x00\x00"       # S1 (scoped): a real price
            return m.group(0)
        if globals()["_CV_CODE"].fullmatch(tok):
            return tok + m.group(2) + "\x00\x00"           # a code / model number
        if not _CV_WORD.fullmatch(tok):
            return m.group(0)
        if len(tok) == 1 and s0 >= 2 and v[s0 - 1] == " " and v[s0 - 2] != " " and (s0 < 3 or v[s0 - 3] == " "):
            return m.group(0)                              # a letter-spaced local part ("j o h n at ...")
        return tok + m.group(2) + "\x00\x00"
    return _CV_TOKEN_SEP.sub(repl, v)


def _cv_email(v: str) -> bool:
    """An address (the strict lane's plain and split forms), ``name [at] domain [dot] tld`` (a dot part required, so
    ``meet us (at) the venue`` is prose), ``name at domain dot tld``, the spelled variants and the letter-spaced /
    split forms; not the plain ``<word> at <domain>`` form nor a price before "at" / "@" (``_cv_plain_at``)."""
    u = globals()["_cv_plain_at"](v)
    if "@" in u and _cv_at_email(u):
        return True
    low = u.lower()
    if ("at" in low or "a t" in low or "@" in u) and ("[" in u or "(" in u or "{" in u or "<" in u):  # [@] [.] too
        n = 0
        for m in _CV_BRACKET_AT.finditer(u):
            n += 1
            if n > EMAIL_AT_MAX:
                return True                                # fail closed
            tail = _CV_DOT.sub(".", u[m.end():m.end() + 300])
            if _CV_DOMAIN.match(tail) or globals()["_cv_wrapped_domain"](tail):
                return True
    d = _defanged(u)                                       # review 08eea6e1 MUST 1: "@" with defanged dots
    if d != u and "@" in d and _cv_at_email(d):
        return True
    views = _email_views(u)
    if len(views) == 1 and "at" not in low and "a t" not in low:
        return False                                       # no "at" and no variant / defanged view: nothing to read
    # the spelled forms, read as written, with defanged dots (MUST 1) and with the spelled variants (MUST 2): the
    # shared e-mail views of the strict lane
    for w in views:
        wl = w.lower()
        if _at_word_email(w):
            return True                                    # ``name at domain.tld`` (not the plain form: blanked)
        if " at " in wl and "dot" in wl and _spelled_email(w):
            return True
        if _spaced_join(w, CV_JOIN_STOP) and _spaced_spelled_email(w):
            return True                                    # letter-spaced / split words (review 601e328a MUST 3)
    return False


def _cv_wrapped_domain(tail: str) -> bool:
    """Review c0c09bd1 MUST 6 (client view): the domain after a bracketed "[at]" / "(at)" / "{at}" / "[a t]" written with
    a mid-word line break or NBSP (read as a space): the next words joined with no space one at a time (the strict
    lane's ``_joins_to_domain``, stopped at a common English word) until they read as a domain with a common top-level
    domain and a dot. Bounded: ``JOIN_WORDS`` words / 255 characters."""
    words: list = []
    for t in tail.lstrip(" ").split(" ")[:JOIN_WORDS]:
        if t.lower().strip(".,;:!?)]}>\"'") in CV_JOIN_STOP:
            break
        words.append(t)
    n = _joins_to_domain(words)
    return n > 1 and "." in "".join(words[:n])


def _cv_at_email(v: str) -> bool:
    """The strict lane's two ``@`` forms (2f): a plain address, or one split by spaces / line breaks (after each ``@``
    the next words joined until they read as a domain with a common top-level domain)."""
    if _EMAIL_PLAIN.search(re.sub(r" ?@ ?", "@", v)):
        return True
    for n, m in enumerate(re.finditer("@", v)):
        if n >= EMAIL_AT_MAX:
            return True                                    # fail closed
        i = m.start() - 1
        while i >= 0 and v[i] == " " and m.start() - i < 8:
            i -= 1
        if i < 0 or not _LOCAL_CHAR.fullmatch(v[i]):
            continue
        if _joins_to_domain(v[m.end():m.end() + 1024].lstrip(" ").split(" ")):
            return True
    return False


_CV_SPAN = re.compile(r"[0-9](?:(?:[^\w\ue000]|_){0,4}[0-9])*")
_CV_GROUP = re.compile(r"[0-9]+")
# review 601e328a MUST 5: a doubled separator (``xxx--xxx--xxxx``) is a phone too
_CV_NANP = re.compile(r"(?<![0-9])(?:\+?1[ .\-]?)?(?:\(\d{3}\) ?\d{3}-{1,2}\d{4}|\d{3}-{1,2}\d{3}-{1,2}\d{4}"
                      r"|\d{3}\.{1,2}\d{3}\.{1,2}\d{4})(?![0-9])")
_CV_NANP_SPACED = re.compile(r"(?<![0-9])(?:\+?1 )?[2-9][0-9]{2} [2-9][0-9]{2} [0-9]{4}(?![0-9])")   # "xxx xxx xxxx"
_CV_SSN_LABEL = re.compile(r"(?i)(?<![a-z])ssn(?![a-z])[\s:#=.\-]{0,4}(?<![0-9])(?P<d>[0-9]{9})(?![0-9])")
_CV_E164 = re.compile(r"(?<![\w+])\+ ?[1-9](?:[ .\-()]{0,3}[0-9]){7,14}(?![0-9])")


def _cv_card(d: str) -> bool:
    """A card number: a network's prefix and length (Visa, Mastercard, Amex, Discover, Diners, JCB, UnionPay), then
    Luhn."""
    n = len(d)
    if not 13 <= n <= 19 or d[0] not in "23456":
        return False
    p2, p3, p4 = int(d[:2]), int(d[:3]), int(d[:4])
    if d[0] == "4":
        ok = n in (13, 16, 19)
    elif 51 <= p2 <= 55 or 2221 <= p4 <= 2720:
        ok = n == 16
    elif p2 in (34, 37):
        ok = n == 15
    elif p4 == 6011 or 644 <= p3 <= 649 or p2 == 65:
        ok = 16 <= n <= 19
    elif 300 <= p3 <= 305 or p2 in (36, 38, 39):
        ok = 14 <= n <= 19
    elif 3528 <= p4 <= 3589 or p2 == 62:
        ok = 16 <= n <= 19
    else:
        ok = False
    return ok and _luhn(d)


def _cv_sep(x: str, extra: str = "") -> bool:
    """A number's own group separator: one or two of ``-`` ``.`` (and ``extra``)."""
    return 0 < len(x) <= 2 and all(c in "-." + extra for c in x)


# review 08eea6e1 SHOULD 3: the SSN 3-2-4 shape takes the strict lane's separator set (``_`` ``/`` ``||`` ``--`` ``:``
# ``~`` ``+`` ``*`` ``#`` ``=`` ``\`` ``,`` ...; 1-4 characters, spaces around them allowed) except list punctuation
# with a space (", " / "; ": "sizes 100, 20, 3000" is prose)
_CV_SSN_SEP = re.compile(r" ?(?:[^\w\s\ue000,;]|_|,){1,2} ?| ")


def _cv_ssn_sep(x: str) -> bool:
    return 0 < len(x) <= 4 and _CV_SSN_SEP.fullmatch(x) is not None and not (("," in x or ";" in x) and " " in x)


def _cv_ssn_ok(d: str) -> bool:
    area, group, serial = int(d[:3]), int(d[3:5]), int(d[5:])
    return 0 < area < 900 and area != 666 and group > 0 and serial > 0


def _cv_span(w: str, s: re.Match, out: set, kinds=CV_NUMBER_KINDS) -> int:
    """One digit span (digits with filler runs of at most four characters between them): card windows at group
    boundaries, the SSN shapes, a NANP number opened by ``(xxx)`` (only the rules named in ``kinds``). Returns the
    number of digit groups read."""
    span = s.group(0)
    gs = [(g.start(), g.end()) for g in _CV_GROUP.finditer(span)]
    if len(gs) > CV_GROUPS_MAX:
        out.add("cap")                                     # fail closed before reading thousands of groups
        return len(gs)
    seps = [span[gs[i][1]:gs[i + 1][0]] for i in range(len(gs) - 1)]
    digits = "".join(span[a:b] for a, b in gs)
    if len(digits) < 9:
        return len(gs)
    if "card" in kinds and "card" not in out:
        bounds = [0]
        for a, b in gs:
            bounds.append(bounds[-1] + b - a)
        for i in range(len(gs)):
            if digits[bounds[i]] not in "23456":
                continue
            for j in range(i + 1, len(bounds)):
                n = bounds[j] - bounds[i]
                if n > 19:
                    break
                if n >= 13 and _cv_card(digits[bounds[i]:bounds[j]]):
                    out.add("card")
                    break
            if "card" in out:
                break
    if "ssn" in kinds and "ssn" not in out:
        lens = [b - a for a, b in gs]
        for i in range(len(gs) - 2):
            if lens[i:i + 3] == [3, 2, 4] and _cv_ssn_sep(seps[i]) and _cv_ssn_sep(seps[i + 1]) \
                    and (i == 0 or not _cv_ssn_sep(seps[i - 1].strip())) \
                    and (i + 3 >= len(gs) or not _cv_ssn_sep(seps[i + 2].strip())):
                d = digits[sum(lens[:i]):sum(lens[:i + 3])]
                if _cv_ssn_ok(d):
                    out.add("ssn")
                    break
        if len(digits) == 9 and len(gs) >= 3 and all(x == "-" for x in seps) and _cv_ssn_ok(digits):
            out.add("ssn")                                 # a 3-2-4 number with more dashes put in
    if "phone" in kinds and "phone" not in out and len(digits) == 10 and gs[0][1] - gs[0][0] == 3 and seps and seps[0].startswith(")") \
            and s.start() > 0 and w[s.start() - 1] == "(":
        out.add("phone")                                   # (xxx) and seven digits, however grouped
    return len(gs)


def _cv_numbers(v: str) -> set:
    out = _cv_numbers_on(v)
    nw = _number_word_view(v)                              # review 08eea6e1 NIT: number words read as digits
    if nw != v:
        out |= _cv_numbers_on(nw)
        out |= globals()["_cv_number_words"](v)
    return out


def _cv_number_words(v: str) -> set:
    """Review c0c09bd1 MUST 7: a run of number words (the strict lane's ``_NUMBER_WORD_RUN``, split words re-joined)
    read as ONE number whatever separates its words (the digit view loses the spoken grouping): nine digits that are a
    valid SSN, ten that are a NANP number (or eleven with a leading 1)."""
    out: set = set()
    w = _rejoin(globals()["_SPLIT_NUMBER"], v)
    for m in _NUMBER_WORD_RUN.finditer(w):
        d = "".join(NUMBER_WORDS[x.lower()] for x in _NW_EACH.findall(m.group(0)))
        if len(d) == 9 and _cv_ssn_ok(d):
            out.add("ssn")
        n = d[1:] if len(d) == 11 and d[0] == "1" else d
        if len(n) == 10 and n[0] in "23456789" and n[3] in "23456789":
            out.add("phone")
    return out


_NW_EACH = re.compile(r"(?i)" + _NW)
# review c0c09bd1 MUST 8: an unformatted phone number (10-15 digits, at most two of " .-()" between digits) with one
# of the strict lane's phone words ("call", "tel:", "phone", "mobile", "cell", "whatsapp", "text", "sms"...;
# ``_PHONE_BEFORE``) within ``PHONE_CONTEXT`` characters before it
_CV_PHONE_RUN = re.compile(r"(?<![0-9])\+?[0-9](?:[ .\-()]{0,2}[0-9]){9,14}(?![0-9])")


def _cv_labelled_phone(m: str) -> bool:
    return any(_phone_context(m, x.start()) for x in _CV_PHONE_RUN.finditer(m))


def _cv_numbers_on(v: str) -> set:
    out: set = set()
    if sum(map(v.count, _ASCII_DIGITS)) < 8:
        return out
    m = _masked(v)
    if any(_cv_ssn_ok(x.group("d")) for x in _CV_SSN_LABEL.finditer(m)):
        out.add("ssn")                                     # "SSN" and nine bare digits (review adacb2ca SHOULD 3)
    if globals()["_cv_labelled_phone"](m):
        out.add("phone")                                   # review c0c09bd1 MUST 8: a labelled unformatted phone
    for w in (m, m.replace(" ", "")) if " " in m else (m,):
        # review adacb2ca MUST 2: filler words between digit groups ("then", "followed by", "and", "plus", "next")
        # bridged as the strict lane bridges them (review 601e328a MUST 2: groups of 2-6 digits). The card rule (a
        # network's prefix and Luhn) reads every bridged word; the SSN and phone rules, which have no check digit, read
        # only the listed filler words (``CV_FILLER_WORDS``), so "12 for 345 dollars then 678" is not a phone
        ub = _unbridged(w, " ")
        fb = _unbridged_fillers(w, " ")
        for x in (w, fb) if fb != w else (w,):
            if _CV_NANP.search(x) or _CV_E164.search(x) or _CV_NANP_SPACED.search(x):
                out.add("phone")
        groups = 0
        passes = ((ub, CV_NUMBER_KINDS),) if ub == fb else ((ub, ("card",)), (fb, ("ssn", "phone")))
        for x, kinds in passes:
            for s in _CV_SPAN.finditer(x):
                groups += _cv_span(x, s, out, kinds)
                if groups > CV_GROUPS_MAX or "cap" in out:
                    out.add("cap")                         # fail closed: a field of thousands of numbers is masked
                    return out
    return out


_CV_CHECKS = (("strong_key", "_cv_strong"), ("userinfo", "_cv_userinfo"), ("label_value", "_cv_label"),
              ("email", "_cv_email"))


def _cv_label_nospace(v: str) -> bool:
    """The label rule on the strict lane's letter-spaced view (``a p i _ k e y = value``: each run of single
    characters joined, as ``_spaced_label`` reads it; other words keep their spaces, so a word before the label is not
    glued to it)."""
    if " " not in v:
        return False
    sv = _singles_view(v)
    return sv != v and _cv_label(sv)


def _cv_findings(text: str) -> set:
    """Every client-view rule on every (distinct) inspection view. Review c13529af MUST 6: a rule already found is not
    read again, the strong-prefix scans are remembered for the call (an identical string is scanned once), and a field
    found ``cap`` (fail closed: masked whatever else is in it) is not read further."""
    found: set = set()
    marked = False
    _CV_MEMO.prefix = {}
    try:
        for v, folded in _tagged_views(text):
            if not v:
                continue
            # review cbdf5e78: a script label is read on a view as written, never on its ASCII-folded form (the
            # strict lane's label rule skips that form too): a letter folded to a Latin lookalike there would stand
            # as a change of script next to the label's letters
            _CV_MEMO.folded = folded
            for name, fn in _CV_CHECKS:
                if name not in found and globals()[fn](v):
                    found.add(name)
            if "label_value" not in found and globals()["_cv_label_nospace"](v):
                found.add("label_value")                   # review 601e328a MUST 5: the strict no-space view
            if "label_value" not in found and not folded and not marked and _marked_candidate(v):
                marked = True                              # the marked views are read once (section 2a)
                if globals()["_cv_marked"](text):
                    found.add("label_value")
            if not {"card", "ssn", "phone"} <= found:
                found |= globals()["_cv_numbers"](v)
            if "cap" in found:
                break
    finally:
        _CV_MEMO.prefix = None
        _CV_MEMO.folded = False
    return found


def client_view_findings(text) -> list:
    """The client view's HIGH-PRECISION findings on ``text`` (sorted names: ``strong_key``, ``userinfo``,
    ``label_value``, ``card``, ``ssn``, ``phone``, ``email``; ``cap`` / ``error`` / ``type`` fail closed), or [] when the
    owner may see it in full. For the owner's decrypted view and its exports ONLY (ADR 0051 job 3): logs, outbound
    data and cross-tenant surfaces use ``check`` (the strict lane). Never raises."""
    if text is None:
        return []
    if not isinstance(text, str):
        return ["type"]
    if not text:
        return []
    if len(text) > MAX_INPUT:
        return ["cap"]
    try:
        return sorted(_cv_findings(text))
    except Exception:  # noqa: BLE001 - fail closed: a field the checks cannot read is masked
        return ["error"]


def client_view_kind(findings) -> str | None:
    """``personal`` when every finding is a personal-data one, ``credential`` when any other is present, else None."""
    f = set(findings or ())
    if not f:
        return None
    return PERSONAL if f <= CV_PERSONAL else CREDENTIAL


# URL query keys whose value is a credential (compared lower case with every non-letter / digit removed)
CV_QUERY_KEYS = frozenset(("token", "key", "apikey", "secret", "sig", "signature", "password", "pwd", "passwd", "pass",
                           "pw", "pin", "phpsessid",
                           "auth", "accesstoken", "clientsecret", "code", "session", "sid", "sessionid", "authtoken",
                           "idtoken", "refreshtoken", "accesskey", "secretkey", "privatekey", "xapikey",
                           "authorization", "bearer", "jwt", "otp", "credential", "credentials"))
CV_QUERY_SUFFIXES = ("token", "secret", "password", "passwd", "apikey", "accesskey", "secretkey", "privatekey",
                     "signature", "sessionid", "credential")
# analytics / tracking keys: their values are random ids by design, so the high-entropy rule does not apply to them
# (every other client-view finding still does)
CV_ANALYTICS_KEYS = TRACKING_PARAMS | frozenset((
    "_kx", "ef_id", "spm", "cid", "clickid", "click_id", "irgwc", "_branch_match_id", "oly_enc_id", "oly_anon_id",
    "vero_id", "rb_clickid", "__hstc", "__hssc", "__hsfp", "_hsq", "hsctatracking", "trk", "trkcampaign", "_bta_tid",
    "_bta_c", "wickedid", "obclid", "fb_action_ids", "gad_source", "gad_campaignid", "_gac",
    "ved", "ei", "sca_esv", "gs_lcrp", "oq", "aqs"))                 # Google search's own (review adacb2ca SHOULD 4)
CV_ANALYTICS_PREFIXES = ("utm_", "hsa_", "pk_", "mtm_", "_ga")
_CV_TOKEN_VALUE = re.compile(r"[A-Za-z0-9_\-+/=.~]+")
_CV_HEX40 = re.compile(r"[0-9a-fA-F]{40,}")


# session-id keys matched by their EXACT name only (review adacb2ca SHOULD 4): Shopify search's ``_sid`` is not one
CV_SID_KEYS = frozenset(("sid", "sessionid", "session_id", "jsessionid", "phpsessid"))


def _cv_query_key(k: str) -> bool:
    low = k.lower()
    n = re.sub(r"[^a-z0-9]", "", low)
    if n == "sid":
        return low in CV_SID_KEYS
    return n in CV_QUERY_KEYS or n.endswith(CV_QUERY_SUFFIXES)


def _cv_analytics(k: str) -> bool:
    k = k.lower()
    return k in CV_ANALYTICS_KEYS or k.startswith(CV_ANALYTICS_PREFIXES)


def _cv_entropy(v: str) -> bool:
    """A high-entropy token value: 32+ characters mixing upper case, lower case and digits, or 40+ hex digits."""
    if len(v) < 32 or not _CV_TOKEN_VALUE.fullmatch(v):
        return False
    up, lo, dg = _classes(v)
    return (up and lo and dg) or bool(_CV_HEX40.fullmatch(v))


def _cv_host_key(label: str) -> bool:
    """A host label holding a strong-key prefix (host names are case-insensitive: read as written, upper-cased, and
    with only its first two letters upper-cased)."""
    return any(_prefix_found(x) for x in (label, label.upper(), label[:2].upper() + label[2:])) \
        or bool(_AWS_JOINED.search(_NOSEP.sub("", label.upper())))


def _cv_pairs(raw: str, kinds: set) -> str:
    """A query (or a ``k=v`` fragment): each value masked on its own (``{hidden}``), the rest kept as written."""
    pairs = raw.split("&")
    if len(pairs) > CV_URL_PARTS_MAX:
        raise ValueError("too many query pairs")
    out = []
    for pair in pairs:
        k_raw, eq, v_raw = pair.partition("=")
        k, v = unquote_plus(k_raw), unquote_plus(v_raw)
        fk = client_view_findings(k) if k else []
        if fk:
            kinds.add(client_view_kind(fk))
            out.append(CLIENT_HIDDEN)                      # the key itself carries a value: the whole pair
            continue
        kind = None
        if v and _cv_query_key(k):
            kind = CREDENTIAL
        elif v:
            kind = client_view_kind(client_view_findings(v))
            if kind is None and not _cv_analytics(k) and _cv_entropy(v):
                kind = CREDENTIAL
        if kind is None:
            out.append(pair)
        else:
            kinds.add(kind)
            out.append(k_raw + eq + CLIENT_HIDDEN)
    return "&".join(out)


def _cv_seg_findings(seg: str) -> list:
    """A path segment's findings, and ``label_value`` for a ``;key=value`` path parameter under a credential-like key
    (``;jsessionid=...``; review adacb2ca SHOULD 3)."""
    f = client_view_findings(seg)
    if ";" in seg and not f:
        for prm in seg.split(";")[1:CV_URL_PARTS_MAX]:
            k, eq, val = unquote_plus(prm).partition("=")
            if eq and val and _cv_query_key(k):
                return ["label_value"]
    return f


def _cv_url(url: str) -> tuple:
    p = urlsplit(url)
    host = p.hostname or ""
    if not p.scheme or not p.netloc or not host:
        k = client_view_kind(client_view_findings(url))
        return (url, None) if k is None else (None, k)
    kinds: set = set()
    ui, at, hostport = p.netloc.rpartition("@")
    netloc = p.netloc
    if at and ((":" in ui and ui.split(":", 1)[1]) or client_view_findings(unquote(ui))):
        kinds.add(CREDENTIAL if ":" in ui else client_view_kind(client_view_findings(unquote(ui))))
        netloc = CLIENT_HIDDEN + "@" + hostport
    for part in [host] + host.split("."):
        if part and (_cv_host_key(part) or client_view_findings(part)):
            k = client_view_kind(client_view_findings(part)) or CREDENTIAL
            return None, k                                 # a host carrying a value: the whole URL is masked
    segs = (p.path or "").split("/")
    seg_f = [_cv_seg_findings(s) if s else [] for s in segs]
    # review c0c09bd1 SHOULD: the segment after a label-word-alone segment carries the label's value
    seg_f = [f or (["label_value"] if after_label(segs, i) else []) for i, f in enumerate(seg_f)]
    kept = [CLIENT_HIDDEN if f else s for s, f in zip(segs, seg_f)]
    for f in seg_f:
        if f:
            kinds.add(client_view_kind(f))
    # a value split across segments: the path read joined by "/" and glued; a finding no single segment has masks every
    # segment
    seen = set().union(*seg_f) if seg_f else set()
    joined = set(client_view_findings("/".join(segs))) | _cv_glued(segs)
    if joined - seen:
        kinds.add(client_view_kind(joined - seen))
        kept = [CLIENT_HIDDEN if s else s for s in segs]
    query = _cv_pairs(p.query, kinds) if p.query else ""
    frag = p.fragment
    if frag:
        if "=" in frag:
            frag = _cv_pairs(frag, kinds)
        else:
            fk = client_view_kind(client_view_findings(frag) or (client_view_findings(unquote(frag))
                                                                 if FRAGMENT_EXTRA_DECODE else []))
            if fk is not None:
                kinds.add(fk)
                frag = CLIENT_HIDDEN
    kinds.discard(None)
    if not kinds:
        return url, None
    shown = urlunsplit((p.scheme, netloc, "/".join(kept), query, frag))
    return shown, (CREDENTIAL if CREDENTIAL in kinds else PERSONAL)


_CV_GLUED_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.(?:" + "|".join(_TLDS)
                             + r")(?![A-Za-z0-9])", re.IGNORECASE)


def _cv_glued(segs: list) -> set:
    """The path's segments glued with nothing between them (a value split across segments): every finding but a
    label (no separator survives the glue) and an e-mail only with a common top-level domain (``pkg@1.2.3/dist/x.js``
    glued is not an address)."""
    glued = "".join(segs)
    found = set(client_view_findings(glued)) - {"label_value", "email"}
    if any("@" in v and _CV_GLUED_EMAIL.search(v) for v in views(glued)):
        found.add("email")
    return found


def client_view_url(url) -> tuple:
    """(shown, kind) for the owner's view of ``url`` (ADR 0051 job 3): ``(url, None)`` when nothing is masked (the
    URL exactly as given); else ``kind`` is ``credential`` or ``personal`` and ``shown`` is the URL with only the
    offending parts replaced by ``{hidden}`` (a path segment, a query or ``k=v`` fragment value, the user info), or
    None when the whole URL is masked (a host label carrying a value, a URL that cannot be read, over the caps).
    Query values are masked under credential-like keys, and when high-entropy under any key but analytics / tracking
    ones. For the owner's view and its exports ONLY; never raises."""
    if url is None:
        return None, None
    if not isinstance(url, str) or len(url) > URL_MAX_INPUT:
        return None, CREDENTIAL
    if not url:
        return url, None
    prev = getattr(_CV_MEMO, "url", False)
    _CV_MEMO.url = True                    # review c0c09bd1 SHOULD: decision 22 (and the price rule) is for PROSE only
    try:
        return _cv_url(url)
    except Exception:  # noqa: BLE001 - fail closed
        return None, CREDENTIAL
    finally:
        _CV_MEMO.url = prev
