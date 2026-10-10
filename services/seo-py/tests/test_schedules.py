"""Wave 2 stage 3: scheduled re-audits — one audit per schedule per slot (duplicate and restarted ticks are safe),
Andre + invoice for paying clients, per-tenant budget caps, kill switches before and during a run, pausing, a drift
report between consecutive runs, and cross-tenant attempts on every schedule route."""

from __future__ import annotations

import pytest

from fixture_server import home_html, install_site
from helpers import invoice_id, rid
from test_audits import harness, own

pytestmark = pytest.mark.local_http


def schedule(h, tid="zbm", every=7, caller="seo_agent", andre=False, **extra):
    body = {"request_id": rid(), "domain": "site.test", "scheme": "http", "paths": ["/", "/about"],
            "every_days": every, **extra}
    return h.post(f"/tenants/{tid}/schedules", body, caller=caller, andre=andre)


def tick(h, request_id=None):
    return h.ok(h.post("/jobs/schedule-tick/run", {"request_id": request_id or rid()}, caller="scheduler"))


def setup(tmp_path, srv, **env):
    install_site(srv)
    h = harness(tmp_path, srv, **env)
    own(h)
    return h


def test_tick_runs_once_per_slot_and_records_drift(tmp_path, srv):
    h = setup(tmp_path, srv)
    s = h.ok(schedule(h), 201)
    sid = s["schedule_id"]
    assert tick(h)["ran"] == 1
    t2 = tick(h)
    assert t2["ran"] == 0 and t2["already_done"] == 1                     # a duplicate tick does nothing
    rq = rid()
    tick(h, rq)
    assert tick(h, rq)["already_ran"] is True                             # the same tick replayed
    h.clock.advance(days=7)
    srv.html("site.test", "/", home_html(canonical=None))
    t3 = tick(h)
    assert t3["ran"] == 1 and t3["drift_recorded"] == 1
    v = h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))
    assert [x["status"] for x in v["slots"].values()] == ["completed", "completed"]
    d = v["drifts"][-1]
    assert d["counts"]["REAL_CHANGE"] >= 1 and d["drift"]["rules_version"]
    ev = {(e["event_type"], e["status"]) for e in h.ok(h.get("/audit/evidence", caller="compliance_38"))["evidence"]}
    assert ("drift_recorded", "committed") in ev and ("schedule_created", "committed") in ev


def test_paying_client_needs_andre_and_invoice(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    h.tenant("acme", domains=("site.test",))
    h.refused(schedule(h, "acme"), 409, "INVOICE_REQUIRED")
    h.refused(schedule(h, "acme", invoice_id=invoice_id()), 403, "ANDRE_APPROVAL_REQUIRED")
    s = h.ok(schedule(h, "acme", caller="dashboard", andre=True, invoice_id=invoice_id()), 201)
    assert s["andre_approved"] is True
    tick(h)
    (a,) = h.ok(h.get("/tenants/acme/audits"))
    assert a["invoice_id"] == invoice_id() and a["andre_approved"] is True and a["schedule_id"] == s["schedule_id"]
    h.ok(h.post(f"/tenants/acme/schedules/{s['schedule_id']}/status", {"request_id": rid(), "status": "paused"}))
    h.refused(h.post(f"/tenants/acme/schedules/{s['schedule_id']}/status", {"request_id": rid(), "status": "active"}),
              403, "ANDRE_APPROVAL_REQUIRED")
    h.ok(h.post(f"/tenants/acme/schedules/{s['schedule_id']}/status", {"request_id": rid(), "status": "active"},
                andre=True))
    own(h, ("other.test",))
    h.refused(schedule(h, invoice_id=invoice_id(), domain="other.test"), 422, "INVOICE_NOT_FOR_OWN_TENANT")


def test_budget_cap_per_tenant_per_period(tmp_path, srv):
    h = setup(tmp_path, srv, SEO_SCHEDULE_BUDGET_RUNS="2", SEO_SCHEDULE_PERIOD_DAYS="10")
    sid = h.ok(schedule(h, every=1), 201)["schedule_id"]
    for _ in range(2):
        assert tick(h)["ran"] == 1
        h.clock.advance(days=1)
    t = tick(h)
    assert t["budget_exhausted"] == 1 and t["ran"] == 0
    assert tick(h)["already_done"] == 1                                     # the skipped slot is consumed
    v = h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))
    assert list(v["slots"].values())[-1] == {**list(v["slots"].values())[-1], "status": "skipped",
                                              "reason": "BUDGET_EXHAUSTED"}
    h.clock.advance(days=10)
    assert tick(h)["ran"] == 1                                              # a new window


def test_kill_switches_before_and_during_a_run(tmp_path, srv):
    h = setup(tmp_path, srv)
    sid = h.ok(schedule(h), 201)["schedule_id"]
    h.ok(h.switch("capability:schedules"))
    t = tick(h)
    assert t["killed"] == 1 and h.ok(h.get("/tenants/zbm/audits")) == []
    h.ok(h.switch("capability:schedules", engaged=False, andre=True))

    def home(handler):
        h.svc.set_switch("dashboard", {"request_id": rid(), "switch": "tenant:zbm", "engaged": True}, andre=False)
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = home
    assert tick(h)["ran"] == 1
    v = h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))
    assert [x["status"] for x in v["slots"].values()] == ["interrupted"]
    assert not any(s["path"] == "/about" for s in srv.seen)


def test_paused_schedule_does_not_run(tmp_path, srv):
    h = setup(tmp_path, srv)
    sid = h.ok(schedule(h), 201)["schedule_id"]
    h.ok(h.post(f"/tenants/zbm/schedules/{sid}/status", {"request_id": rid(), "status": "paused"}))
    assert tick(h)["paused"] == 1
    h.refused(h.post(f"/tenants/zbm/schedules/{sid}/status", {"request_id": rid(), "status": "paused"}), 409,
              "SCHEDULE_STATE")


def test_restart_mid_run_never_reruns_the_slot(tmp_path, srv):
    h = setup(tmp_path, srv, data_dir=str(tmp_path / "data"))
    sid = h.ok(schedule(h), 201)["schedule_id"]

    def home(handler):
        h.ledger.fail = True                                                # the completion record will be lost
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = home
    r = h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler")
    assert r.status_code == 503
    h.ledger.fail = False
    h2 = h.restart()
    install_site(srv)
    t = tick(h2)
    assert t["ran"] == 0 and t["already_done"] == 1
    assert h2.ok(h2.post("/jobs/interrupted-audits/run", {"request_id": rid()}, caller="scheduler"))["interrupted"] == 1
    v = h2.ok(h2.get(f"/tenants/zbm/schedules/{sid}"))
    assert [x["status"] for x in v["slots"].values()] == ["interrupted"]


def test_domain_removed_after_scheduling_is_skipped_and_recorded(tmp_path, srv):
    h = setup(tmp_path, srv)
    sid = h.ok(schedule(h), 201)["schedule_id"]
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["other.test"]}, andre=True))
    assert tick(h)["refused"] == 1
    v = h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))
    assert list(v["slots"].values())[0]["reason"] == "DOMAIN_NOT_AUTHORIZED"


def test_cross_tenant_on_every_schedule_route(tmp_path, srv):
    h = setup(tmp_path, srv)
    h.tenant("acme", domains=("acme.example",))
    sid = h.ok(schedule(h), 201)["schedule_id"]
    h.refused(h.get(f"/tenants/acme/schedules/{sid}"), 404, "SCHEDULE_NOT_FOUND")
    h.refused(h.post(f"/tenants/acme/schedules/{sid}/status", {"request_id": rid(), "status": "paused"}), 404,
              "SCHEDULE_NOT_FOUND")
    for r in (h.get(f"/tenants/zbm/schedules/{sid}", caller="hub", tenant="acme"),
              h.get("/tenants/zbm/schedules", caller="hub", tenant="acme")):
        h.refused(r, 404, "TENANT_NOT_FOUND")
    assert h.ok(h.get("/tenants/acme/schedules", caller="hub", tenant="acme")) == []
    h.refused(schedule(h, "acme"), 403, "DOMAIN_NOT_AUTHORIZED")
    h.refused(schedule(h, caller="hub"), 403, "CALLER_NOT_ALLOWED")
