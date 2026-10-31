"""AEGIS compliance-gate review (REVISE @03421e3): the activation status re-evaluates on every call.

F-1: a change to any rule the ruling relied on (its content, not only its status) and the CURRENT service area
     (HR-05 operate, HR-07 refuse) applied to the client's recorded jurisdiction and campaign targets.
F-2: an open hold on the client refuses; a released (releasable) hold no longer does.
F-3: an activation rule the register gained since the ruling refuses; a re-verification of a rule it relied on
     does not.
F-7: an OFAC record counts only for the lane it was recorded for.
F-9: only the departments that act on the answer read it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import helpers as H
from helpers import ANDRE_TOKEN, rid
from test_service_area_gate import activate, andre_view, attest, harness, status, web_client


def _row(h, oid):
    return dict(h.svc.current.by_id()[oid])


def _amend(h, oid, **params):
    row = _row(h, oid)
    new = {**row, "verified_at": h.clock.today().isoformat(), "parameters": {**row["parameters"], **params}}
    p = h.propose({"kind": "amend", "target_id": oid, "proposed_row": new})
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"], acknowledge_weakening=p.json()["proposal"]["weakening"])


# --- F-1: the service area in force now, and the content of every rule relied on ---------------------------------------

def test_a_client_removed_from_the_service_area_is_refused_at_once():
    h = harness()
    assert activate(h, "client-tx", region="US-TX")["allowed"] is True
    assert status(h, "client-tx")["decision"] == "ALLOW"
    hr05 = _row(h, "HR-05")["parameters"]["operate"]
    _amend(h, "HR-05", operate=[c for c in hr05 if c != "US-TX"])
    s = status(h, "client-tx")
    assert s["decision"] == "REFUSE" and s["current"] is False
    assert any(x.startswith("jurisdiction US-TX is refuse") for x in s["reasons"]), s["reasons"]
    # AEGIS r2 N-1: the live area check refuses; HR-05's parameters are not in the fingerprint
    assert not any(x.startswith("HR-05 changed since the ruling") for x in s["reasons"]), s["reasons"]


def test_a_client_in_a_newly_refused_region_is_refused_at_once():
    h = harness()
    assert activate(h, "client-fl", region="US-FL")["allowed"] is True
    _amend(h, "HR-07", refuse=[*_row(h, "HR-07")["parameters"]["refuse"], "US-FL"])
    s = status(h, "client-fl")
    assert s["decision"] == "REFUSE"
    assert any(x.startswith("jurisdiction US-FL is refuse") for x in s["reasons"]), s["reasons"]
    assert not any(x.startswith("HR-07 changed since the ruling") for x in s["reasons"]), s["reasons"]


def test_a_campaign_target_removed_from_the_service_area_refuses():
    h = harness()
    assert activate(h, "client-pr", country="PR", region=None, targets=("PR",))["allowed"] is True
    _amend(h, "HR-05", operate=[c for c in _row(h, "HR-05")["parameters"]["operate"] if c not in ("PR", "US-PR")])
    s = status(h, "client-pr")
    assert s["decision"] == "REFUSE" and any("target PR" in x for x in s["reasons"]), s["reasons"]


def test_the_live_area_check_holds_even_if_the_fingerprints_are_lost():
    """(b) does not depend on (a): with no recorded fingerprints, the status still re-classifies the jurisdiction."""
    h = harness()
    activate(h, "client-tx", region="US-TX")
    r = h.svc.rulings[h.svc.latest_activation[("client", "client-tx")]]
    r.pop("rule_fingerprints", None)
    s = status(h, "client-tx")
    assert s["decision"] == "REFUSE" and any("fingerprint" in x for x in s["reasons"]), s["reasons"]
    r["rule_fingerprints"] = {}
    _amend(h, "HR-05", operate=[c for c in _row(h, "HR-05")["parameters"]["operate"] if c != "US-TX"])
    assert any(x.startswith("jurisdiction US-TX is refuse") for x in status(h, "client-tx")["reasons"])


def test_the_ruling_records_a_fingerprint_of_every_rule_it_relied_on():
    h = harness()
    activate(h, "client-web")
    r = h.svc.rulings[h.svc.latest_activation[("client", "client-web")]]
    assert set(r["rule_fingerprints"]) == set(r["row_ids_evaluated"])
    assert all(len(v) == 64 for v in r["rule_fingerprints"].values())


# --- F-2: holds -------------------------------------------------------------------------------------------------------

def test_an_ofac_hit_after_activation_refuses_the_client():
    h = harness()
    activate(h, "client-web")
    a = attest(h, "client-web", result="potential_match")
    assert a.status_code == 200 and a.json()["hold_id"]
    s = status(h)
    assert s["decision"] == "REFUSE"
    assert f"hold open: sanctions_match ({a.json()['hold_id']})" in s["reasons"], s["reasons"]


def test_an_open_non_sanctions_hold_refuses_and_its_release_allows_again():
    h = harness()
    activate(h, "client-web")
    # a signal mismatch seen on another lane opens a releasable hold on the same subject
    f = web_client(h, "client-web", network_country_signal="CA")
    h.rule("client-web", "zbc_brand", f)
    holds = [x for x in h.svc.holds.values() if x["subject_id"] == "client-web" and x["status"] == "open"]
    assert [x["kind"] for x in holds] == ["jurisdiction_signal_mismatch"]
    s = status(h)
    assert s["decision"] == "REFUSE" and f"hold open: jurisdiction_signal_mismatch ({holds[0]['hold_id']})" in s["reasons"]
    rel = h.post(f"/compliance/v1/holds/{holds[0]['hold_id']}/release",
                 {"request_id": rid("rel"), "reason": "travelling: declared US-CA confirmed by Andre"}, andre=ANDRE_TOKEN)
    assert rel.status_code == 200, rel.text
    assert status(h)["decision"] == "ALLOW", status(h)["reasons"]


def test_a_hold_on_another_subject_does_not_refuse_this_client():
    h = harness()
    activate(h, "client-web")
    attest(h, "client-other", result="match")
    assert status(h)["decision"] == "ALLOW"


# --- F-3: rules gained since the ruling vs a re-verification ------------------------------------------------------------

def test_a_new_activation_rule_refuses_until_re_activation():
    h = harness()
    activate(h, "client-web")
    base = _row(h, "HR-15")
    new = {**base, "id": "HR-16", "title": "Client: one more founder rule", "verified_at": h.clock.today().isoformat(),
           "gates": ["activation"]}
    p = h.propose({"kind": "new", "proposed_row": new})
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"])
    s = status(h)
    assert s["decision"] == "REFUSE"
    assert any(x.startswith("the register gained activation rule(s) HR-16") for x in s["reasons"]), s["reasons"]


def test_a_re_verification_of_a_rule_relied_on_keeps_the_client_allowed():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reverify_proposal", Path(__file__).resolve().parents[1] / "devtools" / "reverify_proposal.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    at = datetime(2026, 12, 20, 15, 0, tzinfo=timezone.utc)
    h = harness(at=at)
    activate(h, "client-web")
    assert status(h)["decision"] == "ALLOW"
    row = h.get("/compliance/v1/register/US-OFAC-04").json()["row"]
    p = h.post("/compliance/v1/register/proposals",
               tool.build(row, b"<p>Comprehensive sanctions: Cuba, Iran, North Korea</p>",
                          "Comprehensive sanctions: Cuba, Iran, North Korea", at), andre=ANDRE_TOKEN)
    assert p.status_code == 201, p.text
    h.approve(p.json()["proposal"])
    s = status(h)
    assert s["decision"] == "ALLOW", s["reasons"]
    h.clock.at = datetime(2026, 12, 26, 12, 0, tzinfo=timezone.utc)   # past the old expiry: re-verified, still in force
    h.run_controls()
    assert status(h)["decision"] == "ALLOW", status(h)["reasons"]


# --- F-7: the OFAC record is tied to the lane ---------------------------------------------------------------------------

def test_an_ofac_record_for_another_lane_does_not_count():
    h = harness()
    a = attest(h, "client-lane", lane="zbc_brand")
    assert a.status_code == 200, a.text
    f = web_client(h, "client-lane", attest_first=False, sanctions_attestation_id=a.json()["attestation_id"])
    j = h.rule("client-lane", "client", f).json()
    full = andre_view(h, j)                                       # R9-4: the HR-14 detail is Andre's record
    assert j["allowed"] is False and any("lane" in x for x in full["unmet_lines"]), full["unmet_lines"]


# --- F-9: who reads the activation status --------------------------------------------------------------------------------

def test_only_the_departments_that_act_on_it_read_the_activation_status():
    h = harness()
    activate(h, "client-web")
    for caller in ("finance_31", "seo_02", "revenue_recovery", "onboarding"):
        assert status(h, caller=caller)["decision"] == "ALLOW"
    for caller in sorted(set(H.CALLERS) - {"finance_31", "seo_02", "revenue_recovery", "onboarding"}):
        assert h.get("/compliance/v1/clients/client-web/activation", caller=caller).status_code == 403, caller


# --- F-4: the scheduler survives any error and its liveness is in /health ----------------------------------------------

def _sched(h, lines=None):
    import scheduler
    s = scheduler.Scheduler(h.svc, interval_s=21600, watcher=False, clock=h.clock,
                            out=(lines.append if lines is not None else (lambda _x: None)))
    h.svc.scheduler = s
    return s


def test_an_error_outside_the_run_does_not_kill_the_scheduler_thread(monkeypatch):
    import threading

    import scheduler
    h = harness()
    lines: list[str] = []
    s = _sched(h, lines)
    calls = {"n": 0}
    real = h.svc.readiness

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("readiness exploded")
        return real()

    monkeypatch.setattr(h.svc, "readiness", flaky)
    monkeypatch.setattr(scheduler, "TICK_S", 0.01)
    ticks = threading.Event()
    orig_tick = s.tick

    def counted():
        orig_tick()
        if calls["n"] >= 1:
            s.last_controls = None          # force another controls run so readiness is called again
        if calls["n"] >= 2:
            ticks.set()

    monkeypatch.setattr(s, "tick", counted)
    s.start()
    try:
        assert ticks.wait(5), lines
        assert s.alive() is True
        assert any("tick failed" in x and "RuntimeError" in x for x in lines), lines
        assert s.last_error and s.last_error["error"] == "RuntimeError"
    finally:
        s.stop()


def test_health_reports_scheduler_liveness_and_turns_503_when_controls_are_stale():
    from datetime import timedelta
    h = harness()
    assert h.client.get("/health").json()["scheduler"] == {"enabled": False}
    s = _sched(h)
    s.started_at = h.clock.at
    s._thread = _FakeThread(True)                        # no real thread in this test
    s.tick()
    r = h.client.get("/health")
    assert r.status_code == 200, r.text
    sch = r.json()["scheduler"]
    assert sch["enabled"] is True and sch["healthy"] is True and sch["last_controls_ok_at"] == iso_(h.clock.at)
    # controls keep failing for more than two intervals: unhealthy (503) so the platform / a monitor sees it
    h.svc.run_internal_controls = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger down"))
    h.clock.at = h.clock.at + timedelta(hours=12, seconds=1)
    s.tick()
    r = h.client.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "unhealthy"
    sch = r.json()["scheduler"]
    assert sch["healthy"] is False and "controls" in sch["reason"] and sch["last_error"]["where"] == "controls"
    # a dead thread is unhealthy at once
    h2 = harness()
    s2 = _sched(h2)
    s2.started_at = h2.clock.at
    s2._thread = _FakeThread(False)
    assert h2.client.get("/health").status_code == 503


class _FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def iso_(dt):
    from clock import iso
    return iso(dt)


# --- F-13: HR-07's text names what its parameters refuse -----------------------------------------------------------------

def test_hr07_text_names_every_region_it_refuses():
    hr07 = next(r for r in H.SEED_ROWS if r["id"] == "HR-07")
    for code in hr07["parameters"]["refuse"]:
        if "-" in code:
            assert f"{code}" in hr07["obligation"], code
    for name in ("Cuba", "Iran", "North Korea", "Crimea", "Sevastopol", "DNR/LNR", "Zaporizhzhia", "Kherson"):
        assert name in hr07["obligation"], name


# --- F-4 (coordinator decision): a dead thread exits the process; stale controls never do ----------------------------

class _Fatal(BaseException):
    """Something no `except Exception` catches (the only way the loop can still end)."""


def test_a_dead_scheduler_thread_exits_the_process_non_zero(monkeypatch):
    import threading

    import scheduler
    h = harness()
    lines: list[str] = []
    exited = threading.Event()
    codes: list[int] = []

    def on_dead(code):
        codes.append(code)
        exited.set()

    s = scheduler.Scheduler(h.svc, interval_s=21600, watcher=False, clock=h.clock, out=lines.append,
                            on_thread_death=on_dead)
    monkeypatch.setattr(s, "tick", lambda: (_ for _ in ()).throw(_Fatal("boom")))
    s.start()
    assert exited.wait(5), lines
    assert codes == [1]
    assert any("scheduler thread stopped unexpectedly" in x and "_Fatal" in x for x in lines), lines


def test_a_stopped_scheduler_does_not_exit_the_process(monkeypatch):
    import scheduler
    h = harness()
    codes: list[int] = []
    monkeypatch.setattr(scheduler, "TICK_S", 0.01)
    s = scheduler.Scheduler(h.svc, interval_s=21600, watcher=False, clock=h.clock, out=lambda _x: None,
                            on_thread_death=codes.append)
    s.start()
    s.stop()
    s._thread.join(5)
    assert not s.alive() and codes == []


def test_stale_controls_never_exit_the_process_they_turn_health_503(monkeypatch):
    """Ledger down: controls fail on every tick. No restart loop: the thread stays up, /health is 503, C-11 closes gates."""
    import threading
    from datetime import timedelta

    import scheduler
    h = harness()
    codes: list[int] = []
    monkeypatch.setattr(scheduler, "TICK_S", 0.01)
    h.svc.run_internal_controls = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger down"))
    s = scheduler.Scheduler(h.svc, interval_s=21600, watcher=False, clock=h.clock, out=lambda _x: None,
                            on_thread_death=codes.append)
    h.svc.scheduler = s
    s.start()
    try:
        h.clock.at = h.clock.at + timedelta(hours=12, seconds=1)
        ok = threading.Event()
        for _ in range(500):
            if s.last_error and s.last_error["where"] == "controls" and h.client.get("/health").status_code == 503:
                ok.set()
                break
            threading.Event().wait(0.01)
        assert ok.is_set()
        assert s.alive() and codes == []
    finally:
        s.stop()


# --- AEGIS r2 N-1: a service-area amendment refuses only the clients it takes out --------------------------------------

def test_widening_or_reordering_the_service_area_keeps_existing_clients_allowed():
    h = harness()
    activate(h, "client-ca")
    activate(h, "client-fl", region="US-FL")
    _amend(h, "HR-05", operate=[*_row(h, "HR-05")["parameters"]["operate"], "GB"])          # a widening
    assert status(h, "client-ca")["decision"] == "ALLOW", status(h, "client-ca")["reasons"]
    _amend(h, "HR-05", operate=list(reversed(_row(h, "HR-05")["parameters"]["operate"])))  # same set, other order
    _amend(h, "HR-07", refuse=[*_row(h, "HR-07")["parameters"]["refuse"], "US-TX"])        # refuses someone else
    for c in ("client-ca", "client-fl"):
        assert status(h, c)["decision"] == "ALLOW", status(h, c)["reasons"]


def test_a_change_to_the_area_rows_text_or_to_another_rule_still_forces_re_activation():
    from service import rule_fingerprint
    for oid in ("HR-05", "HR-07"):
        row = next(r for r in H.SEED_ROWS if r["id"] == oid)
        fp = rule_fingerprint(row)
        assert rule_fingerprint({**row, "parameters": {}}) == fp                  # parameters: the live check's job
        assert rule_fingerprint({**row, "obligation": row["obligation"] + " x"}) != fp
        assert rule_fingerprint({**row, "gates": ["activation"]}) != fp
    other = next(r for r in H.SEED_ROWS if r["id"] == "HR-14")
    assert rule_fingerprint({**other, "parameters": {}}) != rule_fingerprint(other)
    h2 = harness()
    activate(h2, "client-ca")
    _amend(h2, "HR-14", rescreen_after_days=60)
    s = status(h2, "client-ca")
    assert s["decision"] == "REFUSE" and any(x.startswith("HR-14 changed since the ruling") for x in s["reasons"])


# --- AEGIS r2 N-3: api.main's scheduler wiring ------------------------------------------------------------------------

def test_main_wires_os_exit_as_the_hook_and_attaches_the_scheduler(monkeypatch):
    import os

    import api
    import scheduler
    h = harness()
    started: list = []
    monkeypatch.setattr(scheduler.Scheduler, "start", lambda self: started.append(self) or self)

    class S:
        scheduler_enabled, scheduler_interval_s, watcher_enabled = True, 21600, False

    s = api.start_scheduler(h.svc, S())
    assert started == [s] and h.svc.scheduler is s
    assert s.on_thread_death is os._exit
    S.scheduler_enabled = False
    h.svc.scheduler = None
    assert api.start_scheduler(h.svc, S()) is None and h.svc.scheduler is None


# --- AEGIS r2 N-8: /health shows the error class only ------------------------------------------------------------------

def test_health_never_shows_exception_text(monkeypatch):
    h = harness()
    s = _sched(h)
    s.started_at = h.clock.at
    s._thread = _FakeThread(True)
    monkeypatch.setattr(h.svc, "readiness", lambda: (_ for _ in ()).throw(RuntimeError("secret-detail /data/x")))
    s.tick()
    h.svc.run_internal_controls = lambda *_a, **_k: (_ for _ in ()).throw(ValueError("token=abc123"))
    s.last_controls = None
    s.tick()
    body = h.client.get("/health").text
    assert "secret-detail" not in body and "token=abc123" not in body and "/data/x" not in body
    assert h.client.get("/health").json()["scheduler"]["last_error"]["error"] == "ValueError"


# --- AEGIS r2 N-7 and follow-up: HR-07's wording; every house rule can be amended by Andre -----------------------------

def test_hr07_cites_us_ofac_04s_reading_for_ua23_and_ua65():
    hr07 = next(r for r in H.SEED_ROWS if r["id"] == "HR-07")
    # founder decision, Oct 30 16:04: the service area is the US only; HR-07 is a sanctions refuse list, not a choice
    text = hr07["obligation"]
    assert "founder" not in text.lower() and "pending" not in text.lower() and "andre" not in text.lower()
    assert "United States only" in text and "defence in depth" in text
    assert "included under US-OFAC-04's \"other occupied regions\" reading" in text


def test_every_house_rule_has_no_source_url_so_andre_can_amend_it():
    for r in H.SEED_ROWS:
        if r["source_kind"] == "house-rule":
            assert r["source_quality"] == "founder" and r["source_url"] is None, r["id"]


# --- AEGIS r3 N-11: main() starts the scheduler -------------------------------------------------------------------------

def test_main_starts_the_scheduler_before_serving(monkeypatch):
    import api
    import serve
    h = harness()
    order: list = []
    h.app.state.settings = h.settings          # what _app_from_env sets on the deployed app
    monkeypatch.setattr(api, "app", h.app)
    monkeypatch.setattr(api, "start_scheduler", lambda svc, settings: order.append(("start", svc, settings)))
    monkeypatch.setattr(serve, "run", lambda app, host, port: order.append(("serve", app)))
    api.main()
    assert [x[0] for x in order] == ["start", "serve"]
    assert order[0][1] is h.svc and order[0][2] is h.settings


# --- AEGIS r5 C1: the status says when a refusal is grounded in sanctions (Finance must not refund those) ---------------

def test_a_sanctions_hit_marks_the_refusal_as_sanctions_grounded():
    h = harness()
    activate(h, "client-web")
    assert status(h)["sanctions_grounded"] is False
    attest(h, "client-web", result="potential_match")
    s = status(h)
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is True


def test_a_sanctioned_region_is_sanctions_grounded_but_an_unlisted_one_is_not():
    h = harness()
    activate(h, "client-fl", region="US-FL")
    activate(h, "client-tx", region="US-TX")
    _amend(h, "HR-07", refuse=[*_row(h, "HR-07")["parameters"]["refuse"], "US-FL"])       # on the sanctions list
    _amend(h, "HR-05", operate=[c for c in _row(h, "HR-05")["parameters"]["operate"] if c != "US-TX"])  # out of area
    fl, tx = status(h, "client-fl"), status(h, "client-tx")
    assert fl["decision"] == "REFUSE" and fl["sanctions_grounded"] is True
    assert tx["decision"] == "REFUSE" and tx["sanctions_grounded"] is False


def test_other_refusals_are_not_sanctions_grounded():
    from datetime import timedelta
    h = harness()
    activate(h, "client-web")
    h.clock.at = h.clock.at + timedelta(days=91)                  # re-screen due: not a sanctions finding
    s = status(h)
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is False
    assert status(h, "client-none")["sanctions_grounded"] is False


# --- AEGIS r6 N1, N6: sanctions_grounded holds even when the latest ruling itself refused -------------------------------

def test_a_refused_re_activation_with_a_sanctioned_target_is_sanctions_grounded():
    h = harness()
    activate(h, "client-web")
    j = h.rule("client-web", "client", web_client(h, "client-web", targets=("US", "IR"))).json()
    assert j["allowed"] is False
    s = status(h)
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is True, s


def test_a_client_in_a_sanctioned_country_is_sanctions_grounded():
    h = harness()
    j = h.rule("client-cu", "client", web_client(h, "client-cu", country="CU", region=None, targets=("CU",))).json()
    assert j["allowed"] is False
    s = status(h, "client-cu")
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is True, s


def test_a_refusal_outside_the_area_but_not_sanctioned_is_not_sanctions_grounded():
    h = harness()
    h.rule("client-fr", "client", web_client(h, "client-fr", country="FR", region=None, targets=("FR",)))
    s = status(h, "client-fr")
    assert s["decision"] == "REFUSE" and s["sanctions_grounded"] is False, s


def test_a_non_sanctions_hold_is_not_sanctions_grounded():
    h = harness()
    activate(h, "client-web")
    h.rule("client-web", "zbc_brand", web_client(h, "client-web", network_country_signal="CA"))
    s = status(h)
    assert s["decision"] == "REFUSE" and any("jurisdiction_signal_mismatch" in x for x in s["reasons"])
    assert s["sanctions_grounded"] is False


# --- AEGIS r8: the sanctions ground: an OFAC list match, or a sanctioned place --------------------------------------------

def test_the_sanctions_ground_is_match_or_place_and_match_wins():
    h = harness()
    activate(h, "client-web")
    assert status(h)["sanctions_ground"] is None
    attest(h, "client-web", result="match")
    assert status(h)["sanctions_ground"] == "match"
    h2 = harness()
    h2.rule("client-cu", "client", web_client(h2, "client-cu", country="CU", region=None, targets=("CU",)))
    assert status(h2, "client-cu")["sanctions_ground"] == "place"
    attest(h2, "client-cu", result="potential_match")
    assert status(h2, "client-cu")["sanctions_ground"] == "match"           # both apply: match wins
    h3 = harness()
    h3.rule("client-fr", "client", web_client(h3, "client-fr", country="FR", region=None, targets=("FR",)))
    assert status(h3, "client-fr")["sanctions_ground"] is None              # outside the area, not sanctioned


# --- money red team RTM-06: early warning before the rules client activation relies on expire ---------------------------

def test_readiness_and_health_warn_30_14_and_7_days_before_a_rule_expires():
    from datetime import datetime, timezone
    h = harness(at=datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc))         # 25 days before US-OFAC-04 expires
    r = h.get("/compliance/v1/readiness").json()
    w = r["expiry_warnings"]
    assert "US-OFAC-04" in w["within_30_days"] and "US-OFAC-04" not in w["within_14_days"]
    assert w["client_activation_rules"]["within_30_days"] == ["US-OFAC-04"]
    health = h.client.get("/health").json()["rules_expiring"]
    assert health["within_30_days"] >= 1 and health["within_7_days"] == 0
    assert health["next_client_activation_rule"] == {"id": "US-OFAC-04", "expires_at": "2026-12-25"}
    h.clock.at = datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc)            # 5 days before
    h.run_controls()
    w = h.get("/compliance/v1/readiness").json()["expiry_warnings"]
    assert "US-OFAC-04" in w["within_7_days"] and w["client_activation_rules"]["within_7_days"] == ["US-OFAC-04"]
    assert h.client.get("/health").json()["rules_expiring"]["within_7_days"] >= 1
    assert h.client.get("/health").status_code == 200                           # a warning, not an outage
    import readiness
    text = "\n".join(readiness.summary_lines(h.svc.readiness()))
    assert "WARNING" in text and "US-OFAC-04 expires 2026-12-25 (in 5 days)" in text


# --- money red team RTM-11: identity is checked before the body is validated (no schema hints to the unauthorized) -----

def test_every_post_route_checks_identity_before_the_body():
    import re
    from fastapi.routing import APIRoute
    h = harness()
    checked = 0
    for route in h.app.routes:
        if not isinstance(route, APIRoute) or "POST" not in route.methods:
            continue
        path = re.sub(r"\{[^}]+\}", "x-1", route.path.replace("{control_id}", "C-01"))
        # the service bearer only: no caller token, no Andre token; a body that fails validation
        r = h.client.post(path, json={"bogus": 1}, headers={"Authorization": f"Bearer {H.SERVICE_TOKEN}"})
        assert r.status_code in (401, 403), (route.path, r.status_code, r.text[:160])
        checked += 1
    assert checked >= 14


# --- AEGIS r9 R9-4: only Finance (the money gate) and Andre see why a client is refused ----------------------------------

def _activation_as(h, caller=None, andre=None, client="client-web"):
    return h.client.get(f"/compliance/v1/clients/{client}/activation", headers=h.headers(caller, andre=andre))


def test_r9_4_the_reason_and_sanctions_ground_go_only_to_finance_and_andre():
    h = harness()
    activate(h, "client-web")
    attest(h, "client-web", result="potential_match")             # an OFAC hit after activation: a sanctions match
    full = status(h, caller="finance_31")
    assert full["sanctions_grounded"] is True and full["sanctions_ground"] == "match"
    assert any("sanctions_match" in x for x in full["reasons"])
    mine = _activation_as(h, andre=ANDRE_TOKEN)
    assert mine.status_code == 200 and mine.json() == full, mine.text
    generic = None
    for caller in ("seo_02", "revenue_recovery", "onboarding"):
        d = status(h, caller=caller)
        assert d["decision"] == "REFUSE" and d["current"] is False
        assert d["sanctions_grounded"] is None and d["sanctions_ground"] is None, d
        values = str({k: v for k, v in d.items() if k not in ("sanctions_grounded", "sanctions_ground")}).lower()
        assert "sanction" not in values and "ofac" not in values and "match" not in values, d
        assert d["reasons"] == ["the client is not activated (the reasons go to Andre)"], d
        generic = generic or d
        assert d == generic                                          # one answer for every such caller
    # a refusal for another reason looks exactly the same to them
    h2 = harness()
    activate(h2, "client-web", country="CA", region="CA-ON")
    other = status(h2, caller="onboarding")
    assert sorted(other) == sorted(generic) and other["reasons"] == generic["reasons"]
    assert other["sanctions_grounded"] is None and other["sanctions_ground"] is None


def test_r9_4_an_allow_is_unchanged_for_every_reader():
    h = harness()
    activate(h, "client-web")
    full = status(h, caller="finance_31")
    for caller in ("seo_02", "revenue_recovery", "onboarding"):
        d = status(h, caller=caller)
        assert d["decision"] == "ALLOW" and d["ruling_id"] == full["ruling_id"] and d["reasons"] == []


def test_r9_4_a_wrong_andre_token_is_refused():
    h = harness()
    activate(h, "client-web")
    assert _activation_as(h, andre="x" * 40).status_code in (401, 403)
