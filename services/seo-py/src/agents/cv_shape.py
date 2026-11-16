"""Client view: label-independent value-shape mask (core-spec/2 CV-1..CV-4; thresholds Q2-4 [OWNER DEFAULT]).

CV-1 canonical view (v1 N-1: NFKC, Cf / Cc removed, Unicode spaces and line separators read as one space, Unicode
dashes read ``-``, marks kept) with a map from every canonical character back to its input range; CV-2 tokens, cores,
joiners; CV-3 value-shape rule (VS) plus the K-1 / K-2 / K-5 shapes; CV-4 joined-run rule (JR). The label is never
needed and never masked by VS. ``exempt_shape`` (CV-6) is the ONE entry point for the exempt list (card C2-8 fills it).
Inspection only: nothing here is emitted; spans are (start, end) offsets into the caller's input."""
from __future__ import annotations

import re
import unicodedata

from agents import cv_allowlist                          # AL-1 (C2-15); imports nothing of cv_shape at load time

TOKEN_MIN = 8                    # CV-3: raw token length (code points) for the VS length test
PIECE_MAX = 24                   # CV-4: a fragment is 1..24 code points
RUN_MAX = 64                     # CV-4: the concatenation of a run is at most 64 code points
J = frozenset("  \t-_./:+|,")                       # CV-4: the 11 separator characters
JN = frozenset("-'’._")                             # CV-2: joiners (letters only between two letters)
W_L = frozenset("\"'`([{<*")                             # CV-2: leading wrap characters stripped from the core
W_T = frozenset("\"'`)]}>*,.;:!?")                       # CV-2: trailing wrap characters stripped from the core
_LINE = frozenset("\t\n\r\x0b\x0c\x1c\x1d\x1e\x1f\x85")  # control code points that are line / space separators
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


_D = r"[0-9]"                                            # ASCII digits (the view is NFKC: fullwidth digits fold here)
_NUM = _D + r"+(?:[.,]" + _D + r"+)?"
_UNITS = ("kg", "g", "mm", "cm", "m", "km", "gb", "tb", "mb", "hz", "khz", "mhz", "ghz", "w", "kw", "v", "mah", "in",
          "ft", "lb", "lbs", "oz", "ml", "l", "px", "k", "p", "fps", "mp")
_AMOUNT = (_D + r"{1,3}(?:," + _D + r"{3})+(?:\." + _D + r"{1,2})?|" + _D + r"{1,3}(?:\." + _D + r"{3})+(?:,"
           + _D + r"{1,2})?|" + _D + r"+(?:[.,]" + _D + r"{1,2})?")
ISO_4217 = frozenset("""aed afn all amd ang aoa ars aud awg azn bam bbd bdt bgn bhd bif bmd bnd bob bov brl bsd btn
bwp byn bzd cad cdf che chf chw clf clp cny cop cou crc cuc cup cve czk djf dkk dop dzd egp ern etb eur fjd fkp gbp gel
ghs gip gmd gnf gtq gyd hkd hnl htg huf idr ils inr iqd irr isk jmd jod jpy kes kgs khr kmf kpw krw kwd kyd kzt lak lbp
lkr lrd lsl lyd mad mdl mga mkd mmk mnt mop mru mur mvr mwk mxn mxv myr mzn nad ngn nio nok npr nzd omr pab pen pgk php
pkr pln pyg qar ron rsd rub rwf sar sbd scr sdg sek sgd shp sle sll sos srd ssp stn svc syp szl thb tjs tmt tnd top try
ttd twd tzs uah ugx usd usn uyi uyu uyw uzs ved ves vnd vuv wst xaf xag xau xba xbb xbc xbd xcd xcg xdr xof xpd xpf xpt
xsu xts xua xxx yer zar zmw zwg zwl""".split())    # ISO 4217 alphabetic codes (the "ISO code" of CV-6 amount)
_EXEMPT = (                                              # CV-6, closed and frozen; each matched on the WHOLE core
    ("iso_date", _D + r"{4}-" + _D + r"{2}-" + _D + r"{2}(?:t" + _D + r"{2}:" + _D + r"{2}(?::" + _D + r"{2}(?:\."
     + _D + r"+)?)?(?:z|[+-]" + _D + r"{2}(?::?" + _D + r"{2})?)?)?"),
    ("dmy_date", _D + r"{1,2}[-/.]" + _D + r"{1,2}[-/.]" + _D + r"{2,4}"),
    ("ymd_slash", _D + r"{4}/" + _D + r"{2}/" + _D + r"{2}"),
    ("time", _D + r"{1,2}:" + _D + r"{2}(?::" + _D + r"{2})?(?:am|pm)?"),
    ("amount", r"(?:CUR)?(?:" + _AMOUNT + r")|(?:" + _AMOUNT + r")CUR"),
    ("percent", _NUM + "%"),
    ("version", r"v?" + _D + r"+(?:\." + _D + r"+){1,3}"),
    ("ordinal", _D + r"+(?:st|nd|rd|th)"),
    ("range", _D + r"+-" + _D + r"+"),
    ("dimension", _D + r"+[x×]" + _D + r"+(?:[x×]" + _D + r"+)?[a-z]{0,3}"),
    ("unit", _NUM + r"(?:" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"),
    ("tyre", _D + r"{3}/" + _D + r"{2}z?r ?" + _D + r"{2}"),
)


def _cur_class() -> str:
    """Every Unicode currency sign (Sc) as one character class, or an ISO 4217 code (case-folded)."""
    import sys
    sc = "".join(chr(i) for i in range(sys.maxunicode + 1) if unicodedata.category(chr(i)) == "Sc")
    return "(?:[" + re.escape(sc) + "]|" + "|".join(sorted(ISO_4217)) + ")"


_CUR_RX = _cur_class()
EXEMPT_RX = tuple((name, re.compile(rx.replace("CUR", _CUR_RX))) for name, rx in _EXEMPT)
_ISBN_PARTS = re.compile(r"[0-9]+(?:-[0-9]+)*(?:-?x)?")


def _isbn_ean(c: str) -> bool:
    """ISBN-13 / EAN-13 (mod 10, weights 1 / 3) or ISBN-10 (mod 11, last character may be x), hyphens allowed between
    digit groups; valid checksum only."""
    if not _ISBN_PARTS.fullmatch(c):
        return False
    d = c.replace("-", "")
    if len(d) == 13 and d.isdigit():
        return sum(int(x) * (1 if k % 2 == 0 else 3) for k, x in enumerate(d)) % 10 == 0
    if len(d) == 10 and d[:9].isdigit() and (d[9].isdigit() or d[9] == "x"):
        vals = [int(x) for x in d[:9]] + [10 if d[9] == "x" else int(d[9])]
        return sum((10 - k) * x for k, x in enumerate(vals)) % 11 == 0
    return False


def exempt_name(core: str):
    """The CV-6 shape ``core`` is (closed list, case-folded, whole core), or None."""
    c = core.casefold()
    if not c:
        return None
    for name, rx in EXEMPT_RX:
        if rx.fullmatch(c):
            return name
    return "isbn_ean" if _isbn_ean(c) else None


def exempt_shape(core: str) -> bool:
    """CV-6 exempt shapes (closed, frozen, case-folded, on the core only): the ONE entry point. Nothing else is exempt
    (UUIDs, hashes, SKUs, model numbers, order ids, flight numbers, coupon codes are NOT)."""
    return exempt_name(core) is not None


def _space(c: str) -> bool:
    return c in _LINE or unicodedata.category(c) in ("Zs", "Zl", "Zp")


def canonical(text: str) -> tuple:
    """CV-1: (view, src) where ``src[k]`` = (start, end) of the input characters canonical character ``k`` came from.
    A base character and the marks after it are normalised together (NFKC composes them); runs of spaces read as one
    space, leading / trailing spaces dropped."""
    out: list = []
    src: list = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        j = i + 1
        if _space(c):
            piece = " "
        elif unicodedata.category(c) in ("Cf", "Cc"):
            i = j
            continue
        else:
            while j < n and unicodedata.category(text[j])[0] == "M":
                j += 1
            piece = unicodedata.normalize("NFKC", text[i:j])
        for ch in piece:
            cat = unicodedata.category(ch)
            if _space(ch):
                if not out or out[-1] == " ":
                    continue
                ch = " "
            elif cat in ("Cf", "Cc"):
                continue
            elif cat == "Pd":
                ch = "-"
            out.append(ch)
            src.append((i, j))
        i = j
    if out and out[-1] == " ":
        out.pop()
        src.pop()
    return "".join(out), tuple(src)


def _letter(c: str) -> bool:
    return unicodedata.category(c)[0] in "LM"            # any script, marks included (CV-3)


def classes(core: str) -> tuple:
    """CV-3: (letter, digit, ascii_symbol) present in ``core``; a joiner of JN between two letters counts as a
    letter (CV-2), every other ASCII non-alphanumeric character is a symbol; a digit is any Nd."""
    lt = dg = sy = False
    last = len(core) - 1
    for k, c in enumerate(core):
        cat = unicodedata.category(c)
        if cat[0] in "LM":
            lt = True
        elif cat == "Nd":
            dg = True
        elif c.isascii() and not c.isalnum():
            if c in JN and 0 < k < last and _letter(core[k - 1]) and _letter(core[k + 1]):
                lt = True
            else:
                sy = True
    return lt, dg, sy


def core_bounds(tok: str) -> tuple:
    """CV-2: (a, b) such that ``tok[a:b]`` is the core (W_L stripped from the left, W_T from the right)."""
    a, b = 0, len(tok)
    while a < b and tok[a] in W_L:
        a += 1
    while b > a and tok[b - 1] in W_T:
        b -= 1
    return a, b


def core(tok: str) -> str:
    a, b = core_bounds(tok)
    return tok[a:b]


def strong(c: str) -> bool:
    """CV-3 'also valueish whatever the length test': K-1 strong-key prefixes, K-2 token shapes (on each ASCII
    alphanumeric run of the core), K-5 shapes (card, SSN, phone, userinfo, e-mail) - the v1 checks, unchanged."""
    if not c:
        return False
    from agents import secret_guard as sg                # lazy: secret_guard imports this module
    if sg._cv_prefix_found(c):                           # C2-CL4: through the per-call memo (c13529af MUST 6)
        return True
    if len(c) >= sg.MIXED_MIN and any(sg._random_run(r) for r in _ALNUM_RUN.findall(c)):
        return True
    return bool(sg._cv_numbers(c)) or sg._cv_email(c) or sg._cv_userinfo(c)


def valueish(raw: str) -> bool:
    """CV-3 VS on one token (raw = the token of the canonical view, before the core is taken)."""
    c = core(raw)
    if not c:
        return False
    if strong(c):
        return True
    if len(raw) < TOKEN_MIN or sum(classes(c)) < 2:
        return False
    return not exempt_shape(c)


def fragment(piece: str) -> bool:
    """CV-4: a piece whose core holds a digit or an ASCII symbol (joiners between letters excluded), 1..24 long."""
    if not 1 <= len(piece) <= PIECE_MAX:
        return False
    _lt, dg, sy = classes(core(piece))
    return dg or sy


def tokens(v: str):
    """CV-2: (start, end) of each maximal run of non-space characters of the canonical view."""
    k, n = 0, len(v)
    while k < n:
        if v[k] == " ":
            k += 1
            continue
        e = v.find(" ", k)
        e = n if e < 0 else e
        yield k, e
        k = e


def pieces(v: str) -> list:
    """CV-4: (start, end) of each maximal run of characters outside J."""
    out, k, n = [], 0, len(v)
    while k < n:
        if v[k] in J:
            k += 1
            continue
        e = k
        while e < n and v[e] not in J:
            e += 1
        out.append((k, e))
        k = e
    return out


def _plain(sub: str) -> bool:
    """A sub-token that keeps a run shown: an exempt shape, digits-only or letters-only (CV-4)."""
    if sub and (all(unicodedata.category(c) == "Nd" for c in sub) or all(_letter(c) for c in sub)):
        return True
    c = core(sub)
    return bool(c) and exempt_shape(c)


def vs_tokens(v: str) -> list:
    """CV-3: (start, end) of each CV-2 token of ``v`` that VS masks (valueish), in order."""
    return [(a, b) for a, b in tokens(v) if valueish(v[a:b])]


def _joinable(v: str, p: tuple, masked: list | None) -> bool:
    """CV-4 fragment for joining. With ``masked`` (OWNER DECISION 2026-11-15 ~10:00 PT "Mask more", C2-13c/C2-13d):
    a piece longer than PIECE_MAX whose CV-2 token CV-3 already masks also COUNTS as a fragment for joining (a piece
    never crosses a space, so it lies inside one token). Without ``masked``: the CV-4 rule as written."""
    if fragment(v[p[0]:p[1]]):
        return True
    return masked is not None and p[1] - p[0] > PIECE_MAX and any(a <= p[0] and p[1] <= b for a, b in masked)


def runs(v: str, masked: list | None = None):
    """CV-4: (start, end) of each maximal run of >= 2 fragments separated by exactly one J character. ``masked`` =
    ``vs_tokens(v)`` selects the 10:00 PT joining (see ``_joinable``); None = the CV-4 rule as written."""
    cur: list = []
    for p in pieces(v) + [None]:
        ok = p is not None and _joinable(v, p, masked)
        if ok and (not cur or p[0] == cur[-1][1] + 1):
            cur.append(p)
            continue
        if len(cur) >= 2:
            yield cur[0][0], cur[-1][1], tuple(cur)
        cur = [p] if ok else []


def run_masked(v: str, ps: tuple) -> bool:
    joined = "".join(v[a:b] for a, b in ps)
    if len(joined) > RUN_MAX or not valueish(joined):
        return False
    text = v[ps[0][0]:ps[-1][1]]
    return not all(_plain(s) for s in text.split(" "))


def view_spans(v: str, hand: list | None = None) -> list:
    """Masked spans on the canonical view: every VS token (its core) and every masked JR run, merged. A token handed
    to the URL lane or the e-mail check (CV-7) is read as spaces here, so neither VS nor JR masks it. ``hand`` =
    ``handed(v)`` when the caller already has it (C2-CL4: computed once per call)."""
    if hand is None:
        hand = handed(v)
    if hand:
        w = list(v)
        for a, b, _lane in hand:
            w[a:b] = " " * (b - a)
        v = "".join(w)
    found = []
    masked = vs_tokens(v)
    for a, b in masked:
        x, y = core_bounds(v[a:b])
        found.append((a + x, a + y))
    # Lead ruling 10:40 PT (F13c-1): "mask more" is MONOTONIC - the JR mask is the UNION of the CV-4 runs as written
    # and the runs joined over CV-3-masked long pieces (10:00 PT); each run is bounded by RUN_MAX / VS on its own, so
    # a joined run that fails the limit is simply not added and never removes an old run's mask.
    for sel in (None, masked):
        for a, b, ps in runs(v, sel):
            if run_masked(v, ps):
                found.append((a, b))
    found.sort()
    merged: list = []
    for a, b in found:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def value_spans(text: str) -> list:
    """CV-1..CV-4 on ``text``: masked spans as (start, end) offsets into ``text`` (label-independent)."""
    if not text:
        return []
    v, src = canonical(text)
    return _to_input(text, v, src, view_spans(v))


def _to_input(text: str, v: str, src: tuple, spans: list) -> list:
    """View spans -> input offsets; inside ``cv_allowlist.scope`` a span listed for the scoped tenant (AL-1: code of
    its exact input text in the tenant's set, never K-1 / K-5) is shown, so it is not returned."""
    out = [(src[a][0], src[b - 1][1], a, b) for a, b in spans]
    if out and cv_allowlist.active():
        kept: list = []
        for s in out:
            if not cv_allowlist.shown(text[s[0]:s[1]], v[s[2]:s[3]]):
                kept += _split_listed(text, v, src, s)
        out = kept
    return [(x, y) for x, y, _a, _b in out]


def _split_listed(text: str, v: str, src: tuple, s: tuple) -> list:
    """AL-1 on a merged span (C2-13e, F13c-4 / F13d-1): a span covering more than one CV-2 token shows each WHOLE
    token core inside it whose exact input text is listed (``cv_allowlist.shown``: never K-1 / K-5); the rest of the
    span stays masked (spaces at the cut edges are not part of a value). A span that holds a K-1 / K-5 value as a
    whole exempts nothing; a token glued into a larger CV-2 token is never cut out."""
    a, b = s[2], s[3]
    if " " not in v[a:b] or cv_allowlist._never(v[a:b]):
        return [s]
    cuts = []
    for ta, tb in tokens(v):
        if tb <= a or ta >= b:
            continue
        x, y = core_bounds(v[ta:tb])
        ca, cb = ta + x, ta + y
        if ca < cb and a <= ca and cb <= b and cv_allowlist.shown(text[src[ca][0]:src[cb - 1][1]], v[ca:cb]):
            cuts.append((ca, cb))
    if not cuts:
        return [s]
    out, k = [], a
    for ca, cb in cuts + [(b, b)]:
        p, q = k, ca
        while p < q and v[p] == " ":
            p += 1
        while q > p and v[q - 1] == " ":
            q -= 1
        if p < q:
            out.append((src[p][0], src[q - 1][1], p, q))
        k = cb
    return out


_URL_START = re.compile(r"(?i)(?:https?://|www\.)")


def handed(v: str) -> list:
    """CV-7: (start, end, lane) of each token of the canonical view handed to another lane: ``url`` when its core
    starts a URL (``http(s)://`` or ``www.``, as the v1 URL-in-text rule), ``email`` when its core is a K-5 e-mail."""
    from agents import secret_guard as sg                # lazy: secret_guard imports this module
    out = []
    for a, b in tokens(v):
        x, y = core_bounds(v[a:b])
        c = v[a + x:a + y]
        if not c:
            continue
        if _URL_START.match(c):
            out.append((a, b, "url"))
        elif "@" in c and sg._cv_email(c):
            out.append((a, b, "email"))
    return out


def url_tokens(text: str) -> list:
    """CV-7: the URL tokens of ``text`` (their cores) for ``client_view_url`` (U-1): the canonical form and, when it
    differs, the input characters it came from (both are judged; the worse verdict counts)."""
    if not text:
        return []
    v, src = canonical(text)
    return _url_tokens(text, v, src, handed(v))


def _url_tokens(text: str, v: str, src: tuple, hand: list) -> list:
    out = []
    for a, b, lane in hand:
        if lane != "url":
            continue
        x, y = core_bounds(v[a:b])
        cv = v[a + x:a + y]
        raw = text[src[a + x][0]:src[a + y - 1][1]]
        out.append(cv)
        if raw != cv:
            out.append(raw)
    return out


def spans_and_urls(text: str) -> tuple:
    """(``value_spans(text)``, ``url_tokens(text)``) from ONE canonical view and ONE CV-7 hand-off pass (C2-CL4 /
    F-CL1: the client view used to build both twice per call). Same results as the two calls, in the same order."""
    if not text:
        return [], []
    v, src = canonical(text)
    hand = handed(v)
    spans = _to_input(text, v, src, view_spans(v, hand))
    return spans, _url_tokens(text, v, src, hand)


def display(text: str, mask: str) -> tuple:
    """CV-5: (rendered, spans). Each VS / JR span of ``text`` is replaced by ``mask`` (the v1 string); ``spans`` lists
    {start, end, code} in input offsets with code = B-CD-01 ``codes.code("text", span text)`` (braced: the owner's
    view is an API response, not a sink field). The rendered text never carries a code."""
    from primitives import codes
    spans = value_spans(text)
    parts, k = [], 0
    for a, b in spans:
        parts.append(text[k:a])
        parts.append(mask)
        k = b
    parts.append(text[k:])
    return "".join(parts), [{"start": a, "end": b, "code": codes.code("text", text[a:b])} for a, b in spans]
