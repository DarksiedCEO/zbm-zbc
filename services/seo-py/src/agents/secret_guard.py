"""
Secret-shaped text and URLs in provider output (WR-F012, WR-F013; ADR 0046 B5 / B9). Callum keeps bounded excerpts of
answer text and the URLs an engine cited; neither may ever carry a credential, the engine's own API key included. This
module is ONE rule table with TWO entry points:

  ``redact_text(text) -> (redacted_text, count)``  STABLE PUBLIC ENTRY POINT for any free text (answer excerpts, titles,
                                                    hints; TNZ, PR #53, calls it): every secret-shaped span is replaced
                                                    by ``[redacted]``; a URL written in the text that carries a secret
                                                    is replaced whole. Never raises; ``("", 0)`` for empty input.
  ``url_secret_segments(url) -> (bad, other)``      for a cited URL: the path-segment indexes to replace, and whether
                                                    the query, fragment or user part carries one. ``url_has_secret``
                                                    is the yes/no form; a URL that has one is never fetched.

STRONG rules (``PROVIDERS`` below, plus labelled credentials and the AWS secret format) run on EVERY text, every URL
segment, query key and value, fragment and user part, after EVERY decode round, with NO host or shape exemption. Host
and URL-shape exemptions may only suppress the WEAK generic heuristics (mixed hex runs, high-entropy tokens).

Decoding: each input is checked as written and after each round of percent-decoding (until stable, at most
``DECODE_ROUNDS``) and HTML-entity decoding; a key found after any round is a secret (redacted, never fetched).

Prefixes match glued to any non-alphanumeric character (``tok-eyJ...``, ``x_ghp_...``, ``.AKIA...``), never inside a
word. The AWS secret-access-key format is any 40-character run of ``[A-Za-z0-9/+]`` bounded by characters outside that
class with at least one digit, one upper-case and one lower-case letter; in a URL path the run may cross ``/``
segment boundaries, so the whole decoded path is scanned for 40-character windows aligned to segment or punctuation
boundaries. Its cost (ordinary paths that happen to fit) is measured in tests (``test_callum.py``).

NOTE for later unification: Priya's search-query classifier (PR #50, ``agents/search_performance.py``) applies close
rules to search queries; the two should be merged into this one table when both land.
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import Counter
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

REDACTED = "[redacted]"
DECODE_ROUNDS = 4
GUARD_MAX_DEPTH = 8

# Zero-width, bidi-control, word-joiner, BOM, soft hyphen, Mongolian vowel separator: stripped before matching so a key
# split by an invisible character (``AK​IA…``) or wrapped in bidi overrides is matched as written (threat list:
# obfuscations / invisible-char splits).
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁯﻿­᠎]")


def _strip_invisible(s: str) -> str:
    return _INVISIBLE.sub("", s or "")


def normalise(text: str) -> str:
    """NFKC fold (fullwidth / compatibility forms become ASCII) after invisible / bidi characters are stripped, so a
    fullwidth or invisible-split key is matched by the same ASCII rules (threat list: NFKC, invisible-char splits)."""
    return unicodedata.normalize("NFKC", _strip_invisible(text))

# --------------------------------------------------------------------------------------------- the strong rule table
# (provider, documented key shape). Glued after any non-alphanumeric character: the lookbehind is "not a letter or
# digit", so "-", "_", "." and "/" before a prefix all match. Each entry is a documented format (provider docs /
# secret-scanning partner formats); the generic ``sk-`` family additionally needs a digit and an upper-case letter so a
# lower-case slug such as ``sk-ii-facial-essence`` is not a key.
PROVIDERS = (
    ("aws_access_key_id", r"(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|ABIA|ACCA)[A-Z0-9]{16}"),
    ("openai_anthropic", r"sk-(?:proj-|svcacct-|admin-|ant-(?:api|admin)\d\d-)?[A-Za-z0-9_\-]{20,}"),
    ("github", r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ("stripe", r"(?:(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}|whsec_[A-Za-z0-9]{20,})"),
    # Slack: bot/user/app/legacy/config tokens (``xox[a-z]-``, any letter), plus the refresh forms ``xoxe.xox[a-z]-``
    # and ``xoxe-<digits>-`` (threat list: xox[a-z]- and refresh form)
    ("slack", r"(?:xoxe\.xox[a-z]-[A-Za-z0-9\-]{10,}|xoxe-\d+-[A-Za-z0-9\-]{10,}|xox[a-z]-[A-Za-z0-9\-]{10,})"),
    ("google_api", r"AIza[0-9A-Za-z_\-]{30,}"),
    ("perplexity", r"pplx-[A-Za-z0-9]{16,}"),
    ("gitlab", r"glpat-[A-Za-z0-9_\-]{20,}"),
    ("npm", r"npm_[A-Za-z0-9]{30,}"),
    ("huggingface", r"hf_[A-Za-z0-9]{30,}"),
    ("sendgrid", r"SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    ("jwt", r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
)
_PREFIX = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(f"(?P<{n}>{rx})" for n, rx in PROVIDERS) + ")")
# GLUE forms: the same fixed-prefix shapes with NO leading boundary, so a key glued to a word or to capital letters
# (``internalAKIAIOSFODNN7EXAMPLE``, ``tokenAIza…``, ``configsk-ant-…``) is still caught (threat list: glued to
# words/capitals). The ``sk-`` family keeps its ``_prefix_ok`` gate even when glued, so a lower-case slug is not a key.
_GLUE = re.compile("(?:" + "|".join(f"(?P<{n}>{rx})" for n, rx in PROVIDERS) + ")")
# labelled credentials: ANY value >=6 characters after a credential label and a separator (threat list: labelled
# credentials). The label word (two-word labels with a space/underscore/dash allowed: ``api key``, ``access code``),
# optionally wrapped in a dotted/dashed identifier (``X_API_KEY``, ``db.password``), including common non-English
# labels. No "tokenish" gate when the separator is explicit (``:`` ``=`` ``->`` ``→`` ``—`` `` is `` `` - ``); prose
# ("the key takeaway", "password protection") has no such separator so it stays.
_CRED_WORD = (
    r"api[ _\-]?keys?|apikey|secret[ _\-]?keys?|client[ _\-]?secret|access[ _\-]?(?:tokens?|codes?|keys?)"
    r"|refresh[ _\-]?tokens?|authorization|authorisation|x[ _\-]?api[ _\-]?key|x[ _\-]?goog[ _\-]?api[ _\-]?key"
    r"|bearer|auth|passphrase|passcode|passwords?|passwd|pwd|pin|secrets?|tokens?|credentials?"
    r"|session[ _\-]?id|cookie|keys?|contrase[nñ]a|mot[ _]de[ _]passe|cl[eé]|schl[uü]ssel|senha|clave|chiave"
    r"|пароль|ключ|密码|密碼|金鑰|秘钥|パスワード|비밀번호")
_LABEL = (r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]{1,16}[_\-.]){0,3}(?:" + _CRED_WORD
          + r")(?:[_\-.][A-Za-z0-9]{1,16}){0,2}(?![A-Za-z0-9])")
_VALUE = r"([^\s\"'<>&,;\]\}]{6,512})"
_SCHEME = r"(?:(?i:basic|bearer|token|digest)\s+)?"
# explicit separator -> no shape gate (``password: consistency`` is treated as a credential)
_NAMED = re.compile(_LABEL + r"[\"']?\s{0,4}(?:[:=]|->|→|—|\sis\s|\s-\s|：|＝)\s{0,4}[\"']?" + _SCHEME + _VALUE)
# bare whitespace -> a token-shaped value only (``key product`` stays prose; ``key <token>`` is a credential)
_NAMED_SPACE = re.compile(_LABEL + r"\s+" + _SCHEME + r"([A-Za-z0-9_\-+/=.~]{8,512})")
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])")
# a 40-character ``[A-Za-z0-9/+]`` run (any shape, digits optional) when labelled with aws / secret / key within 40
# characters before it: the no-digit AWS secret form (threat list: AWS secret keys incl. no-digit secrets)
_AWS_SECRET_LABELLED = re.compile(r"(?i)(?:aws|secret|key|access)[^A-Za-z0-9/+]{0,40}?([A-Za-z0-9/+]{40})(?![A-Za-z0-9+])")
# an AWS access key id written with one whitespace between every character (``A K I A I O S F ...``): must hold a digit
_AWS_SPACED = re.compile(r"(?<![A-Za-z0-9])(?:A\sK\sI\sA|A\sS\sI\sA|A\sG\sP\sA|A\sI\sD\sA|A\sR\sO\sA|A\sI\sP\sA"
                         r"|A\sN\sP\sA|A\sN\sV\sA|A\sB\sI\sA|A\sC\sC\sA)(?:\s[A-Z0-9]){16}(?![A-Za-z0-9])")
# a PEM / PGP private-key block (any key type); an unterminated block is redacted to the end of its base64 body
_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
                  r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|[A-Za-z0-9+/=\s]*)")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=~]{20,}")
_PIECE = re.compile(r"[A-Za-z0-9+=~]{20,}")          # "/" splits: a path is not one token (AWS keys: their rule)
_URL_IN_TEXT = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'`]+")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _mixed(v: str) -> bool:
    return any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


def _aws_shape(tok: str) -> bool:
    return any(c.isdigit() for c in tok) and any(c.isupper() for c in tok) and any(c.islower() for c in tok)


def _space_value_ok(v: str) -> bool:
    """A value after a bare-whitespace label is a credential only when token-shaped (a digit, or long, or mixed case):
    ``key product`` stays prose; ``key <token>`` does not. Explicit separators need no such gate."""
    return (any(c.isdigit() for c in v) or len(v) >= 16
            or (len(v) >= 10 and any(c.isupper() for c in v) and any(c.islower() for c in v)))


_SK_SUBPREFIX = re.compile(r"sk-(?:proj|svcacct|admin|ant)-")


def _prefix_ok(m: re.Match) -> bool:
    if m.lastgroup == "openai_anthropic":
        body = m.group(0)
        # a recognised sub-prefix (``sk-proj-``, ``sk-ant-…``) is distinctive on its own; a bare ``sk-<body>`` still
        # needs a digit and an upper-case letter so a lower-case slug (``sk-ii-facial-essence``) is not a key
        return bool(_SK_SUBPREFIX.match(body)) or (any(c.isdigit() for c in body) and any(c.isupper() for c in body))
    return True


def decodings(text: str) -> list:
    """``text`` as written, then its normalised form (NFKC + invisible/bidi strip), then after each percent-decoding
    round (until stable, at most DECODE_ROUNDS) with HTML-entity decoding, re-normalised each round: every distinct
    form, in order. The deepest-decoded form is last."""
    raw = text or ""
    out = [raw]
    seen = {raw}
    cur = normalise(raw)
    if cur not in seen:
        out.append(cur)
        seen.add(cur)
    for _ in range(DECODE_ROUNDS):
        nxt = normalise(html.unescape(unquote(cur))) if ("%" in cur or "&" in cur) else cur
        if nxt == cur:
            break
        if nxt not in seen:
            out.append(nxt)
            seen.add(nxt)
        cur = nxt
    return out


# a line break (or form feed / vertical tab) sitting between two token characters: removed for a second strong pass so
# a key split across lines is rejoined (threat list: line-break splits). A normal space/tab is NOT removed here, so
# ordinary word spacing is never collapsed (that would glue a key to the next word and hide its boundary); single
# spaces between every character are handled by ``_AWS_SPACED``. Bounded: one substitution pass.
_SPLIT = re.compile(r"(?<=[A-Za-z0-9+/=_.~\-])[\r\n\f\v]+(?=[A-Za-z0-9+/=_.~\-])")


def _deglued(text: str) -> str:
    return _SPLIT.sub("", text)


def _deglue_map(text: str):
    """The text with split-point whitespace removed, plus a list mapping each kept-character index back to its index in
    the original (so a strong span of the deglued form maps to an original span). ``(text, None)`` when nothing splits."""
    cuts = list(_SPLIT.finditer(text))
    if not cuts:
        return text, None
    drop = set()
    for m in cuts:
        drop.update(range(*m.span()))
    out, mp = [], []
    for i, ch in enumerate(text):
        if i not in drop:
            out.append(ch)
            mp.append(i)
    return "".join(out), mp


def _aws_spaced_ok(s: str) -> bool:
    return any(c.isdigit() for c in s)


def _strong_spans_one(text: str) -> list:
    spans = [m.span() for m in _PREFIX.finditer(text) if _prefix_ok(m)]
    spans += [m.span() for m in _GLUE.finditer(text) if _prefix_ok(m)]
    spans += [m.span() for m in _AWS_SPACED.finditer(text) if _aws_spaced_ok(m.group(0))]
    spans += [m.span() for m in _PEM.finditer(text)]
    spans += [m.span(1) for m in _NAMED.finditer(text)]
    spans += [m.span(1) for m in _NAMED_SPACE.finditer(text) if _space_value_ok(m.group(1))]
    spans += [m.span() for m in _AWS_SECRET.finditer(text) if _aws_shape(m.group(0))]
    spans += [m.span(1) for m in _AWS_SECRET_LABELLED.finditer(text)]
    return spans


def _weak_spans_one(text: str) -> list:
    spans = [m.span() for m in _HEX.finditer(text) if _mixed(m.group(0))]
    spans += [m.span() for m in _TOKEN.finditer(text)
              if any(_mixed(p) and sum(c.isdigit() for c in p) >= 2 and _entropy(p) >= 3.5
                     for p in _PIECE.findall(m.group(0)))]
    return spans


def _strong_spans(text: str) -> list:
    """The strong-rule spans of ``text`` as written. KEPT under this name for TNZ (PR #53), which calls it until it
    switches to ``redact_text``; do not rename or remove before then."""
    return sorted(_strong_spans_one(text or ""))


def strong_secret(text: str) -> bool:
    """A STRONG rule fires on ``text``, any of its decodings, or the deglued form of any (a key split by a line break
    or spaces). Never exempted."""
    if not text:
        return False
    for d in decodings(text):
        if _strong_spans_one(d):
            return True
        g = _deglued(d)
        if g != d and _strong_spans_one(g):
            return True
    return False


def weak_secret(text: str) -> bool:
    return bool(text) and any(_weak_spans_one(d) for d in decodings(text))


def has_secret(text: str) -> bool:
    return strong_secret(text) or weak_secret(text)


def _merge(spans: list) -> list:
    merged: list = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def redact_text(text: str) -> tuple:
    """STABLE PUBLIC ENTRY POINT: ``redact_text(text) -> (redacted, findings)`` — ``redacted`` is ``text`` with every
    secret-shaped span replaced by ``[redacted]``; ``findings`` is the number of spans redacted (an int, 0 when
    nothing was found). Never raises; a non-string or empty input gives ``("", 0)`` / ``(text, 0)``.

    The same rule table as URLs: the strong rules and the weak heuristics on the text as written; any
    whitespace-delimited word whose decoding (percent / HTML entities, repeated) holds a strong secret is redacted
    whole; any URL written in the text that carries a secret by the URL rules (``url_has_secret``: path windows for
    AWS keys that cross "/", queries, fragments) is redacted whole. Linear in the text apart from the per-URL checks,
    which are bounded by each URL's length."""
    if not isinstance(text, str) or not text:
        return ("" if not isinstance(text, str) else text), 0
    spans = _strong_spans_one(text) + _weak_spans_one(text)
    # normalise (fullwidth / invisible) and decode a word in place, so an obfuscated single word is redacted whole
    for m in re.finditer(r"\S+", text):
        w = m.group(0)
        if w != normalise(w) or "%" in w or "&" in w:
            if strong_secret(w):
                spans.append(m.span())
    # a key split by a line break or spaces: strong spans of the deglued text mapped back to original coordinates (the
    # whitespace inside the key is redacted with it)
    deglued, mp = _deglue_map(text)
    if mp is not None:
        for a, b in _strong_spans_one(deglued):
            spans.append((mp[a], mp[b - 1] + 1))
    for m in _URL_IN_TEXT.finditer(text):
        u = m.group(0).rstrip(".,;:!?)]}")
        url = u if "://" in u else "https://" + u
        if url_has_secret(url):
            spans.append((m.start(), m.start() + len(u)))
    merged = _merge(spans)
    if not merged:
        return text, 0
    out, at = [], 0
    for a, b in merged:
        out.append(text[at:a])
        out.append(REDACTED)
        at = b
    out.append(text[at:])
    return "".join(out), len(merged)


redact = redact_text                                   # the name callum.py used first; same function


def guard_record(obj, _depth: int = 0):
    """FAIL-CLOSED sweep of anything bound for storage, a log or output (a dict, list, tuple or str; ADR 0050 M4).
    Every string is run through ``redact_text``; if a STRONG rule still fires on the result (a split / glued / decoded
    form the span pass could not place), the whole string is replaced by ``[redacted]``. Structure, numbers, booleans
    and ``None`` are returned unchanged. Bounded recursion depth; never raises."""
    if _depth > GUARD_MAX_DEPTH:
        return REDACTED
    if isinstance(obj, str):
        red = redact_text(obj)[0]
        return REDACTED if strong_secret(red) else red
    if isinstance(obj, dict):
        return {k: guard_record(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [guard_record(v, _depth + 1) for v in obj]
    return obj


# ------------------------------------------------------------------------------------------------ URLs

# Exemptions below suppress ONLY the weak heuristics (``weak_secret``); the strong rules always run.
CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gist.github.com",
                        "huggingface.co"})
_CODE_REF = frozenset({"commit", "commits", "tree", "blob", "raw", "compare", "blame", "resolve", "-"})
# hosts (and their subdomains) whose ids are long and random by design: documents, short links, media, CDNs, page
# builders, share links. Weak heuristics only.
ID_HOSTS = frozenset({
    "docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com", "forms.gle", "t.co", "bit.ly",
    "goo.gl", "tinyurl.com", "ow.ly", "lnkd.in", "buff.ly", "rebrand.ly", "is.gd", "youtu.be", "amzn.to",
    "shorturl.at", "cutt.ly", "fonts.gstatic.com", "open.spotify.com", "notion.so", "notion.site", "figma.com",
    "apnews.com", "facebook.com", "fb.com", "wixstatic.com", "wix.com", "squarespace-cdn.com", "squarespace.com",
    "ctfassets.net", "contentful.com", "gist.github.com", "huggingface.co", "loom.com", "canva.com", "zoom.us",
    "jsdelivr.net", "unpkg.com", "cloudfront.net", "googleusercontent.com", "ytimg.com", "maps.app.goo.gl"})
# a path or query that is a Google Maps place / data blob, wherever on google.*
_MAPS = re.compile(r"^/maps(?:/|$)")
# query parameters whose value is a public id on that host (weak heuristics only)
ID_PARAMS = {"indeed.com": frozenset({"jk", "vjk"}), "youtube.com": frozenset({"v", "list", "si"}),
             "google.com": frozenset({"id", "q", "data", "cid", "ftid"}), "zoom.us": frozenset({"pwd"}),
             "facebook.com": frozenset({"story_fbid", "id", "fbid", "set"})}
TRACKING_PARAMS = frozenset({"gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "yclid", "twclid", "ttclid",
                             "igshid", "li_fat_id", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "_ga", "_gl",
                             "srsltid", "ref_src", "ref_url", "si"})
_ASSET = re.compile(r"[A-Za-z0-9_-]+[.-][0-9a-fA-F]{8,64}(?:\.[A-Za-z0-9_-]+)*\.(?:css|js|mjs|map|png|jpe?g|gif|svg|"
                    r"webp|avif|woff2?|ttf|otf|eot|ico|json|html?|wasm|txt)")
_SHA = re.compile(r"[0-9a-f]{7,64}(?:\.\.\.?[0-9a-f]{7,64})?")
_LONG_HEX = re.compile(r"(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{48,}")
# a 40-character window of the decoded path aligned to segment / punctuation boundaries (may cross "/")
_AWS_WINDOW = re.compile(r"(?<![A-Za-z0-9+])(?=([A-Za-z0-9/+]{40})(?![A-Za-z0-9+]))")


def canon_host(host: str) -> str:
    """Lower case, no trailing dot, no ``www.``."""
    h = (host or "").lower().rstrip(".")
    return h.removeprefix("www.")


def _host_in(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def _tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k.startswith("utm_")


def _weak_exempt_path(host: str, path: str, segs: list, i: int) -> bool:
    seg = segs[i]
    if _host_in(host, CODE_HOSTS) and i >= 1 and segs[i - 1] in _CODE_REF and _SHA.fullmatch(seg):
        return True                                       # a commit / tree / blob SHA on a code host
    if _host_in(host, CODE_HOSTS) and host in ("gist.github.com",) and _SHA.fullmatch(seg):
        return True                                       # a gist id
    if _ASSET.fullmatch(seg) or "_next" in segs[:i] or "_buildManifest.js" in segs:
        return True                                       # a hashed asset name; a Next.js build id
    if _host_in(host, ("google.com",)) and _MAPS.match(path):
        return True                                       # a Google Maps place / data blob
    return _host_in(host, ID_HOSTS) and not _LONG_HEX.search(unquote(seg))


def _aws_in_path(path: str) -> set:
    """Segment indexes covered by an AWS-format 40-character window of any decoding of ``path`` (strong). The window
    may cross ``/`` boundaries; there is no path-word skip (ADR 0050: it let real keys through, so it is removed —
    security over precision)."""
    hits: set = set()
    for d in decodings(path):
        for m in _AWS_WINDOW.finditer(d):
            run = m.group(1)
            if _aws_shape(run):
                first = d[:m.start(1)].count("/")
                hits |= set(range(first, first + run.count("/") + 1))
    return hits


def url_secret_segments(url: str) -> tuple:
    """``(indexes of secret-shaped path segments, query or fragment or user part secret-shaped)`` for ``url``.

    STRUCTURAL RULE: the strong rules run on every path segment, every query key and value (tracking and id parameters
    included), the fragment, the user part and the whole decoded path (AWS windows crossing "/"), after every decode
    round, whatever the host. Host and URL-shape exemptions only suppress the weak heuristics."""
    return _url_secret_segments(url, 0)


def _url_secret_segments(url: str, depth: int) -> tuple:
    try:
        p = urlsplit(url)
        host = canon_host(p.hostname or "")
        user, password = p.username, p.password
    except ValueError:
        return set(), True
    # the URL as a whole, after every decode round: a key split by an encoded "&" / "#" / "/" (``&#47;``) is found in
    # the decoded string, and the decoded URL is checked again by the same rules (bounded depth)
    forms = decodings(url)
    path = p.path or ""
    encoded = {i for i, x in enumerate(path.split("/")) if x and decodings(x)[-1] != x}
    if any(_strong_spans_one(f) for f in forms[1:]):
        return encoded, True                              # every encoded path segment is withheld too
    if depth < 1 and len(forms) > 1:
        dbad, dother = _url_secret_segments(forms[-1], depth + 1)
        if dbad or dother:
            return encoded, True
    # the host and every host label, at every decode level: a key hidden as a subdomain label is caught (ADR 0050:
    # one scan over the whole URL, host labels included). Strong only; never exempted.
    raw_host = re.sub(r":\d+$", "", (p.netloc or "").rsplit("@", 1)[-1])
    other = bool(raw_host) and strong_secret(raw_host)
    segs = path.split("/")
    bad = set()
    for i, seg in enumerate(segs):
        if not seg:
            continue
        if strong_secret(seg):
            bad.add(i)                                    # never exempted
        elif weak_secret(seg) and not _weak_exempt_path(host, path, segs, i):
            bad.add(i)
    bad |= {i for i in _aws_in_path(path) if i < len(segs)}
    pairs = parse_qsl(p.query or "", keep_blank_values=True)
    id_params = next((v for h, v in ID_PARAMS.items() if _host_in(host, (h,))), frozenset())
    weak_ok = _host_in(host, ID_HOSTS) or (_host_in(host, ("google.com",)) and _MAPS.match(path) is not None)
    other = other or strong_secret(p.query or "") or strong_secret(p.fragment or "")
    for k, v in pairs:
        if strong_secret(k) or strong_secret(v) or strong_secret(f"{k}={v}"):
            other = True                                  # tracking or id parameters included: never exempted
        elif _LONG_HEX.search(unquote(v)) or (not _tracking(k) and k.lower() not in id_params and not weak_ok
                                               and (weak_secret(v) or weak_secret(k))):
            other = True
    if p.query and not pairs and not weak_ok:
        other = other or weak_secret(p.query)
    for x in (p.fragment or "", user or "", password or ""):
        if x and (strong_secret(x) or _LONG_HEX.search(unquote(x)) or (weak_secret(x) and not weak_ok)):
            other = True
    return bad, other


def url_has_secret(url: str) -> bool:
    bad, other = url_secret_segments(url)
    return bool(bad) or other


def identity_query(url: str) -> list:
    """The query parameters that identify the page (every non-tracking parameter, sorted): ``watch?v=A`` and
    ``watch?v=B`` are two pages."""
    try:
        pairs = parse_qsl(urlsplit(url).query or "", keep_blank_values=True)
    except ValueError:
        return []
    return sorted((k, v) for k, v in pairs if not _tracking(k))


def canonical_key(url: str) -> str:
    """The identity of a cited page: scheme, canonical host (lower case, no trailing dot, no ``www.``, no default
    port), path and identity-bearing query (tracking parameters and the fragment dropped)."""
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
        return url                                    # unchanged byte for byte
    q = [(k, v) for k, v in pairs if not _tracking(k)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))
