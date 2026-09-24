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
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import load_config
from guardrails import OutboundBlocked
from integrations.departments import Departments, InMemoryContractStorage
from integrations.revenue_recovery import HttpRevenueRecoveryClient, NotConfiguredRevenueRecovery
from intelligences import registry
from ledger import HttpLedgerClient, LedgerWriteError, UnconfiguredLedgerClient
from onboarding_schema import AccessGrantIn, ClipperApplication
from onboarding_schema import requests as rq
from redaction import install_log_scrubbing, scrub
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
    out = []
    for e in errors:
        out.append({
            "loc": [scrub(str(x)) if isinstance(x, str) else x for x in e.get("loc", ())],
            "msg": scrub(str(e.get("msg", ""))),
            "type": e.get("type"),
        })
    return out


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
    svc = service
    app.state.service = svc

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _sanitize_validation_errors(exc.errors())})

    @app.exception_handler(OnboardingError)
    async def _onboarding(_: Request, exc: OnboardingError):
        return JSONResponse(status_code=exc.status_code, content={"detail": scrub(exc.detail), **exc.body})

    @app.exception_handler(LedgerWriteError)
    async def _ledger(_: Request, exc: LedgerWriteError):
        log.error("ledger write failed; action refused: %s", exc)
        return JSONResponse(status_code=503, content={
            "detail": f"evidence ledger write failed ({scrub(str(exc))}); the action did not proceed",
            "proceeded": False,
        })

    @app.exception_handler(OutboundBlocked)
    async def _outbound(_: Request, exc: OutboundBlocked):
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

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "onboarding-py", "data_source": "non-live"}

    @app.get("/intelligences", dependencies=auth)
    def intelligences() -> list[dict]:
        return registry()

    # --- client lane (and ZBC brand lane front end) ---------------------------------

    @app.post("/onboarding/clients", dependencies=auth, status_code=201)
    def start(req: rq.StartClientRequest) -> dict:
        return svc.start_client(req)

    @app.get("/onboarding/clients/{client_id}", dependencies=auth)
    def view(client_id: str) -> dict:
        return svc.view(client_id)

    @app.post("/onboarding/clients/{client_id}/intake/facts", dependencies=auth)
    def facts(client_id: str, req: rq.FactsRequest) -> dict:
        return svc.add_facts(client_id, req)

    @app.post("/onboarding/clients/{client_id}/intake/documents", dependencies=auth)
    def documents(client_id: str, req: rq.DocumentRequest) -> dict:
        return svc.add_document(client_id, req)

    @app.post("/onboarding/clients/{client_id}/messages", dependencies=auth)
    def message(client_id: str, req: rq.MessageRequest) -> dict:
        return svc.message(client_id, req)

    @app.post("/onboarding/clients/{client_id}/recap", dependencies=auth)
    def recap(client_id: str) -> dict:
        return svc.recap(client_id)

    @app.post("/onboarding/clients/{client_id}/access/website-scan", dependencies=auth)
    def website_scan(client_id: str, req: rq.WebsiteScanRequest) -> dict:
        return svc.website_scan(client_id, req)

    @app.post("/onboarding/clients/{client_id}/access/grants", dependencies=auth)
    def grant(client_id: str, req: AccessGrantIn) -> dict:
        return svc.add_grant(client_id, req)

    @app.post("/onboarding/clients/{client_id}/access/credentials", dependencies=auth)
    def credentials(client_id: str) -> Any:
        # The request body is deliberately never read or parsed.
        return svc.offer_credential(client_id)

    @app.post("/onboarding/clients/{client_id}/audit", dependencies=auth)
    def audit(client_id: str, req: rq.AuditRequest) -> dict:
        return svc.audit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/plan", dependencies=auth)
    def plan(client_id: str, req: rq.PlanRequest) -> dict:
        return svc.plan(client_id, req)

    @app.post("/onboarding/clients/{client_id}/plan/choices", dependencies=auth)
    def plan_choice(client_id: str, req: rq.PlanChoiceRequest) -> dict:
        return svc.plan_choice(client_id, req)

    @app.post("/onboarding/clients/{client_id}/setup-plan", dependencies=auth)
    def setup_plan(client_id: str) -> dict:
        return svc.setup_plan(client_id)

    @app.post("/onboarding/clients/{client_id}/account-changes", dependencies=auth)
    def account_change(client_id: str, req: rq.AccountChangeRequest) -> dict:
        return svc.account_change(client_id, req)

    @app.post("/onboarding/clients/{client_id}/momentum", dependencies=auth)
    def momentum(client_id: str) -> dict:
        return svc.momentum(client_id)

    @app.post("/onboarding/clients/{client_id}/first-win", dependencies=auth)
    def first_win(client_id: str, req: rq.FirstWinRequest) -> dict:
        return svc.first_win(client_id, req)

    @app.post("/onboarding/clients/{client_id}/recommend-score", dependencies=auth)
    def recommend(client_id: str, req: rq.RecommendScoreRequest) -> dict:
        return svc.recommend_score_submit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/tick", dependencies=auth)
    def tick(client_id: str) -> dict:
        return svc.tick(client_id)

    @app.post("/onboarding/clients/{client_id}/issues/{issue_id}/outcome", dependencies=auth)
    def issue_outcome(client_id: str, issue_id: str, req: rq.IssueOutcomeRequest) -> dict:
        return svc.issue_outcome(client_id, issue_id, req)

    @app.post("/onboarding/clients/{client_id}/escalations/{escalation_id}/acknowledge", dependencies=auth)
    def ack(client_id: str, escalation_id: str) -> dict:
        return svc.acknowledge_escalation(client_id, escalation_id)

    @app.post("/onboarding/clients/{client_id}/escalations/{escalation_id}/resolve", dependencies=auth)
    def resolve(client_id: str, escalation_id: str, req: rq.EscalationResolveRequest) -> dict:
        return svc.resolve_escalation(client_id, escalation_id, req)

    @app.get("/onboarding/clients/{client_id}/health", dependencies=auth)
    def client_health(client_id: str) -> dict:
        return svc.health(client_id)

    @app.delete("/onboarding/clients/{client_id}/memory", dependencies=auth)
    def delete_memory(client_id: str) -> dict:
        return svc.delete_memory(client_id)

    @app.post("/onboarding/clients/{client_id}/exit", dependencies=auth)
    def exit_(client_id: str, req: rq.ExitRequest) -> dict:
        return svc.exit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/activate", dependencies=auth)
    def activate(client_id: str) -> dict:
        return svc.activate_client(client_id)

    @app.get("/onboarding/escalations", dependencies=auth)
    def escalations() -> list[dict]:
        return svc.list_escalations()

    # --- ZBC creator lane ---------------------------------------------------------------

    @app.post("/zbc/creators/applications", dependencies=auth, status_code=201)
    def apply(req: ClipperApplication) -> dict:
        return svc.apply_creator(req)

    @app.post("/zbc/creators/{creator_id}/w9", dependencies=auth)
    def w9(creator_id: str, req: rq.CreatorFlagRequest) -> dict:
        return svc.creator_flag(creator_id, "w9", req)

    @app.post("/zbc/creators/{creator_id}/disclosure-training", dependencies=auth)
    def training(creator_id: str, req: rq.CreatorFlagRequest) -> dict:
        return svc.creator_flag(creator_id, "disclosure_training", req)

    @app.post("/zbc/creators/{creator_id}/activate", dependencies=auth)
    def activate_creator(creator_id: str) -> dict:
        return svc.activate_creator(creator_id)

    @app.post("/zbc/creators/{creator_id}/payments", dependencies=auth)
    def payment(creator_id: str, req: rq.CreatorPaymentRequest) -> dict:
        return svc.creator_payment(creator_id, req)

    @app.post("/zbc/creators/{creator_id}/posts/check", dependencies=auth)
    def post_check(creator_id: str, req: rq.CaptionRequest) -> dict:
        return svc.creator_post_check(creator_id, req)

    # --- ZBC brand lane -------------------------------------------------------------------

    @app.post("/zbc/brands/{brand_id}/campaigns", dependencies=auth, status_code=201)
    def campaign(brand_id: str, req: rq.CampaignRequest) -> dict:
        return svc.plan_campaign(brand_id, req)

    @app.post("/zbc/brands/{brand_id}/campaigns/{campaign_id}/approve", dependencies=auth)
    def approve(brand_id: str, campaign_id: str, req: rq.CampaignApproveRequest) -> dict:
        return svc.approve_campaign(brand_id, campaign_id, req)

    @app.post("/zbc/brands/{brand_id}/campaigns/{campaign_id}/proving-result", dependencies=auth)
    def proving(brand_id: str, campaign_id: str, req: rq.ProvingResultRequest) -> dict:
        return svc.proving_result(brand_id, campaign_id, req)

    # --- playbook + learning loop ------------------------------------------------------------

    @app.get("/playbook", dependencies=auth)
    def playbook() -> dict:
        return svc.playbook_view()

    @app.post("/playbook/rules", dependencies=auth)
    def playbook_rule(req: rq.PlaybookRuleRequest) -> dict:
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
