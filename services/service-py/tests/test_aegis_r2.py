"""AEGIS round 2 (Oct 5 2026, BLOCKING): one regression per finding, each the reviewer's scenario
(scratchpad/aegis-service-r2/c1..c6). The routine gate is now an allow-list (fail closed)."""

from __future__ import annotations

import base64
import json
import os

import pytest

import channels
import config as config_mod
import store as store_mod
from helpers import Harness, RecordingSender, base_env, rid
from ports import Outbound, Ports

# the reviewer's c1 corpus, verbatim (89 messages: money, legal, complaint, security, every one glued to a routine
# anchor so an approved article matches)
REVIEWER_C1_CORPUS = [
 # paraphrases, each glued to a routine anchor so an article matches
 "I want my money back",
 "When are you open so I can get my money returned",
 "you overcharged me",
 "When are you open since you took too much off my card",
 "dispute with my card company",
 "What hours are you open before I call my card company",
 "Im done with you guys so what hours are you open",
 "What hours are you open because im done with you",
 "this is a scam",
 "What hours are you open you con artists",
 "delete my data",
 "What hours are you open to wipe out everything you have on me",
 "my attorney",
 "What hours are you open my atty wants to know",
 "breach of contract",
 "What hours are you open since you broke our deal",
 "someone changed my password",
 "What hours are you open since someone changed my pw",
 "What hours are you open someone reset my log in",
 "When are you open to sort out the overbilling",
 "When are you open to fix the double billing",
 "When are you open to give me a re-fund",
 "When are you open for my re fund",
 "What hours are you open for a charge-back",
 "What hours are you open for my pass-word reset",
 "What hours are you open my law-yer asks",
 "When are you open to discuss the reimbursment",
 # French / Portuguese without accents
 "Quand etes vous open pour mon remboursement",
 "Quels sont vos hours je veux etre rembourse",
 "Quero meu dinheiro de volta hours open",
 "Qual o horario open quero o estorno",
 "Vou processar voces hours open",
 "Meu advogado pergunta your hours open",
 "Apaguem meus dados hours open",
 "Alguem mudou minha senha hours open",
 "Quand etes vous open mon avocat demande",
 "Supprimez mes donnees hours open",
 "Quelqu un a change mon mot de passe hours open",
 # emoji anger
 "\U0001F621\U0001F621\U0001F621",
 "What hours are you open \U0001F92C\U0001F92C",
 "What hours are you open \U0001F595",
 "open hours \U0001F4B8\U0001F4B8 \U0001F621",
 # two-typo misspellings
 "What hours are you open for my rfnud",
 "What hours are you open for my reefnd",
 "What hours are you open my atorny asks",
 "What hours are you open my laywr asks",
 "What hours are you open someone hackd my pasword",
 "What hours are you open for the chargbak",
 "What hours are you open about my cmplant",
 "What hours are you open about my paswrd",
 # split across lines / separators
 "What hours are you open for my ref\nund",
 "What hours are you open for my ref\rund",
 "What hours are you open for my ref und",
 "What hours are you open for my ref und",
 "What hours are you open for my re\u0085fund",
 "What hours are you open for my r  e  f  u  n  d",
 # very short
 "refund?", "money?", "lawyer?", "scam?", "hacked?", "cancel?", "sue?", "chargeback?", "open?",
 # routine question plus a money hint in a separate short sentence
 "What are your hours? Refund.",
 "What hours are you open? $$",
 "What are your hours. Overcharged",
 "What hours are you open? Dispute pending.",
 # misc paraphrases
 "What hours are you open since i want what i paid for back",
 "When are you open so I can stop my card",
 "When are you open since you robbed me",
 "When are you open since you stole from me",
 "What hours are you open I want out of this deal",
 "What hours are you open I am reporting you to the AG",
 "What hours are you open my bank flagged you",
 "What hours are you open I never authorized this",
 "What hours are you open since my account got taken over",
 "What hours are you open since i got a weird login link",
 "What hours are you open since i didnt sign up for this",
 "When are you open since you guys are thieves",
 "When are you open I will see you in court",
 "When are you open my legal team is ready",
 "When are you open since I want my 500 back",
 "When are you open since this cost me 500 bucks",
 "When are you open since you took 500 from me",
 "When are you open so I can unenroll and get credited",
 "When are you open since i was billed twice",
 "When are you open to stop autopay",
]

OPT_OUT_VARIANTS = ["stopp","stahp","unsubcribe","unsusbcribe","🛑","✋ no more","arrete","arrête","sair","SAIR","cancele","descadastrar",
"Please don't send me texts","Remove my number","take my number off your list","lose my number","I didn't sign up for this",
"never text me again","don't text me anymore","dont txt me","quit texting","STOP​ALL","s.t.o.p","st0p","S-T-O-P","5TOP",
"opt-out","unsubscribe​","halt","desist","no more","Who is this?","pls stop","stoooop","STOPPPP","ending","cancelled"]


TURNAROUND_QUESTIONS = ["How long does a project take?", "What is the usual turnaround?"]


def _corpus_harness(tmp_path):
    h = Harness(tmp_path)
    h.article()
    h.article("turnaround", questions=TURNAROUND_QUESTIONS, answer="Most projects take two weeks.")
    return h


# --------------------------------------------------------------------------------------------------- V2-H1

def test_v2_h1_the_corpus_has_89_messages():
    assert len(REVIEWER_C1_CORPUS) == 89


@pytest.mark.parametrize("channel", ["chat", "email", "sms"])
def test_v2_h1_zero_auto_answers_on_the_reviewers_corpus(tmp_path, channel):
    h = _corpus_harness(tmp_path)
    answered = []
    for i, text in enumerate(REVIEWER_C1_CORPUS):
        if channel == "chat":
            r = h.chat(text, ref=f"client:c{i}")
        elif channel == "email":
            r = h.email(text, frm=f"u{i}@x.test", subject="Hours")
        else:
            ph = f"+1310555{i:04d}"
            cid = h.contact(ref=f"client:s{i}", phone=ph, timezone="America/Los_Angeles")
            h.ok(h.consent(cid), 201)
            r = h.sms(text, frm=ph)
        if r.json().get("action") == "answered":
            answered.append(text)
    assert answered == []


POSITIVE = [
    ("What are your hours?", "hours"), ("When are you open?", "hours"), ("What are your opening hours?", "hours"),
    ("Hi, what are your hours?", "hours"), ("Hello! When are you open tomorrow?", "hours"),
    ("Are you open on Saturday?", "hours"), ("what time do you open, thanks", "hours"),
    ("How long does a project take?", "turnaround"), ("What is the usual turnaround please", "turnaround"),
]


@pytest.mark.parametrize("text,article", POSITIVE)
def test_v2_h1_exact_approved_questions_are_still_answered(tmp_path, text, article):
    h = _corpus_harness(tmp_path)
    r = h.ok(h.chat(text), 201)
    assert r["action"] == "answered" and r["answer"]["article_id"] == article


# --------------------------------------------------------------------------------------------------- V2-H2

def _sms_setup(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    h.template("nps", "nps_survey")
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles", email="owner@acme.test")
    h.account("acct-1", contact_id=cid)
    return h, s, cid


def _survey(h):
    return h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "sms"})


def test_v2_h2_c6_a_replay_after_a_number_change_is_refused(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)                                       # for A, captured at T0
    h.clock.advance(minutes=1)
    h.contact(phone="+13105559999", timezone="America/Los_Angeles")  # the hub changes the number to B
    r = h.consent(cid, captured_at="2026-10-06T18:00:00Z")          # the SAME capture, replayed, now for B
    assert r.status_code == 409 and r.json()["detail"] == "CONSENT_PREDATES_ADDRESS"
    assert _survey(h).status_code == 409
    h.ok(h.job("outbound-tick"))
    assert not s.sent
    h.ok(h.consent(cid, captured_at="2026-10-06T18:01:00Z"), 201)  # a capture made for B, after the change


def test_v2_h2_the_consent_names_the_current_address(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    r = h.consent(cid, address="+13105550000")
    assert r.status_code == 409 and r.json()["detail"] == "CONSENT_ADDRESS_MISMATCH"
    r = h.post("/svc/v1/consents", {"request_id": rid(), "contact_id": cid, "channel": "sms", "source": "portal_form",
                                    "consent_text": "yes", "captured_at": "2026-10-01T10:00:00Z", "express": True},
               caller="hub")
    assert r.status_code == 422                                     # the address is required


# --------------------------------------------------------------------------------------------------- V2-H3

C3 = [("sms", "never text me again"), ("sms", "Remove my number"), ("sms", "stopp"), ("sms", "\U0001F6D1"),
      ("chat", "Stop texting me"), ("email", "STOP sending me texts")]


@pytest.mark.parametrize("channel,text", C3)
def test_v2_h3_c3_every_case_ends_with_no_further_sms(tmp_path, channel, text):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    if channel == "sms":
        h.ok(h.sms(text), 201)
    elif channel == "chat":
        h.ok(h.chat(text, ref="client:acme"), 201)
    else:
        h.ok(h.email(text, frm="owner@acme.test", subject="STOP"), 201)
    assert _survey(h).status_code == 409
    h.ok(h.job("outbound-tick"))
    assert h.svc.consents[(cid, "sms")]["status"] == "revoked"
    # only the one opt-out confirmation, and only to someone who opted out by SMS
    assert [m.text[:16] for m in s.sent] == (["You are unsubscr"] if channel == "sms" else [])


@pytest.mark.parametrize("text", [x for x in OPT_OUT_VARIANTS if x != "ending"])
def test_v2_h3_opt_out_variants(text):
    assert channels.is_opt_out(text)


def test_v2_h3_any_unclear_inbound_sms_pauses_proactive_sms_until_andre_clears_it(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    h.ok(h.sms("hmm ok whatever"), 201)
    r = _survey(h)
    assert r.status_code == 409 and r.json()["detail"] == "SMS_PAUSED"
    clear = f"/svc/v1/contacts/{cid}/sms-pause/clear"
    assert h.post(clear, {"request_id": rid()}).status_code == 403              # Andre only
    out = h.ok(h.post(clear, {"request_id": rid()}, andre=True))
    assert out["sms_consent"] == "active"
    h.ok(_survey(h), 201)


def test_v2_h3_a_routine_answered_sms_does_not_pause(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    h.article()
    assert h.ok(h.sms("What are your hours?"), 201)["action"] == "answered"
    h.ok(_survey(h), 201)


def test_v2_h3_clearing_a_pause_never_restores_a_revoked_consent(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    h.ok(h.sms("never text me again"), 201)
    out = h.ok(h.post(f"/svc/v1/contacts/{cid}/sms-pause/clear", {"request_id": rid()}, andre=True))
    assert out["sms_consent"] == "revoked"
    assert _survey(h).json()["detail"] == "SMS_CONSENT_REQUIRED"


# --------------------------------------------------------------------------------------------------- V2-M1, V2-L1

def _keyfile(tmp_path, name, raw):
    p = tmp_path / name
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, base64.b64encode(raw))
    os.close(fd)
    return str(p)


def test_v2_m1_a_different_key_refuses_to_start(tmp_path):
    k1, k2 = _keyfile(tmp_path, "k1", os.urandom(32)), _keyfile(tmp_path, "k2", os.urandom(32))
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), SVC_HMAC_KEY_FILE=k1)
    cid = h.contact(phone="+13105551234")
    h.ok(h.consent(cid), 201)
    assert any(r["kind"] == "key_fingerprint" for r in h.svc.log.iter_records())
    with pytest.raises(store_mod.StoreCorrupt, match="fingerprint mismatch"):
        h.restart(SVC_HMAC_KEY_FILE=k2)
    h2 = h.restart(SVC_HMAC_KEY_FILE=k1)
    assert h2.svc.integrity["ok"] and h2.svc.bodies.get(h2.svc.consents[(cid, "sms")]["consent_text_sha256"])


@pytest.mark.parametrize("raw", [bytes(32), bytes(range(15)) * 3, b"ab" * 32])
def test_v2_l1_weak_keys_are_refused(tmp_path, raw):
    with pytest.raises(RuntimeError, match="weak key"):
        config_mod.load(base_env(SVC_HMAC_KEY_FILE=_keyfile(tmp_path, "weak", raw)))


# --------------------------------------------------------------------------------------------------- V2-L2

def test_v2_l2_a_bad_optional_andre_token_still_revokes_as_dashboard(h):
    cid = h.contact(phone="+13105551234")
    h.ok(h.consent(cid), 201)
    r = h.client.post("/svc/v1/consents/revoke", json={"request_id": rid(), "contact_id": cid, "channel": "sms"},
                      headers={**h.headers(), "X-Andre-Approval-Token": "wrong-token-" + "x" * 30})
    assert r.status_code == 200
    c = h.ok(h.get(f"/svc/v1/contacts/{cid}/consents"))[0]
    assert c["status"] == "revoked" and c["revoked_via"] == "dashboard"


# --------------------------------------------------------------------------------------------------- V2-L4

def test_v2_l4_the_file_sender_is_non_production_only(tmp_path):
    out = str(tmp_path / "outbox.jsonl")
    with pytest.raises(RuntimeError, match="only with SVC_NON_PRODUCTION=1"):
        config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_SMS_PROVIDER="nonprod_file",
                                 SVC_NONPROD_OUTBOX_FILE=out, SVC_DATA_DIR=str(tmp_path / "d"),
                                 SVC_HMAC_KEY_FILE=_keyfile(tmp_path, "k", os.urandom(32))))
    with pytest.raises(RuntimeError, match="SVC_NONPROD_OUTBOX_FILE"):
        config_mod.load(base_env(SVC_NONPROD_OUTBOX_FILE=out))
    s = config_mod.load(base_env(SVC_SMS_PROVIDER="nonprod_file", SVC_NONPROD_OUTBOX_FILE=out))
    import api
    sender = api.build_ports(s).senders["sms"]
    assert sender.send(Outbound("m1", "zbm", "sms", "+1", "+2", None, "hi")) == "sent"
    assert json.loads(open(out).read())["message_id"] == "m1"


# --------------------------------------------------------------------------------------------------- unverified items

def test_render_is_single_pass(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    tpl = h.svc.catalog["template"][h.template("offer-msg", "offer")["item_id"]]
    out = h.svc._render(tpl, {"display_name": "{offer_terms} Smith"},
                        {"offer_title": "T", "offer_terms": "TERMS", "offer_price": "$1.00"})
    assert out.count("TERMS") == 1 and "{offer_terms}" in out


def test_braces_in_a_display_name_are_refused(h):
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:x",
                                    "display_name": "{offer_terms}"}, caller="hub")
    assert r.status_code == 422


def _plan_setup(tmp_path):
    from helpers import FixedSignals
    p = Ports.default()
    sig = FixedSignals("down", "failed")
    p.results, p.finance = sig, sig
    s = RecordingSender()
    p.senders["chat"] = s
    h = Harness(tmp_path, ports=p)
    cid = h.contact(display_name="Dana Rivera")
    h.account("acct-1", contact_id=cid)
    h.ok(h.job("health-recompute"))
    return h, s, h.ok(h.get("/svc/v1/save-plans?status=active"))[0]["plan_id"]


def test_a_template_edited_after_the_check_in_was_queued_is_never_sent(tmp_path):
    h, s, pid = _plan_setup(tmp_path)
    h.template("checkin", "check_in")
    assert h.ok(h.job("save-plan-tick"))["check_ins"] == 1
    h.template("checkin", "check_in", text="Hi {first_name}, edited text.", approve=False)
    tick = h.ok(h.job("outbound-tick"))
    assert tick["cancelled"] == 1 and not s.sent
    assert [m for m in h.ok(h.get("/svc/v1/outbound")) if m["reason"] == "TEMPLATE_NOT_APPROVED"]


def test_an_offer_retired_after_selection_is_never_queued(tmp_path):
    h, s, pid = _plan_setup(tmp_path)
    h.template("offer-msg", "offer")
    h.offer("month-free")
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}, andre=True))
    h.ok(h.post("/svc/v1/offers/month-free/retire", {"request_id": rid()}, andre=True))
    assert h.ok(h.job("save-plan-tick"))["offers"] == 0
    step = h.ok(h.get("/svc/v1/save-plans?status=active"))[0]["steps"][2]
    assert step["status"] == "awaiting_selection" and step["reason"] == "OFFER_NOT_APPROVED"


def test_an_offer_template_retired_after_selection_blocks_the_step(tmp_path):
    h, s, pid = _plan_setup(tmp_path)
    h.template("offer-msg", "offer")
    h.offer("month-free")
    h.ok(h.post(f"/svc/v1/save-plans/{pid}/offer", {"request_id": rid(), "offer_id": "month-free"}, andre=True))
    h.ok(h.post("/svc/v1/templates/offer-msg/retire", {"request_id": rid()}, andre=True))
    assert h.ok(h.job("save-plan-tick"))["offers"] == 0
    assert h.ok(h.get("/svc/v1/save-plans?status=active"))[0]["steps"][2]["reason"] == "NO_APPROVED_TEMPLATE"
    h.ok(h.job("outbound-tick"))
    assert not s.sent


@pytest.mark.parametrize("subject,answered", [("ugh", False), ("you con artists", False), ("Hours \U0001F621", False),
                                              ("Hours", False), ("Question", True), ("Quick question", True),
                                              ("What are your opening hours?", True), ("Opening hours", True)])
def test_v2_h1_the_email_subject_must_be_neutral(h, subject, answered):
    h.article()
    r = h.ok(h.email("What are your opening hours?", subject=subject), 201)
    assert (r["action"] == "answered") is answered
