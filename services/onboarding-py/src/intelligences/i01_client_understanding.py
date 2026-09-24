"""
Intelligence 1 — Client Understanding.

Decides: the structured client profile, a confidence per field, and the
short list of gaps still worth asking about.

Rules (explicit, deterministic):
- Confidence comes from provenance: contract -> very_high; client_confirmed
  and account_pull -> high; client_stated -> medium; website and inferred ->
  low.
- Two sources disagreeing on a field => the field is marked ``conflict``,
  confidence drops to low, the higher-provenance value is kept as the
  prefill, and the field becomes a "confirm" gap. Nothing is silently
  picked as truth.
- Minimum data: only the fields the lane needs are kept; anything else is
  dropped and reported in ``dropped_fields``.
- Gaps: missing, conflicting or low-confidence fields, ranked by the
  lane's priority order, capped to a short list (config, default 5).
  Low-confidence and conflicting gaps carry a prefill (P13: confirm, don't
  type).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from onboarding_schema import (
    ClientProfile,
    DecisionConfidence,
    Lane,
    ProfileField,
    ProfileGap,
    Provenance,
)

from ._status import PHASE1_STATUS

NUMBER = 1
NAME = "Client Understanding"
PHASE = 1
STATUS = PHASE1_STATUS

PROVENANCE_CONFIDENCE = {
    Provenance.CONTRACT: DecisionConfidence.VERY_HIGH,
    Provenance.CLIENT_CONFIRMED: DecisionConfidence.HIGH,
    Provenance.ACCOUNT_PULL: DecisionConfidence.HIGH,
    Provenance.CLIENT_STATED: DecisionConfidence.MEDIUM,
    Provenance.WEBSITE: DecisionConfidence.LOW,
    Provenance.INFERRED: DecisionConfidence.LOW,
}
PROVENANCE_RANK = {p: i for i, p in enumerate([
    Provenance.INFERRED, Provenance.WEBSITE, Provenance.CLIENT_STATED,
    Provenance.ACCOUNT_PULL, Provenance.CLIENT_CONFIRMED, Provenance.CONTRACT,
])}

# Lane field sets, in the order they are worth asking about.
LANE_FIELDS: dict[Lane, list[str]] = {
    Lane.CLIENT: [
        "business_name", "login_holder", "time_zone", "primary_goal", "vertical",
        "platforms", "monthly_revenue_usd", "preferred_channel", "business_hours",
        "quiet_hours", "website",
    ],
    Lane.ZBC_BRAND: [
        "business_name", "login_holder", "time_zone", "campaign_goal", "regulated_industry",
        "distribution_preference", "platforms", "preferred_channel", "business_hours", "website",
    ],
    Lane.ZBC_CREATOR: [
        "legal_name", "date_of_birth", "platforms", "time_zone", "preferred_channel", "content_niche",
    ],
}


@dataclass(frozen=True)
class Fact:
    field: str
    value: Any
    provenance: Provenance
    evidence: str
    observed_at: datetime


def build_profile(client_id: str, lane: Lane, facts: list[Fact], short_list_size: int = 5) -> ClientProfile:
    allowed = LANE_FIELDS[lane]
    dropped = sorted({f.field for f in facts if f.field not in allowed})
    by_field: dict[str, list[Fact]] = {}
    for f in facts:
        if f.field in allowed and f.value not in (None, "", []):
            by_field.setdefault(f.field, []).append(f)

    fields: dict[str, ProfileField] = {}
    for name, fs in by_field.items():
        # Highest provenance wins; among equal provenance, the latest observation.
        best = max(fs, key=lambda f: (PROVENANCE_RANK[f.provenance], f.observed_at))
        distinct = []
        for f in fs:
            if f.value not in distinct:
                distinct.append(f.value)
        conflict = len(distinct) > 1 and not _confirmed_resolves(fs, best)
        fields[name] = ProfileField(
            name=name,
            value=best.value,
            confidence=DecisionConfidence.LOW if conflict else PROVENANCE_CONFIDENCE[best.provenance],
            provenance=best.provenance,
            evidence=best.evidence,
            observed_at=best.observed_at,
            conflict=conflict,
            conflicting_values=[v for v in distinct if v != best.value] if conflict else [],
        )

    gaps: list[ProfileGap] = []
    for priority, name in enumerate(allowed, start=1):
        pf = fields.get(name)
        if pf is None:
            gaps.append(ProfileGap(field=name, priority=priority, reason="missing"))
        elif pf.conflict:
            gaps.append(ProfileGap(field=name, priority=priority, reason="conflict", prefill=pf.value))
        elif pf.confidence == DecisionConfidence.LOW:
            gaps.append(ProfileGap(field=name, priority=priority, reason="low_confidence", prefill=pf.value))
    # conflicts first (they block decisions), then priority order
    gaps.sort(key=lambda g: (0 if g.reason == "conflict" else 1, g.priority))
    return ClientProfile(
        client_id=client_id, lane=lane, fields=fields, gaps=gaps[:short_list_size], dropped_fields=dropped
    )


def _confirmed_resolves(fs: list[Fact], best: Fact) -> bool:
    """A value the client explicitly confirmed (or the contract states) and
    that is newer than every disagreeing fact resolves the conflict."""
    if best.provenance not in (Provenance.CLIENT_CONFIRMED, Provenance.CONTRACT):
        return False
    return all(f.observed_at <= best.observed_at for f in fs if f.value != best.value)
