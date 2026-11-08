"""
The Anthropic answer engine (ADR 0046): the Messages API with the server-side web search tool, behind the shared
multi-engine adapter (answer_adapter.py). NOT_CONNECTED unless ``SEO_ANTHROPIC_ENABLED=1`` AND a key is configured;
nothing here has been called against the real API (fixtures only, built from the documented shapes).

Documentation read on Nov 8 2026 (the page's Markdown, fetched with a web-fetch tool; nothing was called):
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool  ("Web search tool")
  - ``POST https://api.anthropic.com/v1/messages`` with headers ``x-api-key``, ``anthropic-version`` (the page's
    cURL examples send ``2026-10-21``) and ``content-type: application/json``; body ``model``, ``max_tokens``,
    ``messages`` and ``tools: [{"type": "web_search_20250305", "name": "web_search", "max_uses": N}]``. Three tool
    versions exist; ``web_search_20250305`` is the BASIC search (no dynamic filtering, no code execution, so no
    extra tool runs and no extra charge path) and is the one used here;
  - the response is ``{"id", "role": "assistant", "content": [...], "usage", "stop_reason"}``; content blocks:
    ``text`` (optionally ``citations[]`` of type ``web_search_result_location``: ``url``, ``title``,
    ``encrypted_index``, ``cited_text`` up to 150 characters), ``server_tool_use`` (the query) and
    ``web_search_tool_result`` (``content`` a list of ``web_search_result``: ``url``, ``title``,
    ``encrypted_content``, ``page_age``; or, on an error, ONE object ``web_search_tool_result_error`` with
    ``error_code`` one of ``too_many_requests``, ``invalid_tool_input``, ``max_uses_exceeded``, ``query_too_long``,
    ``request_too_large``, ``unavailable``: the API still answers 200);
  - ``stop_reason`` ``pause_turn`` means a long turn was paused and must be resumed by sending it back: not done
    here, the answer is ENGINE_PROTOCOL_ERROR ``RESPONSE_PAUSE_TURN`` (never counted);
  - ``usage.server_tool_use.web_search_requests``; "$10 USD per 1,000 searches, plus standard token costs"; a failed
    search is not billed. The response carries no dollar cost, so a request is charged at the configured reserve.
  - supported models include ``claude-opus-5-5``, ``claude-sonnet-5-5`` and ``claude-haiku-5-5`` (the closed list).
The ``type``/``model`` fields of the response object are the Messages API's as known from its reference, which was
NOT re-read for this build: they are strict here, and a different shape is ENGINE_PROTOCOL_ERROR.

Methodology (Callum): cited = a ``web_search_result_location`` citation of the answer text whose URL its own search
returned (a ``web_search_result``). A citation URL the search did not return is not counted (the Perplexity rule,
AEGIS pplx-agent M1). ``cited_text`` and ``encrypted_*`` are never read or stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from typing import Annotated, Callable, Literal, Optional, Union

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from answer_adapter import (MAX_ANSWER_CHARS, MAX_SOURCES, SYSTEM_PROMPT, ApiKey, EngineError, HttpAnswerEngine,
                            dedupe, drift_names, strict_json)
from finance_client import _DnsGate, _Resolver, _system_resolve
from ports import ProviderAnswer

ENGINE = "anthropic"
BASE_URL = "https://api.anthropic.com"
HOST = "api.anthropic.com"
PATH = "/v1/messages"
API_VERSION = "2026-10-21"                       # the anthropic-version the page's examples send (read Nov 8 2026)
TOOL_TYPE = "web_search_20250305"                # basic web search: no dynamic filtering / code execution
MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 1024
DOCS = {"url": "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool", "read": "2026-11-08",
        "version": f"Messages API, POST /v1/messages, anthropic-version {API_VERSION}, tool {TOOL_TYPE}"}
COMPLETE = ("end_turn", "stop_sequence")
MAX_BLOCKS = 128
_SNAPSHOT = re.compile(r"-[0-9]{8}")
_REASON = re.compile(r"[a-z_]{1,30}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


Url = Annotated[StrictStr, Field(min_length=1, max_length=2048)]
Count = Annotated[StrictInt, Field(ge=0, le=100_000_000)]


class _Citation(_Strict):
    type: Annotated[StrictStr, Field(max_length=60)]
    url: Optional[Url] = None


class _Text(_Strict):
    type: Literal["text"]
    text: Annotated[StrictStr, Field(max_length=MAX_ANSWER_CHARS)]
    citations: Optional[Annotated[list[_Citation], Field(max_length=200)]] = None


class _Result(_Strict):
    type: Literal["web_search_result"]
    url: Url
    title: Optional[Annotated[StrictStr, Field(max_length=2000)]] = None
    page_age: Optional[Annotated[StrictStr, Field(max_length=100)]] = None


class _ToolError(_Strict):
    type: Literal["web_search_tool_result_error"]
    error_code: Annotated[StrictStr, Field(max_length=40)]


class _ToolResult(_Strict):
    type: Literal["web_search_tool_result"]
    content: Union[Annotated[list[_Result], Field(max_length=MAX_SOURCES)], _ToolError]


class _Block(_Strict):
    type: Annotated[StrictStr, Field(min_length=1, max_length=60)]


class _Usage(_Strict):
    input_tokens: Count
    output_tokens: Count
    server_tool_use: Optional[dict] = None


class _Response(_Strict):
    id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    type: Literal["message"]
    role: Literal["assistant"]
    model: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    content: Annotated[list[dict], Field(max_length=MAX_BLOCKS)]
    stop_reason: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    usage: Optional[_Usage] = None


_KNOWN_UNREAD = frozenset({"stop_sequence", "container", "context_management"})


def model_matches(reported: str, asked: str) -> Optional[str]:
    if reported == asked:
        return "?"
    if reported.startswith(asked) and _SNAPSHOT.fullmatch(reported[len(asked):]):
        return reported[len(asked) + 1:]
    return None


def request_body(model: str, prompt: str, max_uses: int = 3) -> bytes:
    """Exactly: the model, ``max_tokens``, the fixed instruction (``system``), the prompt as one user message and the
    basic web search tool with ``max_uses``. No tenant, brand, domain or user identifier, no ``user_location``."""
    return json.dumps({"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT,
                       "messages": [{"role": "user", "content": prompt}],
                       "tools": [{"type": TOOL_TYPE, "name": "web_search", "max_uses": max_uses}]}).encode("utf-8")


def parse_response(body: bytes, allowed_models: tuple = MODELS) -> ProviderAnswer:
    try:
        obj = strict_json(body)
        r = _Response.model_validate(obj)
        types, texts, results = [], [], []
        for raw in r.content:
            t = _Block.model_validate(raw).type
            types.append(t)
            if t == "text":
                texts.append(_Text.model_validate(raw))
            elif t == "web_search_tool_result":
                results.append(_ToolResult.model_validate(raw))
        searches = None
        if r.usage is not None and r.usage.server_tool_use is not None:
            n = r.usage.server_tool_use.get("web_search_requests")
            if n is not None and (type(n) is not int or not 0 <= n <= 1000):
                raise ValueError("web_search_requests")
            searches = n
    except (UnicodeDecodeError, ValueError, RecursionError, ValidationError, TypeError):
        raise EngineError("ENGINE_PROTOCOL_ERROR") from None
    version = next((v for v in (model_matches(r.model, m) for m in allowed_models) if v is not None), None)
    if version is None:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED")
    usage = None if r.usage is None else {"input_tokens": r.usage.input_tokens, "output_tokens": r.usage.output_tokens,
                                          "total_tokens": r.usage.input_tokens + r.usage.output_tokens,
                                          "total_cost_usd": None, "web_search_requests": searches}
    reason = r.stop_reason or ""
    if reason == "refusal":
        return ProviderAnswer("REFUSAL", "", [], r.model, version, cite_from_text=False, usage=usage,
                              finish_reason=reason)
    if reason not in COMPLETE:
        raise EngineError("ENGINE_PROTOCOL_ERROR",
                          "RESPONSE_" + reason.upper() if _REASON.fullmatch(reason) else "RESPONSE_UNKNOWN")
    errors = [x.content.error_code for x in results if isinstance(x.content, _ToolError)]
    if "too_many_requests" in errors:
        raise EngineError("ENGINE_RATE_LIMITED", "SEARCH_RATE_LIMITED")
    text = "".join(b.text for b in texts)
    if not text.strip():
        raise EngineError("ENGINE_PROTOCOL_ERROR", "NO_ANSWER_TEXT")
    if len(text) > MAX_ANSWER_CHARS:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
    if errors and not any(isinstance(x.content, list) and x.content for x in results):
        # every search failed: the answer did not come from search, so it says nothing about citations
        raise EngineError("ENGINE_UNAVAILABLE", "SEARCH_TOOL_ERROR")
    returned = {x.url for res in results if isinstance(res.content, list) for x in res.content}
    cited, unverified = [], 0
    for b in texts:
        for c in b.citations or ():
            if c.type != "web_search_result_location" or not c.url:
                continue
            if c.url not in returned:
                unverified += 1
                continue
            cited.append(c.url)
    ans = ProviderAnswer("ANSWER", text, dedupe(cited), r.model, version, cite_from_text=False, usage=usage,
                         answer_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                         finish_reason=reason)
    ans.citation_basis = "text_citations_in_search_results"
    ans.unverified_citations = unverified
    ans.unmodelled_keys = drift_names(list(obj.keys()) + ["content." + t for t in types],
                                      set(_Response.model_fields) | _KNOWN_UNREAD
                                      | {"content.text", "content.server_tool_use", "content.web_search_tool_result"})
    return ans


_GATE = _DnsGate(2)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GATE._after_fork_in_child)


class AnthropicEngine(HttpAnswerEngine):
    name = ENGINE
    BASE_URL = BASE_URL
    ALLOWED_HOSTS = frozenset({HOST})

    def __init__(self, key: ApiKey, model: Optional[str] = None, timeout_s: float = 60.0, attempts: int = 2,
                 backoff_s: tuple = (0.5, 1.5), transport: Optional[httpx.BaseTransport] = None,
                 network_backend=None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, resolve: Callable[[str, int], list] = _system_resolve,
                 dns_gate: Optional[_DnsGate] = None, rand: Callable[[], float] = random.random,
                 public: Optional[Callable[[str], bool]] = None, wall=None, max_searches: int = 3):
        model = DEFAULT_MODEL if model is None else model
        if model not in MODELS:
            raise ValueError("model not in the allowlist")
        if not 1 <= max_searches <= 10:
            raise ValueError("max_searches must be 1..10")
        self.models, self.model, self.max_searches = MODELS, model, max_searches
        self._init_http(key, timeout_s, attempts, backoff_s, transport, network_backend, monotonic, sleep,
                        _Resolver(resolve, dns_gate if dns_gate is not None else _GATE), rand, public, wall)

    def __repr__(self) -> str:
        return f"AnthropicEngine(model={self.model!r}, timeout_s={self.timeout_s}, attempts={self.attempts})"

    def describe(self) -> dict:
        return {"state": "connected", "base_url": BASE_URL, "path": PATH, "model": self.model,
                "models_allowed": list(MODELS), "tool": TOOL_TYPE, "max_searches": self.max_searches,
                "anthropic_version": API_VERSION, "timeout_s": self.timeout_s, "attempts": self.attempts,
                "key_source": self._key.source, "max_response_bytes": self.MAX_RESPONSE_BYTES, "docs": DOCS}

    def _path(self, model: str) -> str:
        return PATH

    def _auth_headers(self) -> dict:
        return {"x-api-key": self._key.reveal(), "anthropic-version": API_VERSION}

    def _body(self, model: str, prompt: str) -> bytes:
        return request_body(model, prompt, self.max_searches)

    def _parse(self, raw: bytes, model: str) -> ProviderAnswer:
        return parse_response(raw, (model,))
