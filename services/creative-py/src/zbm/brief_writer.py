"""
ZBM intelligence 1 — Brief Writer.

Job: DRAFT the 15-field brief from structured client requirements, written
for the maker (Enigma / Phantom Canvas).
Decides: nothing final. It assembles, asks for what's missing, attaches
the registry rows each deliverable is produced to, and lists the issues it
can see. It can never approve (Creative Lead does; drafter != approver is
enforced in creative_lead and the workflow).

Rules:
- a missing requirement becomes an OPEN QUESTION for the client; the
  writer never invents a field value;
- the `insight` comes only from Audience Insight's selection over the
  evidence supplied;
- every deliverable is checked against the registry by Placement Spec;
  the row ids are attached so the maker sees the sourced spec.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, ValidationError

from shared.registry import PlatformRulesRegistry
from shared.types import ClientId
from zbm.audience_insight import InsightCandidate, select_insight
from zbm.brief import BRIEF_FIELD_NAMES, BriefFields, BriefRecord, BriefStatus, Deliverable, RightsNeed, SuccessMetric, field_issues
from zbm.placement_spec import check_deliverable_spec

WRITER_ACTOR = "zbm_brief_writer"


class ClientRequirements(BaseModel):
    """What the client (via account management) supplies. Every brief
    field is optional HERE so the writer can report what's missing."""

    model_config = ConfigDict(extra="forbid")

    client_id: ClientId
    objective: str | None = None
    audience: str | None = None
    key_message: str | None = None
    deliverables: list[Deliverable] | None = None
    mandatories: list[str] | None = None
    approvers: list[str] | None = None
    distribution: list[str] | None = None
    deadline: date | None = None
    disclosure_requirements: list[str] | None = None
    hook: str | None = None
    success_in_numbers: list[SuccessMetric] | None = None
    insight_candidates: list[InsightCandidate] = []
    tone_of_voice: str | None = None
    rights_and_permissions: list[RightsNeed] | None = None
    transformation_plan: str | None = None


def _maker_summary(f: BriefFields) -> str:
    specs = "; ".join(
        f"{d.count}x {d.length_seconds}s {d.aspect_ratio} {d.format} for {d.platform}/{d.placement}"
        for d in f.deliverables
    )
    mand = "; ".join(f.mandatories) if f.mandatories else "none"
    return (
        f"MAKE: {specs}. SAY ONE THING: {f.key_message} OPEN WITH: {f.hook} "
        f"TRUTH: {f.insight} TONE: {f.tone_of_voice}. MUST INCLUDE: {mand}. "
        f"DISCLOSE: {'; '.join(f.disclosure_requirements)}. TRANSFORM: {f.transformation_plan}"
    )


def draft(
    req: ClientRequirements, brief_id: str, registry: PlatformRulesRegistry, today: date, drafted_by: str = WRITER_ACTOR
) -> BriefRecord:
    selection = select_insight(req.insight_candidates)
    data = {name: getattr(req, name, None) for name in BRIEF_FIELD_NAMES if name != "insight"}
    data["insight"] = selection.insight

    open_questions = []
    for name in BRIEF_FIELD_NAMES:
        value = data.get(name)
        if value is None or (isinstance(value, (str, list)) and len(value) == 0 and name != "mandatories"):
            if name == "insight":
                open_questions.append(f"insight: {selection.reason}")
            else:
                open_questions.append(f"{name}: not supplied by the client")

    record = BriefRecord(brief_id=brief_id, client_id=req.client_id, status=BriefStatus.INCOMPLETE,
                         drafted_by=drafted_by, open_questions=open_questions)
    if open_questions:
        return record

    try:
        fields = BriefFields.model_validate(data)
    except ValidationError as exc:
        record.issues = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return record

    issues = field_issues(fields, today)
    warnings: list[str] = []
    row_ids: dict[str, list[str]] = {}
    for d in fields.deliverables:
        sc = check_deliverable_spec(d, registry, today)
        row_ids[d.deliverable_id] = sc.row_ids
        issues += sc.issues
        warnings += sc.warnings
    return record.model_copy(update={
        "status": BriefStatus.DRAFT,
        "fields": fields,
        "issues": issues,
        "warnings": warnings,
        "spec_row_ids": row_ids,
        "maker_summary": _maker_summary(fields),
    })
