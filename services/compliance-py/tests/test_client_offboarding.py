"""Client offboarding, Compliance's side (ADR 0027 decision 3; MSA 15.5): a dry run lists what Compliance holds; the
real run closes the activation and records a hash-only certificate; OFAC records are kept (10 years); an open hold
blocks; Andre only."""

from __future__ import annotations

import hashlib
import json

from helpers import ANDRE_TOKEN, rid
from test_owner_screening import CLIENT, TREE, attest_owner, put_ownership, rule, screened_tree
from test_service_area_gate import harness, status

OFFB = f"/compliance/v1/clients/{CLIENT}/offboarding"


def dry(h):
    r = h.post(OFFB, {"request_id": rid(), "dry_run": True}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    return r.json()


def test_dry_run_then_real_run_closes_the_activation_and_keeps_the_ofac_records():
    h = harness()
    screened_tree(h)
    assert rule(h)["allowed"] is True
    n = len(h.ledger.events)
    p = dry(h)
    assert len(h.ledger.events) == n                                   # a dry run records nothing
    held = {x["kind"]: x for x in p["held"]}
    assert held["ofac_searches"]["count"] == 4 and held["ofac_searches"]["action"] == "keep_10_years"
    assert held["ownership_record"]["count"] == 1 and held["activation"]["action"] == "close"
    for k in ("ofac_searches", "ownership_record"):                    # MSA 15.5(a), changelog 46
        assert held[k]["retention"].startswith("10 years") and "MSA 15.5(a)" in held[k]["retention"], k
    assert held["activation_rulings"]["retention"].startswith("7 years")
    r = h.post(OFFB, {"request_id": rid(), "dry_run": False, "plan_sha256": p["plan_sha256"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["certificate_sha256"] == hashlib.sha256(
        json.dumps(out["certificate"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert CLIENT not in json.dumps(out["certificate"])
    assert h.ledger.of_type("client_offboarded")
    s = status(h, CLIENT)
    assert s["decision"] == "REFUSE" and any("offboarded" in x for x in s["reasons"])
    assert len(h.svc.attestations) == 4 and (("client", CLIENT) in h.svc.ownerships)   # kept
    # serving the client again needs a new activation (with fresh searches)
    for o in TREE:
        attest_owner(h, o["owner_id"])
    assert rule(h)["allowed"] is True and status(h, CLIENT)["decision"] == "ALLOW"


def test_an_open_hold_blocks_and_a_stale_plan_is_refused():
    h = harness()
    assert put_ownership(h, [dict(TREE[0], ownership_bp=10000)]).status_code == 200
    p = dry(h)
    attest_owner(h, "own-a", result="potential_match")                # the state moves: a hold opens
    r = h.post(OFFB, {"request_id": rid(), "dry_run": False, "plan_sha256": p["plan_sha256"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "plan" in r.text
    p = dry(h)
    assert p["blockers"]
    r = h.post(OFFB, {"request_id": rid(), "dry_run": False, "plan_sha256": p["plan_sha256"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "blocked" in r.text


def test_only_andre_and_the_real_run_needs_a_plan():
    h = harness()
    assert h.post(OFFB, {"request_id": rid(), "dry_run": True}, caller="onboarding").status_code == 403
    assert h.post(OFFB, {"request_id": rid(), "dry_run": False}, andre=ANDRE_TOKEN).status_code == 422
