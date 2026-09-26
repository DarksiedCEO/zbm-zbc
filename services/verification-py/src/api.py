"""
REST surface for Verification and Integrity (spec §D, ADR 0007).

Same discipline as services/compliance-py (the shared blocks below are copied from its api.py):
- fail-closed bearer auth on every route except /health; the service refuses to start without
  VI_SERVICE_TOKEN; ``hmac.compare_digest`` wrapped in ``try/except TypeError`` (a non-ASCII token is a
  401, never a 500);
- caller identity from ``X-VI-Caller-Token`` (VI_CALLER_TOKENS; SHA-256 digests compared against EVERY
  configured token, no early exit); a wrong or absent caller token on a route that needs one is a 403;
- Andre's routes need ``X-Andre-Approval-Token`` (VI_ANDRE_APPROVAL_TOKEN, the FounderGate); a refusal is a
  403 and is recorded as ``founder_approval_refused``; hold/finding decisions take Andre's token or a
  reviewer delegate token (``X-VI-Reviewer-Token``, VI_REVIEWER_TOKENS) that People 43 confirms (stand-in:
  nobody → Andre only); the ban route needs the clipper_network caller token AND Andre's token;
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless VI_BIND_ADDR says otherwise; hardened
  launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), per-route body caps inside the
  1 MiB service cap (413), JSON content type only (415), JSON nesting depth and member count bounded (422),
  body read deadline (408); callers never send counts (``models.forbidden_keys`` → 422). Error bodies are
  bounded and never echo request content.
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
from adapters.http_adapters import TikTokOEmbed
from clock import Clock, SystemClock
from compliance_client import HttpComplianceRegister
from errors import Forbidden, FounderRefused, Invalid, Unavailable, VIError
from founder import FounderGate
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from service import JOBS, Ports, VIService
from sidestore import PlatformDataStore
from store import RecordLog

log = logging.getLogger("verification.api")

CALLER_HEADER = "X-VI-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
REVIEWER_HEADER = "X-VI-Reviewer-Token"
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
    (re.compile(r"^/vi/v1/results/attest$"), 64 * 1024),          # ClipResult strings are up to 4,000 chars each
    (re.compile(r"^/vi/v1/rules/proposals$"), 64 * 1024),
    (re.compile(r"^/vi/v1/rules/decisions$"), 64 * 1024),
    (re.compile(r"^/vi/v1/submissions$"), 32 * 1024),
    (re.compile(r"^/vi/v1/reconcile$"), 1024 * 1024),              # up to 10,000 voided ids
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


SUB_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
VI_ID = re.compile(r"vi-[a-z]{2,5}-[0-9A-Z]{26}")


def _echo(request_id: str, resp: dict) -> dict:
    """Every write answer names the request it answers (AEGIS N16-6: a thin client checks the echo)."""
    return {**resp, "request_id": request_id}


def _id(value: str, rx=SUB_ID) -> str:
    if not rx.fullmatch(value or ""):
        raise Invalid("id format")
    return value


def create_app(service: VIService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Verification and Integrity",
                  description="NON-LIVE: vault, platform adapters, hasher, age provider, Compliance, Finance, Legal, "
                              "People and Clipper Network are fail-closed stand-ins.",
                  version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    reviewers = Callers(settings.reviewer_tokens)
    founder = FounderGate.build(settings.andre_token, settings.service_token,
                                list(settings.caller_tokens.values()) + list(settings.reviewer_tokens.values()))
    app.state.service = service
    svc = service

    def caller(*allowed: str) -> Callable:
        def dep(x_vi_caller_token: Optional[str] = Header(default=None)) -> str:
            name = callers.identify(x_vi_caller_token)
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

    def decider(route: str) -> Callable:
        """Andre, or a reviewer delegate confirmed by People 43 (stand-in: nobody)."""
        def dep(request: Request) -> str:
            a = request.headers.get(FOUNDER_HEADER)
            if a is not None:
                try:
                    founder.verify(a)
                except FounderRefused as exc:
                    svc.founder_refused(route, exc.reason)
                    raise
                return "andre"
            name = reviewers.identify(request.headers.get(REVIEWER_HEADER))
            if name is not None and svc.reviewer_allowed(name):
                return name
            svc.founder_refused(route, "no Andre token and no People-43-confirmed reviewer token")
            raise FounderRefused("Andre's approval token or a confirmed reviewer delegate token is required")
        return dep

    def body(model: type[BaseModel], allow: tuple = ()) -> Callable:
        def parse(payload: Any = Body(default=None)) -> Any:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = m.forbidden_keys(payload, allow=allow)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "callers never send counts, metrics, amounts, "
                                               "rates or guardian fields", "type": "forbidden_field"} for b in bad[:20]])
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

    @app.exception_handler(VIError)
    def _domain(_: Request, exc: VIError):
        content = {"detail": exc.reason, **exc.body}
        if isinstance(exc, Unavailable):
            content["issued"] = False
            return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content=content)
        return JSONResponse(status_code=exc.status_code, content=content)

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

    @app.get("/vi/v1/intelligences", dependencies=auth)
    def intelligences(_: str = Depends(caller())) -> list[dict]:
        return registry()

    @app.get("/vi/v1/integrity", dependencies=auth)
    def integrity(_: str = Depends(caller())) -> dict:
        return svc.integrity()

    # --- connections ------------------------------------------------------------------------

    @app.post("/vi/v1/connections/start", dependencies=auth)
    def conn_start(who: str = Depends(caller("clipper_network")),
                   req: m.ConnectionStart = Depends(body(m.ConnectionStart))) -> dict:
        return svc.connections_start(who, req.request_id, req.clipper_id, req.platform, req.redirect_uri)

    @app.post("/vi/v1/connections/complete", dependencies=auth)
    def conn_complete(who: str = Depends(caller("clipper_network")),
                      req: m.ConnectionComplete = Depends(body(m.ConnectionComplete))) -> dict:
        return svc.connections_complete(who, req.request_id, req.state, req.code)

    @app.post("/vi/v1/connections/{connection_id}/revoke", dependencies=auth)
    def conn_revoke(connection_id: str, who: str = Depends(caller("clipper_network")),
                    req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return svc.revoke_connection(who, req.request_id, _id(connection_id, VI_ID))

    @app.get("/vi/v1/connections", dependencies=auth)
    def conn_list(clipper_id: str = Query(max_length=128),
                  _: str = Depends(caller("clipper_network", "compliance_38"))) -> dict:
        return svc.list_connections(_id(clipper_id))

    # --- submissions and attestations ----------------------------------------------------------

    @app.post("/vi/v1/submissions", dependencies=auth, status_code=201)
    def submit(who: str = Depends(caller("creative_production")),
               req: m.SubmissionRequest = Depends(body(m.SubmissionRequest))) -> dict:
        return _echo(req.request_id, svc.register_submission(who, req.request_id, req.model_dump()))

    @app.post("/vi/v1/submissions/{submission_id}/approval", dependencies=auth)
    def approval(submission_id: str, who: str = Depends(caller("creative_production")),
                 req: m.ApprovalRequest = Depends(body(m.ApprovalRequest))) -> dict:
        return _echo(req.request_id, svc.approve_submission(who, req.request_id, _id(submission_id), req.review_ref))

    @app.post("/vi/v1/clips/attest", dependencies=auth)
    def clip_attest(who: str = Depends(caller("creative_production")),
                    req: m.CreativeClipAttest = Depends(body(m.CreativeClipAttest))) -> dict:
        return svc.attest_creative_clip(who, req.request_id, req.submission_id, req.facts.model_dump())

    @app.post("/vi/v1/clips/hr13", dependencies=auth)
    def clip_hr13(who: str = Depends(caller("compliance_38")), req: m.Hr13Attest = Depends(body(m.Hr13Attest))) -> dict:
        return svc.attest_hr13(who, req.request_id, req.model_dump())

    @app.post("/vi/v1/results/attest", dependencies=auth)
    def result_attest(who: str = Depends(caller("creative_production")),
                      req: m.ResultAttest = Depends(body(m.ResultAttest, allow=("facts.reported_views",)))) -> dict:
        return svc.attest_result(who, req.request_id, req.result_id, req.facts.model_dump())

    @app.get("/vi/v1/feed/verified-results", dependencies=auth)
    def feed_verified(cursor: int = Query(default=0, ge=0, le=10**12),
                      _: str = Depends(caller("creative_production"))) -> dict:
        return svc.feed_verified(cursor)

    readers = ("compliance_38", "creative_production", "finance_31", "clipper_network")

    @app.get("/vi/v1/certifications/{certification_id}", dependencies=auth)
    def certification(certification_id: str, _: str = Depends(caller(*readers))) -> dict:
        return svc.get_certification(_id(certification_id, VI_ID))

    @app.get("/vi/v1/submissions/{submission_id}/certification", dependencies=auth)
    def sub_certification(submission_id: str, _: str = Depends(caller(*readers))) -> dict:
        return svc.certification_for(_id(submission_id))

    @app.get("/vi/v1/certifications", dependencies=auth)
    def certifications_of(clipper_id: str = Query(max_length=128),
                          _: str = Depends(caller("clipper_network", "compliance_38", "finance_31"))) -> dict:
        return svc.certifications_of(_id(clipper_id))

    @app.get("/vi/v1/clawbacks", dependencies=auth)
    def clawbacks(cursor: int = Query(default=0, ge=0, le=10**12), _: str = Depends(caller("finance_31"))) -> dict:
        return svc.feed_clawbacks(cursor)

    # --- age and identity ---------------------------------------------------------------------------

    @app.post("/vi/v1/age/checks", dependencies=auth)
    def age_check(who: str = Depends(caller("clipper_network", "onboarding")), req: m.AgeCheck = Depends(body(m.AgeCheck))) -> dict:
        return svc.age_check(who, req.request_id, req.model_dump())

    @app.get("/vi/v1/age/attestations/{attestation_id}", dependencies=auth)
    def age_attestation(attestation_id: str, _: str = Depends(caller("compliance_38"))) -> dict:
        return svc.age_attestation(_id(attestation_id, VI_ID))

    @app.get("/vi/v1/age/subjects/{subject_id}", dependencies=auth)
    def age_subject(subject_id: str, who: str = Depends(caller("onboarding", "clipper_network"))) -> dict:
        return svc.age_subject(who, _id(subject_id))       # the caller's own namespace only (N16-3)

    @app.post("/vi/v1/identity/checks", dependencies=auth)
    def identity(who: str = Depends(caller("clipper_network")), req: m.IdentityCheck = Depends(body(m.IdentityCheck))) -> dict:
        return svc.identity_check(who, req.request_id, req.clipper_id, req.email)

    @app.get("/vi/v1/clippers/{clipper_id}/integrity", dependencies=auth)
    def clipper_integrity(clipper_id: str, _: str = Depends(caller("clipper_network", "compliance_38"))) -> dict:
        return svc.clipper_integrity(_id(clipper_id))

    @app.get("/vi/v1/strikes", dependencies=auth)
    def strikes(cursor: int = Query(default=0, ge=0, le=10**12), _: str = Depends(caller("clipper_network"))) -> dict:
        return svc.feed_strikes(cursor)

    # --- queues and human decisions -------------------------------------------------------------------

    @app.get("/vi/v1/holds", dependencies=auth)
    def holds(_: str = Depends(caller())) -> list[dict]:
        return svc.list_holds()

    @app.get("/vi/v1/findings", dependencies=auth)
    def findings(_: str = Depends(caller())) -> list[dict]:
        return svc.list_findings()

    @app.get("/vi/v1/findings/{finding_id}", dependencies=auth)
    def finding(finding_id: str, _: str = Depends(caller("clipper_network", "compliance_38"))) -> dict:
        return svc.finding_view(_id(finding_id, VI_ID))

    @app.post("/vi/v1/holds/{hold_id}/decision", dependencies=auth)
    def hold_decision(hold_id: str, who: str = Depends(decider("holds/decision")),
                      req: m.DecisionRequest = Depends(body(m.DecisionRequest))) -> dict:
        return _echo(req.request_id, svc.decide_hold(who, req.request_id, _id(hold_id, VI_ID), req.decision, req.reason))

    @app.post("/vi/v1/findings/{finding_id}/decision", dependencies=auth)
    def finding_decision(finding_id: str, who: str = Depends(decider("findings/decision")),
                         req: m.DecisionRequest = Depends(body(m.DecisionRequest))) -> dict:
        return _echo(req.request_id, svc.decide_finding_route(who, req.request_id, _id(finding_id, VI_ID), req.decision,
                                                              req.reason))

    @app.post("/vi/v1/bans", dependencies=auth)
    def bans(_c: str = Depends(caller("clipper_network")),
             _a: str = Depends(andre("bans")), req: m.BanRequest = Depends(body(m.BanRequest))) -> dict:
        return svc.ban(req.request_id, req.clipper_id, req.cn_decision_id, req.approved_at)

    # --- rules ----------------------------------------------------------------------------------------

    @app.get("/vi/v1/rules", dependencies=auth)
    def rules(_: str = Depends(caller())) -> dict:
        return svc.rules_view()

    @app.post("/vi/v1/rules/proposals", dependencies=auth, status_code=201)
    def propose(_: str = Depends(andre("rules/proposals")),
                req: m.RuleProposalRequest = Depends(body(m.RuleProposalRequest))) -> dict:
        return _echo(req.request_id, svc.create_rule_proposal(req.request_id, req.model_dump(exclude={"request_id"})))

    @app.post("/vi/v1/rules/decisions", dependencies=auth)
    def decide(_: str = Depends(andre("rules/decisions")), req: m.RuleDecisions = Depends(body(m.RuleDecisions))) -> dict:
        return _echo(req.request_id, svc.decide_rules(req.request_id, [d.model_dump() for d in req.decisions]))

    # --- jobs, reconcile, audit ---------------------------------------------------------------------------

    @app.post("/vi/v1/jobs/{job}/run", dependencies=auth)
    def job(job: str, who: str = Depends(caller("scheduler")), req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        if job not in JOBS:
            raise Invalid("unknown job")
        return _echo(req.request_id, svc.run_job(who, req.request_id, job))

    @app.get("/vi/v1/reconcile", dependencies=auth)
    def reconcile_plan(_: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile_plan()

    @app.post("/vi/v1/reconcile", dependencies=auth)
    def reconcile(_: str = Depends(andre("reconcile")), req: m.ReconcileRequest = Depends(body(m.ReconcileRequest))) -> dict:
        return _echo(req.request_id, svc.reconcile(req.request_id, req.head_sha256, list(req.void_lines),
                                                   list(req.void_event_ids)))

    @app.get("/vi/v1/audit/export", dependencies=auth)
    def audit(who: str = Depends(caller()), since: Optional[str] = Query(default=None, max_length=40),
              until: Optional[str] = Query(default=None, max_length=40),
              cursor: int = Query(default=0, ge=0, le=10**12)) -> dict:
        try:
            return svc.audit_export(who, since, until, cursor)
        except ValueError:
            raise Invalid("since/until must be RFC 3339 timestamps with offset") from None

    return app


def build_ports(settings: config_mod.Settings) -> Ports:
    """Production wiring: every port is its fail-closed stand-in except the Compliance thin client (when all
    three VI_COMPLIANCE_* are set) and the TikTok oEmbed client (VI_OEMBED_ENABLED=1)."""
    ports = Ports()
    if settings.compliance_url:
        ports.compliance = HttpComplianceRegister(settings.compliance_url, settings.compliance_token,
                                                  settings.compliance_caller_token)
    if settings.oembed_enabled:
        ports.oembed = TikTokOEmbed()
    return ports


def build_service(settings: config_mod.Settings, clock: Optional[Clock] = None, ports: Optional[Ports] = None,
                  ledger=None) -> VIService:
    clock = clock or SystemClock()
    if ledger is None:
        ledger = (HttpLedgerClient(settings.ledger_url, settings.ledger_token)
                  if settings.ledger_url and settings.ledger_token else UnconfiguredLedgerClient())
    with open(settings.seed_path, "rb") as fh:
        seed_bytes = fh.read()
    expected = settings.seed_sha256 if (settings.allow_unpinned_seed and settings.seed_sha256) else \
        config_mod.PINNED_SEED_SHA256
    return VIService(settings, Recorder(ledger), RecordLog(settings.data_dir), PlatformDataStore(settings.data_dir),
                     seed_bytes, expected, ports or build_ports(settings), clock, config_mod.PINNED_SEED_SHA256)


def _app_from_env() -> FastAPI:
    settings = config_mod.load()
    return create_app(build_service(settings), settings)


app = _app_from_env()


def main() -> None:
    import serve

    host = os.environ.get("VI_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("VI_PORT", "8390"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
