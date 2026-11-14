"""Common rows for Farrah: CB-6 / AC-9 (kill switches at every step), CB-7 / AC-13 (record-first, crash, dead process,
restart, ledger refusing writes), AC-10 (vault unavailable), AC-6 (oversized input)."""

from __future__ import annotations

from datetime import date

import pytest

import svc_attribution
from agents import farrah
from conformance.gen.attribution import arow, dataset, freshness
from conformance.gen.farrah_util import ok_run, rid, run, set_map, set_plan, setup, simple

SWITCHES = ("global", "write", "tenant:acme", "capability:audit", "capability:attribution")


def _engage(fa, sw):
    assert fa.switch(sw).status_code == 200


@pytest.mark.parametrize("sw", SWITCHES)
@pytest.mark.parametrize("step", ["compute", "seal", "commit"])
def test_CB_6_AC_9_kill_mid_run_at_every_step(fa, monkeypatch, sw, step):
    simple(fa)
    real_compute, real_seal = farrah.compute, fa.svc._vaulted_seal
    if step == "compute":
        def compute(*a, **k):
            _engage(fa, sw)
            return real_compute(*a, **k)
        monkeypatch.setattr(farrah, "compute", compute)
    elif step == "seal":
        def seal(*a, **k):
            out = real_seal(*a, **k)
            _engage(fa, sw)
            return out
        monkeypatch.setattr(fa.svc, "_vaulted_seal", seal)
    else:
        real_prep = fa.svc._attribution_prepare

        def prep(doc):
            out = real_prep(doc)
            _engage(fa, sw)
            return out
        monkeypatch.setattr(fa.svc, "_attribution_prepare", prep)
    v = ok_run(fa)
    assert v["status"] == "interrupted" and v["interrupted_reason"].startswith("KILLED"), v
    assert v["outcome"] == "KILLED" and "result" not in v                     # the result discarded
    assert not fa.ledger.of_type("attribution_run_recorded")
    ev = fa.ledger.of_type("attribution_run_interrupted")[-1]["_payload"]
    assert ev["reason"].startswith("KILLED")


def test_A_FA_03_plan_downgraded_mid_run_refused_at_commit(fa, monkeypatch):
    simple(fa)
    real = farrah.compute

    def compute(*a, **k):
        fa.svc.tenants["acme"]["plan"] = "managed_1"         # as a recorded downgrade would leave it
        return real(*a, **k)
    monkeypatch.setattr(farrah, "compute", compute)
    v = ok_run(fa)
    assert v["status"] == "interrupted" and v["interrupted_reason"] == "PLAN_EXCLUDES_ATTRIBUTION"
    assert not fa.ledger.of_type("attribution_run_recorded")


def test_A_FA_03_plan_escalation_through_every_route(fa):
    set_plan(fa, "acme", "managed_1")
    simple(fa)
    for caller in ("dashboard", "seo_agent", "scheduler"):
        fa.refused(run(fa, caller=caller), 403, "PLAN_EXCLUDES_ATTRIBUTION")
    for caller, tenant in (("hub", "acme"), ("seo_agent", None), ("compliance_38", None)):
        r = fa.client.put("/seo/v1/tenants/acme/plan", json={"request_id": rid(), "plan": "managed_3",
                                                             "revenue_basis": "analytics"},
                          headers=fa.headers(caller, tenant=tenant))
        assert r.status_code == 403
    r = fa.client.put("/seo/v1/tenants/acme/plan", json={"request_id": rid(), "plan": "growth",
                                                         "revenue_basis": "analytics"},
                      headers=fa.headers("dashboard", andre=True))
    assert r.status_code == 422                                   # only the config table's tier ids
    assert not fa.ledger.of_type("attribution_run_requested")


def test_CB_7_requested_is_recorded_before_anything_is_computed(fa, monkeypatch):
    simple(fa)
    seen = []
    real = farrah.compute

    def compute(*a, **k):
        seen.append(bool(fa.ledger.of_type("attribution_run_requested")))
        seen.append(any(r["status"] == "running" for r in fa.svc.attribution_runs.values()))
        return real(*a, **k)
    monkeypatch.setattr(farrah, "compute", compute)
    ok_run(fa)
    assert seen == [True, True]


def test_CB_7_a_crash_closes_the_run_RUN_FAILED_with_a_location_code(fa, monkeypatch):
    simple(fa)
    monkeypatch.setattr(farrah, "compute", lambda *a, **k: 1 / 0)
    v = ok_run(fa)
    assert v["status"] == "interrupted" and v["interrupted_reason"] == "RUN_FAILED" and v["outcome"] == "FAILED"
    f = fa.svc.attribution_runs[v["run_id"]]["failure"]
    assert f["error_type"] == "ZeroDivisionError" and f["agent"] == "farrah" and f["where"]


def test_CB_7_AC_13_dead_process_and_restart(fad, monkeypatch):
    simple(fad)
    req = rid()
    monkeypatch.setattr(svc_attribution.AttributionMixin, "_execute_attribution",
                        lambda self, fid, *a, **k: self.attribution_run_view("acme", fid))
    r = run(fad, request_id=req)
    fid = r.json()["run_id"]
    monkeypatch.undo()
    h2 = fad.restart()
    setup(h2, plan=None)
    assert h2.svc.attribution_runs[fid]["status"] == "running"
    out = h2.ok(h2.post("/jobs/interrupted-audits/run", {"request_id": rid()}, caller="scheduler"))
    assert out["attribution_runs_interrupted"] == 1
    assert h2.svc.attribution_runs[fid]["status"] == "interrupted"
    again = h2.ok(run(h2, request_id=req), 201)                    # a replay answers the first run, no duplicate
    assert again["run_id"] == fid and len(h2.ledger.of_type("attribution_run_requested")) == 1


def test_AC_13_ledger_refusing_writes(fa, monkeypatch):
    simple(fa)
    fa.ledger.fail = True
    r = run(fa)
    assert r.status_code == 503 and r.json()["detail"] == "LEDGER_UNAVAILABLE"
    assert not fa.svc.attribution_runs
    fa.ledger.fail = False
    calls = {"n": 0}
    real = fa.svc._commit

    def commit(kind, data, actor, evidence=None):
        if kind == "attribution_run_recorded":
            calls["n"] += 1
            fa.ledger.fail = True
        return real(kind, data, actor, evidence)
    monkeypatch.setattr(fa.svc, "_commit", commit)
    r = run(fa)
    assert r.status_code >= 500 or r.json()["status"] != "completed"
    assert not fa.ledger.of_type("attribution_run_recorded")                # no half-recorded result
    assert calls["n"] == 1


def test_AC_10_vault_unavailable(tmp_path):
    from helpers import Harness
    from clock import FixedClock
    from datetime import datetime, timezone
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), SEO_VAULT_KEY_FILE=None,
                clock=FixedClock(datetime(2026, 10, 9, 18, tzinfo=timezone.utc)))
    setup(h)
    h.store.tenant("acme")
    r = run(h)
    assert r.status_code == 503 and r.json()["detail"] == "VAULT_UNAVAILABLE"
    assert h.store.reads == 0 and not h.ledger.of_type("attribution_run_requested")


def test_AC_10_lost_or_tampered_blob_shows_unavailable(fa):
    simple(fa)
    v = ok_run(fa)
    fa.svc.attribution_runs[v["run_id"]]["result_hmac"] = "0" * 64
    got = fa.ok(fa.get(f"/tenants/acme/attribution-runs/{v['run_id']}"))
    assert got["result"] is None and got["vault_unavailable"] is True
    fa.tenant("globex")
    set_plan(fa, "globex", "managed_2")
    r = fa.svc.attribution_runs[v["run_id"]]
    assert fa.svc._vaulted_open("globex", "revenue_attribution", r["result_ref"], r["result_hmac"]) is None


def test_AC_6_oversized_inputs(fa):
    big = {"request_id": rid(), "organic_search": ["x" * 120] * 1200, "ai_referral": [], "other_known": []}
    r = fa.client.put("/seo/v1/tenants/acme/channel-map", json=big, headers=fa.headers("dashboard", andre=True))
    assert r.status_code == 413
    assert set_map(fa, "acme", {"organic_search": ["a"] * 201}).status_code == 422          # list cap + 1
    assert set_map(fa, "acme", {"organic_search": ["a"] * 200}).status_code == 200          # at the cap
    assert set_map(fa, "acme", {"organic_search": ["a" * 2001]}).status_code == 422          # 10x the bound
    rows = [arow(date(2026, 9, 1), "1.00", landing=f"/{i}") for i in range(farrah.MAX_ROWS + 1)]
    with pytest.raises(farrah.InputRefused):
        farrah.validate_dataset(dataset("acme", "analytics", rows), "acme", "analytics")
    rows.pop()
    farrah.validate_dataset(dataset("acme", "analytics", rows), "acme", "analytics")
    fa.store.add(fa.svc, dataset("acme", "analytics", rows + rows[:1]))
    fa.store.tenant("acme")["freshness"] = freshness("acme")
    v = ok_run(fa)
    assert v["outcome"] == "INSUFFICIENT_EVIDENCE"                             # refused whole, counted
    assert fa.svc.attribution_runs[v["run_id"]]["facts"]["inputs_refused"] == {"DATASET_INVALID": 1}
