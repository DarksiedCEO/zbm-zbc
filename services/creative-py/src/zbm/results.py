"""
Performance results as ZBM receives them. Not an intelligence — the
shared input model for Hook and Retention (6) and Creative Memory (7).

Only `provenance == "measured"` results with a named measurement source
AND a measurement reference count. Self-reported or estimated numbers are
accepted as input (so they can be explicitly rejected and counted), but
nothing downstream learns from them. Metrics are rates/counts, never money.
"""

from __future__ import annotations

import math
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.types import NonEmptyStr, SafeId


class ResultProvenance(str, Enum):
    MEASURED = "measured"
    SELF_REPORTED = "self_reported"
    ESTIMATED = "estimated"


class PerformanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: SafeId
    client_id: SafeId
    creative_id: SafeId
    vertical: NonEmptyStr
    platform: NonEmptyStr
    placement: NonEmptyStr
    hook_type: NonEmptyStr
    provenance: ResultProvenance
    measurement_source: str | None = None
    measurement_ref: str | None = None
    impressions: int = Field(ge=0)
    metrics: dict[str, float]

    @field_validator("metrics")
    @classmethod
    def _finite_non_negative(cls, v: dict[str, float]) -> dict[str, float]:
        for k, x in v.items():
            if isinstance(x, bool) or not math.isfinite(x) or x < 0:
                raise ValueError(f"metric {k!r} must be a finite non-negative number")
        return v


def measured_or_reason(r: PerformanceResult) -> tuple[bool, str]:
    if r.provenance is not ResultProvenance.MEASURED:
        return False, f"{r.result_id}: provenance {r.provenance.value!r}; only measured results are used"
    if not r.measurement_source or not r.measurement_ref:
        return False, f"{r.result_id}: measured result without a measurement source and reference"
    return True, "measured"
