"""farrah-spec/1 section 4: write paths W-FA-1..3, the end-to-end bar W-E (AC-1, AC-2 planted into every Osei dataset
field that reaches Farrah and into the channel map), D-FA-01, TB-FA-1, and CB-14, CB-15, CB-17, CB-18 for Farrah."""

from __future__ import annotations

import json
import logging
import os
from datetime import date

import pytest

import sink_guard
from agents import farrah
from conformance.gen import personal
from conformance.gen.attribution import arow, crow, dataset, freshness
from conformance.gen.farrah_util import ok_run, rid, run, set_map, set_plan, simple
from secret_corpus import corpus as secret_corpus

D = date(2026, 9, 9)
ATTRIBUTION_KINDS = ("attribution_run_requested", "attribution_run_recorded", "attribution_run_interrupted")
LOG_KEYS = {"attribution_run_requested": {"run_id", "tenant_id", "month_start", "basis", "input_dataset_hex",
                                          "inputs_hmac", "supersedes", "actor", "request_id", "request_sha", "_obj",
                                          "evidence"},
            "attribution_run_recorded": {"run_id", "result_ref", "result_hmac", "facts", "actor", "evidence"}}
FACT_KEYS = {"outcome", "labels", "rows", "unknown_rows", "refund_rows", "assumptions", "input_dataset_hex",
             "limitations", "inputs_refused"}


def _log_records(h):
    return [json.loads(x) for x in h.svc.log.raw_lines()]


def test_W_FA_1_requested_record_names_tenant_month_basis_inputs(fa):
    doc = simple(fa)
    v = ok_run(fa)
    ev = fa.ledger.of_type("attribution_run_requested")[-1]["_payload"]
    assert ev["tenant_id"] == "acme" and ev["month_start"] == "2026-09-01" and ev["basis"] == "analytics"
    assert ev["input_dataset_hex"] == [doc["dataset_id"][3:]] and ev["run_id"] == v["run_id"]


def test_W_FA_2_D_FA_01_no_amount_in_the_log_or_the_ledger(fa):
    simple(fa, rev="123456.78", unk_rev="98765.43")
    v = ok_run(fa)
    amounts = {x for c in v["result"]["channels"].values() for x in (c["figure"], c["lower"], c["upper"]) if x}
    amounts |= {x for x in v["result"]["totals"].values() if x}
    assert amounts
    recs = [r for r in _log_records(fa) if r["kind"] in ATTRIBUTION_KINDS]
    blob = json.dumps(recs) + json.dumps([e["_payload"] for e in fa.ledger.events])
    assert not [a for a in amounts if a in blob]                             # amounts only in the vault
    for r in recs:
        assert set(r["data"]) <= LOG_KEYS.get(r["kind"], set(r["data"])), r["kind"]
        if r["kind"] == "attribution_run_recorded":
            assert set(r["data"]["facts"]) == FACT_KEYS
            assert r["data"]["result_hmac"] == fa.ledger.of_type("attribution_run_recorded")[-1]["_payload"][
                "result_hmac"]
        assert not sink_guard.violations(r["kind"], r["data"], stored=True)
    doc = fa.svc.vault.get_blob("acme", fa.svc.attribution_runs[v["run_id"]]["result_ref"])
    assert doc["channels"]["organic_search"]["figure"] == v["result"]["channels"]["organic_search"]["figure"]


def test_W_FA_3_owner_views_and_compliance_label_and_counts_only(fa):
    simple(fa)
    v = ok_run(fa)
    for caller, tenant in (("dashboard", None), ("seo_agent", None), ("hub", "acme")):
        got = fa.ok(fa.get(f"/tenants/acme/attribution-runs/{v['run_id']}", caller=caller, tenant=tenant))
        assert got["result"] == v["result"] and got["display"]["organic_search"]["figure"] == "4000.00"
    c = fa.ok(fa.get(f"/tenants/acme/attribution-runs/{v['run_id']}", caller="compliance_38"))
    assert set(c) & {"result", "display"} == set()
    assert c["labels"] == v["labels"] and c["rows"] == v["rows"] and c["assumptions"] == v["assumptions"]
    text = json.dumps(c)
    assert "4000" not in text and "4010" not in text
    assert fa.get(f"/tenants/acme/attribution-runs/{v['run_id']}", caller="finance_31").status_code == 403


def test_TB_FA_1_guard_record_sweeps_every_non_amount_field(fa):
    simple(fa)
    v = ok_run(fa)
    doc = json.loads(json.dumps(v["result"]))
    assert fa.svc._attribution_prepare(doc) == doc
    planted = secret_corpus()[0][2]
    with pytest.raises(Exception) as exc:
        fa.svc._attribution_prepare({**doc, "timezone": planted})
    assert type(exc.value).__name__ == "GuardWithheld"
    with pytest.raises(Exception):
        fa.svc._attribution_prepare({**doc, "inputs": [planted]})            # an id of another shape: refused


def test_TB_FA_1_a_withheld_field_fails_the_run_closed(fa, monkeypatch):
    simple(fa)
    from agents import secret_guard
    monkeypatch.setattr(secret_guard, "guard_record", lambda obj: {**obj, "timezone": "[withheld]"})
    v = ok_run(fa)
    assert v["status"] == "interrupted" and v["interrupted_reason"] == "RUN_FAILED" and "result" not in v
    assert not fa.ledger.of_type("attribution_run_recorded")


# ------------------------------------------------------------------------------------------------ W-E bar

def _plant(fad, values, basis, k=0):
    tid = "acme"
    if basis == "crm":
        set_plan(fad, tid, "managed_2", "crm")
        assert set_map(fad, tid, {"organic_search": values[:5], "ai_referral": values[5:8],
                                  "other_known": values[8:12]}).status_code == 200
        rows = [crow(D, "10.00", lead_source=x, ref=f"r{i}") for i, x in enumerate(values)]
        rows += [crow(D, "10.00", utm_medium="cpc", utm_source=x, ref=f"u{i}") for i, x in enumerate(values)]
        for i, r in enumerate(rows):
            r["landing_path"] = values[i % len(values)]
            r["utm_campaign"] = values[(i + 1) % len(values)]
    else:
        set_plan(fad, tid, "managed_2", "analytics")
        rows = [arow(D, "10.00", x, "referral", "Referral", "/" + str(i)) for i, x in enumerate(values)]
        rows += [arow(D, "10.00", "google", x, "Organic Search", x) for x in values]
    rows = [r for r in rows if all(v is None or isinstance(v, int) or len(v) <= 2048 for v in r.values())]
    fad.store.add(fad.svc, dataset(tid, basis, rows, produced_at=f"2026-10-05T{k // 60:02d}:{k % 60:02d}:00Z"))
    fad.store.tenant(tid)["freshness"] = freshness(tid, {basis: "FRESH"})
    v = ok_run(fad)
    assert v["status"] == "completed", v.get("interrupted_reason")
    return v


def _readable(fad, needles, views):
    hay = []
    for root, _dirs, files in os.walk(fad.settings.data_dir):
        for f in files:
            with open(os.path.join(root, f), "rb") as fh:
                hay.append(fh.read().decode("utf-8", "replace"))
    hay.append(json.dumps([e["_payload"] for e in fad.ledger.events], ensure_ascii=False))
    hay.append(json.dumps(views, ensure_ascii=False))
    return [n for n in needles if any(n in x for x in hay)]


def _we(fad, carriers_values, basis):
    found = []
    for k in range(0, len(carriers_values), 40):
        batch = carriers_values[k:k + 40]
        _we.n = getattr(_we, "n", 0) + 1
        v = _plant(fad, [c for c, _ in batch], basis, _we.n)
        views = [fad.ok(fad.get(f"/tenants/acme/attribution-runs/{v['run_id']}", caller="compliance_38")),
                 fad.ok(fad.get("/tenants/acme", caller="compliance_38")),
                 fad.ok(fad.get(f"/tenants/acme/attribution-runs/{v['run_id']}", caller="hub", tenant="acme")),
                 run(fad, month=batch[0][0][:50]).json()]
        found += _readable(fad, [x for _, x in batch] + [c for c, _ in batch], views)
    return found


def test_W_E_AC_1_planted_secrets_readable_nowhere(fad):
    cases = [(carrier, secret) for _name, carrier, secret, _kind in secret_corpus()][:960]
    assert len(cases) == 960                                                 # AC-1 fast N (core K-1 x N-1 corpus)
    assert _we(fad, cases[:480], "analytics") == [] and _we(fad, cases[480:960], "crm") == []


def test_W_E_AC_2_planted_personal_data_readable_nowhere(fad):
    cases = [(c, v) for _i, c, v in personal.cases(400, seed=3)]
    assert _we(fad, cases[:200], "analytics") == [] and _we(fad, cases[200:], "crm") == []


# ------------------------------------------------------------------------------------------------ CB-17, CB-18

def test_CB_17_python_logging_takes_machine_values_only(fa, monkeypatch, caplog):
    simple(fa)
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(farrah, "compute", lambda *a, **k: (_ for _ in ()).throw(KeyError("planted value 123")))
    v = ok_run(fa)
    assert v["interrupted_reason"] == "RUN_FAILED"
    recs = [r for r in caplog.records if r.name.startswith("seo.attribution")]
    assert recs and all(sink_guard.classify(str(a)) is not None for r in recs for a in (r.args or ()))
    assert not any("planted" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("month", ["2026-9", "September", "2026-13", "2026-09-01", "<script>2026-09</script>",
                                   "2026-09' OR 1=1", "x" * 300])
def test_CB_18_error_bodies_echo_nothing(fa, month):
    r = fa.post("/tenants/acme/attribution-runs", {"request_id": rid(), "month": month}, caller="dashboard")
    assert r.status_code == 422 and month not in r.text
    r = fa.get("/tenants/acme/attribution-runs/" + "ds-" + "a" * 16)
    assert r.status_code == 422 and "ds-" not in r.text
