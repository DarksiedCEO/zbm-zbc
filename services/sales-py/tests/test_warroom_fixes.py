"""War room leftover (ADR 0018, devtools/warroom/findings.md; ADR 0013 "War room fixes"): sales-py's reply classifier
reads the disguised opt-outs service-py reads, through the repo's shared lookalike fold (src/lookalikes.py, the same
file four other services carry), without reading a word written wholly in another script as an English one.

The war room's scenarios sales-py/email-reply-disguised-opt-out, sms-reply-disguised-opt-out and
foreign-script-ordinary-reply read DISGUISED_OPT_OUTS and BENIGN_FOREIGN below."""

from __future__ import annotations

import random
import re
import unicodedata

import pytest

from helpers import Harness, rid, wired_ports
from intelligences import i10_replies

# Opt-outs disguised the ways service-py reads (ADR 0014 V1-H1, WR-F001..WR-F004): Greek / Cyrillic / Armenian /
# Cherokee lookalikes, small capitals, full-width and mathematical letters, enclosed letters, invisible characters,
# leetspeak (also full-width, and inside a spelled-out word), letter spacing, HTML markup and entities. Each word that
# carries a wide lookalike also carries a Latin-script letter (a small capital, an insular letter) or a letter of a
# second script, so the war room's core homoglyph transform cannot turn it into a word wholly in one non-Latin script
# (read as that language, WR-F006): what the scenario then checks is the fold, not that rule.
DISGUISED_OPT_OUTS = [
    "ЅТОР", "ѕтор", "stoр", "ЅΤΟΡ", "ᏚᎢᎾᏢ", "Ꮪ Ꭲ Ꮎ Ꮲ", "S_τ_O_Ꮲ", "ᏕτOք", "sꞇoք_ᴛexting",
    "unѕubѕcribe", "uηs\u00adυ\u00adbsc\u00adriβe", "μnsubscribe", "ᴜɴsᴜʙsᴄʀɪʙᴇ", "UπᏕйBʙбꜱCᏒIᏴє",
    "ｓｔｏｐ", "ＵＮＳＵＢＳＣＲＩＢＥ", "\U0001d42c\U0001d42d\U0001d428\U0001d429", "\U0001d4c8\U0001d4c9\U0001d45c\U0001d4c5",
    "ⓢⓣⓞⓟ", "s\u200bt\u200bo\u200bp", "un\u2060subscribe", "s\ufefft\u200co\u200dp",
    "5t0p", "５７０ｐ", "s 7 o p", "u n 5 u b", "S T O P", "S.T.O.P", "stoooᎾp",
    "Dο η o t cοntact me", "Do ɴσt conтaᴄԏ mе agᴀin", "ηοt intеrested", "nо tнᎪɴk you", "no τhɑոk ʏou",
    "<p>StoP</p>", "<div>S_T_O_P</div>", "<p>ηoт interеѕteԁ, tнaηκs</p>", "<div>u n s u b</div>",
    "&#83;TOP", "&lt;b&gt;unsubscribe&lt;/b&gt;", "&lt;в&gt;υηѕubѕсriβε&lt;/β&gt;", "&lt;ｂ&gt;ｕｎsuｂｓcｒｉbｅ&lt;／b&gt;",
    "Ꭱ Ꭼ Ꮇ Ꮻ Ꮩ Ꭼ me", "&Lt;Ｂ&Gt;Ｕｎsuｂｓcｒｉbｅ&Lt;／B&Gt;", "≪b≫unsubscribe", "ηοt 1ntеres73d", "Μnsubscribe",
]

# Ordinary replies written in Cyrillic (Russian, Ukrainian, Bulgarian, Serbian), Greek, Hebrew and Arabic: requests to
# be texted or e-mailed, thanks, questions. None is an opt-out (service-py's WR-F006 list, and words a lookalike fold
# could misread: Russian "по" is "by", "не" / "нет" are "not" / "no", "стоп" is the Russian word; Serbian "Баја").
BENIGN_FOREIGN = [
    "Пожалуйста, отправьте код по SMS",
    "Напишите мне по SMS, когда заказ будет готов",
    "Можно по телефону или по SMS?",
    "Ответьте по text message",
    "По",
    "ПО",
    "ПОЖАЛУЙСТА, ОТПРАВЬТЕ КОД ПО SMS",
    "Спасибо, по email тоже можно",
    "Нет, всё нормально, пишите по SMS",
    "Не надо звонить, пишите по SMS",
    "Стоп, я перепутал номер заказа",
    "Надішліть код по SMS",
    "Напишіть мені по SMS, будь ласка",
    "Изпратете кода по SMS",
    "Пишете ми по SMS, моля",
    "Пошаљите код по SMS",
    "Баја ће вам писати по SMS",
    "Παρακαλώ στείλτε τον κωδικό με SMS",
    "Ναι, στείλτε μου SMS παρακαλώ",
    "Πότε θα έρθει η παραγγελία; Στείλτε SMS.",
    "שלחו לי את הקוד ב-SMS",
    "תודה, אפשר לשלוח לי הודעת SMS?",
    "أرسل لي الرمز عبر SMS",
    "شكرا، أرسلوا لي رسالة SMS",
]


@pytest.mark.parametrize("text", DISGUISED_OPT_OUTS)
@pytest.mark.parametrize("channel", ["email", "sms"])
def test_disguised_opt_outs_are_opt_outs(text, channel):
    assert i10_replies.classify(text, channel) == "unsubscribe", i10_replies.normalise(text)


@pytest.mark.parametrize("text", BENIGN_FOREIGN)
@pytest.mark.parametrize("channel", ["email", "sms", "voice"])
def test_a_word_wholly_in_another_script_is_never_an_opt_out(text, channel):
    assert i10_replies.classify(text, channel) != "unsubscribe", i10_replies.normalise(text)


@pytest.mark.parametrize("text", ["по", "ПО", "по sms", "по thanks", "по more emails", "не", "нет", "нет thanks"])
def test_a_short_word_wholly_in_one_script_is_not_read_as_a_negation(text):
    # the WR-F006 rule: a word wholly in one non-Latin script is never folded into "no" / "not" by the shared fold
    # ("ηο thanks" was read as "no thanks" before the war room, by this module's own table, and still is)
    for channel in ("email", "sms"):
        assert i10_replies.classify(text, channel) != "unsubscribe", text


# Ordinary English replies the extra readings (HTML as text, brackets as separators, "1" as "i") must leave alone.
ORDINARY = ["I need a non-stop flight to Denver", "Yes, I'm interested (Tuesday works)", "Can you send pricing?",
            "1 more question: what does it cost?", "<p>Sounds good, let's book a call</p>", "Re: «Q4 plan» looks fine",
            "x ≥ 1 seats, 10 users", "Endless thanks for the info"]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_replies_are_not_opt_outs_on_email(text):
    assert i10_replies.classify(text, "email") != "unsubscribe", i10_replies.normalise(text)


def _harness(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    lead = h.vlead(email="jane@acme-shop.test", phone="+13105550100")
    h.ok(h.consent(lead["contact_id"]), 201)
    return h, lead


def _reply(h, text, channel):
    body = {"request_id": rid(), "channel": channel, "text": text}
    body.update({"from_email": "jane@acme-shop.test"} if channel == "email" else {"from_phone": "+13105550100"})
    return h.ok(h.post("/sales/v1/replies", body, caller="provider_events"), 201)


@pytest.mark.parametrize("text", DISGUISED_OPT_OUTS[::3])
@pytest.mark.parametrize("channel", ["email", "sms"])
def test_end_to_end_a_disguised_opt_out_suppresses_every_channel(tmp_path, text, channel):
    h, lead = _harness(tmp_path)
    r = _reply(h, text, channel)
    assert r["class"] == "unsubscribe" and r["suppressed"] is True, r
    c = h.ok(h.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["email_suppressed"] and c["phone_suppressed"], c


@pytest.mark.parametrize("text", BENIGN_FOREIGN)
def test_end_to_end_benign_foreign_replies_suppress_nothing_and_reach_a_person(tmp_path, text):
    h, lead = _harness(tmp_path)
    for channel in ("sms", "email"):
        r = _reply(h, text, channel)
        assert r["suppressed"] is False and r["task_id"], r        # held for a person (AEGIS S3-C1), never suppressed
    c = h.ok(h.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert not c["email_suppressed"] and not c["phone_suppressed"], c


def _single_word_alternatives(rx: re.Pattern) -> set[str]:
    """The top-level alternatives of ``\\b(a|b|...)\\b`` that are one word (a ``\\w*`` suffix allowed)."""
    body, depth, cur, alts = rx.pattern[3:-3], 0, "", []
    for c in body:
        if c == "|" and depth == 0:
            alts.append(cur)
            cur = ""
            continue
        depth += (c == "(") - (c == ")")
        cur += c
    alts.append(cur)
    return {m.group(1) for a in alts if (m := re.fullmatch(r"([a-z]+)(?:\\w\*)?", a))}


def test_the_disguise_words_are_the_english_single_opt_out_words_of_four_letters_or_more():
    words = {w for w in i10_replies._CARRIER if " " not in w}
    words |= _single_word_alternatives(i10_replies._UNSUB) | _single_word_alternatives(i10_replies._UNSUB_PHONE)
    single = {w for w in words if len(w) >= 4}
    assert {"stop", "unsub", "revoke", "cancel", "quit", "remove", "alto"} <= single, sorted(single)
    # the Spanish words are left out on purpose: a word of another script that folds into one is a word of its own
    # language (Serbian "Баја" folds to "baja"), not a disguised English opt-out
    foreign = {"alto", "parar", "cancelar", "baja"}
    assert single - foreign <= i10_replies.OPT_OUT_DISGUISE_WORDS, sorted(single - foreign - i10_replies.OPT_OUT_DISGUISE_WORDS)
    assert not foreign & i10_replies.OPT_OUT_DISGUISE_WORDS


def test_only_greek_mu_is_read_otherwise_than_the_shared_fold():
    # the one letter this module's small table reads otherwise ("m"): opt-out wording is also read with "u"
    assert i10_replies._SHARED_READS == frozenset({"μ"})
    assert i10_replies.normalise("μe") == "me" and i10_replies.normalise("μe", shared=True) == "ue"
    assert i10_replies.classify("do not contact μe") == "unsubscribe"
    assert i10_replies.classify("μnsub") == "unsubscribe"


def test_html_is_read_as_text_for_every_label_and_as_written_for_opt_outs():
    assert i10_replies.classify("<p>Yes, I'm interested</p>") == "interested"
    assert i10_replies.classify("<div>Out of office until Monday</div>") == "out_of_office"
    assert i10_replies.classify("<p>Who is this?</p>") == "review"
    # an opt-out that only the text as written shows (angle brackets that are not a tag we know) still counts
    assert i10_replies.classify("<stop>") == "unsubscribe"


# The pre-war-room classifier (6a4b1e0..ebc8403 i10_replies.normalise / classify), frozen here: everything it read as
# an opt-out is still one (the war room fix only adds readings).
_OLD_CONFUSABLE = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ɡ": "g",
    "α": "a", "β": "b", "ε": "e", "ζ": "z", "η": "n", "ι": "i", "κ": "k", "μ": "m", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x"})


def _old_normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).casefold()
    t = t.translate(_OLD_CONFUSABLE).replace("_", " ")
    t = " ".join(i10_replies._fold_word(w) for w in t.split())
    t = re.sub(r"(?<=[^\W\d_])[^\w\s]+(?=[^\W\d_])", "", t)
    t = re.sub(r"[^\w\s]+", " ", t)
    words, out, run = t.split(), [], []
    for w in words + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        out += ["".join(run)] if len(run) >= 3 else run
        run = []
        if w:
            out.append(w)
    return " ".join(out)


def _old_opt_out(text: str, channel: str) -> bool:
    t = _old_normalise(text)
    for v in (t, i10_replies._collapse(t)):
        if v in i10_replies._CARRIER or i10_replies._UNSUB.search(v):
            return True
        if channel != "email" and i10_replies._UNSUB_PHONE.search(v):
            return True
    return False


def test_everything_the_classifier_read_as_an_opt_out_before_is_still_one():
    rng = random.Random(20261010)
    words = ["stop", "unsubscribe", "cancel", "end", "quit", "revoke", "opt out", "remove me", "do not contact",
             "not interested", "no thanks", "no more emails", "leave me alone", "alto", "baja", "please", "thanks",
             "me", "the", "order", "text", "sms", "by", "yes", "по", "нет", "μου"]
    pool = list("abcdefghijklmnopqrstuvwxyz013457 _.-'") + [chr(k) for k in _OLD_CONFUSABLE] + \
        ["\u200b", "\u00ad", "Ａ", "ｓ", "<p>", "</div>", "&amp;", "п", "ς", "Ꮪ", "ᴛ", "\u0301"]
    checked = 0
    for _ in range(6000):
        parts = [rng.choice(words) for _ in range(rng.randint(1, 4))]
        s = list(" ".join(parts))
        for _ in range(rng.randint(0, 4)):
            i = rng.randrange(len(s) + 1)
            if rng.random() < 0.5 and s:
                s[min(i, len(s) - 1)] = rng.choice(pool)
            else:
                s.insert(i, rng.choice(pool))
        text = "".join(s)
        if rng.random() < 0.3:
            text = text.upper()
        for channel in ("email", "sms"):
            if _old_opt_out(text, channel):
                checked += 1
                assert i10_replies.classify(text, channel) == "unsubscribe", (text, channel)
    assert checked > 1000
