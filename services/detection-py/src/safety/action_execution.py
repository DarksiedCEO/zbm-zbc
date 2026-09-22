"""
Action Execution Agent — the ONLY agent permitted to actually execute a
fix on a client's live platform, per Decision 1's one-job-per-agent
split from the Trust Graduation Agent (which only judges eligibility,
never acts).

Status as of this build: SCOPED, NOT LIVE. There is no connected client
store (ZBM is pre-revenue on Revenue Recovery), so there is nothing for
this agent to execute against yet, and no action type has been graduated
to AUTONOMOUS mode for any client — the graduation history in
trust_graduation.py has no real client data behind it either. Building a
fake "successful write to Shopify" here would violate the same
no-fabrication discipline as Decision 6's data-source rule.

What IS real and tested here: the gate. This agent's execute() refuses
to run unless the caller-supplied ActionTypeHistory shows AUTONOMOUS mode
for that exact client_id + action_type, checked via the real
trust_graduation.evaluate() logic — not a separate, possibly-diverging
copy of the threshold numbers. That gate is the actual safety mechanism
Decision 1 exists to guarantee, and it's what's under test.
"""

from __future__ import annotations

from pydantic import BaseModel

from safety.trust_graduation import ActionTypeHistory, AutonomyMode


class ActionRequest(BaseModel):
    client_id: str
    action_type: str
    finding_id: str
    description: str


class ActionRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ActionExecutionResult(BaseModel):
    client_id: str
    action_type: str
    finding_id: str
    executed: bool
    note: str


def execute(request: ActionRequest, history: ActionTypeHistory) -> ActionExecutionResult:
    """
    Refuses unless `history` (the actual, current graduation record for
    this exact client_id + action_type) shows AUTONOMOUS mode. Mismatched
    client_id/action_type between the request and the supplied history is
    also refused — the caller must prove it's checking the right record,
    not just any AUTONOMOUS record.
    """
    if history.client_id != request.client_id or history.action_type != request.action_type:
        raise ActionRefused(
            f"History record ({history.client_id}/{history.action_type}) does not match "
            f"the requested action ({request.client_id}/{request.action_type}) — refusing "
            f"rather than trusting an unrelated graduation record."
        )

    if history.current_mode != AutonomyMode.AUTONOMOUS:
        raise ActionRefused(
            f"client={request.client_id} action_type={request.action_type} is in "
            f"{history.current_mode.value} mode, not autonomous — this agent will not execute. "
            f"Decision 1's graduation path has not been completed for this action type."
        )

    # No live store connection exists yet (pre-revenue) — there is nothing
    # real to execute against. Once a real platform connector exists, this
    # is the one place in the codebase permitted to call it.
    return ActionExecutionResult(
        client_id=request.client_id,
        action_type=request.action_type,
        finding_id=request.finding_id,
        executed=False,
        note=(
            "Gate passed (client is graduated to autonomous for this action type), but no "
            "live platform connector exists yet — there is nothing to execute against. "
            "This is expected pre-revenue behavior, not a bug."
        ),
    )
