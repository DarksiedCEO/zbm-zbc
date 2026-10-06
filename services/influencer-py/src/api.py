"""
REST surface for Influencer & Partnership Marketing (11) (ADR 0015).

Same discipline as sales-py / service-py (the request-limit, no-store, bearer and caller blocks are copied from
service-py's api.py, itself security-py's):
- fail-closed bearer auth on every route except /health; the service refuses to start without INF_SERVICE_TOKEN;
  ``hmac.compare_digest`` in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-INF-Caller-Token`` (INF_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token is a 403;
- Andre's actions (template, DM, brief, deal and content approvals, hold decisions) need the ``dashboard`` caller AND
  ``X-Andre-Approval-Token`` (INF_ANDRE_APPROVAL_TOKEN, legal-py's FounderGate): the dashboard alone is never Andre,
  a token equal to the service or a caller token counts as not configured, and every refusal is recorded on the
  ledger (``founder_approval_refused``);
- before a body is parsed (every route but /replies, which is never refused for its content): a raw tax id (a key
  naming one, or a value shaped like one) is refused 422
  ``TAX_ID_REFUSED``; a date of birth, age, government id, payment, bank, IP, device or protected-trait key 422
  ``FORBIDDEN_FIELD`` (textguard.py);
- every response carries ``Cache-Control: no-store``; /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless
  INF_BIND_ADDR says otherwise; port 8480; hardened launcher (serve.py);
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 128 KiB (413; 512 KiB on /replies),
  JSON only (415), JSON nesting depth and member count bounded (422), body read deadline (408). Error bodies carry a reason code from
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
import textguard
from errors import Forbidden, FounderRefused, InfError, Invalid
from founder import FounderGate
from intelligences import registry
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from ports import Ports
from reasons import R
from service import JOBS, InfluencerService
from store import RecordLog

log = logging.getLogger("influencer.api")

CALLER_HEADER = "X-INF-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
MAX_BODY_BYTES = 512 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

# AEGIS R2-N3: a reply (a long quoted thread) may be up to 512 KiB; the relay truncates anything longer before it sends
ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [(re.compile(r"^/inf/v1/replies$"), 512 * 1024)]
DEFAULT_ROUTE_LIMIT = 128 * 1024


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
        loc = [textguard._seg(x)[:ERROR_MAX_STR] if isinstance(x, str) else x for x in list(e.get("loc", ()))[:8]]
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



IF_ID = re.compile(r"^if-[a-z]{3}-[0-9a-f]{40}$")
P = "/inf/v1"


def _id(value: str, rx: re.Pattern = IF_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


def _version(v: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 10_000:
        raise Invalid(R("INVALID"), field="path")
    return v


def create_app(service: InfluencerService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Influencer & Partnership Marketing (11)", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None,
                  description="NOT LIVE: every send provider, discovery source and department port is a fail-closed "
                              "stand-in. Nothing is ever paid.")
    svc = service
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    gate = FounderGate.build(settings.andre_token, settings.service_token, list(settings.caller_tokens.values()))
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
    worker = caller("dashboard", "influencer_agent")
    auditor = caller("dashboard", "compliance_38")

    def andre(route: str) -> Callable:
        def dep(request: Request, who: str = Depends(dashboard)) -> str:
            try:
                gate.verify(request.headers.get(FOUNDER_HEADER))
            except FounderRefused as exc:
                svc.record_refusal(route, exc.reason)
                raise
            return "andre"
        return dep

    def body(model: type[BaseModel], exempt: frozenset = frozenset()) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            found = textguard.find(payload, exempt)
            if found:
                raise Invalid(R(found[0]), field=found[1])         # AEGIS R2-L-c: the field, never the value
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

    @app.exception_handler(InfError)
    def _domain(_: Request, exc: InfError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason, **exc.body})

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

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

    # ------------------------------------------------------------------ influencers

    @app.post(P + "/applications", dependencies=auth, status_code=201)
    def application(req: dict = Depends(body(m.LinkRequest)), who: str = Depends(caller("hub"))) -> dict:
        return svc.request_link(who, req)

    @app.post(P + "/sessions/application", dependencies=auth, status_code=201)
    def session_application(req: dict = Depends(body(m.ApplicationIn)), who: str = Depends(caller("hub"))) -> dict:
        return svc.submit_application(who, req)

    @app.post(P + "/influencers", dependencies=auth, status_code=201)
    def prospect(req: dict = Depends(body(m.ProspectIn)), who: str = Depends(dashboard)) -> dict:
        return svc.create_prospect(who, req)

    @app.post(P + "/discovery/import", dependencies=auth)
    def discovery(req: dict = Depends(body(m.DiscoveryImport)),
                  who: str = Depends(caller("influencer_agent", "scheduler"))) -> dict:
        return svc.import_discovery(who, req)

    @app.get(P + "/influencers", dependencies=auth)
    def influencers(source: Optional[str] = Query(default=None, pattern="^[a-z_]{1,30}$"),
                    who: str = Depends(worker)) -> list:
        return svc.influencers_view(source)

    @app.get(P + "/influencers/{iid}", dependencies=auth)
    def influencer(iid: str, who: str = Depends(worker)) -> dict:
        return svc.influencer(_id(iid))

    @app.post(P + "/influencers/{iid}/first-name", dependencies=auth)
    def first_name(iid: str, req: dict = Depends(body(m.FirstName)), who: str = Depends(dashboard)) -> dict:
        return svc.verify_first_name(who, _id(iid), req)

    @app.post(P + "/influencers/{iid}/minor-review", dependencies=auth)
    def minor_review(iid: str, req: dict = Depends(body(m.MinorReview)),
                     who: str = Depends(andre("influencers/minor-review"))) -> dict:
        return svc.decide_minor_review(_id(iid), req)

    # ------------------------------------------------------------------ the address link and creator sessions (AEGIS round 4)

    @app.post(P + "/confirmations", dependencies=auth)
    def confirm(req: dict = Depends(body(m.Confirm)), who: str = Depends(caller("hub"))) -> dict:
        return svc.confirm(who, req)

    @app.get(P + "/confirmations", dependencies=auth)
    def confirmations(status_: Optional[str] = Query(default=None, alias="status",
                                                     pattern="^(pending|undeliverable|pending_andre|awaiting_andre|awaiting_andre_digest|used|applied|rejected)$"),
                      who: str = Depends(dashboard)) -> list:
        return svc.confirmations_view(status_)

    @app.post(P + "/confirmations/bulk-reject", dependencies=auth)
    def bulk_reject(req: dict = Depends(body(m.BulkReject)), who: str = Depends(andre("confirmations/bulk-reject"))):
        return svc.bulk_reject(req)

    @app.post(P + "/confirmations/{cid}/approve", dependencies=auth)
    def approve_confirmation(cid: str, req: dict = Depends(body(m.Approve)),
                             who: str = Depends(andre("confirmations/approve"))) -> dict:
        return svc.decide_confirmation(_id(cid), req, approve=True)

    @app.post(P + "/confirmations/{cid}/reject", dependencies=auth)
    def reject_confirmation(cid: str, req: dict = Depends(body(m.RequestOnly)),
                            who: str = Depends(andre("confirmations/reject"))) -> dict:
        return svc.decide_confirmation(_id(cid), req, approve=False)

    # ------------------------------------------------------------------ suppression and unsubscribe

    @app.post(P + "/suppressions", dependencies=auth, status_code=201)
    def suppress(req: dict = Depends(body(m.Suppress)),
                 who: str = Depends(caller("hub", "dashboard", "provider_events", "influencer_agent"))) -> dict:
        return svc.suppress(who, req)

    @app.get(P + "/suppressions", dependencies=auth)
    def suppressions(who: str = Depends(auditor)) -> list:
        return svc.suppressions_view()

    @app.post(P + "/unsubscribe", dependencies=auth)
    def unsubscribe(req: dict = Depends(body(m.Unsubscribe)), who: str = Depends(caller("hub"))) -> dict:
        return svc.unsubscribe(who, req)

    # ------------------------------------------------------------------ templates and email

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

    @app.post(P + "/templates/{tid}/versions/{version}/approve", dependencies=auth)
    def approve_template(tid: str, version: int, req: dict = Depends(body(m.Approve)),
                         who: str = Depends(andre("templates/approve"))) -> dict:
        return svc.approve_template(_id(tid), _version(version), req)

    @app.post(P + "/outreach/email", dependencies=auth, status_code=201)
    def outreach_email(req: dict = Depends(body(m.EmailOutreach)), who: str = Depends(caller("influencer_agent"))):
        return svc.queue_email(who, req)

    @app.get(P + "/outreach/messages", dependencies=auth)
    def messages(status_: Optional[str] = Query(default=None, alias="status",
                                                pattern="^(queued|sending|sent|failed|cancelled)$"),
                 who: str = Depends(worker)) -> list:
        return svc.messages_view(status_)

    @app.post(P + "/outreach/messages/{mid}/cancel", dependencies=auth)
    def cancel_message(mid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(worker)) -> dict:
        return svc.cancel_message(who, _id(mid), req)

    # ------------------------------------------------------------------ DMs (Andre approves every one)

    @app.post(P + "/dm-drafts", dependencies=auth, status_code=201)
    def dm_draft(req: dict = Depends(body(m.DmDraft)), who: str = Depends(caller("influencer_agent"))) -> dict:
        return svc.draft_dm(who, req)

    @app.get(P + "/dm-drafts", dependencies=auth)
    def dm_drafts(status_: Optional[str] = Query(default=None, alias="status",
                                                 pattern="^(draft|approved|rejected|cancelled)$"),
                  who: str = Depends(worker)) -> list:
        return svc.drafts_view(status_)

    @app.post(P + "/dm-drafts/{did}/approve", dependencies=auth)
    def dm_approve(did: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre("dm-drafts/approve"))):
        return svc.approve_dm(_id(did), req)

    @app.post(P + "/dm-drafts/{did}/reject", dependencies=auth)
    def dm_reject(did: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre("dm-drafts/reject"))):
        return svc.reject_dm(_id(did), req)

    # ------------------------------------------------------------------ provider events, replies, holds

    @app.post(P + "/events/email", dependencies=auth)
    def email_event(req: dict = Depends(body(m.EmailEvent)), who: str = Depends(caller("provider_events"))) -> dict:
        return svc.email_event(who, req)

    @app.post(P + "/replies", dependencies=auth, status_code=201)
    def reply(payload: Any = Body(default=None), who: str = Depends(caller("provider_events"))) -> dict:
        # AEGIS R2-N3: never refused for anything a provider sends (no tax-id scan: nothing raw is stored); a body that
        # is not a JSON object is read as an empty reply (it is still recorded, as a review entry)
        raw = payload if isinstance(payload, dict) else {}
        return svc.reply(who, m.ReplyIn.model_validate(raw).model_dump(mode="python"), raw)

    @app.get(P + "/holds", dependencies=auth)
    def holds(status_: Optional[str] = Query(default=None, alias="status", pattern="^(active|digest|lifted|opted_out|expired)$"),
              who: str = Depends(worker)) -> list:
        return svc.holds_view(status_)

    @app.post(P + "/holds/{hid}/decision", dependencies=auth)
    def hold_decision(hid: str, req: dict = Depends(body(m.HoldDecision)),
                      who: str = Depends(andre("holds/decision"))) -> dict:
        return svc.decide_hold(_id(hid), req)

    # ------------------------------------------------------------------ campaigns and briefs

    @app.post(P + "/campaigns", dependencies=auth, status_code=201)
    def create_campaign(req: dict = Depends(body(m.CampaignCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_campaign(who, req)

    @app.get(P + "/campaigns", dependencies=auth)
    def campaigns(who: str = Depends(worker)) -> list:
        return svc.campaigns_view()

    @app.get(P + "/campaigns/{cid}", dependencies=auth)
    def campaign(cid: str, who: str = Depends(worker)) -> dict:
        return svc.campaign(_id(cid))

    @app.post(P + "/campaigns/{cid}/close", dependencies=auth)
    def close_campaign(cid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(worker)) -> dict:
        return svc.close_campaign(who, _id(cid), req)

    @app.post(P + "/briefs", dependencies=auth, status_code=201)
    def create_brief(req: dict = Depends(body(m.BriefCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_brief(who, req)

    @app.get(P + "/briefs", dependencies=auth)
    def briefs(campaign_id: Optional[str] = Query(default=None, max_length=50), who: str = Depends(worker)) -> list:
        return svc.briefs_view(_id(campaign_id) if campaign_id is not None else None)

    @app.get(P + "/briefs/{bid}", dependencies=auth)
    def brief(bid: str, who: str = Depends(caller("dashboard", "influencer_agent", "hub"))) -> dict:
        return svc.brief(_id(bid))

    @app.post(P + "/briefs/{bid}/approve", dependencies=auth)
    def approve_brief(bid: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre("briefs/approve"))):
        return svc.approve_brief(_id(bid), req)

    # ------------------------------------------------------------------ deals and contracts

    @app.post(P + "/deals", dependencies=auth, status_code=201)
    def create_deal(req: dict = Depends(body(m.DealCreate)), who: str = Depends(worker)) -> dict:
        return svc.create_deal(who, req)

    @app.get(P + "/deals", dependencies=auth)
    def deals(status_: Optional[str] = Query(default=None, alias="status", pattern="^[a-z_]{1,20}$"),
              influencer_id: Optional[str] = Query(default=None, max_length=50), who: str = Depends(worker)) -> list:
        return svc.deals_view(status_, _id(influencer_id) if influencer_id is not None else None)

    @app.get(P + "/deals/{did}", dependencies=auth)
    def deal(did: str, who: str = Depends(worker)) -> dict:
        return svc.deal(_id(did))

    @app.post(P + "/deals/{did}/approve", dependencies=auth)
    def approve_deal(did: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre("deals/approve"))):
        return svc.approve_deal(_id(did), req)

    @app.post(P + "/deals/{did}/reject", dependencies=auth)
    def reject_deal(did: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre("deals/reject"))):
        return svc.reject_deal(_id(did), req)

    @app.post(P + "/deals/{did}/cancel", dependencies=auth)
    def cancel_deal(did: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(worker)) -> dict:
        return svc.cancel_deal(who, _id(did), req)

    @app.post(P + "/deals/{did}/contract", dependencies=auth)
    def send_contract(did: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(worker)) -> dict:
        return svc.send_contract(who, _id(did), req)

    @app.post(P + "/deals/{did}/contract/confirm", dependencies=auth)
    def confirm_contract(did: str, req: dict = Depends(body(m.RequestOnly)),
                         who: str = Depends(caller("dashboard", "influencer_agent", "scheduler"))) -> dict:
        return svc.confirm_contract(who, _id(did), req)

    # ------------------------------------------------------------------ content (FTC) and material connections

    @app.post(P + "/contents", dependencies=auth, status_code=201)
    def submit_content(req: dict = Depends(body(m.ContentSubmit)),
                       who: str = Depends(caller("influencer_agent", "hub"))) -> dict:
        return svc.submit_content(who, req)

    @app.get(P + "/contents", dependencies=auth)
    def contents(status_: Optional[str] = Query(default=None, alias="status",
                                                pattern="^(submitted|approved|rejected|live)$"),
                 deal_id: Optional[str] = Query(default=None, max_length=50), who: str = Depends(worker)) -> list:
        return svc.contents_view(status_, _id(deal_id) if deal_id is not None else None)

    @app.get(P + "/contents/{cid}", dependencies=auth)
    def content(cid: str, who: str = Depends(worker)) -> dict:
        return svc.content(_id(cid))

    @app.post(P + "/contents/{cid}/approve", dependencies=auth)
    def approve_content(cid: str, req: dict = Depends(body(m.Approve)),
                        who: str = Depends(andre("contents/approve"))) -> dict:
        return svc.approve_content(_id(cid), req)

    @app.post(P + "/contents/{cid}/reject", dependencies=auth)
    def reject_content(cid: str, req: dict = Depends(body(m.RequestOnly)),
                       who: str = Depends(andre("contents/reject"))) -> dict:
        return svc.reject_content(_id(cid), req)

    @app.post(P + "/contents/{cid}/live", dependencies=auth)
    def content_live(cid: str, req: dict = Depends(body(m.ContentLive)), who: str = Depends(worker)) -> dict:
        return svc.content_live(who, _id(cid), req)

    @app.get(P + "/material-connections", dependencies=auth)
    def material(influencer_id: Optional[str] = Query(default=None, max_length=50), who: str = Depends(auditor)):
        return svc.material_view(_id(influencer_id) if influencer_id is not None else None)

    # ------------------------------------------------------------------ payees and payouts

    @app.post(P + "/tax-profiles", dependencies=auth, status_code=201)
    def tax_profile(req: dict = Depends(body(m.TaxProfile)), who: str = Depends(caller("hub"))) -> dict:
        return svc.record_tax_profile(who, req)

    @app.post(P + "/payees/{iid}/verify", dependencies=auth)
    def verify_payee(iid: str, req: dict = Depends(body(m.RequestOnly)),
                     who: str = Depends(caller("dashboard", "influencer_agent", "scheduler"))) -> dict:
        return svc.verify_payee(who, _id(iid), req)

    @app.post(P + "/payouts", dependencies=auth, status_code=201)
    def payout(req: dict = Depends(body(m.PayoutRequest)), who: str = Depends(worker)) -> dict:
        return svc.request_payout(who, req)

    @app.post(P + "/payouts/{pid}/approve", dependencies=auth)
    def approve_payout(pid: str, req: dict = Depends(body(m.Approve)), who: str = Depends(andre("payouts/approve"))):
        return svc.decide_payout(_id(pid), req, approve=True)

    @app.post(P + "/payouts/{pid}/reject", dependencies=auth)
    def reject_payout(pid: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(andre("payouts/reject"))):
        return svc.decide_payout(_id(pid), req, approve=False)

    @app.get(P + "/payouts", dependencies=auth)
    def payouts(status_: Optional[str] = Query(default=None, alias="status",
                                               pattern="^(pending_andre|pending_finance|submitted|refused_by_finance|cancelled)$"),
                who: str = Depends(worker)) -> list:
        return svc.payouts_view(status_)

    @app.get(P + "/payouts/{pid}", dependencies=auth)
    def payout_one(pid: str, who: str = Depends(worker)) -> dict:
        return svc.payout(_id(pid))

    # ------------------------------------------------------------------ jobs and audit

    @app.post(P + "/jobs/{name}/run", dependencies=auth)
    def run_job(name: str, req: dict = Depends(body(m.RequestOnly)), who: str = Depends(caller("scheduler"))) -> dict:
        if name not in JOBS:
            raise Invalid(R("JOB_UNKNOWN"))
        return svc.run_job(name, req)

    @app.get(P + "/audit/integrity", dependencies=auth)
    def integrity(who: str = Depends(auditor)) -> dict:
        return svc.audit_integrity()

    @app.get(P + "/audit/export", dependencies=auth)
    def audit_export(since: int = Query(default=1, ge=1, le=10_000_000), limit: int = Query(default=200, ge=1, le=1000),
                     who: str = Depends(auditor)) -> dict:
        return svc.audit_export(since, limit)

    return app


def _wrap(app: FastAPI):
    return InputLimits(NoStore(app))


def build(env: Optional[dict] = None, ports: Optional[Ports] = None):
    """The production wiring: settings (which take the data-directory flock), the claim, ledger, log, ports; returns
    (asgi, service)."""
    settings = config_mod.load(env)
    lock = settings.data_dir_lock
    token = lock.claim() if lock is not None else None     # service-py V5r-L1: claimed BEFORE the log is opened
    try:
        if settings.ledger_url and settings.ledger_token:
            ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
        else:
            ledger = UnconfiguredLedgerClient()
        svc = InfluencerService(settings, Recorder(ledger), RecordLog(settings.data_dir), ports or Ports.default(),
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
