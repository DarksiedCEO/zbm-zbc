"""Test harness and fact builders (no network: every port is a fake from fakes.py)."""

from __future__ import annotations

import copy
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
from fakes import FakeA11y, FakeFetcher, FakeLedgerClient, FakeSanctions, PassingFinance, PassingLegal, PassingVerification
from service import Ports

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
SERVICE_TOKEN = "test-compliance-service-token-do-not-use"
ANDRE_TOKEN = "test-andre-approval-token-compliance-do-not-use"
CALLERS = {n: f"test-caller-token-{n}-0123456789abcdefghijkl" for n in config_mod.CALLER_NAMES}
SEED_PATH = Path(__file__).resolve().parents[1] / "seed" / "compliance_obligations_seed.json"
SEED_ROWS = json.loads(SEED_PATH.read_bytes())["rows"]
SEED_IDS = {r["id"] for r in SEED_ROWS}
_ids = itertools.count(1)


def rid(prefix: str = "r") -> str:
    return f"{prefix}-{next(_ids)}"


def base_env(**over) -> dict:
    env = {"COMPLIANCE_SERVICE_TOKEN": SERVICE_TOKEN, "COMPLIANCE_ANDRE_APPROVAL_TOKEN": ANDRE_TOKEN,
           "COMPLIANCE_CALLER_TOKENS": json.dumps(CALLERS)}
    env.update({k: v for k, v in over.items() if v is not None})
    return {k: v for k, v in env.items() if v != "__unset__"}


def passing_ports(clock) -> Ports:
    return Ports(verification=PassingVerification(), finance=PassingFinance(), legal=PassingLegal(),
                 sanctions=FakeSanctions(), accessibility=FakeA11y(), fetcher=FakeFetcher(clock))


class Harness:
    def __init__(self, ports: Optional[Ports] = None, env: Optional[dict] = None, data_dir: Optional[str] = None,
                 clock: Optional[FixedClock] = None, ledger: Optional[FakeLedgerClient] = None):
        self.clock = clock or FixedClock(NOW)
        self.ledger = ledger or FakeLedgerClient()
        self.ports = ports or passing_ports(self.clock)
        e = base_env(**(env or {}))
        if data_dir:
            e["COMPLIANCE_DATA_DIR"] = data_dir
        self.settings = config_mod.load(e)
        self.svc = api.build_service(self.settings, self.clock, self.ports, self.ledger)
        self.app = api.create_app(self.svc, self.settings)
        self.client = TestClient(self.app)

    # --- raw HTTP ---------------------------------------------------------------
    def headers(self, caller=None, andre=None, bearer=SERVICE_TOKEN):
        hd = {}
        if bearer is not None:
            hd["Authorization"] = f"Bearer {bearer}"
        if caller:
            hd["X-Compliance-Caller-Token"] = CALLERS.get(caller, caller)
        if andre is not None:
            hd["X-Andre-Approval-Token"] = andre
        return hd

    def post(self, path, body, caller=None, andre=None, bearer=SERVICE_TOKEN):
        return self.client.post(path, json=body, headers=self.headers(caller, andre, bearer))

    def get(self, path, caller="scheduler", **params):
        return self.client.get(path, headers=self.headers(caller), params=params)

    # --- register ------------------------------------------------------------------
    def inbox(self):
        r = self.get("/compliance/v1/inbox")
        assert r.status_code == 200, r.text
        return r.json()

    def decide(self, decisions, andre=ANDRE_TOKEN):
        return self.post("/compliance/v1/register/decisions", {"request_id": rid("dec"), "decisions": decisions}, andre=andre)

    def approve(self, *proposals, andre=ANDRE_TOKEN, acknowledge_weakening=False):
        """``acknowledge_weakening`` (AEGIS N14-9): Andre's explicit acknowledgment, needed for a weakening proposal."""
        ack = {"acknowledge_weakening": True} if acknowledge_weakening else {}
        r = self.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
                          **ack} for p in proposals], andre=andre)
        assert r.status_code == 200, r.text
        return r.json()

    def approve_seed(self):
        seed = [p for p in self.inbox() if p["kind"] == "seed"][0]
        return self.approve(seed)

    def propose(self, body, andre=ANDRE_TOKEN, caller=None):
        return self.post("/compliance/v1/register/proposals", {"request_id": rid("prop"), **body},
                         andre=andre if caller is None else None, caller=caller)

    def run_controls(self):
        r = self.post("/compliance/v1/controls/internal/run", {"request_id": rid("ctl")}, caller="scheduler")
        assert r.status_code == 200, r.text
        return r.json()

    # --- sanctions / gates -------------------------------------------------------------
    def screen(self, subject, role="payee", owner_of=None, country="US", region="US-CA", caller="onboarding"):
        body = {"request_id": rid("scr"), "subject_id": subject, "role": role, "owner_of": owner_of,
                "legal_name": f"Name of {subject}", "aliases": [], "dob": "1990-01-01", "country": country,
                "region": region}
        r = self.post("/compliance/v1/sanctions/screen", body, caller=caller)
        assert r.status_code == 200, r.text
        return r.json()

    def rule(self, subject, lane, facts, caller="onboarding", request_id=None):
        return self.post("/compliance/v1/rule", {"request_id": request_id or rid("rule"), "subject_id": subject,
                                                  "lane": lane, "facts": facts}, caller=caller)

    def review(self, kind, subject, facts, caller="creative_production", request_id=None, caller_context=None):
        body = {"request_id": request_id or rid("rev"), "subject_kind": kind, "subject_id": subject, "facts": facts}
        if caller_context is not None:
            body["caller_context"] = caller_context
        return self.post("/compliance/v1/review", body, caller=caller)

    def activate_creator(self, clipper="clipper-1", **kw):
        s = self.screen(clipper, country=kw.get("country", "US"), region=kw.get("region", "US-CA"))
        f = creator_facts(s["screen_id"], **kw)
        r = self.rule(clipper, "zbc_creator", f)
        assert r.status_code == 200, r.text
        return r.json()

    def activate_brand(self, campaign="camp-1", **kw):
        r = self.rule(campaign, "zbc_brand", brand_facts(**kw))
        assert r.status_code == 200, r.text
        return r.json()

    def activate_client_for_publish(self, client="client-1", targets=("US", "CA-ON"),
                                    platforms=("web", "youtube", "email", "sms")):
        """AEGIS N14-2: every publish needs the client's current, allowed activation covering its targets
        and platforms (publish_facts defaults: US, web)."""
        return self.activate_client(client, targets=targets, platforms=platforms)

    def activate_client(self, client="client-1", **kw):
        r = self.rule(client, "client", client_facts(**kw))
        assert r.status_code == 200, r.text
        return r.json()

    def memo_supersede(self, cq: str):
        row = dict(next(r for r in SEED_ROWS if r["id"] == cq))
        memo_sha = hashlib.sha256(f"memo-{cq}".encode()).hexdigest()
        url = f"urn:zbm:counsel-memo:{cq.lower()}"
        new = {**row, "id": f"{cq}-M1", "domain": "counsel", "title": f"Counsel memo answering {cq}",
               "obligation": f"Counsel memo answering {cq}; approved by Andre.", "source_url": url,
               "additional_sources": [], "source_kind": "guidance", "source_quality": "primary",
               "verified_at": NOW.date().isoformat(), "expires_at": None, "status": "verified", "check": "engine_invariant",
               "counsel_flag": False, "penalty_note": "", "report_ref": None, "effective_date": None,
               "effective_note": None, "parameters": {}}
        ev = {"source_url": url, "fetched_at": iso(NOW), "snapshot_sha256": memo_sha, "normalized_text_sha256": memo_sha,
              "quoted_excerpt": "Memo conclusion (test fixture).", "doc_number": None}
        r = self.propose({"kind": "supersede", "target_id": cq, "proposed_row": new, "evidence": ev})
        assert r.status_code == 201, r.text
        return r.json()["proposal"]


def flags_false(names) -> dict:
    return {n: False for n in names}


ACT_FLAGS = ("political_content", "child_directed", "health_data_shared", "biz_opp_client", "audience_data_sale",
             "marketplace", "pooled_client_funds")


def jur(country="US", region="US-CA", attested=True):
    return {"declared_country": country, "declared_region": region, "attested": attested, "attestation_ref": "att-1"}


def creator_facts(screen_id, country="US", region="US-CA", **over) -> dict:
    f = {"jurisdiction": jur(country, region), "network_country_signal": country, "flags": flags_false(ACT_FLAGS),
         "age": {"verification_attestation_id": "vi-age-1", "method": "photo_id_match", "dob_field_neutral": True},
         "recruitment_channel": "inbound", "payee_type": "individual", "sanctions_screen_id": screen_id,
         "owner_screen_ids": [], "tax_form_kind": "w9", "rail_kyc": {"status": "verified", "rail": "stripe"},
         "creator_agreement_version": "cav-3", "disclosure_training_attested": True,
         "accounts": [{"platform": "youtube", "handle_sha256": "b" * 64}, {"platform": "tiktok", "handle_sha256": "c" * 64}],
         "accounts_complete_attested": True}
    f.update(over)
    return f


def client_facts(targets=("US",), platforms=("youtube",), country="US", region="US-NY", **over) -> dict:
    f = {"jurisdiction": jur(country, region), "flags": flags_false(ACT_FLAGS), "target_jurisdictions": list(targets),
         "platforms": list(platforms), "client_category": "general",
         "claims": {"claims_present": False, "health_or_earnings_claim": False, "claim_file_id": None,
                    "claim_file_approved": False},
         "services_include_review_suppression": False, "outbound_cold_contact_countries": [],
         "hbnr_clause_signed": False}
    f.update(over)
    return f


def brand_facts(**kw) -> dict:
    over = {k: kw.pop(k) for k in list(kw) if k in ("pay_basis", "sentiment_conditions")}
    f = client_facts(**kw)
    f.update({"pay_basis": "verified_views", "sentiment_conditions": False})
    f.update(over)
    return f


CLIP_FLAGS = ("synthetic_performer", "ai_manipulated_media", "real_person_likeness", "implied_affiliation",
              "personal_use_claim")


def clip_facts(clipper="clipper-1", campaign="camp-1", platform="youtube", **over) -> dict:
    f = {"campaign_id": campaign, "rulebook_version": 1, "post_ref": "https://youtube.example/v/abc", "clipper_id": clipper,
         "posted_at": iso(NOW - timedelta(days=15)), "clip_review": "pass", "platform": platform,
         "disclosure": {"platform_toggle_evidence_ref": "toggle-ev-1", "in_video_label_text": "#ad Sponsored",
                        "in_video_label_start_s": 1, "voice_present": False, "audio_disclosure_present": False},
         "music": {"present": False, "source": "none", "track_or_license_id": None}, "rights_clearance_id": "rc-1",
         "flags": flags_false(CLIP_FLAGS), "consent_document_id": None, "personal_use_attested": False}
    for k, v in over.items():
        if k == "disclosure":
            f["disclosure"] = {**f["disclosure"], **v}
        elif k == "flags":
            f["flags"] = {**f["flags"], **v}
        else:
            f[k] = v
    return f


PUB_FLAGS = ("paid_or_endorsement", "synthetic_performer", "ai_manipulated_media", "real_person_likeness",
             "implied_affiliation", "personal_use_claim", "claims_present", "health_or_earnings_claim", "child_directed",
             "child_access_likely", "political_content", "audience_data_sale", "uses_tracking", "collects_pii",
             "consumer_ecommerce")


def publish_facts(asset_type="site", client="client-1", targets=("US",), platforms=("web",), **over) -> dict:
    f = {"brief_id": "brief-1", "asset_type": asset_type, "asset_content_sha256": "d" * 64, "client_id": client,
         "target_jurisdictions": list(targets), "platforms": list(platforms), "flags": flags_false(PUB_FLAGS),
         "claim_file_id": None, "claim_file_approved": False}
    if asset_type in ("site", "landing_page", "form", "portal"):
        f["docs"] = {"privacy_policy": {"doc_id": "pp", "version": "v1"}, "terms": {"doc_id": "tos", "version": "v1"},
                     "forms": []}
        f["tracking"] = {"tracking_disclosed": True, "consent_banner_present": True, "consent_before_nonessential": True,
                         "opt_out_present": True}
    if asset_type == "ad_video":
        f["music"] = {"present": False, "source": "none", "track_or_license_id": None}
    for k, v in over.items():
        if k == "flags":
            f["flags"] = {**f["flags"], **v}
        else:
            f[k] = v
    return f


def unmet_ids(resp_json) -> set:
    return {u["obligation_id"] for u in resp_json["unmet"]}


def unmet_codes(resp_json) -> set:
    return {(u["obligation_id"], u["code"]) for u in resp_json["unmet"]}


def deep(x):
    return copy.deepcopy(x)
