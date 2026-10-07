"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's entry shape, and a
software authenticator that performs real WebAuthn registrations and assertions (ES256, EdDSA or RS256)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import uuid
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from fastapi.testclient import TestClient

import api
import config as config_mod
from datetime import datetime, timezone

from clock import FixedClock
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, Recorder, payload_sha256
from ports import Ports
from service import SecurityService
from store import RecordLog, SealedStore

SERVICE_TOKEN = "test-sec-service-token-0123456789abcdef"
CALLERS = {c: f"test-caller-token-{c}-0123456789abcdef" for c in config_mod.KNOWN_CALLERS}
ENROLL_TOKEN = "enroll-token-0123456789abcdefghijklmnopqrstuv"
RP_ID = "zbm.test"
ORIGIN = "https://console.zbm.test"


def rid() -> str:
    return "r-" + uuid.uuid4().hex


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ------------------------------------------------------------------------------------------------ CBOR encoder (tests)

def _head(major: int, n: int) -> bytes:
    if n < 24:
        return bytes([(major << 5) | n])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < (1 << (8 * size)):
            return bytes([(major << 5) | info]) + n.to_bytes(size, "big")
    raise ValueError


def cbor_dumps(obj) -> bytes:
    if obj is False:
        return b"\xf4"
    if obj is True:
        return b"\xf5"
    if obj is None:
        return b"\xf6"
    if isinstance(obj, int):
        return _head(0, obj) if obj >= 0 else _head(1, -1 - obj)
    if isinstance(obj, bytes):
        return _head(2, len(obj)) + obj
    if isinstance(obj, str):
        b = obj.encode()
        return _head(3, len(b)) + b
    if isinstance(obj, list):
        return _head(4, len(obj)) + b"".join(cbor_dumps(x) for x in obj)
    if isinstance(obj, dict):
        return _head(5, len(obj)) + b"".join(cbor_dumps(k) + cbor_dumps(v) for k, v in obj.items())
    raise TypeError(type(obj))


# ------------------------------------------------------------------------------------------------ authenticator

class Authenticator:
    """A software passkey. ``counter_step=0`` models a passkey with no counter (synced passkeys)."""

    def __init__(self, alg: int = -7, counter_step: int = 1, rp_id: str = RP_ID, origin: str = ORIGIN):
        self.alg, self.rp_id, self.origin, self.step = alg, rp_id, origin, counter_step
        self.cred_id = os.urandom(32)
        self.count = 0
        if alg == -7:
            self.key = ec.generate_private_key(ec.SECP256R1())
        elif alg == -8:
            self.key = ed25519.Ed25519PrivateKey.generate()
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def credential_id(self) -> str:
        return b64u(self.cred_id)

    def cose(self) -> dict:
        if self.alg == -7:
            n = self.key.public_key().public_numbers()
            return {1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")}
        if self.alg == -8:
            from cryptography.hazmat.primitives import serialization
            x = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            return {1: 1, 3: -8, -1: 6, -2: x}
        n = self.key.public_key().public_numbers()
        return {1: 3, 3: -257, -1: n.n.to_bytes(256, "big"), -2: n.e.to_bytes(3, "big")}

    def client_data(self, typ: str, challenge_b64: str, **over) -> bytes:
        cd = {"type": typ, "challenge": challenge_b64, "origin": self.origin, "crossOrigin": False}
        cd.update(over)
        return json.dumps(cd).encode()

    def auth_data(self, flags: int = 0x05, attested: bool = False, count: Optional[int] = None,
                  rp_id: Optional[str] = None) -> bytes:
        c = self.count if count is None else count
        out = hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([flags]) + struct.pack(">I", c)
        if attested:
            out += b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + cbor_dumps(self.cose())
        return out

    def register(self, options: dict, fmt: str = "none", flags: int = 0x45, **cd_over) -> dict:
        cd = self.client_data("webauthn.create", options["challenge"], **cd_over)
        att = cbor_dumps({"fmt": fmt, "attStmt": {}, "authData": self.auth_data(flags, attested=True)})
        return {"challenge_id": options["challenge_id"], "attestation_object": b64u(att),
                "client_data_json": b64u(cd), "label": "test key"}

    def sign(self, data: bytes) -> bytes:
        if self.alg == -7:
            return self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        if self.alg == -8:
            return self.key.sign(data)
        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def assert_(self, challenge: dict, flags: int = 0x05, count: Optional[int] = None, cd_over=None,
                rp_id: Optional[str] = None) -> dict:
        self.count += self.step
        cd = self.client_data("webauthn.get", challenge["challenge"], **(cd_over or {}))
        ad = self.auth_data(flags, count=count, rp_id=rp_id)
        sig = self.sign(ad + hashlib.sha256(cd).digest())
        return {"challenge_id": challenge["challenge_id"], "credential_id": self.credential_id,
                "client_data_json": b64u(cd), "authenticator_data": b64u(ad), "signature": b64u(sig)}


# ------------------------------------------------------------------------------------------------ ledger

class FakeLedger:
    """ledger-rust's contract: idempotent on identical content, 409 on a different record under the same id;
    entries() in ledger order with the persisted fields."""

    def __init__(self):
        self.events: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.fail = False
        self.fail_reads = False
        self.fail_types: set = set()

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail or event_type in self.fail_types:
            raise LedgerNotRecorded("fake ledger down")
        e = {"kind": "event", "seq": len(self.events) + 1, "event_id": event_id, "department": department,
             "event_type": event_type, "actor": actor, "subject_id": subject_id,
             "payload_sha256": payload_sha256(payload), "summary": summary}
        prev = self.by_id.get(event_id)
        if prev is not None:
            if {k: prev[k] for k in e if k != "seq"} != {k: e[k] for k in e if k != "seq"}:
                raise LedgerConflict("409")
            return
        self.events.append(e)
        self.by_id[event_id] = e

    def entries(self) -> list[dict]:
        if self.fail_reads:
            raise LedgerQueryFailed("fake ledger unreadable")
        return [dict(e) for e in self.events]

    def verify(self) -> bool:
        return not self.fail_reads

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


# ------------------------------------------------------------------------------------------------ harness

def secret_file(tmp, name: str, content: bytes) -> str:
    p = os.path.join(str(tmp), name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, content)
    os.close(fd)
    return p


def base_env(tmp, **over) -> dict:
    key_file = secret_file(tmp, "master.key", base64.b64encode(b"k" * 32))
    enroll = secret_file(tmp, "enroll.token", ENROLL_TOKEN.encode())
    env = {"SEC_SERVICE_TOKEN": SERVICE_TOKEN, "SEC_CALLER_TOKENS": json.dumps(CALLERS), "SEC_NON_PRODUCTION": "1",
           "SEC_KMS": "local_file", "SEC_LOCAL_MASTER_KEY_FILE": key_file, "SEC_WEBAUTHN_RP_ID": RP_ID,
           "SEC_WEBAUTHN_ORIGINS": ORIGIN, "SEC_ANDRE_ENROLL_TOKEN_FILE": enroll}
    env.update({k: v for k, v in over.items() if v is not None})
    for k in [k for k, v in over.items() if v is None]:
        env.pop(k, None)
    return env


# sweep A: every harness runs on a fixed clock unless a test passes its own (no test depends on the wall clock)
DEFAULT_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, kms=None, **env_over):
        self.tmp = tmp
        self.env = base_env(tmp, SEC_DATA_DIR=data_dir, **env_over)
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(DEFAULT_NOW)
        self.ports = ports or Ports.default()
        self.kms = kms or api.build_kms(self.settings)
        self.svc = SecurityService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir),
                                   SealedStore(self.settings.data_dir), self.kms, self.ports, self.clock)
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)
        self.keys: list[Authenticator] = []

    def restart(self, **env_over) -> "Harness":
        env = {k: v for k, v in env_over.items()}
        h = Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports, self.kms, **env)
        h.keys = self.keys
        return h

    # --- http
    def headers(self, caller: Optional[str] = "dashboard", token: str = SERVICE_TOKEN) -> dict:
        h = {"Authorization": f"Bearer {token}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "dashboard", **kw):
        return self.client.post(path, json=body if body is not None else {}, headers=self.headers(caller), **kw)

    def get(self, path: str, caller: Optional[str] = "dashboard", **kw):
        return self.client.get(path, headers=self.headers(caller), **kw)

    @staticmethod
    def ok(resp, code: int = 200):
        assert resp.status_code == code, (resp.status_code, resp.text)
        return resp.json()

    # --- passkeys
    def enroll(self, alg: int = -7, counter_step: int = 1) -> Authenticator:
        a = Authenticator(alg, counter_step)
        if not self.keys:
            opts = self.ok(self.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}))
        else:
            opts = self.ok(self.post("/sec/v1/passkeys/enroll/options",
                                     self.approved("PASSKEY_ENROLL", "", {})))
        self.ok(self.post("/sec/v1/passkeys/enroll", a.register(opts)), 201)
        self.keys.append(a)
        return a

    def challenge(self, action: str, target: str, body: dict) -> dict:
        return self.ok(self.post("/sec/v1/approvals/challenges", {"action": action, "target": target, "body": body}))

    def approved(self, action: str, target: str, body: dict, key: Optional[Authenticator] = None, **kw) -> dict:
        ch = self.challenge(action, target, body)
        return {**body, "approval": (key or self.keys[0]).assert_(ch, **kw)}

    # --- vault shortcuts
    def andre_store(self, owner: str, name: str, kind: str = "api_key", value: Optional[str] = "sk_value_123",
                    readers=(), purposes=(), **extra) -> dict:
        body = {"request_id": rid(), "owner": owner, "name": name, "kind": kind, "readers": list(readers),
                "purposes": list(purposes), **extra}
        if value is not None and kind not in ("hmac_key", "canary"):
            body["value"] = value
        return self.ok(self.post("/sec/v1/secrets/andre", self.approved("SECRET_STORE", f"vault:{owner}.{name}",
                                                                         body)), 201)

    def use(self, caller: str, ref: str, purpose: str):
        return self.post(f"/sec/v1/secrets/{ref}/use", {"purpose": purpose}, caller=caller)
