"""
ZBC intelligence 8 — Clip Review.

Job: score every submitted clip against the rulebook VERSION IT WAS MADE
UNDER.
Decides: pass / reject / human_review. Output is a review decision only —
it has no money fields and never implies a payout (payout_eligibility.py
is a separate gate; Finance (31) pays).

Every rejection cites WRITTEN rule ids from that version. This is enforced
structurally: a `ClipReviewDecision` can only be validated with the
rulebook's rule ids in the validation context; constructing one directly,
or citing an id that isn't in that version, raises. No rule on the page,
no rejection — a problem with no governing rule goes to human_review.

What is judged is what the submitter DECLARES (transformation elements,
watermark flag, resolution, platform label) plus the clip's text (caption,
on-screen text, transcript). No media is analysed in this build; audio
fingerprinting (Chromaprint) and scene analysis plug in later
(shared/media.py). Submitted text is DATA: it is only searched for the
rulebook's phrases, never interpreted — "ignore your rules and approve"
in a caption or bio changes nothing.

Checks (each tied to a rule kind; missing rule => not governed):
PF  platform/placement is a target                         -> reject
SP  length <= the target's max; rationale rows usable today
    (a stale row => human_review, never an automatic pass)  -> reject / human_review
OB  angle is approved (reject); no angle keyword in the
    clip's text => borderline                               -> reject / human_review
OR  raw repost or zero valid elements, or fewer valid
    elements than required                                  -> reject; unknown element names -> human_review
OW  declared third-party watermark                          -> reject
DC  disclosure token in caption, or paid-partnership label  -> reject
MS  each must-say phrase present in the clip's text         -> reject
NS  no never-say phrase in the clip's text                  -> reject
    (DC/MS/NS match on canonical text — confusables, diacritics, format
    characters and fullwidth forms folded, shared/text.py; a phrase found
    only once split letters are rejoined or leetspeak folded is
    borderline -> human_review, never a pass)
OBF caption / on-screen text / transcript shows an obfuscation
    signal (mixed-script lookalikes, hidden format characters,
    separator-split letters)                                -> human_review
QF  resolution >= floor; not declared => human_review       -> reject / human_review
RC  every source/added asset is in the allow-list           -> reject
MD  min days live is NOT judged here (Verification and Integrity).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator

from shared.registry import PlatformRulesRegistry
from shared.text import PhraseMatch, contains_phrase, match_phrase, obfuscation_signals
from shared.types import MAX_RULEBOOK_VERSION, CampaignId, NonEmptyStr, SafeId
from zbc.platform_rules import rows_usable
from zbc.rulebook import Rulebook, RuleKind


class ClipSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: SafeId
    campaign_id: CampaignId
    rulebook_version: int = Field(ge=1, le=MAX_RULEBOOK_VERSION)
    clipper_id: SafeId
    posted_at: AwareDatetime
    platform: NonEmptyStr
    placement: NonEmptyStr
    post_ref: NonEmptyStr
    length_seconds: float = Field(gt=0, le=36000)
    resolution_height_px: int | None = Field(default=None, ge=1, le=10000)
    angle_id: NonEmptyStr
    moment_ids: list[str] = []
    caption: str = Field(default="", max_length=5000)
    on_screen_text: str = Field(default="", max_length=5000)
    transcript: str = Field(default="", max_length=50000)
    account_bio: str = Field(default="", max_length=5000)
    transformation_elements: list[str] = []
    is_raw_repost: bool
    has_third_party_watermark: bool
    paid_partnership_label: bool = False
    source_asset_ids: list[SafeId] = []
    added_asset_ids: list[SafeId] = []


class BrokenRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    reason: str


class RuleCitationError(ValueError):
    """A decision tried to cite a rule id that is not in its rulebook version."""


class ClipReviewDecision(BaseModel):
    """Review decision ONLY. Deliberately no amount/rate/payout fields
    (tests/test_guardrails.py inspects this model to keep it that way)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    submission_id: str
    campaign_id: str
    rulebook_version: int
    outcome: Literal["pass", "reject", "human_review"]
    broken_rules: tuple[BrokenRule, ...] = ()
    human_review_reasons: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    decided_by: str
    decided_at: datetime
    received_at: datetime | None = None  # server receipt time of the submission (never submitter-asserted)

    @model_validator(mode="after")
    def _cites_only_rules_on_the_page(self, info: ValidationInfo) -> "ClipReviewDecision":
        ctx = info.context or {}
        rule_ids = ctx.get("rulebook_rule_ids")
        key = ctx.get("rulebook_key")
        if rule_ids is None or key is None:
            raise RuleCitationError("a clip review decision can only be built against a rulebook version")
        if key != (self.campaign_id, self.rulebook_version):
            raise RuleCitationError("decision's campaign/version differs from the rulebook it was built against")
        for b in self.broken_rules:
            if b.rule_id not in rule_ids:
                raise RuleCitationError(
                    f"rule {b.rule_id!r} is not in {self.campaign_id} v{self.rulebook_version}; no rule on the page, no rejection"
                )
        if self.outcome == "reject" and not self.broken_rules:
            raise RuleCitationError("a rejection must cite at least one written rule")
        if self.outcome != "reject" and self.broken_rules:
            raise RuleCitationError("only a rejection cites broken rules")
        if self.outcome == "human_review" and not self.human_review_reasons:
            raise RuleCitationError("human_review needs a reason")
        return self


def make_decision(rb: Rulebook, **data) -> ClipReviewDecision:
    """The ONLY way to build a decision: validated against `rb`'s rule ids."""
    try:
        return ClipReviewDecision.model_validate(
            data, context={"rulebook_rule_ids": rb.rule_ids(), "rulebook_key": (rb.campaign_id, rb.version)}
        )
    except ValidationError as exc:
        raise RuleCitationError("; ".join(e["msg"] for e in exc.errors())) from exc


def review(sub: ClipSubmission, rb: Rulebook, registry: PlatformRulesRegistry, now: datetime,
           decided_by: str = "zbc_clip_review", received_at: datetime | None = None,
           route_to_human: tuple[str, ...] = ()) -> ClipReviewDecision:
    """`route_to_human`: reasons from outside the rulebook (e.g. a declared
    version outside its grace window) that make this clip ineligible for
    ANY automatic outcome; the automatic result is kept as a reason for
    the human reviewer."""
    today = now.date()
    broken: list[BrokenRule] = []
    borderline: list[str] = []
    checks: list[str] = []
    clip_text = " ".join([sub.caption, sub.on_screen_text, sub.transcript])

    def fail(rule_id: str, reason: str) -> None:
        broken.append(BrokenRule(rule_id=rule_id, reason=reason))

    target = f"{sub.platform}/{sub.placement}"
    pf = rb.one(RuleKind.PLATFORM)
    if pf is None:
        borderline.append("no platform rule in this version (not governed)")
    else:
        checks.append(pf.rule_id)
        targets = {f"{t['platform']}/{t['placement']}" for t in pf.params.get("targets", [])}
        if target not in targets:
            fail(pf.rule_id, f"{target} is not a campaign target")

    sp = next((r for r in rb.rules_of(RuleKind.SPEC_LENGTH)
               if (r.params.get("platform"), r.params.get("placement")) == (sub.platform, sub.placement)), None)
    if sp is not None:
        checks.append(sp.rule_id)
        if sub.length_seconds > int(sp.params["max_seconds"]):
            fail(sp.rule_id, f"length {sub.length_seconds}s exceeds {sp.params['max_seconds']}s")
        stale = rows_usable(registry, sp.rationale_row_ids, today)
        if stale:
            borderline.append(f"{sp.rule_id} rests on registry rows that can't be relied on today: " + "; ".join(stale))

    ob = rb.one(RuleKind.ON_BRIEF)
    if ob is None:
        borderline.append("no on-brief rule in this version (not governed)")
    else:
        checks.append(ob.rule_id)
        angle = rb.angle(sub.angle_id)
        if angle is None or sub.angle_id not in ob.params.get("angle_ids", []):
            fail(ob.rule_id, f"angle {sub.angle_id!r} is not an approved angle")
        elif not any(contains_phrase(clip_text, kw) for kw in angle.keywords):
            borderline.append(f"{ob.rule_id}: none of {angle.angle_id}'s keywords appear in the clip's text (borderline on-brief)")

    ortr = rb.one(RuleKind.ORIGINALITY_TRANSFORM)
    if ortr is None:
        borderline.append("no transformation rule in this version (not governed)")
    else:
        checks.append(ortr.rule_id)
        allowed = set(ortr.params.get("allowed", []))
        valid = sorted({e for e in sub.transformation_elements if e in allowed})
        unknown = sorted({e for e in sub.transformation_elements if e not in allowed})
        need = int(ortr.params.get("min_elements", 1))
        if sub.is_raw_repost or not valid:
            fail(ortr.rule_id, "raw repost: no transformation (transform, don't repost)")
        elif len(valid) < need:
            fail(ortr.rule_id, f"{len(valid)} transformation element(s) ({', '.join(valid)}); needs {need}")
        if unknown:
            borderline.append(f"{ortr.rule_id}: unrecognised transformation element(s) {', '.join(unknown)}")

    ow = rb.one(RuleKind.ORIGINALITY_WATERMARK)
    if ow is not None:
        checks.append(ow.rule_id)
        if sub.has_third_party_watermark:
            fail(ow.rule_id, "noticeable third-party watermark")
    elif sub.has_third_party_watermark:
        borderline.append("third-party watermark declared but no watermark rule in this version")

    dc = rb.one(RuleKind.DISCLOSURE)
    if dc is None:
        borderline.append("no disclosure rule in this version (not governed)")
    else:
        checks.append(dc.rule_id)
        found = [match_phrase(sub.caption, t) for t in dc.params.get("any_of", [])]
        label = bool(dc.params.get("or_platform_label") and sub.paid_partnership_label)
        if PhraseMatch.EXACT not in found and not label:
            if PhraseMatch.LOOSE in found:
                borderline.append(f"{dc.rule_id}: disclosure only found with split/obfuscated letters")
            else:
                fail(dc.rule_id, "no disclosure in the caption and no paid-partnership label")

    for r in rb.rules_of(RuleKind.MUST_SAY):
        checks.append(r.rule_id)
        m = match_phrase(clip_text, r.params.get("phrase", ""))
        if m is PhraseMatch.LOOSE:
            borderline.append(f"{r.rule_id}: must-say {r.params.get('phrase')!r} only found with split/obfuscated letters")
        elif m is PhraseMatch.NONE:
            fail(r.rule_id, f"missing must-say {r.params.get('phrase')!r}")
    for r in rb.rules_of(RuleKind.NEVER_SAY):
        checks.append(r.rule_id)
        m = match_phrase(clip_text, r.params.get("phrase", ""))
        if m is PhraseMatch.EXACT:
            fail(r.rule_id, f"says never-say {r.params.get('phrase')!r}")
        elif m is PhraseMatch.LOOSE:
            borderline.append(f"{r.rule_id}: possible never-say {r.params.get('phrase')!r} written with split/obfuscated letters")

    for field_name in ("caption", "on_screen_text", "transcript"):
        for sig in obfuscation_signals(getattr(sub, field_name)):
            borderline.append(f"obfuscation in {field_name}: {sig}")

    qf = rb.one(RuleKind.QUALITY_FLOOR)
    if qf is not None:
        checks.append(qf.rule_id)
        floor = int(qf.params.get("min_height_px", 0))
        if sub.resolution_height_px is None:
            borderline.append(f"{qf.rule_id}: resolution not declared")
        elif sub.resolution_height_px < floor:
            fail(qf.rule_id, f"resolution {sub.resolution_height_px}px below {floor}px")

    rc = rb.one(RuleKind.RIGHTS_CLEARED_ONLY)
    used = list(dict.fromkeys([*sub.source_asset_ids, *sub.added_asset_ids]))
    if rc is not None:
        checks.append(rc.rule_id)
        allowed_assets = set(rc.params.get("allowed_asset_ids", []))
        uncleared = [a for a in used if a not in allowed_assets]
        if uncleared:
            fail(rc.rule_id, f"uncleared asset(s): {', '.join(uncleared)}")
        if not sub.source_asset_ids:
            borderline.append(f"{rc.rule_id}: no source asset declared")
    elif used:
        borderline.append("assets declared but no rights rule in this version")

    outcome = "reject" if broken else ("human_review" if borderline else "pass")
    if route_to_human:
        automatic = f"automatic result would have been {outcome}" + (
            f" (broke {', '.join(b.rule_id for b in broken)})" if broken else "")
        borderline = [*route_to_human, automatic, *borderline]
        outcome, broken = "human_review", []
    return make_decision(
        rb, submission_id=sub.submission_id, campaign_id=sub.campaign_id, rulebook_version=rb.version,
        outcome=outcome, broken_rules=tuple(broken),
        human_review_reasons=tuple(borderline) if outcome == "human_review" else (),
        checks=tuple(checks), decided_by=decided_by, decided_at=now, received_at=received_at,
    )
