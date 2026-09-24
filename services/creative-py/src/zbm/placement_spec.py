"""
ZBM intelligence 4 — Placement Spec (+ the Export Validator).

Job: own the SPEC rows of the shared Platform Rules Registry (size, ratio,
length, codec, safe zones — each sourced, dated, expiring) and validate
exports against them.
Decides: (a) whether a brief deliverable references a registry-valid
spec; (b) pass/fail for a finished export, with reasons.

Never guesses:
- a property with no governing registry row is checked against the
  APPROVED BRIEF only, and reported in `registry_coverage_gaps` — it is
  never checked against a remembered or assumed platform value;
- a governing row that is expired/unverified BLOCKS (the export fails
  with the row's reason) — fail closed;
- advisory rows ("recommended") produce warnings, not failures.

Today's registry has length rows only (YouTube Shorts hard max 180 s,
Instagram Reels recommended <= 180 s). Aspect ratio, format, codec and
safe zones have NO sourced rows yet, so they are always coverage gaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.actors import ActorRegistry, Role
from shared.ledger import EvidenceRecorder
from shared.registry import (
    Enforcement,
    PlatformRulesRegistry,
    RegistryRow,
    RowOwner,
    check_usable,
)
from shared.types import SafeId
from zbm.brief import Deliverable

HARD_LENGTH_KEY = "max_length_seconds"
ADVISORY_LENGTH_KEY = "recommended_max_length_seconds"
LENGTH_TOLERANCE_SECONDS = 0.5
UNSOURCED_PROPERTIES = ("aspect_ratio", "format", "codec", "safe_zones")


@dataclass
class SpecCheck:
    row_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _spec_rows(registry: PlatformRulesRegistry, platform: str, placement: str) -> list[RegistryRow]:
    return [r for r in registry.rows_for(platform, placement) if r.owner is RowOwner.ZBM_PLACEMENT_SPEC]


def _length_against_row(row: RegistryRow, length: float, label: str, out: SpecCheck) -> None:
    if row.rule_key not in (HARD_LENGTH_KEY, ADVISORY_LENGTH_KEY) or not isinstance(row.value, int):
        return
    if length > row.value:
        msg = f"{label}: length {length}s exceeds {row.row_id} ({row.rule_key}={row.value}s, {row.source_url})"
        if row.enforcement is Enforcement.HARD_LIMIT:
            out.issues.append(msg)
        else:
            out.warnings.append(msg + " [advisory]")


def check_deliverable_spec(d: Deliverable, registry: PlatformRulesRegistry, today: date) -> SpecCheck:
    """Is this brief deliverable produced to a registry-valid spec?"""
    out = SpecCheck()
    label = f"deliverable {d.deliverable_id}"
    rows = _spec_rows(registry, d.platform, d.placement)
    if not rows:
        other = [r for r in registry.rows_for(d.platform) if check_usable(r, today).usable is False]
        extra = f" ({'; '.join(check_usable(r, today).reason for r in other)})" if other else ""
        out.issues.append(
            f"{label}: no Platform Rules Registry spec rows for {d.platform}/{d.placement}; "
            f"cannot brief to an unsourced spec{extra}"
        )
        return out
    for row in rows:
        u = check_usable(row, today)
        if not u.usable:
            out.issues.append(f"{label}: blocked — {u.reason}")
            continue
        out.row_ids.append(row.row_id)
        _length_against_row(row, d.length_seconds, label, out)
    if not any(r.rule_key in (HARD_LENGTH_KEY, ADVISORY_LENGTH_KEY) for r in rows):
        out.issues.append(f"{label}: no length row for {d.platform}/{d.placement}")
    return out


class DeclaredExport(BaseModel):
    """What the maker declares about a finished file. No media is read
    (no video libraries in this build); these are declarations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: str
    placement: str
    length_seconds: float = Field(gt=0, le=36000)
    aspect_ratio: str
    format: str
    codec: str | None = None
    file_ref: SafeId


class PropertyCheck(BaseModel):
    property: str
    expected: str
    actual: str
    source: str
    result: Literal["pass", "fail", "blocked"]
    reason: str


class ExportValidation(BaseModel):
    verdict: Literal["pass", "fail"]
    checks: list[PropertyCheck]
    warnings: list[str]
    registry_coverage_gaps: list[str]


def validate_export(
    declared: DeclaredExport, deliverable: Deliverable, registry: PlatformRulesRegistry, today: date
) -> ExportValidation:
    checks: list[PropertyCheck] = []
    warnings: list[str] = []

    def add(prop, expected, actual, source, ok, reason):
        checks.append(PropertyCheck(property=prop, expected=str(expected), actual=str(actual), source=source,
                                    result="pass" if ok else "fail", reason=reason))

    add("platform", deliverable.platform, declared.platform, "approved_brief",
        declared.platform == deliverable.platform, "must match the approved brief")
    add("placement", deliverable.placement, declared.placement, "approved_brief",
        declared.placement == deliverable.placement, "must match the approved brief")
    add("length_seconds", f"{deliverable.length_seconds}±{LENGTH_TOLERANCE_SECONDS}", declared.length_seconds,
        "approved_brief", abs(declared.length_seconds - deliverable.length_seconds) <= LENGTH_TOLERANCE_SECONDS,
        "must match the brief's length")
    add("aspect_ratio", deliverable.aspect_ratio, declared.aspect_ratio, "approved_brief",
        declared.aspect_ratio == deliverable.aspect_ratio, "must match the approved brief")
    add("format", deliverable.format, declared.format, "approved_brief",
        declared.format == deliverable.format, "must match the approved brief")

    rows = _spec_rows(registry, deliverable.platform, deliverable.placement)
    if not rows:
        checks.append(PropertyCheck(property="registry", expected="verified spec rows", actual="none",
                                    source="registry", result="blocked",
                                    reason=f"no spec rows for {deliverable.platform}/{deliverable.placement}"))
    for row in rows:
        u = check_usable(row, today)
        if not u.usable:
            checks.append(PropertyCheck(property=row.rule_key, expected=str(row.value), actual="(not evaluated)",
                                        source=f"registry:{row.row_id}", result="blocked", reason=u.reason))
            continue
        if row.rule_key in (HARD_LENGTH_KEY, ADVISORY_LENGTH_KEY) and isinstance(row.value, int):
            within = declared.length_seconds <= row.value
            if row.enforcement is Enforcement.HARD_LIMIT:
                add("length_seconds", f"<= {row.value}", declared.length_seconds, f"registry:{row.row_id}",
                    within, f"{row.rule_key} per {row.source_url}")
            elif not within:
                warnings.append(f"length {declared.length_seconds}s exceeds advisory {row.row_id} ({row.value}s)")

    covered = {r.rule_key for r in rows}
    gaps = [
        f"{p}: no sourced registry row for {deliverable.platform}/{deliverable.placement}; checked against approved brief only"
        for p in UNSOURCED_PROPERTIES if p not in covered
    ]
    verdict = "pass" if all(c.result == "pass" for c in checks) else "fail"
    return ExportValidation(verdict=verdict, checks=checks, warnings=warnings, registry_coverage_gaps=gaps)


def write_spec_row(
    registry: PlatformRulesRegistry, recorder: EvidenceRecorder, actors: ActorRegistry, actor_id: str, row: RegistryRow
) -> str:
    """Create/replace a SPEC row. Only the Placement Spec owner may; a row
    owned by ZBC Platform Rules can never be written from here."""
    actors.require_role(actor_id, Role.REGISTRY_ZBM_PLACEMENT_SPEC)
    previous = registry.check_write(row, RowOwner.ZBM_PLACEMENT_SPEC)
    event_id = recorder.record(
        "registry_row_written", actor_id, row.row_id,
        {"row": row.model_dump(mode="json"), "replaced": previous.model_dump(mode="json") if previous else None},
        f"Spec row {row.row_id} written ({row.status.value}, expires {row.expires_at})",
    )
    registry.commit(row)
    return event_id
