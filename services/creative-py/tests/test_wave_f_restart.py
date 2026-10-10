"""Wave F (M-1, ADR 0005 "Wave F fixes"): creative-py's consequential state is rebuilt from the local record log at
start. Before this, briefs, jobs, work, rulebooks and kits (with Andre's signatures) lived in memory only and a restart
lost them. Each test here FAILS on 038fa93.

Every round trip compares the WHOLE consequential state of the live app with its restarted copy (the state tracker's
canonical snapshot), after every step, and goes on working on the restarted copy. A signed kit stays signed; an unsigned
kit can never become signed by a replay."""

from __future__ import annotations

import pytest

from conftest import TEST_FOUNDER_TOKEN
from fakes import AcceptingCreativeAgents, PassingCompliance
from flows import C, ok
from samples import (CAMPAIGN, ZBC_ASSETS, zbc_goal, zbc_kit_request, zbc_license, zbc_music_clearance, zbc_source,
                     zbm_clearance, zbm_requirements, zbm_work)
from shared.departments import Departments
from shared.ledger import FakeLedgerClient, LedgerNotRecorded
from shared.statelog import StateReplayError
from shared.store import DataDirLock, RecordLog, StoreWriteError


class Ledger(FakeLedgerClient):
    """Refuses the n-th write of ``fail_type`` from now (1 = the next); counts whole-ledger reads."""

    def __init__(self):
        super().__init__()
        self.fail_type, self.countdown, self.whole_reads = "", 0, 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        if event_type == self.fail_type and self.countdown:
            self.countdown -= 1
            if self.countdown == 0:
                raise LedgerNotRecorded("simulated outage for one write (test double)")
        return super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)

    def entries(self):
        self.whole_reads += 1
        return super().entries()


class Box:
    def __init__(self, make_api, tmp_path):
        self.make_api, self.d = make_api, str(tmp_path / "data")
        self.lock = DataDirLock(self.d)
        self.led = Ledger()
        self.depts = Departments(creative_agents=AcceptingCreativeAgents(), compliance=PassingCompliance())
        self.api = self._new()

    def _new(self):
        return self.make_api(ledger=self.led, data_dir=self.d, dir_lock=self.lock, departments=self.depts)

    def snapshot(self) -> bytes:
        return self.api.app.state.recorder.state.snapshot()

    def restart(self, check: bool = True):
        before = self.snapshot()
        self.api.app.state.close()
        self.api = self._new()
        if check:
            assert self.snapshot() == before
        return self.api

    def close(self):
        self.api.app.state.close()
        self.lock.release()


@pytest.fixture
def box(make_api, tmp_path):
    b = Box(make_api, tmp_path)
    yield b
    b.close()


def _kit_to_draft(box):
    a = box.api
    ok(a.post("/rights/licenses", {"actor_id": "rights_desk", "license": zbc_license()}), 201)
    ok(a.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbc_music_clearance()}), 201)
    v = ok(a.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal()}), 201)["version"]
    ok(box.api.post(f"{C}/rulebooks/{v}/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(box.api.post(f"{C}/rulebooks/{v}/sign", andre=TEST_FOUNDER_TOKEN))
    ok(box.api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS}))
    ok(box.api.post(f"{C}/rulebooks/{v}/go-live"))
    ok(box.api.post(f"{C}/moment-map", zbc_source()))
    ok(box.api.post(f"{C}/hook-sheets"))
    ok(box.api.post(f"{C}/kit", zbc_kit_request()), 201)
    assert box.api.zbc.kits[CAMPAIGN].status == "draft"


def test_every_zbm_step_survives_a_restart_and_the_restarted_app_carries_on(box):
    ids = {}
    steps = [
        lambda: ok(box.api.post("/rights/clearances", {"actor_id": "rights_desk", "record": zbm_clearance()}), 201),
        lambda: ids.update(brief=ok(box.api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)[
            "brief_id"]),
        lambda: ok(box.api.post(f"/zbm/briefs/{ids['brief']}/review", {"actor_id": "zbm_creative_lead"})),
        lambda: ids.update(job=ok(box.api.post(f"/zbm/briefs/{ids['brief']}/jobs"), 201)["job_id"]),
        lambda: ids.update(work=ok(box.api.post(f"/zbm/jobs/{ids['job']}/work", zbm_work()), 201)["work_id"]),
        lambda: ok(box.api.post(f"/zbm/work/{ids['work']}/export-validation")),
        lambda: ok(box.api.post(f"/zbm/work/{ids['work']}/rights")),
        lambda: ok(box.api.post(f"/zbm/work/{ids['work']}/quality", {"actor_id": "zbm_creative_quality",
                                                                     "notes": []})),
        lambda: ok(box.api.post(f"/zbm/work/{ids['work']}/compliance")),
        lambda: ok(box.api.post(f"/zbm/work/{ids['work']}/final-approval", andre=TEST_FOUNDER_TOKEN)),
    ]
    for step in steps:
        step()
        box.restart()
    assert ok(box.api.get(f"/zbm/work/{ids['work']}"))["stage"] == "approved_by_andre"
    assert box.api.zbm.briefs[ids["brief"]].status.value == "approved" and ids["job"] in box.api.zbm.jobs
    # a new brief after the restarts takes a fresh id (counters and state agree)
    nxt = ok(box.api.post("/zbm/briefs", {"requirements": zbm_requirements(client_id="client_b")}), 201)["brief_id"]
    assert nxt not in (ids["brief"], ids["job"], ids["work"])


def test_every_zbc_step_survives_a_restart_and_a_signed_kit_stays_signed(box):
    a = lambda: box.api   # noqa: E731
    steps = [
        lambda: ok(a().post("/rights/licenses", {"actor_id": "rights_desk", "license": zbc_license()}), 201),
        lambda: ok(a().post("/rights/clearances", {"actor_id": "rights_desk", "record": zbc_music_clearance()}), 201),
        lambda: ok(a().post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal()}), 201),
        lambda: ok(a().post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"})),
        lambda: ok(a().post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN)),
        lambda: ok(a().post(f"{C}/rights-check", {"assets": ZBC_ASSETS})),
        lambda: ok(a().post(f"{C}/rulebooks/1/go-live")),
        lambda: ok(a().post(f"{C}/moment-map", zbc_source())),
        lambda: ok(a().post(f"{C}/hook-sheets")),
        lambda: ok(a().post(f"{C}/kit", zbc_kit_request()), 201),
        lambda: ok(a().post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN)),
    ]
    for step in steps:
        step()
        box.restart()
    kit = box.api.zbc.kits[CAMPAIGN]
    assert kit.status == "signed" and kit.signed_by == "andre"
    box.restart()
    box.restart()
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"
    # the identical kit build after the restarts answers the signed kit, never a new draft
    again = box.api.post(f"{C}/kit", zbc_kit_request())
    assert again.status_code in (200, 201) and again.json()["status"] == "signed", again.text


def test_replay_is_idempotent_and_writes_nothing(box):
    _kit_to_draft(box)
    first, lines, events = box.snapshot(), len(box.api.app.state.recorder.journal.log), len(box.led.events)
    box.restart()
    box.restart()
    assert box.snapshot() == first
    assert len(box.api.app.state.recorder.journal.log) == lines and len(box.led.events) == events


def test_a_signature_whose_record_failed_is_not_applied_by_a_restart(box):
    _kit_to_draft(box)
    box.led.fail_type, box.led.countdown = "campaign_kit_signed_by_andre", 1
    r = box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN)
    assert r.status_code == 503 and box.api.zbc.kits[CAMPAIGN].status == "draft", r.text
    box.restart(check=False)
    assert box.api.zbc.kits[CAMPAIGN].status == "draft"         # the open intent: not on the ledger, never applied
    box.restart()
    assert box.api.zbc.kits[CAMPAIGN].status == "draft"
    ok(box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN))  # Andre signs again; it survives
    box.restart()
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"


def test_a_signature_whose_line_was_owed_at_a_crash_is_not_lost(box):
    """The intent anchors and is in the log, the ledger records the signature, the decision's own line cannot be
    anchored (owed) and the process dies: the next start applies the signature from the ledger, exactly once."""
    _kit_to_draft(box)
    box.led.fail_type, box.led.countdown = "log_anchor", 2       # the intent's anchor passes; the decision's fails
    r = box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN)
    assert r.status_code == 503 and r.json().get("evidence") == "pending", r.text
    assert box.led.of_type("campaign_kit_signed_by_andre")
    box.led.whole_reads = 0
    box.restart(check=False)
    assert box.api.zbc.kits[CAMPAIGN].status == "signed" and box.api.zbc.kits[CAMPAIGN].signed_by == "andre"
    assert box.led.pages_read >= 1
    box.restart()                                                # closed: nothing is applied twice
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"


def test_a_signed_kit_no_committed_signature_names_refuses_start(box):
    """A line that would sign a kit without a committed Andre signature (a forged or hand-edited log with a valid
    chain) never yields a signed kit: start-up is refused."""
    _kit_to_draft(box)
    rec = box.api.app.state.recorder
    st = rec.state
    kit = box.api.zbc.kits[CAMPAIGN].model_copy(update={"status": "signed", "signed_by": "andre"})
    forged = {"v": 1, "ops": [["set", "zbc_kits", st.codec.enc(CAMPAIGN), st.codec.enc(kit)]]}
    rec.journal.log.append("decision", "2026-10-09T00:00:00+00:00", {"op": "x", "rk": "x", "evidence": [],
                                                                      "state": forged})
    box.api.app.state.close()
    with pytest.raises(StateReplayError, match="no committed Andre signature"):
        box._new()
    box.api = box.make_api(ledger=box.led, departments=box.depts)   # a closable (in-memory) app for the teardown


@pytest.mark.parametrize("kind,data,why", [
    ("mystery", {"op": "x"}, "not a line kind"),
    ("decision", {"op": "x", "extra_field": 1}, "unknown field"),
    ("decision", {"op": "x", "state": {"v": 9, "ops": []}}, "state version"),
    ("decision", {"op": "x", "state": {"v": 1, "ops": [["set", "zbm_vaults", "k", None]]}}, "unknown state container"),
    ("decision", {"op": "x", "state": {"v": 1, "ops": [["set", "zbm_briefs", "k", {"$m": ["os:path", [], []]}]]}},
     "unknown type"),
    ("decision", {"op": "x", "state": {"v": 1, "ops": [["set", "zbm_briefs", "k", {"$nope": 1}]]}},
     "unknown type tag"),
    ("kit_signature_intent", {"kit_signature_intent": {"kit_id": "kit-0001"}}, "malformed kit signature intent"),
])
def test_a_line_replay_cannot_interpret_refuses_start(make_api, tmp_path, kind, data, why):
    d = str(tmp_path / "bad")
    RecordLog(d).append(kind, "2026-10-09T00:00:00+00:00", data)
    with pytest.raises(StateReplayError, match=why) as exc:
        make_api(data_dir=d)
    assert "line 1" in str(exc.value) and "refusing to start" in str(exc.value)


def test_id_resumption_reads_only_this_department_through_the_paged_read(box):
    ok(box.api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    box.led.whole_reads = box.led.pages_read = 0
    box.restart()
    assert box.led.whole_reads == 0 and box.led.pages_read >= 1


# ============================================================================================ AEGIS review of Wave F


def _lose_the_next_decision_line(box):
    """The intent line lands, the signature is recorded, then the decision's local append fails (the process dies)."""
    log_ = box.api.app.state.recorder.journal.log
    real, n = log_.append_prepared, []

    def append(rec, line):
        n.append(rec["kind"])
        if rec["kind"] == "decision" and n.count("decision") == 1:
            raise StoreWriteError("simulated disk failure (test double)")
        return real(rec, line)
    log_.append_prepared = append


def test_f3_signing_again_after_a_failed_resolution_line_records_no_second_signature(box):
    """The decision's line is lost (its append fails), the restart's resolution line cannot be written (the kit stays a
    draft in that process), Andre signs again: the same signature event id (ledger 200), one event, signed after the
    next restart."""
    _kit_to_draft(box)
    _lose_the_next_decision_line(box)
    assert box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN).status_code == 503
    assert box.led.of_type("campaign_kit_signed_by_andre")
    box.led.fail_type, box.led.countdown = "log_anchor", 2                 # tail re-anchor passes, resolution's fails
    box.restart(check=False)
    assert box.api.zbc.kits[CAMPAIGN].status == "draft"                    # not served before its line is anchored
    ok(box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN))
    box.restart(check=False)
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"
    assert len(box.led.of_type("campaign_kit_signed_by_andre")) == 1
    box.restart()


def test_f3_an_intent_not_yet_on_the_ledger_is_rechecked_after_the_grace_before_it_is_closed(box, make_api):
    """The signature's record was still in flight when the process stopped; it lands while start-up waits out the
    grace: the signature is applied, not closed as "not on the ledger"."""
    _kit_to_draft(box)
    led = box.led
    held = []
    orig = type(led).record_event

    def in_flight(self, event_id, department, event_type, *a, **k):
        if event_type == "campaign_kit_signed_by_andre" and not held:
            held.append((event_id, department, event_type, a, k))
            raise LedgerNotRecorded("no reply yet (in flight, test double)")
        return orig(self, event_id, department, event_type, *a, **k)
    type(led).record_event = in_flight
    try:
        assert box.api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN).status_code == 503
    finally:
        type(led).record_event = orig
    box.api.app.state.close()
    waited = []

    def sleep(s):
        waited.append(s)
        eid, dep, et, a, k = held[0]
        orig(led, eid, dep, et, *a, **k)                                    # the in-flight record lands
    box.api = make_api(ledger=led, data_dir=box.d, dir_lock=box.lock, departments=box.depts, startup_sleep=sleep)
    assert waited and 0 < waited[0] <= 10
    assert box.api.zbc.kits[CAMPAIGN].status == "signed"


def test_f1_memory_deltas_stay_the_same_size_as_the_store_grows():
    """AEGIS F-1: 2,000 feedback entries made each line carry the whole memory (212.9 MB of deltas). Now a line
    carries only the new entry: its size is the same at the 10th and the 2,000th entry (bytes, not time)."""
    import json as _json

    import state_replay
    from shared.statelog import Codec, StateTracker
    from zbc.creative_memory import ZbcCreativeMemory
    from zbm.creative_memory import ZbmCreativeMemory

    class Owner:
        memory = ZbmCreativeMemory()
        zmem = ZbcCreativeMemory()
    o = Owner()
    st = StateTracker(Codec(state_replay._state_modules()))
    for attr in ("winners", "brand_notes", "feedback"):
        st.add_keyed_lists(f"m_{attr}", o.memory, attr)
    st.baseline()
    sizes, lines = [], []
    for i in range(2000):
        o.memory.add_feedback(f"client_{i % 50:02d}", "zbm_creative_lead", "feedback text " * 5)
        d, mark = st.delta()
        lines.append(_json.loads(_json.dumps(d)))
        sizes.append(len(_json.dumps(d)))
        mark()
    assert sizes[1999] == sizes[9] and max(sizes) <= sizes[0] + 16, (sizes[0], sizes[9], max(sizes))
    copy = Owner()
    copy.memory = ZbmCreativeMemory()
    st2 = StateTracker(Codec(state_replay._state_modules()))
    for attr in ("winners", "brand_notes", "feedback"):
        st2.add_keyed_lists(f"m_{attr}", copy.memory, attr)
    for ln in lines:
        st2.apply(ln, "test")
    assert st2.snapshot() == st.snapshot() and len(copy.memory.feedback["client_07"]) == 40
