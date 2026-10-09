"""
Request bodies for the Onboarding API. All inherit ``Inbound``: unknown
fields rejected, every string scrubbed for credentials at ingest, inbound
datetimes must carry a UTC offset. There is deliberately no ``now`` field
on any request: time comes from the service clock, so a caller cannot
move the clock to get around quiet hours, the noon cutoff or the stuck
window.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, Optional, Union

from pydantic import Field, StringConstraints, model_validator

from redaction import CREDENTIAL_REFUSAL, find_credential

from . import (
    Channel,
    ContractTerms,
    Inbound,
    LongText,
    Money,
    Person,
    PositiveMoney,
    Provenance,
    ShortText,
    SubjectId,
)
from pydantic import AwareDatetime

Name64 = Annotated[str, StringConstraints(min_length=1, max_length=64)]
FactValue = Union[bool, int, Annotated[str, StringConstraints(max_length=2000)], list[Annotated[str, StringConstraints(max_length=200)]], None]


class StartClientRequest(Inbound):
    """Contract signed -> onboarding starts (client lane or ZBC brand lane)."""

    client_id: SubjectId
    lane: Literal["client", "zbc_brand"] = "client"
    business_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    signer: Person
    login_holder: Optional[Person] = None  # P22: may differ from the signer
    time_zone: Name64  # client's own IANA zone (P7)
    quiet_hours_start: Optional[Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]] = None
    quiet_hours_end: Optional[Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]] = None
    preferred_channel: Optional[Channel] = None
    deal_size_usd: Optional[PositiveMoney] = None
    contract: Optional[ContractTerms] = None


class FactIn(Inbound):
    field: Name64
    value: FactValue
    provenance: Provenance
    evidence: ShortText
    # Must not be in the future: checked against the SERVER clock by the
    # service (a future timestamp would out-rank a newer real fact).
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _field_and_value_together(self) -> "FactIn":
        # "shopify_password" + "tangerine" is a credential even though
        # neither string is one on its own. After validation (fix wave 4,
        # R1): the field and value lengths are bounded before this scan.
        vals = self.value if isinstance(self.value, list) else [self.value]
        for x in vals:
            if isinstance(x, (str, int)) and not isinstance(x, bool) and find_credential(f"{self.field}: {x}"):
                raise ValueError(CREDENTIAL_REFUSAL)
        return self


class FactsRequest(Inbound):
    facts: list[FactIn] = Field(max_length=200)
    vertical: Optional[Name64] = None


class DocumentRequest(Inbound):
    name: ShortText
    text: LongText


class MessageRequest(Inbound):
    text: Annotated[str, StringConstraints(min_length=1, max_length=5000)]


class WebsiteScanRequest(Inbound):
    # A public web page: third-party content, scrubbed rather than refused.
    SCRUB_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"html"})

    html: Annotated[str, StringConstraints(max_length=500_000)]


class AuditRequest(Inbound):
    # Raw account-pull rows, forwarded to Revenue Recovery unchanged in shape.
    # Platform data, not client-typed text: scrubbed rather than refused.
    SCRUB_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset({"account_data"})

    account_data: dict[Name64, list[dict]]
    observed_monthly_revenue_usd: Optional[Money] = None
    risk_signals: list[Name64] = Field(default_factory=list)


class PlanRequest(Inbound):
    client_priorities: list[Name64] = Field(max_length=20)


class PlanChoiceRequest(Inbound):
    topic: Name64
    choice: Literal["keep_my_order", "accept_recommendation"]


class ClientApprovalIn(Inbound):
    change_id: Name64
    digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    approved_by_client: bool
    source: Name64


class AccountChangeRequest(Inbound):
    change_id: Name64
    platform: Name64
    description: ShortText
    client_approvals: list[ClientApprovalIn] = Field(default_factory=list)


class FirstWinRequest(Inbound):
    finding_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class RecommendScoreRequest(Inbound):
    score: int = Field(ge=0, le=10)


class IssueOutcomeRequest(Inbound):
    resolved: bool
    note: ShortText = ""


ApprovalToken = Annotated[str, StringConstraints(max_length=128)]


class EscalationAckRequest(Inbound):
    """Acknowledging an escalation is Andre's action: it needs his approval
    token (HMAC-SHA256 keyed by ONBOARDING_ANDRE_APPROVAL_KEY over the exact
    action — see ``memory.andre_action_token``). The shared service token
    alone is refused (403)."""

    approval_token: Optional[ApprovalToken] = None


class EscalationResolveRequest(Inbound):
    resolution: ShortText
    snag_category: Name64
    approval_token: Optional[ApprovalToken] = None  # Andre's, over (client, escalation, resolution, snag_category)


class ExitRequest(Inbound):
    memory_choice: Literal["export_then_destroy", "destroy"] = "export_then_destroy"


class CreatorFlagRequest(Inbound):
    received: bool


class CreatorPaymentRequest(Inbound):
    # No payment-date field: the 1099 tax year is the SERVER's date
    # (America/Los_Angeles) when the payment is recorded (fix wave 1, F1 sweep).
    amount_usd: PositiveMoney
    # Bug sweep D (M, payment replay): REQUIRED. A replay of the same request_id answers the first result and
    # records nothing; the same id with a different amount is 409. Without it a lost reply retried was counted twice.
    request_id: SubjectId


class CaptionRequest(Inbound):
    caption: Annotated[str, StringConstraints(max_length=5000)]


class CampaignRequest(Inbound):
    campaign_id: SubjectId
    regulated: bool
    wants_owned_addon: bool = False
    requested_budget_usd: Optional[PositiveMoney] = None


class CampaignApproveRequest(Inbound):
    brand_yes_campaign_id: SubjectId
    plan_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ProvingResultRequest(Inbound):
    views_delivered: int = Field(ge=0)
    clicks: int = Field(ge=0)
    evidence: Literal["observed", "estimated"]


class PlaybookRuleRequest(Inbound):
    rule_id: Name64
    version: int = Field(ge=1)
    text: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    approval_token: ApprovalToken
