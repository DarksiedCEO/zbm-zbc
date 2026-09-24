"""
ZBM intelligence 8 — Creative Quality.

Job: own the premium bar; judge finished work against the approved brief.
Decides: pass / send back / escalate to Andre.

- Automatic checks against the brief (all deterministic, on the maker's
  declared script — no media is analysed in this build):
    Q1 hook lands in the first HOOK_WINDOW_SECONDS and is non-empty
    Q2 the key message appears (normalised) in the script or supers
    Q3 every mandatory appears in the script or supers
    Q4 every disclosure requirement appears in the disclosure text or supers
- Q5 premium bar: the Quality reviewer may send ANY work back with written
  notes, even when Q1–Q4 pass.
- Review rounds are capped at MAX_ROUNDS (2). A failure in round 2 is
  escalated to Andre instead of opening a third round; a third round is
  refused outright.
- Reports only UPWARD (to the Creative Lead or Andre), never through
  Enigma / Phantom Canvas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.errors import PreconditionFailed
from shared.text import contains_phrase
from zbm.brief import BriefFields

MAX_ROUNDS = 2
HOOK_WINDOW_SECONDS = 2.0


class QualityDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opening_text: str
    hook_ends_at_seconds: float = Field(ge=0)
    script_text: str
    supers: list[str] = []
    disclosure_text: str = ""


@dataclass(frozen=True)
class QualityDecision:
    outcome: Literal["pass", "send_back", "escalate_to_andre"]
    round: int
    findings: list[str] = field(default_factory=list)
    reported_to: Literal["creative_lead", "andre"] = "creative_lead"


def judge(fields: BriefFields, decl: QualityDeclaration, reviewer_notes: list[str], round_number: int) -> QualityDecision:
    if round_number < 1:
        raise ValueError("round numbers start at 1")
    if round_number > MAX_ROUNDS:
        raise PreconditionFailed(
            f"review round cap ({MAX_ROUNDS}) reached; this work was escalated to Andre and gets no further rounds"
        )
    findings: list[str] = []
    body = " ".join([decl.script_text, *decl.supers])
    if not decl.opening_text.strip():
        findings.append("Q1: no opening hook declared")
    if decl.hook_ends_at_seconds > HOOK_WINDOW_SECONDS:
        findings.append(f"Q1: hook ends at {decl.hook_ends_at_seconds}s; must land within {HOOK_WINDOW_SECONDS}s")
    if not contains_phrase(body, fields.key_message):
        findings.append("Q2: key message does not appear in the script or supers")
    for m in fields.mandatories:
        if not contains_phrase(body, m):
            findings.append(f"Q3: mandatory missing: {m!r}")
    disclosure_body = " ".join([decl.disclosure_text, *decl.supers])
    for d in fields.disclosure_requirements:
        if not contains_phrase(disclosure_body, d):
            findings.append(f"Q4: disclosure missing: {d!r}")
    for note in reviewer_notes:
        if note.strip():
            findings.append(f"Q5 premium bar: {note.strip()}")

    if not findings:
        return QualityDecision("pass", round_number, [], "creative_lead")
    if round_number < MAX_ROUNDS:
        return QualityDecision("send_back", round_number, findings, "creative_lead")
    return QualityDecision("escalate_to_andre", round_number, findings, "andre")
