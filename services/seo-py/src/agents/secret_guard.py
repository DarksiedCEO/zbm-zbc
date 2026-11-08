"""
Secret-shaped text in provider output (WR-F012, WR-F013; ADR 0046 B5). Callum keeps bounded excerpts of answer text and
the URLs an engine cited; neither may ever carry a credential, the engine's own API key included. This module finds
secret-shaped tokens so they are REDACTED before anything is stored, returned or logged, and tells Callum never to
fetch a URL that carries one.

Rules (documented key prefixes + shape + entropy; standard library only):

  - known credential prefixes: OpenAI ``sk-`` / ``sk-proj-``, Anthropic ``sk-ant-``, Perplexity ``pplx-``, Google
    ``AIza``, AWS ``AKIA`` / ``ASIA``, GitHub ``ghp_`` / ``gho_`` / ``ghu_`` / ``ghs_`` / ``ghr_`` / ``github_pat_``,
    Slack ``xox?-``, Stripe-style ``sk_live_`` / ``sk_test_`` / ``rk_live_`` / ``pk_live_``;
  - a credential announced by its name: ``Bearer <token>``, ``Authorization: <token>``, ``api_key=``, ``x-api-key:``,
    ``token=``, ``secret=``, ``password=`` followed by 8+ token characters;
  - a JSON Web Token (three base64url parts joined by dots);
  - shape: any run of 16+ hexadecimal digits (keys, hashes, the repo's derived test keys), or a token of 20+ base64 /
    URL-safe characters with letters AND at least two digits whose Shannon entropy is at least 3.5 bits per character
    (ordinary words and slugs such as ``revenue-recovery-agency-2026`` are split at ``-`` / ``.`` / ``_`` first and
    never reach that).

False positives cost a redacted span (``[redacted]``), never a leak. NOTE for later unification: Priya's search-query
classifier (PR #50, ``agents/search_performance.py`` ``_SECRET_PREFIX`` / ``_secret_piece``) applies close rules to
search queries; this base does not have it yet, so the two should be merged into one shared classifier when both land.
"""

from __future__ import annotations

import math
import re
from collections import Counter

REDACTED = "[redacted]"
_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:proj-|ant-(?:[a-z]{3}\d{2}-)?|svcacct-)?[A-Za-z0-9_\-]{16,}|pplx-[A-Za-z0-9]{16,}"
    r"|AIza[0-9A-Za-z_\-]{30,}|(?:AKIA|ASIA)[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,})")
_NAMED = re.compile(r"(?i)(?:\bbearer|\bauthorization|\bx-api-key|\bx-goog-api-key|\bapi[_-]?key|\baccess[_-]?token"
                    r"|\btoken|\bsecret|\bpassword|\bpasswd)\s*[:=]?\s*[\"']?(?:bearer\s+)?([A-Za-z0-9_\-+/=.~]{8,})")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")
_HEX = re.compile(r"[0-9a-fA-F]{16,}")
_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=~]{20,}")
_PIECE = re.compile(r"[A-Za-z0-9+/=~]{20,}")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _high_entropy(tok: str) -> bool:
    for piece in _PIECE.findall(tok):
        letters, digits = sum(c.isalpha() for c in piece), sum(c.isdigit() for c in piece)
        if letters and digits >= 2 and _entropy(piece) >= 3.5:
            return True
    return False


def _spans(text: str) -> list:
    spans = [m.span() for m in _PREFIX.finditer(text)]
    spans += [m.span(1) for m in _NAMED.finditer(text)]
    spans += [m.span() for m in _JWT.finditer(text)]
    spans += [m.span() for m in _HEX.finditer(text)]
    spans += [m.span() for m in _TOKEN.finditer(text) if _high_entropy(m.group(0))]
    return sorted(spans)


def redact(text: str) -> tuple:
    """``(text with every secret-shaped span replaced by [redacted], number of spans)``. Linear in the text."""
    if not text:
        return text, 0
    merged: list = []
    for a, b in _spans(text):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    if not merged:
        return text, 0
    out, at = [], 0
    for a, b in merged:
        out.append(text[at:a])
        out.append(REDACTED)
        at = b
    out.append(text[at:])
    return "".join(out), len(merged)


def has_secret(text: str) -> bool:
    return bool(text) and bool(_spans(text))
