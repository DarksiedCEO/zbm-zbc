"""priya-spec/2 section 10 G-9 (card P2-8): tenant isolation of Priya's two routes - no-auth / bad token 401,
cross-tenant 403 / 404 with no code in the body, other departments refused or given no ledger body (no READABLE
text, no WITHHELD code). Synthetic tenants and key only (the key of test_priya_codes_v2)."""

from __future__ import annotations

import json

import pytest
from gsc_fakes import FakeGoogle
from helpers import rid
from test_gsc import PROP, bind, gsc_harness
from test_priya_codes_v2 import ENV_A

pytestmark = pytest.mark.conformance


# ---------------------------------------------------------------- G-9 route matrix
ROUTES = (
    ("POST", "/tenants/acme/keyword-ledger"),
    ("GET", "/tenants/acme/keyword-ledger-runs"),
)


@pytest.fixture()
def h(tmp_path, monkeypatch):
    for k, v in ENV_A.items():
        monkeypatch.setenv(k, v)
    fake = FakeGoogle()
    fake.queries = [
        ("rug cleaner", 3, 120, 4.0),
        ("carpet cleaner near me", 2, 80, 5.0),
        ("zzqxvbt kitten", 1, 40, 5.0),
    ]
    fake.dates_clicks = (10, 150)
    hh = gsc_harness(tmp_path, fake)
    hh.tenant("acme", "own", ("acme.test",))
    hh.tenant("globex", "own", ("globex.test",))
    hh.ok(bind(hh, "acme", PROP))
    return hh


def _call(h, method, path, **kw):
    if method == "POST":
        return h.post(path, {"request_id": rid()}, **kw)
    return h.get(path, **kw)


def test_G9_no_auth_is_401_on_every_priya_route(h):
    for method, path in ROUTES:
        r = h.client.request(
            method,
            "/seo/v1" + path,
            json={"request_id": rid()} if method == "POST" else None,
        )
        assert r.status_code == 401, (method, path, r.status_code)
        r = h.client.request(
            method,
            "/seo/v1" + path,
            headers={"Authorization": "Bearer not-a-token"},
            json={"request_id": rid()} if method == "POST" else None,
        )
        assert r.status_code == 401, (method, path, "bad token", r.status_code)


def test_G9_cross_tenant_is_403_or_404_on_every_priya_route(h):
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 201)
    assert out["ledger"]["withheld_rows"]
    for method, path in ROUTES:
        for caller in ("dashboard", "seo_agent"):
            r = _call(h, method, path, caller=caller, tenant="globex")
            assert r.status_code in (403, 404), (method, path, caller, r.status_code)
            blob = r.text
            assert not any(
                row["code"] in blob for row in out["ledger"]["withheld_rows"]
            )
    assert h.ok(h.get("/tenants/globex/keyword-ledger-runs")) == []


def test_G9_other_departments_get_no_ledger_body(h):
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 201)
    led = out["ledger"]
    secrets = (
        [r["query"]["text"] for r in led["rows"]]
        + [r["code"] for r in led["withheld_rows"]]
        + ["zzqxvbt"]
    )
    assert led["rows"] and led["withheld_rows"]
    matrix = {}
    for caller, tenant in (
        ("hub", "acme"),
        ("finance_31", None),
        ("compliance_38", None),
    ):
        for method, path in ROUTES:
            r = _call(h, method, path, caller=caller, tenant=tenant)
            matrix[(caller, method)] = r.status_code
            assert not any(s in r.text for s in secrets), (caller, method)
            if method == "POST":
                assert r.status_code in (401, 403), (caller, r.status_code)
    assert matrix[("hub", "GET")] == 403 and matrix[("finance_31", "GET")] in (401, 403)
    assert matrix[("compliance_38", "GET")] == 200
    runs = h.ok(h.get("/tenants/acme/keyword-ledger-runs", caller="compliance_38"))
    assert runs and "withheld_rows" not in json.dumps(runs)
