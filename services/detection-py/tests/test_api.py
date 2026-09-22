"""
Real HTTP round-trip tests against the FastAPI app (in-process TestClient —
actual request/response serialization through pydantic, not a mock).
"""

from fastapi.testclient import TestClient

from api import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_fixture_orders_endpoint_serves_the_shared_pool():
    r = client.get("/fixtures/orders")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 7
    assert any(o["order_id"] == "ord_1002" for o in data)


def test_affiliate_agent_over_rest_matches_direct_call():
    orders = client.get("/fixtures/orders").json()
    r = client.post("/agents/affiliate-coupon-extension/detect", json={"orders": orders})
    assert r.status_code == 200
    findings = r.json()["findings"]
    flagged = {f["entity_id"] for f in findings}
    assert "ord_1002" in flagged
    assert "ord_1001" not in flagged  # control case still clean over the wire


def test_correlation_endpoint_catches_double_claim_over_rest():
    orders = client.get("/fixtures/orders").json()
    aff = client.post("/agents/affiliate-coupon-extension/detect", json={"orders": orders}).json()["findings"]
    disc = client.post("/agents/discount-misuse/detect", json={"orders": orders}).json()["findings"]

    r = client.post("/correlation/overlaps", json={"findings": aff + disc})
    assert r.status_code == 200
    overlaps = r.json()
    assert "ord_1007" in overlaps
    assert len(overlaps["ord_1007"]) == 2


def test_renewal_agent_over_rest():
    subs = client.get("/fixtures/subscriptions").json()
    r = client.post("/agents/renewal-never-triggered/detect", json={"subscriptions": subs})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"sub_2001"}
