"""HttpComplianceDepartment (Compliance spec §F.1) against in-process mock transports — no network."""

import json

import httpx
import pytest

from integrations.compliance38 import HttpComplianceDepartment, compliance_from_env, facts_sha256
from integrations.departments import NotBuiltComplianceDepartment

OK_BLOCKED = {"ruling_id": "cmp-rul-abc", "gate": "activation", "subject_id": "client_1", "lane": "client", "allowed": False,
              "unmet": [{"code": "rule_not_in_force"}], "unmet_lines": ["compliance_38/CQ-01/rule_not_in_force: x [no source url]"],
              "detail": "cmp-rul-abc", "register_version": 1, "evaluated_at": "2026-09-26T12:00:00Z", "ledger_event_id": "cmp-rul-abc"}


def _client(handler):
    return HttpComplianceDepartment("http://compliance.test", "svc-token", "caller-token",
                                    transport=httpx.MockTransport(handler))


def _echo(ans, seen=None, **over):
    """A server that answers ``ans`` for THIS request: it echoes request_id and the facts' SHA-256 and says the
    seed is pinned, as the real service does (AEGIS N15-8)."""
    def h(req):
        b = json.loads(req.content)
        if seen is not None:
            seen.append(req)
        return httpx.Response(200, json={**ans, "request_id": b["request_id"], "facts_sha256": facts_sha256(b["facts"]),
                                         "seed_pinned": True, **over})
    return h


def test_posts_the_protocol_body_with_both_tokens():
    seen = []
    r = _client(_echo(OK_BLOCKED, seen)).rule("client_1", "client", {"flags": {}})
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/compliance/v1/rule"
    assert seen[0].headers["Authorization"] == "Bearer svc-token"
    assert seen[0].headers["X-Compliance-Caller-Token"] == "caller-token"
    assert body["subject_id"] == "client_1" and body["lane"] == "client" and body["facts"] == {"flags": {}}
    assert body["request_id"].startswith("onb-")
    assert r.allowed is False and r.unmet == tuple(OK_BLOCKED["unmet_lines"]) and r.detail == "cmp-rul-abc"


def test_allowed_ruling():
    # the answer must name the requested subject (AEGIS N14-10): request "client_1", as OK_BLOCKED names
    r = _client(_echo({**OK_BLOCKED, "allowed": True, "unmet": [], "unmet_lines": []})).rule("client_1", "client", {})
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
    ok = _echo(OK_BLOCKED)

    def h(req):
        n.append(1)
        return httpx.Response(503) if len(n) == 1 else ok(req)
    assert _client(h).rule("client_1", "client", {}).detail == "cmp-rul-abc"   # same subject as the answer (N14-10)


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


# --- AEGIS round 14: N14-7 total deadline, N14-8 parse never raises + size cap, N14-10 subject echo ---------

import time  # noqa: E402

ALLOWED_C1 = {**OK_BLOCKED, "allowed": True, "unmet": [], "unmet_lines": []}


class _Drip(httpx.SyncByteStream):
    """A server that keeps making progress (one byte every ``gap`` seconds) but never finishes in time."""

    def __init__(self, body: bytes, gap: float):
        self.body, self.gap = body, gap

    def __iter__(self):
        for b in self.body:
            time.sleep(self.gap)
            yield bytes([b])


def test_n14_7_total_deadline_is_wall_clock_not_per_byte():
    body = json.dumps(ALLOWED_C1).encode()
    c = HttpComplianceDepartment("http://compliance.test", "svc-token", "caller-token", timeout=1.0,
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=_Drip(body, 0.05))))
    t0 = time.monotonic()
    r = c.rule("client_1", "client", {})
    dt = time.monotonic() - t0
    assert r.allowed is False, r
    assert dt < 2.5, f"call took {dt:.1f}s with a 1.0s deadline"


@pytest.mark.parametrize("payload", [
    b'{"allowed": ' + b"[" * 200_000 + b"]" * 200_000 + b"}",
    b'{"allowed": true, "x": ' + b"1" * 5000 + b"e999999}",
])
def test_n14_8_parse_errors_never_raise(payload):
    r = _client(lambda req: httpx.Response(200, content=payload)).rule("client_1", "client", {})
    assert r.allowed is False and r.detail == "not allowed yet"


def test_n14_8_response_size_is_capped_before_parsing():
    huge = json.dumps({**ALLOWED_C1, "pad": "A" * (2 * 1024 * 1024)}).encode()
    r = _client(lambda req: httpx.Response(200, content=huge)).rule("client_1", "client", {})
    assert r.allowed is False and "too large" in r.unmet[0]


@pytest.mark.parametrize("change", [{"subject_id": "other"}, {"lane": "zbc_creator"}, {"subject_id": None},
                                    {"_drop": "subject_id"}, {"_drop": "lane"}])
def test_n14_10_reply_must_name_the_same_subject_and_lane(change):
    ans = dict(ALLOWED_C1)
    if "_drop" in change:
        ans.pop(change["_drop"])
    else:
        ans.update(change)
    r = _client(_echo(ans)).rule("client_1", "client", {})   # echo right (N15-8): only the subject is wrong
    assert r.allowed is False and "inconsistent" in r.unmet[0]


def test_n14_10_matching_reply_is_still_allowed():
    r = _client(_echo(ALLOWED_C1)).rule("client_1", "client", {})
    assert r.allowed is True


# --- AEGIS round 15: N15-8 request_id / facts_sha256 echo, unpinned seed refused by default -----------------

FACTS = {"jurisdiction": {"declared_country": "IT", "declared_region": None, "attested": True, "attestation_ref": "a"},
         "note": "Pubblicità ＃ＡＤ", "n": 1.5}


@pytest.mark.parametrize("over", [{"request_id": "onb-someone-else"}, {"request_id": None},
                                  {"facts_sha256": "0" * 64}, {"facts_sha256": None},
                                  {"facts_sha256": facts_sha256({"n": 1.5})}, {"seed_pinned": None},
                                  {"seed_pinned": "true"}])
def test_n15_8_answer_must_echo_request_id_and_facts_sha256(over):
    r = _client(_echo(ALLOWED_C1, **over)).rule("client_1", "client", FACTS)
    assert r.allowed is False and "inconsistent" in r.unmet[0], over


def test_n15_8_facts_sha256_formula():
    assert facts_sha256({"b": 1, "a": "é"}) == __import__("hashlib").sha256(b'{"a":"\\u00e9","b":1}').hexdigest()


def test_n15_8_unpinned_seed_refused_unless_explicitly_accepted():
    h = _echo(ALLOWED_C1, seed_pinned=False)
    r = _client(h).rule("client_1", "client", FACTS)
    assert r.allowed is False and "unpinned" in r.unmet[0]
    ok = HttpComplianceDepartment("http://compliance.test", "svc-token", "caller-token",
                                  transport=httpx.MockTransport(h), accept_unpinned=True).rule("client_1", "client", FACTS)
    assert ok.allowed is True
    env = {"COMPLIANCE_SERVICE_URL": "http://x", "COMPLIANCE_SERVICE_TOKEN": "t", "COMPLIANCE_CALLER_TOKEN": "c"}
    assert compliance_from_env(env)._accept_unpinned is False
    assert compliance_from_env({**env, "COMPLIANCE_ACCEPT_UNPINNED": "true"})._accept_unpinned is False
    assert compliance_from_env({**env, "COMPLIANCE_ACCEPT_UNPINNED": "1"})._accept_unpinned is True
