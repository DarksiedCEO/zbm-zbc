"""
The OpenAI answer engine (ADR 0046): the Responses API with the built-in ``web_search`` tool, behind the shared
multi-engine adapter (answer_adapter.py). NOT_CONNECTED unless ``SEO_OPENAI_ENABLED=1`` AND a key is configured;
nothing here has been called against the real API (fixtures only, built from the documented shapes).

Documentation read on Nov 8 2026 (a web-fetch tool's reading of the page, not the raw page; nothing was called):
  https://developers.openai.com/api/docs/guides/tools-web-search.md  ("Web search", Responses API)
  - ``POST https://api.openai.com/v1/responses``; tool ``{"type": "web_search"}`` ("recommended for new
    integrations"; ``web_search_preview`` is legacy); the guide's examples use the model ``gpt-6-astra``;
  - the response's ``output`` holds a ``web_search_call`` item (``id``, ``status``, ``action`` of type ``search`` /
    ``open_page`` / ``find_in_page``) and a ``message`` item whose ``content[]`` part of type ``output_text`` carries
    ``text`` and ``annotations[]`` of type ``url_citation`` (``url``, ``title``, ``start_index``, ``end_index``);
  - ``include: ["web_search_call.action.sources"]`` returns every URL the model CONSULTED (usually more than it
    cited) on the call's ``action.sources``;
  - "Search actions incur a tool call cost": the page states no per-call amount (the pricing page was not read), so a
    request is charged at the configured reserve (svc_engines.py): the response carries no cost.
Bearer auth (``Authorization: Bearer <key>``) and the ``object`` / ``status`` / ``model`` / ``usage`` fields are the
Responses API's documented response object as known from its reference; that page was NOT re-read for this build,
so every one of them is strict here and a different shape is ENGINE_PROTOCOL_ERROR, never a guessed answer.

Methodology (Callum): cited = a ``url_citation`` annotation of the answer text. When the response carries
``action.sources`` for its search calls, an annotation counts only when its URL is among them (a URL the model wrote
that its own search never returned is not a citation: the Perplexity rule, AEGIS pplx-agent M1); without sources the
annotations count and the answer says so (``citation_basis``). The ``utm_source=openai`` query pair is removed before
comparing and recording (UNVERIFIED: the reference was not read; removing a pair that is absent changes nothing).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from typing import Annotated, Callable, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from answer_adapter import (MAX_ANSWER_CHARS, MAX_SOURCES, SYSTEM_PROMPT, ApiKey, EngineError, HttpAnswerEngine,
                            dedupe, drift_names, join_text, strict_json)
from finance_client import _DnsGate, _Resolver, _system_resolve
from ports import ProviderAnswer

ENGINE = "openai"
BASE_URL = "https://api.openai.com"
HOST = "api.openai.com"
PATH = "/v1/responses"
MODELS = ("gpt-6-astra",)                       # closed; the guide's example model
DEFAULT_MODEL = "gpt-6-astra"
DOCS = {"url": "https://developers.openai.com/api/docs/guides/tools-web-search.md", "read": "2026-11-08",
        "version": "Responses API, POST /v1/responses, tool web_search"}
STATUSES = ("completed", "failed", "incomplete", "in_progress", "queued", "cancelled")
MAX_OUTPUT_ITEMS = 64
MAX_PARTS = 64
MAX_ANNOTATIONS = 200
_SNAPSHOT = re.compile(r"-[0-9]{4}-[0-9]{2}-[0-9]{2}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


Url = Annotated[StrictStr, Field(min_length=1, max_length=2048)]
Count = Annotated[StrictInt, Field(ge=0, le=100_000_000)]


class _Annotation(_Strict):
    type: Annotated[StrictStr, Field(max_length=40)]
    url: Optional[Url] = None
    title: Optional[Annotated[StrictStr, Field(max_length=2000)]] = None
    start_index: Optional[Annotated[StrictInt, Field(ge=0, le=10**7)]] = None
    end_index: Optional[Annotated[StrictInt, Field(ge=0, le=10**7)]] = None


class _Part(_Strict):
    type: Annotated[StrictStr, Field(max_length=40)]                 # output_text | refusal (others ignored)
    text: Optional[Annotated[StrictStr, Field(max_length=MAX_ANSWER_CHARS)]] = None
    refusal: Optional[Annotated[StrictStr, Field(max_length=MAX_ANSWER_CHARS)]] = None
    annotations: Optional[Annotated[list[_Annotation], Field(max_length=MAX_ANNOTATIONS)]] = None


class _Message(_Strict):
    type: Literal["message"]
    role: Literal["assistant"]
    content: Annotated[list[_Part], Field(max_length=MAX_PARTS)]


class _Source(_Strict):
    url: Optional[Url] = None                                          # a real-time feed label carries no url


class _Action(_Strict):
    type: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    sources: Optional[Annotated[list[_Source], Field(max_length=MAX_SOURCES * 2)]] = None


class _SearchCall(_Strict):
    type: Literal["web_search_call"]
    status: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    action: Optional[_Action] = None


class _Item(_Strict):
    type: Annotated[StrictStr, Field(min_length=1, max_length=60)]


class _Usage(_Strict):
    input_tokens: Count
    output_tokens: Count
    total_tokens: Count


class _Response(_Strict):
    id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    object: Literal["response"]
    created_at: Annotated[StrictInt, Field(ge=0, le=10**11)]
    status: Literal[STATUSES]
    model: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    output: Annotated[list[dict], Field(max_length=MAX_OUTPUT_ITEMS)]
    usage: Optional[_Usage] = None


# documented / echoed top-level fields this parser does not read (not drift)
_KNOWN_UNREAD = frozenset({"error", "incomplete_details", "instructions", "max_output_tokens", "metadata",
                           "parallel_tool_calls", "previous_response_id", "reasoning", "store", "temperature", "text",
                           "tool_choice", "tools", "top_p", "truncation", "user", "service_tier", "include",
                           "background", "completed_at"})


def _strip_tracking(url: str) -> str:
    """``utm_source=openai`` removed from the query (an engine-added tracking pair, not part of the page)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    if not p.query:
        return url
    pairs = parse_qsl(p.query, keep_blank_values=True)
    q = [(k, v) for k, v in pairs if not (k == "utm_source" and v == "openai")]
    if len(q) == len(pairs):
        return url                                    # nothing removed: the URL exactly as the engine gave it
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


def model_matches(reported: str, asked: str) -> Optional[str]:
    """The version a reported model carries when it is the asked model or a dated snapshot of it, else None."""
    if reported == asked:
        return "?"
    if reported.startswith(asked) and _SNAPSHOT.fullmatch(reported[len(asked):]):
        return reported[len(asked) + 1:]
    return None


def request_body(model: str, prompt: str) -> bytes:
    """Exactly: the model, the fixed instruction, the prompt, the ``web_search`` tool, the sources include, and
    ``store: false``. No tenant, brand, domain or user identifier."""
    return json.dumps({"model": model, "instructions": SYSTEM_PROMPT, "input": prompt,
                       "tools": [{"type": "web_search"}], "include": ["web_search_call.action.sources"],
                       "store": False}).encode("utf-8")


def parse_response(body: bytes, allowed_models: tuple = MODELS) -> ProviderAnswer:
    try:
        obj = strict_json(body)
        r = _Response.model_validate(obj)
        types, messages, calls = [], [], []
        for raw in r.output:
            t = _Item.model_validate(raw).type
            types.append(t)
            if t == "message":
                messages.append(_Message.model_validate(raw))
            elif t == "web_search_call":
                calls.append(_SearchCall.model_validate(raw))
    except (UnicodeDecodeError, ValueError, RecursionError, ValidationError, TypeError):
        raise EngineError("ENGINE_PROTOCOL_ERROR") from None
    if r.status != "completed":
        raise EngineError("ENGINE_PROTOCOL_ERROR", "RESPONSE_" + r.status.upper())
    version = next((v for v in (model_matches(r.model, m) for m in allowed_models) if v is not None), None)
    if version is None:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED")
    parts = [p for m in messages for p in m.content]
    # WR-F011: each message item is its own group (anything between them is another output item)
    text = join_text([[p.text for p in m.content if p.type == "output_text" and p.text] for m in messages])
    refused = any(p.type == "refusal" and p.refusal for p in parts)
    usage = None if r.usage is None else {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens,
                                          "total_tokens": r.usage.total_tokens, "total_cost_usd": None,
                                          "search_calls": len(calls)}
    if not text.strip():
        if refused:
            return ProviderAnswer("REFUSAL", "", [], r.model, version, cite_from_text=False, usage=usage,
                                  finish_reason="refusal")
        raise EngineError("ENGINE_PROTOCOL_ERROR", "NO_ANSWER_TEXT")
    if len(text) > MAX_ANSWER_CHARS:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
    sources = {_strip_tracking(s.url) for c in calls if c.action and c.action.sources for s in c.action.sources
               if s.url}
    have_sources = any(c.action is not None and c.action.sources is not None for c in calls)
    cited, unverified = [], 0
    for p in parts:
        for a in (p.annotations or ()):
            if a.type != "url_citation" or not a.url:
                continue
            u = _strip_tracking(a.url)
            if have_sources and u not in sources:
                unverified += 1                  # a URL its own search never returned: not a citation
                continue
            cited.append(u)
    ans = ProviderAnswer("ANSWER", text, dedupe(cited), r.model, version, cite_from_text=False, usage=usage,
                         answer_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                         finish_reason=r.status)
    ans.citation_basis = "annotations_in_search_sources" if have_sources else "annotations_only"
    ans.unverified_citations = unverified
    ans.unmodelled_keys = drift_names(list(obj.keys()) + ["output." + t for t in types],
                                      set(_Response.model_fields) | _KNOWN_UNREAD
                                      | {"output.message", "output.web_search_call", "output.reasoning"})
    return ans


_GATE = _DnsGate(2)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GATE._after_fork_in_child)


class OpenAIEngine(HttpAnswerEngine):
    name = ENGINE
    BASE_URL = BASE_URL
    ALLOWED_HOSTS = frozenset({HOST})

    def __init__(self, key: ApiKey, model: Optional[str] = None, timeout_s: float = 60.0, attempts: int = 2,
                 backoff_s: tuple = (0.5, 1.5), transport: Optional[httpx.BaseTransport] = None,
                 network_backend=None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, resolve: Callable[[str, int], list] = _system_resolve,
                 dns_gate: Optional[_DnsGate] = None, rand: Callable[[], float] = random.random,
                 public: Optional[Callable[[str], bool]] = None, wall=None):
        model = DEFAULT_MODEL if model is None else model
        if model not in MODELS:
            raise ValueError("model not in the allowlist")
        self.models, self.model = MODELS, model
        self._init_http(key, timeout_s, attempts, backoff_s, transport, network_backend, monotonic, sleep,
                        _Resolver(resolve, dns_gate if dns_gate is not None else _GATE), rand, public, wall)

    def __repr__(self) -> str:
        return f"OpenAIEngine(model={self.model!r}, timeout_s={self.timeout_s}, attempts={self.attempts})"

    def describe(self) -> dict:
        return {"state": "connected", "base_url": BASE_URL, "path": PATH, "model": self.model,
                "models_allowed": list(MODELS), "tool": "web_search", "timeout_s": self.timeout_s,
                "attempts": self.attempts, "key_source": self._key.source,
                "max_response_bytes": self.MAX_RESPONSE_BYTES, "docs": DOCS}

    def _path(self, model: str) -> str:
        return PATH

    def _auth_headers(self) -> dict:
        return {"Authorization": "Bearer " + self._key.reveal()}

    def _body(self, model: str, prompt: str) -> bytes:
        return request_body(model, prompt)

    def _parse(self, raw: bytes, model: str) -> ProviderAnswer:
        return parse_response(raw, (model,))
