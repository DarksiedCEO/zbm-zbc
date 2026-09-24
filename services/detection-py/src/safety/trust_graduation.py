"""
Trust Graduation Agent — implements Decision 1's numeric graduation path.
Sole job: given an action type's history for a given client, decide
whether it has earned promotion to the next autonomy level. This agent
NEVER executes a fix itself (that's the Action Execution Agent's job) —
it only tracks accuracy history and decides on graduation.

Thresholds are the exact numbers locked in the founder-decisions doc
(the single highest-consequence AEGIS finding closed this session):
  shadow -> human_approval:  >=20 shadow decisions, >=95% agreement
  human_approval -> autonomous: >=30 human-approved executions,
                                  zero reversals, spanning >=30 real days
"""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, model_validator

SHADOW_MODE_MIN_DECISIONS = 20
SHADOW_MODE_MIN_AGREEMENT = 0.95
HUMAN_APPROVAL_MIN_EXECUTIONS = 30
HUMAN_APPROVAL_MIN_WINDOW_DAYS = 30


class AutonomyMode(str, Enum):
    SHADOW = "shadow"
    HUMAN_APPROVAL = "human_approval"
    AUTONOMOUS = "autonomous"


class ActionTypeHistory(BaseModel):
    """
    The accuracy/execution history for ONE action type, for ONE client.
    Graduation is evaluated per action type per client, never blanket
    (Decision 1, explicit).
    """
    client_id: str
    action_type: str
    current_mode: AutonomyMode

    # Shadow-mode history
    shadow_decisions_made: int = Field(ge=0, default=0)
    shadow_decisions_agreed_with_human: int = Field(ge=0, default=0)

    # Human-approval-mode history
    human_approved_executions: int = Field(ge=0, default=0)
    human_approved_reversals_or_complaints: int = Field(ge=0, default=0)
    human_approval_window_start: date | None = None
    human_approval_window_end: date | None = None

    @model_validator(mode="after")
    def _history_is_consistent(self):
        # Fix wave 3: an impossible history must not be judged. More
        # agreements than decisions made the agreement rate exceed 100% and
        # could graduate an action type (20 decisions, 40 "agreed" -> 200%).
        if self.shadow_decisions_agreed_with_human > self.shadow_decisions_made:
            raise ValueError("shadow_decisions_agreed_with_human cannot exceed shadow_decisions_made")
        if (self.human_approval_window_start is not None and self.human_approval_window_end is not None
                and self.human_approval_window_end < self.human_approval_window_start):
            raise ValueError("human_approval_window_end cannot be before human_approval_window_start")
        return self

    @property
    def shadow_agreement_rate(self) -> float | None:
        if self.shadow_decisions_made == 0:
            return None
        return self.shadow_decisions_agreed_with_human / self.shadow_decisions_made

    @property
    def human_approval_window_days(self) -> int | None:
        if self.human_approval_window_start is None or self.human_approval_window_end is None:
            return None
        return (self.human_approval_window_end - self.human_approval_window_start).days


class GraduationDecision(BaseModel):
    client_id: str
    action_type: str
    current_mode: AutonomyMode
    eligible_for_promotion: bool
    reason: str


def evaluate(history: ActionTypeHistory) -> GraduationDecision:
    """
    Pure decision function — no side effects, no mutation. Caller (the
    orchestrator/registry) is responsible for actually applying a
    promotion; this agent's whole job is the accuracy-history judgment
    call, kept separate from the action of promoting or executing.
    """
    if history.current_mode == AutonomyMode.SHADOW:
        rate = history.shadow_agreement_rate
        if rate is None:
            return GraduationDecision(
                client_id=history.client_id, action_type=history.action_type,
                current_mode=history.current_mode, eligible_for_promotion=False,
                reason="No shadow-mode decisions recorded yet.",
            )
        if history.shadow_decisions_made >= SHADOW_MODE_MIN_DECISIONS and rate >= SHADOW_MODE_MIN_AGREEMENT:
            return GraduationDecision(
                client_id=history.client_id, action_type=history.action_type,
                current_mode=history.current_mode, eligible_for_promotion=True,
                reason=(
                    f"{history.shadow_decisions_made} shadow decisions with "
                    f"{rate:.1%} agreement clears the {SHADOW_MODE_MIN_DECISIONS}-decision / "
                    f"{SHADOW_MODE_MIN_AGREEMENT:.0%} threshold — eligible for human-approval mode."
                ),
            )
        return GraduationDecision(
            client_id=history.client_id, action_type=history.action_type,
            current_mode=history.current_mode, eligible_for_promotion=False,
            reason=(
                f"{history.shadow_decisions_made}/{SHADOW_MODE_MIN_DECISIONS} decisions, "
                f"{rate:.1%}/{SHADOW_MODE_MIN_AGREEMENT:.0%} agreement — threshold not yet met."
            ),
        )

    if history.current_mode == AutonomyMode.HUMAN_APPROVAL:
        window_days = history.human_approval_window_days
        clean = history.human_approved_reversals_or_complaints == 0
        enough_executions = history.human_approved_executions >= HUMAN_APPROVAL_MIN_EXECUTIONS
        enough_window = window_days is not None and window_days >= HUMAN_APPROVAL_MIN_WINDOW_DAYS

        if enough_executions and clean and enough_window:
            return GraduationDecision(
                client_id=history.client_id, action_type=history.action_type,
                current_mode=history.current_mode, eligible_for_promotion=True,
                reason=(
                    f"{history.human_approved_executions} clean human-approved executions over "
                    f"{window_days} days clears the {HUMAN_APPROVAL_MIN_EXECUTIONS}-execution / "
                    f"{HUMAN_APPROVAL_MIN_WINDOW_DAYS}-day / zero-reversal bar — eligible for autonomous execution."
                ),
            )

        reasons = []
        if not enough_executions:
            reasons.append(f"{history.human_approved_executions}/{HUMAN_APPROVAL_MIN_EXECUTIONS} executions")
        if not clean:
            reasons.append(f"{history.human_approved_reversals_or_complaints} reversal(s)/complaint(s) — must be zero")
        if not enough_window:
            reasons.append(f"window is {window_days} days, needs >= {HUMAN_APPROVAL_MIN_WINDOW_DAYS}")
        return GraduationDecision(
            client_id=history.client_id, action_type=history.action_type,
            current_mode=history.current_mode, eligible_for_promotion=False,
            reason="Not yet eligible: " + "; ".join(reasons),
        )

    # Already autonomous — nothing further to graduate.
    return GraduationDecision(
        client_id=history.client_id, action_type=history.action_type,
        current_mode=history.current_mode, eligible_for_promotion=False,
        reason="Already at autonomous execution — no further graduation applies.",
    )
