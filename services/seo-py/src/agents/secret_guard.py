"""
Shared redaction core v2 for the SEO department (ADR 0050 v2, superseding the v1 text): WITHHOLD THE WHOLE FIELD UNLESS IT
IS CLEARLY SAFE. Every field bound for storage, a log or client output (answer excerpts, titles, hints, candidate
names, URL hosts / path segments / query pairs, any string swept by ``guard_record``) is

  1. NORMALISED for inspection (``views``): NFKC; Cf / Cc / Mn / Me / Co / Cn code points, variation selectors, tag
     characters and Hangul fillers removed; every Unicode space or line / paragraph separator read as one space;
     markdown emphasis, backticks and tags stripped; bounded decoding (percent up to ``PERCENT_ROUNDS`` rounds, HTML
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
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import lookalikes

WITHHELD = "[withheld]"
REDACTED = WITHHELD                      # v1 name; same marker
MAX_INPUT = 65536                        # characters; a longer field is withheld unread
URL_MAX_INPUT = 8192                     # characters; a longer URL is withheld unread (real citation URLs are short)
PERCENT_ROUNDS = 3
DECODE_RUNS_MAX = 64                     # base64 / hex runs decoded per view
DECODE_RUN_MAX = 4096                    # characters of one run decoded
GUARD_MAX_DEPTH = 16
GUARD_MAX_NODES = 200000
CHECKS = ("a_label", "b_userinfo", "c_prefix", "d_token", "e_digits", "f_email", "url_credential", "url_personal")

# ------------------------------------------------------------------------------------------------ 1. normalise

_FOLD = lookalikes.Table()
_HANGUL_FILLERS = frozenset("\u115f\u1160\u3164\uffa0")
_BLANKS = frozenset("\u2800\u180e")                       # braille blank, Mongolian vowel separator: read as space
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
    if not t.isascii() or any(c < " " or c == "\x7f" for c in t):
        t = _nfkc(_map_chars(t))
    return _collapse(t)


def normalise(text: str) -> str:
    """The inspection form of ``text``: NFKC, invisible / joiner / control code points removed, every Unicode space
    and line break read as one space (step 1 of ADR 0050 v2). Never emitted."""
    return _clean(text or "")


_TAG = re.compile(r"<[^<>]{0,256}>")
_MD = re.compile(r"[*`~\\]")


def _strip_markup(t: str) -> str:
    """Markdown emphasis / backticks / escapes and HTML-like tags removed (inspection only)."""
    return _MD.sub("", _TAG.sub("", t)) if ("<" in t or "*" in t or "`" in t or "~" in t or "\\" in t) else t


_ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})")


def _unescape(t: str) -> str:
    return _ESC.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), t) if "\\" in t else t


_B64_RUN = re.compile(r"[A-Za-z0-9+/_\-]{12,}={0,2}")
_HEX_RUN = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")


def _printable(b: bytes) -> str | None:
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s:
        return None
    ok = sum(1 for c in s if c.isprintable() or c in "\n\t\r")
    return s if ok >= 0.85 * len(s) else None


def _decoded_runs(t: str) -> list:
    """Base64- and hex-looking runs of ``t`` decoded (when they decode to readable text), for inspection only.
    Bounded: at most ``DECODE_RUNS_MAX`` runs of at most ``DECODE_RUN_MAX`` characters each."""
    out: list = []
    n = 0
    for rx in (_HEX_RUN, _B64_RUN):
        for m in rx.finditer(t):
            n += 1
            if n > DECODE_RUNS_MAX:
                return out
            run = m.group(0)[:DECODE_RUN_MAX]
            try:
                if rx is _HEX_RUN:
                    b = bytes.fromhex(run if len(run) % 2 == 0 else run[:-1])
                else:
                    body = run.rstrip("=").replace("-", "+").replace("_", "/")
                    body = body[: len(body) - len(body) % 4] if len(body) % 4 == 1 else body
                    b = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
            except (ValueError, binascii.Error):
                continue
            s = _printable(b)
            if s:
                out.append(_clean(s))
    return out


def views(text: str) -> list:
    """Every inspection view of ``text`` (see ``_tagged_views``)."""
    return [v for v, _ in _tagged_views(text)]


def _tagged_views(text: str) -> list:
    """(view, folded) pairs. Every inspection view of ``text`` (step 1): the normalised form, the markup-stripped
    form, each percent / HTML-entity / escape decoding round (re-normalised), the decoded base64 / hex runs of each,
    and (``folded`` True) the ASCII-folded form of each non-ASCII view. Distinct, in order; bounded in number and
    size."""
    base = _clean(text or "")
    out = [base]
    seen = {base}

    def add(v: str) -> None:
        if v not in seen:
            seen.add(v)
            out.append(v)
    add(_strip_markup(base))
    cur = base
    for _ in range(PERCENT_ROUNDS):
        nxt = _clean(_unescape(_entities(_percent(cur))))     # escapes read before markup strips the backslash
        if nxt == cur:
            break
        add(nxt)
        add(_strip_markup(nxt))
        cur = nxt
    for v in out.copy():
        for d in _decoded_runs(v):
            add(d)
            add(_strip_markup(_clean(_entities(_percent(d)))))
    tagged = [(v, False) for v in out]
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
# The CLOSED multilingual credential-label list (ADR 0050 v2 section 2a). Three ways to match, all case-insensitive on a
# confusable-folded, diacritic-stripped view:
#   _JOINED  long labels found anywhere in the text with every non-letter removed, so "pass word", "pass_word",
#            "p.a.s.s.w.o.r.d", "api key", "DB_PASSWORD", "userPassword", "mot de passe" all read as the label;
#   _PIECES  short labels that must be a whole piece (pieces split at non-letters, camelCase and letter/digit
#            boundaries): "pass" in "DB_PASS", "auth" in "authToken", "pin", "sid", "token", "secret", "cookie";
#   _SCRIPT  labels in scripts without word spacing, matched as substrings of the letters-only view.
_JOINED = (
    "password", "passwd", "passcode", "passphrase", "apikey", "accesskey", "accesstoken", "authtoken",
    "secretkey", "privatekey", "clientsecret", "sessionid", "accesscode", "securitycode", "sessiontoken", "seedphrase",
    "recoveryphrase", "mnemonic", "credential", "connectionstring", "accountkey", "refreshtoken", "bearertoken",
    "sharedaccesssignature", "contrasena", "motdepasse", "passwort", "kennwort", "senha", "wachtwoord", "losenord",
    "adgangskode", "salasana", "haslo", "katasandi", "matkhau", "parolasi", "heslo", "jelszo", "lozinka",
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
    "\u0643\u0644\u0645\u0629\u0627\u0644\u0645\u0631\u0648\u0631", "\u0643\u0644\u0645\u0629\u0627\u0644\u0633\u0631",
    "\u0631\u0645\u0632\u0639\u0628\u0648\u0631",
    "\u092a\u093e\u0938\u0935\u0930\u094d\u0921", "\u0e23\u0e2b\u0e31\u0e2a\u0e1c\u0e48\u0e32\u0e19",
    "\u05e1\u05d9\u05e1\u05de\u05d4", "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c3\u03c0\u03c1\u03bf\u03c3\u03b2",
)
_JOINED_RX = re.compile("|".join(sorted({w.replace(" ", "") for w in _JOINED}, key=len, reverse=True)))
_PIECE_RX = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_A-Za-z]+")
_SIG_RX = re.compile(r"(?i)(?<![a-z])sig\s*[=:]")
_LETTER_RUN = re.compile(r"[^\W\d_]+")
_NO_PAIR_JOIN = frozenset(("sid", "sig", "cle", "pw"))  # "customer's ID", "Cle Elum": too common split
# "key" is an ordinary word ("the key takeaway"), so it is a label only when a value follows it: an explicit ":" / "="
# and a value, or a token-shaped value (letters and digits, 8+ characters) as the next word
_KEY_VALUE = re.compile(r"(?i)(?<![a-z])(?:keys?|cl[e\u00e9]s?|schl[u\u00fc]ssel)\s?(?:[:=]\s?[^\s]{6}"
                        r"|\s(?=[A-Za-z0-9+/=_.\-]{0,64}\d)(?=[0-9+/=_.\-]{0,64}[A-Za-z])[A-Za-z0-9+/=_.\-]{8})")


def _letters_view(v: str, strip_marks: bool = True) -> str:
    """Confusables folded (``lookalikes``), lower case, diacritics stripped (Latin / Greek / Cyrillic only; Hangul
    syllables are kept whole), then only letters kept."""
    f = _FOLD.fold_cased(v)
    if strip_marks and not f.isascii():
        f = "".join(c for c in unicodedata.normalize("NFKD", f) if not unicodedata.combining(c))
        f = unicodedata.normalize("NFC", f)
    return "".join(c for c in f if c.isalpha())


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
    f = _FOLD.fold_cased(p)
    if not f.isascii():
        f = "".join(c for c in unicodedata.normalize("NFKD", f) if not unicodedata.combining(c))
    return f


PAIR_WINDOW = 64                         # characters searched on each side of a split short label


def _pair_has_separator(v: str, w: str, nxt: str, at: int = 0, end: int | None = None) -> bool:
    """``s id:`` / ``p w =``: the split short label is followed by an explicit ``:`` or ``=``. Only the window of
    ``PAIR_WINDOW`` characters around the pair (``v[at:end]``) is searched, so a text of many short pairs stays linear;
    a match inside the window is found exactly as a search of the whole text would find it."""
    end = len(v) if end is None else end
    win = v[max(0, at - PAIR_WINDOW):end + PAIR_WINDOW]
    return re.search(re.escape(w) + r"\W{1,3}" + re.escape(nxt) + r"\s{0,3}[:=]", win) is not None


def _label_hit(v: str) -> bool:
    if _SIG_RX.search(v) or _KEY_VALUE.search(v):
        return True
    n = len(v)
    for m in _PIECE_RX.finditer(v):
        p = m.group(0)
        if len(p) > 16:
            continue
        f = _fold_piece(p)
        if f not in _PIECES:
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
        if f in _PIECES and (len(f) >= 5 or w.isascii() is False):
            return True                                    # a whole word in any script (``\u015fifre``)
        if i + 1 < len(words):
            # a short label split by one space / NBSP / separator (``to ken``, ``P IN``): two adjacent short words
            nxt = words[i + 1]
            if len(w) + len(nxt) <= 16 and (min(len(w), len(nxt)) <= 3 or len(w) + len(nxt) >= 8):
                g = _fold_piece(w + nxt)
                if g in _PIECES and (g not in _NO_PAIR_JOIN or _pair_has_separator(v, w, nxt, runs[i].start(),
                                                                                runs[i + 1].end())):
                    return True
    # a label spelled one letter at a time (``t o k e n``)
    singles = "".join(w if len(w) == 1 else " " for w in words)
    if any(_fold_piece(x) in _PIECES for x in singles.split() if len(x) >= 3):
        return True
    letters = _letters_view(v)
    if _JOINED_RX.search(letters):
        return True
    raw_letters = letters if v.isascii() else "".join(c for c in v.lower() if c.isalpha())
    return any(s.replace(" ", "") in raw_letters or s.replace(" ", "") in letters for s in _SCRIPT)


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
                   r"|glc_|sntrys_|figd_|shpat_|rnd_|tskey-|pscale_tkn_|dp\.pt\.|fo1_|phc_|phx_)[A-Za-z0-9_\-]{8,}")),
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


def _prefix_found(s: str, nospace: bool = False) -> bool:
    """A strong-key prefix family matches in ``s`` (the bare ``sk-`` family only with a digit and a capital)."""
    if len(s) <= _LONG:
        rx = _PREFIX_NOSPACE_RX if nospace else _PREFIX_RX
        return any(m.lastgroup != "sk_generic" or _sk_ok(m) for m in rx.finditer(s))
    for n, rx in (_PREFIX_EACH_NOSPACE if nospace else _PREFIX_EACH):
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
    _B + r"(?:"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"|(?P<order>\d{3}-\d{7}-\d{7})"
    r"|(?P<ymd>(?:19|20)\d\d/(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]))"
    r"|(?P<iso>(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,9})?)?(?:Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)?)?)"
    r"|(?P<isbn13>(?:ISBN(?:-13)?:? ?)?97[89](?:[- ]?\d){10})"
    r"|(?P<isbn10>ISBN(?:-10)?:? ?\d(?:[- ]?\d){8}[- ]?[\dX])"
    r"|(?P<ean>\d{13})"
    r"|(?i:commit|commits|revision|sha)[ :#]{1,3}(?P<sha>[0-9a-f]{7,40})"
    r")" + _E)


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
        return _ean_ok(m.group(k))
    return True


def _masked(v: str) -> str:
    """``v`` with every allowlisted safe shape replaced by a mark that no digit run or token can cross."""
    if not any(c.isdigit() for c in v):
        return v
    out, at = [], 0
    for m in _SAFE_SHAPES.finditer(v):
        if not _shape_ok(m):
            continue
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


def _transitions(r: str) -> tuple:
    """(letter<->digit changes, upper<->lower changes) along ``r``."""
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
    r"|ya29[A-Za-z0-9]{20,}|amzn1oa2cs[A-Za-z0-9]{8,}|SG[A-Za-z0-9]{40,}|eyJ[A-Za-z0-9]{30,}")


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
        j = "".join(parts)
        return len(parts) >= 4 and any(c.isdigit() for c in j) and (_random_run(j, joined=True)
                                                                    or bool(_AWS_JOINED.search(j)))

    for t in toks:
        if t and "\ue000" not in t and not _ALNUM.search(t):
            continue                                       # r2 part 2: a filler-only piece (``,,,,``) pads, never splits
        t = t.strip(_EDGE)
        if t:
            t = _TOK_FILLER.sub("-", t)                    # any other filler run inside a token is a separator
        got = seen.get(t)
        if got is None:                                    # the per-token checks are a function of the token alone
            j = _TOK_SEP.sub("", t)
            got = (any(_random_run(r) for r in _RUN.findall(t))
                   or (j != t and (_split_key(t, j) or _prefix_after_separator(t)))
                   or (j != t and _prefix_found(j)), j)    # (the last: a prefix key split by separators)
            if len(seen) < TOKEN_MEMO_MAX:
                seen[t] = got
        if got[0]:
            return True
        j = got[1]
        # fragments of a key split by spaces / line breaks: consecutive token-like pieces, and runs of 3+ pieces of
        # one length (``AKIA IOSF ODNN 7EXA MPLE``, ``A K I A ...``)
        if _tokenish(j):
            chain.append(j)
        else:
            if chain_bad(chain):
                return True
            chain = []
        short = bool(j) and len(j) <= 4 and bool(_RUN.fullmatch(j))
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


def _digits_hit(v: str) -> bool:
    if sum(map(v.count, _ASCII_DIGITS)) < 9:
        return False
    compact = _DIGIT_FILLER.sub("", v)
    for m in _DIGIT_RUN.finditer(compact):
        g = m.group(0)
        if len(g) >= 9 and sum(map(g.count, _ASCII_DIGITS)) >= 9:
            return True
    return False


# ------------------------------------------------------------------------------------------------ 2f. e-mail

_EMAIL_PLAIN = re.compile(r"[A-Za-z0-9._%+\-]@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}(?![A-Za-z])")
_EMAIL_BRACKET = re.compile(r"(?i)[a-z0-9._%+\-] ?[\[\(\{<] ?(?:at|@) ?[\]\)\}>] ?[a-z0-9\-]{1,63}")
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
def _email_hit(v: str) -> bool:
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
            acc = ""
            for tok in v[m.end():m.end() + 1024].lstrip(" ").split(" ")[:40]:
                acc += tok
                if len(acc) > 255:
                    break
                if _DOMAIN_END.fullmatch(acc.rstrip(".,;:!?)]}>\"'")):
                    return True
    low = v.lower()
    if ("at" in low and ("[" in v or "(" in v or "{" in v or "<" in v)) and _EMAIL_BRACKET.search(v):
        return True
    return " at " in low and "dot" in low and _spelled_email(v)


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
    for v, folded in _tagged_views(text):
        for name, fn in (_CHECK_FUNCS[1:] if folded else _CHECK_FUNCS):
            if globals()[fn](v):
                return name
    return None


def _check(text: str, waive=frozenset(), urls: bool = True) -> str | None:
    if len(text) > MAX_INPUT:
        return "cap"
    personal = None
    for v, folded in _tagged_views(text):
        if not v:
            continue
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
        return _check(text, frozenset(waive) & WAIVABLE)
    except Exception:  # noqa: BLE001 - fail closed: an input the checks cannot read is withheld
        return "error"


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
        k = _kind(chk(seg, POSITION_WAIVE if waived[i] else frozenset()))
        if k is None and waived[i] and chk(seg) is not None:
            dependent = True
        if redirect and not fetch and i >= 2 and seg:
            k = k or PERSONAL                              # a grounding redirect id is never kept
        verdicts.append((seg, k))
    # a key or a number split across segments: the segments that passed alone (outside a disclosed position) read
    # together as one text; if that fails, every one of them holding a digit or an upper-case letter is withheld
    kept = [unquote(s) for i, (s, k) in enumerate(verdicts) if s and k is None and not waived[i]]
    if kept:
        cross = chk("/".join(kept)) or ("d_token" if _token_hit(_masked(_clean(" ".join(kept)))) else None)
        if cross is not None:
            kk = _kind(cross)
            verdicts = [(s, kk if (s and k is None and not waived[i] and any(c.isdigit() or c.isupper() for c in s))
                         else k) for i, (s, k) in enumerate(verdicts)]
    # checks (a)-(c) on the WHOLE path glued (a URI or prefix key spread over segments; a segment already withheld
    # for a credential is left out, one withheld for personal data is kept in): if that fails, every segment is
    # withheld, a disclosed position too
    glued = "".join(s if k != CREDENTIAL else " " for s, k in verdicts if s)
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
            "fragment_kind": _kind(chk(p.fragment)) if p.fragment else None, "exempt_dependent": dependent}


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


FETCH_BLOCKS = frozenset((CREDENTIAL, PERSONAL))         # r2 part 3a: personal data is never fetched either


def url_has_secret(url) -> bool:
    """A URL with any credential OR personal-data failure in any part (tracking parameters included): never fetched
    (fail closed; a URL that cannot be read counts as failing). A grounding redirect on the exact redirector host is
    judged with check (d) waived on its id only, so it can be resolved; its id is still never kept."""
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
