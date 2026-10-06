"""Contact identity: normalise an email, phone and company domain, and derive the keyed hashes used for dedupe,
suppression and the audit export (ADR 0013 decision 7).

Decides: the canonical form of an email (lowercased, ``+tag`` removed), a phone (E.164; a 10-digit US number gets
+1), a domain (lowercased, ``www.`` removed), whether an email is on a free-mail provider (then its domain is not a
company domain), and HMAC-SHA256 hashes under the service's PII key. Never: stores, looks up or exports a raw value."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Optional

NUMBER = 2
NAME = "contact_identity"
DECIDES = "canonical email/phone/domain and their keyed hashes"

FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
                       "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
                       "protonmail.com", "gmx.com", "mail.com", "zoho.com", "yandex.com", "hey.com"})
_EMAIL = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_DOMAIN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")


def email(raw: str) -> Optional[str]:
    v = raw.strip().lower()
    if len(v) > 254 or not _EMAIL.fullmatch(v):
        return None
    local, dom = v.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if not local or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    return f"{local}@{dom}"


def phone(raw: str) -> Optional[str]:
    v = re.sub(r"[\s().-]", "", raw.strip())
    if re.fullmatch(r"[0-9]{10}", v):
        v = "+1" + v
    elif re.fullmatch(r"1[0-9]{10}", v):
        v = "+" + v
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", v):
        return None
    return v


def domain(raw: str) -> Optional[str]:
    v = raw.strip().lower().rstrip(".")
    v = v[4:] if v.startswith("www.") else v
    return v if len(v) <= 253 and _DOMAIN.fullmatch(v) else None


def email_domain(canonical_email: str) -> str:
    return canonical_email.rsplit("@", 1)[1]


def company_domain(canonical_email: Optional[str], account_domain: Optional[str]) -> Optional[str]:
    if account_domain:
        return account_domain
    if canonical_email and email_domain(canonical_email) not in FREE_MAIL:
        return email_domain(canonical_email)
    return None


def keyed(key: bytes, kind: str, value: str) -> str:
    """``<kind>:<hex>``: HMAC-SHA256 under the PII key, so an exported hash cannot be reversed by guessing."""
    return f"{kind}:" + hmac.new(key, f"{kind}\x00{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def key_fingerprint(key: bytes) -> str:
    """Bound in the log at first start: a changed key would silently empty the suppression list (refused)."""
    return hmac.new(key, b"sales-py pii key fingerprint", hashlib.sha256).hexdigest()[:32]
