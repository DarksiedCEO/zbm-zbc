"""RR-F4 warm-transfer router: unit rules plus certification against the shared MVP fixture pool (F4, 200 cases per
seed). The key is the generator's planted answer; this test never imports the pool's oracle."""

import sys
from pathlib import Path

import pytest

import transfer_router as tr

POOLS = Path(__file__).resolve().parents[3] / "fixtures" / "mvp_pools"


def case(**kw):
    c = {"case_id": "xfer-1", "ring_mode": "simultaneous", "in_business_hours": True,
         "on_call_order": ["staff-a", "staff-b"], "staff_behavior": {"staff-a": "answers", "staff-b": "no_answer"},
         "ring_timeout_seconds": 20, "caller_number": "+16025550101"}
    c.update(kw)
    return c


def test_the_first_on_call_person_who_answers_is_connected_after_the_whisper():
    o = tr.resolve(case(), whisper="Transferring Ana, about a leak.")
    assert (o.path, o.connect_to, o.whisper_required, o.whisper_played, o.text_back_requested) == \
        ("connected", "staff-a", True, True, False)


def test_the_second_person_connects_when_the_first_does_not_answer():
    o = tr.resolve(case(staff_behavior={"staff-a": "no_answer", "staff-b": "answers"}), whisper="w")
    assert (o.path, o.connect_to) == ("connected", "staff-b")


def test_when_both_answer_the_on_call_order_decides():
    o = tr.resolve(case(staff_behavior={"staff-a": "answers", "staff-b": "answers"}), whisper="w")
    assert o.connect_to == "staff-a"


def test_someone_not_on_call_is_never_connected():
    o = tr.resolve(case(on_call_order=["staff-a"], staff_behavior={"staff-a": "no_answer", "staff-c": "answers"}))
    assert (o.path, o.connect_to) == ("fallback_message_and_text_back", None)


@pytest.mark.parametrize("kw, note", [({"in_business_hours": False}, "outside_hours"),
                                      ({"in_business_hours": None}, "outside_hours"),
                                      ({"in_business_hours": "yes"}, "outside_hours"),
                                      ({"on_call_order": []}, "nobody_on_call"),
                                      ({"on_call_order": None}, "nobody_on_call"),
                                      ({"on_call_order": [None, ""]}, "nobody_on_call")])
def test_outside_hours_or_nobody_on_call_goes_straight_to_the_fallback(kw, note):
    p = tr.plan(case(**kw))
    assert p.ring == ()
    o = tr.resolve(case(**kw))
    assert (o.path, o.text_back_requested, o.seconds_to_outcome) == ("fallback_message_and_text_back", True, 0)
    assert note in o.notes


def test_nobody_answering_ends_in_the_fallback_within_the_timeout():
    o = tr.resolve(case(staff_behavior={"staff-a": "no_answer", "staff-b": "no_answer"}))
    assert (o.path, o.text_back_requested, o.seconds_to_outcome) == ("fallback_message_and_text_back", True, 20)


@pytest.mark.parametrize("behaviour", [{}, None, "answers", {"staff-a": "ANSWERS"}, {"staff-a": True},
                                       {"staff-a": "voicemail"}])
def test_an_unreadable_outcome_is_no_answer_never_a_dropped_caller(behaviour):
    o = tr.resolve(case(staff_behavior=behaviour))
    assert o.path == "fallback_message_and_text_back" and o.text_back_requested


def test_a_late_answer_after_the_caller_hung_up_is_logged_and_texted():
    o = tr.resolve(case(staff_behavior={"staff-a": "answers_after_caller_hung_up", "staff-b": "no_answer"}))
    assert (o.path, o.connect_to, o.text_back_requested) == ("caller_hung_up_log_and_text_back", None, True)


def test_a_live_answer_beats_a_late_one():
    o = tr.resolve(case(staff_behavior={"staff-a": "answers_after_caller_hung_up", "staff-b": "answers"}),
                   whisper="w")
    assert (o.path, o.connect_to) == ("connected", "staff-b")


@pytest.mark.parametrize("raw, used, note", [(12, 12, None), (30, 30, None), (31, 30, "ring_timeout_cut_to_30"),
                                             (600, 30, "ring_timeout_cut_to_30"),
                                             (0, 30, "ring_timeout_unreadable_used_30"),
                                             (-1, 30, "ring_timeout_unreadable_used_30"),
                                             ("20", 30, "ring_timeout_unreadable_used_30"),
                                             (None, 30, "ring_timeout_unreadable_used_30"),
                                             (True, 30, "ring_timeout_unreadable_used_30")])
def test_the_whole_transfer_fits_in_30_seconds(raw, used, note):
    assert tr.plan(case(ring_timeout_seconds=raw)).timeout_seconds == used
    o = tr.resolve(case(ring_timeout_seconds=raw, staff_behavior={}))
    assert o.seconds_to_outcome == used <= 30
    assert (note in o.notes) if note else not any(n.startswith("ring_timeout") for n in o.notes)


def test_a_sequential_ring_mode_still_rings_everyone_at_once():
    p = tr.plan(case(ring_mode="sequential", on_call_order=["staff-a", "staff-b", "staff-c"]))
    assert p.ring == ("staff-a", "staff-b", "staff-c") and p.timeout_seconds <= 30
    assert "rang_simultaneously_per_policy" in tr.resolve(case(ring_mode="sequential"), whisper="w").notes


def test_a_duplicate_on_call_entry_rings_once():
    assert tr.plan(case(on_call_order=["staff-a", "staff-a", "staff-b"])).ring == ("staff-a", "staff-b")


@pytest.mark.parametrize("whisper", [None, "", "   "])
def test_a_missing_whisper_still_connects_and_is_recorded(whisper):
    o = tr.resolve(case(), whisper=whisper)
    assert (o.path, o.whisper_played) == ("connected", False)
    assert "whisper_missing" in o.notes


def test_the_whisper_line_states_only_what_it_is_given():
    assert tr.whisper_line("Ana", "a burst pipe", urgent=True) == \
        "Urgent: Transferring Ana, about a burst pipe. Press 1 to accept."
    assert "did not give a name" in tr.whisper_line(None, None)


def test_a_missing_case_id_refuses():
    with pytest.raises(ValueError):
        tr.plan(case(case_id=""))


# ---------------------------------------------------------------- certification on the shared pool
def _pool():
    if str(POOLS) not in sys.path:
        sys.path.insert(0, str(POOLS))
    from poolgen import frontdesk_sets
    return frontdesk_sets


def _grade(seed, resolve=tr.resolve):
    cases, key = _pool().generate_f4(seed)
    by_id = {k["case_id"]: k for k in key}
    assert len(by_id) == len(cases) == len(key)
    wrong = []
    for c in cases:
        k, o = by_id[c["case_id"]], resolve(c, whisper="Transferring a caller.")
        if (o.path, o.connect_to, o.whisper_required) != (k["path"], k["connect_to"], k["whisper_summary_required"]) \
                or o.seconds_to_outcome > k["max_seconds_to_connect_or_fallback"] \
                or (o.path != "connected") != o.text_back_requested:
            wrong.append((k["case"], k["path"], o.path, o.connect_to))
    return len(cases), wrong


@pytest.mark.parametrize("seed", ["dev-a", "dev-b", "dev-c", "f4-cert-1", "f4-cert-2", "f4-cert-3"])
def test_certification_on_the_shared_f4_pool(seed):
    n, wrong = _grade(seed)
    assert n == 200
    assert wrong == [], wrong[:5]


def test_the_certification_grader_catches_one_dropped_caller():
    def leaky(c, whisper=None):
        o = tr.resolve(c, whisper=whisper)
        if o.path == "fallback_message_and_text_back" and not getattr(leaky, "done", False):
            leaky.done = True
            return tr.TransferOutcome(o.case_id, "dropped", None, 31, False, False, False)
        return o
    assert _grade("dev-a", leaky)[1]
