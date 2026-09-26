"""HttpCompliance38 (Compliance spec §F.1) against in-process mock transports — no network."""

import json

import httpx
import pytest

from shared.compliance38 import HttpCompliance38, compliance_from_env
from shared.departments import NotBuiltCompliance38

# The real service names the subject back (ruling_view); AEGIS N14-10 made the client require it.
BLOCKED = {"ruling_id": "cmp-rul-xyz", "gate": "payout", "allowed": False, "subject_kind": "zbc_clip", "subject_id": "sub-1",
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
                                                    "unmet": [], "unmet_lines": []})).review("zbc_clip", "sub-1", {})
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


# --- AEGIS round 14: N14-7 total deadline, N14-8 parse never raises + size cap, N14-10 subject echo ---------

import time  # noqa: E402

ALLOWED_SUB1 = {**BLOCKED, "subject_id": "sub-1", "allowed": True, "reason": "allowed under register v2", "unmet": [],
                "unmet_lines": []}


class _Drip(httpx.SyncByteStream):
    """A server that keeps making progress (one byte every ``gap`` seconds) but never finishes in time."""

    def __init__(self, body: bytes, gap: float):
        self.body, self.gap = body, gap

    def __iter__(self):
        for b in self.body:
            time.sleep(self.gap)
            yield bytes([b])


def test_n14_7_total_deadline_is_wall_clock_not_per_byte():
    body = json.dumps(ALLOWED_SUB1).encode()
    c = HttpCompliance38("http://compliance.test", "svc-token", "caller-token", timeout=1.0,
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=_Drip(body, 0.05))))
    t0 = time.monotonic()
    g = c.review("zbc_clip", "sub-1", {})
    dt = time.monotonic() - t0
    assert g.allowed is False, g
    assert dt < 2.5, f"call took {dt:.1f}s with a 1.0s deadline"


@pytest.mark.parametrize("payload", [
    b'{"allowed": ' + b"[" * 200_000 + b"]" * 200_000 + b"}",           # RecursionError in the JSON parser
    b'{"allowed": true, "x": ' + b"1" * 5000 + b"e999999}",             # float overflow -> inf, never an exception path
])
def test_n14_8_parse_errors_never_raise(payload):
    g = _client(lambda r: httpx.Response(200, content=payload)).review("zbc_clip", "sub-1", {})
    assert g.allowed is False and "unreachable or refused" in g.reason


def test_n14_8_response_size_is_capped_before_parsing():
    huge = json.dumps({**ALLOWED_SUB1, "pad": "A" * (2 * 1024 * 1024)}).encode()
    g = _client(lambda r: httpx.Response(200, content=huge)).review("zbc_clip", "sub-1", {})
    assert g.allowed is False and "too large" in g.reason


@pytest.mark.parametrize("change", [{"subject_id": "someone-else"}, {"subject_kind": "zbm_work"}, {"subject_id": None},
                                    {"_drop": "subject_id"}, {"_drop": "subject_kind"}])
def test_n14_10_reply_must_name_the_same_subject_and_kind(change):
    ans = dict(ALLOWED_SUB1)
    if "_drop" in change:
        ans.pop(change["_drop"])
    else:
        ans.update(change)
    g = _client(lambda r: httpx.Response(200, json=ans)).review("zbc_clip", "sub-1", {})
    assert g.allowed is False and "inconsistent" in g.reason


def test_n14_10_matching_reply_is_still_allowed():
    g = _client(lambda r: httpx.Response(200, json=ALLOWED_SUB1)).review("zbc_clip", "sub-1", {})
    assert g.allowed is True
