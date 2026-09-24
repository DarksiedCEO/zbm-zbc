"""
Intelligence 13 — Learning Loop and Client Health.

Phase split (locked build order):
- PHASE 1 (on from day one): LOGGING. Every escalation is logged with its
  snag and resolution (``escalation_log_entry``), into institutional
  memory with identifiers stripped, and to the ledger by the service.
- PHASE 2: the live health score + early warning (``health``) and
  PROPOSED rules (``propose_rules``). A proposal is only a proposal: it
  never changes the playbook. Only Andre can approve a rule, with an
  explicit approval token (memory.Playbook), and every version is kept.

Health score (explicit, 0–100, draft weights — tuned only via the
playbook approval path): start at 100; -10 per stall; -8 per escalation;
-15 per breached commitment; -20 if the recommend score is 6 or lower;
-10 if there has been no progress for longer than the stuck window;
+5 once the first real win is delivered (capped at 100, floored at 0).
Bands: >= 75 green, >= 50 yellow, else red. Early warning fires on red,
or on a drop of 15+ points since the previous score.

The P10 scorecard (time to first win, stalls, escalations per client) is
returned alongside.

Phase 2 parts are NOT certified for real clients until phase 1 is
certified and they pass all four certification types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ._status import PHASE1_STATUS, PHASE2_STATUS

NUMBER = 13
NAME = "Learning Loop and Client Health"
PHASE = "1 (logging) / 2 (health score, proposed rules)"
STATUS = f"logging: {PHASE1_STATUS}. health score and proposed rules: {PHASE2_STATUS}"

PROPOSAL_MIN_OCCURRENCES = 3


def escalation_log_entry(lane: str, trigger: str, snag_category: str, attempted: Optional[str], resolution: str) -> dict:
    """The learning-loop record for one resolved escalation. Contains no
    client identifiers by construction; the service additionally runs it
    through memory.strip_identifiers before storing."""
    return {
        "lane": lane,
        "trigger": trigger,
        "snag_category": snag_category,
        "attempted": attempted or "none (hard trigger)",
        "resolution": resolution,
    }


def propose_rules(patterns: list[dict]) -> list[dict]:
    """Snag categories that recur PROPOSAL_MIN_OCCURRENCES+ times become
    proposals for Andre. Never applied here."""
    counts: dict[tuple[str, str], int] = {}
    for p in patterns:
        key = (p.get("trigger", "?"), p.get("snag_category", "?"))
        counts[key] = counts.get(key, 0) + 1
    out = []
    for (trigger, cat), n in sorted(counts.items()):
        if n >= PROPOSAL_MIN_OCCURRENCES:
            out.append({
                "proposed_rule_id": f"auto_{trigger}_{cat}"[:64],
                "text": f"Recurring snag '{cat}' on trigger '{trigger}' ({n} times): add a proactive step to prevent it.",
                "occurrences": n,
                "status": "proposed — needs Andre's approval token to enter the playbook",
            })
    return out


@dataclass(frozen=True)
class Health:
    score: int
    band: str
    early_warning: bool
    reasons: tuple[str, ...]
    scorecard: dict


def health(
    stalls: int,
    escalations: int,
    breached_commitments: int,
    recommend_score: Optional[int],
    hours_since_progress: float,
    stuck_window_hours: int,
    first_win_at: Optional[datetime],
    started_at: datetime,
    previous_score: Optional[int] = None,
) -> Health:
    score = 100
    reasons = []
    if stalls:
        score -= 10 * stalls
        reasons.append(f"{stalls} stall(s)")
    if escalations:
        score -= 8 * escalations
        reasons.append(f"{escalations} escalation(s)")
    if breached_commitments:
        score -= 15 * breached_commitments
        reasons.append(f"{breached_commitments} breached commitment(s)")
    if recommend_score is not None and recommend_score <= 6:
        score -= 20
        reasons.append(f"recommend score {recommend_score}")
    if hours_since_progress > stuck_window_hours:
        score -= 10
        reasons.append(f"no progress for {hours_since_progress:.0f}h")
    if first_win_at is not None:
        score += 5
        reasons.append("first real win delivered")
    score = max(0, min(100, score))
    band = "green" if score >= 75 else "yellow" if score >= 50 else "red"
    warning = band == "red" or (previous_score is not None and previous_score - score >= 15)
    ttfw = None if first_win_at is None else round((first_win_at - started_at).total_seconds() / 86400, 2)
    return Health(
        score=score, band=band, early_warning=warning, reasons=tuple(reasons),
        scorecard={"time_to_first_win_days": ttfw, "stalls": stalls, "escalations": escalations},
    )
