"""
Revenue Recovery client for intelligence 6 (Audit and Baseline).

Onboarding NEVER re-implements a detection agent. It calls detection-py's
real routes (services/detection-py/src/api.py) over HTTP and consumes the
Finding JSON. Route map, read from detection-py's api.py:

    orders         -> /agents/affiliate-coupon-extension/detect   {"orders": [...]}
                      /agents/discount-misuse/detect             {"orders": [...]}
                      /agents/abandoned-cart-coverage/detect     {"orders": [...]}
    subscriptions  -> /agents/renewal-never-triggered/detect      {"subscriptions": [...]}
    server_side_events -> /agents/server-side-attribution/detect  {"events": [...]}
    channel_touchpoints -> /agents/cross-channel-attribution/detect {"touchpoints": [...]}
    platform_connections -> /agents/platform-integration/detect   {"statuses": [...]}
    contract_terms -> /agents/contract-pricing-term-drift/detect   {"terms": [...]}
    (all findings) -> /correlation/overlaps                        {"findings": [...]}

Every detect route returns {"findings": [Finding, ...]}; /correlation/overlaps
returns {entity_id: [Finding, ...]} for entities claimed by 2+ agents.
``recoverable_value.amount_usd`` is treated as a money value per
BUILD_CONTRACTS.md section 1 (string today per contract; a legacy JSON
number is accepted only through ``str()``).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

import httpx

ROUTES: dict[str, list[tuple[str, str]]] = {
    "orders": [
        ("/agents/affiliate-coupon-extension/detect", "orders"),
        ("/agents/discount-misuse/detect", "orders"),
        ("/agents/abandoned-cart-coverage/detect", "orders"),
    ],
    "subscriptions": [("/agents/renewal-never-triggered/detect", "subscriptions")],
    "server_side_events": [("/agents/server-side-attribution/detect", "events")],
    "channel_touchpoints": [("/agents/cross-channel-attribution/detect", "touchpoints")],
    "platform_connections": [("/agents/platform-integration/detect", "statuses")],
    "contract_terms": [("/agents/contract-pricing-term-drift/detect", "terms")],
}
CORRELATION_ROUTE = "/correlation/overlaps"


class RevenueRecoveryError(RuntimeError):
    pass


class RevenueRecoveryClient(Protocol):
    def detect(self, account_data: dict[str, list[dict]]) -> list[dict]: ...

    def overlaps(self, findings: list[dict]) -> dict[str, list[dict]]: ...


class HttpRevenueRecoveryClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 10.0, transport: Optional[httpx.BaseTransport] = None):
        if not base_url or not token:
            raise ValueError("HttpRevenueRecoveryClient needs DETECTION_SERVICE_URL and DETECTION_SERVICE_TOKEN")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=timeout_s, transport=transport
        )

    def _post(self, path: str, body: dict) -> Any:
        try:
            r = self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise RevenueRecoveryError(f"Revenue Recovery unreachable ({type(exc).__name__})") from None
        if r.status_code != 200:
            raise RevenueRecoveryError(f"Revenue Recovery {path} returned HTTP {r.status_code}")
        return r.json()

    def detect(self, account_data: dict[str, list[dict]]) -> list[dict]:
        unknown = set(account_data) - set(ROUTES)
        if unknown:
            raise RevenueRecoveryError(f"no Revenue Recovery route for data kinds: {sorted(unknown)}")
        findings: list[dict] = []
        for kind, rows in account_data.items():
            if not rows:
                continue
            for path, key in ROUTES[kind]:
                findings.extend(self._post(path, {key: rows}).get("findings", []))
        return findings

    def overlaps(self, findings: list[dict]) -> dict[str, list[dict]]:
        if not findings:
            return {}
        return self._post(CORRELATION_ROUTE, {"findings": findings})


class NotConfiguredRevenueRecovery:
    def detect(self, account_data):
        raise RevenueRecoveryError("Revenue Recovery not configured (DETECTION_SERVICE_URL / DETECTION_SERVICE_TOKEN unset)")

    def overlaps(self, findings):
        raise RevenueRecoveryError("Revenue Recovery not configured")


class FakeRevenueRecovery:
    """Test double: returns canned findings; records what it was sent."""

    def __init__(self, findings: list[dict] | None = None, overlaps: dict | None = None, fail: bool = False):
        self.findings = findings or []
        self._overlaps = overlaps or {}
        self.fail = fail
        self.calls: list[dict] = []

    def detect(self, account_data):
        self.calls.append(account_data)
        if self.fail:
            raise RevenueRecoveryError("fake Revenue Recovery failure")
        return list(self.findings)

    def overlaps(self, findings):
        return dict(self._overlaps)
