"""
REST surface for Creative Production (ADR 0005). Thin marshaling around the
two workflows (`zbm.workflow`, `zbc.workflow`) and the shared registry /
rights records. Same discipline as services/fulfillment-py/src/api.py:

- fail-closed bearer auth on every route except /health; the service
  refuses to start without CREATIVE_SERVICE_TOKEN; a non-ASCII token is a
  401, never a 500 (hmac.compare_digest TypeError caught);
- /docs, /redoc, /openapi.json disabled;
- bind address 127.0.0.1 by default (see serve.py, CREATIVE_BIND_ADDR);
- every department that doesn't exist yet is a fail-closed stand-in, and
  the evidence ledger defaults to "not configured" (every decision refused)
  unless LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN are both set.

Andre's approvals need a SECOND secret in the `X-Andre-Approval-Token`
header (CREATIVE_ANDRE_APPROVAL_TOKEN), so API access alone can't sign as
Andre. Actor ids in request bodies are ASSERTED by the caller (one shared
service token; see ADR 0005 "honest gaps").
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from shared.actors import ActorRegistry
from shared.clock import Clock, SystemClock
from shared.departments import Departments
from shared.errors import (
    CreativeError,
    FounderApprovalRefused,
    FrozenError,
    GuardrailViolation,
    NotFound,
    PreconditionFailed,
    RegistryRowBlocked,
    ValidationFailed,
)
from shared.founder import FounderGate
from shared.ledger import (
    EvidenceRecorder,
    HttpLedgerClient,
    LedgerClient,
    LedgerRecordError,
    OutcomeNotRecorded,
    UnconfiguredLedgerClient,
)
from shared.registry import PlatformRulesRegistry, RegistryRow, check_usable, seeded_registry
from shared.rights import CampaignLicense, ClearanceRecord, RightsRegistry, record_clearance, record_license
from shared.types import BOUNDED_ID_PATTERN, MAX_RULEBOOK_VERSION, SAFE_ID_PATTERN
from zbc import platform_rules as zbc_platform_rules
from zbc.campaign_kit import KitRequest
from zbc.clip_review import BrokenRule, ClipSubmission
from zbc.creative_memory import ClipResult
from zbc.rights_clearance import DeclaredAsset
from zbc.rulebook_writer import WRITER_ACTOR as ZBC_WRITER, CampaignGoal
from zbc.source_mining import SourceMaterial
from zbc.workflow import DEFAULT_SUPERSEDED_GRACE_HOURS, HumanVerdict, ZbcWorkflow
from zbm import hook_retention
from zbm import placement_spec as zbm_placement_spec
from zbm.brief_writer import WRITER_ACTOR as ZBM_WRITER, ClientRequirements
from zbm.results import PerformanceResult
from zbm.workflow import WorkSubmission, ZbmWorkflow

FOUNDER_HEADER = "X-Andre-Approval-Token"

# Path ids are validated BEFORE any work (integration defect 2): a campaign
# id is at most 100 characters so every derived ledger subject
# ("{campaign_id}:v{version}") fits ledger-rust's 128; every other id uses
# the ledger's own subject_id rule. A bad id is a 422, never a 503.
CampaignIdPath = Annotated[str, Path(pattern=BOUNDED_ID_PATTERN)]
IdPath = Annotated[str, Path(pattern=SAFE_ID_PATTERN)]
VersionPath = Annotated[int, Path(ge=1, le=MAX_RULEBOOK_VERSION)]

_STATUS = {
    NotFound: 404,
    GuardrailViolation: 403,
    FounderApprovalRefused: 403,
    FrozenError: 409,
    PreconditionFailed: 409,
    RegistryRowBlocked: 409,
    ValidationFailed: 422,
}


def _load_required_token() -> str:
    token = os.environ.get("CREATIVE_SERVICE_TOKEN")
    if not token:
        raise RuntimeError(
            "CREATIVE_SERVICE_TOKEN is not set. This service refuses to start without an auth token "
            "configured (fail closed, not open). Set CREATIVE_SERVICE_TOKEN before starting creative-py."
        )
    return token


def grace_hours_from_env() -> int:
    """CREATIVE_SUPERSEDED_GRACE_HOURS: how long after a rulebook version is
    superseded a clip declaring it is still judged automatically (F13).
    Default 72. Anything but an integer 0..720 refuses to start."""
    raw = os.environ.get("CREATIVE_SUPERSEDED_GRACE_HOURS")
    if raw is None or raw == "":
        return DEFAULT_SUPERSEDED_GRACE_HOURS
    try:
        hours = int(raw)
    except ValueError:
        hours = -1
    if not 0 <= hours <= 720:
        raise RuntimeError(f"CREATIVE_SUPERSEDED_GRACE_HOURS must be an integer 0..720 hours, got {raw!r}")
    return hours


def ledger_from_env() -> LedgerClient:
    if os.environ.get("LEDGER_SERVICE_URL") and os.environ.get("LEDGER_SERVICE_TOKEN"):
        return HttpLedgerClient.from_env()
    return UnconfiguredLedgerClient()


# --- request bodies -----------------------------------------------------------------

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorIn(_In):
    actor_id: str = Field(min_length=1, max_length=64)


class RegistryWriteIn(_In):
    actor_id: str = Field(min_length=1, max_length=64)
    row: RegistryRow


class ClearanceIn(_In):
    actor_id: str
    record: ClearanceRecord


class LicenseIn(_In):
    actor_id: str
    license: CampaignLicense


class DraftBriefIn(_In):
    actor_id: str = ZBM_WRITER
    requirements: ClientRequirements


class QualityIn(_In):
    actor_id: str
    notes: list[str] = []


class EscalationIn(_In):
    decision: Literal["accept", "kill"]


class HookAdviceIn(_In):
    results: list[PerformanceResult]
    platform: str
    placement: str
    metric: str = hook_retention.DEFAULT_METRIC


class ZbmMemoryIn(_In):
    brief_id: str
    result: PerformanceResult


class DraftRulebookIn(_In):
    actor_id: str = ZBC_WRITER
    goal: CampaignGoal


class RightsCheckIn(_In):
    assets: list[DeclaredAsset]
    uses_ai_generative_fill: bool = False


class HumanReviewIn(_In):
    actor_id: str
    outcome: Literal["pass", "reject"]
    broken_rules: list[BrokenRule] = []
    note: str = Field(default="", max_length=2000)


# --- app factory ------------------------------------------------------------------------

def build_app(
    *,
    service_token: str,
    ledger: LedgerClient,
    founder_token: str | None = None,
    clock: Clock | None = None,
    departments: Departments | None = None,
    actors: ActorRegistry | None = None,
    registry: PlatformRulesRegistry | None = None,
    rights: RightsRegistry | None = None,
    superseded_grace_hours: int = DEFAULT_SUPERSEDED_GRACE_HOURS,
) -> FastAPI:
    if not service_token:
        raise RuntimeError("service token required (fail closed)")
    if not 0 <= superseded_grace_hours <= 720:
        raise RuntimeError("superseded_grace_hours must be 0..720")
    clock = clock or SystemClock()
    departments = departments or Departments()
    actors = actors or ActorRegistry()
    registry = registry if registry is not None else seeded_registry()
    rights = rights if rights is not None else RightsRegistry()
    recorder = EvidenceRecorder(ledger)
    founder = FounderGate.build(founder_token, service_token)
    common = dict(registry=registry, rights=rights, actors=actors, recorder=recorder, clock=clock,
                  departments=departments, founder=founder)
    zbm = ZbmWorkflow(**common)
    zbc = ZbcWorkflow(**common, superseded_grace_hours=superseded_grace_hours)
    lock = recorder.lock

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="missing or malformed Authorization header (expected: Bearer <token>)",
                                headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization.removeprefix("Bearer ")
        try:
            valid = hmac.compare_digest(supplied, service_token)
        except TypeError:
            # non-ASCII str: compare_digest raises; anything it can't compare is not the token (401, never 500)
            valid = False
        if not valid:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token",
                                headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(
        title="Creative Production (ZBM advertising + ZBC clipping agency)",
        version="0.1.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.zbm = zbm
    app.state.zbc = zbc
    app.state.registry = registry
    app.state.rights = rights
    app.state.recorder = recorder

    @app.exception_handler(CreativeError)
    async def _creative_error(_: Request, exc: CreativeError):
        code = next((c for cls, c in _STATUS.items() if isinstance(exc, cls)), 400)
        body = {"detail": exc.reason, "error": type(exc).__name__}
        if isinstance(exc, ValidationFailed):
            body["issues"] = exc.issues
        return JSONResponse(status_code=code, content=body)

    @app.exception_handler(LedgerRecordError)
    async def _ledger_error(_: Request, exc: LedgerRecordError):
        if isinstance(exc, OutcomeNotRecorded):
            return JSONResponse(status_code=503, content={
                "detail": f"decision PARTLY took effect: {exc}",
                "error": "OutcomeNotRecorded", "took_effect": "partial", "effect": exc.effect,
            })
        return JSONResponse(status_code=503, content={
            "detail": f"decision did NOT take effect: the evidence ledger record failed ({exc})",
            "error": "LedgerRecordError", "took_effect": False,
        })

    auth = [Depends(require_auth)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "creative-py", "department": "creative_production",
                "ledger_configured": not isinstance(ledger, UnconfiguredLedgerClient),
                "founder_token_configured": founder.configured}

    # --- shared: Platform Rules Registry ------------------------------------------------
    def _row_view(row: RegistryRow) -> dict:
        u = check_usable(row, clock.today())
        return {**row.model_dump(mode="json"), "usable": u.usable, "usability_reason": u.reason}

    @app.get("/registry/rows", dependencies=auth)
    def registry_rows() -> dict:
        return {"rows": [_row_view(r) for r in sorted(registry.rows.values(), key=lambda r: r.row_id)]}

    @app.get("/registry/rows/{row_id}", dependencies=auth)
    def registry_row(row_id: IdPath) -> dict:
        return _row_view(registry.get(row_id))

    @app.put("/registry/rows/{row_id}", dependencies=auth)
    def registry_write(row_id: IdPath, body: RegistryWriteIn) -> dict:
        if body.row.row_id != row_id:
            raise ValidationFailed("row_id in path and body differ", ["row_id"])
        from shared.actors import Role

        actor = actors.get(body.actor_id) if _actor_known(actors, body.actor_id) else None
        if actor is None:
            raise GuardrailViolation(f"unknown actor {body.actor_id!r}")
        with lock:
            if Role.REGISTRY_ZBM_PLACEMENT_SPEC in actor.roles:
                eid = zbm_placement_spec.write_spec_row(registry, recorder, actors, body.actor_id, body.row)
            elif Role.REGISTRY_ZBC_PLATFORM_RULES in actor.roles:
                eid = zbc_platform_rules.write_originality_row(registry, recorder, actors, body.actor_id, body.row)
            else:
                raise GuardrailViolation(f"actor {body.actor_id!r} owns no registry rows")
        return {"row": _row_view(registry.get(row_id)), "ledger_event_id": eid}

    # --- shared: rights records ------------------------------------------------------------
    @app.post("/rights/clearances", status_code=201, dependencies=auth)
    def add_clearance(body: ClearanceIn) -> dict:
        with lock:
            eid = record_clearance(rights, recorder, actors, body.actor_id, body.record)
        return {"record": body.record.model_dump(mode="json"), "ledger_event_id": eid}

    @app.post("/rights/licenses", status_code=201, dependencies=auth)
    def add_license(body: LicenseIn) -> dict:
        with lock:
            eid = record_license(rights, recorder, actors, body.actor_id, body.license)
        return {"license": body.license.model_dump(mode="json"), "ledger_event_id": eid}

    # --- ZBM -----------------------------------------------------------------------------------
    @app.post("/zbm/briefs", status_code=201, dependencies=auth)
    def zbm_draft(body: DraftBriefIn) -> dict:
        return zbm.draft_brief(body.requirements, body.actor_id).model_dump(mode="json")

    @app.get("/zbm/briefs/{brief_id}", dependencies=auth)
    def zbm_get_brief(brief_id: IdPath) -> dict:
        return zbm.get_brief(brief_id).model_dump(mode="json")

    @app.post("/zbm/briefs/{brief_id}/review", dependencies=auth)
    def zbm_review(brief_id: IdPath, body: ActorIn) -> dict:
        return zbm.review_brief(brief_id, body.actor_id).model_dump(mode="json")

    @app.post("/zbm/briefs/{brief_id}/jobs", status_code=201, dependencies=auth)
    def zbm_open_job(brief_id: IdPath) -> dict:
        return zbm.open_job(brief_id).model_dump(mode="json")

    @app.post("/zbm/jobs/{job_id}/work", status_code=201, dependencies=auth)
    def zbm_submit(job_id: IdPath, body: WorkSubmission) -> dict:
        return zbm.submit_work(job_id, body).model_dump(mode="json")

    @app.get("/zbm/work/{work_id}", dependencies=auth)
    def zbm_get_work(work_id: IdPath) -> dict:
        return zbm.get_work(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/export-validation", dependencies=auth)
    def zbm_export(work_id: IdPath) -> dict:
        return zbm.validate_export(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/rights", dependencies=auth)
    def zbm_rights(work_id: IdPath) -> dict:
        return zbm.check_rights(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/quality", dependencies=auth)
    def zbm_quality(work_id: IdPath, body: QualityIn) -> dict:
        return zbm.quality_review(work_id, body.actor_id, body.notes).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/escalation", dependencies=auth)
    def zbm_escalation(work_id: IdPath, body: EscalationIn,
                       x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbm.resolve_escalation(work_id, x_andre_approval_token, body.decision).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/compliance", dependencies=auth)
    def zbm_compliance(work_id: IdPath) -> dict:
        return zbm.compliance_gate(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/final-approval", dependencies=auth)
    def zbm_final(work_id: IdPath, x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbm.final_approval(work_id, x_andre_approval_token).model_dump(mode="json")

    @app.post("/zbm/hook-advice", dependencies=auth)
    def zbm_hook_advice(body: HookAdviceIn) -> dict:
        rep = hook_retention.advise(body.results, body.platform, body.placement, body.metric)
        return {"platform": rep.platform, "placement": rep.placement, "metric": rep.metric, "note": rep.note,
                "ranked": [a.__dict__ for a in rep.ranked], "excluded": rep.excluded}

    @app.post("/zbm/memory/results", dependencies=auth)
    def zbm_memory(body: ZbmMemoryIn) -> dict:
        d = zbm.learn_result(body.result, body.brief_id)
        return {"learned": d.learned, "reason": d.reason}

    # --- ZBC -------------------------------------------------------------------------------------
    def _rb(campaign_id: str, version: int) -> dict:
        return zbc.rulebooks.get(campaign_id, version).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks", status_code=201, dependencies=auth)
    def zbc_draft(campaign_id: CampaignIdPath, body: DraftRulebookIn) -> dict:
        if body.goal.campaign_id != campaign_id:
            raise ValidationFailed("campaign_id in path and goal differ", ["campaign_id"])
        return zbc.draft_rulebook(body.goal, body.actor_id).model_dump(mode="json")

    @app.get("/zbc/campaigns/{campaign_id}/rulebooks", dependencies=auth)
    def zbc_versions(campaign_id: CampaignIdPath) -> dict:
        return {"versions": [rb.model_dump(mode="json") for rb in zbc.rulebooks.versions(campaign_id)]}

    @app.get("/zbc/campaigns/{campaign_id}/rulebooks/{version}", dependencies=auth)
    def zbc_get_rb(campaign_id: CampaignIdPath, version: VersionPath) -> dict:
        return _rb(campaign_id, version)

    @app.put("/zbc/campaigns/{campaign_id}/rulebooks/{version}", dependencies=auth)
    def zbc_edit(campaign_id: CampaignIdPath, version: VersionPath, body: DraftRulebookIn) -> dict:
        return zbc.edit_rulebook(campaign_id, version, body.goal, body.actor_id).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks/{version}/review", dependencies=auth)
    def zbc_review(campaign_id: CampaignIdPath, version: VersionPath, body: ActorIn) -> dict:
        return zbc.review_rulebook(campaign_id, version, body.actor_id).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks/{version}/sign", dependencies=auth)
    def zbc_sign(campaign_id: CampaignIdPath, version: VersionPath, x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbc.sign_rulebook(campaign_id, version, x_andre_approval_token).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rights-check", dependencies=auth)
    def zbc_rights(campaign_id: CampaignIdPath, body: RightsCheckIn) -> dict:
        return zbc.check_rights(campaign_id, body.assets, body.uses_ai_generative_fill).as_dict()

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks/{version}/go-live", dependencies=auth)
    def zbc_go_live(campaign_id: CampaignIdPath, version: VersionPath) -> dict:
        return zbc.go_live(campaign_id, version).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/revisions", status_code=201, dependencies=auth)
    def zbc_revise(campaign_id: CampaignIdPath, body: DraftRulebookIn) -> dict:
        return zbc.revise_rulebook(campaign_id, body.goal, body.actor_id).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/moment-map", dependencies=auth)
    def zbc_moments(campaign_id: CampaignIdPath, body: SourceMaterial) -> dict:
        return zbc.build_moment_map(campaign_id, body).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/hook-sheets", dependencies=auth)
    def zbc_hooks(campaign_id: CampaignIdPath) -> dict:
        return {"sheets": [s.model_dump(mode="json") for s in zbc.build_hook_sheets(campaign_id)]}

    @app.post("/zbc/campaigns/{campaign_id}/kit", status_code=201, dependencies=auth)
    def zbc_kit(campaign_id: CampaignIdPath, body: KitRequest) -> dict:
        return zbc.build_kit(campaign_id, body).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/kit/sign", dependencies=auth)
    def zbc_kit_sign(campaign_id: CampaignIdPath, x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbc.sign_kit(campaign_id, x_andre_approval_token).model_dump(mode="json")

    @app.post("/zbc/clips", status_code=201, dependencies=auth)
    def zbc_submit(body: ClipSubmission) -> dict:
        return zbc.submit_clip(body).model_dump(mode="json")

    @app.get("/zbc/clips/{submission_id}", dependencies=auth)
    def zbc_get_clip(submission_id: IdPath) -> dict:
        return zbc.get_decision(submission_id).model_dump(mode="json")

    @app.post("/zbc/clips/{submission_id}/human-review", dependencies=auth)
    def zbc_human(submission_id: IdPath, body: HumanReviewIn) -> dict:
        verdict = HumanVerdict(outcome=body.outcome, broken_rules=body.broken_rules, note=body.note)
        return zbc.human_review(submission_id, body.actor_id, verdict).model_dump(mode="json")

    @app.post("/zbc/clips/{submission_id}/payout-eligibility", dependencies=auth)
    def zbc_eligibility(submission_id: IdPath) -> dict:
        return zbc.payout_eligibility(submission_id)

    @app.post("/zbc/memory/results", dependencies=auth)
    def zbc_memory(body: ClipResult) -> dict:
        d = zbc.learn_result(body)
        return {"learned": d.learned, "reason": d.reason}

    @app.get("/zbc/memory/winners", dependencies=auth)
    def zbc_winners(vertical: str, platform: str) -> dict:
        return {"winners": [w.model_dump(mode="json") for w in zbc.memory.winners(vertical, platform)]}

    return app


def _actor_known(actors: ActorRegistry, actor_id: str) -> bool:
    return actor_id in actors.actors


def _app_from_env() -> FastAPI:
    token = _load_required_token()
    return build_app(
        service_token=token,
        ledger=ledger_from_env(),
        founder_token=os.environ.get("CREATIVE_ANDRE_APPROVAL_TOKEN"),
        actors=ActorRegistry.from_env(),
        superseded_grace_hours=grace_hours_from_env(),
    )


app = _app_from_env()
