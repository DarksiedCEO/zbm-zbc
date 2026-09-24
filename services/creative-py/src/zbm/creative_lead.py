"""
ZBM intelligence 2 — Creative Lead.

Job: check and APPROVE the brief. Nothing enters production without a
brief this intelligence approved.
Decides: approved / sent back (with every reason).

Guardrails (checked before anything else, in this order):
1. the approver is not the drafter — even if the same identity legitimately
   holds both roles (self-approval is refused, never "reviewed");
2. the approver holds the Creative Lead role;
3. the brief is in a reviewable state.

Then the substantive check is re-run from scratch at approval time — the
writer's own issue list is not trusted, and a registry row that expired
since drafting blocks approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from shared.actors import ActorRegistry, Role, require_not_self
from shared.errors import PreconditionFailed
from shared.registry import PlatformRulesRegistry
from zbm.brief import BriefRecord, BriefStatus, field_issues
from zbm.placement_spec import check_deliverable_spec


@dataclass(frozen=True)
class BriefReview:
    outcome: Literal["approved", "sent_back"]
    issues: list[str] = field(default_factory=list)
    spec_row_ids: dict[str, list[str]] = field(default_factory=dict)


def review(
    brief: BriefRecord, approver_id: str, actors: ActorRegistry, registry: PlatformRulesRegistry, today: date
) -> BriefReview:
    require_not_self(brief.drafted_by, approver_id, "brief")
    actors.require_role(approver_id, Role.ZBM_CREATIVE_LEAD)
    if brief.status not in (BriefStatus.DRAFT, BriefStatus.INCOMPLETE):
        raise PreconditionFailed(f"brief {brief.brief_id} is {brief.status.value}; only drafts can be reviewed")

    if brief.fields is None:
        return BriefReview("sent_back", [f"open question — {q}" for q in brief.open_questions] + brief.issues
                           or ["brief has no fields"])

    issues = field_issues(brief.fields, today)
    row_ids: dict[str, list[str]] = {}
    for d in brief.fields.deliverables:
        sc = check_deliverable_spec(d, registry, today)
        row_ids[d.deliverable_id] = sc.row_ids
        issues += sc.issues
    if issues:
        return BriefReview("sent_back", issues, row_ids)
    return BriefReview("approved", [], row_ids)
