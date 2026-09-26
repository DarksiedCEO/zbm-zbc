"""
REST surface for Clipper Network (spec §E, ADR 0008).

Same discipline as services/compliance-py (whose transport layer is copied
here unchanged: ``InputLimits``, ``make_require_auth``, ``Callers``,
``_sanitize``):
- fail-closed bearer auth on every route except /health; the service refuses
  to start without CN_SERVICE_TOKEN; ``hmac.compare_digest`` in
  ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-CN-Caller-Token`` (CN_CALLER_TOKENS: hub,
  onboarding, creative_production, verification_integrity, finance_31,
  compliance_38, scheduler), digests compared against EVERY token with no
  early exit; a wrong or absent caller on a route that needs one is a 403;
- Andre's routes need ``X-Andre-Approval-Token`` (FounderGate: a token equal
  to the service, a caller or a delegate token counts as not configured); a
  refusal is a 403 recorded as ``founder_approval_refused``;
- delegates (``X-CN-Delegate-Token``, CN_DELEGATE_TOKENS) count only on the
  dispute-outcome route and only when People (43) confirms the delegate — the
  stand-in never does, so it is Andre only;
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless CN_BIND_ADDR
  says otherwise; hardened launcher (serve.py);
- request limits before any route (per-route body caps inside 1 MiB, JSON
  only, bounded depth/members, target/head caps, body deadline). Error
  bodies are bounded and never echo request content.
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
from contacts import ContactStore
from errors import CNError, Forbidden, FounderRefused, Invalid, Unavailable
from founder import FounderGate
from httpclients import clients_from_settings
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from service import CNService, Config
from store import RecordLog

log = logging.getLogger("clipper_network.api")

CALLER_HEADER = "X-CN-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
DELEGATE_HEADER = "X-CN-Delegate-Token"
MAX_BODY_BYTES = 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

# Per-route body caps (all inside MAX_BODY_BYTES): the largest legal body of each route plus headroom.
ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/cn/v1/recruiting/campaigns$"), 512 * 1024),           # up to 1,000 recipients
    (re.compile(r"^/cn/v1/(rules|templates)/proposals$"), 64 * 1024),
    (re.compile(r"^/cn/v1/rules/decisions$"), 32 * 1024),
    (re.compile(r"^/cn/v1/disputes$"), 48 * 1024),                        # 4,000-char statement, escaped
    (re.compile(r"^/cn/v1/reconcile$"), 1024 * 1024),                     # up to 10,000 voided ids
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


def create_app(service: CNService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Clipper Network",
                  description="NON-LIVE: V&I, Compliance, Creative, Finance, Legal, People, messaging, hub and push are "
                              "fail-closed stand-ins unless a thin client is fully configured.",
                  version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    delegates = Callers(settings.delegate_tokens)
    founder = FounderGate.build(settings.andre_token, settings.service_token,
                                list(settings.caller_tokens.values()) + list(settings.delegate_tokens.values()))
    app.state.service = service
    svc = service

    def caller(*allowed: str) -> Callable:
        def dep(x_cn_caller_token: Optional[str] = Header(default=None)) -> str:
            name = callers.identify(x_cn_caller_token)
            if name is None:
                raise Forbidden("caller token missing or not recognised")
            if allowed and name not in allowed:
                raise Forbidden("this caller is not authorized for this route")
            return name
        return dep

    def andre_check(route: str, supplied: Optional[str]) -> str:
        try:
            founder.verify(supplied)
        except FounderRefused as exc:
            svc.founder_refused(route, exc.reason)
            raise
        return "andre"

    def andre(route: str) -> Callable:
        def dep(x_andre_approval_token: Optional[str] = Header(default=None)) -> str:
            return andre_check(route, x_andre_approval_token)
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

    def cid(v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", v):
            raise Invalid("id format")
        return v

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(CNError)
    def _domain(_: Request, exc: CNError):
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

    @app.get("/cn/v1/integrity", dependencies=auth)
    def integrity(_: str = Depends(caller())) -> dict:
        return svc.integrity()

    # --- recruiting ----------------------------------------------------------------------------------------

    @app.post("/cn/v1/opt-ins", dependencies=auth, status_code=201)
    def opt_in(req: m.OptInRequest = Depends(body(m.OptInRequest)), _: str = Depends(caller("hub"))) -> dict:
        return svc.opt_in(req.request_id, req.model_dump())

    @app.post("/cn/v1/opt-outs", dependencies=auth)
    def opt_out(req: m.OptOutRequest = Depends(body(m.OptOutRequest)), _: str = Depends(caller("hub"))) -> dict:
        return svc.opt_out(req.request_id, req.model_dump())

    @app.post("/cn/v1/recruiting/campaigns", dependencies=auth, status_code=201)
    def recruiting(req: m.RecruitingCampaignRequest = Depends(body(m.RecruitingCampaignRequest)),
                   _: str = Depends(andre("recruiting/campaigns"))) -> dict:
        return svc.create_recruiting(req.request_id, req.model_dump())

    @app.post("/cn/v1/recruiting/campaigns/{recruit_id}/send", dependencies=auth)
    def recruiting_send(recruit_id: str, req: m.RunRequest = Depends(body(m.RunRequest)),
                        _: str = Depends(caller("scheduler"))) -> dict:
        return svc.send_recruiting(req.request_id, cid(recruit_id))

    # --- applications and the relays ------------------------------------------------------------------------

    @app.post("/cn/v1/applications", dependencies=auth, status_code=201)
    def application(req: m.ApplicationRequest = Depends(body(m.ApplicationRequest)),
                    who: str = Depends(caller("hub", "onboarding"))) -> dict:
        return svc.apply(who, req.request_id, req.model_dump())

    @app.get("/cn/v1/applications/{application_id}", dependencies=auth)
    def get_application(application_id: str, _: str = Depends(caller("hub", "onboarding"))) -> dict:
        return svc.get_application(cid(application_id))

    @app.post("/cn/v1/clippers/{clipper_id}/connections/start", dependencies=auth)
    def conn_start(clipper_id: str, req: m.ConnectionStartRequest = Depends(body(m.ConnectionStartRequest)),
                   _: str = Depends(caller("hub"))) -> dict:
        return svc.connection_start(req.request_id, cid(clipper_id), req.model_dump())

    @app.post("/cn/v1/clippers/{clipper_id}/connections/complete", dependencies=auth)
    def conn_complete(clipper_id: str, req: m.ConnectionCompleteRequest = Depends(body(m.ConnectionCompleteRequest)),
                      _: str = Depends(caller("hub"))) -> dict:
        return svc.connection_complete(req.request_id, cid(clipper_id), req.model_dump())

    @app.post("/cn/v1/clippers/{clipper_id}/age-check", dependencies=auth)
    def age_check(clipper_id: str, req: m.AgeCheckRequest = Depends(body(m.AgeCheckRequest)),
                  _: str = Depends(caller("hub"))) -> dict:
        return svc.age_check(req.request_id, cid(clipper_id), req.model_dump())

    @app.post("/cn/v1/clippers/{clipper_id}/agreement-acceptances", dependencies=auth)
    def agreement(clipper_id: str, req: m.AgreementAcceptanceRequest = Depends(body(m.AgreementAcceptanceRequest)),
                  _: str = Depends(caller("hub"))) -> dict:
        return svc.accept_agreement(req.request_id, cid(clipper_id), req.model_dump())

    @app.post("/cn/v1/clippers/{clipper_id}/disclosure-training", dependencies=auth)
    def training(clipper_id: str, req: m.TrainingRequest = Depends(body(m.TrainingRequest)),
                 _: str = Depends(caller("hub"))) -> dict:
        return svc.attest_training(req.request_id, cid(clipper_id), req.model_dump())

    @app.post("/cn/v1/clippers/{clipper_id}/admission", dependencies=auth)
    def admission(clipper_id: str, req: m.RunRequest = Depends(body(m.RunRequest)),
                  who: str = Depends(caller("hub", "onboarding", "scheduler"))) -> dict:
        return svc.admission(who, req.request_id, cid(clipper_id))

    @app.get("/cn/v1/clippers/{clipper_id}", dependencies=auth)
    def clipper(clipper_id: str, who: str = Depends(caller())) -> dict:
        return svc.clipper_view(cid(clipper_id), with_contact=(who == "hub"))

    @app.get("/cn/v1/clippers/{clipper_id}/messages", dependencies=auth)
    def messages(clipper_id: str, _: str = Depends(caller("hub"))) -> list[dict]:
        return svc.messages_for(cid(clipper_id))

    @app.get("/cn/v1/clippers/{clipper_id}/export", dependencies=auth)
    def export(clipper_id: str, _: str = Depends(caller("hub"))) -> dict:
        return svc.export_for(cid(clipper_id))

    @app.post("/cn/v1/clippers/{clipper_id}/tier-nomination", dependencies=auth)
    def nominate(clipper_id: str, req: m.TierNominationRequest = Depends(body(m.TierNominationRequest)),
                 _: str = Depends(andre("tier-nomination"))) -> dict:
        return svc.nominate(req.request_id, cid(clipper_id), req.nominate)

    # --- campaigns ----------------------------------------------------------------------------------------------

    @app.put("/cn/v1/campaigns/{campaign_id}/network-config", dependencies=auth)
    def network_config(campaign_id: str, req: m.NetworkConfigRequest = Depends(body(m.NetworkConfigRequest)),
                       _: str = Depends(andre("network-config"))) -> dict:
        return svc.put_network_config(req.request_id, cid(campaign_id), req.model_dump(exclude={"request_id"}))

    @app.post("/cn/v1/campaigns/{campaign_id}/rulebook-announcements", dependencies=auth)
    def announce(campaign_id: str, req: m.AnnouncementRequest = Depends(body(m.AnnouncementRequest)),
                 _: str = Depends(caller("creative_production"))) -> dict:
        return svc.announce(req.request_id, cid(campaign_id), req.version, req.facts)

    @app.post("/cn/v1/campaigns/{campaign_id}/enrolments", dependencies=auth)
    def enrol(campaign_id: str, req: m.EnrolmentRequest = Depends(body(m.EnrolmentRequest)),
              _: str = Depends(caller("hub"))) -> dict:
        return svc.enrol(req.request_id, cid(campaign_id), req.clipper_id)

    @app.get("/cn/v1/campaigns/{campaign_id}/enrolments", dependencies=auth)
    def enrolments(campaign_id: str, _: str = Depends(caller("hub"))) -> list[dict]:
        return svc.list_enrolments(cid(campaign_id))

    @app.post("/cn/v1/enrolments/{enrolment_id}/kit-acknowledgment", dependencies=auth)
    def kit_ack(enrolment_id: str, req: m.KitAckRequest = Depends(body(m.KitAckRequest)),
                _: str = Depends(caller("hub"))) -> dict:
        return svc.kit_ack(req.request_id, cid(enrolment_id), req.model_dump())

    # --- rules and templates (Andre) ------------------------------------------------------------------------------

    @app.get("/cn/v1/rules", dependencies=auth)
    def rules(_: str = Depends(caller())) -> dict:
        return svc.rules_view()

    @app.get("/cn/v1/inbox", dependencies=auth)
    def inbox(_: str = Depends(caller())) -> list[dict]:
        return svc.inbox()

    @app.post("/cn/v1/rules/proposals", dependencies=auth, status_code=201)
    def rule_proposal(req: m.RuleProposalRequest = Depends(body(m.RuleProposalRequest)),
                      _: str = Depends(andre("rules/proposals"))) -> dict:
        return svc.create_proposal(req.request_id, "rules", req.model_dump(exclude={"request_id"}))

    @app.post("/cn/v1/templates/proposals", dependencies=auth, status_code=201)
    def template_proposal(req: m.TemplateProposalRequest = Depends(body(m.TemplateProposalRequest)),
                          _: str = Depends(andre("templates/proposals"))) -> dict:
        return svc.create_proposal(req.request_id, "templates", req.model_dump(exclude={"request_id"}))

    @app.post("/cn/v1/rules/decisions", dependencies=auth)
    def decisions(req: m.DecisionsRequest = Depends(body(m.DecisionsRequest)),
                  _: str = Depends(andre("rules/decisions"))) -> dict:
        return svc.decide(req.request_id, [d.model_dump() for d in req.decisions])

    # --- disputes ------------------------------------------------------------------------------------------------

    @app.post("/cn/v1/disputes", dependencies=auth, status_code=201)
    def dispute(req: m.DisputeRequest = Depends(body(m.DisputeRequest)), _: str = Depends(caller("hub"))) -> dict:
        return svc.file_dispute(req.request_id, req.model_dump())

    @app.get("/cn/v1/disputes/{dispute_id}", dependencies=auth)
    def get_dispute(dispute_id: str, _: str = Depends(caller())) -> dict:
        return svc.get_dispute(cid(dispute_id))

    @app.post("/cn/v1/disputes/{dispute_id}/outcome", dependencies=auth)
    def dispute_outcome(dispute_id: str, request: Request,
                        req: m.DisputeOutcomeRequest = Depends(body(m.DisputeOutcomeRequest))) -> dict:
        andre_hdr = request.headers.get(FOUNDER_HEADER)
        if andre_hdr is not None:
            who = andre_check("disputes/outcome", andre_hdr)
        else:
            name = delegates.identify(request.headers.get(DELEGATE_HEADER))
            if name is None:
                svc.founder_refused("disputes/outcome", "no Andre token and no recognised delegate token")
                raise FounderRefused("dispute outcomes are decided by Andre or a delegate People (43) confirms")
            svc.delegate_confirmed(name)      # raises FounderRefused while People (43) is a stand-in
            who = f"delegate_{name}"
        return svc.dispute_outcome(req.request_id, cid(dispute_id), who, req.outcome, req.note)

    @app.post("/cn/v1/disputes/sla-run", dependencies=auth)
    def sla_run(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.disputes_sla_run(req.request_id)

    # --- discipline, bans, tiers, messages ---------------------------------------------------------------------------

    @app.post("/cn/v1/discipline/sync", dependencies=auth)
    def discipline(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.discipline_sync(req.request_id)

    @app.post("/cn/v1/clippers/{clipper_id}/ban-decision", dependencies=auth)
    def ban_decision(clipper_id: str, request: Request, req: m.BanDecisionRequest = Depends(body(m.BanDecisionRequest)),
                     _: str = Depends(andre("ban-decision"))) -> dict:
        # N16-2: the exact token Andre sent (verified by the dependency above) is passed through to V&I, never kept
        return svc.ban_decision(req.request_id, cid(clipper_id), req.proposal_id, req.decision, req.note,
                                request.headers.get(FOUNDER_HEADER))

    @app.post("/cn/v1/tiers/run", dependencies=auth)
    def tiers(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.tiers_run(req.request_id)

    @app.post("/cn/v1/messages/flush", dependencies=auth)
    def flush(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.flush_messages(req.request_id)

    # --- offboarding ------------------------------------------------------------------------------------------------

    @app.post("/cn/v1/clippers/{clipper_id}/offboarding", dependencies=auth)
    def offboarding(clipper_id: str, request: Request, req: m.OffboardingRequest = Depends(body(m.OffboardingRequest))) -> dict:
        if req.trigger == "andre_decision":
            who = andre_check("offboarding", request.headers.get(FOUNDER_HEADER))
        else:
            if callers.identify(request.headers.get(CALLER_HEADER)) != "hub":
                raise Forbidden("a clipper_request exit comes through the hub")
            who = "hub"
        return svc.offboard(req.request_id, who, cid(clipper_id), req.trigger, req.keep_connections_until_settlement)

    @app.get("/cn/v1/clippers/{clipper_id}/offboarding", dependencies=auth)
    def get_offboarding(clipper_id: str, request: Request) -> dict:
        if request.headers.get(FOUNDER_HEADER) is not None:
            andre_check("offboarding/read", request.headers.get(FOUNDER_HEADER))
        elif callers.identify(request.headers.get(CALLER_HEADER)) != "hub":
            raise Forbidden("hub or Andre only")
        return svc.offboarding_view(cid(clipper_id))

    @app.post("/cn/v1/offboarding/run", dependencies=auth)
    def offboarding_run(req: m.RunRequest = Depends(body(m.RunRequest)), _: str = Depends(caller("scheduler"))) -> dict:
        return svc.offboarding_run(req.request_id)

    # --- reconcile and audit ------------------------------------------------------------------------------------------

    @app.get("/cn/v1/reconcile", dependencies=auth)
    def reconcile_plan(_: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile_plan()

    @app.post("/cn/v1/reconcile", dependencies=auth)
    def reconcile(req: m.ReconcileRequest = Depends(body(m.ReconcileRequest)), _: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile(req.request_id, req.head_sha256, list(req.void_lines), list(req.void_event_ids))

    @app.get("/cn/v1/audit/export", dependencies=auth)
    def audit(who: str = Depends(caller()), since: Optional[str] = Query(default=None, max_length=40),
              until: Optional[str] = Query(default=None, max_length=40),
              cursor: int = Query(default=0, ge=0, le=10**12)) -> dict:
        try:
            return svc.audit_export(who, since, until, cursor)
        except ValueError:
            raise Invalid("since/until must be RFC 3339 timestamps with offset") from None

    return app


def build_service(settings: config_mod.Settings, clock: Optional[Clock] = None, ports: Optional[Ports] = None,
                  ledger=None) -> CNService:
    clock = clock or SystemClock()
    if ledger is None:
        ledger = (HttpLedgerClient(settings.ledger_url, settings.ledger_token)
                  if settings.ledger_url and settings.ledger_token else UnconfiguredLedgerClient())
    with open(settings.seed_path, "rb") as fh:
        seed_bytes = fh.read()
    seed = json.loads(seed_bytes)
    config_mod.check_rule_env(settings.rule_env, {r["rule_id"]: r for r in seed["rules"]})
    if ports is None:
        ports = Ports()
        clients_from_settings(settings, ports)
    cfg = Config(channels=settings.channels, postal_address=settings.postal_address, opt_out_url=settings.opt_out_url)
    expected = settings.seed_sha256 if (settings.allow_unpinned_seed and settings.seed_sha256) else config_mod.PINNED_SEED_SHA256
    return CNService(cfg, Recorder(ledger), RecordLog(settings.data_dir), ContactStore(settings.data_dir), seed_bytes,
                     expected, config_mod.PINNED_SEED_SHA256, settings.identity_hmac_key, ports, clock,
                     reconcile_mode=settings.reconcile_mode)


def _app_from_env() -> FastAPI:
    settings = config_mod.load()
    return create_app(build_service(settings), settings)


app = _app_from_env()


def main() -> None:
    import serve

    host = os.environ.get("CN_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("CN_PORT", "8400"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
