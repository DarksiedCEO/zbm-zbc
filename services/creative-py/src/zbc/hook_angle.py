"""
ZBC intelligence 3 — Hook and Angle.

Job: for each moment on the Moment Map, a sheet per APPROVED angle with
example opening hooks for the first two seconds.
Decides: which hook lines are usable for a moment/angle, and why the
others are not.

Rules (deterministic):
H1  angles come ONLY from the rulebook version's approved angles, and only
    those the moment matched; asking for any other angle is refused;
H2  candidate hooks = the angle's approved hook lines (from the signed
    rulebook) + the moment's own opening words (a verbatim quote of the
    first HOOK_MAX_WORDS words of its transcript) — nothing is invented;
H3  a hook must be speakable in the first two seconds: at most
    HOOK_MAX_WORDS words (~3 spoken words per second);
H4  a hook containing any never-say phrase is dropped (with the rule id);
H5  duplicates (after normalisation) are dropped.
"""

from __future__ import annotations

from pydantic import BaseModel

from shared.errors import GuardrailViolation
from shared.text import mentions_phrase, normalize
from zbc.rulebook import Rulebook, RuleKind
from zbc.source_mining import Moment, MomentMap

HOOK_WINDOW_SECONDS = 2.0
HOOK_MAX_WORDS = 6


class HookSheet(BaseModel):
    moment_id: str
    angle_id: str
    angle_name: str
    hooks: list[str]
    rejected_hooks: list[dict]


def _opening_quote(transcript: str) -> str | None:
    words = transcript.split()
    if not words:
        return None
    return " ".join(words[:HOOK_MAX_WORDS])


def sheet_for(moment: Moment, angle_id: str, rb: Rulebook) -> HookSheet:
    angle = rb.angle(angle_id)
    if angle is None:
        raise GuardrailViolation(f"H1 angle {angle_id!r} is not an approved angle in {rb.campaign_id} v{rb.version}")
    if angle_id not in moment.matched_angle_ids:
        raise GuardrailViolation(f"H1 moment {moment.moment_id} did not match angle {angle_id}")
    never = [(r.rule_id, r.params.get("phrase", "")) for r in rb.rules_of(RuleKind.NEVER_SAY)]
    candidates = list(angle.approved_hook_lines)
    quote = _opening_quote(moment.transcript)
    if quote:
        candidates.append(quote)
    hooks: list[str] = []
    rejected: list[dict] = []
    seen: set[str] = set()
    for h in candidates:
        n = normalize(h)
        if n in seen:
            rejected.append({"hook": h, "reason": "H5 duplicate"})
            continue
        seen.add(n)
        if len(n.split()) > HOOK_MAX_WORDS:
            rejected.append({"hook": h, "reason": f"H3 more than {HOOK_MAX_WORDS} words; won't land in {HOOK_WINDOW_SECONDS}s"})
            continue
        broken = [rid for rid, p in never if mentions_phrase(h, p)]
        if broken:
            rejected.append({"hook": h, "reason": f"H4 breaks never-say {', '.join(broken)}"})
            continue
        hooks.append(h)
    return HookSheet(moment_id=moment.moment_id, angle_id=angle.angle_id, angle_name=angle.name,
                     hooks=hooks, rejected_hooks=rejected)


def sheets(moment_map: MomentMap, rb: Rulebook) -> list[HookSheet]:
    if (moment_map.campaign_id, moment_map.rulebook_version) != (rb.campaign_id, rb.version):
        raise GuardrailViolation("moment map was built under a different rulebook version")
    return [sheet_for(m, aid, rb) for m in moment_map.moments for aid in m.matched_angle_ids]
