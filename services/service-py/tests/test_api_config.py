"""The HTTP surface (auth, callers, Andre's gate, refused fields, limits, error bodies) and start-up settings."""

from __future__ import annotations

import json

import pytest

import api
import config as config_mod
from founder import FounderGate
from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, Harness, base_env, rid


# --------------------------------------------------------------------------------------------------- auth

def test_every_route_but_health_needs_the_bearer_token(h):
    r = h.client.get("/svc/v1/tickets", headers={api.CALLER_HEADER: CALLERS["dashboard"]})
    assert r.status_code == 401
    r = h.client.get("/svc/v1/tickets", headers={"Authorization": "Bearer wrong" + "x" * 40,
                                                 api.CALLER_HEADER: CALLERS["dashboard"]})
    assert r.status_code == 401
    r = h.client.get("/svc/v1/tickets", headers={"Authorization": "Bearer t\xe9st".encode("latin-1"),
                                                 api.CALLER_HEADER: CALLERS["dashboard"]})
    assert r.status_code == 401
    assert h.client.get("/health").json() == {"status": "ok"}


def test_unknown_and_wrong_callers(h):
    r = h.client.get("/svc/v1/tickets", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert r.status_code == 403 and r.json()["detail"] == "CALLER_UNKNOWN"
    assert h.get("/svc/v1/tickets", caller="hub").json()["detail"] == "CALLER_NOT_ALLOWED"
    assert h.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:a",
                                            "text": "hi"}, caller="sms_gateway").status_code == 403


def test_andre_gate(h):
    saved = h.article(approve=False)
    path = "/svc/v1/kb/articles/hours/approve"
    body = {"request_id": rid(), "version": saved["version"], "content_sha256": saved["content_sha256"]}
    hdr = h.headers("dashboard")
    for bad in ("", "nope", "t\xe9st".encode("latin-1"), SERVICE_TOKEN, CALLERS["dashboard"]):
        r = h.client.post(path, json=body, headers={**hdr, "X-Andre-Approval-Token": bad})
        assert r.status_code == 403 and r.json()["detail"] in ("ANDRE_APPROVAL_REQUIRED", "ANDRE_APPROVAL_INVALID")
    r = h.client.post(path, json=body, headers={**h.headers("hub"), "X-Andre-Approval-Token": ANDRE_TOKEN})
    assert r.status_code == 403 and r.json()["detail"] == "CALLER_NOT_ALLOWED"     # Andre via the dashboard only
    h.ok(h.client.post(path, json=body, headers={**hdr, "X-Andre-Approval-Token": ANDRE_TOKEN}))


@pytest.mark.parametrize("token", [None, SERVICE_TOKEN, CALLERS["hub"]])
def test_andre_token_equal_to_another_token_counts_as_not_configured(tmp_path, token):
    h = Harness(tmp_path, SVC_ANDRE_APPROVAL_TOKEN=token or "")
    saved = h.article(approve=False)
    r = h.client.post("/svc/v1/kb/articles/hours/approve",
                      json={"request_id": rid(), "version": saved["version"], "content_sha256": saved["content_sha256"]},
                      headers={**h.headers("dashboard"), "X-Andre-Approval-Token": token or "x"})
    assert r.status_code == 403 and r.json()["detail"] == "ANDRE_NOT_CONFIGURED"
    assert h.ok(h.get("/svc/v1/status"))["andre_approvals_configured"] is False
    assert FounderGate.build(token, SERVICE_TOKEN, CALLERS.values()).configured is False


# --------------------------------------------------------------------------------------------------- bodies

@pytest.mark.parametrize("key", ["dob", "date_of_birth", "ssn", "card_number", "account_number", "ip_address",
                                 "Government-Id", "user_agent"])
def test_forbidden_personal_fields_refused_anywhere(h, key):
    body = {"request_id": rid(), "brand": "zbm", "contact_ref": "client:a", "nested": {key: "1990-01-01"}}
    r = h.post("/svc/v1/contacts", body, caller="hub")
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "forbidden_field"
    assert "1990" not in r.text


def test_unknown_fields_and_coercion_refused(h):
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:a", "extra": 1},
               caller="hub")
    assert r.status_code == 422
    r = h.post("/svc/v1/tickets/sv-tkt-" + "0" * 40 + "/priority", {"request_id": 5, "priority": "p1"})
    assert r.status_code == 422


def test_error_bodies_never_echo_input(h):
    secret = "my-secret-phrase-<script>"
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": secret}, caller="hub")
    assert r.status_code == 422 and "secret-phrase" not in r.text
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": secret, "contact_ref": "client:a"}, caller="hub")
    assert "secret-phrase" not in r.text
    r = h.get(f"/svc/v1/tickets/{secret}")
    assert r.status_code in (404, 422) and "secret-phrase" not in r.text


def test_control_characters_refused(h):
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:a",
                                    "display_name": "Dana\x00"}, caller="hub")
    assert r.status_code == 422


def test_request_limits(h):
    hdr = {**h.headers("hub"), "content-type": "application/json"}
    big = json.dumps({"request_id": rid(), "brand": "zbm", "contact_ref": "client:a", "text": "x" * 200_000})
    assert h.client.post("/svc/v1/chat/messages", content=big, headers=hdr).status_code == 413
    assert h.client.post("/svc/v1/chat/messages", content=b"a=b",
                         headers={**h.headers("hub"), "content-type": "text/plain"}).status_code == 415
    deep = "[" * 40 + "]" * 40
    assert h.client.post("/svc/v1/chat/messages", content=deep, headers=hdr).status_code == 422
    assert h.client.get("/svc/v1/tickets?x=" + "a" * 5000, headers=h.headers()).status_code == 414
    r = h.post("/svc/v1/chat/messages", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:a",
                                         "text": "x" * 20001}, caller="hub")
    assert r.status_code == 422


def test_every_answer_is_no_store_and_docs_are_off(h):
    assert h.get("/svc/v1/tickets").headers["cache-control"] == "no-store"
    assert h.client.get("/health").headers["cache-control"] == "no-store"
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code == 404


def test_health_says_nothing_but_status_and_status_is_dashboard_only(h):
    assert h.client.get("/health").json() == {"status": "ok"}
    assert h.get("/svc/v1/status", caller="hub").status_code == 403
    st = h.ok(h.get("/svc/v1/status"))
    assert st["wired"]["voice"] is False and st["wired"]["senders"] == {"chat": False, "email": False, "sms": False}


def test_request_id_reused_with_a_different_body_is_409(h):
    body = {"request_id": "same-id", "brand": "zbm", "contact_ref": "client:a"}
    h.ok(h.post("/svc/v1/contacts", body, caller="hub"), 201)
    assert h.ok(h.post("/svc/v1/contacts", body, caller="hub"), 201)
    r = h.post("/svc/v1/contacts", {**body, "display_name": "Other"}, caller="hub")
    assert r.status_code == 409 and r.json()["detail"] == "REQUEST_ID_REUSED"


def test_contact_address_cannot_be_taken_twice_in_a_brand(h):
    h.contact("client:a", email="x@y.test")
    r = h.post("/svc/v1/contacts", {"request_id": rid(), "brand": "zbm", "contact_ref": "client:b",
                                    "email": "x@y.test"}, caller="hub")
    assert r.status_code == 409 and r.json()["detail"] == "CONTACT_ADDRESS_TAKEN"
    h.contact("client:b", brand="zbc", email="x@y.test")                     # the other brand is separate


# --------------------------------------------------------------------------------------------------- settings

def test_refuses_to_start_without_a_service_token():
    with pytest.raises(RuntimeError, match="SVC_SERVICE_TOKEN"):
        config_mod.load(base_env(SVC_SERVICE_TOKEN=None))


def test_data_dir_required_in_production():
    with pytest.raises(RuntimeError, match="SVC_DATA_DIR is required"):
        config_mod.load(base_env(SVC_NON_PRODUCTION=None))


def test_data_dir_must_be_private(tmp_path):
    d = tmp_path / "open"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    with pytest.raises(RuntimeError, match="chmod 700"):
        config_mod.load(base_env(SVC_DATA_DIR=str(d)))


@pytest.mark.parametrize("name", sorted(config_mod.NOT_BUILT))
def test_not_built_providers_refuse_start(name):
    with pytest.raises(RuntimeError, match=name):
        config_mod.load(base_env(**{name: "something"}))


def test_caller_tokens_rules():
    with pytest.raises(RuntimeError, match="unknown caller"):
        config_mod.load(base_env(SVC_CALLER_TOKENS=json.dumps({"stranger": "x" * 40})))
    with pytest.raises(RuntimeError, match="distinct"):
        config_mod.load(base_env(SVC_CALLER_TOKENS=json.dumps({"hub": "y" * 40, "dashboard": "y" * 40})))
    with pytest.raises(RuntimeError, match="32..512"):
        config_mod.load(base_env(SVC_CALLER_TOKENS=json.dumps({"hub": "short"})))


def test_brand_identities_must_differ_and_be_valid():
    with pytest.raises(RuntimeError, match="must differ"):
        config_mod.load(base_env(SVC_SUPPORT_EMAIL_ZBC="support@zbestmedia.test"))
    with pytest.raises(RuntimeError, match="E.164"):
        config_mod.load(base_env(SVC_SMS_NUMBER_ZBM="310-555-0100"))


def test_legal_client_settings_come_as_a_set():
    with pytest.raises(RuntimeError, match="as a set"):
        config_mod.load(base_env(SVC_LEGAL_URL="http://127.0.0.1:1"))
    s = config_mod.load(base_env(SVC_LEGAL_URL="http://legal.internal", SVC_LEGAL_TOKEN="t" * 40,
                                 SVC_LEGAL_CALLER_TOKEN="c" * 40))
    assert api.build_ports(s).handoffs["legal_37"].wired is True


def test_port_default_and_bounds():
    s = config_mod.load(base_env())
    assert s.port == 8460 and s.bind_addr == "127.0.0.1"
    with pytest.raises(RuntimeError, match="SVC_AT_RISK_THRESHOLD"):
        config_mod.load(base_env(SVC_AT_RISK_THRESHOLD="100"))
