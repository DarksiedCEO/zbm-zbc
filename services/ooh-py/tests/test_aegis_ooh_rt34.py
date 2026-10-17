"""AEGIS red team on OOH-3/4 (at 138dcc8): the findings still open after 70e6962, and the reachable checks its
mutation run showed untested. Each test fails if its check is removed."""

from __future__ import annotations

import pytest

from helpers import at, rid


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


def rate_card(h, fid, amount="2800.00", period="28", source=None, which=("rate_card_amount", "rate_card_period_days")):
    sid = source or h.source("lamar")
    for name, value in (("rate_card_amount", amount), ("rate_card_period_days", period)):
        if name not in which:
            continue
        f = h.ok(h.get(f"/faces/{fid}"))
        h.ok(h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                                 "attribute": name, "value": value, "source_id": sid}))
    return sid


def estimate(h, cid, uid):
    return h.post(f"/tenants/acme/campaigns/{cid}/inventory/{uid}/rate-card-estimates",
                  {"request_id": rid(), "method": "RATE_CARD_PRORATED_DAILY"})


# ---------------------------------------------------------------------------------------------- F5a (HIGH: untested)

def test_f5a_the_gate_compares_the_contracts_cancellation_terms(acme):
    """Approve a contract, change the commercial model's cancellation terms while that is still allowed, then try to
    buy under the old contract."""
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    uid, qid = h.held_unit("acme", cid)
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    ctx = {"tid": "acme", "cid": cid, "uid": uid, "qid": qid, "amount": "5000.00"}
    h.svc.campaigns[cid]["state"] = "QUOTES_VALIDATED"       # a contract may be drafted from QUOTES_VALIDATED
    kid = h.contract(ctx)
    h.svc.campaigns[cid]["state"] = "AVAILS_REQUESTED"
    h.ok(h.commercial("acme", cid, "20.00", cancellation_terms_ref="lamar-cxl-14d"))
    a = {"contract_id": kid, "vendor": "lamar", "exact_quote_refs": [qid], "authorized_amount": "5000.00",
         "authorized_currency": "USD", "cancellation_terms_ref": "lamar-cxl-14d"}
    assert h.svc.contract_gate_problem(h.svc.campaigns[cid], a) == "CONTRACT_MISMATCH"


def test_f5a_a_contract_is_read_only_through_its_own_campaign(acme):
    h = acme
    h.tenant("globex")
    a = h.funded("acme")
    g = h.campaign("globex")
    h.refused(h.get(f"/tenants/globex/campaigns/{g}/contracts/{a['contract_id']}"), 404, "CONTRACT_NOT_FOUND")
    other = h.campaign("acme")
    h.refused(h.get(f"/tenants/acme/campaigns/{other}/contracts/{a['contract_id']}"), 404, "CONTRACT_NOT_FOUND")
    r = h.get(f"/tenants/globex/campaigns/{a['cid']}/contracts/{a['contract_id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------------------------- F4, F10 (PII)

@pytest.mark.parametrize("text", ["310\x00555\x001234", "310–555–1234", "310 555 1234"])
def test_f4_control_and_dash_separators_do_not_hide_a_phone(acme, text):
    h = acme
    sid = h.source("lamar")
    fid = h.ok(h.post("/faces", {"request_id": rid(), "source_id": sid, "vendor_face_ref": f"pf-{abs(hash(text)) % 9999}",
                                 "format": "STATIC_BILLBOARD", "market": "los-angeles"}), 201)["face_id"]
    f = h.ok(h.get(f"/faces/{fid}"))
    r = h.post(f"/faces/{fid}/attributes", {"request_id": rid(), "expected_version": f["version"],
                                            "attribute": "location_description", "value": text, "source_id": sid})
    assert r.status_code == 422


def test_f10_a_reference_attribute_holding_a_phone_number_is_refused(acme):
    h = acme
    v = h.ok(h.get("/vendors/lamar"))
    r = h.post("/vendors/lamar/attributes", {"request_id": rid(), "expected_version": v["version"],
                                             "attribute": "cancellation_policy_ref", "value": "tel:13105551234",
                                             "source_id": h.source("lamar")})
    h.refused(r, 422, "ATTRIBUTE_VALUE_PERSONAL_DATA")


# ---------------------------------------------------------------------------------------------- F6 (rate cards)

def test_f6_amount_and_period_must_come_from_one_rate_card(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="rc-mix")
    fid = h.svc.inventory[uid]["face_id"]
    rate_card(h, fid, amount="12000.00", which=("rate_card_amount",))
    rate_card(h, fid, period="4", which=("rate_card_period_days",))        # another card's period
    h.refused(estimate(h, cid, uid), 409, "RATE_CARD_UNKNOWN")


def test_f6_an_estimate_from_a_replaced_rate_card_is_stale_in_the_plan(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="rc-old")
    fid = h.svc.inventory[uid]["face_id"]
    rate_card(h, fid, source=h.source("lamar", dated=at(days=-10)))
    h.ok(estimate(h, cid, uid), 201)
    line = lambda: h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["lines"][0]["cost"]  # noqa: E731
    assert line()["basis"] == "VENDOR_COST_ESTIMATE"
    rate_card(h, fid, amount="4000.00", source=h.source("lamar", dated=at(days=-1)))  # a newer card
    assert line()["basis"] == "UNKNOWN"                                       # re-estimate from the current card
    h.ok(estimate(h, cid, uid), 201)
    assert line()["amount"] == "4285.71"                                      # 4000 x 30 / 28


# ---------------------------------------------------------------------------------------------- F8 (avails)

def test_f8_an_unavailable_answer_after_the_hold_request_blocks_andres_confirmation(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    uid = h.unit("acme", cid, ref="av-late")
    for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
        h.ok(h.move("acme", cid, uid, to))
    h.quote("acme", cid, [uid])
    for to in ("QUOTED", "HOLD_REQUESTED"):
        h.ok(h.move("acme", cid, uid, to))
    h.ok(h.hold_evidence("acme", cid, uid), 201)
    fid = h.svc.inventory[uid]["face_id"]
    h.ok(h.post(f"/faces/{fid}/avails", {"request_id": rid(), "source_id": h.source("lamar", kind="VENDOR_QUOTE"),
                                         "flight_start": "2026-11-01", "flight_end": "2026-11-30",
                                         "answer": "UNAVAILABLE"}), 201)
    h.refused(h.hold_decide("acme", cid, uid), 409, "AVAILS_UNAVAILABLE")


# ---------------------------------------------------------------------------------------------- F5b (mutation gaps)

@pytest.mark.parametrize("route", ["brief", "estimate", "markup", "contract", "approve", "finance"])
def test_f5b_kill_switches_stop_every_new_write(acme, route):
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, ctx["sell"]))
    kid = None
    if route == "approve":
        kid = h.contract(ctx, approve=False)
    h.ok(h.switch("tenant:acme"))
    base = f"/tenants/acme/campaigns/{ctx['cid']}"
    v = h.view("acme", ctx["cid"])
    calls = {
        "brief": lambda: h.post(base + "/brief", {"request_id": rid(), "expected_version": v["version"],
                                                  "budget_cap": "1.00", "currency": "USD",
                                                  "formats": ["STATIC_BILLBOARD"], "flight_window_start": "2026-11-01",
                                                  "flight_window_end": "2026-11-30", "brief_doc_ref": "b"}),
        "estimate": lambda: estimate(h, ctx["cid"], ctx["uid"]),
        "markup": lambda: h.post(base + "/markup-evidence", {"request_id": rid(), "kind": "COST_TO_SERVE",
                                                             "doc_ref": "m"}),
        "contract": lambda: h.post(base + "/contracts", {"request_id": rid(), "vendor": "lamar",
                                                         "quote_refs": [ctx["qid"]], "total": "5000.00",
                                                         "currency": "USD", "io_doc_ref": "io",
                                                         "cancellation_terms_ref": "lamar-cxl-30d",
                                                         "payment_terms_days": 30, "vendor_payment_method": "ach"}),
        "approve": lambda: h.post(f"{base}/contracts/{kid}/approve", {"request_id": rid(),
                                                                      "expected_version": v["version"]}, andre=True),
        "finance": lambda: h.post(base + "/finance-verifications",
                                  {"request_id": rid(), "approval_id": v["client_approval"]["approval_id"],
                                   "verified_amount": "6000.00", "currency": "USD", "finance_ref": "fin-k",
                                   "cleared": True}, caller="finance_31"),
    }
    h.refused(calls[route](), 403, "KILLED_TENANT")


def test_f5b_a_cancellation_voids_open_contracts(acme):
    h = acme
    ctx = h.funded()
    h.ok(h.go("acme", ctx["cid"], "CANCELLED", andre=True))
    k = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/contracts/{ctx['contract_id']}"))
    assert k["status"] == "VOID" and k["void_reason"] == "CAMPAIGN_CANCELLED"


def test_f5b_supersession_voids_only_the_superseded_contracts_authorizations(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    u1, q1 = h.held_unit("acme", cid, amount="2000.00")
    u2, q2 = h.held_unit("acme", cid, amount="3000.00")
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    sp = h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[q1, q2]), 201)
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.go("acme", cid, "PROPOSAL_SENT", andre=True))
    ctx = {"tid": "acme", "cid": cid, "uid": u1, "qid": q1, "sell_id": sp["price_id"], "sell": "6000.00",
           "amount": "2000.00"}
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, "6000.00"))
    c = h.view("acme", cid)
    ctx.update(approval_id=c["client_approval"]["approval_id"], terms_id=c["commercial"]["terms_id"])
    h.finance_verify(ctx)
    k1 = h.contract(ctx, quotes=[q1], total="2000.00")
    a1 = h.ok(h.authorize(ctx, contract_id=k1), 201)
    h.contract(ctx, quotes=[q2], total="3000.00")             # shares no quote with k1: supersedes nothing
    assert h.svc.contracts[k1]["status"] == "TERMS_APPROVED_BY_ANDRE"
    assert h.svc.authorizations[a1["authorization_id"]]["status"] == "AUTHORIZED"


def test_f5b_a_draft_refuses_another_campaigns_quote_and_an_estimate_as_a_quote(acme):
    h = acme
    a = h.funded()
    b = h.to_proposal_sent()
    h.ok(h.approve(b))
    h.ok(h.funds(b, b["sell"]))
    body = {"request_id": rid(), "vendor": "lamar", "quote_refs": [a["qid"]], "total": "5000.00", "currency": "USD",
            "io_doc_ref": "io", "cancellation_terms_ref": "lamar-cxl-30d", "payment_terms_days": 30,
            "vendor_payment_method": "ach"}
    h.refused(h.post(f"/tenants/acme/campaigns/{b['cid']}/contracts", body), 409, "QUOTE_NOT_FOUND")
    est = h.ok(h.price("acme", b["cid"], "VENDOR_COST_ESTIMATE", "5000.00", vendor="lamar",
                       inventory_ids=[b["uid"]]), 201)["price_id"]
    h.refused(h.post(f"/tenants/acme/campaigns/{b['cid']}/contracts", {**body, "request_id": rid(),
                                                                       "quote_refs": [est]}), 409, "QUOTE_NOT_FOUND")


def test_f5b_the_plan_ignores_expired_and_retired_quotes(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, ref="pq-1")
    for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
        h.ok(h.move("acme", cid, uid, to))
    h.quote("acme", cid, [uid], "4000.00", valid_until=at(hours=2))
    cost = lambda: h.ok(h.get(f"/tenants/acme/campaigns/{cid}/plan"))["lines"][0]["cost"]  # noqa: E731
    assert cost()["basis"] == "VENDOR_QUOTE"
    h.clock.advance(hours=3)
    assert cost()["basis"] == "UNKNOWN"                       # an expired quote is not a cost basis
    h.quote("acme", cid, [uid], "4100.00")
    assert cost()["amount"] == "4100.00"
    u = h.unit_get("acme", cid, uid)
    h.ok(h.post(f"/tenants/acme/campaigns/{cid}/inventory/{uid}/flight",
                {"request_id": rid(), "expected_version": u["version"], "flight_start": "2026-12-01",
                 "flight_end": "2026-12-31"}))                 # retires the quote
    assert cost()["basis"] == "UNKNOWN"


def test_f5b_the_snapshot_holds_contracts_and_finance_records(acme):
    h = acme
    h.funded()
    snap = h.svc.snapshot()
    assert snap["contracts"] and snap["finance_refs"] and snap["pa_determination"]


@pytest.mark.parametrize("caller,tenant", [("compliance_38", None), ("hub", "acme"), ("finance_31", None),
                                           ("scheduler", None)])
def test_f5b_only_preparers_write_planning_and_contracts(acme, caller, tenant):
    h = acme
    ctx = h.funded()
    base = f"/tenants/acme/campaigns/{ctx['cid']}"
    v = h.view("acme", ctx["cid"])
    bodies = {"/brief": {"request_id": rid(), "expected_version": v["version"], "budget_cap": "1.00", "currency": "USD",
                         "formats": ["STATIC_BILLBOARD"], "flight_window_start": "2026-11-01",
                         "flight_window_end": "2026-11-30", "brief_doc_ref": "b"},
              "/markup-evidence": {"request_id": rid(), "kind": "COST_TO_SERVE", "doc_ref": "m"},
              "/contracts": {"request_id": rid(), "vendor": "lamar", "quote_refs": [ctx["qid"]], "total": "5000.00",
                             "currency": "USD", "io_doc_ref": "io", "cancellation_terms_ref": "lamar-cxl-30d",
                             "payment_terms_days": 30, "vendor_payment_method": "ach"},
              f"/inventory/{ctx['uid']}/rate-card-estimates": {"request_id": rid(),
                                                               "method": "RATE_CARD_PRORATED_DAILY"}}
    for path, body in bodies.items():
        r = h.post(base + path, body, caller=caller, tenant=tenant)
        assert r.status_code == 403, (path, caller, r.text)
