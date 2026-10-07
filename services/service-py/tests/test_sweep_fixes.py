"""AEGIS sweep A (on 5d49ee9) — regression tests for the service-py findings. Each one failed on 5d49ee9 and passes
after the fix (the probes in sweep-A/service passed while the bug existed)."""

import json

from helpers import (
    ZBM_EMAIL,
    ZBM_SMS,
    FakeLedger,
    Harness,
    RecordingAlerts,
    RecordingHandoff,
    rid,
)

from ports import Ports


def _consents(h, cid):
    return {c["channel"]: c["status"] for c in h.ok(h.get(f"/svc/v1/contacts/{cid}"))["consents"]}


def _email(h, frm, text, subject="Re: hello", request_id=None):
    return h.post("/svc/v1/inbound/email", {"request_id": request_id or rid(), "brand": "zbm", "to_address": ZBM_EMAIL,
                                            "from_address": frm, "subject": subject, "text": text},
                  caller="email_gateway")


def _sms(h, text, frm, request_id):
    return h.post("/svc/v1/inbound/sms", {"request_id": request_id, "brand": "zbm", "to_number": ZBM_SMS,
                                          "from_number": frm, "text": text}, caller="sms_gateway")


def _with_consents(h):
    cid = h.contact(email="owner@acme.test", phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid, channel="email"), 201)
    h.ok(h.consent(cid, channel="sms"), 201)
    return cid


# ------------------------------------------------------------------ 1. (High) an email opt-out revokes EMAIL consent

def test_email_unsubscribe_revokes_email_consent_and_stops_proactive_email(tmp_path):
    h = Harness(tmp_path)
    cid = _with_consents(h)
    h.account("acct-1", contact_id=cid)
    h.template("nps", "nps_survey")
    r = h.ok(h.email("UNSUBSCRIBE", frm="owner@acme.test", subject="unsubscribe"), 201)
    assert r.get("email_opted_out") is True
    assert _consents(h, cid) == {"email": "revoked", "sms": "revoked"}
    s = h.post("/svc/v1/nps/surveys", {"request_id": rid(), "account_id": "acct-1", "channel": "email"})
    assert s.status_code != 201, s.text


def test_do_not_email_me_is_an_email_opt_out(tmp_path):
    import channels
    for t in ("do not email me", "dont email me", "don’t email me", "please stop emailing me"):
        assert channels.opt_out_level(t) == "exact", t
    for i, t in enumerate(("do not email me", "dont email me")):
        h = Harness(tmp_path / str(i))
        cid = _with_consents(h)
        h.ok(h.email(t, frm="owner@acme.test", subject="Re: your invoice"), 201)
        assert _consents(h, cid)["email"] == "revoked", t
    # wording that names only the phone stays an SMS opt-out: the ticket can still be answered by email
    h = Harness(tmp_path / "sms-only")
    cid = _with_consents(h)
    r = h.ok(h.email("STOP texting me please", frm="owner@acme.test", subject="Re: your invoice"), 201)
    assert "email_opted_out" not in r
    assert _consents(h, cid) == {"email": "active", "sms": "revoked"}


# ------------------------------------------------------------------ 2. R6-M1: rk + seq, /audit/evidence

def test_r6m1_stop_retry_after_a_state_change_has_one_committed_event(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led)
    stop_rid = rid()
    led.fail_types = {"log_anchor"}
    assert _sms(h, "STOP", "+13105550123", stop_rid).status_code == 503
    led.fail_types = set()
    assert h.svc.verify_integrity(force=True, always=True)["ok"]
    h.ok(_sms(h, "hi there, question about my invoice", "+13105550123", rid()), 201)    # the contact now exists
    h.ok(_sms(h, "STOP", "+13105550123", stop_rid), 201)                               # the provider's retry
    cc = led.of_type("consent_changed")
    assert len(cc) == 2                                     # record-first: the orphan stays on the ledger ...
    for e in cc:
        assert isinstance(e["_payload"]["seq"], int) and e["_payload"]["rk"].startswith("rk-")
    ev = h.ok(h.get("/svc/v1/audit/evidence?event_type=consent_changed", caller="compliance_38"))
    assert ev["committed"] == 1 and ev["attempted"] == 1    # ... but exactly one is committed
    committed = next(e for e in ev["evidence"] if e["status"] == "committed")
    line = next(r for r in h.svc.log.iter_records() if r["seq"] == committed["seq"])
    assert committed["event_id"] in [x["event_id"] for x in line["data"]["ledger_evidence"]]
    assert "3105550123" not in json.dumps([e["_payload"] for e in led.events])     # rk is keyed, never an address


def test_audit_evidence_callers(tmp_path):
    h = Harness(tmp_path)
    assert h.get("/svc/v1/audit/evidence", caller="hub").status_code == 403
    assert h.ok(h.get("/svc/v1/audit/evidence", caller="dashboard"))["rule"].startswith("unanchored")


# ------------------------------------------------------------------ 3. inbound messages are never refused

def test_uppercase_sender_tab_in_subject_and_long_text_are_accepted(tmp_path):
    h = Harness(tmp_path)
    cid = h.contact(email="owner@acme.test", phone="+13105551234", timezone="America/Los_Angeles")
    h.ok(h.consent(cid, channel="sms"), 201)
    r = h.ok(_email(h, "Owner@Acme.test", "UNSUBSCRIBE"), 201)                    # was 422
    assert r["contact_id"] == cid and r.get("opted_out") is True
    assert _consents(h, cid)["sms"] == "revoked"
    h.ok(_email(h, "owner@acme.test", "STOP texting me", subject="Re:\tyour invoice"), 201)      # was 422
    long_text = "STOP\n" + "> quoted line of the old thread\n" * 800
    assert len(long_text) > 20_000
    h.ok(_email(h, "owner@acme.test", long_text), 201)                              # was 422
    h.ok(_email(h, "Owner <owner@acme.test>", "", subject="unsubscribe"), 201)      # Name <addr>, empty body
    h.ok(_sms(h, "STOP " + "x" * 2000, "+13105551234", rid()), 201)                 # SMS over 1,600: truncated


# ------------------------------------------------------------------ 4. health recompute: one pass, not A x T

def test_health_compute_scans_each_ticket_once(tmp_path):
    h = Harness(tmp_path)
    svc = h.svc
    n_acc, n_t = 40, 400
    svc.accounts.clear()
    svc.tickets.clear()
    for i in range(n_acc):
        svc.accounts[f"a{i}"] = {"account_id": f"a{i}", "brand": "zbm", "primary_contact_id": None,
                                 "contract_end": None, "contract_source": None, "last_login": None,
                                 "health": None, "at_risk": False, "plan_id": None, "renewal_flagged": []}
    recent = "2026-10-01T00:00:00Z"
    for j in range(n_t):
        svc.tickets[f"t{j}"] = {"account_id": f"a{j % n_acc}", "contact_id": "c",
                                "status": "escalated" if j % 4 == 0 else "open",
                                "complaint_at": [recent] if j % 5 == 0 else ["2020-01-01T00:00:00Z"]}
    calls = {"n": 0}
    orig = svc._ticket_account

    def counting(t):
        calls["n"] += 1
        return orig(t)
    svc._ticket_account = counting
    complaints, escalations, _ = svc._health_aggregates()
    assert complaints["a0"] == sum(1 for j in range(n_t) if j % n_acc == 0 and j % 5 == 0)
    assert escalations["a0"] == sum(1 for j in range(n_t) if j % n_acc == 0 and j % 4 == 0)
    calls["n"] = 0
    with svc.lock:
        out = svc._health_compute("k", {aid: (None, None, []) for aid in svc.accounts})
    assert out["scored"] == n_acc
    assert calls["n"] <= n_t                     # was 2 x accounts x tickets (32,000 here); an operation count


# ------------------------------------------------------------------ 5. a closed instance dispatches nothing

def test_closed_instance_sends_no_alert_or_handoff(tmp_path):
    ports = Ports.default()
    alerts = ports.alerts = RecordingAlerts()
    alerts.wired = False
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=ports)
    h.ok(h.chat("I want a refund, this is a scam"), 201)
    pending = [a for a in h.svc.alerts.values() if a["status"] == "recorded"]
    hofs = [x for x in h.svc.handoffs.values() if x["status"] in ("pending", "unavailable", "failed")]
    assert pending
    h.svc.close()
    alerts.wired = True
    for dept in {x["department"] for x in hofs}:
        ports.handoffs[dept] = RecordingHandoff()
    h.svc.dispatch_side_effects()
    assert alerts.sent == []                                                     # was 1 send from the CLOSED instance
    assert all(not getattr(p, "calls", []) for p in ports.handoffs.values())
    h2 = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=h.ledger, ports=ports)
    h2.svc.dispatch_side_effects()
    ids = [a.alert_id for a in alerts.sent]
    assert len(ids) == len(pending) and len(set(ids)) == len(ids)                # each alert exactly once


# ------------------------------------------------------------------ AEGIS review of 17cda6a (REVISE)

def _both_revoked(h, cid):
    return _consents(h, cid) == {"email": "revoked", "sms": "revoked"}


def test_h1_other_phone_words_in_the_message_never_narrow_an_unsubscribe(tmp_path):
    cases = [("Unsubscribe\n\nJohn Smith\nCell: 310-555-1212", "Re: hello"),
             ("UNSUBSCRIBE\n\nSent from my phone", "Re: hello"),
             ("unsubscribe\n> ...call our phone line", "Re: hello"),
             ("Please unsubscribe me", "Re: Text us anytime")]
    for i, (text, subject) in enumerate(cases):
        h = Harness(tmp_path / str(i))
        cid = _with_consents(h)
        r = h.ok(_email(h, "owner@acme.test", text, subject=subject), 201)
        assert r.get("email_opted_out") is True and _both_revoked(h, cid), (text, subject)


def test_h1_scope_comes_from_the_phrase_itself():
    import channels
    for t in ("stop texting me", "remove my number", "unsubscribe from texts", "unsubscribe me from your texts",
              "please no more texts"):
        assert channels.email_opt_out(t) is False, t
    for t in ("unsubscribe", "stop", "do not email me", "unsubscribe from texts and emails\n\nunsubscribe",
              "Thanks,\nSTOP"):
        assert channels.email_opt_out(t) is True, t
    assert channels.email_opt_out("> STOP\nthanks for the help") is False          # only in the quoted thread
    assert channels.strip_signature("stop\n-- \nJohn\nCell: 310 555 1212") == "stop"


def test_m1_a_misspelled_unsubscribe_by_email_revokes_email(tmp_path):
    for i, t in enumerate(("unsubcribe", "unsubscibe me please")):
        led = FakeLedger()
        h = Harness(tmp_path / str(i), ledger=led)
        cid = _with_consents(h)
        r = h.ok(_email(h, "owner@acme.test", t), 201)
        assert r.get("email_opted_out") is True, t
        cons = _consents(h, cid)
        assert cons["email"] == "revoked" and cons["sms"] == "active", t       # SMS: paused + alert, as before
        assert any(a["code"] == "SMS_OPT_OUT_SUSPECTED" for a in h.svc.alerts.values())
        assert any(e["_payload"].get("channel") == "email" for e in led.of_type("consent_changed"))   # evidence


def test_m2_inbound_shape_never_refuses(tmp_path):
    h = Harness(tmp_path)
    cid = _with_consents(h)
    base = {"brand": "zbm", "to_address": ZBM_EMAIL, "from_address": "owner@acme.test"}
    for body in ({**base, "text": "hello", "x_gateway_ip": "198.51.100.7", "headers": {"a": 1}},   # extras
                 {**base, "text": "hello again"},                                                   # no request_id
                 {**base, "request_id": "<CAF=abc@mail.example.test>", "text": "and again"},        # Message-ID
                 {**base, "request_id": rid(), "text": None, "subject": "unsubscribe"},             # null text
                 {**base, "request_id": rid(), "text": "x", "subject": 12345},                      # number subject
                 {**base, "request_id": rid(), "text": "x", "subject": ["a"], "ticket_id": "nope"}):
        r = h.post("/svc/v1/inbound/email", body, caller="email_gateway")
        assert r.status_code == 201, (body, r.text)
    sms = {"brand": "zbm", "to_number": ZBM_SMS, "from_number": "+13105551234", "text": None, "carrier": "x"}
    assert h.post("/svc/v1/inbound/sms", sms, caller="sms_gateway").status_code == 201
    # the same body with no request id is the same message (the id is the body's hash)
    a = h.ok(h.post("/svc/v1/inbound/email", {**base, "text": "same"}, caller="email_gateway"), 201)
    b = h.ok(h.post("/svc/v1/inbound/email", {**base, "text": "same"}, caller="email_gateway"), 201)
    assert a == b
    assert b"198.51.100.7" not in b"".join(h.svc.log.raw_lines())
    assert _consents(h, cid)["email"] == "revoked"                       # the null-text "unsubscribe" was honoured


def test_l2_an_opt_out_past_the_cap_is_still_read(tmp_path):
    h = Harness(tmp_path)
    cid = _with_consents(h)
    text = "Here is the long story. " * 1200 + "\n\nUNSUBSCRIBE"
    assert len(text) > 25_000
    h.ok(_email(h, "owner@acme.test", text), 201)
    assert _both_revoked(h, cid)


def test_l3_exports_cannot_be_joined_on_rk(tmp_path):
    h = Harness(tmp_path)
    _with_consents(h)
    a = h.ok(h.get("/svc/v1/audit/events", caller="compliance_38"))
    b = h.ok(h.get("/svc/v1/audit/events", caller="compliance_38"))
    raw = {ev["payload"]["rk"] for r in h.svc.log.iter_records() for ev in r["data"].get("ledger_evidence", [])}

    def rks(x):
        return {ev["payload"]["rk_hmac" if "rk_hmac" in ev["payload"] else "rk"]
                for e in x["events"] for ev in e["data"].get("ledger_evidence", [])}
    assert raw and rks(a) and not (rks(a) & rks(b)) and not (rks(a) & raw)


def _paging_ledger(entries, honours_query=True, refuses=False):
    import httpx

    def handler(request):
        q = dict(request.url.params)
        if refuses and q:                    # 400: a strict ledger; 404: ledger-rust before fix-ledger
            return httpx.Response(404 if refuses == 404 else 400, json={"error": "not found"})
        if not honours_query or not q:
            return httpx.Response(200, json=entries)
        after, limit = int(q.get("after_seq", -1)), int(q["limit"])
        out = [e for e in entries if e["seq"] > after and e.get("department") == q.get("department", e.get(
            "department")) and e.get("event_type") == q.get("event_type", e.get("event_type"))]
        return httpx.Response(200, json=out[:limit])
    return httpx.MockTransport(handler)


def test_m4_audit_evidence_reads_only_this_department_page_by_page(tmp_path):
    from ledger import HttpLedgerClient
    entries = [{"seq": i, "kind": "event", "department": "service" if i % 3 else "finance",
                "event_type": "log_anchor" if i % 2 else "consent_changed", "event_id": f"e{i}"} for i in range(25)]
    want = [e for e in entries if e["department"] == "service"]
    for honours, refuses in ((True, False), (False, False), (True, True), (True, 404)):
        c = HttpLedgerClient("http://ledger.test", "t" * 32, transport=_paging_ledger(entries, honours, refuses))
        assert c.entries_filtered("service", page_size=4) == want
        assert c.entries_filtered("service", "log_anchor", page_size=4) == \
            [e for e in want if e["event_type"] == "log_anchor"]
    h = Harness(tmp_path)
    calls = []
    h.ledger.entries_filtered = lambda d, t=None: calls.append((d, t)) or [
        e for e in h.ledger.entries() if e["department"] == d and (t is None or e["event_type"] == t)]
    _with_consents(h)
    ev = h.ok(h.get("/svc/v1/audit/evidence?event_type=consent_changed", caller="compliance_38"))
    assert ev["committed"] == 2 and calls == [("service", "log_anchor"), ("service", "consent_changed")]
