"""HttpComplianceDepartment (Compliance spec §F.1) against in-process mock transports — no network."""

import json

import httpx
import pytest

from integrations.compliance38 import HttpComplianceDepartment, compliance_from_env
from integrations.departments import NotBuiltComplianceDepartment

OK_BLOCKED = {"ruling_id": "cmp-rul-abc", "gate": "activation", "subject_id": "client_1", "lane": "client", "allowed": False,
              "unmet": [{"code": "rule_not_in_force"}], "unmet_lines": ["compliance_38/CQ-01/rule_not_in_force: x [no source url]"],
              "detail": "cmp-rul-abc", "register_version": 1, "evaluated_at": "2026-09-26T12:00:00Z", "ledger_event_id": "cmp-rul-abc"}


def _client(handler):
    return HttpComplianceDepartment("http://compliance.test", "svc-token", "caller-token",
                                    transport=httpx.MockTransport(handler))


def test_posts_the_protocol_body_with_both_tokens():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=OK_BLOCKED)
    r = _client(h).rule("client_1", "client", {"flags": {}})
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/compliance/v1/rule"
    assert seen[0].headers["Authorization"] == "Bearer svc-token"
    assert seen[0].headers["X-Compliance-Caller-Token"] == "caller-token"
    assert body["subject_id"] == "client_1" and body["lane"] == "client" and body["facts"] == {"flags": {}}
    assert body["request_id"].startswith("onb-")
    assert r.allowed is False and r.unmet == tuple(OK_BLOCKED["unmet_lines"]) and r.detail == "cmp-rul-abc"


def test_allowed_ruling():
    r = _client(lambda req: httpx.Response(200, json={**OK_BLOCKED, "allowed": True, "unmet": [], "unmet_lines": []})).rule(
        "c", "client", {})
    assert r.allowed is True and r.unmet == () and r.detail == "cmp-rul-abc"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 422])
def test_refusals_are_not_allowed_and_not_retried(status):
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(status, json={"detail": "x"})
    r = _client(h).rule("c", "client", {})
    assert r.allowed is False and f"({status})" in r.unmet[0] and len(calls) == 1
    assert r.unmet[0].startswith("compliance_department_38_ruling: Compliance (38) unreachable or refused")


def test_5xx_and_timeouts_retry_once_with_the_same_request_id_then_fail_closed():
    ids = []

    def h(req):
        ids.append(json.loads(req.content)["request_id"])
        if len(ids) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(503, json={"detail": "ledger down", "issued": False})
    r = _client(h).rule("c", "client", {})
    assert r.allowed is False and "(503)" in r.unmet[0]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_retry_can_succeed():
    n = []

    def h(req):
        n.append(1)
        return httpx.Response(503) if len(n) == 1 else httpx.Response(200, json=OK_BLOCKED)
    assert _client(h).rule("c", "client", {}).detail == "cmp-rul-abc"


@pytest.mark.parametrize("payload", [
    b"not json", b"[]", json.dumps({"allowed": True}).encode(),
    json.dumps({**OK_BLOCKED, "allowed": "yes"}).encode(),
    json.dumps({**OK_BLOCKED, "allowed": True}).encode(),            # allowed with unmet lines: inconsistent
    json.dumps({**OK_BLOCKED, "unmet_lines": []}).encode(),          # blocked without reasons: inconsistent
    json.dumps({**OK_BLOCKED, "gate": "payout"}).encode(),
])
def test_unparseable_or_inconsistent_answers_fail_closed(payload):
    r = _client(lambda req: httpx.Response(200, content=payload)).rule("c", "client", {})
    assert r.allowed is False and r.detail == "not allowed yet"


def test_unreachable_fails_closed():
    def h(req):
        raise httpx.ConnectError("refused")
    r = _client(h).rule("c", "client", {})
    assert r.allowed is False and "ConnectError" in r.unmet[0]


def test_env_wiring_defaults_to_the_stand_in():
    assert isinstance(compliance_from_env({}), NotBuiltComplianceDepartment)
    assert isinstance(compliance_from_env({"COMPLIANCE_SERVICE_URL": "http://x", "COMPLIANCE_SERVICE_TOKEN": "t"}),
                      NotBuiltComplianceDepartment)
    assert isinstance(compliance_from_env({"COMPLIANCE_SERVICE_URL": "http://x", "COMPLIANCE_SERVICE_TOKEN": "t",
                                           "COMPLIANCE_CALLER_TOKEN": "c"}), HttpComplianceDepartment)


def test_build_service_from_env_wires_it(monkeypatch):
    import api
    svc = api.build_service_from_env({"COMPLIANCE_SERVICE_URL": "http://127.0.0.1:1", "COMPLIANCE_SERVICE_TOKEN": "t",
                                      "COMPLIANCE_CALLER_TOKEN": "c"})
    assert isinstance(svc.depts.compliance, HttpComplianceDepartment)
    assert isinstance(api.build_service_from_env({}).depts.compliance, NotBuiltComplianceDepartment)
