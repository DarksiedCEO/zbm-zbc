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

Refused outright (422, fix wave 10, AEGIS round 9 N9-3): a rulebook with
more than MAX_NEVER_SAY never-say phrases — Clip Review's cost per clip
grows with the list (rulebook_writer.MAX_NEVER_SAY says how it was chosen);
since fix wave 11 (N10-4) also one with a phrase over MAX_NEVER_SAY_WORDS
words or MAX_NEVER_SAY_PHRASE_CHARS characters, or a list over
MAX_NEVER_SAY_CHARS characters in total (`never_say_over_caps`). Every
draft — first draft, revision or edit — is approved only here, so no path
skips the check. The draft is left as it is: nothing is truncated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from shared.actors import ActorRegistry, Role, require_not_self
from shared.errors import PreconditionFailed, ValidationFailed
from shared.registry import PlatformRulesRegistry
from shared.text import contains_phrase, mentions_phrase
from zbc.rulebook_writer import NeverSayEntry, never_say_over_caps, short_entry_warnings
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
    warnings: list[str] = field(default_factory=list)  # non-blocking (N3: short never-say entries)


def review(rb: Rulebook, approver_id: str, actors: ActorRegistry, registry: PlatformRulesRegistry,
           today: date) -> RulebookReview:
    require_not_self(rb.drafted_by, approver_id, "rulebook")
    actors.require_role(approver_id, Role.ZBC_CAMPAIGN_RULEBOOK)
    if rb.status is not RulebookStatus.DRAFT:
        raise PreconditionFailed(f"rulebook {rb.campaign_id} v{rb.version} is {rb.status.value}; only a draft can be reviewed")
    over = never_say_over_caps([r.params.get("phrase", "") for r in rb.rules_of(RuleKind.NEVER_SAY)])
    if over:
        # fix wave 10 (N9-3): the count; fix wave 11 (N10-4): each phrase's length and the list's total
        raise ValidationFailed(
            f"rulebook {rb.campaign_id} v{rb.version} cannot be approved: " + "; ".join(over)
            + " (Clip Review's cost per clip grows with the list). Nothing was dropped: shorten the list and "
            "submit a new draft", ["never_say"])

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
    fuzzy = {r.params.get("phrase", ""): bool(r.params.get("fuzzy")) for r in rb.rules_of(RuleKind.NEVER_SAY)}
    for a in rb.approved_angles:
        for line in a.approved_hook_lines:
            for rid, n in never:
                if mentions_phrase(line, n, fuzzy.get(n, False)):
                    issues.append(f"R5 hook line {line!r} of {a.angle_id} breaks never-say {rid}")
        if not a.keywords:
            issues.append(f"R6 angle {a.angle_id} has no keywords")
    warnings = [f"R7 {w}" for w in short_entry_warnings(
        [NeverSayEntry(phrase=n, fuzzy=fuzzy.get(n, False)) for _, n in never], {n: rid for rid, n in never})]
    return RulebookReview("sent_back" if issues else "approved", issues, warnings)
