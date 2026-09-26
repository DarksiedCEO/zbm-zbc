"""
REST surface for Creative Production (ADR 0005). Thin marshaling around the
two workflows (`zbm.workflow`, `zbc.workflow`) and the shared registry /
rights records. Same discipline as services/fulfillment-py/src/api.py:

- fail-closed bearer auth on every route except /health; the service
  refuses to start without CREATIVE_SERVICE_TOKEN; a non-ASCII token is a
  401, never a 500 (hmac.compare_digest TypeError caught);
- /docs, /redoc, /openapi.json disabled;
- bind address 127.0.0.1 by default (see serve.py, CREATIVE_BIND_ADDR);
- every department that doesn't exist yet is a fail-closed stand-in, and
  the evidence ledger defaults to "not configured" (every decision refused)
  unless LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN are both set.

Andre's approvals need a SECOND secret in the `X-Andre-Approval-Token`
header (CREATIVE_ANDRE_APPROVAL_TOKEN), so API access alone can't sign as
Andre.

Actor authentication (fix wave 2, N4): every action attributed to an actor
(drafting, approving, reviewing, registry/rights writes) needs that
actor's OWN credential in the `X-Creative-Actor-Token` header. Credentials
are configured server-side in CREATIVE_ACTOR_TOKENS (JSON object
{"actor_id": "token"}); the identity is the actor whose token matches
(SHA-256 digests compared with hmac.compare_digest against EVERY
configured token, no early exit). A body `actor_id` is optional and, if
present, must equal the authenticated actor (else 403) — the body can no
longer assert who is acting, so drafter != approver is enforced on
authenticated identity. Fail closed: no actor tokens configured -> every
actor action is refused (403); missing / unknown token -> 401. At start-up
each token must be >= 16 printable ASCII characters, unique, for a known
actor (never "andre"), and differ from the service and Andre tokens —
otherwise the service refuses to start.

Idempotent creation (fix wave 4, IDEM): every creating POST (brief, job,
work, rulebook draft, revision, kit, clip, clearance record, licence)
accepts an `Idempotency-Key` header (1-128 printable ASCII). The key is
scoped to (route, path, authenticated actor). A retry with the same key and
the same canonical request returns the ORIGINAL response (same status,
same body, header `Idempotent-Replayed: true`) and records nothing new; the
same key with different content is a 409. Without a header:
- routes whose body carries the caller's own id (clip `submission_id`,
  clearance `record_id`, licence `license_id`) use that id as the key;
- every other creating route derives the key from (actor, route, path,
  canonical body) and replays only while the resource it created is still
  exactly as created — so a lost response + retry never duplicates, while
  a deliberate second, identical request made after things moved on (e.g.
  a new job on a brief whose first job already has work) is a new request.
  Send a fresh Idempotency-Key to create a deliberate duplicate.
The store is in memory and bounded (IDEMPOTENCY_MAX_ENTRIES, oldest
evicted first), like every other piece of state in this service.

Request size (fix wave 4, LIM): a body over MAX_BODY_BYTES (1 MiB) is
refused with 413 BEFORE it is read into the JSON parser — by Content-Length
when declared, by counting bytes when streamed. Every route handler is a
plain `def`, so FastAPI runs it in its worker thread pool: text scanning
never blocks the event loop (tests/test_fix_wave_4.py checks both).

Request head and connection limits (fix wave 5, NEW-3; mirrors
detection-py): `serve.py` runs uvicorn's h11 parser with
h11_max_incomplete_event_size = MAX_HEADER_BYTES (16 KiB), so an oversized
request line / header block is refused (400) while it is being read instead
of buffered (httptools buffered a 200 MB header: 107 -> 220 MB RSS); a
request head must arrive within serve.REQUEST_HEAD_TIMEOUT_S of connect or
of the previous response; idle keep-alive is closed after 5 s; at most
serve.MAX_CONCURRENCY connections/requests at once (uvicorn
limit_concurrency; beyond it, 503). BodyLimit re-checks the head size (431)
for any other launcher and bounds body delivery to BODY_READ_TIMEOUT_S
(408), so a slow-drip body can't hold a request open forever.

Bounded error bodies (fix wave 6, N2): the framework's default 422 echoed
each error's `input` (13.5 MiB for a 1 MiB junk body; 60,000 unknown keys
= 60,000 errors), which stalled the event loop under concurrency. Now
`bounded_validation_body` (first ERROR_MAX_ERRORS errors, capped `loc` /
`msg`, no input, unknown keys counted, < 8 KiB, built off the loop past
ERROR_OFFLOAD_ABOVE errors), capped CreativeError reasons / issues, a
fixed JSON 500, and — the root — `json_shape_violation` in BodyLimit: a
JSON body with more members than its route's model can legally hold
(`route_member_limits`, fix wave 7 NEW-1; fix wave 6 had one 4,096 cap,
which refused a legal Moment Map) or nested deeper than MAX_JSON_DEPTH is
refused in a worker thread before the framework sees it.

Content types (fix wave 8, AEGIS round 7 N7-1): the shape pre-scan gated
on the exact `application/json` while FastAPI parses every
`application/*+json` body, so `application/hal+json` with 60,000 keys
reached the framework (60,001 validation errors, /health 3.7 s under 20
senders, RSS never released). Now `is_json_content_type` decides with the
SAME parser FastAPI uses (email.message: main type `application`, subtype
`json` or `<x>+json`, parameters and case ignored): every such body is
pre-scanned; a body under any OTHER content type, or with none, is
refused 415 — with a declared length or chunked encoding BEFORE the body
is read. After a body of LARGE_BODY_BYTES or more has been parsed and no
other large body is in flight, freed heap pages are handed back to the OS
(`malloc_trim(0)` after TRIM_IDLE_S, off the event loop; the same fix as
fulfillment-py's fix wave 7).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from shared.actors import ActorRegistry
from shared.clock import Clock, SystemClock
from shared.compliance38 import compliance_from_env
from shared.departments import Departments
from shared.errors import (
    CreativeError,
    FounderApprovalRefused,
    FrozenError,
    GuardrailViolation,
    NotFound,
    PreconditionFailed,
    RegistryRowBlocked,
    ValidationFailed,
)
from shared.founder import FounderGate
from shared.ledger import (
    EvidenceRecorder,
    HttpLedgerClient,
    LedgerClient,
    LedgerConflict,
    LedgerRecordError,
    OutcomeNotRecorded,
    UnconfiguredLedgerClient,
)
from shared.registry import PlatformRulesRegistry, RegistryRow, check_usable, seeded_registry
from shared.rights import CampaignLicense, ClearanceRecord, RightsRegistry, record_clearance, record_license
from shared.types import BOUNDED_ID_PATTERN, MAX_RULEBOOK_VERSION, SAFE_ID_PATTERN
from zbc import platform_rules as zbc_platform_rules
from zbc.campaign_kit import KitRequest
from zbc.clip_review import BrokenRule, ClipSubmission
from zbc.creative_memory import ClipResult
from zbc.rights_clearance import DeclaredAsset
from zbc.rulebook_writer import CampaignGoal
from zbc.source_mining import SourceMaterial
from zbc.workflow import DEFAULT_SUPERSEDED_GRACE_HOURS, HumanVerdict, ZbcWorkflow
from zbm import hook_retention
from zbm import placement_spec as zbm_placement_spec
from zbm.brief_writer import ClientRequirements
from zbm.results import PerformanceResult
from zbm.workflow import WorkSubmission, ZbmWorkflow

FOUNDER_HEADER = "X-Andre-Approval-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_MAX_ENTRIES = 10_000
MAX_BODY_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
_IDEM_KEY_RE = re.compile(r"[\x21-\x7e]{1,128}")
ACTOR_HEADER = "X-Creative-Actor-Token"
ACTOR_TOKEN_MIN_LEN = 16
# Error bodies are bounded (fix wave 6, N2): never the request's content.
ERROR_MAX_ERRORS = 20        # validation errors reported (the rest are counted)
ERROR_MAX_LOC = 6            # `loc` elements kept
ERROR_MAX_STR = 80           # characters of a loc element / message / issue kept
ERROR_MAX_ISSUES = 20        # CreativeError `issues` reported
ERROR_MAX_DETAIL = 1000      # characters of a CreativeError reason kept
ERROR_BODY_MAX_BYTES = 8 * 1024
ERROR_OFFLOAD_ABOVE = 1000   # validation errors: build the body off the event loop past this many
# JSON shape limits (fix wave 6, N2), checked in BodyLimit off the event loop
# BEFORE the framework parses and validates: a body of 60,000 unknown keys
# cost ~250 ms of event-loop time in error bookkeeping (FastAPI builds one
# error record per key) and ~15 MB of memory, 20 at once stalled /health
# for seconds. Fix wave 7 (AEGIS round 6, NEW-1): the single 4,096-member
# cap of fix wave 6 refused a legal 820-segment Moment Map (the model
# allows 2,000 segments = 10,003 members). Each route's cap is now computed
# from its request model (`shared.request_limits.member_limit_for`: the
# largest legal body plus 25% headroom, a multiple of 64; see
# `route_member_limits`), so the cap can never be tighter than the model:
# from 64 (a body of one key) to 20,672 (hook advice, 500 results of 20
# metrics). A path with no request body, or none of ours, gets
# DEFAULT_JSON_MEMBERS. The depth cap is unchanged.
DEFAULT_JSON_MEMBERS = 64
MAX_JSON_DEPTH = 32
# Heap trim after large parses (fix wave 8, N7-1; see the module docstring).
LARGE_BODY_BYTES = 64 * 1024
TRIM_IDLE_S = 1.0

# Path ids are validated BEFORE any work (integration defect 2): a campaign
# id is at most 100 characters so every derived ledger subject
# ("{campaign_id}:v{version}") fits ledger-rust's 128; every other id uses
# the ledger's own subject_id rule. A bad id is a 422, never a 503.
CampaignIdPath = Annotated[str, Path(pattern=BOUNDED_ID_PATTERN)]
IdPath = Annotated[str, Path(pattern=SAFE_ID_PATTERN)]
VersionPath = Annotated[int, Path(ge=1, le=MAX_RULEBOOK_VERSION)]
RULEBOOK_PAGE = 100  # version summaries per GET .../rulebooks page (fix wave 9, L2)
RETIRED_PAGE = 1000  # retired rule ids per GET .../retired-rule-ids page

_STATUS = {
    NotFound: 404,
    GuardrailViolation: 403,
    FounderApprovalRefused: 403,
    FrozenError: 409,
    PreconditionFailed: 409,
    RegistryRowBlocked: 409,
    ValidationFailed: 422,
}


def _load_required_token() -> str:
    token = os.environ.get("CREATIVE_SERVICE_TOKEN")
    if not token:
        raise RuntimeError(
            "CREATIVE_SERVICE_TOKEN is not set. This service refuses to start without an auth token "
            "configured (fail closed, not open). Set CREATIVE_SERVICE_TOKEN before starting creative-py."
        )
    return token


def grace_hours_from_env() -> int:
    """CREATIVE_SUPERSEDED_GRACE_HOURS: how long after a rulebook version is
    superseded a clip declaring it is still judged automatically (F13).
    Default 72. Anything but an integer 0..720 refuses to start."""
    raw = os.environ.get("CREATIVE_SUPERSEDED_GRACE_HOURS")
    if raw is None or raw == "":
        return DEFAULT_SUPERSEDED_GRACE_HOURS
    try:
        hours = int(raw)
    except ValueError:
        hours = -1
    if not 0 <= hours <= 720:
        raise RuntimeError(f"CREATIVE_SUPERSEDED_GRACE_HOURS must be an integer 0..720 hours, got {raw!r}")
    return hours


def actor_tokens_from_env() -> dict[str, str]:
    """CREATIVE_ACTOR_TOKENS: JSON object {"actor_id": "token", ...}. Unset
    or empty -> {} (every actor action refused). Malformed -> refuse to start."""
    raw = os.environ.get("CREATIVE_ACTOR_TOKENS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise RuntimeError('CREATIVE_ACTOR_TOKENS must be a JSON object {"actor_id": "token", ...}')
    return data


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


def _check_actor_tokens(tokens: dict[str, str], actors: ActorRegistry, service_token: str,
                        founder_token: str | None) -> dict[str, bytes]:
    seen: set[str] = set()
    for actor_id, tok in tokens.items():
        if actor_id == "andre":
            raise RuntimeError("CREATIVE_ACTOR_TOKENS: 'andre' acts only through the Andre approval token")
        if actor_id not in actors.actors:
            raise RuntimeError(f"CREATIVE_ACTOR_TOKENS: unknown actor {actor_id!r}")
        if not isinstance(tok, str) or len(tok) < ACTOR_TOKEN_MIN_LEN or not re.fullmatch(r"[\x21-\x7e]+", tok):
            raise RuntimeError(f"CREATIVE_ACTOR_TOKENS: token for {actor_id!r} must be >= {ACTOR_TOKEN_MIN_LEN} "
                               "printable ASCII characters")
        if tok in seen:
            raise RuntimeError("CREATIVE_ACTOR_TOKENS: two actors share one token")
        if tok == service_token or (founder_token and tok == founder_token):
            raise RuntimeError(f"CREATIVE_ACTOR_TOKENS: token for {actor_id!r} equals the service or Andre token")
        seen.add(tok)
    return {a: _digest(t) for a, t in tokens.items()}


@dataclass
class _Created:
    content: str            # SHA-256 of the canonical request
    body: dict              # the original response body
    snapshot: Any           # the created resource as it was right after creation


class IdempotencyStore:
    """Bounded map key -> original creation (oldest evicted first)."""

    def __init__(self, max_entries: int = IDEMPOTENCY_MAX_ENTRIES):
        self._max = max_entries
        self._d: OrderedDict = OrderedDict()

    def get(self, key) -> _Created | None:
        return self._d.get(key)

    def put(self, key, value: _Created) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)

    def __len__(self) -> int:
        return len(self._d)


def json_shape_violation(body: bytes, max_members: int = DEFAULT_JSON_MEMBERS) -> tuple[int, str, str] | None:
    """(status, detail, error) if a JSON body has more than `max_members`
    members (object keys + array items, counted over the whole document)
    or nests deeper than MAX_JSON_DEPTH; None if it is within bounds or is
    not valid JSON at all (the framework then answers its own bounded 422).
    Counting stops at the cap, so the Python work is O(cap) whatever the
    body; parsing is C. Runs off the event loop."""
    try:
        obj = json.loads(body)
    except RecursionError:
        return 400, f"JSON body nests deeper than {MAX_JSON_DEPTH} levels", "PayloadTooDeep"
    except ValueError:
        return None
    members = 0
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            return 400, f"JSON body nests deeper than {MAX_JSON_DEPTH} levels", "PayloadTooDeep"
        if isinstance(node, dict):
            members += len(node)
            children = node.values()
        elif isinstance(node, list):
            members += len(node)
            children = node
        else:
            continue
        if members > max_members:
            return 422, f"JSON body has more than {max_members} members (keys and items) for this route", "PayloadTooManyMembers"
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return None


def route_member_limits(app: FastAPI) -> list[tuple[frozenset[str], "re.Pattern[str]", str, int]]:
    """(methods, path regex, path template, member cap) for every route of
    `app` that takes a JSON body, the cap computed from the body model
    (`shared.request_limits.member_limit_for`). Derived from the routes,
    so a new route or a changed model is sized automatically; a model
    with an unbounded list or dict refuses to build the app."""
    from fastapi.routing import APIRoute

    from shared.request_limits import member_limit_for

    out = []
    for r in app.routes:
        if isinstance(r, APIRoute) and r.body_field is not None:
            model = r.body_field.field_info.annotation
            out.append((frozenset(r.methods or ()), r.path_regex, r.path, member_limit_for(model)))
    return out


BEARER_MISSING = "missing or malformed Authorization header (expected: Bearer <token>)"


def _bearer_refusal(authorization: bytes | str | None, service_token: str) -> str | None:
    """None if `authorization` is `Bearer <the service token>`, else the
    401 detail. Constant-time; a header that cannot be compared is not the
    token (401, never 500)."""
    if isinstance(authorization, bytes):
        try:
            authorization = authorization.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover (latin-1 decodes every byte)
            return "invalid token"
    if authorization is None or not authorization.startswith("Bearer "):
        return BEARER_MISSING
    try:
        valid = hmac.compare_digest(authorization.removeprefix("Bearer "), service_token)
    except TypeError:
        valid = False
    return None if valid else "invalid token"


def is_json_content_type(value: bytes | str | None) -> bool:
    """Would FastAPI parse a body under this Content-Type as JSON? The same
    decision, made with the same parser (email.message, as
    fastapi.routing does): main type `application` and subtype `json` or
    `<x>+json`; parameters (charset, q) and case are ignored. None / empty
    -> False (FastAPI's strict mode does not parse a body with no type)."""
    if not value:
        return False
    import email.message

    msg = email.message.Message()
    msg["content-type"] = value.decode("latin-1") if isinstance(value, bytes) else value
    return msg.get_content_maintype() == "application" and (
        msg.get_content_subtype() == "json" or msg.get_content_subtype().endswith("+json"))


def _libc():
    try:
        import ctypes

        return ctypes.CDLL("libc.so.6")
    except OSError:
        return None


_LIBC = _libc()


def _malloc_trim() -> None:
    """Return freed heap pages to the OS (fix wave 8, N7-1). glibc only; a
    no-op elsewhere."""
    try:
        _LIBC.malloc_trim(0)
    except AttributeError:
        pass


class BodyLimit:
    """ASGI middleware: refuse a request head over `max_head` bytes (431), a
    body under a content type FastAPI would not parse as JSON (415, before
    the body is read when its length or chunking is declared; fix wave 8,
    N7-1), a body over `limit` bytes (413) before any of it reaches the
    JSON parser, a body not delivered within `read_timeout` seconds in
    total (408), and a JSON body over the route's member cap
    (`member_limits`, from `route_member_limits`; DEFAULT_JSON_MEMBERS for
    any other path) or the depth limit (`json_shape_violation`, fix wave
    6, N2 / fix wave 7, NEW-1 — checked in a worker thread). After a body
    of `large` bytes or more, the heap is trimmed once idle."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES, max_head: int = MAX_HEADER_BYTES,
                 read_timeout: float = BODY_READ_TIMEOUT_S, member_limits=None,
                 default_members: int = DEFAULT_JSON_MEMBERS, large: int = LARGE_BODY_BYTES,
                 service_token: str | None = None):
        self.app, self.limit, self.max_head, self.read_timeout = app, limit, max_head, read_timeout
        self.service_token = service_token
        self.member_limits = list(member_limits or [])
        self.default_members = default_members
        self.large = large
        self.large_in_flight = 0
        self.trim_timer = None
        self.scan_gate: asyncio.Semaphore | None = None

    def members_for(self, method: str, path: str) -> int:
        for methods, regex, _, cap in self.member_limits:
            if method in methods and regex.match(path):
                return cap
        return self.default_members

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        head = len(scope.get("raw_path") or b"") + len(scope.get("query_string") or b"")
        head += sum(len(k) + len(v) + 4 for k, v in scope.get("headers") or [])
        if head > self.max_head:
            return await self._refuse(send, 431, f"request head over {self.max_head} bytes", "RequestHeaderTooLarge")
        declared = content_type = transfer_encoding = authorization = None
        for k, v in scope.get("headers") or []:  # the FIRST of each, as the framework reads them
            if k == b"content-length" and declared is None:
                declared = v
            elif k == b"content-type" and content_type is None:
                content_type = v
            elif k == b"transfer-encoding" and transfer_encoding is None:
                transfer_encoding = v
            elif k == b"authorization" and authorization is None:
                authorization = v
        # Fix wave 9 (AEGIS round 8 L4): authentication FIRST. The 415 / 413 / shape answers below used to
        # reach an unauthenticated caller, who could probe which content types and sizes are accepted.
        # Every route but /health needs the bearer token (the route dependency checks it again).
        if self.service_token is not None and scope.get("path") != "/health":
            refused = _bearer_refusal(authorization, self.service_token)
            if refused is not None:
                return await self._unauthorized(send, refused)
        if declared is not None:
            try:
                too_big = int(declared) > self.limit
            except ValueError:
                too_big = True
            if too_big:
                return await self._refuse(send)
        is_json = is_json_content_type(content_type)
        if not is_json and ((declared is not None and int(declared) > 0) or transfer_encoding is not None):
            # a body is coming under a type the framework would not parse as JSON: refused unread
            return await self._refuse(send, 415, "request body must be JSON (Content-Type: application/json "
                                      "or application/<x>+json)", "UnsupportedMediaType")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.read_timeout
        chunks, total, more = [], 0, True
        while more:
            try:
                msg = await asyncio.wait_for(receive(), max(0.0, deadline - loop.time()))
            except (TimeoutError, asyncio.TimeoutError):
                return await self._refuse(send, 408, f"request body not received within {self.read_timeout:g}s",
                                          "RequestTimeout")
            if msg["type"] == "http.disconnect":
                return
            chunk = msg.get("body", b"")
            total += len(chunk)
            if total > self.limit:
                return await self._refuse(send)
            chunks.append(chunk)
            more = msg.get("more_body", False)
        body = b"".join(chunks)
        if body and not is_json:  # no declared length, no chunking, yet a body arrived
            return await self._refuse(send, 415, "request body must be JSON (Content-Type: application/json "
                                      "or application/<x>+json)", "UnsupportedMediaType")
        large = len(body) >= self.large
        if large:
            self.large_in_flight += 1
        try:
            if body:
                cap = self.members_for(scope.get("method", ""), scope.get("path", ""))
                if large:
                    # Fix wave 9 (AEGIS round 8 L3): one large body's shape scan at a time. json.loads holds
                    # the GIL for the whole parse, so 20 of them at once kept the event loop from running
                    # between them (/health p50 390 ms under 20 junk senders); queued, the loop runs
                    # between every two. Small bodies are not queued behind them.
                    if self.scan_gate is None:
                        self.scan_gate = asyncio.Semaphore(1)
                    async with self.scan_gate:
                        bad = await run_in_threadpool(json_shape_violation, body, cap)
                else:
                    bad = await run_in_threadpool(json_shape_violation, body, cap)
                if bad is not None:
                    return await self._refuse(send, *bad)
            sent = False

            async def replay():
                nonlocal sent
                if sent:
                    return await receive()
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            return await self.app(scope, replay, send)
        finally:
            if large:
                self.large_in_flight -= 1
                if self.trim_timer is not None:
                    self.trim_timer.cancel()
                self.trim_timer = loop.call_later(TRIM_IDLE_S, self._trim_if_idle, loop)

    def _trim_if_idle(self, loop) -> None:
        self.trim_timer = None
        if self.large_in_flight == 0:
            loop.run_in_executor(None, _malloc_trim)

    async def _unauthorized(self, send, detail: str):
        """The same 401 the route's `require_auth` dependency answers."""
        raw = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
                                (b"www-authenticate", b"Bearer"), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": raw})

    async def _refuse(self, send, code: int = 413, detail: str | None = None, error: str = "PayloadTooLarge"):
        raw = json.dumps({"detail": detail or f"request body over {self.limit} bytes", "error": error}).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": raw})


def _clip(text: Any, n: int = ERROR_MAX_STR) -> str:
    t = str(text)
    return t if len(t) <= n else t[:n] + "..."


def bounded_validation_body(errors: list[dict]) -> dict:
    """The 422 body for a request that failed validation (fix wave 6, N2).
    FastAPI's default echoes each error's `input` — the whole body for a
    missing field — so a 1 MiB junk body became a 13.5 MiB answer, and
    60,000 unknown keys 60,000 errors. This body never contains input:
    the first ERROR_MAX_ERRORS errors (loc capped to ERROR_MAX_LOC elements
    of ERROR_MAX_STR characters, msg capped, no `input`, no `ctx`, no
    `url`); unknown keys (`extra_forbidden`) are counted, with the first
    few names; and the whole thing is kept under ERROR_BODY_MAX_BYTES."""
    total = len(errors)
    unknown = 0
    unknown_first: list[str] = []
    kept: list[dict] = []
    for e in errors:
        if e.get("type") == "extra_forbidden":
            unknown += 1
            if len(unknown_first) < ERROR_MAX_ERRORS:
                loc = e.get("loc") or ()
                unknown_first.append(_clip(loc[-1]) if loc else "?")
            continue
        if len(kept) < ERROR_MAX_ERRORS:
            loc = list(e.get("loc") or ())
            kept.append({"type": _clip(e.get("type", "")), "msg": _clip(e.get("msg", "")),
                         "loc": [_clip(x) for x in loc[:ERROR_MAX_LOC]] + (["..."] if len(loc) > ERROR_MAX_LOC else [])})
    truncated = total > len(kept) + (1 if unknown else 0) and (total - unknown > len(kept) or unknown > len(unknown_first))
    body = {"detail": "request failed validation", "error": "RequestValidationError", "error_count": total,
            "errors": kept, "truncated": truncated}
    if unknown:
        body["unknown_fields"] = {"count": unknown, "first": unknown_first}
    while len(json.dumps(body, separators=(",", ":")).encode()) > ERROR_BODY_MAX_BYTES and (body["errors"] or unknown_first):
        body["errors"] = body["errors"][:-1] if len(body["errors"]) >= len(unknown_first) else body["errors"]
        if len(body["errors"]) < len(unknown_first):
            unknown_first.pop()
        body["truncated"] = True
    return body


def ledger_from_env() -> LedgerClient:
    if os.environ.get("LEDGER_SERVICE_URL") and os.environ.get("LEDGER_SERVICE_TOKEN"):
        return HttpLedgerClient.from_env()
    return UnconfiguredLedgerClient()


# --- request bodies -----------------------------------------------------------------

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorIn(_In):
    actor_id: str | None = Field(default=None, min_length=1, max_length=64)  # optional; must match the credential


class RegistryWriteIn(_In):
    actor_id: str | None = Field(default=None, min_length=1, max_length=64)
    row: RegistryRow


class ClearanceIn(_In):
    actor_id: str | None = None
    record: ClearanceRecord


class LicenseIn(_In):
    actor_id: str | None = None
    license: CampaignLicense


class DraftBriefIn(_In):
    actor_id: str | None = None
    requirements: ClientRequirements


class QualityIn(_In):
    actor_id: str | None = None
    notes: list[str] = Field(default_factory=list, max_length=100)


class EscalationIn(_In):
    decision: Literal["accept", "kill"]


class HookAdviceIn(_In):
    results: list[PerformanceResult] = Field(max_length=500)
    platform: str
    placement: str
    metric: str = hook_retention.DEFAULT_METRIC


class ZbmMemoryIn(_In):
    brief_id: str
    result: PerformanceResult


class DraftRulebookIn(_In):
    actor_id: str | None = None
    goal: CampaignGoal


class RightsCheckIn(_In):
    assets: list[DeclaredAsset] = Field(max_length=500)
    uses_ai_generative_fill: bool = False


class HumanReviewIn(_In):
    actor_id: str | None = None
    outcome: Literal["pass", "reject"]
    broken_rules: list[BrokenRule] = Field(default_factory=list, max_length=100)
    note: str = Field(default="", max_length=2000)


class WithdrawVerdictIn(_In):
    actor_id: str | None = None


# --- app factory ------------------------------------------------------------------------

def build_app(
    *,
    service_token: str,
    ledger: LedgerClient,
    founder_token: str | None = None,
    clock: Clock | None = None,
    departments: Departments | None = None,
    actors: ActorRegistry | None = None,
    registry: PlatformRulesRegistry | None = None,
    rights: RightsRegistry | None = None,
    superseded_grace_hours: int = DEFAULT_SUPERSEDED_GRACE_HOURS,
    actor_tokens: dict[str, str] | None = None,
) -> FastAPI:
    if not service_token:
        raise RuntimeError("service token required (fail closed)")
    if not 0 <= superseded_grace_hours <= 720:
        raise RuntimeError("superseded_grace_hours must be 0..720")
    clock = clock or SystemClock()
    departments = departments or Departments()
    actors = actors or ActorRegistry()
    registry = registry if registry is not None else seeded_registry()
    rights = rights if rights is not None else RightsRegistry()
    recorder = EvidenceRecorder(ledger)
    founder = FounderGate.build(founder_token, service_token)
    actor_digests = _check_actor_tokens(dict(actor_tokens or {}), actors, service_token, founder_token)
    common = dict(registry=registry, rights=rights, actors=actors, recorder=recorder, clock=clock,
                  departments=departments, founder=founder)
    zbm = ZbmWorkflow(**common)
    zbc = ZbcWorkflow(**common, superseded_grace_hours=superseded_grace_hours)
    lock = recorder.lock

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        # non-ASCII str: compare_digest raises; anything it can't compare is not the token (401, never 500)
        refused = _bearer_refusal(authorization, service_token)
        if refused is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=refused,
                                headers={"WWW-Authenticate": "Bearer"})

    def authenticate_actor(x_creative_actor_token: str | None = Header(default=None)) -> str:
        """The acting actor, proven by its own credential (N4)."""
        if not actor_digests:
            raise GuardrailViolation("actor credentials are not configured on this service (CREATIVE_ACTOR_TOKENS); "
                                     "no action can be attributed to an actor (fail closed)")
        if not x_creative_actor_token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail=f"missing actor credential ({ACTOR_HEADER} header)")
        supplied = _digest(x_creative_actor_token)
        match = None
        for actor_id, dig in actor_digests.items():  # every entry compared, no early exit
            if hmac.compare_digest(supplied, dig):
                match = actor_id
        if match is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid actor credential")
        return match

    def acting(claimed: str | None, actor: str) -> str:
        if claimed is not None and claimed != actor:
            raise GuardrailViolation(f"the request body names actor {claimed!r} but the credential is {actor!r}; "
                                     "identity comes from the credential only")
        return actor


    idem = IdempotencyStore()
    # Fix wave 10 (AEGIS round 9 N9-4): the expensive `unlocked` work (a clip review) in flight per
    # idempotency key. A retry that arrives while it runs waits for it instead of paying for its own.
    in_flight: dict[tuple, threading.Event] = {}

    def creating(response: Response, route: str, path: dict, actor: str | None, key: str | None,
                 request: Any, create: Callable[..., dict], view: Callable[[dict], Any] | None = None,
                 caller_id: str | None = None, unlocked: Callable[[], Any] | None = None) -> dict:
        """Run a creating request at most once per idempotency key (IDEM).
        `unlocked` (fix wave 9, M1): work that needs no lock — a clip
        review — run between a first replay check and the locked create,
        which receives its result (`create(result)`). Fix wave 10 (N9-4):
        at most ONE request per key runs it at a time; one that arrives
        meanwhile waits for it and then replays its body (or, if it
        failed and recorded nothing, runs it itself)."""
        content = hashlib.sha256(json.dumps({"route": route, "path": path, "actor": actor, "request": request},
                                            sort_keys=True, separators=(",", ":"), default=str)
                                 .encode("utf-8", "surrogatepass")).hexdigest()
        if key is not None:
            if not _IDEM_KEY_RE.fullmatch(key):
                raise ValidationFailed(f"{IDEMPOTENCY_HEADER} must be 1-128 printable ASCII characters",
                                       [IDEMPOTENCY_HEADER])
            k = ("key", route, json.dumps(path, sort_keys=True), actor or "", key)
        elif caller_id is not None:
            k = ("id", route, json.dumps(path, sort_keys=True), caller_id)
        else:
            k = ("derived", route, json.dumps(path, sort_keys=True), actor or "", content)
        def replay():
            hit = idem.get(k)
            if hit is not None:
                if hit.content != content:
                    what = (f"{IDEMPOTENCY_HEADER} {key!r}" if k[0] == "key" else f"id {caller_id!r}")
                    raise PreconditionFailed(f"{what} was already used for a different request on this route; "
                                             "nothing was recorded or changed")
                if k[0] != "derived" or view is None or view(hit.body) == hit.snapshot:
                    response.headers["Idempotent-Replayed"] = "true"
                    return hit.body
            return None

        if unlocked is None:
            with lock:
                got = replay()
                if got is not None:
                    return got
                body = create()
                idem.put(k, _Created(content, body, view(body) if view is not None else None))
                return body
        while True:
            with lock:
                got = replay()
                if got is not None:
                    return got
                running = in_flight.get(k)
                if running is None:
                    mine = in_flight[k] = threading.Event()
                    break
            running.wait()  # no lock held; the owner always sets it (finally, below)
        try:
            pre = unlocked()  # no lock held: other requests proceed meanwhile
            with lock:
                got = replay()
                if got is not None:
                    return got
                body = create(pre)
                idem.put(k, _Created(content, body, view(body) if view is not None else None))
                return body
        finally:
            with lock:
                in_flight.pop(k, None)
            mine.set()

    def _safe(fn):
        def run(body):
            try:
                return fn(body)
            except CreativeError:
                return None
        return run

    app = FastAPI(
        title="Creative Production (ZBM advertising + ZBC clipping agency)",
        version="0.1.0",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    app.state.idempotency = idem
    app.state.zbm = zbm
    app.state.zbc = zbc
    app.state.registry = registry
    app.state.rights = rights
    app.state.recorder = recorder

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        if len(errors) > ERROR_OFFLOAD_ABOVE:  # keep the event loop free for everyone else (N2)
            body = await run_in_threadpool(bounded_validation_body, errors)
        else:
            body = bounded_validation_body(errors)
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(CreativeError)
    async def _creative_error(_: Request, exc: CreativeError):
        code = next((c for cls, c in _STATUS.items() if isinstance(exc, cls)), 400)
        # reasons and issues may quote request content (a rule id, a phrase): bounded (N2)
        body = {"detail": _clip(exc.reason, ERROR_MAX_DETAIL), "error": type(exc).__name__}
        if isinstance(exc, ValidationFailed):
            issues = list(exc.issues or [])
            body["issues"] = [_clip(i, ERROR_MAX_DETAIL) for i in issues[:ERROR_MAX_ISSUES]]
            if len(issues) > ERROR_MAX_ISSUES:
                body["issues_truncated"] = len(issues) - ERROR_MAX_ISSUES
        return JSONResponse(status_code=code, content=body)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        # never the exception's message: it may quote the request (N2)
        return JSONResponse(status_code=500, content={"detail": "internal error", "error": type(exc).__name__[:64]})

    @app.exception_handler(LedgerRecordError)
    async def _ledger_error(_: Request, exc: LedgerRecordError):
        if isinstance(exc, OutcomeNotRecorded):
            return JSONResponse(status_code=503, content={
                "detail": f"decision PARTLY took effect: {exc}",
                "error": "OutcomeNotRecorded", "took_effect": "partial", "effect": exc.effect,
            })
        if isinstance(exc, LedgerConflict):
            return JSONResponse(status_code=409, content={
                "detail": f"outcome UNCERTAIN / CONFLICTING: {exc}. Nothing changed in this service; the ledger "
                          "holds another version of this decision, so it was not applied here",
                "error": "LedgerConflict", "took_effect": "unknown",
            })
        if exc.took_effect is False:
            return JSONResponse(status_code=503, content={
                "detail": f"decision did NOT take effect: the evidence ledger record failed ({exc})",
                "error": "LedgerRecordError", "took_effect": False,
            })
        return JSONResponse(status_code=503, content={
            "detail": f"outcome UNKNOWN: {exc}. Nothing changed in this service yet; the ledger may or may not "
                      "hold the record. Retry the IDENTICAL request to resolve it (it replays the exact record "
                      "and takes effect once)",
            "error": "LedgerOutcomeUnknown", "took_effect": "unknown",
        })

    auth = [Depends(require_auth)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "creative-py", "department": "creative_production",
                "ledger_configured": not isinstance(ledger, UnconfiguredLedgerClient),
                "founder_token_configured": founder.configured}

    # --- shared: Platform Rules Registry ------------------------------------------------
    def _row_view(row: RegistryRow) -> dict:
        u = check_usable(row, clock.today())
        return {**row.model_dump(mode="json"), "usable": u.usable, "usability_reason": u.reason}

    @app.get("/registry/rows", dependencies=auth)
    def registry_rows() -> dict:
        return {"rows": [_row_view(r) for r in sorted(registry.rows.values(), key=lambda r: r.row_id)]}

    @app.get("/registry/rows/{row_id}", dependencies=auth)
    def registry_row(row_id: IdPath) -> dict:
        return _row_view(registry.get(row_id))

    @app.put("/registry/rows/{row_id}", dependencies=auth)
    def registry_write(row_id: IdPath, body: RegistryWriteIn, who: str = Depends(authenticate_actor)) -> dict:
        if body.row.row_id != row_id:
            raise ValidationFailed("row_id in path and body differ", ["row_id"])
        from shared.actors import Role

        who = acting(body.actor_id, who)
        actor = actors.get(who)
        with lock:
            if Role.REGISTRY_ZBM_PLACEMENT_SPEC in actor.roles:
                eid = zbm_placement_spec.write_spec_row(registry, recorder, actors, who, body.row)
            elif Role.REGISTRY_ZBC_PLATFORM_RULES in actor.roles:
                eid = zbc_platform_rules.write_originality_row(registry, recorder, actors, who, body.row)
            else:
                raise GuardrailViolation(f"actor {who!r} owns no registry rows")
        return {"row": _row_view(registry.get(row_id)), "ledger_event_id": eid}

    # --- shared: rights records ------------------------------------------------------------
    @app.post("/rights/clearances", status_code=201, dependencies=auth)
    def add_clearance(body: ClearanceIn, response: Response, who: str = Depends(authenticate_actor),
                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        actor = acting(body.actor_id, who)

        def create() -> dict:
            eid = record_clearance(rights, recorder, actors, actor, body.record)
            return {"record": body.record.model_dump(mode="json"), "ledger_event_id": eid}

        return creating(response, "clearance", {}, actor, idempotency_key, body.record.model_dump(mode="json"),
                        create, caller_id=body.record.record_id)

    @app.post("/rights/licenses", status_code=201, dependencies=auth)
    def add_license(body: LicenseIn, response: Response, who: str = Depends(authenticate_actor),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        actor = acting(body.actor_id, who)

        def create() -> dict:
            eid = record_license(rights, recorder, actors, actor, body.license)
            return {"license": body.license.model_dump(mode="json"), "ledger_event_id": eid}

        return creating(response, "license", {}, actor, idempotency_key, body.license.model_dump(mode="json"),
                        create, caller_id=body.license.license_id)

    # --- ZBM -----------------------------------------------------------------------------------
    @app.post("/zbm/briefs", status_code=201, dependencies=auth)
    def zbm_draft(body: DraftBriefIn, response: Response, who: str = Depends(authenticate_actor),
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        actor = acting(body.actor_id, who)
        return creating(response, "brief", {}, actor, idempotency_key, body.requirements.model_dump(mode="json"),
                        lambda: zbm.draft_brief(body.requirements, actor).model_dump(mode="json"),
                        _safe(lambda b: zbm.get_brief(b["brief_id"]).model_dump(mode="json")))

    @app.get("/zbm/briefs/{brief_id}", dependencies=auth)
    def zbm_get_brief(brief_id: IdPath) -> dict:
        return zbm.get_brief(brief_id).model_dump(mode="json")

    @app.post("/zbm/briefs/{brief_id}/review", dependencies=auth)
    def zbm_review(brief_id: IdPath, body: ActorIn, who: str = Depends(authenticate_actor)) -> dict:
        return zbm.review_brief(brief_id, acting(body.actor_id, who)).model_dump(mode="json")

    @app.post("/zbm/briefs/{brief_id}/jobs", status_code=201, dependencies=auth)
    def zbm_open_job(brief_id: IdPath, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        def view(b: dict):
            job = zbm.jobs.get(b["job_id"])
            return None if job is None else [job.model_dump(mode="json"),
                                             sorted(w for w, x in zbm.work.items() if x.job_id == b["job_id"])]

        return creating(response, "job", {"brief_id": brief_id}, None, idempotency_key, None,
                        lambda: zbm.open_job(brief_id).model_dump(mode="json"), view)

    @app.post("/zbm/jobs/{job_id}/work", status_code=201, dependencies=auth)
    def zbm_submit(job_id: IdPath, body: WorkSubmission, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        return creating(response, "work", {"job_id": job_id}, None, idempotency_key, body.model_dump(mode="json"),
                        lambda: zbm.submit_work(job_id, body).model_dump(mode="json"),
                        _safe(lambda b: zbm.get_work(b["work_id"]).model_dump(mode="json")))

    @app.get("/zbm/work/{work_id}", dependencies=auth)
    def zbm_get_work(work_id: IdPath) -> dict:
        return zbm.get_work(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/export-validation", dependencies=auth)
    def zbm_export(work_id: IdPath) -> dict:
        return zbm.validate_export(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/rights", dependencies=auth)
    def zbm_rights(work_id: IdPath) -> dict:
        return zbm.check_rights(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/quality", dependencies=auth)
    def zbm_quality(work_id: IdPath, body: QualityIn, who: str = Depends(authenticate_actor)) -> dict:
        return zbm.quality_review(work_id, acting(body.actor_id, who), body.notes).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/escalation", dependencies=auth)
    def zbm_escalation(work_id: IdPath, body: EscalationIn,
                       x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbm.resolve_escalation(work_id, x_andre_approval_token, body.decision).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/compliance", dependencies=auth)
    def zbm_compliance(work_id: IdPath) -> dict:
        return zbm.compliance_gate(work_id).model_dump(mode="json")

    @app.post("/zbm/work/{work_id}/final-approval", dependencies=auth)
    def zbm_final(work_id: IdPath, x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbm.final_approval(work_id, x_andre_approval_token).model_dump(mode="json")

    @app.post("/zbm/hook-advice", dependencies=auth)
    def zbm_hook_advice(body: HookAdviceIn) -> dict:
        rep = hook_retention.advise(body.results, body.platform, body.placement, body.metric)
        return {"platform": rep.platform, "placement": rep.placement, "metric": rep.metric, "note": rep.note,
                "ranked": [a.__dict__ for a in rep.ranked], "excluded": rep.excluded}

    @app.post("/zbm/memory/results", dependencies=auth)
    def zbm_memory(body: ZbmMemoryIn) -> dict:
        d = zbm.learn_result(body.result, body.brief_id)
        return {"learned": d.learned, "reason": d.reason}

    # --- ZBC -------------------------------------------------------------------------------------
    def _rb_summary(rb) -> dict:
        return {"campaign_id": rb.campaign_id, "version": rb.version, "status": rb.status.value,
                "supersedes_version": rb.supersedes_version, "rule_count": len(rb.rules),
                "retired_rule_count": rb.retired_rule_count, "rule_number_high_water": rb.rule_number_high_water,
                "blocking_issue_count": len(rb.blocking_issues), "drafted_by": rb.drafted_by,
                "approved_by": rb.approved_by, "signed_by": rb.signed_by,
                "signed_at": rb.signed_at.isoformat() if rb.signed_at else None,
                "live_at": rb.live_at.isoformat() if rb.live_at else None,
                "superseded_at": rb.superseded_at.isoformat() if rb.superseded_at else None}

    def _rb(campaign_id: str, version: int) -> dict:
        return zbc.rulebooks.get(campaign_id, version).model_dump(mode="json")

    def _rb_view(b: dict):
        try:
            return zbc.rulebooks.get(b["campaign_id"], b["version"]).model_dump(mode="json")
        except CreativeError:
            return None

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks", status_code=201, dependencies=auth)
    def zbc_draft(campaign_id: CampaignIdPath, body: DraftRulebookIn, response: Response,
                  who: str = Depends(authenticate_actor), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        if body.goal.campaign_id != campaign_id:
            raise ValidationFailed("campaign_id in path and goal differ", ["campaign_id"])
        actor = acting(body.actor_id, who)
        return creating(response, "rulebook", {"campaign_id": campaign_id}, actor, idempotency_key,
                        body.goal.model_dump(mode="json"),
                        lambda: zbc.draft_rulebook(body.goal, actor).model_dump(mode="json"), _rb_view)

    # Fix wave 9 (AEGIS round 8 L2): every version with every rule and every retired id made this GET
    # 30 MiB after 60 churn revisions. The list is now a page of version SUMMARIES; one version is read
    # with GET .../rulebooks/{version} (its rules and a retired-id count), and its retired ids a page at
    # a time with GET .../rulebooks/{version}/retired-rule-ids.
    @app.get("/zbc/campaigns/{campaign_id}/rulebooks", dependencies=auth)
    def zbc_versions(campaign_id: CampaignIdPath, offset: int = Query(0, ge=0, le=MAX_RULEBOOK_VERSION),
                     limit: int = Query(RULEBOOK_PAGE, ge=1, le=RULEBOOK_PAGE)) -> dict:
        versions = zbc.rulebooks.versions(campaign_id)
        page = versions[offset:offset + limit]
        return {"campaign_id": campaign_id, "total": len(versions), "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(versions) else None,
                "versions": [_rb_summary(rb) for rb in page]}

    @app.get("/zbc/campaigns/{campaign_id}/rulebooks/{version}/retired-rule-ids", dependencies=auth)
    def zbc_retired_ids(campaign_id: CampaignIdPath, version: VersionPath, offset: int = Query(0, ge=0),
                        limit: int = Query(RETIRED_PAGE, ge=1, le=RETIRED_PAGE)) -> dict:
        retired = zbc.rulebooks.get(campaign_id, version).retired_rule_ids
        total = len(retired)
        return {"campaign_id": campaign_id, "version": version, "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < total else None,
                "ids": list(retired[offset:offset + limit])}

    @app.get("/zbc/campaigns/{campaign_id}/rulebooks/{version}", dependencies=auth)
    def zbc_get_rb(campaign_id: CampaignIdPath, version: VersionPath) -> dict:
        return _rb(campaign_id, version)

    @app.put("/zbc/campaigns/{campaign_id}/rulebooks/{version}", dependencies=auth)
    def zbc_edit(campaign_id: CampaignIdPath, version: VersionPath, body: DraftRulebookIn, who: str = Depends(authenticate_actor)) -> dict:
        return zbc.edit_rulebook(campaign_id, version, body.goal, acting(body.actor_id, who)).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks/{version}/review", dependencies=auth)
    def zbc_review(campaign_id: CampaignIdPath, version: VersionPath, body: ActorIn, who: str = Depends(authenticate_actor)) -> dict:
        return zbc.review_rulebook(campaign_id, version, acting(body.actor_id, who)).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks/{version}/sign", dependencies=auth)
    def zbc_sign(campaign_id: CampaignIdPath, version: VersionPath, x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbc.sign_rulebook(campaign_id, version, x_andre_approval_token).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/rights-check", dependencies=auth)
    def zbc_rights(campaign_id: CampaignIdPath, body: RightsCheckIn) -> dict:
        return zbc.check_rights(campaign_id, body.assets, body.uses_ai_generative_fill).as_dict()

    @app.post("/zbc/campaigns/{campaign_id}/rulebooks/{version}/go-live", dependencies=auth)
    def zbc_go_live(campaign_id: CampaignIdPath, version: VersionPath) -> dict:
        return zbc.go_live(campaign_id, version).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/revisions", status_code=201, dependencies=auth)
    def zbc_revise(campaign_id: CampaignIdPath, body: DraftRulebookIn, response: Response,
                   who: str = Depends(authenticate_actor), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        actor = acting(body.actor_id, who)
        return creating(response, "revision", {"campaign_id": campaign_id}, actor, idempotency_key,
                        body.goal.model_dump(mode="json"),
                        lambda: zbc.revise_rulebook(campaign_id, body.goal, actor).model_dump(mode="json"), _rb_view)

    @app.post("/zbc/campaigns/{campaign_id}/moment-map", dependencies=auth)
    def zbc_moments(campaign_id: CampaignIdPath, body: SourceMaterial) -> dict:
        return zbc.build_moment_map(campaign_id, body).model_dump(mode="json")

    @app.post("/zbc/campaigns/{campaign_id}/hook-sheets", dependencies=auth)
    def zbc_hooks(campaign_id: CampaignIdPath) -> dict:
        return {"sheets": [s.model_dump(mode="json") for s in zbc.build_hook_sheets(campaign_id)]}

    @app.post("/zbc/campaigns/{campaign_id}/kit", status_code=201, dependencies=auth)
    def zbc_kit(campaign_id: CampaignIdPath, body: KitRequest, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        def view(b: dict):
            kit = zbc.kits.get(campaign_id)
            return kit.model_dump(mode="json") if kit is not None and kit.kit_id == b["kit_id"] else None

        return creating(response, "kit", {"campaign_id": campaign_id}, None, idempotency_key,
                        body.model_dump(mode="json"),
                        lambda: zbc.build_kit(campaign_id, body).model_dump(mode="json"), view)

    @app.post("/zbc/campaigns/{campaign_id}/kit/sign", dependencies=auth)
    def zbc_kit_sign(campaign_id: CampaignIdPath, x_andre_approval_token: str | None = Header(default=None)) -> dict:
        return zbc.sign_kit(campaign_id, x_andre_approval_token).model_dump(mode="json")

    @app.post("/zbc/clips", status_code=201, dependencies=auth)
    def zbc_submit(body: ClipSubmission, response: Response, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
        # the review runs off the workflow lock; the lock is taken to check and commit (fix wave 9, M1)
        return creating(response, "clip", {}, None, idempotency_key, body.model_dump(mode="json"),
                        lambda plan: zbc.submit_clip(body, plan).model_dump(mode="json"), caller_id=body.submission_id,
                        unlocked=lambda: zbc.review_clip_unlocked(body))

    @app.get("/zbc/clips/{submission_id}", dependencies=auth)
    def zbc_get_clip(submission_id: IdPath) -> dict:
        return zbc.get_decision(submission_id).model_dump(mode="json")

    @app.post("/zbc/clips/{submission_id}/human-review", dependencies=auth)
    def zbc_human(submission_id: IdPath, body: HumanReviewIn, who: str = Depends(authenticate_actor)) -> dict:
        verdict = HumanVerdict(outcome=body.outcome, broken_rules=body.broken_rules, note=body.note)
        return zbc.human_review(submission_id, acting(body.actor_id, who), verdict).model_dump(mode="json")

    @app.post("/zbc/clips/{submission_id}/human-review/withdraw", dependencies=auth)
    def zbc_human_withdraw(submission_id: IdPath, body: WithdrawVerdictIn,
                           who: str = Depends(authenticate_actor)) -> dict:
        return zbc.withdraw_uncertain_verdict(submission_id, acting(body.actor_id, who))

    @app.post("/zbc/clips/{submission_id}/payout-eligibility", dependencies=auth)
    def zbc_eligibility(submission_id: IdPath) -> dict:
        return zbc.payout_eligibility(submission_id)

    @app.post("/zbc/memory/results", dependencies=auth)
    def zbc_memory(body: ClipResult) -> dict:
        d = zbc.learn_result(body)
        return {"learned": d.learned, "reason": d.reason}

    @app.get("/zbc/memory/winners", dependencies=auth)
    def zbc_winners(vertical: str, platform: str) -> dict:
        return {"winners": [w.model_dump(mode="json") for w in zbc.memory.winners(vertical, platform)]}

    # after every route exists: the per-route member caps come from the routes' body models
    app.state.member_limits = route_member_limits(app)
    app.add_middleware(BodyLimit, limit=MAX_BODY_BYTES, member_limits=app.state.member_limits,
                       service_token=service_token)
    return app


def _app_from_env() -> FastAPI:
    token = _load_required_token()
    # Compliance (38): the HTTP client only when COMPLIANCE_SERVICE_URL, _TOKEN and
    # COMPLIANCE_CALLER_TOKEN are all set; otherwise the fail-closed stand-in.
    return build_app(
        service_token=token,
        ledger=ledger_from_env(),
        departments=Departments(compliance=compliance_from_env(dict(os.environ))),
        founder_token=os.environ.get("CREATIVE_ANDRE_APPROVAL_TOKEN"),
        actors=ActorRegistry.from_env(),
        superseded_grace_hours=grace_hours_from_env(),
        actor_tokens=actor_tokens_from_env(),
    )


app = _app_from_env()
