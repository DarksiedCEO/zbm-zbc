"""
Caller-supplied text is DATA (spec §B.1, §C.8.3). Copied from finance-py (itself verification-py's / compliance-py's):

- ``has_control_chars``: C0/C1/DEL and lone surrogates are refused at the schema edge (422); the finding's free-text
  fields allow tab/newline (``allow_newlines=True``) because a reproduction is multi-line by nature;
- ``scan_injection``: the onboarding-py guardrail family. A hit NEVER changes the outcome; it is recorded as
  ``injection_text_ignored`` (rule names and counts only) and the text still reaches the engineer inside the brief's
  data block — the guardrail (not the brief) stops an instruction in it from becoming an action (A6);
- ``secret_shapes``: the shapes a provider key, a bearer token or our own tokens take. Nothing matching them may
  enter a model prompt, a log line, a ledger payload, an evidence file or an exception text (spec §B, test G7).
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
    ("approval_forgery", re.compile(r"(?i)\b(auto[- ]?approve|approve (?:this|me|all)|mark (?:as )?(?:approved|verified|compliant|certified|fixed|done)|certify (?:this|me|all|it)|skip (?:the )?(?:vetting|verification|compliance|checks?|tests?|suite))\b")),
    ("credential_exfiltration", re.compile(r"(?i)\b(reveal|show|send|print|output|tell me)\b.{0,30}\b(password|credential|token|secret|api key)s?\b")),
    ("remote_git_directive", re.compile(r"(?i)\bgit\s+(push|merge|pull|fetch)\b|\bdelete\s+(tests?|the tests?|evidence)\b|\bbefore reporting (done|fixed|complete)\b")),
]
# Bounded scan: at most this many characters of any one string are scanned (every pattern is linear).
SCAN_MAX_CHARS = 16_384

# Secret shapes (G7): provider keys and long bearer-like tokens. Our own tokens are ≥ 32 printable ASCII; a scan for
# a *known* value (``contains_value``) is what the suite uses for the configured tokens and the fake key.
_SECRET_SHAPES = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"sk-(?!ant-)[A-Za-z0-9_\-]{16,}")),
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("x_api_key_header", re.compile(r"(?i)\bx-api-key\s*[:=]\s*\S{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def has_control_chars(value: str, allow_newlines: bool = False) -> bool:
    return bool((_CONTROL if allow_newlines else _CONTROL_STRICT).search(value))


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


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


def secret_shapes(text: str) -> list[str]:
    """Names of the secret shapes found in ``text`` (empty = clean)."""
    if not isinstance(text, str) or not text:
        return []
    return [name for name, rx in _SECRET_SHAPES if rx.search(text)]


def secret_shapes_in(obj: Any) -> list[str]:
    found: set[str] = set()
    for s in iter_strings(obj):
        found.update(secret_shapes(s))
    return sorted(found)


def redact(text: str) -> str:
    """Replace every secret-shaped span with ``[redacted]`` (used before any text is stored or shown)."""
    if not isinstance(text, str):
        return text
    out = text
    for _, rx in _SECRET_SHAPES:
        out = rx.sub("[redacted]", out)
    return out


def contains_value(haystack: Any, values: Iterator[str] | list[str]) -> bool:
    vals = [v for v in values if isinstance(v, str) and v]
    for s in iter_strings(haystack):
        for v in vals:
            if v in s:
                return True
    return False
