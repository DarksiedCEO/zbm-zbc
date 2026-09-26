"""
Request models (strict, unknown fields refused -> 422). No model carries a
money field, a float amount or a Decimal (guardrail H.26); the only number
that is not an integer anywhere is the disclosure label offset in seconds,
inside ``facts`` (validated by facts.py).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, model_validator

from facts import ASSET_TYPES
from jurisdictions import is_known_country, is_known_subdivision
from textguard import has_control_chars


def _no_control(v: str) -> str:
    if has_control_chars(v):
        raise ValueError("control characters are not accepted")
    return v


def _bounded_json(v: Optional[dict]) -> Optional[dict]:
    import json

    if v is not None and len(json.dumps(v, separators=(",", ":"), default=str)) > 8192:
        raise ValueError("caller_context larger than 8 KB")
    return v


Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_no_control)]
Name = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_no_control)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleRequest(Strict):
    request_id: Id
    subject_id: Id
    lane: Literal["client", "zbc_creator", "zbc_brand"]
    facts: dict[str, Any]


class ReviewRequest(Strict):
    request_id: Id
    subject_kind: Literal["zbc_clip", "zbm_work"]
    subject_id: Id
    facts: dict[str, Any]
    caller_context: Annotated[Optional[dict[str, Any]], AfterValidator(_bounded_json)] = None


class ScreenRequest(Strict):
    request_id: Id
    subject_id: Id
    role: Literal["payee", "owner"]
    owner_of: Optional[Id] = None
    legal_name: Name
    aliases: list[Name] = Field(default_factory=list, max_length=10)
    dob: Optional[Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]] = None
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    region: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}-[A-Z0-9]{1,3}$")]] = None

    @model_validator(mode="after")
    def _known_codes(self):
        # AEGIS N14-3: codes are checked against the shipped ISO 3166 lists (no aliases such as CA-PQ)
        if not is_known_country(self.country):
            raise ValueError("country is not a known ISO 3166-1 alpha-2 code")
        if self.region is not None and (not is_known_subdivision(self.region) or not self.region.startswith(self.country + "-")):
            raise ValueError("region is not a known ISO 3166-2 code of that country")
        return self


class A11yRequest(Strict):
    request_id: Id
    asset_ref: Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_no_control)]
    asset_type: Literal[ASSET_TYPES]  # type: ignore[valid-type]
    content_sha256: Sha
    owner_id: Id


class ProposalRequest(Strict):
    request_id: Id
    kind: Literal["new", "amend", "reverify", "supersede", "retire", "control"]
    target_id: Optional[Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9-]{1,39}$")]] = None
    proposed_row: Optional[dict[str, Any]] = None
    evidence: Optional[dict[str, Any]] = None


class Decision(Strict):
    proposal_id: Id
    content_sha256: Sha
    decision: Literal["approve", "reject"]
    note: Optional[Text] = None
    acknowledge_weakening: StrictBool = False   # AEGIS N14-9: required (true) to approve a weakening proposal


class DecisionsRequest(Strict):
    request_id: Id
    decisions: list[Decision] = Field(min_length=1, max_length=200)


class EvidenceItem(Strict):
    kind: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,40}$")]
    ref: Annotated[str, Field(min_length=1, max_length=512), AfterValidator(_no_control)]
    sha256: Sha


class ControlResultRequest(Strict):
    request_id: Id
    result: Literal["pass", "fail"]
    tested_at: Annotated[str, Field(max_length=40)]
    evidence: list[EvidenceItem] = Field(max_length=50)


class HoldReleaseRequest(Strict):
    request_id: Id
    reason: Text


class ResolveRequest(Strict):
    request_id: Id
    person: Optional[dict[str, Any]] = None
    network_country_signal: Optional[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = None
    targets: list[Annotated[str, Field(pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")]] = Field(default_factory=list, max_length=60)


class RunRequest(Strict):
    request_id: Id


REQUEST_MODELS = (RuleRequest, ReviewRequest, ScreenRequest, A11yRequest, ProposalRequest, DecisionsRequest,
                  ControlResultRequest, HoldReleaseRequest, ResolveRequest, RunRequest)
