"""
ZBC intelligence 1a — Rulebook Writer.

Job: DRAFT a campaign rulebook from the client's goal and source material.
Decides: nothing final. It turns the goal into written, numbered rules and
lists every blocking issue it can see. Campaign Rulebook (1) approves; a
different actor is enforced. Andre then signs.

Every rule gets a stable id (`XX-NN`, prefix = kind). Platform-dependent
rules cite the registry rows they rest on (`rationale_row_ids`); a target
platform with no usable length row or no usable originality row is a
BLOCKING issue (e.g. TikTok today: its only row is unverified).

Revision (`revise`): after go-live the rulebook is frozen, so a change is a
NEW version drafted from a full updated goal. Rule identity is by content
(kind + parameters): an unchanged rule keeps its id; a new or changed rule
gets the next unused number for its prefix; ids that disappear are
retired and never reused in this campaign.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.registry import PlatformRulesRegistry
from shared.types import CampaignId, NonEmptyStr, SafeId
from zbc.platform_rules import length_rows, originality_rows
from zbc.rulebook import (
    PREFIX,
    TRANSFORMATION_ELEMENTS,
    Angle,
    PlatformTarget,
    Rule,
    Rulebook,
    RuleKind,
    RulebookStatus,
    next_rule_number,
)

WRITER_ACTOR = "zbc_rulebook_writer"


class AngleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    description: NonEmptyStr
    keywords: list[NonEmptyStr] = Field(min_length=1)
    hook_lines: list[NonEmptyStr] = []


class CampaignGoal(BaseModel):
    """What the client funds and supplies. The writer never invents any of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: CampaignId
    client_id: SafeId
    vertical: NonEmptyStr
    objective: NonEmptyStr
    source_asset_ids: list[SafeId] = Field(min_length=1)
    cleared_asset_ids: list[SafeId] = []
    angles: list[AngleInput] = Field(min_length=1, max_length=20)
    must_say: list[NonEmptyStr] = []
    never_say: list[NonEmptyStr] = []
    disclosure_any_of: list[NonEmptyStr] = Field(min_length=1)
    platforms: list[PlatformTarget] = Field(min_length=1)
    min_days_live: int = Field(ge=1, le=365)
    min_transformation_elements: int = Field(default=2, ge=1, le=len(TRANSFORMATION_ELEMENTS))
    min_resolution_height_px: int = Field(default=720, ge=240, le=4320)
    campaign_max_length_seconds: int | None = Field(default=None, ge=3, le=3600)
    # Language of the campaign's customer-facing text. Only English rulebooks
    # exist in this build; Clip Review sends any clip whose text contains a
    # letter outside the Latin script to a human (N3).
    language: Literal["en"] = "en"


def _rule(kind: RuleKind, n: int, text: str, params: dict, rows: tuple[str, ...] = ()) -> Rule:
    return Rule(rule_id=f"{PREFIX[kind]}-{n:02d}", kind=kind, text=text, params=params, rationale_row_ids=rows)


def _build(goal: CampaignGoal, registry: PlatformRulesRegistry, today: date) -> tuple[list[Angle], list[Rule], list[str]]:
    blocking: list[str] = []
    angles = [
        Angle(angle_id=f"A{i:02d}", name=a.name, description=a.description,
              keywords=tuple(a.keywords), approved_hook_lines=tuple(a.hook_lines))
        for i, a in enumerate(goal.angles, start=1)
    ]
    counters: dict[RuleKind, int] = {}

    def nxt(kind: RuleKind) -> int:
        counters[kind] = counters.get(kind, 0) + 1
        return counters[kind]

    rules: list[Rule] = []
    rules.append(_rule(RuleKind.ON_BRIEF, nxt(RuleKind.ON_BRIEF),
                       "Every clip uses exactly one approved angle: "
                       + ", ".join(f"{a.angle_id} {a.name}" for a in angles) + ".",
                       {"angle_ids": [a.angle_id for a in angles]}))
    for phrase in dict.fromkeys(goal.must_say):
        rules.append(_rule(RuleKind.MUST_SAY, nxt(RuleKind.MUST_SAY), f"The clip must say: \"{phrase}\".", {"phrase": phrase}))
    for phrase in dict.fromkeys(goal.never_say):
        rules.append(_rule(RuleKind.NEVER_SAY, nxt(RuleKind.NEVER_SAY), f"The clip must never say: \"{phrase}\".", {"phrase": phrase}))
    rules.append(_rule(RuleKind.DISCLOSURE, nxt(RuleKind.DISCLOSURE),
                       "The caption carries a paid-partnership disclosure (one of: "
                       + ", ".join(goal.disclosure_any_of) + ") or the platform's paid-partnership label is on.",
                       {"any_of": list(goal.disclosure_any_of), "or_platform_label": True}))
    targets = [{"platform": p.platform, "placement": p.placement} for p in goal.platforms]
    rules.append(_rule(RuleKind.PLATFORM, nxt(RuleKind.PLATFORM),
                       "Post only to: " + ", ".join(f"{t['platform']}/{t['placement']}" for t in targets) + ".",
                       {"targets": targets}))

    originality_row_ids: list[str] = []
    watermark_row_ids: list[str] = []
    for p in goal.platforms:
        lr = length_rows(registry, p.platform, p.placement, today)
        if not lr.usable:
            why = "; ".join(lr.blocked) or "no length row in the Platform Rules Registry"
            blocking.append(f"{p.platform}/{p.placement}: no usable length rule ({why})")
        else:
            cap = min(int(r.value) for r in lr.usable)
            if goal.campaign_max_length_seconds is not None:
                cap = min(cap, goal.campaign_max_length_seconds)
            rules.append(_rule(RuleKind.SPEC_LENGTH, nxt(RuleKind.SPEC_LENGTH),
                               f"Clips for {p.platform}/{p.placement} are at most {cap} seconds.",
                               {"platform": p.platform, "placement": p.placement, "max_seconds": cap},
                               tuple(sorted(r.row_id for r in lr.usable))))
        orr = originality_rows(registry, p.platform, p.placement, today)
        if not orr.usable:
            why = "; ".join(orr.blocked) or "no originality row in the Platform Rules Registry"
            blocking.append(f"{p.platform}/{p.placement}: no usable originality rule ({why})")
        for r in orr.usable:
            originality_row_ids.append(r.row_id)
            if r.rule_key == "originality_repost_watermark":
                watermark_row_ids.append(r.row_id)

    rules.append(_rule(RuleKind.ORIGINALITY_TRANSFORM, nxt(RuleKind.ORIGINALITY_TRANSFORM),
                       f"Transform, don't repost: every clip adds at least {goal.min_transformation_elements} of: "
                       + ", ".join(sorted(TRANSFORMATION_ELEMENTS)) + ". A raw repost is always rejected.",
                       {"min_elements": goal.min_transformation_elements, "allowed": sorted(TRANSFORMATION_ELEMENTS)},
                       tuple(sorted(set(originality_row_ids)))))
    rules.append(_rule(RuleKind.ORIGINALITY_WATERMARK, nxt(RuleKind.ORIGINALITY_WATERMARK),
                       "No noticeable third-party watermark.", {}, tuple(sorted(set(watermark_row_ids)))))
    rules.append(_rule(RuleKind.QUALITY_FLOOR, nxt(RuleKind.QUALITY_FLOOR),
                       f"Minimum vertical resolution {goal.min_resolution_height_px}px.",
                       {"min_height_px": goal.min_resolution_height_px}))
    allowed_assets = sorted(set(goal.source_asset_ids) | set(goal.cleared_asset_ids))
    rules.append(_rule(RuleKind.RIGHTS_CLEARED_ONLY, nxt(RuleKind.RIGHTS_CLEARED_ONLY),
                       "Use only the campaign's licensed source footage and cleared assets (no other music, "
                       "likeness or footage).", {"allowed_asset_ids": allowed_assets}))
    rules.append(_rule(RuleKind.MIN_DAYS_LIVE, nxt(RuleKind.MIN_DAYS_LIVE),
                       f"Each clip stays live at least {goal.min_days_live} days.", {"days": goal.min_days_live}))
    return angles, rules, blocking


def draft(goal: CampaignGoal, registry: PlatformRulesRegistry, today: date, version: int = 1,
          drafted_by: str = WRITER_ACTOR) -> Rulebook:
    angles, rules, blocking = _build(goal, registry, today)
    return Rulebook(
        campaign_id=goal.campaign_id, client_id=goal.client_id, vertical=goal.vertical, version=version,
        status=RulebookStatus.DRAFT, objective=goal.objective, source_asset_ids=tuple(goal.source_asset_ids),
        approved_angles=tuple(angles), platforms=tuple(goal.platforms), rules=tuple(rules),
        blocking_issues=tuple(blocking), drafted_by=drafted_by, language=goal.language,
    )


def _content_key(rule: Rule) -> tuple[str, str]:
    return rule.kind.value, json.dumps(rule.params, sort_keys=True)


def revise(previous: Rulebook, goal: CampaignGoal, registry: PlatformRulesRegistry, today: date, version: int,
           drafted_by: str = WRITER_ACTOR) -> Rulebook:
    if goal.campaign_id != previous.campaign_id:
        raise ValueError("a revision must be for the same campaign")
    angles, fresh, blocking = _build(goal, registry, today)
    old_by_key = {_content_key(r): r for r in previous.rules}
    used = {r.rule_id for r in previous.rules} | set(previous.retired_rule_ids)
    kept_ids: set[str] = set()
    out: list[Rule] = []
    for r in fresh:
        old = old_by_key.get(_content_key(r))
        if old is not None:
            out.append(r.model_copy(update={"rule_id": old.rule_id}))
            kept_ids.add(old.rule_id)
        else:
            prefix = PREFIX[r.kind]
            n = next_rule_number(prefix, used)
            new_id = f"{prefix}-{n:02d}"
            used.add(new_id)
            out.append(Rule(rule_id=new_id, kind=r.kind, text=r.text, params=r.params,
                            rationale_row_ids=r.rationale_row_ids))
    retired = tuple(sorted(set(previous.retired_rule_ids) | ({r.rule_id for r in previous.rules} - kept_ids)))
    return Rulebook(
        campaign_id=goal.campaign_id, client_id=goal.client_id, vertical=goal.vertical, version=version,
        status=RulebookStatus.DRAFT, objective=goal.objective, source_asset_ids=tuple(goal.source_asset_ids),
        approved_angles=tuple(angles), platforms=tuple(goal.platforms), rules=tuple(out),
        retired_rule_ids=retired, blocking_issues=tuple(blocking), drafted_by=drafted_by,
        supersedes_version=previous.version, language=goal.language,
    )
