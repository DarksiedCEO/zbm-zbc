"""
ZBC intelligence 6 — Campaign Kit.

Job: make the kit that shows clippers the bar — 3 to 5 seed clip specs,
caption styles, overlays, templates, brand assets, do/don't examples — and
commission the seed clips from Enigma and Phantom Canvas through their
contract (interface only; both are Andre's external agents in separate
repos).
Decides: which moments become seed clips, with which angle, hook and
platform; which do/don't lines the kit carries.

Rules (deterministic):
K1  built only from the LIVE rulebook version, its Moment Map and hook sheets;
K2  seed count is 3..5; fewer usable moments than requested -> refused;
K3  a seed needs a moment with at least one usable hook and a platform it
    fits; seeds are chosen by score (desc), then start time (asc), one per
    moment; the platform rotates over the moment's fitting targets;
K4  every seed carries the rulebook's disclosure, must-say lines and the
    minimum transformation elements, citing rule ids;
K5  brand assets must be in the rulebook's rights rule (RC) allow-list;
K6  client "do" examples that contain a never-say phrase are refused;
K7  commissioning goes through CreativeAgentsPort; today's stand-in
    answers "not commissioned", so `seed_clips_produced` is false and the
    kit says so. Andre signs the kit SPEC (workflow); production of the
    seed clips themselves is an open item.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from shared.departments import CommissionRequest, CreativeAgentsPort
from shared.errors import PreconditionFailed, ValidationFailed
from shared.text import contains_phrase
from shared.types import NonEmptyStr, SafeId
from zbc.hook_angle import HookSheet
from zbc.rulebook import Rulebook, RuleKind, RulebookStatus
from zbc.source_mining import MomentMap

PREFERRED_ELEMENTS = ("original_commentary", "captions_added", "recut_edit", "added_context",
                      "graphics_overlay", "voiceover", "reaction")
AGENTS = ("enigma", "phantom_canvas")


class KitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed_count: int = Field(ge=3, le=5)
    caption_styles: list[NonEmptyStr] = Field(min_length=1)
    overlays: list[NonEmptyStr] = []
    templates: list[NonEmptyStr] = []
    brand_asset_ids: list[SafeId] = []
    do_examples: list[NonEmptyStr] = []
    dont_examples: list[NonEmptyStr] = []


class SeedClipSpec(BaseModel):
    seed_id: str
    moment_id: str
    start_seconds: float
    end_seconds: float
    angle_id: str
    hook: str
    platform: str
    placement: str
    max_length_seconds: int
    required_transformation_elements: list[str]
    disclosure: str
    must_say: list[str]
    cites_rule_ids: list[str]


class CampaignKit(BaseModel):
    kit_id: str
    campaign_id: str
    rulebook_version: int
    seeds: list[SeedClipSpec]
    caption_styles: list[str]
    overlays: list[str]
    templates: list[str]
    brand_asset_ids: list[str]
    do: list[str]
    dont: list[str]
    commissions: list[dict]
    seed_clips_produced: bool
    status: str = "draft"
    signed_by: str | None = None


def build(kit_id: str, rb: Rulebook, moment_map: MomentMap, sheets: list[HookSheet], req: KitRequest,
          agents: CreativeAgentsPort) -> CampaignKit:
    if rb.status is not RulebookStatus.LIVE:
        raise PreconditionFailed(f"K1 the kit is built from the LIVE rulebook; {rb.campaign_id} v{rb.version} is {rb.status.value}")
    if (moment_map.campaign_id, moment_map.rulebook_version) != (rb.campaign_id, rb.version):
        raise PreconditionFailed("K1 moment map was built under a different rulebook version")

    rc = rb.one(RuleKind.RIGHTS_CLEARED_ONLY)
    allowed = set(rc.params.get("allowed_asset_ids", [])) if rc else set()
    bad_assets = [a for a in req.brand_asset_ids if a not in allowed]
    never = [(r.rule_id, r.params.get("phrase", "")) for r in rb.rules_of(RuleKind.NEVER_SAY)]
    issues = [f"K5 brand asset {a} is not cleared in {rc.rule_id if rc else 'the rights rule'}" for a in bad_assets]
    for ex in req.do_examples:
        for rid, p in never:
            if contains_phrase(ex, p):
                issues.append(f"K6 'do' example {ex!r} breaks never-say {rid}")
    if issues:
        raise ValidationFailed("kit request breaks the rulebook", issues)

    hooks_by_moment: dict[str, list[tuple[str, str]]] = {}
    for s in sheets:
        for h in s.hooks:
            hooks_by_moment.setdefault(s.moment_id, []).append((s.angle_id, h))
    candidates = sorted(
        (m for m in moment_map.moments if hooks_by_moment.get(m.moment_id) and m.fits),
        key=lambda m: (-m.score, m.start_seconds, m.moment_id),
    )
    if len(candidates) < req.seed_count:
        raise PreconditionFailed(
            f"K2 only {len(candidates)} usable moment(s) with a hook and a fitting platform; the kit needs {req.seed_count}"
        )

    dc = rb.one(RuleKind.DISCLOSURE)
    ortr = rb.one(RuleKind.ORIGINALITY_TRANSFORM)
    must = rb.rules_of(RuleKind.MUST_SAY)
    min_el = int(ortr.params.get("min_elements", 1)) if ortr else 1
    sp = {f"{r.params['platform']}/{r.params['placement']}": r for r in rb.rules_of(RuleKind.SPEC_LENGTH)}
    seeds: list[SeedClipSpec] = []
    for i, m in enumerate(candidates[: req.seed_count]):
        angle_id, hook = hooks_by_moment[m.moment_id][0]
        target = m.fits[i % len(m.fits)]
        platform, placement = target.split("/", 1)
        cites = [r.rule_id for r in (dc, ortr, sp.get(target)) if r is not None] + [r.rule_id for r in must]
        seeds.append(SeedClipSpec(
            seed_id=f"{kit_id}.seed{i + 1}", moment_id=m.moment_id, start_seconds=m.start_seconds,
            end_seconds=m.end_seconds, angle_id=angle_id, hook=hook, platform=platform, placement=placement,
            max_length_seconds=int(sp[target].params["max_seconds"]) if target in sp else 0,
            required_transformation_elements=list(PREFERRED_ELEMENTS[:min_el]),
            disclosure=dc.params["any_of"][0] if dc else "",
            must_say=[r.params["phrase"] for r in must], cites_rule_ids=cites,
        ))

    do = [f"Say \"{r.params['phrase']}\" ({r.rule_id})" for r in must]
    if dc:
        do.append(f"Disclose with one of {', '.join(dc.params['any_of'])} or the paid-partnership label ({dc.rule_id})")
    if ortr:
        do.append(f"Transform: add at least {min_el} of {', '.join(ortr.params['allowed'])} ({ortr.rule_id})")
    md = rb.one(RuleKind.MIN_DAYS_LIVE)
    if md:
        do.append(f"Keep the clip live at least {md.params['days']} days ({md.rule_id})")
    do += list(req.do_examples)
    dont = [f"Don't say \"{p}\" ({rid})" for rid, p in never]
    if ortr:
        dont.append(f"Don't post a raw repost ({ortr.rule_id})")
    ow = rb.one(RuleKind.ORIGINALITY_WATERMARK)
    if ow:
        dont.append(f"Don't leave a third-party watermark ({ow.rule_id})")
    if rc:
        dont.append(f"Don't add music, faces or footage that aren't cleared ({rc.rule_id})")
    dont += list(req.dont_examples)

    commissions = []
    for s in seeds:
        for agent in AGENTS:
            receipt = agents.commission(CommissionRequest(f"{s.seed_id}.{agent}", "zbc", agent, s.model_dump()))
            commissions.append({"request_id": receipt.request_id, "agent": receipt.agent,
                                "commissioned": receipt.commissioned, "reason": receipt.reason,
                                "external_ref": receipt.external_ref})
    return CampaignKit(
        kit_id=kit_id, campaign_id=rb.campaign_id, rulebook_version=rb.version, seeds=seeds,
        caption_styles=list(req.caption_styles), overlays=list(req.overlays), templates=list(req.templates),
        brand_asset_ids=list(req.brand_asset_ids), do=do, dont=dont, commissions=commissions,
        seed_clips_produced=bool(commissions) and all(c["commissioned"] for c in commissions),
    )
