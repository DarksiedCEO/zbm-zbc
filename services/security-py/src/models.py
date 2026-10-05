"""Request bodies (strict: unknown fields refused, no coercion). Responses are plain dicts built by service.py."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictStr

import config


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _printable(v: str) -> str:
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in v):
        raise ValueError("control characters are refused")
    return v


def _date(v: str) -> str:
    date.fromisoformat(v)
    return v


Id = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
Name = Annotated[StrictStr, Field(pattern=r"^[a-z0-9_]{1,40}$")]
SecretName = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,79}$")]
B64Url = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]{1,22000}$")]
Caller = Literal[config.KNOWN_CALLERS]  # type: ignore[valid-type]
Owner = Literal[config.KNOWN_CALLERS + ("cybersecurity",)]  # type: ignore[valid-type]
ClientId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")]
SubjectRef = Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,20}:[A-Za-z0-9._:-]{1,100}$")]
Value = Annotated[StrictStr, Field(min_length=1, max_length=8192)]
Note = Annotated[StrictStr, Field(min_length=1, max_length=500), AfterValidator(_printable)]
Severity = Literal["sev1", "sev2", "sev3", "sev4"]
DateStr = Annotated[StrictStr, Field(pattern=r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$"), AfterValidator(_date)]
Code = Annotated[StrictStr, Field(pattern=r"^[A-Z][A-Z0-9_]{2,47}$")]

SERVICE_KINDS = ("credential", "api_key", "oauth_token", "client_credential", "contact", "other")
ANDRE_KINDS = SERVICE_KINDS + ("hmac_key", "canary")


class Approval(Strict):
    """A WebAuthn assertion over the challenge issued for exactly this action (POST /sec/v1/approvals/challenges)."""
    challenge_id: Id
    credential_id: B64Url
    client_data_json: B64Url
    authenticator_data: B64Url
    signature: B64Url


class ServiceStore(Strict):
    request_id: Id
    name: SecretName
    kind: Literal[SERVICE_KINDS]  # type: ignore[valid-type]
    value: Value
    encoding: Literal["utf8", "base64"] = "utf8"
    client_id: Optional[ClientId] = None
    subject_refs: list[SubjectRef] = Field(default_factory=list, max_length=10)
    readers: list[Owner] = Field(default_factory=list, max_length=1)
    purposes: list[Name] = Field(default_factory=list, max_length=10)
    rotate_by: Optional[DateStr] = None


class AndreStore(Strict):
    request_id: Id
    owner: Caller
    name: SecretName
    kind: Literal[ANDRE_KINDS]  # type: ignore[valid-type]
    value: Optional[Value] = None
    generate: StrictBool = False
    encoding: Literal["utf8", "base64"] = "utf8"
    client_id: Optional[ClientId] = None
    subject_refs: list[SubjectRef] = Field(default_factory=list, max_length=10)
    readers: list[Caller] = Field(default_factory=list, max_length=14)
    purposes: list[Name] = Field(default_factory=list, max_length=10)
    rotate_by: Optional[DateStr] = None
    approval: Approval


class Use(Strict):
    purpose: Name


class Rotate(Strict):
    request_id: Id
    value: Optional[Value] = None
    generate: StrictBool = False
    encoding: Literal["utf8", "base64"] = "utf8"
    rotate_by: Optional[DateStr] = None
    approval: Optional[Approval] = None


class Destroy(Strict):
    request_id: Id
    approval: Optional[Approval] = None


class SetAccess(Strict):
    request_id: Id
    readers: list[Caller] = Field(max_length=14)
    purposes: list[Name] = Field(max_length=10)
    approval: Approval


class ChallengeRequest(Strict):
    action: Code
    target: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{0,160}$")] = ""
    body: dict = Field(default_factory=dict)


class EnrollOptions(Strict):
    enroll_token: Optional[Annotated[StrictStr, Field(min_length=32, max_length=256)]] = None
    approval: Optional[Approval] = None


class Enroll(Strict):
    challenge_id: Id
    attestation_object: B64Url
    client_data_json: B64Url
    label: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9 ._-]{1,40}$")]


class Revoke(Strict):
    request_id: Id
    approval: Approval


class Freeze(Strict):
    request_id: Id
    target_kind: Literal["caller", "secret", "all"]
    target_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,140}$")]
    reason_code: Code
    approval: Approval


class Lift(Strict):
    request_id: Id
    approval: Approval


class Preserve(Strict):
    request_id: Id
    hold_id: Id
    systems: list[Literal["email", "chat", "drive"]] = Field(min_length=1, max_length=3)
    subject_refs: list[SubjectRef] = Field(default_factory=list, max_length=200)


class ReleaseHold(Strict):
    request_id: Id


class IncidentOpen(Strict):
    request_id: Id
    severity: Severity
    code: Code
    subject: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,140}$")]


class IncidentNote(Strict):
    request_id: Id
    note: Note


class IncidentClose(Strict):
    request_id: Id
    root_cause_code: Code
    note: Note
    approval: Approval


class Finding(Strict):
    advisory_id: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{3,80}$")]
    package: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9@/._+-]{1,120}$")]
    installed_version: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9.+_-]{1,60}$")]
    fixed_versions: list[Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9.+_<>=, -]{1,60}$")]] = \
        Field(default_factory=list, max_length=20)
    severity: Literal["critical", "high", "medium", "low", "unknown"]


class Scan(Strict):
    request_id: Id
    source: Annotated[StrictStr, Field(pattern=r"^(python|go|rust|node|container):[a-z0-9-]{1,40}$")]
    tool: Annotated[StrictStr, Field(pattern=r"^[a-z0-9-]{1,30}@[A-Za-z0-9.+-]{1,30}$")]
    scanned_at: Annotated[StrictStr, Field(max_length=40)]
    findings: list[Finding] = Field(max_length=2000)


class AcceptRisk(Strict):
    request_id: Id
    until: DateStr
    reason_code: Code
    approval: Approval


class Mint(Strict):
    audience: Caller
    scope: list[Name] = Field(default_factory=list, max_length=8)


class JobRun(Strict):
    request_id: Id


PLACEHOLDER_APPROVAL = {"challenge_id": "placeholder", "credential_id": "AAAA", "client_data_json": "AAAA",
                        "authenticator_data": "AAAA", "signature": "AAAA"}

# every action Andre approves, with the body model its route validates
APPROVAL_ACTIONS = {"SECRET_STORE": AndreStore, "SECRET_ROTATE": Rotate, "SECRET_DESTROY": Destroy,
                    "SECRET_ACCESS": SetAccess, "PASSKEY_ENROLL": EnrollOptions, "PASSKEY_REVOKE": Revoke,
                    "FREEZE": Freeze, "LIFT_FREEZE": Lift, "INCIDENT_CLOSE": IncidentClose, "RISK_ACCEPT": AcceptRisk}
