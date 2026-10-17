"""
REST surface for Out-of-Home & DOOH (15) (ADR 0019). The request-limit, no-store, bearer and caller blocks are
seo-py's (itself bizdev-py's / service-py's / security-py's / legal-py's), unchanged in behaviour:
- fail-closed bearer auth on every route except /health; the service refuses to start without OOH_SERVICE_TOKEN;
- caller identity from ``X-OOH-Caller-Token`` (OOH_CALLER_TOKENS; SHA-256 digests compared against EVERY configured
  token, no early exit); a wrong or absent caller token is a 403;
- a client's read-only view comes through the ``hub`` caller AND a tenant token (``X-OOH-Tenant-Token``,
  OOH_TENANT_TOKENS): it sees its own tenant only; another tenant's objects answer 404, exactly like objects that do
  not exist (no existence oracle);
- Andre's actions arrive through the ``dashboard`` caller AND carry ``X-Andre-Approval-Token`` (FounderGate,
  ``hmac.compare_digest``): client approval, funds verification, purchase authorization, submission (signing) and its
  outcome, the commercial model, the buy limit and the client sell price, confirming a hold, tenants, releasing a kill
  switch;
- personal-data keys anywhere in a body are refused 422 before it is parsed; every response is ``no-store``;
  /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless OOH_BIND_ADDR says otherwise; port 8510;
- request limits before any route: target 4 KiB (414), head 16 KiB (431), body 64 KiB (413), JSON only (415),
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
import policy
from errors import Forbidden, Invalid, NotFound, OohError
from founder import HEADER as FOUNDER_HEADER
from founder import FounderGate
from ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from reasons import R
from service import JOBS, OohService
from store import RecordLog

log = logging.getLogger("ooh.api")

CALLER_HEADER = "X-OOH-Caller-Token"
TENANT_HEADER = "X-OOH-Tenant-Token"
MAX_BODY_BYTES = 64 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120

# seo-py's FORBIDDEN_KEYS: refused anywhere in a body, at any depth. Bank account and routing numbers are among them:
# a funds verification carries the bank's transfer REFERENCE, never an account.
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


def _plain(status_code: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, **extra})


def json_shape_problem(body: bytes) -> Optional[str]:
    """Nesting depth and member count of a JSON text, counted outside strings (linear, no parsing)."""
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
    """Outermost ASGI middleware: refuse over-long targets and heads, bodies over the cap (by Content-Length and on
    the bytes received), non-JSON bodies and hostile JSON shapes, before any route sees them."""

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
        limit = MAX_BODY_BYTES
        too_large = _plain(413, f"request body larger than {limit} bytes; refused")
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
    """Every answer is marked not to be cached or stored anywhere."""

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


OOH_ID = re.compile(r"^ooh-[a-z]{3}-[0-9a-f]{40}$")
MARKET_RX = re.compile(r"^[a-z0-9-]{2,60}$")
VENDOR_RX = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
TENANT_RX = re.compile(config_mod.TENANT_ID.pattern)


def _id(value: str, rx: re.Pattern = OOH_ID) -> str:
    if not isinstance(value, str) or not rx.fullmatch(value):
        raise Invalid(R("INVALID"), field="path")
    return value


def create_app(service: OohService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM Out-of-Home & DOOH (15)", version="0.1.0", docs_url=None, redoc_url=None,
                  openapi_url=None, description="OOH-0 constitution and OOH-1 operating spine: campaigns, availability, "
                                                "price objects, prepaid funds, the purchase gate, retry-safe orders.")
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

    def andre_scoped(request: Request, tid: str, who: str = Depends(andre)) -> tuple[str, str]:
        if not TENANT_RX.fullmatch(tid):
            raise Invalid(R("INVALID"), field="path")
        svc.tenant_view(tid)
        return "andre", tid

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> dict:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            bad = forbidden_keys(payload)
            if bad:
                # checked BEFORE the model (whose extra=forbid would otherwise answer a generic extra_forbidden):
                # the caller learns this service never takes personal or account data (AEGIS L1)
                raise RequestValidationError([{"loc": ("body", b), "msg": "personal or account data is never taken "
                                               "here (no IP, device, birth date, id, card, bank account or phone)",
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

    @app.exception_handler(OohError)
    def _domain(_: Request, exc: OohError):
        content = {"detail": exc.reason, **exc.body}
        if exc.status_code == 503:
            # AEGIS L2: whether the call may still have taken effect (a ledger answer was lost and the pending line
            # will be rolled forward): never retry it with a new request id
            content["maybe"] = bool(getattr(exc, "maybe", False))
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(Exception)
    def _unexpected(_: Request, exc: Exception):
        log.error("unexpected %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    P = "/ooh/v1"
    T = P + "/tenants/{tid}"
    C = T + "/campaigns/{cid}"
    PREP = ("dashboard", "ooh_agent")                 # who may prepare work (never commit it)
    READ = ("dashboard", "ooh_agent", "compliance_38")

    # ------------------------------------------------------------------ health, status, roster

    @app.get("/health")
    def health():
        st = svc.health()["status"]                        # unauthenticated: ok, degraded or closed, nothing more
        if st == "closed":
            return JSONResponse(status_code=503, content={"status": st})
        return {"status": st}

    @app.get(P + "/status", dependencies=auth)
    def full_status(who: str = Depends(dashboard)) -> dict:
        return {**svc.health(), "andre_approvals_configured": gate.configured}

    @app.get(P + "/constitution", dependencies=auth)
    def constitution(who: str = Depends(caller("dashboard", "ooh_agent", "compliance_38"))) -> dict:
        return {"andre_roles": list(policy.ANDRE_ROLES), "agents_commit": False,
                "principal_agent_states": list(policy.PRINCIPAL_AGENT_STATES),
                "principal_agent_default": policy.PRINCIPAL_AGENT_DEFAULT, "funding_policy": policy.FUNDING_POLICY,
                "suggested_markup_pct": policy.SUGGESTED_MARKUP_PCT, "suggested_markup_applied": False,
                "price_types": list(policy.PRICE_TYPES), "evidence_classes": list(policy.EVIDENCE_CLASSES),
                "decisions": list(policy.DECISIONS), "decision_self_authorizes": False,
                "campaign_states": list(policy.CAMPAIGN_STATES), "exception_states": list(policy.EXCEPTION_STATES),
                "campaign_gated": list(policy.CAMPAIGN_GATED), "not_built": list(policy.CAMPAIGN_NOT_BUILT),
                "section_4_main": list(policy.SECTION_4_MAIN), "section_4_exception": list(policy.SECTION_4_EXCEPTION),
                "section_4_derived": list(policy.DERIVED_STATES), "beyond_section_4": dict(policy.BEYOND_SECTION_4),
                "exceptions_built": list(policy.EXCEPTIONS_BUILT),
                "availability_states": list(policy.AVAILABILITY_STATES),
                "availability_andre_only": list(policy.AVAILABILITY_ANDRE_ONLY),
                "hold_evidence_states": list(policy.HOLD_EVIDENCE_STATES),
                "hold_reject_reasons": list(policy.HOLD_REJECT_REASONS),
                "markets": [m[0] for m in policy.MARKETS], "readiness_flags": list(policy.READINESS_FLAGS),
                "readiness_gates": dict(policy.READINESS_GATES), "formats": list(policy.FORMATS),
                "proof_requirements": dict(policy.PROOF_REQUIREMENTS), "source_kinds": list(policy.SOURCE_KINDS),
                "source_kinds_not_connected": list(policy.SOURCE_KINDS_NOT_CONNECTED),
                "submission_states": list(policy.SUBMISSION_STATES)}

    @app.get(P + "/roster", dependencies=auth)
    def roster(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        r = policy.roster()
        return {"capabilities": r, "total": len(r), "initial_cell": [x["capability_id"] for x in r if x["initial_cell"]],
                "built": 0}

    # ------------------------------------------------------------------ tenants and kill switches

    @app.get(P + "/tenants", dependencies=auth)
    def tenants(who: str = Depends(dashboard)) -> list:
        return svc.tenants_view()

    @app.post(P + "/tenants", dependencies=auth, status_code=201)
    def create_tenant(who: str = Depends(andre), req: dict = Depends(body(m.TenantCreate))) -> dict:
        return svc.create_tenant(req)

    @app.get(T, dependencies=auth)
    def tenant(scope: tuple = Depends(scoped("dashboard", "ooh_agent", "hub", "compliance_38"))) -> dict:
        return svc.tenant_view(scope[1])

    @app.get(P + "/vendors", dependencies=auth)
    def vendors(who: str = Depends(caller("dashboard", "ooh_agent", "compliance_38"))) -> list:
        return svc.vendors_view()

    @app.post(P + "/vendors", dependencies=auth, status_code=201)
    def register_vendor(who: str = Depends(andre), req: dict = Depends(body(m.VendorCreate))) -> dict:
        return svc.register_vendor(req)

    # ------------------------------------------------------------------ OOH-2 registry (global; never the hub)

    @app.get(P + "/markets", dependencies=auth)
    def markets(who: str = Depends(caller(*READ))) -> list:
        return svc.markets_view()

    @app.get(P + "/markets/{mid}", dependencies=auth)
    def market(mid: str, who: str = Depends(caller(*READ))) -> dict:
        _id(mid, MARKET_RX)
        if mid not in svc.markets:
            raise NotFound(R("MARKET_UNKNOWN"))
        return svc.market_view(mid)

    @app.post(P + "/markets/{mid}/readiness", dependencies=auth)
    def readiness(request: Request, mid: str, req: dict = Depends(body(m.ReadinessSet)),
                  who: str = Depends(caller("dashboard", "ooh_agent", "compliance_38"))) -> dict:
        """MET is Andre's; NOT_MET (the safe direction) any preparer or Compliance."""
        return svc.set_readiness(who, _id(mid, MARKET_RX), req, andre=andre_if_presented(request))

    @app.post(P + "/sources", dependencies=auth, status_code=201)
    def record_source(req: dict = Depends(body(m.SourceRecord)), who: str = Depends(caller(*PREP))) -> dict:
        return svc.record_source(who, req)

    @app.get(P + "/sources/{sid}", dependencies=auth)
    def source(sid: str, who: str = Depends(caller(*READ))) -> dict:
        return svc.source_view(_id(sid))

    @app.post(P + "/faces", dependencies=auth, status_code=201)
    def register_face(req: dict = Depends(body(m.FaceCreate)), who: str = Depends(caller(*PREP))) -> dict:
        return svc.register_face(who, req)

    @app.get(P + "/faces", dependencies=auth)
    def faces(market: Optional[str] = Query(default=None, pattern=MARKET_RX.pattern),
              format: Optional[str] = Query(default=None, pattern=r"^[A-Z_]{3,40}$"),
              vendor: Optional[str] = Query(default=None, pattern=VENDOR_RX.pattern),
              vendor_face_ref: Optional[str] = Query(default=None, pattern=r"^[A-Za-z0-9._:-]{1,100}$"),
              who: str = Depends(caller(*READ))) -> list:
        return svc.faces_view(market, format, vendor, vendor_face_ref)

    @app.get(P + "/faces/{fid}", dependencies=auth)
    def face(fid: str, who: str = Depends(caller(*READ))) -> dict:
        return svc.face_view(_id(fid))

    @app.post(P + "/faces/{fid}/attributes", dependencies=auth)
    def face_attribute(fid: str, req: dict = Depends(body(m.AttributeSet)), who: str = Depends(caller(*PREP))):
        return svc.set_face_attribute(who, _id(fid), req)

    @app.post(P + "/faces/{fid}/avails", dependencies=auth, status_code=201)
    def face_avails(fid: str, req: dict = Depends(body(m.AvailsRecord)), who: str = Depends(caller(*PREP))):
        return svc.record_avails(who, _id(fid), req)

    @app.get(P + "/vendors/{vid}", dependencies=auth)
    def supplier(vid: str, who: str = Depends(caller(*READ))) -> dict:
        return svc.supplier_view(_id(vid, VENDOR_RX))

    @app.post(P + "/vendors/{vid}/attributes", dependencies=auth)
    def supplier_attribute(vid: str, req: dict = Depends(body(m.AttributeSet)), who: str = Depends(caller(*PREP))):
        return svc.set_supplier_attribute(who, _id(vid, VENDOR_RX), req)

    @app.get(P + "/inventory-port", dependencies=auth)
    def inventory_port(who: str = Depends(caller(*READ))) -> dict:
        return {"port": svc.inventory_port.name, "status": svc.inventory_port.status,
                "sources_accepted": list(policy.SOURCE_KINDS),
                "sources_not_connected": list(policy.SOURCE_KINDS_NOT_CONNECTED)}

    @app.post(P + "/inventory-port/sync", dependencies=auth)
    def inventory_port_sync(req: dict = Depends(body(m.PortSync)), who: str = Depends(caller(*PREP))) -> dict:
        return svc.sync_from_port(req["vendor"])

    @app.get(P + "/principal-agent", dependencies=auth)
    def principal_agent(who: str = Depends(caller("dashboard", "ooh_agent", "compliance_38"))) -> dict:
        return svc.pa_view()

    @app.post(P + "/principal-agent-determinations", dependencies=auth)
    def determine_pa(who: str = Depends(andre), req: dict = Depends(body(m.PrincipalAgentDetermination))) -> dict:
        return svc.determine_principal_agent(req)

    @app.get(P + "/kill-switches", dependencies=auth)
    def switches(who: str = Depends(caller("dashboard", "compliance_38"))) -> dict:
        with svc.lock:
            return svc.switch_view()

    @app.post(P + "/kill-switches", dependencies=auth)
    def set_switch(request: Request, req: dict = Depends(body(m.SwitchSet)),
                   who: str = Depends(caller("dashboard", "ooh_agent", "compliance_38"))) -> dict:
        return svc.set_switch(who, req, andre=andre_if_presented(request))

    # ------------------------------------------------------------------ campaigns (preparation: agents and dashboard)

    @app.post(T + "/campaigns", dependencies=auth, status_code=201)
    def create_campaign(req: dict = Depends(body(m.CampaignCreate)), scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.create_campaign(scope[0], scope[1], req)

    @app.get(C, dependencies=auth)
    def campaign(cid: str, scope: tuple = Depends(scoped(*READ, "hub"))) -> dict:
        return svc.campaign_view(scope[1], _id(cid), hub=scope[0] == "hub")

    @app.get(C + "/exposure", dependencies=auth)
    def exposure(cid: str, scope: tuple = Depends(scoped(*READ))) -> dict:
        with svc.lock:
            return svc.exposure(svc._campaign(scope[1], _id(cid)))

    @app.post(C + "/transitions", dependencies=auth)
    def transition(request: Request, cid: str, req: dict = Depends(body(m.Transition)),
                   scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.transition(scope[0], scope[1], _id(cid), req, andre=andre_if_presented(request))

    @app.post(C + "/inventory", dependencies=auth, status_code=201)
    def add_inventory(cid: str, req: dict = Depends(body(m.InventoryAdd)), scope: tuple = Depends(scoped(*PREP))):
        return svc.add_inventory(scope[0], scope[1], _id(cid), req)

    @app.get(C + "/inventory/{uid}", dependencies=auth)
    def unit(cid: str, uid: str, scope: tuple = Depends(scoped(*READ))) -> dict:
        return svc.unit_for(scope[1], _id(cid), _id(uid))

    @app.post(C + "/inventory/{uid}/availability", dependencies=auth)
    def availability(cid: str, uid: str, req: dict = Depends(body(m.AvailabilityMove)),
                     scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.move_availability(scope[0], scope[1], _id(cid), _id(uid), req)

    @app.post(C + "/inventory/{uid}/hold-evidence", dependencies=auth, status_code=201)
    def hold_evidence(cid: str, uid: str, req: dict = Depends(body(m.HoldEvidence)),
                      scope: tuple = Depends(scoped(*PREP))) -> dict:
        """The vendor's hold evidence, attached for Andre's decision (it confirms nothing)."""
        return svc.submit_hold_evidence(scope[0], scope[1], _id(cid), _id(uid), req)

    @app.post(C + "/inventory/{uid}/hold-decisions", dependencies=auth)
    def hold_decision(cid: str, uid: str, scope: tuple = Depends(andre_scoped),
                      req: dict = Depends(body(m.HoldDecision))) -> dict:
        """Andre's decision on one hold-evidence record: the only way a unit becomes HOLD_CONFIRMED."""
        return svc.decide_hold(scope[1], _id(cid), _id(uid), req)

    @app.post(C + "/inventory/{uid}/flight", dependencies=auth)
    def flight(cid: str, uid: str, req: dict = Depends(body(m.FlightSet)), scope: tuple = Depends(scoped(*PREP))):
        return svc.set_flight(scope[0], scope[1], _id(cid), _id(uid), req)

    @app.post(C + "/prices", dependencies=auth, status_code=201)
    def record_price(request: Request, cid: str, req: dict = Depends(body(m.PriceRecord)),
                     scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.record_price(scope[0], scope[1], _id(cid), req, andre=andre_if_presented(request))

    @app.post(C + "/brief", dependencies=auth)
    def brief(cid: str, req: dict = Depends(body(m.Brief)), scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.set_brief(scope[0], scope[1], _id(cid), req)

    @app.post(C + "/inventory/{uid}/rate-card-estimates", dependencies=auth, status_code=201)
    def rate_card_estimate(cid: str, uid: str, req: dict = Depends(body(m.Estimate)),
                           scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.estimate_from_rate_card(scope[0], scope[1], _id(cid), _id(uid), req)

    @app.get(C + "/plan", dependencies=auth)
    def plan(cid: str, scope: tuple = Depends(scoped(*READ))) -> dict:
        """The plan with vendor cost: preparers and Compliance only (never the hub)."""
        with svc.lock:
            return svc.plan(svc._campaign(scope[1], _id(cid)))

    @app.post(C + "/markup-evidence", dependencies=auth, status_code=201)
    def markup_evidence(cid: str, req: dict = Depends(body(m.MarkupEvidence)),
                        scope: tuple = Depends(scoped(*PREP))) -> dict:
        return svc.record_markup_evidence(scope[0], scope[1], _id(cid), req)

    # ------------------------------------------------------------------ Andre's gates

    @app.post(C + "/commercial-model", dependencies=auth)
    def commercial(cid: str, scope: tuple = Depends(andre_scoped), req: dict = Depends(body(m.CommercialModelSet))):
        return svc.set_commercial_model(scope[1], _id(cid), req)

    @app.post(C + "/client-approval", dependencies=auth)
    def client_approval(cid: str, scope: tuple = Depends(andre_scoped), req: dict = Depends(body(m.ClientApproval))):
        return svc.client_approve(scope[1], _id(cid), req)

    @app.post(C + "/funds-verifications", dependencies=auth)
    def funds(cid: str, scope: tuple = Depends(andre_scoped), req: dict = Depends(body(m.FundsVerify))) -> dict:
        return svc.verify_funds(scope[1], _id(cid), req)

    @app.post(C + "/funds-reconfirmations", dependencies=auth)
    def funds_reconfirm(cid: str, scope: tuple = Depends(andre_scoped),
                        req: dict = Depends(body(m.FundsReconfirm))) -> dict:
        return svc.reconfirm_funds(scope[1], _id(cid), req)

    @app.post(C + "/authorizations", dependencies=auth, status_code=201)
    def authorize(cid: str, scope: tuple = Depends(andre_scoped), req: dict = Depends(body(m.Authorize))) -> dict:
        return svc.authorize(scope[1], _id(cid), req)

    @app.get(C + "/authorizations/{aid}", dependencies=auth)
    def authorization(cid: str, aid: str, scope: tuple = Depends(scoped(*READ))) -> dict:
        return svc.authorization_for(scope[1], _id(cid), _id(aid))

    @app.post(C + "/authorizations/{aid}/submit", dependencies=auth)
    def submit(cid: str, aid: str, scope: tuple = Depends(andre_scoped), req: dict = Depends(body(m.Submit))) -> dict:
        return svc.submit(scope[1], _id(cid), _id(aid), req)

    @app.post(C + "/authorizations/{aid}/outcome", dependencies=auth)
    def outcome(cid: str, aid: str, scope: tuple = Depends(andre_scoped), req: dict = Depends(body(m.Outcome))) -> dict:
        return svc.record_outcome(scope[1], _id(cid), _id(aid), req)

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
    """The production wiring: settings, ledger, log; returns (asgi, service). Every port is NOT_CONNECTED
    (config.NOT_BUILT refuses start if one is selected)."""
    settings = config_mod.load(env)
    lock = settings.data_dir_lock              # the flock, taken by config.load before the log is opened
    token = lock.claim() if lock is not None else None   # claimed BEFORE the log is built
    try:
        if settings.ledger_url and settings.ledger_token:
            ledger = HttpLedgerClient(settings.ledger_url, settings.ledger_token)
        else:
            ledger = UnconfiguredLedgerClient()
        svc = OohService(settings, Recorder(ledger), RecordLog(settings.data_dir), lock_token=token)
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
