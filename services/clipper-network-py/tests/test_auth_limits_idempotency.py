"""Auth on every route, caller identities, request limits, idempotency."""

from __future__ import annotations

import json

import pytest

from helpers import ANDRE_TOKEN, SERVICE_TOKEN, Harness, rid

H = Harness()
ROUTES = [(sorted(r.methods - {"HEAD"})[0], r.path) for r in H.app.routes
          if hasattr(r, "methods") and r.path != "/health"]


def _path(p):
    return p.replace("{", "").replace("}", "")


def test_route_inventory():
    paths = {p for _, p in ROUTES}
    for need in ("/cn/v1/opt-ins", "/cn/v1/opt-outs", "/cn/v1/recruiting/campaigns", "/cn/v1/recruiting/campaigns/{recruit_id}/send",
                 "/cn/v1/applications", "/cn/v1/applications/{application_id}", "/cn/v1/clippers/{clipper_id}/connections/start",
                 "/cn/v1/clippers/{clipper_id}/connections/complete", "/cn/v1/clippers/{clipper_id}/age-check",
                 "/cn/v1/clippers/{clipper_id}/agreement-acceptances", "/cn/v1/clippers/{clipper_id}/disclosure-training",
                 "/cn/v1/clippers/{clipper_id}/admission", "/cn/v1/clippers/{clipper_id}",
                 "/cn/v1/campaigns/{campaign_id}/network-config", "/cn/v1/campaigns/{campaign_id}/rulebook-announcements",
                 "/cn/v1/campaigns/{campaign_id}/enrolments", "/cn/v1/enrolments/{enrolment_id}/kit-acknowledgment",
                 "/cn/v1/clippers/{clipper_id}/messages", "/cn/v1/templates/proposals", "/cn/v1/rules/proposals",
                 "/cn/v1/rules/decisions", "/cn/v1/disputes", "/cn/v1/disputes/{dispute_id}",
                 "/cn/v1/disputes/{dispute_id}/outcome", "/cn/v1/discipline/sync", "/cn/v1/clippers/{clipper_id}/ban-decision",
                 "/cn/v1/tiers/run", "/cn/v1/messages/flush", "/cn/v1/clippers/{clipper_id}/offboarding",
                 "/cn/v1/audit/export"):
        assert need in paths, need


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_but_health_needs_the_bearer(method, path):
    for hd in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"},
               {"Authorization": "Bearer tök".encode("utf-8")}, {"Authorization": f"Bearer {SERVICE_TOKEN}x"}):
        r = H.client.request(method, _path(path), headers=hd, json={"request_id": "x"} if method in ("POST", "PUT") else None)
        assert r.status_code == 401, (method, path, hd, r.status_code)


def test_health_is_open_and_async():
    assert H.client.get("/health").status_code == 200


CALLER_ROUTES = [("POST", "/cn/v1/opt-ins", "scheduler"), ("POST", "/cn/v1/applications", "scheduler"),
                 ("POST", "/cn/v1/clippers/x/admission", "creative_production"),
                 ("POST", "/cn/v1/campaigns/c/rulebook-announcements", "hub"),
                 ("POST", "/cn/v1/discipline/sync", "hub"), ("POST", "/cn/v1/tiers/run", "hub"),
                 ("POST", "/cn/v1/messages/flush", "hub"), ("POST", "/cn/v1/campaigns/c/enrolments", "scheduler"),
                 ("GET", "/cn/v1/clippers/x/messages", "scheduler"), ("POST", "/cn/v1/disputes", "scheduler"),
                 ("POST", "/cn/v1/offboarding/run", "hub"), ("POST", "/cn/v1/disputes/sla-run", "hub")]


@pytest.mark.parametrize("method,path,wrong", CALLER_ROUTES)
def test_wrong_or_missing_caller_is_403(method, path, wrong):
    for caller in (wrong, None, "not-a-token-" + "z" * 30):
        hd = H.headers(caller=caller)
        r = H.client.request(method, path, headers=hd, json={"request_id": rid()} if method == "POST" else None)
        assert r.status_code in (403, 422), (path, caller, r.status_code)
        if r.status_code == 422:      # validation ran before the caller check only when the body is incomplete
            r2 = H.client.request(method, path, headers=hd, json=None)
            assert r2.status_code in (403, 422)


def test_contact_data_only_to_the_hub():
    h = Harness().ready()
    cid = h.apply().json()["clipper_id"]
    for caller in ("scheduler", "onboarding", "compliance_38", "finance_31"):
        j = h.get(f"/cn/v1/clippers/{cid}", caller=caller).json()
        assert "contact" not in j and "clip@example.com" not in json.dumps(j)
    assert h.get(f"/cn/v1/clippers/{cid}", caller="hub").json()["contact"]["email"] == "clip@example.com"


def test_offboarding_trigger_needs_the_right_identity():
    h = Harness().ready()
    cid = h.admitted_clipper()
    assert h.post(f"/cn/v1/clippers/{cid}/offboarding", {"request_id": rid(), "trigger": "clipper_request"},
                  caller="scheduler").status_code == 403
    assert h.post(f"/cn/v1/clippers/{cid}/offboarding", {"request_id": rid(), "trigger": "andre_decision"},
                  caller="hub").status_code == 403
    assert h.post(f"/cn/v1/clippers/{cid}/offboarding", {"request_id": rid(), "trigger": "andre_decision"},
                  andre=ANDRE_TOKEN).status_code == 200


# ------------------------------------------------------------------------------------------------ limits

def _raw(h, path, data: bytes, ctype="application/json", extra=None):
    hd = {**h.headers(caller="hub"), "Content-Type": ctype, **(extra or {})}
    return h.client.post(path, content=data, headers=hd)


def test_body_limits_content_type_and_shape():
    h = Harness()
    assert _raw(h, "/cn/v1/applications", b"{" + b" " * (17 * 1024) + b"}").status_code == 413
    assert _raw(h, "/cn/v1/recruiting/campaigns", b"{" + b" " * (400 * 1024) + b"}").status_code != 413
    assert _raw(h, "/cn/v1/applications", b"x=1", ctype="application/x-www-form-urlencoded").status_code == 415
    assert _raw(h, "/cn/v1/applications", b"[" * 40 + b"]" * 40).status_code == 422
    assert _raw(h, "/cn/v1/applications", b"[" + b"1," * 21000 + b"1]", ).status_code in (413, 422)
    assert h.client.get("/cn/v1/clippers/" + "a" * 5000, headers=h.headers(caller="hub")).status_code == 414
    assert h.client.get("/health", headers={"X-Big": "b" * 17000}).status_code == 431
    r = _raw(h, "/cn/v1/applications", b'{"request_id": "x", "email": 5}')
    assert r.status_code == 422 and "5" not in json.dumps(r.json().get("detail", [])[0].get("msg", ""))


def test_error_bodies_never_echo_input():
    h = Harness()
    secret = "SECRET-DOB-1999-12-31"
    r = h.post("/cn/v1/applications", {"request_id": rid(), "email": secret}, caller="hub")
    assert r.status_code == 422 and secret not in r.text


def test_bounded_strings_and_control_characters():
    h = Harness().ready()
    assert h.apply(display_name="x" * 81).status_code == 422
    assert h.apply(display_name="bad\x07name").status_code == 422
    assert h.apply(statement="s" * 2001).status_code == 422
    assert h.apply(declared_region="CA-PQ", declared_country="CA").status_code == 422      # not an ISO 3166-2 code
    assert h.apply(declared_country="XX").status_code == 422
    assert h.apply(time_zone="Mars/Olympus").status_code == 422


# ------------------------------------------------------------------------------------------------ idempotency

def test_identical_retry_returns_the_same_answer_and_records_nothing_new():
    h = Harness().ready()
    cid = h.ready_applicant()
    a = h.admit(cid, request_id="adm-1")
    n = len(h.ledger.events)
    b = h.admit(cid, request_id="adm-1")
    assert a.json() == b.json() and len(h.ledger.events) == n


def test_request_id_reuse_after_15_minutes_is_409():
    h = Harness().ready()
    r = h.apply(request_id="late-1")
    assert r.status_code == 201
    h.clock.advance(minutes=16)
    assert h.apply(request_id="late-1").status_code == 409


def test_refused_admission_replay_re_evaluates():
    h = Harness().ready()
    cid = h.ready_applicant()
    h.ports.finance.form = False
    a = h.admit(cid, request_id="adm-x").json()
    assert a["admitted"] is False
    h.ports.finance.form = True
    b = h.admit(cid, request_id="adm-x").json()
    assert b["admitted"] is True and b["admission_id"] != a["admission_id"]


def test_idempotency_survives_restart_for_rulings(tmp_path):
    h = Harness(data_dir=str(tmp_path / "d")).ready()
    cid = h.ready_applicant()
    a = h.admit(cid, request_id="adm-r").json()
    h2 = Harness(data_dir=str(tmp_path / "d"), ledger=h.ledger, clock=h.clock, ports=h.ports)
    assert h2.admit(cid, request_id="adm-r").json() == a
    other = h2.ready_applicant("o@example.com")
    assert h2.admit(other, request_id="adm-r").status_code == 409
