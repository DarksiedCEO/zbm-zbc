"""Bug sweep D (Oct 6 2026 sweep at integration 5d49ee9) -- fulfillment-py. Each test pins one finding and FAILS on
5d49ee9 (the sweep's probes were lost; these are re-derived from the code):

  H   no ledger at all: a callback dial and a write-back left no evidence anywhere. Now record-first (R6 pattern)
      with a local anchored log; GET /audit/evidence says committed vs attempted
  M   /resolve was not idempotent (a retry wrote back again) and recorded no evidence
  M   call limits (the gate's attempt history) and the dial dedupe reset on restart: now replayed from the log
  E-5/F-3  the store survives one fsync error; one process per data directory
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
from conftest import TEST_SERVICE_TOKEN
from fulfillment_schema import FollowUpTask, TaskChannel, TaskPurpose
from integrations.sip_dialer import InMemorySipDialer
from integrations.system_of_record import InMemorySystemOfRecord
from ledger import FakeLedgerClient
from store import LOG_NAME, RecordLog

client = TestClient(api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
ROUTE = "/agents/callback-orchestration/run"
RESOLVE = "/agents/resolution-writeback/resolve"
T = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)   # 11:00 Los Angeles
LA = "+12135550101"


class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def env(monkeypatch):
    dialer = InMemorySipDialer()
    clock = _Clock(T)
    monkeypatch.setattr(api, "_dialer", dialer)
    monkeypatch.setattr(api, "_now", clock)
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    return dialer, clock


def _task(task_id: str, call_id: str = "call_1", customer_id: str | None = "cust_1") -> dict:
    return FollowUpTask(
        task_id=task_id, purpose=TaskPurpose.MISSED_CALL_CALLBACK, channel=TaskChannel.CALL,
        customer_id=customer_id, source_call_id=call_id,
        created_at=T - timedelta(days=2), due_at=T + timedelta(minutes=5), reason="r",
    ).model_dump(mode="json")


def _dial(task_id: str, customer_id: str | None = "cust_1") -> dict:
    r = client.post(ROUTE, json={"tasks": [_task(task_id, customer_id=customer_id)], "phone_by_call_id": {"call_1": LA},
                                 "timezone_by_call_id": {"call_1": "America/Los_Angeles"}})
    assert r.status_code == 200, r.text
    return r.json()["outcomes"][0]


def _evidence(**q) -> dict:
    r = client.get("/audit/evidence", params=q)
    assert r.status_code == 200, r.text
    return r.json()


# ============================================================================================ H: dial evidence


def test_h_a_dial_is_recorded_and_its_line_anchored_before_the_dialer_is_called(env):
    dialer, _ = env
    order: list[str] = []
    real_record = api._ledger.record_event

    def spy(event_id, event_type, *a, **k):
        order.append(event_type)
        return real_record(event_id, event_type, *a, **k)
    api._ledger.record_event = spy
    real_place = dialer.place_call
    dialer.place_call = lambda auth, line: (order.append("DIAL"), real_place(auth, line))[1]
    o = _dial("fu-1")
    assert o["attempted"] is True and o["dial_placed"] is True
    assert order == ["callback_dial_requested", "log_anchor", "DIAL", "callback_dial_result", "log_anchor"], order
    ev = _evidence()
    assert ev["counts"] == {"committed": 2, "attempted": 0}, ev
    assert {e["event_type"] for e in ev["events"]} == {"callback_dial_requested", "callback_dial_result"}


def test_h_no_ledger_no_dial(env, monkeypatch):
    dialer, _ = env
    monkeypatch.setattr(api, "_ledger", FakeLedgerClient(fail=True))
    o = _dial("fu-1")
    assert o["attempted"] is False and "fail closed" in o["skip_reason"]
    assert dialer.calls_placed == [] and dialer.numbers_dialed == []


# ============================================================================================ M: /resolve


def test_m_resolve_is_idempotent_and_records_evidence(monkeypatch):
    sor = InMemorySystemOfRecord()
    monkeypatch.setattr(api, "_system_of_record", sor)
    body = {"events": [{"entity_type": "call", "entity_id": "call_9", "customer_id": "c1", "resolution_type": "booked"}]}
    r1 = client.post(RESOLVE, json=body).json()["records"][0]
    r2 = client.post(RESOLVE, json=body).json()["records"][0]
    assert r1 == r2 and r1["write_back_status"] == "success"
    assert len(sor.written) == 1                                   # the retry wrote nothing
    other = {"events": [{**body["events"][0], "resolution_type": "no_resolution"}]}
    r3 = client.post(RESOLVE, json=other).json()["records"][0]
    assert r3["write_back_status"] == "failed" and "already resolved" in r3["write_back_detail"]
    assert len(sor.written) == 1
    ev = _evidence()
    assert ev["counts"] == {"committed": 2, "attempted": 0}, ev
    assert sorted(e["event_type"] for e in ev["events"]) == ["resolution_writeback_requested",
                                                             "resolution_writeback_result"]


def test_m_resolve_without_its_evidence_writes_nothing_back(monkeypatch):
    sor = InMemorySystemOfRecord()
    monkeypatch.setattr(api, "_system_of_record", sor)
    monkeypatch.setattr(api, "_ledger", FakeLedgerClient(fail=True))
    body = {"events": [{"entity_type": "call", "entity_id": "call_9", "customer_id": "c1", "resolution_type": "booked"}]}
    rec = client.post(RESOLVE, json=body).json()["records"][0]
    assert rec["write_back_status"] == "failed" and "fail closed" in rec["write_back_detail"] and sor.written == []


# ============================================================================================ M: restart


def _restart(monkeypatch, d: str) -> None:
    """A new process on the same data directory: fresh in-memory state, the log replayed."""
    monkeypatch.setattr(api, "_GATE", api._build_gate())
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    monkeypatch.setattr(api, "_resolutions", {})
    monkeypatch.setattr(api, "_journal", api._make_journal(RecordLog(d)))
    api._restore_from_log()


def test_m_call_limits_and_write_back_dedupe_survive_a_restart(env, monkeypatch, tmp_path):
    dialer, clock = env
    d = str(tmp_path / "d")
    sor = InMemorySystemOfRecord()
    monkeypatch.setattr(api, "_system_of_record", sor)
    monkeypatch.setattr(api, "_journal", api._make_journal(RecordLog(d)))
    assert _dial("fu-1")["attempted"] is True
    body = {"events": [{"entity_type": "call", "entity_id": "call_9", "customer_id": "c1", "resolution_type": "booked"}]}
    client.post(RESOLVE, json=body)
    _restart(monkeypatch, d)
    clock.t = T + timedelta(minutes=1)
    o = _dial("fu-2")                                             # same number, a minute later, after a restart
    assert o["attempted"] is False and "not contacted" in o["skip_reason"], o
    assert _dial("fu-1")["skip_reason"] == "already attempted by this service — not redialed"
    client.post(RESOLVE, json=body)
    assert len(sor.written) == 1 and len(dialer.calls_placed) == 1


# ============================================================================================ E-5 / F-3


def test_store_one_fsync_error_never_bricks_the_log(env, monkeypatch, tmp_path):
    import store as store_mod
    dialer, clock = env
    d = str(tmp_path / "d")
    monkeypatch.setattr(api, "_journal", api._make_journal(RecordLog(d)))
    real = os.fsync
    hit = {"n": 0}

    def flaky(fd):
        hit["n"] += 1
        if hit["n"] == 1:
            raise OSError(5, "simulated EIO on fsync")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky)
    o = _dial("fu-1")
    assert o["attempted"] is False and dialer.calls_placed == []     # its line failed BEFORE the dial: no dial
    monkeypatch.setattr(store_mod.os, "fsync", real)
    assert _dial("fu-2")["attempted"] is True
    assert api._journal.log.verify() and len(api._journal.log) == 2
    with open(os.path.join(d, LOG_NAME), "rb") as fh:
        assert fh.read().count(b"\n") == 2


def test_store_one_process_per_data_directory(tmp_path):
    d = str(tmp_path / "d")
    first = api._hold_data_dir(d)
    try:
        with pytest.raises(RuntimeError, match="another fulfillment-py process holds this data directory"):
            api._hold_data_dir(d)
    finally:
        first.release()
