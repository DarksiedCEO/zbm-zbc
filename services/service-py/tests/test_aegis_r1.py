"""AEGIS round 1 (Oct 5 2026, BLOCKING): one regression per finding, each the reviewer's scenario
(scratchpad/aegis-service-r1/r1..r6)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest

import channels
import triage
from helpers import ANDRE_TOKEN, ZBM_SMS, Harness, RecordingSender, rid
from ports import Ports


def _sms_setup(tmp_path, sender=None):
    p = Ports.default()
    s = sender or RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    h.template("nps", "nps_survey")
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    h.account("acct-1", contact_id=cid)
    return h, s, cid


def _survey(h):
    return h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "sms"})


# --------------------------------------------------------------------------------------------------- V1-C1

def test_v1_c1_consent_follows_the_number_not_the_contact(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)                                      # given by the owner of +13105551234
    h.contact(phone="+13105559999")                                # the hub changes the number
    r = _survey(h)
    assert r.status_code == 409 and r.json()["detail"] == "SMS_CONSENT_REQUIRED"
    h.ok(h.job("outbound-tick"))
    assert not s.sent
    c = h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))[0]
    assert c["status"] == "revoked" and c["revoked_via"] == "address_changed"


def test_v1_c1_check_requires_the_consent_address_to_be_the_current_number():
    from helpers import T0
    contact = {"contact_id": "c", "phone": "+13105559999", "timezone": "America/Los_Angeles"}
    consent = {"status": "active", "address": "+13105551234"}
    assert channels.check("sms", contact, lambda ch: consent, True, T0) == "SMS_CONSENT_REQUIRED"
    consent["address"] = "+13105559999"
    assert channels.check("sms", contact, lambda ch: consent, True, T0) is None
    assert channels.check("sms", contact, lambda ch: {"status": "active"}, True, T0) == "SMS_CONSENT_REQUIRED"


def test_v1_c1_email_consent_is_bound_to_the_address_too(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.contact(email="old@acme.test")
    h.ok(h.consent(cid, "email"), 201)
    h.contact(email="new@acme.test")
    r = h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "email"})
    assert r.json()["detail"] == "EMAIL_CONSENT_REQUIRED"


# --------------------------------------------------------------------------------------------------- V1-C2

def test_v1_c2_a_stop_that_arrives_mid_tick_is_honoured(tmp_path):
    holder = {}

    class StopDuringSend(RecordingSender):
        def send(self, msg):
            self.sent.append(msg)
            if len(self.sent) == 1:                     # the contact replies STOP while the tick is still sending
                holder["r"] = holder["h"].svc.inbound("sms_gateway", "sms", {
                    "request_id": rid(), "brand": "zbm", "to_number": ZBM_SMS, "from_number": "+13105551234",
                    "text": "STOP"})
            return "sent"
    h, s, cid = _sms_setup(tmp_path, StopDuringSend())
    holder["h"] = h
    h.ok(h.consent(cid), 201)
    for _ in range(2):
        h.ok(_survey(h), 201)
    tick = h.ok(h.job("outbound-tick"))
    assert holder["r"]["action"] == "opted_out"
    assert len(s.sent) == 1 and tick["sent"] == 1                  # the second survey never reached the provider
    surveys = [m for m in h.svc.messages.values() if m["dir"] == "out" and m["origin"] == "template"]
    assert sorted(m["status"] for m in surveys) == ["cancelled", "sent"]


def test_v1_c2_a_cancelled_message_never_flips_to_sent(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    mid = h.ok(_survey(h), 201)["message_id"]
    h.svc._commit("outbound_cancelled", {"effects": [{"op": "message_status", "message_id": mid,
                                                      "status": "cancelled", "reason": "CONSENT_REVOKED"}]}, "service_desk")
    h.svc._commit("outbound_result", {"effects": [{"op": "message_status", "message_id": mid, "status": "sent",
                                                   "reason": None}]}, "service_desk")
    m = h.svc.messages[mid]
    assert m["status"] == "cancelled" and m["provider_accepted"] is True


def test_v1_c2_an_article_edited_between_pick_and_send_is_not_sent(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["email"] = s
    h = Harness(tmp_path, ports=p)
    h.article()
    h.ok(h.email("What are your opening hours?"), 201)
    real = h.svc._send_block
    calls = {"n": 0}

    def edit_on_second_check(m):
        calls["n"] += 1
        if calls["n"] == 2:                      # between the pick and the send: Andre edits the article
            h.svc.catalog["kb"]["hours"]["approved"] = None
        return real(m)
    h.svc._send_block = edit_on_second_check
    assert h.ok(h.job("outbound-tick"))["cancelled"] == 1 and not s.sent


# --------------------------------------------------------------------------------------------------- V1-H1

REVIEWER_CORPUS = {
    "zero-width in refund": "What are your opening hours? I want a re​fund now",
    "cyrillic e in refund": "What are your opening hours? I want a rеfund",
    "spanish reembolso": "Cuales son sus hours? Quiero un reembolso de mi dinero",
    "spanish abogado/demanda": "What hours are you open? Mi abogado presentara una demanda",
    "misspelling refnd": "What are your hours? I need a refnd",
    "synonym reimburse": "What are your hours? Please reimburse me",
    "synonym return my money": "What hours are you open? Return my money",
    "html split": "What are your hours? I want a re<b></b>fund",
    "html entity": "What are your hours? I want a &#114;efund",
    "soft hyphen": "What are your hours? I want a re­fund",
    "end my service": "What are your hours? I want to end my service and stop paying",
    "complaint synonym": "What are your hours? Your service is garbage and pathetic",
    "security synonym": "What are your hours? Someone else got into my account",
    "chargeback via bank": "What are your hours? I'm calling my bank to reverse this",
    "legal synonym": "What are your hours? See you in small-claims; my solicitor agrees",
    "fullwidth": "What are your hours? I want a ｒｅｆｕｎｄ",
    "plain refund": "What are your hours? I want a refund",
}

# each alone, with nothing routine around it: the category must fire
ISOLATED = [
    ("I want a re​fund", "money"), ("I want a rеfund", "money"), ("Quiero un reembolso", "money"),
    ("Mi abogado presentara una demanda", "contract"), ("I need a refnd", "money"), ("please reimburse me", "money"),
    ("I want a re<b></b>fund", "money"), ("I want a &#114;efund", "money"), ("I want a &amp;#114;efund", "money"),
    ("I want a re­fund", "money"), ("filing a chargebak", "money"), ("please reverse this", "money"),
    ("someone got into my account", "security"), ("I want to end my service", "contract"),
    ("I want a ｒｅｆｕｎｄ", "money"), ("r e f u n d", "money"),
    ("my solicitor will call", "contract"), ("your service is garbage", "complaint"),
    ("borrar mis datos por favor", "privacy"), ("mi contrasena no funciona", "security"),
    ("esto es una estafa", "complaint"), ("quiero cancelar", "contract"), ("it cost me $500", "money"),
    ("I was overcharged", "money"), ("my lawyr says so", "contract"), ("this is a ref⁠und request", "money"),
]


@pytest.mark.parametrize("name", sorted(REVIEWER_CORPUS))
def test_v1_h1_reviewer_corpus_is_never_auto_answered(h, name):
    h.article()
    r = h.ok(h.chat(REVIEWER_CORPUS[name], ref=f"client:c{abs(hash(name)) % 10 ** 8}"), 201)
    assert r["action"] == "escalated" and "answer" not in r


@pytest.mark.parametrize("text,cat", ISOLATED)
def test_v1_h1_evasions_still_fire_their_category(text, cat):
    t = triage.classify(text)
    assert cat in t.categories and not t.routine_candidate


@pytest.mark.parametrize("text", ["What are your hours? Thanks so much", "What are your hours and do you work weekends",
                                  "What are your hours, also where are you", "Hours? Address?",
                                  "Какие часы работы?",
                                  "営業時間は？", " ".join(["what are your hours"] * 7)])
def test_v1_h1_only_short_single_intent_ascii_messages_are_routine(h, text):
    h.article()
    assert h.ok(h.chat(text), 201)["action"] != "answered"


@pytest.mark.parametrize("raw,cleaned", [
    ("re\u200bfund", "refund"), ("re\u00adfund", "refund"), ("re\u2060fu\u200dnd", "refund"),   # format chars
    ("&#114;efund", "refund"), ("&amp;#114;efund", "refund"), ("re&shy;fund", "refund"),            # entities
    ("re<b></b>fund", "refund"), ("&lt;i&gt;re&lt;/i&gt;fund", "refund"),                          # tags
    ("\uff52\uff45\uff46\uff55\uff4e\uff44", "refund"),                                           # NFKC
    ("r\u0435fund", "refund"), ("\u0440\u0435f\u03c5nd", "pefund"),                              # confusables
])
def test_v1_h1_each_cleaning_step(raw, cleaned):
    assert triage.clean(raw) == cleaned


def test_v1_h1_a_surviving_non_ascii_letter_sends_the_message_to_a_human(h):
    h.article()
    text = "What are your hours \u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430"
    assert triage.classify(text).signals == ("other:non_ascii",)
    assert h.ok(h.chat(text), 201)["action"] == "queued_for_human"


@pytest.mark.parametrize("text", ["What are your hours?", "Hi! what are your opening hours?",
                                  "Hello, when are you open?"])
def test_v1_h1_a_simple_question_is_still_answered(h, text):
    h.article()
    assert h.ok(h.chat(text), 201)["action"] == "answered"


# --------------------------------------------------------------------------------------------------- V1-H2

def test_v1_h2_the_email_subject_is_triaged(h):
    h.article()
    r = h.ok(h.email("What are your opening hours?", subject="REFUND my $500 or I sue - chargeback filed"), 201)
    assert r["action"] == "escalated"
    t = h.ok(h.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert set(t["categories"]) >= {"money", "contract"} and t["queue"] == "andre"
    assert not [m for m in t["messages"] if m["dir"] == "out"]


def test_v1_h2_a_long_subject_is_a_second_intent(h):
    h.article()
    r = h.ok(h.email("What are your opening hours?", subject="Hours. Also a question about my campaign"), 201)
    assert r["action"] == "queued_for_human"


# --------------------------------------------------------------------------------------------------- V1-H3

@pytest.mark.parametrize("text", ["STOP", "Stop.", "Please stop", "stop texting me", "STOP!!!", "Stop please",
                                  "leave me alone", "ALTO", "PARAR", "unsubscribe me", "S T O P", "no more texts",
                                  "remove me", "wrong number", "cancelar", "baja", "s​top", "ＳＴＯＰ",
                                  "no", "nope", "end"])
def test_v1_h3_opt_out_phrases_revoke(tmp_path, text):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    r = h.ok(h.sms(text), 201)
    assert r.get("action") == "opted_out" or r.get("opted_out") is True
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))[0]["status"] == "revoked"


def test_v1_h3_one_confirmation_is_sent_and_only_once(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    h.ok(h.sms("Please stop texting me"), 201)
    second = h.ok(h.sms("STOP"), 201)
    assert "confirmation_message_id" not in second
    h.ok(h.job("outbound-tick"))
    assert len(s.sent) == 1 and "unsubscribed" in s.sent[0].text
    assert _survey(h).json()["detail"] == "SMS_CONSENT_REQUIRED"


def test_v1_h3_a_longer_message_with_an_opt_out_still_reaches_andre(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)
    r = h.ok(h.sms("stop texting me and refund my last invoice"), 201)
    assert r["opted_out"] is True and r["action"] == "escalated"


# --------------------------------------------------------------------------------------------------- V1-H4

def test_v1_h4_a_consent_captured_before_a_stop_cannot_bring_it_back(tmp_path):
    h, s, cid = _sms_setup(tmp_path)
    h.ok(h.consent(cid), 201)                                      # captured 2026-10-01
    h.ok(h.sms("STOP"), 201)
    r = h.consent(cid)                                             # a replay of the same old capture
    assert r.status_code == 409 and r.json()["detail"] == "CONSENT_PREDATES_REVOCATION"
    r = h.consent(cid, captured_at="2026-10-06T18:00:00Z")        # the very second of the STOP: still refused
    assert r.status_code == 409
    assert _survey(h).status_code == 409
    h.ok(h.job("outbound-tick"))
    assert [m.text[:16] for m in s.sent] == ["You are unsubscr"]


# --------------------------------------------------------------------------------------------------- V1-M1

def test_v1_m1_older_consent_evidence_survives_restart(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    r1 = h.ok(h.consent(cid, text="ORIGINAL wording v1"), 201)
    h.clock.advance(minutes=1)
    h.ok(h.consent(cid, text="NEW wording v2"), 201)
    h2 = h.restart()
    assert h2.svc.bodies.get(r1["consent_text_sha256"]) == "ORIGINAL wording v1"


# --------------------------------------------------------------------------------------------------- V1-M2

def test_v1_m2_audit_export_uses_a_per_export_hmac_never_a_plain_hash(h):
    h.contact(phone="+13105551234", email="dana@acme.test")
    a = h.ok(h.get("/svc/v1/audit/events", caller="compliance_38"))
    b = h.ok(h.get("/svc/v1/audit/events", caller="compliance_38"))
    plain = hashlib.sha256(b"+13105551234").hexdigest()
    assert plain not in json.dumps(a)
    first = next(i for i, e in enumerate(a["events"]) if e["kind"] == "contact_saved")
    eff_a, eff_b = a["events"][first]["data"]["effects"][0], b["events"][first]["data"]["effects"][0]
    key = base64.b64decode(a["hmac_key"])
    assert eff_a["phone_hmac"] == hmac.new(key, b"+13105551234", hashlib.sha256).hexdigest()
    assert eff_a["phone_hmac"] != eff_b["phone_hmac"] and a["hmac_key"] != b["hmac_key"]


def test_v1_m2_stored_digests_are_keyed(h):
    r = h.ok(h.chat("yes"), 201)
    digest = h.svc.messages[r["message_id"]]["body_sha256"]
    assert digest != hashlib.sha256(b"yes").hexdigest()
    assert digest == hmac.new(h.settings.hmac_key, b"yes", hashlib.sha256).hexdigest()
    assert hashlib.sha256(b"yes").hexdigest() not in json.dumps(h.ledger.events)


def test_v1_m2_production_always_has_a_key(tmp_path):
    import config as config_mod
    from helpers import base_env
    with pytest.raises(RuntimeError, match="SVC_DATA_DIR is required"):
        config_mod.load(base_env(SVC_NON_PRODUCTION=None))
    s = config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(tmp_path / "d")))   # V3-L1: generated
    assert len(s.hmac_key) == 32 and (os.stat(tmp_path / "d" / "hmac.key").st_mode & 0o777) == 0o600


# --------------------------------------------------------------------------------------------------- V1-L1, V1-L2

def test_v1_l1_dashboard_revocation_is_labelled_andre_only_with_his_token(h):
    cid = h.contact(phone="+13105551234")
    h.ok(h.consent(cid), 201)
    path, body = "/svc/v1/consents/revoke", {"request_id": rid(), "contact_id": cid, "channel": "sms"}
    h.ok(h.post(path, body, andre=True))
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}/consents"))[0]["revoked_via"] == "andre"
    assert ANDRE_TOKEN not in json.dumps(h.ledger.events)


@pytest.mark.parametrize("text", ["I want a refund", "my lawyer will call", "this is pathetic", "I was hacked",
                                  "delete my data"])
def test_v1_l2_dashboard_alone_cannot_close_a_sensitive_ticket(h, text):
    tid = h.ok(h.chat(text), 201)["ticket_id"]
    r = h.post(f"/svc/v1/tickets/{tid}/status", {"request_id": rid(), "status": "resolved"})
    assert r.status_code == 403 and r.json()["detail"] == "ANDRE_APPROVAL_REQUIRED"
    h.ok(h.post(f"/svc/v1/tickets/{tid}/status", {"request_id": rid(), "status": "resolved"}, andre=True))
    r = h.post(f"/svc/v1/tickets/{tid}/status", {"request_id": rid(), "status": "closed"})
    assert r.status_code == 403
    h.ok(h.post(f"/svc/v1/tickets/{tid}/status", {"request_id": rid(), "status": "closed"}, andre=True))


def test_v1_l2_a_routine_ticket_is_still_closed_by_the_dashboard(h):
    tid = h.ok(h.chat("I need help with my campaign"), 201)["ticket_id"]
    h.ok(h.post(f"/svc/v1/tickets/{tid}/status", {"request_id": rid(), "status": "resolved"}))


# --------------------------------------------------------------------------------------------------- unconfirmed sends

def test_unconfirmed_send_is_durable_and_held_after_restart_never_resent(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["email"] = s
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=p)
    r = h.ok(h.email("I need help"), 201)
    rep = h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "ok"}, andre=True), 201)
    real = h.svc._commit

    def crash_after_send(kind, data, actor):
        if kind == "outbound_result":
            raise RuntimeError("process dies after the provider took the message")
        return real(kind, data, actor)
    h.svc._commit = crash_after_send
    assert h.job("outbound-tick").status_code == 500
    assert len(s.sent) == 1 and s.sent[0].message_id == rep["message_id"]      # the provider's dedupe key
    h2 = h.restart()
    assert h2.svc.messages[rep["message_id"]]["status"] == "sending"
    assert h2.ok(h2.job("outbound-tick"))["held"] == 1 and len(s.sent) == 1     # held, never re-sent
    assert h2.ledger.of_type("message_sending")
    path = f"/svc/v1/outbound/{rep['message_id']}/resolve"
    assert h2.post(path, {"request_id": rid(), "outcome": "sent"}).status_code == 403   # Andre only
    h2.ok(h2.post(path, {"request_id": rid(), "outcome": "sent"}, andre=True))
    assert h2.svc.messages[rep["message_id"]]["status"] == "sent"
    assert h2.post(path, {"request_id": rid(), "outcome": "requeue"}, andre=True).json()["detail"] == "NOT_HELD"
