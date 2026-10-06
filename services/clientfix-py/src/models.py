"""Request bodies (strict: unknown fields refused, no coercion, frozen; bizdev-py's Strict base). Responses are plain
dicts built by the service. No model has a field that can hold a password, a token or personal data; api.py also
refuses such keys, and credential-shaped values, anywhere in a body before it is parsed (secrets_guard). Money is a
canonical two-decimal JSON STRING; a float or an int in a money field is refused 422 (strict mode: no coercion)."""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictStr

from catalogue import CHECKS, DETECTION_CATEGORIES


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _lower(v):
    return v.lower() if isinstance(v, str) else v


RequestId = Annotated[StrictStr, BeforeValidator(_lower),
                      Field(pattern=r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,64})$")]
# onboarding-py's client ids (services/onboarding-py/src/ledger.py: onb-<uuid4 hex> or onb-<sha256 hex>), exactly
ClientId = Annotated[StrictStr, Field(pattern=r"^onb-(?:[0-9a-f]{32}|[0-9a-f]{64})$")]
# finance-py's OWN generated ids (services/finance-py/src/models.py OWN_ID_RE), exactly
FinanceId = Annotated[StrictStr, Field(pattern=r"^fin-[a-z][a-z0-9]{0,7}-(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{40})$")]
# detection-py's FindingRef / AgentId (services/detection-py/src/zbm_schema/limits.py: 128 / 64 characters), with a
# safe alphabet on top
FindingRef = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
AgentId = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
CfxId = Annotated[StrictStr, Field(pattern=r"^cfx-[a-z]{3}-[0-9a-f]{40}$")]
VaultRef = Annotated[StrictStr, Field(pattern=r"^vault:[a-z0-9_]{1,40}\.[A-Za-z0-9_][A-Za-z0-9._-]{0,79}$")]
AccountRef = Annotated[StrictStr, Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9._:/-]{3,200}$")]
Scope = Annotated[StrictStr, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]{1,200}$")]
Money = Annotated[StrictStr, Field(max_length=18, pattern=r"^(0|[1-9][0-9]{0,14})\.[0-9]{2}$")]
Sha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Check = Literal[tuple(sorted(CHECKS))]                                     # type: ignore[valid-type]
LeakCategory = Literal[tuple(sorted(DETECTION_CATEGORIES))]                # type: ignore[valid-type]
Connector = Annotated[StrictStr, Field(pattern=r"^[a-z0-9_]{2,40}$")]
OpName = Annotated[StrictStr, Field(pattern=r"^[a-z0-9_]{2,40}(?:\.[a-z0-9_]{2,40}){1,3}$")]
Target = Annotated[StrictStr, Field(min_length=1, max_length=1100)]
FieldName = Annotated[StrictStr, Field(min_length=1, max_length=330, pattern=r"^[A-Za-z0-9._:-]{1,330}$")]
OpValue = Union[StrictStr, StrictBool, list[StrictStr], dict[StrictStr, Union[StrictStr, list[StrictStr]]], None]


class RequestOnly(Strict):
    request_id: RequestId


# ---------------------------------------------------------------------------------------------- connections

class ConnectionCreate(Strict):
    """What the hub sends after the client finished an official OAuth app connection: ids and the VAULT REFERENCE of
    the token the hub stored in the Cybersecurity (22) vault. There is no field that can hold a token."""
    request_id: RequestId
    client_id: ClientId
    connector: Connector
    account_ref: AccountRef
    token_ref: Optional[VaultRef] = None
    scopes: list[Scope] = Field(default_factory=list, max_length=40)


class Revoke(Strict):
    request_id: RequestId
    origin: Literal["client", "platform_uninstall", "andre"] = "client"


class SessionOpen(Strict):
    request_id: RequestId
    client_id: ClientId


# ------------------------------------------------------------------------------------------------- findings

class Resource(Strict):
    connection_id: CfxId
    target: Target
    # the exact field, for checks whose fields are named (a key event, a metafield): AEGIS round 2 R2-7
    field: Optional[Annotated[StrictStr, Field(min_length=3, max_length=330, pattern=r"^[A-Za-z0-9._:-]{3,330}$")]] = None


class FindingIn(Strict):
    """A Revenue Recovery finding (detection-py's ``Finding`` ids, carried through orchestrator-go) mapped to one check
    of the version-1 catalogue and one client resource."""
    request_id: RequestId
    finding_id: FindingRef
    agent_id: AgentId
    leak_category: Optional[LeakCategory] = None
    client_id: ClientId
    check_code: Check
    resource: Resource


# ----------------------------------------------------------------------------------------------- jobs, quote

class QuoteItem(Strict):
    finding_id: FindingRef
    price: Money


class JobCreate(Strict):
    request_id: RequestId
    client_id: ClientId
    items: list[QuoteItem] = Field(min_length=1, max_length=200)
    currency: Literal["USD"] = "USD"


class HashApproval(Strict):
    request_id: RequestId
    sha256: Sha256


class PaymentEvent(Strict):
    request_id: RequestId
    finance_event_id: FinanceId
    job_id: CfxId
    kind: Literal["payment_confirmed"]
    amount: Money
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]   # AEGIS round 4 L2: an ISO 4217 shape, nothing else
    quote_sha256: Sha256


# --------------------------------------------------------------------------------------------------- plans

class Op(Strict):
    """No ``before``: the service reads it from the platform at plan submission (AEGIS round 2 R2-4); a plan that
    carries one is refused 422 (unknown field)."""
    op: OpName
    target: Target
    field: FieldName
    after: OpValue


class PlanItem(Strict):
    item_id: CfxId
    connection_id: CfxId
    ops: list[Op] = Field(min_length=1, max_length=50)


class PlanSubmit(Strict):
    request_id: RequestId
    team: Literal["alpha", "bravo"]
    items: list[PlanItem] = Field(min_length=1, max_length=200)


class ManualDone(Strict):
    request_id: RequestId
    item_id: CfxId


class StateDecision(Strict):
    request_id: RequestId
    state_sha256: Sha256


class FreezeClient(Strict):
    request_id: RequestId
    client_id: ClientId
