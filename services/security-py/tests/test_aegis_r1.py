"""AEGIS round 1 (Oct 5 2026, BLOCKING): one regression per finding, each the reviewer's own scenario."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

import config
from helpers import ENROLL_TOKEN, FakeLedger, Harness, base_env, rid, secret_file
from ledger import LedgerRecordError

REF = "vault:finance_31.stripe_secret"


class LossyLedger(FakeLedger):
    """Records the event, then loses the answer (a read timeout: the ledger DID record it)."""

    lose: int = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
        if self.lose and event_type == "log_anchor":
            self.lose -= 1
            raise LedgerRecordError("ledger response lost")


def store(h, name, caller="finance_31", **kw):
    return h.post("/sec/v1/secrets", {"request_id": rid(), "name": name, "kind": "api_key", "value": "v", **kw},
                  caller=caller)


def test_h1_a_lost_answer_is_retried_and_the_write_completes(tmp_path):
    led = LossyLedger()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.enroll()
    led.lose = 1
    assert store(h, "k2").status_code == 201           # the retry of the same anchor confirms it
    assert store(h, "k3").status_code == 201
    h2 = h.restart()
    assert h2.ok(h2.get("/sec/v1/status"))["integrity"]["ok"] is True
    assert {"vault:finance_31.k2", "vault:finance_31.k3"} <= set(h2.svc.by_ref)


def test_h1_two_lost_answers_settle_from_the_pending_line(tmp_path):
    led = LossyLedger()
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d, ledger=led)
    h.enroll()
    led.lose = 2
    r = store(h, "k2")
    assert r.status_code == 503 and os.path.exists(os.path.join(d, "pending.line"))
    h.svc._last_integrity_try = 0
    assert store(h, "k3").status_code == 201           # the gate settled the pending line, then this one went in
    assert "vault:finance_31.k2" in h.svc.by_ref        # the anchored write was completed, not lost
    h2 = h.restart()
    assert h2.ok(h2.get("/sec/v1/status"))["integrity"]["ok"] is True


def test_h1_in_memory_too(tmp_path):
    led = LossyLedger()
    h = Harness(tmp_path, ledger=led)
    h.enroll()
    led.lose = 2
    assert store(h, "k2").status_code == 503
    h.svc._last_integrity_try = 0
    assert store(h, "k3").status_code == 201
    assert h.ok(h.get("/sec/v1/status"))["integrity"]["ok"] is True


def test_m1_a_retried_clean_exit_finishes_every_secret(h):
    for n in ("a", "b", "c"):
        h.ok(store(h, n, caller="onboarding", client_id="cl1"), 201)
    R = rid()
    orig, calls = h.svc._destroy, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        h.ledger.fail = calls["n"] == 2
        try:
            return orig(*a, **k)
        finally:
            h.ledger.fail = False
    h.svc._destroy = flaky
    assert h.post("/sec/v1/clients/cl1/destroy", {"request_id": R}, caller="onboarding").status_code == 503
    h.svc._destroy = orig
    r = h.ok(h.post("/sec/v1/clients/cl1/destroy", {"request_id": R}, caller="onboarding"))
    assert r["destroyed"] == 3
    assert not [s for s in h.svc.secrets.values() if s["owner"] == "onboarding" and s["status"] == "active"]


def test_m2_a_department_looking_at_or_destroying_a_canary_is_caught(hk):
    hk.andre_store("finance_31", "stripe_live_backup", kind="canary", value=None)
    ref = "vault:finance_31.stripe_live_backup"
    assert hk.get(f"/sec/v1/secrets/{ref}", caller="finance_31").status_code == 404
    assert hk.svc._frozen("caller", "finance_31")
    assert any(i["code"] == "CANARY_TOUCHED" for i in hk.svc.incidents.values())
    hk.andre_store("legal_37", "c2", kind="canary", value=None)
    r = hk.post("/sec/v1/secrets/vault:legal_37.c2/destroy", {"request_id": rid()}, caller="legal_37")
    assert r.status_code == 404 and hk.svc._frozen("caller", "legal_37")
    assert hk.svc.secrets[hk.svc.by_ref["vault:legal_37.c2"]]["status"] == "active"
    hk.andre_store("onboarding", "c3", kind="canary", value=None)
    r = hk.post("/sec/v1/secrets/vault:onboarding.c3/rotate", {"request_id": rid(), "value": "x"},
                caller="onboarding")
    assert r.status_code == 404 and hk.svc._frozen("caller", "onboarding")


def test_m3_a_frozen_caller_can_do_nothing_anywhere(hk):
    hk.ok(hk.post("/sec/v1/holds", {"request_id": rid(), "hold_id": "H1", "systems": ["drive"]},
                  caller="legal_37"), 201)
    body = {"request_id": rid(), "target_kind": "all", "target_id": "all", "reason_code": "TEST"}
    hk.ok(hk.post("/sec/v1/freezes", hk.approved("FREEZE", "all:all", body)), 201)
    assert hk.post("/sec/v1/holds/H1/release", {"request_id": rid()}, caller="legal_37").json()["detail"] == "LOCKDOWN"
    assert hk.post("/sec/v1/holds", {"request_id": rid(), "hold_id": "H2", "systems": ["drive"]},
                   caller="legal_37").status_code == 403
    assert hk.post("/sec/v1/jobs/rotation-due/run", {"request_id": rid()}, caller="scheduler").status_code == 403
    scan = {"request_id": rid(), "source": "python:x", "tool": "pip-audit@1",
            "scanned_at": datetime.now(timezone.utc).isoformat(), "findings": []}
    assert hk.post("/sec/v1/scans", scan, caller="scheduler").status_code == 403
    assert hk.get(f"/sec/v1/secrets/{REF}", caller="finance_31").status_code == 403
    # the integrity check and the dashboard still work during a lockdown
    assert hk.post("/sec/v1/jobs/integrity/run", {"request_id": rid()}, caller="scheduler").status_code == 200
    assert hk.post("/sec/v1/scans", {**scan, "request_id": rid()}).status_code == 201


def test_m4_a_challenge_flood_cannot_stop_the_freeze_switch(hk):
    for i in range(64):
        hk.challenge("SECRET_STORE", f"vault:finance_31.k{i}", {"request_id": rid(), "owner": "finance_31",
                                                                "name": f"k{i}", "kind": "api_key", "value": "v"})
    r = hk.post("/sec/v1/approvals/challenges", {"action": "SECRET_ROTATE", "target": REF,
                                                 "body": {"request_id": rid(), "value": "x"}})
    assert r.status_code == 429 and r.json()["detail"] == "APPROVAL_CHALLENGES_EXHAUSTED"
    assert any(i["code"] == "APPROVAL_CHALLENGES_EXHAUSTED" for i in hk.svc.incidents.values())
    body = {"request_id": rid(), "target_kind": "all", "target_id": "all", "reason_code": "TEST"}
    hk.ok(hk.post("/sec/v1/freezes", hk.approved("FREEZE", "all:all", body)), 201)


def test_m5_a_chosen_enroll_token_refuses_start_and_its_hash_is_not_exported(tmp_path, hk):
    env = base_env(tmp_path)
    for weak in ("a" * 50, "password-password-password-password-password1"):
        f = secret_file(tmp_path, "weak.token", weak.encode())
        with pytest.raises(RuntimeError, match="GENERATED"):
            config.load({**env, "SEC_ANDRE_ENROLL_TOKEN_FILE": f})
    blob = json.dumps(hk.ok(hk.get("/sec/v1/audit/events")))
    import hashlib
    assert hashlib.sha256(ENROLL_TOKEN.encode()).hexdigest() not in blob and "token_sha256" not in blob


def test_m5_a_spent_token_never_enrols_again(tmp_path):
    """Mutation T1: the 'consumed' guards had no test that presents a spent token."""
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    a = h.enroll()
    b = h.enroll()
    # revoke both is impossible (last passkey); revoke one, suspend the other by a counter regression
    h.ok(h.post(f"/sec/v1/passkeys/{a.credential_id}/revoke",
                h.approved("PASSKEY_REVOKE", a.credential_id, {"request_id": rid()}, key=b)))
    ch = h.challenge("FREEZE", "caller:legal_37", {"request_id": "x1", "target_kind": "caller",
                                                   "target_id": "legal_37", "reason_code": "TEST"})
    h.post("/sec/v1/freezes", {"request_id": "x1", "target_kind": "caller", "target_id": "legal_37",
                               "reason_code": "TEST", "approval": b.assert_(ch, count=0)})
    assert not h.svc._active_passkeys()               # no active passkey left
    r = h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN})
    assert r.status_code == 403 and r.json()["detail"] == "ENROLL_TOKEN_REFUSED"
    h2 = h.restart()
    assert h2.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}).status_code == 403


def test_m6_rotation_under_a_hold_is_refused_and_the_version_kept(hk):
    hk.ok(store(hk, "a", caller="onboarding", client_id="c1"), 201)
    hk.ok(hk.post("/sec/v1/holds", {"request_id": rid(), "hold_id": "h1", "systems": ["drive"],
                                    "subject_refs": ["client:c1"]}, caller="legal_37"), 201)
    r = hk.post("/sec/v1/secrets/vault:onboarding.a/rotate", {"request_id": rid(), "value": "x"}, caller="onboarding")
    assert r.status_code == 409 and r.json()["detail"] == "PRESERVATION_HOLD"
    sid = hk.svc.by_ref["vault:onboarding.a"]
    assert hk.svc.sealed.get(sid, 1) is not None


def test_l1_a_request_id_is_scoped_to_its_target(hk):
    hk.ok(store(hk, "a", caller="onboarding"), 201)
    hk.ok(store(hk, "b", caller="onboarding"), 201)
    R = rid()
    hk.ok(hk.post("/sec/v1/secrets/vault:onboarding.a/destroy", {"request_id": R}, caller="onboarding"))
    r = hk.ok(hk.post("/sec/v1/secrets/vault:onboarding.b/destroy", {"request_id": R}, caller="onboarding"))
    assert r["ref"] == "vault:onboarding.b" and r["status"] == "destroyed"


def test_l2_the_same_request_id_on_another_action_is_not_a_500(hk):
    hk.ok(store(hk, "a", caller="onboarding"), 201)
    R = rid()
    body = {"request_id": R}
    hk.ok(hk.post("/sec/v1/secrets/vault:onboarding.a/destroy", hk.approved("SECRET_DESTROY", "vault:onboarding.a",
                                                                             body)))
    cid = hk.keys[0].credential_id
    r = hk.post(f"/sec/v1/passkeys/{cid}/revoke", hk.approved("PASSKEY_REVOKE", cid, {"request_id": R}))
    assert r.status_code == 409 and r.json()["detail"] == "LAST_PASSKEY"


def test_l3_an_action_mismatch_burns_the_challenge(hk):
    body = {"request_id": rid(), "owner": "finance_31", "name": "k", "kind": "api_key", "value": "v"}
    ch = hk.challenge("SECRET_STORE", "vault:finance_31.k", body)
    a = hk.keys[0].assert_(ch)
    assert hk.post("/sec/v1/secrets/andre", {**body, "value": "other", "approval": a}).json()["detail"] == \
        "APPROVAL_ACTION_MISMATCH"
    r = hk.post("/sec/v1/secrets/andre", {**body, "approval": a})
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_USED"


def test_l4_alerts_are_never_sent_while_the_lock_is_held(tmp_path):
    from ports import Ports
    held, ref = [], []

    class Probe:
        def __init__(self, name):
            self.name = name

        def send(self, msg):
            held.append(ref[0].lock._is_owned())
            return "delivered"
    ports = Ports.default()
    ports.channels = {c: Probe(c) for c in ("sms", "email", "push")}
    h = Harness(tmp_path, ports=ports)
    ref.append(h.svc)
    h.enroll()
    h.andre_store("finance_31", "c", kind="canary", value=None)
    h.use("finance_31", "vault:finance_31.c", "p")                     # detection inside the lock
    h.ok(h.post("/sec/v1/incidents", {"request_id": rid(), "severity": "sev1", "code": "MANUAL_TEST",
                                      "subject": "x"}), 201)
    assert len(held) >= 6 and not any(held)


def test_l5_a_large_pending_line_is_settled_at_start(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    findings = [{"advisory_id": f"GHSA-{i:04d}-" + "a" * 60, "package": f"pkg-{i}-" + "x" * 100,
                 "installed_version": "1.0.0", "fixed_versions": [f"{j}." + "9" * 55 for j in range(20)],
                 "severity": "low"} for i in range(700)]
    h.svc.log.fail_next_append = True
    r = h.post("/sec/v1/scans", {"request_id": rid(), "source": "python:big", "tool": "pip-audit@2",
                                 "scanned_at": datetime.now(timezone.utc).isoformat(), "findings": findings},
               caller="scheduler")
    assert r.status_code == 503 and os.path.getsize(os.path.join(d, "pending.line")) > 1024 * 1024
    h2 = h.restart()
    assert h2.ok(h2.get("/sec/v1/status"))["integrity"]["ok"] is True
    assert len(h2.svc.findings) == 700


def test_l9_the_counter_is_kept_even_when_the_route_fails(hk):
    a = hk.keys[0]
    hk.ok(hk.post("/sec/v1/secrets/andre", hk.approved("SECRET_STORE", "vault:finance_31.k",
          {"request_id": rid(), "owner": "finance_31", "name": "k", "kind": "api_key", "value": "v"})), 201)
    body = {"request_id": rid(), "owner": "finance_31", "name": "k", "kind": "api_key", "value": "v"}
    r = hk.post("/sec/v1/secrets/andre", hk.approved("SECRET_STORE", "vault:finance_31.k", body))
    assert r.json()["detail"] == "SECRET_EXISTS"
    assert hk.svc.passkeys[a.credential_id]["sign_count"] == a.count
