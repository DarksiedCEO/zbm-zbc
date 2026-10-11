"""
REST surface for Search & Answer Intelligence (2) — SEO / AEO / GEO / LLMO (ADR 0017). The request-limit, no-store,
bearer and caller blocks are bizdev-py's (itself service-py's / security-py's / legal-py's):
- fail-closed bearer auth on every route except /health; the service refuses to start without SEO_SERVICE_TOKEN;
- caller identity from ``X-SEO-Caller-Token`` (SEO_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token is a 403;
- a client's read-only view comes through the ``hub`` caller AND a tenant token (``X-SEO-Tenant-Token``,
  SEO_TENANT_TOKENS): it sees its own tenant only; another tenant's objects answer 404, exactly like objects that do
  not exist (no existence oracle);
- Andre's actions arrive through the ``dashboard`` caller AND carry ``X-Andre-Approval-Token`` (FounderGate);
- personal-data keys anywhere in a body are refused 422 before it is parsed; every response is ``no-store``;
  /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless SEO_BIND_ADDR says otherwise; port 8500;
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 128 KiB (413), JSON only (415),
  JSON nesting depth and member count bounded (422), body read deadline (408).
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
from errors import Forbidden, Invalid, NotFound, SeoError
from founder import HEADER as FOUNDER_HEADER
from founder import FounderGate
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from reasons import R
from service import JOBS, SeoService
from store import RecordLog

log = logging.getLogger("seo.api")

CALLER_HEADER = "X-SEO-Caller-Token"
TENANT_HEADER = "X-SEO-Tenant-Token"
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
# added: refused anywhere in a body, at any depth (bizdev-py ADR 0016; kept for ADR 0017)
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



SEO_ID = re.compile(r"^seo-[a-z]{3}-[0-9a-f]{40}$")
ENTITY_ID = re.compile(r"^ent-[a-z0-9-]{1,40}$")
TENANT_RX = re.compile(config_mod.TENANT_ID.pattern)


def _id(value: str, rx: re.Pattern = SEO_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


def create_app(service: SeoService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Search & Answer Intelligence (2)", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None, description="SEO / AEO / GEO / LLMO, Wave 1: read core, machine readability, "
                                                "AI-visibility probes, minimal proof, the audit product.")
    svc = service
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    tenant_tokens = Callers(settings.tenant_tokens)
    gate = FounderGate.build(settings.andre_token, settings.service_token,
                             list(settings.caller_tokens.values()) + list(settings.tenant_tokens.values()))
    app.state.service = svc
    app.state.gate = gate

    def caller(*allowed: str) -> Callable:
        def dep(request: Request) -> str:
            name = callers.identify(request.headers.get(CALLER_HEADER))
            if name is None:
                raise Forbidden(R("CALLER_UNKNOWN"))
            if allowed and name not in allowed:
                raise Forbidden(R("CALLER_NOT_ALLOWED"))
            if name != "hub" and request.headers.get(TENANT_HEADER) is not None:
                raise Forbidden(R("CALLER_NOT_ALLOWED"))       # a tenant token only ever travels with the hub
            return name
        return dep

    dashboard = caller("dashboard")

    def andre(request: Request, who: str = Depends(dashboard)) -> str:
        gate.verify(request.headers.get(FOUNDER_HEADER))
        return "andre"

    def andre_if_presented(request: Request) -> bool:
        """True only for a VERIFIED Andre token through the dashboard; a presented but wrong token is refused."""
        supplied = request.headers.get(FOUNDER_HEADER)
        if supplied is None:
            return False
        if callers.identify(request.headers.get(CALLER_HEADER)) != "dashboard":
            raise Forbidden(R("CALLER_NOT_ALLOWED"))
        gate.verify(supplied)
        return True

    def scoped(*allowed: str) -> Callable:
        """The tenant in the path, checked against the caller's scope. ``hub`` sees exactly its tenant token's
        tenant; anything else answers 404 TENANT_NOT_FOUND (the same as a tenant that does not exist)."""
        who_dep = caller(*allowed)

        def dep(request: Request, tid: str, who: str = Depends(who_dep)) -> tuple[str, str]:
            if not TENANT_RX.fullmatch(tid):
                raise Invalid(R("INVALID"), field="path")
            if who == "hub":
                mine = tenant_tokens.identify(request.headers.get(TENANT_HEADER))
                if mine is None:
                    raise Forbidden(R("TENANT_TOKEN_UNKNOWN"))
                if mine != tid:
                    raise NotFound(R("TENANT_NOT_FOUND"))
            svc.tenant_view(tid)                       # 404 for a tenant that does not exist
            return who, tid
        return dep

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = forbidden_keys(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "this service never takes an IP, "
                                               "user-agent, device, date of birth, government or tax id, payment "
                                               "data or a phone-number field", "type": "forbidden_field"}
                                              for b in bad[:20]])
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

    @app.exception_handler(SeoError)
    def _domain(_: Request, exc: SeoError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    P = "/seo/v1"

    # ------------------------------------------------------------------ health, status, pricing

    @app.get("/health")
    def health():
        st = svc.health()["status"]                        # unauthenticated: ok, degraded or closed, nothing more
        if st == "closed":
            return JSONResponse(status_code=503, content={"status": st})
        return {"status": st}

    @app.get(P + "/status", dependencies=auth)
    def full_status(who: str = Depends(dashboard)) -> dict:
        return {**svc.health(), "andre_approvals_configured": gate.configured}

    @app.get(P + "/pricing", dependencies=auth)
    def pricing(who: str = Depends(caller("dashboard", "seo_agent", "hub", "finance_31"))) -> dict:
        return {**config_mod.PRICING, "note": "locked tier table (config); this service charges nothing"}

    # ------------------------------------------------------------------ tenants and kill switches

    @app.get(P + "/tenants", dependencies=auth)
    def tenants(who: str = Depends(dashboard)) -> list:
        return svc.tenants_view()

    @app.post(P + "/tenants", dependencies=auth, status_code=201)
    def create_tenant(req: dict = Depends(body(m.TenantCreate)), who: str = Depends(andre)) -> dict:
        return svc.create_tenant(req)

    @app.get(P + "/tenants/{tid}", dependencies=auth)
    def tenant(scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "compliance_38"))) -> dict:
        return svc.tenant_view(scope[1])

    @app.post(P + "/tenants/{tid}/domains", dependencies=auth)
    def tenant_domains(tid: str, req: dict = Depends(body(m.DomainsSet)), who: str = Depends(andre)) -> dict:
        return svc.set_domains(_id(tid, TENANT_RX), req)

    @app.post(P + "/tenants/{tid}/finance-client", dependencies=auth)
    def tenant_finance_client(tid: str, req: dict = Depends(body(m.FinanceClientBind)),
                              who: str = Depends(andre)) -> dict:
        return svc.set_finance_client(_id(tid, TENANT_RX), req)

    @app.get(P + "/kill-switches", dependencies=auth)
    def switches(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        with svc.lock:
            return svc.switch_view()

    @app.post(P + "/kill-switches", dependencies=auth)
    def set_switch(request: Request, req: dict = Depends(body(m.SwitchSet)),
                   who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        return svc.set_switch(who, req, andre=andre_if_presented(request))

    # ------------------------------------------------------------------ canonical entity record

    @app.get(P + "/tenants/{tid}/entity", dependencies=auth)
    def entity(scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "compliance_38"))) -> dict:
        e = svc.entity_for_tenant(scope[1])
        if e is None:
            raise NotFound(R("ENTITY_NOT_FOUND"))
        return e

    @app.post(P + "/entities/{eid}/fields", dependencies=auth)
    def entity_field(eid: str, req: dict = Depends(body(m.EntityFieldSet)), who: str = Depends(andre)) -> dict:
        return svc.update_entity_field(_id(eid, ENTITY_ID), req)


    # ------------------------------------------------------------------ prompt sets and audits

    @app.post(P + "/tenants/{tid}/prompt-sets", dependencies=auth, status_code=201)
    def create_prompt_set(req: dict = Depends(body(m.PromptSetCreate)),
                          scope: tuple = Depends(scoped("dashboard", "seo_agent"))) -> dict:
        return svc.create_prompt_set(scope[0], scope[1], req)

    @app.get(P + "/tenants/{tid}/prompt-sets/{psid}", dependencies=auth)
    def prompt_set(psid: str, scope: tuple = Depends(scoped("dashboard", "seo_agent", "compliance_38"))) -> dict:
        return svc.prompt_set_view(scope[1], _id(psid))

    @app.post(P + "/tenants/{tid}/audits", dependencies=auth, status_code=201)
    def request_audit(request: Request, req: dict = Depends(body(m.AuditCreate)),
                      scope: tuple = Depends(scoped("dashboard", "seo_agent"))) -> dict:
        return svc.request_audit(scope[0], scope[1], req, andre=andre_if_presented(request))

    @app.get(P + "/tenants/{tid}/audits", dependencies=auth)
    def audits(scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "finance_31", "compliance_38"))) -> list:
        return svc.audits_view(scope[1])

    @app.get(P + "/tenants/{tid}/audits/{aid}/drift", dependencies=auth)
    def drift(aid: str, against: str = Query(pattern=r"^seo-aud-[0-9a-f]{40}$"),
              scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "compliance_38"))) -> dict:
        return svc.drift_view(scope[1], _id(aid), against)

    @app.get(P + "/tenants/{tid}/audits/{aid}", dependencies=auth)
    def audit(aid: str, scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "finance_31",
                                                      "compliance_38"))) -> dict:
        return svc.audit_view(scope[1], _id(aid), full=scope[0] != "finance_31")

    # ------------------------------------------------------------------ first-party log ingests (Wave 2)

    LOG_CALLERS = ("dashboard", "seo_agent", "hub")

    @app.post(P + "/tenants/{tid}/log-ingests", dependencies=auth, status_code=201)
    def create_log_ingest(req: dict = Depends(body(m.LogIngestCreate)), scope: tuple = Depends(scoped(*LOG_CALLERS))):
        return svc.create_log_ingest(scope[0], scope[1], req)

    @app.post(P + "/tenants/{tid}/log-ingests/{iid}/chunks", dependencies=auth)
    def log_chunk(iid: str, req: dict = Depends(body(m.LogChunk)), scope: tuple = Depends(scoped(*LOG_CALLERS))):
        return svc.add_log_chunk(scope[0], scope[1], _id(iid), req)

    @app.post(P + "/tenants/{tid}/log-ingests/{iid}/finish", dependencies=auth)
    def log_finish(iid: str, req: dict = Depends(body(m.RequestOnly)), scope: tuple = Depends(scoped(*LOG_CALLERS))):
        return svc.finish_log_ingest(scope[0], scope[1], _id(iid), req)

    @app.get(P + "/tenants/{tid}/log-ingests", dependencies=auth)
    def log_ingests(scope: tuple = Depends(scoped(*LOG_CALLERS, "compliance_38"))) -> list:
        return svc.log_ingests_view(scope[1])

    @app.get(P + "/tenants/{tid}/log-ingests/{iid}", dependencies=auth)
    def log_ingest(iid: str, scope: tuple = Depends(scoped(*LOG_CALLERS, "compliance_38"))) -> dict:
        return svc.log_ingest_view(scope[1], _id(iid))

    # ------------------------------------------------------------------ scheduled re-audits (Wave 2)

    @app.post(P + "/tenants/{tid}/schedules", dependencies=auth, status_code=201)
    def create_schedule(request: Request, req: dict = Depends(body(m.ScheduleCreate)),
                        scope: tuple = Depends(scoped("dashboard", "seo_agent"))) -> dict:
        return svc.create_schedule(scope[0], scope[1], req, andre=andre_if_presented(request))

    @app.post(P + "/tenants/{tid}/schedules/{sid}/status", dependencies=auth)
    def schedule_status(request: Request, sid: str, req: dict = Depends(body(m.ScheduleStatus)),
                        scope: tuple = Depends(scoped("dashboard", "seo_agent"))) -> dict:
        return svc.set_schedule_status(scope[0], scope[1], _id(sid), req, andre=andre_if_presented(request))

    @app.get(P + "/tenants/{tid}/schedules", dependencies=auth)
    def schedules(scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "compliance_38"))) -> list:
        return svc.schedules_view(scope[1])

    @app.get(P + "/tenants/{tid}/schedules/{sid}", dependencies=auth)
    def schedule(sid: str, scope: tuple = Depends(scoped("dashboard", "seo_agent", "hub", "compliance_38"))) -> dict:
        return svc.schedule_view(scope[1], _id(sid))

    # ------------------------------------------------------------------ department manager (Wave 2)

    @app.get(P + "/department", dependencies=auth)
    def department(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        return svc.department_view()

    @app.post(P + "/agents/{agent}/lifecycle", dependencies=auth)
    def agent_lifecycle(request: Request, agent: str, req: dict = Depends(body(m.AgentMove)),
                        who: str = Depends(dashboard)) -> dict:
        return svc.move_agent(agent, req, andre=andre_if_presented(request))

    # ------------------------------------------------------------------ jobs and audit

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

    @app.get(P + "/audit/evidence", dependencies=auth)
    def audit_evidence(limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0, le=10_000_000),
                       event_type: Optional[str] = Query(default=None, pattern=r"^[a-z_]{1,64}$"),
                       who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        """The evidence view Compliance (38) and auditors use (R6-M1): unanchored evidence = attempted, not done."""
        return svc.audit_evidence(limit, offset, event_type)

    return app


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build(env: Optional[dict] = None):
    """The production wiring: settings, ledger, log, ports; returns (asgi, service). The only connected port is the
    web fetcher; every other port is NOT_CONNECTED (config.NOT_BUILT refuses start if one is selected)."""
    settings = config_mod.load(env)
    lock = settings.data_dir_lock              # the flock, taken by config.load before the log is opened
    token = lock.claim() if lock is not None else None   # claimed BEFORE the log is built
    try:
        if settings.ledger_url and settings.ledger_token:
            ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
        else:
            ledger = UnconfiguredLedgerClient()
        svc = SeoService(settings, Recorder(ledger), RecordLog(settings.data_dir), lock_token=token)
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
