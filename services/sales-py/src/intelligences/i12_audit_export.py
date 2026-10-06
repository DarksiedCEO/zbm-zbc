"""Audit export with personal data minimised (ADR 0013 decision 16).

Decides: what an exported log record shows. Emails and phone numbers are replaced by their keyed hashes, names and
free-text notes by their SHA-256, and nothing else personal is in the log to begin with (no date of birth, government
id or payment data is ever accepted). Never: exports a raw email, phone, name or note."""

from __future__ import annotations

import hashlib
from typing import Any

NUMBER = 12
NAME = "audit_export"
DECIDES = "the minimised form of each exported record"

HASHED = {"email": "email_hash", "phone": "phone_hash"}
DIGESTED = ("name", "note", "title", "referrer_name", "first_name")


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def minimise(obj: Any, depth: int = 0) -> Any:
    if depth > 20:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in HASHED:
                if HASHED[k] not in obj:
                    out[HASHED[k]] = None          # a raw value with no stored hash is dropped, never exported
                continue
            if k in DIGESTED and isinstance(v, str):
                out[f"{k}_sha256"] = _sha(v)
                continue
            out[k] = minimise(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [minimise(v, depth + 1) for v in obj]
    return obj
