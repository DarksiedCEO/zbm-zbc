"""
REST surface for the Onboarding department (ADR 0004).

Same discipline as services/fulfillment-py and services/detection-py:
thin marshaling around the service layer, fail-closed bearer auth on every
route except /health (hmac.compare_digest wrapped in try/except TypeError,
so a non-ASCII token is a 401, never a 500), /docs, /redoc and
/openapi.json disabled, and ``python3 -m api`` binds 127.0.0.1 unless
ONBOARDING_BIND_ADDR says otherwise.

Honest defaults — every missing dependency is the fail-closed stand-in:
- ledger: HttpLedgerClient only if LEDGER_SERVICE_URL and
  LEDGER_SERVICE_TOKEN are both set; otherwise every action that needs a
  record is refused with 503 ``proceeded: false``.
- Revenue Recovery: HttpRevenueRecoveryClient only if DETECTION_SERVICE_URL
  and DETECTION_SERVICE_TOKEN are set; otherwise the audit answers 502.
- Compliance (38), Verification and Integrity, Billing, ZBC payouts,
  handoff targets, Andre's push channel, live platform checks, the vault:
  all stand-ins that answer "not allowed yet" / "not wired".
- Contract storage: stand-in holding nothing, unless an operator opts in to
  ONBOARDING_CONTRACT_STORAGE=in_memory (local demos only; terms are lost
  on restart and it is NOT the decided storage location).

Errors never echo request input: 422 bodies carry only field locations and
messages (no ``input``/``ctx``), unhandled exceptions are logged by type
only, and every log record is scrubbed for credentials.

Hostile-input limits (fix wave 4, R1 — a quadratic credential scanner held
the whole process for minutes, /health included):
- ``InputLimits`` (outermost ASGI middleware): request target (path + query)
  over ``max_request_target_bytes`` -> 414; body over ``max_body_bytes``
  (1 MiB) -> 413, from Content-Length and again on the bytes received.
- Request bodies are validated in a SYNC dependency, i.e. in the threadpool,
  never on the event loop; field lengths are checked before any credential
  scan (``Inbound`` validates ``mode="after"``), and the scans of one body
  run under ``redaction.scan_budget`` (a body not checked in time -> 422).
- /health is ``async`` and does no work, so it answers from the event loop
  even while every worker thread is busy.
- Log lines are cut to a bounded prefix before they are scrubbed.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from config import load_config
from guardrails import OutboundBlocked
from integrations.departments import Departments, InMemoryContractStorage
from integrations.revenue_recovery import HttpRevenueRecoveryClient, NotConfiguredRevenueRecovery
from intelligences import registry
from ledger import HttpLedgerClient, LedgerWriteAfterEffects, LedgerWriteError, UnconfiguredLedgerClient
from onboarding_schema import AccessGrantIn, ClipperApplication
from onboarding_schema import requests as rq
from redaction import ScanBudgetExceeded, cap_text, install_log_scrubbing, scan_budget, scrub, scrub_obj
from service import OnboardingError, OnboardingService

install_log_scrubbing()
log = logging.getLogger("onboarding.api")


def _load_required_token() -> str:
    token = os.environ.get("ONBOARDING_SERVICE_TOKEN")
    if not token:
        raise RuntimeError(
            "ONBOARDING_SERVICE_TOKEN is not set. This service refuses to start without an auth "
            "token configured (fail closed, not open). Set ONBOARDING_SERVICE_TOKEN to a shared "
            "secret before starting onboarding-py."
        )
    return token


def make_require_auth(required_token: str) -> Callable:
    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or malformed Authorization header (expected: Bearer <token>)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = authorization.removeprefix("Bearer ")
        try:
            valid = hmac.compare_digest(supplied, required_token)
        except TypeError:
            # Same confirmed finding as fulfillment-py / detection-py: a
            # non-ASCII str makes compare_digest raise; that is an invalid
            # token (401), never an unauthenticated 500.
            valid = False
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token", headers={"WWW-Authenticate": "Bearer"}
            )

    return require_auth


class _ScrubbedResponses:
    """Wraps the service so every public method's result is scrubbed."""

    def __init__(self, service: OnboardingService):
        self._svc = service

    def __getattr__(self, name: str):
        attr = getattr(self._svc, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        def call(*a, **kw):
            return scrub_obj(attr(*a, **kw))

        return call


def build_service_from_env(env: dict | None = None) -> OnboardingService:
    env = dict(os.environ) if env is None else env
    config = load_config(env)
    if env.get("LEDGER_SERVICE_URL") and env.get("LEDGER_SERVICE_TOKEN"):
        ledger = HttpLedgerClient(env["LEDGER_SERVICE_URL"], env["LEDGER_SERVICE_TOKEN"])
    else:
        ledger = UnconfiguredLedgerClient()
    if env.get("DETECTION_SERVICE_URL") and env.get("DETECTION_SERVICE_TOKEN"):
        rr = HttpRevenueRecoveryClient(env["DETECTION_SERVICE_URL"], env["DETECTION_SERVICE_TOKEN"])
    else:
        rr = NotConfiguredRevenueRecovery()
    depts = Departments()
    if env.get("ONBOARDING_CONTRACT_STORAGE") == "in_memory":
        depts.contracts = InMemoryContractStorage()
    return OnboardingService(config, ledger, rr, departments=depts, andre_approval_key=env.get("ONBOARDING_ANDRE_APPROVAL_KEY") or None)


def _sanitize_validation_errors(errors: list[dict]) -> list[dict]:
    # Every piece is cut to a bounded prefix before it is scrubbed (a
    # location can be a client-chosen dict key of any length).
    out = []
    for e in errors[:50]:
        out.append({
            "loc": [scrub(cap_text(str(x), 128)) if isinstance(x, str) else x for x in list(e.get("loc", ()))[:16]],
            "msg": scrub(cap_text(str(e.get("msg", "")), 1024)),
            "type": e.get("type"),
        })
    return out


def _plain_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


class InputLimits:
    """Outermost ASGI middleware (fix wave 4, R1): refuse an over-long
    request target (414) and an over-size body (413) before any route,
    parser or scanner sees them. The body cap is checked on Content-Length
    and again on the bytes actually received (chunked bodies too): the body
    is read here, at most ``max_body_bytes`` of it, and handed on in one
    piece — a body that goes past the cap is answered 413 at once, and the
    rest of it is never read."""

    def __init__(self, app, max_body_bytes: int, max_target_bytes: int):
        self.app = app
        self.max_body = max_body_bytes
        self.max_target = max_target_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        target = len(scope.get("raw_path") or scope.get("path", "").encode("utf-8", "surrogatepass")) + len(scope.get("query_string") or b"")
        if target > self.max_target:
            return await _plain_response(414, f"request target longer than {self.max_target} bytes; refused")(scope, receive, send)
        too_large = _plain_response(413, f"request body larger than {self.max_body} bytes; refused")
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                if not value.isdigit():
                    return await _plain_response(400, "invalid Content-Length")(scope, receive, send)
                if int(value) > self.max_body:
                    return await too_large(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # client went away
                return
            chunk = message.get("body") or b""
            size += len(chunk)
            if size > self.max_body:
                return await too_large(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body, replayed = b"".join(chunks), False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(service: OnboardingService, required_token: str) -> FastAPI:
    app = FastAPI(
        title="ZBM/ZBC Onboarding",
        description="NON-LIVE: no platform, vault, push channel or other department is wired; stand-ins fail closed.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    auth = [Depends(make_require_auth(required_token))]
    app.state.service = service
    budget_s = service.config.scan_budget_seconds

    def body(model: type[BaseModel], optional: bool = False) -> Callable:
        """The request body, validated IN THE THREADPOOL (a sync dependency;
        fix wave 4, R1). FastAPI validates declared body models on the event
        loop, so credential scanning there stalled every request, /health
        included. Field constraints run first, then the credential checks
        under the per-request scan budget."""

        def parse(payload: Any = Body(default=None)) -> Any:
            if payload is None and optional:
                return None
            with scan_budget(budget_s):
                try:
                    return model.model_validate(payload)
                except ValidationError as exc:
                    raise RequestValidationError(
                        [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
                    ) from None

        return parse
    # Output-side scrub (third credential layer, F10): every route's return
    # value passes through scrub_obj on its way out.
    svc = _ScrubbedResponses(service)

    # Exception handlers are SYNC (fix wave 4, R1): Starlette runs them in the
    # threadpool, so their scrubbing never runs on the event loop either.
    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _sanitize_validation_errors(exc.errors())})

    @app.exception_handler(ScanBudgetExceeded)
    def _scan_budget(_: Request, exc: ScanBudgetExceeded):
        log.warning("request body refused: credential checks exceeded the %.1fs budget", budget_s)
        return JSONResponse(status_code=422, content={"detail": [{
            "loc": ["body"], "type": "scan_budget_exceeded",
            "msg": "the request could not be checked for credentials within the time budget, so it was not accepted"}]})

    @app.exception_handler(OnboardingError)
    def _onboarding(_: Request, exc: OnboardingError):
        return JSONResponse(status_code=exc.status_code, content=scrub_obj({"detail": exc.detail, **exc.body}))

    @app.exception_handler(LedgerWriteAfterEffects)
    def _ledger_after_effects(_: Request, exc: LedgerWriteAfterEffects):
        # Honest partial result: outside effects already happened (each was
        # recorded before it was made); the record of a later step failed,
        # so nothing further happened. Never "did not proceed".
        log.error("ledger write failed after outside effects %s; stopped", exc.effects)
        return JSONResponse(status_code=503, content={
            "detail": (f"evidence ledger write failed ({scrub(str(exc))}) after these outside effects had already "
                       f"happened: {', '.join(exc.effects)}; nothing further was done"),
            "proceeded": True,
            "completed": False,
            "outside_effects_done": list(exc.effects),
        })

    @app.exception_handler(LedgerWriteError)
    def _ledger(_: Request, exc: LedgerWriteError):
        log.error("ledger write failed; action refused: %s", exc)
        return JSONResponse(status_code=503, content={
            "detail": f"evidence ledger write failed ({scrub(str(exc))}); the action did not proceed",
            "proceeded": False,
        })

    @app.exception_handler(OutboundBlocked)
    def _outbound(_: Request, exc: OutboundBlocked):
        return JSONResponse(status_code=409, content={"detail": "client-facing text blocked by a guardrail; nothing was sent",
                                                      "rule": scrub(str(exc))})

    @app.middleware("http")
    async def _unhandled(request: Request, call_next):
        # A middleware, not an Exception handler: Starlette re-raises after
        # running a 500 handler, and the server would then log the full
        # traceback — whose message may contain input. Here the exception
        # stops, and only its TYPE is logged.
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            log.error("unhandled error: %s", type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "internal error"})

    cfg = service.config
    app.add_middleware(InputLimits, max_body_bytes=cfg.max_body_bytes, max_target_bytes=cfg.max_request_target_bytes)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "onboarding-py", "data_source": "non-live"}

    @app.get("/intelligences", dependencies=auth)
    def intelligences() -> list[dict]:
        return registry()

    # --- client lane (and ZBC brand lane front end) ---------------------------------

    @app.post("/onboarding/clients", dependencies=auth, status_code=201)
    def start(req: rq.StartClientRequest = Depends(body(rq.StartClientRequest))) -> dict:
        return svc.start_client(req)

    @app.get("/onboarding/clients/{client_id}", dependencies=auth)
    def view(client_id: str) -> dict:
        return svc.view(client_id)

    @app.post("/onboarding/clients/{client_id}/intake/facts", dependencies=auth)
    def facts(client_id: str, req: rq.FactsRequest = Depends(body(rq.FactsRequest))) -> dict:
        return svc.add_facts(client_id, req)

    @app.post("/onboarding/clients/{client_id}/intake/documents", dependencies=auth)
    def documents(client_id: str, req: rq.DocumentRequest = Depends(body(rq.DocumentRequest))) -> dict:
        return svc.add_document(client_id, req)

    @app.post("/onboarding/clients/{client_id}/messages", dependencies=auth)
    def message(client_id: str, req: rq.MessageRequest = Depends(body(rq.MessageRequest))) -> dict:
        return svc.message(client_id, req)

    @app.post("/onboarding/clients/{client_id}/recap", dependencies=auth)
    def recap(client_id: str) -> dict:
        return svc.recap(client_id)

    @app.post("/onboarding/clients/{client_id}/access/website-scan", dependencies=auth)
    def website_scan(client_id: str, req: rq.WebsiteScanRequest = Depends(body(rq.WebsiteScanRequest))) -> dict:
        return svc.website_scan(client_id, req)

    @app.post("/onboarding/clients/{client_id}/access/grants", dependencies=auth)
    def grant(client_id: str, req: AccessGrantIn = Depends(body(AccessGrantIn))) -> dict:
        return svc.add_grant(client_id, req)

    @app.post("/onboarding/clients/{client_id}/access/credentials", dependencies=auth)
    def credentials(client_id: str) -> Any:
        # The request body is deliberately never read or parsed.
        return svc.offer_credential(client_id)

    @app.post("/onboarding/clients/{client_id}/audit", dependencies=auth)
    def audit(client_id: str, req: rq.AuditRequest = Depends(body(rq.AuditRequest))) -> dict:
        return svc.audit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/plan", dependencies=auth)
    def plan(client_id: str, req: rq.PlanRequest = Depends(body(rq.PlanRequest))) -> dict:
        return svc.plan(client_id, req)

    @app.post("/onboarding/clients/{client_id}/plan/choices", dependencies=auth)
    def plan_choice(client_id: str, req: rq.PlanChoiceRequest = Depends(body(rq.PlanChoiceRequest))) -> dict:
        return svc.plan_choice(client_id, req)

    @app.post("/onboarding/clients/{client_id}/setup-plan", dependencies=auth)
    def setup_plan(client_id: str) -> dict:
        return svc.setup_plan(client_id)

    @app.post("/onboarding/clients/{client_id}/account-changes", dependencies=auth)
    def account_change(client_id: str, req: rq.AccountChangeRequest = Depends(body(rq.AccountChangeRequest))) -> dict:
        return svc.account_change(client_id, req)

    @app.post("/onboarding/clients/{client_id}/momentum", dependencies=auth)
    def momentum(client_id: str) -> dict:
        return svc.momentum(client_id)

    @app.post("/onboarding/clients/{client_id}/first-win", dependencies=auth)
    def first_win(client_id: str, req: rq.FirstWinRequest = Depends(body(rq.FirstWinRequest))) -> dict:
        return svc.first_win(client_id, req)

    @app.post("/onboarding/clients/{client_id}/recommend-score", dependencies=auth)
    def recommend(client_id: str, req: rq.RecommendScoreRequest = Depends(body(rq.RecommendScoreRequest))) -> dict:
        return svc.recommend_score_submit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/tick", dependencies=auth)
    def tick(client_id: str) -> dict:
        return svc.tick(client_id)

    @app.post("/onboarding/clients/{client_id}/issues/{issue_id}/outcome", dependencies=auth)
    def issue_outcome(client_id: str, issue_id: str, req: rq.IssueOutcomeRequest = Depends(body(rq.IssueOutcomeRequest))) -> dict:
        return svc.issue_outcome(client_id, issue_id, req)

    @app.post("/onboarding/clients/{client_id}/escalations/{escalation_id}/acknowledge", dependencies=auth)
    def ack(client_id: str, escalation_id: str,
            req: Optional[rq.EscalationAckRequest] = Depends(body(rq.EscalationAckRequest, optional=True))) -> dict:
        # Andre-only: needs his approval token in the body; the shared
        # service token alone is 403 (F4).
        return svc.acknowledge_escalation(client_id, escalation_id, req)

    @app.post("/onboarding/clients/{client_id}/escalations/{escalation_id}/resolve", dependencies=auth)
    def resolve(client_id: str, escalation_id: str, req: rq.EscalationResolveRequest = Depends(body(rq.EscalationResolveRequest))) -> dict:
        return svc.resolve_escalation(client_id, escalation_id, req)

    @app.get("/onboarding/clients/{client_id}/health", dependencies=auth)
    def client_health(client_id: str) -> dict:
        return svc.health(client_id)

    @app.delete("/onboarding/clients/{client_id}/memory", dependencies=auth)
    def delete_memory(client_id: str) -> dict:
        return svc.delete_memory(client_id)

    @app.post("/onboarding/clients/{client_id}/exit", dependencies=auth)
    def exit_(client_id: str, req: rq.ExitRequest = Depends(body(rq.ExitRequest))) -> dict:
        return svc.exit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/activate", dependencies=auth)
    def activate(client_id: str) -> dict:
        return svc.activate_client(client_id)

    @app.get("/onboarding/escalations", dependencies=auth)
    def escalations() -> list[dict]:
        return svc.list_escalations()

    # --- ZBC creator lane ---------------------------------------------------------------

    @app.post("/zbc/creators/applications", dependencies=auth, status_code=201)
    def apply(req: ClipperApplication = Depends(body(ClipperApplication))) -> dict:
        return svc.apply_creator(req)

    @app.post("/zbc/creators/{creator_id}/w9", dependencies=auth)
    def w9(creator_id: str, req: rq.CreatorFlagRequest = Depends(body(rq.CreatorFlagRequest))) -> dict:
        return svc.creator_flag(creator_id, "w9", req)

    @app.post("/zbc/creators/{creator_id}/disclosure-training", dependencies=auth)
    def training(creator_id: str, req: rq.CreatorFlagRequest = Depends(body(rq.CreatorFlagRequest))) -> dict:
        return svc.creator_flag(creator_id, "disclosure_training", req)

    @app.post("/zbc/creators/{creator_id}/activate", dependencies=auth)
    def activate_creator(creator_id: str) -> dict:
        return svc.activate_creator(creator_id)

    @app.post("/zbc/creators/{creator_id}/payments", dependencies=auth)
    def payment(creator_id: str, req: rq.CreatorPaymentRequest = Depends(body(rq.CreatorPaymentRequest))) -> dict:
        return svc.creator_payment(creator_id, req)

    @app.post("/zbc/creators/{creator_id}/posts/check", dependencies=auth)
    def post_check(creator_id: str, req: rq.CaptionRequest = Depends(body(rq.CaptionRequest))) -> dict:
        return svc.creator_post_check(creator_id, req)

    # --- ZBC brand lane -------------------------------------------------------------------

    @app.post("/zbc/brands/{brand_id}/campaigns", dependencies=auth, status_code=201)
    def campaign(brand_id: str, req: rq.CampaignRequest = Depends(body(rq.CampaignRequest))) -> dict:
        return svc.plan_campaign(brand_id, req)

    @app.post("/zbc/brands/{brand_id}/campaigns/{campaign_id}/approve", dependencies=auth)
    def approve(brand_id: str, campaign_id: str, req: rq.CampaignApproveRequest = Depends(body(rq.CampaignApproveRequest))) -> dict:
        return svc.approve_campaign(brand_id, campaign_id, req)

    @app.post("/zbc/brands/{brand_id}/campaigns/{campaign_id}/proving-result", dependencies=auth)
    def proving(brand_id: str, campaign_id: str, req: rq.ProvingResultRequest = Depends(body(rq.ProvingResultRequest))) -> dict:
        return svc.proving_result(brand_id, campaign_id, req)

    # --- playbook + learning loop ------------------------------------------------------------

    @app.get("/playbook", dependencies=auth)
    def playbook() -> dict:
        return svc.playbook_view()

    @app.post("/playbook/rules", dependencies=auth)
    def playbook_rule(req: rq.PlaybookRuleRequest = Depends(body(rq.PlaybookRuleRequest))) -> dict:
        return svc.change_playbook(req)

    @app.get("/learning/proposals", dependencies=auth)
    def proposals() -> dict:
        return svc.propose_rules()

    return app


_REQUIRED_TOKEN = _load_required_token()
app = create_app(build_service_from_env(), _REQUIRED_TOKEN)


def main() -> None:
    import uvicorn

    host = os.environ.get("ONBOARDING_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("ONBOARDING_PORT", "8200"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
