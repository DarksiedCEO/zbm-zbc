"""
Proves the auth dependency actually enforces something on the
Fulfillment service — same pattern as detection-py/tests/test_auth.py,
written in from day 1 rather than added after an independent review.
"""

from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN

anon_client = TestClient(app)
auth_client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
wrong_token_client = TestClient(app, headers={"Authorization": "Bearer not-the-real-token"})


def test_health_is_open_with_no_token():
    r = anon_client.get("/health")
    assert r.status_code == 200


def test_fixture_endpoint_rejects_missing_token():
    r = anon_client.get("/fixtures/call-events")
    assert r.status_code == 401


def test_fixture_endpoint_rejects_wrong_token():
    r = wrong_token_client.get("/fixtures/call-events")
    assert r.status_code == 401


def test_fixture_endpoint_accepts_correct_token():
    r = auth_client.get("/fixtures/call-events")
    assert r.status_code == 200


def test_agent_endpoint_rejects_missing_token():
    r = anon_client.post("/agents/missed-call-detection/detect", json={"call_events": []})
    assert r.status_code == 401


def test_agent_endpoint_rejects_wrong_token():
    r = wrong_token_client.post("/agents/missed-call-detection/detect", json={"call_events": []})
    assert r.status_code == 401


def test_agent_endpoint_accepts_correct_token():
    r = auth_client.post("/agents/missed-call-detection/detect", json={"call_events": []})
    assert r.status_code == 200


def test_malformed_authorization_header_is_rejected():
    r = TestClient(app, headers={"Authorization": TEST_SERVICE_TOKEN}).get("/fixtures/call-events")
    assert r.status_code == 401


def test_resolution_writeback_endpoint_rejects_missing_token():
    r = anon_client.post("/agents/resolution-writeback/resolve", json={"events": []})
    assert r.status_code == 401


# --- regressions from the Sep 22 2026 independent review --------------------

def test_non_ascii_bearer_token_is_rejected_not_500():
    """CONFIRMED finding: hmac.compare_digest raises TypeError on a
    non-ASCII str comparison, which was unhandled and surfaced as an
    unauthenticated 500 instead of a 401 — an attacker-reachable crash
    that also leaks more than "invalid token" should. Passed as raw
    bytes because httpx's own header encoding refuses non-ASCII str
    values client-side before they'd ever reach the server."""
    r = TestClient(app, headers={"Authorization": b"Bearer caf\xc3\xa9"}).get("/fixtures/call-events")
    assert r.status_code == 401


def test_docs_redoc_and_openapi_are_disabled():
    """CONFIRMED finding: /docs, /redoc, /openapi.json were reachable
    with no auth at all, exposing every route name and shape to an
    unauthenticated caller. Disabled outright rather than gated."""
    for path in ["/docs", "/redoc", "/openapi.json"]:
        r = anon_client.get(path)
        assert r.status_code == 404, f"{path} should be disabled, got {r.status_code}"
