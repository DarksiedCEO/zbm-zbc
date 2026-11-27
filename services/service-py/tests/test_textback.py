"""RR-F2 missed-call text-back: unit rules plus certification against the shared MVP fixture pool (F2, 300 missed
calls per seed). The key is the generator's planted answer; this test never imports the pool's oracle."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import consent_keeper as ck
import textback as tb

POOLS = Path(__file__).resolve().parents[3] / "fixtures" / "mvp_pools"


def call(**kw):
    c = {"missed_call_id": "mc-1", "caller_id": "+16025550101", "line_type": "mobile", "status": "busy",
         "recipient_local_time": "12:00", "recipient_time_zone": "America/Phoenix", "number_opted_out": False,
         "seconds_since_last_text_to_number": None}
    c.update(kw)
    return c


def act(**kw):
    return tb.decide(call(**kw)).action


def test_an_ordinary_missed_call_is_texted_now_with_a_30_second_deadline():
    d = tb.decide(call())
    assert (d.action, d.deadline_seconds, d.message_class) == ("send_now", 30, "informational")


@pytest.mark.parametrize("line", ["mobile", "voip"])
def test_mobile_and_voip_are_textable(line):
    assert act(line_type=line) == "send_now"


@pytest.mark.parametrize("kw, reason", [
    ({"line_type": "landline"}, "landline"),
    ({"line_type": None}, "unknown_line_type"),
    ({"line_type": "pager"}, "unknown_line_type"),
    ({"caller_id": None}, "blocked_caller_id"),
    ({"caller_id": "anonymous"}, "unreadable_caller_id"),
    ({"caller_id": "555-0101"}, "unreadable_caller_id"),
    ({"number_opted_out": True}, "opted_out"),
    ({"number_opted_out": None}, "consent_unknown"),
    ({"number_opted_out": "no"}, "consent_unknown"),
    ({"seconds_since_last_text_to_number": 299}, "texted_within_5_minutes"),
    ({"seconds_since_last_text_to_number": 0}, "texted_within_5_minutes"),
    ({"seconds_since_last_text_to_number": -5}, "unreadable_last_text_time"),
    ({"seconds_since_last_text_to_number": "300"}, "unreadable_last_text_time"),
    ({"recipient_local_time": "25:00"}, "unknown_local_time"),
    ({"recipient_local_time": "8:00"}, "unknown_local_time"),
])
def test_do_not_send(kw, reason):
    d = tb.decide(call(**kw))
    assert (d.action, d.reason, d.deadline_seconds) == ("do_not_send", reason, None)


def test_exactly_five_minutes_after_the_last_text_may_text_again():
    assert act(seconds_since_last_text_to_number=300) == "send_now"


@pytest.mark.parametrize("hhmm, action", [("07:59", "defer_until_0800"), ("08:00", "send_now"),
                                           ("20:59", "send_now"), ("21:00", "defer_until_0800"),
                                           ("00:00", "defer_until_0800"), ("23:59", "defer_until_0800")])
def test_quiet_hours_are_the_recipients(hhmm, action):
    assert act(recipient_local_time=hhmm) == action


def test_the_recipients_zone_decides_when_only_the_clock_is_known():
    now = datetime(2026, 11, 27, 4, 30, tzinfo=timezone.utc)        # 20:30 in Los Angeles, 23:30 in New York
    assert tb.decide(call(recipient_local_time=None, recipient_time_zone="America/Los_Angeles"), now=now).action == \
        "send_now"
    assert tb.decide(call(recipient_local_time=None, recipient_time_zone="America/New_York"), now=now).action == \
        "defer_until_0800"


@pytest.mark.parametrize("tz", [None, "", "Mars/Olympus", "America/../etc"])
def test_an_unknown_zone_is_never_guessed(tz):
    now = datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc)
    d = tb.decide(call(recipient_local_time=None, recipient_time_zone=tz), now=now)
    assert (d.action, d.reason) == ("do_not_send", "unknown_local_time")


def test_a_naive_clock_is_not_trusted():
    d = tb.decide(call(recipient_local_time=None), now=datetime(2026, 11, 27, 19, 0))
    assert d.action == "do_not_send"


def test_the_consent_keeper_answer_wins_over_the_record_flag():
    assert tb.decide(call(number_opted_out=False), opted_out=lambda n: True).action == "do_not_send"
    assert tb.decide(call(number_opted_out=True), opted_out=lambda n: False).action == "send_now"
    assert tb.decide(call(), opted_out=lambda n: None).reason == "consent_unknown"


def test_with_the_real_consent_keeper():
    keeper_events = [{"kind": "inbound_sms", "t": 1, "client_id": "c", "number": "+16025550101", "text": "STOP"},
                     {"kind": "send_attempt", "t": 2, "client_id": "c", "number": "+16025550101",
                      "send_id": "probe", "message_class": "informational"}]
    blocked = ck.ConsentKeeper().replay(keeper_events)[0].decision == "block"
    assert tb.decide(call(), opted_out=lambda n: blocked).action == "do_not_send"


def test_the_keeper_is_asked_with_the_normalised_number():
    seen = []
    tb.decide(call(caller_id="(602) 555-0101"), opted_out=lambda n: seen.append(n) or False)
    assert seen == ["+16025550101"]


def test_the_text_is_informational_and_says_how_to_opt_out():
    d = tb.decide(call(), business_name="Shop (Fixture)", callback_number="+16025550111")
    assert "STOP" in d.text and "+16025550111" in d.text
    for word in ("%", "off", "deal", "offer", "sale", "free", "discount", "http"):
        assert word not in d.text.lower()
    es = tb.decide(call(), business_name="Shop (Fixture)", callback_number="+16025550111", spanish=True)
    assert "ALTO" in es.text


def test_a_missing_id_refuses():
    with pytest.raises(ValueError):
        tb.decide(call(missed_call_id=None))


# ---------------------------------------------------------------- certification on the shared pool
def _pool():
    if str(POOLS) not in sys.path:
        sys.path.insert(0, str(POOLS))
    from poolgen import frontdesk_sets
    return frontdesk_sets


def _grade(seed, decide=tb.decide):
    calls, key = _pool().generate_f2(seed)
    by_id = {k["missed_call_id"]: k for k in key}
    assert len(by_id) == len(calls) == len(key)
    wrong = []
    for c in calls:
        k, d = by_id[c["missed_call_id"]], decide(c)
        if (d.action, d.deadline_seconds, d.message_class) != (k["action"], k["max_latency_seconds"],
                                                               k["message_class"]):
            wrong.append((k["case"], k["action"], d.action, d.reason))
    return len(calls), wrong


@pytest.mark.parametrize("seed", ["dev-a", "dev-b", "dev-c", "f2-cert-1", "f2-cert-2", "f2-cert-3"])
def test_certification_on_the_shared_f2_pool(seed):
    n, wrong = _grade(seed)
    assert n == 300
    assert wrong == [], wrong[:5]


def test_the_certification_grader_catches_one_wrong_text():
    def leaky(c):
        d = tb.decide(c)
        if d.reason == "opted_out" and not getattr(leaky, "done", False):
            leaky.done = True
            return tb.TextBackDecision(d.missed_call_id, "send_now", "ok", 30)
        return d
    assert _grade("dev-a", leaky)[1]
