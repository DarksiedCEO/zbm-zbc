"""
ZBC intelligence 1 — Campaign Rulebook.

Job: check and APPROVE the rulebook the Rulebook Writer drafted. Andre then
signs it (founder token, in the workflow).
Decides: approved / sent back, with every reason.

Guardrails, checked first and in this order:
1. the approver is not the drafter (self-approval refused, even when one
   identity legitimately holds both roles);
2. the approver holds the Campaign Rulebook role;
3. only a DRAFT can be reviewed (a live rulebook is frozen).

Substantive checks (all must hold):
R1  no blocking issues left by the writer (e.g. an unverified platform);
R2  every required section is present: on-brief (OB), disclosure (DC),
    platforms (PF), a length rule (SP) for every target platform,
    originality/transform (OR), watermark (OW), quality floor (QF),
    rights (RC), minimum days live (MD); at least one must-say (MS);
R3  every registry row a rule rests on is usable TODAY (re-checked, not
    trusted from drafting time);
R4  no phrase is both must-say and never-say;
R5  no approved hook line contains a never-say phrase;
R6  every approved angle has at least one keyword (so on-brief can be judged).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from shared.actors import ActorRegistry, Role, require_not_self
from shared.errors import PreconditionFailed
from shared.registry import PlatformRulesRegistry
from shared.text import contains_phrase, mentions_phrase
from zbc.platform_rules import rows_usable
from zbc.rulebook import Rulebook, RuleKind, RulebookStatus

REQUIRED_SINGLE = (
    RuleKind.ON_BRIEF, RuleKind.DISCLOSURE, RuleKind.PLATFORM, RuleKind.ORIGINALITY_TRANSFORM,
    RuleKind.ORIGINALITY_WATERMARK, RuleKind.QUALITY_FLOOR, RuleKind.RIGHTS_CLEARED_ONLY, RuleKind.MIN_DAYS_LIVE,
)


@dataclass(frozen=True)
class RulebookReview:
    outcome: Literal["approved", "sent_back"]
    issues: list[str] = field(default_factory=list)


def review(rb: Rulebook, approver_id: str, actors: ActorRegistry, registry: PlatformRulesRegistry,
           today: date) -> RulebookReview:
    require_not_self(rb.drafted_by, approver_id, "rulebook")
    actors.require_role(approver_id, Role.ZBC_CAMPAIGN_RULEBOOK)
    if rb.status is not RulebookStatus.DRAFT:
        raise PreconditionFailed(f"rulebook {rb.campaign_id} v{rb.version} is {rb.status.value}; only a draft can be reviewed")

    issues: list[str] = [f"R1 blocking: {b}" for b in rb.blocking_issues]
    for kind in REQUIRED_SINGLE:
        if not rb.rules_of(kind):
            issues.append(f"R2 missing section: {kind.value}")
    if not rb.rules_of(RuleKind.MUST_SAY):
        issues.append("R2 missing section: must_say (at least one)")
    sp_targets = {(r.params.get("platform"), r.params.get("placement")) for r in rb.rules_of(RuleKind.SPEC_LENGTH)}
    for p in rb.platforms:
        if (p.platform, p.placement) not in sp_targets:
            issues.append(f"R2 missing length rule for {p.platform}/{p.placement}")
    for r in rb.rules:
        for reason in rows_usable(registry, r.rationale_row_ids, today):
            issues.append(f"R3 {r.rule_id} rests on a row that can't be used: {reason}")
    must = [r.params.get("phrase", "") for r in rb.rules_of(RuleKind.MUST_SAY)]
    never = [(r.rule_id, r.params.get("phrase", "")) for r in rb.rules_of(RuleKind.NEVER_SAY)]
    for m in must:
        for rid, n in never:
            if contains_phrase(m, n) or contains_phrase(n, m):
                issues.append(f"R4 must-say {m!r} conflicts with never-say {rid} {n!r}")
    for a in rb.approved_angles:
        for line in a.approved_hook_lines:
            for rid, n in never:
                if mentions_phrase(line, n):
                    issues.append(f"R5 hook line {line!r} of {a.angle_id} breaks never-say {rid}")
        if not a.keywords:
            issues.append(f"R6 angle {a.angle_id} has no keywords")
    return RulebookReview("sent_back" if issues else "approved", issues)
