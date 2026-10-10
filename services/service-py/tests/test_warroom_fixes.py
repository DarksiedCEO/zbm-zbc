"""
War room fixes (ADR 0018; devtools/warroom/findings.md): WR-F001 (lookalikes the shared table folds were not folded
on the opt-out path), WR-F004 (full-width digits read as letters), WR-F006 (a word written wholly in another script
was folded into a Latin opt-out word: Russian "по" read as "no"), and the SHOULD observations fixed with them
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


# WR-F006 (AEGIS H1): ordinary customer messages written in Cyrillic (Russian, Ukrainian, Bulgarian, Serbian), Greek,
# Hebrew and Arabic. Each is a request to be contacted, a thank-you or a question; none is an opt-out. The war room
# scenario foreign-script-ordinary-sms / -email reads this list (devtools/warroom/scenarios/service-py.json).
BENIGN_FOREIGN = [
    "Пожалуйста, отправьте код по SMS",                        # "please send the code by SMS"
    "Напишите мне по SMS, когда заказ будет готов",             # "text me when the order is ready"
    "Можно по телефону или по SMS?",                            # "by phone or by SMS?"
    "Это по cell номер",
    "Ответьте по text message",
    "По",
    "ПО",
    "ПОЖАЛУЙСТА, ОТПРАВЬТЕ КОД ПО SMS",
    "Спасибо, по email тоже можно",
    "Нет, всё нормально, пишите по SMS",
    "Надішліть код по SMS",                                     # Ukrainian
    "Напишіть мені по SMS, будь ласка",
    "Изпратете кода по SMS",                                    # Bulgarian
    "Пишете ми по SMS, моля",
    "Пошаљите код по SMS",                                      # Serbian (Cyrillic)
    "Пишите ми по SMS, хвала",
    "Παρακαλώ στείλτε τον κωδικό με SMS",                       # Greek
    "Ναι, στείλτε μου SMS παρακαλώ",
    "Πότε θα έρθει η παραγγελία; Στείλτε SMS.",
    "שלחו לי את הקוד ב-SMS",                                    # Hebrew
    "תודה, אפשר לשלוח לי הודעת SMS?",
    "أرسل لي الرمز عبر SMS",                                    # Arabic
    "شكرا، أرسلوا لي رسالة SMS",
]


@pytest.mark.parametrize("text", BENIGN_FOREIGN)
def test_wr_f006_a_word_wholly_in_another_script_is_never_an_opt_out(text):
    assert ch.opt_out_level(text) is None
    assert ch.email_opt_out_decision(text) is None
    assert not ch.possible_opt_out(text)
    assert not ch.typo_opt_out(text)
    assert ch.opt_out_scope(text) is None
    assert ch.quoted_tail_opt_out("Thanks" + QUOTE + "Shipped.\n" + text) is None


@pytest.mark.parametrize("text", BENIGN_FOREIGN)
def test_wr_f006_benign_foreign_sms_and_email_keep_consent(h, text):
    """End to end (AEGIS H1's repro): the SMS and the e-mail consent both stay active."""
    cid = h.contact("client:acme", email="owner@acme.test", phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid, channel="sms"), 201)
    h.ok(h.consent(cid, channel="email"), 201)
    h.ok(h.sms(text, frm="+13105551234"), 201)
    h.ok(h.email(text, frm="owner@acme.test"), 201)
    cons = {c["channel"]: c["status"] for c in h.ok(h.get(f"/svc/v1/contacts/{cid}"))["consents"]}
    assert cons == {"sms": "active", "email": "active"}


@pytest.mark.parametrize("text", [
    "ЅТОР", "ѕтор", "stoр", "ЅΤΟΡ", "вуе", "Βуе", "ΝΟ",           # caught before the war room (2cedde8): still caught
    "ᏚᎢᎾᏢ",                                                       # one script, folds to an opt-out word of 4+ letters
    "Ꮪ Ꭲ Ꮎ Ꮲ",                                                    # spaced-out single letters fold as before
    "unѕubѕcribe", "ｓｔｏｐ", "𝐬𝐭𝐨𝐩", "s\u200bt\u200bo\u200bp", "5t0p", "S T O P", "Dο η o t τeχt or emaіl mе pleаѕe",
])
def test_wr_f006_disguised_opt_outs_are_still_exact(text):
    assert ch.opt_out_level(text) == "exact"


@pytest.mark.parametrize("text", ["ηο", "ηο sms", "по sms", "По SMS"])
def test_wr_f006_a_short_word_wholly_in_one_script_is_not_read_as_no(text):
    # the rule AEGIS H1 asks for: a word wholly in one non-Latin script never becomes a negation or one-word reply
    assert ch.opt_out_level(text) is None


def test_wr_f006_russian_stop_is_unchanged_from_before_the_war_room():
    # "стоп" (Russian for stop) was not an opt-out at 2cedde8 either: no language's opt-out wording is guessed from
    # its look; a person reads a message in a script we have no lexicon for (triage.non_ascii_letters)
    for text in ("стоп", "СТОП"):
        assert ch.opt_out_level(text) is None
        assert triage.non_ascii_letters(text)


def test_wr_f006_the_disguise_words_are_the_english_single_opt_out_words_of_four_letters_or_more():
    words = {t for t in ch.OPT_OUT_TERMS + ch.OPT_OUT_STRONG + ch.OPT_OUT_FUZZY if " " not in t and len(t) >= 4}
    # the Spanish / French / Portuguese words are left out on purpose: a word of another script that folds into
    # one is a word of its own language (Serbian "Баја" folds to "baja"), not a disguised English opt-out
    foreign = {"alto", "arrete", "arreter", "baja", "cancelar", "cancele", "desabonner", "descadastrar", "parar",
               "pare", "parem", "sair"}
    assert triage.OPT_OUT_DISGUISE_WORDS == words - foreign
    assert ch.opt_out_level("Баја") is None


def test_wr_f006_triage_reading_unchanged():
    assert triage.normalise("Пожалуйста, отправьте код по SMS") == triage.normalise_opt_out(
        "Пожалуйста, отправьте код по SMS")
