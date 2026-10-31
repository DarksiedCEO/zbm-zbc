"""Self-authenticating client acceptance link tokens (ADR 0028, AEGIS r2 N-M1, r3 N3-L4). Standard library only; the
client-portal service holds this same file (byte-identical, a test checks it) and the same key
(PORTAL_LINK_TAG_KEY = LEGAL_LINK_TAG_KEY), so it can refuse a forged or mistyped token without asking Legal.

token = base64url(VERSION[1] || random[32] || HMAC-SHA256(key, "zbm-link" 0x1F VERSION 0x1F random)[:16]), no
padding: 66 characters, canonical only (the one spelling that re-encodes to itself). The 32 random bytes are the secret; the tag proves the token was minted with the key; the
version byte lets a service that reads another format say so (deploy skew) instead of refusing every link silently.

Any change to this file ships in BOTH services in the same deploy (legal-py and client-portal-py); bump VERSION.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

VERSION = 1
RANDOM_BYTES, TAG_BYTES = 32, 16
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{66}$")


def _tag(key: bytes, version: int, random: bytes) -> bytes:
    msg = b"zbm-link\x1f" + bytes([version]) + b"\x1f" + random
    return hmac.new(key, msg, hashlib.sha256).digest()[:TAG_BYTES]


def mint(key: bytes) -> str:
    random = secrets.token_bytes(RANDOM_BYTES)
    raw = bytes([VERSION]) + random + _tag(key, VERSION, random)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _raw(token: str):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 1 + RANDOM_BYTES + TAG_BYTES:
        return None
    # AEGIS r4 N4-L1: one spelling per token. The last character carries 4 unused bits, so 16 strings decode to the
    # same bytes; only the one that re-encodes to itself (the one Legal minted) is a token.
    return raw if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") == token else None


def version_of(token: str):
    """The format version a right-shaped token claims (None for anything else)."""
    raw = _raw(token)
    return raw[0] if raw else None


def valid(token: str, key: bytes) -> bool:
    """This format's version, and the tag under ``key`` (constant-time compare); anything else is False."""
    raw = _raw(token)
    if raw is None or not key or raw[0] != VERSION:
        return False
    return hmac.compare_digest(raw[1 + RANDOM_BYTES:], _tag(key, VERSION, raw[1:1 + RANDOM_BYTES]))


def log_fingerprint(token: str) -> str:
    """AEGIS r3 N3-M1: what a log line names a link by: the first 8 hex characters of the token's SHA-256 (the token
    is 256 random bits, so this reveals nothing usable). Legal shows the same value on the link."""
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()[:8]


def key_id(key: bytes) -> str:
    """Both services report this on /health, so Andre can see they hold the same key (32 bits of the key's hash)."""
    return hashlib.sha256(key).hexdigest()[:8]
