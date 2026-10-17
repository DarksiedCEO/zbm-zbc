"""OOH-3 (ADR 0021): the client's brief, rate-card cost estimates, the plan view and its approval guards, and markup
set from recorded evidence. Nothing is estimated from what nobody sourced."""

from __future__ import annotations

import pytest

from helpers import at, rid


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


def bare_campaign(h):
    """A campaign with no brief yet."""
    return h.ok(h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "No Brief", "markets": ["los-angeles"]}),
                201)["campaign_id"]


def rate_card(h, fid, amount="2800.00", period="28", dated=None):
    for name, value in (("rate_card_amount", amount), ("rate_card_period_days", period)):
        if value is None:
            continue
        f = h.ok(h.get(f"/faces/{fid}"))
        h.ok(h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                                 "attribute": name, "value": value,
                                                 "source_id": h.source("lamar", dated=dated)}))


def estimate(h, cid, uid):
    return h.post(f"/tenants/acme/campaigns/{cid}/inventory/{uid}/rate-card-estimates",
                  {"request_id": rid(), "method": "RATE_CARD_PRORATED_DAILY"})


def drafted(h, **brief):
    cid = bare_campaign(h)
    h.ok(h.brief("acme", cid, **brief))
    for to in ("BRIEF_VALIDATED", "PLAN_DRAFTED"):
        h.ok(h.go("acme", cid, to))
    return cid


# ---------------------------------------------------------------------------------------------- the brief

def test_units_need_a_brief_and_must_fit_it(acme):
    h = acme
    cid = bare_campaign(h)
    fid = h.face("lamar", "b-1")
    path = f"/tenants/acme/campaigns/{cid}/inventory"
    h.refused(h.post(path, {"request_id": rid(), "face_id": fid}), 409, "BRIEF_REQUIRED")
    h.ok(h.brief("acme", cid, formats=["DIGITAL_BILLBOARD"], flight_window_start="2026-11-01",
                 flight_window_end="2026-12-31"))
    h.refused(h.post(path, {"request_id": rid(), "face_id": fid}), 409, "FORMAT_NOT_IN_BRIEF")   # a static face
    dfid = h.face("lamar", "b-2", fmt="DIGITAL_BILLBOARD")
    h.refused(h.post(path, {"request_id": rid(), "face_id": dfid, "flight_start": "2026-10-20",
                            "flight_end": "2026-11-10"}), 409, "FLIGHT_OUTSIDE_BRIEF")
    u = h.ok(h.post(path, {"request_id": rid(), "face_id": dfid, "flight_start": "2026-11-01",
                           "flight_end": "2026-11-30"}), 201)
    h.refused(h.post(f"{path}/{u['unit_id']}/flight", {"request_id": rid(), "expected_version": u["version"],
                                                       "flight_start": "2026-12-15", "flight_end": "2027-01-15"}),
              409, "FLIGHT_OUTSIDE_BRIEF")


def test_the_brief_is_validated_and_locked_once_the_plan_is_approved(acme):
    h = acme
    cid = bare_campaign(h)
    for bad in ({"budget_cap": "0.00"}, {"currency": "EUR"}, {"flight_window_start": "2027-01-01",
                                                               "flight_window_end": "2026-12-01"},
                {"flight_window_start": "2026-01-01", "flight_window_end": "2028-12-31"},
                {"formats": ["STATIC_BILLBOARD", "STATIC_BILLBOARD"]}, {"formats": ["BUS_WRAP"]}, {"formats": []}):
        assert h.brief("acme", cid, **bad).status_code == 422, bad
    c = h.ok(h.brief("acme", cid))
    assert c["brief"]["budget_cap"] == "50000.00" and c["brief"]["set_by"] == "ooh_agent"
    assert h.ledger.of_type("campaign_brief_set")
    h.plan("acme", cid)
    h.refused(h.brief("acme", cid), 409, "BRIEF_LOCKED")
    v = h.view("acme", cid)
    body = {"request_id": rid(), "expected_version": v["version"], "budget_cap": "1.00", "currency": "USD",
            "formats": ["STATIC_BILLBOARD"], "flight_window_start": "2026-11-01", "flight_window_end": "2026-11-30",
            "brief_doc_ref": "hub-brief"}
    for caller, tenant in (("hub", "acme"), ("compliance_38", None), ("scheduler", None)):
        r = h.post(f"/tenants/acme/campaigns/{cid}/brief", {**body, "request_id": rid()}, caller=caller, tenant=tenant)
        assert r.status_code == 403, caller


# ---------------------------------------------------------------------------------------------- estimates

def test_an_estimate_comes_only_from_a_sourced_rate_card(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="e-1")
    fid = h.svc.inventory[uid]["face_id"]
    h.refused(estimate(h, cid, uid), 409, "RATE_CARD_UNKNOWN")                 # nothing sourced: no estimate
    rate_card(h, fid, period=None)
    h.refused(estimate(h, cid, uid), 409, "RATE_CARD_UNKNOWN")                 # half a rate card is no rate card
    rate_card(h, fid, amount=None)
    p = h.ok(estimate(h, cid, uid), 201)
    # 2800.00 per 28 days, a 30-day flight (Nov 1-30): 3000.00
    assert (p["amount"], p["price_type"], p["evidence_class"], p["method"]) == \
        ("3000.00", "VENDOR_COST_ESTIMATE", "ESTIMATED", "RATE_CARD_PRORATED_DAILY")
    assert p["flight_days"] == 30 and p["rate_card_amount"] == "2800.00" and p["rate_card_period_days"] == 28
    assert len(p["rate_card_source_ids"]) == 2
    assert h.ledger.of_type("rate_card_estimate_recorded")


def test_an_estimate_rounds_half_up_and_uses_the_current_rate_card(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="e-2", flight=("2026-11-01", "2026-11-10"))
    fid = h.svc.inventory[uid]["face_id"]
    rate_card(h, fid, amount="1000.00", period="30", dated=at(days=-2))
    rate_card(h, fid, amount="9999.00", period="7", dated=at(days=-60))       # older, recorded later: not current
    p = h.ok(estimate(h, cid, uid), 201)
    assert p["amount"] == "333.33"                                            # 1000 x 10 / 30
    u2 = h.unit("acme", cid, ref="e-2b", flight=("2026-11-01", "2026-11-11"))
    rate_card(h, h.svc.inventory[u2]["face_id"], amount="1000.00", period="30")
    assert h.ok(estimate(h, cid, u2), 201)["amount"] == "366.67"              # 366.666...: half up, not down


def test_an_estimate_needs_a_flight(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="e-3", flight=None)
    rate_card(h, h.svc.inventory[uid]["face_id"])
    h.refused(estimate(h, cid, uid), 409, "FLIGHT_REQUIRED")


# ---------------------------------------------------------------------------------------------- the plan view

def test_each_line_is_quote_else_estimate_else_unknown(acme):
    h = acme
    cid = h.campaign()
    a = h.unit("acme", cid, ref="p-a")
    b = h.unit("acme", cid, ref="p-b")
    p = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))
    assert [ln["cost"]["basis"] for ln in p["lines"]] == ["UNKNOWN", "UNKNOWN"]
    assert p["totals"]["unknown_lines"] == 2 and p["budget_fit"] == "UNKNOWN" and p["approvable"] is False
    rate_card(h, h.svc.inventory[a]["face_id"])
    h.ok(estimate(h, cid, a), 201)
    for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
        h.ok(h.move("acme", cid, b, to))
    h.quote("acme", cid, [b], "4000.00")
    p = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))
    by = {ln["unit_id"]: ln["cost"] for ln in p["lines"]}
    assert by[a]["basis"] == "VENDOR_COST_ESTIMATE" and by[a]["amount"] == "3000.00"
    assert by[b]["basis"] == "VENDOR_QUOTE" and by[b]["amount"] == "4000.00"
    assert p["totals"] == {"quoted": "4000.00", "estimated": "3000.00", "known": "7000.00", "unknown_lines": 0,
                           "currency": "USD"}
    assert p["budget_fit"] == "WITHIN" and p["approvable"] is True
    assert p["markup"] == {"suggested_pct": "15.00", "suggested_applied": False, "deal_pct": None}


def test_the_plan_flags_a_vendor_total_above_the_single_purchase_limit(acme):
    h = acme
    cid = h.campaign()
    for i in range(3):
        uid = h.unit("acme", cid, ref=f"big-{i}")
        for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
            h.ok(h.move("acme", cid, uid, to))
        h.quote("acme", cid, [uid], "4000.00")
    w = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["warnings"]
    assert w == [{"code": "VENDOR_TOTAL_ABOVE_SINGLE_PURCHASE_LIMIT", "vendor": "lamar", "amount": "12000.00",
                  "limit": "10000.00"}]


def test_the_hub_never_reads_the_plan(acme):
    h = acme
    cid = h.campaign()
    assert h.get(f"/tenants/acme/campaigns/{cid}/plan", caller="hub", tenant="acme").status_code == 403
    assert "brief" not in h.ok(h.get(f"/tenants/acme/campaigns/{cid}", caller="hub", tenant="acme"))


# ---------------------------------------------------------------------------------------------- the approval guards

def test_a_plan_with_an_unknown_cost_or_over_budget_is_not_approved(acme):
    h = acme
    cid = drafted(h, budget_cap="5000.00")
    uid = h.unit("acme", cid, ref="g-1")
    r = h.refused(h.go("acme", cid, "PLAN_APPROVED", andre=True), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "PLAN_COST_UNKNOWN"
    rate_card(h, h.svc.inventory[uid]["face_id"], amount="5600.00", period="28")     # 6000.00 for November
    h.ok(estimate(h, cid, uid), 201)
    r = h.refused(h.go("acme", cid, "PLAN_APPROVED", andre=True), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "PLAN_OVER_BUDGET"
    h.ok(h.move("acme", cid, uid, "WITHDRAWN"))                                       # out of the plan
    h.ok(h.go("acme", cid, "PLAN_APPROVED", andre=True))


def test_a_plan_without_a_brief_is_not_approved(acme):
    h = acme
    cid = bare_campaign(h)
    h.svc.campaigns[cid]["brief"] = None
    for to in ("BRIEF_VALIDATED", "PLAN_DRAFTED"):
        h.ok(h.go("acme", cid, to))
    r = h.refused(h.go("acme", cid, "PLAN_APPROVED", andre=True), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "NO_BRIEF"


# ---------------------------------------------------------------------------------------------- markup evidence

def test_the_markup_is_set_from_recorded_evidence_of_this_deal(acme):
    h = acme
    cid = h.campaign()
    other = h.campaign()
    theirs = h.markup_evidence("acme", other)
    h.refused(h.commercial("acme", cid, markup_basis_ref="rate-card-2026"), 409, "MARKUP_BASIS_UNKNOWN")
    h.refused(h.commercial("acme", cid, markup_basis_ref=theirs), 409, "MARKUP_BASIS_UNKNOWN")
    for bad in ("STANDARD_15", "GUESS"):
        r = h.post(f"/tenants/acme/campaigns/{cid}/markup-evidence", {"request_id": rid(), "kind": bad,
                                                                       "doc_ref": "x"})
        assert r.status_code == 422
    mine = h.markup_evidence("acme", cid, kind="COST_TO_SERVE")
    c = h.ok(h.commercial("acme", cid, "18.00", markup_basis_ref=mine))
    assert c["commercial"]["markup_basis_ref"] == mine and c["commercial"]["suggested_markup_applied"] is False
    assert c["markup_evidence"][-1]["kind"] == "COST_TO_SERVE"
    assert h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["markup"]["deal_pct"] == "18.00"


def test_planning_replays_identically(hd):
    h = hd
    h.tenant("acme")
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="rp-1")
    rate_card(h, h.svc.inventory[uid]["face_id"])
    h.ok(estimate(h, cid, uid), 201)
    h.markup_evidence("acme", cid)
    before = h.svc.snapshot()
    plan = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))
    h2 = h.restart()
    assert h2.svc.snapshot() == before
    assert h2.ok(h2.get(f"/tenants/acme/campaigns/{cid}/plan")) == plan



# ---------------------------------------------------------------------------------------------- AEGIS OOH-3/4 source

def test_s34_f1_a_new_brief_must_still_hold_every_live_unit(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="rb-1")                                # static, November 2026
    h.refused(h.brief("acme", cid, formats=["DIGITAL_BILLBOARD"]), 409, "FORMAT_NOT_IN_BRIEF")
    h.refused(h.brief("acme", cid, flight_window_start="2026-12-01", flight_window_end="2027-03-31"), 409,
              "FLIGHT_OUTSIDE_BRIEF")
    h.ok(h.move("acme", cid, uid, "WITHDRAWN"))
    h.ok(h.brief("acme", cid, formats=["DIGITAL_BILLBOARD"]))           # nothing live left outside it


def test_s34_f1_plan_approval_rechecks_every_line_against_the_brief(acme):
    h = acme
    cid = drafted(h)
    uid = h.unit("acme", cid, ref="rb-2")
    rate_card(h, h.svc.inventory[uid]["face_id"])
    h.ok(estimate(h, cid, uid), 201)
    h.svc.campaigns[cid]["brief"]["formats"] = ["DIGITAL_BILLBOARD"]     # tampered in memory (no route can)
    r = h.refused(h.go("acme", cid, "PLAN_APPROVED", andre=True), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "PLAN_UNIT_OUTSIDE_BRIEF"


def test_s34_f5_a_manual_estimate_never_counts_in_the_plan(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="me-1", flight=None)
    h.ok(h.price("acme", cid, "VENDOR_COST_ESTIMATE", "10.00", vendor="lamar", inventory_ids=[uid]), 201)
    line = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["lines"][0]
    assert line["cost"]["basis"] == "UNKNOWN"
