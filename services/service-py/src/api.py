"""
REST surface for Customer Service (30) + Client Success (29) (ADR 0014).

Same discipline as security-py / legal-py (the request-limit, no-store, bearer and caller blocks are copied from
security-py's api.py):
- fail-closed bearer auth on every route except /health; the service refuses to start without SVC_SERVICE_TOKEN;
  ``hmac.compare_digest`` in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-SVC-Caller-Token`` (SVC_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token is a 403;
- Andre's actions arrive through the ``dashboard`` caller AND carry ``X-Andre-Approval-Token`` (FounderGate,
  legal-py's): the dashboard alone is never Andre;
- a body naming a date of birth, government id, card or bank account number, IP or device anywhere is refused 422
  before it is parsed (legal-py's FORBIDDEN_KEYS);
- every response carries ``Cache-Control: no-store``; /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless
  SVC_BIND_ADDR says otherwise; port 8460; hardened launcher (serve.py);
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
from errors import Forbidden, Invalid, SvcError
from founder import HEADER as FOUNDER_HEADER
from founder import FounderGate
from legal_client import HttpLegal
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from reasons import R
from service import CATALOGS, JOBS, SupportService
from store import BodyStore, DataDirLock, RecordLog

log = logging.getLogger("service.api")

CALLER_HEADER = "X-SVC-Caller-Token"
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

# legal-py's i03_acceptance.FORBIDDEN_KEYS: refused anywhere in a body, at any depth (ADR 0014 decision 21)
FORBIDDEN_KEYS = frozenset({
    "ip", "ip_address", "ipaddress", "ip_addr", "remote_addr", "remote_ip", "client_ip", "x_forwarded_for",
    "user_agent", "useragent", "ua", "device", "device_id", "device_fingerprint", "fingerprint", "browser",
    "dob", "date_of_birth", "birth_date", "birthdate", "ssn", "tin", "ein", "tax_id", "government_id", "passport",
    "drivers_license", "national_id", "card_number", "pan", "cvv", "cvc", "account_number", "routing_number", "iban",
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



SV_ID = re.compile(r"^sv-[a-z]{3}-[0-9a-f]{40}$")
ITEM_ID = re.compile(r"^[a-z][a-z0-9_-]{2,59}$")
ACCOUNT_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
CONTACT_REF = re.compile(r"^[a-z_]{1,20}:[A-Za-z0-9._-]{1,100}$")
BRAND = re.compile(r"^(zbm|zbc)$")


def _id(value: str, rx: re.Pattern) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


CATALOG_MODELS = {"kb": m.ArticleSave, "template": m.TemplateSave, "offer": m.OfferSave}
CATALOG_PATHS = {"kb": "kb/articles", "template": "templates", "offer": "offers"}


def create_app(service: SupportService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Customer Service (30) + Client Success (29)", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None,
                  description="Tickets, approved answers, escalations, consent, health scores and save plans.")
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

    def andre(request: Request, who: str = Depends(dashboard)) -> str:
        gate.verify(request.headers.get(FOUNDER_HEADER))
        return "andre"

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = forbidden_keys(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "this service never takes an IP, "
                                               "user-agent, device, date of birth, government id or payment data",
                                               "type": "forbidden_field"} for b in bad[:20]])
            try:
                return model.model_validate(payload).model_dump(mode="json")
            except ValidationError as exc:
                raise RequestValidationError(
                    [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False,
                                                                                   include_input=False)]
                ) from None
        return parse

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(SvcError)
    def _domain(_: Request, exc: SvcError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    # ------------------------------------------------------------------ health and status

    @app.get("/health")
    def health() -> dict:
        return {"status": svc.health()["status"]}          # unauthenticated: up or degraded, nothing more

    @app.get("/svc/v1/status", dependencies=auth)
    def full_status(who: str = Depends(dashboard)) -> dict:
        return {**svc.health(), "andre_approvals_configured": gate.configured}

    # ------------------------------------------------------------------ contacts and consent

    @app.post("/svc/v1/contacts", dependencies=auth, status_code=201)
    def save_contact(req: dict = Depends(body(m.ContactSave)), who: str = Depends(caller("hub", "onboarding"))) -> dict:
        return svc.save_contact(who, req)

    @app.get("/svc/v1/contacts/{contact_id}", dependencies=auth)
    def contact(contact_id: str, who: str = Depends(dashboard)) -> dict:
        return svc.contact_view(_id(contact_id, SV_ID))

    @app.get("/svc/v1/contacts/{contact_id}/consents", dependencies=auth)
    def consents(contact_id: str, who: str = Depends(caller("dashboard", "hub", "compliance_38"))) -> list:
        return svc.consents_view(_id(contact_id, SV_ID))

    @app.post("/svc/v1/consents", dependencies=auth, status_code=201)
    def consent(req: dict = Depends(body(m.ConsentRecord)), who: str = Depends(caller("hub", "onboarding"))) -> dict:
        return svc.record_consent(who, req)

    @app.post("/svc/v1/consents/revoke", dependencies=auth)
    def revoke(req: dict = Depends(body(m.ConsentRevoke)), who: str = Depends(caller("hub", "dashboard"))) -> dict:
        return svc.revoke_consent(who, req)

    # ------------------------------------------------------------------ inbound

    @app.post("/svc/v1/chat/messages", dependencies=auth, status_code=201)
    def chat(req: dict = Depends(body(m.ChatIn)), who: str = Depends(caller("hub"))) -> dict:
        return svc.inbound(who, "chat", req)

    @app.get("/svc/v1/chat/threads/{ticket_id}", dependencies=auth)
    def thread(ticket_id: str, contact_ref: str = Query(max_length=121), brand: str = Query(max_length=3),
               who: str = Depends(caller("hub"))) -> dict:
        return svc.thread(_id(ticket_id, SV_ID), _id(contact_ref, CONTACT_REF), _id(brand, BRAND))

    @app.post("/svc/v1/inbound/email", dependencies=auth, status_code=201)
    def email_in(req: dict = Depends(body(m.EmailIn)), who: str = Depends(caller("email_gateway"))) -> dict:
        return svc.inbound(who, "email", req)

    @app.post("/svc/v1/inbound/sms", dependencies=auth, status_code=201)
    def sms_in(req: dict = Depends(body(m.SmsIn)), who: str = Depends(caller("sms_gateway"))) -> dict:
        return svc.inbound(who, "sms", req)

    # ------------------------------------------------------------------ phone (plumbing; no voice provider)

    @app.post("/svc/v1/calls", dependencies=auth, status_code=201)
    def call(req: dict = Depends(body(m.CallIn)), who: str = Depends(caller("voice_gateway"))) -> dict:
        return svc.record_call(who, req)

    @app.post("/svc/v1/calls/route", dependencies=auth)
    def call_route(req: dict = Depends(body(m.CallRoute)), who: str = Depends(caller("voice_gateway"))) -> dict:
        return svc.route_call(req["brand"])

    @app.post("/svc/v1/calls/{call_id}/handoff", dependencies=auth)
    def call_handoff(call_id: str, req: dict = Depends(body(m.RequestOnly)),
                     who: str = Depends(caller("voice_gateway", "dashboard"))) -> dict:
        return svc.call_handoff(who, _id(call_id, SV_ID), req)

    @app.put("/svc/v1/phone/routing/{brand}", dependencies=auth)
    def routing(brand: str, req: dict = Depends(body(m.RoutingSet)), who: str = Depends(andre)) -> dict:
        return svc.set_routing(_id(brand, BRAND), req)

    # ------------------------------------------------------------------ tickets

    @app.get("/svc/v1/tickets", dependencies=auth)
    def tickets(status_: Optional[str] = Query(default=None, alias="status",
                                               pattern="^(open|pending_customer|escalated|resolved|closed)$"),
                brand: Optional[str] = Query(default=None, pattern="^(zbm|zbc)$"),
                queue: Optional[str] = Query(default=None, pattern="^(bot|andre)$"),
                who: str = Depends(dashboard)) -> list:
        return svc.tickets_view(status_, brand, queue)

    @app.get("/svc/v1/tickets/{ticket_id}", dependencies=auth)
    def ticket(ticket_id: str, who: str = Depends(dashboard)) -> dict:
        return svc.ticket_view(_id(ticket_id, SV_ID))

    @app.post("/svc/v1/tickets/{ticket_id}/reply", dependencies=auth, status_code=201)
    def reply(ticket_id: str, req: dict = Depends(body(m.Reply)), who: str = Depends(andre)) -> dict:
        return svc.reply(_id(ticket_id, SV_ID), req)

    @app.post("/svc/v1/tickets/{ticket_id}/status", dependencies=auth)
    def set_status(ticket_id: str, req: dict = Depends(body(m.StatusSet)), who: str = Depends(dashboard)) -> dict:
        return svc.set_status(who, _id(ticket_id, SV_ID), req)

    @app.post("/svc/v1/tickets/{ticket_id}/priority", dependencies=auth)
    def set_priority(ticket_id: str, req: dict = Depends(body(m.PrioritySet)), who: str = Depends(dashboard)) -> dict:
        return svc.set_priority(who, _id(ticket_id, SV_ID), req)

    # ------------------------------------------------------------------ catalogue: KB articles, templates, offers

    def catalog_routes(catalog: str) -> None:
        base = f"/svc/v1/{CATALOG_PATHS[catalog]}"
        model = CATALOG_MODELS[catalog]

        @app.post(base, dependencies=auth, status_code=201, name=f"{catalog}_save")
        def save(req: dict = Depends(body(model)), who: str = Depends(dashboard)) -> dict:
            return svc.save_item(catalog, req)

        @app.get(base, dependencies=auth, name=f"{catalog}_list")
        def listing(who: str = Depends(dashboard)) -> list:
            return svc.catalog_view(catalog)

        @app.post(base + "/{item_id}/approve", dependencies=auth, name=f"{catalog}_approve")
        def approve(item_id: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre)) -> dict:
            return svc.approve_item(catalog, _id(item_id, ITEM_ID), req)

        @app.post(base + "/{item_id}/retire", dependencies=auth, name=f"{catalog}_retire")
        def retire(item_id: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
            return svc.retire_item(catalog, _id(item_id, ITEM_ID), req)

    for c in CATALOGS:
        catalog_routes(c)

    # ------------------------------------------------------------------ client success

    @app.post("/svc/v1/accounts", dependencies=auth, status_code=201)
    def save_account(req: dict = Depends(body(m.AccountSave)), who: str = Depends(caller("onboarding"))) -> dict:
        return svc.save_account(who, req)

    @app.get("/svc/v1/accounts", dependencies=auth)
    def accounts(who: str = Depends(dashboard)) -> list:
        return svc.accounts_view()

    @app.post("/svc/v1/accounts/{account_id}/events", dependencies=auth, status_code=201)
    def account_event(account_id: str, req: dict = Depends(body(m.AccountEvent)),
                      who: str = Depends(caller("hub"))) -> dict:
        return svc.account_event(who, _id(account_id, ACCOUNT_ID), req)

    @app.get("/svc/v1/accounts/{account_id}/health", dependencies=auth)
    def account_health(account_id: str, who: str = Depends(dashboard)) -> dict:
        return svc.account_health(_id(account_id, ACCOUNT_ID))

    @app.get("/svc/v1/renewals", dependencies=auth)
    def renewals(who: str = Depends(dashboard)) -> list:
        return svc.renewals_view()

    @app.get("/svc/v1/save-plans", dependencies=auth)
    def plans(status_: Optional[str] = Query(default=None, alias="status", pattern="^(active|closed)$"),
              who: str = Depends(dashboard)) -> list:
        return svc.plans_view(status_)

    @app.post("/svc/v1/save-plans/{plan_id}/offer", dependencies=auth)
    def plan_offer(plan_id: str, req: dict = Depends(body(m.OfferSelect)), who: str = Depends(andre)) -> dict:
        return svc.select_offer(_id(plan_id, SV_ID), req)

    @app.post("/svc/v1/save-plans/{plan_id}/steps/{step_id}/done", dependencies=auth)
    def plan_step(plan_id: str, step_id: str, req: dict = Depends(body(m.RequestOnly)),
                  who: str = Depends(dashboard)) -> dict:
        return svc.complete_step(_id(plan_id, SV_ID), _id(step_id, SV_ID), req)

    @app.post("/svc/v1/save-plans/{plan_id}/close", dependencies=auth)
    def plan_close(plan_id: str, req: dict = Depends(body(m.PlanClose)), who: str = Depends(andre)) -> dict:
        return svc.close_plan(_id(plan_id, SV_ID), req)

    @app.post("/svc/v1/nps/surveys", dependencies=auth, status_code=201)
    def survey(req: dict = Depends(body(m.SurveySend)), who: str = Depends(caller("dashboard", "scheduler"))) -> dict:
        return svc.send_survey(who, req)

    @app.post("/svc/v1/nps/responses", dependencies=auth, status_code=201)
    def nps(req: dict = Depends(body(m.NpsResponse)), who: str = Depends(caller("hub"))) -> dict:
        return svc.nps_response(who, req)

    # ------------------------------------------------------------------ alerts, outbound, jobs, audit

    @app.get("/svc/v1/alerts", dependencies=auth)
    def alerts(who: str = Depends(dashboard)) -> list:
        return svc.alerts_view()

    @app.get("/svc/v1/outbound", dependencies=auth)
    def outbound(status_: Optional[str] = Query(default=None, alias="status",
                                                pattern="^(queued|sent|cancelled)$"),
                 who: str = Depends(dashboard)) -> list:
        return svc.outbound_view(status_)

    @app.post("/svc/v1/jobs/{name}/run", dependencies=auth)
    def run_job(name: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(caller("scheduler"))) -> dict:
        if name not in JOBS:
            raise Invalid(R("JOB_UNKNOWN"))
        return svc.run_job(name, req)

    @app.get("/svc/v1/audit/integrity", dependencies=auth)
    def integrity(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        res = svc.verify_integrity(force=True)
        return {"integrity": res, "ledger_valid": svc.rec.client.verify(), "log_length": len(svc.log)}

    @app.get("/svc/v1/audit/events", dependencies=auth)
    def audit_events(since: int = Query(default=1, ge=1, le=10_000_000), limit: int = Query(default=200, ge=1, le=1000),
                     who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        return svc.audit_events(since, limit)

    return app


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build_ports(settings: config_mod.Settings) -> Ports:
    ports = Ports.default()
    if settings.legal_url:
        ports.handoffs["legal_37"] = HttpLegal(settings.legal_url, settings.legal_token, settings.legal_caller_token)
    return ports


def build(env: Optional[dict] = None):
    """The production wiring: settings, ledger, log, body store, ports; returns (asgi, service)."""
    settings = config_mod.load(env)
    lock = DataDirLock(settings.data_dir)
    if settings.ledger_url and settings.ledger_token:
        ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
    else:
        ledger = UnconfiguredLedgerClient()
    svc = SupportService(settings, Recorder(ledger), RecordLog(settings.data_dir), BodyStore(settings.data_dir),
                         build_ports(settings))
    svc.data_dir_lock = lock
    return _wrap(create_app(svc, settings)), svc


def main() -> None:
    import serve
    app, svc = build()
    serve.run(app, svc.settings.bind_addr, svc.settings.port)


if __name__ == "__main__":
    main()
