"""
ZBC intelligence 2 — Source Mining.

Job: mark clip-worthy moments, with timestamps, in the campaign's source
material (the Moment Map).
Decides: which supplied segments are clip-worthy moments, and why each
other segment is not.

Input today is PROVIDED, already-timestamped segments with transcript text
(from the rights holder or a human logger). Scene detection
(PySceneDetect) and transcription (faster-whisper / WhisperX) are approved
building blocks that will plug in here later (shared/media.py); neither is
integrated, and no media is read.

Rules (deterministic):
M0  the source asset must be one of the rulebook's licensed source assets
    (footage comes from the rights holder; nothing is scraped) — otherwise
    the whole request is refused;
M1  0 <= start < end <= source duration;
M2  segment ids are unique;
M3  segments may not overlap (sorted by start; a segment starting before the
    previous kept segment ends is rejected, never silently merged);
M4  duration >= MIN_MOMENT_SECONDS;
M5  duration fits at least one target platform's length rule in the
    rulebook version, AND that rule's registry rows are usable today
    (re-checked before use); `too_long_for` / `blocked_for` say which not;
M6  the transcript hits at least one keyword of an APPROVED angle
    (whole-word match on normalised text); score = distinct keyword hits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from shared.errors import PreconditionFailed
from shared.registry import PlatformRulesRegistry
from shared.text import contains_phrase
from shared.types import SafeId
from zbc.platform_rules import rows_usable
from zbc.rulebook import Rulebook, RuleKind

MIN_MOMENT_SECONDS = 3.0


class SourceSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: SafeId
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    transcript: str = Field(default="", max_length=20000)


class SourceMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_asset_id: SafeId
    duration_seconds: float = Field(gt=0, le=86400)
    segments: list[SourceSegment] = Field(min_length=1, max_length=2000)


class Moment(BaseModel):
    moment_id: str
    segment_id: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    transcript: str
    matched_angle_ids: list[str]
    score: int
    fits: list[str]
    too_long_for: list[str]
    blocked_for: list[str]


class RejectedSegment(BaseModel):
    segment_id: str
    reasons: list[str]


class MomentMap(BaseModel):
    campaign_id: str
    rulebook_version: int
    source_asset_id: str
    moments: list[Moment]
    rejected: list[RejectedSegment]


@dataclass
class _Limits:
    max_by_target: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)


def _limits(rb: Rulebook, registry: PlatformRulesRegistry, today: date) -> _Limits:
    out = _Limits()
    for r in rb.rules_of(RuleKind.SPEC_LENGTH):
        key = f"{r.params['platform']}/{r.params['placement']}"
        reasons = rows_usable(registry, r.rationale_row_ids, today)
        if reasons:
            out.blocked[key] = f"{r.rule_id}: " + "; ".join(reasons)
        else:
            out.max_by_target[key] = int(r.params["max_seconds"])
    return out


def build(material: SourceMaterial, rb: Rulebook, registry: PlatformRulesRegistry, today: date) -> MomentMap:
    if material.source_asset_id not in rb.source_asset_ids:
        raise PreconditionFailed(
            f"M0 source {material.source_asset_id!r} is not a licensed source asset of rulebook "
            f"{rb.campaign_id} v{rb.version}; footage comes from the rights holder only"
        )
    limits = _limits(rb, registry, today)
    moments: list[Moment] = []
    rejected: list[RejectedSegment] = []
    seen: set[str] = set()
    last_end: float | None = None
    last_id: str | None = None
    for seg in sorted(material.segments, key=lambda s: (s.start_seconds, s.end_seconds, s.segment_id)):
        reasons: list[str] = []
        if seg.segment_id in seen:
            reasons.append("M2 duplicate segment id")
        seen.add(seg.segment_id)
        if not seg.start_seconds < seg.end_seconds:
            reasons.append(f"M1 start {seg.start_seconds}s is not before end {seg.end_seconds}s")
        if seg.end_seconds > material.duration_seconds:
            reasons.append(f"M1 end {seg.end_seconds}s is past the source duration {material.duration_seconds}s")
        if reasons:
            rejected.append(RejectedSegment(segment_id=seg.segment_id, reasons=reasons))
            continue
        if last_end is not None and seg.start_seconds < last_end:
            rejected.append(RejectedSegment(segment_id=seg.segment_id,
                                            reasons=[f"M3 overlaps {last_id} (which ends at {last_end}s)"]))
            continue
        last_end, last_id = seg.end_seconds, seg.segment_id
        dur = round(seg.end_seconds - seg.start_seconds, 3)
        if dur < MIN_MOMENT_SECONDS:
            reasons.append(f"M4 {dur}s is shorter than {MIN_MOMENT_SECONDS}s")
        fits = sorted(k for k, mx in limits.max_by_target.items() if dur <= mx)
        too_long = sorted(k for k, mx in limits.max_by_target.items() if dur > mx)
        blocked = sorted(f"{k} ({why})" for k, why in limits.blocked.items())
        if not fits:
            reasons.append("M5 fits no target platform's length rule"
                           + (f"; too long for {', '.join(too_long)}" if too_long else "")
                           + (f"; blocked for {', '.join(blocked)}" if blocked else ""))
        matched: list[str] = []
        hits = 0
        for a in rb.approved_angles:
            n = sum(1 for kw in a.keywords if contains_phrase(seg.transcript, kw))
            if n:
                matched.append(a.angle_id)
                hits += n
        if not seg.transcript.strip():
            reasons.append("M6 no transcript; cannot be matched to an approved angle")
        elif not matched:
            reasons.append("M6 no approved-angle keyword in the transcript")
        if reasons:
            rejected.append(RejectedSegment(segment_id=seg.segment_id, reasons=reasons))
            continue
        moments.append(Moment(
            moment_id=f"m-{seg.segment_id}", segment_id=seg.segment_id,
            start_seconds=seg.start_seconds, end_seconds=seg.end_seconds, duration_seconds=dur,
            transcript=seg.transcript, matched_angle_ids=matched, score=hits, fits=fits,
            too_long_for=too_long, blocked_for=blocked,
        ))
    return MomentMap(campaign_id=rb.campaign_id, rulebook_version=rb.version,
                     source_asset_id=material.source_asset_id, moments=moments, rejected=rejected)
