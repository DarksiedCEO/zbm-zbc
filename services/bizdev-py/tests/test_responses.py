"""Boilerplate blocks, responses and pitches: Andre approves by hash, any change invalidates, submission gates
(deadline from stored data and the injected clock, never the wall clock), the queue, win and hand-offs."""

from datetime import timedelta

from clock import iso
from helpers import T0, Harness, RecordingSubmission, rid, wired_ports


def _responding(h, **kw):
    p = h.pursuit(**kw)
    return h.bid(p["pursuit_id"])


def test_block_approval_binds_hash(h):
    b = h.block(approve=False)
    v = b["versions"][0]
    h.refused(h.post(f"/blocks/{b['block_id']}/versions/1/approve", {"request_id": rid(), "content_sha256": v[
        "content_sha256"]}), 403)
    h.refused(h.post(f"/blocks/{b['block_id']}/versions/1/approve", {"request_id": rid(), "content_sha256": "0" * 64},
                     andre=True), 409, "BLOCK_HASH_MISMATCH")
    b = h.ok(h.post(f"/blocks/{b['block_id']}/versions/1/approve",
                    {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True))
    assert b["versions"][0]["status"] == "approved"
    h.refused(h.post(f"/blocks/{b['block_id']}/versions/1/approve",
                     {"request_id": rid(), "content_sha256": v["content_sha256"]}, andre=True), 409,
              "BLOCK_ALREADY_APPROVED")


def test_response_refuses_unapproved_or_wrong_brand_block(h):
    p = _responding(h)
    draft = h.block("draft-only", approve=False)
    h.refused(h.post("/responses", {"request_id": rid(), "pursuit_id": p["pursuit_id"],
                                    "parts": [{"block_id": draft["block_id"], "version": 1}]}), 409,
              "BLOCK_NOT_APPROVED")
    zbc = h.block("zbc-only", brand="zbc")
    h.refused(h.post("/responses", {"request_id": rid(), "pursuit_id": p["pursuit_id"],
                                    "parts": [{"block_id": zbc["block_id"], "version": 1}]}), 409,
              "BLOCK_BRAND_MISMATCH")
    both = h.block("shared", brand="both")
    assert h.ok(h.post("/responses", {"request_id": rid(), "pursuit_id": p["pursuit_id"],
                                      "parts": [{"block_id": both["block_id"], "version": 1}]}), 201)


def test_response_needs_bid_decision(h):
    p = h.pursuit()
    b = h.block()
    h.refused(h.post("/responses", {"request_id": rid(), "pursuit_id": p["pursuit_id"],
                                    "parts": [{"block_id": b["block_id"], "version": 1}]}), 409,
              "BID_DECISION_REQUIRED")


def test_approval_needs_andre_and_exact_hash(h):
    p = _responding(h)
    b = h.block()
    r = h.response(p["pursuit_id"], [{"block_id": b["block_id"], "version": 1}, {"custom": "Six weeks of boards."}])
    v = r["versions"][0]
    body = {"request_id": rid(), "version": 1, "content_sha256": v["content_sha256"], "acknowledged_flags": []}
    h.refused(h.post(f"/responses/{r['response_id']}/approve", body), 403)
    h.refused(h.post(f"/responses/{r['response_id']}/approve", {**body, "content_sha256": "1" * 64}, andre=True), 409,
              "RESPONSE_HASH_MISMATCH")
    h.refused(h.post(f"/responses/{r['response_id']}/approve", {**body, "version": 2}, andre=True), 409,
              "RESPONSE_SUPERSEDED")
    assert h.ok(h.post(f"/responses/{r['response_id']}/approve", body, andre=True))["versions"][0][
        "status"] == "approved"
    assert h.ledger.of_type("response_approved")


def test_any_change_after_approval_invalidates(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    old = r["versions"][0]
    r2 = h.ok(h.post(f"/responses/{r['response_id']}/versions",
                     {"request_id": rid(), "parts": [{"custom": "Changed text after approval."}]}), 201)
    assert r2["versions"][0]["status"] == "superseded" and r2["versions"][1]["status"] == "draft"
    h.refused(h.post(f"/responses/{r['response_id']}/submit",
                     {"request_id": rid(), "version": 1, "content_sha256": old["content_sha256"]}), 409,
              "RESPONSE_SUPERSEDED")
    h.refused(h.submit(r2), 409, "RESPONSE_NOT_APPROVED")


def test_new_block_version_does_not_change_approved_text_but_retire_blocks_it(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    bid = r["versions"][0]["doc"]["parts"][0]["block_id"]
    h.ok(h.post(f"/blocks/{bid}/versions", {"request_id": rid(), "title": "About", "text": "Changed."}), 201)
    assert h.ok(h.submit(r), 201)["status"] == "queued"          # version 1 still the approved text
    h.ok(h.post(f"/blocks/{bid}/versions/1/retire", {"request_id": rid()}, andre=True))
    out = h.ok(h.job("submission-queue"))
    assert out["cancelled"] == 1
    assert h.ok(h.get("/submissions"))[0]["reason"] == "BLOCK_NOT_APPROVED"


def test_tampered_block_text_in_memory_is_caught_at_submit(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    bid = r["versions"][0]["doc"]["parts"][0]["block_id"]
    h.svc.blocks[bid]["versions"]["1"]["text"] = "tampered"
    h.refused(h.submit(r), 409, "BLOCK_HASH_MISMATCH")


def test_sensitivity_flags_must_be_acknowledged_exactly(h):
    p = _responding(h)
    r = h.response(p["pursuit_id"], [{"custom": "We will host your council members at a dinner with tickets."}])
    v = r["versions"][0]
    assert v["flags"] == ["GIFT", "LOBBYING"]
    for ack in ([], ["GIFT"], ["GIFT", "LOBBYING", "CONTINGENT_FEE"]):
        h.refused(h.post(f"/responses/{r['response_id']}/approve",
                         {"request_id": rid(), "version": 1, "content_sha256": v["content_sha256"],
                          "acknowledged_flags": ack}, andre=True), 409, "FLAGS_NOT_ACKNOWLEDGED")
    h.approve_response(r, ["GIFT", "LOBBYING"])


def test_flag_normalisation_catches_obfuscation(h):
    from intelligences import i06_sensitivity
    assert i06_sensitivity.flags("a GÍFT card for the buyer") == ["GIFT"]
    assert i06_sensitivity.flags("L.o.b.b.y.i.n.g support") == ["LOBBYING"]
    assert i06_sensitivity.flags("a success-fee arrangement") == ["CONTINGENT_FEE"]
    assert i06_sensitivity.flags("Our team includes a former county employee") == ["CONFLICT_OF_INTEREST"]
    assert i06_sensitivity.flags("Twelve digital boards for six weeks.") == []


def test_submit_gates_and_queue_stays_queued_without_port(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    s = h.ok(h.submit(r), 201)
    h.refused(h.submit(r), 409, "RESPONSE_ALREADY_QUEUED")
    assert h.ledger.of_type("submission_queued")
    for _ in range(2):
        assert h.ok(h.job("submission-queue"))["not_wired"] == 1
    assert h.ok(h.get("/submissions?status=queued"))[0]["submission_id"] == s["submission_id"]
    assert not h.ledger.of_type("submission_sending")


def test_submission_past_deadline_refused_by_injected_clock(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    h.clock.at = T0 + timedelta(days=14)                 # exactly the deadline: late
    h.refused(h.submit(r), 409, "DEADLINE_PASSED")
    h.clock.at = T0 + timedelta(days=14) - timedelta(seconds=1)
    assert h.ok(h.submit(r), 201)["status"] == "queued"


def test_queued_submission_cancelled_when_deadline_passes(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    h.ok(h.submit(r), 201)
    h.clock.advance(days=15)
    assert h.ok(h.job("submission-queue"))["cancelled"] == 1
    sub = h.ok(h.get("/submissions"))[0]
    assert sub["status"] == "cancelled" and sub["reason"] == "DEADLINE_PASSED"
    assert h.ports.submission.calls == []


def test_deadline_sweep_cancels_and_opens_task(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    h.ok(h.submit(r), 201)
    p2 = h.pursuit(ref="org:late", name="Late Co", domain="late.test")
    h.clock.advance(days=20)
    out = h.ok(h.job("deadline-sweep"))
    assert out == {"job": "deadline-sweep", "cancelled": 1, "tasks_opened": 2}
    assert h.ok(h.job("deadline-sweep"))["tasks_opened"] == 0          # once per pursuit and deadline
    kinds = {t["kind"] for t in h.ok(h.get("/tasks"))}
    assert "deadline_passed" in kinds and p2


def test_wired_submission_then_won_by_andre_hands_off(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"], custom="Six weeks of boards across the county.")
    h.ok(h.submit(r), 201)
    assert h.ok(h.job("submission-queue"))["submitted"] == 1
    call = h.ports.submission.calls[0]
    assert "Six weeks of boards" in call[3] and "Z Best Media runs outdoor campaigns." in call[3]
    assert [e["event_type"] for e in h.ledger.events if e["subject_id"].startswith("submission:")][-2:] == [
        "submission_sending", "submission_result"]
    assert h.ok(h.get(f"/pursuits/{p['pursuit_id']}"))["stage"] == "submitted"
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}), 403)
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}, caller="dashboard"), 403)
    won = h.ok(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}, andre=True))
    assert won["stage"] == "won" and {x["status"] for x in won["handoffs"]} == {"delivered"}
    assert {x["kind"] for x in won["handoffs"]} == {"onboarding_create_client", "finance_invoice_draft"}


def test_won_with_stand_ins_stays_pending_delivery(tmp_path):
    ports = wired_ports()
    from ports import NotWiredHandoff
    ports.onboarding, ports.finance = NotWiredHandoff(), NotWiredHandoff()
    h = Harness(tmp_path, ports=ports)
    p = _responding(h)
    h.ok(h.submit(h.ready_response(p["pursuit_id"])), 201)
    h.ok(h.job("submission-queue"))
    won = h.ok(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}, andre=True))
    assert {x["status"] for x in won["handoffs"]} == {"pending_delivery"}
    assert h.ok(h.job("handoff-retry"))["pending_delivery"] == 2


def test_refused_submission_can_be_resubmitted(tmp_path):
    h = Harness(tmp_path, ports=wired_ports(submission=RecordingSubmission("refused")))
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    h.ok(h.submit(r), 201)
    assert h.ok(h.job("submission-queue"))["refused"] == 1
    h.ports.submission.status = "accepted"
    h.ok(h.submit(r), 201)
    assert h.ok(h.job("submission-queue"))["submitted"] == 1


def test_won_refused_before_submission(h):
    p = _responding(h)
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/won", {"request_id": rid()}, andre=True), 409, "STAGE_NOT_ALLOWED")


def test_pitch_needs_andre_approval_regardless_of_value(h):
    p = h.pursuit(kind="formal_pitch", value="0.00", deadline=None)
    h.bid(p["pursuit_id"])
    b = h.block()
    r = h.response(p["pursuit_id"], [{"block_id": b["block_id"], "version": 1}])
    h.refused(h.submit(r), 409, "RESPONSE_NOT_APPROVED")
    h.approve_response(r)
    assert h.ok(h.submit(r), 201)["status"] == "queued"


def test_cancel_submission(h):
    p = _responding(h)
    s = h.ok(h.submit(h.ready_response(p["pursuit_id"])), 201)
    h.ok(h.post(f"/submissions/{s['submission_id']}/cancel", {"request_id": rid()}))
    h.refused(h.post(f"/submissions/{s['submission_id']}/cancel", {"request_id": rid()}), 409, "SUBMISSION_NOT_QUEUED")


def test_withdraw_cancels_queued_submission(h):
    p = _responding(h)
    h.ok(h.submit(h.ready_response(p["pursuit_id"])), 201)
    h.ok(h.post(f"/pursuits/{p['pursuit_id']}/withdraw", {"request_id": rid()}, andre=True))
    assert h.ok(h.get("/submissions"))[0]["reason"] == "PURSUIT_CLOSED"


def test_deadline_extension_does_not_revive_without_andre(h):
    p = _responding(h)
    r = h.ready_response(p["pursuit_id"])
    h.clock.advance(days=15)
    h.refused(h.submit(r), 409, "DEADLINE_PASSED")
    h.refused(h.post(f"/pursuits/{p['pursuit_id']}/deadline",
                     {"request_id": rid(), "deadline": iso(T0 + timedelta(days=30))}), 403)
