"""
Live HTTP round-trip tests through FastAPI's TestClient (real ASGI
request/response cycle, real pydantic (de)serialization) — same pattern
as detection-py/tests/test_api.py.
"""

from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN

client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_source"] == "non-live"


def test_fixtures_roundtrip():
    r = client.get("/fixtures/call-events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 6  # matches fixtures/fulfillment_call_events.json


def test_missed_call_detection_endpoint_against_real_fixtures():
    events = client.get("/fixtures/call-events").json()
    r = client.post("/agents/missed-call-detection/detect", json={"call_events": events})
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 5  # 6 calls, 1 answered (control case) excluded


def test_appointment_tracking_endpoint_against_real_fixtures():
    appts = client.get("/fixtures/appointments").json()
    r = client.post("/agents/appointment-tracking/detect", json={"appointments": appts})
    assert r.status_code == 200
    # Result depends on real wall-clock "now" vs fixture dates (Sep 2026),
    # so only assert the endpoint round-trips and returns well-formed tasks
    # rather than a fixed count (that exact-count case is covered with a
    # frozen clock in test_appointment_tracking.py).
    for t in r.json()["tasks"]:
        assert t["purpose"] == "completion_check"


def test_callback_orchestration_endpoint_with_no_dialer_configured_reports_not_wired(monkeypatch):
    """CRITICAL finding, Sep 22 2026 independent review, now fixed: this
    endpoint previously defaulted to InMemorySipDialer and reported
    dial_placed=true for a call that was never placed. With
    FULFILLMENT_SIP_DIALER unset (the honest default — see
    conftest.py), it must now report attempted=false and say plainly
    that the dialer isn't wired, over the real live HTTP round trip, not
    just in the agent-level unit test.

    Sep 24 2026 audit: the request no longer accepts `now` (a caller-
    controlled clock could bypass quiet hours); the server clock is
    pinned via monkeypatch instead, and the recipient time zone is now
    required."""
    import api as api_module
    from datetime import datetime, timezone

    monkeypatch.setattr(api_module, "_now", lambda: datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc))
    r = client.post(
        "/agents/callback-orchestration/run",
        json={
            "tasks": [
                {
                    "task_id": "fu-x",
                    "purpose": "missed_call_callback",
                    "channel": "call",
                    "customer_id": "cust_x",
                    "source_call_id": "call_x",
                    "created_at": "2020-01-01T00:00:00Z",
                    "due_at": "2020-01-01T00:05:00Z",
                    "attempt_number": 1,
                    "status": "pending",
                    "reason": "test",
                }
            ],
            # Fix wave 1, F3: was "+15551234" (not a 10-digit NANP number) "in UTC",
            # a pairing the gate now refuses. A real New York number at 14:00 EDT
            # is inside the strict +1 window, which isolates the dialer-not-wired path.
            "phone_by_call_id": {"call_x": "+12125551234"},
            "line_by_call_id": {},
            "timezone_by_call_id": {"call_x": "America/New_York"},
        },
    )
    assert r.status_code == 200
    outcome = r.json()["outcomes"][0]
    assert outcome["attempted"] is False
    assert outcome["dial_placed"] is None
    assert "dialer not wired" in outcome["skip_reason"]


def test_customer_dossier_endpoint_persists_across_calls():
    events = client.get("/fixtures/call-events").json()
    r1 = client.post("/agents/customer-dossier/update", json={"call_events": events, "appointments": []})
    assert r1.status_code == 200
    dossier_ids = {d["customer_id"] for d in r1.json()["dossiers"]}
    assert "cust_f1" in dossier_ids


def test_resolution_writeback_endpoint_returns_not_configured_by_default():
    """CRITICAL finding, Sep 22 2026 independent review, now fixed: this
    endpoint previously defaulted to InMemorySystemOfRecord and reported
    write_back_status="success" for a write that never reached any real
    CRM. With FULFILLMENT_SYSTEM_OF_RECORD unset (the honest default),
    it must now report not_configured, over the real live HTTP round
    trip — this test's name was already claiming this before the fix
    existed; it now actually tests what it claims to."""
    r = client.post(
        "/agents/resolution-writeback/resolve",
        json={"events": [{"entity_type": "call", "entity_id": "call_1", "customer_id": "cust_1", "resolution_type": "booked"}]},
    )
    assert r.status_code == 200
    records = r.json()["records"]
    assert len(records) == 1
    assert records[0]["write_back_status"] == "not_configured"


def test_resolution_writeback_ids_are_unique_across_separate_api_calls():
    """CONFIRMED finding: resolution_id collided across separate request
    batches because it was built from a per-batch index. Verified fixed
    over the real API, not just the agent unit test."""
    payload = {"events": [{"entity_type": "call", "entity_id": "call_1001", "customer_id": "cust_f1", "resolution_type": "booked"}]}
    r1 = client.post("/agents/resolution-writeback/resolve", json=payload)
    r2 = client.post("/agents/resolution-writeback/resolve", json=payload)
    id1 = r1.json()["records"][0]["resolution_id"]
    id2 = r2.json()["records"][0]["resolution_id"]
    assert id1 != id2


def test_escalate_endpoint_records_a_resolution_when_sequence_exhausts():
    """CONFIRMED finding: a failed human_handoff (the last resort)
    previously vanished — next_task: null and nothing else. Now the API
    records it as an explicit NO_RESOLUTION, attempted write-back."""
    exhausted_task = {
        "task_id": "fu-x-esc4",
        "purpose": "escalation",
        "channel": "human_handoff",
        "customer_id": "cust_x",
        "source_call_id": "call_x",
        "created_at": "2026-09-22T12:00:00Z",
        "due_at": "2026-09-22T12:00:00Z",
        "attempt_number": 4,
        "status": "failed",
        "reason": "test",
    }
    r = client.post("/agents/followup-sequencing/escalate", json={"task": exhausted_task})
    assert r.status_code == 200
    body = r.json()
    assert body["next_task"] is None
    assert body["sequence_exhausted"] is True
    assert body["resolution"] is not None
    assert body["resolution"]["resolution_type"] == "no_resolution"
    assert body["resolution"]["write_back_status"] == "not_configured"  # honest default


def test_escalate_endpoint_normal_mid_sequence_step_is_not_treated_as_exhausted():
    mid_sequence_task = {
        "task_id": "fu-x",
        "purpose": "missed_call_callback",
        "channel": "call",
        "customer_id": "cust_x",
        "source_call_id": "call_x",
        "created_at": "2026-09-22T12:00:00Z",
        "due_at": "2026-09-22T12:00:00Z",
        "attempt_number": 1,
        "status": "failed",
        "reason": "test",
    }
    r = client.post("/agents/followup-sequencing/escalate", json={"task": mid_sequence_task})
    assert r.status_code == 200
    body = r.json()
    assert body["next_task"] is not None
    assert body["sequence_exhausted"] is False
    assert body["resolution"] is None


def test_opt_in_in_memory_adapters_are_explicit_not_default():
    """The in-memory test doubles still exist and are still reachable —
    but only via an explicit, named env var, never as the silent
    default. This test documents that opt-in exists without flipping
    the module-level default for the rest of the suite (that would
    require a process restart with the env var set beforehand, which is
    exercised instead by the live-process smoke test in the README)."""
    import os

    assert os.environ.get("FULFILLMENT_SIP_DIALER") != "in_memory"
    assert os.environ.get("FULFILLMENT_SYSTEM_OF_RECORD") != "in_memory"
