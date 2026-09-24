"""
Fix wave 1 — in-memory state is bounded (audit: unbounded growth).

Before: OutboundContactGate._attempts kept one entry per phone number and
per customer ever contacted, and only dropped expired keys every 256th
contact; api._attempted_task_ids and api._exhausted_resolutions were plain
set/dict that grew forever. A caller submitting fresh task ids / numbers
could grow process memory without limit.

After: every one of these structures (a) evicts entries older than the 24h
window on the clock the service already uses, and (b) has a hard cap. At
the cap, with nothing old enough to evict, the service FAILS CLOSED: it
refuses the new contact / new write-back rather than forget history it
needs to enforce limits and idempotency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api
from contact_window import ContactWindow
from fulfillment_schema import TaskChannel
from integrations.sip_dialer import InMemorySipDialer
from outbound_gate import AttemptLimits, ContactRefused, OutboundContactGate
from conftest import TEST_SERVICE_TOKEN

client = TestClient(
    api.app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False
)

NY = "America/New_York"
T0 = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)  # 14:00 New York, 11:00 Los Angeles


def ny_phone(i: int) -> str:
    return f"+1212555{i:04d}"


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _gate(clock, **kw) -> OutboundContactGate:
    return OutboundContactGate(window=ContactWindow.default(), limits=AttemptLimits(), clock=clock, **kw)


def _contact(g, phone, customer_id=None):
    d = g.authorize(channel=TaskChannel.CALL, phone=phone, claimed_tz=NY, customer_id=customer_id)
    if not d.allowed:
        return d.reason
    try:
        d.authorization.redeem(TaskChannel.CALL)
    except ContactRefused as exc:
        return str(exc)
    return None


# --- gate attempt history ----------------------------------------------------

def test_gate_evicts_history_older_than_24h():
    clock = Clock(T0)
    g = _gate(clock)
    for i in range(10):
        assert _contact(g, ny_phone(i), customer_id=f"c{i}") is None
    assert g.tracked_keys == 20  # one per number, one per customer
    clock.t = T0 + timedelta(hours=24, minutes=1)
    assert _contact(g, ny_phone(100)) is None
    assert g.tracked_keys == 1  # only the contact just made


def test_gate_does_not_evict_history_still_inside_the_window():
    clock = Clock(T0)
    g = _gate(clock)
    assert _contact(g, ny_phone(1)) is None
    clock.t = T0 + timedelta(hours=23, minutes=59)
    assert _contact(g, ny_phone(2)) is None
    assert g.tracked_keys == 2
    # and the limit on the first number still holds (spacing passed, count kept)
    assert _contact(g, ny_phone(1)) is None


def test_gate_at_hard_cap_fails_closed_and_contacts_nobody_new():
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=5)
    for i in range(5):
        assert _contact(g, ny_phone(i)) is None
    reason = _contact(g, ny_phone(99))
    assert reason is not None and "attempt history is full" in reason
    assert g.tracked_keys == 5
    # a number already tracked is still decided by its own limits
    clock.t += timedelta(hours=2)
    assert _contact(g, ny_phone(0)) is None


def test_gate_cap_counts_customer_keys_too():
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=3)
    assert _contact(g, ny_phone(0), customer_id="c0") is None  # 2 keys
    assert "attempt history is full" in _contact(g, ny_phone(1), customer_id="c1")  # would be 4


def test_gate_at_cap_recovers_once_old_entries_expire():
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=5)
    for i in range(5):
        assert _contact(g, ny_phone(i)) is None
    assert "attempt history is full" in _contact(g, ny_phone(99))
    clock.t = T0 + timedelta(hours=24, seconds=1)
    assert _contact(g, ny_phone(99)) is None
    assert g.tracked_keys == 1


def test_gate_cap_is_rechecked_at_redeem():
    """Two authorizations for new numbers minted while one slot is free:
    only one may record (and contact); the other is refused at redeem."""
    clock = Clock(T0)
    g = _gate(clock, max_tracked_keys=1)
    a = g.authorize(channel=TaskChannel.CALL, phone=ny_phone(1), claimed_tz=NY, customer_id=None)
    b = g.authorize(channel=TaskChannel.CALL, phone=ny_phone(2), claimed_tz=NY, customer_id=None)
    assert a.allowed and b.allowed
    assert a.authorization.redeem(TaskChannel.CALL) == ny_phone(1)
    with pytest.raises(ContactRefused, match="attempt history is full"):
        b.authorization.redeem(TaskChannel.CALL)


@pytest.mark.parametrize("bad", [0, -1, True, 10**9])
def test_gate_cap_must_be_sane(bad):
    with pytest.raises(ValueError):
        _gate(Clock(T0), max_tracked_keys=bad)


# --- API dial dedupe (task ids already handed to the dialer) -----------------

def _task_json(i: int) -> dict:
    return {
        "task_id": f"fu-call_{i}", "purpose": "missed_call_callback", "channel": "call",
        "customer_id": f"c{i}", "source_call_id": f"call_{i}",
        "created_at": (T0 - timedelta(minutes=1)).isoformat(), "due_at": T0.isoformat(), "reason": "r",
    }


def _run(tasks: list[dict]) -> dict:
    body = {
        "tasks": tasks,
        "phone_by_call_id": {t["source_call_id"]: ny_phone(int(t["source_call_id"].split("_")[1])) for t in tasks},
        "timezone_by_call_id": {t["source_call_id"]: NY for t in tasks},
    }
    r = client.post("/agents/callback-orchestration/run", json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def api_clock(monkeypatch):
    clock = Clock(T0)
    monkeypatch.setattr(api, "_now", clock)
    monkeypatch.setattr(api, "_GATE", api._build_gate())
    dialer = InMemorySipDialer()
    monkeypatch.setattr(api, "_dialer", dialer)
    return clock, dialer


def test_dial_dedupe_evicts_task_ids_older_than_24h(monkeypatch, api_clock):
    clock, _ = api_clock
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe())
    for i in range(5):
        assert _run([_task_json(i)])["outcomes"][0]["attempted"] is True
    assert len(api._attempted_task_ids) == 5
    clock.t = T0 + timedelta(hours=24, minutes=1)
    _run([_task_json(50)])
    assert len(api._attempted_task_ids) == 1


def test_dial_dedupe_at_cap_fails_closed(monkeypatch, api_clock):
    clock, dialer = api_clock
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe(max_entries=3))
    out = _run([_task_json(i) for i in range(5)])["outcomes"]
    assert [o["attempted"] for o in out] == [True, True, True, False, False]
    assert all("dedupe state is full" in o["skip_reason"] for o in out[3:])
    assert len(dialer.calls_placed) == 3
    assert len(api._attempted_task_ids) == 3
    # a repeat of an already-attempted id is still recognised at the cap
    again = _run([_task_json(0)])["outcomes"][0]
    assert again["attempted"] is False and "already attempted" in again["skip_reason"]


def test_dial_dedupe_at_cap_recovers_after_24h(monkeypatch, api_clock):
    clock, dialer = api_clock
    monkeypatch.setattr(api, "_attempted_task_ids", api._new_dedupe(max_entries=1))
    assert _run([_task_json(0)])["outcomes"][0]["attempted"] is True
    assert _run([_task_json(1)])["outcomes"][0]["attempted"] is False
    clock.t = T0 + timedelta(hours=24, seconds=1)
    assert _run([_task_json(1)])["outcomes"][0]["attempted"] is True


# --- API exhausted-escalation dedupe ------------------------------------------

def _exhausted_task(i: int) -> dict:
    return {
        "task_id": f"fu-x{i}-esc3", "purpose": "escalation", "channel": "human_handoff", "customer_id": "c",
        "due_at": T0.isoformat(), "attempt_number": 4, "status": "failed", "reason": "r",
    }


def test_exhausted_resolution_dedupe_evicts_after_24h_and_fails_closed_at_cap(monkeypatch, api_clock):
    clock, _ = api_clock
    monkeypatch.setattr(api, "_exhausted_resolutions", api._new_dedupe(max_entries=2))
    r0 = client.post("/agents/followup-sequencing/escalate", json={"task": _exhausted_task(0)})
    r1 = client.post("/agents/followup-sequencing/escalate", json={"task": _exhausted_task(1)})
    assert r0.status_code == r1.status_code == 200
    assert r0.json()["sequence_exhausted"] is True
    # retry of a known task returns the same record even at the cap
    again = client.post("/agents/followup-sequencing/escalate", json={"task": _exhausted_task(0)})
    assert again.json()["resolution"] == r0.json()["resolution"]
    # a new one at the cap is refused before any write-back happens
    full = client.post("/agents/followup-sequencing/escalate", json={"task": _exhausted_task(2)})
    assert full.status_code == 503
    assert "full" in full.json()["detail"]
    assert len(api._exhausted_resolutions) == 2
    clock.t = T0 + timedelta(hours=24, seconds=1)
    ok = client.post("/agents/followup-sequencing/escalate", json={"task": _exhausted_task(2)})
    assert ok.status_code == 200
    assert len(api._exhausted_resolutions) == 1


def test_default_caps_are_finite():
    assert api._attempted_task_ids.max_entries <= 1_000_000
    assert api._exhausted_resolutions.max_entries <= 1_000_000
    assert api._GATE.max_tracked_keys <= 1_000_000


# --- dossier store (same class: api._dossiers grew without limit) -------------

def _dossier_body(customers: list[str], calls_per_customer: int = 1) -> dict:
    return {
        "call_events": [
            {"call_id": f"{c}-call{j}", "customer_id": c, "phone_number": "+12125550101", "direction": "inbound",
             "status": "missed", "started_at": T0.isoformat(), "line_id": "l"}
            for c in customers for j in range(calls_per_customer)
        ]
    }


def test_dossier_store_at_cap_fails_closed_and_keeps_existing_state(monkeypatch):
    monkeypatch.setattr(api, "_dossiers", {})
    monkeypatch.setattr(api, "_MAX_DOSSIERS", 2)
    assert client.post("/agents/customer-dossier/update", json=_dossier_body(["a", "b"])).status_code == 200
    r = client.post("/agents/customer-dossier/update", json=_dossier_body(["c"]))
    assert r.status_code == 503 and "full" in r.json()["detail"]
    assert set(api._dossiers) == {"a", "b"}  # nothing half-applied
    # existing customers can still be updated at the cap
    assert client.post("/agents/customer-dossier/update", json=_dossier_body(["a"], 2)).status_code == 200


def test_dossier_history_lists_are_capped_fail_closed(monkeypatch):
    monkeypatch.setattr(api, "_dossiers", {})
    monkeypatch.setattr(api, "_MAX_DOSSIER_HISTORY", 3)
    assert client.post("/agents/customer-dossier/update", json=_dossier_body(["a"], 3)).status_code == 200
    r = client.post("/agents/customer-dossier/update", json=_dossier_body(["a"], 4))
    assert r.status_code == 503
    assert len(api._dossiers["a"].call_history) == 3


def test_dossier_default_caps_are_finite():
    assert api._MAX_DOSSIERS <= 1_000_000 and api._MAX_DOSSIER_HISTORY <= 100_000
