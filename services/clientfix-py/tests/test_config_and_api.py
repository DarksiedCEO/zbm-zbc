"""Start-up refusals (every NOT_BUILT switch, tokens, data directory), the API's boundaries (auth, callers, Andre's
token only through the dashboard, request limits, strict bodies, floats refused) and founder decision 4's secret rules
(no password field, no credential-shaped value, vault references only, never shown back)."""

from __future__ import annotations

import json
import os

import pytest

import config
from helpers import (ANDRE, CALLERS, CLIENT_A, PRODUCT, SERVICE_TOKEN, SHOP_A, SHOPIFY_SCOPES, Harness, base_env,
                     derived, rid)


@pytest.mark.parametrize("name", sorted(config.NOT_BUILT))
def test_not_built_settings_refuse_start(name):
    with pytest.raises(RuntimeError) as e:
        config.load(base_env(**{name: "yes"}))
    assert name in str(e.value)


def test_service_token_and_caller_tokens_are_validated():
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_SERVICE_TOKEN="short"))
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_CALLER_TOKENS=json.dumps({"stranger": derived("x")})))
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_CALLER_TOKENS=json.dumps({"hub": SERVICE_TOKEN})))
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_CALLER_TOKENS=json.dumps({"hub": derived("same"), "dashboard": derived("same")})))


def test_production_needs_a_private_data_dir(tmp_path):
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_NON_PRODUCTION=None))
    d = tmp_path / "open"
    d.mkdir()
    os.chmod(d, 0o755)
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_DATA_DIR=str(d)))
    with pytest.raises(RuntimeError):
        config.load(base_env(CFX_DATA_DIR="relative/path"))


def test_numeric_settings_are_bounded():
    for name, bad in (("CFX_CLIENT_SESSION_MINUTES", "1"), ("CFX_UNKNOWN_TICKS_BEFORE_TASK", "0"),
                      ("CFX_MAX_OPS_PER_ITEM", "51"), ("CFX_PORT", "80"), ("CFX_MAX_ITEMS_PER_JOB", "x")):
        with pytest.raises(RuntimeError):
            config.load(base_env(**{name: bad}))
    assert config.load(base_env()).port == 8500


def test_auth_and_callers(h):
    assert h.client.get("/cfx/v1/status").status_code == 401
    assert h.client.get("/cfx/v1/status", headers={"Authorization": "Bearer " + "x" * 64}).status_code == 401
    r = h.client.get("/cfx/v1/status", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    h.refused(r, 403, "CALLER_UNKNOWN")
    h.refused(h.get("/status", caller="hub"), 403, "CALLER_NOT_ALLOWED")
    r = h.client.get("/cfx/v1/status", headers={"Authorization": b"Bearer " + bytes([0xE9]) * 3})
    assert r.status_code == 401                               # a non-ASCII token is a 401, never a 500


def test_andres_token_only_counts_through_the_dashboard(h):
    conn, j = h.seo_job()
    body = {"request_id": rid()}
    h.refused(h.post(f"/jobs/{j['job_id']}/cancel", body, caller="hub", andre=True), 403, "CALLER_NOT_ALLOWED")
    h.refused(h.post(f"/jobs/{j['job_id']}/cancel", body, caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    h2 = Harness(h.tmp / "x", CFX_ANDRE_APPROVAL_TOKEN=CALLERS["dashboard"])   # equal to a caller token: not set
    h2.refused(h2.post("/clients/freeze", {"request_id": rid(), "client_id": CLIENT_A}, andre=True), 403,
               "ANDRE_NOT_CONFIGURED")
    assert h.ok(h.get("/status"))["andre_approvals_configured"] is True
    assert ANDRE not in json.dumps(h.ledger.entries())


@pytest.mark.parametrize("key", ["password", "Password", "pass", "client_secret", "access_token", "refresh_token",
                                 "token", "api_key", "consumer_secret", "credentials", "cvv", "ssn", "ip_address"])
def test_password_shaped_and_personal_keys_are_refused_anywhere(h, key):
    body = {"request_id": rid(), "client_id": CLIENT_A, "connector": "shopify", "account_ref": SHOP_A,
            "token_ref": "vault:delivery_28.cfx-a", "scopes": SHOPIFY_SCOPES, "extra": {"deep": [{key: "x"}]}}
    r = h.post("/connections", body, caller="hub")
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "forbidden_field"
    assert "x" not in json.dumps(r.json()["detail"][0]["msg"])
    assert not h.svc.connections


@pytest.mark.parametrize("value", ["shpat_" + "a1b2c3d4" * 4, "ya29." + "A" * 30, "1//" + "0g" * 20,
                                   "sk-ant-" + "api03" * 5, "Bearer " + "q" * 30, "https://user:pw@shop.test/",
                                   "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
                                   "-----BEGIN RSA PRIVATE KEY-----"])
def test_credential_shaped_values_are_refused_wherever_they_appear(h, value):
    conn = h.connection()
    h.t.shop(SHOP_A).product(PRODUCT)
    f = h.finding(conn)
    j = h.job([f])
    h.ok(h.accept(j))
    h.ok(h.pay(j))
    item = h.item(j["job_id"])
    r = h.plan(j, [{"item_id": item["item_id"], "connection_id": conn["connection_id"],
                    "ops": [{"op": "shopify.product.update", "target": PRODUCT, "field": "seo.title",
                             "before": None, "after": value}]}])
    h.refused(r, 422, "SECRET_REFUSED")
    assert value not in r.text
    assert value not in json.dumps([e["_payload"] for e in h.ledger.events])


def test_connections_carry_only_vault_references_never_shown_back(h):
    for bad in ("shpat_" + "0" * 32, "plain-token-value", "vault:", "vault:Owner.name", "env:CFX_TOKEN"):
        r = h.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "shopify",
                                    "account_ref": SHOP_A, "token_ref": bad, "scopes": SHOPIFY_SCOPES}, caller="hub")
        assert r.status_code == 422, bad
    h.refused(h.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "shopify",
                                      "account_ref": SHOP_A, "scopes": SHOPIFY_SCOPES}, caller="hub"), 422,
              "TOKEN_REF_INVALID")
    h.refused(h.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "shopify",
                                      "account_ref": SHOP_A, "token_ref": "vault:delivery_28.a",
                                      "scopes": ["read_products"]}, caller="hub"), 422, "SCOPES_INSUFFICIENT")
    h.refused(h.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "shopify",
                                      "account_ref": "evil.example.com", "token_ref": "vault:delivery_28.a",
                                      "scopes": SHOPIFY_SCOPES}, caller="hub"), 422, "ACCOUNT_REF_INVALID")
    c = h.connection()
    view = h.ok(h.get(f"/connections/{c['connection_id']}"))
    assert "token_ref" not in view and view["has_token_ref"] is True
    assert "vault:" not in json.dumps(h.ok(h.get("/connections"))) + json.dumps(h.ok(h.get("/status")))
    h.refused(h.post("/connections", {"request_id": rid(), "client_id": CLIENT_A, "connector": "yelp",
                                      "account_ref": "abcdefghijklmnopqrstuv", "token_ref": "vault:delivery_28.y"},
                     caller="hub"), 422, "TOKEN_REF_INVALID")


def test_strict_bodies_and_money_strings(h):
    conn = h.connection()
    f = h.finding(conn)
    for price in (150.0, 150, "150", "150.5", "-1.00", "1e2", "0.00"):
        r = h.post("/jobs", {"request_id": rid(), "client_id": CLIENT_A, "items": [{"finding_id": f["finding_id"],
                                                                                    "price": price}]},
                   caller="clientfix_agent")
        assert r.status_code == 422, price
    r = h.post("/jobs", {"request_id": rid(), "client_id": CLIENT_A, "items": [{"finding_id": f["finding_id"],
                                                                                "price": "10.00"}], "note": "x"},
               caller="clientfix_agent")
    assert r.status_code == 422
    r = h.post("/jobs", {"request_id": "not-a-uuid", "client_id": CLIENT_A,
                         "items": [{"finding_id": f["finding_id"], "price": "10.00"}]}, caller="clientfix_agent")
    assert r.status_code == 422


def test_request_limits(h):
    hdr = {**h.headers("hub"), "content-type": "application/json"}
    r = h.client.post("/cfx/v1/connections", content=b"{" + b'"a":1,' * 20000 + b'"b":1}', headers=hdr)
    assert r.status_code == 413
    r = h.client.post("/cfx/v1/connections", content=b"[" * 40 + b"]" * 40, headers=hdr)
    assert r.status_code == 422
    r = h.client.post("/cfx/v1/connections", content=b"x=1", headers={**h.headers("hub"),
                                                                      "content-type": "text/plain"})
    assert r.status_code == 415
    assert h.client.get("/cfx/v1/status", headers=h.headers()).headers["cache-control"] == "no-store"
    assert h.client.get("/docs").status_code == 404 and h.client.get("/openapi.json").status_code == 404


def test_hub_sees_only_its_own_clients_job_inside_a_session(h):
    conn, j = h.seo_job(approve=False)
    h.refused(h.get(f"/jobs/{j['job_id']}", caller="hub"), 403, "CLIENT_SESSION_REQUIRED")
    from helpers import CLIENT_B
    h.refused(h.get(f"/jobs/{j['job_id']}", caller="hub", session=h.session(CLIENT_B)), 403, "CLIENT_MISMATCH")
    assert h.ok(h.get(f"/jobs/{j['job_id']}", caller="hub", session=h.session()))["job_id"] == j["job_id"]


def test_a_session_token_is_shown_once_and_only_its_hash_is_kept(h):
    body = {"request_id": rid(), "client_id": CLIENT_A}
    first = h.ok(h.post("/client-sessions", body, caller="hub"), 201)
    assert len(first["session_token"]) == 64
    assert h.ok(h.post("/client-sessions", body, caller="hub"), 201)["session_token"] is None
    raw = json.dumps(h.svc.log.records)
    assert first["session_token"] not in raw
    assert first["session_token"] not in json.dumps([e["_payload"] for e in h.ledger.events])
