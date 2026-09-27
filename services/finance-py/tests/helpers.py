"""Test harness (no network: every port is a fake from fakes.py, the ledger is FakeLedgerClient)."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock, iso
from fakes import (RAIL_SIG, FakeBank, FakeCN, FakeCompliance, FakeGL, FakeLedgerClient, FakeLegal, FakePeople,
                   FakePush, FakeRail, FakeTax, FakeVI, FakeVault)
from intelligences import i01_journal as J
from ports import Ports

NOW = datetime(2026, 10, 2, 17, 0, tzinfo=timezone.utc)        # Friday 10:00 America/Los_Angeles
SERVICE_TOKEN = "test-fin-service-token-do-not-use-0123456"
ANDRE_TOKEN = "test-andre-approval-token-fin-do-not-use-01"
SECOND_TOKEN = "test-second-approver-token-fin-do-not-use"
CALLERS = {n: f"test-fin-caller-{n}-0123456789abcdefghij" for n in config_mod.CALLER_NAMES}
_ids = itertools.count(1)
HARNESSES: list = []            # conftest's G9 fixture checks every harness a test built


def rid(prefix: str = "r") -> str:
    return f"{prefix}-{next(_ids)}"


def base_env(**over) -> dict:
    env = {"FIN_SERVICE_TOKEN": SERVICE_TOKEN, "FIN_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
           "FIN_CALLER_TOKENS": json.dumps(CALLERS)}
    env.update({k: v for k, v in over.items() if v is not None})
    return {k: v for k, v in env.items() if v != "__unset__"}


def make_fakes(clock) -> dict:
    rails = {"stripe": FakeRail("stripe", clock), "trolley": FakeRail("trolley", clock)}
    rails["trolley"].balance_available = False                 # dormant, like the stand-in (R8)
    return {"vi": FakeVI(), "compliance": FakeCompliance(clock), "cn": FakeCN(), "legal": FakeLegal(), "rails": rails,
            "bank": FakeBank(clock, rails), "tax": FakeTax(), "gl": FakeGL(), "vault": FakeVault(),
            "people": FakePeople(), "push": FakePush()}


class Harness:
    def __init__(self, env: Optional[dict] = None, data_dir: Optional[str] = None, clock: Optional[FixedClock] = None,
                 ledger: Optional[FakeLedgerClient] = None, passing: bool = True, fakes: Optional[dict] = None,
                 log=None):
        self.clock = clock or FixedClock(NOW)
        self.ledger = ledger if ledger is not None else FakeLedgerClient()
        self.f = fakes or make_fakes(self.clock)
        ports = Ports(vi=self.f["vi"], compliance=self.f["compliance"], cn=self.f["cn"], legal=self.f["legal"],
                      rails=self.f["rails"], bank=self.f["bank"], tax=self.f["tax"], gl=self.f["gl"],
                      vault=self.f["vault"], people=self.f["people"], push=self.f["push"]) if passing else Ports()
        e = base_env(**(env or {}))
        if data_dir:
            e["FIN_DATA_DIR"] = data_dir
        self.env = e
        self.settings = config_mod.load(e)
        if log is not None:
            from service import Service
            from ledger import Recorder
            seed = open(self.settings.seed_path, "rb").read()
            self.svc = Service(self.settings, Recorder(self.ledger), log, seed, config_mod.PINNED_SEED_SHA256, ports,
                               self.clock, config_mod.PINNED_SEED_SHA256)
        else:
            self.svc = api.build_service(self.settings, self.clock, ports, self.ledger)
        self.app = api.create_app(self.svc, self.settings)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        HARNESSES.append(self)

    # --- fakes shortcuts
    @property
    def vi(self):
        return self.f["vi"]

    @property
    def cmp(self):
        return self.f["compliance"]

    @property
    def stripe(self):
        return self.f["rails"]["stripe"]

    @property
    def bank(self):
        return self.f["bank"]

    # --- raw HTTP ---------------------------------------------------------------------------
    def headers(self, caller=None, andre=None, bearer=SERVICE_TOKEN, second=None):
        hd = {}
        if bearer is not None:
            hd["Authorization"] = f"Bearer {bearer}"
        if caller:
            hd["X-FIN-Caller-Token"] = CALLERS.get(caller, caller)
        if andre is not None:
            hd["X-Andre-Approval-Token"] = andre
        if second is not None:
            hd["X-FIN-Second-Approver-Token"] = second
        return hd

    def post(self, path, body, caller=None, andre=None, bearer=SERVICE_TOKEN, second=None):
        return self.client.post(path, json=body, headers=self.headers(caller, andre, bearer, second))

    def put(self, path, body, andre=ANDRE_TOKEN):
        return self.client.put(path, json=body, headers=self.headers(None, andre))

    def get(self, path, caller="scheduler", andre=None, **params):
        return self.client.get(path, headers=self.headers(caller, andre), params=params)

    def ok(self, r, code=200):
        assert r.status_code == code, (r.status_code, r.text[:3000])
        return r.json()

    # --- rules ----------------------------------------------------------------------------------
    def rules(self):
        return self.ok(self.get("/fin/v1/rules"))

    def approve_rules(self):
        seed = [p for p in self.rules()["open_proposals"] if p["kind"] == "seed"][0]
        return self.ok(self.post("/fin/v1/rules/decisions", {"request_id": rid("dec"), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=ANDRE_TOKEN))

    def verify_counsel(self, *cqs):
        rows = {r["rule_id"]: r for r in self.rules()["rules"]}
        for cq in cqs:
            row = dict(rows[cq])
            row.pop("in_force", None)
            row["status"] = "verified"
            row["parameters"] = {**row["parameters"], "memo_ref": f"memo-{cq}-2026"}
            p = self.ok(self.post("/fin/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": cq,
                                                              "proposed_row": row}, andre=ANDRE_TOKEN), 201)["proposal"]
            self.ok(self.post("/fin/v1/rules/decisions", {"request_id": rid(), "decisions": [
                {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
                 "acknowledge_weakening": True}]}, andre=ANDRE_TOKEN))

    def access_review(self):
        return self.ok(self.post("/fin/v1/controls/FC-05/results", {"request_id": rid(), "result": "pass",
                                                                   "evidence_ref": "roster-2026-10"}, andre=ANDRE_TOKEN))

    def ready(self, counsel=("FIN-CQ-01", "FIN-CQ-11")):
        self.approve_rules()
        self.verify_counsel(*counsel)
        self.access_review()
        return self

    # --- books ----------------------------------------------------------------------------------
    def rate_card(self, campaign="camp-1", rate="2.35", cap=1_000_000, effective_at="2026-09-01T00:00:00Z", tiers=None):
        rates = tiers or {t: rate for t in ("T0", "T1", "T2", "T3")}
        p = self.ok(self.post("/fin/v1/rate-cards/proposals", {"request_id": rid(), "campaign_id": campaign,
                                                                "creator_rate_per_1000": rates,
                                                                "max_paid_views_per_clip": cap,
                                                                "effective_at": effective_at}, andre=ANDRE_TOKEN), 201)
        p = p["proposal"]
        self.ok(self.post("/fin/v1/rate-cards/decisions", {"request_id": rid(), "decisions": [
            {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
             "acknowledge_weakening": True}]}, andre=ANDRE_TOKEN))
        return p["doc_id"]

    def profile(self, campaign="camp-1", client="client-1", budget="1000.00", client_rate="4.00", doc_id=None):
        doc = {"doc_id": "of-1", "version": 1, "doc_sha256": "a" * 64, "acceptance_id": "acc-1"}
        return self.ok(self.put(f"/fin/v1/campaigns/{campaign}/commercial-profile",
                                {"request_id": rid(), "client_id": client, "order_form": doc, "budget": budget,
                                 "client_rate_per_1000": client_rate, "rate_card_doc_id": doc_id}))

    def deposit_invoice(self, campaign="camp-1", client="client-1", amount="1000.00"):
        inv = self.ok(self.post("/fin/v1/invoices", {
            "request_id": rid(), "entity": "zbc", "client_id": client, "campaign_id": campaign,
            "kind": "campaign_deposit", "lines": [{"line_code": "campaign_deposit", "quantity": 1, "unit_price": amount}],
            "payment_methods": ["ach", "wire"], "legal_ref": {"doc_id": "of-1", "version": 1, "doc_sha256": "a" * 64,
                                                              "acceptance_id": "acc-1"}}, caller="scheduler"), 201)["invoice"]
        return self.ok(self.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                                 {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"},
                                 andre=ANDRE_TOKEN))["invoice"]

    def receive(self, entity, account, amount, token=None):
        self.bank.deposit(entity, account, amount)
        line = {"txn_ref_sha256": hashlib.sha256(rid("txn").encode()).hexdigest(), "entity": entity, "account": account,
                "direction": "credit", "amount": amount, "value_date": "2026-10-02"}
        if token:
            line["reference_token"] = token
        return self.ok(self.post("/fin/v1/bank/events", {"request_id": rid(), "lines": [line]}, caller="bank_feed"))

    def fund_campaign(self, campaign="camp-1", client="client-1", budget="1000.00", rate="2.35", client_rate="4.00",
                      cap=1_000_000):
        doc = self.rate_card(campaign, rate, cap)
        self.profile(campaign, client, budget, client_rate, doc)
        inv = self.deposit_invoice(campaign, client, budget)
        self.receive("zbc", "1020", budget, inv["invoice_id"])
        return inv

    # --- payees and payables ----------------------------------------------------------------------------------------
    def payee(self, pid="clip-a", country="US", contact=True, **kw):
        body = {"request_id": rid(), "payee_id": pid, "kind": "clipper", "declared_country": country, **kw}
        if contact:
            body["callback_contact_ref"] = f"vault:contact-{pid}"
        return self.ok(self.post("/fin/v1/payees", body, caller="clipper_network"))

    def handoff(self, sid="sub-1", clipper="clip-a", campaign="camp-1", views=12345, create_time="2026-09-20T12:00:00Z",
                ruling_id=None, certify=True, rule=True, status="certified", facts=None):
        ruling_id = ruling_id or f"cmp-rul-{sid}"
        if certify:
            self.vi.certify(sid, clipper, campaign, views, create_time, status=status)
        if rule:
            self.cmp.rule(ruling_id, sid)
        f = facts or {"submission_id": sid, "eligible": True, "blockers": [], "clip_review_outcome": "pass",
                      "verification": {"verified": True, "reason": "", "attestation_id": "att-1"},
                      "compliance": {"allowed": True, "reason": "", "reference": ruling_id},
                      "note": "Eligibility only: Creative sets no amounts; Finance (31) pays from verified views."}
        return self.post("/fin/v1/payout-handoffs", {"request_id": rid(), "submission_id": sid, "facts": f},
                         caller="creative_production")

    def accrue(self, sid="sub-1", **kw):
        r = self.ok(self.handoff(sid, **kw))
        assert r["allowed"], r
        return self.ok(self.get(f"/fin/v1/payables/{r['reference']}"))

    # --- reconciliation, runs ---------------------------------------------------------------------------------------
    def recon(self):
        return self.ok(self.post("/fin/v1/reconciliations/run", {"request_id": rid()}, caller="scheduler"))

    def run(self, rail="stripe", code=201):
        return self.ok(self.post("/fin/v1/payout-runs", {"request_id": rid(), "rail": rail}, caller="scheduler"), code)

    def approve(self, batch, **kw):
        return self.ok(self.post(f"/fin/v1/payout-batches/{batch['batch_id']}/decision",
                                 {"request_id": rid(), "content_sha256": batch["content_sha256"], "decision": "approve"},
                                 andre=ANDRE_TOKEN, **kw))

    def fund_batch(self, batch_id):
        op = self.ok(self.post("/fin/v1/treasury/funding", {"request_id": rid(), "batch_id": batch_id},
                               caller="scheduler"), 201)["operation"]
        return self.ok(self.post(f"/fin/v1/treasury/funding/{op['op_id']}/decision",
                                 {"request_id": rid(), "content_sha256": op["content_sha256"], "decision": "approve"},
                                 andre=ANDRE_TOKEN))

    def release(self, batch_id, code=200):
        return self.ok(self.post(f"/fin/v1/payout-batches/{batch_id}/release", {"request_id": rid()}, caller="scheduler"),
                       code)

    def rail_event(self, etype, item_id=None, rail="stripe", **kw):
        ev = {"event_id": rid("evt"), "type": etype, "signature": RAIL_SIG, **kw}
        if item_id:
            ev["item_id"] = item_id
        return self.ok(self.post(f"/fin/v1/rails/{rail}/events", {"request_id": rid(), "events": [ev]},
                                 caller="rail_gateway"))

    def pay_items(self, batch):
        for it in batch["items"]:
            if it["status"] == "submitted":
                self.stripe.paid(it["idempotency_key"])
                self.rail_event("paid", it["item_id"])

    def full_payout(self, advance_h=13):
        """recon -> run -> Andre approves -> funding (Andre) -> +advance_h -> recon -> release."""
        self.recon()
        b = self.run()["batch"]
        self.approve(b)
        self.fund_batch(b["batch_id"])
        self.clock.advance(hours=advance_h)
        self.recon()
        rel = self.release(b["batch_id"])
        return b, rel

    # --- invariants (G9) ---------------------------------------------------------------------------------------------
    def assert_books_balance(self):
        for e in self.svc.entries:
            d, c = J.totals(e)
            assert d == c and d > 0, e["entry_id"]
        for ent in ("zbc", "zbm"):
            tb = self.ok(self.get(f"/fin/v1/journal/{ent}/trial-balance"))
            assert tb["difference"] == "0.00", tb

    def batch(self, bid):
        return self.ok(self.get(f"/fin/v1/payout-batches/{bid}"))

    def bal(self, account, sub=None, entity="zbc"):
        return self.svc.bal(account, sub, entity)

    def all_text(self) -> str:
        parts = [json.dumps(self.ledger.events, default=str)]
        parts += [json.dumps(r) for r in self.svc.log.records]
        cur = 0
        while cur is not None:
            page = self.ok(self.get("/fin/v1/audit/export", cursor=cur))
            parts.append(json.dumps(page))
            cur = page["next_cursor"]
        return "\n".join(parts)
