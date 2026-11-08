"""
The multi-engine answer adapter (ADR 0046): ONE provider-agnostic interface for asking an AI answer engine a prompt
and getting back the answer text, the URLs it cited, and provider / model / version / time metadata. Shared plumbing,
not an agent: it decides nothing about what an answer means (that is Callum's job, ``agents/callum.py``).

The interface (``AnswerEngine``) is what agents/probes.py and agents/callum.py already call:

  ask(prompt, guard=..., meter=...) -> ports.ProviderAnswer      or raises EngineError (one of CODES), nothing else
  describe() -> dict                                             /status: state, model, limits, the key's SOURCE
  connected, metered, accepts_guard, name, model, attempts       attributes

A ``ProviderAnswer`` carries ``status`` (ANSWER / REFUSAL / ERROR / NOT_CONNECTED), ``text``, ``citations`` (the URLs
the ENGINE attributed to the answer, from its structured fields only, in the engine's own order), ``model``,
``model_version``, ``usage`` (tokens and a Decimal-string cost when the provider reports one), ``answer_sha256``,
``provider`` (the engine's name) and ``asked_at`` (ISO time the answer was received).

Switching provider is configuration only: every engine is one class behind this interface, built from settings by
``ports.answer_engines``; nothing outside the adapter knows a provider's wire format. Implementations:
perplexity.py (Agent API; the Sonar rollback), openai_engine.py (Responses API + web_search), google_engine.py
(Gemini generateContent + Google Search grounding), anthropic_engine.py (Messages API + the web search tool). Each
records the documentation URL and version it was built from, and each is NOT_CONNECTED until enabled with a key.

What every HTTP engine gets from ``HttpAnswerEngine`` (moved here unchanged from the Perplexity adapter, ADR 0017
W4-2, so every engine has the controls Perplexity was certified with):
  - one fixed base URL per engine and a closed ``ALLOWED_HOSTS`` egress allow-list, checked before every request and
    again by the network backend before every connect (SSRF / egress); https and port 443 only; no redirects
    followed; no proxy taken from the environment;
  - one HARD overall deadline per question over DNS, connect, TLS, every read and write and the body loop
    (``_GuardedDeadlineBackend``: the kill-switch guard before every socket operation, every read waiting at most
    ``READ_SLICE_S`` between guard checks, every resolved address required to be public);
  - a bounded response (``MAX_RESPONSE_BYTES``), identity coding and ``application/json`` only, duplicate keys and
    NaN refused;
  - bounded retries ONLY on 429, 5xx and transport errors; ``Retry-After`` (whole seconds) honoured when it fits in
    the time left; an error body is never read;
  - the meter (svc_engines.py): ``meter.before()`` before every request (raises ENGINE_BUDGET_EXHAUSTED), and
    ``meter.after(cost)`` after it, a request of unknown cost charged at the reserve;
  - the key in ``ApiKey`` (masked repr, never pickled, ``reveal()`` only for the auth header).
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpcore
import httpx

from ports import NOT_CONNECTED, ProviderAnswer

MAX_RESPONSE_BYTES = 512 * 1024
MAX_RETRY_AFTER_S = 30
# one socket read waits at most this long (within the deadline) before the kill-switch guard is checked again, so a
# switch engaged while the API is still composing its answer stops the request within seconds (AEGIS L1)
READ_SLICE_S = 2.0
MAX_UNMODELLED_KEYS = 20
MAX_SOURCES = 100
MAX_ANSWER_CHARS = 100_000

CODES = frozenset({"ENGINE_AUTH_REFUSED", "ENGINE_RATE_LIMITED", "ENGINE_UNAVAILABLE", "ENGINE_PROTOCOL_ERROR",
                   "ENGINE_BUDGET_EXHAUSTED", "ENGINE_KILLED", "ENGINE_NOT_CONNECTED"})
# a run stops asking an engine after one of these (asking again cannot help, or must not happen). A run also stops
# after PROTOCOL_ERROR_STOP_AFTER ENGINE_PROTOCOL_ERRORs with no answer in between (agents/probes.py, agents/callum.py)
PROTOCOL_ERROR_STOP_AFTER = 2
STOP_CODES = frozenset({"ENGINE_AUTH_REFUSED", "ENGINE_RATE_LIMITED", "ENGINE_BUDGET_EXHAUSTED", "ENGINE_KILLED",
                        "ENGINE_NOT_CONNECTED"})
# The one fixed instruction sent with every question, by every engine: no tenant, no person, no brand, no identifier.
SYSTEM_PROMPT = "Answer the question concisely and cite your sources."
USER_AGENT = "ZBM-SEO-Answer-Probe/0.1"
_KEY_RX = re.compile(r"[\x21-\x7e]{20,512}")
_MICRO = Decimal("0.000001")
_NAME_RX = re.compile(r"[A-Za-z0-9_.-]{1,40}")


class EngineError(Exception):
    """An answer-engine outcome that is not an answer. ``code`` is one of CODES; nothing else is carried (no body,
    no header, no key, no URL)."""

    def __init__(self, code: str, detail: Optional[str] = None, cost: Optional[str] = None):
        if code not in CODES:
            raise AssertionError(f"not an engine code: {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is None or re.fullmatch(r"[A-Z_]{1,40}", detail) else None
        # the cost the provider reported on a response that is not an answer (failed, incomplete, another model):
        # a micro-dollar Decimal string or None; the adapter charges it when it exceeds the reserve (AEGIS L4)
        self.cost = cost if cost is None or re.fullmatch(r"[0-9]{1,6}\.[0-9]{6}", cost) else None

    def __repr__(self) -> str:
        return f"EngineError({self.code!r})"


class ApiKey:
    """A provider API key. ``repr``/``str`` never show it, it cannot be pickled or compared by value from the outside,
    and ``reveal()`` is the one way to read it (used only to build the request's auth header)."""

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


@runtime_checkable
class AnswerEngine(Protocol):
    """The provider-agnostic port every answer engine implements (connected adapter or NOT_CONNECTED stand-in)."""

    name: str
    connected: bool

    def ask(self, prompt: str, model: Optional[str] = None, **kw) -> ProviderAnswer: ...

    def describe(self) -> dict: ...


# ---------------------------------------------------------------------------------------------- strict JSON helpers

def no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def strict_json(body: bytes):
    """UTF-8 JSON with duplicate keys and NaN / Infinity refused (raises ValueError / UnicodeDecodeError)."""
    return json.loads(body.decode("utf-8"), object_pairs_hook=no_duplicates,
                      parse_constant=lambda c: (_ for _ in ()).throw(ValueError("NaN")))


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


def drift_names(keys, known) -> tuple:
    """The NAMES (never the values) of response keys a parser does not model, sorted and bounded (drift visibility).
    A name that is not a plain identifier is reported as "<other>"."""
    names = set()
    for k in keys:
        if k in known:
            continue
        names.add(k if isinstance(k, str) and _NAME_RX.fullmatch(k) else "<other>")
    return tuple(sorted(names)[:MAX_UNMODELLED_KEYS])


def dedupe(urls) -> list:
    out: list = []
    for u in urls:
        if isinstance(u, str) and u and u not in out:
            out.append(u)
    return out[:MAX_SOURCES]


def history_bytes(history: list) -> int:
    """The size of a multi-turn history as it is sent (JSON, UTF-8)."""
    return len(json.dumps(history, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    before each one, the host resolved INSIDE the deadline (on the shared resolver gate pattern), the host required
    to be on the engine's egress allow-list (``hosts``; None = the caller checked it), and every resolved address
    required to be public (a provider API never answers from a private address; one that does is refused, so a
    poisoned resolver cannot point this client inside the network)."""

    def __init__(self, monotonic: Callable[[], float], end: float, guard: Callable, resolver,
                 inner=None, public: Optional[Callable[[str], bool]] = None, hosts: Optional[frozenset] = None):
        self._b = inner or httpcore.SyncBackend()
        self._mono, self._end, self._guard, self._resolver = monotonic, end, guard, resolver
        if public is None:
            from primitives.fetch import public_address as public
        self._public = public
        self._hosts = hosts

    def left(self, timeout: Optional[float], exc: type) -> float:
        left = self._end - self._mono()
        if left <= 0:
            raise exc("the question's overall deadline has passed")
        return left if timeout is None else min(timeout, left)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self._guard()
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
        if self._hosts is not None and (name.lower() not in self._hosts or port != 443):
            raise httpcore.ConnectError("the engine host is not on the egress allow-list; refused")
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


class _Retry(Exception):
    def __init__(self, code: str, retry_after: Optional[float] = None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


def _retry_after(value: Optional[str]) -> Optional[float]:
    """Whole seconds only (the documented form); anything else is ignored (the backoff applies)."""
    if value is None or not re.fullmatch(r"\s*[0-9]{1,6}\s*", value):
        return None
    return float(int(value))


def allowed_url(url: str, hosts: frozenset) -> bool:
    """The egress allow-list rule: https, a host on the engine's list, port 443 (implicit), no credentials, no
    fragment. Checked before every request."""
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and (p.hostname or "") in hosts and port in (None, 443) and p.username is None
            and p.password is None and not p.fragment and "@" not in p.netloc)


class HttpAnswerEngine:
    """The shared ask loop of every HTTP answer engine. A subclass sets ``name``, ``BASE_URL``, ``ALLOWED_HOSTS``,
    ``models`` / ``model`` and implements ``_path(model)``, ``_auth_headers()``, ``_body(model, prompt)`` and
    ``_parse(raw, model)``. ``ask`` returns a ProviderAnswer or raises EngineError (and nothing else for any network
    or protocol outcome)."""

    connected = True
    metered = True
    accepts_guard = True
    name = "engine"
    BASE_URL = ""
    ALLOWED_HOSTS: frozenset = frozenset()
    MAX_RESPONSE_BYTES = MAX_RESPONSE_BYTES
    MAX_PROMPT_CHARS = 2000
    # ADR 0049: native multi-turn (earlier turns sent as the provider's own user / assistant messages). Only an
    # engine whose documented multi-turn request shape was read sets MULTI_TURN (its MULTI_TURN_DOCS say where);
    # ``ask(..., history=...)`` on any other engine is refused, never approximated
    MULTI_TURN = False
    MULTI_TURN_DOCS: Optional[dict] = None
    MAX_HISTORY_TURNS = 8
    # the whole history as JSON (texts and the provider-native content blocks sent back as received): a bigger
    # history is refused, never truncated (review round 2: a cut content block would change what the engine saw)
    MAX_HISTORY_BYTES = 256 * 1024
    MAX_CONTENT_BLOCKS = 128

    def _init_http(self, key: ApiKey, timeout_s: float, attempts: int, backoff_s: tuple,
                   transport: Optional[httpx.BaseTransport], network_backend, monotonic: Callable[[], float],
                   sleep: Callable[[float], None], resolver, rand: Callable[[], float],
                   public: Optional[Callable[[str], bool]], wall: Optional[Callable[[], datetime]] = None) -> None:
        if not isinstance(key, ApiKey):
            raise TypeError("the key must be an ApiKey")
        if not 1 <= attempts <= 3:
            raise ValueError("attempts must be 1..3")
        if not 1 <= timeout_s <= 120:
            raise ValueError("timeout must be 1..120 s")
        self._key = key
        self.timeout_s = float(timeout_s)
        self.attempts = attempts
        self.backoff_s = tuple(backoff_s)
        self._transport = transport
        self._inner = network_backend
        self._mono, self._sleep, self._rand = monotonic, sleep, rand
        self._resolver = resolver
        self._public = public
        self._wall = wall or utc_now

    # ------------------------------------------------------------------ subclass hooks

    def _path(self, model: str) -> str:
        raise NotImplementedError

    def _auth_headers(self) -> dict:
        raise NotImplementedError

    def _body(self, model: str, prompt: str) -> bytes:
        raise NotImplementedError

    def _body_turns(self, model: str, history: list, prompt: str) -> bytes:
        """The request carrying earlier turns (``history``: [{"role": "user"|"assistant", "text"}], oldest first)
        and the new user prompt. Only engines with MULTI_TURN implement it."""
        raise NotImplementedError

    @property
    def multi_turn(self) -> bool:
        return bool(self.MULTI_TURN)

    def _check_history(self, history) -> None:
        """Alternating user / assistant turns starting with user, bounded: anything else is refused before a
        request is built (ADR 0049). An assistant turn may carry ``content``: the provider's own content blocks of
        that answer exactly as received (``ProviderAnswer.turn_content``), which a MULTI_TURN engine sends back
        unchanged instead of the text."""
        if not self.MULTI_TURN:
            raise EngineError("ENGINE_PROTOCOL_ERROR", "MULTI_TURN_NOT_SUPPORTED")
        if not isinstance(history, list) or not history or len(history) > 2 * self.MAX_HISTORY_TURNS \
                or len(history) % 2:
            raise EngineError("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED")
        for i, h in enumerate(history):
            role = "user" if i % 2 == 0 else "assistant"
            keys = {"role", "text"} if role == "user" else ({"role", "text"}, {"role", "text", "content"})
            if not isinstance(h, dict) or (set(h) != keys if role == "user" else set(h) not in keys) \
                    or h["role"] != role or not isinstance(h["text"], str) or not h["text"]:
                raise EngineError("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED")
            if "content" in h and (not isinstance(h["content"], list) or not h["content"]
                                   or len(h["content"]) > self.MAX_CONTENT_BLOCKS
                                   or not all(isinstance(b, dict) for b in h["content"])):
                raise EngineError("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED")
        if history_bytes(history) > self.MAX_HISTORY_BYTES:
            raise EngineError("ENGINE_PROTOCOL_ERROR", "HISTORY_TOO_LARGE")

    def _parse(self, raw: bytes, model: str) -> ProviderAnswer:
        raise NotImplementedError

    def _stamp(self, ans: ProviderAnswer) -> ProviderAnswer:
        """Provider and receipt time on every answer (the parsers stay pure)."""
        ans.provider = self.name
        try:
            ans.asked_at = iso_utc(self._wall())
        except Exception:  # noqa: BLE001 - a broken clock never turns an answer into a crash
            ans.asked_at = None
        return ans

    # ------------------------------------------------------------------ public

    def ask(self, prompt: str, model: Optional[str] = None, guard: Optional[Callable] = None,
            meter=None, history: Optional[list] = None) -> ProviderAnswer:
        """One question, at most ``attempts`` requests, all within one hard deadline. ``guard`` is the kill-switch
        check (raises to stop); ``meter`` charges every request against the run's budget reservation
        (``before()`` raises ENGINE_BUDGET_EXHAUSTED; ``after(cost)``). ``history`` (ADR 0049, MULTI_TURN engines
        only): earlier turns, sent as the provider's own user / assistant messages; None = one question, the
        request exactly as before."""
        model = model or self.model
        if model not in self.models or not isinstance(prompt, str) or not 1 <= len(prompt) <= self.MAX_PROMPT_CHARS:
            raise EngineError("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED")
        if history is None:
            body = self._body(model, prompt)
        else:
            self._check_history(history)
            body = self._body_turns(model, history, prompt)
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
                status, headers, raw = self._post(body, end, guard, model)
                self._check(guard)                   # a switch engaged during the last read: drop the answer
                if status == 200:
                    ans = self._parse(raw, model)
                    if meter is not None:
                        meter.after((ans.usage or {}).get("total_cost_usd"))
                        charged = True
                    return self._stamp(ans)
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
            except EngineError as e:
                if meter is not None and not charged and e.cost is not None:
                    # a response that is not an answer: charged what it reported when that is above the reserve,
                    # never less than the reserve (the finally below charges the reserve otherwise)
                    per_call = getattr(meter, "per_call", None)
                    try:
                        above = per_call is not None and Decimal(e.cost) > Decimal(str(per_call))
                    except (InvalidOperation, ValueError):
                        above = False
                    if above:
                        meter.after(e.cost)
                        charged = True
                raise
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

    def _check(self, guard: Optional[Callable]) -> None:
        if guard is None:
            return
        try:
            guard(capability="ai_probe", provider=self.name)
        except Exception as exc:   # noqa: BLE001 - the run's guard raises Killed; it becomes ENGINE_KILLED
            code = getattr(exc, "code", None)
            raise EngineError("ENGINE_KILLED", code if isinstance(code, str) else None) from None

    def _post(self, body: bytes, end: float, guard: Optional[Callable], model: str) -> tuple[int, dict, bytes]:
        name = self.name

        def g() -> None:
            if guard is None:
                return
            try:
                guard(capability="ai_probe", provider=name)
            except Exception as exc:  # noqa: BLE001
                raise _Killed(getattr(exc, "code", "KILLED")) from None

        url = self.BASE_URL + self._path(model)
        if not allowed_url(url, self.ALLOWED_HOSTS):
            raise EngineError("ENGINE_NOT_CONNECTED", "HOST_NOT_ALLOWED")      # egress allow-list (never sent)
        transport = self._transport
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False, http2=False, retries=0)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise EngineError("ENGINE_NOT_CONNECTED", "NO_DEADLINE_BACKEND")    # never ask without a deadline
            pool._network_backend = _GuardedDeadlineBackend(self._mono, end, g, self._resolver, inner=self._inner,
                                                            public=self._public, hosts=self.ALLOWED_HOSTS)
        headers = {**self._auth_headers(), "Content-Type": "application/json", "Accept": "application/json",
                   "Accept-Encoding": "identity", "User-Agent": USER_AGENT}
        left = end - self._mono()
        if left <= 0:
            raise _Retry("ENGINE_UNAVAILABLE")
        limit = self.MAX_RESPONSE_BYTES
        try:
            with httpx.Client(timeout=httpx.Timeout(left), transport=transport, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("POST", url, headers=headers, content=body) as resp:
                    status = resp.status_code
                    kept = {"retry-after": resp.headers.get("retry-after")}
                    if status != 200:
                        return status, kept, b""          # an error body is never read, logged or parsed
                    enc = resp.headers.get("content-encoding", "identity").strip().lower()
                    ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if enc not in ("identity", "") or ctype != "application/json":
                        raise EngineError("ENGINE_PROTOCOL_ERROR")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise EngineError("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
                    chunks, size = [], 0
                    for chunk in resp.iter_raw():
                        if self._mono() >= end:
                            raise _Retry("ENGINE_UNAVAILABLE")          # the deadline, mid-body
                        size += len(chunk)
                        if size > limit:
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


class NotConnectedAnswerEngine:
    """An engine that is not enabled (no key, ``SEO_<ENGINE>_ENABLED`` unset) or not usable as configured: every
    question is NOT_CONNECTED (no request is made, nothing is invented)."""

    connected = False
    metered = False

    def __init__(self, name: str, why: str, reason: Optional[str] = None, models: tuple = (),
                 base_url: Optional[str] = None, doc: Optional[dict] = None):
        self.name = name
        self.reason = reason if reason is None or re.fullmatch(r"[A-Z_]{1,40}", reason) else None
        self._why, self._models, self._base, self._doc = why, tuple(models), base_url, doc

    def ask(self, prompt: str, model: Optional[str] = None, **_kw) -> ProviderAnswer:
        return ProviderAnswer(NOT_CONNECTED)

    def describe(self) -> dict:
        return {"state": NOT_CONNECTED, "base_url": self._base, "model": None, "models_allowed": list(self._models),
                "reason": self.reason, "why": self._why, "docs": self._doc}
