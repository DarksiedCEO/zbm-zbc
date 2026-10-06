"""
REST surface for New Business Development (12) (ADR 0016). Copied from service-py's api.py (itself security-py's /
legal-py's request-limit, no-store, bearer and caller blocks):
- fail-closed bearer auth on every route except /health; the service refuses to start without NBD_SERVICE_TOKEN;
  ``hmac.compare_digest`` in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-NBD-Caller-Token`` (NBD_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token is a 403;
- Andre's actions arrive through the ``dashboard`` caller AND carry ``X-Andre-Approval-Token`` (FounderGate,
  legal-py's): the dashboard alone is never Andre;
- a body naming a date of birth, government or tax id (TIN, SSN, EIN, ITIN), card or bank account number, phone, IP
  or device anywhere is refused 422 before it is parsed;
- every response carries ``Cache-Control: no-store``; /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless
  NBD_BIND_ADDR says otherwise; port 8480; hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 128 KiB (413), JSON only (415),
  JSON nesting depth and member count bounded (422), body read deadline (408). Error bodies carry a reason code from
  reasons.py and never echo request content.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
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
from errors import Forbidden, Invalid, NbdError
from founder import HEADER as FOUNDER_HEADER
from founder import FounderGate
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from reasons import R
from service import JOBS, BizDevService
from store import RecordLog

log = logging.getLogger("bizdev.api")

CALLER_HEADER = "X-NBD-Caller-Token"
MAX_BODY_BYTES = 128 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

ROUTE_LIMITS: list[tuple[re.Pattern, int]] = []
DEFAULT_ROUTE_LIMIT = 128 * 1024

# service-py's FORBIDDEN_KEYS (legal-py's i03_acceptance), with every tax-id spelling, payment fields and phone fields
# added: refused anywhere in a body, at any depth (ADR 0016 decisions 16 and 23)
FORBIDDEN_KEYS = frozenset({
    "ip", "ip_address", "ipaddress", "ip_addr", "remote_addr", "remote_ip", "client_ip", "x_forwarded_for",
    "user_agent", "useragent", "ua", "device", "device_id", "device_fingerprint", "fingerprint", "browser",
    "dob", "date_of_birth", "birth_date", "birthdate", "ssn", "tin", "ein", "itin", "tax_id", "taxid", "tax_number",
    "taxpayer_id", "taxpayer_identification_number", "social_security_number", "employer_identification_number",
    "government_id", "passport", "drivers_license", "driver_license", "national_id", "card", "card_number",
    "credit_card", "pan", "cvv", "cvc", "expiry", "account_number", "bank_account", "routing_number", "iban", "swift",
    "bic", "phone", "phone_number", "mobile", "cell", "sms", "telephone",
})


def forbidden_keys(obj: Any, path: str = "", depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if str(k).lower().replace("-", "_") in FORBIDDEN_KEYS:
                out.append(p[:120])
            out += forbidden_keys(v, p, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:10_000]:
            out += forbidden_keys(v, path, depth + 1)
    return out


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


def make_require_auth(required_token: str, on_failure: Callable[[], None] = lambda: None) -> Callable:
    def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            on_failure()
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="missing or malformed Authorization header (expected: Bearer <token>)",
                                headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization.removeprefix("Bearer ")
        try:
            valid = hmac.compare_digest(supplied, required_token)
        except TypeError:
            valid = False  # non-ASCII token: invalid (401), never a 500
        if not valid:
            on_failure()
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




class NoStore:
    """Every answer (a released secret above all) is marked not to be cached or stored anywhere."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_no_store(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers += [(b"cache-control", b"no-store"), (b"pragma", b"no-cache"),
                            (b"x-content-type-options", b"nosniff")]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_no_store)



NB_ID = re.compile(r"^nb-[a-z]{3}-[0-9a-f]{40}$")


def _id(value: str, rx: re.Pattern = NB_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


def _version(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", value):
        raise Invalid(R("INVALID"), field="path")
    return int(value)


def create_app(service: BizDevService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC New Business Development (12)", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None, description="Bids, RFP/RFQ responses, pitches and partnerships.")
    svc = service
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    gate = FounderGate.build(settings.andre_token, settings.service_token, settings.caller_tokens.values())
    app.state.service = svc
    app.state.gate = gate

    def caller(*allowed: str) -> Callable:
        def dep(request: Request) -> str:
            name = callers.identify(request.headers.get(CALLER_HEADER))
            if name is None:
                raise Forbidden(R("CALLER_UNKNOWN"))
            if allowed and name not in allowed:
                raise Forbidden(R("CALLER_NOT_ALLOWED"))
            return name
        return dep

    dashboard = caller("dashboard")
    worker = caller("dashboard", "bizdev_agent")

    def andre(request: Request, who: str = Depends(dashboard)) -> str:
        gate.verify(request.headers.get(FOUNDER_HEADER))
        return "andre"

    def andre_if_presented(request: Request) -> bool:
        """True only for a VERIFIED Andre token through the dashboard; a presented but wrong token is refused (403),
        never ignored; a token through any other caller is refused."""
        supplied = request.headers.get(FOUNDER_HEADER)
        if supplied is None:
            return False
        if callers.identify(request.headers.get(CALLER_HEADER)) != "dashboard":
            raise Forbidden(R("CALLER_NOT_ALLOWED"))
        gate.verify(supplied)
        return True

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = forbidden_keys(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "this service never takes an IP, "
                                               "user-agent, device, date of birth, government or tax id, payment "
                                               "data or a phone number", "type": "forbidden_field"} for b in bad[:20]])
            try:
                return model.model_validate(payload).model_dump(mode="json", exclude_none=True)
            except ValidationError as exc:
                raise RequestValidationError(
                    [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False,
                                                                                   include_input=False)]
                ) from None
        return parse

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(NbdError)
    def _domain(_: Request, exc: NbdError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    P = "/nbd/v1"

    # ------------------------------------------------------------------ health and status

    @app.get("/health")
    def health():
        st = svc.health()["status"]                        # unauthenticated: ok, degraded or closed, nothing more
        if st == "closed":
            return JSONResponse(status_code=503, content={"status": st})
        return {"status": st}

    @app.get(P + "/status", dependencies=auth)
    def full_status(who: str = Depends(dashboard)) -> dict:
        return {**svc.health(), "andre_approvals_configured": gate.configured}

    @app.get(P + "/intelligences", dependencies=auth)
    def intelligences(who: str = Depends(worker)) -> list:
        return registry()

    # ------------------------------------------------------------------ pursuits

    @app.post(P + "/pursuits", dependencies=auth, status_code=201)
    def open_pursuit(req: dict = Depends(body(m.PursuitCreate)), who: str = Depends(worker)) -> dict:
        return svc.open_pursuit(who, req)

    @app.post(P + "/pursuits/import", dependencies=auth)
    def import_pursuits(req: dict = Depends(body(m.Import)),
                        who: str = Depends(caller("bizdev_agent", "scheduler"))) -> dict:
        return svc.import_pursuits(who, req)

    @app.get(P + "/pursuits", dependencies=auth)
    def pursuits(stage: Optional[str] = Query(default=None, pattern="^(identified|qualifying|responding|submitted|won|"
                                                                    "lost|no_bid|withdrawn)$"),
                 who: str = Depends(worker)) -> list:
        return svc.pursuits_view(stage)

    @app.get(P + "/pursuits/{pid}", dependencies=auth)
    def pursuit(pid: str, who: str = Depends(worker)) -> dict:
        return svc.pursuit(_id(pid))

    @app.post(P + "/pursuits/{pid}/qualification", dependencies=auth)
    def qualify(pid: str, req: dict = Depends(body(m.Qualification)), who: str = Depends(worker)) -> dict:
        return svc.qualify(who, _id(pid), req)

    @app.post(P + "/pursuits/{pid}/bid-decision", dependencies=auth)
    def bid_decision(request: Request, pid: str, req: dict = Depends(body(m.BidDecision)),
                     who: str = Depends(worker)) -> dict:
        return svc.decide_bid(who, _id(pid), req, andre=andre_if_presented(request))

    @app.post(P + "/pursuits/{pid}/value", dependencies=auth)
    def pursuit_value(pid: str, req: dict = Depends(body(m.ValueSet)), who: str = Depends(worker)) -> dict:
        return svc.set_pursuit_value(who, _id(pid), req)

    @app.post(P + "/pursuits/{pid}/deadline", dependencies=auth)
    def pursuit_deadline(pid: str, req: dict = Depends(body(m.DeadlineSet)), who: str = Depends(andre)) -> dict:
        return svc.set_deadline(_id(pid), req)

    @app.post(P + "/pursuits/{pid}/deal-approval", dependencies=auth)
    def pursuit_deal_approval(pid: str, req: dict = Depends(body(m.DealApproval)), who: str = Depends(andre)) -> dict:
        return svc.approve_deal(_id(pid), req)

    @app.post(P + "/pursuits/{pid}/checklist", dependencies=auth)
    def extend_checklist(pid: str, req: dict = Depends(body(m.ChecklistExtend)), who: str = Depends(worker)) -> dict:
        return svc.extend_checklist(who, _id(pid), req)

    @app.post(P + "/pursuits/{pid}/checklist/{item_id}/attest", dependencies=auth)
    def attest(pid: str, item_id: str, req: dict = Depends(body(m.Attest)), who: str = Depends(andre)) -> dict:
        return svc.attest(_id(pid), _id(item_id), req)

    @app.post(P + "/pursuits/{pid}/won", dependencies=auth)
    def pursuit_won(pid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
        return svc.pursuit_won(_id(pid), req)

    @app.post(P + "/pursuits/{pid}/lost", dependencies=auth)
    def pursuit_lost(pid: str, req: dict = Depends(body(m.Lost)), who: str = Depends(worker)) -> dict:
        return svc.pursuit_lost(who, _id(pid), req)

    @app.post(P + "/pursuits/{pid}/withdraw", dependencies=auth)
    def pursuit_withdraw(pid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
        return svc.withdraw(_id(pid), req)

    @app.post(P + "/pursuits/{pid}/agreements", dependencies=auth, status_code=201)
    def pursuit_agreement(pid: str, req: dict = Depends(body(m.AgreementRequest)), who: str = Depends(worker)) -> dict:
        return svc.request_agreement(who, "pursuit", _id(pid), req)

    @app.get(P + "/handoffs", dependencies=auth)
    def handoffs(who: str = Depends(worker)) -> list:
        return svc.handoffs_view()

    # ------------------------------------------------------------------ boilerplate blocks

    @app.post(P + "/blocks", dependencies=auth, status_code=201)
    def create_block(req: dict = Depends(body(m.BlockCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_block(who, req)

    @app.get(P + "/blocks", dependencies=auth)
    def blocks(who: str = Depends(worker)) -> list:
        return svc.blocks_view()

    @app.get(P + "/blocks/{bid}", dependencies=auth)
    def block(bid: str, who: str = Depends(worker)) -> dict:
        return svc.block(_id(bid))

    @app.post(P + "/blocks/{bid}/versions", dependencies=auth, status_code=201)
    def block_version(bid: str, req: dict = Depends(body(m.BlockVersion)), who: str = Depends(worker)) -> dict:
        return svc.add_block_version(who, _id(bid), req)

    @app.post(P + "/blocks/{bid}/versions/{v}/approve", dependencies=auth)
    def approve_block(bid: str, v: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre)) -> dict:
        return svc.approve_block(_id(bid), _version(v), req)

    @app.post(P + "/blocks/{bid}/versions/{v}/retire", dependencies=auth)
    def retire_block(bid: str, v: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
        return svc.retire_block(_id(bid), _version(v), req)

    # ------------------------------------------------------------------ responses, pitches, submissions

    @app.post(P + "/responses", dependencies=auth, status_code=201)
    def create_response(req: dict = Depends(body(m.ResponseCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_response(who, req)

    @app.get(P + "/responses/{rid}", dependencies=auth)
    def response(rid: str, who: str = Depends(worker)) -> dict:
        return svc.response(_id(rid))

    @app.post(P + "/responses/{rid}/versions", dependencies=auth, status_code=201)
    def response_version(rid: str, req: dict = Depends(body(m.ResponseVersion)), who: str = Depends(worker)) -> dict:
        return svc.add_response_version(who, _id(rid), req)

    @app.post(P + "/responses/{rid}/approve", dependencies=auth)
    def approve_response(rid: str, req: dict = Depends(body(m.ResponseApprove)), who: str = Depends(andre)) -> dict:
        return svc.approve_response(_id(rid), req)

    @app.post(P + "/responses/{rid}/submit", dependencies=auth, status_code=201)
    def submit(rid: str, req: dict = Depends(body(m.ResponseSubmit)), who: str = Depends(worker)) -> dict:
        return svc.submit(who, _id(rid), req)

    @app.get(P + "/submissions", dependencies=auth)
    def submissions(status_: Optional[str] = Query(default=None, alias="status",
                                                   pattern="^(queued|sending|submitted|failed|cancelled)$"),
                    who: str = Depends(worker)) -> list:
        return svc.submissions_view(status_)

    @app.post(P + "/submissions/{sid}/cancel", dependencies=auth)
    def cancel_submission(sid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(worker)) -> dict:
        return svc.cancel_submission(who, _id(sid), req)

    # ------------------------------------------------------------------ partners

    @app.post(P + "/partners", dependencies=auth, status_code=201)
    def register_partner(req: dict = Depends(body(m.PartnerCreate)), who: str = Depends(worker)) -> dict:
        return svc.register_partner(who, req)

    @app.get(P + "/partners", dependencies=auth)
    def partners(who: str = Depends(worker)) -> list:
        return svc.partners_view()

    @app.get(P + "/partners/{pid}", dependencies=auth)
    def partner(pid: str, who: str = Depends(worker)) -> dict:
        return svc.partner(_id(pid))

    @app.post(P + "/partners/{pid}/rate", dependencies=auth)
    def propose_rate(pid: str, req: dict = Depends(body(m.RatePropose)), who: str = Depends(worker)) -> dict:
        return svc.propose_rate(who, _id(pid), req)

    @app.post(P + "/partners/{pid}/rate/approve", dependencies=auth)
    def approve_rate(pid: str, req: dict = Depends(body(m.RateApprove)), who: str = Depends(andre)) -> dict:
        return svc.approve_rate(_id(pid), req)

    @app.post(P + "/partners/{pid}/payee", dependencies=auth)
    def set_payee(pid: str, req: dict = Depends(body(m.PayeeSet)), who: str = Depends(andre)) -> dict:
        return svc.set_payee(_id(pid), req)

    @app.post(P + "/partners/{pid}/agreements", dependencies=auth, status_code=201)
    def partner_agreement(pid: str, req: dict = Depends(body(m.AgreementRequest)), who: str = Depends(worker)) -> dict:
        return svc.request_agreement(who, "partner", _id(pid), req)

    @app.post(P + "/partner-deals", dependencies=auth, status_code=201)
    def register_deal(req: dict = Depends(body(m.PartnerDealCreate)), who: str = Depends(worker)) -> dict:
        return svc.register_deal(who, req)

    @app.get(P + "/partner-deals", dependencies=auth)
    def partner_deals(status_: Optional[str] = Query(default=None, alias="status", pattern="^(registered|won|lost)$"),
                      who: str = Depends(worker)) -> list:
        return svc.partner_deals_view(status_)

    @app.get(P + "/partner-deals/{did}", dependencies=auth)
    def partner_deal(did: str, who: str = Depends(worker)) -> dict:
        return svc.partner_deal(_id(did))

    @app.post(P + "/partner-deals/{did}/value", dependencies=auth)
    def deal_value(did: str, req: dict = Depends(body(m.DealValueSet)), who: str = Depends(worker)) -> dict:
        return svc.set_deal_value(who, _id(did), req)

    @app.post(P + "/partner-deals/{did}/deal-approval", dependencies=auth)
    def deal_approval(did: str, req: dict = Depends(body(m.DealApproval)), who: str = Depends(andre)) -> dict:
        return svc.approve_deal(_id(did), req)

    @app.post(P + "/partner-deals/{did}/won", dependencies=auth)
    def deal_won(did: str, req: dict = Depends(body(m.PartnerDealWon)), who: str = Depends(andre)) -> dict:
        return svc.deal_won(_id(did), req)

    @app.post(P + "/partner-deals/{did}/lost", dependencies=auth)
    def deal_lost(did: str, req: dict = Depends(body(m.Lost)), who: str = Depends(worker)) -> dict:
        return svc.deal_lost(who, _id(did), req)

    @app.post(P + "/finance/events", dependencies=auth)
    def finance_event(req: dict = Depends(body(m.FinanceEvent)), who: str = Depends(caller("finance_31"))) -> dict:
        return svc.finance_event(who, req)

    @app.post(P + "/finance/payouts/{pay_id}/paid", dependencies=auth)
    def payout_paid(pay_id: str, req: dict = Depends(body(m.PayoutPaid)), who: str = Depends(caller("finance_31"))):
        return svc.payout_paid(who, _id(pay_id), req)

    @app.get(P + "/payouts", dependencies=auth)
    def payouts(status_: Optional[str] = Query(default=None, alias="status",
                                               pattern="^(queued|sending|with_finance|paid|cancelled)$"),
                who: str = Depends(caller("dashboard", "finance_31"))) -> list:
        return svc.payouts_view(status_)

    # ------------------------------------------------------------------ outreach

    @app.post(P + "/contacts", dependencies=auth, status_code=201)
    def create_contact(req: dict = Depends(body(m.ContactCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_contact(who, req)

    @app.get(P + "/contacts/{cid}", dependencies=auth)
    def contact(cid: str, who: str = Depends(worker)) -> dict:
        return svc.contact(_id(cid))

    @app.post(P + "/contacts/{cid}/merge-fields", dependencies=auth)
    def merge_fields(cid: str, req: dict = Depends(body(m.MergeFields)), who: str = Depends(dashboard)) -> dict:
        return svc.verify_merge_fields(_id(cid), req)

    @app.post(P + "/templates", dependencies=auth, status_code=201)
    def create_template(req: dict = Depends(body(m.TemplateCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_template(who, req)

    @app.get(P + "/templates", dependencies=auth)
    def templates(who: str = Depends(worker)) -> list:
        return svc.templates_view()

    @app.get(P + "/templates/{tid}", dependencies=auth)
    def template(tid: str, who: str = Depends(worker)) -> dict:
        return svc.template(_id(tid))

    @app.post(P + "/templates/{tid}/versions", dependencies=auth, status_code=201)
    def template_version(tid: str, req: dict = Depends(body(m.TemplateVersion)), who: str = Depends(worker)) -> dict:
        return svc.add_template_version(who, _id(tid), req)

    @app.post(P + "/templates/{tid}/versions/{v}/approve", dependencies=auth)
    def approve_template(tid: str, v: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre)) -> dict:
        return svc.approve_template(_id(tid), _version(v), req)

    @app.post(P + "/outreach/email", dependencies=auth, status_code=201)
    def queue_email(req: dict = Depends(body(m.QueueEmail)), who: str = Depends(caller("bizdev_agent"))) -> dict:
        return svc.queue_email(who, req)

    @app.get(P + "/outreach/messages", dependencies=auth)
    def messages(status_: Optional[str] = Query(default=None, alias="status",
                                                pattern="^(queued|sending|sent|failed|cancelled)$"),
                 who: str = Depends(worker)) -> list:
        return svc.messages_view(status_)

    @app.post(P + "/outreach/messages/{mid}/cancel", dependencies=auth)
    def cancel_message(mid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(worker)) -> dict:
        return svc.cancel_message(who, _id(mid), req)

    @app.post(P + "/events/email", dependencies=auth)
    def email_event(req: dict = Depends(body(m.EmailEvent)), who: str = Depends(caller("provider_events"))) -> dict:
        return svc.email_event(who, req)

    @app.post(P + "/replies", dependencies=auth, status_code=201)
    def reply(req: dict = Depends(body(m.Reply)), who: str = Depends(caller("provider_events"))) -> dict:
        return svc.reply(who, req)

    @app.post(P + "/unsubscribe", dependencies=auth)
    def unsubscribe(req: dict = Depends(body(m.Unsubscribe)), who: str = Depends(caller("hub"))) -> dict:
        return svc.unsubscribe(who, req)

    @app.post(P + "/suppressions", dependencies=auth, status_code=201)
    def suppress(req: dict = Depends(body(m.Suppress)),
                 who: str = Depends(caller("dashboard", "bizdev_agent", "provider_events"))) -> dict:
        return svc.suppress(who, req)

    @app.get(P + "/suppressions", dependencies=auth)
    def suppressions(who: str = Depends(caller("dashboard", "compliance_38"))) -> list:
        return svc.suppressions_view()

    @app.get(P + "/holds", dependencies=auth)
    def holds(status_: Optional[str] = Query(default=None, alias="status", pattern="^(active|lifted|opted_out)$"),
              who: str = Depends(dashboard)) -> list:
        return svc.holds_view(status_)

    @app.post(P + "/holds/{hold_id}/decision", dependencies=auth)
    def hold_decision(hold_id: str, req: dict = Depends(body(m.HoldDecision)), who: str = Depends(andre)) -> dict:
        return svc.decide_hold(_id(hold_id), req)

    # ------------------------------------------------------------------ tasks, jobs, audit

    @app.get(P + "/tasks", dependencies=auth)
    def tasks(status_: Optional[str] = Query(default=None, alias="status", pattern="^(open|closed)$"),
              who: str = Depends(dashboard)) -> list:
        return svc.tasks_view(status_)

    @app.post(P + "/tasks/{task_id}/close", dependencies=auth)
    def close_task(task_id: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
        return svc.close_task(_id(task_id), req)

    @app.post(P + "/jobs/{name}/run", dependencies=auth)
    def run_job(name: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(caller("scheduler"))) -> dict:
        if name not in JOBS:
            raise Invalid(R("JOB_UNKNOWN"))
        return svc.run_job(name, req)

    @app.get(P + "/audit/integrity", dependencies=auth)
    def integrity(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        return svc.audit_integrity()

    @app.get(P + "/audit/export", dependencies=auth)
    def audit_export(since: int = Query(default=1, ge=1, le=10_000_000), limit: int = Query(default=200, ge=1, le=1000),
                     who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        return svc.audit_export(since, limit)

    return app


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build(env: Optional[dict] = None):
    """The production wiring: settings, ledger, log, ports; returns (asgi, service). No port is wired: every
    provider and department client is NOT_BUILT (config.NOT_BUILT refuses start if one is selected)."""
    settings = config_mod.load(env)
    lock = settings.data_dir_lock              # the flock, taken by config.load before the log is opened
    token = lock.claim() if lock is not None else None   # claimed BEFORE the log is built
    try:
        if settings.ledger_url and settings.ledger_token:
            ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
        else:
            ledger = UnconfiguredLedgerClient()
        svc = BizDevService(settings, Recorder(ledger), RecordLog(settings.data_dir), Ports.default(),
                            lock_token=token)
    except BaseException:
        if lock is not None:
            lock.release_claim(token)
        raise
    return _wrap(create_app(svc, settings)), svc


def main() -> None:
    import serve
    app, svc = build()
    serve.run(app, svc.settings.bind_addr, svc.settings.port)


if __name__ == "__main__":
    main()
