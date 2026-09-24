"""
ZBC intelligence 4 — Platform Rules.

Job: own the ORIGINALITY / REPOST rows of the shared Platform Rules
Registry (every fact dated, sourced, expiring) and re-check them before
any ZBC decision relies on one.
Decides: (a) whether a registry row may be written by ZBC (only rows owned
by `zbc_platform_rules`); (b) which rows a campaign may rely on today for a
platform, and why the others are blocked.

It READS spec rows owned by ZBM Placement Spec (length limits) because the
registry is shared reference data; it can never WRITE them (row-level
ownership is enforced in shared/registry.py `check_write`).

TikTok: the only TikTok row is seeded UNVERIFIED (spec: exact wording not
sourced), so every TikTok lookup is blocked with that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from shared.actors import ActorRegistry, Role
from shared.ledger import EvidenceRecorder
from shared.registry import PlatformRulesRegistry, RegistryRow, RowOwner, check_usable

ORIGINALITY_KEYS = ("originality_repost_watermark", "originality_reused_content", "originality_policy")
LENGTH_KEYS = ("max_length_seconds", "recommended_max_length_seconds")
WATERMARK_KEY = "originality_repost_watermark"


@dataclass
class RowLookup:
    usable: list[RegistryRow] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def _lookup(registry: PlatformRulesRegistry, platform: str, placement: str, owner: RowOwner,
            keys: tuple[str, ...], today: date) -> RowLookup:
    out = RowLookup()
    for row in registry.rows_for(platform, placement):
        if row.owner is not owner or row.rule_key not in keys:
            continue
        u = check_usable(row, today)
        if u.usable:
            out.usable.append(row)
        else:
            out.blocked.append(u.reason)
    return out


def originality_rows(registry: PlatformRulesRegistry, platform: str, placement: str, today: date) -> RowLookup:
    return _lookup(registry, platform, placement, RowOwner.ZBC_PLATFORM_RULES, ORIGINALITY_KEYS, today)


def length_rows(registry: PlatformRulesRegistry, platform: str, placement: str, today: date) -> RowLookup:
    """Spec rows are owned by ZBM Placement Spec; ZBC only reads them."""
    return _lookup(registry, platform, placement, RowOwner.ZBM_PLACEMENT_SPEC, LENGTH_KEYS, today)


def rows_usable(registry: PlatformRulesRegistry, row_ids: tuple[str, ...] | list[str], today: date) -> list[str]:
    """Re-check before use: the reasons any of these rows can't be relied on today."""
    reasons = []
    for rid in row_ids:
        row = registry.rows.get(rid)
        if row is None:
            reasons.append(f"row {rid} no longer exists in the registry")
            continue
        u = check_usable(row, today)
        if not u.usable:
            reasons.append(u.reason)
    return reasons


def write_originality_row(
    registry: PlatformRulesRegistry, recorder: EvidenceRecorder, actors: ActorRegistry, actor_id: str, row: RegistryRow
) -> str:
    """Create/replace an ORIGINALITY row. Only the ZBC Platform Rules owner
    may; a ZBM spec row can never be written from here. Ledger first."""
    actors.require_role(actor_id, Role.REGISTRY_ZBC_PLATFORM_RULES)
    previous = registry.check_write(row, RowOwner.ZBC_PLATFORM_RULES)
    event_id = recorder.record(
        "registry_row_written", actor_id, row.row_id,
        {"row": row.model_dump(mode="json"), "replaced": previous.model_dump(mode="json") if previous else None},
        f"Originality row {row.row_id} written ({row.status.value}, expires {row.expires_at})",
    )
    registry.commit(row)
    return event_id
