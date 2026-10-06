"""Replies and holds (ADR 0015 decision 12; sales-py's fail-closed reply design): ANY reply on ANY channel holds every
further automatic outreach to that influencer until Andre decides; opt-out wording also suppresses at once."""

from __future__ import annotations

import pytest

from helpers import rid


def reply(h, text, channel="email", **kw):
    return h.post("/replies", {"request_id": rid(), "channel": channel, "text": text, **kw}, caller="provider_events")


def _dm(h, inf):
    dr = h.ok(h.post("/dm-drafts", {"request_id": rid(), "influencer_id": inf["influencer_id"],
                                    "platform": "instagram", "brand": "zbm", "text": "Hi there"},
                     caller="influencer_agent"), 201)
    return dr


@pytest.mark.parametrize("text", ["yes", "Interested!", "ok", "sounds good, call me", "", "🙂", "yes stop"])
def test_any_reply_holds_email_and_dms(w, text):
    inf = w.creator()
    t = w.template()
    queued = w.ok(w.email(inf, t), 201)
    dr = _dm(w, inf)
    out = w.ok(reply(w, text, from_email="creator@example.test"), 201)
    assert out["held"] is True and out["influencer_id"] == inf["influencer_id"]
    assert w.svc.messages[queued["message_id"]]["status"] == "cancelled"
    assert w.svc.messages[queued["message_id"]]["reason"] in ("REPLY_HOLD", "SUPPRESSED")
    w.code(w.email(inf, t), 403, "REPLY_HOLD" if not out["suppressed"] else "SUPPRESSED")
    r = w.post(f"/dm-drafts/{dr['draft_id']}/approve", {"request_id": rid(), "content_sha256": dr["content_sha256"]},
               andre=True)
    assert r.status_code == 403


@pytest.mark.parametrize("channel", ["instagram", "tiktok", "x", "youtube"])
def test_a_dm_reply_holds_too(w, channel):
    inf = w.creator(handles=((channel, "@who"),))
    t = w.template()
    out = w.ok(reply(w, "thanks", channel=channel, from_handle="@WHO"), 201)
    assert out["held"] is True and out["influencer_id"] == inf["influencer_id"]
    w.code(w.email(inf, t), 403, "REPLY_HOLD")


def test_only_an_exact_machine_auto_reply_does_not_hold(w):
    inf = w.creator()
    t = w.template()
    out = w.ok(reply(w, "  Out of office  ", from_email="creator@example.test"), 201)
    assert out["held"] is False and out["class"] == "out_of_office"
    w.ok(w.email(inf, t), 201)
    out = w.ok(reply(w, "Out of office. Stop emailing me", from_email="creator@example.test"), 201)
    assert out["held"] is True and out["suppressed"] is True
    # on a DM the same text is a person's words: it holds
    inf2 = w.creator(email="b@example.test", handles=(("x", "@bb"),))
    assert w.ok(reply(w, "out of office", channel="x", from_handle="@bb"), 201)["held"] is True
    w.code(w.email(inf2, t), 403, "REPLY_HOLD")


@pytest.mark.parametrize("text,channel", [("unsubscribe", "email"), ("STOP", "email"), ("not interested", "email"),
                                          ("please remove me", "instagram"), ("S.T.O.P", "tiktok"),
                                          ("leave me alone", "x")])
def test_opt_out_wording_suppresses_every_address_and_handle(w, text, channel):
    inf = w.creator(handles=(("instagram", "@c1"), ("tiktok", "@c1"), ("x", "@c1")))
    kw = {"from_email": "creator@example.test"} if channel == "email" else {"from_handle": "@c1"}
    out = w.ok(reply(w, text, channel=channel, **kw), 201)
    assert out["suppressed"] is True
    for hh in [inf["email_hash"]] + [x["handle_hash"] for x in inf["handles"]]:
        assert hh in w.svc.suppression


def test_reply_text_is_never_stored(w):
    w.creator()
    w.ok(reply(w, "my private words xyz", from_email="creator@example.test"), 201)
    assert "my private words" not in str(w.svc.log.records) and "my private words" not in str(w.ledger.events)


def test_an_unresolved_reply_holds_whoever_the_address_turns_out_to_be(w):
    out = w.ok(reply(w, "who is this?", from_email="later@example.test"), 201)
    assert out["influencer_id"] is None and out["held"] is True
    inf = w.creator(email="later@example.test", handles=(("x", "@later"),))
    t = w.template()
    w.code(w.email(inf, t), 403, "REPLY_HOLD")


def test_a_reply_with_no_sender_is_still_recorded_for_andre(h):
    out = h.ok(reply(h, "hi"), 201)
    assert out["held"] is True and out["influencer_id"] is None
    out = h.ok(reply(h, "hi", from_handle="@x"), 201)                 # a handle on an email reply: ignored
    assert out["ignored"] == ["from_handle"] and h.svc.holds[out["hold_id"]]["unresolved"] is True


def test_andre_decides_a_hold(w):
    inf = w.creator()
    t = w.template()
    out = w.ok(reply(w, "maybe", from_email="creator@example.test"), 201)
    url = f"/holds/{out['hold_id']}/decision"
    w.code(w.post(url, {"request_id": rid(), "decision": "continue"}), 403, "ANDRE_APPROVAL_REQUIRED")
    w.code(w.post(url, {"request_id": rid(), "decision": "continue"}, caller="influencer_agent", andre=True), 403,
           "CALLER_NOT_ALLOWED")
    w.ok(w.post(url, {"request_id": rid(), "decision": "continue"}, andre=True))
    w.ok(w.email(inf, t), 201)
    w.code(w.post(url, {"request_id": rid(), "decision": "opt_out"}, andre=True), 409, "HOLD_NOT_ACTIVE")
    out2 = w.ok(reply(w, "hmm", from_email="creator@example.test"), 201)
    w.ok(w.post(f"/holds/{out2['hold_id']}/decision", {"request_id": rid(), "decision": "opt_out"}, andre=True))
    assert w.ok(w.get(f"/influencers/{inf['influencer_id']}"))["suppressed"] is True
    assert {e["_payload"]["decision"] for e in w.ledger.of_type("hold_decided")} == {"continue", "opt_out"}


def test_continue_never_lifts_a_suppression(w):
    inf = w.creator()
    t = w.template()
    out = w.ok(reply(w, "unsubscribe", from_email="creator@example.test"), 201)
    w.ok(w.post(f"/holds/{out['hold_id']}/decision", {"request_id": rid(), "decision": "continue"}, andre=True))
    w.code(w.email(inf, t), 403, "SUPPRESSED")


def test_a_held_message_queued_before_the_hold_is_cancelled_at_send_time(w):
    inf = w.creator()
    t = w.template()
    m = w.ok(w.email(inf, t), 201)
    w.svc.holds["x"] = {"hold_id": "x", "influencer_id": inf["influencer_id"], "hashes": [], "status": "active"}
    w.svc.messages[m["message_id"]]["status"] = "queued"
    out = w.ok(w.job("send-queue"))
    assert out["cancelled"] == 1 and w.svc.messages[m["message_id"]]["reason"] == "REPLY_HOLD"
    assert not w.ports.email.sent
