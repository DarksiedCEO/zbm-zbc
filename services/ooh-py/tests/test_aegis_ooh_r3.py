"""AEGIS round 3 on OOH (Oct 16 2026): each finding's fix, proven by a test that fails on the code AEGIS reviewed
(a612790). R3-H1 a flight changed under a quote, a hold and a client approval; the flight lows (a flight already begun,
an unbounded flight); the vendor over-merge override; the rail warning; the two test gaps; the old-log message."""

from __future__ import annotations

import hashlib
import os

import pytest

from helpers import at, rid
from store import GENESIS, LOG_NAME, RecordLog, StoreCorrupt, encode


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


def _flight(h, ctx, start, end, uid=None, caller="ooh_agent"):
    uid = uid or ctx["uid"]
    u = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{uid}"))
    return h.post(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{uid}/flight",
                  {"request_id": rid(), "expected_version": u["version"], "flight_start": start, "flight_end": end},
                  caller=caller)


# ---------------------------------------------------------------------------------------------- R3-H1

def test_r3h1_the_aegis_repro_a_twelve_month_flight_under_a_one_month_quote(acme):
    h = acme
    ctx = h.funded()                                              # quoted, held and approved for 2026-11-01..30
    h.refused(_flight(h, ctx, "2026-11-01", "2027-10-31"), 409, "FLIGHT_LOCKED")
    h.refused(_flight(h, ctx, "2026-11-01", "2027-10-31", caller="dashboard"), 409, "FLIGHT_LOCKED")
    u = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{ctx['uid']}"))
    assert u["flight"] == ["2026-11-01", "2026-11-30"] and u["state"] == "HOLD_CONFIRMED"
    a = h.ok(h.authorize(ctx), 201)
    assert a["unit_flights"] == {ctx["uid"]: ["2026-11-01", "2026-11-30"]}
    ctx["aid"] = a["authorization_id"]
    assert h.ok(h.submit(ctx))["submission_state"] == "SUBMISSION_UNKNOWN"


@pytest.mark.parametrize("state", ["PROPOSAL_SENT", "CLIENT_APPROVED", "FUNDING_REQUIRED"])
def test_r3h1_the_flight_is_locked_from_the_proposal_on(acme, state):
    h = acme
    ctx = h.to_proposal_sent()
    if state != "PROPOSAL_SENT":
        h.ok(h.approve(ctx))
    if state == "FUNDING_REQUIRED":
        h.ok(h.funds(ctx, "100.00"))
    assert h.view("acme", ctx["cid"])["state"] == state
    h.refused(_flight(h, ctx, "2026-11-01", "2026-11-15"), 409, "FLIGHT_LOCKED")


def test_r3h1_a_new_flight_in_planning_resets_the_unit_and_retires_its_quotes(acme):
    h = acme
    cid = h.campaign()
    h.plan("acme", cid)
    uid, qid = h.held_unit("acme", cid)
    h.ok(h.go("acme", cid, "AVAILS_REQUESTED"))
    h.ok(h.commercial("acme", cid, "20.00"))
    h.ok(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[qid]), 201)
    ctx = {"cid": cid, "uid": uid}
    u = h.ok(_flight(h, ctx, "2026-11-01", "2026-12-31"))
    assert u["state"] == "VERIFIED" and u["vendor_ref"] is None and u["hold_expires_at"] is None
    assert h.svc.prices[qid]["retired"] is True and h.svc.prices[qid]["retired_reason"] == "FLIGHT_CHANGED"
    c = h.view("acme", cid)
    assert c["current_sell_price_id"] is None and c["hold_state"] is None
    assert h.refused(h.go("acme", cid, "QUOTES_VALIDATED"), 409)["guard"] == "NO_CURRENT_SELL_PRICE"
    h.refused(h.price("acme", cid, "CLIENT_SELL_PRICE", "6000.00", andre=True, quote_refs=[qid]), 409,
              "SELL_PRICE_QUOTES_INVALID")                        # the retired quote prices nothing
    h.ok(h.move("acme", cid, uid, "AVAILABILITY_REQUESTED"))
    r = h.refused(h.move("acme", cid, uid, "QUOTED"), 409, "TRANSITION_GUARD_FAILED")
    assert r["guard"] == "NO_VENDOR_QUOTE_FOR_UNIT"
    q2 = h.quote("acme", cid, [uid], "9000.00")
    assert h.svc.prices[q2]["flights"] == {uid: ["2026-11-01", "2026-12-31"]}
    assert h.ok(h.move("acme", cid, uid, "QUOTED"))["state"] == "QUOTED"


def test_r3h1_the_gate_compares_quoted_and_held_flights_with_the_current_one(acme):
    """Defense in depth: even if a unit's flight moved by a path that does not exist today, nothing is bought for a
    flight it was not quoted and held for."""
    h = acme
    ctx = h.funded()
    h.svc.inventory[ctx["uid"]]["flight"] = ["2026-11-01", "2027-10-31"]
    h.refused(h.authorize(ctx), 409, "QUOTE_FLIGHT_MISMATCH")
    h.svc.prices[ctx["qid"]]["flights"][ctx["uid"]] = ["2026-11-01", "2027-10-31"]
    h.refused(h.authorize(ctx), 409, "HOLD_FLIGHT_MISMATCH")
    h.svc.prices[ctx["qid"]]["flights"][ctx["uid"]] = ["2026-11-01", "2026-11-30"]
    h.svc.inventory[ctx["uid"]]["flight"] = ["2026-11-01", "2026-11-30"]
    a = h.ok(h.authorize(ctx), 201)
    ctx["aid"] = a["authorization_id"]
    h.svc.prices[ctx["qid"]]["flights"][ctx["uid"]] = ["2026-11-01", "2027-10-31"]
    h.refused(h.submit(ctx), 409, "QUOTE_FLIGHT_MISMATCH")       # and again at submission


# ---------------------------------------------------------------------------------------------- flight lows

def test_r3l_a_flight_already_begun_is_neither_quoted_nor_bought(acme):
    h = acme
    ctx = h.funded(flight=("2026-10-20", "2026-10-30"))
    h.clock.advance(days=5)                                       # 2026-10-21: the flight has begun
    h.refused(h.authorize(ctx, expires_at=at(days=6)), 409, "FLIGHT_IN_PAST")
    cid = h.campaign()
    uid = h.unit("acme", cid, flight=("2026-10-20", "2026-10-30"))
    h.refused(h.price("acme", cid, "VENDOR_QUOTE", "10.00", vendor="lamar", inventory_ids=[uid],
                      valid_until=at(days=8)), 409, "FLIGHT_IN_PAST")


@pytest.mark.parametrize("start,end,ok", [("0001-01-01", "9999-12-31", False), ("2026-11-01", "2027-11-01", True),
                                          ("2026-11-01", "2027-11-02", False)])
def test_r3l_a_flight_is_at_most_366_days(acme, start, end, ok):
    h = acme
    cid = h.campaign()
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory",
               {"request_id": rid(), "face_id": h.face("lamar", "f-1"),
                "flight_start": start, "flight_end": end})
    if ok:
        h.ok(r, 201)
    else:
        h.refused(r, 422, "FLIGHT_INVALID")


# ---------------------------------------------------------------------------------------------- vendor override

def test_r3l_andre_may_register_a_genuinely_distinct_vendor_whose_name_collides(acme):
    h = acme
    h.vendor("model-media", "Model Media")
    body = {"request_id": rid(), "vendor_id": "mode1-media", "legal_name": "Mode1 Media"}
    h.refused(h.post("/vendors", body, andre=True), 409, "VENDOR_EXISTS")
    wrong = {**body, "request_id": rid(), "distinct_from_ack": {"vendor_ids": ["lamar"], "reason": "SEPARATE_LEGAL_ENTITY"}}
    h.refused(h.post("/vendors", wrong, andre=True), 409, "VENDOR_EXISTS")          # must name the real collision
    bad = {**body, "request_id": rid(), "distinct_from_ack": {"vendor_ids": ["model-media"], "reason": "TRUST_ME"}}
    assert h.post("/vendors", bad, andre=True).status_code == 422                      # closed set of reasons
    ack = {**body, "request_id": rid(), "distinct_from_ack": {"vendor_ids": ["model-media"],
                                                              "reason": "SEPARATE_LEGAL_ENTITY"}}
    h.refused(h.post("/vendors", ack, caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    v = h.ok(h.post("/vendors", ack, andre=True), 201)
    assert v["distinct_from"] == ["model-media"] and v["distinct_reason"] == "SEPARATE_LEGAL_ENTITY"
    ev = h.ledger.of_type("vendor_registered_distinct_by_andre")
    assert len(ev) == 1 and ev[0]["_payload"]["distinct_from"] == ["model-media"]
    # an id is never shared, and an ack with nothing to acknowledge is refused
    h.refused(h.post("/vendors", {**ack, "request_id": rid()}, andre=True), 409, "VENDOR_EXISTS")
    lone = {"request_id": rid(), "vendor_id": "bulletin-west", "legal_name": "Bulletin West",
            "distinct_from_ack": {"vendor_ids": ["lamar"], "reason": "SEPARATE_LEGAL_ENTITY"}}
    h.refused(h.post("/vendors", lone, andre=True), 422, "INVALID")


# ---------------------------------------------------------------------------------------------- rail warning

def test_r3l_the_same_reference_on_another_rail_is_allowed_with_a_warning(acme):
    h = acme
    a = h.to_proposal_sent()
    h.ok(h.approve(a))
    first = h.ok(h.funds(a, a["sell"], ref="boa-12345"))
    assert first["warnings"] == []
    b = h.to_proposal_sent()
    h.ok(h.approve(b))
    # clients pay by ACH only (locked, Oct 16 round 2): a client check is refused outright...
    assert h.funds(b, b["sell"], ref="boa-12345", method="check").status_code == 422
    # ...so the warning can only meet a reference already held on another rail (dormant while ACH is the only client
    # rail; kept for when Finance (31) records other rails). Simulated: the same identity recorded as a check.
    h.svc.funds_evidence["check:boa777"] = a["cid"]
    r = h.ok(h.funds(b, b["sell"], ref="boa-777"))
    assert r["warnings"] == [{"code": "SAME_REFERENCE_OTHER_RAIL", "rails": ["check"]}]
    assert r["funds"][0]["warnings"] == r["warnings"]
    ev = [e for e in h.ledger.of_type("client_funds_verified_by_andre") if e["_payload"].get("warnings")]
    assert len(ev) == 1 and ev[0]["_payload"]["warnings"][0]["code"] == "SAME_REFERENCE_OTHER_RAIL"


# ---------------------------------------------------------------------------------------------- test gaps

def test_r3_vendor_registration_needs_andre(acme):
    body = {"request_id": rid(), "vendor_id": "bulletin-west", "legal_name": "Bulletin West"}
    acme.refused(acme.post("/vendors", body, caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    acme.refused(acme.post("/vendors", body, caller="compliance_38"), 403, "CALLER_NOT_ALLOWED")
    assert "bulletin-west" not in acme.svc.vendors


def test_r3_a_new_flight_that_clashes_with_the_same_face_is_refused(acme):
    h = acme
    a, b = h.campaign(), h.campaign()
    h.unit("acme", a, ref="face-555", flight=("2026-11-01", "2026-11-30"))
    uid = h.unit("acme", b, ref="FACE-555", flight=("2026-12-01", "2026-12-31"))
    h.refused(_flight(h, {"cid": b}, "2026-11-20", "2026-12-31", uid=uid), 409, "UNIT_ALREADY_REGISTERED")
    assert h.svc.inventory[uid]["flight"] == ["2026-12-01", "2026-12-31"]


# ---------------------------------------------------------------------------------------------- cosmetic

def test_r3_the_old_log_message_is_whole(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    rec = {"seq": 1, "kind": "tenant_created", "at": "2026-10-16T18:00:00Z",
           "data": {"tenant_id": "acme", "actor": "andre"}, "prev_line_sha256": GENESIS}
    rec["record_sha256"] = hashlib.sha256(encode(rec)).hexdigest()
    fd = os.open(data / LOG_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, encode(rec) + b"\n")
    os.close(fd)
    with pytest.raises(StoreCorrupt) as e:
        RecordLog(str(data))
    assert str(e.value) == ("log line 1 has no log format (written before format 2); this build reads log format 3 "
                            "only and has no migration (the service is unreleased): move the old data directory "
                            "aside and start from an empty one")
