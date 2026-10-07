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


# ------------------------------------------------------------------ AEGIS re-review of 1e709a0 (REVISE)

def test_n1_an_opt_out_typed_below_the_quote_is_honoured(tmp_path):
    cases = ["On Mon, Oct 5, 2026 at 9:00 AM Acme <help@acme.test> wrote:\n> How was your visit?\n\nunsubscribe",
             "-----Original Message-----\nFrom: Acme\nSent: Monday\n\nHow was your visit?\n\nUNSUBSCRIBE",
             "On Mon, Oct 5, 2026 Acme wrote:\nHow was your visit?\n\nstop",
             "Hi,\n<blockquote>How was your visit?</blockquote>\nplease unsubscribe me"]
    for i, text in enumerate(cases):
        h = Harness(tmp_path / str(i))
        cid = _with_consents(h)
        r = h.ok(_email(h, "owner@acme.test", text, subject="Re: hello"), 201)
        assert r.get("email_opted_out") is True and _both_revoked(h, cid), text
    # the tail case tells Andre (it may have been quoted text)
    h = Harness(tmp_path / "alert")
    _with_consents(h)
    h.ok(_email(h, "owner@acme.test", cases[1], subject="Re: hello"), 201)
    assert any(a["code"] == "OPT_OUT_IN_QUOTED_TEXT" for a in h.svc.alerts.values())


def test_n1_our_own_quoted_words_never_opt_anyone_out(tmp_path):
    cases = ["Thanks, see you Friday!\n\nOn Mon, Oct 5, 2026 Acme wrote:\n> You can cancel anytime. Offer ends soon.\n"
             "> Not interested? Reply and let us know.",
             "Sounds good.\n\n-----Original Message-----\nFrom: Acme\n\nYour plan renews at the end of the month. "
             "Cancel anytime.",
             "Sounds good.\n<blockquote>Cancel anytime. Stop by our office.</blockquote>"]
    for i, text in enumerate(cases):
        h = Harness(tmp_path / str(i))
        cid = _with_consents(h)
        r = h.ok(_email(h, "owner@acme.test", text, subject="Re: hello"), 201)
        assert r.get("action") != "opted_out" and _consents(h, cid) == {"email": "active", "sms": "active"}, text


def test_h1_residual_an_email_word_in_the_phrase_makes_it_an_email_opt_out():
    import channels
    for t in ("unsubscribe me from your texts and emails", "stop sending me texts or emails",
              "remove my number and my email", "Unsubscribe from texts.\nThanks,\nAlso stop the newsletters",
              "STOP\nmy number is 310-555-1212", "Stop. My number changed", "Unsubscribe - Cell: 310 555 1212",
              "take me off your list"):
        assert channels.email_opt_out(t) is True, t
    for t in ("stop texting me", "remove my number", "unsubscribe from texts", "unsubscribe me from your texts",
              "please no more texts", "stop sending me texts"):
        assert channels.email_opt_out(t) is False, t


def test_n4_html_tags_never_merge_words():
    import channels
    for t in ("Unsubscribe<br>Sent from my iPhone", "<p>Unsubscribe</p><p>Cell: 310 555 1212</p>",
              "<div>STOP</div>", "please&nbsp;unsubscribe<br/>thanks"):
        assert channels.opt_out_level(t) == "exact", t
        assert channels.email_opt_out(t) is True, t


def test_n3_a_reused_request_id_with_another_body_is_never_refused(tmp_path):
    h = Harness(tmp_path)
    cid = _with_consents(h)
    base = {"brand": "zbm", "to_address": ZBM_EMAIL, "from_address": "owner@acme.test", "request_id": "dup-1"}
    h.ok(h.post("/svc/v1/inbound/email", {**base, "text": "is my order shipped?"}, caller="email_gateway"), 201)
    r = h.ok(h.post("/svc/v1/inbound/email", {**base, "text": "UNSUBSCRIBE"}, caller="email_gateway"), 201)
    assert r.get("email_opted_out") is True and _both_revoked(h, cid)
    again = h.ok(h.post("/svc/v1/inbound/email", {**base, "text": "UNSUBSCRIBE"}, caller="email_gateway"), 201)
    assert again == r                                    # the re-keyed message is itself idempotent


# ------------------------------------------------------------------ AEGIS re-review of 91f5b8b (REVISE)

_OUTLOOK = "-----Original Message-----\nFrom: Acme\nSent: Monday\nSubject: hello\n\nHow was your visit?\n\n"


def test_r2_opt_outs_below_an_unmarked_quote():
    import channels
    for reply in ("no more emails", "do not send me any more emails\nThanks", "STOP. Thanks", "stop\nSent from my iPhone",
                  "STOP\n--\nJane", "Stop\nThanks", "STOP STOP", "please unsubscribe me"):
        assert channels.quoted_tail_opt_out(_OUTLOOK + reply) == "revoke", reply
        assert channels.email_opt_out(_OUTLOOK + reply) is True, reply
    for reply in ("cancel my subscription", "stop sending me these please, I asked twice already"):
        assert channels.quoted_tail_opt_out(_OUTLOOK + reply) == "alert", reply   # Andre reads it


def test_r2_weaker_wording_below_an_unmarked_quote_alerts_andre(tmp_path):
    h = Harness(tmp_path)
    _with_consents(h)
    h.ok(_email(h, "owner@acme.test", _OUTLOOK + "cancel my subscription", subject="Re: hello"), 201)
    assert any(a["code"] == "OPT_OUT_IN_QUOTED_TEXT" for a in h.svc.alerts.values())


def test_r4_third_party_words_in_a_quote_never_revoke():
    import channels
    for quoted in ("Please do not contact the carrier directly.", "Can you remove me from the cc?",
                   "You may opt out of the warranty.", "Cancel anytime. Offer ends soon."):
        text = "Sounds good, thanks.\n\n-----Original Message-----\nFrom: Vendor\n\n" + quoted + "\nRegards"
        assert channels.email_opt_out(text) is False, quoted


def test_r1_text_between_two_html_quotes_is_the_persons_own():
    import channels
    for t in ("<div>Hi</div><blockquote>A</blockquote><div>please stop emailing me</div><blockquote>B</blockquote>",
              "<blockquote>A<blockquote>nested</blockquote>B</blockquote><p>unsubscribe</p>"):
        assert channels.email_opt_out(t) is True, t
    assert channels.email_opt_out("<p>Thanks!</p><blockquote>Cancel anytime<blockquote>end</blockquote></blockquote>") \
        is False


def test_r3_an_email_word_anywhere_in_the_persons_words_widens_the_opt_out():
    import channels
    for t in ("unsubscribe me from texts as well as emails", "Stop texting me - that goes for email too",
              "stop texting me, same for email", "stop texting me and spamming my inbox",
              "stop texting me my number is 3105551212 and my email too"):
        assert channels.email_opt_out(t) is True, t


def test_r5_wrapped_forwarded_and_localized_headers_are_quote_headers():
    import channels
    for header in ("On Mon, Oct 5, 2026 at 9:00 AM Acme Support Team <help@acme.test>\nwrote:",
                   "---------- Forwarded message ---------", "El lun, 5 oct 2026, Acme escribió:",
                   "Le lun. 5 oct. 2026, Acme a écrit :"):
        assert channels.email_opt_out("Thanks, see you then!\n\n" + header + "\nCancel anytime.") is False, header


def test_r6_angle_brackets_are_not_tags_for_an_opt_out():
    import channels
    for t in ("<STOP>", "<<STOP>>", "<unsubscribe me>", "i <3 u but stop texting me >:("):
        assert channels.opt_out_level(t) == "exact", t


def test_r7_a_literal_rekeyed_id_with_another_body_is_never_refused(tmp_path):
    h = Harness(tmp_path)
    cid = _with_consents(h)
    base = {"brand": "zbm", "to_address": ZBM_EMAIL, "from_address": "owner@acme.test", "request_id": "dup-2"}
    h.ok(h.post("/svc/v1/inbound/email", {**base, "text": "hello"}, caller="email_gateway"), 201)
    second = {**base, "text": "how are you"}
    h.ok(h.post("/svc/v1/inbound/email", second, caller="email_gateway"), 201)
    import hashlib as _h
    from service import body_sha
    rk = f"dup-2.r{_h.sha256(b'dup-2').hexdigest()[:8]}.b{body_sha(second)[:16]}"
    h.ok(h.post("/svc/v1/inbound/email", {**base, "request_id": rk, "text": "UNSUBSCRIBE"},
                caller="email_gateway"), 201)
    assert _both_revoked(h, cid)


# ------------------------------------------------------------------ AEGIS re-review of d1477aa (REVISE)

def test_ha_asking_to_be_reached_by_email_keeps_email_consent():
    import channels
    for t in ("Stop texting me. Email me instead.", "Please stop texting me, I prefer email.",
              "stop texting me, my email is a@b.com", "stop texting me, only use email"):
        assert channels.email_opt_out(t) is False, t
    for t in ("stop texting me, same for email", "stop texting me and emailing me too",
              "unsubscribe me from texts as well as emails", "stop texting me and spamming my inbox"):
        assert channels.email_opt_out(t) is True, t


def test_hb_an_outlook_header_block_is_a_quote_header(tmp_path):
    text = ("Sounds good, thanks!\n\nFrom: Acme Support <help@acme.test>\nDate: Monday, October 5, 2026\n"
            "To: owner@acme.test\nSubject: Your order\n\nYou can cancel or change your order any time.")
    h = Harness(tmp_path)
    cid = _with_consents(h)
    h.ok(_email(h, "owner@acme.test", text, subject="Re: Your order"), 201)
    assert _consents(h, cid) == {"email": "active", "sms": "active"}
    assert not any(a["code"] == "OPT_OUT_IN_QUOTED_TEXT" for a in h.svc.alerts.values())


def test_m1_ordinary_words_in_our_quoted_mail_do_not_alert():
    import channels
    for quoted in ("It ships by the end of the week.", "You can cancel anytime.", "No more than one update a day."):
        assert channels.quoted_tail_opt_out(_OUTLOOK.replace("How was your visit?", quoted) + "ok") is None, quoted


def test_m2_a_forwarded_newsletter_is_never_an_opt_out():
    import channels
    for header in ("---------- Forwarded message ---------", "Begin forwarded message:"):
        t = "Is this from you?\n\n" + header + "\nFrom: News\n\nTo unsubscribe click here."
        assert channels.email_opt_out(t) is False, header
        assert channels.quoted_tail_opt_out(t) is None, header


def test_email_angle_bracket_stop_is_an_opt_out():
    import channels
    for t in ("<STOP>", "Please <unsubscribe> me", "<p>STOP</p>"):
        assert channels.email_opt_out(t) is True, t


def test_l2_a_blank_footer_entry_is_ignored(monkeypatch):
    import channels
    monkeypatch.setattr(channels, "OWN_FOOTER_LINES", ("", "   "))
    assert channels.quoted_tail_opt_out(_OUTLOOK + "UNSUBSCRIBE") == "revoke"


# ------------------------------------------------------------------ AEGIS re-review of 3c89631 (REVISE)

def test_hc_text_or_email_me_is_an_email_opt_out():
    import channels
    for t in ("Do not text or email me", "dont text or email me again", "Please don't text or email me anymore.",
              "please stop texting me and emailing me, instead call me",
              "stop texting and emailing me, I only want to be contacted by mail"):
        assert channels.email_opt_out(t) is True, t
    assert channels.email_opt_out("Stop texting me. Email me instead.") is False


def test_m4_a_short_stop_above_a_name_signoff_alerts():
    import channels
    for reply in ("Stop!\n\nJane Doe\nCEO, Acme", "STOP\nJane", "Stop contacting me", "Quit it"):
        assert channels.quoted_tail_opt_out(_OUTLOOK + reply) in ("alert", "revoke"), reply


def test_m5_a_customers_own_from_date_lines_are_not_a_quote_header():
    import channels
    t = "From: Jane Smith\nDate: Oct 1\nItem: blue mug\nAlso please stop texting me."
    assert channels.opt_out_level(channels.strip_quoted(t)) == "exact"


def test_l4_style_and_script_are_not_the_persons_words():
    import channels
    assert channels.email_opt_out("<style>.unsubscribe{color:red}</style><p>Thanks, see you Friday</p>") is False


# ------------------------------------------------------------------ AEGIS re-review of ffe7ede (REVISE)

def test_hd_asking_to_be_emailed_is_not_an_email_opt_out():
    import channels
    for t in ("stop texting me and email me instead", "Please stop texting and email me instead",
              "Stop texting me or email me if you must", "stop the texts, emails are fine",
              "Stop texting me. Email me instead.", "stop texting me, my email is a@b.com"):
        assert channels.email_opt_out(t) is False, t
    for t in ("Do not text or email me", "please stop texting and emailing me", "dont text me nor email me",
              "stop texting me, same for email"):
        assert channels.email_opt_out(t) is True, t


def test_l6_our_quoted_short_lines_do_not_alert():
    import channels
    for line in ("Stop by anytime!", "Stop in today", "Don't stop now!", "Non-stop support"):
        assert channels.quoted_tail_opt_out(_OUTLOOK.replace("How was your visit?", "Hi") + line + "\n\nAcme Team") \
            is None, line


def test_m7_an_unclosed_style_flood_is_cheap():
    import time
    import channels
    t = "<style>" * 2800
    start = time.perf_counter()
    channels.html_as_text(t)
    assert time.perf_counter() - start < 0.05


# ------------------------------------------------------------------ AEGIS re-review of 37eff26 (REVISE)

def test_he_a_polite_or_qualified_text_or_email_opt_out_keeps_email():
    import channels
    for t in ("Do not text or email me please", "Stop the texts and the emails please. I would rather not hear from you.",
              "Dont text or email me if you can help it", "Do not text or email me, only call",
              "Stop the texts and the emails please. I prefer you call."):
        assert channels.email_opt_out(t) is True, t
    for t in ("Stop texting me or email me if you must", "stop texting me and email me instead"):
        assert channels.email_opt_out(t) is False, t


# ------------------------------------------------------------------ AEGIS re-review of b7cc067 (REVISE): decide only clear

def test_hf_a_request_to_be_called_or_emailed_keeps_email():
    import channels
    for t in ("Stop texting me, call or email me instead", "Please stop texting. Call or email if needed",
              "stop texting me, you can call or email", "stop texting me, email or mail is fine"):
        assert channels.email_opt_out_decision(t) == "keep", t


def test_hf_unclear_email_scope_asks_andre(tmp_path):
    import channels
    assert channels.email_opt_out_decision("Do not text me, call or email me") == "keep"     # a request
    for t in ("Stop texting me. Email or regular mail please", "Do not call, text or email me",
              "stop texting me. emails"):
        assert channels.email_opt_out_decision(t) in ("ask", "revoke", "revoke_direct"), t
    assert channels.email_opt_out_decision("stop texting me. emails") == "ask"
    h = Harness(tmp_path)
    cid = _with_consents(h)
    h.ok(_email(h, "owner@acme.test", "stop texting me. emails"), 201)
    assert _consents(h, cid) == {"email": "active", "sms": "revoked"}
    assert any(a["code"] == "EMAIL_OPT_OUT_UNCLEAR" for a in h.svc.alerts.values())


def test_scope_corpus_all_rounds_at_once():
    import channels
    revoke = ("Do not text or email me", "dont text or email me again", "Please don't text or email me anymore.",
              "Do not text or email me please", "Dont text or email me if you can help it",
              "Do not text or email me, only call", "please stop texting and emailing me",
              "please stop texting me and emailing me, instead call me", "stop texting me, same for email",
              "Stop texting me - that goes for email too", "unsubscribe me from your texts and emails",
              "unsubscribe me from texts as well as emails", "stop sending me texts or emails",
              "remove my number and my email", "stop texting me and spamming my inbox",
              "Stop the texts and the emails please. I would rather not hear from you.",
              "Stop the texts and the emails please. I prefer you call.", "unsubscribe", "STOP", "do not email me")
    keep = ("stop texting me", "remove my number", "unsubscribe from texts", "please no more texts",
            "Stop texting me. Email me instead.", "Please stop texting me, I prefer email.",
            "stop texting me, my email is a@b.com", "stop texting me, only use email",
            "stop texting me and email me instead", "Please stop texting and email me instead",
            "Stop texting me or email me if you must", "stop the texts, emails are fine",
            "Stop texting me, call or email me instead", "stop texting me, you can call or email")
    for t in revoke:
        assert channels.email_opt_out_decision(t) == "revoke", t
    for t in keep:
        assert channels.email_opt_out_decision(t) == "keep", t


# ------------------------------------------------------------------ AEGIS re-review of 7d58d7b (REVISE)

def test_hg_a_two_item_comma_is_two_clauses():
    import channels
    for t in ("Don't text, email me instead", "Please don't text, email me", "Don't text, email is better",
              "don't text, e-mail me", "Don't text, email me if there's a problem", "Do not text, email me at jane@x.com"):
        assert channels.email_opt_out_decision(t) in ("keep", "ask"), t
    assert channels.email_opt_out_decision("Do not call, text or email me") in ("revoke", "revoke_direct")


def test_m9_call_wording_alone_never_revokes_sms():
    import channels
    for t in ("Never call before 9 please", "Do not call the buzzer, it is broken"):
        assert channels.opt_out_level(t) != "exact", t


def test_hh_call_or_email_opt_outs_revoke_email(tmp_path):
    import channels
    for t in ("Do not call or email me", "Please don't call or email me", "dont call or email",
              "Please do not call me or email me about this", "Never call or email me again",
              "Dont call me or email me anymore", "Don't call, text, or email me"):
        assert channels.email_opt_out_decision(t) in ("revoke", "revoke_direct"), t
    h = Harness(tmp_path)
    cid = _with_consents(h)
    r = h.ok(_email(h, "owner@acme.test", "Do not call or email me"), 201)
    assert r.get("email_opted_out") is True and _consents(h, cid)["email"] == "revoked"



# ------------------------------------------------------------------ AEGIS re-review of 2018cd2 (REVISE)

def test_hi_complaints_and_time_limits_never_revoke_email(tmp_path):
    import channels
    for t in ("Why do you never call or email back? I've been waiting a week.",
              "My order is late and you never call or email me with updates!",
              "We never call or email asking for your password, right?",
              "Do not call or email the old address, it's changed to j@x.com",
              "Dont call or email me before 9am please, I work nights", "Please don't call or email after 8pm"):
        assert channels.email_opt_out_decision(t) in (None, "ask", "keep"), t
    h = Harness(tmp_path)
    cid = _with_consents(h)
    h.ok(_email(h, "owner@acme.test", "Why do you never call or email back? I've been waiting a week."), 201)
    assert _consents(h, cid)["email"] == "active"


def test_hi_a_direct_revoke_is_alerted_in_its_own_words(tmp_path):
    h = Harness(tmp_path)
    cid = _with_consents(h)
    h.ok(_email(h, "owner@acme.test", "Please do not call or email me again."), 201)
    assert _consents(h, cid)["email"] == "revoked"
    codes = {a["code"] for a in h.svc.alerts.values()}
    assert "EMAIL_OPTED_OUT_BY_REQUEST" in codes and "SMS_OPT_OUT_SUSPECTED" not in codes
