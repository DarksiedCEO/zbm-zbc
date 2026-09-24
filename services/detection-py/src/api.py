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

from typing import Callable, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

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


# --- request parsing and validation errors (fix wave 1, Sep 24 2026) ------
#
# Every request body is parsed with pydantic's own JSON parser
# (`model_validate_json`) instead of FastAPI's default json.loads +
# python-mode validation, for two reasons:
#   1. Money on the wire is a JSON string (build contract section 1). In
#      JSON mode the money validator can see that a value was a JSON number
#      and reject it, exactly as orchestrator-go and the ledger do; in
#      python mode 12.345 and "12.345" are indistinguishable from a float
#      fixture value and the number was silently rounded.
#   2. json.loads accepts NaN/Infinity. FastAPI's default 422 handler then
#      echoed the NaN back and json.dumps(allow_nan=False) crashed rendering
#      the error: invalid input -> 500. Pydantic's parser rejects NaN.
# And the 422 body never echoes the rejected input (it can be huge, or not
# JSON-serializable) — only where it was and why.

_M = TypeVar("_M", bound=BaseModel)


def _clean_errors(errors) -> list[dict]:
    return [{"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))} for e in errors]


def wire_body(model: type[_M]) -> Callable:
    async def parse(request: Request) -> _M:
        # Same requirement FastAPI's default body parsing had: a JSON body.
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json" and not (ctype.startswith("application/") and ctype.endswith("+json")):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="request body must be JSON (Content-Type: application/json)",
            )
        raw = await request.body()
        try:
            return model.model_validate_json(raw)
        except ValidationError as e:
            errors = e.errors(include_url=False, include_input=False, include_context=False)
            raise RequestValidationError(
                [{**err, "loc": ("body", *err["loc"])} for err in errors]
            ) from None

    return parse


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": _clean_errors(exc.errors())})


app.add_exception_handler(RequestValidationError, _validation_error_handler)


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
#
# N6 (AEGIS round 2, Sep 24 2026): a pydantic ValidationError raised INSIDE
# an agent (a computed value that failed its own model, e.g. a 0.00
# LabeledValue) used to escape as an unhandled exception -> HTTP 500, and
# failed the whole batch with no hint of which item caused it. The agents
# no longer do that for any valid input (ADR 0001 "Zero-value findings";
# tests/test_zero_value_no_500.py fuzzes all eight). As defense in depth,
# every agent now runs through _run_agent:
#   - each item (each order/subscription/event/status/term; for cross-channel,
#     each order's group of touchpoints, since that agent reasons per order)
#     is run through the agent on its own;
#   - a ValueError from the agent for an item (pydantic's ValidationError and
#     zbm_schema's MoneyRangeError are both ValueErrors) is recorded against
#     that item, and the other items still run so every failing item is named;
#   - if any item failed, the response is 422 with one detail entry per
#     failed item ({"type": "agent_value_error", "loc": ["body", <field>,
#     <index>], "msg": ...}) and NO findings. Deliberately not a partial 200:
#     the orchestrator writes every returned finding to the evidence ledger,
#     and a silently shortened list would be recorded as a complete scan.
# Any other exception is a bug and still surfaces as a 500.

_T = TypeVar("_T")


def _item_label(item: object) -> str:
    for attr in ("order_id", "subscription_id", "term_id"):
        value = getattr(item, attr, None)
        if isinstance(value, str):
            return f"{attr}={value[:64]}"
    return type(item).__name__


def _agent_error_message(item: object, err: ValueError) -> str:
    if isinstance(err, ValidationError):
        parts = [f"{'.'.join(map(str, e['loc'])) or '(model)'}: {e['msg']}"
                 for e in err.errors(include_url=False, include_input=False, include_context=False)]
        reason = f"{err.title}: " + "; ".join(parts)
    else:
        reason = str(err)
    return f"agent could not produce a valid finding for {_item_label(item)} ({reason[:300]})"


def _run_agent(detect: Callable[[list[_T]], list[Finding]], items: list[_T], field: str,
               group_by: Callable[[_T], str] | None = None) -> list[Finding]:
    if group_by is None:
        groups = [([i], [item]) for i, item in enumerate(items)]
    else:
        by_key: dict[str, tuple[list[int], list[_T]]] = {}
        for i, item in enumerate(items):
            idx, members = by_key.setdefault(group_by(item), ([], []))
            idx.append(i)
            members.append(item)
        groups = list(by_key.values())

    findings: list[Finding] = []
    errors: list[dict] = []
    for indexes, members in groups:
        try:
            findings.extend(detect(members))
        except ValueError as err:
            msg = _agent_error_message(members[0], err)
            errors.extend({"type": "agent_value_error", "loc": ["body", field, i], "msg": msg} for i in indexes)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)
    return findings


@app.post("/agents/affiliate-coupon-extension/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_affiliate_coupon_extension(req: OrdersRequest = Depends(wire_body(OrdersRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(affiliate_coupon_extension.detect, req.orders, "orders"))


@app.post("/agents/discount-misuse/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_discount_misuse(req: OrdersRequest = Depends(wire_body(OrdersRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(discount_misuse.detect, req.orders, "orders"))


@app.post("/agents/abandoned-cart-coverage/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_abandoned_cart_coverage(req: OrdersRequest = Depends(wire_body(OrdersRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(abandoned_cart_coverage.detect, req.orders, "orders"))


@app.post("/agents/renewal-never-triggered/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_renewal_never_triggered(req: SubscriptionsRequest = Depends(wire_body(SubscriptionsRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(renewal_never_triggered.detect, req.subscriptions, "subscriptions"))


@app.post("/agents/server-side-attribution/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_server_side_attribution(req: ServerSideEventsRequest = Depends(wire_body(ServerSideEventsRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(server_side_attribution.detect, req.events, "events"))


@app.post("/agents/cross-channel-attribution/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_cross_channel_attribution(req: ChannelTouchpointsRequest = Depends(wire_body(ChannelTouchpointsRequest))) -> FindingsResponse:
    # This agent reasons over all touchpoints of one order together, so an
    # order's touchpoints are one item.
    return FindingsResponse(findings=_run_agent(
        cross_channel_attribution.detect, req.touchpoints, "touchpoints", group_by=lambda t: t.order_id))


@app.post("/agents/platform-integration/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_platform_integration(req: PlatformConnectionsRequest = Depends(wire_body(PlatformConnectionsRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(platform_integration.detect, req.statuses, "statuses"))


@app.post("/agents/contract-pricing-term-drift/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_contract_pricing_term_drift(req: ContractTermsRequest = Depends(wire_body(ContractTermsRequest))) -> FindingsResponse:
    return FindingsResponse(findings=_run_agent(contract_pricing_term_drift.detect, req.terms, "terms"))


# --- correlation (Decision 3 / Failure Mode #2 safeguard) -------------------

@app.post("/correlation/overlaps", dependencies=[Depends(require_auth)])
def correlation_overlaps(req: FindingsRequest = Depends(wire_body(FindingsRequest))) -> dict[str, list[Finding]]:
    return find_overlapping_entities(req.findings)
