"""AEGIS sweep A (on 5d49ee9) — regression tests for the security-py findings. Each one failed on 5d49ee9 and passes
after the fix (the probes in sweep-A/security passed while the bug existed)."""

from clock import FixedClock
from helpers import FakeLedger, Harness, rid
from ports import Ports


class Drive:
    system = "drive"

    def __init__(self, boom=False, release_fails=0):
        self.held, self.boom, self.release_fails, self.release_calls = set(), boom, release_fails, 0

    def preserve(self, hold_id, refs):
        if self.boom:
            raise ConnectionError("drive API down")
        self.held.add(hold_id)
        return True

    def release(self, hold_id):
        self.release_calls += 1
        if self.release_fails:
            self.release_fails -= 1
            raise ConnectionError("drive API down")
        self.held.discard(hold_id)
        return True


def _hold(h, hold_id="h1"):
    return h.ok(h.post("/sec/v1/holds", {"request_id": rid(), "hold_id": hold_id, "systems": ["drive"],
                                         "subject_refs": ["client:acme"]}, caller="legal_37"), 201)


# ------------------------------------------------------------------ 1. release_hold: commit first, then release

def test_release_hold_keeps_the_data_preserved_when_the_record_fails(tmp_path):
    led = FakeLedger()
    ports = Ports.default()
    drive = ports.preservation["drive"] = Drive()
    h = Harness(tmp_path, ledger=led, ports=ports)
    _hold(h)
    led.fail_types = {"log_anchor"}
    body = {"request_id": rid()}
    assert h.post("/sec/v1/holds/h1/release", body, caller="legal_37").status_code == 503
    led.fail_types = set()
    assert drive.held == {"h1"} and h.svc.holds["h1"]["status"] == "active"    # was: released, record still active
    assert h.svc.verify_integrity(force=True, always=True)["ok"]
    r = h.ok(h.post("/sec/v1/holds/h1/release", body, caller="legal_37"))
    assert r["status"] == "released" and r["release_pending"] == [] and drive.held == set()


def test_an_unconfirmed_release_is_retried_until_confirmed(tmp_path):
    ports = Ports.default()
    drive = ports.preservation["drive"] = Drive(release_fails=4)          # fails the 3 in-request tries, then 1
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=ports)
    _hold(h)
    r = h.ok(h.post("/sec/v1/holds/h1/release", {"request_id": rid()}, caller="legal_37"))
    assert r["status"] == "released" and r["release_pending"] == ["drive"] and drive.held == {"h1"}
    h2 = h.restart()                                                         # the pending release survives
    assert h2.svc.holds["h1"]["release_pending"] == ["drive"]
    j = h2.ok(h2.post("/sec/v1/jobs/hold-release-retry/run", {"request_id": rid()}, caller="scheduler"))
    assert j["confirmed"] == 1 and drive.held == set() and h2.svc.holds["h1"]["release_pending"] == []
    j = h2.ok(h2.post("/sec/v1/jobs/hold-release-retry/run", {"request_id": rid()}, caller="scheduler"))
    assert j["confirmed"] == 0 and drive.release_calls == 5                  # nothing left to release


# ------------------------------------------------------------------ 2. an adapter that raises is not a 500

def test_preservation_adapter_exception_is_a_failed_preservation(tmp_path):
    ports = Ports.default()
    ports.preservation["drive"] = Drive(boom=True)
    h = Harness(tmp_path, ports=ports)
    r = h.post("/sec/v1/holds", {"request_id": rid(), "hold_id": "h2", "systems": ["drive"],
                                 "subject_refs": ["client:acme"]}, caller="legal_37")
    assert r.status_code == 201, r.text                                      # was 500
    a = r.json()
    assert a["delivered"] is False and a["systems"] == {"drive": "failed"} and "failed" in a["reason"]
    assert any(i["subject"] == "hold:h2" for i in h.svc.incidents.values())


# ------------------------------------------------------------------ 3. fixed clock; emergency challenges on self.clock

def test_the_harness_defaults_to_a_fixed_clock(tmp_path):
    h = Harness(tmp_path)
    assert isinstance(h.svc.clock, FixedClock) and h.svc.clock is h.clock


def test_an_emergency_challenge_expires_on_the_service_clock(hk):
    body = {"request_id": rid(), "target_kind": "caller", "target_id": "legal_37", "reason_code": "TEST"}
    ch = hk.challenge("FREEZE", "caller:legal_37", body)
    hk.clock.advance(seconds=301)                       # no wall-clock time passes: only the service clock moves
    r = hk.post("/sec/v1/freezes", {**body, "approval": hk.keys[0].assert_(ch)})
    assert r.json()["detail"] == "APPROVAL_CHALLENGE_EXPIRED"
