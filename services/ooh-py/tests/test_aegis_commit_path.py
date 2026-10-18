"""AEGIS mutation gate at e680f98: the ledger-commit path in ``_commit`` had lines no test pinned.

Each test drives one branch of ``_commit`` and checks what the caller and the log see afterwards:
- R19: a certain "nothing recorded" anchor refusal drops the pending line, so a 503 with maybe=false never applies later.
- C06: a typed-evidence record failure refuses the request (no order without its ledger evidence).
- C02: a line whose seq is not the one the evidence named is never anchored.
- C03: the line this process wrote is the one rolled forward after a lost answer.
- C04: a pending-line write failure whose clean-up also fails answers maybe=true.
- C07: an anchored line that could not be appended stops writes until the integrity check rolls it forward.
- C08: a committed write leaves no pending file behind."""
from __future__ import annotations

import os

from helpers import Harness, rid
from ledger import LedgerNotRecorded
from store import StoreWriteError
from test_aegis_omega_red import _ScriptedLedger


def _h(tmp_path):
    led = _ScriptedLedger()
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), ledger=led)
    h.tenant("acme")
    return led, h


def _recover(h):
    for _ in range(3):
        h.svc._last_integrity_try = 0
        h.svc.verify_integrity(force=True, always=True)
    return h.svc.integrity


def _new_tenant(h, tid):
    return h.post("/tenants", {"request_id": rid(), "tenant_id": tid}, andre=True)


def test_r19_certain_anchor_refusal_never_takes_effect_later(tmp_path):
    led, h = _h(tmp_path)
    anchors = len(led.of_type("log_anchor"))
    led.script = ["unreachable"]
    r = _new_tenant(h, "refused-1")
    assert r.status_code == 503 and r.json() == {"detail": "LEDGER_UNAVAILABLE", "maybe": False}, r.text
    assert not os.path.exists(h.svc.log.pending_path)          # dropped at once, not left for the next check
    assert h.svc.integrity["ok"] is True, h.svc.integrity       # writes do not stop for a certain refusal
    assert _recover(h)["ok"] is True
    assert "refused-1" not in h.svc.tenants, "a request answered 503 maybe=false later took effect"
    assert len(led.of_type("log_anchor")) == anchors
    h.ok(_new_tenant(h, "after-1"), 201)


def test_c06_typed_evidence_failure_refuses_the_authorization(tmp_path):
    led, h = _h(tmp_path)
    ctx = h.funded("acme")
    log_len = len(h.svc.log)
    orig = led.record_event

    def no_evidence(event_id, department, event_type, *a, **k):
        if event_type != "log_anchor":
            raise LedgerNotRecorded("ledger unreachable: ConnectError")
        return orig(event_id, department, event_type, *a, **k)

    led.record_event = no_evidence
    r = h.authorize(ctx)
    assert r.status_code == 503 and r.json()["detail"] == "LEDGER_UNAVAILABLE", r.text
    assert len(h.svc.log) == log_len                             # nothing anchored, nothing appended
    led.record_event = orig
    h.ok(h.authorize(ctx), 201)                                  # control: same request with evidence recorded


def test_c02_a_line_with_another_seq_is_never_anchored(tmp_path, monkeypatch):
    led, h = _h(tmp_path)
    anchors = len(led.of_type("log_anchor"))
    real = h.svc.log.prepare

    def moved(kind, at, data):
        rec, line = real(kind, at, data)
        return {**rec, "seq": rec["seq"] + 1}, line

    monkeypatch.setattr(h.svc.log, "prepare", moved)
    r = _new_tenant(h, "moved-1")
    assert r.status_code == 503 and r.json()["detail"] == "STORE_UNAVAILABLE", r.text
    assert len(led.of_type("log_anchor")) == anchors
    monkeypatch.setattr(h.svc.log, "prepare", real)
    h.ok(_new_tenant(h, "moved-1"), 201)


def test_c03_own_pending_line_is_rolled_forward_after_a_lost_answer(tmp_path):
    led, h = _h(tmp_path)
    led.script = ["lose_unrecorded", "unreachable"]
    r = _new_tenant(h, "maybe-1")
    assert r.status_code == 503 and r.json()["maybe"] is True, r.text
    assert h.svc._own_pending is not None
    assert _recover(h)["ok"] is True, h.svc.integrity
    assert "maybe-1" in h.svc.tenants                            # re-recorded and appended, not set aside
    assert len(led.of_type("log_anchor")) == len(h.svc.log)


def test_c04_pending_write_failure_with_failed_cleanup_answers_maybe(tmp_path, monkeypatch):
    led, h = _h(tmp_path)

    def fail(*a, **k):
        raise StoreWriteError("disk full")

    monkeypatch.setattr(h.svc.log, "write_pending", fail)
    monkeypatch.setattr(h.svc.log, "clear_pending", fail)
    r = _new_tenant(h, "disk-1")
    assert r.status_code == 503 and r.json() == {"detail": "STORE_UNAVAILABLE", "maybe": True}, r.text
    assert h.svc.integrity["ok"] is False


def test_c04_control_pending_write_failure_with_clean_cleanup_answers_not_maybe(tmp_path, monkeypatch):
    led, h = _h(tmp_path)

    def fail(*a, **k):
        raise StoreWriteError("disk full")

    monkeypatch.setattr(h.svc.log, "write_pending", fail)
    r = _new_tenant(h, "disk-2")
    assert r.status_code == 503 and r.json() == {"detail": "STORE_UNAVAILABLE", "maybe": False}, r.text


def test_c07_anchored_but_unwritten_line_stops_writes_until_rolled_forward(tmp_path, monkeypatch):
    led, h = _h(tmp_path)
    real = h.svc.log.append_prepared
    calls = {"n": 0}

    def fail_once(rec, line):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StoreWriteError("fsync failed")
        return real(rec, line)

    monkeypatch.setattr(h.svc.log, "append_prepared", fail_once)
    r = _new_tenant(h, "anchored-1")
    assert r.status_code == 503 and r.json() == {"detail": "STORE_UNAVAILABLE", "maybe": True}, r.text
    assert h.svc.integrity["ok"] is False
    r2 = _new_tenant(h, "blocked-1")
    assert r2.status_code == 503 and r2.json()["detail"] == "INTEGRITY_UNVERIFIED", r2.text
    assert _recover(h)["ok"] is True, h.svc.integrity
    assert "anchored-1" in h.svc.tenants and "blocked-1" not in h.svc.tenants
    assert len(led.of_type("log_anchor")) == len(h.svc.log)
    h.ok(_new_tenant(h, "after-7"), 201)


def test_c08_committed_write_leaves_no_pending_file(tmp_path):
    led, h = _h(tmp_path)
    h.ok(_new_tenant(h, "clean-1"), 201)
    assert not os.path.exists(h.svc.log.pending_path)
    assert h.svc._own_pending is None
