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


class EntityValue(Strict):
    field: Literal["name", "street_address", "locality", "region", "telephone"]
    value: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    source: Annotated[StrictStr, Field(max_length=40)]
    provenance: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    authority: Annotated[StrictStr, Field(max_length=40)]


class EntityCreate(Strict):
    request_id: RequestId
    fields: Annotated[list[EntityValue], Field(min_length=1, max_length=5)]


class EntityFieldSet(Strict):
    request_id: RequestId
    field: Annotated[StrictStr, Field(max_length=40)]
    value: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    source: Annotated[StrictStr, Field(max_length=40)]
    provenance: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    authority: Annotated[StrictStr, Field(max_length=40)]
    expected_version: Annotated[StrictInt, Field(ge=1, le=1_000_000)]


class ClientReportCreate(Strict):
    request_id: RequestId
    format: Literal["markdown", "html"]


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


# A Search Console property: a Domain property or a URL-prefix property (Google's two forms; gsc_client.PROPERTY)
GSC_PROPERTY = r"^(sc-domain:[a-z0-9.-]{4,253}|https?://[a-z0-9.-]{4,253}/[\x21-\x7e]{0,200})$"


class GscPropertySet(Strict):
    """Andre binds a tenant to its Search Console property (null unbinds)."""
    request_id: RequestId
    property: Annotated[StrictStr, Field(pattern=GSC_PROPERTY)] | None


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


class BrandTermRule(Strict):
    """ADR 0046 review round 1 L1 (agents/brand_mention.py): how one brand term is matched. ``term`` must be one of
    the prompt set's brand terms."""
    term: Annotated[StrictStr, Field(min_length=2, max_length=80)]
    case_sensitive: StrictBool = False
    context_terms: Annotated[list[Annotated[StrictStr, Field(min_length=2, max_length=80)]], Field(max_length=10)] = []
    negative_terms: Annotated[list[Annotated[StrictStr, Field(min_length=2, max_length=80)]],
                              Field(max_length=10)] = []
    common_word: Optional[StrictBool] = None


class PromptSetCreate(Strict):
    request_id: RequestId
    name: Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")]
    brand_terms: Annotated[list[Annotated[StrictStr, Field(min_length=2, max_length=80)]],
                           Field(min_length=1, max_length=10)]
    brand_term_rules: Annotated[list[BrandTermRule], Field(max_length=10)] = []
    competitor_domains: Annotated[list[Annotated[StrictStr, Field(pattern=DOMAIN)]], Field(max_length=20)] = []
    prompts: Annotated[list[Annotated[StrictStr, Field(min_length=3, max_length=500)]],
                       Field(min_length=1, max_length=50)]
    engines: Annotated[list[Literal["openai", "anthropic", "google", "perplexity"]],
                       Field(min_length=1, max_length=4)] = ["openai", "anthropic", "google", "perplexity"]


class CitationRunCreate(Strict):
    """ADR 0046: Callum's citation-source run for one of the tenant's domains and prompt sets; ``engines`` narrows the
    prompt set's engines (never widens them)."""
    request_id: RequestId
    domain: Annotated[StrictStr, Field(pattern=DOMAIN)]
    prompt_set_id: Annotated[StrictStr, Field(pattern=r"^seo-pst-[0-9a-f]{40}$")]
    engines: Optional[Annotated[list[Literal["openai", "anthropic", "google", "perplexity"]],
                                Field(min_length=1, max_length=4)]] = None


class ConversationRunCreate(Strict):
    """ADR 0049: one conversation-mining + prompt-volume fireteam run (Theo, Naomi, Zara) for one of the tenant's
    domains and prompt sets. ``competitor_terms``: competitor brand names Theo looks for in answer text (the prompt
    set's competitor domains are matched in citations). ``priya_handoff``: Priya's ``priya-emerging-handoff/1``
    document as she returned it (validated by agents/priya_handoff.py; its tenant must be this tenant)."""
    request_id: RequestId
    domain: Annotated[StrictStr, Field(pattern=DOMAIN)]
    prompt_set_id: Annotated[StrictStr, Field(pattern=r"^seo-pst-[0-9a-f]{40}$")]
    engines: Optional[Annotated[list[Literal["openai", "anthropic", "google", "perplexity"]],
                                Field(min_length=1, max_length=4)]] = None
    competitor_terms: Annotated[list[Annotated[StrictStr, Field(min_length=2, max_length=80)]],
                                Field(max_length=10)] = []
    priya_handoff: Optional[dict] = None
