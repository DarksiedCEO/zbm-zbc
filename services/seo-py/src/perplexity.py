"""
The Perplexity answer-engine adapter (ADR 0017 Wave 4; flag 1: Perplexity included, founder-chosen Oct 2026).

One request kind per protocol, and the Agent API is the default (ADR 0017 W4-1 and W4-8, docs re-read on Oct 24
2026):

  - ``agent`` (default): ``POST https://api.perplexity.ai/v1/agent`` with ``model`` (closed allowlist, default
    ``perplexity/sonar``), ``effort: "fast"`` (the migration guide's replacement for ``sonar``; the model field
    overrides the effort's model), ``instructions`` (one fixed line), ``input`` (the prompt as a plain string),
    ``stream: false`` and ``store: false``;
  - ``sonar`` (rollback only, ``SEO_PERPLEXITY_PROTOCOL=sonar``): the old ``POST /v1/sonar`` chat-completions
    request, past Perplexity's end of support (Sep 27 2026) and served by reformulating it as an Agent API request.

Nothing else is ever called: no other host, path or method, no redirects followed, no proxy from the environment.

Rules:
  - fixed base URL and a CLOSED model allowlist per protocol (``MODELS`` / ``SONAR_MODELS``); a response reporting
    any other model than the one asked is ENGINE_PROTOCOL_ERROR (``MODEL_NOT_ALLOWED``);
  - one HARD overall deadline per question, over DNS, connect, TLS, every read and write and the body loop
    (``_GuardedDeadlineBackend``: the Finance client's deadline backend pattern, plus the kill-switch guard before
    every socket operation and again after the response, every read waiting at most ``READ_SLICE_S`` between guard
    checks, plus an address check: api.perplexity.ai must resolve to public addresses only);
  - a bounded response (``MAX_RESPONSE_BYTES``, identity coding only, JSON only, duplicate keys refused);
  - bounded retries ONLY on 429, 5xx and transport errors; ``Retry-After`` (whole seconds) honoured when it fits in
    the time left, otherwise the answer is ENGINE_RATE_LIMITED at once (never a sleep past the deadline);
  - STRICT parsing of what we rely on (pydantic, strict types). Agent API: ``object == "response"``, ``status ==
    "completed"`` (any other status is ENGINE_PROTOCOL_ERROR with detail ``RESPONSE_<STATUS>``), the ``message``
    output items' ``output_text`` parts (the answer), the ``search_results`` output items' ``results[].url`` (the
    sources), ``url_citation`` annotation URLs, the model and ``usage`` / ``usage.cost`` (``currency`` must be USD).
    Output item types we do not read (``fetch_url_results``, ``function_call``, ...) and fields we do not read are
    ignored and dropped, never stored; their NAMES are reported for drift (``unmodelled_keys``);
  - Sonar (rollback): citations from ``search_results[].url`` first and from ``citations`` when present (removed per
    the changelog; the parser tolerates its absence);
  - usage and cost (``usage.cost.total_cost``) captured as Decimal strings for the spend budgets; no cost means the
    request is charged at the reserve (svc_engines.py);
  - the API key is held in ``ApiKey`` (its repr is masked; ``reveal()`` is called in exactly one place, the request
    header) and never reaches a log line, an exception, a report, the log or the ledger;
  - the answer is DATA: its text is returned to the probe framework to be scanned, hashed and dropped; citations
    come only from structured fields, never from URLs in the text.

Closed reason codes (``CODES``): ENGINE_AUTH_REFUSED, ENGINE_RATE_LIMITED, ENGINE_UNAVAILABLE, ENGINE_PROTOCOL_ERROR,
ENGINE_BUDGET_EXHAUSTED, ENGINE_KILLED, ENGINE_NOT_CONNECTED.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from decimal import Decimal  # noqa: F401 - part of this module's names since Wave 4 (tests read perplexity.Decimal)
from typing import Annotated, Callable, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, ValidationError

# The shared plumbing (ADR 0046): the error type, the key holder, the network layer and the ask loop moved to
# answer_adapter.py UNCHANGED in behaviour; they are re-exported here so every existing name keeps working.
from answer_adapter import (CODES, MAX_ANSWER_CHARS, MAX_RESPONSE_BYTES, MAX_RETRY_AFTER_S, MAX_SOURCES,  # noqa: F401
                            MAX_UNMODELLED_KEYS, PROTOCOL_ERROR_STOP_AFTER, READ_SLICE_S, STOP_CODES, SYSTEM_PROMPT,
                            ApiKey, EngineError, HttpAnswerEngine, _GuardedDeadlineBackend, _Killed, _Retry,
                            _retry_after, _Stream, join_text, money)
from answer_adapter import no_duplicates as _no_duplicates
from finance_client import _DnsGate, _Resolver, _system_resolve
from ports import ProviderAnswer

# the documentation this adapter was built from (ADR 0017 W4-1; re-read for the Agent API migration)
DOCS = {"url": "https://docs.perplexity.ai/api-reference/agent-post", "read": "2026-10-24",
        "version": "Agent API, POST /v1/agent (Sonar /v1/sonar as the rollback)"}

ENGINE = "perplexity"
BASE_URL = "https://api.perplexity.ai"
HOST = "api.perplexity.ai"
# the Agent API (default since Oct 24 2026, ADR 0017 W4-8) and the legacy Sonar request (rollback only)
AGENT, SONAR = "agent", "sonar"
PROTOCOLS = (AGENT, SONAR)
DEFAULT_PROTOCOL = AGENT
PATHS = {AGENT: "/v1/agent", SONAR: "/v1/sonar"}
PATH = PATHS[DEFAULT_PROTOCOL]
# closed: Perplexity's own grounded search model on the Agent API (docs: Agent API Models lists `perplexity/sonar`)
MODELS = ("perplexity/sonar",)
DEFAULT_MODEL = "perplexity/sonar"
SONAR_MODELS = ("sonar", "sonar-pro")       # the rollback protocol's allowlist; no deep research
SONAR_DEFAULT_MODEL = "sonar"
MODELS_BY_PROTOCOL = {AGENT: MODELS, SONAR: SONAR_MODELS}
DEFAULT_MODEL_BY_PROTOCOL = {AGENT: DEFAULT_MODEL, SONAR: SONAR_DEFAULT_MODEL}
# the same model under either protocol's name, so flipping the protocol never strands a setting (AEGIS L1);
# `sonar-pro` has no documented Agent API model: under `agent` the engine is NOT_CONNECTED (the service still starts)
MODEL_ALIASES = {AGENT: {"sonar": "perplexity/sonar"}, SONAR: {"perplexity/sonar": "sonar"}}
# the migration guide's replacement for `sonar` ("sonar -> fast"); the request's model overrides the effort's model
AGENT_EFFORT = "fast"
MAX_OUTPUT_ITEMS = 64
MAX_CONTENT_PARTS = 64
MAX_ANNOTATIONS = 200
# CODES, STOP_CODES, PROTOCOL_ERROR_STOP_AFTER and SYSTEM_PROMPT are the shared adapter's (answer_adapter.py)


# ---------------------------------------------------------------------------------------------- the response shape

class _Strict(BaseModel):
    # strict types on every field we model; fields we do not model are ignored (dropped, never read or stored)
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


Bounded = Annotated[StrictStr, Field(max_length=2000)]


class _Message(_Strict):
    role: Literal["assistant"]
    content: Annotated[StrictStr, Field(max_length=MAX_ANSWER_CHARS)]


class _Choice(_Strict):
    index: Optional[Annotated[StrictInt, Field(ge=0, le=16)]] = None      # the reference examples elide it
    finish_reason: Optional[Annotated[StrictStr, Field(pattern=r"^[a-z_]{1,40}$")]] = None
    message: _Message


class _SearchResult(_Strict):
    title: Optional[Bounded] = None
    url: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    date: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    last_updated: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    snippet: Optional[Annotated[StrictStr, Field(max_length=20_000)]] = None
    source: Optional[Annotated[StrictStr, Field(max_length=100)]] = None
    place_metadata: Optional[dict] = None          # documented; never read, never stored


Money = Annotated[StrictFloat | StrictInt, Field(ge=0, le=1000)]


class _Cost(_Strict):
    input_tokens_cost: Optional[Money] = None
    output_tokens_cost: Optional[Money] = None
    request_cost: Optional[Money] = None
    reasoning_tokens_cost: Optional[Money] = None
    citation_tokens_cost: Optional[Money] = None
    search_queries_cost: Optional[Money] = None
    total_cost: Money


Count = Annotated[StrictInt, Field(ge=0, le=100_000_000)]


class _Usage(_Strict):
    prompt_tokens: Optional[Count] = None
    completion_tokens: Optional[Count] = None
    total_tokens: Optional[Count] = None
    search_context_size: Optional[Literal["low", "medium", "high"]] = None
    citation_tokens: Optional[Count] = None
    num_search_queries: Optional[Count] = None
    reasoning_tokens: Optional[Count] = None
    cost: Optional[_Cost] = None


class _Response(_Strict):
    id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    model: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    created: Annotated[StrictInt, Field(ge=0, le=10**11)]
    object: Literal["chat.completion"]
    choices: Annotated[list[_Choice], Field(min_length=1, max_length=4)]
    # removed per the changelog (May 2025), still in the Sonar reference examples: optional, used when present
    citations: Optional[Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=2048)]],
                                  Field(max_length=MAX_SOURCES)]] = None
    search_results: Optional[Annotated[list[_SearchResult], Field(max_length=MAX_SOURCES)]] = None
    usage: Optional[_Usage] = None


def unmodelled_keys(obj: dict) -> tuple:
    """The NAMES (never the values) of top-level response keys the parser does not model, sorted and bounded, so a
    moved or renamed citations field shows up (AEGIS round 2). A name that is not a plain identifier is reported as
    "<other>"."""
    names = set()
    for k in obj:
        if k in _Response.model_fields:
            continue
        names.add(k if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", k) else "<other>")
    return tuple(sorted(names)[:MAX_UNMODELLED_KEYS])


def parse_response(body: bytes, allowed_models: tuple = SONAR_MODELS) -> ProviderAnswer:
    """The documented Sonar response (the rollback protocol) -> a ProviderAnswer. A field we rely on with any other shape is
    ENGINE_PROTOCOL_ERROR. Sources: ``search_results[].url`` in order (primary), then any ``citations`` URL not
    already listed."""
    try:
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=lambda c: (_ for _ in ()).throw(ValueError("NaN")))
        r = _Response.model_validate(obj)
    except (UnicodeDecodeError, ValueError, RecursionError, ValidationError, TypeError):
        raise EngineError("ENGINE_PROTOCOL_ERROR") from None
    if r.model not in allowed_models:
        cost = money(r.usage.cost.total_cost) if r.usage is not None and r.usage.cost is not None else None
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED", cost)
    first = min(r.choices, key=lambda c: 0 if c.index is None else c.index)
    sources: list = []
    for u in [s.url for s in (r.search_results or ())] + list(r.citations or ()):
        if u not in sources:
            sources.append(u)
    usage = None
    if r.usage is not None:
        u = r.usage
        usage = {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                 "total_tokens": u.total_tokens, "search_context_size": u.search_context_size,
                 "num_search_queries": u.num_search_queries,
                 "total_cost_usd": money(u.cost.total_cost) if u.cost is not None else None}
    text = first.message.content
    ans = ProviderAnswer("ANSWER", text, sources[:MAX_SOURCES], r.model, None, cite_from_text=False,
                         usage=usage, answer_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                         finish_reason=first.finish_reason)
    ans.unmodelled_keys = unmodelled_keys(obj)        # names only (drift visibility), read by agents/probes.py
    return ans


def cost_of(ans: ProviderAnswer) -> Optional[str]:
    return (ans.usage or {}).get("total_cost_usd")


def resolve_model(protocol: str, raw: Optional[str]) -> str:
    """A configured model name -> the one sent, for this protocol: the protocol's default when unset, the documented
    alias (``sonar`` -> ``perplexity/sonar`` on the Agent API), otherwise it must be on the protocol's closed list.
    Raises ValueError naming the allowed values (never echoing the value: a mistyped value could be the key)."""
    if protocol not in PROTOCOLS:
        raise ValueError(f"the protocol must be one of {', '.join(PROTOCOLS)}")
    name = (raw or "").strip() or DEFAULT_MODEL_BY_PROTOCOL[protocol]
    name = MODEL_ALIASES[protocol].get(name, name)
    if name not in MODELS_BY_PROTOCOL[protocol]:
        raise ValueError(f"must be one of {', '.join(MODELS_BY_PROTOCOL[protocol])} with the {protocol} protocol")
    return name


# ------------------------------------------------------------------------------- the Agent API response (default)
#
# POST /v1/agent, 200, `stream: false`: `ResponsesResponse` in the API reference (docs.perplexity.ai/api-reference/
# agent-post, read Oct 24 2026). Required there: id, object ("response"), created_at (int64), status, model, output.
# `output` is a list of items discriminated by `type`; we read `message` (role "assistant", content parts of type
# "output_text" with `text` and optional `annotations`) and `search_results` (`results[]`: id, url, title, snippet
# required; date, last_updated, source optional). `usage`: input_tokens, output_tokens, total_tokens required; `cost`
# optional with currency ("USD"), input_cost, output_cost, total_cost required (numbers).

AGENT_STATUSES = ("completed", "failed", "incomplete", "in_progress", "queued", "cancelled")


class _AgentAnnotation(_Strict):
    # every field optional in the reference ("URL citation"); only `type == "url_citation"` with a url is used
    type: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    url: Optional[Annotated[StrictStr, Field(min_length=1, max_length=2048)]] = None
    title: Optional[Bounded] = None
    start_index: Optional[Annotated[StrictInt, Field(ge=0, le=10**7)]] = None
    end_index: Optional[Annotated[StrictInt, Field(ge=0, le=10**7)]] = None


class _AgentPart(_Strict):
    type: Literal["output_text"]                      # the reference's ContentPartType has this one value
    text: Annotated[StrictStr, Field(max_length=MAX_ANSWER_CHARS)]
    annotations: Optional[Annotated[list[_AgentAnnotation], Field(max_length=MAX_ANNOTATIONS)]] = None


class _AgentMessage(_Strict):
    type: Literal["message"]
    id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    status: Literal[AGENT_STATUSES]
    role: Literal["assistant"]
    content: Annotated[list[_AgentPart], Field(max_length=MAX_CONTENT_PARTS)]


class _AgentResult(_Strict):
    id: Annotated[StrictInt, Field(ge=0, le=10**9)]
    url: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    title: Annotated[StrictStr, Field(max_length=2000)]
    snippet: Annotated[StrictStr, Field(max_length=20_000)]
    date: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    last_updated: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    source: Optional[Annotated[StrictStr, Field(max_length=100)]] = None


class _AgentSearchResults(_Strict):
    type: Literal["search_results"]
    results: Annotated[list[_AgentResult], Field(max_length=MAX_SOURCES)]
    queries: Optional[Annotated[list[Annotated[StrictStr, Field(max_length=2000)]], Field(max_length=50)]] = None


class _AgentCost(_Strict):
    currency: Literal["USD"]
    input_cost: Money
    output_cost: Money
    total_cost: Money


class _AgentUsage(_Strict):
    input_tokens: Count
    output_tokens: Count
    total_tokens: Count
    cost: Optional[_AgentCost] = None


class _AgentItem(_Strict):
    # every output item: only its discriminator is read here; the two item kinds we use are validated in full below
    type: Annotated[StrictStr, Field(min_length=1, max_length=60)]


class _AgentResponse(_Strict):
    id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    object: Literal["response"]
    created_at: Annotated[StrictInt, Field(ge=0, le=10**11)]
    status: Literal[AGENT_STATUSES]
    model: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    output: Annotated[list[dict], Field(max_length=MAX_OUTPUT_ITEMS)]
    effort: Optional[Annotated[StrictStr, Field(max_length=80)]] = None
    usage: Optional[_AgentUsage] = None


# documented top-level fields this parser does not read, so they are not drift (AEGIS L5): `error` (ErrorInfo, in the
# reference schema); every other top-level key of the quickstart's example responses
# (docs.perplexity.ai/docs/agent-api/quickstart, read Oct 25 2026: request settings echoed back, timestamps, ...);
# and `preset` (the changelog, Oct 2026: "`preset` is still accepted as a deprecated alias, and responses return both
# fields")
_AGENT_KNOWN_UNREAD = frozenset({
    "error", "preset", "background", "completed_at", "frequency_penalty", "incomplete_details", "instructions",
    "max_output_tokens", "max_tool_calls", "metadata", "parallel_tool_calls", "presence_penalty",
    "previous_response_id", "prompt_cache_key", "reasoning", "safety_identifier", "service_tier", "store",
    "temperature", "text", "tool_choice", "tools", "top_logprobs", "top_p", "truncation", "user"})
_EFFORT_RX = re.compile(r"[a-z]{1,10}(@[A-Za-z0-9._-]{1,40})?")


def _agent_drift(obj: dict, item_types: list) -> tuple:
    """Top-level keys the Agent parser does not model, plus ``output.<type>`` for every output item type it does not
    read (NAMES only, sorted and bounded; a name that is not a plain identifier is "<other>")."""
    names = set()
    for k in obj:
        if k not in _AgentResponse.model_fields and k not in _AGENT_KNOWN_UNREAD:
            names.add(k if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", k) else "<other>")
    for t in item_types:
        if t not in ("message", "search_results"):
            names.add("output." + t if re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", t) else "output.<other>")
    return tuple(sorted(names)[:MAX_UNMODELLED_KEYS])


def parse_agent_response(body: bytes, allowed_models: tuple = MODELS) -> ProviderAnswer:
    """The documented Agent API response -> a ProviderAnswer. A field we rely on with any other shape is
    ENGINE_PROTOCOL_ERROR; a status other than ``completed`` is ENGINE_PROTOCOL_ERROR with detail
    ``RESPONSE_<STATUS>`` (the docs do not say what a partial answer means, so none is counted); no assistant text is
    ENGINE_PROTOCOL_ERROR ``NO_ANSWER_TEXT``. The answer is every ``output_text`` part of every ``message`` item in
    order (what the SDKs' ``output_text`` aggregates). Sources: ``search_results`` items' ``results[].url`` in output
    order (the docs: "treat the id and url fields of each search_results entry as the source of truth for
    citations"), then any ``url_citation`` annotation URL not already listed. No source at all is allowed (counted as
    a zero-source answer, drift-visible), never invented."""
    try:
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=lambda c: (_ for _ in ()).throw(ValueError("NaN")))
        r = _AgentResponse.model_validate(obj)
        types, messages, results = [], [], []
        for raw_item in r.output:
            t = _AgentItem.model_validate(raw_item).type
            types.append(t)
            if t == "message":
                messages.append(_AgentMessage.model_validate(raw_item))
            elif t == "search_results":
                results.append(_AgentSearchResults.model_validate(raw_item))
    except (UnicodeDecodeError, ValueError, RecursionError, ValidationError, TypeError):
        raise EngineError("ENGINE_PROTOCOL_ERROR") from None
    # the reported cost, read before any refusal below, so a response that is not an answer is still charged what it
    # cost when that is above the reserve (AEGIS L4)
    cost = money(r.usage.cost.total_cost) if r.usage is not None and r.usage.cost is not None else None
    if r.status != "completed":
        raise EngineError("ENGINE_PROTOCOL_ERROR", "RESPONSE_" + r.status.upper(), cost)
    if r.model not in allowed_models:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED", cost)
    parts = [p for m in messages for p in m.content]
    text = join_text([[p.text for p in m.content] for m in messages])     # WR-F011: one group per message item
    if not text.strip():
        raise EngineError("ENGINE_PROTOCOL_ERROR", "NO_ANSWER_TEXT", cost)
    if len(text) > MAX_ANSWER_CHARS:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE", cost)
    # Sources: search_results only ("treat the id and url fields of each search_results entry as the source of truth
    # for citations"). An annotation never adds a URL: one that search did not return (a hallucination, or a page
    # that talked the model into it) is not a citation (AEGIS M1). Cited therefore means "returned by search".
    sources: list = []
    for u in [x.url for sr in results for x in sr.results]:
        if u not in sources:
            sources.append(u)
    usage = None
    if r.usage is not None:
        u = r.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "total_tokens": u.total_tokens,
                 "total_cost_usd": money(u.cost.total_cost) if u.cost is not None else None}
    version = r.effort if r.effort is not None and _EFFORT_RX.fullmatch(r.effort) else None
    ans = ProviderAnswer("ANSWER", text, sources[:MAX_SOURCES], r.model, version, cite_from_text=False,
                         usage=usage, answer_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                         finish_reason=r.status)
    ans.unmodelled_keys = _agent_drift(obj, types)
    return ans


# ---------------------------------------------------------------------------------------------- the engine
#
# The network layer (``_GuardedDeadlineBackend`` and its stream, the guard wrapper, the retry signal) and the ask loop
# are the shared adapter's (answer_adapter.py, ADR 0046), moved there unchanged; this engine keeps its own resolver
# gate of two and supplies only what is Perplexity's: the base URL and allow-listed host, the path per protocol, the
# Bearer header, the request body and the parser.

_GATE = _DnsGate(2)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GATE._after_fork_in_child)


def request_body(protocol: str, model: str, prompt: str) -> bytes:
    """The one request body per protocol. Agent API: ``model``, ``effort`` (the migration guide's ``fast``; "any field
    you pass alongside the effort level overrides that default", so the model is ours), ``instructions`` (the one
    fixed line; "System instructions for the model"), ``input`` (a plain string: "For simple single-turn prompts,
    pass a plain string"), ``stream: false`` and ``store: false`` (the response is not kept for later retrieval).
    Sonar (rollback): ``model``, ``messages`` and ``stream: false``. Nothing else: no tenant, brand, domain or user
    identifier, no tools beyond the effort's own."""
    if protocol == AGENT:
        doc = {"model": model, "effort": AGENT_EFFORT, "instructions": SYSTEM_PROMPT, "input": prompt,
               "stream": False, "store": False}
    elif protocol == SONAR:
        doc = {"model": model, "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                            {"role": "user", "content": prompt}], "stream": False}
    else:
        raise ValueError("protocol")
    return json.dumps(doc).encode("utf-8")


class PerplexityEngine(HttpAnswerEngine):
    """The answer-engine port for Perplexity. ``ask(prompt, guard=..., meter=...)`` returns a ProviderAnswer or raises
    EngineError (and nothing else for any network or protocol outcome)."""

    connected = True
    metered = True
    accepts_guard = True
    name = ENGINE
    BASE_URL = BASE_URL
    ALLOWED_HOSTS = frozenset({HOST})

    def __init__(self, key: ApiKey, model: Optional[str] = None, timeout_s: float = 30.0, attempts: int = 3,
                 backoff_s: tuple = (0.5, 1.5), transport: Optional[httpx.BaseTransport] = None,
                 network_backend=None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, resolve: Callable[[str, int], list] = _system_resolve,
                 dns_gate: Optional[_DnsGate] = None, rand: Callable[[], float] = random.random,
                 public: Optional[Callable[[str], bool]] = None, protocol: str = DEFAULT_PROTOCOL, wall=None):
        if not isinstance(key, ApiKey):
            raise TypeError("the key must be an ApiKey")
        if protocol not in PROTOCOLS:
            raise ValueError("protocol must be agent or sonar")
        model = DEFAULT_MODEL_BY_PROTOCOL[protocol] if model is None else model
        if model not in MODELS_BY_PROTOCOL[protocol]:
            raise ValueError("model not in the allowlist")
        self.protocol = protocol
        self.path = PATHS[protocol]
        self.models = MODELS_BY_PROTOCOL[protocol]
        self.model = model
        self._init_http(key, timeout_s, attempts, backoff_s, transport, network_backend, monotonic, sleep,
                        _Resolver(resolve, dns_gate if dns_gate is not None else _GATE), rand, public, wall)

    def __repr__(self) -> str:
        return (f"PerplexityEngine(protocol={self.protocol!r}, model={self.model!r}, timeout_s={self.timeout_s}, "
                f"attempts={self.attempts})")

    def describe(self) -> dict:
        return {"state": "connected", "base_url": BASE_URL, "protocol": self.protocol, "path": self.path,
                "model": self.model, "effort": AGENT_EFFORT if self.protocol == AGENT else None,
                "models_allowed": list(self.models), "timeout_s": self.timeout_s, "attempts": self.attempts,
                "key_source": self._key.source, "max_response_bytes": MAX_RESPONSE_BYTES}

    # ------------------------------------------------------------------ the adapter hooks (answer_adapter.py)

    def _path(self, model: str) -> str:
        return self.path

    def _auth_headers(self) -> dict:
        return {"Authorization": "Bearer " + self._key.reveal()}

    def _body(self, model: str, prompt: str) -> bytes:
        return request_body(self.protocol, model, prompt)

    def _parse(self, raw: bytes, model: str) -> ProviderAnswer:
        parse = parse_agent_response if self.protocol == AGENT else parse_response
        return parse(raw, (model,))


class NotConnectedPerplexity:
    """No key configured: every question is ENGINE_NOT_CONNECTED (no request is made, nothing is invented)."""

    connected = False
    metered = False
    name = ENGINE

    def __init__(self, protocol: str = DEFAULT_PROTOCOL, reason: Optional[str] = None):
        self.protocol = protocol if protocol in PROTOCOLS else DEFAULT_PROTOCOL
        # why a configured engine is not connected (a closed code, never a setting's value): MODEL_NOT_SUPPORTED,
        # PROTOCOL_INVALID; None when simply no key is configured
        self.reason = reason if reason is None or re.fullmatch(r"[A-Z_]{1,40}", reason) else None

    def ask(self, prompt: str, model: Optional[str] = None, **_kw) -> ProviderAnswer:
        from ports import NOT_CONNECTED
        return ProviderAnswer(NOT_CONNECTED)

    def describe(self) -> dict:
        why = {None: "no API key: set SEO_PERPLEXITY_API_KEY_FILE (or SEO_PERPLEXITY_API_KEY on Railway)",
               "MODEL_NOT_SUPPORTED": "SEO_PERPLEXITY_MODEL is not on this protocol's closed list; unset it (or set "
                                      "one of models_allowed) and redeploy",
               "PROTOCOL_INVALID": "SEO_PERPLEXITY_PROTOCOL must be agent or sonar; unset it and redeploy"}
        return {"state": "NOT_CONNECTED", "base_url": BASE_URL, "protocol": self.protocol, "path": PATHS[self.protocol],
                "model": None, "models_allowed": list(MODELS_BY_PROTOCOL[self.protocol]), "reason": self.reason,
                "why": why.get(self.reason, why[None])}
