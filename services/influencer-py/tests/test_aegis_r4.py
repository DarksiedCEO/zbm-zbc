"""AEGIS round 4 on 9170c01 (NOT BLOCKING): every probe that reproduced a problem, turned into a regression that fails
on 9170c01 and passes after the redesign (ADR 0015 amendment, round 4): verify the address first, then take the
application inside a short-lived creator session."""

from __future__ import annotations

import hashlib

import pytest

from helpers import Harness, rid, wired_ports


@pytest.fixture
def w(tmp_path):
    return Harness(tmp_path, ports=wired_ports())


def _reply(w, **body):
    return w.post("/replies", {"request_id": rid(), **body}, caller="provider_events")


# ------------------------------------------------------------------------------------------------ M1'

def test_m1_three_stranger_requests_and_the_creator_still_completes(w):
    for _ in range(3):
        w.ok(w.link("target@example.test"), 201)                   # a stranger, three times
    w.clock.advance(days=6, hours=23)
    for _ in range(3):
        w.ok(w.link("target@example.test"), 201)                   # 9170c01: 429 CONFIRMATIONS_OPEN_LIMIT for good
    app = w.ok(w.link("target@example.test"), 201)                 # the creator: never refused
    assert len([c for c in w.svc.confirmations.values() if c["email_hash"] == w.svc.confirmations[
        app["confirmation_id"]]["email_hash"]]) == 1               # one link, reused
    s = w.ok(w.confirm(app["confirmation_id"]))
    inf = w.ok(w.submit(s, handles=(("instagram", "real"),)), 201)
    assert inf["adult_attested"] is True and [h["handle"] for h in inf["handles"]] == ["real"]


def test_m1_repeats_never_burn_a_quota_and_mail_once_a_day(w):
    first = w.ok(w.link("t2@example.test"), 201)
    w.ok(w.job("send-queue"))
    for _ in range(20):
        assert w.ok(w.link("t2@example.test"), 201)["confirmation_id"] == first["confirmation_id"]   # 9170c01: 429
    w.ok(w.job("send-queue"))
    w.clock.advance(hours=23)
    w.ok(w.job("send-queue"))
    assert sum(1 for x in w.ports.email.sent if x[1] == "t2@example.test") == 1     # at most one a day
    w.clock.advance(hours=1, seconds=1)
    w.ok(w.job("send-queue"))
    assert sum(1 for x in w.ports.email.sent if x[1] == "t2@example.test") == 2
    queued = [m for m in w.svc.messages.values() if m["status"] == "queued" and m["to_hash"] == w.svc.confirmations[
        first["confirmation_id"]]["email_hash"]]
    assert not queued


def test_m1_a_stranger_can_never_attest_or_attach_handles(w):
    p = w.prospect(email="found@example.test", handles=(("tiktok", "@found.one"),))
    for extra in ({"adult_18_plus": True, "attestation_text_version": "v1", "attestation_text_sha256": "a" * 64},
                  {"handles": [{"platform": "x", "handle": "@stranger"}]}, {"display_name": "Mallory"}):
        assert w.link("found@example.test", **extra).status_code == 422          # 9170c01: 201, payload kept
    w.ok(w.link("found@example.test"), 201)
    inf = w.svc.influencers[p["influencer_id"]]
    assert inf["adult_attested"] is False and [h["handle"] for h in inf["handles"]] == ["found.one"]
    assert w.post("/sessions/application", {"request_id": rid(), "session_token": "if-ses-" + "0" * 40 + "." + "0" * 64,
                                            "display_name": "M", "adult_18_plus": True,
                                            "attestation_text_version": "v1", "attestation_text_sha256": "a" * 64},
                  caller="hub").json()["detail"] == "SESSION_INVALID"
    assert w.svc.influencers[p["influencer_id"]]["adult_attested"] is False


def test_m1_strangers_never_block_the_creators_tax_change(w):
    inf = w.creator(email="paid@example.test", handles=(("instagram", "paid"),))
    for _ in range(5):
        w.ok(w.link("paid@example.test"), 201)                     # 9170c01: three opens, then the tax change 429
    out = w.ok(w.tax(inf), 201)
    assert out["status"] == "applied" and w.svc.influencers[inf["influencer_id"]]["tax"] is not None


def test_m1_a_session_expires_on_the_service_clock(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CREATOR_SESSION_MINUTES="10")
    inf = h.creator()
    s = h.session()
    h.clock.advance(minutes=10, seconds=1)
    h.code(h.tax(inf, session=s), 403, "SESSION_EXPIRED")
    h.code(h.submit(s), 403, "SESSION_EXPIRED")
    assert h.svc.influencers[inf["influencer_id"]]["tax"] is None


def test_m1_a_session_is_bound_to_its_record(w):
    a = w.creator(email="a@example.test", handles=(("x", "aa"),))
    b = w.creator(email="b@example.test", handles=(("x", "bb"),))
    s = w.session("a@example.test")
    w.code(w.tax(b, session=s), 403, "SESSION_RECORD_MISMATCH")
    assert w.svc.influencers[b["influencer_id"]]["tax"] is None
    w.ok(w.tax(a, session=s), 201)


def test_m1_one_tax_change_per_session(w):
    inf = w.creator()
    s = w.session()
    w.ok(w.tax(inf, session=s), 201)
    w.code(w.tax(inf, ref="stripe:acct_SECONDreference000", session=s), 409, "SESSION_ACTION_USED")
    assert w.svc.influencers[inf["influencer_id"]]["tax"]["tax_ref"] != "stripe:acct_SECONDreference000"
    w.ok(w.tax(inf, ref="stripe:acct_SECONDreference000"), 201)    # a new session (a new click) may
    w.ok(w.submit(s), 201)                                         # the other action kind, once
    w.code(w.submit(s), 409, "SESSION_ACTION_USED")


def test_m1_the_session_token_is_256_bits_never_logged_never_on_the_ledger(w):
    s = w.session()
    sid, mac = s["session_token"].split(".")
    assert len(mac) == 64 and int(mac, 16) > 0 and sid == s["session_id"]
    other = w.session("other@example.test")
    assert other["session_token"] != s["session_token"]
    w.ok(w.submit(s), 201)
    w.ok(w.tax(w.svc.influencers[s["influencer_id"]], session=s), 201)
    raw = str(w.svc.log.records)
    assert mac not in raw and mac not in str(w.ledger.events) and mac not in str(w.svc.sessions)
    for tampered in (sid + "." + mac[:-1] + ("0" if mac[-1] != "0" else "1"), s["session_token"][:-8]):
        w.code(w.tax(w.svc.influencers[s["influencer_id"]], session={"session_token": tampered}), 403,
               "SESSION_INVALID")


def test_m1_the_click_replays_the_same_session_and_survives_a_restart(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=wired_ports())
    link = h.ok(h.link(), 201)
    body = {"request_id": rid(), "token": h.svc.confirmation_token(link["confirmation_id"])}
    s = h.ok(h.post("/confirmations", body, caller="hub"))
    assert h.ok(h.post("/confirmations", body, caller="hub"))["session_token"] == s["session_token"]
    mac = s["session_token"].split(".")[1]
    assert mac.encode() not in (tmp_path / "d" / "influencer_log.jsonl").read_bytes()
    h2 = h.restart()
    h2.ok(h2.submit(s), 201)


def test_m1_a_suppressed_address_still_waits_for_andre_and_the_review_cap(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_ANDRE_REVIEW_DAILY_CAP="1")
    for a in ("s1@example.test", "s2@example.test"):
        h.ok(_reply(h, channel="email", from_email=a, text="STOP"), 201)
    one = h.ok(h.link("s1@example.test"), 201)
    two = h.ok(h.link("s2@example.test"), 201)
    assert (one["confirmation_status"], two["confirmation_status"]) == ("awaiting_andre", "awaiting_andre_digest")
    assert h.ok(h.link("s1@example.test"), 201)["confirmation_id"] == one["confirmation_id"]
    h.code(h.confirm(one["confirmation_id"]), 409, "CONFIRMATION_USED")       # not mailed: no session


# ------------------------------------------------------------------------------------------------ L1'

def test_l1_the_same_reply_again_attaches_to_the_active_hold(w):
    inf = w.creator()
    b = {"channel": "email", "from_email": "creator@example.test", "text": "interested"}
    a = w.ok(w.post("/replies", {**b, "request_id": rid()}, caller="provider_events"), 201)
    c = w.ok(w.post("/replies", {**b, "request_id": rid()}, caller="provider_events"), 201)
    assert c["hold_id"] == a["hold_id"] and c["reply_id"] != a["reply_id"]        # 9170c01: a second hold
    assert w.svc.holds[a["hold_id"]]["reply_ids"] == [a["reply_id"], c["reply_id"]]
    assert w.ledger.of_type("outreach_hold_reply_attached")
    w.ok(w.post(f"/holds/{a['hold_id']}/decision", {"request_id": rid(), "decision": "continue"}, andre=True))
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["held"] is False
    d = w.ok(w.post("/replies", {**b, "request_id": rid()}, caller="provider_events"), 201)
    assert d["hold_id"] != a["hold_id"]                            # after Andre decided, a new reply holds again


# ------------------------------------------------------------------------------------------------ L5'

def test_l5_an_unresolved_opt_out_goes_to_andres_digest_before_it_expires(w):
    inf = w.creator()
    w.ok(_reply(w, channel="email", from_email="creator@example.test", text="maybe"), 201)
    stop = w.ok(_reply(w, channel="email", text="STOP please"), 201)          # resolves nothing
    ooo = w.ok(_reply(w, channel="email", text="sounds good"), 201)
    w.clock.advance(days=31)
    anchors = len(w.ledger.of_type("log_anchor"))
    out = w.ok(w.job("hold-expiry"))
    assert out["expired"] == 1 and out["digested"] == 1             # 9170c01: the opt-out expired silently
    assert w.svc.holds[stop["hold_id"]]["status"] == "active" and w.svc.holds[ooo["hold_id"]]["status"] == "expired"
    assert [h["hold_id"] for h in w.ok(w.get("/holds", params={"status": "digest"}))] == [stop["hold_id"]]
    assert w.ledger.of_type("holds_digested")[0]["_payload"]["hold_ids"] == [stop["hold_id"]]
    assert len(w.ledger.of_type("log_anchor")) == anchors + 1
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["held"] is True   # the targeted hold stays
    w.clock.advance(days=6)
    assert w.ok(w.job("hold-expiry"))["expired"] == 0
    w.clock.advance(days=1, seconds=1)
    assert w.ok(w.job("hold-expiry"))["expired"] == 1 and w.svc.holds[stop["hold_id"]]["status"] == "expired"


def test_l5_andre_decides_a_digest_hold(w):
    r = w.ok(_reply(w, channel="email", text="please review this"), 201)
    w.clock.advance(days=31)
    w.ok(w.job("hold-expiry"))
    assert w.svc.holds[r["hold_id"]]["digest_at"]
    w.ok(w.post(f"/holds/{r['hold_id']}/decision", {"request_id": rid(), "decision": "continue"}, andre=True))
    assert w.svc.holds[r["hold_id"]]["status"] == "lifted"


def test_bulk_reject_still_binds_the_exact_list_of_address_links(w):
    ids = []
    for i in range(2):
        a = f"sp{i}@example.test"
        w.ok(_reply(w, channel="email", from_email=a, text="STOP"), 201)
        ids.append(w.ok(w.link(a), 201)["confirmation_id"])
    sha = hashlib.sha256("\n".join(ids).encode()).hexdigest()
    w.code(w.post("/confirmations/bulk-reject", {"request_id": rid(), "conf_ids": ids[::-1], "ids_sha256": sha},
                  andre=True), 409, "ID_LIST_MISMATCH")
    w.ok(w.post("/confirmations/bulk-reject", {"request_id": rid(), "conf_ids": ids, "ids_sha256": sha}, andre=True))
