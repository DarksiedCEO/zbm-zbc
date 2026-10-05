"""
Envelope encryption for the vault (ADR 0012 decisions 3-5).

Every secret version gets its own random 256-bit data key (DEK). The value is sealed with AES-256-GCM under the
DEK; the DEK is wrapped by the key service (the master key never leaves it) and stored next to the ciphertext.
Both layers bind the same context as associated data — secret id, version, owner, kind — so a sealed file moved
to another secret, version or owner fails to open (no cut-and-paste between records).

Key services (the master-key holder; hosting is not chosen yet, so this is a port):
  - ``NotWiredKeyService``  the default: wrap and unwrap raise ``KeyServiceUnavailable``; the vault answers 503.
  - ``LocalFileKeyService`` a 32-byte master key read once from a 0600 file; NON-PRODUCTION ONLY (config refuses
    it without SEC_NON_PRODUCTION=1). The key is in this process's memory, which is exactly what a cloud key
    service (AWS KMS, Google Cloud KMS) avoids; it exists so the vault can be tested end to end.
  - AWS KMS / Google Cloud KMS adapters are NOT BUILT: SEC_KMS=aws or gcp refuses to start until Andre picks the
    host (open item) and the adapter is built and reviewed.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DEK_BYTES = 32
NONCE_BYTES = 12
ENVELOPE_VERSION = 1


class KeyServiceUnavailable(Exception):
    """The master key cannot be used right now (not wired, unreachable, refused)."""


class SealBroken(Exception):
    """A sealed value failed authentication: tampered, moved to another record, or wrapped by another key."""


def context_bytes(context: dict[str, str]) -> bytes:
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in context.items()):
        raise ValueError("context values must be strings")
    return json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class KeyService(Protocol):
    name: str
    key_id: str

    def wrap(self, dek: bytes, context: dict[str, str]) -> bytes: ...

    def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes: ...

    def healthy(self) -> bool: ...


class NotWiredKeyService:
    name = "not_wired"
    key_id = "none"

    def wrap(self, dek, context):
        raise KeyServiceUnavailable("no key service is wired (SEC_KMS unset): the vault cannot seal or open anything")

    def unwrap(self, wrapped, context):
        raise KeyServiceUnavailable("no key service is wired (SEC_KMS unset): the vault cannot seal or open anything")

    def healthy(self) -> bool:
        return False


class LocalFileKeyService:
    """Non-production master key held in memory (see module docstring)."""

    name = "local_file"

    def __init__(self, master_key: bytes):
        if not isinstance(master_key, bytes) or len(master_key) != 32:
            raise ValueError("the local master key must be exactly 32 bytes")
        self._aead = AESGCM(master_key)
        # an identifier of the key that is not the key: a sealed file names the key that wrapped it
        self.key_id = "lf-" + hmac.new(master_key, b"zbm-sec22-key-id", "sha256").hexdigest()[:16]

    def wrap(self, dek: bytes, context: dict[str, str]) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, dek, context_bytes({**context, "layer": "dek"}))

    def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        if len(wrapped) != NONCE_BYTES + DEK_BYTES + 16:
            raise SealBroken("wrapped key has the wrong length")
        try:
            return self._aead.decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:],
                                      context_bytes({**context, "layer": "dek"}))
        except InvalidTag:
            raise SealBroken("wrapped key failed authentication") from None

    def healthy(self) -> bool:
        return True


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s, what: str) -> bytes:
    if not isinstance(s, str) or len(s) > 90_000:
        raise SealBroken(f"{what} is not base64")
    try:
        return base64.b64decode(s, validate=True)
    except ValueError:
        raise SealBroken(f"{what} is not base64") from None


@dataclass(frozen=True)
class Envelope:
    kms: str
    key_id: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        return json.dumps({"v": ENVELOPE_VERSION, "kms": self.kms, "key_id": self.key_id,
                           "wrapped_dek": _b64(self.wrapped_dek), "nonce": _b64(self.nonce),
                           "ciphertext": _b64(self.ciphertext)}, sort_keys=True, separators=(",", ":")).encode("ascii")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Envelope":
        try:
            d = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SealBroken("sealed file is not JSON") from None
        if not isinstance(d, dict) or d.get("v") != ENVELOPE_VERSION or set(d) != {"v", "kms", "key_id", "wrapped_dek",
                                                                                   "nonce", "ciphertext"}:
            raise SealBroken("sealed file has the wrong shape")
        if not isinstance(d["kms"], str) or not isinstance(d["key_id"], str):
            raise SealBroken("sealed file has the wrong shape")
        nonce = _unb64(d["nonce"], "nonce")
        if len(nonce) != NONCE_BYTES:
            raise SealBroken("nonce has the wrong length")
        return cls(d["kms"], d["key_id"], _unb64(d["wrapped_dek"], "wrapped key"), nonce,
                   _unb64(d["ciphertext"], "ciphertext"))


def seal(kms: KeyService, value: bytes, context: dict[str, str]) -> Envelope:
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(dek).encrypt(nonce, value, context_bytes({**context, "layer": "value"}))
    wrapped = kms.wrap(dek, context)
    return Envelope(kms.name, kms.key_id, wrapped, nonce, ciphertext)


def open_sealed(kms: KeyService, env: Envelope, context: dict[str, str]) -> bytes:
    if env.kms != kms.name or not hmac.compare_digest(env.key_id, kms.key_id):
        raise SealBroken("sealed by another key service or master key")
    dek = kms.unwrap(env.wrapped_dek, context)
    try:
        return AESGCM(dek).decrypt(env.nonce, env.ciphertext, context_bytes({**context, "layer": "value"}))
    except InvalidTag:
        raise SealBroken("sealed value failed authentication") from None
