"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract (bizdev-py's
FakeLedger), a settable fixed clock, and builders. Every token is DERIVED (sha256 of a test-only label), never written
as a secret-shaped literal. Nothing here is importable from src/."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, Recorder, payload_sha256
from ports import Ports
from service import SeoService
from store import RecordLog


def derived(label: str) -> str:
    return hashlib.sha256(f"seo-py test-only {label}".encode()).hexdigest()


SERVICE_TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller token {c}") for c in config_mod.KNOWN_CALLERS}
TENANT_TOKENS = {t: derived(f"tenant token {t}") for t in ("zbm", "acme", "globex")}
T0 = datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc)


def rid() -> str:
    return str(uuid.uuid4())


def invoice_id(n: int = 1) -> str:
    """A Finance (31) invoice id shape: fin-inv- + 26 Crockford base32 characters."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    d = int.from_bytes(hashlib.sha256(f"inv {n}".encode()).digest()[:17], "big")
    return "fin-inv-" + "".join(alphabet[(d >> (5 * i)) & 31] for i in range(26))


class FakeLedger:
    """ledger-rust's contract: idempotent on identical content, 409 on a different record under the same id."""

    def __init__(self):
        self.events: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.fail = False
        self.fail_reads = False
        self.verify_result = True

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail:
            raise LedgerNotRecorded("fake ledger down")
        e = {"kind": "event", "seq": len(self.events) + 1, "event_id": event_id, "department": department,
             "event_type": event_type, "actor": actor, "subject_id": subject_id,
             "payload_sha256": payload_sha256(payload), "summary": summary, "_payload": payload}
        prev = self.by_id.get(event_id)
        if prev is not None:
            if {k: prev[k] for k in e if k not in ("seq", "_payload")} != \
                    {k: e[k] for k in e if k not in ("seq", "_payload")}:
                raise LedgerConflict("409")
            return
        self.events.append(e)
        self.by_id[event_id] = e

    def entries(self) -> list[dict]:
        if self.fail_reads:
            raise LedgerQueryFailed("fake ledger unreadable")
        return [{k: v for k, v in e.items() if k != "_payload"} for e in self.events]

    def verify(self) -> bool:
        return self.verify_result and not self.fail_reads

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


def write_key(path) -> str:
    """A derived test-only key file, mode 0600."""
    import os
    if not path.exists():
        path.write_text(derived("log ip hash key"))
        os.chmod(path, 0o600)
    return str(path)


def base_env(**over) -> dict:
    env = {"SEO_SERVICE_TOKEN": SERVICE_TOKEN, "SEO_NON_PRODUCTION": "1", "SEO_ANDRE_APPROVAL_TOKEN": ANDRE,
           "SEO_CALLER_TOKENS": json.dumps(CALLERS), "SEO_TENANT_TOKENS": json.dumps(TENANT_TOKENS)}
    for k, v in over.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, **env_over):
        self.tmp = tmp
        if data_dir and "SEO_LOG_HASH_KEY_FILE" not in env_over:
            env_over["SEO_LOG_HASH_KEY_FILE"] = write_key(tmp / "log.key")
        self.env = base_env(SEO_DATA_DIR=data_dir, **env_over)
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(T0)
        self.ports = ports or Ports.default(self.settings)
        lock = self.settings.data_dir_lock
        token = lock.claim() if lock is not None else None
        try:
            self.svc = SeoService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir),
                                  self.ports, self.clock, lock_token=token)
        except BaseException:
            if lock is not None:
                lock.release_claim(token)
            raise
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)

    def restart(self, **env_over) -> "Harness":
        self.svc.close()
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports, **env_over)

    def headers(self, caller: Optional[str] = "dashboard", andre: bool = False, tenant: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        if tenant:
            h[api.TENANT_HEADER] = TENANT_TOKENS[tenant]
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "seo_agent", andre: bool = False,
             tenant: Optional[str] = None):
        if andre and caller == "seo_agent":
            caller = "dashboard"
        return self.client.post("/seo/v1" + path, json=body if body is not None else {},
                                headers=self.headers(caller, andre, tenant))

    def get(self, path: str, caller: Optional[str] = "dashboard", tenant: Optional[str] = None, **kw):
        return self.client.get("/seo/v1" + path, headers=self.headers(caller, tenant=tenant), **kw)

    @staticmethod
    def ok(resp, code: int = 200):
        assert resp.status_code == code, (resp.status_code, resp.text)
        return resp.json()

    @staticmethod
    def refused(resp, code: int, detail: Optional[str] = None):
        assert resp.status_code == code, (resp.status_code, resp.text)
        if detail is not None:
            assert resp.json()["detail"] == detail, resp.text
        return resp.json()

    def tenant(self, tid: str, kind: str = "client", domains: tuple = ()) -> dict:
        t = self.ok(self.post("/tenants", {"request_id": rid(), "tenant_id": tid, "kind": kind}, andre=True), 201)
        if domains:
            t = self.ok(self.post(f"/tenants/{tid}/domains", {"request_id": rid(), "domains": list(domains)},
                                  andre=True))
        return t

    def switch(self, name: str, engaged: bool = True, andre: bool = False, caller: str = "dashboard"):
        return self.post("/kill-switches", {"request_id": rid(), "switch": name, "engaged": engaged},
                         caller=caller, andre=andre)
