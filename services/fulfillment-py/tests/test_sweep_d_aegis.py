"""AEGIS re-review of bug sweep D (37a2830), fulfillment-py: F-1 (a write-back whose result line was owed at a restart
was written to the client's system again) and F-2 (the outside write-back ran under the evidence lock). Ported from
the reviewer's probes (aegis-ocfd/probe); each FAILS on 37a2830."""

from __future__ import annotations

import threading

import pytest

import api
from founder import FounderGate
from integrations.system_of_record import InMemorySystemOfRecord, WriteBackResult
from ledger import FakeLedgerClient, LedgerRecordError
from store import RecordLog
from test_sweep_d import RESOLVE, _restart, client

ANDRE = "test-andre-approval-token-fulfillment-do-not-use"
BODY = {"events": [{"entity_type": "call", "entity_id": "call_9", "customer_id": "c1", "resolution_type": "booked"}]}


@pytest.fixture(autouse=True)
def _andre(monkeypatch):
    monkeypatch.setattr(api, "_FOUNDER", FounderGate.build(ANDRE, api._REQUIRED_TOKEN))


def _reconcile(outcome: str, token: str | None = ANDRE):
    return client.post("/agents/resolution-writeback/reconcile",
                       json={"entity_type": "call", "entity_id": "call_9", "outcome": outcome},
                       headers={"X-Andre-Approval-Token": token} if token is not None else {})


class AnchorFail(FakeLedgerClient):
    arm = 0

    def record_event(self, event_id, event_type, *a, **k):
        if event_type == "log_anchor" and self.arm:
            self.arm -= 1
            raise LedgerRecordError("down")
        return super().record_event(event_id, event_type, *a, **k)


def test_f1_owed_result_line_then_restart_is_never_written_again_until_reconciled(monkeypatch, tmp_path):
    d = str(tmp_path / "d")
    sor = InMemorySystemOfRecord()
    led = AnchorFail()
    monkeypatch.setattr(api, "_ledger", led)
    monkeypatch.setattr(api, "_system_of_record", sor)
    monkeypatch.setattr(api, "_journal", api._make_journal(RecordLog(d)))
    real_commit = api._journal.commit

    def commit(kind, *a, **k):
        if kind == "resolution":
            led.arm = 5
        return real_commit(kind, *a, **k)
    monkeypatch.setattr(api._journal, "commit", commit)
    assert client.post(RESOLVE, json=BODY).json()["records"][0]["write_back_status"] == "success"
    assert len(api._journal.owed) == 1 and len(sor.written) == 1
    led.arm = 0
    _restart(monkeypatch, d)
    r2 = client.post(RESOLVE, json=BODY).json()["records"][0]
    assert r2["write_back_status"] == "failed" and "reconciled" in r2["write_back_detail"], r2
    assert len(sor.written) == 1                                            # never written twice
    # the service token alone (any department) is refused, and so is a wrong Andre token: nothing recorded
    for tok in (None, "wrong-token", api._REQUIRED_TOKEN):
        assert _reconcile("written", tok).status_code == 403
    assert len(sor.written) == 1 and "call|call_9" in api._unresolved
    rc = _reconcile("written")
    assert rc.status_code == 200, rc.text
    r3 = client.post(RESOLVE, json=BODY).json()["records"][0]
    assert r3["write_back_status"] == "success" and len(sor.written) == 1
    _restart(monkeypatch, d)                                                # the reconcile survives a restart
    assert client.post(RESOLVE, json=BODY).json()["records"][0]["write_back_status"] == "success"
    assert len(sor.written) == 1


def test_f1_reconciled_not_written_lets_the_next_resolve_write(monkeypatch, tmp_path):
    d = str(tmp_path / "d")
    class Breaks(InMemorySystemOfRecord):
        broken = True

        def write_back(self, record) -> WriteBackResult:
            if self.broken:
                self.broken = False
                raise ConnectionError("CRM went away mid-write")
            return super().write_back(record)
    sor = Breaks()
    monkeypatch.setattr(api, "_system_of_record", sor)
    monkeypatch.setattr(api, "_journal", api._make_journal(RecordLog(d)))
    try:
        client.post(RESOLVE, json=BODY)
    except ConnectionError:
        pass
    _restart(monkeypatch, d)
    assert client.post(RESOLVE, json=BODY).json()["records"][0]["write_back_status"] == "failed"
    assert _reconcile("not_written", "nope").status_code == 403
    assert _reconcile("not_written").status_code == 200
    assert client.post(RESOLVE, json=BODY).json()["records"][0]["write_back_status"] == "success"
    assert len(sor.written) == 1


def test_f2_the_outside_write_back_runs_with_the_evidence_lock_free(monkeypatch):
    gate, inside = threading.Event(), threading.Event()

    class Slow(InMemorySystemOfRecord):
        def write_back(self, record) -> WriteBackResult:
            inside.set()
            gate.wait(10)
            return super().write_back(record)
    sor = Slow()
    monkeypatch.setattr(api, "_system_of_record", sor)
    out: list = []
    t = threading.Thread(target=lambda: out.append(client.post(RESOLVE, json=BODY).json()))
    t.start()
    assert inside.wait(10)
    got = api._evidence_lock.acquire(timeout=2)
    if got:
        api._evidence_lock.release()
    same = client.post(RESOLVE, json=BODY).json()["records"][0]          # concurrent request: never a second write
    gate.set()
    t.join(10)
    assert got and "in progress" in same["write_back_detail"]
    assert out[0]["records"][0]["write_back_status"] == "success" and len(sor.written) == 1


def test_reconcile_refused_when_andre_token_is_unset_or_equals_the_service_token(monkeypatch):
    for gate in (FounderGate.build(None, api._REQUIRED_TOKEN), FounderGate.build(api._REQUIRED_TOKEN, api._REQUIRED_TOKEN)):
        monkeypatch.setattr(api, "_FOUNDER", gate)
        r = _reconcile("written", api._REQUIRED_TOKEN)
        assert r.status_code == 403 and "not configured" in r.json()["detail"]
