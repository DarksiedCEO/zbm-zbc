"""farrah-spec/1 section 1 (registry, plan gate, routes, kill switches) and the common rows CB-1..CB-5, CB-8..CB-12,
CB-20, CB-21, AC-7 for Farrah."""

from __future__ import annotations

import pytest

import config
from agents import farrah
from conformance.gen.farrah_util import ok_run, rid, run, set_plan, simple

SPEC_REGISTRY = {"assignment": "monthly revenue attributed to organic search and AI referrals, as a figure with a band "
                               "and a confidence label",
                 "inputs": ["osei.dataset/1 (analytics, crm) for the month", "osei.reconciliation/1",
                            "osei.panel_freshness/1", "the tenant's plan (config pricing table)"],
                 "outputs": ["farrah.revenue_attribution/1"]}
CALLERS = ("dashboard", "seo_agent", "scheduler", "hub", "finance_31", "compliance_38")


def test_B_FA_01_CB_1_registry_entry_byte_for_byte(fa):
    import json
    import svc_manager
    assert json.dumps(svc_manager.REGISTRY["farrah"], sort_keys=True) == json.dumps(SPEC_REGISTRY, sort_keys=True)
    assert "farrah" in svc_manager.AGENTS and "farrah" not in svc_manager.NOT_BUILT_AGENTS
    dep = fa.ok(fa.get("/department"))
    assert dep["agents"]["farrah"]["registry"] == SPEC_REGISTRY and "farrah" not in dep["not_built_agents"]
    card = dep["agents"]["farrah"]["scorecard"]
    assert card["attribution_runs"] == {"runs": 0, "completed": 0, "interrupted": 0, "outcomes": {}}
    assert card["failure_rate"] is None and card["not_connected_rate"] is None     # a rate over 0 runs is null


def test_CB_1_lifecycle_moves_for_farrah(fa):
    r = fa.post("/agents/farrah/lifecycle", {"request_id": rid(), "to": "watch", "reason": "QUALITY_REVIEW",
                                             "expected_state": "active"}, caller="dashboard")
    assert fa.ok(r)["state"] == "watch"
    simple(fa)
    ok_run(fa)
    card = fa.ok(fa.get("/department"))["agents"]["farrah"]["scorecard"]["attribution_runs"]
    assert card["runs"] == 1 and card["completed"] == 1 and sum(card["outcomes"].values()) == 1


@pytest.mark.parametrize("plan,allowed", [(None, False), ("managed_1", False), ("managed_2", True),
                                          ("managed_3", True)])
def test_B_FA_02_plan_gate_one_test_per_plan(fa, plan, allowed):
    fa.tenant("globex")
    if plan is not None:
        assert set_plan(fa, "globex", plan).status_code == 200
    simple(fa, tid="globex")
    r = run(fa, tid="globex")
    if allowed:
        assert r.status_code == 201, r.text
    else:
        fa.refused(r, 403, "PLAN_EXCLUDES_ATTRIBUTION")
        assert not fa.ledger.of_type("attribution_run_requested")     # no run
    assert farrah.plan_blocked_by("globex", config.PLAN_IDS.get(plan), "zbm") == (None if allowed else "PLAN_EXCLUDES")


def test_B_FA_02_own_tenant_zbm_runs_regardless_of_plan(fa):
    assert set_plan(fa, "zbm", "managed_1").status_code == 200       # Entry, yet ZBM's own tenant (Q-FA-4)
    simple(fa, tid="zbm")
    assert ok_run(fa, tid="zbm")["status"] == "completed"


@pytest.mark.parametrize("caller,andre", [("seo_agent", False), ("hub", False), ("scheduler", False),
                                          ("finance_31", False), ("compliance_38", False), ("dashboard", False)])
def test_B_FA_02_a_hub_or_agent_caller_cannot_change_a_plan(fa, caller, andre):
    headers = fa.headers(caller, andre=andre, tenant="acme" if caller == "hub" else None)
    r = fa.client.put("/seo/v1/tenants/acme/plan", json={"request_id": rid(), "plan": "managed_3",
                                                         "revenue_basis": "crm"}, headers=headers)
    assert r.status_code == 403
    assert fa.svc.tenants["acme"]["plan"] == "managed_2" and fa.svc.tenants["acme"]["revenue_basis"] == "analytics"
    assert len(fa.ledger.of_type("tenant_plan_set")) == 1


@pytest.mark.xfail(strict=True, reason="GF-2 / GO-10: BLOCKED until Osei full lands (its refresh-plan route is not "
                                      "on the base; farrah.plan_blocked_by is the hook it reads, tested above)")
def test_B_FA_02_osei_refresh_plan_lists_blocked_by_plan_excludes(fa):
    set_plan(fa, "acme", "managed_1")
    r = fa.get("/tenants/acme/refresh-plan")
    assert r.status_code == 200
    rows = [x for x in r.json()["items"] if x["producer"] == "farrah"]
    assert rows and rows[0]["blocked_by"] == "PLAN_EXCLUDES"


def _route_calls(fa, fid):
    return [("POST", "/tenants/acme/attribution-runs"), ("GET", "/tenants/acme/attribution-runs"),
            ("GET", f"/tenants/acme/attribution-runs/{fid}")]


@pytest.mark.parametrize("caller", CALLERS + ("hub_other", "none"))
def test_B_FA_03_CB_9_route_by_caller_matrix(fa, caller):
    simple(fa)
    fid = ok_run(fa)["run_id"]
    for method, path in _route_calls(fa, fid):
        name = "hub" if caller == "hub_other" else None if caller == "none" else caller
        tenant = "acme" if caller == "hub" else "globex" if caller == "hub_other" else None
        if caller == "hub_other" and "globex" not in fa.svc.tenants:
            fa.tenant("globex")
        h = fa.headers(name, tenant=tenant)
        if method == "POST":
            r = fa.client.post("/seo/v1" + path, json={"request_id": rid(), "month": "2026-09"}, headers=h)
            want = {"dashboard": 201, "seo_agent": 201, "scheduler": 201}.get(caller, 403)
            assert r.status_code == want, (caller, path, r.status_code)
            continue
        r = fa.client.get("/seo/v1" + path, headers=h)
        if caller in ("dashboard", "seo_agent", "hub"):
            assert r.status_code == 200, (caller, path)
            body = r.json()
            one = body[0] if isinstance(body, list) else body
            if path.endswith(fid):
                assert one["result"]["channels"]["organic_search"]["figure"] is not None   # the figure, band ...
                assert one["labels"] and one["assumptions"]
        elif caller == "compliance_38":
            assert r.status_code == 200
            body = r.json()
            for one in body if isinstance(body, list) else [body]:
                assert "result" not in one and "display" not in one                         # no amounts (Q-FA-5)
                assert one["labels"]["organic_search"] in farrah.LABELS and one["rows"]
        elif caller == "hub_other":
            assert r.status_code in (403, 404)
        else:
            assert r.status_code == 403, (caller, path)                                     # finance_31 nothing


def test_CB_10_AC_7_another_tenants_run_is_the_same_404_as_a_missing_one(fa):
    simple(fa)
    fid = ok_run(fa)["run_id"]
    fa.tenant("globex")
    set_plan(fa, "globex", "managed_3")
    a = fa.get(f"/tenants/globex/attribution-runs/{fid}")
    b = fa.get("/tenants/globex/attribution-runs/seo-att-" + "0" * 40)
    assert a.status_code == b.status_code == 404 and a.content == b.content
    assert fa.ok(fa.get("/tenants/globex/attribution-runs")) == []
    hub = fa.get(f"/tenants/acme/attribution-runs/{fid}", caller="hub", tenant="globex")
    assert hub.status_code == 404


def test_B_FA_05_CB_6_kill_switches_and_capability_attribution(fa):
    simple(fa)
    for sw in ("global", "write", "tenant:acme", "capability:audit", "capability:attribution"):
        assert fa.switch(sw).status_code == 200
        r = run(fa)
        assert r.status_code == 403 and r.json()["detail"].startswith("KILLED"), (sw, r.text)
        assert fa.switch(sw, engaged=False, andre=True).status_code == 200
    assert ok_run(fa)["status"] == "completed"


def test_B_FA_05_CB_11_CB_12_zero_port_calls_zero_spend(fa):
    class Trap:
        def __getattr__(self, name):
            raise AssertionError("a port was touched")
    ports = fa.svc.ports
    saved = dict(vars(ports))
    for k in list(vars(ports)):
        setattr(ports, k, Trap())
    try:
        simple(fa)
        assert ok_run(fa)["outcome"] in ("OK", "PARTIAL")
    finally:
        for k, v in saved.items():
            setattr(ports, k, v)
    assert not fa.ledger.of_type("engine_reservation_recorded") and not fa.ledger.of_type("engine_reserved")


def test_CB_2_restricted_farrah_answers_403(fa):
    simple(fa)
    r = fa.post("/agents/farrah/lifecycle", {"request_id": rid(), "to": "restricted", "reason": "FOUNDER_DECISION",
                                             "expected_state": "active"}, caller="dashboard", andre=True)
    assert fa.ok(r)["state"] == "restricted"
    fa.refused(run(fa), 403, "AGENT_RESTRICTED")
    assert not fa.ledger.of_type("attribution_run_requested")


def test_CB_8_one_running_run_per_tenant_and_replay(fa):
    simple(fa)
    req = rid()
    a = ok_run(fa, request_id=req)
    b = ok_run(fa, request_id=req)                               # a replay answers the first result
    assert a["run_id"] == b["run_id"] and len(fa.ledger.of_type("attribution_run_requested")) == 1
    fa.svc.running_attribution.add(a["run_id"])
    fa.svc.attribution_runs[a["run_id"]]["status"] = "running"
    fa.store.tenant("acme")["freshness"]["panels"][0]["inputs"][0]["state"] = "DUE"   # inputs changed
    fa.refused(run(fa), 409, "RUN_IN_PROGRESS")


def test_CB_3_CB_4_envelope_and_no_decisions(fa):
    simple(fa)
    v = ok_run(fa)
    assert v["outcome"] in ("OK", "PARTIAL", "NOT_CONNECTED", "BLOCKED", "FAILED", "KILLED", "QUARANTINED",
                            "INSUFFICIENT_EVIDENCE", "RESTRICTED")
    assert v["findings"] == [] and isinstance(v["methodology"], list) and isinstance(v["limitations"], list)
    assert "decision" not in str(v)
    fa.svc.osei_source = None                                     # production: Osei not on the base
    v2 = ok_run(fa, month="2026-08")
    assert v2["outcome"] == "NOT_CONNECTED" and v2["not_connected"] is True and v2["findings"] == []
    assert all(c["figure"] is None for c in v2["result"]["channels"].values())


def test_CB_20_CB_21_refresh_tier_and_evidence_ref(fa):
    simple(fa)
    v = ok_run(fa)
    for o in (v, v["result"]):
        assert o["refresh_tier"] == "monthly" and o["produced_at"] and o["evidence_ref"]["run_id"] == v["run_id"]
    ref = v["result"]["evidence_ref"]
    got = fa.ok(fa.get(f"/tenants/acme/attribution-runs/{ref['run_id']}"))
    assert got["result"]["inputs"] == v["inputs"]                  # resolves for the owner, to its input rows' ids
    fa.tenant("globex")
    assert fa.get(f"/tenants/globex/attribution-runs/{ref['run_id']}").status_code == 404
