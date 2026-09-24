"""
Credential redaction — defense in depth for "no credential ever appears in
an API response, log line, error message or ledger payload".

The STRUCTURAL defense is elsewhere: no model has a secret field, inbound
models forbid unknown fields, validation errors never echo input, the
vault stand-in refuses to hold anything. This module is the second layer
for free text a client might paste a credential into (an intake answer, a
note, a website blob): anything that looks like a credential is replaced
by ``[REDACTED]`` at ingest, before it is stored, echoed, logged or hashed.

Honest limit (ADR 0004 gap): a password that looks like an ordinary English
word, typed with no cue ("password is", "pw:"), cannot be recognised by
pattern. The structural defense still holds for every field built to carry
access (none can carry a secret); free text is best-effort.
"""

from __future__ import annotations

import logging
import re
from typing import Any

REDACTED = "[REDACTED]"

# key/cue followed by a value: "password: x", "my pw is x", "api key = x"
_CUE_STRONG = re.compile(
    r"(?i)\b(password|passwd|passcode|pwd|pw)\b"
    r"(\s*(?:is|was|=|:|->)\s*|\s+)(\"[^\"]*\"|'[^']*'|\S+)"
)
_CUE = re.compile(
    r"(?i)\b(pass|secret|token|api[\s_-]?key|access[\s_-]?token|"
    r"refresh[\s_-]?token|client[\s_-]?secret|private[\s_-]?key|2fa(?:\s+code)?|otp|login)"
    r"(\s*(?:is|was|=|:|->)\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
# well-known credential prefixes
_PREFIXED = re.compile(
    r"\b(sk_(?:live|test)_[A-Za-z0-9]{6,}|rk_(?:live|test)_[A-Za-z0-9]{6,}|shp(?:at|ss|ca|pa)_[A-Za-z0-9]{8,}|"
    r"EAA[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9_\-.]{10,}|1//[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{10,}|"
    r"AKIA[0-9A-Z]{12,}|xox[abpr]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*)"
)
# high-entropy token: >=20 chars, upper+lower+digit, token charset, no dots
_TOKENISH = re.compile(r"(?<![A-Za-z0-9_\-+/=])[A-Za-z0-9_\-+/=]{20,}(?![A-Za-z0-9_\-+/=])")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")

_SECRET_KEYS = {
    "password", "passwd", "pwd", "secret", "token", "access_token", "refresh_token", "id_token",
    "client_secret", "api_key", "apikey", "private_key", "authorization", "credential", "credentials",
    "otp", "passcode",
}


def _looks_high_entropy(tok: str) -> bool:
    return (
        any(c.isupper() for c in tok)
        and any(c.islower() for c in tok)
        and any(c.isdigit() for c in tok)
    )


def scrub(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = _BEARER.sub(f"Bearer {REDACTED}", text)
    out = _CUE_STRONG.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _CUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _PREFIXED.sub(REDACTED, out)
    out = _TOKENISH.sub(lambda m: REDACTED if _looks_high_entropy(m.group(0)) else m.group(0), out)
    return out


def contains_credential(text: str) -> bool:
    return isinstance(text, str) and scrub(text) != text


def scrub_obj(obj: Any) -> Any:
    """Recursively scrub strings; drop values under secret-named keys."""
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                clean[k] = REDACTED
            else:
                clean[k] = scrub_obj(v)
        return clean
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    if isinstance(obj, str):
        return scrub(obj)
    return obj


class ScrubbingFilter(logging.Filter):
    """Logging filter: rewrites every record's message with ``scrub``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = scrub(msg)
        record.args = None
        return True


_factory_installed = False


def install_log_scrubbing() -> None:
    """Scrub EVERY log record in the process, whatever logger emits it.

    Logger-level filters are not inherited by child loggers (a filter on
    "onboarding" does not see records from "onboarding.guardrails"), so the
    WIP version could miss records. Wrapping the record factory scrubs the
    message of every record at creation, including uvicorn's access log
    (whose request path could carry an identifier). Exception tracebacks
    are handled separately: the API never lets an exception message that
    could contain input reach a log (see api.py's generic handler)."""
    global _factory_installed
    if _factory_installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old(*args, **kwargs)
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        record.msg = scrub(msg)
        record.args = None
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True
