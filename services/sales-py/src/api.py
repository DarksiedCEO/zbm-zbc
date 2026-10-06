"""
REST surface for Lead Generation & Opportunity Intelligence (26) + Sales (27) (ADR 0013).

Same discipline as security-py / legal-py (the request-limit, no-store, bearer and caller blocks are copied from
security-py's api.py):
- fail-closed bearer auth on every route except /health; the service refuses to start without SALES_SERVICE_TOKEN;
  ``hmac.compare_digest`` in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-SALES-Caller-Token`` (SALES_CALLER_TOKENS; SHA-256 digests compared against EVERY
  configured token, no early exit); a wrong or absent caller token is a 403;
- Andre's actions (template, price and proposal approvals) need the ``dashboard`` caller AND
  ``X-Andre-Approval-Token`` (SALES_ANDRE_APPROVAL_TOKEN, legal-py's FounderGate): the dashboard alone is never
  Andre, and a token equal to the service or a caller token counts as not configured;
- a body naming a date of birth, government id, payment card, bank account, IP, device or protected-trait field at
  any depth is refused 422 before it is parsed (textguard.py);
- every response carries ``Cache-Control: no-store``; /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless
  SALES_BIND_ADDR says otherwise; port 8450; hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 256 KiB (413), JSON only (415), JSON
  nesting depth and member count bounded (422), body read deadline (408). Error bodies carry a reason code from
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
from errors import Forbidden, FounderRefused, Invalid, SalesError
from founder import FounderGate
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from reasons import R
from service import JOBS, SalesService
from store import DataDirLock, RecordLog
from textguard import forbidden_keys

log = logging.getLogger("sales.api")

CALLER_HEADER = "X-SALES-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
MAX_BODY_BYTES = 256 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

ROUTE_LIMITS: list[tuple[re.Pattern, int]] = []
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
                message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
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


ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SL_ID = re.compile(r"^sl-[a-z]{3}-[0-9a-f]{40}$")
LINE_ID = re.compile(r"^(zbm|zbc)\.[a-z0-9_]{1,60}$")


def _id(value: str, rx: re.Pattern = SL_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


class NoStore:
    """Every answer (contact data above all) is marked not to be cached or stored anywhere."""

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
LEAD_STATUSES = "^(new|nurture|qualified|converted|disqualified|stale)$"


def create_app(service: SalesService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Lead Generation (26) + Sales (27)", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None,
                  description="NOT LIVE: every send provider, lead source and department port is a fail-closed "
                              "stand-in.")
    svc = service
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    founder = FounderGate.build(settings.andre_token, settings.service_token, list(settings.caller_tokens.values()))
    app.state.service = svc

    def caller(*allowed: str) -> Callable:
        def dep(request: Request) -> str:
            name = callers.identify(request.headers.get(CALLER_HEADER))
            if name is None:
                raise Forbidden(R("CALLER_UNKNOWN"))
            if allowed and name not in allowed:
                raise Forbidden(R("CALLER_NOT_ALLOWED"))
            return name
        return dep

    def andre(route: str) -> Callable:
        dash = caller("dashboard")

        def dep(request: Request, x_andre_approval_token: Optional[str] = Header(default=None)) -> str:
            dash(request)
            try:
                founder.verify(x_andre_approval_token)
            except FounderRefused as exc:
                svc.rec.try_record(svc_refusal_id(route, exc.reason), "founder_approval_refused", "sales",
                                   f"route:{route}", {"route": route, "reason": exc.reason},
                                   f"Andre approval refused on {route}")
                raise
            return "andre"
        return dep

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = forbidden_keys(payload)
            if bad:
                raise RequestValidationError([{"loc": ("body", b), "msg": "Sales stores no date of birth, government "
                                               "id, payment, bank, IP, device or protected-trait data",
                                               "type": "forbidden_field"} for b in bad[:20]])
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

    @app.exception_handler(SalesError)
    def _domain(_: Request, exc: SalesError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    dashboard = caller("dashboard")
    worker = caller("dashboard", "sales_agent")
    lead_posters = caller("hub", "onboarding", "detection", "dashboard")

    # ------------------------------------------------------------------ health and status

    @app.get("/health")
    def health() -> dict:
        return {"status": svc.health()["status"]}          # nothing else unauthenticated

    @app.get("/sales/v1/status", dependencies=auth)
    def full_status(who: str = Depends(dashboard)) -> dict:
        return svc.health()

    @app.get("/sales/v1/intelligences", dependencies=auth)
    def intelligences(who: str = Depends(worker)) -> list:
        return registry()

    # ------------------------------------------------------------------ leads and pipeline

    @app.post("/sales/v1/leads", dependencies=auth, status_code=201)
    def create_lead(req: dict = Depends(body(m.LeadIn)), who: str = Depends(lead_posters)) -> dict:
        return svc.intake(who, req)

    @app.post("/sales/v1/leads/import", dependencies=auth)
    def import_leads(req: dict = Depends(body(m.LeadImport)), who: str = Depends(caller("sales_agent", "scheduler"))):
        return svc.import_leads(who, req)

    @app.get("/sales/v1/leads", dependencies=auth)
    def leads(status_: Optional[str] = Query(default=None, alias="status", pattern=LEAD_STATUSES),
              brand: Optional[str] = Query(default=None, pattern="^(zbm|zbc)$"), who: str = Depends(worker)) -> list:
        return svc.leads_view(status_, brand)

    @app.get("/sales/v1/leads/{lead_id}", dependencies=auth)
    def lead(lead_id: str, who: str = Depends(worker)) -> dict:
        return svc.lead(_id(lead_id))

    @app.post("/sales/v1/leads/{lead_id}/owner", dependencies=auth)
    def lead_owner(lead_id: str, req: dict = Depends(body(m.Owner)), who: str = Depends(worker)) -> dict:
        return svc.set_owner(who, _id(lead_id), req)

    @app.post("/sales/v1/leads/{lead_id}/disqualify", dependencies=auth)
    def disqualify(lead_id: str, req: dict = Depends(body(m.Disqualify)), who: str = Depends(worker)) -> dict:
        return svc.disqualify(who, _id(lead_id), req)

    @app.post("/sales/v1/leads/{lead_id}/convert", dependencies=auth, status_code=201)
    def convert(lead_id: str, req: dict = Depends(body(m.Convert)), who: str = Depends(worker)) -> dict:
        return svc.convert(who, _id(lead_id), req)

    @app.get("/sales/v1/contacts/{contact_id}", dependencies=auth)
    def contact(contact_id: str, who: str = Depends(worker)) -> dict:
        return svc.contact(_id(contact_id))

    @app.post("/sales/v1/contacts/{contact_id}/time-zone", dependencies=auth)
    def time_zone(contact_id: str, req: dict = Depends(body(m.TimeZoneSet)),
                  who: str = Depends(caller("hub", "onboarding", "dashboard"))) -> dict:    # never the agent (S1-M1)
        return svc.set_time_zone(who, _id(contact_id), req)

    @app.post("/sales/v1/accounts/{account_id}/display-name", dependencies=auth)
    def display_name(account_id: str, req: dict = Depends(body(m.DisplayName)), who: str = Depends(dashboard)):
        return svc.verify_display_name(who, _id(account_id), req)

    @app.post("/sales/v1/contacts/{contact_id}/first-name", dependencies=auth)
    def first_name(contact_id: str, req: dict = Depends(body(m.FirstName)), who: str = Depends(dashboard)) -> dict:
        return svc.verify_first_name(who, _id(contact_id), req)

    @app.get("/sales/v1/opportunities", dependencies=auth)
    def opportunities(stage: Optional[str] = Query(default=None, pattern="^[a-z_]{1,20}$"),
                      who: str = Depends(worker)) -> list:
        return svc.opportunities_view(stage)

    @app.get("/sales/v1/opportunities/{opp_id}", dependencies=auth)
    def opportunity(opp_id: str, who: str = Depends(worker)) -> dict:
        return svc.opportunity(_id(opp_id))

    @app.post("/sales/v1/opportunities/{opp_id}/stage", dependencies=auth)
    def stage(opp_id: str, req: dict = Depends(body(m.StageSet)), who: str = Depends(worker)) -> dict:
        return svc.set_stage(who, _id(opp_id), req)

    @app.post("/sales/v1/activities", dependencies=auth, status_code=201)
    def activity(req: dict = Depends(body(m.ActivityIn)), who: str = Depends(worker)) -> dict:
        return svc.log_activity(who, req)

    @app.get("/sales/v1/tasks", dependencies=auth)
    def tasks(status_: Optional[str] = Query(default=None, alias="status", pattern="^(open|closed)$"),
              who: str = Depends(worker)) -> list:
        return svc.tasks_view(status_)

    @app.post("/sales/v1/tasks/{task_id}/close", dependencies=auth)
    def close_task(task_id: str, req: dict = Depends(body(m.TaskClose)), who: str = Depends(worker)) -> dict:
        return svc.close_task(who, _id(task_id), req)

    @app.post("/sales/v1/tasks/{task_id}/decision", dependencies=auth)
    def hold_decision(task_id: str, req: dict = Depends(body(m.HoldDecision)),
                      who: str = Depends(andre("hold_decision"))) -> dict:
        return svc.decide_hold(_id(task_id), req)

    # ------------------------------------------------------------------ consent and suppression

    @app.post("/sales/v1/consents", dependencies=auth, status_code=201)
    def grant(req: dict = Depends(body(m.ConsentGrant)), who: str = Depends(caller("hub", "onboarding", "dashboard"))):
        return svc.grant_consent(who, req)

    @app.post("/sales/v1/consents/revoke", dependencies=auth)
    def revoke(req: dict = Depends(body(m.ConsentRevoke)),
               who: str = Depends(caller("hub", "dashboard", "provider_events", "sales_agent"))) -> dict:
        return svc.revoke_consent(who, req)

    @app.post("/sales/v1/suppressions", dependencies=auth, status_code=201)
    def suppress(req: dict = Depends(body(m.Suppress)),
                 who: str = Depends(caller("hub", "dashboard", "provider_events", "sales_agent"))) -> dict:
        return svc.suppress(who, req)

    @app.get("/sales/v1/suppressions", dependencies=auth)
    def suppressions(who: str = Depends(caller("dashboard", "compliance_38"))) -> list:
        return svc.suppressions_view()

    @app.post("/sales/v1/unsubscribe", dependencies=auth)
    def unsubscribe(req: dict = Depends(body(m.Unsubscribe)), who: str = Depends(caller("hub"))) -> dict:
        return svc.unsubscribe(who, req)

    # ------------------------------------------------------------------ templates

    @app.get("/sales/v1/templates", dependencies=auth)
    def templates(who: str = Depends(worker)) -> list:
        return svc.templates_view()

    @app.get("/sales/v1/templates/{template_id}", dependencies=auth)
    def template(template_id: str, who: str = Depends(worker)) -> dict:
        return svc.template(_id(template_id))

    @app.post("/sales/v1/templates", dependencies=auth, status_code=201)
    def create_template(req: dict = Depends(body(m.TemplateCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_template(who, req)

    @app.post("/sales/v1/templates/{template_id}/versions", dependencies=auth, status_code=201)
    def add_version(template_id: str, req: dict = Depends(body(m.TemplateEdit)), who: str = Depends(worker)) -> dict:
        return svc.add_template_version(who, _id(template_id), req)

    @app.post("/sales/v1/templates/{template_id}/versions/{version}/edit", dependencies=auth)
    def edit_version(template_id: str, version: int, req: dict = Depends(body(m.TemplateEdit)),
                     who: str = Depends(worker)) -> dict:
        return svc.edit_template(who, _id(template_id), _version(version), req)

    @app.post("/sales/v1/templates/{template_id}/versions/{version}/approve", dependencies=auth)
    def approve_template(template_id: str, version: int, req: dict = Depends(body(m.HashApproval)),
                         who: str = Depends(andre("template_approve"))) -> dict:
        return svc.approve_template(_id(template_id), _version(version), req)

    # ------------------------------------------------------------------ outreach

    agent = caller("sales_agent")

    @app.post("/sales/v1/outreach/email", dependencies=auth, status_code=201)
    def outreach_email(req: dict = Depends(body(m.OutreachMessage)), who: str = Depends(agent)) -> dict:
        return svc.queue_email(who, req)

    @app.post("/sales/v1/outreach/sms", dependencies=auth, status_code=201)
    def outreach_sms(req: dict = Depends(body(m.OutreachMessage)), who: str = Depends(agent)) -> dict:
        return svc.queue_sms(who, req)

    @app.post("/sales/v1/outreach/voice", dependencies=auth, status_code=201)
    def outreach_voice(req: dict = Depends(body(m.OutreachVoice)), who: str = Depends(agent)) -> dict:
        return svc.queue_voice(who, req)

    @app.get("/sales/v1/outreach/messages", dependencies=auth)
    def messages(status_: Optional[str] = Query(default=None, alias="status",
                                                pattern="^(queued|sending|sent|failed|cancelled)$"),
                 who: str = Depends(worker)) -> list:
        return svc.messages_view(status_)

    @app.post("/sales/v1/outreach/messages/{message_id}/cancel", dependencies=auth)
    def cancel(message_id: str, req: dict = Depends(body(m.MessageCancel)), who: str = Depends(worker)) -> dict:
        return svc.cancel_message(who, _id(message_id), req)

    events = caller("provider_events")

    @app.post("/sales/v1/events/email", dependencies=auth)
    def email_event(req: dict = Depends(body(m.EmailEvent)), who: str = Depends(events)) -> dict:
        return svc.email_event(who, req)

    @app.post("/sales/v1/replies", dependencies=auth, status_code=201)
    def reply(req: dict = Depends(body(m.ReplyIn)), who: str = Depends(events)) -> dict:
        return svc.reply(who, req)

    # ------------------------------------------------------------------ price books and proposals

    @app.get("/sales/v1/pricebook/{brand}", dependencies=auth)
    def pricebook(brand: str, who: str = Depends(worker)) -> list:
        return svc.pricebook_view(_id(brand, BRAND))

    @app.post("/sales/v1/pricebook/{brand}/lines/{line_id}/approve", dependencies=auth)
    def approve_price(brand: str, line_id: str, req: dict = Depends(body(m.PriceApprove)),
                      who: str = Depends(andre("price_approve"))) -> dict:
        return svc.approve_price(_id(brand, BRAND), _id(line_id, LINE_ID), req)

    @app.post("/sales/v1/pricebook/{brand}/lines/{line_id}/withdraw", dependencies=auth)
    def withdraw_price(brand: str, line_id: str, req: dict = Depends(body(m.PriceWithdraw)),
                       who: str = Depends(andre("price_withdraw"))) -> dict:
        return svc.withdraw_price(_id(brand, BRAND), _id(line_id, LINE_ID), req)

    @app.get("/sales/v1/proposals", dependencies=auth)
    def proposals(status_: Optional[str] = Query(default=None, alias="status",
                                                 pattern="^(pending_andre|approved|sent|won|lost)$"),
                  who: str = Depends(worker)) -> list:
        return svc.proposals_view(status_)

    @app.get("/sales/v1/proposals/{pid}", dependencies=auth)
    def proposal(pid: str, who: str = Depends(worker)) -> dict:
        return svc.proposal(_id(pid))

    @app.post("/sales/v1/proposals", dependencies=auth, status_code=201)
    def create_proposal(req: dict = Depends(body(m.ProposalCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_proposal(who, req)

    @app.post("/sales/v1/proposals/{pid}/approve", dependencies=auth)
    def approve_proposal(pid: str, req: dict = Depends(body(m.HashApproval)),
                         who: str = Depends(andre("proposal_approve"))) -> dict:
        return svc.approve_proposal(_id(pid), req)

    @app.post("/sales/v1/proposals/{pid}/send", dependencies=auth)
    def send_proposal(pid: str, req: dict = Depends(body(m.ProposalSend)), who: str = Depends(worker)) -> dict:
        return svc.send_proposal(who, _id(pid), req)

    andre_won = andre("proposal_won")

    def won_actor(request: Request, x_andre_approval_token: Optional[str] = Header(default=None)) -> str:
        """Andre (dashboard + his token), or a worker that must then show the client's confirmed acceptance."""
        if x_andre_approval_token is not None:
            return andre_won(request, x_andre_approval_token)
        return worker(request)

    @app.post("/sales/v1/proposals/{pid}/won", dependencies=auth)
    def won(pid: str, req: dict = Depends(body(m.ProposalWon)), who: str = Depends(won_actor)) -> dict:
        return svc.proposal_won(who, _id(pid), req)

    @app.post("/sales/v1/proposals/{pid}/lost", dependencies=auth)
    def lost(pid: str, req: dict = Depends(body(m.ProposalLost)), who: str = Depends(worker)) -> dict:
        return svc.proposal_lost(who, _id(pid), req)

    @app.get("/sales/v1/handoffs", dependencies=auth)
    def handoffs(who: str = Depends(worker)) -> list:
        return svc.handoffs_view()

    # ------------------------------------------------------------------ jobs and audit

    @app.post("/sales/v1/jobs/{name}/run", dependencies=auth)
    def run_job(name: str, req: dict = Depends(body(m.JobRun)), who: str = Depends(caller("scheduler"))) -> dict:
        if name not in JOBS:
            raise Invalid(R("JOB_UNKNOWN"))
        return svc.run_job(name, req)

    @app.get("/sales/v1/audit/integrity", dependencies=auth)
    def integrity(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        res = svc.verify_integrity(force=True)
        return {"integrity": res, "ledger_valid": svc.rec.client.verify(), "log_length": len(svc.log)}

    @app.get("/sales/v1/audit/export", dependencies=auth)
    def audit_export(since: int = Query(default=1, ge=1, le=10_000_000), limit: int = Query(default=200, ge=1, le=1000),
                     who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        return svc.audit_export(since, limit)

    return app


BRAND = re.compile(r"^(zbm|zbc)$")


def _version(v: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10_000:
        raise Invalid(R("INVALID"), field="path")
    return v


def svc_refusal_id(route: str, reason: str) -> str:
    from ledger import derived_id
    return derived_id("ref", route, reason, time.time_ns())


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build(env: Optional[dict] = None, ports: Optional[Ports] = None):
    """The production wiring: settings, data-directory lock, ledger, log, ports; returns (asgi, service)."""
    settings = config_mod.load(env)
    lock = DataDirLock(settings.data_dir)
    if settings.ledger_url and settings.ledger_token:
        ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
    else:
        ledger = UnconfiguredLedgerClient()
    svc = SalesService(settings, Recorder(ledger), RecordLog(settings.data_dir), ports or Ports.default())
    svc.data_dir_lock = lock
    return _wrap(create_app(svc, settings)), svc


def main() -> None:
    import serve
    app, svc = build()
    serve.run(app, svc.settings.bind_addr, svc.settings.port)


if __name__ == "__main__":
    main()
