"""Contact and counterparty identity (ADR 0016 decisions 9 and 20). Copied from sales-py's i02_identity (email and
domain rules, keyed hashes); phone handling dropped (this department never texts or calls).

Decides: the canonical form of an email (lowercased, ``+tag`` removed), a domain (lowercased, ``www.`` removed), a
counterparty name key (letters and digits only, legal suffixes dropped), and HMAC-SHA256 hashes under the service's
PII key. Never: stores, looks up or exports a raw value through the ledger."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Optional

NUMBER = 2
NAME = "identity"
DECIDES = "canonical email/domain/counterparty keys and their keyed hashes"

_EMAIL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_SUFFIXES = ("incorporated", "corporation", "company", "limited", "inc", "corp", "co", "llc", "llp", "lp", "ltd",
             "plc", "gmbh", "sa", "ag", "pc", "the")


def email(raw: str) -> Optional[str]:
    v = raw.strip().lower()
    if len(v) > 254 or not _EMAIL.fullmatch(v):
        return None
    local, dom = v.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if not local or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    return f"{local}@{dom}"


def domain(raw: str) -> Optional[str]:
    v = raw.strip().lower().rstrip(".")
    v = v[4:] if v.startswith("www.") else v
    return v if len(v) <= 253 and _DOMAIN.fullmatch(v) else None


def org_key(name: str) -> Optional[str]:
    """``Acme, Inc.`` / ``ACME Incorporated`` / ``The Acme Co`` -> ``acme``: NFKC, accents dropped, case folded, every
    non-alphanumeric removed after dropping common legal suffixes. None when nothing is left."""
    t = unicodedata.normalize("NFKC", name)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).casefold()
    words = [w for w in re.split(r"[^0-9a-z]+", t) if w]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    key = "".join(words)
    return key or None


def keyed(key: bytes, kind: str, value: str) -> str:
    """``<kind>:<hex>``: HMAC-SHA256 under the PII key, so a recorded hash cannot be reversed by guessing."""
    return f"{kind}:" + hmac.new(key, f"{kind}\x00{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def key_fingerprint(key: bytes) -> str:
    """Bound in the log at first start: a changed key would silently empty the suppression list (refused)."""
    return hmac.new(key, b"bizdev-py pii key fingerprint", hashlib.sha256).hexdigest()[:32]


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")


def emails_in(text: str) -> list[str]:
    out = []
    for m in _EMAIL_IN_TEXT.findall(text or "")[:20]:
        e = email(m)
        if e and e not in out:
            out.append(e)
    return out[:5]
