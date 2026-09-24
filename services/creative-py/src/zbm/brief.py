"""
The ZBM brief — 15 fields, locked Sep 24 2026:

 1 objective            6 mandatories        11 success_in_numbers (numeric)
 2 audience             7 approvers          12 insight
 3 key_message          8 distribution       13 tone_of_voice
 4 deliverables         9 deadline           14 rights_and_permissions
   (exact specs)       10 disclosure_reqs    15 transformation_plan
 5 hook

Key-message rule (deterministic, documented in ADR 0005; deliberately
conservative — a false reject costs a rewrite, a false accept costs a
muddled ad):

ONE SENTENCE
  K1  ends with exactly one terminal mark: '.', '!' or '?'
  K2  no other '.', '!' or '?' anywhere before the end (a '.' between two
      digits, e.g. "2.5", is allowed), and no line breaks
ONE IDEA
  K3  none of the joining words: and, but, or, nor, plus, also, while,
      whereas, yet, "as well as"
  K4  none of the joining marks: ';' ':' '&' '+' '—' '–' or a spaced ' - '
  K5  at most one comma (two or more = a list = several ideas)
LENGTH
  K6  3 to 20 words
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from shared.rights import AssetKind, Use
from shared.text import word_count
from shared.types import NonEmptyStr, SafeId

KEY_MESSAGE_MIN_WORDS = 3
KEY_MESSAGE_MAX_WORDS = 20
_JOINING_WORDS = ("and", "but", "or", "nor", "plus", "also", "while", "whereas", "yet")
_JOINING_MARKS = (";", ":", "&", "+", "—", "–", " - ")
_DECIMAL_POINT = re.compile(r"(?<=\d)\.(?=\d)")
_NUMERIC_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def key_message_violations(text: str) -> list[str]:
    """Empty list = one sentence, one idea."""
    out: list[str] = []
    t = (text or "").strip()
    if not t:
        return ["K0: key message is empty"]
    if "\n" in t or "\r" in t:
        out.append("K2: key message contains a line break")
    if t[-1] not in ".!?":
        out.append("K1: key message must end with '.', '!' or '?'")
    body = _DECIMAL_POINT.sub("", t[:-1] if t[-1] in ".!?" else t)
    if any(ch in body for ch in ".!?"):
        out.append("K2: key message contains more than one sentence")
    lowered = f" {re.sub(r'[^a-z0-9 ]+', ' ', t.lower())} "
    found = [w for w in _JOINING_WORDS if f" {w} " in lowered]
    if " as well as " in lowered:
        found.append("as well as")
    if found:
        out.append(f"K3: key message joins ideas with {', '.join(repr(w) for w in found)}")
    marks = [m for m in _JOINING_MARKS if m in t]
    if marks:
        out.append(f"K4: key message joins ideas with {', '.join(repr(m) for m in marks)}")
    if t.count(",") > 1:
        out.append("K5: key message has more than one comma (a list is several ideas)")
    n = word_count(t)
    if not KEY_MESSAGE_MIN_WORDS <= n <= KEY_MESSAGE_MAX_WORDS:
        out.append(f"K6: key message has {n} words; must be {KEY_MESSAGE_MIN_WORDS}-{KEY_MESSAGE_MAX_WORDS}")
    return out


class Deliverable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deliverable_id: SafeId
    platform: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]
    placement: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]
    length_seconds: int = Field(ge=1, le=3600)
    aspect_ratio: Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]?:[1-9][0-9]?$")]
    format: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{2,8}$")]
    count: int = Field(ge=1, le=50)


class SuccessMetric(BaseModel):
    """Success in numbers: `target` MUST be a JSON number (not a string,
    not a boolean, not NaN/Infinity)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: NonEmptyStr
    comparator: Literal[">=", "<="]
    target: Decimal
    unit: NonEmptyStr
    measured_by: NonEmptyStr

    @field_validator("target", mode="before")
    @classmethod
    def _numeric_only(cls, v):
        # A numeric string ("1000", "2.50") is accepted so a brief read back
        # from the API (Decimal serialises as a string) round-trips; words
        # ("lots of sales") never are.
        if isinstance(v, str) and _NUMERIC_TEXT.fullmatch(v.strip()):
            v = Decimal(v.strip())
        if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
            raise ValueError("success target must be a number, not text")
        if isinstance(v, float):
            if not math.isfinite(v):
                raise ValueError("success target must be finite")
            v = Decimal(str(v))
        if isinstance(v, Decimal) and not v.is_finite():
            raise ValueError("success target must be finite")
        return v


class RightsNeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: SafeId
    asset_kind: AssetKind
    use: Use


class BriefFields(BaseModel):
    """All 15 fields are required; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: NonEmptyStr
    audience: NonEmptyStr
    key_message: NonEmptyStr
    deliverables: list[Deliverable] = Field(min_length=1)
    mandatories: list[NonEmptyStr]
    approvers: list[NonEmptyStr] = Field(min_length=1)
    distribution: list[NonEmptyStr] = Field(min_length=1)
    deadline: date
    disclosure_requirements: list[NonEmptyStr] = Field(min_length=1)
    hook: NonEmptyStr
    success_in_numbers: list[SuccessMetric] = Field(min_length=1)
    insight: NonEmptyStr
    tone_of_voice: NonEmptyStr
    rights_and_permissions: list[RightsNeed] = Field(min_length=1)
    transformation_plan: NonEmptyStr


BRIEF_FIELD_NAMES = tuple(BriefFields.model_fields)
assert len(BRIEF_FIELD_NAMES) == 15


def field_issues(fields: BriefFields, today: date) -> list[str]:
    """Registry-independent rules. Registry validity of deliverables is
    checked by placement_spec (intelligence 4)."""
    issues = [f"key_message {v}" for v in key_message_violations(fields.key_message)]
    ids = [d.deliverable_id for d in fields.deliverables]
    if len(ids) != len(set(ids)):
        issues.append("deliverables: deliverable_id values must be unique")
    if fields.deadline < today:
        issues.append(f"deadline {fields.deadline} is in the past")
    return issues


class BriefStatus(str, Enum):
    INCOMPLETE = "incomplete"      # writer could not fill all 15 fields
    DRAFT = "draft"                # complete, awaiting Creative Lead
    APPROVED = "approved"
    SENT_BACK = "sent_back"


class BriefRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_id: SafeId
    client_id: SafeId
    status: BriefStatus
    drafted_by: str
    fields: BriefFields | None = None
    open_questions: list[str] = []
    issues: list[str] = []
    warnings: list[str] = []
    spec_row_ids: dict[str, list[str]] = {}
    maker_summary: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    review_issues: list[str] = []
    ledger_event_ids: list[str] = []
