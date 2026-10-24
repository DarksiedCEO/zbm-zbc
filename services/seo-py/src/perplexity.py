"""
The Perplexity answer-engine adapter (ADR 0017 Wave 4; flag 1: Perplexity included, founder-chosen Oct 2026).

One request kind only: ``POST https://api.perplexity.ai/v1/sonar`` (the Sonar chat-completions request, verified
against docs.perplexity.ai on Oct 24 2026; the facts and their URLs are in ADR 0017 W4-1). Nothing else is ever
called: no other host, path or method, no redirects followed, no proxy from the environment.

Rules:
  - fixed base URL and a CLOSED model allowlist (``MODELS``; default ``sonar``, the general Sonar model);
  - one HARD overall deadline per question, over DNS, connect, TLS, every read and write and the body loop
    (``_GuardedDeadlineBackend``: the Finance client's deadline backend pattern, plus the kill-switch guard before
    every socket operation and again after the response, every read waiting at most ``READ_SLICE_S`` between guard
    checks, plus an address check: api.perplexity.ai must resolve to public addresses only);
  - a bounded response (``MAX_RESPONSE_BYTES``, identity coding only, JSON only, duplicate keys refused);
  - bounded retries ONLY on 429, 5xx and transport errors; ``Retry-After`` (whole seconds) honoured when it fits in
    the time left, otherwise the answer is ENGINE_RATE_LIMITED at once (never a sleep past the deadline);
  - STRICT parsing of what we rely on (pydantic, strict types): the answer text, the model, the citation URLs and
    the usage/cost numbers must have the documented shape, or the answer is ENGINE_PROTOCOL_ERROR, never a guessed
    answer. Fields we do not read (documented optional ones such as ``images``, ``related_questions``, a choice's
    ``delta``, a search result's ``id``, ``usage.tool_calls_details``) are ignored and dropped, never stored: the API
    adds fields over time and a known-but-irrelevant field must not stop the run (AEGIS H2);
  - citations come from ``search_results[].url`` first (the field Perplexity says to use since May 2025, when it
    announced ``citations`` removed) and from ``citations`` when present (the Sonar reference examples still show it);
  - usage and cost (``usage.cost.total_cost``) captured as Decimal strings for the spend budgets;
  - the API key is held in ``ApiKey`` (its repr is masked; ``reveal()`` is called in exactly one place, the request
    header) and never reaches a log line, an exception, a report, the log or the ledger;
  - the answer is DATA: its text is returned to the probe framework to be scanned, hashed and dropped; citations
    come only from the structured ``search_results`` / ``citations`` fields, never from URLs in the text.

Closed reason codes (``CODES``): ENGINE_AUTH_REFUSED, ENGINE_RATE_LIMITED, ENGINE_UNAVAILABLE, ENGINE_PROTOCOL_ERROR,
ENGINE_BUDGET_EXHAUSTED, ENGINE_KILLED, ENGINE_NOT_CONNECTED.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Callable, Literal, Optional

import httpcore
import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, ValidationError

from finance_client import _DnsGate, _Resolver, _system_resolve
from ports import ProviderAnswer

ENGINE = "perplexity"
BASE_URL = "https://api.perplexity.ai"
PATH = "/v1/sonar"
HOST = "api.perplexity.ai"
MODELS = ("sonar", "sonar-pro")             # closed: the general model and the pro model; no deep research
DEFAULT_MODEL = "sonar"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_ANSWER_CHARS = 100_000
MAX_SOURCES = 100
MAX_RETRY_AFTER_S = 30
# one socket read waits at most this long (within the deadline) before the kill-switch guard is checked again, so a
# switch engaged while the API is still composing its answer stops the request within seconds (AEGIS L1)
READ_SLICE_S = 2.0

CODES = frozenset({"ENGINE_AUTH_REFUSED", "ENGINE_RATE_LIMITED", "ENGINE_UNAVAILABLE", "ENGINE_PROTOCOL_ERROR",
                   "ENGINE_BUDGET_EXHAUSTED", "ENGINE_KILLED", "ENGINE_NOT_CONNECTED"})
# a run stops asking this engine after one of these (asking again cannot help, or must not happen). A run also stops
# after PROTOCOL_ERROR_STOP_AFTER ENGINE_PROTOCOL_ERRORs with no answer in between, whatever other errors come between
# them (agents/probes.py): a response shape we cannot
# read will not change on the next request, and every request costs money.
PROTOCOL_ERROR_STOP_AFTER = 2
STOP_CODES = frozenset({"ENGINE_AUTH_REFUSED", "ENGINE_RATE_LIMITED", "ENGINE_BUDGET_EXHAUSTED", "ENGINE_KILLED",
                        "ENGINE_NOT_CONNECTED"})
# The one fixed instruction sent with every question: no tenant, no person, no brand, no identifier of any kind.
SYSTEM_PROMPT = "Answer the question concisely and cite your sources."
_KEY_RX = re.compile(r"[\x21-\x7e]{20,512}")
_MICRO = Decimal("0.000001")


class EngineError(Exception):
    """An answer-engine outcome that is not an answer. ``code`` is one of CODES; nothing else is carried (no body,
    no header, no key, no URL)."""

    def __init__(self, code: str, detail: Optional[str] = None):
        if code not in CODES:
            raise AssertionError(f"not an engine code: {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is None or re.fullmatch(r"[A-Z_]{1,40}", detail) else None

    def __repr__(self) -> str:
        return f"EngineError({self.code!r})"


class ApiKey:
    """The Perplexity API key. ``repr``/``str`` never show it, it cannot be pickled or compared by value from the
    outside, and ``reveal()`` is the one way to read it (used only to build the Authorization header)."""

    __slots__ = ("_v", "source")

    def __init__(self, value: str, source: str):
        if not isinstance(value, str) or not _KEY_RX.fullmatch(value):
            raise ValueError("an API key is 20..512 printable ASCII characters without spaces")
        if source not in ("file", "env"):
            raise ValueError("source")
        self._v = value
        self.source = source

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return f"ApiKey(***, source={self.source})"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("an API key is never pickled")


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


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def money(x) -> Optional[str]:
    """A provider cost (a JSON number, in USD) as a Decimal string to the micro-dollar, or None."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and not math.isfinite(x):
            return None
        d = Decimal(str(x)).quantize(_MICRO)
    except (InvalidOperation, ValueError):
        return None
    return str(d) if d >= 0 else None


def parse_response(body: bytes, allowed_models: tuple = MODELS) -> ProviderAnswer:
    """The documented Sonar response -> a ProviderAnswer. A field we rely on with any other shape is
    ENGINE_PROTOCOL_ERROR. Sources: ``search_results[].url`` in order (primary), then any ``citations`` URL not
    already listed."""
    try:
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates,
                         parse_constant=lambda c: (_ for _ in ()).throw(ValueError("NaN")))
        r = _Response.model_validate(obj)
    except (UnicodeDecodeError, ValueError, RecursionError, ValidationError, TypeError):
        raise EngineError("ENGINE_PROTOCOL_ERROR") from None
    if r.model not in allowed_models:
        raise EngineError("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED")
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
    return ProviderAnswer("ANSWER", text, sources[:MAX_SOURCES], r.model, None, cite_from_text=False,
                          usage=usage, answer_sha256=hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
                          finish_reason=first.finish_reason)


def cost_of(ans: ProviderAnswer) -> Optional[str]:
    return (ans.usage or {}).get("total_cost_usd")


# ---------------------------------------------------------------------------------------------- the network layer

class _Stream(httpcore.NetworkStream):
    def __init__(self, inner, left: Callable, guard: Callable):
        self._s, self._left, self._guard = inner, left, guard

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        while True:
            self._guard()
            left = self._left(timeout, httpcore.ReadTimeout)     # raises once the deadline has passed
            part = min(left, READ_SLICE_S)
            try:
                return self._s.read(max_bytes, part)
            except httpcore.ReadTimeout:
                if part >= left:
                    raise                                         # the whole time left is used up
                # a slice ran out with time left: check the switch, then wait again

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self._guard()
        self._s.write(buffer, self._left(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._s.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._guard()
        return _Stream(self._s.start_tls(ssl_context, server_hostname, self._left(timeout, httpcore.ConnectTimeout)),
                       self._left, self._guard)

    def get_extra_info(self, info: str):
        return self._s.get_extra_info(info)


class _GuardedDeadlineBackend(httpcore.NetworkBackend):
    """Every socket operation bounded by the time left until the question's deadline, the kill-switch guard checked
    before each one, the host resolved INSIDE the deadline (on the shared resolver gate pattern) and every resolved
    address required to be public (api.perplexity.ai never answers from a private address; one that does is
    refused, so a poisoned resolver cannot point this client inside the network)."""

    def __init__(self, monotonic: Callable[[], float], end: float, guard: Callable, resolver: _Resolver,
                 inner=None, public: Optional[Callable[[str], bool]] = None):
        self._b = inner or httpcore.SyncBackend()
        self._mono, self._end, self._guard, self._resolver = monotonic, end, guard, resolver
        if public is None:
            from primitives.fetch import public_address as public
        self._public = public

    def left(self, timeout: Optional[float], exc: type) -> float:
        left = self._end - self._mono()
        if left <= 0:
            raise exc("the question's overall deadline has passed")
        return left if timeout is None else min(timeout, left)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self._guard()
        addrs = self._resolver(host, port, self.left(timeout, httpcore.ConnectTimeout))
        if not addrs or any(not self._public(a) for a in addrs):
            raise httpcore.ConnectError("the engine host resolved to a non-public address; refused")
        last: Optional[Exception] = None
        for ip in addrs:
            self._guard()
            left = self.left(timeout, httpcore.ConnectTimeout)
            try:
                inner = self._b.connect_tcp(ip, port, left, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
                continue
            return _Stream(inner, self.left, self._guard)
        raise last if last is not None else httpcore.ConnectError("the engine host does not resolve")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never used")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _Killed(Exception):
    """Raised by the guard wrapper inside a socket operation; carries the switch's code out of httpx."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_GATE = _DnsGate(2)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GATE._after_fork_in_child)


class _Retry(Exception):
    def __init__(self, code: str, retry_after: Optional[float] = None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


def _retry_after(value: Optional[str]) -> Optional[float]:
    """Whole seconds only (the documented form); anything else is ignored (the backoff applies)."""
    if value is None or not re.fullmatch(r"\s*[0-9]{1,6}\s*", value):
        return None
    return float(int(value))


class PerplexityEngine:
    """The answer-engine port for Perplexity. ``ask(prompt, guard=..., meter=...)`` returns a ProviderAnswer or raises
    EngineError (and nothing else for any network or protocol outcome)."""

    connected = True
    metered = True
    accepts_guard = True
    name = ENGINE

    def __init__(self, key: ApiKey, model: str = DEFAULT_MODEL, timeout_s: float = 30.0, attempts: int = 3,
                 backoff_s: tuple = (0.5, 1.5), transport: Optional[httpx.BaseTransport] = None,
                 network_backend=None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, resolve: Callable[[str, int], list] = _system_resolve,
                 dns_gate: Optional[_DnsGate] = None, rand: Callable[[], float] = random.random,
                 public: Optional[Callable[[str], bool]] = None):
        if not isinstance(key, ApiKey):
            raise TypeError("the key must be an ApiKey")
        if model not in MODELS:
            raise ValueError("model not in the allowlist")
        if not 1 <= attempts <= 3:
            raise ValueError("attempts must be 1..3")
        if not 1 <= timeout_s <= 120:
            raise ValueError("timeout must be 1..120 s")
        self._key = key
        self.model = model
        self.timeout_s = float(timeout_s)
        self.attempts = attempts
        self.backoff_s = tuple(backoff_s)
        self._transport = transport
        self._inner = network_backend
        self._mono, self._sleep, self._rand = monotonic, sleep, rand
        self._resolver = _Resolver(resolve, dns_gate if dns_gate is not None else _GATE)
        self._public = public

    def __repr__(self) -> str:
        return f"PerplexityEngine(model={self.model!r}, timeout_s={self.timeout_s}, attempts={self.attempts})"

    def describe(self) -> dict:
        return {"state": "connected", "base_url": BASE_URL, "path": PATH, "model": self.model,
                "models_allowed": list(MODELS), "timeout_s": self.timeout_s, "attempts": self.attempts,
                "key_source": self._key.source, "max_response_bytes": MAX_RESPONSE_BYTES}

    # ------------------------------------------------------------------ public

    def ask(self, prompt: str, model: Optional[str] = None, guard: Optional[Callable] = None,
            meter=None) -> ProviderAnswer:
        """One question, at most ``attempts`` requests, all within one hard deadline. ``guard`` is the kill-switch
        check (raises to stop); ``meter`` charges every request against the run's budget reservation
        (``before()`` raises ENGINE_BUDGET_EXHAUSTED; ``after(cost)``)."""
        model = model or self.model
        if model not in MODELS or not isinstance(prompt, str) or not 1 <= len(prompt) <= 2000:
            raise EngineError("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED")
        body = json.dumps({"model": model, "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                         {"role": "user", "content": prompt}],
                           "stream": False}).encode("utf-8")
        end = self._mono() + self.timeout_s
        last = "ENGINE_UNAVAILABLE"
        for attempt in range(self.attempts):
            self._check(guard)
            if end - self._mono() <= 0:
                break
            if meter is not None:
                meter.before()                       # raises EngineError(ENGINE_BUDGET_EXHAUSTED)
            charged = False
            try:
                status, headers, raw = self._post(body, end, guard)
                self._check(guard)                   # a switch engaged during the last read: drop the answer
                if status == 200:
                    ans = parse_response(raw, (model,))
                    if meter is not None:
                        meter.after(cost_of(ans))
                        charged = True
                    return ans
                if status in (401, 402, 403):
                    raise EngineError("ENGINE_AUTH_REFUSED")
                if status == 429:
                    raise _Retry("ENGINE_RATE_LIMITED", _retry_after(headers.get("retry-after")))
                if 500 <= status <= 599:
                    raise _Retry("ENGINE_UNAVAILABLE", _retry_after(headers.get("retry-after")))
                raise EngineError("ENGINE_PROTOCOL_ERROR")
            except _Retry as r:
                last = r.code
                pause = r.retry_after
            finally:
                if meter is not None and not charged:
                    meter.after(None)                 # sent, cost unknown: the reservation stays charged
            if attempt + 1 >= self.attempts:
                break
            if pause is None:
                base = self.backoff_s[min(attempt, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
                pause = base * (1.0 + 0.25 * self._rand())
            elif pause > MAX_RETRY_AFTER_S:
                break
            if self._mono() + pause >= end:
                break                                 # never sleep past the deadline: give the answer now
            self._sleep(pause)
        raise EngineError(last)

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _check(guard: Optional[Callable]) -> None:
        if guard is None:
            return
        try:
            guard(capability="ai_probe", provider=ENGINE)
        except Exception as exc:   # noqa: BLE001 - the run's guard raises Killed; it becomes ENGINE_KILLED
            code = getattr(exc, "code", None)
            raise EngineError("ENGINE_KILLED", code if isinstance(code, str) else None) from None

    def _post(self, body: bytes, end: float, guard: Optional[Callable]) -> tuple[int, dict, bytes]:
        def g() -> None:
            if guard is None:
                return
            try:
                guard(capability="ai_probe", provider=ENGINE)
            except Exception as exc:  # noqa: BLE001
                raise _Killed(getattr(exc, "code", "KILLED")) from None

        transport = self._transport
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False, http2=False, retries=0)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise EngineError("ENGINE_NOT_CONNECTED", "NO_DEADLINE_BACKEND")    # never ask without a deadline
            pool._network_backend = _GuardedDeadlineBackend(self._mono, end, g, self._resolver, inner=self._inner,
                                                            public=self._public)
        headers = {"Authorization": "Bearer " + self._key.reveal(), "Content-Type": "application/json",
                   "Accept": "application/json", "Accept-Encoding": "identity",
                   "User-Agent": "ZBM-SEO-Answer-Probe/0.1"}
        left = end - self._mono()
        if left <= 0:
            raise _Retry("ENGINE_UNAVAILABLE")
        try:
            with httpx.Client(timeout=httpx.Timeout(left), transport=transport, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("POST", BASE_URL + PATH, headers=headers, content=body) as resp:
                    status = resp.status_code
                    kept = {"retry-after": resp.headers.get("retry-after")}
                    if status != 200:
                        return status, kept, b""          # an error body is never read, logged or parsed
                    enc = resp.headers.get("content-encoding", "identity").strip().lower()
                    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if enc not in ("identity", "") or ctype != "application/json":
                        raise EngineError("ENGINE_PROTOCOL_ERROR")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                        raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
                    chunks, size = [], 0
                    for chunk in resp.iter_raw():
                        if self._mono() >= end:
                            raise _Retry("ENGINE_UNAVAILABLE")          # the deadline, mid-body
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
                        chunks.append(chunk)
                    return status, kept, b"".join(chunks)
        except (EngineError, _Retry):
            raise
        except _Killed as k:
            raise EngineError("ENGINE_KILLED", k.code if isinstance(k.code, str) else None) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise _Retry("ENGINE_UNAVAILABLE") from None
        except Exception as exc:  # noqa: BLE001 - nothing from the network ever escapes as anything but a code
            cause = exc
            while cause is not None:
                if isinstance(cause, _Killed):
                    raise EngineError("ENGINE_KILLED", cause.code if isinstance(cause.code, str) else None) \
                        from None
                cause = cause.__cause__ or cause.__context__
            raise EngineError("ENGINE_PROTOCOL_ERROR") from None


class NotConnectedPerplexity:
    """No key configured: every question is ENGINE_NOT_CONNECTED (no request is made, nothing is invented)."""

    connected = False
    metered = False
    name = ENGINE

    def ask(self, prompt: str, model: Optional[str] = None, **_kw) -> ProviderAnswer:
        from ports import NOT_CONNECTED
        return ProviderAnswer(NOT_CONNECTED)

    def describe(self) -> dict:
        return {"state": "NOT_CONNECTED", "base_url": BASE_URL, "path": PATH, "model": None,
                "models_allowed": list(MODELS),
                "why": "no API key: set SEO_PERPLEXITY_API_KEY_FILE (or SEO_PERPLEXITY_API_KEY on Railway)"}
