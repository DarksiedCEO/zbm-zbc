"""
Actor identities and roles.

Every approve/draft action names an actor. The rules enforced here:
- the actor must be known to the registry;
- the actor must hold the role the action needs;
- the approver can never be the drafter (`require_not_self`), even when a
  single human legitimately holds both roles.

Identity is AUTHENTICATED (fix wave 2, N4): the API resolves the acting
actor from that actor's own credential (`X-Creative-Actor-Token`, tokens
configured server-side in CREATIVE_ACTOR_TOKENS; see api.py), never from
the request body, so drafter!=approver is enforced on proven identity.
Two actors who share one person's token are one identity. Andre's
approvals need his separate founder token (`shared/founder.py`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum

from shared.errors import GuardrailViolation


class Role(str, Enum):
    ZBM_BRIEF_WRITER = "zbm_brief_writer"
    ZBM_CREATIVE_LEAD = "zbm_creative_lead"
    ZBM_CREATIVE_QUALITY = "zbm_creative_quality"
    ZBC_RULEBOOK_WRITER = "zbc_rulebook_writer"
    ZBC_CAMPAIGN_RULEBOOK = "zbc_campaign_rulebook"
    ZBC_CLIP_HUMAN_REVIEWER = "zbc_clip_human_reviewer"
    REGISTRY_ZBM_PLACEMENT_SPEC = "registry_zbm_placement_spec"
    REGISTRY_ZBC_PLATFORM_RULES = "registry_zbc_platform_rules"
    RIGHTS_RECORDER = "rights_recorder"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    roles: frozenset[Role]


# Each intelligence acts under its own actor id; humans can be added via
# CREATIVE_EXTRA_ACTORS (JSON: {"actor_id": ["role", ...]}).
DEFAULT_ACTORS: dict[str, frozenset[Role]] = {
    "zbm_brief_writer": frozenset({Role.ZBM_BRIEF_WRITER}),
    "zbm_creative_lead": frozenset({Role.ZBM_CREATIVE_LEAD}),
    "zbm_creative_quality": frozenset({Role.ZBM_CREATIVE_QUALITY}),
    "zbm_placement_spec": frozenset({Role.REGISTRY_ZBM_PLACEMENT_SPEC}),
    "zbc_rulebook_writer": frozenset({Role.ZBC_RULEBOOK_WRITER}),
    "zbc_campaign_rulebook": frozenset({Role.ZBC_CAMPAIGN_RULEBOOK}),
    "zbc_platform_rules": frozenset({Role.REGISTRY_ZBC_PLATFORM_RULES}),
    "zbc_clip_human_reviewer": frozenset({Role.ZBC_CLIP_HUMAN_REVIEWER}),
    "rights_desk": frozenset({Role.RIGHTS_RECORDER}),
}


@dataclass
class ActorRegistry:
    actors: dict[str, frozenset[Role]] = field(default_factory=lambda: dict(DEFAULT_ACTORS))

    @classmethod
    def from_env(cls) -> "ActorRegistry":
        reg = cls()
        raw = os.environ.get("CREATIVE_EXTRA_ACTORS")
        if raw:
            data = json.loads(raw)
            for actor_id, roles in data.items():
                reg.add(actor_id, [Role(r) for r in roles])
        return reg

    def add(self, actor_id: str, roles: list[Role]) -> None:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", actor_id or ""):
            raise ValueError(f"actor id {actor_id!r} must match [a-z0-9_]{{1,64}} (ledger actor format)")
        if actor_id == "andre":
            # Andre is never an asserted actor; he acts only via the founder token.
            raise ValueError("'andre' is reserved for founder-token approvals")
        self.actors[actor_id] = frozenset(roles)

    def get(self, actor_id: str) -> Actor:
        roles = self.actors.get(actor_id)
        if roles is None:
            raise GuardrailViolation(f"unknown actor {actor_id!r}")
        return Actor(actor_id, roles)

    def require_role(self, actor_id: str, role: Role) -> Actor:
        actor = self.get(actor_id)
        if role not in actor.roles:
            raise GuardrailViolation(f"actor {actor_id!r} does not hold role {role.value!r}")
        return actor


def require_not_self(drafter: str, approver: str, what: str) -> None:
    if drafter == approver:
        raise GuardrailViolation(
            f"self-approval refused: {approver!r} drafted this {what} and can never approve it"
        )
