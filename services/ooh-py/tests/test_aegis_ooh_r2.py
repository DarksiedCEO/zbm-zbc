"""AEGIS round 2 on OOH (Oct 16 2026): each finding's fix, proven by a test that fails on the code AEGIS reviewed
(5e8dcf0). R2-H1 zero padding; R2-L1 over-normalised transfer references and payment rails; R2-M1 the vendor registry;
R2-M2 flights and per-tenant registration by flight; R2-M3 the cross-tenant face clash at the purchase gate, through
the API; R2-L3 the log format version; R2-L4 the history entry for an answer recorded during a dispute."""

from __future__ import annotations

import hashlib
import os

import pytest

from helpers import Harness, at, rid
from store import GENESIS, LOG_NAME, RecordLog, StoreCorrupt, encode
from svc_campaigns import funds_ref_key

# war room seeds (devtools/warroom/scenarios/ooh-py.json)
ZERO_PADDED = ("000012345", "0012345", "ach:0000012345", "ACH-00012345", "12345", "ACH00012345")   # all = "12345"
VENDOR_SPOOFS = ("la-mar", "lamar-co", "lamar-inc", "the-lamar", "lamar-llc", "lamar-corp", "lamar", "l4mar",
                 "lamar-1nc", "l-a-m-a-r", "lamar-l-l-c")
CLASH_REFS = ("face-777", "FACE-777", "face_777", "Face.777", "face777")


@pytest.fixture
def acme(h):
    h.tenant("acme")
    return h


def _second(h, ref: str, method: str = "ach", first: str = "12345"):
    a = h.to_proposal_sent()
    h.ok(h.approve(a))
    h.ok(h.funds(a, a["sell"], ref=first))
    b = h.to_proposal_sent()
    h.ok(h.approve(b))
    return h.funds(b, "100.00", ref=ref, method=method)


# ---------------------------------------------------------------------------------------------- R2-H1

@pytest.mark.parametrize("ref", ZERO_PADDED)
def test_r2h1_zero_padding_never_makes_a_second_transfer(acme, ref):
    acme.refused(_second(acme, ref), 409, "FUNDS_EVIDENCE_REUSED")


def test_r2h1_an_all_zero_run_keeps_one_zero():
    assert funds_ref_key("BOA-0000", "ach") == "ach:boa0" != funds_ref_key("BOA", "ach")
    assert funds_ref_key("BOA-ACH-0012345", "ach") == funds_ref_key("boa-ach-12345", "ach")


# ---------------------------------------------------------------------------------------------- R2-L1

@pytest.mark.parametrize("first,second", [("REFUND77", "UND77"), ("CHECKOUT1", "OUT1"),
                                          ("TRANSFERWISE-889", "WISE-889"), ("WIRELESS5", "LESS5"),
                                          ("TRACEY01", "Y01")])
def test_r2l1_a_method_word_inside_a_word_is_part_of_the_reference(acme, first, second):
    r = _second(acme, second, first=first)
    assert r.status_code == 200 and r.json()["funds"][0]["evidence_ref"] == second, r.text
    assert funds_ref_key(first, "ach") != funds_ref_key(second, "ach")


def test_r2l1_different_rails_are_different_transfers(acme):
    assert funds_ref_key("BOA-ACH-12345", "ach") != funds_ref_key("BOA-WIRE-12345", "wire")
    # a reference naming the wire rail cannot be recorded as an ACH payment (nor the other way round)
    acme.refused(_second(acme, "BOA-WIRE-12345", first="BOA-ACH-12345"), 422, "FUNDS_METHOD_CONFLICT")
    acme.refused(_second(acme, "CHK-12345", first="BOA-ACH-9"), 422, "FUNDS_METHOD_CONFLICT")
    # clients pay by ACH only (locked, Oct 16 round 2): a check is not client money here
    assert _second(acme, "check-12345", method="check").status_code == 422


# ---------------------------------------------------------------------------------------------- R2-M1 vendors

@pytest.mark.parametrize("spoof", VENDOR_SPOOFS)
def test_r2m1_free_text_never_makes_a_second_identity_for_a_vendor(acme, spoof):
    r = acme.post("/vendors", {"request_id": rid(), "vendor_id": spoof, "legal_name": "Some Other Name"}, andre=True)
    acme.refused(r, 409, "VENDOR_EXISTS")
    assert sorted(acme.svc.vendors) == ["clear-channel", "lamar", "outfront"]


def test_r2m1_distinct_vendors_stay_distinct():
    from svc_tenants import vendor_identity_key as k
    assert k("lamar") == k("L4MAR") == k("Lamar Company, Inc.") == k("lamar-1nc") == k("l.a.m.a.r")
    assert len({k(x) for x in ("lamar", "outfront", "clear-channel", "bulletin-west", "lamar-transit")}) == 5


def test_r2m1_the_registry_is_andres_agents_read_it(acme):
    h = acme
    h.refused(h.post("/vendors", {"request_id": rid(), "vendor_id": "bulletin-co-west", "legal_name": "Bulletin West"}),
              403, "CALLER_NOT_ALLOWED")
    r = h.post("/vendors", {"request_id": rid(), "vendor_id": "lamar-2", "legal_name": "LAMAR ADVERTISING CO."},
               andre=True)
    h.refused(r, 409, "VENDOR_EXISTS")                          # the legal name is Lamar's
    h.ok(h.post("/vendors", {"request_id": rid(), "vendor_id": "bulletin-west", "legal_name": "Bulletin West LLC"},
                andre=True), 201)
    names = [v["vendor_id"] for v in h.ok(h.get("/vendors", caller="ooh_agent"))]
    assert names == ["bulletin-west", "clear-channel", "lamar", "outfront"]


def test_r2m1_unregistered_vendors_are_refused_everywhere(acme):
    h = acme
    cid = h.campaign()
    r = h.post("/sources", {"request_id": rid(), "kind": "RATE_CARD", "vendor": "lamarr", "doc_ref": "rc-1",
                            "dated": at(days=-1)})
    h.refused(r, 409, "VENDOR_UNKNOWN")                         # OOH-2: no source, so no face, of an unknown vendor
    h.refused(h.price("acme", cid, "VENDOR_COST_ESTIMATE", "10.00", vendor="lamarr"), 409, "VENDOR_UNKNOWN")
    ctx = h.funded()
    h.refused(h.authorize(ctx, vendor="lamarr"), 409, "VENDOR_UNKNOWN")


# ---------------------------------------------------------------------------------------------- R2-M2 flights

def test_r2m2_a_flight_is_required_before_quote_and_authorization_and_is_bound(acme):
    h = acme
    cid = h.campaign()
    uid = h.unit("acme", cid, flight=None)
    for to in ("VERIFIED", "AVAILABILITY_REQUESTED"):
        h.ok(h.move("acme", cid, uid, to))
    h.refused(h.price("acme", cid, "VENDOR_QUOTE", "10.00", vendor="lamar", inventory_ids=[uid],
                      valid_until=at(days=5)), 409, "FLIGHT_REQUIRED")
    u = h.ok(h.get(f"/tenants/acme/campaigns/{cid}/inventory/{uid}"))
    path = f"/tenants/acme/campaigns/{cid}/inventory/{uid}/flight"
    for start, end in (("2026-12-31", "2026-12-01"), ("2026-02-30", "2026-03-01")):
        h.refused(h.post(path, {"request_id": rid(), "expected_version": u["version"], "flight_start": start,
                                "flight_end": end}), 422, "FLIGHT_INVALID")
    r = h.post(f"/tenants/acme/campaigns/{cid}/inventory",
               {"request_id": rid(), "face_id": h.face("lamar", "f9"),
                "flight_start": "2026-12-01"})
    h.refused(r, 422, "FLIGHT_INVALID")                         # one end without the other
    u = h.ok(h.post(path, {"request_id": rid(), "expected_version": u["version"], "flight_start": "2026-12-01",
                           "flight_end": "2026-12-31"}))
    assert u["flight"] == ["2026-12-01", "2026-12-31"]
    h.ok(h.price("acme", cid, "VENDOR_QUOTE", "10.00", vendor="lamar", inventory_ids=[uid], valid_until=at(days=5)),
         201)


def test_r2m2_the_authorization_binds_the_flight_and_it_cannot_change_under_it(acme):
    h = acme
    ctx = h.authorized()
    u = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{ctx['uid']}"))
    h.refused(h.post(f"/tenants/acme/campaigns/{ctx['cid']}/inventory/{ctx['uid']}/flight",
                     {"request_id": rid(), "expected_version": u["version"], "flight_start": "2026-11-01",
                      "flight_end": "2026-12-15"}), 409, "FLIGHT_LOCKED")      # AEGIS R3-H1 (was INVENTORY_CHANGED)
    a = h.ok(h.get(f"/tenants/acme/campaigns/{ctx['cid']}/authorizations/{ctx['aid']}"))
    assert a["unit_flights"] == {ctx["uid"]: ["2026-11-01", "2026-11-30"]}


def test_r2m2_a_booked_face_is_free_again_for_another_flight_in_the_same_tenant(acme):
    h = acme
    ctx = h.authorized(ref="face-888")
    h.ok(h.submit(ctx))
    h.ok(h.outcome(ctx))
    b = h.campaign()
    body = {"request_id": rid(), "face_id": h.face("lamar", "FACE-888")}
    path = f"/tenants/acme/campaigns/{b}/inventory"
    h.refused(h.post(path, body), 409, "UNIT_ALREADY_REGISTERED")                 # no flight: may overlap
    h.refused(h.post(path, {**body, "request_id": rid(), "flight_start": "2026-11-30", "flight_end": "2026-12-30"}),
              409, "UNIT_ALREADY_REGISTERED")                                     # one shared day
    h.ok(h.post(path, {**body, "request_id": rid(), "flight_start": "2026-12-01", "flight_end": "2026-12-31"}), 201)


# ---------------------------------------------------------------------------------------------- R2-M3 face clash

@pytest.mark.parametrize("ref", CLASH_REFS)
def test_r2m3_one_face_one_flight_one_buyer_across_tenants(h, ref):
    h.tenant("acme")
    h.tenant("globex")
    first = h.authorized("acme", ref="face-777")
    h.ok(h.submit(first))                                         # SUBMISSION_UNKNOWN: an open order
    other = h.funded("globex", ref=ref, flight=("2026-11-15", "2026-12-15"))
    r = h.authorize(other)
    assert r.status_code == 409 and r.json() == {"detail": "FACE_ALREADY_ORDERED"}
    assert "acme" not in r.text and first["cid"] not in r.text and first["aid"] not in r.text
    assert h.view("globex", other["cid"])["authorization_ids"] == []


def test_r2m3_a_flight_that_does_not_overlap_is_allowed_and_booked_faces_still_block(h):
    h.tenant("acme")
    h.tenant("globex")
    first = h.authorized("acme", ref="face-777")
    h.ok(h.submit(first))
    h.ok(h.outcome(first))                                        # ACCEPTED: BOOKED, still open for the clash
    later = h.funded("globex", ref="face-777", flight=("2026-12-01", "2026-12-31"))
    h.ok(h.authorize(later), 201)
    clash = h.funded("globex", ref="face-777", flight=("2026-11-30", "2026-11-30"))
    h.refused(h.authorize(clash), 409, "FACE_ALREADY_ORDERED")


def test_r2m3_the_clash_is_checked_again_at_submission(h):
    h.tenant("acme")
    h.tenant("globex")
    g = h.authorized("globex", ref="face-777")                    # authorized first, not yet submitted
    a = h.authorized("acme", ref="face-777")
    h.ok(h.submit(a))
    h.refused(h.submit(g), 409, "FACE_ALREADY_ORDERED")
    h.ok(h.outcome(a, "REJECTED"))                                # the vendor said no: the face is free again
    assert h.ok(h.submit(g))["submission_state"] == "SUBMISSION_UNKNOWN"


# ---------------------------------------------------------------------------------------------- R2-L3, R2-L4

def test_r2l3_a_log_of_another_format_refuses_with_a_reason(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    rec = {"seq": 1, "kind": "tenant_created", "at": "2026-10-16T18:00:00Z",
           "data": {"tenant_id": "acme", "actor": "andre"}, "prev_line_sha256": GENESIS}
    rec["record_sha256"] = hashlib.sha256(encode(rec)).hexdigest()   # a valid chain, written before format 2
    fd = os.open(data / LOG_NAME, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, encode(rec) + b"\n")
    os.close(fd)
    with pytest.raises(StoreCorrupt, match="format .* no migration"):
        RecordLog(str(data))
    with pytest.raises(StoreCorrupt, match="log format"):
        Harness(tmp_path, data_dir=str(data))


def test_r2l4_an_answer_during_a_dispute_is_in_the_history(acme):
    h = acme
    ctx = h.authorized()
    h.ok(h.submit(ctx))
    h.ok(h.go("acme", ctx["cid"], "DISPUTED"))
    h.ok(h.outcome(ctx, "ACCEPTED"))
    last = h.view("acme", ctx["cid"])["history"][-1]
    assert last["from"] == last["to"] == "DISPUTED" and last["reason"] == "VENDOR_ACCEPTED_DURING_DISPUTE"
    assert last["resume_to"] == "BOOKED"
