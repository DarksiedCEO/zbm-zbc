"""
REST surface for Legal (37) (Legal spec §E, ADR 0010).

Same discipline as services/verification-py and services/compliance-py (the shared blocks below are copied
from verification-py's api.py):
- fail-closed bearer auth on every route except /health; the service refuses to start without
  LEGAL_SERVICE_TOKEN; ``hmac.compare_digest`` wrapped in ``try/except TypeError`` (a non-ASCII token is a
  401, never a 500);
- caller identity from ``X-LEGAL-Caller-Token`` (LEGAL_CALLER_TOKENS; SHA-256 digests compared against EVERY
  configured token, no early exit); a wrong or absent caller token on a route that needs one is a 403;
- Andre's routes need ``X-Andre-Approval-Token`` (LEGAL_ANDRE_APPROVAL_TOKEN, the FounderGate); a refusal is a
  403 and is recorded as ``founder_approval_refused``; a service bearer or a caller token is never Andre;
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless LEGAL_BIND_ADDR says otherwise; port 8420;
  hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), per-route body caps (blob routes
  7 MiB so a 5 MiB blob fits as base64; everything else 256 KiB; 413), JSON content type only (415), JSON
  nesting depth and member count bounded (422), body read deadline (408). A body carrying an IP, user-agent,
  device, DOB, government-id or payment key anywhere is refused 422 before it is parsed (G6). Error bodies are
  bounded and never echo request content.
Legal returns no free-text answers: every response is records, dates, statuses, ids, hashes and reason codes.
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
from clock import Clock, SystemClock
from compliance_client import HttpCompliance
from errors import Forbidden, FounderRefused, Invalid, LegalError, NotFound, Unavailable
from founder import FounderGate
from intelligences import registry
from intelligences.i03_acceptance import forbidden_keys
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from service import JOBS, LegalService, Seeds
from store import BlobStore, RecordLog
from textguard import ip_fields, ip_in

log = logging.getLogger("legal.api")

CALLER_HEADER = "X-LEGAL-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
MAX_BODY_BYTES = 7 * 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

BLOB = 7 * 1024 * 1024
ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/legal/v1/documents/[^/]+/versions$"), BLOB),
    (re.compile(r"^/legal/v1/documents/[^/]+/versions/[^/]+/counsel-signoff$"), BLOB),
    (re.compile(r"^/legal/v1/memos$"), BLOB),
    (re.compile(r"^/legal/v1/esign/events$"), BLOB),
    (re.compile(r"^/legal/v1/playbooks/[^/]+/reviews$"), BLOB),
    (re.compile(r"^/legal/v1/playbooks/proposals$"), BLOB),
    (re.compile(r"^/legal/v1/reconcile$"), 1024 * 1024),
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


DOC_ID = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}$")
LG_ID = re.compile(r"^lg-[a-z]{3}-[0-9A-Z]{26}$")
SUB_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CQ_OR_TARGET = re.compile(r"^((CQ|VI-CQ|CN-CQ|FIN-CQ)-[0-9]{2}|retention:[a-z_]{1,40}|signoff:[a-z_]{1,40})$")
SHA = re.compile(r"^[0-9a-f]{64}$")


def _id(value: str, rx=SUB_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value) or ip_in(value):
        raise Invalid("id format (never an IP address)")
    return value


def _echo(request_id: str, resp: dict) -> dict:
    """Every write answer names the request it answers (a thin client checks the echo)."""
    return {**resp, "request_id": request_id}


def create_app(service: LegalService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Legal (37)",
                  description="NON-LIVE: counsel channel, e-sign provider, Cybersecurity 22, People 43 and every "
                              "department port are fail-closed stand-ins. Nothing here is legal advice.",
                  version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    founder = FounderGate.build(settings.andre_token, settings.service_token, list(settings.caller_tokens.values()))
    app.state.service = service
    svc = service

    def _andre(route: str, supplied: Optional[str]) -> str:
        try:
            founder.verify(supplied)
        except FounderRefused as exc:
            svc.founder_refused(route, exc.reason)
            raise
        return "andre"

    def caller(*allowed: str, andre_ok: bool = False, route: str = "") -> Callable:
        """A recognised caller token (in ``allowed`` when given); with ``andre_ok`` Andre's token also passes."""
        def dep(request: Request) -> str:
            if andre_ok and request.headers.get(FOUNDER_HEADER) is not None:
                return _andre(route or request.url.path[:64], request.headers.get(FOUNDER_HEADER))
            name = callers.identify(request.headers.get(CALLER_HEADER))
            if name is None:
                raise Forbidden("caller token missing or not recognised")
            if allowed and name not in allowed:
                raise Forbidden("this caller is not authorized for this route")
            return name
        return dep

    def andre(route: str) -> Callable:
        def dep(x_andre_approval_token: Optional[str] = Header(default=None)) -> str:
            return _andre(route, x_andre_approval_token)
        return dep

    reader = caller(andre_ok=True, route="read")

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> Any:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = forbidden_keys(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "Legal stores no IP address, device, "
                                               "user-agent, DOB, government-id or payment data",
                                               "type": "forbidden_field"} for b in bad[:20]])
            ips = ip_fields(payload)             # AEGIS N17-6: an IP address in ANY string of ANY write route
            if ips:
                raise RequestValidationError([{"loc": (p,), "msg": "Legal stores no IP address (IPv4, IPv6, embedded "
                                               "in a ref, with a port or URL-encoded)", "type": "IP_ADDRESS_REFUSED"}
                                              for p in ips])
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

    @app.exception_handler(LegalError)
    def _domain(_: Request, exc: LegalError):
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

    @app.get("/legal/v1/intelligences", dependencies=auth)
    def intelligences(_: str = Depends(reader)) -> list[dict]:
        return registry()

    @app.get("/legal/v1/integrity", dependencies=auth)
    def integrity(_: str = Depends(reader)) -> dict:
        return svc.integrity()

    # --- documents -----------------------------------------------------------------------------------------------

    @app.get("/legal/v1/documents/{doc_id}/current", dependencies=auth)
    def doc_current(doc_id: str, _: str = Depends(reader)) -> dict:
        return svc.current_answer(_id(doc_id, DOC_ID))

    @app.get("/legal/v1/documents/{doc_id}", dependencies=auth)
    def doc_view(doc_id: str, _: str = Depends(reader)) -> dict:
        return svc.document_view(_id(doc_id, DOC_ID))

    @app.get("/legal/v1/documents/{doc_id}/versions/{version}", dependencies=auth)
    def doc_version(doc_id: str, version: str, _: str = Depends(reader)) -> dict:
        return svc.version_get(_id(doc_id, DOC_ID), _id(version, VERSION))

    @app.get("/legal/v1/documents/{doc_id}/versions/{version}/text", dependencies=auth)
    def doc_text(doc_id: str, version: str, _: str = Depends(andre("documents/text"))) -> dict:
        return svc.version_text(_id(doc_id, DOC_ID), _id(version, VERSION))

    @app.post("/legal/v1/documents/{doc_id}/versions", dependencies=auth, status_code=201)
    def doc_create(doc_id: str, who: str = Depends(caller("scheduler", andre_ok=True, route="documents/versions")),
                   req: m.DocVersionCreate = Depends(body(m.DocVersionCreate))) -> dict:
        return _echo(req.request_id, svc.create_version(who, req.request_id, _id(doc_id, DOC_ID), req.model_dump()))

    @app.post("/legal/v1/documents/{doc_id}/versions/{version}/counsel-review", dependencies=auth)
    def doc_counsel_review(doc_id: str, version: str, _: str = Depends(andre("documents/counsel-review")),
                           req: m.CounselReview = Depends(body(m.CounselReview))) -> dict:
        return _echo(req.request_id, svc.counsel_review(req.request_id, _id(doc_id, DOC_ID), _id(version, VERSION),
                                                        req.model_dump()))

    @app.post("/legal/v1/documents/{doc_id}/versions/{version}/counsel-signoff", dependencies=auth)
    def doc_signoff(doc_id: str, version: str, _: str = Depends(andre("documents/counsel-signoff")),
                    req: m.CounselSignoff = Depends(body(m.CounselSignoff))) -> dict:
        return _echo(req.request_id, svc.counsel_signoff(req.request_id, _id(doc_id, DOC_ID), _id(version, VERSION),
                                                         req.model_dump()))

    @app.post("/legal/v1/documents/{doc_id}/versions/{version}/decision", dependencies=auth)
    def doc_decision(doc_id: str, version: str, _: str = Depends(andre("documents/decision")),
                     req: m.DocDecision = Depends(body(m.DocDecision))) -> dict:
        return _echo(req.request_id, svc.decide_version(req.request_id, _id(doc_id, DOC_ID), _id(version, VERSION),
                                                        req.model_dump()))

    # --- acceptances and envelopes -------------------------------------------------------------------------------

    @app.post("/legal/v1/acceptances", dependencies=auth, status_code=201)
    def acceptance(who: str = Depends(caller("hub", "clipper_network", "onboarding")),
                   req: m.AcceptanceCreate = Depends(body(m.AcceptanceCreate))) -> dict:
        return _echo(req.request_id, svc.record_acceptance(who, req.request_id, req.model_dump()))

    @app.get("/legal/v1/acceptances/{acceptance_id}", dependencies=auth)
    def acceptance_get(acceptance_id: str, _: str = Depends(caller("clipper_network", "finance_31", "onboarding",
                                                                   andre_ok=True))) -> dict:
        return svc.get_acceptance(_id(acceptance_id, LG_ID))

    @app.post("/legal/v1/envelopes", dependencies=auth)
    def envelope(_: str = Depends(andre("envelopes")), req: m.EnvelopeCreate = Depends(body(m.EnvelopeCreate))) -> dict:
        return _echo(req.request_id, svc.create_envelope(req.request_id, req.model_dump()))

    @app.post("/legal/v1/esign/events", dependencies=auth)
    def esign_event(_: str = Depends(caller("esign_gateway")), req: m.ESignEvent = Depends(body(m.ESignEvent))) -> dict:
        return _echo(req.request_id, svc.esign_event(req.request_id, req.model_dump()))

    # --- playbooks -----------------------------------------------------------------------------------------------

    @app.post("/legal/v1/playbooks/proposals", dependencies=auth, status_code=201)
    def pb_propose(_: str = Depends(andre("playbooks/proposals")),
                   req: m.PlaybookProposal = Depends(body(m.PlaybookProposal))) -> dict:
        return _echo(req.request_id, svc.create_playbook_proposal(req.request_id, req.model_dump()))

    @app.post("/legal/v1/playbooks/decisions", dependencies=auth)
    def pb_decide(_: str = Depends(andre("playbooks/decisions")),
                  req: m.PlaybookDecision = Depends(body(m.PlaybookDecision))) -> dict:
        return _echo(req.request_id, svc.decide_playbook(req.request_id, req.model_dump()))

    @app.get("/legal/v1/playbooks/{doc_type}", dependencies=auth)
    def pb_view(doc_type: str, _: str = Depends(reader)) -> dict:
        return svc.playbook_view(_id(doc_type, DOC_ID))

    @app.post("/legal/v1/playbooks/{doc_type}/reviews", dependencies=auth)
    def pb_review(doc_type: str, who: str = Depends(caller("onboarding", "scheduler", andre_ok=True, route="reviews")),
                  req: m.PlaybookReview = Depends(body(m.PlaybookReview))) -> dict:
        return _echo(req.request_id, svc.review(who, req.request_id, _id(doc_type, DOC_ID), req.model_dump()))

    # --- obligations and contract storage --------------------------------------------------------------------------

    @app.get("/legal/v1/obligations", dependencies=auth)
    def obligations(_: str = Depends(reader), party_ref: Optional[str] = Query(default=None, max_length=120),
                    owner_department: Optional[str] = Query(default=None, pattern=r"^[a-z0-9_]{1,40}$"),
                    status_: Optional[str] = Query(default=None, alias="status",
                                                   pattern=r"^(open|due_soon|done|missed|waived)$")) -> dict:
        return svc.list_obligations(party_ref, owner_department, status_)

    @app.post("/legal/v1/obligations", dependencies=auth, status_code=201)
    def obligation_entry(_: str = Depends(andre("obligations")),
                         req: m.ObligationEntry = Depends(body(m.ObligationEntry))) -> dict:
        return _echo(req.request_id, svc.obligation_entry(req.request_id, req.model_dump()))

    @app.post("/legal/v1/obligations/{obligation_id}/done", dependencies=auth)
    def obligation_done(obligation_id: str, who: str = Depends(caller("onboarding", "finance_31", "creative_production",
                                                                      "clipper_network", "compliance_38", andre_ok=True,
                                                                      route="obligations/done")),
                        req: m.ObligationDone = Depends(body(m.ObligationDone))) -> dict:
        return _echo(req.request_id, svc.obligation_done(who, req.request_id, _id(obligation_id, LG_ID),
                                                         req.model_dump()))

    @app.post("/legal/v1/obligations/{obligation_id}/waive", dependencies=auth)
    def obligation_waive(obligation_id: str, _: str = Depends(andre("obligations/waive")),
                         req: m.ObligationWaive = Depends(body(m.ObligationWaive))) -> dict:
        return _echo(req.request_id, svc.obligation_waive(req.request_id, _id(obligation_id, LG_ID), req.model_dump()))

    @app.get("/legal/v1/contracts/{client_id}/terms", dependencies=auth)
    def contract_get(client_id: str, _: str = Depends(caller("onboarding"))) -> dict:
        terms = svc.contract_get(_id(client_id))
        if terms is None:
            raise NotFound("no stored contract terms for this client")
        return terms

    @app.put("/legal/v1/contracts/{client_id}/terms", dependencies=auth)
    def contract_put(client_id: str, _: str = Depends(caller("onboarding")),
                     req: m.ContractTermsPut = Depends(body(m.ContractTermsPut))) -> dict:
        return _echo(req.request_id, svc.contract_put(req.request_id, _id(client_id), req.model_dump()))

    # --- counsel-question register and memos --------------------------------------------------------------------

    @app.get("/legal/v1/register", dependencies=auth)
    def register(_: str = Depends(reader)) -> dict:
        return svc.register_view()

    @app.get("/legal/v1/register/{cq_id}", dependencies=auth)
    def register_row(cq_id: str, _: str = Depends(reader)) -> dict:
        with svc.lock:
            return svc.register_row_view(_id(cq_id, CQ_OR_TARGET))

    @app.post("/legal/v1/register/{cq_id}/invalidate", dependencies=auth)
    def invalidate(cq_id: str, _: str = Depends(caller("compliance_38")),
                   req: m.Invalidate = Depends(body(m.Invalidate))) -> dict:
        return _echo(req.request_id, svc.invalidate(req.request_id, _id(cq_id, CQ_OR_TARGET), req.source_ref,
                                                    req.detected_change_sha256))

    @app.post("/legal/v1/memos", dependencies=auth, status_code=201)
    def memo(_: str = Depends(andre("memos")), req: m.MemoIntake = Depends(body(m.MemoIntake))) -> dict:
        return _echo(req.request_id, svc.file_memo(req.request_id, req.model_dump()))

    @app.post("/legal/v1/memos/{memo_id}/compliance-proposals", dependencies=auth, status_code=201)
    def memo_proposals(memo_id: str, _: str = Depends(andre("memos/compliance-proposals")),
                       req: m.MemoProposals = Depends(body(m.MemoProposals))) -> dict:
        return _echo(req.request_id, svc.memo_proposals(req.request_id, _id(memo_id, LG_ID), req.model_dump()))

    @app.get("/legal/v1/memos/{memo_id}", dependencies=auth)
    def memo_get(memo_id: str, _: str = Depends(reader)) -> dict:
        return svc.memo_view(_id(memo_id, LG_ID))

    # --- matters and holds -----------------------------------------------------------------------------------------

    @app.post("/legal/v1/requests", dependencies=auth, status_code=201)
    def intake(who: str = Depends(caller(andre_ok=True, route="requests")),
               req: m.MatterIntake = Depends(body(m.MatterIntake))) -> dict:
        return _echo(req.request_id, svc.matter_intake(who, req.request_id, req.model_dump()))

    @app.get("/legal/v1/matters/{matter_id}", dependencies=auth)
    def matter(matter_id: str, _: str = Depends(reader)) -> dict:
        return svc.matter_view(_id(matter_id, LG_ID))

    @app.post("/legal/v1/matters/{matter_id}/close", dependencies=auth)
    def matter_close(matter_id: str, _: str = Depends(andre("matters/close")),
                     req: m.MatterClose = Depends(body(m.MatterClose))) -> dict:
        return _echo(req.request_id, svc.close_matter(req.request_id, _id(matter_id, LG_ID), req.model_dump()))

    @app.get("/legal/v1/holds/check", dependencies=auth)
    def hold_check(subject_ref: str = Query(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]{1,200}$"),
                   _: str = Depends(reader)) -> dict:
        return svc.hold_check(subject_ref)

    @app.post("/legal/v1/holds/{hold_id}/acknowledgments", dependencies=auth)
    def hold_ack(hold_id: str, who: str = Depends(caller("hub")), req: m.HoldAck = Depends(body(m.HoldAck))) -> dict:
        return _echo(req.request_id, svc.hold_ack(who, req.request_id, _id(hold_id, LG_ID), req.model_dump()))

    @app.post("/legal/v1/holds/{hold_id}/release", dependencies=auth)
    def hold_release(hold_id: str, _: str = Depends(andre("holds/release")),
                     req: m.HoldRelease = Depends(body(m.HoldRelease))) -> dict:
        return _echo(req.request_id, svc.hold_release(req.request_id, _id(hold_id, LG_ID), req.model_dump()))

    # --- takedowns --------------------------------------------------------------------------------------------------

    td_callers = caller("hub", "clipper_network", andre_ok=True, route="takedowns")

    @app.post("/legal/v1/takedowns", dependencies=auth, status_code=201)
    def takedown(who: str = Depends(td_callers), req: m.TakedownIn = Depends(body(m.TakedownIn))) -> dict:
        return _echo(req.request_id, svc.takedown_in(who, req.request_id, req.model_dump()))

    @app.get("/legal/v1/takedowns/count", dependencies=auth)
    def takedown_count(post_ref_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
                       _: str = Depends(caller("verification_integrity"))) -> dict:
        return svc.takedown_count(post_ref_sha256)

    @app.post("/legal/v1/takedowns/outbound", dependencies=auth, status_code=201)
    def outbound(_: str = Depends(andre("takedowns/outbound")),
                 req: m.OutboundNotice = Depends(body(m.OutboundNotice))) -> dict:
        return _echo(req.request_id, svc.outbound_notice(req.request_id, req.model_dump()))

    @app.get("/legal/v1/takedowns/{notice_id}", dependencies=auth)
    def takedown_get(notice_id: str, _: str = Depends(reader)) -> dict:
        return svc.takedown_view(_id(notice_id, LG_ID))

    @app.post("/legal/v1/takedowns/{notice_id}/counter-notice", dependencies=auth)
    def counter(notice_id: str, who: str = Depends(td_callers), req: m.CounterNotice = Depends(body(m.CounterNotice))) -> dict:
        return _echo(req.request_id, svc.counter_notice(who, req.request_id, _id(notice_id, LG_ID), req.model_dump()))

    @app.post("/legal/v1/takedowns/{notice_id}/claimant-action", dependencies=auth)
    def claimant(notice_id: str, who: str = Depends(td_callers),
                 req: m.ClaimantAction = Depends(body(m.ClaimantAction))) -> dict:
        return _echo(req.request_id, svc.claimant_action(who, req.request_id, _id(notice_id, LG_ID), req.model_dump()))

    @app.post("/legal/v1/takedowns/{notice_id}/restore", dependencies=auth)
    def restore(notice_id: str, who: str = Depends(td_callers), req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return _echo(req.request_id, svc.restore(who, req.request_id, _id(notice_id, LG_ID)))

    @app.post("/legal/v1/takedowns/{notice_id}/withdraw", dependencies=auth)
    def withdraw(notice_id: str, who: str = Depends(td_callers), req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return _echo(req.request_id, svc.withdraw_notice(who, req.request_id, _id(notice_id, LG_ID)))

    # --- filings, sign-offs, music, retention --------------------------------------------------------------------------

    @app.get("/legal/v1/filings", dependencies=auth)
    def filings(_: str = Depends(reader)) -> dict:
        return svc.list_filings()

    @app.post("/legal/v1/filings", dependencies=auth, status_code=201)
    def filing_create(_: str = Depends(andre("filings")), req: m.FilingCreate = Depends(body(m.FilingCreate))) -> dict:
        return _echo(req.request_id, svc.create_filing(req.request_id, req.model_dump()))

    @app.post("/legal/v1/filings/{filing_id}/ready", dependencies=auth)
    def filing_ready(filing_id: str, _: str = Depends(andre("filings/ready")),
                     req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        return _echo(req.request_id, svc.filing_ready(req.request_id, _id(filing_id, LG_ID)))

    @app.post("/legal/v1/filings/{filing_id}/filed", dependencies=auth)
    def filing_filed(filing_id: str, _: str = Depends(andre("filings/filed")),
                     req: m.FilingFiled = Depends(body(m.FilingFiled))) -> dict:
        return _echo(req.request_id, svc.filing_filed(req.request_id, _id(filing_id, LG_ID), req.model_dump()))

    @app.post("/legal/v1/signoffs", dependencies=auth)
    def signoffs(who: str = Depends(caller("creative_production")),
                 req: m.SignoffRequest = Depends(body(m.SignoffRequest))) -> dict:
        return svc.signoff(who, req.request_id, req.model_dump())

    @app.post("/legal/v1/music/rulings", dependencies=auth)
    def music(who: str = Depends(caller("creative_production", "compliance_38")),
              req: m.MusicRuling = Depends(body(m.MusicRuling))) -> dict:
        return svc.music_ruling(who, req.request_id, req.model_dump())

    @app.get("/legal/v1/retention", dependencies=auth)
    def retention(_: str = Depends(reader)) -> dict:
        return svc.retention_view()

    # --- jobs, rules, reconcile, audit ----------------------------------------------------------------------------

    @app.post("/legal/v1/jobs/{job}/run", dependencies=auth)
    def job(job: str, who: str = Depends(caller("scheduler")), req: m.RunRequest = Depends(body(m.RunRequest))) -> dict:
        if job not in JOBS:
            raise Invalid("unknown job")
        return _echo(req.request_id, svc.run_job(who, req.request_id, job))

    @app.get("/legal/v1/rules", dependencies=auth)
    def rules(_: str = Depends(reader)) -> dict:
        return svc.rules_view()

    @app.post("/legal/v1/rules/proposals", dependencies=auth, status_code=201)
    def rule_propose(_: str = Depends(andre("rules/proposals")),
                     req: m.RuleProposalRequest = Depends(body(m.RuleProposalRequest))) -> dict:
        return _echo(req.request_id, svc.create_rule_proposal(req.request_id, req.model_dump(exclude={"request_id"})))

    @app.post("/legal/v1/rules/decisions", dependencies=auth)
    def rule_decide(_: str = Depends(andre("rules/decisions")),
                    req: m.RuleDecisions = Depends(body(m.RuleDecisions))) -> dict:
        return _echo(req.request_id, svc.decide_rules(req.request_id, [d.model_dump() for d in req.decisions]))

    @app.get("/legal/v1/reconcile", dependencies=auth)
    def reconcile_plan(_: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile_plan()

    @app.post("/legal/v1/reconcile", dependencies=auth)
    def reconcile(_: str = Depends(andre("reconcile")), req: m.ReconcileRequest = Depends(body(m.ReconcileRequest))) -> dict:
        return _echo(req.request_id, svc.reconcile(req.request_id, req.head_sha256, list(req.void_lines),
                                                   list(req.void_event_ids)))

    @app.get("/legal/v1/audit/export", dependencies=auth)
    def audit(who: str = Depends(reader), since: Optional[str] = Query(default=None, max_length=40),
              until: Optional[str] = Query(default=None, max_length=40),
              cursor: int = Query(default=0, ge=0, le=10**12)) -> dict:
        try:
            return svc.audit_export(who, since, until, cursor)
        except ValueError:
            raise Invalid("since/until must be RFC 3339 timestamps with offset") from None

    @app.get("/legal/v1/audit/evidence", dependencies=auth)
    def audit_evidence(_: str = Depends(caller("compliance_38", andre_ok=True, route="audit/evidence")),
                       limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0, le=10**7),
                       event_type: Optional[str] = Query(default=None, pattern=r"^[a-z0-9_]{1,64}$")) -> dict:
        """Bug sweep E (bizdev-py R6-M1): the evidence view Compliance (38) and auditors use; unanchored evidence
        = attempted, not done."""
        return svc.audit_evidence(limit, offset, event_type)

    return app


def build_ports(settings: config_mod.Settings) -> Ports:
    """Production wiring: every port is its fail-closed stand-in except the Compliance thin client (when all three
    LEGAL_COMPLIANCE_* are set)."""
    ports = Ports()
    if settings.compliance_url:
        ports.compliance = HttpCompliance(settings.compliance_url, settings.compliance_token,
                                          settings.compliance_caller_token)
    return ports


def build_service(settings: config_mod.Settings, clock: Optional[Clock] = None, ports: Optional[Ports] = None,
                  ledger=None) -> LegalService:
    clock = clock or SystemClock()
    if ledger is None:
        ledger = (HttpLedgerClient(settings.ledger_url, settings.ledger_token)
                  if settings.ledger_url and settings.ledger_token else UnconfiguredLedgerClient())
    raw = config_mod.read_seeds(settings)
    seeds = Seeds(raw["legal_rules_seed.json"], raw["documents.json"], raw["counsel_questions.json"],
                  raw["retention.json"], raw["signoff_topics.json"], raw["advice_patterns.json"],
                  raw["us_federal_holidays.json"])
    pinned = hashlib.sha256(seeds.rules).hexdigest() == config_mod.PINNED_SEED_SHA256
    lock = settings.data_dir_lock              # bug sweep E: the flock, taken by config.load before the log is opened
    token = lock.claim() if lock is not None else None   # claimed BEFORE the log is built
    try:
        return LegalService(settings, Recorder(ledger), RecordLog(settings.data_dir), BlobStore(settings.data_dir),
                            seeds, ports or build_ports(settings), clock, pinned, lock_token=token)
    except BaseException:
        if lock is not None:
            lock.release_claim(token)
        raise


def _app_from_env() -> FastAPI:
    settings = config_mod.load()
    return create_app(build_service(settings), settings)


app = _app_from_env()


def main() -> None:
    import serve

    host = os.environ.get("LEGAL_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("LEGAL_PORT", "8420"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
