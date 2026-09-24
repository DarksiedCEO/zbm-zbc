"""
ZBC intelligence 7 — Creative Memory.

Job: the winners library, by vertical and platform — what angles and hooks
actually worked across campaigns.
Decides: whether a reported clip result is learned.

Rules:
L1  a SELF-REPORTED result (a clipper's own screenshot or number) is
    rejected outright — it is never even sent for attestation;
L2  every other result must carry a Verification and Integrity attestation
    that says `verified`; the stand-in today says "unverified, not allowed
    yet", so NOTHING is learned until that department exists;
L3  the number stored is the attested `verified_views` from the
    attestation, never the number in the submitted report; an attestation
    without it is not learnable;
L4  the library ranks by attested verified views (desc), then result id.
Results carry no money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.departments import VerificationAttestation, VerificationIntegrityPort
from shared.types import NonEmptyStr, SafeId


class ClipResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: SafeId
    campaign_id: SafeId
    submission_id: SafeId
    vertical: NonEmptyStr
    platform: NonEmptyStr
    angle_id: NonEmptyStr
    hook: NonEmptyStr
    source: Literal["self_reported", "platform_export", "tracking_link"]
    reported_views: int = Field(ge=0)


class LearnedWinner(BaseModel):
    result_id: str
    campaign_id: str
    vertical: str
    platform: str
    angle_id: str
    hook: str
    verified_views: int
    attestation_id: str


@dataclass(frozen=True)
class MemoryDecision:
    learned: bool
    reason: str
    attestation: VerificationAttestation | None = None
    winner: LearnedWinner | None = None


@dataclass
class ZbcCreativeMemory:
    library: dict[tuple[str, str], list[LearnedWinner]] = field(default_factory=dict)

    def evaluate(self, result: ClipResult, verification: VerificationIntegrityPort) -> MemoryDecision:
        if result.source == "self_reported":
            return MemoryDecision(False, f"L1 {result.result_id}: self-reported numbers are never learned")
        att = verification.attest_result(result.result_id, result.model_dump(mode="json"))
        if not att.verified:
            return MemoryDecision(False, f"L2 {result.result_id}: not verified by Verification and Integrity ({att.reason})", att)
        views = att.checks.get("verified_views")
        if isinstance(views, bool) or not isinstance(views, int) or views < 0 or not att.attestation_id:
            return MemoryDecision(False, f"L3 {result.result_id}: attestation carries no verified view count / id", att)
        winner = LearnedWinner(result_id=result.result_id, campaign_id=result.campaign_id, vertical=result.vertical,
                               platform=result.platform, angle_id=result.angle_id, hook=result.hook,
                               verified_views=views, attestation_id=att.attestation_id)
        return MemoryDecision(True, f"{result.result_id}: learned with {views} verified views ({att.attestation_id})", att, winner)

    def commit(self, winner: LearnedWinner) -> None:
        self.library.setdefault((winner.vertical, winner.platform), []).append(winner)

    def winners(self, vertical: str, platform: str, top: int = 10) -> list[LearnedWinner]:
        rows = self.library.get((vertical, platform), [])
        return sorted(rows, key=lambda w: (-w.verified_views, w.result_id))[:top]
