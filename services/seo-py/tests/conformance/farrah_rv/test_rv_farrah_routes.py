"""Reviewer: B-FA-02 plan gate, B-FA-03 / CB-9 route x caller, CB-10 isolation, A-FA-03, CB-6, CB-8, AC-10,
B-FA-29 clock and supersede. Machine words only in output."""

from __future__ import annotations

from datetime import datetime


from gen_rv_farrah import make_case
from rv_service import install_case, make_harness, post_run, put_plan, rid


def good_case():
    for s in range(5, 900):
        c = make_case(s, "plain")
        if c.basis == "analytics" and c.month == "2026-09" or (c.basis == "analytics" and len(c.basis_doc["rows"]) > 20):
            return c
    raise AssertionError


def H(tmp_path, tid="acme", plan="managed_2", **kw):
    h = make_harness(tmp_path, **kw)
    for t in ("acme", "globex"):
        h.tenant(t)
    if plan:
        assert put_plan(h, tid, plan).status_code == 200
    return h


def test_rv_plan_gate_each_plan(tmp_path):
    c = good_case()
    out = {}
    for plan in (None, "managed_1", "managed_2", "managed_3"):
        h = H(tmp_path / str(plan), plan=plan)
        install_case(h, c)
        r = post_run(h, "acme", c.month)
        out[plan] = (r.status_code, r.json().get("detail") if r.status_code != 201 else r.json().get("outcome"))
        if r.status_code != 201:
            assert not h.svc.attribution_runs and h.ledger.of_type("attribution_run_requested") == []
    print("RV-PLAN", out)
    assert out[None][0] == 403 and out["managed_1"] == (403, "PLAN_EXCLUDES_ATTRIBUTION")
    assert out["managed_2"][0] == 201 and out["managed_3"][0] == 201
    # the own tenant zbm runs regardless of plan (Q-FA-4) -- but it still needs a basis
    h = make_harness(tmp_path / "zbm")
    if "zbm" not in h.svc.tenants:
        h.tenant("zbm", kind="own")
    r0 = post_run(h, "zbm", c.month)
    assert put_plan(h, "zbm", "managed_1").status_code == 200
    c2 = make_case(c.seed, "plain", tid="zbm")
    install_case(h, c2, tid="zbm")
    r = post_run(h, "zbm", c.month)
    print("RV-ZBM no-plan", r0.status_code, r0.json().get("detail"), "entry-plan", r.status_code)
    assert r.status_code == 201


def test_rv_plan_escalation(tmp_path):
    h = H(tmp_path, plan="managed_1")
    res = {}
    for caller, andre, tenant in (("hub", False, "acme"), ("seo_agent", False, None), ("dashboard", False, None),
                                  ("scheduler", False, None), ("finance_31", False, None),
                                  ("compliance_38", False, None), ("seo_agent", True, None)):
        hd = h.headers(caller, andre=andre and caller == "dashboard", tenant=tenant)
        if andre and caller != "dashboard":
            from helpers import ANDRE
            hd["X-Andre-Approval-Token"] = ANDRE
        r = h.client.put("/seo/v1/tenants/acme/plan", json={"request_id": rid(), "plan": "managed_3",
                                                            "revenue_basis": "analytics"}, headers=hd)
        res[(caller, andre)] = r.status_code
    print("RV-PLAN-ESC", res)
    assert all(v in (401, 403) for v in res.values())
    assert h.svc.tenants["acme"]["plan"] == "managed_1"
    # entry tenant through every route
    c = good_case()
    install_case(h, c)
    for caller in ("dashboard", "seo_agent", "scheduler"):
        assert post_run(h, "acme", c.month, caller=caller).status_code == 403


def test_rv_plan_downgraded_mid_run(tmp_path):
    h = H(tmp_path)
    c = good_case()
    install_case(h, c)
    import agents.farrah as fm
    real = fm.compute

    def compute(*a, **k):
        h.svc._commit  # noqa: B018
        r = put_plan(h, "acme", "managed_1")
        assert r.status_code == 200
        return real(*a, **k)
    fm.compute = compute
    try:
        r = post_run(h, "acme", c.month)
    finally:
        fm.compute = real
    v = r.json()
    print("RV-DOWNGRADE", r.status_code, v.get("status"), v.get("outcome"), v.get("interrupted_reason"))
    assert v["status"] == "interrupted" and v.get("result") is None
    assert h.ledger.of_type("attribution_run_recorded") == []


ROUTES = ("POST runs", "GET runs", "GET run")


def test_rv_route_caller_matrix(tmp_path):
    h = H(tmp_path)
    c = good_case()
    install_case(h, c)
    fid = post_run(h, "acme", c.month).json()["run_id"]
    got = {}
    for caller, tenant in (("dashboard", None), ("seo_agent", None), ("scheduler", None), ("hub", "acme"),
                           ("hub", "globex"), ("hub", None), ("finance_31", None), ("compliance_38", None),
                           (None, None)):
        hd = h.headers(caller, tenant=tenant)
        p = h.client.post("/seo/v1/tenants/acme/attribution-runs", json={"request_id": rid(), "month": c.month},
                          headers=hd)
        g = h.client.get("/seo/v1/tenants/acme/attribution-runs", headers=hd)
        o = h.client.get(f"/seo/v1/tenants/acme/attribution-runs/{fid}", headers=hd)
        amounts = o.status_code == 200 and isinstance(o.json().get("result"), dict)
        got[(caller, tenant)] = (p.status_code, g.status_code, o.status_code, amounts)
    print("RV-MATRIX", got)
    exp = {("dashboard", None): (201, 200, 200, True), ("seo_agent", None): (201, 200, 200, True),
           ("scheduler", None): (201, 403, 403, False), ("hub", "acme"): (403, 200, 200, True),
           ("hub", "globex"): (403, 404, 404, False), ("finance_31", None): (403, 403, 403, False),
           ("compliance_38", None): (403, 200, 200, False)}
    for k, e in exp.items():
        assert got[k] == e, k
    assert got[("hub", None)][1] in (401, 403) and got[(None, None)][1] in (401, 403)


def test_rv_isolation_byte_identical_404(tmp_path):
    h = H(tmp_path)
    assert put_plan(h, "globex").status_code == 200
    c = good_case()
    install_case(h, c)
    fid = post_run(h, "acme", c.month).json()["run_id"]
    missing = "seo-att-" + "0" * 40
    a = h.client.get(f"/seo/v1/tenants/globex/attribution-runs/{fid}", headers=h.headers("dashboard"))
    b = h.client.get(f"/seo/v1/tenants/globex/attribution-runs/{missing}", headers=h.headers("dashboard"))
    print("RV-ISO", a.status_code, b.status_code, a.content == b.content)
    assert a.status_code == b.status_code == 404 and a.content == b.content
    lst = h.client.get("/seo/v1/tenants/globex/attribution-runs", headers=h.headers("dashboard")).json()
    assert lst == []
    # a dataset of acme placed in globex's store is refused (tenant mismatch)
    t = h.store.reset("globex")
    t["datasets"].append(c.basis_doc)
    from rv_service import record
    t["records"].append(record(h.svc, c.basis_doc, tid="globex"))
    v = post_run(h, "globex", c.month).json()
    assert v["inputs"] == [] and v["outcome"] == "INSUFFICIENT_EVIDENCE"


def test_rv_month_clock_edges(tmp_path):
    """B-FA-29 on the injected clock: Sep 2026 in America/Los_Angeles closes at the local start of Oct 4."""
    from clock import FixedClock
    from test_rv_farrah_edges import arow, case, doc
    d = doc([arow("20260905", "10.00")], tz="America/Los_Angeles")
    out = []
    for when in ("2026-10-04T06:59:59+00:00", "2026-10-04T07:00:00+00:00"):
        h = H(tmp_path / when[11:13] / when[17:19])
        h.svc.clock = FixedClock(datetime.fromisoformat(when))
        install_case(h, case(d))
        r = post_run(h, "acme", "2026-09")
        out.append((r.status_code, r.json().get("detail")))
    print("RV-CLOCK", out)
    assert out[0] == (422, "MONTH_NOT_CLOSED") and out[1][0] == 201
