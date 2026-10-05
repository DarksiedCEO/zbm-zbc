"""
REST surface for Cybersecurity (22) (ADR 0012).

Same discipline as legal-py / verification-py (the request-limit, bearer and caller blocks are copied from
legal-py's api.py):
- fail-closed bearer auth on every route except /health; the service refuses to start without SEC_SERVICE_TOKEN;
  ``hmac.compare_digest`` in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-SEC-Caller-Token`` (SEC_CALLER_TOKENS; SHA-256 digests compared against EVERY
  configured token, no early exit); a wrong or absent caller token is a 403; every authentication failure is
  counted by detection rule D1;
- Andre's actions arrive through the ``dashboard`` caller AND carry a passkey approval in the body: the
  dashboard alone is never Andre, and no route returns a secret value to the dashboard;
- every response carries ``Cache-Control: no-store`` (a released value must never sit in a cache);
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless SEC_BIND_ADDR says otherwise; port 8440;
  hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 256 KiB (413; scans 2 MiB), JSON
  only (415), JSON nesting depth and member count bounded (422), body read deadline (408). Error bodies carry a
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
import crypto
import models as m
from compliance_client import HttpCompliance
from errors import Forbidden, Invalid, SecError
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from reasons import R
from service import JOBS, SecurityService
from store import DataDirLock, RecordLog, SealedStore

log = logging.getLogger("security.api")

CALLER_HEADER = "X-SEC-Caller-Token"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/sec/v1/scans$"), 2 * 1024 * 1024),
]
DEFAULT_ROUTE_LIMIT = 256 * 1024


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


REF = re.compile(r"^(vault:)?[a-z0-9_]{1,40}\.[A-Za-z0-9_][A-Za-z0-9._-]{0,79}$")
SEC_ID = re.compile(r"^sc-[a-z]{3}-[0-9a-f]{40}$")
CRED_ID = re.compile(r"^[A-Za-z0-9_-]{16,1400}$")
HOLD_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SERVICE_CALLERS = tuple(c for c in config_mod.KNOWN_CALLERS if c != "dashboard")


def _id(value: str, rx: re.Pattern) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


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


def create_app(service: SecurityService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Cybersecurity (22)", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None,
                  description="Vault, service identity, freezes, incidents, alerts and vulnerability tracking.")
    svc = service
    auth = [Depends(make_require_auth(settings.service_token, svc.auth_failed))]
    callers = Callers(settings.caller_tokens)
    app.state.service = svc

    def caller(*allowed: str) -> Callable:
        def dep(request: Request) -> str:
            name = callers.identify(request.headers.get(CALLER_HEADER))
            if name is None:
                svc.auth_failed()
                raise Forbidden(R("CALLER_UNKNOWN"))
            if allowed and name not in allowed:
                raise Forbidden(R("CALLER_NOT_ALLOWED"))
            return name
        return dep

    any_caller = caller()
    dashboard = caller("dashboard")
    services_only = caller(*SERVICE_CALLERS)

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            try:
                return model.model_validate(payload).model_dump(mode="json")
            except ValidationError as exc:
                raise RequestValidationError(
                    [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
                ) from None
        return parse

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(SecError)
    def _domain(_: Request, exc: SecError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    # ------------------------------------------------------------------ health

    @app.get("/health")
    def health() -> dict:
        # unauthenticated: up or degraded, nothing more (AEGIS L8); the detail is /sec/v1/status (dashboard)
        return {"status": svc.health()["status"]}

    @app.middleware("http")
    async def flush_alerts_after(request: Request, call_next):
        # AEGIS L4: alerts queued while the lock was held (detections, integrity) are sent here, outside the lock
        response = await call_next(request)
        await run_in_threadpool(svc.flush_alerts)
        return response

    @app.get("/sec/v1/status", dependencies=auth)
    def full_status(who: str = Depends(dashboard)) -> dict:
        return svc.health()

    # ------------------------------------------------------------------ approvals and passkeys

    @app.post("/sec/v1/approvals/challenges", dependencies=auth)
    def challenge(req: dict = Depends(body(m.ChallengeRequest)), who: str = Depends(dashboard)) -> dict:
        model = m.APPROVAL_ACTIONS.get(req["action"])
        if model is None:
            raise Invalid(R("INVALID"), field="action")
        # the body is normalised exactly as the route will normalise it (defaults filled in), so the hash the
        # passkey signs is the hash the route recomputes
        try:
            normal = model.model_validate({**req["body"], "approval": m.PLACEHOLDER_APPROVAL}).model_dump(mode="json")
        except ValidationError as exc:
            raise RequestValidationError(
                [{**e, "loc": ("body", "body", *e.get("loc", ()))} for e in exc.errors(include_url=False,
                                                                                       include_input=False)]) from None
        return svc.approval_challenge(req["action"], req["target"], normal)

    @app.post("/sec/v1/passkeys/enroll/options", dependencies=auth)
    def enroll_options(req: dict = Depends(body(m.EnrollOptions)), who: str = Depends(dashboard)) -> dict:
        return svc.enroll_options(req)

    @app.post("/sec/v1/passkeys/enroll", dependencies=auth, status_code=201)
    def enroll(req: dict = Depends(body(m.Enroll)), who: str = Depends(dashboard)) -> dict:
        return svc.enroll(req)

    @app.get("/sec/v1/passkeys", dependencies=auth)
    def passkeys(who: str = Depends(dashboard)) -> list:
        return svc.passkeys_view()

    @app.post("/sec/v1/passkeys/{credential_id}/revoke", dependencies=auth)
    def revoke(credential_id: str, req: dict = Depends(body(m.Revoke)), who: str = Depends(dashboard)) -> dict:
        return svc.revoke_passkey(_id(credential_id, CRED_ID), req)

    # ------------------------------------------------------------------ vault

    @app.post("/sec/v1/secrets", dependencies=auth, status_code=201)
    def store(req: dict = Depends(body(m.ServiceStore)), who: str = Depends(services_only)) -> dict:
        return svc.store(who, req)

    @app.post("/sec/v1/secrets/andre", dependencies=auth, status_code=201)
    def andre_store(req: dict = Depends(body(m.AndreStore)), who: str = Depends(dashboard)) -> dict:
        return svc.andre_store(req)

    @app.get("/sec/v1/secrets", dependencies=auth)
    def list_secrets(who: str = Depends(dashboard)) -> list:
        return svc.list_secrets()

    @app.get("/sec/v1/secrets/{ref}", dependencies=auth)
    def secret_status(ref: str, who: str = Depends(any_caller)) -> dict:
        return svc.status(who, _id(ref, REF))

    @app.post("/sec/v1/secrets/{ref}/use", dependencies=auth)
    def use(ref: str, req: dict = Depends(body(m.Use)), who: str = Depends(services_only)) -> dict:
        return svc.use(who, _id(ref, REF), req["purpose"])

    def _actor(who: str, req: dict) -> str:
        if who == "dashboard":
            return "andre"
        if req.get("approval") is not None:
            raise Invalid(R("INVALID"), field="approval")
        return who

    @app.post("/sec/v1/secrets/{ref}/rotate", dependencies=auth)
    def rotate(ref: str, req: dict = Depends(body(m.Rotate)), who: str = Depends(any_caller)) -> dict:
        return svc.rotate(_actor(who, req), _id(ref, REF), req)

    @app.post("/sec/v1/secrets/{ref}/destroy", dependencies=auth)
    def destroy(ref: str, req: dict = Depends(body(m.Destroy)), who: str = Depends(any_caller)) -> dict:
        return svc.destroy(_actor(who, req), _id(ref, REF), req)

    @app.post("/sec/v1/secrets/{ref}/access", dependencies=auth)
    def access(ref: str, req: dict = Depends(body(m.SetAccess)), who: str = Depends(dashboard)) -> dict:
        return svc.set_access(_id(ref, REF), req)

    @app.post("/sec/v1/clients/{client_id}/destroy", dependencies=auth)
    def destroy_client(client_id: str, req: dict = Depends(body(m.JobRun)),
                       who: str = Depends(services_only)) -> dict:
        return svc.destroy_client(who, _id(client_id, CLIENT_ID), req)

    # ------------------------------------------------------------------ service identity

    @app.post("/sec/v1/identity/tokens", dependencies=auth, status_code=201)
    def mint(req: dict = Depends(body(m.Mint)), who: str = Depends(any_caller)) -> dict:
        return svc.mint(who, req["audience"], req["scope"])

    @app.get("/sec/v1/identity/jwks", dependencies=auth)
    def jwks(who: str = Depends(any_caller)) -> dict:
        return svc.jwks()

    @app.get("/sec/v1/identity/denylist", dependencies=auth)
    def denylist(who: str = Depends(any_caller)) -> dict:
        return svc.denylist()

    # ------------------------------------------------------------------ freezes and Legal holds

    @app.post("/sec/v1/freezes", dependencies=auth, status_code=201)
    def freeze(req: dict = Depends(body(m.Freeze)), who: str = Depends(dashboard)) -> dict:
        return svc.freeze(req)

    @app.get("/sec/v1/freezes", dependencies=auth)
    def freezes(who: str = Depends(dashboard)) -> list:
        return svc.freezes_view()

    @app.post("/sec/v1/freezes/{freeze_id}/lift", dependencies=auth)
    def lift(freeze_id: str, req: dict = Depends(body(m.Lift)), who: str = Depends(dashboard)) -> dict:
        return svc.lift(_id(freeze_id, SEC_ID), req)

    @app.post("/sec/v1/holds", dependencies=auth, status_code=201)
    def preserve(req: dict = Depends(body(m.Preserve)), who: str = Depends(caller("legal_37"))) -> dict:
        return svc.preserve(who, req)

    @app.post("/sec/v1/holds/{hold_id}/release", dependencies=auth)
    def release_hold(hold_id: str, req: dict = Depends(body(m.ReleaseHold)),
                     who: str = Depends(caller("legal_37"))) -> dict:
        return svc.release_hold(who, _id(hold_id, HOLD_ID), req)

    # ------------------------------------------------------------------ incidents

    @app.post("/sec/v1/incidents", dependencies=auth, status_code=201)
    def open_incident(req: dict = Depends(body(m.IncidentOpen)), who: str = Depends(dashboard)) -> dict:
        return svc.open_incident(who, req)

    @app.get("/sec/v1/incidents", dependencies=auth)
    def incidents(status_: Optional[str] = Query(default=None, alias="status", pattern="^(open|closed)$"),
                  who: str = Depends(dashboard)) -> list:
        return svc.incidents_view(status_)

    @app.get("/sec/v1/incidents/{incident_id}", dependencies=auth)
    def incident(incident_id: str, who: str = Depends(dashboard)) -> dict:
        return svc.incident(_id(incident_id, SEC_ID))

    @app.post("/sec/v1/incidents/{incident_id}/notes", dependencies=auth)
    def note(incident_id: str, req: dict = Depends(body(m.IncidentNote)), who: str = Depends(dashboard)) -> dict:
        return svc.note_incident(who, _id(incident_id, SEC_ID), req)

    @app.post("/sec/v1/incidents/{incident_id}/close", dependencies=auth)
    def close(incident_id: str, req: dict = Depends(body(m.IncidentClose)), who: str = Depends(dashboard)) -> dict:
        return svc.close_incident(_id(incident_id, SEC_ID), req)

    # ------------------------------------------------------------------ vulnerabilities

    @app.post("/sec/v1/scans", dependencies=auth, status_code=201)
    def scan(req: dict = Depends(body(m.Scan)), who: str = Depends(caller("scheduler", "dashboard"))) -> dict:
        return svc.ingest_scan(who, req)

    @app.get("/sec/v1/findings", dependencies=auth)
    def findings(status_: Optional[str] = Query(default=None, alias="status", pattern="^(open|fixed|accepted)$"),
                 who: str = Depends(caller("dashboard", "compliance_38"))) -> list:
        return svc.findings_view(status_)

    @app.post("/sec/v1/findings/{finding_id}/accept", dependencies=auth)
    def accept(finding_id: str, req: dict = Depends(body(m.AcceptRisk)), who: str = Depends(dashboard)) -> dict:
        return svc.accept_risk(_id(finding_id, SEC_ID), req)

    # ------------------------------------------------------------------ jobs and audit

    @app.post("/sec/v1/jobs/{name}/run", dependencies=auth)
    def run_job(name: str, req: dict = Depends(body(m.JobRun)), who: str = Depends(caller("scheduler"))) -> dict:
        if name not in JOBS:
            raise Invalid(R("JOB_UNKNOWN"))
        return svc.run_job(name, req)

    @app.get("/sec/v1/audit/integrity", dependencies=auth)
    def integrity(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        res = svc.verify_integrity(force=True)
        return {"integrity": res, "ledger_valid": svc.rec.client.verify(), "log_length": len(svc.log)}

    @app.get("/sec/v1/audit/events", dependencies=auth)
    def audit_events(since: int = Query(default=1, ge=1, le=10_000_000), limit: int = Query(default=200, ge=1, le=1000),
                     who: str = Depends(dashboard)) -> dict:
        return svc.audit_events(since, limit)

    @app.get("/sec/v1/audit/access", dependencies=auth)
    def audit_access(who: str = Depends(dashboard)) -> list:
        return svc.recent_access()

    return app


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build_ports(settings: config_mod.Settings) -> Ports:
    ports = Ports.default()
    if settings.compliance_url:
        ports.compliance = HttpCompliance(settings.compliance_url, settings.compliance_token,
                                          settings.compliance_caller_token)
    return ports


def build_kms(settings: config_mod.Settings) -> crypto.KeyService:
    if settings.kms == "local_file":
        return crypto.LocalFileKeyService(settings.local_master_key.reveal())
    return crypto.NotWiredKeyService()


def build(env: Optional[dict] = None):
    """The production wiring: settings, ledger, log, sealed store, key service, ports; returns (asgi, service)."""
    settings = config_mod.load(env)
    lock = DataDirLock(settings.data_dir)
    if settings.ledger_url and settings.ledger_token:
        ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
    else:
        ledger = UnconfiguredLedgerClient()
    svc = SecurityService(settings, Recorder(ledger), RecordLog(settings.data_dir), SealedStore(settings.data_dir),
                          build_kms(settings), build_ports(settings))
    svc.data_dir_lock = lock
    return _wrap(create_app(svc, settings)), svc


def main() -> None:
    import serve
    app, svc = build()
    serve.run(app, svc.settings.bind_addr, svc.settings.port)


if __name__ == "__main__":
    main()
