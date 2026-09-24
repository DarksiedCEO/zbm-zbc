"""
P3 — permission receipt: a plain note of what ZBM can see, what it can't,
and how to revoke. Built from the same dated platform facts as
intelligence 4, so it inherits their quotability: an unverified or stale
fact produces a receipt marked ``quotable=False`` that must not be sent
until a person verifies the facts.
"""

from __future__ import annotations

from datetime import date

from guardrails import check_outbound
from intelligences.i04_platform_access import facts_for, facts_quotable
from onboarding_schema import AccessGrantIn, PermissionReceipt


def build_receipt(grant: AccessGrantIn, today: date, shelf_life_days: int, knowledge: dict | None = None) -> tuple[PermissionReceipt | None, bool, str]:
    f = facts_for(grant.platform, knowledge)
    if f is None:
        return None, False, "no facts for this platform"
    label = grant.platform.value.replace("_", " ").title()
    can_see = f.can_see.get(grant.granted_role) or f.can_see.get(f.job_role.get(grant.job, ""), []) or [
        f"what the '{grant.granted_role}' access level allows in {label}"
    ]
    text = "\n".join(
        [f"Permission receipt for {label} account {grant.account_id}.", "What ZBM can see:"]
        + [f"- {x}" for x in can_see]
        + ["What ZBM cannot see:"] + [f"- {x}" for x in f.cannot_see]
        + ["How to revoke at any time:"] + [f"- {x}" for x in f.revoke_steps]
        + ["We only use this access for the work in your contract, and we never change anything without your yes for that specific change."]
    )
    ok, why = facts_quotable(f, today, shelf_life_days)
    receipt = PermissionReceipt(
        platform=grant.platform, can_see=list(can_see), cannot_see=list(f.cannot_see),
        how_to_revoke=list(f.revoke_steps), text=check_outbound(text),
    )
    return receipt, ok, why
