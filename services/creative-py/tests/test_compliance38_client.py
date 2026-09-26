"""HttpCompliance38 (Compliance spec §F.1) against in-process mock transports — no network."""

import json

import httpx
import pytest

from shared.compliance38 import HttpCompliance38, compliance_from_env
from shared.departments import NotBuiltCompliance38

BLOCKED = {"ruling_id": "cmp-rul-xyz", "gate": "payout", "allowed": False, "subject_kind": "zbc_clip",
           "reason": "3 unmet: compliance_38/CQ-01/rule_not_in_force: x [no source url]", "reference": "cmp-rul-xyz",
           "unmet": [], "unmet_lines": ["compliance_38/CQ-01/rule_not_in_force: x [no source url]"],
           "register_version": 1, "evaluated_at": "2026-09-26T12:00:00Z", "ledger_event_id": "cmp-rul-xyz"}


def _client(handler):
    return HttpCompliance38("http://compliance.test", "svc-token", "caller-token", transport=httpx.MockTransport(handler))


def test_payout_review_posts_the_protocol_body():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json=BLOCKED)
    facts = {"campaign_id": "c1", "clip_review": "pass"}
    g = _client(h).review("zbc_clip", "sub-1", facts)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/compliance/v1/review"
    assert seen[0].headers["Authorization"] == "Bearer svc-token"
    assert seen[0].headers["X-Compliance-Caller-Token"] == "caller-token"
    assert body["subject_kind"] == "zbc_clip" and body["subject_id"] == "sub-1" and body["facts"] == facts
    assert "caller_context" not in body and body["request_id"].startswith("cre-")
    assert g == type(g)("compliance_38", False, BLOCKED["reason"], "cmp-rul-xyz")


def test_publish_review_moves_export_and_rights_into_caller_context():
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={**BLOCKED, "gate": "publish", "subject_kind": "zbm_work"})
    facts = {"brief_id": "b1", "export": {"format": "mp4"}, "rights": {"cleared": True}}
    _client(h).review("zbm_work", "w1", facts)
    assert seen[0]["facts"] == {"brief_id": "b1"}
    assert seen[0]["caller_context"] == {"export": {"format": "mp4"}, "rights": {"cleared": True}}
    assert facts["export"] == {"format": "mp4"}  # the caller's dict is not mutated


def test_allowed():
    g = _client(lambda r: httpx.Response(200, json={**BLOCKED, "allowed": True, "reason": "allowed under register v2",
                                                    "unmet": [], "unmet_lines": []})).review("zbc_clip", "s", {})
    assert g.allowed is True and g.reference == "cmp-rul-xyz"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_refusals_not_allowed_no_retry(status):
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(status)
    g = _client(h).review("zbc_clip", "s", {})
    assert g.allowed is False and g.reason == f"Compliance (38) unreachable or refused ({status}): not allowed"
    assert len(calls) == 1 and g.department == "compliance_38"


def test_5xx_and_timeouts_retry_once_with_same_request_id():
    ids = []

    def h(req):
        ids.append(json.loads(req.content)["request_id"])
        if len(ids) == 1:
            raise httpx.ConnectTimeout("slow")
        return httpx.Response(503)
    g = _client(h).review("zbc_clip", "s", {})
    assert g.allowed is False and "(503)" in g.reason and len(ids) == 2 and ids[0] == ids[1]


@pytest.mark.parametrize("payload", [
    b"<html>", b"{}", json.dumps({**BLOCKED, "allowed": 1}).encode(),
    json.dumps({**BLOCKED, "gate": "publish"}).encode(),                 # wrong gate for a clip
    json.dumps({**BLOCKED, "allowed": True}).encode(),                   # allowed with a blocking reason
])
def test_bad_answers_fail_closed(payload):
    g = _client(lambda r: httpx.Response(200, content=payload)).review("zbc_clip", "s", {})
    assert g.allowed is False and "unreachable or refused" in g.reason


def test_env_wiring_defaults_to_the_stand_in():
    assert isinstance(compliance_from_env({}), NotBuiltCompliance38)
    assert isinstance(compliance_from_env({"COMPLIANCE_SERVICE_URL": "http://x", "COMPLIANCE_CALLER_TOKEN": "c"}),
                      NotBuiltCompliance38)
    assert isinstance(compliance_from_env({"COMPLIANCE_SERVICE_URL": "http://x", "COMPLIANCE_SERVICE_TOKEN": "t",
                                           "COMPLIANCE_CALLER_TOKEN": "c"}), HttpCompliance38)
