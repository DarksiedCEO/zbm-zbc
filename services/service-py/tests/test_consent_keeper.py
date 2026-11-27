"""RR-F5 consent keeper: unit rules plus certification against the shared MVP fixture pool (fixtures/mvp_pools F5).

The fixture answers are the generator's planted key, re-derived independently by the pool's own oracle; this test
never imports the oracle. Decisions must match the key exactly. Reasons must match too, with one disclosed
exception: the keeper reads a STOP typo with doubled letters or a digit ("stopp", "st0p") or a STOP with a trailing
word ("Stop pls") as an exact opt-out (``opted_out``), where the key labels it ``possible_opt_out``. Both block.
"""

import collections
import random
import sys
from pathlib import Path

import pytest

import consent_keeper as ck

POOLS = Path(__file__).resolve().parents[3] / "fixtures" / "mvp_pools"
OPT_OUT_PAIR = {"opted_out", "possible_opt_out"}


def ev(kind, t, client="client-a", number="+16025550101", **kw):
    e = {"kind": kind, "t": t, "client_id": client, "number": number}
    e.update(kw)
    return e


def send(t, sid="s1", mclass="informational", **kw):
    return ev("send_attempt", t, send_id=sid, message_class=mclass, **kw)


def decide(events):
    return {(d.send_id, d.t): (d.decision, d.reason) for d in ck.ConsentKeeper().replay(events)}


def one(events):
    ds = ck.ConsentKeeper().replay(events)
    assert len(ds) == 1
    return ds[0].decision, ds[0].reason


# ---------------------------------------------------------------- rules
def test_marketing_needs_written_consent_and_informational_does_not():
    assert one([send(10, mclass="marketing")]) == ("block", "no_written_consent")
    assert one([send(10)]) == ("allow", None)
    assert one([ev("opt_in", 1, consent="express_written", source="web_form"), send(10, mclass="marketing")]) == \
        ("allow", None)


def test_weaker_consent_is_not_written_consent():
    assert one([ev("opt_in", 1, consent="implied", source="web_form"), send(10, mclass="marketing")]) == \
        ("block", "no_written_consent")


@pytest.mark.parametrize("text", ["STOP", "stop", "Stop.", "STOP!", "UNSUBSCRIBE", "CANCEL", "END", "QUIT",
                                  "REVOKE", "OPT OUT", "stop texting me", "ALTO", "alto", "BAJA", "S T O P"])
def test_stop_words_block_every_later_send(text):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text), send(6)]) == \
        ("block", "opted_out")


@pytest.mark.parametrize("text", ["STPO", "SOTP"])
def test_stop_typos_block_as_possible_opt_out(text):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text), send(6)]) == \
        ("block", "possible_opt_out")


@pytest.mark.parametrize("text", ["HELP", "help", "INFO", "Spot", "Post", "Stay", "STAR", "Thanks", "OK"])
def test_help_and_look_alike_words_change_nothing(text):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
                send(6, mclass="marketing")]) == ("allow", None)


@pytest.mark.parametrize("word", ["START", "UNSTOP", "YES", "start"])
def test_start_keyword_never_restores(word):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
                ev("inbound_sms", 9, text=word), send(20)]) == ("block", "start_keyword_does_not_restore")


def test_new_written_consent_after_the_stop_restores():
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
                ev("opt_in", 9, consent="express_written"), send(20, mclass="marketing")]) == ("allow", None)


def test_consent_given_before_the_stop_does_not_restore():
    assert one([ev("inbound_sms", 5, text="STOP"), ev("opt_in", 1, consent="express_written"), send(20)]) == \
        ("block", "opted_out")


def test_opt_out_takes_effect_on_the_next_second():
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"), send(6)])[0] == "block"


def test_carrier_opt_out_is_never_lifted_by_our_records():
    assert one([ev("opt_in", 1, consent="express_written"), ev("carrier_opt_out", 5),
                ev("opt_in", 9, consent="express_written"), send(20)]) == ("block", "carrier_opt_out")
    assert one([ev("carrier_opt_out", 5), ev("inbound_sms", 7, text="STOP"), send(20)]) == \
        ("block", "carrier_opt_out")


def test_reassigned_number_kills_consent_until_new_written_consent():
    base = [ev("opt_in", 1, consent="express_written"), ev("number_reassigned", 5)]
    assert one(base + [send(20)]) == ("block", "number_reassigned")
    assert one(base + [ev("opt_in", 9, consent="express_written"), send(20, mclass="marketing")]) == ("allow", None)


def test_stop_and_send_in_the_same_instant_blocks_in_any_input_order():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"), send(5)]
    for perm in ([0, 1, 2], [0, 2, 1], [2, 1, 0], [2, 0, 1], [1, 2, 0], [1, 0, 2]):
        assert one([events[k] for k in perm]) == ("block", "opted_out")


def test_opt_in_and_stop_in_the_same_instant_fails_toward_suppression():
    assert one([ev("inbound_sms", 5, text="STOP"), ev("opt_in", 5, consent="express_written"), send(6)]) == \
        ("block", "opted_out")


def test_consent_is_held_per_client_and_per_number():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
              send(6, sid="other-client", client="client-b"),
              send(6, sid="other-number", number="+16025550102"),
              send(6, sid="same")]
    got = decide(events)
    assert got[("other-client", 6)] == ("allow", None)
    assert got[("other-number", 6)] == ("allow", None)
    assert got[("same", 6)] == ("block", "opted_out")


@pytest.mark.parametrize("raw", ["602-555-0101", "(602) 555-0101", "16025550101", "+1 602 555 0101"])
def test_number_formats_reach_the_same_record(raw):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
                send(6, number=raw)]) == ("block", "opted_out")


@pytest.mark.parametrize("raw", ["", "555-0101", "+44 20 7946 0958", "+11005550101", None, 6025550101, "call me"])
def test_an_unreadable_number_blocks(raw):
    assert one([send(6, number=raw)]) == ("block", "invalid_number")


def test_an_unknown_message_class_blocks():
    assert one([ev("opt_in", 1, consent="express_written"), send(6, mclass="promo")]) == \
        ("block", "unknown_message_class")


@pytest.mark.parametrize("bad", [
    {"kind": "mystery", "t": 1, "client_id": "c", "number": "+16025550101"},
    {"kind": "inbound_sms", "t": "1", "client_id": "c", "number": "+16025550101", "text": "STOP"},
    {"kind": "inbound_sms", "t": True, "client_id": "c", "number": "+16025550101", "text": "STOP"},
    {"kind": "inbound_sms", "t": 1, "number": "+16025550101", "text": "STOP"},
    {"kind": "inbound_sms", "t": 1, "client_id": "c", "number": "+16025550101"},
    {"kind": "send_attempt", "t": 1, "client_id": "c", "number": "+16025550101", "message_class": "informational"},
])
def test_a_malformed_event_refuses_the_whole_replay(bad):
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([send(9), bad])


def test_every_send_gets_exactly_one_decision():
    rng = random.Random(7)
    events = [send(rng.randrange(1000), sid=f"s{i}", mclass=rng.choice(["marketing", "informational"]))
              for i in range(200)]
    events += [ev("inbound_sms", rng.randrange(1000), text="STOP") for _ in range(5)]
    ds = ck.ConsentKeeper().replay(events)
    assert sorted(d.send_id for d in ds) == sorted(f"s{i}" for i in range(200))


def test_every_decision_carries_its_evidence():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"), send(6)]
    d = ck.ConsentKeeper().replay(events)[0]
    assert 1 in d.evidence and 2 in d.evidence


# ---------------------------------------------------------------- certification on the shared pool
def _pool():
    if str(POOLS) not in sys.path:
        sys.path.insert(0, str(POOLS))
    from poolgen import frontdesk_sets
    return frontdesk_sets


def _grade(seed):
    events, key = _pool().generate_f5(seed)
    decided = collections.defaultdict(list)
    for d in ck.ConsentKeeper().replay(events):
        decided[d.send_id].append(d)
    assert set(decided) == {k["send_id"] for k in key}, "a send got no decision, or a decision has no send"
    wrong, relabelled = [], collections.Counter()
    for k in key:
        for d in decided[k["send_id"]]:
            if d.decision != k["decision"]:
                wrong.append((k["episode"], k["decision"], d.decision, d.reason))
            elif d.reason != k["reason"]:
                if {d.reason, k["reason"]} <= OPT_OUT_PAIR:
                    relabelled[k["episode"]] += 1
                else:
                    wrong.append((k["episode"], k["reason"], d.reason))
    return len(key), wrong, relabelled


@pytest.mark.parametrize("seed", ["dev-a", "dev-b", "dev-c", "f5-cert-1", "f5-cert-2"])
def test_certification_on_the_shared_f5_pool(seed):
    n, wrong, relabelled = _grade(seed)
    assert n >= 3600
    assert wrong == [], wrong[:5]
    # the only relabelling allowed: the typo episode, read as an exact opt-out
    assert set(relabelled) <= {"typo_stop_then_send"}, relabelled


def test_the_certification_grader_catches_a_keeper_that_lets_one_send_through(monkeypatch):
    real = ck.ConsentKeeper._decide

    def leaky(self, e, i):
        d = real(self, e, i)
        if d.reason == "opted_out" and not getattr(leaky, "done", False):
            leaky.done = True
            return ck.Decision(d.send_id, "allow", None, d.t, d.evidence)
        return d

    monkeypatch.setattr(ck.ConsentKeeper, "_decide", leaky)
    _, wrong, _ = _grade("dev-a")
    assert wrong, "the grader must notice a single opted-out send allowed"
