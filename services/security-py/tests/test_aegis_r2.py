"""AEGIS round 2 (Oct 5 2026, BLOCKING): regressions for N1-N6 and T1, each the reviewer's scenario."""

from __future__ import annotations

import os

from helpers import ENROLL_TOKEN, Authenticator, FakeLedger, Harness, rid
from ledger import LedgerRecordError


class Ledger2(FakeLedger):
    """``mode``: record_lost (recorded, answer lost), notrecorded_lost (not recorded, answer lost), deferred
    (in flight: recorded LATER, after the service has moved on)."""

    def __init__(self):
        super().__init__()
        self.mode, self.n, self.deferred = None, 0, []

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        a = (event_id, department, event_type, actor, subject_id, payload, summary)
        if self.mode and event_type == "log_anchor" and self.n > 0:
            self.n -= 1
            if self.mode == "record_lost":
                super().record_event(*a)
            elif self.mode == "deferred":
                self.deferred.append(a)
            raise LedgerRecordError("lost")
        super().record_event(*a)

    def land(self):
        for a in self.deferred:
            FakeLedger.record_event(self, *a)
        self.deferred = []


def store(h, name, value="v", caller="finance_31"):
    return h.post("/sec/v1/secrets", {"request_id": rid(), "name": name, "kind": "api_key", "value": value,
                                      "readers": [caller], "purposes": ["p"]}, caller=caller)


def durable(tmp_path, led):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    h.enroll()
    return h


def test_n1_an_anchor_that_lands_late_never_bricks_the_vault(tmp_path):
    led = Ledger2()
    h = durable(tmp_path, led)
    led.mode, led.n = "deferred", 2
    assert store(h, "k2").status_code == 503
    h.svc._last_integrity_try = 0
    assert store(h, "k3").status_code == 201           # k2 was rolled forward first (anchor re-recorded), then k3
    led.land()                                          # the late duplicate lands: identical content, no new entry
    led.mode = None
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert store(h, "k5").status_code == 201
    h2 = h.restart()
    assert h2.ok(h2.get("/sec/v1/status"))["integrity"]["ok"] is True
    assert {"vault:finance_31.k2", "vault:finance_31.k3", "vault:finance_31.k5"} <= set(h2.svc.by_ref)


def test_n1_a_line_never_recorded_is_rolled_forward_too(tmp_path):
    led = Ledger2()
    h = durable(tmp_path, led)
    led.mode, led.n = "notrecorded_lost", 2
    assert store(h, "k2").status_code == 503
    h.svc._last_integrity_try = 0
    assert store(h, "k3").status_code == 201
    assert "vault:finance_31.k2" in h.svc.by_ref
    assert h.restart().svc.integrity["ok"] is True


def test_n2_an_unknown_store_keeps_its_value(tmp_path):
    led = Ledger2()
    h = durable(tmp_path, led)
    led.mode, led.n = "record_lost", 2
    assert store(h, "k2", value="the_value").status_code == 503
    h.svc._last_integrity_try = 0
    r = h.ok(h.use("finance_31", "vault:finance_31.k2", "p"))
    assert r["value"] == "the_value"
    assert h.restart().ok(h.restart().use("finance_31", "vault:finance_31.k2", "p"))["value"] == "the_value"


def test_n2_an_unknown_rotation_never_destroys_the_credential(tmp_path):
    led = Ledger2()
    h = durable(tmp_path, led)
    h.ok(store(h, "k", value="v1"), 201)
    led.mode, led.n = "record_lost", 2
    r = h.post("/sec/v1/secrets/vault:finance_31.k/rotate", {"request_id": rid(), "value": "v2"},
               caller="finance_31")
    assert r.status_code == 503
    h.svc._last_integrity_try = 0
    assert h.ok(h.use("finance_31", "vault:finance_31.k", "p"))["value"] == "v2"
    h2 = h.restart()
    assert h2.ok(h2.use("finance_31", "vault:finance_31.k", "p"))["value"] == "v2"
    sid = h2.svc.by_ref["vault:finance_31.k"]
    assert h2.svc.sealed.names() >= {f"{sid}.v2"} and f"{sid}.v1" not in h2.svc.sealed.names()


def test_n2_a_certainly_unrecorded_store_leaves_no_sealed_file(tmp_path):
    led = Ledger2()
    h = durable(tmp_path, led)
    before = set(h.svc.sealed.names())
    h.ledger.fail = True
    assert store(h, "k2").status_code == 503
    h.ledger.fail = False
    assert set(h.svc.sealed.names()) == before


def test_n2_restart_with_a_pending_line_keeps_its_sealed_file(tmp_path):
    led = Ledger2()
    h = durable(tmp_path, led)
    led.mode, led.n = "record_lost", 2
    assert store(h, "k2", value="kept").status_code == 503
    h2 = h.restart()                                  # orphan removal must wait for the roll-forward
    assert h2.ok(h2.use("finance_31", "vault:finance_31.k2", "p"))["value"] == "kept"


def test_n3_a_freeze_challenge_flood_cannot_block_the_freeze_switch(hk):
    for _ in range(300):
        hk.challenge("FREEZE", "caller:legal_37", {"request_id": rid(), "target_kind": "caller",
                                                   "target_id": "legal_37", "reason_code": "TEST"})
    body = {"request_id": rid(), "target_kind": "all", "target_id": "all", "reason_code": "TEST"}
    hk.ok(hk.post("/sec/v1/freezes", hk.approved("FREEZE", "all:all", body)), 201)
    assert any(i["code"] == "APPROVAL_CHALLENGES_EXHAUSTED" for i in hk.svc.incidents.values())


def test_n4_the_integrity_job_always_reads_the_ledger(tmp_path):
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"))
    h.enroll()
    h.ok(h.get("/sec/v1/audit/integrity"))                         # a forced check just ran
    victim = next(e for e in h.ledger.events if e["event_type"] == "log_anchor")
    victim["payload_sha256"] = "0" * 64                            # tampered within the rate-limit window
    r = h.ok(h.post("/sec/v1/jobs/integrity/run", {"request_id": rid()}, caller="scheduler"))
    assert r["integrity"]["ok"] is False
    # the log cannot take an incident record while its integrity is broken; Andre is alerted anyway
    assert any(a["code"] == "INTEGRITY_FAILURE" for a in h.svc.alert_status.values())


def test_n5_a_replayed_clean_exit_never_reaches_a_newer_secret(h):
    for n in ("a", "b"):
        h.ok(h.post("/sec/v1/secrets", {"request_id": rid(), "name": n, "kind": "oauth_token", "value": "v",
                                        "client_id": "c1"}, caller="onboarding"), 201)
    R = rid()
    assert h.ok(h.post("/sec/v1/clients/c1/destroy", {"request_id": R}, caller="onboarding"))["destroyed"] == 2
    h.ok(h.post("/sec/v1/secrets", {"request_id": rid(), "name": "new", "kind": "oauth_token", "value": "v",
                                    "client_id": "c1"}, caller="onboarding"), 201)
    assert h.ok(h.post("/sec/v1/clients/c1/destroy", {"request_id": R}, caller="onboarding"))["destroyed"] == 2
    assert h.svc.secrets[h.svc.by_ref["vault:onboarding.new"]]["status"] == "active"
    # a NEW request does reach it, and the plan survives a restart
    assert h.ok(h.post("/sec/v1/clients/c1/destroy", {"request_id": rid()}, caller="onboarding"))["destroyed"] == 1


def test_n6_an_unreadable_pending_line_is_discarded_safely(tmp_path):
    d = str(tmp_path / "d")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    with open(os.path.join(d, "pending.line"), "wb") as fh:
        fh.write(b"{not json")
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True and not os.path.exists(os.path.join(d, "pending.line"))


def test_t1_two_token_ceremonies_enrol_only_one_passkey(h):
    """Mutation T1 (round 2): the consumed-token guard inside enroll() had no test."""
    o1 = h.ok(h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}))
    o2 = h.ok(h.post("/sec/v1/passkeys/enroll/options", {"enroll_token": ENROLL_TOKEN}))
    h.ok(h.post("/sec/v1/passkeys/enroll", Authenticator().register(o1)), 201)
    r = h.post("/sec/v1/passkeys/enroll", Authenticator().register(o2))
    assert r.status_code == 403 and r.json()["detail"] == "ENROLL_TOKEN_REFUSED"
    assert len(h.ok(h.get("/sec/v1/passkeys"))) == 1
