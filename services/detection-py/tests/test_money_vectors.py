"""
F15 — the shared money wire-format vectors (fixtures/money_vectors.json).

The same file is loaded by orchestrator-go (internal/client/money_vectors_test.go)
and the dashboard (apps/dashboard-ts/tests/money.test.ts), so the three
implementations cannot drift apart: a vector added or changed there is
checked here too.

Three levels are checked for every string vector:
  1. `to_money` (Money, zero allowed) accepts / rejects it, and an accepted
     value serializes back to the identical string;
  2. `LabeledValue.amount_usd` (PositiveMoney) accepts / rejects it;
  3. the real HTTP route returns 200 / 422 for it as a line-item price —
     never 500.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import app
from conftest import TEST_SERVICE_TOKEN
from zbm_schema import DecisionConfidence, LabeledValue, ValueClassification, format_money, to_money
from zbm_schema.money import MAX_MONEY, WIRE_PATTERN
from _fx import AS_OF, AS_OF_WIRE, TENANT, make_finding  # noqa: F401

VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "fixtures" / "money_vectors.json").read_text(encoding="utf-8")
)
STRINGS = VECTORS["string_vectors"]
JSONS = VECTORS["json_vectors"]

client = TestClient(
    app, headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}, raise_server_exceptions=False
)


def _ids(vs, key):
    return [f"{i}:{v[key][:24]!r}" for i, v in enumerate(vs)]


def test_contract_block_matches_the_python_implementation():
    assert VECTORS["contract"]["pattern"] == WIRE_PATTERN.pattern
    assert VECTORS["contract"]["max"] == format_money(MAX_MONEY)


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_to_money_verdict(vec):
    s = vec["input"]
    if vec["money"] == "accept":
        assert format_money(to_money(s)) == s
    else:
        with pytest.raises(ValueError):
            to_money(s)


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_positive_money_verdict(vec):
    s = vec["input"]
    if vec["positive_money"] == "accept":
        lv = LabeledValue(amount_usd=s, classification=ValueClassification.OBSERVED, confidence=DecisionConfidence.HIGH)
        assert lv.model_dump(mode="json")["amount_usd"] == s
    else:
        with pytest.raises(ValidationError):
            LabeledValue(amount_usd=s, classification=ValueClassification.OBSERVED, confidence=DecisionConfidence.HIGH)


def _cart_body(price_json: str) -> bytes:
    return (
        '{"client_id":"fixture-pool","orders":[{"order_id":"o","customer_id":"c","placed_at":"2026-06-01T00:00:00Z",'
        '"status":"abandoned_cart","source_platform":"x","line_items":[{"sku":"s","unit_price_usd":'
        + price_json
        + ',"quantity":1}],'
        # E-4: the affiliate route at a 100% commission rate carries the subtotal exactly (the
        # abandoned-cart route no longer claims a figure).
        '"affiliate":{"affiliate_id":"a","click_timestamp":"2026-05-01T00:00:00Z",'
        '"order_timestamp":"2026-06-01T00:00:00Z","attribution_window_hours":24,"commission_rate_percent":100}}]}'
    ).encode()


@pytest.mark.parametrize("vec", STRINGS, ids=_ids(STRINGS, "input"))
def test_http_route_verdict_is_200_or_422_never_500(vec):
    r = client.post(
        "/agents/affiliate-coupon-extension/detect",
        content=_cart_body(json.dumps(vec["input"])),
        headers={"Content-Type": "application/json"},
    )
    if vec["positive_money"] == "accept":
        assert r.status_code == 200, r.text[:300]
        assert r.json()["findings"][0]["recoverable_value"]["amount_usd"] == vec["input"]
    else:
        assert r.status_code == 422, (r.status_code, r.text[:300])


@pytest.mark.parametrize("vec", JSONS, ids=_ids(JSONS, "json"))
def test_http_route_json_value_verdict(vec):
    """A JSON number is never money on the wire (Go and the ledger reject it
    too): accepting 12.345 as a number would silently round it."""
    r = client.post(
        "/agents/affiliate-coupon-extension/detect",
        content=_cart_body(vec["json"]),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == (200 if vec["verdict"] == "accept" else 422), (r.status_code, r.text[:300])
