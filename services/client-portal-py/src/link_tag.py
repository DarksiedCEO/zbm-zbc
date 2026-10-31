"""Self-authenticating client acceptance link tokens (ADR 0028, AEGIS r2 N-M1). Standard library only; the
client-portal service holds the same function and the same key (PORTAL_LINK_TAG_KEY = LEGAL_LINK_TAG_KEY), so it can
refuse a forged or mistyped token without asking Legal or spending any shared budget.

token = base64url(random[32] || HMAC-SHA256(key, "zbm-link-v1" 0x1F || random)[:16]), no padding: 64 characters.
The 32 random bytes are the secret; the 16-byte tag only proves the token was minted with the key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

DOMAIN = b"zbm-link-v1\x1f"
RANDOM_BYTES, TAG_BYTES = 32, 16
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{64}$")


def _tag(key: bytes, random: bytes) -> bytes:
    return hmac.new(key, DOMAIN + random, hashlib.sha256).digest()[:TAG_BYTES]


def mint(key: bytes) -> str:
    random = secrets.token_bytes(RANDOM_BYTES)
    return base64.urlsafe_b64encode(random + _tag(key, random)).decode("ascii").rstrip("=")


def valid(token: str, key: bytes) -> bool:
    """Constant time in the tag comparison; a token of the wrong shape is refused before any HMAC."""
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or not key:
        return False
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        return False
    if len(raw) != RANDOM_BYTES + TAG_BYTES:
        return False
    return hmac.compare_digest(raw[RANDOM_BYTES:], _tag(key, raw[:RANDOM_BYTES]))
