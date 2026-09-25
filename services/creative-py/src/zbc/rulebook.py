"""
The ZBC campaign rulebook — versioned, and FROZEN once live.

Sections (spec): objective, approved angles, must-say, never-say,
disclosure requirement, platforms, specs, originality rules, minimum days
live. Every rule carries a STABLE rule id `XX-N` whose prefix names its
kind: two capital letters, a dash, and a decimal number written with at
least two digits and no other leading zero (`NS-01` ... `NS-99`, `NS-100`,
`NS-1000`, ...; up to nine digits, RULE_ID_PATTERN). Fix wave 8 (AEGIS
round 7, N7-3): the space used to be `XX-NN` (01-99) while a goal admits
1,000 never-say and 100 must-say entries — the 100th entry was a 500 and,
since ids are never reused, a churning campaign wedged at NS-100. The
two-digit form is unchanged, so every id ever issued still parses
(`parse_rule_number`). Ids are never reused within a campaign: a revision
keeps the ids of unchanged rules and gives new rules the next unused
number (`next_rule_number`, computed once per prefix per revision).

Frozen: a `Rulebook` is an immutable pydantic model; the store refuses to
overwrite a live or superseded version. Changes after go-live create a
NEW version (rulebook_writer.revise), which must be approved and signed
again; clips are judged by the version they were made under.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from shared.errors import FrozenError, NotFound, PreconditionFailed
from shared.types import MAX_RULEBOOK_VERSION, CampaignId, NonEmptyStr, SafeId

RULE_ID_PATTERN = r"^[A-Z]{2}-(?:[0-9]{2}|[1-9][0-9]{2,8})$"
RuleId = Annotated[str, StringConstraints(pattern=RULE_ID_PATTERN)]
_RULE_NUMBER = re.compile(r"(?:[0-9]{2}|[1-9][0-9]{2,8})")
_RULE_ID = re.compile(RULE_ID_PATTERN)
AngleId = Annotated[str, StringConstraints(pattern=r"^A[0-9]{2}$")]

TRANSFORMATION_ELEMENTS = frozenset({
    "original_commentary",
    "voiceover",
    "recut_edit",
    "added_context",
    "reaction",
    "graphics_overlay",
    "captions_added",
})


class RuleKind(str, Enum):
    ON_BRIEF = "on_brief"
    MUST_SAY = "must_say"
    NEVER_SAY = "never_say"
    DISCLOSURE = "disclosure"
    PLATFORM = "platform"
    SPEC_LENGTH = "spec_length"
    ORIGINALITY_TRANSFORM = "originality_transform"
    ORIGINALITY_WATERMARK = "originality_watermark"
    QUALITY_FLOOR = "quality_floor"
    MIN_DAYS_LIVE = "min_days_live"
    RIGHTS_CLEARED_ONLY = "rights_cleared_only"


PREFIX = {
    RuleKind.ON_BRIEF: "OB",
    RuleKind.MUST_SAY: "MS",
    RuleKind.NEVER_SAY: "NS",
    RuleKind.DISCLOSURE: "DC",
    RuleKind.PLATFORM: "PF",
    RuleKind.SPEC_LENGTH: "SP",
    RuleKind.ORIGINALITY_TRANSFORM: "OR",
    RuleKind.ORIGINALITY_WATERMARK: "OW",
    RuleKind.QUALITY_FLOOR: "QF",
    RuleKind.MIN_DAYS_LIVE: "MD",
    RuleKind.RIGHTS_CLEARED_ONLY: "RC",
}


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: RuleId
    kind: RuleKind
    text: NonEmptyStr
    params: dict[str, Any] = Field(default_factory=dict)
    rationale_row_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _prefix_matches_kind(self) -> "Rule":
        if not self.rule_id.startswith(PREFIX[self.kind] + "-"):
            raise ValueError(f"rule id {self.rule_id} does not match kind {self.kind.value}")
        return self


class Angle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    angle_id: AngleId
    name: NonEmptyStr
    description: NonEmptyStr
    keywords: tuple[NonEmptyStr, ...] = Field(min_length=1)
    approved_hook_lines: tuple[NonEmptyStr, ...] = ()


class PlatformTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]
    placement: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]


class RulebookStatus(str, Enum):
    DRAFT = "draft"
    SENT_BACK = "sent_back"
    APPROVED = "approved"
    SIGNED = "signed"
    LIVE = "live"
    SUPERSEDED = "superseded"


FROZEN_STATUSES = frozenset({RulebookStatus.LIVE, RulebookStatus.SUPERSEDED})


class Rulebook(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: CampaignId
    client_id: SafeId
    vertical: NonEmptyStr
    version: int = Field(ge=1, le=MAX_RULEBOOK_VERSION)
    status: RulebookStatus
    objective: NonEmptyStr
    source_asset_ids: tuple[SafeId, ...] = Field(min_length=1)
    approved_angles: tuple[Angle, ...]
    platforms: tuple[PlatformTarget, ...]
    rules: tuple[Rule, ...]
    retired_rule_ids: tuple[str, ...] = ()
    blocking_issues: tuple[str, ...] = ()
    # Non-blocking notes from the writer (fix wave 6, N3): e.g. a never-say
    # entry of <= 4 letters, which is matched exactly only unless opted in.
    warnings: tuple[str, ...] = ()
    drafted_by: str
    supersedes_version: int | None = None
    approved_by: str | None = None
    review_issues: tuple[str, ...] = ()
    review_warnings: tuple[str, ...] = ()  # the approver's non-blocking notes (N3)
    signed_by: str | None = None
    signed_at: datetime | None = None
    live_at: datetime | None = None
    superseded_at: datetime | None = None
    language: Literal["en"] = "en"

    @model_validator(mode="after")
    def _ids_unique(self) -> "Rulebook":
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique within a rulebook version")
        if set(ids) & set(self.retired_rule_ids):
            raise ValueError("a retired rule id cannot be reused")
        angle_ids = [a.angle_id for a in self.approved_angles]
        if len(angle_ids) != len(set(angle_ids)):
            raise ValueError("angle ids must be unique")
        return self

    @property
    def frozen(self) -> bool:
        return self.status in FROZEN_STATUSES

    def rule_ids(self) -> frozenset[str]:
        return frozenset(r.rule_id for r in self.rules)

    def rules_of(self, kind: RuleKind) -> list[Rule]:
        return [r for r in self.rules if r.kind is kind]

    def one(self, kind: RuleKind) -> Rule | None:
        rs = self.rules_of(kind)
        return rs[0] if rs else None

    def angle(self, angle_id: str) -> Angle | None:
        return next((a for a in self.approved_angles if a.angle_id == angle_id), None)


def parse_rule_number(prefix: str, rule_id: str) -> int | None:
    """The number of `rule_id` if it is a well-formed id with `prefix`
    (`NS-07` -> 7, `NS-100` -> 100), else None."""
    if len(rule_id) < 5 or rule_id[:2] != prefix or rule_id[2] != "-" or not _RULE_NUMBER.fullmatch(rule_id[3:]):
        return None
    return int(rule_id[3:])


def format_rule_id(prefix: str, n: int) -> str:
    return f"{prefix}-{n:02d}"


def highest_rule_numbers(used: set[str]) -> dict[str, int]:
    """{prefix: highest number used with it} in one pass over `used`
    (a churned campaign has hundreds of thousands of retired ids)."""
    out: dict[str, int] = {}
    for u in used:
        if _RULE_ID.fullmatch(u):  # anything else was never issued by format_rule_id: not a number in use
            n = int(u[3:])
            prefix = u[:2]
            if n > out.get(prefix, 0):
                out[prefix] = n
    return out


def next_rule_number(prefix: str, used: set[str]) -> int:
    """One more than the highest number used with `prefix` (1 if none)."""
    return highest_rule_numbers(used).get(prefix, 0) + 1


# --- content identity ---------------------------------------------------------

# Fields that are the rulebook's CONTENT (what clips are judged by). The
# remaining fields are lifecycle metadata (status, who approved/signed, when).
CONTENT_FIELDS = (
    "campaign_id", "client_id", "vertical", "version", "objective", "source_asset_ids",
    "approved_angles", "platforms", "rules", "retired_rule_ids", "drafted_by", "supersedes_version",
)


def content_of(rb: "Rulebook") -> dict:
    return rb.model_dump(mode="json", include=set(CONTENT_FIELDS))


# --- store ---------------------------------------------------------------------


class RulebookStore:
    """All versions of every campaign's rulebook.

    Freezing is enforced HERE, at the only place a version is written:
    - a LIVE or SUPERSEDED version's content can never be replaced;
    - the only permitted change to a frozen version is the lifecycle move
      LIVE -> SUPERSEDED (content byte-identical), made when a newer version
      goes live;
    - a version number is never reused.
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int], Rulebook] = {}

    def get(self, campaign_id: str, version: int) -> Rulebook:
        rb = self._by_key.get((campaign_id, version))
        if rb is None:
            raise NotFound(f"rulebook {campaign_id} v{version} not found")
        return rb

    def versions(self, campaign_id: str) -> list[Rulebook]:
        return sorted((rb for (cid, _), rb in self._by_key.items() if cid == campaign_id), key=lambda r: r.version)

    def live(self, campaign_id: str) -> Rulebook | None:
        return next((rb for rb in self.versions(campaign_id) if rb.status is RulebookStatus.LIVE), None)

    def next_version(self, campaign_id: str) -> int:
        vs = self.versions(campaign_id)
        return (vs[-1].version + 1) if vs else 1

    def check_add(self, rb: Rulebook) -> None:
        if (rb.campaign_id, rb.version) in self._by_key:
            raise PreconditionFailed(f"rulebook {rb.campaign_id} v{rb.version} already exists; versions are never reused")
        if rb.version != self.next_version(rb.campaign_id):
            raise PreconditionFailed(f"next version for {rb.campaign_id} is v{self.next_version(rb.campaign_id)}")

    def check_replace(self, rb: Rulebook) -> Rulebook:
        existing = self.get(rb.campaign_id, rb.version)
        if existing.frozen:
            allowed_supersede = (
                existing.status is RulebookStatus.LIVE
                and rb.status is RulebookStatus.SUPERSEDED
                and content_of(existing) == content_of(rb)
            )
            if not allowed_supersede:
                raise FrozenError(
                    f"rulebook {rb.campaign_id} v{rb.version} is {existing.status.value} and FROZEN; "
                    "it cannot be changed in place — draft a new version instead"
                )
        return existing

    def commit(self, rb: Rulebook) -> None:
        self._by_key[(rb.campaign_id, rb.version)] = rb
