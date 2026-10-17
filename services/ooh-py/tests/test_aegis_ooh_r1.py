"""AEGIS round 1 on OOH waves 0-1 (Oct 16 2026): each finding's fix, proven by a test that fails on the code AEGIS
reviewed (5ad0c62). H1 one transfer funding two campaigns; H2 one unit bought twice in one order; M1 one face as two
unit records; M2 an outcome promoting a disputed campaign; M3 a 0.00 funds record and Andre's re-confirmation step;
M4 a rejected order leaving a confirmed hold; L1 the personal-data refusal's own error type; L2 ``maybe`` on 503;
L5 a proposal sent with no current sell price."""

from __future__ import annotations

import pytest

from helpers import rid
from svc_campaigns import funds_ref_key, unit_ref_key

# war room seeds (devtools/warroom/scenarios/ooh-py.json): each is the same transfer as "boa-ach-12345"
REF_VARIANTS = ("BOA-ACH-12345", "boa.ach.12345", "ach:boa-12345", "BOA_ACH_12345", "boa-12345", "Boa-Ach-12345-ref",
                "EFT:BOA-12345", "boa-ach-0012345")
# war room seeds: each is the same face as vendor "lamar", ref "face-101"
UNIT_REF_VARIANTS = ("FACE-101", "face_101", "Face.101", "face:101", "FACE101", "f-a-c-e-1-0-1")
# war room seeds: no funds record may move nothing
ZERO_AMOUNTS = ("0.00",)


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


# ---------------------------------------------------------------------------------------------- H1

@pytest.mark.parametrize("first,second,method", [("BOA-ACH-12345", "boa-ach-12345", "ach"), ("12345", "ach:12345", "ach"),
                                                 ("ACH12345", "12345", "ach"), ("boa.ach.12345", "BOA_ACH_12345", "ach"),
                                                 ("eft-778", "EFT778", "ach")])
def test_h1_one_transfer_funds_one_campaign_whatever_its_spelling(acme, first, second, method):
    h = acme
    a = h.to_proposal_sent()
    h.ok(h.approve(a))
    h.ok(h.funds(a, a["sell"], ref=first, method=method))
    b = h.to_proposal_sent()
    h.ok(h.approve(b))
    h.refused(h.funds(b, b["sell"], ref=second, method=method), 409, "FUNDS_EVIDENCE_REUSED")
    assert h.view("acme", b["cid"])["state"] == "CLIENT_APPROVED"
    assert funds_ref_key(first, method) == funds_ref_key(second, method)


@pytest.mark.parametrize("variant", REF_VARIANTS)
def test_h1_the_war_room_reference_variants_are_one_transfer(acme, variant):
    h = acme
    a = h.to_proposal_sent()
    h.ok(h.approve(a))
    h.ok(h.funds(a, a["sell"], ref="boa-ach-12345"))
    assert h.view("acme", a["cid"])["funds"][0]["evidence_key"] == "ach:boa12345"
    b = h.to_proposal_sent()
    h.ok(h.approve(b))
    h.refused(h.funds(b, "100.00", ref=variant), 409, "FUNDS_EVIDENCE_REUSED")


def test_h1_the_key_survives_restart(hd):
    h = hd
    h.tenant("acme")
    a = h.to_proposal_sent()
    h.ok(h.approve(a))
    h.ok(h.funds(a, a["sell"], ref="BOA-ACH-777"))
    h2 = h.restart()
    b = h2.to_proposal_sent()
    h2.ok(h2.approve(b))
    h2.refused(h2.funds(b, b["sell"], ref="ach:boa-777"), 409, "FUNDS_EVIDENCE_REUSED")


def test_h1_a_reference_that_is_only_method_words_is_refused(acme):
    h = acme
    a = h.to_proposal_sent()
    h.ok(h.approve(a))
    h.refused(h.funds(a, a["sell"], ref="ach-wire"), 422, "INVALID")


# ---------------------------------------------------------------------------------------------- H2

def test_h2_one_unit_in_two_quotes_is_never_priced_or_bought_twice(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    uid, q1 = h.held_unit("acme", cid, amount="2500.00")
    q2 = h.quote("acme", cid, [uid], "2500.00")
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    h.refused(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[q1, q2]), 409,
              "UNIT_IN_MULTIPLE_QUOTES")
    sp = h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "3000.00", andre=True, quote_refs=[q1]), 201)
    h.ok(h.price("acme", cid, "APPROVED_BUY_LIMIT", "5000.00", andre=True), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.go("acme", cid, "PROPOSAL_SENT", andre=True))
    ctx = {"tid": "acme", "cid": cid, "uid": uid, "qid": q1, "sell_id": sp["price_id"], "sell": "3000.00",
           "amount": "2500.00"}
    h.ok(h.approve(ctx))
    h.ok(h.funds(ctx, "6000.00"))
    v = h.view("acme", cid)
    ctx.update(approval_id=v["client_approval"]["approval_id"], terms_id=v["commercial"]["terms_id"])
    h.refused(h.authorize(ctx, exact_quote_refs=[q1, q2], authorized_amount="5000.00"), 409,
              "UNIT_IN_MULTIPLE_QUOTES")
    assert h.view("acme", cid)["authorization_ids"] == []


# ---------------------------------------------------------------------------------------------- M1

@pytest.mark.parametrize("variant", UNIT_REF_VARIANTS)
def test_m1_one_face_is_one_live_unit_record_across_campaigns(acme, variant):
    h = acme
    a, b = h.campaign(), h.campaign()
    body = {"request_id": rid(), "face_id": h.face("lamar", "face-101")}
    h.ok(h.post(f"/tenants/acme/campaigns/{a}/inventory", body), 201)
    # OOH-2: a respelt reference is refused at the registry (one face, one record) and finds the same face...
    r = h.post("/faces", {"request_id": rid(), "source_id": h.source("lamar"), "vendor_face_ref": variant,
                          "format": "STATIC_BILLBOARD", "market": "los-angeles"})
    h.refused(r, 409, "FACE_EXISTS")
    assert h.face("lamar", variant) == body["face_id"]
    # ...and that face is one live unit record across campaigns
    for cid in (a, b):
        h.refused(h.post(f"/tenants/acme/campaigns/{cid}/inventory", {**body, "request_id": rid(),
                                                                      "face_id": h.face("lamar", variant)}),
                  409, "UNIT_ALREADY_REGISTERED")
    # another vendor's face with the same reference is another face
    h.ok(h.post(f"/tenants/acme/campaigns/{b}/inventory", {**body, "request_id": rid(),
                                                            "face_id": h.face("outfront", "face-101")}), 201)
    assert sum(1 for u in h.svc.inventory.values() if u["vendor"] == "lamar") == 1
    assert unit_ref_key(variant) == unit_ref_key("face-101") == "face101"


def test_m1_a_withdrawn_or_cancelled_record_frees_the_face_but_an_open_order_still_blocks_it(acme):
    h = acme
    a = h.campaign()
    uid = h.unit("acme", a)
    h.ok(h.move("acme", a, uid, "WITHDRAWN"))
    ref = h.svc.inventory[uid]["vendor_unit_ref"]
    b = h.campaign()
    body = {"request_id": rid(), "face_id": h.face("lamar", ref)}
    h.ok(h.post(f"/tenants/acme/campaigns/{b}/inventory", body), 201)
    # a face on an open order in one campaign is refused by the gate in any other (defense in depth: the record
    # uniqueness above means a second record can only come from a cancelled campaign)
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    ordered = h.svc.inventory[ctx["uid"]]
    other = {**ordered, "unit_id": "ooh-inv-" + "9" * 40, "campaign_id": b}
    assert h.svc._face_on_open_order(other, None) is True
    assert h.svc._face_on_open_order(other, ctx["aid"]) is False


# ---------------------------------------------------------------------------------------------- M2

@pytest.mark.parametrize("outcome,resume", [("ACCEPTED", "BOOKED"), ("REJECTED", "FUNDS_VERIFIED")])
def test_m2_an_outcome_never_promotes_a_disputed_campaign(acme, outcome, resume):
    h = acme
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    h.ok(h.go("acme", ctx["cid"], "DISPUTED"))                 # the agent runtime may raise a dispute
    a = h.ok(h.outcome(ctx, outcome))
    assert a["submission_state"] == outcome
    c = h.view("acme", ctx["cid"])
    assert c["state"] == "DISPUTED" and c["resume_to"] == resume
    h.refused(h.go("acme", ctx["cid"], resume), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.go("acme", ctx["cid"], "ORDER_SUBMITTED", andre=True), 409, "RESUME_TARGET_INVALID")
    assert h.ok(h.go("acme", ctx["cid"], resume, andre=True))["state"] == resume


# ---------------------------------------------------------------------------------------------- M3

@pytest.mark.parametrize("amount", ZERO_AMOUNTS)
def test_m3_a_funds_record_of_nothing_is_refused(acme, amount):
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.refused(h.funds(ctx, amount), 422, "INVALID")
    assert h.view("acme", ctx["cid"])["funds"] == []


def _back_to_proposal_sent(h, ctx):
    h.ok(h.go("acme", ctx["cid"], "CHANGE_REQUESTED", andre=True))
    h.ok(h.go("acme", ctx["cid"], "PLAN_DRAFTED"))
    h.ok(h.go("acme", ctx["cid"], "PLAN_APPROVED", andre=True))
    for to in ("AVAILS_REQUESTED", "QUOTES_VALIDATED"):
        h.ok(h.go("acme", ctx["cid"], to))
    h.ok(h.go("acme", ctx["cid"], "PROPOSAL_SENT", andre=True))


def _reconfirm(h, ctx, amount, **over):
    c = h.view("acme", ctx["cid"])
    return h.post(f"/tenants/acme/campaigns/{ctx['cid']}/funds-reconfirmations",
                  {"request_id": rid(), "expected_version": c["version"], "approved_amount": amount,
                   "currency": "USD", **over}, andre=True)


def test_m3_andre_reconfirms_cleared_funds_after_a_change_without_a_new_reference(acme):
    h = acme
    ctx = h.funded()
    _back_to_proposal_sent(h, ctx)
    h.ok(h.approve(ctx))
    h.refused(_reconfirm(h, ctx, "5999.00"), 409, "CLIENT_APPROVAL_MISMATCH")
    r = h.post(f"/tenants/acme/campaigns/{ctx['cid']}/funds-reconfirmations",
               {"request_id": rid(), "expected_version": 1, "approved_amount": ctx["sell"], "currency": "USD"},
               caller="ooh_agent")
    assert r.status_code == 403
    c = h.ok(_reconfirm(h, ctx, ctx["sell"]))
    assert c["state"] == "FUNDS_VERIFIED" and len(c["funds"]) == 1          # no new money, no new reference
    assert c["history"][-1]["reason"] == "CLEARED_FUNDS_RECONFIRMED"


def test_m3_reconfirmation_never_covers_a_higher_approved_price(acme):
    h = acme
    ctx = h.funded()
    h.ok(h.go("acme", ctx["cid"], "CHANGE_REQUESTED", andre=True))
    h.ok(h.go("acme", ctx["cid"], "PLAN_DRAFTED"))
    h.ok(h.go("acme", ctx["cid"], "PLAN_APPROVED", andre=True))
    h.ok(h.commercial("acme", ctx["cid"], "30.00"))
    sp = h.ok(h.price("acme", ctx["cid"], "CLIENT_SELL_PRICE", "6500.00", andre=True, quote_refs=[ctx["qid"]]), 201)
    ctx.update(sell_id=sp["price_id"], sell="6500.00")
    for to in ("AVAILS_REQUESTED", "QUOTES_VALIDATED"):
        h.ok(h.go("acme", ctx["cid"], to))
    h.ok(h.go("acme", ctx["cid"], "PROPOSAL_SENT", andre=True))
    h.ok(h.approve(ctx))
    h.refused(_reconfirm(h, ctx, "6500.00"), 409, "FUNDS_INSUFFICIENT")
    assert h.ok(h.funds(ctx, "500.00"))["state"] == "FUNDS_VERIFIED"


# ---------------------------------------------------------------------------------------------- M4

def test_m4_a_rejected_order_leaves_no_confirmed_hold_and_the_quote_is_not_bought_again_at_once(acme):
    h = acme
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    h.ok(h.outcome(ctx, "REJECTED"))
    u = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{ctx['uid']}"))
    assert u["state"] == "QUOTED" and u["vendor_ref"] is None and u["hold_expires_at"] is None
    h.refused(h.authorize(ctx), 409, "INVENTORY_NOT_HELD")


# ---------------------------------------------------------------------------------------------- L1, L2, L5

@pytest.mark.parametrize("key", ["routing_number", "account_number", "card_number", "phone", "client_ip"])
def test_l1_personal_data_is_refused_as_such_not_as_an_unknown_field(acme, key):
    h = acme
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    for r in (h.funds(ctx, ctx["sell"], **{key: "021000021"}),
              h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "x y", "markets": ["los-angeles"],
                                                 "meta": {key: "021000021"}})):
        body = r.json()
        assert r.status_code == 422 and "021000021" not in r.text
        assert [e["type"] for e in body["detail"]] == ["forbidden_field"], body
        assert body["detail"][0]["loc"][-1].endswith(key)


def test_l2_a_503_says_whether_the_call_may_have_taken_effect(acme):
    h = acme
    ctx = h.authorized()
    h.ledger.lose_anchors = 2
    r = h.submit(ctx)
    assert r.status_code == 503 and r.json() == {"detail": "LEDGER_UNAVAILABLE", "maybe": True}
    h.ok(h.post("/jobs/integrity/run", {"request_id": rid()}, caller="scheduler"))
    h.ledger.fail = True
    r = h.post("/tenants/acme/campaigns", {"request_id": rid(), "name": "x y", "markets": ["los-angeles"]})
    assert r.status_code == 503 and r.json() == {"detail": "LEDGER_UNAVAILABLE", "maybe": False}


def test_l5_no_proposal_goes_out_without_a_current_sell_price(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    uid, qid = h.held_unit("acme", cid)
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[qid]), 201)
    h.ok(h.go("acme", cid, "QUOTES_VALIDATED"))
    h.ok(h.commercial("acme", cid, "25.00"))                  # a reset at QUOTES_VALIDATED unseats the price
    r = h.refused(h.go("acme", cid, "PROPOSAL_SENT", andre=True), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "NO_CURRENT_SELL_PRICE"
