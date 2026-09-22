"""
Proves the auth dependency actually enforces something — not just that
authenticated requests succeed (every other test file already covers
that, incidentally, by attaching a valid token). Specifically checks:
  - /health stays open with no token (needed for basic liveness checks)
  - every other endpoint rejects a missing token
  - every other endpoint rejects a wrong token
  - a correct token is accepted
without which a silently-broken `require_auth` dependency (e.g. someone
removes it from a route during a future edit) would go undetected by the
rest of the suite, since those tests only ever exercise the happy path.
"""

from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN

anon_client = TestClient(app)  # deliberately no Authorization header
auth_client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
wrong_token_client = TestClient(app, headers={"Authorization": "Bearer not-the-real-token"})


def test_health_is_open_with_no_token():
    r = anon_client.get("/health")
    assert r.status_code == 200


def test_fixture_endpoint_rejects_missing_token():
    r = anon_client.get("/fixtures/orders")
    assert r.status_code == 401


def test_fixture_endpoint_rejects_wrong_token():
    r = wrong_token_client.get("/fixtures/orders")
    assert r.status_code == 401


def test_fixture_endpoint_accepts_correct_token():
    r = auth_client.get("/fixtures/orders")
    assert r.status_code == 200


def test_agent_endpoint_rejects_missing_token():
    r = anon_client.post("/agents/affiliate-coupon-extension/detect", json={"orders": []})
    assert r.status_code == 401


def test_agent_endpoint_rejects_wrong_token():
    r = wrong_token_client.post("/agents/affiliate-coupon-extension/detect", json={"orders": []})
    assert r.status_code == 401


def test_agent_endpoint_accepts_correct_token():
    r = auth_client.post("/agents/affiliate-coupon-extension/detect", json={"orders": []})
    assert r.status_code == 200


def test_correlation_endpoint_rejects_missing_token():
    r = anon_client.post("/correlation/overlaps", json={"findings": []})
    assert r.status_code == 401


def test_malformed_authorization_header_is_rejected():
    # Not "Bearer <token>" at all — a raw token with no scheme prefix.
    r = TestClient(app, headers={"Authorization": TEST_SERVICE_TOKEN}).get("/fixtures/orders")
    assert r.status_code == 401


# --- ported from fulfillment-py's Sep 22 2026 independent review ------------

def test_non_ascii_bearer_token_is_rejected_not_500():
    """CONFIRMED finding, ported from fulfillment-py: hmac.compare_digest
    raises TypeError on a non-ASCII str comparison, which was unhandled
    and surfaced as an unauthenticated 500 instead of a 401 — an
    attacker-reachable crash. Passed as raw bytes because httpx's own
    header encoding refuses non-ASCII str values client-side before
    they'd ever reach the server."""
    r = TestClient(app, headers={"Authorization": b"Bearer caf\xc3\xa9"}).get("/fixtures/orders")
    assert r.status_code == 401


def test_docs_redoc_and_openapi_are_disabled():
    """CONFIRMED finding, ported from fulfillment-py: /docs, /redoc,
    /openapi.json were reachable with no auth at all, exposing every
    route name and shape to an unauthenticated caller. Disabled outright
    rather than gated."""
    for path in ["/docs", "/redoc", "/openapi.json"]:
        r = anon_client.get(path)
        assert r.status_code == 404, f"{path} should be disabled, got {r.status_code}"
