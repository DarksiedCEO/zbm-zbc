"""
Request models (spec §D): strict (unknown key → 422, no type coercion), bounded strings, no control
characters, and NO money, float or count field anywhere (§0.1.9, G1). Callers never supply counts: any key
named or containing ``views``, ``count``, ``metric``, ``amount`` or ``rate`` anywhere in a body is refused
(``forbidden_keys``) — the one exception is Creative's ``ClipResult.reported_views`` inside
``/vi/v1/results/attest`` facts, which is accepted, stored only inside ``facts_sha256`` and never read.
Any key starting with ``guardian`` is refused everywhere (no guardian path exists, spec A6).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from intelligences.i05_age_assurance import METHODS
from platforms import PLATFORMS

ID_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"
ID_RE = re.compile(ID_PATTERN)
Id = Annotated[str, StringConstraints(pattern=ID_PATTERN)]
BoundedId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,100}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
FORBIDDEN_FRAGMENTS = ("views", "count", "metric", "amount", "rate")


def _printable(v: str) -> str:
    if _CONTROL.search(v):
        raise ValueError("control characters are not allowed")
    return v


def _rfc3339(v: str) -> str:
    from clock import parse_iso
    try:
        parse_iso(v)
    except ValueError:
        raise ValueError("must be an RFC 3339 timestamp with a UTC offset") from None
    return v


Text = Annotated[str, StringConstraints(min_length=1, max_length=500), AfterValidator(_printable)]
Ref = Annotated[str, StringConstraints(min_length=1, max_length=2048), AfterValidator(_printable)]
Timestamp = Annotated[str, StringConstraints(min_length=1, max_length=40), AfterValidator(_rfc3339)]
Iso2 = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Platform = Literal["youtube", "tiktok", "instagram", "x", "snapchat", "twitch"]
assert set(PLATFORMS) == {"youtube", "tiktok", "instagram", "x", "snapchat", "twitch"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def forbidden_keys(obj: Any, path: str = "", allow: tuple = (), depth: int = 0) -> list[str]:
    """Every key path whose key is a count/money word or a guardian field (bounded walk)."""
    out: list[str] = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            kl = str(k).lower()
            if (any(f in kl for f in FORBIDDEN_FRAGMENTS) and p not in allow) or kl.startswith("guardian"):
                out.append(p[:120])
            out += forbidden_keys(v, p, allow, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:10_000]):
            out += forbidden_keys(v, path, allow, depth + 1)
    return out


class RunRequest(Strict):
    request_id: Id


class ConnectionStart(Strict):
    request_id: Id
    clipper_id: Id
    platform: Platform
    redirect_uri: Annotated[str, StringConstraints(pattern=r"^https://[^\s\x00-\x1f\x7f]{3,500}$")]


class ConnectionComplete(Strict):
    request_id: Id
    state: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,128}$")]
    code: Ref


class SubmissionRequest(Strict):
    request_id: Id
    submission_id: Id
    campaign_id: BoundedId
    rulebook_version: int = Field(ge=1, le=9_999_999)
    clipper_id: Id
    platform: Platform
    post_ref: Ref
    posted_at: Timestamp
    min_days_live: Optional[int] = Field(ge=1, le=365)
    collab_permitted: bool
    media_ref: Optional[Annotated[str, StringConstraints(min_length=1, max_length=512), AfterValidator(_printable)]] = None
    seed_media_refs: list[Annotated[str, StringConstraints(min_length=1, max_length=512), AfterValidator(_printable)]] = \
        Field(default_factory=list, max_length=20)
    target_regions: Optional[list[Iso2]] = Field(default=None, max_length=250)


class ApprovalRequest(Strict):
    request_id: Id
    review_ref: Optional[Id] = None


class CreativeClipFacts(Strict):
    campaign_id: BoundedId
    rulebook_version: int = Field(ge=1, le=9_999_999)
    post_ref: Ref
    clipper_id: Id
    posted_at: Timestamp


class CreativeClipAttest(Strict):
    request_id: Id
    submission_id: Id
    facts: CreativeClipFacts


class Hr13Attest(Strict):
    request_id: Id
    submission_id: Id
    post_ref: Ref
    platform: Annotated[str, StringConstraints(min_length=1, max_length=32), AfterValidator(_printable)]
    posted_at: Timestamp
    settlement_lag_days: int = Field(ge=0, le=365)


class ClipResultFacts(Strict):
    """Creative's ``ClipResult`` model (creative-py src/zbc/creative_memory.py), mirrored field for field."""

    result_id: Id
    campaign_id: BoundedId
    submission_id: Id
    vertical: Annotated[str, StringConstraints(min_length=1, max_length=4000), AfterValidator(_printable)]
    platform: Annotated[str, StringConstraints(min_length=1, max_length=4000), AfterValidator(_printable)]
    angle_id: Annotated[str, StringConstraints(min_length=1, max_length=4000), AfterValidator(_printable)]
    hook: Annotated[str, StringConstraints(min_length=1, max_length=4000), AfterValidator(_printable)]
    source: Literal["self_reported", "platform_export", "tracking_link"]
    reported_views: int = Field(ge=0)          # accepted, hashed into facts_sha256, NEVER read


class ResultAttest(Strict):
    request_id: Id
    result_id: Id
    facts: ClipResultFacts


class AgeCheck(Strict):
    request_id: Id
    subject_id: Id
    dob: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    dob_field_neutral: bool
    method: Literal[METHODS]  # type: ignore[valid-type]
    provider_session_ref: Annotated[str, StringConstraints(min_length=1, max_length=256), AfterValidator(_printable)]


class IdentityCheck(Strict):
    request_id: Id
    clipper_id: Id
    email: Annotated[str, StringConstraints(pattern=r"^[^\s@\x00-\x1f\x7f]{1,64}@[^\s@\x00-\x1f\x7f]{1,189}$")]


class DecisionRequest(Strict):
    request_id: Id
    decision: Literal["release", "uphold", "overturn"]
    reason: Text
    appeal_id: Optional[Id] = None


class BanRequest(Strict):
    request_id: Id
    clipper_id: Id
    cn_decision_id: Id
    approved_at: Timestamp


class RuleProposalRequest(Strict):
    request_id: Id
    kind: Literal["add", "amend", "retire"]
    target_id: Optional[Annotated[str, StringConstraints(pattern=r"^VI-[0-9]{2,3}[a-z]?$")]] = None
    proposed_row: Optional[dict] = None


class RuleDecision(Strict):
    proposal_id: Id
    content_sha256: Sha256
    decision: Literal["approve", "reject"]
    note: Optional[Text] = None
    acknowledge_weakening: Optional[bool] = None


class RuleDecisions(Strict):
    request_id: Id
    decisions: list[RuleDecision] = Field(min_length=1, max_length=200)


class ReconcileRequest(Strict):
    request_id: Id
    head_sha256: Sha256
    void_lines: list[Annotated[int, Field(ge=1, le=10**12)]] = Field(max_length=10_000)
    void_event_ids: list[Id] = Field(max_length=10_000)
