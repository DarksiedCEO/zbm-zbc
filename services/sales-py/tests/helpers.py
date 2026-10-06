"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract (copied
from security-py's tests/helpers.py), wired fakes for every port, and builders for the common objects."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, Recorder, payload_sha256
from ports import ContractCheck, Delivery, Ports, SendResult
from service import SalesService
from store import RecordLog

SERVICE_TOKEN = "test-sales-service-token-0123456789abcdef"
CALLERS = {c: f"test-sales-caller-{c}-0123456789abcdef" for c in config_mod.KNOWN_CALLERS}
ANDRE = "test-andre-approval-token-0123456789abcdefgh"
OUTREACH = "zbm-outreach.test"
ZBM_DOMAIN, ZBC_DOMAIN = "zbestmedia.test", "zbestclips.test"
POSTAL = "123 Test Street, Suite 4, Los Angeles, CA 90001"
CONSENT_SHA = "a" * 64
# a fixed test-only key, derived rather than written out (no key-shaped literal for the secret scanner)
PII_KEY_HEX = __import__("hashlib").sha256(b"sales-py test-only PII key").hexdigest()
# 2026-10-06 18:00 UTC = 11:00 in Los Angeles, 14:00 in New York, 19:00 in London
NOON = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


def rid() -> str:
    return "r-" + uuid.uuid4().hex


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
        return not self.fail_reads

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


class FakeSender:
    wired = True

    def __init__(self, status: str = "accepted"):
        self.status = status
        self.sent: list[tuple] = []

    def send(self, message_id, to, msg) -> SendResult:
        self.sent.append((message_id, to, msg))
        return SendResult(self.status, f"prov-{len(self.sent)}")


class FakeSource:
    wired = True

    def __init__(self, records: list[dict]):
        self.records = records

    def fetch(self, limit: int) -> list[dict]:
        return self.records[:limit]


class FakeHandoff:
    wired = True

    def __init__(self, status: str = "delivered"):
        self.status = status
        self.calls: list[tuple] = []

    def deliver(self, handoff_id, payload) -> Delivery:
        self.calls.append((handoff_id, payload))
        return Delivery(self.status, f"ref-{handoff_id[-6:]}" if self.status == "delivered" else None)


class FakeLegal:
    wired = True

    def __init__(self, status: str = "in_force"):
        self.status = status

    def in_force(self, account_id, contract_kind, contract_ref) -> ContractCheck:
        return ContractCheck(self.status)

    def accepted(self, account_id, proposal_id, content_sha256, kind, ref) -> ContractCheck:
        self.acceptance_checks = getattr(self, "acceptance_checks", []) + [(proposal_id, content_sha256, kind, ref)]
        return ContractCheck(self.acceptance)

    acceptance = "accepted"


def wired_ports(**over) -> Ports:
    p = Ports.default()
    p.email, p.sms, p.voice = FakeSender(), FakeSender(), FakeSender()
    p.onboarding, p.finance, p.legal = FakeHandoff(), FakeHandoff(), FakeLegal()
    for k, v in over.items():
        setattr(p, k, v)
    return p


def base_env(**over) -> dict:
    env = {"SALES_SERVICE_TOKEN": SERVICE_TOKEN, "SALES_CALLER_TOKENS": json.dumps(CALLERS),
           "SALES_NON_PRODUCTION": "1", "SALES_ANDRE_APPROVAL_TOKEN": ANDRE, "SALES_OUTREACH_DOMAIN": OUTREACH,
           "SALES_ZBM_DOMAIN": ZBM_DOMAIN, "SALES_ZBC_DOMAIN": ZBC_DOMAIN, "SALES_POSTAL_ADDRESS": POSTAL}
    env.update({k: v for k, v in over.items() if v is not None})
    for k in [k for k, v in over.items() if v is None]:
        env.pop(k, None)
    return env


def secret_file(tmp, name: str, content: bytes) -> str:
    p = os.path.join(str(tmp), name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, content)
    os.close(fd)
    return p


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, **env_over):
        self.tmp = tmp
        self.env_over = env_over
        if data_dir and "SALES_PII_HASH_KEY_FILE" not in env_over:     # a durable log needs a real key (S1-L3)
            env_over["SALES_PII_HASH_KEY_FILE"] = secret_file(tmp, "pii.key", PII_KEY_HEX.encode())
        self.env = base_env(SALES_DATA_DIR=data_dir, **env_over)
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(NOON)
        self.ports = ports or Ports.default()
        self.svc = SalesService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir), self.ports,
                                self.clock)
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)

    def restart(self, **env_over) -> "Harness":
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports,
                       **{**self.env_over, **env_over})

    # --- http
    def headers(self, caller: Optional[str] = "dashboard", andre: Optional[str] = None) -> dict:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h[api.FOUNDER_HEADER] = andre
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "dashboard",
             andre: Optional[str] = None):
        return self.client.post(path, json=body if body is not None else {}, headers=self.headers(caller, andre))

    def andre(self, path: str, body: dict, token: str = ANDRE):
        return self.post(path, body, "dashboard", token)

    def get(self, path: str, caller: Optional[str] = "dashboard", **kw):
        return self.client.get(path, headers=self.headers(caller), **kw)

    @staticmethod
    def ok(resp, code: int = 200):
        assert resp.status_code == code, (resp.status_code, resp.text)
        return resp.json()

    @staticmethod
    def refused(resp, code: int, reason: str):
        assert resp.status_code == code, (resp.status_code, resp.text)
        assert resp.json()["detail"] == reason, resp.text
        return resp.json()

    # --- builders
    def lead(self, caller: str = "hub", source: str = "inbound", kind: str = "site_form", email: Optional[str] =
             "jane@acme-shop.test", phone: Optional[str] = "+13105550100", tz: Optional[str] = "America/Los_Angeles",
             interest=("revenue_recovery",), brand: Optional[str] = None, signals: Optional[dict] = None,
             account: Optional[dict] = None, referrer: Optional[dict] = None, code: int = 201, **extra) -> dict:
        contact = {"name": "Jane Doe"}
        if email:
            contact["email"] = email
        if phone:
            contact["phone"] = phone
        if tz:
            contact["time_zone"] = tz
        body = {"request_id": rid(), "source": source, "product_interest": list(interest), "contact": contact,
                "evidence": {"kind": kind, "ref": "ev-" + uuid.uuid4().hex[:12], "captured_at": "2026-10-06T17:00:00Z"},
                "signals": signals if signals is not None else {"requested_call": True, "timeline": "now"},
                "account": account or {"name": "Acme Shop", "domain": "acme-shop.test", "industry": "ecommerce",
                                       "employees_band": "11-50", "revenue_band": "1m_10m"}, **extra}
        if brand:
            body["brand"] = brand
        if referrer:
            body["referrer"] = referrer
        return self.ok(self.post("/sales/v1/leads", body, caller), code)

    def verify(self, lead: dict, display_name: Optional[str] = None, first_name: Optional[str] = None) -> dict:
        """A person at the console verifies the names merge fields may render (AEGIS S2-H1). A value the rules refuse
        stays unverified (the template is then refused for that contact)."""
        acc = self.svc.accounts[lead["account_id"]]
        c = self.svc.contacts[lead["contact_id"]]
        self.post(f"/sales/v1/accounts/{lead['account_id']}/display-name",
                  {"request_id": rid(), "display_name": display_name or acc["name"]})
        self.post(f"/sales/v1/contacts/{lead['contact_id']}/first-name",
                  {"request_id": rid(), "first_name": first_name or c["name"].split(" ")[0]})
        return lead

    def vlead(self, **kw) -> dict:
        return self.verify(self.lead(**kw))

    def opportunity(self, **lead_kw) -> dict:
        lead = self.lead(**lead_kw)
        return self.ok(self.post(f"/sales/v1/leads/{lead['lead_id']}/convert", {"request_id": rid()},
                                 "sales_agent"), 201)

    def template(self, brand: str = "zbm", channel: str = "email", name: str = "intro", approve: bool = True,
                 subject: Optional[str] = "Quick idea for {{company}}",
                 body: str = "Hi {{first_name}}, we help stores recover lost revenue. Worth a short call?") -> dict:
        req = {"request_id": rid(), "brand": brand, "channel": channel, "name": name, "body": body}
        if channel == "email":
            req["subject"] = subject
        t = self.ok(self.post("/sales/v1/templates", req, "sales_agent"), 201)
        if approve:
            v = t["versions"][0]
            t = self.ok(self.andre(f"/sales/v1/templates/{t['template_id']}/versions/1/approve",
                                   {"request_id": rid(), "content_sha256": v["content_sha256"]}))
        return t

    def consent(self, contact_id: str, channel: str = "sms", brand: str = "zbm", code: int = 201):
        return self.post("/sales/v1/consents", {"request_id": rid(), "contact_id": contact_id, "channel": channel,
                                                "brand": brand, "source": "web_form",
                                                "captured_at": "2026-10-06T17:30:00Z",
                                                "consent_text_version": "sms-consent-v1",
                                                "consent_text_sha256": CONSENT_SHA}, "hub")

    def price(self, line_id: str, price: Optional[str] = None, markup: Optional[str] = None, version: int = 1):
        brand = line_id.split(".")[0]
        body = {"request_id": rid(), "version": version}
        if price is not None:
            body["price"] = price
        if markup is not None:
            body["markup_pct"] = markup
        return self.andre(f"/sales/v1/pricebook/{brand}/lines/{line_id}/approve", body)

    def job(self, name: str, request_id: Optional[str] = None):
        return self.post(f"/sales/v1/jobs/{name}/run", {"request_id": request_id or rid()}, "scheduler")
