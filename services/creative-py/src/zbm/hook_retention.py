"""
ZBM intelligence 6 — Hook and Retention.

Job: advise makers which opening (hook type) holds attention, from ZBM's
OWN MEASURED results only.
Decides: a ranked list of hook types for a platform/placement, or "no
advice" when the measured evidence is too thin. It never falls back to
generic best practice or invented numbers.

Rule: group measured results by hook_type; a hook type needs at least
MIN_SAMPLES results carrying the metric; rank by the MEDIAN of the metric
(median, so one outlier can't carry a hook type); ties -> name order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from zbm.results import PerformanceResult, measured_or_reason

DEFAULT_METRIC = "hook_hold_rate_3s"
MIN_SAMPLES = 3


@dataclass(frozen=True)
class HookAdvice:
    hook_type: str
    median: float
    samples: int


@dataclass
class HookAdviceReport:
    platform: str
    placement: str
    metric: str
    ranked: list[HookAdvice] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    note: str = ""


def advise(results: list[PerformanceResult], platform: str, placement: str, metric: str = DEFAULT_METRIC) -> HookAdviceReport:
    rep = HookAdviceReport(platform, placement, metric)
    by_hook: dict[str, list[float]] = {}
    for r in results:
        if (r.platform, r.placement) != (platform, placement):
            continue
        ok, reason = measured_or_reason(r)
        if not ok:
            rep.excluded.append(reason)
            continue
        if metric not in r.metrics:
            rep.excluded.append(f"{r.result_id}: no {metric!r} metric")
            continue
        by_hook.setdefault(r.hook_type, []).append(r.metrics[metric])
    for hook, values in by_hook.items():
        if len(values) < MIN_SAMPLES:
            rep.excluded.append(f"hook type {hook!r}: {len(values)} measured sample(s), need {MIN_SAMPLES}")
            continue
        rep.ranked.append(HookAdvice(hook, median(values), len(values)))
    rep.ranked.sort(key=lambda a: (-a.median, a.hook_type))
    rep.note = (
        f"ranked by median {metric} over ZBM's own measured results"
        if rep.ranked else "no advice: not enough measured results (advice is never invented)"
    )
    return rep
