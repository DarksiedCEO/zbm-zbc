"""LedgerClient (contract section 2) and the Revenue Recovery HTTP client
(contract section 3), against in-process mock transports — no network."""

import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest

from integrations.revenue_recovery import CORRELATION_ROUTE, ROUTES, HttpRevenueRecoveryClient, RevenueRecoveryError
from intelligences import i06_audit_baseline as i06
from ledger import FakeLedgerClient, HttpLedgerClient, LedgerWriteError, payload_sha256

DETECTION_API = Path(__file__).resolve().parents[2] / "detection-py" / "src" / "api.py"


def test_payload_sha256_matches_the_contract_formula():
    payload = {"b": 1, "a": "x", "amount": "12.30"}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    assert payload_sha256(payload) == expected


def _ledger(status, seen):
    def handler(req):
        seen.append(req)
        return httpx.Response(status, json={"seq": 1})
    return HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))


def test_http_ledger_posts_the_contract_body_with_bearer_auth():
    seen = []
    _ledger(201, seen).record_event("onb-1", "onboarding", "gate_ruling", "intel_15_compliance", "client_1", {"x": 1}, "Blocked: 2 unmet")
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/ledger/events"
    assert req.headers["Authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body == {"event_id": "onb-1", "department": "onboarding", "event_type": "gate_ruling", "actor": "intel_15_compliance",
                    "subject_id": "client_1", "payload_sha256": payload_sha256({"x": 1}), "summary": "Blocked: 2 unmet"}


def test_http_ledger_200_idempotent_retry_is_success():
    _ledger(200, []).record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")


@pytest.mark.parametrize("status", [400, 401, 409, 500, 503])
def test_http_ledger_any_other_status_fails_closed(status):
    with pytest.raises(LedgerWriteError):
        _ledger(status, []).record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")


def test_http_ledger_unreachable_fails_closed():
    def handler(req):
        raise httpx.ConnectError("refused")
    lc = HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(LedgerWriteError, match="unreachable"):
        lc.record_event("onb-1", "onboarding", "t", "a", "s", {}, "x")


def test_invalid_contract_fields_are_refused_before_sending():
    for args in [("bad id!", "onboarding", "t", "a", "s"), ("onb-1", "Onboarding", "t", "a", "s"),
                 ("onb-1", "onboarding", "T-x", "a", "s"), ("onb-1", "onboarding", "t", "a", "s p a c e")]:
        with pytest.raises(LedgerWriteError):
            FakeLedgerClient().record_event(*args, {}, "x")


def test_summary_is_clipped_and_control_chars_removed():
    f = FakeLedgerClient()
    f.record_event("onb-1", "onboarding", "t", "a", "s", {}, "line1\nline2\x07" + "x" * 400)
    s = f.events[0]["summary"]
    assert len(s) == 280 and "\n" not in s and "\x07" not in s


def test_rr_routes_are_detection_py_real_routes():
    src = DETECTION_API.read_text()
    for kind, routes in ROUTES.items():
        for path, key in routes:
            assert f'@app.post("{path}"' in src, path
    assert f'@app.post("{CORRELATION_ROUTE}"' in src
    # request body keys match detection-py's request models
    for key in ("orders", "subscriptions", "events", "touchpoints", "statuses", "terms", "findings"):
        assert re.search(rf"^\s+{key}: list\[", src, re.M), key


def test_rr_http_client_calls_every_agent_for_a_kind_and_consumes_string_money():
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append((req.url.path, list(body), req.headers["Authorization"]))
        if req.url.path == CORRELATION_ROUTE:
            return httpx.Response(200, json={"ord_1": body["findings"]})
        return httpx.Response(200, json={"findings": [{
            "finding_id": f"f-{req.url.path.split('/')[2]}", "agent_id": "a", "leak_category": "discount_misuse",
            "entity_type": "order", "entity_id": "ord_1", "customer_id": "c", "cause_certainty": "named",
            "cause_description": "d", "recoverable_value": {"amount_usd": "12.30", "classification": "observed", "confidence": "high"}}]})

    rr = HttpRevenueRecoveryClient("http://rr.test", "rrtok", transport=httpx.MockTransport(handler))
    raw = rr.detect({"orders": [{"order_id": "o"}], "subscriptions": []})
    assert [c[0] for c in calls] == [p for p, _ in ROUTES["orders"]]
    assert all(c[1] == ["orders"] and c[2] == "Bearer rrtok" for c in calls)
    overlaps = rr.overlaps(raw)
    findings, rejected = i06.consume(raw, overlaps)
    assert rejected == [] and all(f.double_count_risk for f in findings)
    base = i06.baseline(findings)
    assert base.totals_by_classification == {} and base.double_count_entities == ["ord_1"]
    assert str(findings[0].recoverable_value.amount_usd) == "12.30"


def test_rr_http_client_errors():
    rr = HttpRevenueRecoveryClient("http://rr.test", "t", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RevenueRecoveryError):
        rr.detect({"orders": [{"x": 1}]})
    with pytest.raises(RevenueRecoveryError, match="no Revenue Recovery route"):
        rr.detect({"payroll": [{"x": 1}]})
