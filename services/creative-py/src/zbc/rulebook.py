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
import bisect
import functools
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, computed_field, model_validator

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
    # Fix wave 9 (AEGIS round 8 L2): per id prefix, the highest rule number ever issued in this
    # campaign up to this version. The retired ids are DERIVED from it (`retired_rule_ids`): every
    # number up to it that is not a rule of this version. Every version used to carry the whole list
    # (60 churn revisions of 1,000 phrases: 60,000 ids per version, 96 MiB, a 30 MiB GET); this is a
    # handful of integers whatever the history (memory O(rules of the version + prefixes)).
    rule_number_high_water: dict[str, int] = Field(default_factory=dict)
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

    @model_validator(mode="before")
    @classmethod
    def _high_water(cls, data: Any) -> Any:
        """The high-water marks cover every rule of the version (numbers
        only ever grow); a dump from before fix wave 9 (`retired_rule_ids`,
        a list) is read as the marks it implies — every number up to the
        highest one listed counts as used, so nothing it retired can be
        reused; the derived count is output only."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        data.pop("retired_rule_count", None)
        legacy = data.pop("retired_rule_ids", None) or ()
        hw = dict(data.get("rule_number_high_water") or {})
        ids = [r.rule_id if isinstance(r, Rule) else (r or {}).get("rule_id", "") for r in data.get("rules") or ()]
        for prefix, n in highest_rule_numbers({*ids, *legacy}).items():
            if n > hw.get(prefix, 0):
                hw[prefix] = n
        data["rule_number_high_water"] = hw
        return data

    @model_validator(mode="after")
    def _ids_unique(self) -> "Rulebook":
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique within a rulebook version")
        angle_ids = [a.angle_id for a in self.approved_angles]
        if len(angle_ids) != len(set(angle_ids)):
            raise ValueError("angle ids must be unique")
        return self

    @property
    def frozen(self) -> bool:
        return self.status in FROZEN_STATUSES

    def rule_ids(self) -> frozenset[str]:
        return frozenset(r.rule_id for r in self.rules)

    @functools.cached_property
    def retired_rule_ids(self) -> "RetiredRuleIds":
        """Every id issued in this campaign up to this version that is not
        one of its rules — derived from `rule_number_high_water`, never
        stored (fix wave 9, L2). A read-only sequence: len(), indexing,
        slicing, `in`, iteration."""
        return RetiredRuleIds(self.rule_number_high_water, self.rule_ids())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def retired_rule_count(self) -> int:
        return len(self.retired_rule_ids)

    def is_retired(self, rule_id: str) -> bool:
        return rule_id in self.retired_rule_ids

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


class RetiredRuleIds(Sequence):
    """The retired rule ids of one version, derived from its high-water
    marks and its current rules: for each prefix (in order) the numbers 1
    to the mark (ascending) that are not current. O(current rules +
    prefixes) memory; `in` is O(1); indexing is O(log) (fix wave 9, L2)."""

    def __init__(self, high_water: dict[str, int], current: frozenset[str]):
        self._blocks: list[tuple[str, int, list[int]]] = []  # (prefix, high water, sorted current numbers)
        self._starts: list[int] = []
        by_prefix: dict[str, list[int]] = {}
        for rid in current:
            n = parse_rule_number(rid[:2], rid)
            if n is not None and 1 <= n <= high_water.get(rid[:2], 0):
                by_prefix.setdefault(rid[:2], []).append(n)
        total = 0
        for prefix in sorted(high_water):
            hw = high_water[prefix]
            nums = sorted(by_prefix.get(prefix, ()))
            self._starts.append(total)
            self._blocks.append((prefix, hw, nums))
            total += hw - len(nums)
        self._len = total
        self._current = current

    def __len__(self) -> int:
        return self._len

    def __contains__(self, rule_id: object) -> bool:
        if not isinstance(rule_id, str) or rule_id in self._current or not _RULE_ID.fullmatch(rule_id):
            return False
        prefix, n = rule_id[:2], int(rule_id[3:])
        if rule_id != format_rule_id(prefix, n):
            return False  # not a form format_rule_id issues (e.g. "NS-007"): never issued, never retired
        return any(p == prefix and 1 <= n <= hw for p, hw, _ in self._blocks)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return tuple(self[j] for j in range(*i.indices(self._len)))
        if i < 0:
            i += self._len
        if not 0 <= i < self._len:
            raise IndexError("retired rule id index out of range")
        b = bisect.bisect_right(self._starts, i) - 1
        prefix, hw, nums = self._blocks[b]
        want = i - self._starts[b]  # the want-th (0-based) number in 1..hw that is not current
        lo, hi = 1, hw
        while lo < hi:  # the smallest n with n - (current numbers <= n) > want
            mid = (lo + hi) // 2
            if mid - bisect.bisect_right(nums, mid) > want:
                hi = mid
            else:
                lo = mid + 1
        return format_rule_id(prefix, lo)

    def __iter__(self):
        for prefix, hw, nums in self._blocks:
            cur = set(nums)
            for n in range(1, hw + 1):
                if n not in cur:
                    yield format_rule_id(prefix, n)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (tuple, list, RetiredRuleIds)):
            return len(other) == self._len and tuple(self) == tuple(other)
        return NotImplemented

    def __repr__(self) -> str:
        return f"RetiredRuleIds({self._len} ids)"


def next_rule_number(prefix: str, used: set[str]) -> int:
    """One more than the highest number used with `prefix` (1 if none)."""
    return highest_rule_numbers(used).get(prefix, 0) + 1


# --- content identity ---------------------------------------------------------

# Fields that are the rulebook's CONTENT (what clips are judged by). The
# remaining fields are lifecycle metadata (status, who approved/signed, when).
CONTENT_FIELDS = (
    "campaign_id", "client_id", "vertical", "version", "objective", "source_asset_ids",
    "approved_angles", "platforms", "rules", "rule_number_high_water", "drafted_by", "supersedes_version",
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
        self.check_no_reuse(rb)

    def check_no_reuse(self, rb: Rulebook) -> None:
        """A retired id is never reused (fix wave 9, L2: this guard was the
        model's, against its own stored list): every rule of `rb` that is
        not a rule of the version it supersedes carries a number above that
        version's high-water mark."""
        if rb.supersedes_version is None or (rb.campaign_id, rb.supersedes_version) not in self._by_key:
            return
        prev = self._by_key[(rb.campaign_id, rb.supersedes_version)]
        kept = prev.rule_ids()
        for rid in rb.rule_ids() - kept:
            n = parse_rule_number(rid[:2], rid)
            if n is not None and n <= prev.rule_number_high_water.get(rid[:2], 0):
                raise PreconditionFailed(f"rule id {rid} was already issued in {rb.campaign_id} (v{prev.version} or "
                                         "before); retired ids are never reused")

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
