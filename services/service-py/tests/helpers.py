"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract, a fixed
clock, recording fakes for the ports (they live here only, never importable from src/), and shortcuts for the
common set-up (approved articles, templates, offers, contacts with consent)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, Recorder, payload_sha256
from ports import ContractEnd, Handoff, PaymentStatus, Ports, Trend
from service import SupportService
from store import BodyStore, RecordLog

SERVICE_TOKEN = "test-svc-service-token-0123456789abcdef"
ANDRE_TOKEN = "test-andre-approval-token-0123456789abcdef"
CALLERS = {c: f"test-caller-token-{c}-0123456789abcdef" for c in config_mod.KNOWN_CALLERS}
ZBM_EMAIL, ZBC_EMAIL = "support@zbestmedia.test", "help@zbestclips.test"
ZBM_SMS, ZBC_SMS = "+13105550100", "+13105550200"
# Tuesday 2026-10-06 18:00 UTC = 11:00 America/Los_Angeles (inside SMS hours)
T0 = datetime(2026, 10, 6, 18, 0, 0, tzinfo=timezone.utc)


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
            if {k: prev[k] for k in e if k != "seq"} != {k: e[k] for k in e if k != "seq"}:
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


class RecordingSender:
    wired = True

    def __init__(self, result: str = "sent"):
        self.sent: list = []
        self.result = result

    def send(self, msg):
        self.sent.append(msg)
        return self.result


class RecordingAlerts:
    wired = True

    def __init__(self):
        self.sent: list = []

    def send(self, alert):
        self.sent.append(alert)
        return "delivered"


class RecordingHandoff:
    wired = True

    def __init__(self, status: str = "delivered"):
        self.calls: list = []
        self.status = status

    def handoff(self, req):
        self.calls.append(req)
        return Handoff(self.status, f"ref-{len(self.calls)}" if self.status == "delivered" else None)


class FixedSignals:
    wired = True

    def __init__(self, trend=None, payment=None, end=None):
        self.t, self.p, self.e = trend, payment, end

    def trend(self, account_id):
        return Trend(self.t is not None, self.t)

    def payment_status(self, account_id):
        return PaymentStatus(self.p is not None, self.p)

    def contract_end(self, account_id):
        return ContractEnd(self.e is not None, self.e)


def base_env(**over) -> dict:
    env = {"SVC_SERVICE_TOKEN": SERVICE_TOKEN, "SVC_CALLER_TOKENS": json.dumps(CALLERS), "SVC_NON_PRODUCTION": "1",
           "SVC_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN, "SVC_SUPPORT_EMAIL_ZBM": ZBM_EMAIL,
           "SVC_SUPPORT_EMAIL_ZBC": ZBC_EMAIL, "SVC_SMS_NUMBER_ZBM": ZBM_SMS, "SVC_SMS_NUMBER_ZBC": ZBC_SMS}
    env.update({k: v for k, v in over.items() if v is not None})
    for k in [k for k, v in over.items() if v is None]:
        env.pop(k, None)
    return env


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, **env_over):
        self.tmp = tmp
        self.env = base_env(SVC_DATA_DIR=data_dir, **env_over)
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(T0)
        self.ports = ports or Ports.default()
        self.svc = SupportService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir),
                                  BodyStore(self.settings.data_dir), self.ports, self.clock)
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)

    def restart(self, **env_over) -> "Harness":
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports, **env_over)

    # --- http
    def headers(self, caller: Optional[str] = "dashboard", andre: bool = False, token: str = SERVICE_TOKEN) -> dict:
        h = {"Authorization": f"Bearer {token}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE_TOKEN
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "dashboard", andre: bool = False):
        return self.client.post(path, json=body if body is not None else {}, headers=self.headers(caller, andre))

    def put(self, path: str, body: dict, caller: Optional[str] = "dashboard", andre: bool = False):
        return self.client.put(path, json=body, headers=self.headers(caller, andre))

    def get(self, path: str, caller: Optional[str] = "dashboard", **kw):
        return self.client.get(path, headers=self.headers(caller), **kw)

    @staticmethod
    def ok(resp, code: int = 200):
        assert resp.status_code == code, (resp.status_code, resp.text)
        return resp.json()

    def job(self, name: str):
        return self.post(f"/svc/v1/jobs/{name}/run", {"request_id": rid()}, caller="scheduler")

    # --- catalogue
    def approve(self, catalog_path: str, saved: dict) -> dict:
        return self.ok(self.post(f"/svc/v1/{catalog_path}/{saved['item_id']}/approve",
                                 {"request_id": rid(), "version": saved["version"],
                                  "content_sha256": saved["content_sha256"]}, andre=True))

    def article(self, item_id: str = "hours", approve: bool = True, brands=("zbm", "zbc"),
                channels=("chat", "email", "sms"), answer: str = "We are open Monday to Friday, 9am to 6pm Pacific.",
                rules: Optional[dict] = None) -> dict:
        body = {"request_id": rid(), "item_id": item_id, "brands": list(brands), "channels": list(channels),
                "title": "Opening hours", "answer": answer,
                "rules": rules or {"any": ["hours", "open", "opening"], "min_any": 1}}
        saved = self.ok(self.post("/svc/v1/kb/articles", body), 201)
        if approve:
            self.approve("kb/articles", saved)
        return saved

    def template(self, item_id: str, purpose: str, brand: str = "zbm", channels=("email", "sms", "chat"),
                 text: Optional[str] = None, approve: bool = True) -> dict:
        text = text or {"check_in": "Hi {first_name}, checking in from {brand_name}. How are results looking?",
                        "nps_survey": "Hi {first_name}, how likely are you to recommend {brand_name}? Survey "
                                      "{survey_id}",
                        "offer": "Hi {first_name}: {offer_title}. {offer_terms} Price: {offer_price}."}[purpose]
        saved = self.ok(self.post("/svc/v1/templates", {"request_id": rid(), "item_id": item_id, "purpose": purpose,
                                                        "brand": brand, "channels": list(channels), "text": text}),
                        201)
        if approve:
            self.approve("templates", saved)
        return saved

    def offer(self, item_id: str = "month-free", brand: str = "zbm", price: str = "0.00", approve: bool = True,
              terms: str = "One month of management at no charge when you renew for six months.") -> dict:
        saved = self.ok(self.post("/svc/v1/offers", {"request_id": rid(), "item_id": item_id, "brand": brand,
                                                     "title": "A month on us", "terms": terms, "price": price}), 201)
        if approve:
            self.approve("offers", saved)
        return saved

    # --- contacts
    def contact(self, ref: str = "client:acme", brand: str = "zbm", **fields) -> str:
        body = {"request_id": rid(), "brand": brand, "contact_ref": ref, **fields}
        return self.ok(self.post("/svc/v1/contacts", body, caller="hub"), 201)["contact_id"]

    def consent(self, contact_id: str, channel: str = "sms"):
        return self.post("/svc/v1/consents", {"request_id": rid(), "contact_id": contact_id, "channel": channel,
                                              "source": "portal_form", "consent_text":
                                              "I agree to receive account texts from Z Best Media.",
                                              "captured_at": "2026-10-01T10:00:00Z", "express": True}, caller="hub")

    def chat(self, text: str, ref: str = "client:acme", brand: str = "zbm", ticket_id: Optional[str] = None,
             request_id: Optional[str] = None):
        body = {"request_id": request_id or rid(), "brand": brand, "contact_ref": ref, "text": text}
        if ticket_id:
            body["ticket_id"] = ticket_id
        return self.post("/svc/v1/chat/messages", body, caller="hub")

    def email(self, text: str, frm: str = "owner@acme.test", brand: str = "zbm", subject: str = "Question"):
        return self.post("/svc/v1/inbound/email", {"request_id": rid(), "brand": brand,
                                                   "to_address": ZBM_EMAIL if brand == "zbm" else ZBC_EMAIL,
                                                   "from_address": frm, "subject": subject, "text": text},
                         caller="email_gateway")

    def sms(self, text: str, frm: str = "+13105551234", brand: str = "zbm"):
        return self.post("/svc/v1/inbound/sms", {"request_id": rid(), "brand": brand,
                                                 "to_number": ZBM_SMS if brand == "zbm" else ZBC_SMS,
                                                 "from_number": frm, "text": text}, caller="sms_gateway")

    def account(self, account_id: str = "acct-1", brand: str = "zbm", contact_id: Optional[str] = None, **extra):
        body = {"request_id": rid(), "account_id": account_id, "brand": brand, **extra}
        if contact_id:
            body["primary_contact_id"] = contact_id
        return self.ok(self.post("/svc/v1/accounts", body, caller="onboarding"), 201)
