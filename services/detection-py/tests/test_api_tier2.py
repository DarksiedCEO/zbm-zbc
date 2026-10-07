from fastapi.testclient import TestClient

from api import app
from conftest import TEST_SERVICE_TOKEN
from _fx import AS_OF, AS_OF_WIRE, TENANT, make_finding  # noqa: F401

client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})


def test_server_side_attribution_over_rest():
    events = client.get("/fixtures/tier2/server-side-events").json()
    r = client.post("/agents/server-side-attribution/detect", json={"client_id": TENANT, "events": events})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"ord_3001"}


def test_cross_channel_attribution_over_rest():
    tps = client.get("/fixtures/tier2/channel-touchpoints").json()
    r = client.post("/agents/cross-channel-attribution/detect", json={"client_id": TENANT, "touchpoints": tps})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"ord_4001"}


def test_platform_integration_over_rest():
    statuses = client.get("/fixtures/tier2/platform-connections").json()
    r = client.post("/agents/platform-integration/detect", json={"client_id": TENANT, "statuses": statuses})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"tiktok_shop"}


def test_contract_pricing_term_drift_over_rest():
    terms = client.get("/fixtures/tier2/contract-terms").json()
    r = client.post("/agents/contract-pricing-term-drift/detect", json={"client_id": TENANT, "terms": terms})
    assert r.status_code == 200
    findings = r.json()["findings"]
    assert len(findings) == 1
    assert findings[0]["recoverable_value"]["amount_usd"] == "900.00"  # money is a two-decimal JSON string
