"""priya-spec/1 G-9: tenant isolation of Priya's routes - cross-tenant reads see nothing (404 / empty), other
departments are refused (403) and never receive a ledger body, no auth is 401."""

from __future__ import annotations

import json

import pytest
from gsc_fakes import FakeGoogle
from helpers import rid
from test_gsc import PROP, bind, gsc_harness

pytestmark = pytest.mark.conformance


def _setup(tmp_path):
    fake = FakeGoogle()
    fake.queries = [("rug cleaner", 3, 120, 4.0), ("carpet cleaner near me", 2, 80, 5.0)]
    fake.dates_clicks = (10, 150)
    h = gsc_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test",))
    h.tenant("globex", "own", ("globex.test",))
    h.ok(bind(h, "acme", PROP))
    return h


def test_G9_no_auth_is_401(tmp_path):
    h = _setup(tmp_path)
    assert h.client.post("/seo/v1/tenants/acme/keyword-ledger", json={"request_id": rid()}).status_code == 401
    assert h.client.get("/seo/v1/tenants/acme/keyword-ledger-runs").status_code == 401


def test_G9_other_departments_are_refused_and_get_no_ledger_body(tmp_path):
    h = _setup(tmp_path)
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 201)
    assert out["ledger"]["rows"]
    for caller, tenant in (("hub", "acme"), ("finance_31", None), ("compliance_38", None)):
        r = h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}, caller=caller, tenant=tenant)
        assert r.status_code in (401, 403), (caller, r.status_code)
    r = h.get("/tenants/acme/keyword-ledger-runs", caller="hub", tenant="acme")
    assert r.status_code == 403
    runs = h.ok(h.get("/tenants/acme/keyword-ledger-runs", caller="compliance_38"))
    blob = json.dumps(runs)
    assert runs and not any(r["query"]["text"] in blob for r in out["ledger"]["rows"])   # no ledger body


def test_G9_cross_tenant_reads_see_nothing(tmp_path):
    h = _setup(tmp_path)
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 201)
    assert h.ok(h.get("/tenants/globex/keyword-ledger-runs")) == []
    with pytest.raises(Exception) as ei:
        h.svc.keyword_run_view("globex", out["run_id"])
    assert getattr(ei.value, "reason", None) == "KEYWORD_RUN_NOT_FOUND"
    r = h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}, caller="hub", tenant="globex")
    assert r.status_code in (403, 404)
    g = h.ok(h.post("/tenants/globex/keyword-ledger", {"request_id": rid()}), 201)
    assert g["ledger"]["sources"]["search_console"]["state"] == "GSC_PROPERTY_UNBOUND" and not g["ledger"]["rows"]
    h.refused(h.post("/tenants/acme/keyword-ledger", {"request_id": rid(), "property": "sc-domain:globex.test"}), 422)
