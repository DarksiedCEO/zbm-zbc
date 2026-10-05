"""
REST surface for Finance (31) (spec §D, ADR 0009).

Same discipline as services/verification-py and services/compliance-py (the shared blocks below are copied from
verification-py's api.py):
- fail-closed bearer auth on every route except /health; the service refuses to start without FIN_SERVICE_TOKEN;
  ``hmac.compare_digest`` wrapped in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-FIN-Caller-Token`` (FIN_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token on a route that needs one is a 403;
- Andre's routes need ``X-Andre-Approval-Token`` (FIN_ANDRE_APPROVAL_TOKEN, the FounderGate; a token equal to the
  service, a caller or the second approver's token counts as not configured); a refusal is a 403 recorded as
  ``founder_approval_refused``; the optional second approver sends ``X-FIN-Second-Approver-Token``;
- separation of duties (FIN-19) is enforced HERE by caller identity: the release route takes only the scheduler and
  refuses any request that carries an Andre token (the approver cannot release); approval routes take only Andre's
  token (no caller, the scheduler or the service bearer can approve);
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless FIN_BIND_ADDR says otherwise; hardened launcher;
- request limits before any route: target 4 KiB (414), head 16 KiB (431), per-route body caps inside the 1 MiB cap
  (413), JSON content type only (415), JSON depth and member count bounded (422), body read deadline (408);
- every body is scanned for bank/card/tax/identity data (422 SENSITIVE_DATA_REFUSED, FIN-28, never echoed); the
  protocol routes refuse any key naming a count, metric, amount or rate (callers never supply them, FIN-04).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

import config as config_mod
import models as m
import money as M
import reasons as R
from clock import Clock, SystemClock
from errors import FinError, Forbidden, FounderRefused, Invalid, Unavailable
from founder import FounderGate
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from service import Service
from store import RecordLog

log = logging.getLogger("finance.api")

CALLER_HEADER = "X-FIN-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
SECOND_HEADER = "X-FIN-Second-Approver-Token"
MAX_BODY_BYTES = 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

# Per-route body caps (all inside MAX_BODY_BYTES).
ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/fin/v1/rules/(proposals|decisions)$"), 64 * 1024),
    (re.compile(r"^/fin/v1/rate-cards/decisions$"), 64 * 1024),
    (re.compile(r"^/fin/v1/bank/events$"), 128 * 1024),
    (re.compile(r"^/fin/v1/rails/[a-z]+/events$"), 64 * 1024),
    (re.compile(r"^/fin/v1/stripe/events$"), 300 * 1024),          # a raw Stripe event (<= 256 KiB) in JSON
    (re.compile(r"^/fin/v1/invoices$"), 64 * 1024),
    (re.compile(r"^/fin/v1/payout-handoffs$"), 32 * 1024),
    (re.compile(r"^/fin/v1/journal/[a-z]+/corrections$"), 32 * 1024),
    (re.compile(r"^/fin/v1/reconcile$"), 1024 * 1024),              # up to 10,000 voided ids
]
DEFAULT_ROUTE_LIMIT = 16 * 1024


def route_limit(path: str) -> int:
    for rx, limit in ROUTE_LIMITS:
        if rx.match(path):
            return limit
    return DEFAULT_ROUTE_LIMIT


def _plain(status_code: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, **extra})


def json_shape_problem(body: bytes) -> Optional[str]:
    """Nesting depth and member count of a JSON text, counted outside strings
    (linear, no parsing). Members = object keys + array items, approximated
    by separators; enough to refuse a hostile shape before the parser."""
    depth = members = 0
    in_str = esc = False
    for b in body:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
            continue
        if b == 0x22:
            in_str = True
        elif b in (0x7B, 0x5B):
            depth += 1
            members += 1
            if depth > MAX_JSON_DEPTH:
                return f"JSON nested deeper than {MAX_JSON_DEPTH}"
        elif b in (0x7D, 0x5D):
            depth -= 1
        elif b == 0x2C:
            members += 1
            if members > MAX_JSON_MEMBERS:
                return f"JSON with more than {MAX_JSON_MEMBERS} members"
    return None


def is_json_content_type(value: Optional[bytes]) -> bool:
    if not value:
        return False
    main = value.split(b";", 1)[0].strip().lower()
    return main == b"application/json" or (main.startswith(b"application/") and main.endswith(b"+json"))


class InputLimits:
    """Outermost ASGI middleware: refuse over-long targets and heads, bodies
    over the route's cap (by Content-Length and on the bytes received),
    non-JSON bodies and hostile JSON shapes, before any route sees them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        target = len(scope.get("raw_path") or path.encode("utf-8", "surrogatepass")) + len(scope.get("query_string") or b"")
        if target > MAX_TARGET_BYTES:
            return await _plain(414, f"request target longer than {MAX_TARGET_BYTES} bytes; refused")(scope, receive, send)
        headers = scope.get("headers") or ()
        if target + sum(len(k) + len(v) + 4 for k, v in headers) > MAX_HEAD_BYTES:
            return await _plain(431, f"request head larger than {MAX_HEAD_BYTES} bytes; refused")(scope, receive, send)
        limit = min(route_limit(path), MAX_BODY_BYTES)
        too_large = _plain(413, f"request body larger than {limit} bytes for this route; refused")
        ctype = None
        declared = None
        chunked = False
        for name, value in headers:
            if name == b"content-length":
                if not value.isdigit():
                    return await _plain(400, "invalid Content-Length")(scope, receive, send)
                declared = int(value)
                if declared > limit:
                    return await too_large(scope, receive, send)
            elif name == b"content-type":
                ctype = value
            elif name == b"transfer-encoding" and b"chunked" in value.lower():
                chunked = True
        if (declared or chunked) and not is_json_content_type(ctype):
            return await _plain(415, "request bodies must be application/json")(scope, receive, send)
        chunks, size = [], 0
        deadline = time.monotonic() + BODY_READ_TIMEOUT_S
        while True:
            try:
                message = await asyncio.wait_for(receive(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return await _plain(408, "request body not received in time; refused")(scope, receive, send)
            if message["type"] != "http.request":
                return
            chunk = message.get("body") or b""
            size += len(chunk)
            if size > limit:
                return await too_large(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        if body:
            if not is_json_content_type(ctype):
                return await _plain(415, "request bodies must be application/json")(scope, receive, send)
            problem = await run_in_threadpool(json_shape_problem, body)
            if problem:
                return await _plain(422, f"request body refused: {problem}")(scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def make_require_auth(required_token: str) -> Callable:
    def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="missing or malformed Authorization header (expected: Bearer <token>)",
                                headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization.removeprefix("Bearer ")
        try:
            valid = hmac.compare_digest(supplied, required_token)
        except TypeError:
            valid = False  # non-ASCII token: invalid (401), never a 500
        if not valid:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token",
                                headers={"WWW-Authenticate": "Bearer"})

    return require_auth


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


class Callers:
    def __init__(self, tokens: dict[str, str]):
        self._digests = {name: _digest(tok) for name, tok in tokens.items()}

    def identify(self, supplied: Optional[str]) -> Optional[str]:
        if not supplied:
            return None
        d = _digest(supplied)
        found = None
        for name, dg in self._digests.items():  # no early exit
            if hmac.compare_digest(d, dg):
                found = name
        return found


def _sanitize(errors: list[dict]) -> dict:
    out = []
    for e in errors[:ERROR_MAX_ERRORS]:
        loc = [str(x)[:ERROR_MAX_STR] if isinstance(x, str) else x for x in list(e.get("loc", ()))[:8]]
        out.append({"loc": loc, "msg": str(e.get("msg", ""))[:ERROR_MAX_STR], "type": e.get("type")})
    return {"detail": out, "errors_total": len(errors)}


ID_RE = m.ID_RE
_NO_ID = re.compile(r"[^A-Za-z0-9._:-]")


def _id(value: str) -> str:
    if not ID_RE.fullmatch(value or "") or m.sensitive_value(value or ""):
        raise Invalid("id format (1-128 characters of [A-Za-z0-9._:-]; never bank/card/tax data)")
    return value


def create_app(service: Service, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Finance (31)",
                  description="NON-LIVE: rails, bank, tax agent, GL, vault, V&I, Compliance, Clipper Network, Legal, "
                              "People and push are fail-closed stand-ins unless wired.",
                  version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    others = list(settings.caller_tokens.values()) + [t for t in (settings.second_approver_token,) if t]
    founder = FounderGate.build(settings.andre_token, settings.service_token, others)
    second = FounderGate.build(settings.second_approver_token, settings.service_token,
                               list(settings.caller_tokens.values()) + [t for t in (settings.andre_token,) if t],
                               who="second approver")
    app.state.service = service
    svc = service

    def caller(*allowed: str) -> Callable:
        def dep(x_fin_caller_token: Optional[str] = Header(default=None)) -> str:
            name = callers.identify(x_fin_caller_token)
            if name is None:
                raise Forbidden("caller token missing or not recognised")
            if allowed and name not in allowed:
                raise Forbidden("this caller is not authorized for this route (separation of duties, FIN-19)")
            return name
        return dep

    def andre(route: str) -> Callable:
        def dep(x_andre_approval_token: Optional[str] = Header(default=None)) -> str:
            try:
                founder.verify(x_andre_approval_token)
            except FounderRefused as exc:
                svc.founder_refused(route, exc.reason)
                raise
            return "andre"
        return dep

    def andre_or_caller(route: str, *allowed: str) -> Callable:
        def dep(request: Request) -> str:
            a = request.headers.get(FOUNDER_HEADER)
            if a is not None:
                try:
                    founder.verify(a)
                except FounderRefused as exc:
                    svc.founder_refused(route, exc.reason)
                    raise
                return "andre"
            name = callers.identify(request.headers.get(CALLER_HEADER))
            if name is None or name not in allowed:
                raise Forbidden("Andre's token or an authorized caller token is required")
            return name
        return dep

    def body(model: type[BaseModel], no_counts: bool = False) -> Callable:
        def parse(payload: Any = Body(default=None)) -> Any:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = m.sensitive_problems(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body",), "msg": p, "type": "SENSITIVE_DATA_REFUSED"}
                                              for p in bad[:20]])
            if no_counts:
                fk = m.forbidden_keys(payload)
                if fk:
                    raise RequestValidationError([{"loc": ("body", k), "msg": "callers never send counts, metrics, "
                                                   "amounts or rates (FIN-04)", "type": "forbidden_field"}
                                                  for k in fk[:20]])
            try:
                return model.model_validate(payload)
            except ValidationError as exc:
                raise RequestValidationError(
                    [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
                ) from None
        return parse

    def dump(x: BaseModel) -> dict:
        return x.model_dump(mode="python")

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(FinError)
    def _domain(_: Request, exc: FinError):
        content = {"detail": exc.reason, **exc.body}
        if isinstance(exc, Unavailable):
            content["took_effect"] = False
            return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content=content)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(M.MoneyError)
    def _money(_: Request, exc: M.MoneyError):
        # AEGIS N17-11: an amount out of range is the caller's input (a rate card, a count), never a 500
        return JSONResponse(status_code=422, content={"detail": "AMOUNT_OUT_OF_RANGE", "reasons": [
            R.item("AMOUNT_OUT_OF_RANGE", str(exc)[:120])]})

    @app.middleware("http")
    async def _unhandled(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - only the TYPE is logged (never input, never a secret)
            log.error("unhandled error: %s", type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.add_middleware(InputLimits)

    @app.get("/health")
    async def health() -> dict:
        return svc.health()

    @app.get("/fin/v1/intelligences", dependencies=auth)
    def intelligences(_: str = Depends(caller())) -> list[dict]:
        return registry()

    @app.get("/fin/v1/integrity", dependencies=auth)
    def integrity(_: str = Depends(caller())) -> dict:
        return svc.integrity()

    # --- protocol routes (§D.1) --------------------------------------------------------------------------------------

    @app.post("/fin/v1/payout-handoffs", dependencies=auth)
    def handoff(who: str = Depends(caller("creative_production")),
                req: m.PayoutHandoff = Depends(body(m.PayoutHandoff, no_counts=True))) -> dict:
        return svc.accept_handoff(who, req.request_id, req.submission_id, dump(req.facts))

    @app.post("/fin/v1/payees", dependencies=auth)
    def payees(who: str = Depends(caller("onboarding", "clipper_network")),
               req: m.PayeeCreate = Depends(body(m.PayeeCreate, no_counts=True))) -> dict:
        return svc.create_payee(who, req.request_id, req.model_dump(mode="python", exclude_none=True))

    @app.get("/fin/v1/payees/{payee_id}", dependencies=auth)
    def payee(payee_id: str, _: str = Depends(caller())) -> dict:
        return svc.payee_view(_id(payee_id))

    @app.get("/fin/v1/payees/{payee_id}/tax-status", dependencies=auth)
    def tax_status(payee_id: str, who: str = Depends(caller("compliance_38", "clipper_network"))) -> dict:
        return svc.tax_status(who, _id(payee_id))

    @app.get("/fin/v1/payees/{payee_id}/rail-status", dependencies=auth)
    def rail_status(payee_id: str, who: str = Depends(caller("compliance_38"))) -> dict:
        return svc.rail_status(who, _id(payee_id))

    @app.get("/fin/v1/payees/{payee_id}/payout-identity", dependencies=auth)
    def payout_identity(payee_id: str, who: str = Depends(caller("verification_integrity"))) -> dict:
        return svc.payout_identity(who, _id(payee_id))

    @app.get("/fin/v1/payees/{payee_id}/open-items", dependencies=auth)
    def open_items(payee_id: str, who: str = Depends(caller("clipper_network"))) -> dict:
        return svc.open_items(who, _id(payee_id))

    @app.post("/fin/v1/payees/{payee_id}/offboarding-notices", dependencies=auth)
    def offboarding(payee_id: str, who: str = Depends(caller("clipper_network")),
                    req: m.OffboardingNotice = Depends(body(m.OffboardingNotice, no_counts=True))) -> dict:
        return svc.offboarding_notice(who, req.request_id, _id(payee_id), req.offboarding_id)

    @app.post("/fin/v1/payees/{payee_id}/callbacks", dependencies=auth)
    def callbacks(payee_id: str, _: str = Depends(andre("callbacks")),
                  req: m.CallbackRecord = Depends(body(m.CallbackRecord))) -> dict:
        return svc.record_callback(req.request_id, _id(payee_id), dump(req))

    @app.post("/fin/v1/payees/{payee_id}/tax/b-notices", dependencies=auth)
    def b_notices(payee_id: str, _: str = Depends(andre("b-notices")), req: m.BNotice = Depends(body(m.BNotice))) -> dict:
        return svc.b_notice(req.request_id, _id(payee_id), dump(req))

    # --- rate cards, profiles, billing ------------------------------------------------------------------------------

    @app.get("/fin/v1/rate-cards/{doc_id}/versions/{version}", dependencies=auth)
    def rate_card(doc_id: str, version: int, _: str = Depends(caller())) -> dict:
        return svc.rate_card_meta(_id(doc_id), version)

    @app.get("/fin/v1/rate-cards/{doc_id}/versions/{version}/document", dependencies=auth)
    def rate_card_doc(doc_id: str, version: int, _: str = Depends(andre("rate-cards/document"))) -> dict:
        return svc.rate_card_document(_id(doc_id), version)

    @app.post("/fin/v1/rate-cards/proposals", dependencies=auth, status_code=201)
    def rc_propose(_: str = Depends(andre("rate-cards/proposals")),
                   req: m.RateCardProposal = Depends(body(m.RateCardProposal))) -> dict:
        return svc.propose_rate_card(req.request_id, dump(req))

    @app.post("/fin/v1/rate-cards/decisions", dependencies=auth)
    def rc_decide(_: str = Depends(andre("rate-cards/decisions")),
                  req: m.RuleDecisions = Depends(body(m.RuleDecisions))) -> dict:
        return svc.decide_rate_cards(req.request_id, [dump(d) for d in req.decisions])

    @app.put("/fin/v1/campaigns/{campaign_id}/commercial-profile", dependencies=auth)
    def profile(campaign_id: str, _: str = Depends(andre("commercial-profile")),
                req: m.CommercialProfile = Depends(body(m.CommercialProfile))) -> dict:
        return svc.put_profile(req.request_id, _id(campaign_id), dump(req))

    @app.get("/fin/v1/campaigns/{campaign_id}/budget", dependencies=auth)
    def budget(campaign_id: str, _: str = Depends(caller("clipper_network", "creative_production"))) -> dict:
        return svc.budget_view(_id(campaign_id))

    @app.put("/fin/v1/clients/{client_id}/billing-profile", dependencies=auth)
    def billing_profile(client_id: str, _: str = Depends(andre("billing-profile")),
                        req: m.BillingProfile = Depends(body(m.BillingProfile))) -> dict:
        if not m.CLIENT_ID_RE.fullmatch(client_id or ""):
            raise Invalid("client id must be 1-100 characters of [A-Za-z0-9._-] (it is the client's party reference "
                          "at Legal)")
        return svc.put_billing_profile(req.request_id, _id(client_id), dump(req))

    @app.get("/fin/v1/clients/{client_id}/billing-readiness", dependencies=auth)
    def billing_readiness(client_id: str, who: str = Depends(caller("onboarding"))) -> dict:
        return svc.billing_readiness(who, _id(client_id))

    # --- receivables ------------------------------------------------------------------------------------------------

    @app.post("/fin/v1/invoices", dependencies=auth, status_code=201)
    def invoice_draft(who: str = Depends(caller("onboarding", "scheduler")),
                      req: m.InvoiceDraft = Depends(body(m.InvoiceDraft))) -> dict:
        return svc.draft_invoice(who, req.request_id, dump(req))

    @app.get("/fin/v1/invoices/{invoice_id}", dependencies=auth)
    def invoice_get(invoice_id: str, _: str = Depends(caller())) -> dict:
        return svc.get_invoice(_id(invoice_id))

    @app.post("/fin/v1/invoices/{invoice_id}/decision", dependencies=auth)
    def invoice_decide(invoice_id: str, _: str = Depends(andre("invoices/decision")),
                       req: m.Decision = Depends(body(m.Decision))) -> dict:
        return svc.decide_invoice(req.request_id, _id(invoice_id), dump(req))

    # --- media buys and client receipts (ADR 0009 amendment, Oct 5 2026) ---------------------------------------------

    @app.post("/fin/v1/media-buys", dependencies=auth, status_code=201)
    def media_buy(_: str = Depends(andre("media-buys")),
                  req: m.MediaBuyCreate = Depends(body(m.MediaBuyCreate))) -> dict:
        return svc.create_media_buy(req.request_id, dump(req))

    @app.get("/fin/v1/media-buys/{buy_id}", dependencies=auth)
    def media_buy_get(buy_id: str, _: str = Depends(andre_or_caller("media-buys/get", "scheduler", "onboarding"))) -> dict:
        return svc.get_media_buy(_id(buy_id))

    @app.post("/fin/v1/media-buys/{buy_id}/vendor-payments", dependencies=auth)
    def media_vendor_payment(buy_id: str, _: str = Depends(andre("media-buys/vendor-payments")),
                             req: m.VendorPayment = Depends(body(m.VendorPayment))) -> dict:
        return svc.record_vendor_payment(req.request_id, _id(buy_id), dump(req))

    @app.post("/fin/v1/media-buys/{buy_id}/delivery", dependencies=auth)
    def media_delivery(buy_id: str, _: str = Depends(andre("media-buys/delivery")),
                       req: m.MediaDelivery = Depends(body(m.MediaDelivery))) -> dict:
        return svc.record_media_delivery(req.request_id, _id(buy_id), dump(req))

    @app.post("/fin/v1/media-buys/{buy_id}/cancel", dependencies=auth)
    def media_cancel(buy_id: str, _: str = Depends(andre("media-buys/cancel")),
                     req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.cancel_media_buy(req.request_id, _id(buy_id))

    @app.get("/fin/v1/client-receipts/{client_receipt_id}", dependencies=auth)
    def client_receipt_get(client_receipt_id: str, _: str = Depends(caller())) -> dict:
        return svc.get_client_receipt(_id(client_receipt_id))

    @app.post("/fin/v1/client-receipts/{client_receipt_id}/send", dependencies=auth)
    def client_receipt_send(client_receipt_id: str, who: str = Depends(caller("scheduler")),
                            req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.send_client_receipt(who, req.request_id, _id(client_receipt_id))

    @app.post("/fin/v1/bank/events", dependencies=auth)
    def bank_events(who: str = Depends(caller("bank_feed")), req: m.BankEvents = Depends(body(m.BankEvents))) -> dict:
        return svc.bank_events(who, req.request_id, [dump(l) for l in req.lines])

    @app.post("/fin/v1/receipts/{receipt_id}/apply", dependencies=auth)
    def receipt_apply(receipt_id: str, _: str = Depends(andre("receipts/apply")),
                      req: m.ApplyReceipt = Depends(body(m.ApplyReceipt))) -> dict:
        return svc.apply_receipt(req.request_id, _id(receipt_id), req.invoice_id)

    @app.post("/fin/v1/receipts/{receipt_id}/return", dependencies=auth)
    def receipt_return(receipt_id: str, who: str = Depends(andre_or_caller("receipts/return", "bank_feed")),
                       req: m.DepositReturn = Depends(body(m.DepositReturn))) -> dict:
        return svc.deposit_return(who, req.request_id, _id(receipt_id), dump(req))

    @app.post("/fin/v1/rails/{rail}/events", dependencies=auth)
    def rail_events(rail: str, who: str = Depends(caller("rail_gateway")),
                    req: m.RailEvents = Depends(body(m.RailEvents))) -> dict:
        return svc.rail_events(who, req.request_id, _id(rail), [e.model_dump(mode="python") for e in req.events])

    # --- Stripe incoming (ADR 0009 amendment, Oct 5 2026) -----------------------------------------------------------

    @app.post("/fin/v1/invoices/{invoice_id}/stripe-checkout", dependencies=auth)
    def stripe_checkout(invoice_id: str, who: str = Depends(andre_or_caller("stripe/checkout", "onboarding")),
                        req: m.StripeCheckoutRequest = Depends(body(m.StripeCheckoutRequest))) -> dict:
        return svc.stripe_checkout(who, req.request_id, _id(invoice_id))

    @app.get("/fin/v1/invoices/{invoice_id}/stripe-checkout", dependencies=auth)
    def stripe_checkout_view(invoice_id: str, _: str = Depends(andre_or_caller("stripe/checkout", "onboarding"))) -> dict:
        return svc.get_stripe_checkout(_id(invoice_id))

    def stripe_body(payload: Any = Body(default=None)) -> m.StripeEventIn:
        # NOT body(): Stripe's raw event legitimately carries bank last-4s, emails and names, which the sensitive-data
        # scan refuses. Finance verifies the signature, reads only ids from it, and never stores or logs the body.
        if payload is None:
            raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
        try:
            return m.StripeEventIn.model_validate(payload)
        except ValidationError as exc:
            raise RequestValidationError(
                [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
            ) from None

    @app.post("/fin/v1/stripe/events", dependencies=auth)
    def stripe_events(who: str = Depends(caller("rail_gateway")), req: m.StripeEventIn = Depends(stripe_body)) -> dict:
        return svc.stripe_event(who, req.request_id, req.payload, req.signature)

    @app.post("/fin/v1/disputes", dependencies=auth, status_code=201)
    def dispute(who: str = Depends(andre_or_caller("disputes", "rail_gateway")),
                req: m.DisputeOpen = Depends(body(m.DisputeOpen))) -> dict:
        return svc.open_dispute(who, req.request_id, dump(req))

    @app.post("/fin/v1/disputes/{dispute_id}/outcome", dependencies=auth)
    def dispute_outcome(dispute_id: str, _: str = Depends(andre("disputes/outcome")),
                        req: m.DisputeOutcome = Depends(body(m.DisputeOutcome))) -> dict:
        return svc.dispute_outcome(req.request_id, _id(dispute_id), req.outcome)

    @app.post("/fin/v1/refunds/{campaign_id}", dependencies=auth, status_code=201)
    def refund(campaign_id: str, who: str = Depends(caller("scheduler")),
               req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.propose_refund(who, req.request_id, _id(campaign_id))

    @app.post("/fin/v1/refunds/{refund_id}/decision", dependencies=auth)
    def refund_decide(refund_id: str, _: str = Depends(andre("refunds/decision")),
                      req: m.Decision = Depends(body(m.Decision))) -> dict:
        return svc.decide_refund(req.request_id, _id(refund_id), dump(req))

    # --- jobs, payouts ----------------------------------------------------------------------------------------------

    @app.post("/fin/v1/jobs/{job}/run", dependencies=auth)
    def job(job: str, who: str = Depends(caller("scheduler")), req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.run_job(who, req.request_id, job)

    @app.post("/fin/v1/payout-runs", dependencies=auth, status_code=201)
    def payout_run(who: str = Depends(caller("scheduler")), req: m.PayoutRun = Depends(body(m.PayoutRun))) -> dict:
        return svc.run_payouts(who, req.request_id, req.rail)

    @app.get("/fin/v1/payout-batches/{batch_id}", dependencies=auth)
    def batch_get(batch_id: str, _: str = Depends(caller())) -> dict:
        return svc.get_batch(_id(batch_id))

    @app.get("/fin/v1/payout-batches", dependencies=auth)
    def batch_list(status_: Optional[str] = Query(default=None, alias="status", max_length=20),
                   _: str = Depends(caller())) -> dict:
        return svc.list_batches(status_)

    @app.post("/fin/v1/payout-batches/{batch_id}/decision", dependencies=auth)
    def batch_decide(batch_id: str, request: Request, _: str = Depends(andre("payout-batches/decision")),
                     req: m.Decision = Depends(body(m.Decision))) -> dict:
        if request.headers.get(SECOND_HEADER) is not None:
            # AEGIS N17-13: two tokens on one request are one actor; the second approver sends its own request
            svc.founder_refused("payout-batches/decision:second", "a second-approver token on Andre's request")
            raise Forbidden("the second approver approves on its own request (POST .../second-approval), never "
                            "alongside Andre's token")
        return svc.decide_batch(req.request_id, _id(batch_id), dump(req))

    @app.post("/fin/v1/payout-batches/{batch_id}/second-approval", dependencies=auth)
    def batch_second(batch_id: str, request: Request, req: m.Decision = Depends(body(m.Decision))) -> dict:
        if request.headers.get(FOUNDER_HEADER) is not None or request.headers.get(CALLER_HEADER) is not None:
            svc.founder_refused("payout-batches/second-approval", "another identity's token on the second approval")
            raise Forbidden("the second approval carries the second approver's token only (one identity per request)")
        try:
            second.verify(request.headers.get(SECOND_HEADER))
        except FounderRefused as exc:
            svc.founder_refused("payout-batches/second-approval", exc.reason)
            raise
        return svc.second_approve_batch(req.request_id, _id(batch_id), dump(req))

    @app.post("/fin/v1/payout-batches/{batch_id}/release", dependencies=auth)
    def batch_release(batch_id: str, request: Request, who: str = Depends(caller("scheduler")),
                      req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        if request.headers.get(FOUNDER_HEADER) is not None or request.headers.get(SECOND_HEADER) is not None:
            svc.founder_refused("payout-batches/release", "an approver token was presented on the release route")
            raise Forbidden("the approver cannot release (separation of duties, FIN-19): send no approval token here")
        return svc.release_batch(who, req.request_id, _id(batch_id))

    @app.get("/fin/v1/payables/{payable_id}", dependencies=auth)
    def payable_get(payable_id: str, _: str = Depends(caller())) -> dict:
        return svc.get_payable(_id(payable_id))

    @app.get("/fin/v1/exceptions", dependencies=auth)
    def exceptions(_: str = Depends(caller())) -> dict:
        return svc.list_exceptions()

    @app.post("/fin/v1/exceptions/{exception_id}/decision", dependencies=auth)
    def exception_decide(exception_id: str, _: str = Depends(andre("exceptions/decision")),
                         req: m.ExceptionDecision = Depends(body(m.ExceptionDecision))) -> dict:
        return svc.decide_exception(req.request_id, _id(exception_id), req.decision)

    # --- reconciliation, treasury, clawbacks, close, journal ---------------------------------------------------------

    @app.post("/fin/v1/reconciliations/run", dependencies=auth)
    def recon(who: str = Depends(caller("scheduler")), req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.run_recon(who, req.request_id)

    @app.get("/fin/v1/breaks", dependencies=auth)
    def breaks(_: str = Depends(caller())) -> dict:
        return svc.list_breaks()

    @app.post("/fin/v1/breaks/{break_id}/resolution", dependencies=auth)
    def break_resolve(break_id: str, _: str = Depends(andre("breaks/resolution")),
                      req: m.BreakResolution = Depends(body(m.BreakResolution))) -> dict:
        return svc.resolve_break(req.request_id, _id(break_id), dump(req))

    @app.get("/fin/v1/treasury", dependencies=auth)
    def treasury(_: str = Depends(caller())) -> dict:
        return svc.treasury_view()

    @app.post("/fin/v1/treasury/sweeps", dependencies=auth, status_code=201)
    def sweeps(who: str = Depends(caller("scheduler")), req: m.SweepProposal = Depends(body(m.SweepProposal))) -> dict:
        return svc.propose_sweep(who, req.request_id, req.amount)

    @app.post("/fin/v1/treasury/sweeps/{op_id}/decision", dependencies=auth)
    def sweep_decide(op_id: str, _: str = Depends(andre("treasury/sweeps")), req: m.Decision = Depends(body(m.Decision))) -> dict:
        return svc.decide_treasury(req.request_id, "sweep", _id(op_id), dump(req))

    @app.post("/fin/v1/treasury/funding", dependencies=auth, status_code=201)
    def funding(who: str = Depends(caller("scheduler")), req: m.FundingProposal = Depends(body(m.FundingProposal))) -> dict:
        return svc.propose_funding(who, req.request_id, _id(req.batch_id))

    @app.post("/fin/v1/treasury/funding/{op_id}/decision", dependencies=auth)
    def funding_decide(op_id: str, _: str = Depends(andre("treasury/funding")),
                       req: m.Decision = Depends(body(m.Decision))) -> dict:
        return svc.decide_treasury(req.request_id, "funding", _id(op_id), dump(req))

    @app.post("/fin/v1/treasury/top-ups", dependencies=auth)
    def top_up(_: str = Depends(andre("treasury/top-ups")), req: m.TopUp = Depends(body(m.TopUp))) -> dict:
        return svc.top_up(req.request_id, dump(req))

    @app.post("/fin/v1/clawbacks/{payee_id}/write-off", dependencies=auth)
    def write_off(payee_id: str, _: str = Depends(andre("clawbacks/write-off")),
                  req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.write_off(req.request_id, _id(payee_id))

    @app.post("/fin/v1/close/{entity}/{period}/tasks/{task}", dependencies=auth)
    def close_task(entity: str, period: str, task: str, who: str = Depends(caller("scheduler")),
                   req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
            raise Invalid("period must be YYYY-MM")
        return svc.close_task(who, req.request_id, _id(entity), period, _id(task))

    @app.post("/fin/v1/close/{entity}/{period}/approve", dependencies=auth)
    def close_approve(entity: str, period: str, _: str = Depends(andre("close/approve")),
                      req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
            raise Invalid("period must be YYYY-MM")
        return svc.close_approve(req.request_id, _id(entity), period)

    @app.post("/fin/v1/journal/{entity}/corrections", dependencies=auth, status_code=201)
    def correction(entity: str, _: str = Depends(andre("journal/corrections")),
                   req: m.Correction = Depends(body(m.Correction))) -> dict:
        return svc.correction(req.request_id, _id(entity), dump(req))

    @app.get("/fin/v1/journal/{entity}/entries", dependencies=auth)
    def journal_entries(entity: str, cursor: int = Query(default=0, ge=0, le=10**9), _: str = Depends(caller())) -> dict:
        return svc.journal_entries(_id(entity), cursor)

    @app.get("/fin/v1/journal/{entity}/trial-balance", dependencies=auth)
    def trial_balance(entity: str, as_of: Optional[str] = Query(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
                      _: str = Depends(caller())) -> dict:
        return svc.trial_balance(_id(entity), as_of)

    # --- controls, rules, tax, audit, reconcile ------------------------------------------------------------------------

    @app.get("/fin/v1/controls", dependencies=auth)
    def controls(_: str = Depends(caller())) -> dict:
        return svc.controls_view()

    @app.post("/fin/v1/controls/{control_id}/results", dependencies=auth)
    def control_result(control_id: str, who: str = Depends(andre_or_caller("controls/results", "compliance_38")),
                       req: m.ControlResult = Depends(body(m.ControlResult))) -> dict:
        return svc.record_control_result(who, req.request_id, _id(control_id), dump(req))

    @app.get("/fin/v1/rules", dependencies=auth)
    def rules(_: str = Depends(caller())) -> dict:
        return svc.rules_view()

    @app.post("/fin/v1/rules/proposals", dependencies=auth, status_code=201)
    def rules_propose(_: str = Depends(andre("rules/proposals")),
                      req: m.RuleProposalRequest = Depends(body(m.RuleProposalRequest))) -> dict:
        return svc.create_rule_proposal(req.request_id, req.model_dump(exclude={"request_id"}))

    @app.post("/fin/v1/rules/decisions", dependencies=auth)
    def rules_decide(_: str = Depends(andre("rules/decisions")), req: m.RuleDecisions = Depends(body(m.RuleDecisions))) -> dict:
        return svc.decide_rules(req.request_id, [d.model_dump() for d in req.decisions])

    @app.post("/fin/v1/tax/readiness", dependencies=auth)
    def tax_readiness(_: str = Depends(andre("tax/readiness")), req: m.TaxReadiness = Depends(body(m.TaxReadiness))) -> dict:
        return svc.tax_readiness(req.request_id, dump(req))

    @app.get("/fin/v1/tax/1099/{tax_year}", dependencies=auth)
    def form_1099(tax_year: int, _: str = Depends(andre("tax/1099"))) -> dict:
        if not 2026 <= tax_year <= 2100:
            raise Invalid("tax year out of range")
        return svc.form_1099("andre", tax_year)

    @app.get("/fin/v1/audit/export", dependencies=auth)
    def audit(who: str = Depends(caller()), since: Optional[str] = Query(default=None, max_length=40),
              until: Optional[str] = Query(default=None, max_length=40),
              cursor: int = Query(default=0, ge=0, le=10**12)) -> dict:
        try:
            return svc.audit_export(who, since, until, cursor)
        except ValueError:
            raise Invalid("since/until must be RFC 3339 timestamps with offset") from None

    @app.get("/fin/v1/reconcile", dependencies=auth)
    def reconcile_plan(_: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile_plan()

    @app.post("/fin/v1/reconcile", dependencies=auth)
    def reconcile(_: str = Depends(andre("reconcile")), req: m.ReconcileRequest = Depends(body(m.ReconcileRequest))) -> dict:
        return svc.reconcile(req.request_id, req.head_sha256, list(req.void_lines), list(req.void_event_ids))

    return app


def build_ports(settings: config_mod.Settings) -> Ports:
    """Production wiring: every port is its fail-closed stand-in except the V&I, Compliance and Legal thin clients,
    each wired only when all three of its FIN_VI_* / FIN_COMPLIANCE_* / FIN_LEGAL_* settings are present, and Stripe
    incoming when FIN_STRIPE_INCOMING=1."""
    ports = Ports()
    if settings.vi_url:
        from clients import HttpVerification
        ports.vi = HttpVerification(settings.vi_url, settings.vi_token, settings.vi_caller_token)
    if settings.compliance_url:
        from clients import HttpCompliance
        ports.compliance = HttpCompliance(settings.compliance_url, settings.compliance_token,
                                          settings.compliance_caller_token)
    if settings.legal_url:
        from clients import HttpLegal
        ports.legal = HttpLegal(settings.legal_url, settings.legal_token, settings.legal_caller_token)
    if settings.stripe_incoming:
        from stripe_incoming import StripeIncoming
        ports.stripe_in = StripeIncoming(settings.stripe_secret_key.reveal(), settings.stripe_webhook_secret.reveal(),
                                         settings.stripe_livemode, settings.stripe_success_url,
                                         settings.stripe_cancel_url)
    return ports


def build_service(settings: config_mod.Settings, clock: Optional[Clock] = None, ports: Optional[Ports] = None,
                  ledger=None) -> Service:
    clock = clock or SystemClock()
    if ledger is None:
        ledger = (HttpLedgerClient(settings.ledger_url, settings.ledger_token)
                  if settings.ledger_url and settings.ledger_token else UnconfiguredLedgerClient())
    with open(settings.seed_path, "rb") as fh:
        seed_bytes = fh.read()
    expected = settings.seed_sha256 if (settings.allow_unpinned_seed and settings.seed_sha256) else \
        config_mod.PINNED_SEED_SHA256
    return Service(settings, Recorder(ledger), RecordLog(settings.data_dir), seed_bytes, expected,
                   ports or build_ports(settings), clock, config_mod.PINNED_SEED_SHA256)


def _app_from_env() -> FastAPI:
    settings = config_mod.load()
    return create_app(build_service(settings), settings)


app = _app_from_env()


def main() -> None:
    import serve

    host = os.environ.get("FIN_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("FIN_PORT", "8410"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
