"""Wave F (M-1, ADR 0004 "Wave F fixes"): onboarding's consequential state is rebuilt from the local record log at
start. Before this, clients, creators, escalations, campaigns and the playbook lived in memory only: after a restart a
payment to an onboarded creator was refused until the creator re-applied, and the re-application let a name variant
through (a second 1099 identity). Each test here FAILS on 038fa93.

Every round trip compares the WHOLE consequential state of the live service with its restarted copy
(``state_snapshot``: canonical bytes), after every step, and goes on working on the restarted copy."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import ANDRE_KEY, GOOD_GRANT, Clock, andre_resolve_body, client_for, make_service, start_body
from ledger import FakeLedgerClient, HttpLedgerClient, LedgerWriteError, select_paged
from memory import andre_action_token, approval_token
from statelog import StateReplayError
from store import DataDirLock, RecordLog
from test_sweep_d import _app


class Ledger(FakeLedgerClient):
    """Refuses the n-th write of ``fail_type`` from now (1 = the next one); ``whole_reads`` counts unfiltered reads."""

    def __init__(self):
        super().__init__()
        self.fail_type, self.countdown, self.whole_reads = "", 0, 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        if event_type == self.fail_type and self.countdown:
            self.countdown -= 1
            if self.countdown == 0:
                raise LedgerWriteError("simulated outage for one write")
        return super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)

    def entries(self):
        self.whole_reads += 1
        return super().entries()


class Box:
    """One data directory, one ledger, one clock; ``restart()`` closes the service and starts a new one on them."""

    def __init__(self, tmp_path, **kw):
        self.d = str(tmp_path / "data")
        self.lock = DataDirLock(self.d)
        self.led = kw.pop("ledger", None) or Ledger()
        self.clock = kw.pop("clock", None) or Clock()
        self.depts = None
        self.svc = self._new()
        self.depts = self.svc.depts     # the other departments (contract storage, payouts...) outlive our restarts
        self.c = client_for(self.svc)

    def _new(self):
        kw = {"departments": self.depts} if self.depts is not None else {}
        return make_service(all_fakes=True, ledger=self.led, clock=self.clock, log=RecordLog(self.d),
                            dir_lock=self.lock, **kw)

    def restart(self, check: bool = True):
        before = self.svc.state_snapshot()
        self.svc.close()
        self.svc = self._new()
        self.c = client_for(self.svc)
        if check:
            assert self.svc.state_snapshot() == before
        return self.svc

    def close(self):
        self.svc.close()
        self.lock.release()


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.close()


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


FACTS = {"vertical": "ecommerce", "facts": [
    {"field": "monthly_revenue_usd", "value": "10000.00", "provenance": "client_stated", "evidence": "intake chat",
     "observed_at": "2026-09-24T17:00:00Z"},
    {"field": "primary_goal", "value": "recover abandoned carts", "provenance": "client_stated",
     "evidence": "intake chat", "observed_at": "2026-09-24T17:00:00Z"}]}


def test_every_client_lane_step_survives_a_restart_and_the_restarted_service_carries_on(box):
    """Start (deal-size escalation, commitment), intake, scan, grant, audit, plan, a blocked activation, Andre's
    resolution (institutional pattern), activation, momentum, first win, score, tick, message, memory deletion, exit:
    after EVERY step the restarted service holds exactly the live state and the next step runs on it."""
    esc = {}

    def start():
        esc.update(_ok(box.c.post("/onboarding/clients", json=start_body()), 201)["escalation"])
    steps = [
        start,
        lambda: _ok(box.c.post("/onboarding/clients/client_a/intake/facts", json=FACTS)),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/access/website-scan",
                               json={"html": '<script src="https://cdn.shopify.com/x.js"></script>'})),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/access/grants", json=GOOD_GRANT)),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/audit", json={
            "account_data": {"orders": [{"order_id": "o1"}]}, "observed_monthly_revenue_usd": "9800.00"})),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["abandoned_carts"]})),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/activate"), 409),
        lambda: _ok(box.c.post(f"/onboarding/clients/client_a/escalations/{esc['escalation_id']}/resolve",
                               json=andre_resolve_body("client_a", esc["escalation_id"], "Andre approved the deal",
                                                       "deal_review"))),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/activate")),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/momentum")),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/first-win", json={"finding_id": "f1"})),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/recommend-score", json={"score": 10})),
        lambda: (box.clock.advance(days=3), _ok(box.c.post("/onboarding/clients/client_a/tick"))),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/messages", json={"text": "How is it going?"})),
        lambda: _ok(box.c.delete("/onboarding/clients/client_a/memory")),
        lambda: _ok(box.c.post("/onboarding/clients/client_a/exit", json={"memory_choice": "destroy"})),
    ]
    for step in steps:
        step()
        box.restart()
    view = box.svc.clients["client_a"]
    assert view.activation.activated and view.exited and view.recommend_score == 10 and view.first_win_finding == "f1"
    assert box.svc.escalations[esc["escalation_id"]].resolved_at is not None
    assert box.svc.institutional.patterns()                     # Andre's resolution was learned, and replayed
    assert _ok(box.c.post("/onboarding/clients/client_a/momentum"), 409)  # exited survives the restart


def test_creator_brand_campaign_and_playbook_survive_restarts(box):
    body = start_body("brand_1", lane="zbc_brand")
    body["contract"]["services"] = ["zbc_brand_campaign"]
    _ok(box.c.post("/onboarding/clients", json=body), 201)
    box.restart()
    p = _ok(box.c.post("/zbc/brands/brand_1/campaigns", json={"campaign_id": "camp_1", "regulated": False,
                                                               "wants_owned_addon": False,
                                                               "requested_budget_usd": "400.00"}), 201)
    box.restart()
    assert box.c.post("/zbc/brands/brand_1/campaigns", json={"campaign_id": "camp_1", "regulated": False,
                                                             "wants_owned_addon": False}).status_code == 409
    _ok(box.c.post("/zbc/brands/brand_1/campaigns/camp_1/proving-result",
                   json={"views_delivered": 1000, "clicks": 10, "evidence": "observed"}))
    box.restart()
    assert box.svc.campaigns[("brand_1", "camp_1")]["result"]["views_delivered"] == 1000
    assert box.svc.campaigns[("brand_1", "camp_1")]["plan"].scale_allowed is False and p["plan_digest"]
    text = "Always confirm the store currency before quoting numbers."
    _ok(box.c.post("/playbook/rules", json={"rule_id": "r1", "version": 1, "text": text,
                                            "approval_token": approval_token(ANDRE_KEY, "r1", 1, text)}))
    box.restart()
    assert [r["rule_id"] for r in _ok(box.c.get("/playbook"))["history"]] == ["r1"]
    # the version counter survived: v1 again is refused, v2 is the next
    assert box.c.post("/playbook/rules", json={"rule_id": "r1", "version": 1, "text": text,
                                               "approval_token": approval_token(ANDRE_KEY, "r1", 1, text)}
                      ).status_code == 403


def test_payments_are_accepted_after_a_restart_for_an_onboarded_creator_and_a_name_variant_cannot_reapply(box):
    _ok(box.c.post("/zbc/creators/applications", json=_app()), 201)
    _ok(box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "400.00"}))
    box.restart()
    # no re-application needed: the creator (activated, payout active) was rebuilt from the log
    r = _ok(box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p2", "amount_usd": "300.00"}))
    assert r["paid_to_date_usd"] == "700.00"
    # re-applying under the same creator id with a variant of the name is refused (it was let through before)
    assert box.c.post("/zbc/creators/applications",
                      json=_app(legal_name="Patricia Young")).status_code == 409
    assert box.svc.creators["clip_1"].application.legal_name == "Pat Young"
    _ok(box.c.post("/zbc/creators/clip_1/w9", json={"received": False}))
    box.restart()
    assert box.svc.creators["clip_1"].w9_on_file is False
    assert box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p3", "amount_usd": "1.00"}).status_code == 409


def test_replay_is_idempotent_restart_twice_gives_the_same_state_and_writes_nothing(box):
    _ok(box.c.post("/onboarding/clients", json=start_body()), 201)
    _ok(box.c.post("/zbc/creators/applications", json=_app()), 201)
    _ok(box.c.post("/onboarding/clients/client_a/intake/facts", json=FACTS))
    first = box.svc.state_snapshot()
    lines, events = len(box.svc.log), len(box.led.events)
    box.restart()
    box.restart()
    assert box.svc.state_snapshot() == first
    assert len(box.svc.log) == lines and len(box.led.events) == events     # a replay records nothing


def test_f2_a_w9_revocation_whose_anchor_failed_survives_a_crash_and_payments_stay_refused(box):
    """AEGIS F-2 repro: the revocation is recorded, its line's anchor fails (owed), the process dies. The line is
    written locally FIRST, so the restart keeps the revocation: payments stay refused (they were allowed again)."""
    _ok(box.c.post("/zbc/creators/applications", json=_app()), 201)
    box.led.fail_type, box.led.countdown = "log_anchor", 1      # the operation's line cannot be anchored
    r = box.c.post("/zbc/creators/clip_1/w9", json={"received": False})
    assert r.status_code == 503 and r.json()["evidence"] == "pending"
    box.restart(check=False)                                    # the process dies before the anchor is re-sent
    assert box.svc.creators["clip_1"].w9_on_file is False
    assert box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "5.00"}
                      ).status_code == 409
    ev = _ok(box.c.get("/onboarding/audit/evidence"))           # the start re-sent the anchor: committed now
    assert ev["counts"]["attempted"] == 0, ev


def _crash_before_the_local_line(box):
    """The ledger takes the operation's records, then the local append fails and the process dies."""
    box.svc.log.fail_next_append = True


def test_f2_a_revocation_whose_local_line_never_landed_holds_the_creator_until_andre_resolves(box):
    _ok(box.c.post("/zbc/creators/applications", json=_app()), 201)
    _crash_before_the_local_line(box)
    assert box.c.post("/zbc/creators/clip_1/w9", json={"received": False}).status_code == 503
    box.restart(check=False)
    assert box.svc.creators["clip_1"].w9_on_file is True        # the log never had it...
    r = box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "5.00"})
    assert r.status_code == 409 and r.json()["quarantined"] is True, r.text   # ...so the creator is held
    assert "creator_w9" in r.json()["event_types"]
    held = _ok(box.c.get("/onboarding/quarantine"))["quarantined"]
    assert [q["subject_id"] for q in held] == ["clip_1"]
    box.restart()                                               # held across restarts, not rescanned twice
    assert box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "5.00"}
                      ).status_code == 409
    assert box.c.post("/onboarding/quarantine/clip_1/resolve", json={"approval_token": "bad"}).status_code == 403
    tok = andre_action_token(ANDRE_KEY, "quarantine_resolve", "clip_1")
    _ok(box.c.post("/onboarding/quarantine/clip_1/resolve", json={"approval_token": tok}))
    _ok(box.c.post("/zbc/creators/clip_1/w9", json={"received": False}))       # Andre's reconciliation
    box.restart()
    assert box.svc.creators["clip_1"].w9_on_file is False and box.svc._quarantine == {}


def test_f2_a_creator_onboarding_lost_in_a_crash_cannot_be_reapplied_with_a_name_variant(box):
    _crash_before_the_local_line(box)
    assert box.c.post("/zbc/creators/applications", json=_app()).status_code in (201, 503)
    box.restart(check=False)
    assert "clip_1" not in box.svc.creators
    r = box.c.post("/zbc/creators/applications", json=_app(legal_name="Patricia Young"))
    assert r.status_code == 409 and r.json()["quarantined"] is True, r.text


def test_f2_a_failed_operation_leaves_an_attempt_line_so_its_events_never_hold_anyone(box):
    box.led.fail_type, box.led.countdown = "contract_storage_request", 1
    assert box.c.post("/onboarding/clients", json=start_body()).status_code == 503
    box.restart()
    assert box.svc._quarantine == {}
    _ok(box.c.post("/onboarding/clients", json=start_body()), 201)


def test_f3_start_waits_out_the_in_flight_grace_before_closing_an_intent_as_not_on_the_ledger(box):
    """A payment record still on its way to the ledger when the process stopped lands during the grace: counted."""
    _ok(box.c.post("/zbc/creators/applications", json=_app()), 201)
    led, svc = box.led, box.svc
    late = []

    orig = type(led).record_event

    def hold(self, event_id, department, event_type, *a, **k):
        if event_type == "creator_payment_tracked" and not late:
            late.append((event_id, department, event_type, a, k))
            raise LedgerWriteError("no reply (the request is still in flight)", "unknown")
        return orig(self, event_id, department, event_type, *a, **k)
    type(led).record_event = hold
    try:
        r = box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "7.00"})
        assert r.status_code == 503
    finally:
        type(led).record_event = orig
    svc.close()
    waited = []

    def sleep(s):                                   # the in-flight write lands while start-up waits
        waited.append(s)
        eid, dep, et, a, k = late[0]
        orig(led, eid, dep, et, *a, **k)
        box.clock.advance(seconds=s)
    box.svc = make_service(all_fakes=True, ledger=led, clock=box.clock, log=RecordLog(box.d), dir_lock=box.lock,
                           departments=box.depts, sleep=sleep)
    box.c = client_for(box.svc)
    assert waited and 0 < waited[0] <= 10
    assert _ok(box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p2", "amount_usd": "1.00"}))[
        "paid_to_date_usd"] == "8.00"


def test_a_partial_start_and_its_owed_result_record_survive_a_restart_and_the_retry_completes_it(box):
    """The contract was stored (outside effect), then its result record failed: the state that happened and the owed
    result record are in the partial line; after a restart the identical start writes that record (same id) and
    finishes, instead of a 409 or a second contract."""
    box.led.fail_type, box.led.countdown = "contract_storage_ruling", 1
    r = box.c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 503 and r.json()["proceeded"] is True
    owed = dict(box.svc._pending["client_a"])
    assert owed
    box.restart()
    assert box.svc._pending["client_a"] == owed and box.svc.clients["client_a"].start_view is not None
    _ok(box.c.post("/onboarding/clients", json=start_body()), 201)
    assert [e for e in box.led.events if e["event_id"] in owed]          # the owed record, under its own id
    assert box.svc.clients["client_a"].start_view is None
    box.restart()
    assert box.c.post("/onboarding/clients", json=start_body()).status_code == 409


def _log_with(tmp_path, kind, data):
    d = str(tmp_path / "bad")
    log = RecordLog(d)
    log.append(kind, "2026-10-09T00:00:00+00:00", data)
    return d


GOOD_SET = ["set", "creators", "clip_9", None]


@pytest.mark.parametrize("kind,data,why", [
    ("mystery", {"op": "x"}, "not a line kind"),
    ("op", {"op": "x", "surprise": 1}, "unknown field"),
    ("op", {"op": "x", "state": {"v": 2, "ops": []}}, "state version"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["set", "vaults", "k", None]]}}, "unknown state container"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["rename", "creators", "k"]]}}, "unknown state operation"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["set", "creators", "k", {"$zz": 1}]]}}, "unknown type tag"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["set", "creators", "k", {"$dc": ["os:system", []]}]]}},
     "unknown type"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["set", "creators", "k",
                                                  {"$dc": ["service:SoftIssue", [["evil", 1]]]}]]}}, "no field"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["set", "creators", "k", {"$dec": 5}]]}}, "malformed"),
    ("op", {"op": "x", "state": {"v": 1, "ops": [["set", "creators", "k", {"$d": [["a", 1], ["a", 2]]}]]}},
     "re-encode"),
])
def test_a_line_replay_cannot_interpret_refuses_start_with_its_line_number(tmp_path, kind, data, why):
    d = _log_with(tmp_path, kind, data)
    with pytest.raises(StateReplayError, match=why) as exc:
        make_service(all_fakes=True, log=RecordLog(d))
    assert "line 1" in str(exc.value) and "refusing to start" in str(exc.value)


def test_a_pre_wave_f_line_without_state_is_evidence_only(tmp_path):
    d = _log_with(tmp_path, "op", {"op": "apply_creator", "effects": [], "rk": "apply_creator", "evidence": []})
    svc = make_service(all_fakes=True, log=RecordLog(d))
    assert svc.creators == {} and svc.clients == {}
    svc.close()


def test_an_open_payment_intent_is_resolved_with_the_paged_filtered_read_never_the_whole_ledger(box):
    _ok(box.c.post("/zbc/creators/applications", json=_app()), 201)
    box.led.fail_type, box.led.countdown = "log_anchor", 2      # the intent anchors; the operation's line is owed
    r = box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p1", "amount_usd": "550.00"})
    assert r.status_code == 503
    box.led.whole_reads = 0
    box.restart(check=False)
    assert box.led.whole_reads == 0 and box.led.pages_read >= 1
    r = _ok(box.c.post("/zbc/creators/clip_1/payments", json={"request_id": "p2", "amount_usd": "1.00"}))
    assert r["paid_to_date_usd"] == "551.00"


def test_the_paged_read_stops_at_the_page_holding_every_wanted_event():
    led = FakeLedgerClient()
    for i in range(50):
        led.record_event(f"onb-{i:04d}", "onboarding", "creator_payment_tracked" if i % 2 else "first_message_sent",
                         "a", "s1", {"i": i}, "x")
    wanted = {"onb-0003"}
    got = led.entries_filtered("onboarding", "creator_payment_tracked", page_size=5, want=wanted)
    assert led.pages_read == 1 and wanted <= {e["event_id"] for e in got}
    led.pages_read = 0
    every = led.entries_filtered("onboarding", "creator_payment_tracked", page_size=5)
    assert len(every) == 25 and led.pages_read == 6


def test_the_http_client_uses_the_paged_query_and_falls_back_on_a_ledger_without_it():
    seen = []
    rows = [{"seq": i, "event_id": f"e{i}", "department": "onboarding", "event_type": "creator_payment_tracked"}
            for i in range(1, 4)]

    def paged(req):
        seen.append(dict(req.url.params))
        after = int(req.url.params.get("after_seq", 0))
        return httpx.Response(200, json=[r for r in rows if r["seq"] > after][:int(req.url.params["limit"])])
    cl = HttpLedgerClient("http://ledger", "t" * 40, transport=httpx.MockTransport(paged))
    assert [e["event_id"] for e in cl.entries_filtered("onboarding", "creator_payment_tracked", page_size=2)] == [
        "e1", "e2", "e3"]
    assert seen[0] == {"limit": "2", "department": "onboarding", "event_type": "creator_payment_tracked"}
    assert seen[1]["after_seq"] == "2"

    def old(req):
        if req.url.params:
            return httpx.Response(404)
        return httpx.Response(200, content=json.dumps(rows + [{"seq": 9, "department": "sales"}]))
    cl = HttpLedgerClient("http://ledger", "t" * 40, transport=httpx.MockTransport(old))
    assert len(cl.entries_filtered("onboarding", "creator_payment_tracked")) == 3
    assert select_paged(lambda p: rows, "onboarding", None, page_size=2) == rows   # a ledger ignoring the query


def test_the_tracker_preserves_insertion_order_catches_in_place_mutation_and_replays_to_identical_bytes():
    import onboarding_schema
    import service as service_mod
    from statelog import Codec, StateTracker

    class Owner:
        def __init__(self):
            self.d = {}

    def tracked():
        o = Owner()
        st = StateTracker(Codec([service_mod, onboarding_schema]))
        st.add_keyed("d", o, "d")
        st.baseline()
        return o, st

    live, st = tracked()
    lines = []

    def commit():
        delta, mark = st.delta()
        if delta is not None:
            lines.append(json.loads(json.dumps(delta)))      # what the log holds: plain JSON
        mark()
    live.d.get("b")                                          # a miss first, then inserted after "a"
    live.d["a"] = service_mod.SoftIssue("i1", service_mod.TriggerKind.DEAL_SIZE, "s", "a", service_mod._utc_now())
    live.d["b"] = [1, (2, 3), {"x": {4}}]
    commit()
    live.d.get("a").status = "resolved"                      # mutated in place after a get: caught
    commit()
    del live.d["a"]
    live.d["a"] = "again"                                    # deleted and set again: it moved to the end
    commit()
    for k in list(live.d):                                   # iteration marks everything (nothing changed: no line)
        pass
    commit()
    assert len(lines) == 3 and list(live.d) == ["b", "a"]
    for _ in range(2):                                       # replay twice: identical bytes, same order
        copy, st2 = tracked()
        for ln in lines:
            st2.apply(ln, "test")
        assert st2.snapshot() == st.snapshot() and list(copy.d) == ["b", "a"]
