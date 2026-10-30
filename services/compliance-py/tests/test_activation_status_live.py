"""AEGIS compliance-gate review (REVISE @03421e3): the activation status re-evaluates on every call.

F-1: a change to any rule the ruling relied on (its content, not only its status) and the CURRENT service area
     (HR-05 operate, HR-07 refuse) applied to the client's recorded jurisdiction and campaign targets.
F-2: an open hold on the client refuses; a released (releasable) hold no longer does.
F-3: an activation rule the register gained since the ruling refuses; a re-verification of a rule it relied on
     does not.
F-7: an OFAC record counts only for the lane it was recorded for.
F-9: only the departments that act on the answer read it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import helpers as H
from helpers import ANDRE_TOKEN, rid
from test_service_area_gate import activate, attest, harness, status, web_client


def _row(h, oid):
    return dict(h.svc.current.by_id()[oid])


def _amend(h, oid, **params):
    row = _row(h, oid)
    new = {**row, "verified_at": h.clock.today().isoformat(), "parameters": {**row["parameters"], **params}}
    p = h.propose({"kind": "amend", "target_id": oid, "proposed_row": new})
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"], acknowledge_weakening=p.json()["proposal"]["weakening"])


# --- F-1: the service area in force now, and the content of every rule relied on ---------------------------------------

def test_a_client_removed_from_the_service_area_is_refused_at_once():
    h = harness()
    assert activate(h, "client-tx", region="US-TX")["allowed"] is True
    assert status(h, "client-tx")["decision"] == "ALLOW"
    hr05 = _row(h, "HR-05")["parameters"]["operate"]
    _amend(h, "HR-05", operate=[c for c in hr05 if c != "US-TX"])
    s = status(h, "client-tx")
    assert s["decision"] == "REFUSE" and s["current"] is False
    assert any(x.startswith("jurisdiction US-TX is refuse") for x in s["reasons"]), s["reasons"]
    assert any(x.startswith("HR-05 changed since the ruling") for x in s["reasons"]), s["reasons"]


def test_a_client_in_a_newly_refused_region_is_refused_at_once():
    h = harness()
    assert activate(h, "client-fl", region="US-FL")["allowed"] is True
    _amend(h, "HR-07", refuse=[*_row(h, "HR-07")["parameters"]["refuse"], "US-FL"])
    s = status(h, "client-fl")
    assert s["decision"] == "REFUSE"
    assert any(x.startswith("jurisdiction US-FL is refuse") for x in s["reasons"]), s["reasons"]
    assert any(x.startswith("HR-07 changed since the ruling") for x in s["reasons"]), s["reasons"]
    # a client elsewhere relied on HR-07 too: its content changed, so it re-activates (and is allowed again)
    assert activate(h, "client-ca2")["allowed"] is True
    assert status(h, "client-ca2")["decision"] == "ALLOW"


def test_a_campaign_target_removed_from_the_service_area_refuses():
    h = harness()
    assert activate(h, "client-pr", country="PR", region=None, targets=("PR",))["allowed"] is True
    _amend(h, "HR-05", operate=[c for c in _row(h, "HR-05")["parameters"]["operate"] if c not in ("PR", "US-PR")])
    s = status(h, "client-pr")
    assert s["decision"] == "REFUSE" and any("target PR" in x for x in s["reasons"]), s["reasons"]


def test_the_live_area_check_holds_even_if_the_fingerprints_are_lost():
    """(b) does not depend on (a): with no recorded fingerprints, the status still re-classifies the jurisdiction."""
    h = harness()
    activate(h, "client-tx", region="US-TX")
    r = h.svc.rulings[h.svc.latest_activation[("client", "client-tx")]]
    r.pop("rule_fingerprints", None)
    s = status(h, "client-tx")
    assert s["decision"] == "REFUSE" and any("fingerprint" in x for x in s["reasons"]), s["reasons"]
    r["rule_fingerprints"] = {}
    _amend(h, "HR-05", operate=[c for c in _row(h, "HR-05")["parameters"]["operate"] if c != "US-TX"])
    assert any(x.startswith("jurisdiction US-TX is refuse") for x in status(h, "client-tx")["reasons"])


def test_the_ruling_records_a_fingerprint_of_every_rule_it_relied_on():
    h = harness()
    activate(h, "client-web")
    r = h.svc.rulings[h.svc.latest_activation[("client", "client-web")]]
    assert set(r["rule_fingerprints"]) == set(r["row_ids_evaluated"])
    assert all(len(v) == 64 for v in r["rule_fingerprints"].values())


# --- F-2: holds -------------------------------------------------------------------------------------------------------

def test_an_ofac_hit_after_activation_refuses_the_client():
    h = harness()
    activate(h, "client-web")
    a = attest(h, "client-web", result="potential_match")
    assert a.status_code == 200 and a.json()["hold_id"]
    s = status(h)
    assert s["decision"] == "REFUSE"
    assert f"hold open: sanctions_match ({a.json()['hold_id']})" in s["reasons"], s["reasons"]


def test_an_open_non_sanctions_hold_refuses_and_its_release_allows_again():
    h = harness()
    activate(h, "client-web")
    # a signal mismatch seen on another lane opens a releasable hold on the same subject
    f = web_client(h, "client-web", network_country_signal="CA")
    h.rule("client-web", "zbc_brand", f)
    holds = [x for x in h.svc.holds.values() if x["subject_id"] == "client-web" and x["status"] == "open"]
    assert [x["kind"] for x in holds] == ["jurisdiction_signal_mismatch"]
    s = status(h)
    assert s["decision"] == "REFUSE" and f"hold open: jurisdiction_signal_mismatch ({holds[0]['hold_id']})" in s["reasons"]
    rel = h.post(f"/compliance/v1/holds/{holds[0]['hold_id']}/release",
                 {"request_id": rid("rel"), "reason": "travelling: declared US-CA confirmed by Andre"}, andre=ANDRE_TOKEN)
    assert rel.status_code == 200, rel.text
    assert status(h)["decision"] == "ALLOW", status(h)["reasons"]


def test_a_hold_on_another_subject_does_not_refuse_this_client():
    h = harness()
    activate(h, "client-web")
    attest(h, "client-other", result="match")
    assert status(h)["decision"] == "ALLOW"


# --- F-3: rules gained since the ruling vs a re-verification ------------------------------------------------------------

def test_a_new_activation_rule_refuses_until_re_activation():
    h = harness()
    activate(h, "client-web")
    base = _row(h, "HR-15")
    new = {**base, "id": "HR-16", "title": "Client: one more founder rule", "verified_at": h.clock.today().isoformat(),
           "gates": ["activation"]}
    p = h.propose({"kind": "new", "proposed_row": new})
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"])
    s = status(h)
    assert s["decision"] == "REFUSE"
    assert any(x.startswith("the register gained activation rule(s) HR-16") for x in s["reasons"]), s["reasons"]


def test_a_re_verification_of_a_rule_relied_on_keeps_the_client_allowed():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reverify_proposal", Path(__file__).resolve().parents[1] / "devtools" / "reverify_proposal.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    at = datetime(2026, 12, 20, 15, 0, tzinfo=timezone.utc)
    h = harness(at=at)
    activate(h, "client-web")
    assert status(h)["decision"] == "ALLOW"
    row = h.get("/compliance/v1/register/US-OFAC-04").json()["row"]
    p = h.post("/compliance/v1/register/proposals",
               tool.build(row, b"<p>Comprehensive sanctions: Cuba, Iran, North Korea</p>",
                          "Comprehensive sanctions: Cuba, Iran, North Korea", at), andre=ANDRE_TOKEN)
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"])
    s = status(h)
    assert s["decision"] == "ALLOW", s["reasons"]
    h.clock.at = datetime(2026, 12, 26, 12, 0, tzinfo=timezone.utc)   # past the old expiry: re-verified, still in force
    h.run_controls()
    assert status(h)["decision"] == "ALLOW", status(h)["reasons"]


# --- F-7: the OFAC record is tied to the lane ---------------------------------------------------------------------------

def test_an_ofac_record_for_another_lane_does_not_count():
    h = harness()
    a = attest(h, "client-lane", lane="zbc_brand")
    assert a.status_code == 200, a.text
    f = web_client(h, "client-lane", attest_first=False, sanctions_attestation_id=a.json()["attestation_id"])
    j = h.rule("client-lane", "client", f).json()
    assert j["allowed"] is False and any("lane" in x for x in j["unmet_lines"]), j["unmet_lines"]


# --- F-9: who reads the activation status --------------------------------------------------------------------------------

def test_only_the_departments_that_act_on_it_read_the_activation_status():
    h = harness()
    activate(h, "client-web")
    for caller in ("finance_31", "seo_02", "revenue_recovery", "onboarding"):
        assert status(h, caller=caller)["decision"] == "ALLOW"
    for caller in sorted(set(H.CALLERS) - {"finance_31", "seo_02", "revenue_recovery", "onboarding"}):
        assert h.get("/compliance/v1/clients/client-web/activation", caller=caller).status_code == 403, caller
