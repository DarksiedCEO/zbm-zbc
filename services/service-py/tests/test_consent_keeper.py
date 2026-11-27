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
    if kind == "opt_in":
        e["source"] = "web_form"
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


# ---------------------------------------------------------------- AEGIS round 1 (Inquisitor + test-verification, c08c3f3f)
@pytest.mark.parametrize("kind, extra", [("inbound_sms", {"text": "STOP"}), ("carrier_opt_out", {}),
                                         ("number_reassigned", {}), ("opt_in", {"consent": "express_written"})])
@pytest.mark.parametrize("raw", ["602/555/0101x", "call me", None, 6025550101, "", "+44 20 7946 0958"])
def test_a_state_change_for_an_unreadable_number_refuses_the_replay(kind, extra, raw):
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([ev(kind, 5, number=raw, **extra), send(6)])


def test_a_state_change_without_a_number_refuses_the_replay():
    e = ev("inbound_sms", 5, text="STOP")
    del e["number"]
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([e, send(6)])


@pytest.mark.parametrize("raw", ["602‑555‑0101", "602–555–0101", "tel:+16025550101",
                                 "602/555/0101", "602​555​0101", "+1602555０１０１",
                                 "+١٦٠٢٥٥٥٠١٠١"])
def test_every_written_form_of_one_number_is_one_record(raw):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP", number=raw),
                send(6)]) == ("block", "opted_out")
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
                send(6, number=raw)]) == ("block", "opted_out")


@pytest.mark.parametrize("raw", ["+16021550101", "+16025550101x", "+160255501019", "+1602555010", "+10025550101"])
def test_a_number_that_is_not_a_north_american_number_blocks(raw):
    assert one([send(6, number=raw)]) == ("block", "invalid_number")


def test_a_reused_keeper_starts_every_replay_from_nothing():
    keeper = ck.ConsentKeeper()
    keeper.replay([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 10, text="STOP"), send(11)])
    second = keeper.replay([ev("opt_in", 2, consent="express_written"), send(20, mclass="marketing")])
    assert [(d.decision, d.reason) for d in second] == [("allow", None)]   # its own history only
    third = keeper.replay([ev("inbound_sms", 10, text="STOP"), ev("opt_in", 2, consent="express_written"),
                           send(20, mclass="marketing")])
    assert [(d.decision, d.reason) for d in third] == [("block", "opted_out")]


@pytest.mark.parametrize("text", ["Do not call me", "Don't call me", "Never call me again", "DNC", "dont call",
                                  "stop calling me", "please don't contact me", "No me llamen",
                                  "No me manden mensajes", "ya no me escriban", "no quiero llamadas"])
def test_calls_only_revocations_block_texts_and_calls(text):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
                send(6, mclass="marketing")]) == ("block", "opted_out")


def test_wording_customer_service_hands_to_a_human_still_blocks():
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="Opt me out"),
                send(6)]) == ("block", "possible_opt_out")


@pytest.mark.parametrize("text", ["Please call me back", "Call me at 5", "Can you call me tomorrow?",
                                  "Llámenme mañana", "Yes please"])
def test_asking_to_be_called_is_not_an_opt_out_but_waits_for_a_person(text):
    """Round 5: free text is never judged harmless by the keeper; it blocks as unrecognised until a staff review."""
    history = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text)]
    assert one(history + [send(6, mclass="marketing")]) == ("block", "unrecognised_reply")
    reviewed = history + [ev("staff_review", 7, reviewer="andre", ruling="not_an_opt_out"), send(8, mclass="marketing")]
    assert one(reviewed) == ("allow", None)


@pytest.mark.parametrize("bad", [None, "STOP", 5, ["inbound_sms"]])
def test_a_non_object_event_refuses_with_value_error(bad):
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([send(9), bad])


def test_a_send_id_used_twice_refuses_the_replay():
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([send(1, sid="dup"), ev("inbound_sms", 5, text="STOP"), send(6, sid="dup")])


def test_an_empty_client_id_refuses():
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([send(6, client="")])


@pytest.mark.parametrize("client", ["client-a ", "CLIENT-A", "client‑a", "​client-a"])
def test_one_client_typed_differently_is_one_record(client):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP", client=client),
                send(6)]) == ("block", "opted_out")


@pytest.mark.parametrize("source", ["inbound_sms_START", None, "unknown", "phone_call"])
def test_consent_from_an_unknown_source_does_not_count(source):
    assert one([ev("inbound_sms", 1, text="STOP"), ev("opt_in", 5, consent="express_written", source=source),
                send(9)]) == ("block", "opted_out")
    assert one([ev("opt_in", 5, consent="express_written", source=source), send(9, mclass="marketing")]) == \
        ("block", "no_written_consent")


def test_new_written_consent_after_start_after_stop_restores():
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
                ev("inbound_sms", 7, text="START"), ev("opt_in", 9, consent="express_written"),
                send(20, mclass="marketing")]) == ("allow", None)


@pytest.mark.parametrize("word", ["YES", "START", "Sí", "si"])
def test_a_restart_word_from_a_consenting_number_changes_nothing(word):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=word),
                send(6, mclass="marketing")]) == ("allow", None)


def test_si_after_stop_keeps_the_block_with_the_start_reason():
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="ALTO"),
                ev("inbound_sms", 7, text="si"), send(9)]) == ("block", "start_keyword_does_not_restore")


def test_an_allow_carries_the_consent_it_rests_on():
    d = ck.ConsentKeeper().replay([ev("opt_in", 1, consent="express_written"), send(6, mclass="marketing")])[0]
    assert (d.decision, d.evidence) == ("allow", (0, 1))


# ---------------------------------------------------------------- AEGIS round 2 (Inquisitor, e5f545e0)
@pytest.mark.parametrize("text", ["Do not ever call me", "I don't want any calls", "No calls",
                                  "I don't want to be called", "do not telephone me", "dont cal me", "d0nt c4ll me",
                                  "N0 ME LLAMEN", "No me llame", "No llamar", "dejen de llamarme",
                                  "no quiero que me llamen", "no me contacten", "nao me liguem", "ne m'appelez plus",
                                  "D.N.C.", "d-n-c"])
def test_more_calls_only_revocations_block(text):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
                send(6, mclass="marketing")]) == ("block", "opted_out")


@pytest.mark.parametrize("text", ["I called earlier", "Thanks for calling", "Did you call me?", "Hola, llamo por mi cita",
                                  "Do call me tomorrow", "Please call me back"])
def test_ordinary_calling_words_are_not_a_revocation(text):
    assert ck.opt_out_reading(text) == "unrecognised_reply"          # never opted_out: a person clears it


@pytest.mark.parametrize("mark", ["⁦", "⁩", "‪", "‮", "͏", "᠎", "️",
                                  "\U000e0041", "؜"])
def test_every_invisible_in_a_client_id_is_removed(mark):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP", client="client-a" + mark),
                send(6)]) == ("block", "opted_out")


@pytest.mark.parametrize("a, b", [("strasse-dental", "straße-dental"), ("acme2", "acme²"),
                                  ("k-plumbing", "\uff4b-plumbing")])
def test_two_different_clients_never_merge(a, b):
    got = decide([ev("inbound_sms", 1, text="STOP", client=a), ev("opt_in", 5, consent="express_written", client=b),
                  send(9, sid="a", client=a)])
    assert got[("a", 9)] == ("block", "opted_out")      # b's consent never lifts a's STOP


# ---------------------------------------------------------------- AEGIS round 3 (Inquisitor, 2554e328)
ROUND3_LEAKS = ["dontcall", "no phonecalls", "calls? no thanks", "enough with the calls", "\U0001F4F5",
                "don't leave me voicemails", "Call me never",
                "I really do not want you or anyone from your office to call me", "no me hablen", "ya no me hablen",
                "favor de no hablarme", "dejen de marcarme", "no me estén marcando", "paren de llamarme", "no me yamen",
                "dejen de yamarme", "sin llamadas por favor", "cero llamadas", "ninguna llamada", "llamadas no",
                "sáquenme de su lista", "quítenme de su lista", "bórrenme de su lista", "eliminen mi número",
                "no me molesten", "no me interesa"]


@pytest.mark.parametrize("text", ROUND3_LEAKS)
def test_round_3_revocations_block_every_send(text):
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
              send(6, sid="m", mclass="marketing"), send(6, sid="i")]
    got = decide(events)
    assert got[("m", 6)][0] == "block" and got[("i", 6)][0] == "block"
    assert ck.opt_out_reading(text) in ("opted_out", "possible_opt_out")   # an opt-out: no staff review clears it


@pytest.mark.parametrize("text", ["No problem, call me back", "No, llame mañana por favor", "Not now call me later"])
def test_a_refusal_anywhere_near_a_contact_word_blocks_by_design(text):
    """Fail toward suppression (ADR 0014 RR-F5 round 3): these lose texts until new written consent; a missed
    opt-out would be a TCPA violation."""
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text), send(6)])[0] == "block"


# ---------------------------------------------------------------- AEGIS round 4 (Inquisitor, e42571ef)
ROUND4_LEAKS = ["Don‘t text me", "Don`t call me", "Please don´t text me anymore", "Donʼt text me",
                "Don＇t call me", "I withdraw my consent to receive texts", "I am revoking my consent",
                "I revoked consent", "Consent revoked", "I'm opting out", "I opted out already",
                "I no longer wish to receive these", "I do not consent", "I no longer consent",
                "I rescind my consent", "Kindly refrain from texting me", "I want to be removed", "unsubscribing",
                "Please cease", "Desuscríbanme", "Desuscribirme por favor", "Retiro mi consentimiento",
                "Revoco mi consentimiento", "Déjenme en paz", "No más", "Ya basta",
                "No quiero saber nada de ustedes", "Arrêtez", "Bitte keine SMS mehr", "Non scrivermi", "退订"]


@pytest.mark.parametrize("text", ROUND4_LEAKS)
def test_round_4_revocations_block_every_send(text):
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
              send(6, sid="m", mclass="marketing"), send(6, sid="i")]
    got = decide(events)
    assert got[("m", 6)][0] == "block" and got[("i", 6)][0] == "block"
    assert ck.opt_out_reading(text) in ("opted_out", "possible_opt_out")


@pytest.mark.parametrize("source", [["web_form"], {"x": 1}, 5])
def test_an_opt_in_source_that_is_not_text_refuses_with_valueerror(source):
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([ev("opt_in", 1, consent="express_written", source=source), send(2)])


@pytest.mark.parametrize("a, b", [("École-Dental", "école-dental"), ("ÑANDÚ-SPA", "ñandú-spa"),
                                  ("Ørsted-Clinic", "ørsted-clinic")])
def test_one_client_typed_with_other_capitals_is_one_record(a, b):
    events = [ev("opt_in", 1, consent="express_written", client=a), ev("inbound_sms", 5, text="STOP", client=a),
              send(6, client=b)]
    assert one(events)[0] == "block"


def test_stop_and_start_at_one_instant_give_one_reason_in_any_order():
    a = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
         ev("inbound_sms", 5, text="START"), send(6)]
    b = [a[0], a[2], a[1], a[3]]
    assert one(a) == one(b)


# ---------------------------------------------------------------- AEGIS round 5 (Inquisitor, 70adc1b6)
ROUND5_LEAKS = ["Don\uff40t text me", "Don\u0384t call me", "Please don\u02cat text me anymore", "Don\u2035t text me",
                "R3voking consent", "I w1thdr4w my c0ns3nt", "Un5ubscribing", "0pting 0ut", "N0 m4s", "Y4 b4st4",
                "Dej3nme en paz", "Please discontinue these texts", "Delete me from your list", "Erase my number",
                "Forget my number", "Turn off texts", "Unenroll me from these texts", "Lay off the texts",
                "Please don't reach me again", "Cancelen los mensajes", "Cancela mi suscripci\u00f3n",
                "Detengan los mensajes", "Suspendan los mensajes", "Olv\u00eddense de mi n\u00famero", "Paren ya",
                "\U0001F1F8\U0001F1F9\U0001F1F4\U0001F1F5", "\U0001F1F8 \U0001F1F9 \U0001F1F4 \U0001F1F5",
                "\U0001F162\U0001F163\U0001F15E\U0001F15F", "\U0001F182\U0001F183\U0001F17E\U0001F17F", "\u274c\U0001F4F1",
                "", " ", "\U0001F6D1", "Arrêtez svp", "Bitte nicht mehr schreiben"]


@pytest.mark.parametrize("text", ROUND5_LEAKS)
def test_round_5_revocations_block_every_send(text):
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
              send(6, sid="m", mclass="marketing"), send(6, sid="i")]
    got = decide(events)
    assert got[("m", 6)][0] == "block" and got[("i", 6)][0] == "block"


@pytest.mark.parametrize("text", ["OK", "ok!", "Okay.", "Thanks", "Thank you!", "Yes", "Y", "Confirmed", "C", "1",
                                  "Sounds good", "See you then", "Gracias", "S\u00ed", "Perfecto", "\U0001F44D", "HELP",
                                  "Spot", "Post"])
def test_a_known_safe_reply_changes_nothing(text):
    assert one([ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
                send(6, mclass="marketing")]) == ("allow", None)


def test_a_staff_review_clears_only_an_unrecognised_reply_and_only_after_it():
    base = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="Can I come at 4?")]
    review = lambda t: ev("staff_review", t, reviewer="andre", ruling="not_an_opt_out")
    assert one(base + [review(5), send(6)]) == ("block", "unrecognised_reply")         # same instant: not after
    assert one([base[0], review(3), base[1], send(6)]) == ("block", "unrecognised_reply")  # before the reply
    assert one(base + [review(6), send(7)]) == ("allow", None)
    for text in ("STOP", "Please don't text me", "STPO"):
        stopped = [base[0], ev("inbound_sms", 5, text=text), review(6), send(7)]
        assert one(stopped)[0] == "block"                                               # an opt-out is never cleared


@pytest.mark.parametrize("bad", [{"reviewer": ""}, {"reviewer": "  "}, {"reviewer": 5}, {"ruling": "ok"}, {}])
def test_a_staff_review_without_a_reviewer_and_ruling_refuses_the_replay(bad):
    e = ev("staff_review", 6, reviewer="andre", ruling="not_an_opt_out")
    e.update(bad)
    for k in [k for k in ("reviewer", "ruling") if k not in bad and not bad]:
        e.pop(k)
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay([ev("opt_in", 1, consent="express_written"), e, send(7)])


def test_a_staff_ruling_of_opt_out_is_permanent():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="Please discontinue these texts"),
              ev("staff_review", 6, reviewer="andre", ruling="opt_out"),
              ev("staff_review", 7, reviewer="andre", ruling="not_an_opt_out"), send(8)]
    assert one(events) == ("block", "opted_out")


# ---------------------------------------------------------------- AEGIS round 6 (Inquisitor, d462354a)
@pytest.mark.parametrize("block", [[ev("inbound_sms", 5, text="STOP")], [ev("inbound_sms", 5, text="STPO")],
                                   [ev("inbound_sms", 5, text="Please discontinue these texts"),
                                    ev("staff_review", 6, reviewer="andre", ruling="opt_out")],
                                   [ev("inbound_sms", 5, text="STOP"), ev("inbound_sms", 6, text="START")],
                                   [ev("number_reassigned", 5)]])
def test_a_later_reply_and_a_review_never_clear_a_stronger_block(block):
    events = [ev("opt_in", 1, consent="express_written")] + block + [
        ev("inbound_sms", 8, text="hello?"), ev("staff_review", 9, reviewer="andre", ruling="not_an_opt_out"),
        send(10, sid="m", mclass="marketing"), send(10, sid="i")]
    got = decide(events)
    assert got[("m", 10)][0] == "block" and got[("i", 10)][0] == "block"


def test_a_stop_and_a_reply_at_one_instant_decide_the_same_in_any_order():
    stop, hello = ev("inbound_sms", 5, text="STOP"), ev("inbound_sms", 5, text="hello?")
    tail = [ev("staff_review", 6, reviewer="andre", ruling="not_an_opt_out"), send(7)]
    head = [ev("opt_in", 1, consent="express_written")]
    assert one(head + [stop, hello] + tail) == one(head + [hello, stop] + tail) == ("block", "opted_out")


@pytest.mark.parametrize("text", ["Yes \u8bf7\u505c\u6b62\u53d1\u9001", "Yes \uadf8\ub9cc \ubcf4\ub0b4\uc138\uc694", "Yes, \u062a\u0648\u0642\u0641",
                                  "yes \u092c\u0902\u0926 \u0915\u0930\u094b", "Yes \u6b62\u3081\u3066", "start \u043d\u0435\u0442",
                                  "S\u00ed \u505c\u6b62"])
def test_a_restart_word_with_anything_else_is_not_a_restart_word(text):
    assert ck.opt_out_reading(text) == "unrecognised_reply"


@pytest.mark.parametrize("text", ["START", "Yes", "yes!", "UNSTOP", "S\u00ed"])
def test_a_bare_restart_word_is_still_recognised(text):
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="STOP"),
              ev("inbound_sms", 6, text=text), send(7)]
    assert one(events) == ("block", "start_keyword_does_not_restore")


@pytest.mark.parametrize("bad", [None, 5, 2.5, True, "STOP", {"kind": "opt_in"}])
def test_a_replay_that_is_not_a_list_refuses_with_valueerror(bad):
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay(bad)


# ---------------------------------------------------------------- AEGIS round 7 (Inquisitor, ea4fe868)
@pytest.mark.parametrize("text", ["U\u0336N\u0336STOP", "U\u0336N\u0336BLOCK", "S\u0336T\u0336A\u0336R\u0336T\u0336", "S\u0338TART",
                                  "O\u0336K\u0336", "Y\u0336e\u0336s\u0336"])
def test_a_struck_or_marked_word_is_never_a_restart_word_or_a_safe_reply(text):
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text=text),
              send(6, sid="m", mclass="marketing"), send(6, sid="i")]
    got = decide(events)
    assert got[("m", 6)][0] == "block" and got[("i", 6)][0] == "block"


def test_the_evidence_keeps_the_event_that_set_a_block():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="Can I come at 4?"),
              ev("opt_in", 5, consent="express_written"), send(6)]
    d = ck.ConsentKeeper().replay(events)[0]
    assert d.decision == "block" and 1 in d.evidence


def test_new_written_consent_after_an_unrecognised_reply_restores_texting():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="Can I come at 4?"),
              ev("opt_in", 6, consent="express_written"), send(7)]
    assert one(events) == ("allow", None)


# ---------------------------------------------------------------- AEGIS round 8 (Inquisitor, ded558d9)
@pytest.mark.parametrize("variant", ["Acme  Dental", "Acme Dental", "Acme　Dental", "Acme Dental",
                                     "Acme Dental", "Acme\tDental"])
def test_a_client_with_other_spaces_is_one_record(variant):
    assert ck.normalise_client(variant) == ck.normalise_client("Acme Dental")
    assert ck.normalise_client("AcmeDental") != ck.normalise_client("Acme Dental")


@pytest.mark.parametrize("name", ["​", "ㅤ", "⠀", "ᅟ", "­", "-", " ", "​ㅤ"])
def test_a_reviewer_must_have_a_visible_name(name):
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 5, text="Can I come at 4?"),
              ev("staff_review", 6, reviewer=name, ruling="not_an_opt_out"), send(7)]
    with pytest.raises(ValueError):
        ck.ConsentKeeper().replay(events)


def test_the_evidence_keeps_a_staff_opt_out_the_opt_in_could_not_clear():
    events = [ev("opt_in", 1, consent="express_written"), ev("inbound_sms", 3, text="Can I come at 4?"),
              ev("staff_review", 5, reviewer="Dana", ruling="opt_out"), ev("opt_in", 5, consent="express_written"),
              send(6)]
    d = ck.ConsentKeeper().replay(events)[0]
    assert (d.decision, d.reason) == ("block", "opted_out") and 2 in d.evidence
