"""Bug sweep D (Oct 6 2026 sweep at integration 5d49ee9) -- creative-py (department 6). Each test pins one finding and
FAILS on 5d49ee9 (the sweep's probes were lost; these are re-derived from the code):

  D-2      the ledger was the only store: evidence of a decision that never took effect looked like evidence of one
           that did; the 503 said "did NOT take effect" after the Compliance (38) call had gone out
  E-5/F-3  no local log, no single-writer guard (and the store must survive one fsync error)
  M        server-assigned ids (brief-0001, kit-0001) reused after a restart; a retried kit build unsigned the kit;
           the evidence view reads the ledger with the service lock free
"""

from __future__ import annotations

import os
import threading

import pytest

from fakes import AcceptingCreativeAgents, PassingCompliance
from flows import C, ok, zbc_open, zbm_work_at_quality
from samples import CAMPAIGN, zbc_kit_request
from shared.departments import Departments
from shared.ledger import FakeLedgerClient, LedgerNotRecorded
from shared.store import LOG_NAME, DataDirBusy, DataDirLock


class OnceFailing(FakeLedgerClient):
    """Refuses (certainly not recorded) the next write of a type in ``fail_types``, once."""

    def __init__(self):
        super().__init__()
        self.fail_types: set = set()

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        if event_type in self.fail_types:
            self.fail_types.discard(event_type)
            raise LedgerNotRecorded("simulated outage for one write")
        return super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)


def _evidence(api, **q):
    return ok(api.get("/audit/evidence", params=q))


# ============================================================================================ D-2


def test_d2_after_the_compliance_call_went_out_the_503_never_says_did_not_take_effect(make_api):
    led = OnceFailing()
    api = make_api(ledger=led, departments=Departments(compliance=PassingCompliance()))
    _, _, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    led.fail_types = {"crossing_compliance_38"}          # the answer's record fails AFTER Compliance was called
    r = api.post(f"/zbm/work/{w['work_id']}/compliance")
    body = r.json()
    assert r.status_code == 503 and body["took_effect"] == "partial", body
    assert "did NOT take effect" not in body["detail"]
    assert body["effect"]["departments"] == ["compliance_38"] and body["effect"]["failed_record"] == "not_recorded"


def test_d2_a_record_whose_reply_was_lost_is_attempted_until_the_retry_commits_it(make_api):
    """The ledger committed the brief's record, the reply was lost: the brief does not exist here. Its record must
    read ``attempted`` (on 5d49ee9 nothing told it apart from a brief that exists); the identical retry replays the
    record (the ledger's 200) and commits it: then exactly one ``committed`` record."""
    from samples import zbm_requirements
    from shared.ledger import LedgerRecordError

    class LosesOneReply(FakeLedgerClient):
        lose = False

        def record_event(self, *a, **k):
            super().record_event(*a, **k)
            if self.lose:
                self.lose = False
                raise LedgerRecordError("ledger response lost (test double)")
    led = LosesOneReply()
    api = make_api(ledger=led)
    led.lose = True
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements()})
    assert r.status_code == 503 and r.json()["took_effect"] == "unknown", r.text
    ev = _evidence(api, event_type="brief_drafted")
    assert ev["counts"] == {"committed": 0, "attempted": 1}, ev
    ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    ev = _evidence(api, event_type="brief_drafted")
    assert ev["counts"] == {"committed": 1, "attempted": 0}, ev


def test_d2_every_record_of_a_completed_decision_is_committed_and_an_owed_line_is_written_next(make_api):
    led = OnceFailing()
    api = make_api(ledger=led, departments=Departments(compliance=PassingCompliance()))
    _, _, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    ev = _evidence(api)
    assert ev["counts"]["attempted"] == 0 and ev["counts"]["committed"] >= 4, ev
    led.fail_types = {"log_anchor"}
    r = api.post(f"/zbm/work/{w['work_id']}/compliance")
    body = r.json()
    assert r.status_code == 503 and body["took_effect"] is True and body["evidence"] == "pending", body
    assert ok(api.get("/health"))["evidence_lines_owed"] == 1
    assert _evidence(api)["counts"]["attempted"] >= 1
    from samples import zbm_requirements
    ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)   # the next decision writes it first
    after = _evidence(api)
    assert after["counts"]["attempted"] == 0 and ok(api.get("/health"))["evidence_lines_owed"] == 0, after


# ============================================================================================ E-5/F-3 + restart ids


def test_store_single_writer_and_ids_never_reused_after_a_restart(make_api, tmp_path):
    d = str(tmp_path / "d")
    led = FakeLedgerClient()
    lock = DataDirLock(d)
    api = make_api(ledger=led, data_dir=d, dir_lock=lock)
    with pytest.raises(DataDirBusy):
        make_api(ledger=led, data_dir=d, dir_lock=lock)              # one live instance per directory
    from flows import zbm_approved_brief
    first = zbm_approved_brief(api)["brief_id"]
    api.app.state.close()
    api2 = make_api(ledger=led, data_dir=d, dir_lock=lock)          # restart on the same directory
    second = zbm_approved_brief(api2)["brief_id"]
    assert first != second, (first, second)
    assert ok(api2.get("/health"))["in_memory"] is False
    api2.app.state.close()
    lock.release()


def test_store_one_fsync_error_never_bricks_the_log(make_api, tmp_path, monkeypatch):
    import shared.store as store_mod
    d = str(tmp_path / "d")
    lock = DataDirLock(d)
    api = make_api(data_dir=d, dir_lock=lock)
    real = os.fsync
    hit = {"n": 0}

    def flaky(fd):
        hit["n"] += 1
        if hit["n"] == 1:
            raise OSError(5, "simulated EIO on fsync")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky)
    from samples import zbm_requirements
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements()})
    assert r.status_code == 503 and r.json()["evidence"] == "pending", r.text
    monkeypatch.setattr(store_mod.os, "fsync", real)
    ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    log_ = api.app.state.recorder.journal.log
    assert log_.verify() and len(log_) == 2
    with open(os.path.join(d, LOG_NAME), "rb") as fh:
        assert fh.read().count(b"\n") == 2
    api.app.state.close()
    lock.release()


# ============================================================================================ M: kit


def test_m_retrying_the_kit_build_after_andre_signed_never_unsigns_it(make_api):
    api = make_api(departments=Departments(creative_agents=AcceptingCreativeAgents()))
    zbc_open(api)                                                     # the kit is built and signed
    assert api.zbc.kits[CAMPAIGN].status == "signed"
    again = api.post(f"{C}/kit", zbc_kit_request())                  # the identical request, retried
    assert again.status_code in (200, 201) and again.json()["status"] == "signed", again.text
    assert api.zbc.kits[CAMPAIGN].status == "signed"
    r = api.post(f"{C}/kit", zbc_kit_request(seed_count=4))
    assert r.status_code in (409, 412) and api.zbc.kits[CAMPAIGN].status == "signed", r.text


# ============================================================================================ M: lock


def test_m_the_evidence_view_reads_the_ledger_with_the_service_lock_free(make_api):
    gate, inside = threading.Event(), threading.Event()

    class Slow(FakeLedgerClient):
        def entries(self):
            inside.set()
            gate.wait(10)
            return super().entries()
    api = make_api(ledger=Slow())
    from flows import zbm_approved_brief
    zbm_approved_brief(api)
    out: list = []
    t = threading.Thread(target=lambda: out.append(api.get("/audit/evidence").json()))
    t.start()
    assert inside.wait(10)
    lock = api.app.state.recorder.lock
    got = lock.acquire(timeout=2)
    if got:
        lock.release()
    gate.set()
    t.join(10)
    assert got and out and out[0]["counts"]["committed"] >= 1
