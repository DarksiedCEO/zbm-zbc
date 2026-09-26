"""
REST surface for Compliance (38) (spec §F, ADR 0006).

Same discipline as services/onboarding-py and services/creative-py:
- fail-closed bearer auth on every route except /health; the service
  refuses to start without COMPLIANCE_SERVICE_TOKEN; ``hmac.compare_digest``
  wrapped in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-Compliance-Caller-Token`` (COMPLIANCE_CALLER_TOKENS,
  SHA-256 digests compared against EVERY configured token, no early exit);
  a wrong or absent caller token on a route that needs one is a 403;
- Andre's routes need ``X-Andre-Approval-Token`` (COMPLIANCE_ANDRE_APPROVAL_TOKEN,
  the creative-py FounderGate pattern); a refusal is a 403 and is recorded
  as ``founder_approval_refused``;
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless
  COMPLIANCE_BIND_ADDR says otherwise; hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431),
  per-route body caps inside the 1 MiB service cap (413), JSON content type
  only (415), JSON nesting depth and member count bounded (422), body read
  deadline (408). Error bodies are bounded and never echo request content.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
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
from clock import Clock, SystemClock
from errors import ComplianceError, Forbidden, FounderRefused, Invalid, Unavailable
from fetcher import HttpFeedFetcher, host_of, is_excluded_host, seeded_sources
from founder import FounderGate
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from service import SPEC_SEED_SHA256, ComplianceService, Config, Ports
from store import RecordLog

log = logging.getLogger("compliance.api")

CALLER_HEADER = "X-Compliance-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
MAX_BODY_BYTES = 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

# Per-route body caps (all inside MAX_BODY_BYTES). Gate bodies are the
# largest legal ones (facts + 8 KB caller_context); a register proposal
# carries one row; the seed is never posted (it is read from disk).
ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/compliance/v1/(rule|review|gates/(activation|payout|publish))$"), 256 * 1024),
    (re.compile(r"^/compliance/v1/register/proposals$"), 128 * 1024),
    (re.compile(r"^/compliance/v1/register/decisions$"), 64 * 1024),
    (re.compile(r"^/compliance/v1/reconcile$"), 1024 * 1024),   # up to 10,000 voided ids (AEGIS N15-1)
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


def create_app(service: ComplianceService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Compliance (38)",
                  description="NON-LIVE: V&I, Finance, Legal, sanctions and accessibility providers are fail-closed stand-ins.",
                  version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    founder = FounderGate.build(settings.andre_token, settings.service_token, settings.caller_tokens.values())
    app.state.service = service
    svc = service

    def caller(*allowed: str) -> Callable:
        def dep(x_compliance_caller_token: Optional[str] = Header(default=None)) -> str:
            name = callers.identify(x_compliance_caller_token)
            if name is None:
                raise Forbidden("caller token missing or not recognised")
            if allowed and name not in allowed:
                raise Forbidden("this caller is not authorized for this route")
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

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> Any:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            try:
                return model.model_validate(payload)
            except ValidationError as exc:
                raise RequestValidationError(
                    [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
                ) from None
        return parse

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(ComplianceError)
    def _domain(_: Request, exc: ComplianceError):
        content = {"detail": exc.reason, **exc.body}
        if isinstance(exc, Unavailable):
            content["issued"] = False
            return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content=content)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.middleware("http")
    async def _unhandled(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - only the TYPE is logged (never input)
            log.error("unhandled error: %s", type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.add_middleware(InputLimits)

    @app.get("/health")
    async def health() -> dict:
        return svc.health()

    @app.get("/intelligences", dependencies=auth)
    def intelligences(_: str = Depends(caller())) -> list[dict]:
        return registry()

    # --- gates -----------------------------------------------------------------------

    @app.post("/compliance/v1/rule", dependencies=auth)
    def rule(req: m.RuleRequest = Depends(body(m.RuleRequest)), who: str = Depends(caller("onboarding"))) -> dict:
        return svc.gate(who, "activation", req.request_id, req.subject_id, req.facts, lane=req.lane, route="rule")

    @app.post("/compliance/v1/review", dependencies=auth)
    def review(req: m.ReviewRequest = Depends(body(m.ReviewRequest)),
               who: str = Depends(caller("creative_production"))) -> dict:
        gate = "payout" if req.subject_kind == "zbc_clip" else "publish"
        return svc.gate(who, gate, req.request_id, req.subject_id, req.facts, subject_kind=req.subject_kind,
                        caller_context=req.caller_context, route="review")

    @app.post("/compliance/v1/gates/activation", dependencies=auth)
    def gate_activation(req: m.RuleRequest = Depends(body(m.RuleRequest)),
                        who: str = Depends(caller("onboarding"))) -> dict:
        return svc.gate(who, "activation", req.request_id, req.subject_id, req.facts, lane=req.lane,
                        route="gates/activation")

    def _native(gate: str, kind: str):
        def handler(req: m.ReviewRequest = Depends(body(m.ReviewRequest)),
                    who: str = Depends(caller("creative_production"))) -> dict:
            if req.subject_kind != kind:
                raise Invalid(f"the {gate} gate takes subject_kind {kind}")
            return svc.gate(who, gate, req.request_id, req.subject_id, req.facts, subject_kind=kind,
                            caller_context=req.caller_context, route=f"gates/{gate}")
        return handler

    app.post("/compliance/v1/gates/payout", dependencies=auth)(_native("payout", "zbc_clip"))
    app.post("/compliance/v1/gates/publish", dependencies=auth)(_native("publish", "zbm_work"))

    @app.get("/compliance/v1/rulings/{ruling_id}", dependencies=auth)
    def ruling(ruling_id: str, _: str = Depends(caller())) -> dict:
        return svc.get_ruling(ruling_id[:64])

    # --- sanctions, accessibility, jurisdictions --------------------------------------

    @app.post("/compliance/v1/sanctions/screen", dependencies=auth)
    def screen(req: m.ScreenRequest = Depends(body(m.ScreenRequest)),
               who: str = Depends(caller("onboarding", "finance_31"))) -> dict:
        return svc.screen(who, req.request_id, req.model_dump())

    @app.post("/compliance/v1/accessibility/checks", dependencies=auth)
    def a11y(req: m.A11yRequest = Depends(body(m.A11yRequest)), who: str = Depends(caller("creative_production"))) -> dict:
        return svc.accessibility_check(who, req.request_id, req.model_dump())

    @app.post("/compliance/v1/jurisdictions/resolve", dependencies=auth)
    def resolve(req: m.ResolveRequest = Depends(body(m.ResolveRequest)), who: str = Depends(caller())) -> dict:
        return svc.resolve(who, req.request_id, req.model_dump())

    # --- register -----------------------------------------------------------------------

    @app.get("/compliance/v1/register", dependencies=auth)
    def register(_: str = Depends(caller()),
                 gate: Optional[str] = Query(default=None, pattern=r"^(activation|payout|publish|control)$"),
                 jurisdiction: Optional[str] = Query(default=None, pattern=r"^(ALL|EU|[A-Z]{2}(-[A-Z0-9]{1,3})?)$"),
                 status_: Optional[str] = Query(default=None, alias="status",
                                                pattern=r"^(verified|unverified|expired|superseded)$"),
                 domain: Optional[str] = Query(default=None, pattern=r"^[a-z_]{1,40}$"),
                 page: int = Query(default=1, ge=1, le=10_000)) -> dict:
        return svc.register_rows(gate, jurisdiction, status_, domain, page)

    @app.get("/compliance/v1/register/versions", dependencies=auth)
    def versions(_: str = Depends(caller())) -> list[dict]:
        return svc.version_list()

    @app.get("/compliance/v1/register/{obligation_id}", dependencies=auth)
    def register_row(obligation_id: str, _: str = Depends(caller())) -> dict:
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,39}", obligation_id):
            raise Invalid("obligation id format")
        return svc.register_row(obligation_id)

    @app.post("/compliance/v1/register/proposals", dependencies=auth, status_code=201)
    def propose(request: Request, req: m.ProposalRequest = Depends(body(m.ProposalRequest))) -> dict:
        andre_hdr = request.headers.get(FOUNDER_HEADER)
        if andre_hdr is not None:
            try:
                founder.verify(andre_hdr)
            except FounderRefused as exc:
                svc.founder_refused("register/proposals", exc.reason)
                raise
            who = "andre"
        else:
            who = callers.identify(request.headers.get(CALLER_HEADER))
            if who != "legal_37":
                raise Forbidden("only legal_37 or Andre may create a register proposal")
        return svc.create_proposal(who, req.request_id, req.model_dump(exclude={"request_id"}))

    @app.get("/compliance/v1/inbox", dependencies=auth)
    def inbox(_: str = Depends(caller())) -> list[dict]:
        return svc.inbox()

    @app.post("/compliance/v1/register/decisions", dependencies=auth)
    def decisions(req: m.DecisionsRequest = Depends(body(m.DecisionsRequest)),
                  _: str = Depends(andre("register/decisions"))) -> dict:
        return svc.decide(req.request_id, [d.model_dump() for d in req.decisions])

    # --- controls -------------------------------------------------------------------------

    @app.get("/compliance/v1/controls", dependencies=auth)
    def controls(_: str = Depends(caller())) -> list[dict]:
        return svc.controls_view()

    @app.post("/compliance/v1/controls/internal/run", dependencies=auth)
    def internal_run(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.run_internal_controls(req.request_id)

    @app.get("/compliance/v1/controls/{control_id}", dependencies=auth)
    def control(control_id: str, _: str = Depends(caller())) -> dict:
        return svc.control_view(control_id[:16])

    @app.post("/compliance/v1/controls/{control_id}/results", dependencies=auth)
    def control_result(control_id: str, request: Request,
                       req: m.ControlResultRequest = Depends(body(m.ControlResultRequest))) -> dict:
        if not re.fullmatch(r"C-[0-9]{2,3}", control_id):
            raise Invalid("control id format")
        owner = svc.owner_caller(control_id)
        if owner == "andre":
            try:
                founder.verify(request.headers.get(FOUNDER_HEADER))
            except FounderRefused as exc:
                svc.founder_refused(f"controls/{control_id}/results", exc.reason)
                raise
            who = "andre"
        else:
            who = callers.identify(request.headers.get(CALLER_HEADER))
            if who is None:
                raise Forbidden("caller token missing or not recognised")
        try:
            from clock import parse_iso
            parse_iso(req.tested_at)
        except ValueError:
            raise Invalid("tested_at must be an RFC 3339 timestamp with offset") from None
        return svc.push_control_result(who, control_id, req.request_id, req.model_dump())

    @app.get("/compliance/v1/trust-center", dependencies=auth)
    def trust(_: str = Depends(caller())) -> list[dict]:
        return svc.trust_center()

    # --- holds ------------------------------------------------------------------------------

    @app.get("/compliance/v1/holds", dependencies=auth)
    def holds(_: str = Depends(caller())) -> list[dict]:
        return svc.list_holds()

    @app.post("/compliance/v1/holds/{hold_id}/release", dependencies=auth)
    def release(hold_id: str, req: m.HoldReleaseRequest = Depends(body(m.HoldReleaseRequest)),
                _: str = Depends(andre("holds/release"))) -> dict:
        if not re.fullmatch(r"hold-[a-z2-7]{20}", hold_id):
            raise Invalid("hold id format")
        return svc.release_hold(req.request_id, hold_id, req.reason)

    # --- reconcile (AEGIS N15-1) -------------------------------------------------------------------

    @app.get("/compliance/v1/reconcile", dependencies=auth)
    def reconcile_plan(_: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile_plan()

    @app.post("/compliance/v1/reconcile", dependencies=auth)
    def reconcile(req: m.ReconcileRequest = Depends(body(m.ReconcileRequest)),
                  _: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile(req.request_id, req.head_sha256, list(req.void_lines), list(req.void_event_ids))

    # --- watcher and audit -----------------------------------------------------------------------

    @app.post("/compliance/v1/watcher/run", dependencies=auth)
    def watcher(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.watcher_run(req.request_id)

    @app.get("/compliance/v1/audit/export", dependencies=auth)
    def audit(who: str = Depends(caller()), since: Optional[str] = Query(default=None, max_length=40),
              until: Optional[str] = Query(default=None, max_length=40),
              cursor: int = Query(default=0, ge=0, le=10**12)) -> dict:
        try:
            return svc.audit_export(who, since, until, cursor)
        except ValueError:
            raise Invalid("since/until must be RFC 3339 timestamps with offset") from None

    return app


def watcher_allowlist(seed_rows: list[dict]) -> frozenset[str]:
    hosts = {host_of(s.url) for s in seeded_sources()}
    for r in seed_rows:
        u = r.get("source_url")
        if r["source_kind"] == "platform-policy" and u and u.startswith("https://") and not is_excluded_host(host_of(u)):
            hosts.add(host_of(u))
    return frozenset(h for h in hosts if h)


def build_service(settings: config_mod.Settings, clock: Optional[Clock] = None, ports: Optional[Ports] = None,
                  ledger=None) -> ComplianceService:
    clock = clock or SystemClock()
    if ledger is None:
        ledger = (HttpLedgerClient(settings.ledger_url, settings.ledger_token)
                  if settings.ledger_url and settings.ledger_token else UnconfiguredLedgerClient())
    with open(settings.seed_path, "rb") as fh:
        seed_bytes = fh.read()
    ports = ports or Ports()
    if settings.watcher_enabled and ports.fetcher.__class__.__name__ == "NotWiredFetcher":
        rows = json.loads(seed_bytes)["rows"]
        ports.fetcher = HttpFeedFetcher(watcher_allowlist(rows), clock)
    cfg = Config(sanctions_freshness_days=settings.sanctions_freshness_days,
                 disclosure_max_offset_s=settings.disclosure_max_offset_s,
                 a11y_max_age_days=settings.a11y_max_age_days, watcher_enabled=settings.watcher_enabled,
                 site_owner_caller=settings.site_owner_caller,
                 watcher_max_proposals_per_cycle=settings.watcher_max_proposals_per_cycle,
                 watcher_max_proposals_per_source=settings.watcher_max_proposals_per_source)
    # AEGIS N14-13: the pinned hash, unless the operator explicitly opted into an unpinned (non-production) seed
    expected = settings.seed_sha256 if (settings.allow_unpinned_seed and settings.seed_sha256) else SPEC_SEED_SHA256
    return ComplianceService(cfg, Recorder(ledger), RecordLog(settings.data_dir), seed_bytes, expected, ports, clock,
                             reconcile_mode=settings.reconcile_mode)


def _app_from_env() -> FastAPI:
    settings = config_mod.load()
    return create_app(build_service(settings), settings)


app = _app_from_env()


def main() -> None:
    import serve

    host = os.environ.get("COMPLIANCE_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("COMPLIANCE_PORT", "8380"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
