"""
Secret-shaped text in provider output (WR-F012, WR-F013; ADR 0046 B5 / B9). Callum keeps bounded excerpts of answer
text and the URLs an engine cited; neither may ever carry a credential, the engine's own API key included. This module
finds secret-shaped tokens so they are REDACTED before anything is stored, returned or logged, and tells Callum never to
fetch a URL that carries one — while keeping ordinary text and URLs intact (re-certification review: precision).

Rules (standard library only). Text is checked as written AND percent-decoded (``Bearer%20<token>``):

  - documented credential prefixes: OpenAI ``sk-`` / ``sk-proj-``, Anthropic ``sk-ant-``, Perplexity ``pplx-``, Google
    ``AIza``, AWS access key ids ``AKIA`` / ``ASIA``, GitHub ``ghp_`` / ``gho_`` / ``ghu_`` / ``ghs_`` / ``ghr_`` /
    ``github_pat_``, Slack ``xox?-``, Stripe-style ``sk_live_`` / ``sk_test_`` / ``rk_live_`` / ``pk_live_``;
  - a credential announced by its label (``Bearer``, ``Authorization``, ``api_key`` / ``api-key``, ``x-api-key``,
    ``access_token``, ``token``, ``secret``, ``password``, ``passwd``) ONLY when the label is followed by ``:``, ``=``
    or whitespace and then a TOKEN-SHAPED value (8+ token characters with letters and digits): ``password protection``
    and the slug ``/password-reset-guide`` are prose, not credentials;
  - a JSON Web Token (``eyJ`` + three base64url parts);
  - the AWS secret-access-key format: a standalone 40-character run of ``[A-Za-z0-9/+]`` with upper case, lower case
    and a digit, ``/`` or ``+`` (the documented example ``wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`` has one digit);
  - a run of 16+ hexadecimal characters holding BOTH letters and digits (keys, the repo's derived test keys); a pure
    digit run (a tweet's status id, a 16-digit item id) is not a secret;
  - a token of 20+ base64 / URL-safe characters with letters AND at least two digits whose Shannon entropy is at least
    3.5 bits per character (words and slugs are split at ``-`` / ``.`` / ``_`` first and never reach that).

URL shapes that are allowed although a path segment would match (``url_secret_segments``):

  - a commit / tree / blob SHA on a known code host (github.com, gitlab.com, bitbucket.org, codeberg.org):
    ``/<org>/<repo>/commit/<sha>`` and the like;
  - a hashed asset name ``main.8f14e45fceea167a.css`` / ``app-3f2a9c1b7e4d5a6f.js`` (name, hash, file extension);
  - any path on docs.google.com / drive.google.com / sheets.google.com / slides.google.com (document ids) and on known
    link shorteners (t.co, bit.ly, ...): their ids are not credentials;
  - known tracking parameters (``gclid``, ``fbclid``, ``msclkid``, ``utm_*``, ...) are not secrets: stored URLs drop the
    whole query anyway, and ``fetch_url`` drops these before a fetch.

False positives cost a redacted span (``[redacted]``) or an unfetched URL, never a leak. NOTE for later unification:
Priya's search-query classifier (PR #50, ``agents/search_performance.py``) applies close rules to search queries; this
base does not have it yet, so the two should be merged into one shared classifier when both land.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

REDACTED = "[redacted]"
_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:proj-|ant-(?:[a-z]{3}\d{2}-)?|svcacct-)?[A-Za-z0-9_\-]{16,}|pplx-[A-Za-z0-9]{16,}"
    r"|AIza[0-9A-Za-z_\-]{30,}|(?:AKIA|ASIA)[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,})")
_LABEL = (r"(?i)(?<![A-Za-z0-9])(?:bearer|authorization|x-api-key|x-goog-api-key|api[_-]?key|access[_-]?token|token"
          r"|secret|password|passwd)")
# the label, then ":" / "=" (optionally spaced) or whitespace, then the value (a second "Bearer" allowed after a label)
_NAMED = re.compile(_LABEL + r"(?:\s*[:=]\s*|\s+)[\"']?(?:bearer\s+)?([A-Za-z0-9_\-+/=.~]{8,})")
_JWT = re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
_AWS_SECRET = re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=~]{20,}")
_PIECE = re.compile(r"[A-Za-z0-9+=~]{20,}")          # "/" splits: a path is not one token (AWS keys: their rule)


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _token_shaped(v: str) -> bool:
    return any(c.isdigit() for c in v) and any(c.isalpha() for c in v)


def _high_entropy(tok: str) -> bool:
    for piece in _PIECE.findall(tok):
        letters, digits = sum(c.isalpha() for c in piece), sum(c.isdigit() for c in piece)
        if letters and digits >= 2 and _entropy(piece) >= 3.5:
            return True
    return False


_LOWER_RUN = re.compile(r"[a-z]{6,}")


def _aws_secret(tok: str) -> bool:
    """The 40-character AWS secret format: both cases, a digit / "/" / "+", high entropy, and no run of 6+ lower-case
    letters (random base64 has none; words and CamelCase paths such as ``FulfillmentByAmazon/comments`` do)."""
    return any(c.isupper() for c in tok) and any(c.islower() for c in tok) \
        and any(c.isdigit() or c in "/+" for c in tok) and _entropy(tok) >= 4.0 and not _LOWER_RUN.search(tok)


def _spans(text: str) -> list:
    spans = [m.span() for m in _PREFIX.finditer(text)]
    spans += [m.span(1) for m in _NAMED.finditer(text) if _token_shaped(m.group(1))]
    spans += [m.span() for m in _JWT.finditer(text)]
    spans += [m.span() for m in _AWS_SECRET.finditer(text) if _aws_secret(m.group(0))]
    spans += [m.span() for m in _HEX.finditer(text)
              if any(c.isdigit() for c in m.group(0)) and any(c.isalpha() for c in m.group(0))]
    spans += [m.span() for m in _TOKEN.finditer(text) if _high_entropy(m.group(0))]
    return sorted(spans)


def _merge(spans: list) -> list:
    merged: list = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def has_secret(text: str) -> bool:
    """Secret-shaped as written or once percent-decoded (``Bearer%20<token>``)."""
    if not text:
        return False
    if _spans(text):
        return True
    decoded = unquote(text) if "%" in text else text
    return decoded != text and bool(_spans(decoded))


def redact(text: str) -> tuple:
    """``(text with every secret-shaped span replaced by [redacted], number of spans)``. Linear in the text. A span
    found only after percent-decoding (``Bearer%20<token>``) redacts the encoded word that holds it."""
    if not text:
        return text, 0
    spans = _spans(text)
    if "%" in text:
        for m in re.finditer(r"\S*%[0-9A-Fa-f]{2}\S*", text):
            if has_secret(m.group(0)) and not _spans(m.group(0)):
                spans.append(m.span())
    merged = _merge(sorted(spans))
    if not merged:
        return text, 0
    out, at = [], 0
    for a, b in merged:
        out.append(text[at:a])
        out.append(REDACTED)
        at = b
    out.append(text[at:])
    return "".join(out), len(merged)


# ------------------------------------------------------------------------------------------------ URLs

CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"})
_CODE_REF = frozenset({"commit", "commits", "tree", "blob", "raw", "compare", "blame", "-"})
ID_HOSTS = frozenset({"docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com",
                      "forms.gle", "t.co", "bit.ly", "goo.gl", "tinyurl.com", "ow.ly", "lnkd.in", "buff.ly",
                      "rebrand.ly", "is.gd", "youtu.be", "amzn.to", "shorturl.at", "cutt.ly"})
TRACKING_PARAMS = frozenset({"gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "yclid", "twclid", "ttclid",
                             "igshid", "li_fat_id", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "_ga", "_gl",
                             "srsltid", "ref_src", "ref_url"})
_ASSET = re.compile(r"[A-Za-z0-9_-]+[.-][0-9a-fA-F]{8,64}(?:\.[A-Za-z0-9_-]+)*\.(?:css|js|mjs|map|png|jpe?g|gif|svg|"
                    r"webp|avif|woff2?|ttf|otf|eot|ico|json|html?|wasm|txt)")
_SHA = re.compile(r"[0-9a-f]{7,64}(?:\.\.\.?[0-9a-f]{7,64})?")


def _tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k.startswith("utm_")


def url_secret_segments(url: str) -> tuple:
    """``(indexes of secret-shaped path segments, query or fragment or user part secret-shaped)`` for ``url``, with
    the allowed URL shapes above applied."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        user, password = p.username, p.password
    except ValueError:
        return set(), True
    if host.startswith("www."):
        host = host[4:]
    segs = (p.path or "").split("/")
    bad = set()
    if host not in ID_HOSTS:
        for i, seg in enumerate(segs):
            if not seg or not has_secret(seg):
                continue
            if host in CODE_HOSTS and i >= 2 and segs[i - 1] in _CODE_REF and _SHA.fullmatch(seg):
                continue                                  # a commit / tree SHA on a code host
            if _ASSET.fullmatch(seg):
                continue                                  # a hashed asset name
            bad.add(i)
        # an AWS-format secret holds "/", so it spans segments: every run of consecutive segments 40 characters long
        # (bounded: at most 64 segments are looked at)
        dec = [unquote(x) for x in segs[:64]]
        for i in range(len(dec)):
            total = -1
            for j in range(i, len(dec)):
                total += len(dec[j]) + 1
                if total > 40:
                    break
                if total == 40:
                    run = "/".join(dec[i:j + 1])
                    if _AWS_SECRET.fullmatch(run) and _aws_secret(run):
                        bad |= set(range(i, j + 1))
    rest = [v for k, v in parse_qsl(p.query or "", keep_blank_values=True) if not _tracking(k)]
    rest += [k for k, _ in parse_qsl(p.query or "", keep_blank_values=True) if not _tracking(k)]
    other = any(has_secret(x) for x in rest + [p.fragment or "", user or "", password or ""] if x)
    if not other and p.query and not parse_qsl(p.query, keep_blank_values=True):
        other = has_secret(p.query)
    return bad, other


def url_has_secret(url: str) -> bool:
    bad, other = url_secret_segments(url)
    return bool(bad) or other


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
