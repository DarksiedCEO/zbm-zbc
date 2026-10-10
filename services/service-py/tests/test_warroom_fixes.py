"""
War room fixes (ADR 0018; devtools/warroom/findings.md): WR-F001 (lookalikes the shared table folds were not folded
on the opt-out path), WR-F004 (full-width digits read as letters), and the SHOULD observations fixed with them
(dotted spelling, sentence-shaped opt-outs below an unmarked quote, a full-width clause end). The inputs are the war
room's replay cases (devtools/warroom/replay/service-py.json), copied.
"""

from __future__ import annotations

import pytest

import channels as ch
import triage

QUOTE = "\n\n-----Original Message-----\nFrom: support@acme.example\nSent: Monday\nTo: you\nSubject: hi\n\n"


@pytest.mark.parametrize("text", [
    "uηs­υ­bsc­riβe",                               # R0001
    "Unsuβѕcr1bе<br>ѕ3ητ fr0m му 1Pн0η3",  # R0003
    "ԁoηt τеxt or εмаil мe іf уоu cαn hεlp іт",  # R0005
    "Dο η o t τeχt or emaіl mе pleаѕe",    # R0006
    "wroη­ɡ p­e­r­soη",                          # R0011
    "caηcеlar",                                                          # R0012
    "Please ԁοη'τ τeχt оr емαil ме аηуmorε.",  # R0013
    "r3mον3 mу ηυmβ3r αηԁ mу ema1l",  # R0014
    "ｕnѕｕβsсrιвε",                          # R0015
])
def test_wr_f001_lookalike_opt_outs_are_exact(text):
    assert ch.opt_out_level(text) == "exact"
    assert ch.email_opt_out_decision(text) == "revoke"


@pytest.mark.parametrize("text", [
    "рlеaѕe doη'т call or εмail мε\n\n> Thanks for reaching out.",   # R0002
    "Plεаѕe doη't сall оr εмail мe\n\nOn Mon, Oct 5, 2026 Acme wrote:\n> Shipped.",  # R0008
])
def test_wr_f001_lookalike_direct_no_contact_revokes_email(text):
    assert ch.email_opt_out_decision(text) == "revoke_direct"


@pytest.mark.parametrize("text", [
    "ι ｄｏη＇ｔ ｗａｎｔ τｏ ｈεаｒ ｆｒｏм ｙｏｕ ａｇаіη",  # R0004
    "Pleaѕe ԁoη'τ reaсн out agaιn",                 # R0009
    "Ceaѕe all сOmmΥnіCATIoΗ",                                       # R0010: capitals of lower-case lookalikes
])
def test_wr_f001_lookalike_possible_opt_outs_are_surfaced(text):
    assert ch.possible_opt_out(text)


def test_wr_f001_capitals_keep_their_visual_reading_and_only_surface_otherwise():
    assert ch.opt_out_level("ЅΤΟΡ") == "exact"                # Cyrillic S, Greek T O P
    assert ch.opt_out_level("Ceaѕe all сOmmΥnіCATIoΗ") is None     # never revoked on that reading
    assert not ch.possible_opt_out("Can you send the ΑΒΓ invoice?")  # nothing to read differently


def test_wr_f001_triage_still_routes_another_script_to_a_human():
    assert triage.non_ascii_letters("Привет, как дела?")
    assert triage.normalise("η") != triage.normalise_opt_out("η")


def test_wr_f004_full_width_leet_digits_read_as_letters():
    text = "pleａsｅ ５７０ｐ tex７ｉｎｇ anｄ ｅm４1l1ng m３"   # R0007
    assert ch.opt_out_level(text) == "exact"
    assert ch.email_opt_out_decision(text) == "revoke"
    assert ch.typo_opt_out("５７０ｐｐ")                       # "570pp": one typo from stop


def test_war_room_seed3_full_width_clause_end_keeps_an_email_request_apart():
    text = ("Ｐｌeаｓｅ stop tｅxtｉnｇ． Cａｌｌ ｏｒ "
            "ｅmail ｉf neｅded\n\nSent from Mail for Windows")
    assert ch.email_opt_out_decision(text) == "keep"


def test_should_dotted_spelling_is_read():
    assert ch.email_opt_out_decision("N.e.v.e.r call or email me again") == "revoke_direct"
    assert ch.opt_out_level("s-t-o-p") == "exact"
    assert ch.opt_out_level("See you at 9 a.m., e.g. Monday") is None


@pytest.mark.parametrize("words", ["Dont call me or email me anymore", "I want to be removed from your email list",
                                   "I don't want your emails"])
def test_should_sentence_opt_outs_below_an_unmarked_quote_alert(words):
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Our spring offer is here.\n" + words) == "alert"


@pytest.mark.parametrize("tail", ["No more than one update a week. Cancel anytime.", "Stop by anytime!",
                                  "We never share your number. Text or email us anytime."])
def test_should_our_own_quoted_mail_still_raises_nothing(tail):
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + tail) is None


@pytest.mark.parametrize("words", ["Stop the texts and the emails please. I prefer you call.", "please stop emailing me",
                                   "Quit sending me messages", "S7OP THE 73X75 please", "Ｓｔｏｐ ｔｈｅ ｅｍａｉｌｓ"])
def test_should_imperative_stop_naming_a_channel_below_an_unmarked_quote_alerts(words):
    # war room service-py/email-clear-opt-out#016.1@1 (SHOULD): R2, "any other opt-out wording" alerts
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Our spring offer is here.\n" + words) in ("alert", "revoke")


@pytest.mark.parametrize("tail", ["Stop by anytime!", "Stop the presses: our sale is on.",
                                  "Stopping by? Text or email us anytime.", "You can stop these emails at any time."])
def test_should_a_mailers_own_stop_wording_still_raises_nothing(tail):
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + tail) is None


def test_should_a_bare_stop_below_a_sign_off_in_the_tail_alerts():
    # war room service-py/email-clear-opt-out#022.2@2 (SHOULD): "Thanks,\nSTOP" was cut with the signature
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Your order has shipped.\n\nThanks,\nSTOP") == "alert"
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Your order has shipped.\n\nThanks,\nJane Stopford") is None


def test_should_leet_alert_wording_in_the_tail_alerts():
    # war room service-py/email-clear-opt-out#004.2@3 (SHOULD; the WR-F004 class)
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Shipped.\n\nD0n7 text or email m3 1f y0u c4n h3lp 1t") == "alert"
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Order A13 ships by 5pm. Text or email us anytime.") is None
