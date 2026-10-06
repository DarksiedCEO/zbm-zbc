"""AEGIS round 4 (Oct 5 2026, BLOCKING): regressions for R4-1..R4-3, each the reviewer's scenario."""

from __future__ import annotations

import json
import os

import pytest

import store as store_mod
import webauthn
from helpers import Authenticator, FakeLedger, Harness, b64u, rid
from ledger import LedgerRecordError


def forge_pending_enrolment(h, d):
    """An attacker with write access to SEC_DATA_DIR only: a valid, chained passkey_enrolled line for their key."""
    evil = Authenticator()
    raw_ch = os.urandom(32)
    reg = evil.register({"challenge_id": "x", "challenge": b64u(raw_ch)})
    cred = webauthn.verify_registration(reg["attestation_object"], reg["client_data_json"], raw_ch, h.svc.rp)
    c = {"credential_id": cred.credential_id, "alg": cred.alg, "public_key_spki": cred.public_key_spki,
         "sign_count": cred.sign_count, "aaguid": cred.aaguid, "backup_eligible": cred.backup_eligible}
    lines = [ln for ln in open(os.path.join(d, "security_log.jsonl"), "rb").read().split(b"\n") if ln]
    rl = store_mod.RecordLog(None)
    rl._lines = lines
    _, line = rl.prepare("passkey_enrolled", json.loads(lines[-1])["at"],
                         {"credential": c, "label": "andre backup", "via": "approval", "token_sha256": None,
                          "actor": "andre"})
    with open(os.path.join(d, "pending.line"), "wb") as fh:
        fh.write(line)
    return evil


def test_r4_1_a_forged_pending_line_is_never_anchored_or_applied(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    andre = h.enroll()
    evil = forge_pending_enrolment(h, d)
    anchors = len(h.ledger.of_type("log_anchor"))
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True
    assert evil.credential_id not in h2.svc.passkeys
    assert len(h2.ledger.of_type("log_anchor")) == anchors          # the service never vouched for it
    body = {"request_id": rid(), "target_kind": "all", "target_id": "all", "reason_code": "TEST"}
    r = h2.post("/sec/v1/freezes", h2.approved("FREEZE", "all:all", body, key=evil))
    assert r.status_code == 403 and r.json()["detail"] == "APPROVAL_CREDENTIAL_UNKNOWN"
    assert h2.svc.passkeys[andre.credential_id]["status"] == "active"


def test_r4_1_a_forged_line_replacing_our_own_pending_line_is_ignored(tmp_path):
    """While running: our own line is kept in memory; the file copy is never what is trusted."""
    class Lossy(FakeLedger):
        lose = 0

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            if self.lose and event_type == "log_anchor":
                self.lose -= 1
                raise LedgerRecordError("lost")
            super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
    d = str(tmp_path / "d")
    led = Lossy()
    h = Harness(tmp_path, data_dir=d, ledger=led)
    h.enroll()
    led.lose = 2
    r = h.post("/sec/v1/secrets", {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"},
               caller="finance_31")
    assert r.status_code == 503
    evil = forge_pending_enrolment(h, d)                            # overwrite our pending line
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert "vault:finance_31.k" in h.svc.by_ref and evil.credential_id not in h.svc.passkeys


def test_r4_1_a_line_whose_anchor_lands_after_restart_is_appended_then(tmp_path):
    """The process stopped with its anchor in flight: the line is set aside, and appended once the ledger shows it."""
    class Deferred(FakeLedger):
        hold = False
        held = []

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            a = (event_id, department, event_type, actor, subject_id, payload, summary)
            if self.hold and event_type == "log_anchor":
                self.held.append(a)
                raise LedgerRecordError("in flight")
            super().record_event(*a)
    d = str(tmp_path / "d")
    led = Deferred()
    h = Harness(tmp_path, data_dir=d, ledger=led)
    h.enroll()
    led.hold = True
    assert h.post("/sec/v1/secrets", {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"},
                  caller="finance_31").status_code == 503
    led.hold = False
    h2 = h.restart()                                                 # no anchor yet: set aside, not trusted
    assert "vault:finance_31.k" not in h2.svc.by_ref and h2.svc.integrity["ok"] is True
    FakeLedger.record_event(led, *led.held[0])                       # the in-flight anchor lands
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert "vault:finance_31.k" in h2.svc.by_ref                     # the ledger vouched; the line is in


def test_r4_1_a_stale_set_aside_line_is_dropped_once_the_log_moves_on(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    forge_pending_enrolment(h, d)
    h2 = h.restart()
    assert os.path.exists(os.path.join(d, "pending.discarded"))
    h2.ok(h2.post("/sec/v1/secrets", {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"},
                  caller="finance_31"), 201)
    h2.svc._last_integrity_try = 0
    h2.svc.verify_integrity(force=True, always=True)
    assert not os.path.exists(os.path.join(d, "pending.discarded"))


def test_r4_2_a_blank_line_in_the_log_refuses_start_with_a_clear_message(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    with open(os.path.join(d, "security_log.jsonl"), "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        h.restart()


def test_r4_2_verify_sees_a_blank_line(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    with open(os.path.join(d, "security_log.jsonl"), "ab") as fh:
        fh.write(b"\n")
    assert h.svc.log.verify() is False


def test_r4_3_a_failed_freeze_attempt_uses_the_challenge_up(hk):
    body = {"request_id": rid(), "target_kind": "caller", "target_id": "legal_37", "reason_code": "TEST"}
    ch = hk.challenge("FREEZE", "caller:legal_37", body)
    bad = hk.keys[0].assert_(ch)
    bad["signature"] = b64u(b"\x30" + b"\x00" * 70)
    assert hk.post("/sec/v1/freezes", {**body, "approval": bad}).json()["detail"] == "PASSKEY_SIGNATURE_INVALID"
    r = hk.post("/sec/v1/freezes", {**body, "approval": hk.keys[0].assert_(ch)})
    assert r.status_code == 403 and r.json()["detail"] == "APPROVAL_CHALLENGE_USED"
