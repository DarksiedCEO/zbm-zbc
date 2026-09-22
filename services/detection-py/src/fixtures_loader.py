"""
Loads the shared fixture pool (fixtures/*.json at repo root) into the
zbm_schema domain models. Every Tier 1 agent tests against this same
pool — see Decision (voice session, Sep 21 2026): shared fixtures, not
per-agent fixtures, specifically so overlap/double-counting between
agents can be tested honestly.

This is explicitly non-live, fixture-only data. Never wire this loader
into a code path that could present its output as real client data.
"""

from __future__ import annotations

import json
from pathlib import Path

from zbm_schema import Customer, Order, Subscription
from zbm_schema.tier2 import (
    ChannelTouchpoint,
    ContractTerm,
    PlatformConnectionStatus,
    ServerSideAttributionEvent,
)

# repo root is four levels up from this file: src/ -> detection-py/ -> services/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES_DIR = _REPO_ROOT / "fixtures"


def _strip_notes(records: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "_fixture_note"} for r in records]


def load_customers() -> list[Customer]:
    raw = json.loads((_FIXTURES_DIR / "customers.json").read_text())
    return [Customer.model_validate(r) for r in _strip_notes(raw)]


def load_orders() -> list[Order]:
    raw = json.loads((_FIXTURES_DIR / "orders.json").read_text())
    return [Order.model_validate(r) for r in _strip_notes(raw)]


def load_subscriptions() -> list[Subscription]:
    raw = json.loads((_FIXTURES_DIR / "subscriptions.json").read_text())
    return [Subscription.model_validate(r) for r in _strip_notes(raw)]


def load_server_side_events() -> list[ServerSideAttributionEvent]:
    raw = json.loads((_FIXTURES_DIR / "tier2_server_side_events.json").read_text())
    return [ServerSideAttributionEvent.model_validate(r) for r in _strip_notes(raw)]


def load_channel_touchpoints() -> list[ChannelTouchpoint]:
    raw = json.loads((_FIXTURES_DIR / "tier2_channel_touchpoints.json").read_text())
    return [ChannelTouchpoint.model_validate(r) for r in _strip_notes(raw)]


def load_platform_connections() -> list[PlatformConnectionStatus]:
    raw = json.loads((_FIXTURES_DIR / "tier2_platform_connections.json").read_text())
    return [PlatformConnectionStatus.model_validate(r) for r in _strip_notes(raw)]


def load_contract_terms() -> list[ContractTerm]:
    raw = json.loads((_FIXTURES_DIR / "tier2_contract_terms.json").read_text())
    return [ContractTerm.model_validate(r) for r in _strip_notes(raw)]
