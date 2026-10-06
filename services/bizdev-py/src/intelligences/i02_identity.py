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

# a TLD is letters, or an IDN TLD in its punycode form (``xn--p1ai``); IDN labels below it are punycode too
_TLD = r"(?:[a-z]{2,24}|xn--[a-z0-9-]{1,59})"
_EMAIL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+" + _TLD + "$")
_DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+" + _TLD + "$")
_SUFFIXES = ("incorporated", "corporation", "company", "limited", "inc", "corp", "co", "llc", "llp", "lp", "ltd",
             "plc", "gmbh", "sa", "ag", "pc", "the")


def idna(dom: str) -> Optional[str]:
    """AEGIS round 2 L4: a domain IDNA-encoded (``münchen.de`` -> ``xn--mnchen-3ya.de``) before it is hashed or
    matched, so the Unicode and punycode spellings are one key. None when it cannot be encoded."""
    d = unicodedata.normalize("NFKC", dom).strip().lower().rstrip(".")
    if d.isascii():
        return d
    try:
        return d.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


def email(raw: str) -> Optional[str]:
    v = raw.strip()
    if "@" in v:
        local, dom = v.rsplit("@", 1)
        dom = idna(dom)
        if dom is None:
            return None
        v = f"{local}@{dom}"
    v = v.lower()
    if len(v) > 254 or not _EMAIL.fullmatch(v):
        return None
    local, dom = v.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if not local or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    return f"{local}@{dom}"


def raw_address(raw: str) -> str:
    """An address that does not parse, normalised only so the same text always gives the same keyed hash (NFKC,
    case folded, surrounding whitespace dropped). Used to HOLD such a sender; it never becomes a contact."""
    return unicodedata.normalize("NFKC", raw).strip().casefold()[:1000]


def domain(raw: str) -> Optional[str]:
    v = idna(raw)
    if v is None:
        return None
    v = v[4:] if v.startswith("www.") else v
    return v if len(v) <= 253 and _DOMAIN.fullmatch(v) else None


def org_key(name: str) -> Optional[str]:
    """``Acme, Inc.`` / ``ACME Incorporated`` / ``The Acme Co`` / Cyrillic ``Аcme`` -> ``acme``: NFKC, accents
    dropped, case folded, Cyrillic / Greek lookalikes folded with sales-py's confusables table (the one i09 uses,
    AEGIS round 1 Low), every non-alphanumeric removed after dropping common legal suffixes. None when nothing is
    left."""
    from intelligences.i09_replies import _CONFUSABLE
    t = unicodedata.normalize("NFKC", name)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).casefold()
    t = t.translate(_CONFUSABLE)
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


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.(?:[A-Za-z]{2,24}|xn--[A-Za-z0-9-]{1,59})")


def all_emails_in(text: str, limit: int = 2000) -> list[str]:
    """Every distinct canonical address written in a text (after NFKC), up to ``limit``."""
    out: list[str] = []
    seen = set()
    for m in _EMAIL_IN_TEXT.findall(unicodedata.normalize("NFKC", text or ""))[:limit]:
        e = email(m)
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def emails_in(text: str) -> list[str]:
    """Addresses written in a text, after NFKC (fullwidth ``ｓａｍ＠ｏｔｈｅｒ．ｔｅｓｔ`` is ``sam@other.test``)."""
    out = []
    for m in _EMAIL_IN_TEXT.findall(unicodedata.normalize("NFKC", text or ""))[:20]:
        e = email(m)
        if e and e not in out:
            out.append(e)
    return out[:5]
