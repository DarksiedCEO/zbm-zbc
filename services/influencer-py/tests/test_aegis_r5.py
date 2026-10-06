"""AEGIS round 5 on bb073dc (NOT BLOCKING): every probe that reproduced a problem, turned into a regression that fails
on bb073dc and passes after the fix (ADR 0015 amendment, round 5). R5-L1 (link prefetchers) and the lost-mailbox
procedure are hub and operator contracts in the ADR and README, not code."""

from __future__ import annotations

import pytest

from helpers import Harness, rid, wired_ports


@pytest.fixture
def w(tmp_path):
    return Harness(tmp_path, ports=wired_ports())


def _link_mails(w):
    return {m["confirmation_id"]: m for m in w.svc.messages.values() if m.get("purpose") == "confirmation"}


# ------------------------------------------------------------------------------------------------ M1

def test_m1_an_unrelated_opt_out_cancels_no_new_address_link(w):
    links = [w.ok(w.link(f"new{i}@example.test"), 201)["confirmation_id"] for i in range(5)]
    w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "rando@x.test", "text": "STOP"},
                caller="provider_events"), 201)
    assert [_link_mails(w)[c]["status"] for c in links] == ["queued"] * 5     # bb073dc: all five cancelled
    assert w.ok(w.job("send-queue"))["confirmations_sent"] == 5


def test_m1_an_opt_out_still_cancels_its_own_addresss_link(w):
    mine = w.ok(w.link("leaving@example.test"), 201)["confirmation_id"]
    other = w.ok(w.link("staying@example.test"), 201)["confirmation_id"]
    w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "leaving@example.test",
                             "text": "STOP"}, caller="provider_events"), 201)
    mails = _link_mails(w)
    assert mails[mine]["status"] == "cancelled" and mails[other]["status"] == "queued"


# ------------------------------------------------------------------------------------------------ M2

def test_m2_a_full_queue_never_refuses_a_link(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="20")
    h.creator(email="real@example.test", handles=(("instagram", "real"),))
    junk = [h.ok(h.link(f"junk{i}@example.test"), 201)["confirmation_id"] for i in range(20)]
    new = h.ok(h.link("newcreator@example.test"), 201)            # bb073dc: 429 QUEUE_FULL
    known = h.ok(h.link("real@example.test"), 201)                # bb073dc: 429 QUEUE_FULL
    mails = _link_mails(h)
    assert mails[new["confirmation_id"]]["status"] == "queued" and mails[known["confirmation_id"]]["status"] == "queued"
    assert mails[junk[0]]["status"] == "cancelled" and mails[junk[0]]["reason"] == "QUEUE_EVICTED"   # the oldest
    assert all(mails[c]["status"] == "queued" for c in junk[1:])
    ev = h.ledger.of_type("confirmation_mail_evicted")
    assert [e["_payload"]["conf_id"] for e in ev] == [junk[0]]
    assert h.svc.confirmations[junk[0]]["status"] == "pending"    # the evicted link stays valid
    h.ok(h.link("junk0@example.test"), 201)                        # asked again: mailed again, the next oldest goes
    assert _link_mails(h)[junk[0]]["status"] == "queued" and _link_mails(h)[junk[1]]["reason"] == "QUEUE_EVICTED"


def test_m2_records_we_hold_have_their_own_share(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="2")
    for i in range(3):
        h.creator(email=f"k{i}@example.test", handles=(("x", f"k{i}"),))
    for i in range(5):
        h.ok(h.link(f"junk{i}@example.test"), 201)
    known = [h.ok(h.link(f"k{i}@example.test"), 201)["confirmation_id"] for i in range(3)]
    assert all(_link_mails(h)[c]["status"] == "queued" for c in known)
    assert sum(1 for m in h.svc.messages.values() if m["status"] == "queued" and not m.get("existing_record")) == 2


def test_m2_an_eviction_is_anchored_and_replays(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="1")
    a = h.ok(h.link("a@example.test"), 201)["confirmation_id"]
    anchors = len(h.ledger.of_type("log_anchor"))
    b = h.ok(h.link("b@example.test"), 201)["confirmation_id"]
    assert len(h.ledger.of_type("log_anchor")) == anchors + 1     # one line: the new mail and the eviction
    h2 = h.restart()
    mails = _link_mails(h2)
    assert mails[a]["status"] == "cancelled" and mails[b]["status"] == "queued"


# ------------------------------------------------------------------------------------------------ L2 / L3

def test_l2_a_record_created_after_the_click_is_the_one_the_session_binds(w):
    s = w.session("race@example.test")
    p = w.prospect(handles=(("x", "racep"),), email="race@example.test")   # created between the click and the submit
    a = w.ok(w.submit(s, handles=(("instagram", "racei"),)), 201)
    assert a["influencer_id"] == p["influencer_id"]               # bb073dc: a second record for the same address
    assert len(w.svc.influencers) == 1 and a["adult_attested"] is True and a["email_confirmed"] is True
    assert {h["handle"] for h in a["handles"]} == {"racep", "racei"} and a["source"] == "manual_research"
    assert a["display_name"] == "Found One"                       # identity fields of the record are not changed
    out = w.ok(w.tax(a, session=s), 201)                          # the same session acts on that record
    assert out["influencer_id"] == p["influencer_id"]


def test_l3_a_declared_minor_in_such_a_session_freezes_the_record_found(w):
    s = w.session("race2@example.test")
    p = w.prospect(handles=(("x", "race2"),), email="race2@example.test")
    w.code(w.submit(s, adult=False), 422, "MINOR_REFUSED")
    assert w.svc.influencers[p["influencer_id"]]["blocked"] == "MINOR_DECLARED" and len(w.svc.influencers) == 1
