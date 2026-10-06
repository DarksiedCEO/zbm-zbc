"""AEGIS round 2 on 50312db (NOT BLOCKING): every ``test_vuln_*`` probe of the reviewer turned into a regression that
fails on 50312db and passes after the fix (ADR 0015 amendment, round 2)."""

from __future__ import annotations

import pytest

from helpers import FakeFinance, Harness, rid, wired_ports


@pytest.fixture
def w(tmp_path):
    return Harness(tmp_path, ports=wired_ports())


def _reply(w, **body):
    return w.post("/replies", {"request_id": rid(), **body}, caller="provider_events")


# ------------------------------------------------------------------------------------------------ N1

def test_n1_a_confirmation_flood_does_not_starve_a_real_creator_or_outreach(w):
    known = w.creator(email="known@example.test", handles=(("x", "kn"),))
    t = w.template()
    w.ok(w.email(known, t), 201)
    w.clock.advance(hours=25)
    for i in range(50):
        w.ok(w.link(f"junk{i}@example.test"), 201)
    legit = w.ok(w.link("real@example.test"), 201)
    out = w.ok(w.job("send-queue"))
    sent_to = [x[1] for x in w.ports.email.sent]
    assert "real@example.test" in sent_to                          # 50312db: the 51st mail was over the cap
    assert out["sent"] == 1 and "known@example.test" in sent_to   # outreach has its own cap
    assert out["confirmations_sent"] == 51
    assert w.svc.confirmations[legit["confirmation_id"]]["status"] == "pending"


def test_n1_a_known_creators_confirmation_goes_before_new_addresses(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_DAILY_CAP="3")
    h.creator(email="known@example.test", handles=(("x", "kn"),))
    h.clock.advance(hours=25)
    h.ok(h.job("send-queue"))
    for i in range(5):
        h.ok(h.link(f"junk{i}@example.test"), 201)
    conf = h.ok(h.link("known@example.test"), 201)                 # a real creator, queued last
    n = len(h.ports.email.sent)
    h.ok(h.job("send-queue"))
    assert [x[1] for x in h.ports.email.sent][n] == "known@example.test"
    assert h.svc.messages[h.svc.confirmations[conf["confirmation_id"]]["message_id"]]["status"] == "sent"


def test_n1_one_address_gets_one_mail_a_day_and_one_open_link(w):
    # amended in round 4 (R4-M1'): repeats are never refused (no 5-a-day limit); they reuse the one open link
    first = w.ok(w.link("victim2@example.test"), 201)
    for _ in range(9):
        again = w.ok(w.link("victim2@example.test"), 201)
        assert again["confirmation_id"] == first["confirmation_id"]   # every request reuses it
    w.ok(w.job("send-queue"))
    assert sum(1 for x in w.ports.email.sent if x[1] == "victim2@example.test") == 1   # 50312db: 20 mails


def test_n1_a_repeat_request_waits_a_day_for_its_mail(w):
    # amended in round 3 (R3-M1: no superseding) and round 4 (R4-M1': the one link is reused and mailed again)
    a = w.ok(w.link("someone@example.test"), 201)
    w.ok(w.job("send-queue"))
    b = w.ok(w.link("someone@example.test"), 201)
    assert b["confirmation_id"] == a["confirmation_id"]
    assert w.svc.confirmations[a["confirmation_id"]]["status"] == "pending"
    assert w.ok(w.job("send-queue"))["deferred"] == 1
    w.clock.advance(hours=24, seconds=1)
    assert w.ok(w.job("send-queue"))["confirmations_sent"] == 1
    assert w.svc.confirmation_token(a["confirmation_id"]) in w.ports.email.sent[-1][2]["body"]


def test_n1_the_confirmation_queue_is_bounded(tmp_path):
    # amended in round 5 (R5-M2): a full new-address queue evicts its oldest mail instead of refusing the new link
    h = Harness(tmp_path, ports=wired_ports(), INF_CONFIRMATION_QUEUE_MAX="2")
    h.ok(h.link("a1@example.test"), 201)
    h.ok(h.link("a2@example.test"), 201)
    h.ok(h.link("a3@example.test"), 201)
    assert sum(1 for m in h.svc.messages.values() if m["status"] == "queued") == 2


# ------------------------------------------------------------------------------------------------ N2

def test_n2_two_tax_references_one_tin_are_one_person(tmp_path):
    fin = FakeFinance(same_person={"stripe:acct_PERSONaaaaaaaaaaaaaa", "stripe:acct_PERSONbbbbbbbbbbbbbb"})
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    a = h.creator(email="q1@example.test", handles=(("instagram", "qa"),))
    b = h.creator(email="q2@example.test", handles=(("tiktok", "qb"),))
    h.tax_confirmed(a, ref="stripe:acct_PERSONaaaaaaaaaaaaaa")
    h.tax_confirmed(b, ref="stripe:acct_PERSONbbbbbbbbbbbbbb")
    h.ok(h.verify(a))
    h.ok(h.verify(b))
    c = h.campaign()
    br = h.brief(c)
    assert h.ok(h.deal(a, c, br, fee="5000.00"), 201)["status"] == "approved"
    second = h.ok(h.deal(b, c, br, fee="5000.00"), 201)
    assert second["status"] == "pending_andre"                     # 50312db: approved (two references)


def test_n2_the_person_key_is_rechecked_at_payout(tmp_path):
    fin = FakeFinance(same_person={"stripe:acct_PERSONaaaaaaaaaaaaaa", "stripe:acct_PERSONbbbbbbbbbbbbbb"})
    h = Harness(tmp_path, ports=wired_ports(finance=fin))
    pays = []
    for i, ref in enumerate(("stripe:acct_PERSONaaaaaaaaaaaaaa", "stripe:acct_PERSONbbbbbbbbbbbbbb")):
        inf = h.creator(email=f"r{i}@example.test", handles=(("instagram", f"r{i}"),))
        c = h.campaign()
        br = h.brief(c)
        d = h.ok(h.deal(inf, c, br, fee="5000.00"), 201)          # no tax reference yet: not linked at deal time
        assert d["status"] == "approved"
        h.contract(d)
        ct = h.ok(h.content(d), 201)
        h.approve_content(ct)
        h.ok(h.live(ct))
        h.tax_confirmed(inf, ref=ref)
        h.ok(h.verify(inf))
        pays.append(h.ok(h.payout(d, "5000.00", [ct["content_id"]]), 201))
    assert pays[0]["status"] == "submitted"
    assert pays[1]["status"] == "pending_andre" and pays[1]["needs_andre"] == ["PAYEE_TOTAL_OVER_LIMIT"]


def test_n2_without_a_person_key_a_payout_waits_for_andre(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(finance=FakeFinance(person_keys=False)))
    inf, d, ct = h.paid_ready(fee="100.00")
    p = h.ok(h.payout(d, "100.00", [ct["content_id"]]), 201)
    assert p["status"] == "pending_andre" and p["needs_andre"] == ["PERSON_KEY_MISSING"]
    assert not h.ports.finance.payouts


# ------------------------------------------------------------------------------------------------ N3

def test_n3_a_long_reply_is_truncated_never_refused(w):
    inf = w.creator()
    r = w.ok(_reply(w, channel="email", from_email="creator@example.test", text="STOP\n\n" + "> quoted\n" * 3000),
             201)
    assert r["suppressed"] is True
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["suppressed"] is True


def test_n3_a_numeric_message_id_is_never_refused(w):
    inf = w.creator()
    r = w.ok(_reply(w, channel="email", message_id="<123456789@mail.example>", from_email="creator@example.test",
                    text="STOP"), 201)
    assert r["suppressed"] is True and r["ignored"] == ["message_id"] and r["influencer_id"] == inf["influencer_id"]
    assert "123456789@mail.example" not in str(w.svc.log.records)


@pytest.mark.parametrize("body", [{"channel": "sms", "from_email": "creator@example.test", "text": "stop"},
                                  {"channel": "email", "from_email": "creator@example.test", "text": None},
                                  {"channel": "email", "from_email": 42, "text": ["stop"]},
                                  {"channel": "email", "from_email": "creator@example.test", "text": "stop",
                                   "provider_extra": {"ssn": "123-45-6789"}},
                                  {"from_email": "creator@example.test", "text": "stop", "request_id": None}])
def test_n3_nothing_a_provider_sends_refuses_a_reply(w, body):
    w.creator()
    r = w.post("/replies", body, caller="provider_events")
    assert r.status_code == 201, r.text
    assert r.json()["held"] is True


def test_n3_a_reply_body_that_is_not_an_object_is_still_recorded(w):
    r = w.client.post("/inf/v1/replies", json=["stop"], headers=w.headers("provider_events"))
    assert r.status_code == 201 and r.json()["held"] is True


def test_n3_the_same_body_without_a_request_id_is_the_same_reply(w):
    w.creator()
    body = {"channel": "email", "from_email": "creator@example.test", "text": "maybe"}
    a = w.ok(w.post("/replies", body, caller="provider_events"), 201)
    b = w.ok(w.post("/replies", body, caller="provider_events"), 201)
    assert a["reply_id"] == b["reply_id"] and len(w.svc.replies) == 1


# ------------------------------------------------------------------------------------------------ L-a

def test_la_the_confirmation_goes_to_the_address_on_the_record(w):
    w.creator(email="owner@corp.test", handles=(("instagram", "own"),))
    w.ok(w.link("owner+evil@corp.test"), 201)
    w.clock.advance(hours=25)
    w.ok(w.job("send-queue"))
    assert w.ports.email.sent[-1][1] == "owner@corp.test"         # 50312db: owner+evil@corp.test


# ------------------------------------------------------------------------------------------------ L-b

def test_lb_vault_references_have_no_digits(w):
    inf = w.creator()
    for ref, ok in (("vault:abcdefghijklmnopqrstuvwxyz", True), ("vault:0f0e0d0c-0b0a-4908-8706-a5a4a3a2a1a0", False),
                    ("vault:abcdefghijklmnopqrstuvwxy1", False)):
        assert (w.tax(inf, ref=ref).status_code == 201) is ok, ref


# ------------------------------------------------------------------------------------------------ L-c

def test_lc_a_tax_id_refusal_names_the_field_never_the_value(w):
    r = w.post("/influencers", {"request_id": rid(), "display_name": "Jo 123-45-6789",
                                "handles": [{"platform": "x", "handle": "jo"}], "evidence_ref": "ev-1"})
    assert r.status_code == 422 and r.json() == {"detail": "TAX_ID_REFUSED", "field": "display_name"}
    r = w.post("/influencers", {"request_id": rid(), "display_name": "Jo",
                                "handles": [{"platform": "x", "handle": "123456789"}], "evidence_ref": "ev-1"})
    assert r.json() == {"detail": "TAX_ID_REFUSED", "field": "handles[].handle"}
    r = w.application(**{"123-45-6789": "x"})
    assert r.status_code == 422 and "123" not in r.text


# ------------------------------------------------------------------------------------------------ L-d

def test_ld_a_suppressed_creator_can_apply_once_andre_approves_that_mail(w):
    p = w.prospect(email="back@example.test")
    w.ok(_reply(w, channel="email", from_email="back@example.test", text="no thanks"), 201)
    app = w.ok(w.link("back@example.test"), 201)
    assert app["confirmation_status"] == "awaiting_andre"          # 50312db: undeliverable, for ever
    conf = w.svc.confirmations[app["confirmation_id"]]
    url = f"/confirmations/{app['confirmation_id']}/approve"
    w.code(w.post(url, {"request_id": rid(), "content_sha256": conf["content_sha256"]}), 403,
           "ANDRE_APPROVAL_REQUIRED")
    w.code(w.post(url, {"request_id": rid(), "content_sha256": "0" * 64}, andre=True), 409, "CONTENT_HASH_MISMATCH")
    w.ok(w.post(url, {"request_id": rid(), "content_sha256": conf["content_sha256"]}, andre=True))
    w.clock.advance(hours=25)
    w.ok(w.job("send-queue"))
    assert w.ports.email.sent[-1][1] == "back@example.test"
    w.ok(w.submit(w.ok(w.confirm(app["confirmation_id"])), handles=()), 201)
    inf = w.ok(w.get(f"/influencers/{p['influencer_id']}"))
    assert inf["adult_attested"] is True and inf["suppressed"] is True   # outreach stays suppressed
    t = w.template()
    w.ok(w.post(f"/influencers/{p['influencer_id']}/first-name", {"request_id": rid(), "first_name": "Back"}))
    w.code(w.email(inf, t), 403, "SUPPRESSED")
