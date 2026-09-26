"""Test harness and builders (no network: every port is a fake from fakes.py)."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

import api
import config as config_mod
from clock import FixedClock, iso
from fakes import (AGREEMENT_SHA, FakeHub, FakeLedgerClient, FakeMessaging, FakePush, PassingCompliance, PassingCreative,
                   PassingFinance, PassingLegal, PassingPeople, PassingVI)
from ports import Ports

# Monday 2026-09-28 18:00 UTC = 11:00 in Los Angeles (inside the 08:00-20:00 window)
NOW = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
SERVICE_TOKEN = "test-cn-service-token-do-not-use-0000"
HMAC_KEY = "test-cn-identity-hmac-key-do-not-use-000"
ANDRE_TOKEN = "test-andre-approval-token-cn-do-not-use"
CALLERS = {n: f"test-cn-caller-{n}-0123456789abcdefghijklmn" for n in config_mod.CALLER_NAMES}
DELEGATES = {"maria": "test-cn-delegate-maria-0123456789abcdefghij"}
SEED_PATH = Path(__file__).resolve().parents[1] / "seed" / "cn_rules_seed.json"
SEED = json.loads(SEED_PATH.read_bytes())
_ids = itertools.count(1)


def rid(prefix: str = "r") -> str:
    return f"{prefix}-{next(_ids)}"


def rate_sha(doc="rc-1", version="v1") -> str:
    return hashlib.sha256(f"rate card {doc} {version}".encode()).hexdigest()


def base_env(**over) -> dict:
    env = {"CN_SERVICE_TOKEN": SERVICE_TOKEN, "CN_IDENTITY_HMAC_KEY": HMAC_KEY, "CN_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
           "CN_CALLER_TOKENS": json.dumps(CALLERS), "CN_DELEGATE_TOKENS": json.dumps(DELEGATES),
           "CN_POSTAL_ADDRESS": "Z Best Media, 100 Test St, Los Angeles CA 90001",
           "CN_OPT_OUT_URL": "https://zbc.example/opt-out"}
    env.update({k: v for k, v in over.items() if v is not None})
    return {k: v for k, v in env.items() if v != "__unset__"}


def passing_ports(clock) -> Ports:
    return Ports(vi=PassingVI(), compliance=PassingCompliance(), creative=PassingCreative(), finance=PassingFinance(),
                 legal=PassingLegal(), people=PassingPeople(), messaging=FakeMessaging(), hub=FakeHub(), push=FakePush())


class Harness:
    def __init__(self, ports: Optional[Ports] = None, env: Optional[dict] = None, data_dir: Optional[str] = None,
                 clock: Optional[FixedClock] = None, ledger: Optional[FakeLedgerClient] = None, stand_ins: bool = False):
        self.clock = clock or FixedClock(NOW)
        self.ledger = ledger or FakeLedgerClient()
        self.ports = ports or (Ports() if stand_ins else passing_ports(self.clock))
        e = base_env(**(env or {}))
        if data_dir:
            e["CN_DATA_DIR"] = data_dir
        self.data_dir = data_dir
        self.settings = config_mod.load(e)
        self.svc = api.build_service(self.settings, self.clock, self.ports, self.ledger)
        self.app = api.create_app(self.svc, self.settings)
        self.client = TestClient(self.app)

    @property
    def vi(self) -> PassingVI:
        return self.ports.vi

    # --- raw HTTP ---------------------------------------------------------------------------------
    def headers(self, caller=None, andre=None, bearer=SERVICE_TOKEN, delegate=None):
        hd = {}
        if bearer is not None:
            hd["Authorization"] = f"Bearer {bearer}"
        if caller:
            hd["X-CN-Caller-Token"] = CALLERS.get(caller, caller)
        if andre is not None:
            hd["X-Andre-Approval-Token"] = andre
        if delegate is not None:
            hd["X-CN-Delegate-Token"] = DELEGATES.get(delegate, delegate)
        return hd

    def post(self, path, body, caller=None, andre=None, bearer=SERVICE_TOKEN, delegate=None):
        return self.client.post(path, json=body, headers=self.headers(caller, andre, bearer, delegate))

    def put(self, path, body, caller=None, andre=None):
        return self.client.put(path, json=body, headers=self.headers(caller, andre))

    def get(self, path, caller="scheduler", andre=None, **params):
        return self.client.get(path, headers=self.headers(caller, andre), params=params)

    # --- rules -------------------------------------------------------------------------------------
    def inbox(self):
        r = self.get("/cn/v1/inbox")
        assert r.status_code == 200, r.text
        return r.json()

    def decide(self, decisions, andre=ANDRE_TOKEN):
        return self.post("/cn/v1/rules/decisions", {"request_id": rid("dec"), "decisions": decisions}, andre=andre)

    def approve(self, *proposals, ack=False):
        r = self.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
                          **({"acknowledge_weakening": True} if ack else {})} for p in proposals])
        assert r.status_code == 200, r.text
        return r.json()

    def approve_seed(self):
        return self.approve([p for p in self.inbox() if p["kind"] == "seed"][0])

    def propose_rule(self, body):
        return self.post("/cn/v1/rules/proposals", {"request_id": rid("rp"), **body}, andre=ANDRE_TOKEN)

    def propose_template(self, body):
        return self.post("/cn/v1/templates/proposals", {"request_id": rid("tp"), **body}, andre=ANDRE_TOKEN)

    def clear_counsel(self, *cqs):
        props = []
        for cq in cqs:
            r = self.propose_rule({"kind": "counsel_memo", "target_id": cq,
                                   "memo": {"memo_sha256": hashlib.sha256(cq.encode()).hexdigest(),
                                            "memo_ref": f"memo-{cq}", "summary": f"Counsel memo answering {cq} (fixture)"}})
            assert r.status_code == 201, r.text
            props.append(r.json()["proposal"])
        return self.approve(*props)

    def rule(self, rule_id):
        return next(r for r in self.get("/cn/v1/rules").json()["rules"] if r["rule_id"] == rule_id)

    # --- clippers ------------------------------------------------------------------------------------
    def apply(self, email="clip@example.com", caller="hub", **over):
        body = {"request_id": rid("app"), "display_name": "Clip Person", "email": email, "declared_country": "US",
                "declared_region": "US-CA", "jurisdiction_attested": True, "time_zone": "America/Los_Angeles",
                "channel": "inbound_form", "declared_18_plus": True, "sag_aftra_member": False}
        body.update(over)
        return self.post("/cn/v1/applications", {k: v for k, v in body.items() if v != "__omit__"}, caller=caller)

    def ready_applicant(self, email="clip@example.com", dob="1995-05-05", **over):
        r = self.apply(email, **over)
        assert r.status_code == 201, r.text
        cid = r.json()["clipper_id"]
        s = self.post(f"/cn/v1/clippers/{cid}/connections/start",
                      {"request_id": rid("cs"), "platform": "youtube", "redirect_uri": "https://hub.example/cb",
                       "handle": "@clipperson"}, caller="hub")
        assert s.status_code == 200, s.text
        state = s.json()["authorization_url"].split("state=")[1]
        c = self.post(f"/cn/v1/clippers/{cid}/connections/complete",
                      {"request_id": rid("cc"), "state": state, "code": "OAUTH-CODE-SECRET-4f9a2b"}, caller="hub")
        assert c.status_code == 200, c.text
        a = self.post(f"/cn/v1/clippers/{cid}/age-check",
                      {"request_id": rid("age"), "dob": dob, "dob_field_neutral": True, "method": "photo_id_match",
                       "provider_session_ref": "prov-sess-1"}, caller="hub")
        assert a.status_code == 200, a.text
        g = self.accept(cid)
        assert g.status_code == 200 and g.json()["accepted"], g.text
        t = self.post(f"/cn/v1/clippers/{cid}/disclosure-training",
                      {"request_id": rid("trn"), "training_version": "dt-1", "attested": True}, caller="hub")
        assert t.status_code == 200, t.text
        return cid

    def accept(self, cid, version="v3", sha=AGREEMENT_SHA, presented=None):
        return self.post(f"/cn/v1/clippers/{cid}/agreement-acceptances",
                         {"request_id": rid("agr"), "doc_id": "clipper_agreement", "version": version, "doc_sha256": sha,
                          "presented_sha256": presented or sha, "method": "clickwrap_unticked_box", "box_ticked": True,
                          "session_ref": "sess-abc"}, caller="hub")

    def admit(self, cid, caller="hub", request_id=None):
        return self.post(f"/cn/v1/clippers/{cid}/admission", {"request_id": request_id or rid("adm")}, caller=caller)

    def admitted_clipper(self, email="clip@example.com", **over):
        cid = self.ready_applicant(email, **over)
        r = self.admit(cid)
        assert r.status_code == 200 and r.json()["admitted"], r.text
        return cid

    # --- campaigns -----------------------------------------------------------------------------------
    def config(self, campaign="camp-1", **over):
        body = {"request_id": rid("cfg"), "min_tier": "T0", "platforms": ["youtube", "tiktok"],
                "clipper_jurisdictions": ["US"], "max_clippers": 10, "max_submissions_per_clipper": 5,
                "view_terms": {"min_views_to_review": 1000, "max_paid_views_per_clip": 1_000_000},
                "rate_card_ref": {"finance_doc_id": "rc-1", "version": "v1", "sha256": rate_sha()},
                "rate_card_effective_at": iso(self.clock.now()), "opens_at": iso(self.clock.now() - timedelta(days=1)),
                "closes_at": iso(self.clock.now() + timedelta(days=60))}
        body.update(over)
        return self.put(f"/cn/v1/campaigns/{campaign}/network-config", body, andre=ANDRE_TOKEN)

    def enrol(self, cid, campaign="camp-1", request_id=None):
        return self.post(f"/cn/v1/campaigns/{campaign}/enrolments", {"request_id": request_id or rid("enr"),
                                                                     "clipper_id": cid}, caller="hub")

    def run(self, path, caller="scheduler"):
        return self.post(path, {"request_id": rid("run")}, caller=caller)

    def messages(self, cid):
        r = self.get(f"/cn/v1/clippers/{cid}/messages", caller="hub")
        assert r.status_code == 200, r.text
        return r.json()

    def ready(self):
        """Seed approved; the enrolment counsel hold CN-CQ-01 answered by a memo."""
        self.approve_seed()
        self.clear_counsel("CN-CQ-01")
        return self


def codes(resp_json) -> set:
    return {(u["rule_id"], u["code"]) for u in resp_json["unmet"]}


def code_set(resp_json) -> set:
    return {u["code"] for u in resp_json["unmet"]}
