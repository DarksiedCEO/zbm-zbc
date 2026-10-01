"""
Request models (spec §B.1, §C.8.7, §D): strict pydantic v2 (unknown key → 422); every free-text field is checked
for control characters (422) and scanned for injection (recorded, outcome unchanged — the text is data).
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zbm_delivery.textguard import has_control_chars

ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
FINDING_ID_RE = re.compile(r"^(?:[A-Z]{1,4}[0-9]{1,3}-[0-9]{1,3}|[A-Za-z0-9._-]{1,32})$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,60}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,200}$")
PATH_RE = re.compile(r"^services/[a-z0-9][a-z0-9\-]{0,60}/[A-Za-z0-9._/\-]{1,400}$")
SEVERITIES = ("critical", "high", "medium", "low", "info")
FREE_TEXT_MAX = 4096


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=FREE_TEXT_MAX)


def _text(v: str, name: str, allow_newlines: bool = True, max_len: int = FREE_TEXT_MAX) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{name} must be a string")
    if len(v) > max_len:
        raise ValueError(f"{name} longer than {max_len} characters")
    if has_control_chars(v, allow_newlines=allow_newlines):
        raise ValueError(f"{name} contains control characters")
    return v


class Source(Strict):
    kind: Literal["aegis_review", "andre_session"]
    ref: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=SHA_RE.pattern)

    @field_validator("ref")
    @classmethod
    def _ref(cls, v):
        return _text(v, "source.ref", allow_newlines=False, max_len=128)


REPRO_TEST_MAX = 64 * 1024
REPRO_TEST_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/\-]{0,399}$")


class ReproductionTest(BaseModel):
    """Wave 21 (L2): a RED test authored by the reviewer (AEGIS) for a finding that has no test at the base commit.
    ``path`` is relative to the service directory and must be a NEW file there; the engine writes ``content`` onto
    every tree it builds for the run (never into a commit), records its sha256 as reviewer-authored, and denies
    any change of that path by the agent. The finding's ``reproduction`` must name ``<path>::<test>``."""
    model_config = ConfigDict(extra="forbid", str_max_length=REPRO_TEST_MAX)
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(min_length=1, max_length=REPRO_TEST_MAX)

    @field_validator("path")
    @classmethod
    def _path(cls, v):
        if not REPRO_TEST_PATH_RE.fullmatch(v) or ".." in v.split("/") or "//" in v or v.endswith("/"):
            raise ValueError("reproduction_test.path must be a plain path relative to the service directory")
        return v

    @field_validator("content")
    @classmethod
    def _content(cls, v):
        if "\x00" in v or has_control_chars(v.replace("\t", " ").replace("\r", ""), allow_newlines=True):
            raise ValueError("reproduction_test.content contains control characters")
        return v


class Finding(Strict):
    id: str = Field(pattern=FINDING_ID_RE.pattern)
    severity: Literal["critical", "high", "medium", "low", "info"]
    title: str = Field(min_length=1, max_length=400)
    file: str = Field(pattern=PATH_RE.pattern)
    line: int = Field(ge=1, le=10 ** 6)
    reproduction: str = Field(min_length=1, max_length=FREE_TEXT_MAX)
    expected: str = Field(min_length=1, max_length=FREE_TEXT_MAX)
    observed: str = Field(min_length=1, max_length=FREE_TEXT_MAX)
    class_hint: Optional[str] = Field(default=None, max_length=120)
    reproduction_test: Optional[ReproductionTest] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return _text(v, "title", allow_newlines=False, max_len=400)

    @field_validator("reproduction", "expected", "observed")
    @classmethod
    def _free(cls, v):
        return _text(v, "finding text")

    @field_validator("class_hint")
    @classmethod
    def _hint(cls, v):
        if v is None:
            return v
        if not re.fullmatch(r"[a-z0-9_\-]{1,120}", v):
            raise ValueError("class_hint must be a snake_case identifier")
        return v

    @field_validator("file")
    @classmethod
    def _file(cls, v):
        if ".." in v.split("/") or "//" in v:
            raise ValueError("file must be a plain path under services/<service>/")
        return v


class FindingsDocument(Strict):
    request_id: str = Field(pattern=f"^{ID_RE.pattern}$")
    source: Source
    base_ref: str = Field(pattern=REF_RE.pattern)
    base_sha: str = Field(pattern=COMMIT_RE.pattern)
    service: str = Field(pattern=SERVICE_RE.pattern)
    findings: list[Finding] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _consistent(self):
        ids = [f.id for f in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("finding ids must be unique")
        for f in self.findings:
            if not f.file.startswith(f"services/{self.service}/"):
                raise ValueError(f"finding {f.id}: file must be inside services/{self.service}/")
        if self.base_ref.startswith("-") or ".." in self.base_ref:
            raise ValueError("base_ref must be a plain ref name")
        return self


FLAG_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}-(?:F[0-9]{3,4}|RD)$")
REVIEW_NOTE_MIN = 20


class FindingVerdict(Strict):
    """Wave 23 (D1): the reviewer's explicit verdict on ONE finding of the run. ``accept`` is the only route to the
    finding state ``accepted``; ``reopen`` sends it to a new run (it must also be listed in ``reopened``). A
    runner-dependent finding's ``accept`` needs a ``note`` of at least 20 characters (D3)."""
    finding_id: str
    verdict: Literal["accept", "reopen"]
    note: str = Field(default="", max_length=2000)

    @field_validator("finding_id")
    @classmethod
    def _fid(cls, v):
        if not FINDING_ID_RE.fullmatch(v):
            raise ValueError("finding_id must be a finding id")
        return v

    @field_validator("note")
    @classmethod
    def _note(cls, v):
        return _text(v, "note", allow_newlines=True, max_len=2000)


class FlagNote(Strict):
    """Wave 24 (E2): the reviewer's note on ONE review flag — what was checked at that line. An accepting review
    needs one per flag of every accepted finding: >= 20 characters, no single character over half of it, and not
    the same text as another note of the review (checked by the service)."""
    flag_id: str
    note: str = Field(default="", max_length=2000)

    @field_validator("flag_id")
    @classmethod
    def _fid(cls, v):
        if not FLAG_ID_RE.fullmatch(v):
            raise ValueError("flag_id must be a review flag id (<finding>-F001 / <finding>-RD)")
        return v

    @field_validator("note")
    @classmethod
    def _note(cls, v):
        return _text(v, "note", allow_newlines=True, max_len=2000)


class ReviewRequest(Strict):
    request_id: str = Field(pattern=f"^{ID_RE.pattern}$")
    review_ref: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=SHA_RE.pattern)
    verdict: Literal["pass", "fail"]
    reopened: list[str] = Field(default_factory=list, max_length=200)
    new_findings: list[Finding] = Field(default_factory=list, max_length=200)
    # wave 23 (D1/D2): a verdict per finding (a pass needs one for EVERY finding of the run, all accept) and every
    # review flag id of each accepted finding
    finding_verdicts: list[FindingVerdict] = Field(default_factory=list, max_length=200)
    # wave 24 (E2): a note per flag (a bare flag id is read as a flag with an empty note — refused when it matters,
    # with the reason, by the service) and the sha256 of the run's complete source diff the reviewer read
    flags_addressed: list[FlagNote] = Field(default_factory=list, max_length=2000)
    src_diff_sha256: Optional[str] = Field(default=None, pattern=SHA_RE.pattern)

    @field_validator("finding_verdicts")
    @classmethod
    def _verdicts(cls, v):
        ids = [x.finding_id for x in v]
        if len(set(ids)) != len(ids):
            raise ValueError("one verdict per finding")
        return v

    @field_validator("flags_addressed", mode="before")
    @classmethod
    def _flag_ids_alone(cls, v):
        if isinstance(v, list):
            return [{"flag_id": x, "note": ""} if isinstance(x, str) else x for x in v]
        return v

    @field_validator("flags_addressed")
    @classmethod
    def _flags(cls, v):
        ids = [x.flag_id for x in v]
        if len(set(ids)) != len(ids):
            raise ValueError("flags_addressed ids must be unique")
        return v

    @field_validator("review_ref")
    @classmethod
    def _ref(cls, v):
        return _text(v, "review_ref", allow_newlines=False, max_len=128)

    @field_validator("reopened")
    @classmethod
    def _reopened(cls, v):
        for x in v:
            if not FINDING_ID_RE.fullmatch(x):
                raise ValueError("reopened ids must be finding ids")
        if len(set(v)) != len(v):
            raise ValueError("reopened ids must be unique")
        return v

    @model_validator(mode="after")
    def _fail_needs_work(self):
        if self.verdict == "pass" and (self.reopened or self.new_findings):
            raise ValueError("a pass carries no reopened or new findings")
        if self.verdict == "fail" and not (self.reopened or self.new_findings):
            raise ValueError("a fail names at least one reopened or new finding")
        if self.verdict == "pass" and not self.finding_verdicts:
            raise ValueError("a pass needs an explicit verdict for every finding (finding_verdicts; D1)")
        for fv in self.finding_verdicts:
            if fv.verdict == "reopen" and fv.finding_id not in self.reopened:
                raise ValueError(f"finding {fv.finding_id}: a reopen verdict must also be listed in reopened")
            if fv.verdict == "accept" and fv.finding_id in self.reopened:
                raise ValueError(f"finding {fv.finding_id}: accepted and reopened at once")
        return self


class CancelRequest(Strict):
    request_id: str = Field(pattern=f"^{ID_RE.pattern}$")
    reason: str = Field(min_length=1, max_length=400)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v):
        return _text(v, "reason", allow_newlines=False, max_len=400)


class ReconcileRequest(Strict):
    request_id: str = Field(pattern=f"^{ID_RE.pattern}$")
    head_sha256: str = Field(pattern=SHA_RE.pattern)
    void_lines: list[int] = Field(default_factory=list, max_length=10_000)
    void_event_ids: list[str] = Field(default_factory=list, max_length=10_000)

    @field_validator("void_lines")
    @classmethod
    def _lines(cls, v):
        if any(not (1 <= n <= 10 ** 12) for n in v):
            raise ValueError("void_lines out of range")
        return v

    @field_validator("void_event_ids")
    @classmethod
    def _ids(cls, v):
        for x in v:
            if not ID_RE.fullmatch(x):
                raise ValueError("void_event_ids must be ledger event ids")
        return v
