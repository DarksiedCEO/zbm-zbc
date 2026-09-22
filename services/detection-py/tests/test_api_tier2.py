from fastapi.testclient import TestClient

from api import app

client = TestClient(app)


def test_server_side_attribution_over_rest():
    events = client.get("/fixtures/tier2/server-side-events").json()
    r = client.post("/agents/server-side-attribution/detect", json={"events": events})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"ord_3001"}


def test_cross_channel_attribution_over_rest():
    tps = client.get("/fixtures/tier2/channel-touchpoints").json()
    r = client.post("/agents/cross-channel-attribution/detect", json={"touchpoints": tps})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"ord_4001"}


def test_platform_integration_over_rest():
    statuses = client.get("/fixtures/tier2/platform-connections").json()
    r = client.post("/agents/platform-integration/detect", json={"statuses": statuses})
    assert r.status_code == 200
    flagged = {f["entity_id"] for f in r.json()["findings"]}
    assert flagged == {"client_a1:tiktok_shop"}


def test_contract_pricing_term_drift_over_rest():
    terms = client.get("/fixtures/tier2/contract-terms").json()
    r = client.post("/agents/contract-pricing-term-drift/detect", json={"terms": terms})
    assert r.status_code == 200
    findings = r.json()["findings"]
    assert len(findings) == 1
    assert findings[0]["recoverable_value"]["amount_usd"] == 900.00
