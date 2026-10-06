"""AEGIS round 3 (Oct 5 2026, BLOCKING): regressions for R3-1..R3-4, each the reviewer's scenario."""

from __future__ import annotations

import os

import store as store_mod
import webauthn
from helpers import Harness, rid
from ports import Ports
from store import StoreWriteError


def durable(tmp_path, **kw):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), **kw)
    h.enroll()
    return h


def store(h, name, value="v"):
    return h.post("/sec/v1/secrets", {"request_id": rid(), "name": name, "kind": "api_key", "value": value,
                                      "readers": ["finance_31"], "purposes": ["p"]}, caller="finance_31")


def rotate(h, value):
    return h.post("/sec/v1/secrets/vault:finance_31.k/rotate", {"request_id": rid(), "value": value},
                  caller="finance_31")


def use(h):
    return h.use("finance_31", "vault:finance_31.k", "p")


def test_r3_1a_a_pending_file_that_could_not_be_confirmed_is_a_maybe(tmp_path, monkeypatch):
    """write_pending placed the file, then failed (directory fsync): the caller must keep the sealed file."""
    h = durable(tmp_path)
    h.ok(store(h, "k", "v1"), 201)
    real = store_mod.RecordLog.write_pending

    def placed_then_failed(self, line):
        real(self, line)
        raise StoreWriteError("dir fsync failed")
    monkeypatch.setattr(store_mod.RecordLog, "write_pending", placed_then_failed)
    monkeypatch.setattr(store_mod.RecordLog, "clear_pending",
                        lambda self: (_ for _ in ()).throw(StoreWriteError("unlink failed")))
    assert rotate(h, "v2").status_code == 503
    monkeypatch.undo()
    h.svc._last_integrity_try = 0
    assert h.ok(use(h))["value"] in ("v1", "v2")              # never SEAL_BROKEN, whichever way it settled
    h2 = h.restart()
    assert h2.ok(use(h2))["value"] in ("v1", "v2")


def test_r3_1a_a_cleanly_removed_pending_file_is_a_certain_failure(tmp_path, monkeypatch):
    h = durable(tmp_path)
    h.ok(store(h, "k", "v1"), 201)
    real = store_mod.RecordLog.write_pending

    def placed_then_failed(self, line):
        real(self, line)
        raise StoreWriteError("dir fsync failed")
    monkeypatch.setattr(store_mod.RecordLog, "write_pending", placed_then_failed)
    assert rotate(h, "v2").status_code == 503
    monkeypatch.undo()
    r = h.ok(use(h))
    assert (r["value"], r["version"]) == ("v1", 1)
    assert not os.path.exists(os.path.join(str(tmp_path / "d"), "pending.line"))


def test_r3_1b_a_refused_anchor_whose_pending_file_stays_is_a_maybe(tmp_path, monkeypatch):
    h = durable(tmp_path)
    h.ok(store(h, "k", "v1"), 201)
    h.ledger.fail_types.add("log_anchor")
    monkeypatch.setattr(store_mod.RecordLog, "clear_pending",
                        lambda self: (_ for _ in ()).throw(StoreWriteError("unlink failed")))
    assert rotate(h, "v2").status_code == 503
    monkeypatch.undo()
    h.ledger.fail_types.clear()
    h.svc._last_integrity_try = 0
    assert h.ok(use(h))["value"] in ("v1", "v2")
    assert h.restart().ok(use(h.restart()))["value"] in ("v1", "v2")


def test_r3_1_an_anchored_line_whose_sealed_file_vanished_is_rolled_forward_loudly(tmp_path):
    """Discarding an anchored line would leave the ledger ahead of the log for ever (N1). A sealed file removed
    from outside is tampering or disk loss: the line is rolled forward, a sev1 opened, the old version kept."""
    d = str(tmp_path / "d")
    h = durable(tmp_path)
    h.ok(store(h, "k", "v1"), 201)
    h.svc.log.fail_next_append = True
    assert rotate(h, "v2").status_code == 503
    sid = h.svc.by_ref["vault:finance_31.k"]
    os.unlink(os.path.join(d, "sealed", f"{sid}.v2"))
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True
    assert any(i["code"] == "SEALED_SECRET_TAMPERED" and i["severity"] == "sev1" for i in h2.svc.incidents.values())
    assert f"{sid}.v1" in h2.svc.sealed.names()                # kept for a manual recovery


def test_r3_3_an_fsync_error_after_the_write_never_duplicates_the_line(tmp_path, monkeypatch):
    d = str(tmp_path / "d")
    h = durable(tmp_path)
    real_fsync, calls = os.fsync, {"n": 0}

    def fsync_fails_once_after_write(fd):
        if os.fstat(fd).st_size and os.path.samefile(f"/proc/self/fd/{fd}", os.path.join(d, "security_log.jsonl")) \
                and calls["n"] == 0:
            calls["n"] += 1
            raise OSError(5, "EIO")
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fsync_fails_once_after_write)
    r = store(h, "k2")
    monkeypatch.undo()
    assert r.status_code == 503
    h.svc._last_integrity_try = 0
    assert store(h, "k3").status_code == 201
    assert h.svc.log.verify() is True                          # the file equals memory
    h2 = h.restart()                                           # and the service starts
    assert h2.svc.integrity["ok"] is True


def test_r3_3_a_line_already_on_disk_is_adopted_not_written_twice(tmp_path):
    h = durable(tmp_path)
    rec, line = h.svc.log.prepare("job_ran", "2026-10-06T00:00:00Z", {"job": "x", "actor": "scheduler"})
    with open(h.svc.log.path, "ab") as fh:
        fh.write(line + b"\n")                                 # written before a failed fsync
    h.svc.log.append_prepared(rec, line)
    assert h.svc.log.verify() is True


def test_r3_3_a_file_that_disagrees_with_memory_is_never_written(tmp_path):
    h = durable(tmp_path)
    with open(h.svc.log.path, "ab") as fh:
        fh.write(b"junk\n")
    rec, line = h.svc.log.prepare("job_ran", "2026-10-06T00:00:00Z", {"job": "x", "actor": "scheduler"})
    try:
        h.svc.log.append_prepared(rec, line)
        raise AssertionError("appended to a file that disagrees with memory")
    except StoreWriteError:
        pass


def test_r3_2_freeze_challenges_are_bound_single_use_and_expire(hk):
    body = {"request_id": rid(), "target_kind": "caller", "target_id": "legal_37", "reason_code": "TEST"}
    ch = hk.challenge("FREEZE", "caller:legal_37", body)
    assert ch["challenge_id"].startswith("em-")
    other = {**body, "target_id": "finance_31"}
    r = hk.post("/sec/v1/freezes", {**other, "approval": hk.keys[0].assert_(ch)})
    assert r.json()["detail"] == "APPROVAL_ACTION_MISMATCH"
    a = hk.keys[0].assert_(ch)
    hk.ok(hk.post("/sec/v1/freezes", {**body, "approval": a}), 201)
    r = hk.post("/sec/v1/freezes", {**body, "request_id": rid(), "approval": hk.keys[0].assert_(ch)})
    assert r.json()["detail"] in ("APPROVAL_CHALLENGE_USED", "APPROVAL_ACTION_MISMATCH")
    # a freeze challenge cannot approve anything else, and a forged one is refused
    sbody = {"request_id": rid(), "owner": "finance_31", "name": "x", "kind": "api_key", "value": "v"}
    r = hk.post("/sec/v1/secrets/andre", {**sbody, "approval": hk.keys[0].assert_(ch)})
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_UNKNOWN"
    forged = {"challenge_id": "em-" + webauthn.b64url_encode(os.urandom(56)), "challenge":
              webauthn.b64url_encode(os.urandom(56))}
    r = hk.post("/sec/v1/freezes", {**body, "request_id": rid(), "approval": hk.keys[0].assert_(forged)})
    assert r.json()["detail"] == "APPROVAL_ACTION_MISMATCH"


def test_r3_2_an_expired_freeze_challenge_is_refused(hk, monkeypatch):
    import time as time_mod
    body = {"request_id": rid(), "target_kind": "caller", "target_id": "legal_37", "reason_code": "TEST"}
    ch = hk.challenge("FREEZE", "caller:legal_37", body)
    later = time_mod.time() + 301
    monkeypatch.setattr(time_mod, "time", lambda: later)
    r = hk.post("/sec/v1/freezes", {**body, "approval": hk.keys[0].assert_(ch)})
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_EXPIRED"


def test_r3_4_a_failed_integrity_alert_is_sent_again(tmp_path):
    class Flaky:
        def __init__(self, name):
            self.name, self.ok, self.sent = name, False, 0

        def send(self, msg):
            self.sent += 1
            return "delivered" if self.ok else "failed"
    ports = Ports.default()
    ports.channels = {c: Flaky(c) for c in ("sms", "email", "push")}
    h = durable(tmp_path, ports=ports)
    victim = next(e for e in h.ledger.events if e["event_type"] == "log_anchor")
    victim["payload_sha256"] = "0" * 64
    h.ok(h.post("/sec/v1/jobs/integrity/run", {"request_id": rid()}, caller="scheduler"))
    for ch in ports.channels.values():
        ch.ok = True
    h.ok(h.post("/sec/v1/jobs/integrity/run", {"request_id": rid()}, caller="scheduler"))
    h.ok(h.post("/sec/v1/jobs/integrity/run", {"request_id": rid()}, caller="scheduler"))
    assert ports.channels["sms"].sent == 2                     # failed once, delivered once, then quiet


def test_r3_2_a_freeze_challenge_approves_once_even_without_the_request_record(hk):
    """Defence in depth: the route's request record normally answers a replay; the challenge itself is single use."""
    from errors import ApprovalRefused
    body = {"request_id": rid(), "target_kind": "caller", "target_id": "legal_37", "reason_code": "TEST"}
    approved = hk.approved("FREEZE", "caller:legal_37", body)
    with hk.svc.lock:
        hk.svc._approve("FREEZE", "caller:legal_37", approved)
        approved2 = {**approved, "approval": {**approved["approval"]}}
        hk.keys[0].count += 1
        try:
            hk.svc._approve("FREEZE", "caller:legal_37", approved2)
            raise AssertionError("a freeze challenge approved twice")
        except ApprovalRefused as exc:
            assert exc.reason == "APPROVAL_CHALLENGE_USED"
