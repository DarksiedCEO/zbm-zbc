"""
Short-lived service credentials (ADR 0012 decisions 18-21).

A department proves who it is with its bootstrap caller token ONCE per credential and gets back a signed token,
valid for at most MAX_TTL_S (15 minutes), naming exactly one audience (the service it will call). The token is a
JWT signed with Ed25519 (``alg: EdDSA``); the signing key is generated inside Cybersecurity 22, sealed in the
vault and never released. Verifiers fetch the public keys (``GET /sec/v1/identity/jwks``) and the deny list of
frozen callers (``GET /sec/v1/identity/denylist``).

``verify`` is written to be copied byte-for-byte into the other services when they are wired (standard library
plus ``cryptography``): it checks the header (alg EdDSA, typ JWT, a known kid), the signature, iss, aud, sub,
iat/nbf/exp with a bounded clock skew, a TTL no longer than MAX_TTL_S, and the deny list.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

ISSUER = "zbm-cybersecurity-22"
MAX_TTL_S = 15 * 60
DEFAULT_TTL_S = 10 * 60
MAX_SKEW_S = 60
MAX_TOKEN_CHARS = 2048
_SEG = re.compile(r"[A-Za-z0-9_-]{1,1400}")
_NAME = re.compile(r"[a-z0-9_]{1,40}")
_KID = re.compile(r"[a-z0-9-]{1,40}")
_JTI = re.compile(r"[A-Za-z0-9_-]{16,64}")


class TokenInvalid(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(seg: str) -> bytes:
    if not _SEG.fullmatch(seg):
        raise TokenInvalid("TOKEN_MALFORMED")
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
    except (binascii.Error, ValueError):
        raise TokenInvalid("TOKEN_MALFORMED") from None
    if _b64e(raw) != seg:
        raise TokenInvalid("TOKEN_MALFORMED")
    return raw


def _no_duplicates(pairs):
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError("duplicate member")
    return out


def _json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def public_jwk(kid: str, private_raw: bytes) -> dict:
    pub = ed25519.Ed25519PrivateKey.from_private_bytes(private_raw).public_key()
    x = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig", "kid": kid, "x": _b64e(x)}


def mint(kid: str, private_raw: bytes, subject: str, audience: str, scope: tuple[str, ...], iat: int, ttl_s: int,
         jti: str) -> str:
    if not (_NAME.fullmatch(subject) and _NAME.fullmatch(audience) and _KID.fullmatch(kid) and _JTI.fullmatch(jti)):
        raise ValueError("token field format")
    if not (60 <= ttl_s <= MAX_TTL_S):
        raise ValueError("token lifetime out of range")
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    claims = {"iss": ISSUER, "sub": subject, "aud": audience, "iat": iat, "nbf": iat, "exp": iat + ttl_s,
              "jti": jti, "scope": " ".join(scope)}
    signing_input = _b64e(_json(header)) + "." + _b64e(_json(claims))
    sig = ed25519.Ed25519PrivateKey.from_private_bytes(private_raw).sign(signing_input.encode("ascii"))
    return signing_input + "." + _b64e(sig)


@dataclass(frozen=True)
class Verified:
    subject: str
    audience: str
    scope: tuple[str, ...]
    jti: str
    expires_at: int


def verify(token: str, jwks: Mapping[str, dict], audience: str, now: int,
           denied_subjects: frozenset = frozenset(), lockdown: bool = False) -> Verified:
    """Raises TokenInvalid(code) on any failure; never a partial answer."""
    if lockdown:
        raise TokenInvalid("TOKEN_LOCKDOWN")      # the deny list says every caller is frozen (AEGIS L6)
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        raise TokenInvalid("TOKEN_MALFORMED")
    h_seg, c_seg, s_seg = token.split(".")
    try:
        header = json.loads(_b64d(h_seg), object_pairs_hook=_no_duplicates)
        claims = json.loads(_b64d(c_seg), object_pairs_hook=_no_duplicates)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise TokenInvalid("TOKEN_MALFORMED") from None
    if not isinstance(header, dict) or set(header) != {"alg", "typ", "kid"} or header["alg"] != "EdDSA" \
            or header["typ"] != "JWT":
        raise TokenInvalid("TOKEN_HEADER_REFUSED")
    jwk = jwks.get(header["kid"]) if isinstance(header["kid"], str) else None
    if not jwk or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise TokenInvalid("TOKEN_UNKNOWN_KEY")
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(_b64d(jwk["x"]))
        pub.verify(_b64d(s_seg), (h_seg + "." + c_seg).encode("ascii"))
    except (InvalidSignature, ValueError, KeyError):
        raise TokenInvalid("TOKEN_SIGNATURE_INVALID") from None
    want = {"iss", "sub", "aud", "iat", "nbf", "exp", "jti", "scope"}
    if not isinstance(claims, dict) or set(claims) != want:
        raise TokenInvalid("TOKEN_CLAIMS_REFUSED")
    ints = [claims[k] for k in ("iat", "nbf", "exp")]
    if not all(type(v) is int for v in ints) or not all(isinstance(claims[k], str) for k in ("iss", "sub", "aud",
                                                                                             "jti", "scope")):
        raise TokenInvalid("TOKEN_CLAIMS_REFUSED")
    if claims["iss"] != ISSUER:
        raise TokenInvalid("TOKEN_ISSUER_REFUSED")
    if claims["aud"] != audience:
        raise TokenInvalid("TOKEN_AUDIENCE_REFUSED")
    if not _NAME.fullmatch(claims["sub"]):
        raise TokenInvalid("TOKEN_CLAIMS_REFUSED")
    iat, nbf, exp = ints
    if exp - iat > MAX_TTL_S or exp <= iat or nbf < iat:
        raise TokenInvalid("TOKEN_LIFETIME_REFUSED")
    if now + MAX_SKEW_S < nbf or now >= exp:
        raise TokenInvalid("TOKEN_EXPIRED")
    if claims["sub"] in denied_subjects:
        raise TokenInvalid("TOKEN_SUBJECT_FROZEN")
    scope = tuple(s for s in claims["scope"].split(" ") if s)
    return Verified(claims["sub"], claims["aud"], scope, claims["jti"], exp)


def jwks_map(keys: list[dict]) -> dict[str, dict]:
    return {k["kid"]: k for k in keys if isinstance(k, dict) and isinstance(k.get("kid"), str)}


def new_private_key() -> bytes:
    return ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw,
                                                             serialization.PrivateFormat.Raw,
                                                             serialization.NoEncryption())


def kid_for(private_raw: bytes) -> str:
    import hashlib
    pub = ed25519.Ed25519PrivateKey.from_private_bytes(private_raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed-" + hashlib.sha256(pub).hexdigest()[:16]


__all__ = ["DEFAULT_TTL_S", "ISSUER", "MAX_TTL_S", "TokenInvalid", "Verified", "jwks_map", "kid_for", "mint",
           "new_private_key", "public_jwk", "verify"]
