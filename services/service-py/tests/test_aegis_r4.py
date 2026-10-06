"""AEGIS round 4 (Oct 5 2026, NOT BLOCKING): one regression per finding, each the reviewer's scenario
(scratchpad/aegis-service-r4/p2..p7)."""

from __future__ import annotations

import json
import os
import re

import pytest

import config as config_mod
import store as store_mod
from helpers import Harness, RecordingSender, base_env, rid
from ports import Ports

# the reviewer's p2 list, verbatim
REVIEWER_R4_QUESTIONS = ["can i get my funds back","how do i get my funds returned","can you get rid of my info","can you forget about me",
"how do i shut down my account","how do i end things with you","is there a way to dump you","can you take my videos off your site",
"someone got into my email","is my card number safe with you","do you sell my info","can i speak to your lawyer",
"can i get my clips taken offline","can i get a partial credit","can you send my stuff back","how do i get out of my deal with you",
"can you stop posting my clips","how do i close out my account","what happens to my info when i leave","can i get a rebate",
"are you going to take me to small claims","what are your hours and can i get my stuff back","what are your hours, also wire me my funds",
"do you share my phone number","can i stop the auto renew","my account got broken into","can i get my deposit back",
"how do i report you","is this a scam","can i get a free month","who do i call about a problem","can you unsubscribe me",
"do not text me","how do i stop your texts","can i get my membership fee waived","i want to leave","i want out","get me out of this"]

E164 = re.compile(r"\+[1-9][0-9]{7,14}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def _save(h, questions, item="qq-x"):
    return h.post("/svc/v1/kb/articles", {"request_id": rid(), "item_id": item, "brands": ["zbm"],
                                          "channels": ["chat", "email", "sms"], "title": "Hours",
                                          "answer": "Yes, absolutely.", "questions": questions})


# --------------------------------------------------------------------------------------------------- V4-M1

@pytest.mark.parametrize("question", REVIEWER_R4_QUESTIONS)
def test_v4_m1_every_reviewer_question_is_refused_at_approval(h, question):
    r = _save(h, [question])
    assert r.status_code == 422 and r.json()["detail"] == "QUESTION_DENIED"


def test_v4_m1_p3_nothing_from_the_list_can_be_answered(h):
    for i, q in enumerate(["can i get my funds back", "what are your hours, also wire me my funds",
                           "do you sell my info", "someone got into my email", "do not text me"]):
        assert _save(h, [q], f"art{i}x").status_code == 422
        assert h.ok(h.chat(q, ref=f"client:c{i}"), 201)["action"] != "answered"


@pytest.mark.parametrize("question", ["What are your hours and when are you open?", "What are your hours, when are you open?",
                                      "When are you open? What are your hours?"])
def test_v4_m1_a_question_with_a_second_clause_is_refused(h, question):
    assert _save(h, [question]).json()["detail"] == "QUESTION_DENIED"


def test_v4_m1_questions_are_capped_at_twelve_words(h):
    assert _save(h, ["What time do you open on the first Saturday of every month in summer?"]).status_code == 422
    assert _save(h, ["What time do you open on the first Saturday of the month?"]).status_code == 201


def test_v4_m1_ordinary_questions_are_still_approvable(h):
    for q in ("What are your hours?", "When are you open tomorrow?", "How long does a project usually take?",
              "Where do I send my clips?", "What is the usual turnaround?"):
        assert _save(h, [q], "ok-" + str(abs(hash(q)) % 10 ** 6)).status_code == 201


# --------------------------------------------------------------------------------------------------- V4-M2

def test_v4_m2_no_phone_number_or_email_in_the_audit_export_or_any_ledger_payload(tmp_path):
    p = Ports.default()
    senders = {c: RecordingSender() for c in ("sms", "email", "chat")}
    p.senders.update(senders)
    h = Harness(tmp_path, ports=p)
    h.article()
    h.template("nps", "nps_survey")
    cid = h.contact(phone="+13105551234", email="owner@acme.test", timezone="America/Los_Angeles",
                    display_name="Dana Rivera")
    h.ok(h.consent(cid), 201)
    h.ok(h.consent(cid, "email"), 201)
    h.account("acct-1", contact_id=cid)
    h.ok(h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "email"}), 201)
    h.ok(h.sms("STOP"), 201)                                                   # confirmation bound to the number
    r = h.ok(h.email("I need help with my invoice", frm="owner@acme.test"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "On it."}, andre=True), 201)
    h.ok(h.email("STOP texting me at 310-555-1234", frm="stranger@other.test", subject="stop"), 201)
    h.ok(h.post("/svc/v1/calls", {"request_id": rid(), "brand": "zbm", "from_number": "+13105559999",
                                  "started_at": "2026-10-06T17:00:00Z", "duration_seconds": 5, "outcome": "voicemail",
                                  "voicemail_ref": "vm/1"}, caller="voice_gateway"), 201)
    h.ok(h.post(f"/svc/v1/contacts/{cid}/sms-pause/clear", {"request_id": rid()}, andre=True))
    h.contact(phone="+13105550001", email="new@acme.test")
    h.ok(h.job("outbound-tick"))
    export = json.dumps(h.ok(h.get("/svc/v1/audit/events?limit=1000", caller="compliance_38")))
    assert not E164.search(export) and not EMAIL.search(export)
    ledger = json.dumps([{**e, "_payload": e["_payload"]} for e in h.ledger.events])
    assert not E164.search(ledger) and not EMAIL.search(ledger)


# --------------------------------------------------------------------------------------------------- V4-L1

def test_v4_l1_p5_a_pause_follows_the_number_to_a_new_contact(tmp_path):
    p = Ports.default()
    s = RecordingSender()
    p.senders["sms"] = s
    h = Harness(tmp_path, ports=p)
    h.template("nps", "nps_survey")
    num = "+13105551234"
    a = h.contact(ref="client:acme", phone=num, timezone="America/Los_Angeles", email="owner@acme.test")
    h.ok(h.consent(a), 201)
    h.ok(h.email("Please no texts to my phone, email only.", frm="owner@acme.test", subject="Texts"), 201)
    h.contact(ref="client:acme", phone="+13105550001", timezone="America/Los_Angeles")
    b = h.contact(ref="client:acme-new", phone=num, timezone="America/Los_Angeles")
    h.clock.advance(minutes=1)
    h.ok(h.consent(b), 201)
    h.account("acct-1", contact_id=b)
    r = h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "sms"})
    assert r.status_code == 409 and r.json()["detail"] == "SMS_PAUSED"
    h.ok(h.job("outbound-tick"))
    assert [m.to for m in s.sent] == []


# --------------------------------------------------------------------------------------------------- V4-L2

def _prod(d, **kw):
    return config_mod.load(base_env(SVC_NON_PRODUCTION=None, SVC_DATA_DIR=str(d), **kw))


def test_v4_l2_the_key_is_generated_atomically(tmp_path, monkeypatch):
    d = tmp_path / "d"
    calls = []
    real_link = os.link

    def spy(src, dst, *a, **k):
        calls.append((os.path.basename(src), os.path.basename(dst)))
        return real_link(src, dst, *a, **k)
    monkeypatch.setattr(os, "link", spy)
    key = _prod(d).hmac_key
    assert len(calls) == 1 and calls[0][1] == "hmac.key"
    assert re.fullmatch(rf"hmac\.key\.{os.getpid()}\.[0-9a-f]{{16}}\.tmp", calls[0][0])        # per process (V4b-I1)
    assert sorted(os.listdir(d)) == ["hmac.key", "service.lock"] and _prod(d).hmac_key == key


def test_v4_l2_a_leftover_temp_file_from_a_crash_is_replaced(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    (d / "hmac.key.tmp").write_bytes(b"half")
    assert len(_prod(d).hmac_key) == 32 and not (d / "hmac.key.tmp").exists()


def test_v4_l2_an_empty_key_file_tells_the_operator_what_to_do(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    fd = os.open(d / "hmac.key", os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    with pytest.raises(RuntimeError) as e:
        _prod(d)
    msg = str(e.value)
    assert f"delete {d / 'hmac.key'} and start again" in msg and "service_log.jsonl" in msg and "restore" in msg


def test_v4_l2_a_loose_permission_is_reported_as_such(tmp_path):
    d = tmp_path / "d"
    _prod(d)
    os.chmod(d / "hmac.key", 0o644)
    with pytest.raises(RuntimeError, match="readable by group or others") as e:
        _prod(d)
    assert "delete" not in str(e.value)               # a permissions problem: fix it, never delete the key


# --------------------------------------------------------------------------------------------------- V4-L3

def test_v4_l3_a_changed_generated_key_is_named_in_the_error(tmp_path):
    d = tmp_path / "d"
    h = Harness(tmp_path, data_dir=str(d), SVC_NON_PRODUCTION=None)
    h.ok(h.chat("I need help"), 201)
    os.remove(d / "hmac.key")                                   # a new key is generated at the next start
    with pytest.raises(store_mod.StoreCorrupt) as e:
        h.restart(SVC_NON_PRODUCTION=None)
    assert f"the generated key file {d / 'hmac.key'}" in str(e.value) and "SVC_HMAC_KEY_FILE" not in str(e.value)
