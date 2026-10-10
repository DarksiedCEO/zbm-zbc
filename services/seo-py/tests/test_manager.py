"""Wave 2 stage 4: the department manager — per-agent work queues, scorecards built only from recorded runs and drift
reports, and the lifecycle with Andre-only moves into / out of restricted and into retired; a restricted agent is
refused by the run guard."""

from __future__ import annotations

import base64

import pytest

from fixture_server import home_html, install_site
from helpers import rid
from test_audits import audit, harness, own

pytestmark = pytest.mark.local_http


def move(h, agent, to, expected, andre=False, reason="QUALITY_REVIEW", caller="dashboard"):
    return h.post(f"/agents/{agent}/lifecycle", {"request_id": rid(), "to": to, "expected_state": expected,
                                                 "reason": reason}, caller=caller, andre=andre)


def setup(tmp_path, srv, **kw):
    install_site(srv)
    h = harness(tmp_path, srv, **kw)
    own(h)
    return h


def test_lifecycle_rules(tmp_path, srv):
    h = setup(tmp_path, srv)
    a = h.ok(move(h, "roman", "watch", "active"))
    assert a["state"] == "watch" and a["history"][0]["reason"] == "QUALITY_REVIEW"
    h.refused(move(h, "roman", "retired", "watch", andre=True), 409, "AGENT_MOVE_NOT_ALLOWED")
    h.refused(move(h, "roman", "retrain", "active"), 409, "AGENT_STATE_STALE")
    h.ok(move(h, "roman", "retrain", "watch"))
    h.refused(move(h, "roman", "restricted", "retrain"), 403, "ANDRE_APPROVAL_REQUIRED")
    r = h.ok(move(h, "roman", "restricted", "retrain", andre=True, reason="OVERTURN_RATE"))
    assert r["state"] == "restricted" and r["history"][-1]["by"] == "andre" and r["history"][-1]["scorecard_sha256"]
    h.refused(move(h, "roman", "active", "restricted"), 403, "ANDRE_APPROVAL_REQUIRED")   # leaving it is his too
    h.ok(move(h, "roman", "retired", "restricted", andre=True, reason="FOUNDER_DECISION"))
    h.refused(move(h, "roman", "active", "retired", andre=True), 409, "AGENT_MOVE_NOT_ALLOWED")    # terminal
    h.refused(move(h, "marcus", "watch", "active"), 422, "AGENT_UNKNOWN")
    h.refused(move(h, "delia", "watch", "active", reason="BECAUSE"), 422)
    h.refused(move(h, "delia", "watch", "active", caller="seo_agent"), 403, "CALLER_NOT_ALLOWED")
    ev = {(e["event_type"], e["status"]) for e in h.ok(h.get("/audit/evidence", caller="compliance_38"))["evidence"]}
    assert ("agent_lifecycle", "committed") in ev


def test_restricted_agent_is_refused_by_the_run_guard(tmp_path, srv):
    h = setup(tmp_path, srv)
    h.ok(move(h, "roman", "restricted", "active", andre=True, reason="FAILURE_RATE"))
    r = h.ok(audit(h), 201)["report"]
    roman = next(e for e in r["agents"] if e["agent"] == "roman")
    assert roman["outcome"] == "RESTRICTED" and roman["findings"] == [] and roman["reason"] == "AGENT_RESTRICTED"
    assert r["summary"]["agent_outcomes"]["selene"] == "OK" and r["outcome"] == "PARTIAL"
    h.ok(move(h, "selene", "restricted", "active", andre=True))
    r2 = h.ok(audit(h), 201)["report"]
    assert r2["summary"]["agent_outcomes"]["selene"] == "RESTRICTED"
    n = len(srv.seen)
    r3 = h.ok(audit(h), 201)
    assert len(srv.seen) == n and r3["report"]["pages"] == {}   # a restricted Selene fetches nothing


def test_restricted_selene_refuses_log_reports(tmp_path, srv):
    h = setup(tmp_path, srv)
    iid = h.ok(h.post("/tenants/zbm/log-ingests", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                                   "format": "combined"}), 201)["ingest_id"]
    h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/chunks",
                {"request_id": rid(), "seq": 1, "data_b64": base64.b64encode(b"x\n").decode(), "last": True}))
    h.ok(move(h, "selene", "restricted", "active", andre=True))
    h.refused(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}), 403, "AGENT_RESTRICTED")


def test_restriction_mid_run_stops_the_later_agents(tmp_path, srv):
    h = setup(tmp_path, srv)

    def home(handler):
        h.svc.move_agent("callum", {"request_id": rid(), "to": "restricted", "expected_state": "active",
                                    "reason": "FOUNDER_DECISION"}, andre=True)
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = home
    r = h.ok(audit(h), 201)["report"]
    assert r["summary"]["agent_outcomes"]["callum"] == "RESTRICTED"


def test_restricting_is_allowed_under_the_write_switch(tmp_path, srv):
    h = setup(tmp_path, srv)
    h.ok(h.switch("write"))
    h.refused(move(h, "delia", "watch", "active"), 403, "KILLED_WRITE")
    h.ok(move(h, "delia", "restricted", "active", andre=True))


def test_scorecards_and_queues_come_from_recorded_runs(tmp_path, srv):
    h = setup(tmp_path, srv)
    d0 = h.ok(h.get("/department", caller="compliance_38"))["agents"]
    assert d0["roman"]["scorecard"]["runs"] == 0 and d0["roman"]["scorecard"]["failure_rate"] is None
    sid = h.ok(h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                                 "paths": ["/", "/about"], "every_days": 7}), 201)["schedule_id"]
    assert h.ok(h.get("/department"))["agents"]["selene"]["queue"]["schedule_slots_due"] == 1
    h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    h.clock.advance(days=7)
    h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    d = h.ok(h.get("/department"))["agents"]
    sc = d["roman"]["scorecard"]
    assert sc["runs"] == 2 and sc["findings_produced"] > 0 and sc["findings_confirmed"] > 0
    assert sc["findings_overturned"] == 0 and sc["overturn_rate"] == 0.0 and sc["failure_rate"] == 0.0
    assert d["callum"]["scorecard"]["outcomes"] == {"INSUFFICIENT_EVIDENCE": 2}
    assert d["naomi"]["scorecard"]["not_connected_rate"] == 1.0
    assert d["osei"]["scorecard"]["runs"] == 2                   # its recorded data-hygiene envelopes
    assert d["osei"]["scorecard"]["drift_reports_recorded"] == 1
    assert d["selene"]["queue"] == {"audits_running": 0, "schedule_slots_due": 0, "log_ingests_open": 0}
    assert h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))["drifts"]
    h.refused(h.get("/department", caller="hub", tenant="zbm"), 403, "CALLER_NOT_ALLOWED")


def test_lifecycle_survives_restart(tmp_path, srv):
    h = setup(tmp_path, srv, data_dir=str(tmp_path / "data"))
    h.ok(move(h, "delia", "restricted", "active", andre=True))
    h2 = h.restart()
    assert h2.ok(h2.get("/department"))["agents"]["delia"]["state"] == "restricted"
    assert h2.svc.agent_blocked("delia")
