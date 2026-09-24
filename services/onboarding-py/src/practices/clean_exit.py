"""
P4 — clean exit plan: at contract end, access revoked, vault secrets
destroyed, reports exported, client memory exported or destroyed.

This builds the PLAN (a checklist with owners and the exact revoke steps).
Executing it needs platform write access and a real vault, neither of
which exists; the plan says so rather than pretending.
"""

from __future__ import annotations

from intelligences.i04_platform_access import facts_for
from onboarding_schema import AccessGrantIn


def exit_plan(client_id: str, grants: list[AccessGrantIn], vault_certified: bool, memory_choice: str = "export_then_destroy") -> dict:
    steps = []
    for g in grants:
        f = facts_for(g.platform)
        steps.append({
            "action": "revoke_access",
            "platform": g.platform.value,
            "account_id": g.account_id,
            "how": list(f.revoke_steps) if f else ["platform not certified; Andre to confirm revoke path"],
            "executor": "client or ZBM operator (automated revoke not wired)",
        })
    steps.append({
        "action": "destroy_vault_secrets",
        "detail": "vault stand-in has never stored a credential for this client; nothing to destroy"
        if not vault_certified else "destroy all per-client keys and secrets; log the destruction",
    })
    steps.append({"action": "export_reports", "detail": "export every report delivered to the client (export job not wired)"})
    steps.append({"action": "client_memory", "detail": memory_choice})
    steps.append({"action": "confirm_to_client", "detail": "send written confirmation of each step above"})
    return {"client_id": client_id, "steps": steps}
