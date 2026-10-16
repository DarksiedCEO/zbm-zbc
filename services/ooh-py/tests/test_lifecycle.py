"""The campaign state machine (sections 3-4) and inventory availability (section 15): explicit allowed transitions
only, guards, exception states, gated states reachable only through their own gate, stale callers refused, and no
state ever silently promoted (ADR 0019 decisions 9-12)."""

from __future__ import annotations

import pytest

import policy
from helpers import at, rid


def _post_transition(h, tid, cid, to, state, version, andre=False):
    return h.post(f"/tenants/{tid}/campaigns/{cid}/transitions",
                  {"request_id": rid(), "to": to, "expected_state": state, "expected_version": version,
                   "reason": "TEST_STEP"}, andre=andre)


def test_every_non_listed_transition_from_draft_is_refused(h):
    h.tenant("acme")
    cid = h.campaign()
    for to in policy.ALL_STATES:
        if to in policy.CAMPAIGN_ALLOWED["INTAKE"]:
            continue
        r = _post_transition(h, "acme", cid, to, "INTAKE", 1, andre=True)
        expected = "TRANSITION_GATED" if to in policy.CAMPAIGN_GATED else "TRANSITION_NOT_ALLOWED"
        h.refused(r, 409, expected)
    c = h.view("acme", cid)
    assert c["state"] == "INTAKE" and c["version"] == 1 and len(c["history"]) == 1


@pytest.mark.parametrize("frm,to", [("INTAKE", "AVAILS_REQUESTED"), ("BRIEF_VALIDATED", "PROPOSAL_SENT"),
                                    ("PLAN_DRAFTED", "QUOTES_VALIDATED")])
def test_skipping_a_step_is_refused(h, frm, to):
    h.tenant("acme")
    cid = h.campaign()
    for step in ("BRIEF_VALIDATED", "PLAN_DRAFTED"):
        if h.view("acme", cid)["state"] == frm:
            break
        h.ok(h.go("acme", cid, step))
    h.refused(h.go("acme", cid, to, andre=True), 409, "TRANSITION_NOT_ALLOWED")


@pytest.mark.parametrize("gated", policy.CAMPAIGN_GATED)
def test_gated_states_are_never_reached_through_the_generic_route_even_by_andre(h, gated):
    h.tenant("acme")
    ctx = h.to_proposal_sent()
    h.refused(h.go("acme", ctx["cid"], gated, andre=True), 409, "TRANSITION_GATED")
    assert h.view("acme", ctx["cid"])["state"] == "PROPOSAL_SENT"


def test_stale_state_and_version_are_refused(h):
    h.tenant("acme")
    cid = h.campaign()
    h.ok(h.go("acme", cid, "BRIEF_VALIDATED"))
    h.refused(_post_transition(h, "acme", cid, "PLAN_DRAFTED", "INTAKE", 2), 409, "STATE_STALE")
    h.refused(_post_transition(h, "acme", cid, "PLAN_DRAFTED", "BRIEF_VALIDATED", 1), 409, "VERSION_STALE")


def test_guards(h):
    h.tenant("acme")
    cid = h.campaign()
    h.ok(h.go("acme", cid, "BRIEF_VALIDATED"))
    h.ok(h.go("acme", cid, "PLAN_DRAFTED"))
    h.refused(h.go("acme", cid, "PLAN_APPROVED"), 403, "ANDRE_APPROVAL_REQUIRED")     # the plan is Andre's
    h.ok(h.go("acme", cid, "PLAN_APPROVED", andre=True))
    r = h.refused(h.go("acme", cid, "AVAILS_REQUESTED"), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "NO_INVENTORY"
    h.unit("acme", cid)
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    assert h.refused(h.go("acme", cid, "QUOTES_VALIDATED"), 409)["guard"] == "NO_COMMERCIAL_MODEL"
    h.ok(h.commercial("acme", cid))
    assert h.refused(h.go("acme", cid, "QUOTES_VALIDATED"), 409)["guard"] == "NO_CURRENT_SELL_PRICE"


def test_proposal_sent_and_backing_out_of_approval_are_andres(h):
    h.tenant("acme")
    ctx = h.to_proposal_sent()
    h.ok(h.approve(ctx))
    h.refused(h.go("acme", ctx["cid"], "CHANGE_REQUESTED"), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.go("acme", ctx["cid"], "CANCELLED"), 403, "ANDRE_APPROVAL_REQUIRED")
    c = h.ok(h.go("acme", ctx["cid"], "CHANGE_REQUESTED", andre=True))
    assert c["client_approval"] is None                       # the client approves the changed campaign again
    h.ok(h.go("acme", ctx["cid"], "PLAN_DRAFTED"))


def test_production_and_later_states_are_not_in_this_wave(h):
    h.tenant("acme")
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    h.ok(h.outcome(ctx))
    h.refused(h.go("acme", ctx["cid"], "IN_PRODUCTION", andre=True), 409, "STATE_NOT_BUILT")
    h.refused(h.go("acme", ctx["cid"], "CANCELLED", andre=True), 409, "TRANSITION_NOT_ALLOWED")


def test_on_hold_resumes_only_where_it_was_and_never_restores_an_authorization(h):
    h.tenant("acme")
    ctx = h.authorized()
    c = h.ok(h.go("acme", ctx["cid"], "CLIENT_HOLD"))
    assert c["resume_to"] == "FUNDS_VERIFIED"
    a = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}"))
    assert a["status"] == "VOID" and a["void_reason"] == "CAMPAIGN_CLIENT_HOLD"
    for to in ("PURCHASE_AUTHORIZED", "ORDER_SUBMITTED", "INTAKE", "BOOKED"):
        h.refused(h.go("acme", ctx["cid"], to, andre=True), 409, "RESUME_TARGET_INVALID")
    h.ok(h.go("acme", ctx["cid"], "FUNDS_VERIFIED"))
    h.refused(h.submit(ctx), 409, "AUTHORIZATION_VOID")
    a2 = h.ok(h.authorize(ctx), 201)                          # Andre authorizes again, against the current state
    assert a2["status"] == "AUTHORIZED"


@pytest.mark.parametrize("hold", ["CLIENT_HOLD", "VENDOR_HOLD"])
def test_both_holds_resume_only_where_they_were(h, hold):
    h.tenant("acme")
    ctx = h.to_proposal_sent()
    c = h.ok(h.go("acme", ctx["cid"], hold))
    assert c["resume_to"] == "PROPOSAL_SENT"
    other = "VENDOR_HOLD" if hold == "CLIENT_HOLD" else "CLIENT_HOLD"
    h.refused(h.go("acme", ctx["cid"], other, andre=True), 409, "RESUME_TARGET_INVALID")
    assert h.ok(h.go("acme", ctx["cid"], "PROPOSAL_SENT"))["state"] == "PROPOSAL_SENT"


@pytest.mark.parametrize("state", policy.CAMPAIGN_NOT_BUILT)
def test_not_built_states_exist_but_are_unreachable(h, state):
    assert state in policy.ALL_STATES
    h.tenant("acme")
    cid = h.campaign()
    r = h.go("acme", cid, state, andre=True)
    assert r.status_code == 409 and r.json()["detail"] == "TRANSITION_NOT_ALLOWED"


def test_an_order_in_flight_cannot_be_held_changed_or_cancelled(h):
    h.tenant("acme")
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    for to in ("CLIENT_HOLD", "CHANGE_REQUESTED", "CANCELLED", "FUNDS_VERIFIED"):
        r = h.go("acme", ctx["cid"], to, andre=True)
        assert r.status_code == 409 and r.json()["detail"] in ("TRANSITION_NOT_ALLOWED", "TRANSITION_GATED"), to
    c = h.ok(h.go("acme", ctx["cid"], "DISPUTED"))
    assert c["resume_to"] == "ORDER_SUBMITTED"
    h.refused(h.go("acme", ctx["cid"], "ORDER_SUBMITTED"), 403, "ANDRE_APPROVAL_REQUIRED")
    h.ok(h.go("acme", ctx["cid"], "ORDER_SUBMITTED", andre=True))


def test_history_is_continuous_and_every_change_is_evidenced(h):
    h.tenant("acme")
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    h.ok(h.outcome(ctx))
    c = h.view("acme", ctx["cid"])
    hist = c["history"]
    assert hist[0]["from"] is None and hist[0]["to"] == "INTAKE"
    for prev, nxt in zip(hist, hist[1:]):
        assert nxt["from"] == prev["to"]                      # nothing changed state outside a recorded transition
        assert nxt["to"] in policy.CAMPAIGN_ALLOWED[nxt["from"]] or nxt["from"] in ("CLIENT_HOLD", "DISPUTED")
    assert [x["to"] for x in hist] == ["INTAKE", "BRIEF_VALIDATED", "PLAN_DRAFTED", "PLAN_APPROVED", "AVAILS_REQUESTED", "QUOTES_VALIDATED",
                                       "PROPOSAL_SENT", "CLIENT_APPROVED", "FUNDS_VERIFIED", "PURCHASE_AUTHORIZED",
                                       "ORDER_SUBMITTED", "BOOKED"]
    lines = [r for r in h.svc.log.iter_records() if r["data"].get("campaign_id") == ctx["cid"]]
    assert all(r["data"].get("evidence") for r in lines)      # every line that touched the campaign carries evidence


# ---------------------------------------------------------------------------------------------- availability (s. 15)

def test_availability_cannot_skip_and_booking_is_gated(h):
    h.tenant("acme")
    cid = h.campaign()
    uid = h.unit("acme", cid)
    for to in ("HOLD_CONFIRMED", "QUOTED", "HOLD_REQUESTED", "AVAILABILITY_REQUESTED"):
        h.refused(h.move("acme", cid, uid, to, vendor_ref="v", hold_expires_at=at(days=3)), 409,
                  "AVAILABILITY_TRANSITION_NOT_ALLOWED")
    for to in policy.AVAILABILITY_GATED:
        h.refused(h.move("acme", cid, uid, to), 409, "AVAILABILITY_GATED")
    h.ok(h.move("acme", cid, uid, "VERIFIED"))
    h.ok(h.move("acme", cid, uid, "AVAILABILITY_REQUESTED"))
    r = h.refused(h.move("acme", cid, uid, "QUOTED"), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "NO_VENDOR_QUOTE_FOR_UNIT"           # known is not available; quoted needs a quote


def test_hold_requested_is_not_hold_confirmed(h):
    h.tenant("acme")
    cid = h.campaign()
    uid = h.unit("acme", cid)
    h.ok(h.move("acme", cid, uid, "VERIFIED"))
    h.ok(h.move("acme", cid, uid, "AVAILABILITY_REQUESTED"))
    h.quote("acme", cid, [uid])
    h.ok(h.move("acme", cid, uid, "QUOTED"))
    u = h.ok(h.move("acme", cid, uid, "HOLD_REQUESTED"))      # a hold may be requested without any funds
    assert u["state"] == "HOLD_REQUESTED" and u["vendor_ref"] is None and u["evidence_class"] is None
    h.refused(h.move("acme", cid, uid, "HOLD_CONFIRMED"), 422, "VENDOR_REF_REQUIRED")
    h.refused(h.move("acme", cid, uid, "HOLD_CONFIRMED", vendor_ref="lamar-h1"), 422, "HOLD_EXPIRY_REQUIRED")
    h.refused(h.move("acme", cid, uid, "HOLD_CONFIRMED", vendor_ref="lamar-h1", hold_expires_at=at(hours=-1)), 409,
              "HOLD_EXPIRY_REQUIRED")
    u = h.ok(h.move("acme", cid, uid, "HOLD_CONFIRMED", vendor_ref="lamar-h1", hold_expires_at=at(days=5)))
    assert u["state"] == "HOLD_CONFIRMED" and u["evidence_class"] == "VENDOR_ASSERTED"


def test_availability_stale_caller_is_refused(h):
    h.tenant("acme")
    cid = h.campaign()
    uid = h.unit("acme", cid)
    h.ok(h.move("acme", cid, uid, "VERIFIED"))
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory/{uid}/availability",
               {"request_id": rid(), "to": "AVAILABILITY_REQUESTED", "expected_state": "VERIFIED",
                "expected_version": 1})
    h.refused(r, 409, "VERSION_STALE")


def test_rejected_order_returns_units_and_campaign_without_promotion(h):
    h.tenant("acme")
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    u = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{ctx['uid']}"))
    assert u["state"] == "BOOKING_PENDING"
    a = h.ok(h.outcome(ctx, "REJECTED"))
    assert a["submission_state"] == "REJECTED" and a["status"] == "VOID"
    u = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{ctx['uid']}"))
    assert u["state"] == "QUOTED"                             # AEGIS M4: the failed order's hold is not kept
    c = h.view("acme", ctx["cid"])
    assert c["state"] == "FUNDS_VERIFIED" and c["exposure"]["vendor_commitment"] == "0.00"
