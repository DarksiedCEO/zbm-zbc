"""RR-F5 review queue: what is queued (only replies the keeper holds for unrecognised_reply), what each item shows,
stale-ruling protection, the rulings, and that the keeper stays the only judge."""

import pytest

import consent_keeper as ck
import review_queue as rq

C, N = "client-a1", "+16025550101"


def opt_in(t=1, number=N, client=C):
    return {"kind": "opt_in", "t": t, "client_id": client, "number": number, "consent": "express_written",
            "source": "web_form"}


def sms(t, text, number=N, client=C):
    return {"kind": "inbound_sms", "t": t, "client_id": client, "number": number, "text": text}


def send(t, sid, number=N, client=C, mclass="informational"):
    return {"kind": "send_attempt", "t": t, "client_id": client, "number": number, "send_id": sid,
            "message_class": mclass}


def decide(events, number=N):
    t = max(e["t"] for e in events) + 10
    return ck.ConsentKeeper().replay(events + [send(t, "check", number)])[-1]


def test_a_free_text_reply_is_queued_with_its_text():
    h = [opt_in(), sms(5, "what time do you open?")]
    items = rq.pending(h)
    assert len(items) == 1 and items[0].client_id == C and items[0].number == N
    assert items[0].replies == ((5, "what time do you open?"),) and items[0].held_since == 5


@pytest.mark.parametrize("text", ["STOP", "unsubscribe", "stop texting me please"])
def test_an_opt_out_or_possible_opt_out_is_never_queued(text):
    assert rq.pending([opt_in(), sms(5, text)]) == []


@pytest.mark.parametrize("text", ["OK", "Thanks", "Yes", "HELP"])
def test_a_known_safe_reply_is_not_queued(text):
    assert rq.pending([opt_in(), sms(5, text)]) == []


def test_a_carrier_block_or_reassignment_is_never_queued():
    for e in ({"kind": "carrier_opt_out", "t": 6, "client_id": C, "number": N},
              {"kind": "number_reassigned", "t": 6, "client_id": C, "number": N}):
        assert rq.pending([opt_in(), sms(5, "what time do you open?"), e]) == []


def test_every_unread_reply_is_shown_oldest_first():
    h = [opt_in(), sms(5, "hmm"), sms(9, "what time do you open?")]
    assert rq.pending(h)[0].replies == ((5, "hmm"), (9, "what time do you open?"))


def test_not_an_opt_out_clears_the_hold_and_the_keeper_agrees():
    h = [opt_in(), sms(5, "what time do you open?")]
    assert decide(h).reason == "unrecognised_reply"
    item = rq.pending(h)[0]
    event, audit = rq.review_event(h, item.item_id, "Dana Ortiz", "not_an_opt_out")
    assert decide(h + [event]).decision == "allow" and rq.pending(h + [event]) == []
    assert (audit.reviewer, audit.ruling, audit.replies) == ("Dana Ortiz", "not_an_opt_out", item.replies)


def test_opt_out_ruling_holds_against_later_replies():
    h = [opt_in(), sms(5, "sounds good, see you tuesday")]
    item = rq.pending(h)[0]
    event, _ = rq.review_event(h, item.item_id, "Dana Ortiz", "opt_out")
    h2 = h + [event]
    assert decide(h2).reason == "opted_out" and rq.pending(h2) == []
    h3 = h2 + [sms(30, "hello again")]
    assert decide(h3).reason == "opted_out" and rq.pending(h3) == []


def test_a_reply_after_listing_makes_the_ruling_stale():
    h = [opt_in(), sms(5, "what time do you open?")]
    item = rq.pending(h)[0]
    with pytest.raises(rq.StaleItem):
        rq.review_event(h + [sms(8, "actually never text me again lol")], item.item_id, "Dana Ortiz", "not_an_opt_out")


def test_a_new_reply_after_a_review_needs_a_new_review_that_shows_only_the_new_reply():
    h = [opt_in(), sms(5, "what time do you open?")]
    e1, _ = rq.review_event(h, rq.pending(h)[0].item_id, "Dana Ortiz", "not_an_opt_out")
    h2 = h + [e1, sms(20, "ok but why")]
    items = rq.pending(h2)
    assert len(items) == 1 and items[0].replies == ((20, "ok but why"),)


def test_a_stronger_block_after_listing_makes_the_ruling_stale():
    h = [opt_in(), sms(5, "what time do you open?")]
    item = rq.pending(h)[0]
    with pytest.raises(rq.StaleItem):
        rq.review_event(h + [sms(8, "STOP")], item.item_id, "Dana Ortiz", "not_an_opt_out")


@pytest.mark.parametrize("reviewer", [None, "", "   ", "​", "ㅤ", 7, ["Dana"]])
def test_a_reviewer_needs_a_readable_name(reviewer):
    h = [opt_in(), sms(5, "what time do you open?")]
    with pytest.raises(ValueError):
        rq.review_event(h, rq.pending(h)[0].item_id, reviewer, "not_an_opt_out")


@pytest.mark.parametrize("ruling", ["ok", "", None, "NOT_AN_OPT_OUT", "clear"])
def test_only_the_two_rulings(ruling):
    h = [opt_in(), sms(5, "what time do you open?")]
    with pytest.raises(ValueError):
        rq.review_event(h, rq.pending(h)[0].item_id, "Dana Ortiz", ruling)


@pytest.mark.parametrize("item_id", ["", "nope", None, 3])
def test_an_unknown_item_is_stale(item_id):
    h = [opt_in(), sms(5, "what time do you open?")]
    with pytest.raises(ValueError):
        rq.review_event(h, item_id, "Dana Ortiz", "not_an_opt_out")


def test_records_are_kept_apart_by_client_and_number():
    h = [opt_in(), opt_in(number="+16025550102"), sms(5, "what time do you open?"), sms(6, "hi?", number="+16025550102"),
         sms(7, "hello", client="client-b2")]
    items = rq.pending(h)
    assert [(i.client_id, i.number) for i in items] == [(C, N), (C, "+16025550102"), ("client-b2", N)]
    e, _ = rq.review_event(h, items[0].item_id, "Dana Ortiz", "not_an_opt_out")
    assert [(i.client_id, i.number) for i in rq.pending(h + [e])] == [(C, "+16025550102"), ("client-b2", N)]


def test_number_formats_are_one_record():
    h = [opt_in(), sms(5, "what time do you open?", number="(602) 555-0101"), sms(6, "hello?", number="602.555.0101")]
    items = rq.pending(h)
    assert len(items) == 1 and len(items[0].replies) == 2


@pytest.mark.parametrize("bad", [None, {"kind": "inbound_sms"}, [{"kind": "x", "t": 1}],
                                 [{"kind": "inbound_sms", "t": 1, "client_id": C, "number": "nope", "text": "x"}]])
def test_a_malformed_history_refuses(bad):
    with pytest.raises(ValueError):
        rq.pending(bad)


def test_a_history_send_id_that_collides_with_a_probe_refuses():
    h = [opt_in(), sms(5, "what time do you open?"), send(6, "review-queue-probe-0")]
    with pytest.raises(ValueError):
        rq.pending(h)


def test_new_written_consent_already_cleared_it_so_nothing_is_queued():
    assert rq.pending([opt_in(), sms(5, "what time do you open?"), opt_in(t=9)]) == []


def test_the_queue_never_changes_the_history():
    h = [opt_in(), sms(5, "what time do you open?")]
    snapshot = [dict(e) for e in h]
    item = rq.pending(h)[0]
    rq.review_event(h, item.item_id, "Dana Ortiz", "not_an_opt_out")
    assert h == snapshot


def test_a_reply_the_keeper_cannot_read_goes_to_a_person_even_if_it_sounds_like_an_opt_out():
    items = rq.pending([opt_in(), sms(5, "Please discontinue these texts")])
    assert len(items) == 1 and items[0].replies == ((5, "Please discontinue these texts"),)


def test_a_reply_recorded_after_the_review_with_an_earlier_time_is_not_cleared():
    h = [opt_in(), sms(5, "what time do you open?")]
    event, audit = rq.review_event(h, rq.pending(h)[0].item_id, "Dana Ortiz", "not_an_opt_out")
    late = sms(5, "Please discontinue these texts")
    assert decide(h + [event, late]).reason == "unrecognised_reply"
    items = rq.pending(h + [event, late])
    assert len(items) == 1 and (5, "Please discontinue these texts") in items[0].replies
    assert audit.replies == ((5, "what time do you open?"),)


def test_any_reply_text_has_a_digest_and_is_queued():
    h = [opt_in(), sms(5, "hi \ud83d"), sms(6, "hello", number="+16025550102"), opt_in(number="+16025550102")]
    assert len(rq.pending([opt_in(), sms(5, "hi \ud83d")])) == 1


class Shifty(str):
    def strip(self, *a):
        raise RuntimeError("boom")

    def __iter__(self):
        raise RuntimeError("boom")


@pytest.mark.parametrize("field", ["reviewer", "ruling", "item_id"])
def test_hostile_string_subclasses_raise_only_value_error(field):
    h = [opt_in(), sms(5, "what time do you open?")]
    args = {"item_id": rq.pending(h)[0].item_id, "reviewer": "Dana Ortiz", "ruling": "not_an_opt_out"}
    args[field] = Shifty(args[field])
    with pytest.raises(ValueError):
        rq.review_event(h, args["item_id"], args["reviewer"], args["ruling"])


def test_opt_out_ruling_is_lifted_only_by_new_written_consent():
    h = [opt_in(), sms(5, "sounds good, see you tuesday")]
    event, _ = rq.review_event(h, rq.pending(h)[0].item_id, "Dana Ortiz", "opt_out")
    assert decide(h + [event, sms(20, "hello")]).reason == "opted_out"
    assert decide(h + [event, opt_in(t=30)]).decision == "allow"


@pytest.mark.parametrize("text", ["śtp", "st́p", "śtp", "ѕtp", "ѕtор", "sτop", "stοp", "S̈TOP"])
def test_an_accented_or_look_alike_stop_typo_is_never_queued(text):
    h = [opt_in(), sms(5, text)]
    assert rq.pending(h) == [] and decide(h).reason in ("possible_opt_out", "opted_out")


def test_a_history_at_the_last_time_is_still_listed_but_cannot_be_reviewed():
    h = [opt_in(), sms(ck.T_LIMIT, "what time do you open?")]
    items = rq.pending(h)
    assert len(items) == 1 and items[0].replies == ((ck.T_LIMIT, "what time do you open?"),)
    with pytest.raises(ValueError):
        rq.review_event(h, items[0].item_id, "Dana Ortiz", "not_an_opt_out")


def test_the_probe_sees_a_reply_at_the_same_time_as_a_history_send():
    h = [opt_in(), send(5, "s1"), sms(5, "what time do you open?")]
    assert len(rq.pending(h)) == 1
