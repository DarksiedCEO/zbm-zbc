"""AEGIS sweep A (on 5d49ee9) — regression tests for the security-py findings. Each one failed on 5d49ee9 and passes
after the fix (the probes in sweep-A/security passed while the bug existed)."""

from helpers import FakeLedger, Harness, rid

from clock import FixedClock
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
    codes = {i["code"] for i in h.svc.incidents.values() if i["subject"] == "hold:h2"}
    assert codes == {"PRESERVATION_FAILED"}                                     # AEGIS L6: not "not connected"


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


# ------------------------------------------------------------------ AEGIS M3: one release in flight per (hold, system)

def test_m3_concurrent_retries_release_and_confirm_once(tmp_path):
    import threading

    class SlowDrive(Drive):
        def __init__(self):
            super().__init__(release_fails=3)           # the request's own tries fail: the release stays pending
            self.entered, self.go = threading.Event(), threading.Event()

        def release(self, hold_id):
            if self.release_fails:
                return super().release(hold_id)
            self.entered.set()
            assert self.go.wait(10)
            return super().release(hold_id)
    ports = Ports.default()
    drive = ports.preservation["drive"] = SlowDrive()
    h = Harness(tmp_path, ports=ports)
    _hold(h)
    assert h.ok(h.post("/sec/v1/holds/h1/release", {"request_id": rid()}, caller="legal_37"))["release_pending"] == \
        ["drive"]
    first: dict = {}
    t = threading.Thread(target=lambda: first.update(h.svc.retry_hold_releases()))
    t.start()
    assert drive.entered.wait(10)                       # the job's release is in flight ...
    second = h.svc.retry_hold_releases("h1")            # ... a concurrent retry skips the same (hold, system)
    drive.go.set()
    t.join(10)
    assert second == {"confirmed": 0, "unconfirmed": []} and first["confirmed"] == 1
    assert drive.release_calls == 4                     # 3 failed tries in the request, then exactly one
    assert sum(1 for r in h.svc.log.iter_records() if r["kind"] == "hold_release_confirmed") == 1
    assert h.svc.retry_hold_releases() == {"confirmed": 0, "unconfirmed": []}


def test_m3_a_confirmation_is_never_committed_twice(tmp_path):
    ports = Ports.default()
    ports.preservation["drive"] = Drive(release_fails=3)
    h = Harness(tmp_path, ports=ports)
    _hold(h)
    h.ok(h.post("/sec/v1/holds/h1/release", {"request_id": rid()}, caller="legal_37"))
    # the pending system was confirmed meanwhile (another path): the result of a stale release commits nothing
    assert h.svc._release_external([("h1", "drive")])["confirmed"] == 1
    assert h.svc._release_external([("h1", "drive")])["confirmed"] == 0
    assert sum(1 for r in h.svc.log.iter_records() if r["kind"] == "hold_release_confirmed") == 1
