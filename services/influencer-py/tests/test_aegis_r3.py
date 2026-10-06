"""AEGIS round 3 on fa0c091 (NOT BLOCKING): every probe that reproduced a problem, turned into a regression that fails
on fa0c091 and passes after the fix (ADR 0015 amendment, round 3). The vault-alphabet probe (R3-L4) is answered on the
unlock list, not in code: Finance must confirm a vault reference exists before anything ships."""

from __future__ import annotations

import hashlib
import pytest

from helpers import Harness, rid, wired_ports


@pytest.fixture
def w(tmp_path):
    return Harness(tmp_path, ports=wired_ports())


# ------------------------------------------------------------------------------------------------ M1

# amended in round 4 (R4-M1'): the public form takes an address only, so a stranger's request carries nothing to bind;
# the per-address open cap these tests once met is gone (any such cap was a lockout)

def test_m1_a_stranger_never_invalidates_a_creators_open_confirmation(w):
    app = w.ok(w.link("newbie@example.test"), 201)
    w.ok(w.job("send-queue"))                                      # the creator's mail goes out
    again = w.ok(w.link("newbie@example.test"), 201)               # a stranger
    assert again["confirmation_id"] == app["confirmation_id"]
    s = w.ok(w.confirm(app["confirmation_id"]))                    # fa0c091: 409 CONFIRMATION_USED (superseded)
    inf = w.ok(w.submit(s, handles=(("instagram", "newbie"),)), 201)
    assert inf["adult_attested"] is True
    assert [h["handle"] for h in inf["handles"]] == ["newbie"]     # only what the mailbox owner submitted


def test_m1_daily_superseding_cannot_lock_a_creator_out(w):
    app = w.ok(w.link("newbie2@example.test"), 201)
    w.ok(w.job("send-queue"))
    for _ in range(3):
        assert w.ok(w.link("newbie2@example.test"), 201)["confirmation_id"] == app["confirmation_id"]
    assert w.svc.confirmations[app["confirmation_id"]]["status"] == "pending"
    w.clock.advance(hours=12)
    s = w.ok(w.confirm(app["confirmation_id"]))
    assert w.ok(w.submit(s, handles=(("instagram", "n2"),)), 201)["adult_attested"] is True


def test_m1_a_cancelled_mail_does_not_delay_the_next(w):
    a = w.ok(w.link("quick@example.test"), 201)
    w.ok(w.confirm(a["confirmation_id"]))                          # clicked from a mail sent before; this one cancelled
    w.ok(w.link("quick@example.test"), 201)                        # a new link later the same day
    out = w.ok(w.job("send-queue"))
    assert out["confirmations_sent"] == 1 and out["deferred"] == 0   # fa0c091: deferred (counted from queue time)


def test_m1_the_daily_rule_counts_from_send_time(w):
    w.ok(w.link("slow@example.test"), 201)
    w.clock.advance(hours=20)
    w.ok(w.job("send-queue"))                                      # sent at hour 20
    w.ok(w.link("slow@example.test"), 201)                         # mailed again: queued now
    w.clock.advance(hours=10)                                      # 30 h after queueing, 10 h after sending
    assert w.ok(w.job("send-queue"))["deferred"] == 1
    w.clock.advance(hours=15)
    assert w.ok(w.job("send-queue"))["confirmations_sent"] == 1


# ------------------------------------------------------------------------------------------------ L1

def test_l1_a_reused_request_id_with_another_body_is_another_reply(w):
    inf = w.creator()
    rq = rid()
    w.ok(w.post("/replies", {"request_id": rq, "channel": "email", "text": "thanks!"}, caller="provider_events"), 201)
    r = w.ok(w.post("/replies", {"request_id": rq, "channel": "email", "from_email": "creator@example.test",
                                 "text": "STOP"}, caller="provider_events"), 201)   # fa0c091: 409, opt-out dropped
    assert r["suppressed"] is True
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["suppressed"] is True
    again = w.ok(w.post("/replies", {"request_id": rq, "channel": "email", "from_email": "creator@example.test",
                                     "text": "STOP"}, caller="provider_events"), 201)
    assert again["reply_id"] == r["reply_id"] and len(w.svc.replies) == 2   # the same body and id: the same reply


# ------------------------------------------------------------------------------------------------ L2

def _self_suppressed(w, n):
    out = []
    for i in range(n):
        a = f"spam{i}@example.test"
        w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": a, "text": "STOP"},
                    caller="provider_events"), 201)
        out.append(w.ok(w.link(a), 201))
    return out


def test_l2_andres_review_queue_is_capped_a_day_and_the_rest_go_to_the_digest(w):
    apps = _self_suppressed(w, 30)
    statuses = [a["confirmation_status"] for a in apps]
    assert statuses.count("awaiting_andre") == 20 and statuses.count("awaiting_andre_digest") == 10
    digest = w.svc.confirmations[apps[-1]["confirmation_id"]]
    w.ok(w.post(f"/confirmations/{digest['conf_id']}/approve",
                {"request_id": rid(), "content_sha256": digest["content_sha256"]}, andre=True))   # still approvable


def test_l2_the_review_cap_is_a_setting(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_ANDRE_REVIEW_DAILY_CAP="2")
    apps = _self_suppressed(h, 3)
    assert [a["confirmation_status"] for a in apps] == ["awaiting_andre", "awaiting_andre", "awaiting_andre_digest"]


def test_l2_andre_bulk_rejects_by_the_hash_of_the_exact_list(w):
    apps = _self_suppressed(w, 5)
    ids = [a["confirmation_id"] for a in apps]
    sha = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    w.code(w.post("/confirmations/bulk-reject", {"request_id": rid(), "conf_ids": ids, "ids_sha256": sha}), 403,
           "ANDRE_APPROVAL_REQUIRED")
    w.code(w.post("/confirmations/bulk-reject", {"request_id": rid(), "conf_ids": ids[:4], "ids_sha256": sha},
                  andre=True), 409, "ID_LIST_MISMATCH")
    out = w.ok(w.post("/confirmations/bulk-reject", {"request_id": rid(), "conf_ids": ids, "ids_sha256": sha},
                      andre=True))
    assert out["rejected"] == ids and all(w.svc.confirmations[i]["status"] == "rejected" for i in ids)
    assert w.ledger.of_type("confirmations_bulk_rejected")[0]["_payload"]["ids_sha256"] == sha


# ------------------------------------------------------------------------------------------------ L3

def test_l3_new_addresses_keep_their_share_of_the_confirmation_cap(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_DAILY_CAP="4")
    for i in range(6):
        h.creator(email=f"k{i}@example.test", handles=(("x", f"k{i}"),))
    h.clock.advance(hours=25)
    h.ok(h.job("send-queue"))
    h.clock.advance(hours=25)
    for i in range(6):
        h.ok(h.link(f"k{i}@example.test"), 201)                    # six known creators ask first
    h.ok(h.link("fresh@example.test"), 201)
    h.ok(h.job("send-queue"))
    sent = [x[1] for x in h.ports.email.sent[-4:]]
    assert "fresh@example.test" in sent                            # fa0c091: known creators took the whole cap
    assert sum(1 for x in sent if x.startswith("k")) == 3


# ------------------------------------------------------------------------------------------------ L5

def test_l5_rate_state_older_than_a_day_is_pruned(w):
    w.ok(w.link("p1@example.test"), 201)
    w.ok(w.job("send-queue"))
    assert w.svc.conf_mail_at                                      # (round 4: no per-address application times)
    w.clock.advance(hours=25)
    w.ok(w.job("send-queue"))
    assert not w.svc.conf_mail_at


def test_l5_unresolved_holds_expire_recorded_and_anchored(w):
    r = w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "text": "sounds good"},
                    caller="provider_events"), 201)       # interested (an opt-out or a review goes to the digest: R4)
    inf = w.creator()
    t = w.template()
    targeted = w.ok(w.post("/replies", {"request_id": rid(), "channel": "email", "from_email": "creator@example.test",
                                        "text": "maybe"}, caller="provider_events"), 201)
    w.clock.advance(days=29)
    assert w.ok(w.job("hold-expiry"))["expired"] == 0
    w.clock.advance(days=1, seconds=1)
    anchors = len(w.ledger.of_type("log_anchor"))
    assert w.ok(w.job("hold-expiry"))["expired"] == 1              # fa0c091: no such job, holds kept for ever
    assert w.svc.holds[r["hold_id"]]["status"] == "expired"
    assert w.svc.holds[targeted["hold_id"]]["status"] == "active"   # a hold with a target only Andre lifts
    assert len(w.ledger.of_type("log_anchor")) == anchors + 1 and w.ledger.of_type("holds_expired")
    w.code(w.email(inf, t), 403, "REPLY_HOLD")


def test_l5_hold_expiry_days_is_a_setting(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_UNRESOLVED_HOLD_DAYS="2")
    r = h.ok(h.post("/replies", {"request_id": rid(), "channel": "email", "text": "sounds good"}, caller="provider_events"), 201)
    h.clock.advance(days=2, seconds=1)
    assert h.ok(h.job("hold-expiry"))["expired"] == 1 and h.svc.holds[r["hold_id"]]["status"] == "expired"
