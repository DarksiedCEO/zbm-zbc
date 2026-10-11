"""Request bodies (strict: unknown fields refused, no coercion, frozen; bizdev-py's Strict base). Responses are plain
dicts built by the service. api.py also refuses personal-data keys anywhere in a body before it is parsed."""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

REQUEST_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TENANT = r"^[a-z][a-z0-9-]{1,39}$"
DOMAIN = r"^[a-z0-9.-]{4,253}$"
PATH = r"^/[\x21-\x7e]{0,500}$"
INVOICE = r"^fin-inv-[0-9A-HJKMNP-TV-Z]{26}$"
FINANCE_CLIENT = r"^[A-Za-z0-9._-]{1,100}$"          # finance-py models.CLIENT_ID_RE (Legal's party reference)
# Andre's override of an UNVERIFIABLE invoice check (Finance not configured, down, refusing us, or answering nonsense).
# It never overrides a definitive answer from Finance, nor a reused invoice (ADR 0017 W3-2).
InvoiceOverride = Literal["ANDRE_CONFIRMED_PAYMENT", "FINANCE_OUTAGE"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


RequestId = Annotated[StrictStr, Field(pattern=REQUEST_ID)]


class RequestOnly(Strict):
    request_id: RequestId


class TenantCreate(Strict):
    request_id: RequestId
    tenant_id: Annotated[StrictStr, Field(pattern=TENANT)]
    kind: Literal["own", "client"]


class DomainsSet(Strict):
    request_id: RequestId
    domains: Annotated[list[Annotated[StrictStr, Field(pattern=DOMAIN)]], Field(max_length=50)]


class SwitchSet(Strict):
    request_id: RequestId
    switch: Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,20}(:[a-z0-9_-]{1,40})?$")]
    engaged: StrictBool


class EntityFieldSet(Strict):
    request_id: RequestId
    field: Annotated[StrictStr, Field(max_length=40)]
    value: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    source: Annotated[StrictStr, Field(max_length=40)]
    provenance: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    authority: Annotated[StrictStr, Field(max_length=40)]
    expected_version: Annotated[StrictInt, Field(ge=1, le=1_000_000)]


class AuditCreate(Strict):
    request_id: RequestId
    domain: Annotated[StrictStr, Field(pattern=DOMAIN)]
    scheme: Literal["https", "http"] = "https"
    paths: Annotated[list[Annotated[StrictStr, Field(pattern=PATH)]], Field(max_length=25)] = ["/"]
    invoice_id: Optional[Annotated[StrictStr, Field(pattern=INVOICE)]] = None
    invoice_override: Optional[InvoiceOverride] = None
    prompt_set_id: Optional[Annotated[StrictStr, Field(pattern=r"^seo-pst-[0-9a-f]{40}$")]] = None


class FinanceClientBind(Strict):
    request_id: RequestId
    finance_client_id: Annotated[StrictStr, Field(pattern=FINANCE_CLIENT)]


class LogIngestCreate(Strict):
    request_id: RequestId
    domain: Annotated[StrictStr, Field(pattern=DOMAIN)]
    scheme: Literal["https", "http"] = "https"
    format: Literal["combined", "common", "jsonl"]


class LogChunk(Strict):
    request_id: RequestId
    seq: Annotated[StrictInt, Field(ge=1, le=10_000_000)]
    data_b64: Annotated[StrictStr, Field(min_length=4, max_length=120_000)]
    last: StrictBool = False


class ScheduleCreate(Strict):
    request_id: RequestId
    domain: Annotated[StrictStr, Field(pattern=DOMAIN)]
    scheme: Literal["https", "http"] = "https"
    paths: Annotated[list[Annotated[StrictStr, Field(pattern=PATH)]], Field(max_length=25)] = ["/"]
    every_days: Annotated[StrictInt, Field(ge=1, le=90)]
    invoice_id: Optional[Annotated[StrictStr, Field(pattern=INVOICE)]] = None
    invoice_override: Optional[InvoiceOverride] = None
    prompt_set_id: Optional[Annotated[StrictStr, Field(pattern=r"^seo-pst-[0-9a-f]{40}$")]] = None


class ScheduleStatus(Strict):
    request_id: RequestId
    status: Literal["active", "paused"]


class AgentMove(Strict):
    request_id: RequestId
    to: Literal["active", "watch", "retrain", "restricted", "retired"]
    expected_state: Literal["active", "watch", "retrain", "restricted", "retired"]
    reason: Annotated[StrictStr, Field(pattern=r"^[A-Z_]{3,40}$")]


class PromptSetCreate(Strict):
    request_id: RequestId
    name: Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")]
    brand_terms: Annotated[list[Annotated[StrictStr, Field(min_length=2, max_length=80)]],
                           Field(min_length=1, max_length=10)]
    competitor_domains: Annotated[list[Annotated[StrictStr, Field(pattern=DOMAIN)]], Field(max_length=20)] = []
    prompts: Annotated[list[Annotated[StrictStr, Field(min_length=3, max_length=500)]],
                       Field(min_length=1, max_length=50)]
    engines: Annotated[list[Literal["openai", "anthropic", "google", "perplexity"]],
                       Field(min_length=1, max_length=4)] = ["openai", "anthropic", "google", "perplexity"]
