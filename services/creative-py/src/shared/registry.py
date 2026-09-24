"""
Platform Rules Registry — ONE registry, row-level owners (spec: "Shared").

Every row: platform, placement, rule key, value, official source URL,
verified_at, expires_at, owner. Two owners:
- `zbm_placement_spec` (ZBM intelligence 4) owns SPEC rows (size, ratio,
  length, codec, safe zones);
- `zbc_platform_rules` (ZBC intelligence 4) owns ORIGINALITY/REPOST rows.

Enforced here, not by convention:
- a writer can only create/replace rows its owner owns, and can never
  take over a row another owner holds (`write`);
- a row that is unverified, has no verified_at, is past expires_at, or is
  missing entirely BLOCKS its use with a clear reason (`require_usable`) —
  fail closed, never "probably still true";
- a verified row must carry an https source URL and a shelf life no longer
  than MAX_SHELF_LIFE_DAYS.

Seed (see ADR 0005): exactly the four sourced facts in the spec, all
verified_at 2026-09-23 with a 30-day shelf life (expires 2026-10-23), plus
ONE TikTok row seeded as UNVERIFIED (no quoted wording) so any attempt to
rely on TikTok rules fails closed with an explicit reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

from shared.errors import GuardrailViolation, NotFound, RegistryRowBlocked
from shared.types import NonEmptyStr, SafeId

SHELF_LIFE_DAYS = 30
MAX_SHELF_LIFE_DAYS = 30
SEED_VERIFIED_AT = date(2026, 9, 23)


class RowOwner(str, Enum):
    ZBM_PLACEMENT_SPEC = "zbm_placement_spec"
    ZBC_PLATFORM_RULES = "zbc_platform_rules"


class RowStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class Enforcement(str, Enum):
    HARD_LIMIT = "hard_limit"    # exceeding it is a failure
    ADVISORY = "advisory"        # exceeding it is a warning (platform "recommends")
    POLICY = "policy"            # a written platform policy, cited as rationale


class RegistryRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: SafeId
    platform: NonEmptyStr
    placement: NonEmptyStr
    rule_key: NonEmptyStr
    value: int | str
    unit: str | None = None
    enforcement: Enforcement
    source_url: str | None = None
    verified_at: date | None = None
    expires_at: date | None = None
    owner: RowOwner
    status: RowStatus
    note: str | None = None

    @model_validator(mode="after")
    def _verified_rows_are_sourced_and_dated(self) -> "RegistryRow":
        if self.status is RowStatus.VERIFIED:
            if not self.source_url or not self.source_url.startswith("https://"):
                raise ValueError("a verified row needs an official https source_url")
            if self.verified_at is None or self.expires_at is None:
                raise ValueError("a verified row needs verified_at and expires_at")
            if self.expires_at <= self.verified_at:
                raise ValueError("expires_at must be after verified_at")
            if (self.expires_at - self.verified_at).days > MAX_SHELF_LIFE_DAYS:
                raise ValueError(f"shelf life may not exceed {MAX_SHELF_LIFE_DAYS} days")
        return self


@dataclass(frozen=True)
class Usability:
    usable: bool
    reason: str


def check_usable(row: RegistryRow, today: date) -> Usability:
    if row.status is not RowStatus.VERIFIED:
        return Usability(False, f"row {row.row_id} is unverified; it may not be relied on until verified from an official source")
    if row.verified_at is None or row.expires_at is None:
        return Usability(False, f"row {row.row_id} has no verification dates")
    if row.verified_at > today:
        return Usability(False, f"row {row.row_id} verified_at {row.verified_at} is in the future")
    if today >= row.expires_at:
        return Usability(False, f"row {row.row_id} expired on {row.expires_at}; re-verify against {row.source_url} before use")
    return Usability(True, f"row {row.row_id} verified {row.verified_at}, valid until {row.expires_at}")


@dataclass
class PlatformRulesRegistry:
    rows: dict[str, RegistryRow] = field(default_factory=dict)

    def get(self, row_id: str) -> RegistryRow:
        row = self.rows.get(row_id)
        if row is None:
            raise NotFound(f"registry row {row_id!r} not found")
        return row

    def find(self, platform: str, placement: str, rule_key: str) -> RegistryRow | None:
        for row in self.rows.values():
            if (row.platform, row.placement, row.rule_key) == (platform, placement, rule_key):
                return row
        return None

    def rows_for(self, platform: str, placement: str | None = None) -> list[RegistryRow]:
        return [
            r for r in self.rows.values()
            if r.platform == platform and (placement is None or r.placement in (placement, "all"))
        ]

    def require_usable(self, row_id: str, today: date) -> RegistryRow:
        row = self.rows.get(row_id)
        if row is None:
            raise RegistryRowBlocked(row_id, "no such row")
        u = check_usable(row, today)
        if not u.usable:
            raise RegistryRowBlocked(row_id, u.reason)
        return row

    def check_write(self, row: RegistryRow, writer: RowOwner) -> RegistryRow | None:
        """Ownership check without mutation. Returns the row being replaced."""
        if row.owner is not writer:
            raise GuardrailViolation(
                f"owner {writer.value!r} cannot write a row owned by {row.owner.value!r}"
            )
        existing = self.rows.get(row.row_id)
        if existing is not None and existing.owner is not writer:
            raise GuardrailViolation(
                f"row {row.row_id!r} is owned by {existing.owner.value!r}; {writer.value!r} cannot overwrite it"
            )
        clash = self.find(row.platform, row.placement, row.rule_key)
        if clash is not None and clash.row_id != row.row_id:
            raise GuardrailViolation(
                f"row {clash.row_id!r} already holds ({row.platform}, {row.placement}, {row.rule_key})"
            )
        return existing

    def commit(self, row: RegistryRow) -> None:
        self.rows[row.row_id] = row


def _seed_rows() -> list[RegistryRow]:
    v, e = SEED_VERIFIED_AT, SEED_VERIFIED_AT + timedelta(days=SHELF_LIFE_DAYS)
    ig_url = "https://creators.instagram.com/blog/tips-for-improving-your-reach"
    return [
        RegistryRow(
            row_id="yt-shorts-max-length",
            platform="youtube", placement="shorts", rule_key="max_length_seconds",
            value=180, unit="seconds", enforcement=Enforcement.HARD_LIMIT,
            source_url="https://support.google.com/youtube/answer/15424877",
            verified_at=v, expires_at=e, owner=RowOwner.ZBM_PLACEMENT_SPEC, status=RowStatus.VERIFIED,
            note="YouTube Shorts can be up to 3 minutes long.",
        ),
        RegistryRow(
            row_id="ig-reels-recommended-length",
            platform="instagram", placement="reels", rule_key="recommended_max_length_seconds",
            value=180, unit="seconds", enforcement=Enforcement.ADVISORY,
            source_url=ig_url, verified_at=v, expires_at=e,
            owner=RowOwner.ZBM_PLACEMENT_SPEC, status=RowStatus.VERIFIED,
            note="Instagram recommends Reels of 3 minutes or less (a recommendation, not a hard cap).",
        ),
        RegistryRow(
            row_id="ig-repost-watermark-derecommendation",
            platform="instagram", placement="all", rule_key="originality_repost_watermark",
            value="less likely to recommend: reposted content, content with noticeable watermarks, "
                  "accounts that collect/reshare others' content",
            enforcement=Enforcement.POLICY, source_url=ig_url, verified_at=v, expires_at=e,
            owner=RowOwner.ZBC_PLATFORM_RULES, status=RowStatus.VERIFIED,
        ),
        RegistryRow(
            row_id="yt-reused-content-monetization",
            platform="youtube", placement="all", rule_key="originality_reused_content",
            value="reused content without significant original commentary, substantive modification, "
                  "or educational/entertainment value is not eligible for monetization",
            enforcement=Enforcement.POLICY,
            source_url="https://support.google.com/youtube/answer/1311392",
            verified_at=v, expires_at=e, owner=RowOwner.ZBC_PLATFORM_RULES, status=RowStatus.VERIFIED,
        ),
        RegistryRow(
            row_id="tiktok-originality-unverified",
            platform="tiktok", placement="all", rule_key="originality_policy",
            value="UNVERIFIED: TikTok's exact wording has not been sourced; do not quote or rely on it",
            enforcement=Enforcement.POLICY, owner=RowOwner.ZBC_PLATFORM_RULES, status=RowStatus.UNVERIFIED,
        ),
    ]


def seeded_registry() -> PlatformRulesRegistry:
    reg = PlatformRulesRegistry()
    for row in _seed_rows():
        reg.commit(row)
    return reg
