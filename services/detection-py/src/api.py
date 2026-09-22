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

import hmac
import os

from fastapi import Depends, FastAPI, Header, HTTPException, status
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

# --- auth ---------------------------------------------------------------
#
# Fail-closed by design: this service refuses to start at all if no token
# is configured, rather than silently serving every endpoint unauthenticated.
# Every route except /health requires `Authorization: Bearer <token>`.
# orchestrator-go must be configured with the same shared secret
# (DETECTION_SERVICE_TOKEN env var there — see internal/client/client.go).
#
# This is a single shared-secret bearer token, not a real auth system
# (no per-caller identity, no rotation, no scoping) — adequate for a
# service-to-service call between two processes on the same private
# network, and explicitly not adequate for anything internet-facing.
# That upgrade (mTLS, or per-service signed tokens) is a real gap if this
# ever leaves a private network, and is tracked as a known limitation in
# the README rather than silently assumed away.


def _load_required_token() -> str:
    token = os.environ.get("ZBM_SERVICE_TOKEN")
    if not token:
        raise RuntimeError(
            "ZBM_SERVICE_TOKEN is not set. This service refuses to start "
            "without an auth token configured (fail closed, not open). "
            "Set ZBM_SERVICE_TOKEN to a shared secret before starting "
            "detection-py, and set the identical value as "
            "DETECTION_SERVICE_TOKEN on orchestrator-go, which calls this "
            "service."
        )
    return token


_REQUIRED_TOKEN = _load_required_token()


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token auth dependency, applied to every endpoint below except
    /health. Uses hmac.compare_digest for the token comparison to avoid a
    timing side-channel that could otherwise leak the secret byte-by-byte."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header (expected: Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization.removeprefix("Bearer ")
    # Port of a confirmed Sep 22 2026 finding from fulfillment-py's
    # independent review: hmac.compare_digest raises TypeError when either
    # operand contains non-ASCII characters ("comparing strings with
    # non-ASCII characters is not supported"). Unhandled, that turned an
    # attacker-reachable invalid-token request into an unauthenticated 500
    # instead of a 401 — a crash, and one that risks leaking more than
    # "invalid token" via its traceback. Same bug pattern, same fix.
    try:
        valid = hmac.compare_digest(supplied, _REQUIRED_TOKEN)
    except TypeError:
        valid = False
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = FastAPI(
    title="ZBM Revenue Recovery — Detection Agents (1A: e-commerce)",
    description=(
        "Tier 1 detection agents over REST. NON-LIVE: this service has no "
        "connection to a real store. All data is either request-supplied "
        "or served from the fixtures/ pool for dev/testing only."
    ),
    version="0.1.0",
    # Confirmed Sep 22 2026 finding (same pattern as fulfillment-py):
    # FastAPI's auto-generated /docs, /redoc, /openapi.json were reachable
    # with NO auth, exposing the full route/schema map to anyone who found
    # the port. Disabled outright rather than gated — no interactive-docs
    # need for a private service-to-service API.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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

@app.get("/fixtures/orders", dependencies=[Depends(require_auth)])
def fixtures_orders() -> list[Order]:
    return load_orders()


@app.get("/fixtures/customers", dependencies=[Depends(require_auth)])
def fixtures_customers():
    return load_customers()


@app.get("/fixtures/subscriptions", dependencies=[Depends(require_auth)])
def fixtures_subscriptions() -> list[Subscription]:
    return load_subscriptions()


@app.get("/fixtures/tier2/server-side-events", dependencies=[Depends(require_auth)])
def fixtures_server_side_events() -> list[ServerSideAttributionEvent]:
    return load_server_side_events()


@app.get("/fixtures/tier2/channel-touchpoints", dependencies=[Depends(require_auth)])
def fixtures_channel_touchpoints() -> list[ChannelTouchpoint]:
    return load_channel_touchpoints()


@app.get("/fixtures/tier2/platform-connections", dependencies=[Depends(require_auth)])
def fixtures_platform_connections() -> list[PlatformConnectionStatus]:
    return load_platform_connections()


@app.get("/fixtures/tier2/contract-terms", dependencies=[Depends(require_auth)])
def fixtures_contract_terms() -> list[ContractTerm]:
    return load_contract_terms()


# --- agent endpoints ---------------------------------------------------------

@app.post("/agents/affiliate-coupon-extension/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_affiliate_coupon_extension(req: OrdersRequest) -> FindingsResponse:
    return FindingsResponse(findings=affiliate_coupon_extension.detect(req.orders))


@app.post("/agents/discount-misuse/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_discount_misuse(req: OrdersRequest) -> FindingsResponse:
    return FindingsResponse(findings=discount_misuse.detect(req.orders))


@app.post("/agents/abandoned-cart-coverage/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_abandoned_cart_coverage(req: OrdersRequest) -> FindingsResponse:
    return FindingsResponse(findings=abandoned_cart_coverage.detect(req.orders))


@app.post("/agents/renewal-never-triggered/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_renewal_never_triggered(req: SubscriptionsRequest) -> FindingsResponse:
    return FindingsResponse(findings=renewal_never_triggered.detect(req.subscriptions))


@app.post("/agents/server-side-attribution/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_server_side_attribution(req: ServerSideEventsRequest) -> FindingsResponse:
    return FindingsResponse(findings=server_side_attribution.detect(req.events))


@app.post("/agents/cross-channel-attribution/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_cross_channel_attribution(req: ChannelTouchpointsRequest) -> FindingsResponse:
    return FindingsResponse(findings=cross_channel_attribution.detect(req.touchpoints))


@app.post("/agents/platform-integration/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_platform_integration(req: PlatformConnectionsRequest) -> FindingsResponse:
    return FindingsResponse(findings=platform_integration.detect(req.statuses))


@app.post("/agents/contract-pricing-term-drift/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_contract_pricing_term_drift(req: ContractTermsRequest) -> FindingsResponse:
    return FindingsResponse(findings=contract_pricing_term_drift.detect(req.terms))


# --- correlation (Decision 3 / Failure Mode #2 safeguard) -------------------

@app.post("/correlation/overlaps", dependencies=[Depends(require_auth)])
def correlation_overlaps(req: FindingsRequest) -> dict[str, list[Finding]]:
    return find_overlapping_entities(req.findings)
