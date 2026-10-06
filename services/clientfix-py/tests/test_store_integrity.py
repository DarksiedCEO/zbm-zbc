"""Record-first durability (ADR 0017 decision 21), bizdev-py's design as fixed in its AEGIS rounds: restart keeps state,
a truncated or replaced log stops all writes, nothing takes effect without the ledger, an apply interrupted by a
ledger outage or a stop never sends an unrecorded request and is settled ``interrupted`` (frozen, Andre told),
unanchored evidence = attempted, and a closed instance is inert."""

from __future__ import annotations

import json
import os

import pytest

from helpers import CLIENT_A, PRODUCT, SHOP_A, Harness, rid
from service import ClientFixService


def _log(d):
    return os.path.join(d, "clientfix_log.jsonl")


def test_restart_keeps_everything_and_replay_equals_live_state(hd):
    conn, j = hd.seo_job()
    hd.ok(hd.apply(j["job_id"]))
    live = hd.ok(hd.get(f"/jobs/{j['job_id']}"))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is True
    again = h2.ok(h2.get(f"/jobs/{j['job_id']}"))
    assert again == live
    assert h2.ok(h2.get(f"/connections/{conn['connection_id']}"))["status"] == "active"
    assert h2.ok(h2.get("/leases"))[0]["status"] == "released"


def test_idempotent_replay_survives_restart_and_a_different_body_is_409(hd):
    conn = hd.connection()
    body = {"request_id": "6f0f2b1e-0000-4000-8000-00000000a001", "finding_id": "rr:one", "agent_id": "a",
            "client_id": CLIENT_A, "check_code": "product_seo_missing",
            "resource": {"connection_id": conn["connection_id"], "target": PRODUCT}}
    a = hd.ok(hd.post("/findings", body, caller="orchestrator"), 201)
    h2 = hd.restart()
    assert h2.ok(h2.post("/findings", body, caller="orchestrator"), 201) == a
    h2.refused(h2.post("/findings", {**body, "agent_id": "b"}, caller="orchestrator"), 409, "REQUEST_ID_REUSED")
    assert h2.ok(h2.post("/findings", {**body, "request_id": body["request_id"].upper()}, caller="orchestrator"),
                 201) == a


def test_every_line_is_anchored_and_no_client_value_reaches_the_ledger(h):
    conn, j = h.seo_job(title="Secret Product Title For Ledger Test")
    h.ok(h.apply(j["job_id"]))
    h.ok(h.tick("redetect"))
    anchors = h.ledger.of_type("log_anchor")
    assert len(anchors) == len(h.svc.log)
    blob = json.dumps([e["_payload"] for e in h.ledger.events]) + json.dumps(h.ledger.entries())
    assert "Secret Product Title" not in blob and SHOP_A not in blob and "vault:" not in blob
    assert "150.00" not in blob                                   # amounts enter as terms_sha256 only
    types = {e["event_type"] for e in h.ledger.events}
    assert {"connection_registered", "finding_added", "job_created", "quote_accepted", "payment_confirmed",
            "plan_submitted", "plan_approved", "apply_started", "lease_acquired", "apply_snapshot_taken",
            "apply_request_sending", "apply_request_answered", "apply_verified", "item_settled", "lease_released",
            "apply_finished", "redetected", "report_issued", "job_closed"} <= types


def test_audit_export_replaces_every_client_value_by_its_hash(hd):
    conn, j = hd.seo_job(title="Exported Title Text", before="Original Title Text")
    hd.ok(hd.apply(j["job_id"]))
    out = json.dumps(hd.ok(hd.get("/audit/export", params={"limit": 1000}, caller="compliance_38")))
    assert "Exported Title Text" not in out and "Original Title Text" not in out and '"sha256"' in out


def test_ledger_down_nothing_applied(h):
    conn = h.connection()
    h.ledger.fail = True
    r = h.post("/findings", {"request_id": rid(), "finding_id": "rr:x", "agent_id": "a", "client_id": CLIENT_A,
                             "check_code": "product_seo_missing",
                             "resource": {"connection_id": conn["connection_id"], "target": PRODUCT}},
               caller="orchestrator")
    h.refused(r, 503, "LEDGER_UNAVAILABLE")
    assert not h.svc.findings


def test_a_ledger_outage_mid_apply_sends_nothing_unrecorded_and_freezes(h):
    conn, j = h.seo_job()
    h.ledger.fail_types = {"apply_request_sending"}               # the record-first step for the write fails
    it = h.ok(h.apply(j["job_id"]))["items"][0]
    assert it["status"] == "interrupted" and it["result"]["failure"] == "LEDGER_UNAVAILABLE"
    assert not h.t.writes()                                       # the write never left
    assert h.ok(h.get("/frozen"))["resources"]
    assert any(t["code"] == "APPLY_INTERRUPTED" for t in h.ok(h.get("/tasks")))


def test_an_apply_left_running_by_a_stop_is_recovered_as_interrupted(hd):
    conn, j = hd.seo_job()
    jid = j["job_id"]
    # the process stops right after apply_started was committed (simulated: commit it, never run the items)
    with hd.svc.lock:
        job = hd.svc.jobs[jid]
        hd.svc._commit("apply_started", {"job_id": jid, "epoch": 0,
                                         "leases": [{"lease_id": "cfx-lse-" + "0" * 40,
                                                     "resource_key": f"shopify|{SHOP_A}|{PRODUCT}",
                                                     "item_id": next(iter(job["items"]))}]}, "scheduler")
    h2 = hd.restart()
    assert h2.ok(h2.get(f"/jobs/{jid}"))["status"] == "applying"
    assert h2.ok(h2.tick("recover"))["interrupted"] == 1
    jj = h2.ok(h2.get(f"/jobs/{jid}"))
    assert jj["items"][0]["status"] == "interrupted" and jj["status"] == "refund_pending"
    assert h2.ok(h2.get("/leases", params={"status": "active"})) == []
    assert h2.ok(h2.get("/frozen"))["resources"][0]["reason"] == "interrupted"


def test_truncated_log_is_detected_against_the_ledger(hd):
    conn, j = hd.seo_job()
    d = hd.settings.data_dir
    hd.svc.close()
    lines = open(_log(d), "rb").read().splitlines(keepends=True)
    open(_log(d), "wb").write(b"".join(lines[:-2]))
    h2 = Harness(hd.tmp, d, hd.ledger, hd.clock, hd.ports)
    assert h2.svc.integrity["ok"] is False and "anchors beyond" in h2.svc.integrity["problem"]
    h2.refused(h2.post("/connections/" + conn["connection_id"] + "/revoke", {"request_id": rid()}, caller="hub"), 503)
    assert h2.ok(h2.client.get("/health"))["status"] == "degraded"


def test_a_deleted_or_replaced_log_is_detected(hd):
    hd.connection()
    d = hd.settings.data_dir
    hd.svc.close()
    os.remove(_log(d))
    h2 = Harness(hd.tmp, d, hd.ledger, hd.clock, hd.ports)
    assert h2.svc.integrity["ok"] is False and "deleted or replaced" in h2.svc.integrity["problem"]
    r = h2.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "yelp",
                                 "account_ref": "abcdefghijklmnopqrstuv"}, caller="hub")
    h2.refused(r, 503, "INTEGRITY_UNVERIFIED")


def test_an_edited_log_line_refuses_start(hd):
    hd.connection()
    d = hd.settings.data_dir
    hd.svc.close()
    raw = open(_log(d), "rb").read().replace(b'"connector":"shopify"', b'"connector":"shopifx"', 1)
    open(_log(d), "wb").write(raw)
    with pytest.raises(Exception):
        Harness(hd.tmp, d, hd.ledger, hd.clock, hd.ports)


def test_second_service_instance_on_the_same_data_dir_refuses(hd):
    from ledger import Recorder
    from store import RecordLog
    with pytest.raises(Exception):
        ClientFixService(hd.settings, Recorder(hd.ledger), RecordLog(hd.settings.data_dir), hd.ports, hd.clock,
                         lock_token="not-the-token")


def test_the_evidence_view_marks_unanchored_evidence_attempted(h):
    conn = h.connection()
    h.ledger.fail_types = {"log_anchor"}
    r = h.post("/findings", {"request_id": rid(), "finding_id": "rr:attempt", "agent_id": "a", "client_id": CLIENT_A,
                             "check_code": "product_seo_missing",
                             "resource": {"connection_id": conn["connection_id"], "target": PRODUCT}},
               caller="orchestrator")
    assert r.status_code == 503
    h.ledger.fail_types = set()
    ev = h.ok(h.get("/audit/evidence", caller="compliance_38"))
    rows = {(e["event_type"], e["status"]) for e in ev["evidence"]}
    assert ("finding_added", "attempted") in rows and ("connection_registered", "committed") in rows
    assert ev["rule"] == "unanchored evidence = attempted, not done"


def test_close_makes_the_instance_inert(h):
    conn = h.connection()
    h.svc.close()
    h.refused(h.post(f"/connections/{conn['connection_id']}/revoke", {"request_id": rid()}, caller="hub"), 503,
              "SERVICE_CLOSED")
    h.refused(h.tick("integrity"), 503, "SERVICE_CLOSED")
    assert h.client.get("/health").status_code == 503
    n = len(h.ledger.events)
    h.svc.verify_integrity(force=True, always=True)
    assert len(h.ledger.events) == n


def test_the_integrity_tick_reports_the_ledgers_own_verdict(h):
    h.ledger.verify_result = False
    out = h.ok(h.tick("integrity"))
    assert out["ledger_valid"] is False
    h.ledger.verify_result = True
    assert h.ok(h.get("/audit/integrity", caller="compliance_38"))["ledger_valid"] is True
