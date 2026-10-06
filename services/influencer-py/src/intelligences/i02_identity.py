"""Influencer identity: normalise an email and a platform handle, and derive the keyed hashes used for dedupe,
suppression, holds, ledger events and the audit export (ADR 0015 decision 7).

Decides: the sending address (the email as given, lowercased and validated), the CANONICAL email that dedupe,
suppression and holds match on (``+tag`` removed for every domain; for gmail.com / googlemail.com also every dot in the
local part removed and the domain read as gmail.com — AEGIS R1-L2), the address inside ``Name <addr>`` (replies), the
canonical handle (NFKC, lowercased,
one leading ``@`` removed; ASCII letters, digits, ``.``, ``_`` and ``-`` only, 1..60 characters; anything else is
refused, never altered), and HMAC-SHA256 hashes under the service's PII key: ``email:<hex>`` and ``handle:<hex>`` (the
platform is part of the hashed value, so ``@acme`` on TikTok and on X are different identities). Never: stores, looks
up or exports a raw value outside the system-of-record log."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Optional

NUMBER = 2
NAME = "influencer_identity"
DECIDES = "canonical email and handle, and their keyed hashes"

PLATFORMS = ("instagram", "tiktok", "x", "youtube")
_EMAIL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_HANDLE = re.compile(r"[a-z0-9._-]{1,60}")


GMAIL = ("gmail.com", "googlemail.com")
_NAMED = re.compile(r"^[^<>]{0,200}<\s*([^<>\s]{3,254})\s*>\s*$")


def email(raw: str) -> Optional[str]:
    """The address as given, lowercased and validated (it is where mail goes); None when it is not an address."""
    v = raw.strip().lower()
    if len(v) > 254 or not v.isascii() or not _EMAIL.fullmatch(v):
        return None
    local, dom = v.rsplit("@", 1)
    if local.startswith(".") or local.endswith(".") or ".." in local or not local.split("+", 1)[0]:
        return None
    return v


def canonical(address: str) -> str:
    """What one mailbox's addresses have in common: ``+tag`` removed everywhere; gmail's dots removed too."""
    local, dom = address.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if dom in GMAIL:
        local, dom = local.replace(".", ""), "gmail.com"
    return f"{local}@{dom}"


def sender_email(raw) -> Optional[str]:
    """A reply's From: a bare address or ``Name <addr>``; None when no address can be read (never an error)."""
    if not isinstance(raw, str):
        return None
    m = _NAMED.fullmatch(raw.strip())
    return email(m.group(1) if m else raw)


def handle(platform: str, raw: str) -> Optional[str]:
    if platform not in PLATFORMS or not isinstance(raw, str):
        return None
    v = unicodedata.normalize("NFKC", raw).strip().lower()
    if v.startswith("@"):
        v = v[1:]
    if not v.isascii() or not _HANDLE.fullmatch(v) or v.strip("._-") == "":
        return None
    return v


def keyed(key: bytes, kind: str, value: str) -> str:
    """``<kind>:<hex>``: HMAC-SHA256 under the PII key, so an exported hash cannot be reversed by guessing."""
    return f"{kind}:" + hmac.new(key, f"{kind}\x00{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def email_hash(key: bytes, address: str) -> str:
    """Keyed hash of the CANONICAL form, so ``jane.doe+x@gmail.com`` and ``janedoe@googlemail.com`` match."""
    return keyed(key, "email", canonical(address))


def handle_hash(key: bytes, platform: str, canonical_handle: str) -> str:
    return keyed(key, "handle", f"{platform}\x00{canonical_handle}")


def key_fingerprint(key: bytes) -> str:
    """Bound in the log at first start: a changed key would silently empty the suppression list (refused)."""
    return hmac.new(key, b"influencer-py pii key fingerprint", hashlib.sha256).hexdigest()[:32]
