"""
Passkey (WebAuthn Level 2) verification for Andre's approvals (ADR 0012 decisions 10-14).

Andre approves every sensitive action with a passkey or security key (YubiKey). Nothing else is Andre: no
password, no text-message code, no bearer token. The browser ceremony (navigator.credentials.create / .get) runs
in Andre's dashboard; this module verifies what it hands back.

Registration (``verify_registration``): clientDataJSON type ``webauthn.create``, the challenge we issued, an
allowed origin, not cross-origin; attestation format ``none`` only (we request attestation "none": the security
of enrolment comes from how enrolment is authorised, not from a vendor certificate); authenticator data with
the RP id hash, user present (UP), user verified (UV: PIN or biometric) and attested credential data (AT); a
COSE public key of ES256, EdDSA (Ed25519) or RS256.

Assertion (``verify_assertion``): type ``webauthn.get``, our challenge, an allowed origin, not cross-origin; RP
id hash, UP and UV; no attested data; the signature over ``authenticatorData || SHA-256(clientDataJSON)``
with the enrolled key; and the signature counter: when either counter is non-zero the new one must be
greater, else the key may have been cloned (``CounterRegression``: the approval is refused and an incident
opened). Every input is size-bounded and base64url-strict.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

import cbor

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40
FLAG_ED = 0x80

ALG_ES256 = -7
ALG_EDDSA = -8
ALG_RS256 = -257
ALGS = (ALG_ES256, ALG_EDDSA, ALG_RS256)

MAX_CLIENT_DATA = 4096
MAX_AUTH_DATA = 4096
MAX_SIGNATURE = 1024
MAX_ATTESTATION = 16 * 1024
MAX_CREDENTIAL_ID = 1023
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


class WebAuthnError(ValueError):
    """The ceremony result does not verify; ``code`` is a stable reason code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CounterRegression(WebAuthnError):
    def __init__(self):
        super().__init__("PASSKEY_COUNTER_REGRESSION")


def b64url_decode(value, max_bytes: int, what: str) -> bytes:
    if not isinstance(value, str) or len(value) > (max_bytes * 4) // 3 + 4 or not _B64URL.fullmatch(value):
        raise WebAuthnError(f"PASSKEY_BAD_{what}")
    try:
        out = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise WebAuthnError(f"PASSKEY_BAD_{what}") from None
    if not out or len(out) > max_bytes or b64url_encode(out) != value:
        raise WebAuthnError(f"PASSKEY_BAD_{what}")   # non-canonical encodings refused
    return out


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class Credential:
    credential_id: str      # base64url
    alg: int
    public_key_spki: str    # base64 (standard) of the SubjectPublicKeyInfo DER
    sign_count: int
    aaguid: str             # hex
    backup_eligible: bool


@dataclass(frozen=True)
class Relying:
    rp_id: str
    origins: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.rp_id) and bool(self.origins)


def _client_data(raw_b64: str, expected_type: str, expected_challenge: bytes, rp: Relying) -> bytes:
    raw = b64url_decode(raw_b64, MAX_CLIENT_DATA, "CLIENT_DATA")
    try:
        cd = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WebAuthnError("PASSKEY_BAD_CLIENT_DATA") from None
    if not isinstance(cd, dict) or cd.get("type") != expected_type:
        raise WebAuthnError("PASSKEY_WRONG_CEREMONY")
    challenge = cd.get("challenge")
    try:
        got = b64url_decode(challenge, 64, "CHALLENGE")
    except WebAuthnError:
        raise WebAuthnError("PASSKEY_CHALLENGE_MISMATCH") from None
    if not hmac.compare_digest(got, expected_challenge):
        raise WebAuthnError("PASSKEY_CHALLENGE_MISMATCH")
    if cd.get("origin") not in rp.origins:
        raise WebAuthnError("PASSKEY_ORIGIN_REFUSED")
    if cd.get("crossOrigin") not in (None, False) or "topOrigin" in cd:
        raise WebAuthnError("PASSKEY_CROSS_ORIGIN_REFUSED")
    return raw


@dataclass(frozen=True)
class AuthData:
    rp_id_hash: bytes
    flags: int
    sign_count: int
    credential_id: Optional[bytes] = None
    aaguid: Optional[bytes] = None
    cose_key: Optional[dict] = None


def parse_auth_data(raw: bytes) -> AuthData:
    if len(raw) < 37:
        raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
    rp_hash, flags, count = raw[:32], raw[32], int.from_bytes(raw[33:37], "big")
    rest = raw[37:]
    cred_id = aaguid = cose = None
    if flags & FLAG_AT:
        if len(rest) < 18:
            raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
        aaguid, n = rest[:16], int.from_bytes(rest[16:18], "big")
        if not (16 <= n <= MAX_CREDENTIAL_ID) or len(rest) < 18 + n:
            raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
        cred_id = rest[18:18 + n]
        try:
            cose, used = cbor.loads_prefix(rest[18 + n:])
        except cbor.CBORError:
            raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY") from None
        rest = rest[18 + n + used:]
        if not isinstance(cose, dict):
            raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY")
    if flags & FLAG_ED:
        try:
            ext = cbor.loads(rest)
        except cbor.CBORError:
            raise WebAuthnError("PASSKEY_BAD_AUTH_DATA") from None
        if not isinstance(ext, dict):
            raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
    elif rest:
        raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
    return AuthData(rp_hash, flags, count, cred_id, aaguid, cose)


def _check_flags(ad: AuthData, rp: Relying) -> None:
    if not hmac.compare_digest(ad.rp_id_hash, hashlib.sha256(rp.rp_id.encode("ascii")).digest()):
        raise WebAuthnError("PASSKEY_RP_MISMATCH")
    if not ad.flags & FLAG_UP:
        raise WebAuthnError("PASSKEY_USER_NOT_PRESENT")
    if not ad.flags & FLAG_UV:
        raise WebAuthnError("PASSKEY_USER_NOT_VERIFIED")


def cose_public_key(cose: dict):
    """(alg, public key object) from a COSE_Key map; only the three algorithms we accept."""
    alg, kty = cose.get(3), cose.get(1)
    try:
        if alg == ALG_ES256 and kty == 2 and cose.get(-1) == 1:
            x, y = cose.get(-2), cose.get(-3)
            if not (isinstance(x, bytes) and isinstance(y, bytes) and len(x) == 32 and len(y) == 32):
                raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY")
            return alg, ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + x + y)
        if alg == ALG_EDDSA and kty == 1 and cose.get(-1) == 6:
            x = cose.get(-2)
            if not (isinstance(x, bytes) and len(x) == 32):
                raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY")
            return alg, ed25519.Ed25519PublicKey.from_public_bytes(x)
        if alg == ALG_RS256 and kty == 3:
            n, e = cose.get(-1), cose.get(-2)
            if not (isinstance(n, bytes) and isinstance(e, bytes) and 256 <= len(n) <= 512 and 1 <= len(e) <= 4):
                raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY")
            ni, ei = int.from_bytes(n, "big"), int.from_bytes(e, "big")
            if ni.bit_length() < 2048 or ei < 3 or ei % 2 == 0:
                raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY")
            return alg, rsa.RSAPublicNumbers(ei, ni).public_key()
    except ValueError:
        raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY") from None
    raise WebAuthnError("PASSKEY_ALGORITHM_REFUSED")


def _spki(key) -> str:
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode("ascii")


def load_spki(alg: int, spki_b64: str):
    key = serialization.load_der_public_key(base64.b64decode(spki_b64, validate=True))
    expected = {ALG_ES256: ec.EllipticCurvePublicKey, ALG_EDDSA: ed25519.Ed25519PublicKey,
                ALG_RS256: rsa.RSAPublicKey}[alg]
    if not isinstance(key, expected):
        raise WebAuthnError("PASSKEY_BAD_PUBLIC_KEY")
    return key


def verify_registration(attestation_object_b64: str, client_data_b64: str, challenge: bytes,
                        rp: Relying) -> Credential:
    if not rp.configured:
        raise WebAuthnError("PASSKEY_NOT_CONFIGURED")
    _client_data(client_data_b64, "webauthn.create", challenge, rp)
    att_raw = b64url_decode(attestation_object_b64, MAX_ATTESTATION, "ATTESTATION")
    try:
        att = cbor.loads(att_raw)
    except cbor.CBORError:
        raise WebAuthnError("PASSKEY_BAD_ATTESTATION") from None
    if not isinstance(att, dict) or set(att) != {"fmt", "attStmt", "authData"}:
        raise WebAuthnError("PASSKEY_BAD_ATTESTATION")
    if att["fmt"] != "none" or att["attStmt"] != {}:
        raise WebAuthnError("PASSKEY_ATTESTATION_FORMAT_REFUSED")
    if not isinstance(att["authData"], bytes) or len(att["authData"]) > MAX_AUTH_DATA:
        raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
    ad = parse_auth_data(att["authData"])
    _check_flags(ad, rp)
    if not ad.flags & FLAG_AT or ad.cose_key is None:
        raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
    alg, key = cose_public_key(ad.cose_key)
    return Credential(b64url_encode(ad.credential_id), alg, _spki(key), ad.sign_count, ad.aaguid.hex(),
                      bool(ad.flags & 0x08))


def verify_assertion(cred: Credential, client_data_b64: str, auth_data_b64: str, signature_b64: str,
                     challenge: bytes, rp: Relying) -> int:
    """Returns the authenticator's new signature counter."""
    if not rp.configured:
        raise WebAuthnError("PASSKEY_NOT_CONFIGURED")
    cd_raw = _client_data(client_data_b64, "webauthn.get", challenge, rp)
    ad_raw = b64url_decode(auth_data_b64, MAX_AUTH_DATA, "AUTH_DATA")
    ad = parse_auth_data(ad_raw)
    _check_flags(ad, rp)
    if ad.flags & FLAG_AT:
        raise WebAuthnError("PASSKEY_BAD_AUTH_DATA")
    sig = b64url_decode(signature_b64, MAX_SIGNATURE, "SIGNATURE")
    signed = ad_raw + hashlib.sha256(cd_raw).digest()
    key = load_spki(cred.alg, cred.public_key_spki)
    try:
        if cred.alg == ALG_ES256:
            key.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        elif cred.alg == ALG_EDDSA:
            key.verify(sig, signed)
        else:
            key.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise WebAuthnError("PASSKEY_SIGNATURE_INVALID") from None
    if (ad.sign_count or cred.sign_count) and ad.sign_count <= cred.sign_count:
        raise CounterRegression()
    return ad.sign_count


def allowed_origins(raw: Optional[str]) -> tuple[str, ...]:
    """Comma-separated https origins (no path, no trailing slash); http only for localhost (non-production)."""
    if not raw:
        return ()
    out = []
    for o in (x.strip() for x in raw.split(",")):
        if not re.fullmatch(r"https://[a-z0-9.-]{1,253}(:[0-9]{1,5})?|http://localhost(:[0-9]{1,5})?", o):
            raise ValueError(f"not an origin: {o[:60]!r}")
        out.append(o)
    return tuple(out)


def origins_match_rp(origins: Iterable[str], rp_id: str) -> bool:
    """Every origin's host is the RP id or a subdomain of it (WebAuthn's own rule, checked at start)."""
    for o in origins:
        host = o.split("://", 1)[1].split(":", 1)[0]
        if not (host == rp_id or host.endswith("." + rp_id)):
            return False
    return True
