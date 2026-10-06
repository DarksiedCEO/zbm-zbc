"""Test harness: an in-process service over the real ASGI stack, a fake ledger with ledger-rust's contract (sales-py's
FakeLedger), a settable fixed clock, recording fakes for the ports (they live here only, never importable from src/),
and builders for the common objects. Every token and key is DERIVED (sha256 of a test-only label), never written as
a secret-shaped literal."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock, iso
from legal_client import LegalAnswer
from ledger import LedgerConflict, LedgerNotRecorded, LedgerQueryFailed, Recorder, payload_sha256
from ports import Delivery, Ports, SendResult
from service import BizDevService
from store import RecordLog


def derived(label: str) -> str:
    return hashlib.sha256(f"bizdev-py test-only {label}".encode()).hexdigest()


SERVICE_TOKEN = derived("service token")
ANDRE = derived("andre approval token")
CALLERS = {c: derived(f"caller token {c}") for c in config_mod.KNOWN_CALLERS}
PII_KEY_HEX = derived("PII hash key")
OUTREACH = "zbm-partners.test"
ZBM_DOMAIN, ZBC_DOMAIN = "zbestmedia.test", "zbestclips.test"
POSTAL = "123 Test Street, Suite 4, Los Angeles, CA 90001"
# a fixed instant; nothing in these tests depends on the wall clock or on the hour of day
T0 = datetime(2026, 10, 6, 18, 0, tzinfo=timezone.utc)
DEADLINE = iso(T0 + timedelta(days=14))


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
        self.verify_result = True
        self.verify_calls = 0

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
        self.verify_calls += 1
        return self.verify_result and not self.fail_reads

    def of_type(self, t: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == t]


class RecordingSender:
    wired = True

    def __init__(self, status: str = "accepted"):
        self.sent: list = []
        self.status = status

    def send(self, message_id, to, msg):
        self.sent.append((message_id, to, msg))
        return SendResult(self.status, f"prov-{len(self.sent)}" if self.status == "accepted" else None)


class RecordingSubmission:
    wired = True

    def __init__(self, status: str = "accepted"):
        self.calls: list = []
        self.status = status

    def submit(self, submission_id, pursuit_id, content_sha256, text):
        self.calls.append((submission_id, pursuit_id, content_sha256, text))
        return SendResult(self.status, f"sub-ref-{len(self.calls)}" if self.status == "accepted" else None)


class RecordingHandoff:
    wired = True

    def __init__(self, status: str = "delivered"):
        self.calls: list = []
        self.status = status

    def deliver(self, handoff_id, payload):
        self.calls.append((handoff_id, payload))
        return Delivery(self.status, f"ref-{len(self.calls)}" if self.status == "delivered" else None)


class RecordingPayouts:
    wired = True

    def __init__(self, status: str = "delivered"):
        self.calls: list = []
        self.status = status

    def request_payout(self, payout_id, payload):
        self.calls.append((payout_id, payload))
        return Delivery(self.status, f"fin:payout-{len(self.calls)}" if self.status == "delivered" else None)


class FakeLegal:
    wired = True

    def __init__(self, send_status: str = "delivered", in_force: str = "in_force"):
        self.sent: list = []
        self.asked: list = []
        self.send_status = send_status
        self.in_force_status = in_force

    def send(self, handoff):
        self.sent.append(handoff)
        return LegalAnswer(self.send_status, "legal:matter-1" if self.send_status == "delivered" else None)

    def in_force(self, partner_id, kind):
        self.asked.append((partner_id, kind))
        return LegalAnswer(self.in_force_status, "legal:agreement-1" if self.in_force_status == "in_force" else None)


def wired_ports(**over) -> Ports:
    p = Ports.default()
    p.email = over.get("email", RecordingSender())
    p.submission = over.get("submission", RecordingSubmission())
    p.onboarding = over.get("onboarding", RecordingHandoff())
    p.finance = over.get("finance", RecordingHandoff())
    p.payouts = over.get("payouts", RecordingPayouts())
    p.legal = over.get("legal", FakeLegal())
    return p


def base_env(**over) -> dict:
    env = {"NBD_SERVICE_TOKEN": SERVICE_TOKEN, "NBD_CALLER_TOKENS": json.dumps(CALLERS), "NBD_NON_PRODUCTION": "1",
           "NBD_ANDRE_APPROVAL_TOKEN": ANDRE, "NBD_OUTREACH_DOMAIN": OUTREACH, "NBD_ZBM_DOMAIN": ZBM_DOMAIN,
           "NBD_ZBC_DOMAIN": ZBC_DOMAIN, "NBD_POSTAL_ADDRESS": POSTAL}
    env.update({k: v for k, v in over.items() if v is not None})
    for k in [k for k, v in over.items() if v is None]:
        env.pop(k, None)
    return env


def write_key(path) -> str:
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, PII_KEY_HEX.encode())
    os.close(fd)
    return str(path)


class Harness:
    def __init__(self, tmp, data_dir: Optional[str] = None, ledger: Optional[FakeLedger] = None,
                 clock: Optional[FixedClock] = None, ports: Optional[Ports] = None, **env_over):
        self.tmp = tmp
        extra = {}
        if data_dir:
            extra["NBD_PII_HASH_KEY_FILE"] = write_key(tmp / "pii.key")
        self.env = base_env(NBD_DATA_DIR=data_dir, **{**extra, **env_over})
        self.settings = config_mod.load(self.env)
        self.ledger = ledger or FakeLedger()
        self.clock = clock or FixedClock(T0)
        self.ports = ports or Ports.default()
        lock = self.settings.data_dir_lock
        token = lock.claim() if lock is not None else None   # as api.build: before the log
        try:
            self.svc = BizDevService(self.settings, Recorder(self.ledger), RecordLog(self.settings.data_dir),
                                     self.ports, self.clock, lock_token=token)
        except BaseException:
            if lock is not None:
                lock.release_claim(token)
            raise
        self.client = TestClient(api._wrap(api.create_app(self.svc, self.settings)), raise_server_exceptions=False)

    def restart(self, **env_over) -> "Harness":
        self.svc.close()
        return Harness(self.tmp, self.settings.data_dir, self.ledger, self.clock, self.ports, **env_over)

    # --- http
    def headers(self, caller: Optional[str] = "dashboard", andre: bool = False) -> dict:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        if caller:
            h[api.CALLER_HEADER] = CALLERS[caller]
        if andre:
            h["X-Andre-Approval-Token"] = ANDRE
        return h

    def post(self, path: str, body: Optional[dict] = None, caller: Optional[str] = "bizdev_agent",
             andre: bool = False):
        if andre and caller == "bizdev_agent":
            caller = "dashboard"
        return self.client.post("/nbd/v1" + path, json=body if body is not None else {},
                                headers=self.headers(caller, andre))

    def get(self, path: str, caller: Optional[str] = "dashboard", **kw):
        return self.client.get("/nbd/v1" + path, headers=self.headers(caller), **kw)

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

    def job(self, name: str):
        return self.post(f"/jobs/{name}/run", {"request_id": rid()}, caller="scheduler")

    # --- builders: pursuits
    def pursuit(self, kind: str = "rfp", value: str = "5000.00", ref: str = "org:acme", name: str = "Acme Inc",
                domain: str = "acme.test", brand: str = "zbm", deadline: Optional[str] = DEADLINE,
                checklist=None, notes: Optional[str] = None, **extra) -> dict:
        body = {"request_id": rid(), "brand": brand, "kind": kind, "title": "Billboard campaign RFP",
                "counterparty": {"ref": ref, "name": name, "domain": domain}, "value": value, **extra}
        if deadline is not None:
            body["deadline"] = deadline
        if checklist is not None:
            body["checklist"] = checklist
        if notes is not None:
            body["notes"] = notes
        return self.ok(self.post("/pursuits", body), 201)

    def qualify(self, pid: str, **answers) -> dict:
        a = {c: "yes" for c in ("scope_fit", "capacity", "deadline_feasible", "compliance_feasible", "relationship",
                                "price_competitive", "payment_terms_acceptable")}
        a.update(answers)
        return self.ok(self.post(f"/pursuits/{pid}/qualification", {"request_id": rid(), **a}))

    def bid(self, pid: str) -> dict:
        p = self.qualify(pid)
        return self.ok(self.post(f"/pursuits/{pid}/bid-decision",
                                 {"request_id": rid(), "decision": "bid",
                                  "qualification_sha256": p["qualification"]["qualification_sha256"]}, andre=True))

    def attest_all(self, pid: str) -> dict:
        p = self.ok(self.get(f"/pursuits/{pid}"))
        for item in p["checklist"]:
            p = self.ok(self.post(f"/pursuits/{pid}/checklist/{item['item_id']}/attest",
                                  {"request_id": rid(), "item_sha256": item["item_sha256"]}, andre=True))
        return p

    def block(self, key: str = "about-us", brand: str = "zbm", text: str = "Z Best Media runs outdoor campaigns.",
              approve: bool = True) -> dict:
        b = self.ok(self.post("/blocks", {"request_id": rid(), "block_key": key, "brand": brand, "title": "About",
                                          "text": text}), 201)
        if approve:
            v = b["versions"][-1]
            b = self.ok(self.post(f"/blocks/{b['block_id']}/versions/{v['version']}/approve",
                                  {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True))
        return b

    def response(self, pid: str, parts: list) -> dict:
        return self.ok(self.post("/responses", {"request_id": rid(), "pursuit_id": pid, "parts": parts}), 201)

    def approve_response(self, r: dict, flags=None) -> dict:
        v = r["versions"][-1]
        return self.ok(self.post(f"/responses/{r['response_id']}/approve",
                                 {"request_id": rid(), "version": v["version"], "content_sha256": v["content_sha256"],
                                  "acknowledged_flags": sorted(flags if flags is not None else v["flags"])},
                                 andre=True))

    def submit(self, r: dict):
        v = r["versions"][-1]
        return self.post(f"/responses/{r['response_id']}/submit",
                         {"request_id": rid(), "version": v["version"], "content_sha256": v["content_sha256"]})

    def ready_response(self, pid: str, custom: str = "We propose twelve digital boards for six weeks.") -> dict:
        b = self.block()
        r = self.response(pid, [{"block_id": b["block_id"], "version": 1}, {"custom": custom}])
        return self.approve_response(r)

    # --- builders: partners
    def partner(self, key: str = "west-agency", kind: str = "referral", brands=("zbm",), **extra) -> dict:
        return self.ok(self.post("/partners", {"request_id": rid(), "partner_key": key, "kind": kind,
                                               "brands": list(brands), "name": "West Coast Agency",
                                               "domain": "westagency.test", **extra}), 201)

    def rate(self, pid: str, rate_pct: str = "10.00", approve: bool = True) -> dict:
        p = self.ok(self.get(f"/partners/{pid}"))
        v = p["rate_version"] + 1
        p = self.ok(self.post(f"/partners/{pid}/rate", {"request_id": rid(), "version": v, "rate_pct": rate_pct}))
        if approve:
            p = self.ok(self.post(f"/partners/{pid}/rate/approve",
                                  {"request_id": rid(), "version": v,
                                   "binding_sha256": p["rate_proposed"]["binding_sha256"]}, andre=True))
        return p

    def payee(self, pid: str, tax_ref: str = "vault:tax:abcdefABCDEF_tok-ref") -> dict:
        return self.post(f"/partners/{pid}/payee", {"request_id": rid(), "finance_payee_ref": "fin:payee-west",
                                                    "tax_info_ref": tax_ref}, andre=True)

    def deal(self, partner_id: str, value: str = "8000.00", ref: str = "org:client1", name: str = "Client One LLC",
             domain: str = "clientone.test", brand: str = "zbm") -> dict:
        return self.ok(self.post("/partner-deals", {"request_id": rid(), "partner_id": partner_id, "brand": brand,
                                                    "counterparty": {"ref": ref, "name": name, "domain": domain},
                                                    "deal_value": value}), 201)

    def won_deal(self, value: str = "8000.00", rate: str = "10.00") -> dict:
        p = self.partner()
        self.rate(p["partner_id"], rate)
        self.ok(self.payee(p["partner_id"]))
        d = self.deal(p["partner_id"], value)
        return self.ok(self.post(f"/partner-deals/{d['deal_id']}/won",
                                 {"request_id": rid(), "agreement_kind": "referral_agreement"}, andre=True))

    def money_event(self, deal_id: str, kind: str, amount: str, ev: Optional[str] = None):
        return self.post("/finance/events", {"request_id": rid(), "finance_event_id": ev or f"fin:ev-{uuid.uuid4().hex}",
                                             "deal_id": deal_id, "kind": kind, "amount": amount, "currency": "USD"},
                         caller="finance_31")

    # --- builders: outreach
    def contact(self, email: str = "pat@westagency.test", brand: str = "zbm", verify: bool = True, **extra) -> dict:
        c = self.ok(self.post("/contacts", {"request_id": rid(), "brand": brand, "email": email, "name": "Pat Lee",
                                            **extra}), 201)
        if verify:
            c = self.ok(self.post(f"/contacts/{c['contact_id']}/merge-fields",
                                  {"request_id": rid(), "first_name": "Pat", "company": "West Coast Agency"},
                                  caller="dashboard"))
        return c

    def template(self, key: str = "intro", brand: str = "zbm", subject: str = "A referral partnership idea",
                 body: str = "Hi {{first_name}}, we think {{company}} and Z Best Media could refer work to each other.",
                 approve: bool = True) -> dict:
        t = self.ok(self.post("/templates", {"request_id": rid(), "template_key": key, "brand": brand,
                                             "subject": subject, "body": body}), 201)
        if approve:
            v = t["versions"][-1]
            t = self.ok(self.post(f"/templates/{t['template_id']}/versions/{v['version']}/approve",
                                  {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True))
        return t

    def queue(self, c: dict, t: dict, version: int = 1, sha: Optional[str] = None):
        v = t["versions"][version - 1]
        return self.post("/outreach/email", {"request_id": rid(), "contact_id": c["contact_id"],
                                             "template_id": t["template_id"], "version": version,
                                             "content_sha256": sha or v["content_sha256"]})
