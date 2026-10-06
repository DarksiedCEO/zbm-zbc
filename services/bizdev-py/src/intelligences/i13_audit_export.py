"""Audit export with personal data minimised (ADR 0016 decision 24). Copied from sales-py's i12_audit_export.

Decides: what an exported log record shows. Emails are replaced by their keyed hashes (a raw value with no stored
hash is dropped), names, notes, labels and free text by their SHA-256. Nothing else personal is in the log to begin
with. Never: exports a raw email, name, note or text."""

from __future__ import annotations

import hashlib
from typing import Any

NUMBER = 13
NAME = "audit_export"
DECIDES = "the minimised form of each exported record"

HASHED = {"email": "email_hash"}
DIGESTED = ("name", "note", "notes", "title", "label", "text", "custom", "subject", "body", "first_name",
            "counterparty_name", "company")


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
                    out[HASHED[k]] = None
                continue
            if k in DIGESTED and isinstance(v, str):
                out[f"{k}_sha256"] = _sha(v)
                continue
            out[k] = minimise(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [minimise(v, depth + 1) for v in obj]
    return obj
