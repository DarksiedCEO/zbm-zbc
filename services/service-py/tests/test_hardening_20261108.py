"""Hardening 2026-11-08: service-py brought to finance-py's standard. One section per fix; each has success, failure,
boundary, regression and abuse cases. Synthetic data only (example.test addresses, 555 numbers)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import channels
import config as config_mod
import triage
from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness, RecordingSender, base_env, rid
from ports import Outbound, Ports

SRC = Path(__file__).resolve().parents[1] / "src"
EMAIL = "owner@acme.test"
PHONE = "+13105551234"


def _contact(h, ref="client:acme", email=EMAIL, phone=PHONE):
    cid = h.contact(ref=ref, email=email, phone=phone, timezone="America/Los_Angeles")
    h.ok(h.consent(cid, "sms"), 201)
    h.ok(h.consent(cid, "email"), 201)
    return cid


def _status(h, cid) -> dict:
    return {c["channel"]: c for c in h.svc.consents_view(cid)}


def _survey_ready(h, cid, account_id="acct-1"):
    h.account(account_id, contact_id=cid)
    h.template("nps-email", "nps_survey", channels=("email",))


def _survey(h, account_id="acct-1", channel="email"):
    return h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": account_id, "channel": channel},
                  caller="scheduler")


def _codes(h) -> list:
    return [a["code"] for a in h.svc.alerts.values()]


# =============================================================================================== 1. the email pause
# Fail closed, exact match: any inbound the bot does not answer pauses proactive EMAIL (as V3-C1 pauses SMS). Only a
# message that IS an approved example question is answered and leaves it off.

def test_an_approved_question_answered_leaves_proactive_email_on(h):
    cid = _contact(h)
    h.article()
    assert h.ok(h.chat("What are your hours?"), 201)["action"] == "answered"
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}"))["email_paused"] is False
    _survey_ready(h, cid)
    assert h.ok(_survey(h), 201)["status"] == "queued"


@pytest.mark.parametrize("fn,text", [
    ("chat", "Hello there, quick thing about my page"),
    ("email", "Hi, can you look at this for me"),
    ("sms", "Hello there"),
])
def test_any_unanswered_inbound_pauses_proactive_email(h, fn, text):
    cid = _contact(h)
    _survey_ready(h, cid)
    h.ok(getattr(h, fn)(text), 201)
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}"))["email_paused"] is True
    r = _survey(h)
    assert r.status_code == 409 and r.json()["detail"] == "EMAIL_PAUSED"
    assert _status(h, cid)["email"]["status"] == "active"          # a pause is not a revocation


def test_abuse_an_email_opt_out_outside_every_word_list_still_stops_proactive_email(h):
    """The class the word lists keep missing: wording no rule reads as an opt-out. Consent stays as it was (nothing
    was understood) but nothing proactive goes until Andre has read it."""
    cid = _contact(h)
    _survey_ready(h, cid)
    text = "Kindly cease the electronic correspondence henceforth"
    assert channels.opt_out_level(text) is None and channels.email_opt_out_decision(text) is None
    h.ok(h.email(text), 201)
    assert _status(h, cid)["email"]["status"] == "active"
    assert _survey(h).json()["detail"] == "EMAIL_PAUSED"


def test_a_queued_proactive_email_is_cancelled_by_a_pause_that_lands_before_the_tick(tmp_path):
    p = Ports.default()
    sender = RecordingSender()
    p.senders["email"] = sender
    h = Harness(tmp_path, ports=p)
    cid = _contact(h)
    _survey_ready(h, cid)
    mid = h.ok(_survey(h), 201)["message_id"]
    h.ok(h.chat("Hello there, about my account"), 201)
    out = h.ok(h.job("outbound-tick"))
    assert out["cancelled"] == 1 and out["sent"] == 0 and sender.sent == []
    assert h.svc.messages[mid]["status"] == "cancelled" and h.svc.messages[mid]["reason"] == "EMAIL_PAUSED"


def test_a_reply_on_the_contacts_own_ticket_is_not_proactive_and_still_goes(h):
    cid = _contact(h)
    h.ok(h.email("Hi, can you look at this for me"), 201)
    contact = h.svc.contacts[cid]
    assert h.svc.email_paused_for(contact)
    assert h.svc._channel_check("email", contact, proactive=False) is None
    assert h.svc._channel_check("email", contact, proactive=True) == "EMAIL_PAUSED"


def test_a_message_naming_another_contacts_email_pauses_that_contact_too(h):
    a = _contact(h)
    b = _contact(h, ref="client:other", email="other@acme.test", phone="+13105559876")
    h.ok(h.chat("Please look at the account for other@acme.test", ref="client:acme"), 201)
    assert h.svc.email_paused_for(h.svc.contacts[a]) and h.svc.email_paused_for(h.svc.contacts[b])


def test_the_pause_belongs_to_the_address_a_new_address_is_not_paused(h):
    cid = _contact(h)
    h.ok(h.chat("Hello there, about my account"), 201)
    assert h.svc.email_paused_for(h.svc.contacts[cid])
    h.contact(email="new@acme.test")             # the hub saves a new address for the same contact ref
    assert not h.svc.email_paused_for(h.svc.contacts[cid])


def test_andre_clears_the_pause_and_it_never_restores_a_revoked_consent(h):
    cid = _contact(h)
    _survey_ready(h, cid)
    h.ok(h.chat("Hello there, about my account"), 201)
    path = f"/svc/v1/contacts/{cid}/email-pause/clear"
    body = {"request_id": rid()}
    assert h.post(path, body).status_code == 403                                   # dashboard alone is not Andre
    assert h.post(path, body, caller="hub", andre=True).status_code == 403         # Andre only via the dashboard
    first = h.ok(h.post(path, body, andre=True))
    assert first == {"contact_id": cid, "email_paused": False, "email_consent": "active"}
    assert h.ok(h.post(path, body, andre=True)) == first                           # idempotent replay
    assert h.post(path, {"request_id": rid()}, andre=True).json()["detail"] == "NOT_PAUSED"
    assert h.ok(_survey(h), 201)["status"] == "queued"
    # a revoked email consent stays revoked after a later pause is cleared
    h.ok(h.post("/svc/v1/consents/revoke", {"request_id": rid(), "contact_id": cid, "channel": "email"}, caller="hub"))
    h.ok(h.chat("Hello again, about my account"), 201)
    assert h.ok(h.post(path, {"request_id": rid()}, andre=True))["email_consent"] == "revoked"
    assert _survey(h).json()["detail"] == "EMAIL_CONSENT_REVOKED"


def test_clear_on_an_unknown_contact_is_404_and_a_bad_id_is_422(h):
    assert h.post("/svc/v1/contacts/sv-con-" + "0" * 40 + "/email-pause/clear", {"request_id": rid()},
                  andre=True).json()["detail"] == "CONTACT_NOT_FOUND"
    assert h.post("/svc/v1/contacts/../email-pause/clear", {"request_id": rid()}, andre=True).status_code in (404, 422)


def test_pause_and_clear_are_typed_ledger_events_without_the_address_and_survive_restart(hd):
    cid = _contact(hd)
    hd.ok(hd.chat("Hello there, about my account"), 201)
    events = hd.ledger.of_type("email_paused")
    assert len(events) == 1 and EMAIL not in json.dumps(events[0]["_payload"]) and "email" not in events[0]["_payload"]
    h2 = hd.restart()
    assert h2.svc.email_paused_for(h2.svc.contacts[cid])                           # replayed from the log
    h2.ok(h2.post(f"/svc/v1/contacts/{cid}/email-pause/clear", {"request_id": rid()}, andre=True))
    assert len(h2.ledger.of_type("email_pause_cleared")) == 1
    h3 = h2.restart()
    assert not h3.svc.email_paused_for(h3.svc.contacts[cid])


def test_ledger_down_nothing_is_paused_and_the_message_is_refused(h):
    cid = _contact(h)
    h.ledger.fail_types.add("email_paused")
    r = h.chat("Hello there, about my account")
    assert r.status_code == 503 and r.json()["detail"] == "LEDGER_UNAVAILABLE"
    assert not h.svc.email_paused_for(h.svc.contacts[cid])
    assert not any(m["dir"] == "in" for m in h.svc.messages.values())               # record-first: no effect at all


def test_channel_rule_unit_boundaries():
    contact = {"email": EMAIL}
    active = {"status": "active", "address": EMAIL}
    for proactive, paused, want in ((True, True, "EMAIL_PAUSED"), (True, False, None), (False, True, None)):
        assert channels.check("email", contact, lambda ch: active, proactive, None, email_paused=paused) == want
    # consent first: no consent is still EMAIL_CONSENT_REQUIRED, a revocation still EMAIL_CONSENT_REVOKED
    assert channels.check("email", contact, lambda ch: None, True, None, email_paused=True) == "EMAIL_CONSENT_REQUIRED"
    revoked = {"status": "revoked", "address": EMAIL}
    assert channels.check("email", contact, lambda ch: revoked, False, None, email_paused=True) == "EMAIL_CONSENT_REVOKED"


# ======================================================================== 2. an email opt-out sent by chat or SMS

@pytest.mark.parametrize("fn,text", [
    ("chat", "please stop emailing me"),
    ("chat", "do not email me"),
    ("chat", "remove me from your mailing list"),
    ("chat", "unsubscribe me from your texts and emails"),
    ("sms", "stop emailing me"),
    ("sms", "no more texts or emails"),
    ("sms", "stop texting me and the emails too"),
])
def test_an_email_opt_out_by_chat_or_sms_revokes_email_consent(h, fn, text):
    cid = _contact(h)
    h.ok(getattr(h, fn)(text), 201)
    email = _status(h, cid)["email"]
    assert email["status"] == "revoked" and email["revoked_via"] == f"email_opt_out_by_{fn}"
    assert any(e["_payload"].get("channel") == "email" for e in h.ledger.of_type("consent_changed"))


@pytest.mark.parametrize("text", ["please st0p em4iling me", "stop e-mailing me", "STOP EMAILING ME!!",
                                  "stop e​mailing me", "stop ｅmailing me"])
def test_abuse_disguised_email_opt_outs_by_chat_are_read(h, text):
    cid = _contact(h)
    h.ok(h.chat(text), 201)
    assert _status(h, cid)["email"]["status"] == "revoked"


@pytest.mark.parametrize("text", ["STOP", "Stop.", "stop texting me", "stop texting me, email me instead",
                                  "my email is owner@acme.test, stop texting"])
def test_boundary_an_sms_opt_out_that_does_not_name_email_leaves_email_consent(h, text):
    cid = _contact(h)
    h.ok(h.sms(text), 201)
    st = _status(h, cid)
    assert st["sms"]["status"] == "revoked" and st["email"]["status"] == "active"
    assert "EMAIL_OPT_OUT_UNCLEAR" not in _codes(h)


def test_an_unclear_email_scope_by_sms_asks_andre_and_keeps_email(h):
    cid = _contact(h)
    h.ok(h.sms("stop texting me. not sure about the emails"), 201)
    assert _status(h, cid)["email"]["status"] == "active"
    assert "EMAIL_OPT_OUT_UNCLEAR" in _codes(h)


def test_a_queued_email_is_cancelled_when_a_chat_revokes_email(h):
    cid = _contact(h)
    _survey_ready(h, cid)
    mid = h.ok(_survey(h), 201)["message_id"]
    h.ok(h.chat("please stop emailing me"), 201)
    assert h.svc.messages[mid]["status"] == "cancelled" and h.svc.messages[mid]["reason"] == "CONSENT_REVOKED"


def test_regression_a_contact_without_an_email_is_unaffected(h):
    cid = h.contact(phone=PHONE, timezone="America/Los_Angeles")
    h.ok(h.consent(cid, "sms"), 201)
    h.ok(h.sms("stop emailing me"), 201)
    assert set(_status(h, cid)) == {"sms"}


def test_names_email_unit():
    for yes in ("stop emailing", "no e-mails", "newsletter", "your mailing list", "E M A I L"):
        assert channels.names_email(yes), yes
    for no in ("stop", "take me off your list", "stop texting", "", None):
        assert not channels.names_email(no), no


# ===================================================================================== 3. NPS comments, fail closed

def _answered_survey(h, comment):
    cid = _contact(h)
    _survey_ready(h, cid)
    sid = h.ok(_survey(h), 201)["survey_id"]
    body = {"request_id": rid(), "survey_id": sid, "score": 9}
    if comment is not None:
        body["comment"] = comment
    h.ok(h.post("/svc/v1/nps/responses", body, caller="hub"), 201)
    return cid


def test_an_nps_comment_pauses_proactive_sms_and_email(h):
    cid = _answered_survey(h, "Great work this month")
    c = h.svc.contacts[cid]
    assert h.svc.email_paused_for(c) and h.svc.sms_paused_for(c)
    assert "OPT_OUT_POSSIBLE" not in _codes(h)


def test_an_nps_without_a_comment_pauses_nothing(h):
    cid = _answered_survey(h, None)
    c = h.svc.contacts[cid]
    assert not h.svc.email_paused_for(c) and not h.svc.sms_paused_for(c)


@pytest.mark.parametrize("comment", ["stop texting and emailing me", "unsubscribe", "please remove me from your list"])
def test_opt_out_wording_in_an_nps_comment_alerts_andre_and_changes_no_consent(h, comment):
    cid = _answered_survey(h, comment)
    assert "OPT_OUT_POSSIBLE" in _codes(h)
    st = _status(h, cid)
    assert st["email"]["status"] == "active" and st["sms"]["status"] == "active"


# ============================================================================================ 4. fail-closed config

REF = "${{shared.SVC_SERVICE_TOKEN_THAT_IS_LONG_ENOUGH}}"


@pytest.mark.parametrize("over,match", [
    ({"SVC_SERVICE_TOKEN": REF}, "unresolved Railway reference"),
    ({"SVC_CALLER_TOKENS": json.dumps({**CALLERS, "hub": REF})}, "unresolved Railway reference"),
    ({"SVC_ANDRE_APPROVAL_TOKEN": REF}, "unresolved Railway reference"),
    ({"SVC_ANDRE_APPROVAL_TOKEN": "x"}, "SVC_ANDRE_APPROVAL_TOKEN must be 32..512"),
    ({"SVC_ANDRE_APPROVAL_TOKEN": "a" * 31}, "SVC_ANDRE_APPROVAL_TOKEN must be 32..512"),
    ({"SVC_ANDRE_APPROVAL_TOKEN": "a b" + "c" * 40}, "SVC_ANDRE_APPROVAL_TOKEN must be 32..512"),
    ({"SVC_LEGAL_URL": "http://legal.internal", "SVC_LEGAL_TOKEN": REF, "SVC_LEGAL_CALLER_TOKEN": "c" * 40},
     "unresolved Railway reference"),
    ({"LEDGER_SERVICE_URL": "http://127.0.0.1:8090"}, "come as a pair"),
    ({"LEDGER_SERVICE_TOKEN": "l" * 40}, "come as a pair"),
    ({"LEDGER_SERVICE_URL": "ftp://ledger", "LEDGER_SERVICE_TOKEN": "l" * 40}, "base URL"),
    ({"LEDGER_SERVICE_URL": "http://ledger/x?y", "LEDGER_SERVICE_TOKEN": "l" * 40}, "base URL"),
    ({"LEDGER_SERVICE_URL": "http://ledger", "LEDGER_SERVICE_TOKEN": REF}, "unresolved Railway reference"),
    ({"LEDGER_SERVICE_URL": "http://ledger", "LEDGER_SERVICE_TOKEN": SERVICE_TOKEN}, "must differ"),
    ({"LEDGER_SERVICE_URL": "http://ledger", "LEDGER_SERVICE_TOKEN": ANDRE_TOKEN}, "must differ"),
    ({"LEDGER_SERVICE_URL": "http://ledger", "LEDGER_SERVICE_TOKEN": CALLERS["hub"]}, "must differ"),
    ({"SVC_BIND_ADDR": "0.0.0.0; rm -rf"}, "SVC_BIND_ADDR"),
    ({"SVC_BIND_ADDR": "example.com"}, "SVC_BIND_ADDR"),
])
def test_bad_settings_refuse_start_and_never_echo_the_value(over, match):
    with pytest.raises(RuntimeError, match=match) as e:
        config_mod.load(base_env(**over))
    for v in over.values():
        if v not in ("http://127.0.0.1:8090",) and len(v) > 8:
            assert v not in str(e.value)


@pytest.mark.parametrize("addr", ["127.0.0.1", "::", "[::]", "::1", "0.0.0.0", "localhost", "10.0.0.5"])
def test_good_bind_addresses_start(addr):
    assert config_mod.load(base_env(SVC_BIND_ADDR=addr)).bind_addr == addr


def test_a_good_ledger_pair_starts_and_trailing_slash_is_fine():
    s = config_mod.load(base_env(LEDGER_SERVICE_URL="http://127.0.0.1:8090/", LEDGER_SERVICE_TOKEN="l" * 40))
    assert s.ledger_url == "http://127.0.0.1:8090/" and s.ledger_token == "l" * 40
    s = config_mod.load(base_env())
    assert s.ledger_url is None and s.ledger_token is None


def test_a_refused_start_touches_no_data_directory(tmp_path):
    d = tmp_path / "data"
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SVC_DATA_DIR=str(d), SVC_ANDRE_APPROVAL_TOKEN="short"))
    assert not d.exists()
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SVC_DATA_DIR=str(d), LEDGER_SERVICE_URL="http://x"))
    assert not d.exists()


# ===================================================================================== 5. launcher tuning bounds

@pytest.fixture
def serve_mod():
    import serve
    return serve


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-1", "0.1", "1e9", "ten", "301"])
def test_launcher_head_timeout_refuses_non_finite_and_out_of_range(serve_mod, monkeypatch, raw):
    monkeypatch.setenv("SVC_REQUEST_HEAD_TIMEOUT_SECONDS", raw)
    with pytest.raises(RuntimeError, match="finite number"):
        serve_mod._positive("SVC_REQUEST_HEAD_TIMEOUT_SECONDS", 10.0)


@pytest.mark.parametrize("name,raw", [("SVC_LIMIT_CONCURRENCY", "0"), ("SVC_LIMIT_CONCURRENCY", "1000000"),
                                      ("SVC_LIMIT_CONCURRENCY", "1.5"), ("SVC_KEEP_ALIVE_TIMEOUT_SECONDS", "nan"),
                                      ("SVC_KEEP_ALIVE_TIMEOUT_SECONDS", "100000")])
def test_launcher_int_settings_refuse_out_of_range(serve_mod, monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(RuntimeError, match="finite number"):
        serve_mod._positive(name, 5, int)


def test_launcher_defaults_and_good_values(serve_mod, monkeypatch):
    for blank in (None, "", "  "):
        if blank is None:
            monkeypatch.delenv("SVC_LIMIT_CONCURRENCY", raising=False)
        else:
            monkeypatch.setenv("SVC_LIMIT_CONCURRENCY", blank)
        assert serve_mod._positive("SVC_LIMIT_CONCURRENCY", 128, int) == 128
    monkeypatch.setenv("SVC_LIMIT_CONCURRENCY", "10000")
    assert serve_mod._positive("SVC_LIMIT_CONCURRENCY", 128, int) == 10000
    monkeypatch.setenv("SVC_REQUEST_HEAD_TIMEOUT_SECONDS", "0.5")
    assert serve_mod._positive("SVC_REQUEST_HEAD_TIMEOUT_SECONDS", 10.0) == 0.5


# ======================================================================================== 6. dual-stack serving

def test_the_launcher_serves_through_dual_stack():
    src = (SRC / "serve.py").read_text()
    assert "dual_stack.serve(" in src and "uvicorn.run(" not in src


def test_dual_stack_is_finance_pys_module_byte_for_byte():
    assert (SRC / "dual_stack.py").read_bytes() == (SRC.parents[1] / "finance-py" / "src" / "dual_stack.py").read_bytes()


def test_dual_stack_any_other_host_goes_to_uvicorn_unchanged(monkeypatch):
    import dual_stack

    made = []
    monkeypatch.setattr(dual_stack.socket, "socket", lambda *a: made.append(a))

    class UV:
        ran = None

        def run(self, app, **kw):
            UV.ran = (app, kw)
    dual_stack.serve(UV(), "app", "127.0.0.1", 0, "service-py", http="H")
    assert UV.ran == ("app", {"host": "127.0.0.1", "port": 0, "http": "H"}) and made == []


_REAL_SOCKET_CHILD = """
import socket, sys
sys.path.insert(0, sys.argv[1])
import dual_stack
try:
    socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
except OSError:
    try:
        dual_stack.dual_stack_socket("::", 0)
    except OSError:
        print("no-ipv6 bind-refused")
    else:
        print("no-ipv6 bind-accepted")
    sys.exit(0)
s = dual_stack.dual_stack_socket("::", 0)
v6only = s.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
s.listen(1)
c = socket.create_connection(("127.0.0.1", s.getsockname()[1]), timeout=5)
conn, peer = s.accept()
print("ipv6", v6only, peer[0])
conn.close(); c.close(); s.close()
"""


def test_ipv4_reaches_the_real_wildcard_socket_or_the_bind_is_refused_without_ipv6():
    r = subprocess.run([sys.executable, "-I", "-B", "-c", _REAL_SOCKET_CHILD, str(SRC)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout.split()
    if out[0] == "no-ipv6":
        assert out == ["no-ipv6", "bind-refused"], out
    else:
        assert out == ["ipv6", "0", "::ffff:127.0.0.1"], out


# ====================================================================================== 7. Legal's intake kind

@pytest.mark.parametrize("text,kind", [
    ("My lawyr will be in touch about the contract", "litigation_threat"),        # a typo (near_lawyer)
    ("We are litigating this agreement", "litigation_threat"),                     # a stem (litigat)
    ("cancel contract agreement terms notice termination lawsuit", "litigation_threat"),   # the sixth contract hit
    ("my solicitor has the contract", "litigation_threat"),
    ("DMCA takedown of my video", "ip_claim"),
    ("you infringed my copyright", "ip_claim"),
    ("cancel my contract", "contract_dispute"),
    ("I want to cancel, thanks for your courtesy", "contract_dispute"),             # "courtesy" is not "court"
])
def test_legal_kind_reads_every_contract_term(text, kind):
    assert triage.legal_kind(triage.classify(text)) == kind


def test_legal_kind_reads_the_subject_too():
    assert triage.legal_kind(triage.classify("please cancel my contract", subject="Lawsuit")) == "litigation_threat"


def test_legal_kind_without_contract_terms_still_reads_signals():
    t = triage.Triage("contract", ("contract",), ("contract:near_lawyer",), False)
    assert triage.legal_kind(t) == "litigation_threat"
    t = triage.Triage("contract", ("contract",), ("contract:stem_court",), False)
    assert triage.legal_kind(t) == "contract_dispute"


def test_a_typo_lawsuit_threat_reaches_legal_as_litigation(h):
    h.ok(h.chat("My lawyr will be in touch about the contract"), 201)
    kinds = {x["kind"] for x in h.svc.handoffs.values() if x["department"] == "legal_37"}
    assert kinds == {"litigation_threat"}


# ========================================================================= 8. the non-production sender writes fully

def test_nonprod_sender_survives_short_writes(tmp_path, monkeypatch):
    import nonprod_sender
    real = os.write
    monkeypatch.setattr(nonprod_sender.os, "write", lambda fd, data: real(fd, bytes(data[:3])))
    out = tmp_path / "outbox.jsonl"
    s = nonprod_sender.NonProdFileSender(str(out))
    assert s.send(Outbound("m1", "zbm", "sms", PHONE, "+13105550100", None, "hello there")) == "sent"
    assert json.loads(out.read_text())["text"] == "hello there"


def test_nonprod_sender_raises_on_a_zero_write(tmp_path, monkeypatch):
    import nonprod_sender
    monkeypatch.setattr(nonprod_sender.os, "write", lambda fd, data: 0)
    s = nonprod_sender.NonProdFileSender(str(tmp_path / "outbox.jsonl"))
    with pytest.raises(OSError):
        s.send(Outbound("m1", "zbm", "sms", PHONE, "+13105550100", None, "hello"))


# ============================================================================== 9. review of PR #46 (CONDITIONAL)

@pytest.mark.parametrize("text", ["stop contacting me", "STOP all messages", "unsubscribe from everything", "STOP ALL",
                                  "STOP correos", "stop em ails"])
def test_h1_short_broad_sms_opt_outs_pause_email_and_alert_andre(h, text):
    cid = _contact(h)
    _survey_ready(h, cid)
    r = h.ok(h.sms(text), 201)
    st = _status(h, cid)
    assert st["sms"]["status"] == "revoked" and r.get("ticket_id") is None        # still the short opt-out path
    assert h.svc.email_paused_for(h.svc.contacts[cid])
    assert "OPT_OUT_POSSIBLE" in _codes(h)
    assert _survey(h).json()["detail"] in ("EMAIL_PAUSED", "EMAIL_CONSENT_REVOKED")


@pytest.mark.parametrize("text", ["stop texts, emails?", "stop txt n mail"])
def test_h1_short_unclear_email_scope_pauses_email_and_asks(h, text):
    cid = _contact(h)
    h.ok(h.sms(text), 201)
    assert _status(h, cid)["email"]["status"] == "active"
    assert h.svc.email_paused_for(h.svc.contacts[cid])
    assert "EMAIL_OPT_OUT_UNCLEAR" in _codes(h) and "OPT_OUT_POSSIBLE" not in _codes(h)


@pytest.mark.parametrize("text", ["STOP", "stop texting me please", "st0p", "STOPPP", "unsubscribe"])
def test_h1_boundary_a_bare_sms_keyword_pauses_email_but_raises_no_alert(h, text):
    cid = _contact(h)
    h.ok(h.sms(text), 201)
    st = _status(h, cid)
    assert st["sms"]["status"] == "revoked" and st["email"]["status"] == "active"
    assert h.svc.email_paused_for(h.svc.contacts[cid])                              # fail closed
    assert "OPT_OUT_POSSIBLE" not in _codes(h)


def test_h1_short_email_opt_out_by_sms_still_revokes_email(h):
    cid = _contact(h)
    h.ok(h.sms("stop emailing me"), 201)
    assert _status(h, cid)["email"]["status"] == "revoked"


def test_h1_long_broad_chat_opt_out_alerts_and_pauses(h):
    cid = _contact(h)
    h.ok(h.chat("STOP contacting me through any of your channels whatsoever"), 201)
    assert h.svc.email_paused_for(h.svc.contacts[cid]) and "OPT_OUT_POSSIBLE" in _codes(h)


def test_broad_opt_out_unit():
    assert not channels.broad_opt_out(None) and not channels.broad_opt_out("")
    assert not channels.broad_opt_out("Stop texting me, thanks")
    assert channels.broad_opt_out("stop everything")


def test_m1_an_unknown_pause_effect_from_a_newer_version_is_skipped_on_replay(h):
    cid = _contact(h)
    h.svc._apply("x", {"effects": [{"op": "push_paused", "contact_id": cid}]}, "2026-10-06T18:00:00Z")
    h.svc._apply("x", {"effects": [{"op": "push_pause_cleared", "contact_id": cid}]}, "2026-10-06T18:00:00Z")


@pytest.mark.parametrize("op", ["consent_withdrawn", "push_revoked", "paused", "x_paused_extra", "PUSH_PAUSED"])
def test_m1_any_other_unknown_effect_still_refuses_start(h, op):
    from store import StoreCorrupt
    with pytest.raises(StoreCorrupt):
        h.svc._apply("x", {"effects": [{"op": op}]}, "2026-10-06T18:00:00Z")


def test_l1_a_replayed_clear_answers_with_the_live_state(h):
    cid = _contact(h)
    h.ok(h.chat("Hello there, about my account"), 201)
    body = {"request_id": rid()}
    path = f"/svc/v1/contacts/{cid}/email-pause/clear"
    assert h.ok(h.post(path, body, andre=True))["email_paused"] is False
    h.ok(h.chat("Hello again, a different thing"), 201)                             # paused again
    again = h.ok(h.post(path, body, andre=True))                                    # same request replayed
    assert again["email_paused"] is True and again["contact_id"] == cid


@pytest.mark.parametrize("text,kind", [
    ("I loved Tom Sawyer, cancel my contract", "contract_dispute"),
    ("Sawyer here, please cancel the agreement", "contract_dispute"),
    ("My lawyr will be in touch about the contract", "litigation_threat"),
    ("my attorny says cancel the contract", "litigation_threat"),
    ("the lawsiut over the contract", "litigation_threat"),
])
def test_l2_short_typos_need_context_for_legal_kind(text, kind):
    assert triage.legal_kind(triage.classify(text)) == kind
