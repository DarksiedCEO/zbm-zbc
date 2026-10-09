"""AEGIS re-review of bug sweep D (37a2830), creative-py: C-1 (a partial decision whose evidence line was then owed
was reported as "took effect, do not repeat") and C-2 (an id burned by a lost reply, its line owed at a restart, was
reissued for another client). Ported from the reviewer's probes (aegis-ocfd/probe); each FAILS on 37a2830."""

from __future__ import annotations

from fakes import PassingCompliance
from flows import ok, zbm_work_at_quality
from shared.departments import Departments
from shared.ledger import FakeLedgerClient, LedgerNotRecorded, LedgerRecordError
from shared.store import DataDirLock
from test_sweep_d import OnceFailing


def test_c1_a_partial_decision_with_an_owed_line_stays_partial(make_api):
    led = OnceFailing()
    api = make_api(ledger=led, departments=Departments(compliance=PassingCompliance()))
    _, _, w = zbm_work_at_quality(api)
    ok(api.post(f"/zbm/work/{w['work_id']}/quality", {"actor_id": "zbm_creative_quality", "notes": []}))
    led.fail_types = {"crossing_compliance_38", "log_anchor"}
    r = api.post(f"/zbm/work/{w['work_id']}/compliance")
    body = r.json()
    assert r.status_code == 503 and body["took_effect"] == "partial", body
    assert body["effect"]["evidence"] == "pending" and "Do not repeat" not in body["detail"]
    assert ok(api.get("/health"))["evidence_lines_owed"] == 1


def test_c2_an_id_on_the_ledger_only_is_never_reissued_after_a_restart(make_api, tmp_path):
    from samples import zbm_requirements
    d = str(tmp_path / "d")

    class Down(FakeLedgerClient):
        mode = None

        def record_event(self, *a, **k):
            et = k.get("event_type")
            if self.mode == "lost" and et == "brief_drafted":
                super().record_event(*a, **k)
                raise LedgerRecordError("reply lost")
            if self.mode == "lost" and et == "log_anchor":
                raise LedgerNotRecorded("down")
            return super().record_event(*a, **k)
    led = Down()
    lock = DataDirLock(d)
    api = make_api(ledger=led, data_dir=d, dir_lock=lock)
    first = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)["brief_id"]
    led.mode = "lost"
    assert api.post("/zbm/briefs", {"requirements": zbm_requirements(client_id="client_b")}).status_code == 503
    api.app.state.close()
    led.mode = None
    api2 = make_api(ledger=led, data_dir=d, dir_lock=lock)
    b = ok(api2.post("/zbm/briefs", {"requirements": zbm_requirements(client_id="client_c")}), 201)["brief_id"]
    subjects = [e["subject_id"] for e in led.events if e["event_type"] == "brief_drafted"]
    assert len(subjects) == len(set(subjects)) == 3 and b not in (first, "brief-0002"), (first, b, subjects)
    api2.app.state.close()
    lock.release()
