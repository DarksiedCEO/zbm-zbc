"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract (bizdev-py's
FakeLedger), a settable fixed clock, recording fakes for the ports (they live here only, never importable from src/),
and builders for the whole flow. Every token and key is DERIVED (sha256 of a test-only label), never written as a
secret-shaped literal."""

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
from platforms import FakeTransport
from ports import Delivery, ModelNotWired, Ports
from service import ClientFixService
from store import RecordLog


def derived(label: str) -> str:
    return hashlib.sha256(f"clientfix-py test-only {label}".encode()).hexdigest()


SERVICE_TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller token {c}") for c in config_mod.KNOWN_CALLERS}
# a fixed instant; nothing in these tests depends on the wall clock or on the hour of day
T0 = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
CLIENT_A = "onb-" + derived("client A")[:32]
CLIENT_B = "onb-" + derived("client B")[:32]
SHOP_A, SHOP_B = "zbest-a.myshopify.com", "zbest-b.myshopify.com"
PRODUCT = "gid://shopify/Product/1001"
PRODUCT2 = "gid://shopify/Product/1002"
SHOPIFY_SCOPES = ["read_products", "write_products", "read_content", "write_content", "read_online_store_pages",
                  "write_online_store_pages", "read_online_store_navigation", "write_online_store_navigation"]
GOOGLE_SCOPES = {"ga4": ["https://www.googleapis.com/auth/analytics.edit"],
                 "gtm": ["https://www.googleapis.com/auth/tagmanager.edit.containers",
                         "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
                         "https://www.googleapis.com/auth/tagmanager.publish"],
                 "gbp": ["https://www.googleapis.com/auth/business.manage"]}


def rid() -> str:
    return str(uuid.uuid4())


def fin_id(prefix: str = "evt") -> str:
    return f"fin-{prefix}-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def vault_ref(label: str) -> str:
    return f"vault:delivery_28.cfx-{hashlib.sha256(label.encode()).hexdigest()[:24]}"


class FakeLedger:
    """ledger-rust's contract: idempotent on identical content, 409 on a different record under the same id."""

    def __init__(self):
        self.events: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.fail = False
        self.fail_reads = False
        self.fail_types: set = set()
        self.verify_result = True

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail or event_type in self.fail_types:
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


class FakeDetection:
    wired = True

    def __init__(self, answer: str = "cleared"):
        self.answer = answer          # cleared | present | unknown
        self.calls: list = []

    def rescan(self, client_id, checks):
        self.calls.append((client_id, checks))
        return {c["finding_id"]: self.answer for c in checks} if self.answer != "unknown" else {}


class FakeFinance:
    wired = True

    def __init__(self, status: str = "delivered"):
        self.status = status
        self.refunds: list = []
        self.invoices: list = []
        self.status_answer = "unknown"

    def request_invoice(self, job_id, payload):
        self.invoices.append((job_id, payload))
        return Delivery("delivered", fin_id("inv"))

    def request_refund(self, refund_id, payload):
        self.refunds.append((refund_id, payload))
        if isinstance(self.status, BaseException):
            raise self.status
        return Delivery(self.status, fin_id("rfd") if self.status == "delivered" else None)

    def refund_status(self, refund_id):
        return Delivery(self.status_answer, fin_id("rfd") if self.status_answer == "delivered" else None)


class FakeEngineers:
    """A scripted fire team: returns the change sets it is given (or raises)."""
    wired = True

    def __init__(self, items=None, raises: Optional[BaseException] = None):
        self.items = items or []
        self.raises = raises
        self.briefs: list = []

    def propose(self, team, brief):
        self.briefs.append((team, brief))
        if self.raises:
            raise self.raises
        return self.items


class NotWiredButNamedEngineers:
    wired = True

    def propose(self, team, brief):
        raise ModelNotWired("no key")


def wired_ports(**over) -> Ports:
    p = Ports.default()
    p.transport = over.get("transport", FakeTransport())
    p.detection = over.get("detection", FakeDetection())
    p.finance = over.get("finance", FakeFinance())
    p.engineers = over.get("engineers", FakeEngineers())
    return p


def base_env(**over) -> dict:
    env = {"CFX_SERVICE_TOKEN": SERVICE_TOKEN, "CFX_CALLER_TOKENS": json.dumps(CALLERS), "CFX_NON_PRODUCTION": "1",
           "CFX_ANDRE_APPROVAL_TOKEN": ANDRE}
    env.update({k: v for k, v in over.items() if v is not None})
    for k in [k for k, v in over.items() if v is None]:
        env.pop(k, None)
    return env


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, **env_over):
        self.tmp = tmp
        self.env = base_env(CFX_DATA_DIR=data_dir, **env_over)
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(T0)
        self.ports = ports or wired_ports()
        lock = self.settings.data_dir_lock
        token = lock.claim() if lock is not None else None   # as api.build: before the log
        try:
            self.svc = ClientFixService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir),
                                        self.ports, self.clock, lock_token=token)
        except BaseException:
            if lock is not None:
                lock.release_claim(token)
            raise
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)
        self.sessions: dict = {}

    @property
    def t(self) -> FakeTransport:
        return self.ports.transport

    def restart(self, **env_over) -> "Harness":
        self.svc.close()
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports, **env_over)

    # --- http
    def headers(self, caller: Optional[str] = "dashboard", andre: bool = False, session: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        if session:
            h[api.SESSION_HEADER] = session
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "dashboard", andre: bool = False,
             session: Optional[str] = None):
        return self.client.post("/cfx/v1" + path, json=body if body is not None else {},
                                headers=self.headers(caller, andre, session))

    def get(self, path: str, caller: Optional[str] = "dashboard", session: Optional[str] = None, **kw):
        return self.client.get("/cfx/v1" + path, headers=self.headers(caller, session=session), **kw)

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

    def tick(self, name: str):
        return self.post(f"/ticks/{name}", {"request_id": rid()}, caller="scheduler")

    # --- builders
    def connection(self, client=CLIENT_A, connector="shopify", account=SHOP_A, token_ref: Optional[str] = "auto",
                   scopes=None) -> dict:
        if token_ref == "auto":
            token_ref = None if connector == "yelp" else vault_ref(f"{client}|{connector}|{account}|{rid()}")
        body = {"request_id": rid(), "client_id": client, "connector": connector, "account_ref": account,
                "scopes": scopes if scopes is not None else (SHOPIFY_SCOPES if connector == "shopify" else
                                                             GOOGLE_SCOPES.get(connector, []))}
        if token_ref is not None:
            body["token_ref"] = token_ref
        return self.ok(self.post("/connections", body, caller="hub"), 201)

    def finding(self, conn: dict, check="product_seo_missing", target=PRODUCT, fid: Optional[str] = None,
                client: Optional[str] = None) -> dict:
        return self.ok(self.post("/findings", {"request_id": rid(), "finding_id": fid or f"rr:{uuid.uuid4().hex[:16]}",
                                               "agent_id": "platform-integration", "client_id": client or conn["client_id"],
                                               "check_code": check, "resource": {"connection_id": conn["connection_id"],
                                                                                  "target": target}},
                                caller="orchestrator"), 201)

    def job(self, findings: list, prices=None, client: Optional[str] = None) -> dict:
        prices = prices or ["150.00"] * len(findings)
        return self.ok(self.post("/jobs", {"request_id": rid(), "client_id": client or findings[0]["client_id"],
                                           "items": [{"finding_id": f["finding_id"], "price": p}
                                                     for f, p in zip(findings, prices)]},
                                 caller="clientfix_agent"), 201)

    def session(self, client=CLIENT_A) -> str:
        if client not in self.sessions:
            self.sessions[client] = self.ok(self.post("/client-sessions", {"request_id": rid(), "client_id": client},
                                                      caller="hub"), 201)["session_token"]
        return self.sessions[client]

    def accept(self, job: dict):
        return self.post(f"/jobs/{job['job_id']}/quote/accept", {"request_id": rid(), "sha256": job["quote_sha256"]},
                         caller="hub", session=self.session(job["client_id"]))

    def pay(self, job: dict, amount: Optional[str] = None, ev: Optional[str] = None, quote_sha: Optional[str] = None):
        return self.post("/finance/events", {"request_id": rid(), "finance_event_id": ev or fin_id(),
                                             "job_id": job["job_id"], "kind": "payment_confirmed",
                                             "amount": amount or job["quote"]["total"], "currency": "USD",
                                             "quote_sha256": quote_sha or job["quote_sha256"]}, caller="finance_31")

    def plan(self, job: dict, items: list, team: Optional[str] = None):
        return self.post(f"/jobs/{job['job_id']}/plan", {"request_id": rid(), "team": team or job["team"],
                                                         "items": items}, caller="fire_team")

    def approve(self, job_id: str, sha: Optional[str] = None, client=CLIENT_A, session: Optional[str] = None):
        j = self.ok(self.get(f"/jobs/{job_id}"))
        return self.post(f"/jobs/{job_id}/plan/approve", {"request_id": rid(), "sha256": sha or j["plan_sha256"]},
                         caller="hub", session=session or self.session(client))

    def apply(self, job_id: str, caller="scheduler"):
        return self.post(f"/jobs/{job_id}/apply", {"request_id": rid()}, caller=caller)

    def item(self, job_id: str, n: int = 0) -> dict:
        return self.ok(self.get(f"/jobs/{job_id}"))["items"][n]

    # --- a whole SEO job up to an approved plan
    def seo_job(self, conn: Optional[dict] = None, title="Blue Hoodie | Warm Winter Wear", before=None,
                product=PRODUCT, approve=True) -> tuple[dict, dict]:
        conn = conn or self.connection()
        self.t.shop(conn["account_ref"]).product(product, seo_title=before)
        f = self.finding(conn, target=product)
        j = self.job([f])
        self.ok(self.accept(j))
        self.ok(self.pay(j))
        item = self.item(j["job_id"])
        self.ok(self.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                               "ops": [{"op": "shopify.product.update", "target": product, "field": "seo.title",
                                        "before": before, "after": title}]}]))
        if approve:
            self.ok(self.approve(j["job_id"], client=conn["client_id"]))
        return conn, self.ok(self.get(f"/jobs/{j['job_id']}"))
