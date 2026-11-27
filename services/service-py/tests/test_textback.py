"""RR-F2 missed-call text-back: unit rules plus certification against the shared MVP fixture pool (F2, 300 missed
calls per seed). The key is the generator's planted answer; this test never imports the pool's oracle."""

import sys
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo
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


# ---------------------------------------------------------------- AEGIS round 1 (Inquisitor, 81e0dc7f)
@pytest.mark.parametrize("tz", [None, "", "Mars/Olympus"])
def test_a_local_time_string_never_stands_in_for_a_readable_zone(tz):
    d = tb.decide(call(recipient_local_time="12:00", recipient_time_zone=tz))
    assert (d.action, d.reason) == ("do_not_send", "unknown_local_time")


def test_a_real_clock_beats_the_records_local_time():
    now = datetime(2026, 11, 27, 4, 30, tzinfo=timezone.utc)        # 23:30 in New York
    d = tb.decide(call(recipient_local_time="12:00", recipient_time_zone="America/New_York"), now=now)
    assert d.action == "defer_until_0800"


@pytest.mark.parametrize("hms, action", [("20:59:29", "send_now"), ("20:59:30", "defer_until_0800"),
                                          ("20:59:59", "defer_until_0800"), ("07:59:59", "defer_until_0800"),
                                          ("08:00:00", "send_now")])
def test_with_a_clock_the_30_second_deadline_must_land_before_21_00(hms, action):
    h, m, sec = map(int, hms.split(":"))
    now = datetime(2026, 11, 27, h, m, sec, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert tb.decide(call(recipient_time_zone="America/Los_Angeles"), now=now).action == action


def test_a_clock_with_no_utc_offset_is_not_trusted():
    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

        def dst(self, dt):
            return None
    d = tb.decide(call(), now=datetime(2026, 11, 27, 19, 0, tzinfo=NoOffset()))
    assert (d.action, d.reason) == ("do_not_send", "unknown_local_time")


def test_a_record_without_the_last_text_field_fails_closed():
    c = call()
    del c["seconds_since_last_text_to_number"]
    assert tb.decide(c).reason == "unreadable_last_text_time"


def test_the_gate_never_double_texts_a_number_inside_5_minutes():
    gate = tb.TextBackGate()
    t0 = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)          # 11:00 in Phoenix
    acts = [gate.decide(call(missed_call_id=f"mc-{i}"), now=t0 + timedelta(seconds=s)).action
            for i, s in enumerate([0, 10, 299])]
    assert acts == ["send_now", "do_not_send", "do_not_send"]
    assert gate.decide(call(missed_call_id="mc-9"), now=t0 + timedelta(seconds=300)).action == "send_now"
    assert gate.decide(call(missed_call_id="mc-x", caller_id="+16025550102"), now=t0).action == "send_now"


def test_the_gate_reads_one_number_however_it_is_written():
    gate = tb.TextBackGate()
    t0 = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    gate.decide(call(), now=t0)
    assert gate.decide(call(missed_call_id="mc-2", caller_id="(602) 555-0101"), now=t0).reason == \
        "texted_within_5_minutes"


def test_the_gate_without_an_aware_clock_never_sends_and_never_raises():
    gate = tb.TextBackGate()
    for now in (datetime(2026, 11, 27, 18, 0), "2026-11-27T18:00", None):
        d = gate.decide(call(), now=now)
        assert (d.action, d.reason) == ("do_not_send", "unknown_local_time")
    assert gate._last == {}
    with pytest.raises(ValueError):
        gate.decide(call(missed_call_id=None), now=None)


def test_a_deferral_with_a_clock_names_the_next_0800_in_the_recipients_zone():
    la = ZoneInfo("America/Los_Angeles")
    late = tb.decide(call(recipient_time_zone="America/Los_Angeles"), now=datetime(2026, 11, 27, 22, 15, tzinfo=la))
    early = tb.decide(call(recipient_time_zone="America/Los_Angeles"), now=datetime(2026, 11, 27, 6, 0, tzinfo=la))
    assert late.not_before == datetime(2026, 11, 28, 8, 0, tzinfo=la)
    assert early.not_before == datetime(2026, 11, 27, 8, 0, tzinfo=la)


@pytest.mark.parametrize("name", ["50% OFF today https://deal.example", "Shop $99", "www.shop.test",
                                  "FREE estimates", "Shop\nReply YES", "x" * 41])
def test_a_business_name_with_an_offer_or_a_link_is_left_out(name):
    d = tb.decide(call(), business_name=name, callback_number="+16025550111")
    assert d.text.startswith("Sorry we missed your call.") and "STOP" in d.text


def test_every_text_says_how_to_opt_out_even_with_nothing_supplied():
    for action_call in (call(), call(recipient_local_time="22:00")):
        d = tb.decide(action_call)
        assert d.text and "STOP" in d.text and "http" not in d.text
    assert "ALTO" in tb.decide(call(), spanish=True).text


def test_a_bad_callback_number_is_left_out_of_the_text():
    assert "call us back" not in tb.decide(call(), business_name="Shop", callback_number="call me").text


def _clock_mode(c):
    """The same record, decided from a real clock in its zone instead of its HH:MM string."""
    h, m = map(int, c["recipient_local_time"].split(":"))
    local = datetime(2026, 11, 27, h, m, tzinfo=ZoneInfo(c["recipient_time_zone"]))
    c = dict(c, recipient_local_time=None)
    return c, local.astimezone(timezone.utc)


@pytest.mark.parametrize("seed", ["dev-a", "f2-cert-1"])
def test_certification_holds_on_the_clock_path_and_grades_reason_and_text(seed):
    reasons = {"send": "ok", "voip_textable": "ok", "landline": "landline", "blocked_caller_id": "blocked_caller_id",
               "opted_out": "opted_out", "repeat_within_5_min": "texted_within_5_minutes",
               "quiet_hours": "quiet_hours"}
    calls, key = _pool().generate_f2(seed)
    by_id = {k["missed_call_id"]: k for k in key}
    wrong = []
    for c in calls:
        k = by_id[c["missed_call_id"]]
        clock_call, now = _clock_mode(c)
        for d in (tb.decide(c), tb.decide(clock_call, now=now)):
            if (d.action, d.reason) != (k["action"], reasons[k["case"]]):
                wrong.append((k["case"], d.action, d.reason))
            if d.action != "do_not_send" and not (d.text and "STOP" in d.text):
                wrong.append((k["case"], "text", d.text))
    assert wrong == [], wrong[:5]


# ---------------------------------------------------------------- AEGIS round 2 (Inquisitor, 9fcd033b)
def test_the_gate_compares_in_utc_whatever_zone_the_clocks_carry():
    gate = tb.TextBackGate()
    ny, utc = ZoneInfo("America/New_York"), timezone.utc
    t0 = datetime(2026, 11, 27, 15, 0, tzinfo=utc)              # 10:00 New York
    gate.decide(call(recipient_time_zone="America/New_York", recipient_local_time=None), now=t0)
    t1 = (t0 + timedelta(seconds=180)).astimezone(ny)
    d = gate.decide(call(missed_call_id="mc-2", recipient_time_zone="America/New_York", recipient_local_time=None),
                    now=t1)
    assert d.reason == "texted_within_5_minutes"


def test_the_gate_measures_real_seconds_across_a_dst_jump():
    """The Inquisitor's repro: a clock in New York across spring-forward, 01:58 EST then 03:01 EDT (180 real s), for a
    caller in Tokyo where it is mid-afternoon. On the wall clock that is 63 minutes; in real time it is 3."""
    ny = ZoneInfo("America/New_York")
    gate = tb.TextBackGate()
    c = call(recipient_time_zone="Asia/Tokyo", recipient_local_time=None)
    assert gate.decide(c, now=datetime(2027, 3, 14, 1, 58, tzinfo=ny)).action == "send_now"
    second = gate.decide(dict(c, missed_call_id="mc-2"), now=datetime(2027, 3, 14, 3, 1, tzinfo=ny))
    assert (second.action, second.reason) == ("do_not_send", "texted_within_5_minutes")


@pytest.mark.parametrize("name", ["acme.com", "bit.ly", "FR\u0415\u0415 Estimates", "Daily Deals", "Discounts Galore",
                                  "Ofertas Hoy", "Cuts 19.99 USD", "Reply YES to join", "Call 0044 20 7946 0958",
                                  "Visit shop", "Save Big"])
def test_round_2_names_that_must_be_left_out(name):
    d = tb.decide(call(), business_name=name, callback_number="+16025550111")
    assert d.text.startswith("Sorry we missed your call.")


@pytest.mark.parametrize("name", ["Desert Sky Plumbing", "Smith & Sons", "Studio 54", "Caf\u00e9 Rosa",
                                  "O'Brien Dental (Fixture)"])
def test_ordinary_business_names_are_kept(name):
    assert tb.decide(call(), business_name=name).text.startswith(name + ":")


@pytest.mark.parametrize("tz", ["Factory", "localtime", "UTC", "Etc/GMT+7", "EST", "posixrules"])
def test_a_zone_that_is_not_a_place_is_never_used(tz):
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    assert tb.decide(call(recipient_time_zone=tz, recipient_local_time=None), now=now).reason == "unknown_local_time"
    assert tb.decide(call(recipient_time_zone=tz)).reason == "unknown_local_time"


def test_an_unconvertible_clock_fails_closed_with_a_reason():
    d = tb.decide(call(recipient_time_zone="Pacific/Kiritimati", recipient_local_time=None),
                  now=datetime.max.replace(tzinfo=timezone.utc))
    assert (d.action, d.reason) == ("do_not_send", "unknown_local_time")

    class Broken(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("broken")

        def dst(self, dt):
            return None
    d = tb.decide(call(recipient_local_time=None), now=datetime(2026, 11, 27, 12, 0, tzinfo=Broken()))
    assert d.reason == "unknown_local_time"


# ---------------------------------------------------------------- AEGIS round 3 (Inquisitor, 437d2871)
@pytest.mark.parametrize("name", ["Cheapest Plumbing", "Discounted Dental", "Freebies Inc", "BOGO Tacos",
                                  "Clearance Center", "Corte Gr\u00e1tis", "Fr\u0113\u0113 Estimates",
                                  "\u0191ree Estimates", "Baratos Auto", "Limpieza Gratuita", "Rebajas Hoy",
                                  "Half Price Cuts", "Cuts 9.99", "50 0FF", "Fr33 Quotes", "Respond Now",
                                  "Opt In Today", "Responda Ya", "Envie SI", "Llame Ya", "Call Now"])
def test_round_3_names_that_must_be_left_out(name):
    d = tb.decide(call(), business_name=name, callback_number="+16025550111")
    assert d.text.startswith("Sorry we missed your call."), d.text


def test_a_consent_keeper_that_raises_is_unknown_consent():
    def broken(number):
        raise RuntimeError("store down")
    assert tb.decide(call(), opted_out=broken).reason == "consent_unknown"


def test_a_deferral_past_the_last_clock_date_never_raises():
    from zoneinfo import ZoneInfo
    now = datetime(9999, 12, 31, 23, 0, tzinfo=ZoneInfo("America/Phoenix"))
    d = tb.decide(call(), now=now)
    assert (d.action, d.reason) == ("do_not_send", "unknown_local_time")
