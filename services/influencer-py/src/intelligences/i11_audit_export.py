"""Audit export with personal data minimised (ADR 0015 decision 16).

Decides: what an exported log record shows. Emails and handles are dropped (their keyed hashes, stored beside them,
remain); names, captions, briefs, DM texts, template bodies and notes are replaced by their SHA-256; references a
caller supplied (tax, evidence, envelope, post) by their SHA-256. Nothing else personal is in the log to begin with (no
date of birth, age, raw tax id, government id or payment data is ever accepted). Never: exports a raw email, handle,
name, text or tax reference."""

from __future__ import annotations

import hashlib
from typing import Any

NUMBER = 11
NAME = "audit_export"
DECIDES = "the minimised form of each exported record"

DROPPED = ("email", "handle", "to")
DIGESTED = ("display_name", "first_name", "verified_first_name", "name", "partner_name", "text", "body", "subject",
            "caption", "title", "note", "tax_ref", "evidence_ref", "envelope_ref", "post_ref", "payee_ref",
            "finance_ref", "provider_ref", "partner_ref", "person_key")


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def minimise(obj: Any, depth: int = 0) -> Any:
    if depth > 20:
        return None
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in DROPPED:
                continue
            if k in DIGESTED and isinstance(v, str):
                out[f"{k}_sha256"] = _sha(v)
                continue
            out[k] = minimise(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [minimise(v, depth + 1) for v in obj]
    return obj
