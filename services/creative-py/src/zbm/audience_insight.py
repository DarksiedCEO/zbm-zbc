"""
ZBM intelligence 3 — Audience Insight.

Job: find THE one human truth for the brief's `insight` field.
Decides: which single candidate insight (if any) the evidence supports.

Rule (deterministic):
- a candidate needs evidence from at least MIN_DISTINCT_SOURCES distinct
  source ids; otherwise it is rejected with a reason;
- among qualifying candidates, most distinct sources wins; ties go to more
  `measured` evidence, then to the lexicographically smallest statement
  (so the answer never depends on input order);
- no qualifying candidate -> NO insight (the brief stays incomplete). It
  never writes an insight of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.types import NonEmptyStr, SafeId

MIN_DISTINCT_SOURCES = 2


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: SafeId
    kind: Literal["interview", "survey", "research", "measured"]
    ref: NonEmptyStr


class InsightCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: NonEmptyStr
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class InsightSelection:
    insight: str | None
    reason: str
    rejected: tuple[tuple[str, str], ...]


def select_insight(candidates: list[InsightCandidate]) -> InsightSelection:
    qualifying = []
    rejected = []
    for c in candidates:
        sources = {e.source_id for e in c.evidence}
        if len(sources) < MIN_DISTINCT_SOURCES:
            rejected.append((c.statement, f"only {len(sources)} distinct source(s); need {MIN_DISTINCT_SOURCES}"))
            continue
        measured = sum(1 for e in c.evidence if e.kind == "measured")
        qualifying.append((-len(sources), -measured, c.statement, c))
    if not qualifying:
        return InsightSelection(None, "no candidate insight is supported by enough independent evidence", tuple(rejected))
    qualifying.sort(key=lambda t: (t[0], t[1], t[2]))
    best = qualifying[0][3]
    for _, _, stmt, _ in qualifying[1:]:
        rejected.append((stmt, "outranked: the brief carries exactly one insight"))
    n = len({e.source_id for e in best.evidence})
    return InsightSelection(best.statement, f"supported by {n} distinct sources", tuple(rejected))
