"""
REST surface for the Python detection-agent layer (Decision, voice session
Sep 21 2026: plain REST/JSON between services, not gRPC — pragmatic choice
for a solo-builder team, revisit only if a specific connection proves it
needs stricter contracts).

This is what the Go orchestrator calls. It is a thin, honest wrapper: no
business logic lives here, only request/response marshaling around the
pure agent functions in agents/*.py. Fixture endpoints exist for local
dev/testing convenience only and are explicitly labeled non-live.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from agents import (
    abandoned_cart_coverage,
    affiliate_coupon_extension,
    contract_pricing_term_drift,
    cross_channel_attribution,
    discount_misuse,
    platform_integration,
    renewal_never_triggered,
    server_side_attribution,
)
from fixtures_loader import (
    load_channel_touchpoints,
    load_contract_terms,
    load_customers,
    load_orders,
    load_platform_connections,
    load_server_side_events,
    load_subscriptions,
)
from zbm_schema import Finding, Order, Subscription
from zbm_schema.correlation import find_overlapping_entities
from zbm_schema.tier2 import (
    ChannelTouchpoint,
    ContractTerm,
    PlatformConnectionStatus,
    ServerSideAttributionEvent,
)

app = FastAPI(
    title="ZBM Revenue Recovery — Detection Agents (1A: e-commerce)",
    description=(
        "Tier 1 detection agents over REST. NON-LIVE: this service has no "
        "connection to a real store. All data is either request-supplied "
        "or served from the fixtures/ pool for dev/testing only."
    ),
    version="0.1.0",
)


class OrdersRequest(BaseModel):
    orders: list[Order]


class SubscriptionsRequest(BaseModel):
    subscriptions: list[Subscription]


class ServerSideEventsRequest(BaseModel):
    events: list[ServerSideAttributionEvent]


class ChannelTouchpointsRequest(BaseModel):
    touchpoints: list[ChannelTouchpoint]


class PlatformConnectionsRequest(BaseModel):
    statuses: list[PlatformConnectionStatus]


class ContractTermsRequest(BaseModel):
    terms: list[ContractTerm]


class FindingsRequest(BaseModel):
    findings: list[Finding]


class FindingsResponse(BaseModel):
    findings: list[Finding]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "detection-py", "data_source": "non-live"}


# --- fixture endpoints (dev/test only, explicitly labeled) -----------------

@app.get("/fixtures/orders")
def fixtures_orders() -> list[Order]:
    return load_orders()


@app.get("/fixtures/customers")
def fixtures_customers():
    return load_customers()


@app.get("/fixtures/subscriptions")
def fixtures_subscriptions() -> list[Subscription]:
    return load_subscriptions()


@app.get("/fixtures/tier2/server-side-events")
def fixtures_server_side_events() -> list[ServerSideAttributionEvent]:
    return load_server_side_events()


@app.get("/fixtures/tier2/channel-touchpoints")
def fixtures_channel_touchpoints() -> list[ChannelTouchpoint]:
    return load_channel_touchpoints()


@app.get("/fixtures/tier2/platform-connections")
def fixtures_platform_connections() -> list[PlatformConnectionStatus]:
    return load_platform_connections()


@app.get("/fixtures/tier2/contract-terms")
def fixtures_contract_terms() -> list[ContractTerm]:
    return load_contract_terms()


# --- agent endpoints ---------------------------------------------------------

@app.post("/agents/affiliate-coupon-extension/detect", response_model=FindingsResponse)
def detect_affiliate_coupon_extension(req: OrdersRequest) -> FindingsResponse:
    return FindingsResponse(findings=affiliate_coupon_extension.detect(req.orders))


@app.post("/agents/discount-misuse/detect", response_model=FindingsResponse)
def detect_discount_misuse(req: OrdersRequest) -> FindingsResponse:
    return FindingsResponse(findings=discount_misuse.detect(req.orders))


@app.post("/agents/abandoned-cart-coverage/detect", response_model=FindingsResponse)
def detect_abandoned_cart_coverage(req: OrdersRequest) -> FindingsResponse:
    return FindingsResponse(findings=abandoned_cart_coverage.detect(req.orders))


@app.post("/agents/renewal-never-triggered/detect", response_model=FindingsResponse)
def detect_renewal_never_triggered(req: SubscriptionsRequest) -> FindingsResponse:
    return FindingsResponse(findings=renewal_never_triggered.detect(req.subscriptions))


@app.post("/agents/server-side-attribution/detect", response_model=FindingsResponse)
def detect_server_side_attribution(req: ServerSideEventsRequest) -> FindingsResponse:
    return FindingsResponse(findings=server_side_attribution.detect(req.events))


@app.post("/agents/cross-channel-attribution/detect", response_model=FindingsResponse)
def detect_cross_channel_attribution(req: ChannelTouchpointsRequest) -> FindingsResponse:
    return FindingsResponse(findings=cross_channel_attribution.detect(req.touchpoints))


@app.post("/agents/platform-integration/detect", response_model=FindingsResponse)
def detect_platform_integration(req: PlatformConnectionsRequest) -> FindingsResponse:
    return FindingsResponse(findings=platform_integration.detect(req.statuses))


@app.post("/agents/contract-pricing-term-drift/detect", response_model=FindingsResponse)
def detect_contract_pricing_term_drift(req: ContractTermsRequest) -> FindingsResponse:
    return FindingsResponse(findings=contract_pricing_term_drift.detect(req.terms))


# --- correlation (Decision 3 / Failure Mode #2 safeguard) -------------------

@app.post("/correlation/overlaps")
def correlation_overlaps(req: FindingsRequest) -> dict[str, list[Finding]]:
    return find_overlapping_entities(req.findings)
