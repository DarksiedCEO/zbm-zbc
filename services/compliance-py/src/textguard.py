"""
Client-supplied text is DATA (spec §C design rules, H.21).

- ``has_control_chars``: C0/C1/DEL and lone surrogates are refused at the
  schema edge (422), except tab/newline where a field allows multi-line text
  (none of the fact fields do).
- ``normalize``: NFKC + case-fold + whitespace collapse, for vocabulary checks
  (disclosure labels) and for the Change Watcher's normalized-text hash.
- ``scan_injection``: the onboarding-py guardrail family (onboarding-py
  src/guardrails.py ``_INJECTION``, copied, not imported: services do not
  import each other). A hit NEVER changes a ruling; it is recorded as
  ``injection_text_ignored`` (rule names and counts only) and that is all.
Nothing here evaluates, formats or templates client text.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterator

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")
_CONTROL_STRICT = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

_INJECTION = [
    ("ignore_instructions", re.compile(r"(?i)\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|all|your|the|any)\b.{0,20}\b(instructions?|rules?|guidelines?|policy|policies|prompt|guardrails?)")),
    ("role_reassignment", re.compile(r"(?i)\b(you are now|act as|pretend (?:to be|you are)|from now on you)\b")),
    ("system_prompt_probe", re.compile(r"(?i)\b(system prompt|developer message|hidden instructions?|jailbreak)\b")),
    ("fake_role_marker", re.compile(r"(?im)^[^\S\n]*(system|assistant|developer)\s*:")),
    ("ai_directive_comment", re.compile(r"(?i)<!--\s*(ai|assistant|agent|llm|bot)\b")),
    ("approval_forgery", re.compile(r"(?i)\b(auto[- ]?approve|approve (?:this|me|all)|mark (?:as )?(?:approved|verified|compliant)|skip (?:the )?(?:vetting|verification|compliance|checks?))\b")),
    ("credential_exfiltration", re.compile(r"(?i)\b(reveal|show|send|print|output|tell me)\b.{0,30}\b(password|credential|token|secret|api key)s?\b")),
    ("guarantee_coercion", re.compile(r"(?i)\b(say|tell them|promise|state) (?:that )?(?:we|you) (?:guarantee|will guarantee)\b")),
]
# Bounded scan: at most this many characters of any one string are scanned
# (every pattern above is linear; the cap bounds the total work per request).
SCAN_MAX_CHARS = 16_384


def has_control_chars(value: str, allow_newlines: bool = False) -> bool:
    return bool((_CONTROL if allow_newlines else _CONTROL_STRICT).search(value))


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokens(text: str) -> list[str]:
    """Whole tokens of normalized text; '#' is kept as part of a token."""
    return re.findall(r"#?[^\W_]+", normalize(text))


def scan_injection(text: str) -> list[str]:
    head = text[:SCAN_MAX_CHARS]
    return [name for name, rx in _INJECTION if rx.search(head)]


def iter_strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > 40:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from iter_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from iter_strings(v, depth + 1)


def injection_rules_in(obj: Any) -> list[str]:
    found: set[str] = set()
    for s in iter_strings(obj):
        found.update(scan_injection(s))
    return sorted(found)


_ENTITY = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}
_BLOCKS = ("script", "style", "noscript")


def _strip_markup(text: str) -> str:
    """Linear-time tag stripper (str.find only, no backtracking regex): drops
    <script>/<style>/<noscript> blocks with their content, then every tag.
    An unclosed block or tag drops the rest of the document (fail safe:
    unreadable markup never becomes 'text')."""
    out: list[str] = []
    low = text.lower()
    i, n = 0, len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            out.append(text[i:])
            break
        out.append(text[i:lt])
        gt = text.find(">", lt + 1)
        if gt < 0:
            break
        name = low[lt + 1:gt].split(None, 1)[0].rstrip("/") if gt > lt + 1 else ""
        if name in _BLOCKS:
            end = low.find(f"</{name}", gt + 1)
            if end < 0:
                break
            close = text.find(">", end)
            if close < 0:
                break
            i = close + 1
        else:
            i = gt + 1
        out.append(" ")
    return "".join(out)


def normalized_page_text(raw: bytes) -> str:
    """Change Watcher normalization (spec C.4): decode, strip scripts/styles
    and tags, NFKC, collapse whitespace. Deterministic; no HTML engine."""
    text = _strip_markup(raw.decode("utf-8", errors="replace"))
    for k, v in _ENTITY.items():
        text = text.replace(k, v)
    return " ".join(unicodedata.normalize("NFKC", text).split())
