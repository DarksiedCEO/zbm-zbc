"""
The Google answer engine (ADR 0046): the Gemini API's ``generateContent`` with Grounding with Google Search, behind
the shared multi-engine adapter (answer_adapter.py). NOT_CONNECTED unless ``SEO_GOOGLE_ENABLED=1`` AND a key is
configured; nothing here has been called against the real API (fixtures only, built from the documented shapes).

Documentation read on Nov 8 2026 (a web-fetch tool's reading of the pages, not the raw pages; nothing was called):
  https://ai.google.dev/gemini-api/docs/generate-content/google-search  ("Grounding with Google Search")
  - ``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`` (the model in the path),
    header ``x-goog-api-key``, body ``contents[].parts[].text`` plus ``"tools": [{"google_search": {}}]``; the page's
    example model is ``gemini-3.8-flash``;
  - the answer is ``candidates[].content.parts[].text``; ``candidates[].groundingMetadata`` holds
    ``webSearchQueries``, ``searchEntryPoint`` (rendered HTML, never read here), ``groundingChunks[].web.uri`` /
    ``.title`` (the sources) and ``groundingSupports[]`` (``segment`` + ``groundingChunkIndices``);
  - the example ``uri`` values begin ``https://vertexaisearch.cloud.google.com...`` (truncated on the page; the page
    does not say they are redirects): Callum therefore treats a grounding URI on that host as a REDIRECT whose target
    is unknown until resolved, never as the cited page (agents/callum.py);
  - billing (Gemini 3 and newer): each search query the model runs is billed, $14 per 1,000 after a shared free tier
    of 5,000 queries a month; the response carries no cost, so a request is charged at the configured reserve.
  https://ai.google.dev/api/generate-content  (the API reference)
  - ``GenerateContentResponse``: ``candidates[]``, ``promptFeedback`` (``blockReason``: BLOCK_REASON_UNSPECIFIED,
    SAFETY, OTHER, BLOCKLIST, PROHIBITED_CONTENT, IMAGE_SAFETY), ``usageMetadata`` (``promptTokenCount``,
    ``candidatesTokenCount``, ``totalTokenCount``, ``toolUsePromptTokenCount``, ``thoughtsTokenCount``, ...),
    ``modelVersion``, ``responseId``; ``Candidate``: ``content``, ``finishReason``, ``groundingMetadata``, ``index``,
    ...; ``Part``: ``text``, ``thought``; ``GroundingChunk.web``: ``uri``, ``title``.
  The FinishReason values were cut from the reading; the parser accepts ``STOP`` as a complete answer, the documented
  block reasons and SAFETY / RECITATION / BLOCKLIST / PROHIBITED_CONTENT / SPII / IMAGE_SAFETY as a REFUSAL, and
  every other value as ENGINE_PROTOCOL_ERROR ``RESPONSE_<REASON>`` (never counted).

Methodology (Callum): cited = a grounding chunk's ``web.uri`` (cited = returned by grounding, in chunk order). The
chunk's ``title`` (the page shows the source DOMAIN there) is kept as an unverified hint for the drill-down, never
used to credit the client.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from typing import Annotated, Callable, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from answer_adapter import (MAX_ANSWER_CHARS, MAX_SOURCES, SYSTEM_PROMPT, ApiKey, EngineError, HttpAnswerEngine,
                            drift_names, strict_json)
from finance_client import _DnsGate, _Resolver, _system_resolve
from ports import ProviderAnswer

ENGINE = "google"
BASE_URL = "https://generativelanguage.googleapis.com"
HOST = "generativelanguage.googleapis.com"
MODELS = ("gemini-3.8-flash",)                  # closed; the grounding page's example model
DEFAULT_MODEL = "gemini-3.8-flash"
DOCS = {"url": "https://ai.google.dev/gemini-api/docs/generate-content/google-search", "read": "2026-11-08",
        "reference": "https://ai.google.dev/api/generate-content",
        "version": "Gemini API v1beta, models/{model}:generateContent, tool google_search"}
COMPLETE = ("STOP",)
REFUSED = ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY")
MAX_CANDIDATES = 8
MAX_PARTS = 64
_REASON = re.compile(r"[A-Z_]{1,40}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


Count = Annotated[StrictInt, Field(ge=0, le=100_000_000)]


class _Part(_Strict):
    text: Optional[Annotated[StrictStr, Field(max_length=MAX_ANSWER_CHARS)]] = None
    thought: Optional[StrictBool] = None


class _Content(_Strict):
    parts: Annotated[list[_Part], Field(max_length=MAX_PARTS)] = []
    role: Optional[Annotated[StrictStr, Field(max_length=20)]] = None


class _Web(_Strict):
    uri: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    title: Optional[Annotated[StrictStr, Field(max_length=2000)]] = None


class _Chunk(_Strict):
    web: Optional[_Web] = None                        # image / retrievedContext / maps chunks are not web citations


class _Grounding(_Strict):
    groundingChunks: Optional[Annotated[list[_Chunk], Field(max_length=MAX_SOURCES)]] = None
    webSearchQueries: Optional[Annotated[list[Annotated[StrictStr, Field(max_length=2000)]],
                                         Field(max_length=50)]] = None


class _Candidate(_Strict):
    content: Optional[_Content] = None
    finishReason: Optional[Annotated[StrictStr, Field(max_length=40)]] = None
    groundingMetadata: Optional[_Grounding] = None
    index: Optional[Annotated[StrictInt, Field(ge=0, le=16)]] = None


class _Feedback(_Strict):
    blockReason: Optional[Annotated[StrictStr, Field(max_length=40)]] = None


class _Usage(_Strict):
    promptTokenCount: Optional[Count] = None
    candidatesTokenCount: Optional[Count] = None
    totalTokenCount: Optional[Count] = None
    toolUsePromptTokenCount: Optional[Count] = None
    thoughtsTokenCount: Optional[Count] = None


class _Response(_Strict):
    candidates: Optional[Annotated[list[_Candidate], Field(max_length=MAX_CANDIDATES)]] = None
    promptFeedback: Optional[_Feedback] = None
    usageMetadata: Optional[_Usage] = None
    modelVersion: Optional[Annotated[StrictStr, Field(min_length=1, max_length=100)]] = None
    responseId: Optional[Annotated[StrictStr, Field(max_length=200)]] = None


_KNOWN_UNREAD = frozenset({"modelStatus"})


# review round 1 L2: the ``modelVersion`` values accepted for each allow-listed model, EXACTLY. Empty beyond the
# model's own name: no alias or dated suffix has been seen (nothing has been called), so ``gemini-3.8-flash-<x>`` is
# refused until a first live call shows what the API reports and an alias is added here by hand.
MODEL_ALIASES: dict = {"gemini-3.8-flash": frozenset({"gemini-3.8-flash"})}


def model_matches(reported: Optional[str], asked: str) -> Optional[str]:
    """The version a reported ``modelVersion`` stands for when it is exactly the asked model or one of its
    allow-listed aliases (``MODEL_ALIASES``), else None. FAIL CLOSED (review round 1 L2): a missing ``modelVersion``
    (documented as a plain string, required nowhere) is None too — the answer cannot be attributed to a model, so it
    is refused (ENGINE_PROTOCOL_ERROR ``MODEL_UNVERIFIED``) rather than counted under the asked model's name."""
    if reported is None:
        return None
    if reported in MODEL_ALIASES.get(asked, frozenset({asked})):
        return "?" if reported == asked else reported
    return None


def request_body(model: str, prompt: str) -> bytes:
    """Exactly: the fixed instruction (``systemInstruction``), the prompt as one user part, and the ``google_search``
    tool. No tenant, brand, domain or user identifier."""
    return json.dumps({"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                       "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "tools": [{"google_search": {}}]}).encode("utf-8")


# ADR 0049: generateContent's ``contents`` "for multi-turn queries like chat ... contains the conversation history
# and the latest request" (the API reference's Chat example alternates role user / model)
MULTI_TURN_DOCS = {"url": "https://ai.google.dev/api/generate-content", "read": "2026-11-08",
                   "shape": "contents: [{role: user, parts}, {role: model, parts}, ...]"}


def request_body_turns(model: str, history: list, prompt: str) -> bytes:
    """``request_body`` with the earlier turns (assistant = role ``model``) before the new user content."""
    contents = [{"role": "user" if h["role"] == "user" else "model", "parts": [{"text": h["text"]}]}
                for h in history] + [{"role": "user", "parts": [{"text": prompt}]}]
    return json.dumps({"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]}, "contents": contents,
                       "tools": [{"google_search": {}}]}).encode("utf-8")


def parse_response(body: bytes, allowed_models: tuple = MODELS) -> ProviderAnswer:
    try:
        obj = strict_json(body)
        r = _Response.model_validate(obj)
    except (UnicodeDecodeError, ValueError, RecursionError, ValidationError, TypeError):
        raise EngineError("ENGINE_PROTOCOL_ERROR") from None
    if r.modelVersion is None:                     # fail closed (L2): an answer no model can be attributed to
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_UNVERIFIED")
    hit = next(((m, v) for m, v in ((m, model_matches(r.modelVersion, m)) for m in allowed_models) if v is not None),
               None)
    if hit is None:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED")
    model, version = hit
    u = r.usageMetadata
    first = min(r.candidates or (), key=lambda c: 0 if c.index is None else c.index, default=None)
    g = first.groundingMetadata if first is not None else None
    usage = {"input_tokens": None if u is None else u.promptTokenCount,
             "output_tokens": None if u is None else u.candidatesTokenCount,
             "total_tokens": None if u is None else u.totalTokenCount, "total_cost_usd": None,
             "search_queries": len(g.webSearchQueries or ()) if g is not None else 0}
    block = r.promptFeedback.blockReason if r.promptFeedback is not None else None
    if first is None:
        if block:
            return ProviderAnswer("REFUSAL", "", [], model, version, cite_from_text=False, usage=usage,
                                  finish_reason="BLOCKED")
        raise EngineError("ENGINE_PROTOCOL_ERROR", "NO_CANDIDATE")
    reason = first.finishReason or ""
    if reason in REFUSED:
        return ProviderAnswer("REFUSAL", "", [], model, version, cite_from_text=False, usage=usage,
                              finish_reason=reason)
    if reason not in COMPLETE:
        raise EngineError("ENGINE_PROTOCOL_ERROR",
                          "RESPONSE_" + reason if reason and _REASON.fullmatch(reason) and len(reason) <= 31
                          else "RESPONSE_UNKNOWN")
    text = "".join(p.text for p in (first.content.parts if first.content else ()) if p.text and not p.thought)
    if not text.strip():
        raise EngineError("ENGINE_PROTOCOL_ERROR", "NO_ANSWER_TEXT")
    if len(text) > MAX_ANSWER_CHARS:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
    cites, hints = [], {}
    for c in (g.groundingChunks or ()) if g is not None else ():
        if c.web is not None and c.web.uri not in cites:
            cites.append(c.web.uri)
            if c.web.title:
                hints[c.web.uri] = c.web.title[:253]
    ans = ProviderAnswer("ANSWER", text, cites[:MAX_SOURCES], model, version, cite_from_text=False, usage=usage,
                         answer_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                         finish_reason=reason)
    ans.citation_basis = "grounding_chunks"
    ans.citation_hints = hints                      # the chunk titles (a domain, per the docs): unverified hints only
    ans.unmodelled_keys = drift_names(obj.keys(), set(_Response.model_fields) | _KNOWN_UNREAD)
    return ans


_GATE = _DnsGate(2)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GATE._after_fork_in_child)


class GoogleEngine(HttpAnswerEngine):
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
        return f"GoogleEngine(model={self.model!r}, timeout_s={self.timeout_s}, attempts={self.attempts})"

    def describe(self) -> dict:
        return {"state": "connected", "base_url": BASE_URL, "path": self._path(self.model), "model": self.model,
                "models_allowed": list(MODELS), "tool": "google_search", "timeout_s": self.timeout_s,
                "attempts": self.attempts, "key_source": self._key.source,
                "max_response_bytes": self.MAX_RESPONSE_BYTES, "docs": DOCS}

    def _path(self, model: str) -> str:
        if model not in MODELS:                       # the model is in the path: only an allow-listed name
            raise EngineError("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED")
        return f"/v1beta/models/{model}:generateContent"

    def _auth_headers(self) -> dict:
        return {"x-goog-api-key": self._key.reveal()}

    def _body(self, model: str, prompt: str) -> bytes:
        return request_body(model, prompt)

    MULTI_TURN = True
    MULTI_TURN_DOCS = MULTI_TURN_DOCS

    def _body_turns(self, model: str, history: list, prompt: str) -> bytes:
        return request_body_turns(model, history, prompt)

    def _parse(self, raw: bytes, model: str) -> ProviderAnswer:
        return parse_response(raw, (model,))
