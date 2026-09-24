"""
ZBM intelligence 7 — Creative Memory.

Job: remember the client's brand, the feedback given on work, and past
winners.
Decides: whether a result is admitted to the winners library.

A result is a WINNER only if it is MEASURED (see zbm/results.py) and meets
the brief's own numeric success target for that metric. Self-reported or
estimated numbers are rejected and never stored as winners. Feedback is
stored as given by named reviewers (it is feedback, not performance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from zbm.brief import SuccessMetric
from zbm.results import PerformanceResult, measured_or_reason


@dataclass(frozen=True)
class MemoryDecision:
    learned: bool
    reason: str


@dataclass
class ZbmCreativeMemory:
    winners: dict[str, list[PerformanceResult]] = field(default_factory=dict)
    brand_notes: dict[str, list[str]] = field(default_factory=dict)
    feedback: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def evaluate_winner(self, result: PerformanceResult, target: SuccessMetric) -> MemoryDecision:
        ok, reason = measured_or_reason(result)
        if not ok:
            return MemoryDecision(False, reason)
        if target.metric not in result.metrics:
            return MemoryDecision(False, f"{result.result_id}: no {target.metric!r} metric to compare with the target")
        actual = Decimal(str(result.metrics[target.metric]))
        met = actual >= target.target if target.comparator == ">=" else actual <= target.target
        if not met:
            return MemoryDecision(False, f"{result.result_id}: {target.metric}={actual} does not meet {target.comparator} {target.target}")
        return MemoryDecision(True, f"{result.result_id}: measured {target.metric}={actual} meets {target.comparator} {target.target}")

    def commit_winner(self, result: PerformanceResult) -> None:
        self.winners.setdefault(result.client_id, []).append(result)

    def add_brand_note(self, client_id: str, note: str) -> None:
        self.brand_notes.setdefault(client_id, []).append(note)

    def add_feedback(self, client_id: str, reviewer: str, text: str) -> None:
        self.feedback.setdefault(client_id, []).append((reviewer, text))

    def winners_for(self, client_id: str, platform: str | None = None) -> list[PerformanceResult]:
        return [w for w in self.winners.get(client_id, []) if platform is None or w.platform == platform]
