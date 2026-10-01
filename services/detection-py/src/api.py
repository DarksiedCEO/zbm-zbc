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

import asyncio
import hmac
import os

from typing import Callable, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

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
from request_limits import body_limit_for
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


# --- request limits (fix wave 1, Sep 24 2026) --------------------------------
#
# Finding (LOW, CONFIRMED): no body-size limit, and the body was parsed ON the
# event loop — a 33 MB body held /health for 3.2 s, and a 33 MB batch was
# accepted and run. Now (ADR 0001 "Request limits"):
#   - MAX_BATCH_ITEMS caps every request list (orders, subscriptions, events,
#     touchpoints, statuses, terms, findings): more is a 422 "too_long".
#   - Every field of every request model has a limit (zbm_schema/limits.py),
#     and each route's body limit is the worst-case JSON size of its largest
#     LEGAL batch at those limits, computed by request_limits.py, plus 25%
#     headroom (ROUTE_BODY_LIMITS below). LOW-C (fix wave 1): the old single
#     2 MiB limit was sized from a typical order, so 1,000 orders x 30 line
#     items (2.12 MiB) — a batch the API advertised — got 413. A larger body
#     is refused with 413 BEFORE any parsing: from Content-Length without
#     reading a byte of the body, and — for a chunked body with no
#     Content-Length — as soon as the running total passes it. Any path that
#     takes no body (GET /health, the fixture routes, unknown paths) gets
#     DEFAULT_BODY_BYTES.
#   - At most MAX_CONCURRENT_HEAVY requests whose body may exceed
#     HEAVY_BODY_BYTES (declared larger, or chunked with no length) run at
#     once; one more is answered 503 + Retry-After at once, before its body
#     is read. Parsing, agents and serialization hold the GIL (pydantic-core
#     keeps it for tens of ms per call), so concurrent large batches do not
#     run in parallel anyway — they only take turns delaying the event loop.
#     Measured with 16 clients sending ~28 MiB worst-case batches: cap 2 ->
#     /health max 0.43-0.52 s, p50 50-120 ms; cap 1 -> max 0.19-0.22 s, p50
#     8 ms, with the same batch throughput. Fix wave 23: on a 2-CPU box the
#     cap-1 max was 0.44-0.63 s — GIL re-acquisition at the default 5 ms
#     switch interval, not a blocked loop; serve.py now sets 1 ms (as the
#     other serve.py launchers do). Measured with it (fix wave 24, the
#     same numbers and conditions as serve.py's note: 2-CPU box, 3.13.13,
#     the live test, 5 runs each): 1 ms, no other load — p50 11-17 ms,
#     max 0.10-0.16 s; 1 ms, three busy loops — p50 6-8 ms, max
#     0.21-0.27 s; 5 ms, three busy loops — p50 11-14 ms, max 0.23-0.45 s.
#     The live test's bound stays 0.5 s. Small requests (every
#     orchestrator scan sends fixture-sized batches) are never capped.
#   - BODY_READ_TIMEOUT_S bounds how long one request may take to deliver its
#     body (408), so a slow-drip body cannot hold a request open forever.
#   - MAX_HEADER_BYTES bounds the request line + headers. The real bound is in
#     the HTTP parser (serve.py runs uvicorn's h11 parser with
#     h11_max_incomplete_event_size = MAX_HEADER_BYTES, so an oversized head
#     is refused while it is being read); the middleware re-checks it (431)
#     for any other launcher. uvicorn's default httptools parser has NO head
#     size limit (a 20 MB header was accepted), which is why serve.py exists.
#   - JSON parsing runs in the threadpool (run_in_threadpool below), and every
#     agent route handler is a plain `def`, which FastAPI runs in the
#     threadpool, so neither parsing nor agent execution runs on the event
#     loop. /health is `async def`: it is answered on the event loop itself,
#     never queued behind the threadpool.

DEFAULT_BODY_BYTES = 64 * 1024
MAX_BATCH_ITEMS = 1000
MAX_HEADER_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
HEAVY_BODY_BYTES = 256 * 1024
MAX_CONCURRENT_HEAVY = 1
HEAVY_RETRY_AFTER_S = 1


class _BodyLimitMiddleware:
    """Pure ASGI middleware (no buffering of its own) enforcing the head size,
    the per-route body size, the heavy-request concurrency cap and the body
    read deadline on every HTTP request, before routing, auth or parsing."""

    def __init__(self, app, route_limits: dict[str, int] | None = None,
                 default_body: int = DEFAULT_BODY_BYTES, max_head: int = MAX_HEADER_BYTES,
                 read_timeout: float = BODY_READ_TIMEOUT_S, heavy_body: int = HEAVY_BODY_BYTES,
                 max_heavy: int = MAX_CONCURRENT_HEAVY):
        self.app = app
        self.route_limits = route_limits or {}
        self.default_body = default_body
        self.max_head = max_head
        self.read_timeout = read_timeout
        self.heavy_body = heavy_body
        self.max_heavy = max_heavy
        # One event loop, and the check-and-increment below has no await in
        # between, so a plain counter is exact.
        self.heavy_in_flight = 0

    @staticmethod
    async def _refuse(send, code: int, detail: str, extra_headers: dict[str, str] | None = None) -> None:
        body = JSONResponse(status_code=code, content={"detail": detail},
                            headers={"Connection": "close", **(extra_headers or {})})
        await send({"type": "http.response.start", "status": code, "headers": body.raw_headers})
        await send({"type": "http.response.body", "body": body.body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        head = len(scope.get("raw_path") or b"") + len(scope.get("query_string") or b"")
        head += sum(len(k) + len(v) + 4 for k, v in scope["headers"])
        if head > self.max_head:
            return await self._refuse(send, 431, f"request head exceeds {self.max_head} bytes")

        max_body = self.route_limits.get(scope["path"], self.default_body)
        declared = [v for k, v in scope["headers"] if k == b"content-length"]
        declared_len: int | None = None
        if declared:
            if len(declared) > 1 or not declared[0].isdigit():
                return await self._refuse(send, 400, "invalid Content-Length")
            declared_len = int(declared[0])
            if declared_len > max_body:
                return await self._refuse(send, 413, f"request body exceeds {max_body} bytes")

        chunked = any(k == b"transfer-encoding" for k, _ in scope["headers"])
        heavy = (declared_len is not None and declared_len > self.heavy_body) or (declared_len is None and chunked)
        if heavy:
            if self.heavy_in_flight >= self.max_heavy:
                return await self._refuse(
                    send, 503,
                    f"busy: {self.max_heavy} large requests already in progress; retry shortly",
                    {"Retry-After": str(HEAVY_RETRY_AFTER_S)})
            self.heavy_in_flight += 1

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.read_timeout
        received = 0
        body_done = False

        async def limited_receive():
            nonlocal received, body_done
            if body_done:
                return await receive()
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), remaining)
            except TimeoutError:
                raise HTTPException(status_code=status.HTTP_408_REQUEST_TIMEOUT,
                                    detail=f"request body not received within {self.read_timeout:g}s",
                                    headers={"Connection": "close"}) from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body:
                    # Raised inside the route, so FastAPI's HTTPException
                    # handler answers it (nothing has been sent yet).
                    raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                                        detail=f"request body exceeds {max_body} bytes",
                                        headers={"Connection": "close"})
                if not message.get("more_body", False):
                    body_done = True
            else:
                body_done = True
            return message

        try:
            return await self.app(scope, limited_receive, send)
        finally:
            if heavy:
                self.heavy_in_flight -= 1


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
        # Size and read deadline are enforced by _BodyLimitMiddleware while
        # this reads; parsing happens off the event loop.
        raw = await request.body()
        try:
            return await run_in_threadpool(model.model_validate_json, raw)
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
    orders: list[Order] = Field(max_length=MAX_BATCH_ITEMS)


class SubscriptionsRequest(BaseModel):
    subscriptions: list[Subscription] = Field(max_length=MAX_BATCH_ITEMS)


class ServerSideEventsRequest(BaseModel):
    events: list[ServerSideAttributionEvent] = Field(max_length=MAX_BATCH_ITEMS)


class ChannelTouchpointsRequest(BaseModel):
    touchpoints: list[ChannelTouchpoint] = Field(max_length=MAX_BATCH_ITEMS)


class PlatformConnectionsRequest(BaseModel):
    statuses: list[PlatformConnectionStatus] = Field(max_length=MAX_BATCH_ITEMS)


class ContractTermsRequest(BaseModel):
    terms: list[ContractTerm] = Field(max_length=MAX_BATCH_ITEMS)


class FindingsRequest(BaseModel):
    findings: list[Finding] = Field(max_length=MAX_BATCH_ITEMS)


class FindingsResponse(BaseModel):
    findings: list[Finding]


# Every route that takes a body, and its request model. The body limit of
# each is computed from the model (request_limits.body_limit_for); a route
# missing here would get DEFAULT_BODY_BYTES (64 KiB).
ROUTE_REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "/agents/affiliate-coupon-extension/detect": OrdersRequest,
    "/agents/discount-misuse/detect": OrdersRequest,
    "/agents/abandoned-cart-coverage/detect": OrdersRequest,
    "/agents/renewal-never-triggered/detect": SubscriptionsRequest,
    "/agents/server-side-attribution/detect": ServerSideEventsRequest,
    "/agents/cross-channel-attribution/detect": ChannelTouchpointsRequest,
    "/agents/platform-integration/detect": PlatformConnectionsRequest,
    "/agents/contract-pricing-term-drift/detect": ContractTermsRequest,
    "/correlation/overlaps": FindingsRequest,
}
ROUTE_BODY_LIMITS: dict[str, int] = {path: body_limit_for(model) for path, model in ROUTE_REQUEST_MODELS.items()}
MAX_BODY_BYTES = max(ROUTE_BODY_LIMITS.values())  # the largest limit of any route

app.add_middleware(_BodyLimitMiddleware, route_limits=ROUTE_BODY_LIMITS)


@app.get("/health")
async def health() -> dict:
    # async (LOW-C, fix wave 1): answered on the event loop, not queued in
    # the threadpool behind parsing and agent work.
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

# LOW-C (fix wave 1): the response JSON is built HERE, in the route's
# threadpool thread. Returning a model made FastAPI validate it in the
# threadpool but serialize it on the event loop — for 1,000 findings that
# held /health. The findings are already validated Finding objects.
_FINDINGS_RESPONSE = TypeAdapter(FindingsResponse)
_OVERLAPS = TypeAdapter(dict[str, list[Finding]])


def _findings_json(findings: list[Finding]) -> Response:
    return Response(_FINDINGS_RESPONSE.dump_json(FindingsResponse(findings=findings)), media_type="application/json")


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
def detect_affiliate_coupon_extension(req: OrdersRequest = Depends(wire_body(OrdersRequest))) -> Response:
    return _findings_json(_run_agent(affiliate_coupon_extension.detect, req.orders, "orders"))


@app.post("/agents/discount-misuse/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_discount_misuse(req: OrdersRequest = Depends(wire_body(OrdersRequest))) -> Response:
    return _findings_json(_run_agent(discount_misuse.detect, req.orders, "orders"))


@app.post("/agents/abandoned-cart-coverage/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_abandoned_cart_coverage(req: OrdersRequest = Depends(wire_body(OrdersRequest))) -> Response:
    return _findings_json(_run_agent(abandoned_cart_coverage.detect, req.orders, "orders"))


@app.post("/agents/renewal-never-triggered/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_renewal_never_triggered(req: SubscriptionsRequest = Depends(wire_body(SubscriptionsRequest))) -> Response:
    return _findings_json(_run_agent(renewal_never_triggered.detect, req.subscriptions, "subscriptions"))


@app.post("/agents/server-side-attribution/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_server_side_attribution(req: ServerSideEventsRequest = Depends(wire_body(ServerSideEventsRequest))) -> Response:
    return _findings_json(_run_agent(server_side_attribution.detect, req.events, "events"))


@app.post("/agents/cross-channel-attribution/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_cross_channel_attribution(req: ChannelTouchpointsRequest = Depends(wire_body(ChannelTouchpointsRequest))) -> Response:
    # This agent reasons over all touchpoints of one order together, so an
    # order's touchpoints are one item.
    return _findings_json(_run_agent(
        cross_channel_attribution.detect, req.touchpoints, "touchpoints", group_by=lambda t: t.order_id))


@app.post("/agents/platform-integration/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_platform_integration(req: PlatformConnectionsRequest = Depends(wire_body(PlatformConnectionsRequest))) -> Response:
    return _findings_json(_run_agent(platform_integration.detect, req.statuses, "statuses"))


@app.post("/agents/contract-pricing-term-drift/detect", response_model=FindingsResponse, dependencies=[Depends(require_auth)])
def detect_contract_pricing_term_drift(req: ContractTermsRequest = Depends(wire_body(ContractTermsRequest))) -> Response:
    return _findings_json(_run_agent(contract_pricing_term_drift.detect, req.terms, "terms"))


# --- correlation (Decision 3 / Failure Mode #2 safeguard) -------------------

@app.post("/correlation/overlaps", dependencies=[Depends(require_auth)])
def correlation_overlaps(req: FindingsRequest = Depends(wire_body(FindingsRequest))) -> Response:
    return Response(_OVERLAPS.dump_json(find_overlapping_entities(req.findings)), media_type="application/json")
