"""AEGIS sweep A (on 5d49ee9) — regression tests for the sales-py findings. Each one failed on 5d49ee9 and passes
after the fix (the probes in sweep-A/sales passed while the bug existed)."""

from helpers import FakeLedger, FakeSource, Harness, rid, wired_ports


def _evidence(h, event_type=None):
    q = f"?event_type={event_type}" if event_type else ""
    return h.ok(h.get(f"/sales/v1/audit/evidence{q}", "compliance_38"))


# ------------------------------------------------------------------ 1. R6-M1: evidence id carries the payload hash

def test_r6m1_stop_retry_after_the_contact_resolves_is_honoured(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    body = {"request_id": rid(), "channel": "email", "from_email": "jane@acme-shop.test", "text": "STOP"}
    led.fail_types = {"log_anchor"}
    assert h.post("/sales/v1/replies", body, caller="provider_events").status_code == 503
    led.fail_types = set()
    assert len(led.of_type("suppression_added")) == 1                # recorded first, never applied
    assert h.svc.verify_integrity(force=True, always=True)["ok"]
    h.lead(email="jane@acme-shop.test", phone="+13105550100")        # the sender now resolves: another payload
    r = h.ok(h.post("/sales/v1/replies", body, caller="provider_events"), 201)   # was 503 for ever (LedgerConflict)
    assert r["suppressed"] is True and h.svc.suppression
    again = h.ok(h.post("/sales/v1/replies", body, caller="provider_events"), 201)
    assert again == r
    ev = _evidence(h, "suppression_added")
    assert ev["total"] == 2 and ev["committed"] == 1 and ev["attempted"] == 1


def test_r6m1_time_zone_retry_after_another_change_is_not_a_permanent_503(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led)
    cid = h.lead(tz=None)["contact_id"]
    a = {"request_id": rid(), "time_zone": "America/Los_Angeles"}
    led.fail_types = {"log_anchor"}
    assert h.post(f"/sales/v1/contacts/{cid}/time-zone", a, "hub").status_code == 503
    led.fail_types = set()
    h.ok(h.post(f"/sales/v1/contacts/{cid}/time-zone", {"request_id": rid(), "time_zone": "America/Los_Angeles"},
                "hub"))
    codes = [h.post(f"/sales/v1/contacts/{cid}/time-zone", a, "hub").status_code for _ in range(2)]
    assert codes == [200, 200]


def test_r6m1_evidence_names_rk_and_seq_and_an_orphan_is_attempted(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    t = h.template(approve=False)
    v = t["versions"][0]
    path = f"/sales/v1/templates/{t['template_id']}/versions/1/approve"
    body = {"request_id": rid(), "content_sha256": v["content_sha256"]}
    led.fail_types = {"log_anchor"}
    assert h.andre(path, body).status_code == 503
    led.fail_types = set()
    p = led.of_type("template_approved")[0]["_payload"]
    assert p["rk"].startswith("rk-") and body["request_id"] not in p["rk"] and isinstance(p["seq"], int)   # AEGIS L4
    ev = _evidence(h, "template_approved")
    assert ev["attempted"] == 1 and ev["committed"] == 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"]
    h.ok(h.andre(path, body))
    ev = _evidence(h, "template_approved")
    assert ev["committed"] == 1
    row = next(e for e in ev["evidence"] if e["status"] == "committed")
    line = next(r for r in h.svc.log.iter_records() if r["seq"] == row["seq"])
    assert row["event_id"] in [e["event_id"] for e in line["data"]["ledger_evidence"]]


def test_audit_evidence_is_for_the_dashboard_and_compliance_only(h):
    assert h.get("/sales/v1/audit/evidence", "sales_agent").status_code == 403


# ------------------------------------------------------------------ 2. opt-out wording: "_", repeated letters, unsub

def test_underscore_repeated_letters_and_unsub_are_opt_outs_on_email(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    for i, text in enumerate(("please_unsubscribe", "STOP_please", "S_T_O_P", "unsub", "stoooop")):
        email = f"jane{i}@acme-shop.test"
        h.vlead(email=email, phone=None, tz=None)
        r = h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": email,
                                              "text": text}, caller="provider_events"), 201)
        assert r["class"] == "unsubscribe" and r["suppressed"] is True, (text, r)


def test_classifier_variants():
    from intelligences import i10_replies
    for t in ("please_unsubscribe", "STOP_please", "S_T_O_P", "unsub", "stoooop", "UNSUBBBSCRIBE", "stop_texting"):
        assert i10_replies.classify(t, "email") == "unsubscribe", t
    assert i10_replies.classify("I'd like to book a call", "email") == "interested"
    assert i10_replies.classify("hello there", "email") == "review"


# ------------------------------------------------------------------ 3. replies are never refused

def test_stop_with_a_display_name_sender_or_a_long_thread_is_honoured(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    lead = h.vlead(phone=None, tz=None)
    t = h.template()
    m = h.ok(h.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                                  "template_id": t["template_id"], "version": 1}, "sales_agent"), 201)
    h.ok(h.job("send-queue"))
    r = h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "message_id": m["message_id"],
                                          "from_email": "Jane Doe <jane@acme-shop.test>", "text": "STOP"},
                    caller="provider_events"), 201)                     # was 422
    assert r["suppressed"] is True and r["ignored"] == []
    long_text = "STOP\n\n" + "> quoted\n" * 1200                       # > 10,000 characters: was 422
    assert len(long_text) > 10_000
    r2 = h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "message_id": m["message_id"],
                                           "from_email": "jane@acme-shop.test", "text": long_text},
                     caller="provider_events"), 201)
    assert r2["class"] == "unsubscribe" and r2["suppressed"] is True


def test_a_display_name_sender_alone_resolves_the_contact(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    h.vlead(email="jane@acme-shop.test", phone=None, tz=None)
    r = h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email",
                                          "from_email": "\"Doe, Jane\" <Jane@Acme-Shop.test>", "text": "unsubscribe"},
                    caller="provider_events"), 201)
    assert r["suppressed"] is True


def test_unreadable_provider_fields_are_ignored_never_refused(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    r = h.ok(h.post("/sales/v1/replies", {"request_id": 7, "channel": "fax", "message_id": "sl-msg-unknown",
                                          "from_email": "not an address", "from_phone": "12", "text": "x" * 30_000,
                                          "ip_address": "whatever", "unknown": {"a": 1}},
                    caller="provider_events"), 201)
    assert set(r["ignored"]) == {"message_id", "from_email", "from_phone"} and r["task_id"]
    raw = b"".join(h.svc.log.raw_lines())
    assert b"whatever" not in raw and b"not an address" not in raw


def test_same_request_id_with_another_body_is_another_reply(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    h.vlead(email="jane@acme-shop.test", phone=None, tz=None)
    r_id = rid()
    a = h.ok(h.post("/sales/v1/replies", {"request_id": r_id, "channel": "email", "from_email": "jane@acme-shop.test",
                                          "text": "thanks"}, caller="provider_events"), 201)
    b = h.ok(h.post("/sales/v1/replies", {"request_id": r_id, "channel": "email", "from_email": "jane@acme-shop.test",
                                          "text": "actually, STOP"}, caller="provider_events"), 201)   # never 409
    assert a["reply_id"] != b["reply_id"] and b["suppressed"] is True


# ------------------------------------------------------------------ 4. lead import idempotency

def _import_harness(tmp_path, n=5):
    recs = [{"contact": {"name": f"P{i}", "email": f"p{i}@shop-{i}.test"}, "product_interest": ["social"],
             "evidence": {"kind": "public_record", "ref": f"pr-{i}", "captured_at": "2026-10-01T00:00:00Z"}}
            for i in range(n)]
    ports = wired_ports()
    ports.sources["public_data"] = FakeSource(recs)
    return Harness(tmp_path, ports=ports)


def test_import_request_id_reuse_with_another_body_is_409(tmp_path):
    h = _import_harness(tmp_path)
    r_id = rid()
    a = h.ok(h.post("/sales/v1/leads/import", {"request_id": r_id, "source": "public_data", "limit": 2},
                    "sales_agent"))
    assert len(a["created"]) == 2
    h.refused(h.post("/sales/v1/leads/import", {"request_id": r_id, "source": "public_data", "limit": 5},
                     "sales_agent"), 409, "REQUEST_ID_REUSED")
    assert len(h.svc.leads) == 2


def test_import_retry_answers_the_first_result_and_survives_a_restart(tmp_path):
    h = _import_harness(tmp_path)
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=h.ports)
    body = {"request_id": rid(), "source": "public_data", "limit": 3}
    a = h.ok(h.post("/sales/v1/leads/import", body, "sales_agent"))
    h2 = h.restart()
    again = h2.ok(h2.post("/sales/v1/leads/import", body, "sales_agent"))
    assert again["already_ran"] is True and again["created"] == a["created"] and len(h2.svc.leads) == 3


# ------------------------------------------------------------------ 5. an inert close()

def test_a_closed_instance_is_inert(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    h.lead()
    n_events, n_lines = len(led.events), len(h.svc.log)
    h.svc.close()
    assert h.svc.closed is True
    led.fail_reads = True                                    # any ledger read would be visible as "cannot be read"
    res = h.svc.verify_integrity(force=True, always=True)
    assert res["ok"] is False and "closed" in res["problem"]
    assert h.lead(email="z@zz-shop.test", code=503)["detail"] == "SERVICE_CLOSED"
    for job in ("send-queue", "integrity", "stale-leads"):
        h.refused(h.post(f"/sales/v1/jobs/{job}/run", {"request_id": rid()}, "scheduler"), 503, "SERVICE_CLOSED")
    h.refused(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "a@b-shop.test",
                                           "text": "STOP"}, "provider_events"), 503, "SERVICE_CLOSED")
    for path in ("/sales/v1/audit/integrity", "/sales/v1/audit/evidence", "/sales/v1/audit/export"):
        h.refused(h.get(path, "compliance_38"), 503, "SERVICE_CLOSED")
    assert len(led.events) == n_events and len(h.svc.log) == n_lines


# ------------------------------------------------------------------ AEGIS review of 17cda6a (REVISE)

def test_l4_the_ledger_never_sees_a_raw_request_id(tmp_path):
    led = FakeLedger()
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    r_id = "r-caller-chosen-" + rid()[2:]
    h.vlead(email="jane@acme-shop.test", phone=None, tz=None)
    h.ok(h.post("/sales/v1/replies", {"request_id": r_id, "channel": "email", "from_email": "jane@acme-shop.test",
                                      "text": "STOP"}, caller="provider_events"), 201)
    assert r_id not in str([e["_payload"] for e in led.events]) and r_id not in str([e for e in led.entries()])
    assert all(e["_payload"]["rk"].startswith("rk-") for e in led.events if "rk" in e["_payload"])
    assert _evidence(h, "suppression_added")["committed"] == 1


def test_l2_an_opt_out_past_the_reply_cap_is_still_read(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    h.vlead(email="jane@acme-shop.test", phone=None, tz=None)
    text = "A long story about our order. " * 1000 + "\n\nUNSUBSCRIBE"
    assert len(text) > 25_000
    r = h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "jane@acme-shop.test",
                                          "text": text}, caller="provider_events"), 201)
    assert r["class"] == "unsubscribe" and r["suppressed"] is True


def _paging_ledger(entries, honours_query=True, refuses=None):
    import httpx

    def handler(request):
        q = dict(request.url.params)
        if refuses and q:                    # 400: a strict ledger; 404: ledger-rust before fix-ledger
            return httpx.Response(refuses, json={"error": "no"})
        if not honours_query or not q:
            return httpx.Response(200, json=entries)
        after, limit = int(q.get("after_seq", -1)), int(q["limit"])
        out = [e for e in entries if e["seq"] > after and e.get("department") == q["department"]
               and e.get("event_type") == q.get("event_type", e.get("event_type"))]
        return httpx.Response(200, json=out[:limit])
    return httpx.MockTransport(handler)


def test_m4_paged_filtered_ledger_read_with_fallbacks(tmp_path):
    from ledger import HttpLedgerClient
    entries = [{"seq": i, "kind": "event", "department": "sales" if i % 3 else "finance",
                "event_type": "log_anchor" if i % 2 else "suppression_added", "event_id": f"e{i}"} for i in range(25)]
    want = [e for e in entries if e["department"] == "sales"]
    for honours, refuses in ((True, None), (False, None), (True, 400), (True, 404)):
        c = HttpLedgerClient("http://ledger.test", "t" * 32, transport=_paging_ledger(entries, honours, refuses))
        assert c.entries_filtered("sales", page_size=4) == want, (honours, refuses)
        assert c.entries_filtered("sales", "log_anchor", page_size=4) == \
            [e for e in want if e["event_type"] == "log_anchor"]
    led = FakeLedger()
    calls = []
    led.entries_filtered = lambda d, t=None: calls.append((d, t)) or [
        e for e in led.entries() if e["department"] == d and (t is None or e["event_type"] == t)]
    h = Harness(tmp_path, ledger=led, ports=wired_ports())
    h.vlead(email="jane@acme-shop.test", phone=None, tz=None)
    h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "jane@acme-shop.test",
                                      "text": "STOP"}, caller="provider_events"), 201)
    calls.clear()
    assert _evidence(h, "suppression_added")["committed"] == 1
    assert calls == [("sales", "log_anchor"), ("sales", "suppression_added")]


# ------------------------------------------------------------------ AEGIS re-review of 1e709a0 (REVISE)

def test_n2_evidence_written_before_the_keyed_rk_still_reads_committed(tmp_path, monkeypatch):
    import service
    led = FakeLedger()
    orig = service.SalesService._evidence_rk
    monkeypatch.setattr(service.SalesService, "_evidence_rk", lambda self, k: k)     # the pre-L4 raw rk
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), ledger=led, ports=wired_ports())
    h.vlead(email="jane@acme-shop.test", phone=None, tz=None)
    h.ok(h.post("/sales/v1/replies", {"request_id": rid(), "channel": "email", "from_email": "jane@acme-shop.test",
                                      "text": "STOP"}, caller="provider_events"), 201)
    monkeypatch.setattr(service.SalesService, "_evidence_rk", orig)
    h2 = h.restart()
    ev = _evidence(h2, "suppression_added")
    assert ev["committed"] == 1 and ev["attempted"] == 0
    assert all(r["rk"] is None or r["rk"].startswith("rk-") for r in ev["evidence"])
