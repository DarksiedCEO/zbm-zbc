"""
REST surface for the Client Fix lane of Client Delivery & Operations (28) (ADR 0017). Copied from bizdev-py's api.py
(itself service-py's / security-py's / legal-py's request-limit, no-store, bearer and caller blocks):
- fail-closed bearer auth on every route except /health; the service refuses to start without CFX_SERVICE_TOKEN;
  ``hmac.compare_digest`` in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-CFX-Caller-Token`` (CFX_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token is a 403;
- Andre's actions arrive through the ``dashboard`` caller AND carry ``X-Andre-Approval-Token`` (FounderGate): the
  dashboard alone is never Andre; the client's approvals arrive through the ``hub`` caller AND carry a live
  ``X-CFX-Client-Session`` of exactly that client;
- every body is refused 422 when it names a password, a secret, a raw token or personal data anywhere, or carries a
  credential-shaped value anywhere (``SECRET_REFUSED``, secrets_guard), before it is parsed;
- every response carries ``Cache-Control: no-store``; /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless
  CFX_BIND_ADDR says otherwise; port 8500; hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 64 KiB (1 MiB for a fix plan) (413),
  JSON only (415), JSON nesting depth and member count bounded (422), body read deadline (408). Error bodies carry a
  reason code from reasons.py and never echo request content.
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
import connectors
import models as m
import secrets_guard
from errors import CfxError, Forbidden, Invalid
from founder import HEADER as FOUNDER_HEADER
from founder import FounderGate
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from reasons import R
from service import JOBS, ClientFixService
from store import RecordLog

log = logging.getLogger("clientfix.api")

CALLER_HEADER = "X-CFX-Caller-Token"
SESSION_HEADER = "X-CFX-Client-Session"
MAX_BODY_BYTES = 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

# a fix plan carries page bodies and product descriptions (up to 512 KB of text): its route takes up to 1 MiB; every
# other route 64 KiB
ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [(re.compile(r"^/cfx/v1/jobs/[^/]+/plan$"), 1024 * 1024)]
DEFAULT_ROUTE_LIMIT = 64 * 1024


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




CFX_ID = re.compile(r"^cfx-[a-z]{3}-[0-9a-f]{40}$")


def _id(value: str, rx: re.Pattern = CFX_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


def create_app(service: ClientFixService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Client Delivery & Operations (28): client fix lane", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None, description="Client-approved, deterministic fixes with proof.")
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
            if request.headers.get(FOUNDER_HEADER) is not None and name != "dashboard":
                raise Forbidden(R("CALLER_NOT_ALLOWED"))       # Andre's token through any other caller
            return name
        return dep

    dashboard = caller("dashboard")
    audit = caller("dashboard", "compliance_38")

    def andre(request: Request, who: str = Depends(dashboard)) -> str:
        try:
            gate.verify(request.headers.get(FOUNDER_HEADER))
        except CfxError as exc:
            svc.record_refusal(request.url.path, exc.reason)
            raise
        return "andre"

    def session(request: Request) -> Optional[str]:
        return request.headers.get(SESSION_HEADER)

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = secrets_guard.forbidden_keys(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "this service never takes a password, a "
                                               "secret, a raw token or personal data", "type": "forbidden_field"}
                                              for b in bad[:20]])
            if secrets_guard.secret_value(payload):
                raise Invalid(R("SECRET_REFUSED"))
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

    @app.exception_handler(CfxError)
    def _domain(_: Request, exc: CfxError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    P = "/cfx/v1"

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

    @app.get(P + "/connectors", dependencies=auth)
    def connector_list(who: str = Depends(caller("dashboard", "clientfix_agent", "fire_team", "compliance_38"))) -> list:
        return connectors.describe()

    # ------------------------------------------------------------------ connections and client sessions

    @app.post(P + "/connections", dependencies=auth, status_code=201)
    def register(req: dict = Depends(body(m.ConnectionCreate)), who: str = Depends(caller("hub"))) -> dict:
        return svc.register_connection(who, req)

    @app.get(P + "/connections", dependencies=auth)
    def connection_list(client_id: Optional[str] = Query(default=None, pattern=r"^onb-(?:[0-9a-f]{32}|[0-9a-f]{64})$"),
                        who: str = Depends(caller("dashboard", "clientfix_agent", "compliance_38"))) -> list:
        return svc.connections_view(client_id)

    @app.get(P + "/connections/{cid}", dependencies=auth)
    def connection(cid: str, who: str = Depends(caller("dashboard", "clientfix_agent", "hub"))) -> dict:
        return svc.connection_view(_id(cid))

    @app.post(P + "/connections/{cid}/revoke", dependencies=auth)
    def revoke(cid: str, req: dict = Depends(body(m.Revoke)), who: str = Depends(caller("hub", "dashboard"))) -> dict:
        return svc.revoke_connection(who, _id(cid), req)

    @app.post(P + "/client-sessions", dependencies=auth, status_code=201)
    def open_session(req: dict = Depends(body(m.SessionOpen)), who: str = Depends(caller("hub"))) -> dict:
        return svc.open_session(who, req)

    # ------------------------------------------------------------------ findings, jobs, quotes, payment

    @app.post(P + "/findings", dependencies=auth, status_code=201)
    def finding(req: dict = Depends(body(m.FindingIn)), who: str = Depends(caller("orchestrator", "dashboard"))) -> dict:
        return svc.add_finding(who, req)

    @app.post(P + "/jobs", dependencies=auth, status_code=201)
    def create_job(req: dict = Depends(body(m.JobCreate)),
                   who: str = Depends(caller("clientfix_agent", "dashboard"))) -> dict:
        return svc.create_job(who, req)

    @app.get(P + "/jobs", dependencies=auth)
    def jobs(client_id: Optional[str] = Query(default=None, pattern=r"^onb-(?:[0-9a-f]{32}|[0-9a-f]{64})$"),
             status_: Optional[str] = Query(default=None, alias="status", pattern=r"^[a-z_]{3,20}$"),
             who: str = Depends(caller("dashboard", "clientfix_agent", "compliance_38"))) -> list:
        return svc.jobs_view(client_id, status_)

    @app.get(P + "/jobs/{job_id}", dependencies=auth)
    def job(job_id: str, request: Request,
            who: str = Depends(caller("dashboard", "clientfix_agent", "hub", "compliance_38"))) -> dict:
        view = svc.job_view(_id(job_id))
        if who == "hub":                                  # the client sees its own job only, inside its session
            with svc.lock:
                svc.client_of_session(request.headers.get(SESSION_HEADER), view["client_id"])
        return view

    @app.post(P + "/jobs/{job_id}/quote/accept", dependencies=auth)
    def accept_quote(job_id: str, req: dict = Depends(body(m.HashApproval)), who: str = Depends(caller("hub")),
                     token: Optional[str] = Depends(session)) -> dict:
        return svc.accept_quote(token, _id(job_id), req)

    @app.post(P + "/finance/events", dependencies=auth)
    def payment(req: dict = Depends(body(m.PaymentEvent)), who: str = Depends(caller("finance_31"))) -> dict:
        return svc.payment_event(who, req)

    # ------------------------------------------------------------------ fire teams and plans

    @app.get(P + "/jobs/{job_id}/brief", dependencies=auth)
    def brief(job_id: str, who: str = Depends(caller("fire_team", "dashboard"))) -> dict:
        with svc.lock:
            j = svc._get(svc.jobs, _id(job_id), "JOB_NOT_FOUND")
            svc._paid(j)
            return svc.brief(job_id)

    @app.post(P + "/jobs/{job_id}/engage", dependencies=auth)
    def engage(job_id: str, req: dict = Depends(body(m.RequestOnly)),
               who: str = Depends(caller("clientfix_agent", "dashboard", "scheduler"))) -> dict:
        return svc.engage(who, _id(job_id), req)

    @app.post(P + "/jobs/{job_id}/plan", dependencies=auth)
    def plan(job_id: str, req: dict = Depends(body(m.PlanSubmit)),
             who: str = Depends(caller("fire_team", "dashboard"))) -> dict:
        return svc.submit_plan(who, _id(job_id), req)

    @app.post(P + "/jobs/{job_id}/plan/approve", dependencies=auth)
    def approve_plan(job_id: str, req: dict = Depends(body(m.HashApproval)), who: str = Depends(caller("hub")),
                     token: Optional[str] = Depends(session)) -> dict:
        return svc.approve_plan(token, _id(job_id), req)

    # ------------------------------------------------------------------ apply, manual fixes, Andre's controls

    @app.post(P + "/jobs/{job_id}/apply", dependencies=auth)
    def apply(job_id: str, req: dict = Depends(body(m.RequestOnly)),
              who: str = Depends(caller("scheduler", "dashboard"))) -> dict:
        return svc.apply(who, _id(job_id), req)

    @app.post(P + "/jobs/{job_id}/manual-done", dependencies=auth)
    def manual_done(job_id: str, request: Request, req: dict = Depends(body(m.ManualDone)),
                    who: str = Depends(caller("hub", "dashboard"))) -> dict:
        if who == "hub":
            with svc.lock:
                svc.client_of_session(request.headers.get(SESSION_HEADER),
                                      svc._get(svc.jobs, _id(job_id), "JOB_NOT_FOUND")["client_id"])
        return svc.manual_done(who, _id(job_id), req)

    @app.post(P + "/jobs/{job_id}/cancel", dependencies=auth)
    def cancel(job_id: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
        return svc.cancel_job(_id(job_id), req)

    @app.post(P + "/jobs/{job_id}/items/{item_id}/close", dependencies=auth)
    def close_item(job_id: str, item_id: str, req: dict = Depends(body(m.StateDecision)),
                   who: str = Depends(andre)) -> dict:
        return svc.close_item_unfixed(_id(job_id), _id(item_id), req)

    @app.post(P + "/clients/freeze", dependencies=auth)
    def freeze(req: dict = Depends(body(m.FreezeClient)), who: str = Depends(andre)) -> dict:
        return svc.freeze_client(req, True)

    @app.post(P + "/clients/unfreeze", dependencies=auth)
    def unfreeze_client(req: dict = Depends(body(m.FreezeClient)), who: str = Depends(andre)) -> dict:
        return svc.freeze_client(req, False)

    @app.get(P + "/frozen", dependencies=auth)
    def frozen(who: str = Depends(audit)) -> dict:
        return svc.frozen_view()

    @app.post(P + "/frozen/unfreeze", dependencies=auth)
    def unfreeze(req: dict = Depends(body(m.StateDecision)), who: str = Depends(andre)) -> dict:
        return svc.unfreeze(req)

    @app.get(P + "/leases", dependencies=auth)
    def leases(status_: Optional[str] = Query(default=None, alias="status", pattern="^(active|released)$"),
               who: str = Depends(audit)) -> list:
        return svc.leases_view(status_)

    @app.get(P + "/refunds", dependencies=auth)
    def refunds(status_: Optional[str] = Query(default=None, alias="status",
                                               pattern="^(proposed|queued|sending|with_finance)$"),
                who: str = Depends(audit)) -> list:
        return svc.refunds_view(status_)

    @app.post(P + "/refunds/{refund_id}/approve", dependencies=auth)
    def approve_refund(refund_id: str, req: dict = Depends(body(m.HashApproval)), who: str = Depends(andre)) -> dict:
        return svc.approve_refund(_id(refund_id), req)

    # ------------------------------------------------------------------ tasks, runs, audit

    @app.get(P + "/tasks", dependencies=auth)
    def tasks(status_: Optional[str] = Query(default=None, alias="status", pattern="^(open|closed)$"),
              who: str = Depends(dashboard)) -> list:
        return svc.tasks_view(status_)

    @app.post(P + "/tasks/{task_id}/close", dependencies=auth)
    def close_task(task_id: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre)) -> dict:
        return svc.close_task(_id(task_id), req)

    @app.post(P + "/ticks/{name}", dependencies=auth)
    def run_tick(name: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(caller("scheduler"))) -> dict:
        if name not in JOBS:
            raise Invalid(R("JOB_UNKNOWN"))
        return svc.run_job(name, req)

    @app.get(P + "/audit/integrity", dependencies=auth)
    def integrity(who: str = Depends(audit)) -> dict:
        return svc.audit_integrity()

    @app.get(P + "/audit/export", dependencies=auth)
    def audit_export(since: int = Query(default=1, ge=1, le=10_000_000), limit: int = Query(default=200, ge=1, le=1000),
                     who: str = Depends(audit)) -> dict:
        return svc.audit_export(since, limit)

    @app.get(P + "/audit/evidence", dependencies=auth)
    def audit_evidence(limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0, le=10_000_000),
                       event_type: Optional[str] = Query(default=None, pattern=r"^[a-z_]{1,64}$"),
                       who: str = Depends(audit)) -> dict:
        """Unanchored evidence = attempted, not done (bizdev-py AEGIS round 6 M1)."""
        return svc.audit_evidence(limit, offset, event_type)

    return app


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build(env: Optional[dict] = None):
    """The production wiring: settings, ledger, log, ports. No port is wired: the transport, the vault, re-detection,
    Finance, the engineers and every automation port are NOT_BUILT (config.NOT_BUILT refuses start if one is
    selected)."""
    settings = config_mod.load(env)
    lock = settings.data_dir_lock              # the flock, taken by config.load before the log is opened
    token = lock.claim() if lock is not None else None   # claimed BEFORE the log is built
    try:
        if settings.ledger_url and settings.ledger_token:
            ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
        else:
            ledger = UnconfiguredLedgerClient()
        svc = ClientFixService(settings, Recorder(ledger), RecordLog(settings.data_dir), Ports.default(),
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
