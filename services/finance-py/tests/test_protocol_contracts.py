"""
§D.1: every existing Finance stand-in in the other services must be callable through this service. Each caller's
own answer dataclass is loaded READ-ONLY from its source file (compliance-py and clipper-network-py ``src/ports.py``,
verification-py ``src/ports.py``, creative-py ``src/shared/departments.py``, onboarding-py
``src/integrations/departments.py``) and built from this service's JSON exactly as the spec's mapping column says a
thin client would. Nothing in those services is modified or imported by ``src/``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from helpers import Harness, rid

SERVICES = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str, extra_path: str | None = None):
    if extra_path and extra_path not in sys.path:
        sys.path.append(extra_path)
    spec = importlib.util.spec_from_file_location(name, SERVICES / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


CMP = _load("peer_compliance_ports", "compliance-py/src/ports.py")
CN = _load("peer_cn_ports", "clipper-network-py/src/ports.py")
VI = _load("peer_vi_ports", "verification-py/src/ports.py")
CRE = _load("peer_creative_departments", "creative-py/src/shared/departments.py")
ONB = _load("peer_onboarding_departments", "onboarding-py/src/integrations/departments.py",
            str(SERVICES / "onboarding-py" / "src"))


# --- the mappings of spec §D.1 (what each caller's thin client does) ---------------------------------------------------

def cmp_tax(j):
    tm = {"matched": True, "mismatched": False}.get(j["tin_match"])
    return CMP.TaxStatus(j["available"], j["form_kind"], j["form_on_file"], tm, j["w8_current"],
                         j["services_outside_us_attested"], j["reason"])


def cmp_rail(j):
    return CMP.RailStatus(j["available"], j["status"], j["payouts_enabled"], j["reason"])


def cn_tax(j):
    return CN.TaxAnswer(j["available"], j["form_on_file"])


def cn_rate_card(j):
    return CN.RateCardAnswer(True, j["published"], j["doc_id"], str(j["version"]), j["sha256"])


def cn_open_items(j):
    return CN.OpenItemsAnswer(j["state"] != "unknown", j["state"])


def cn_ack(j):
    return CN.Ack(True, j["ok"], j["reference"])


def vi_identity(j):
    return VI.PayoutIdentity(j["available"], j["identity_hmac"])


def creative_gate(j):
    return CRE.GateResult(j["department"], j["allowed"], j["reason"], j["reference"])


def onb_ruling(j):
    return ONB.Ruling(j["allowed"], tuple(j["unmet"]), j["detail"])


# --- day one (production wiring: every dependency a stand-in) -----------------------------------------------------------

def test_day_one_every_protocol_answer_is_negative_never_fine():
    x = Harness(passing=False).ready()
    t = cmp_tax(x.ok(x.get("/fin/v1/payees/p1/tax-status", caller="compliance_38")))
    assert t.available is False and t.tin_match is None
    assert cmp_rail(x.ok(x.get("/fin/v1/payees/p1/rail-status", caller="compliance_38"))).available is False
    assert cn_tax(x.ok(x.get("/fin/v1/payees/p1/tax-status", caller="clipper_network"))).available is False
    assert cn_open_items(x.ok(x.get("/fin/v1/payees/p1/open-items", caller="clipper_network"))).state == "unknown"
    assert vi_identity(x.ok(x.get("/fin/v1/payees/p1/payout-identity", caller="verification_integrity"))).available is False
    g = creative_gate(x.ok(x.handoff()))
    assert g.department == "finance_31" and g.allowed is False and g.reason
    r = onb_ruling(x.ok(x.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "p1", "kind": "clipper",
                                                    "declared_country": "US"}, caller="onboarding")))
    assert r.allowed is False and r.unmet
    b = onb_ruling(x.ok(x.get("/fin/v1/clients/c1/billing-readiness", caller="onboarding")))
    assert b.allowed is False and b.unmet


def test_with_passing_fakes_every_protocol_answer_maps_cleanly(hr):
    inv = hr.fund_campaign()
    act = onb_ruling(hr.ok(hr.post("/fin/v1/payees", {"request_id": rid(), "payee_id": "clip-a", "kind": "clipper",
                                                        "declared_country": "US",
                                                        "callback_contact_ref": "vault:c-a"}, caller="onboarding")))
    assert act == ONB.Ruling(True, (), "payee active")
    t = cmp_tax(hr.ok(hr.get("/fin/v1/payees/clip-a/tax-status", caller="compliance_38")))
    assert t.available and t.form_kind == "w9" and t.form_on_file and t.tin_match is True
    rs = cmp_rail(hr.ok(hr.get("/fin/v1/payees/clip-a/rail-status", caller="compliance_38")))
    assert rs.available and rs.status == "verified" and rs.payouts_enabled
    assert cn_tax(hr.ok(hr.get("/fin/v1/payees/clip-a/tax-status", caller="clipper_network"))).form_on_file is True
    doc = next(iter(hr.svc.db["rate_cards"].values()))
    rc = cn_rate_card(hr.ok(hr.get(f"/fin/v1/rate-cards/{doc['doc_id']}/versions/1", caller="clipper_network")))
    assert rc.published and rc.sha256 == doc["sha256"] and rc.version == "1"
    meta = hr.ok(hr.get(f"/fin/v1/rate-cards/{doc['doc_id']}/versions/1", caller="clipper_network"))
    assert "creator_rate_per_1000" not in meta and meta["rules_pinned"] is True          # metadata only, no money
    pi = vi_identity(hr.ok(hr.get("/fin/v1/payees/clip-a/payout-identity", caller="verification_integrity")))
    assert pi.available and re.fullmatch(r"[0-9a-f]{64}", pi.identity_hmac)
    assert pi == vi_identity(hr.ok(hr.get("/fin/v1/payees/clip-a/payout-identity", caller="verification_integrity")))
    oi = cn_open_items(hr.ok(hr.get("/fin/v1/payees/clip-a/open-items", caller="clipper_network")))
    assert oi.state == "none"
    g = creative_gate(hr.ok(hr.handoff()))
    assert g.allowed and g.reference.startswith("fin-pay-")
    assert cn_open_items(hr.ok(hr.get("/fin/v1/payees/clip-a/open-items", caller="clipper_network"))).state == "open"
    ack_j = hr.ok(hr.post("/fin/v1/payees/clip-a/offboarding-notices", {"request_id": "off-r", "offboarding_id": "off-1"},
                          caller="clipper_network"))
    assert cn_ack(ack_j).ok and ack_j["request_id"] == "off-r" and len(ack_j["facts_sha256"]) == 64
    hr.put("/fin/v1/clients/client-1/billing-profile", {"request_id": rid(), "entity": "zbc", "payment_method": "ach",
                                                       "msa": {"doc_id": "msa", "version": 1, "doc_sha256": "a" * 64,
                                                               "acceptance_id": "acc"}})
    br = onb_ruling(hr.ok(hr.get("/fin/v1/clients/client-1/billing-readiness", caller="onboarding")))
    assert br.allowed, br
    bud = hr.ok(hr.get("/fin/v1/campaigns/camp-1/budget", caller="creative_production"))
    assert bud["budget_state"] == "open" and isinstance(bud["remaining_views_estimate"], int)
    assert not any(re.fullmatch(r"[0-9]+\.[0-9]{2}", str(v)) for v in bud.values())      # no money for CN/Creative


def test_handoff_answer_echoes_request_and_facts_hash(hr):
    hr.fund_campaign()
    hr.payee()
    r = hr.ok(hr.handoff())
    import hashlib
    import json
    facts = {"submission_id": "sub-1", "eligible": True, "blockers": [], "clip_review_outcome": "pass",
             "verification": {"verified": True, "reason": "", "attestation_id": "att-1"},
             "compliance": {"allowed": True, "reason": "", "reference": "cmp-rul-sub-1"},
             "note": "Eligibility only: Creative sets no amounts; Finance (31) pays from verified views."}
    want = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert r["facts_sha256"] == want and r["request_id"].startswith("r-") and r["submission_id"] == "sub-1"
    assert r["rules_pinned"] is True
