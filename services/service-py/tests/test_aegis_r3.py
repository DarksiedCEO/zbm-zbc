"""AEGIS round 3 (Oct 5 2026, BLOCKING): one regression per finding, each the reviewer's scenario
(scratchpad/aegis-service-r3/c1..c5, v_low). The bot answers only exact approved example questions (V3-H1)."""

from __future__ import annotations

import base64
import os

import pytest

import config as config_mod
import service as service_mod
import store as store_mod
import triage
from helpers import Harness, RecordingSender, base_env, rid
from ports import Ports

# the reviewer's round-3 corpus, verbatim: (label, text)
REVIEWER_R3_CORPUS = [
 # privacy: data access / retention / deletion
 ("privacy","how long do you have my info for"),
 ("privacy","how long will you have my info"),
 ("privacy","how long do you usually take to tell me what info you have on me"),
 ("privacy","how long do you take to get my info to me"),
 ("privacy","what hours can i get my info"),
 ("privacy","can you tell me what info you have about me when you open tomorrow"),
 ("privacy","when you open can you tell me what you know about me"),
 ("privacy","what do you do with my info in office hours"),
 ("privacy","can i have my info this morning when you open"),
 ("privacy","what info do you have on me in office hours"),
 ("privacy","what hours can you tell me what you have on me"),
 ("privacy","how long do you have my clips for"),
 ("privacy","how long will you have my clips"),
 ("privacy","can my clips be removed"),
 ("privacy","can you have my clips removed"),
 ("privacy","where do my removed clips go"),
 ("privacy","can you send me my clips"),
 ("privacy","what do you do with my clips"),
 ("privacy","who can see my clips"),
 ("privacy","what timeline do you have for my info"),
 ("privacy","what is the turnaround to get my info"),
 ("privacy","what is your timeline to tell me what you know about me"),
 # security: phishing check, stolen content, account
 ("security","is this from you about my clips"),
 ("security","can you tell me if this is you about my clips"),
 ("security","my clips have been taken"),
 ("security","my clips have been posted by you"),
 ("security","my clips are lost"),
 ("security","is this you with my clips"),
 ("security","how long have you had my info"),
 ("security","is this from your office about my clips"),
 ("security","how long will my clips be lost"),
 ("security","can you tell me if this is from you this morning about the clips"),
 # legal / contract / takedown
 ("legal","can you have my clips taken down"),
 ("legal","my clips have been taken down"),
 ("legal","my clips have been removed"),
 ("legal","how long until my clips are taken down"),
 ("legal","how long do i have to tell you"),
 ("legal","how long do i have to let you know"),
 ("legal","how long is my project with you for"),
 ("legal","how long do i have with you"),
 ("legal","how long will you have me"),
 ("legal","how long do you have me for"),
 ("legal","what is the timeline to have my clips taken down"),
 ("legal","can my clips be taken down"),
 ("legal","how long should i let you know"),
 ("legal","how long do i have to let you know about my projects"),
 ("legal","what is the turnaround to be with someone"),      # 'someone' denied (control)
 # money (payout / price via allowed words)
 ("money","what is the timeline to get what you have for me"),
 ("money","how long until i get what you have for me"),
 ("money","how long do you take to send what you have for me"),
 ("money","what will my projects take"),
 ("money","how long until you send what is mine"),
 ("money","what is the usual turnaround to get what i have with you"),
 ("money","how long do you take to get it to me for my clips"),
 ("money","how long do you usually take to send it"),
 ("money","when can i get it back"),                            # control: 'back' denied
 ("money","can you take it all off"),                           # control
 # complaint / sarcasm
 ("complaint","how long can you take"),
 ("complaint","how long will this take you this time"),
 ("complaint","how long do you usually take to do what you should"),
 ("complaint","how long should this take you"),
 ("complaint","how long have my projects been with you"),
 ("complaint","how long have you had my projects"),
 ("complaint","how long have you had my clips"),
 ("complaint","is your office open to help me this time"),
 ("complaint","when are you open to do what you should"),
 ("complaint","thanks for the usual turnaround"),
 ("complaint","thanks for the help with my clips"),
 ("complaint","what is the usual timeline you should know"),
 ("complaint","do you have any hours"),
 ("complaint","what hours are you open this time"),
 ("complaint","how long do you take to help"),
 ("complaint","what hours do you usually help anyone"),       # 'anyone' unknown (control)
 ("complaint","when are you open, you thieves"),               # control from brief
 ("complaint","what are your hours, i am done"),               # control from brief
 ("complaint","how long do i have to take this"),
 ("complaint","how long should i have to take this"),
 # opt-out meaning (SMS: answered => no pause, consent kept)
 ("optout","when you open can you let me be"),
 ("optout","can you let me be about the clips"),
 ("optout","how long until you let me be"),
 ("optout","how long will you have my number"),
 ("optout","what hours do you usually text"),                 # 'text' denied (control)
 ("optout","how long until this is over"),
 ("optout","is my account still mine"),                        # brief control
 ("optout","how do i get out"),                                # brief control
 ("optout","can you remove me"),                               # brief control
 ("optout","how long will you be with me"),
 ("optout","what hours should i tell you to let me be"),
 # inflections of lexicon stems (should never be answered)
 ("money","how long do you take for refunding"),
 ("money","how long do chargebacks take"),
 ("privacy","how long do you take for deleting my info"),
 ("privacy","how long until my info is deleted"),
 ("security","how long do you take when hacking is"),
 ("legal","what hours do your lawyers have"),
 ("legal","how long is the cancellation"),
 # positive controls (should be answered)
 ("routine","what are your hours"),
 ("routine","when are you open tomorrow"),
 ("routine","how long does a project usually take"),
 ("routine","where do i send my clips"),
]

INFLECTED = "refunded refunding refunder lawyers lawyered sued suing cancelled cancellation cancels chargebacks hacked hacking hacks breached breaching deleting deleted deletes erased erasing wiped sueing disputing overcharging billing invoicing scammer scamming complained complains reimbursing passwrd taken down stole robbed thieves leaked exposed".split()
CLIP_QUESTIONS = ["Where do I send my clips?"]
TURNAROUND_QUESTIONS = ["How long does a project usually take?", "How long does a project take?"]
POSITIVE_CONTROLS = {"what are your hours", "when are you open tomorrow", "how long does a project usually take",
                     "where do i send my clips"}


def _r3_harness(tmp_path, ports=None):
    h = Harness(tmp_path, ports=ports)
    h.article()
    h.article("turnaround", questions=TURNAROUND_QUESTIONS, answer="Most projects take two weeks.")
    h.article("clips", questions=CLIP_QUESTIONS, answer="Send clips through your portal; we post within 48 hours.")
    return h


# --------------------------------------------------------------------------------------------------- V3-H1

@pytest.mark.parametrize("channel", ["chat", "email", "sms"])
def test_v3_h1_only_exact_approved_questions_are_answered_on_the_r3_corpus(tmp_path, channel):
    h = _r3_harness(tmp_path)
    answered = []
    for i, (_, text) in enumerate(REVIEWER_R3_CORPUS):
        if channel == "chat":
            r = h.chat(text, ref=f"client:c{i}")
        elif channel == "email":
            r = h.email(text, frm=f"u{i}@x.test", subject="Question")
        else:
            ph = f"+1310555{i:04d}"
            cid = h.contact(ref=f"client:s{i}", phone=ph, timezone="America/Los_Angeles")
            h.ok(h.consent(cid), 201)
            r = h.sms(text, frm=ph)
        if r.json().get("action") == "answered":
            answered.append(text)
    assert set(answered) == POSITIVE_CONTROLS          # the four positive controls, nothing else


def test_v3_h1_the_free_vocabulary_mechanism_is_gone(h):
    r = h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "x1x", "brands": ["zbm"], "channels": ["chat"],
                                       "title": "t", "answer": "a", "questions": ["What are your hours?"],
                                       "vocabulary": ["project"]})
    assert r.status_code == 422
    assert not hasattr(triage, "allow_listed") and not hasattr(triage, "BASE_VOCABULARY")


def test_v3_h1_example_questions_are_part_of_what_andre_approves(h):
    saved = h.article()
    edited = h.ok(h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "hours", "brands": ["zbm", "zbc"],
                                                 "channels": ["chat", "email", "sms"], "title": "Opening hours",
                                                 "answer": "We are open Monday to Friday, 9am to 6pm Pacific.",
                                                 "questions": ["What are your hours?", "Can I come by?"]}), 201)
    assert edited["content_sha256"] != saved["content_sha256"] and edited["approved"] is False
    assert h.ok(h.chat("What are your hours?"), 201)["action"] == "queued_for_human"


# --------------------------------------------------------------------------------------------------- V3-M1

@pytest.mark.parametrize("word", [w for w in INFLECTED if w not in ("taken", "down")])
def test_v3_m1_inflections_are_labelled(word):
    assert triage.classify(f"how long until my clips are {word}").categories


def test_v3_m1_taken_down_as_a_phrase():
    assert "contract" in triage.classify("how long until my clips are taken down").categories


@pytest.mark.parametrize("question", ["How long is the refunding?", "When are my clips deleted?",
                                      "Do your lawyers work weekends?", "How long do chargebacks take?",
                                      "Can my clips be taken down?", "Were my clips stolen?", "Is the cancellation free?"])
def test_v3_m1_a_question_with_a_denied_stem_cannot_be_approved(h, question):
    r = h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": "bad", "brands": ["zbm"], "channels": ["chat"],
                                       "title": "t", "answer": "a", "questions": [question]})
    assert r.status_code == 422 and r.json()["detail"] == "QUESTION_DENIED"


def test_v3_m1_deletion_routes_to_compliance_and_legal(h):
    r = h.ok(h.chat("how long until my info is deleted"), 201)
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert {x["department"] for x in t["handoffs"]} >= {"compliance_38", "legal_37"}


# --------------------------------------------------------------------------------------------------- V3-C1

def _world(tmp_path, article=False):
    p = Ports.default()
    s = RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    h.template("nps", "nps_survey")
    if article:
        h.article()
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles", email="owner@acme.test")
    h.ok(h.consent(cid), 201)
    h.account("acct-1", contact_id=cid)
    return h, s, cid


def _survey(h):
    return h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "sms"})


C3_AB = [("email", "Please no texts to my phone, email only from now on."),
         ("email", "I do not want text messages from you."), ("email", "Texts are not welcome. Email me instead."),
         ("chat", "No SMS please, I prefer email."), ("chat", "I don't want any more texts."),
         ("email", "Please quit messaging my cell."), ("chat", "Do not message my phone ever again")]


@pytest.mark.parametrize("channel,text", C3_AB)
def test_v3_c1_an_unanswered_chat_or_email_pauses_proactive_sms(tmp_path, channel, text):
    h, s, cid = _world(tmp_path)
    h.ok(h.email(text, frm="owner@acme.test", subject="Texts") if channel == "email"
         else h.chat(text, ref="client:acme"), 201)
    assert _survey(h).status_code == 409
    h.ok(h.job("outbound-tick"))
    assert not s.sent and h.svc.sms_paused_for(h.svc.contacts[cid])


@pytest.mark.parametrize("text", ["when you open can you let me be", "what hours should i tell you to let me be",
                                  "I need help with my campaign"])
def test_v3_c1_any_unanswered_message_on_any_channel_pauses(tmp_path, text):
    for channel in ("sms", "chat", "email"):
        h, s, cid = _world(tmp_path / channel, article=True)
        r = h.sms(text) if channel == "sms" else (h.chat(text, ref="client:acme") if channel == "chat"
                                                   else h.email(text, frm="owner@acme.test", subject="Question"))
        assert r.json()["action"] != "answered"
        assert _survey(h).json()["detail"] == "SMS_PAUSED"


def test_v3_c1_an_answered_exact_question_does_not_pause(tmp_path):
    h, s, cid = _world(tmp_path, article=True)
    assert h.ok(h.chat("What are your hours?", ref="client:acme"), 201)["action"] == "answered"
    h.ok(_survey(h), 201)


def test_v3_c1_negation_near_a_channel_word_is_labelled_and_alerts_andre(tmp_path):
    h, s, cid = _world(tmp_path)
    r = h.ok(h.chat("No SMS please, I prefer email.", ref="client:acme"), 201)
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert "sms:opt_out_suspected" in t["messages"][0]["triage"]["signals"]
    assert "SMS_OPT_OUT_SUSPECTED" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]


# --------------------------------------------------------------------------------------------------- V3-C2

def test_v3_c2_c4_a_revocation_belongs_to_the_address_not_the_contact(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    h.template("nps", "nps_survey")
    a_num = "+13105551234"
    x = h.contact(ref="client:acme", phone=a_num, timezone="America/Los_Angeles")
    h.ok(h.consent(x), 201)
    h.ok(h.sms("STOP", frm=a_num), 201)
    h.contact(ref="client:acme", phone="+13105550001", timezone="America/Los_Angeles")
    y = h.contact(ref="client:acme-2", phone=a_num, timezone="America/Los_Angeles")
    r = h.consent(y, captured_at="2026-10-06T18:00:00Z")          # the pre-STOP capture, replayed for Y
    assert r.status_code == 409 and r.json()["detail"] == "CONSENT_PREDATES_REVOCATION"
    h.account("acct-1", contact_id=y)
    assert _survey(h).status_code == 409
    h.ok(h.job("outbound-tick"))
    assert [m.to for m in s.sent if m.to == a_num and "unsubscribed" not in m.text] == []


def test_v3_c2_a_capture_before_the_contact_existed_is_refused(h):
    cid = h.contact(phone="+13105551234")
    r = h.consent(cid, captured_at="2026-10-01T10:00:00Z")
    assert r.status_code == 409 and r.json()["detail"] == "CONSENT_PREDATES_CONTACT"


def test_v3_c2_a_revocation_of_the_address_after_a_capture_kills_that_consent_at_send_time(tmp_path):
    h, s, cid = _world(tmp_path)
    c = h.svc.contacts[cid]
    assert h.svc._live_consent(c, "sms")["status"] == "active"
    # the address was revoked (on whichever contact held it) after this consent was captured
    h.svc.addr_revocations[("zbm", "sms", h.svc._addr_key("sms", "+13105551234"))] = "2026-10-06T18:00:01Z"
    assert h.svc._live_consent(c, "sms") is None
    assert _survey(h).json()["detail"] == "SMS_CONSENT_REQUIRED"
    h.svc.addr_revocations[("zbc", "sms", h.svc._addr_key("sms", "+13105551234"))] = "2026-10-06T18:00:01Z"
    del h.svc.addr_revocations[("zbm", "sms", h.svc._addr_key("sms", "+13105551234"))]
    assert h.svc._live_consent(c, "sms")["status"] == "active"          # per brand


# --------------------------------------------------------------------------------------------------- V3-C3

def test_v3_c3_c5_the_confirmation_goes_only_to_the_stop_number(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    x = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(x), 201)
    r = h.ok(h.sms("STOP", frm="+13105551234"), 201)
    assert r["confirmation_message_id"]
    h.contact(phone="+13105550001", timezone="America/Los_Angeles")   # the hub updates the number before the tick
    tick = h.ok(h.job("outbound-tick"))
    assert tick["cancelled"] == 1 and not s.sent
    assert h.svc.messages[r["confirmation_message_id"]]["reason"] == "ADDRESS_CHANGED"


def test_v3_c3_the_confirmation_is_addressed_to_the_bound_number(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    x = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(x), 201)
    h.ok(h.sms("STOP", frm="+13105551234"), 201)
    h.ok(h.job("outbound-tick"))
    assert [m.to for m in s.sent] == ["+13105551234"]


# --------------------------------------------------------------------------------------------------- V3-M2

def test_v3_m2_an_opt_out_from_an_unknown_address_naming_the_contact_pauses_it(tmp_path):
    h, s, cid = _world(tmp_path)
    r = h.ok(h.email("STOP texting me at 310-555-1234", frm="personal@other.test", subject="stop"), 201)
    assert r["opted_out"] is True and r["action"] in ("queued_for_human", "escalated")
    assert h.svc.sms_paused_for(h.svc.contacts[cid])
    assert _survey(h).json()["detail"] == "SMS_PAUSED"
    assert "OPT_OUT_UNKNOWN_SENDER" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]


def test_v3_m2_a_stop_from_a_second_number_opens_a_ticket_for_andre(tmp_path):
    h, s, cid = _world(tmp_path)
    r = h.ok(h.sms("STOP texting my other phone", frm="+13105559876"), 201)
    assert r["opted_out"] is True and r["ticket_id"]
    assert h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))["queue"] == "andre"


# --------------------------------------------------------------------------------------------------- V3-L1, V3-L2

def test_v3_l1_the_service_generates_its_key(tmp_path):
    d = tmp_path / "d"
    h = Harness(tmp_path, data_dir=str(d))
    key_file = d / "hmac.key"
    assert (os.stat(key_file).st_mode & 0o777) == 0o600 and len(base64.b64decode(key_file.read_bytes())) == 32
    first = h.settings.hmac_key
    assert h.restart().settings.hmac_key == first
    assert any(r["kind"] == "key_fingerprint" for r in h.svc.log.iter_records())


def _keyfile(tmp_path, name, raw):
    p = tmp_path / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, base64.b64encode(raw))
    os.close(fd)
    return str(p)


@pytest.mark.parametrize("raw", [bytes(range(16)) * 2, b"0123456789abcdef0123456789abcdef", b"a" * 31 + b"b",
                                 bytes(32), os.urandom(16) * 2, b"correct horse battery staple 123"])
def test_v3_l1_supplied_keys_must_look_random(tmp_path, raw):
    with pytest.raises(RuntimeError, match="weak key"):
        config_mod.load(base_env(SVC_HMAC_KEY_FILE=_keyfile(tmp_path, "weak", raw)))


def test_v3_l2_a_log_from_before_the_fingerprint_is_checked_against_its_bodies(tmp_path, monkeypatch):
    k1, k2 = _keyfile(tmp_path, "k1", os.urandom(32)), _keyfile(tmp_path, "k2", os.urandom(32))
    real = service_mod.SupportService._commit

    def no_fingerprint(self, kind, data, actor):
        if kind == "key_fingerprint":
            return None                                   # a log written before round 2
        return real(self, kind, data, actor)
    monkeypatch.setattr(service_mod.SupportService, "_commit", no_fingerprint)
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), SVC_HMAC_KEY_FILE=k1)
    h.ok(h.chat("I need help with my campaign"), 201)
    assert not any(r["kind"] == "key_fingerprint" for r in h.svc.log.iter_records())
    monkeypatch.setattr(service_mod.SupportService, "_commit", real)
    with pytest.raises(store_mod.StoreCorrupt, match="predates the key fingerprint"):
        h.restart(SVC_HMAC_KEY_FILE=k2)
    h2 = h.restart(SVC_HMAC_KEY_FILE=k1)
    assert any(r["kind"] == "key_fingerprint" for r in h2.svc.log.iter_records())


def test_v3_l2_rotation_is_documented_as_not_supported():
    readme = (os.path.dirname(__file__) + "/../README.md")
    assert "Key rotation is not supported" in " ".join(open(readme).read().split())


# --------------------------------------------------------------------------------------------------- V3-L3

def test_v3_l3_pause_and_clear_are_typed_ledger_events(tmp_path):
    h, s, cid = _world(tmp_path)
    h.ok(h.sms("hmm whatever"), 201)
    h.ok(h.post(f"/svc/v1/contacts/{cid}/sms-pause/clear", {"request_id": rid()}, andre=True))
    assert h.ledger.of_type("sms_paused") and h.ledger.of_type("sms_pause_cleared")
    assert "+13105551234" not in str(h.ledger.events)


# --------------------------------------------------------------------------------------------------- Info

@pytest.mark.parametrize("channel", ["chat", "email"])
def test_info_a_typo_only_opt_out_by_chat_or_email_pauses_and_never_revokes(tmp_path, channel):
    h, s, cid = _world(tmp_path)
    text = "do you have a shop"
    h.ok(h.chat(text, ref="client:acme") if channel == "chat" else h.email(text, frm="owner@acme.test"), 201)
    assert h.svc.consents[(cid, "sms")]["status"] == "active"
    assert _survey(h).json()["detail"] == "SMS_PAUSED"
    assert "SMS_OPT_OUT_SUSPECTED" in [a["code"] for a in h.ok(h.get("/svc/v1/alerts"))]


@pytest.mark.parametrize("text", ["stop", "unsubscribe", "Please STOP texting me"])
def test_info_an_exact_stop_by_chat_still_revokes(tmp_path, text):
    h, s, cid = _world(tmp_path)
    h.ok(h.chat(text, ref="client:acme"), 201)
    assert h.svc.consents[(cid, "sms")]["status"] == "revoked"
