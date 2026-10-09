"""
REST surface for the Onboarding department (ADR 0004).

Same discipline as services/fulfillment-py and services/detection-py:
thin marshaling around the service layer, fail-closed bearer auth on every
route except /health (hmac.compare_digest wrapped in try/except TypeError,
so a non-ASCII token is a 401, never a 500), /docs, /redoc and
/openapi.json disabled, and ``python3 -m api`` binds 127.0.0.1 unless
ONBOARDING_BIND_ADDR says otherwise.

Honest defaults — every missing dependency is the fail-closed stand-in:
- ledger: HttpLedgerClient only if LEDGER_SERVICE_URL and
  LEDGER_SERVICE_TOKEN are both set; otherwise every action that needs a
  record is refused with 503 ``proceeded: false``.
- Revenue Recovery: HttpRevenueRecoveryClient only if DETECTION_SERVICE_URL
  and DETECTION_SERVICE_TOKEN are set; otherwise the audit answers 502.
- Compliance (38): ``integrations.compliance38.HttpComplianceDepartment`` only
  when COMPLIANCE_SERVICE_URL, COMPLIANCE_SERVICE_TOKEN and
  COMPLIANCE_CALLER_TOKEN are all set (any failure = not allowed).
- Compliance (38) otherwise, Verification and Integrity, Billing, ZBC payouts,
  handoff targets, Andre's push channel, live platform checks, the vault:
  all stand-ins that answer "not allowed yet" / "not wired".
- Contract storage: stand-in holding nothing, unless an operator opts in to
  ONBOARDING_CONTRACT_STORAGE=in_memory (local demos only; terms are lost
  on restart and it is NOT the decided storage location).

Errors never echo request input: 422 bodies carry only field locations and
messages (no ``input``/``ctx``), unhandled exceptions are logged by type
only, and every log record is scrubbed for credentials.

Hostile-input limits (fix wave 4, R1 — a quadratic credential scanner held
the whole process for minutes, /health included):
- ``InputLimits`` (outermost ASGI middleware): request target (path + query)
  over ``max_request_target_bytes`` -> 414; body over ``max_body_bytes``
  (1 MiB) -> 413, from Content-Length and again on the bytes received.
- Request bodies are validated in a SYNC dependency, i.e. in the threadpool,
  never on the event loop; field lengths are checked before any credential
  scan (``Inbound`` validates ``mode="after"``), and the scans of one body
  run under ``redaction.scan_budget`` — a budget of the request thread's OWN
  CPU time, proportional to the body size (fix wave 5, NEW-2: it was
  wall-clock, so concurrent benign bodies were refused for each other's
  work). A body not checked within it -> 422.
- Scanning concurrency (fix wave 5, NEW-2; weighted in fix wave 6, N4) is
  bounded by ``ScanAdmission``: every body takes cost proportional to its
  size from a shared in-flight budget, a bounded number wait a bounded time;
  busy -> 503 with Retry-After and ``proceeded: false``, never 422. Bodies
  of at most 16 KiB have a lane of their own (``ScanLanes``, fix wave 7,
  NEW-5): they never wait behind a large scan, and large bodies cannot
  fill their queue.
- The body must arrive within ``body_read_timeout_seconds`` -> 408; the
  request head is capped and timed by the hardened launcher (src/serve.py,
  fix wave 5, NEW-3), which ``python3 -m api`` uses.
- /health is ``async`` and does no work, so it answers from the event loop
  even while every worker thread is busy.
- Log lines are cut to a bounded prefix before they are scrubbed.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from config import load_config
from guardrails import OutboundBlocked
from integrations.compliance38 import compliance_from_env
from integrations.departments import Departments, InMemoryContractStorage
from integrations.revenue_recovery import HttpRevenueRecoveryClient, NotConfiguredRevenueRecovery
from intelligences import registry
from ledger import EvidenceLineOwed, HttpLedgerClient, LedgerWriteAfterEffects, LedgerWriteError, UnconfiguredLedgerClient
from store import LOCK_NAME, DataDirBusy, DataDirLock, RecordLog, StoreCorrupt
from onboarding_schema import AccessGrantIn, ClipperApplication
from onboarding_schema import requests as rq
from redaction import ScanBudgetExceeded, cap_text, install_log_scrubbing, scan_budget, scan_memo, scrub, scrub_obj
from service import OnboardingError, OnboardingService

install_log_scrubbing()
log = logging.getLogger("onboarding.api")


def _load_required_token() -> str:
    token = os.environ.get("ONBOARDING_SERVICE_TOKEN")
    if not token:
        raise RuntimeError(
            "ONBOARDING_SERVICE_TOKEN is not set. This service refuses to start without an auth "
            "token configured (fail closed, not open). Set ONBOARDING_SERVICE_TOKEN to a shared "
            "secret before starting onboarding-py."
        )
    return token


def make_require_auth(required_token: str) -> Callable:
    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or malformed Authorization header (expected: Bearer <token>)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = authorization.removeprefix("Bearer ")
        try:
            valid = hmac.compare_digest(supplied, required_token)
        except TypeError:
            # Same confirmed finding as fulfillment-py / detection-py: a
            # non-ASCII str makes compare_digest raise; that is an invalid
            # token (401), never an unauthenticated 500.
            valid = False
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token", headers={"WWW-Authenticate": "Bearer"}
            )

    return require_auth


class _ScrubbedResponses:
    """Wraps the service so every public method's result is scrubbed."""

    def __init__(self, service: OnboardingService):
        self._svc = service

    def __getattr__(self, name: str):
        attr = getattr(self._svc, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        def call(*a, **kw):
            return scrub_obj(attr(*a, **kw))

        return call


_HELD: dict = {}


def hold_data_dir(data_dir: str) -> DataDirLock:
    """The exclusive flock on ONBOARDING_DATA_DIR (bug sweep D, E-5/F-3; finance-py's ``hold_data_dir``), taken once
    per process before the log is opened and held for the life of the process. A second process on the same
    directory refuses to start; a second service instance in this process must win the single claim."""
    key = os.path.realpath(data_dir)
    lock = _HELD.get(key)
    if lock is None:
        try:
            lock = DataDirLock(data_dir)
        except DataDirBusy:
            raise RuntimeError(f"{data_dir}: another onboarding-py process holds this data directory (flock on "
                               f"{LOCK_NAME}); refusing to start. Stop the other process first: two writers would "
                               "fork the log") from None
        except StoreCorrupt as exc:
            raise RuntimeError(f"{data_dir}: {exc}") from None
        _HELD[key] = lock
    return lock


def refuse_shared_andre_key(andre_key: Optional[str], service_token: Optional[str]) -> None:
    """Bug sweep D (M): Andre's approval key must not be the service token. Every caller holding the token (every
    department that calls this service) could otherwise approve as Andre. Refuse to start."""
    if andre_key and service_token and hmac.compare_digest(andre_key.encode(), service_token.encode()):
        raise RuntimeError("ONBOARDING_ANDRE_APPROVAL_KEY equals ONBOARDING_SERVICE_TOKEN: every caller with the "
                           "service token could approve as Andre. Refusing to start; set a separate Andre key")


def build_service_from_env(env: dict | None = None) -> OnboardingService:
    env = dict(os.environ) if env is None else env
    refuse_shared_andre_key(env.get("ONBOARDING_ANDRE_APPROVAL_KEY"), env.get("ONBOARDING_SERVICE_TOKEN"))
    config = load_config(env)
    if env.get("LEDGER_SERVICE_URL") and env.get("LEDGER_SERVICE_TOKEN"):
        ledger = HttpLedgerClient(env["LEDGER_SERVICE_URL"], env["LEDGER_SERVICE_TOKEN"])
    else:
        ledger = UnconfiguredLedgerClient()
    if env.get("DETECTION_SERVICE_URL") and env.get("DETECTION_SERVICE_TOKEN"):
        rr = HttpRevenueRecoveryClient(env["DETECTION_SERVICE_URL"], env["DETECTION_SERVICE_TOKEN"])
    else:
        rr = NotConfiguredRevenueRecovery()
    depts = Departments()
    # Compliance (38): the HTTP client only when COMPLIANCE_SERVICE_URL, _TOKEN and
    # COMPLIANCE_CALLER_TOKEN are all set; otherwise the fail-closed stand-in.
    depts.compliance = compliance_from_env(env)
    if env.get("ONBOARDING_CONTRACT_STORAGE") == "in_memory":
        depts.contracts = InMemoryContractStorage()
    # Bug sweep D (D-1, E-5/F-3): the local evidence log. ONBOARDING_DATA_DIR unset = in memory (/health says so).
    data_dir = env.get("ONBOARDING_DATA_DIR") or None
    lock = hold_data_dir(data_dir) if data_dir else None
    token = lock.claim() if lock is not None else None
    try:
        log_ = RecordLog(data_dir)
        return OnboardingService(config, ledger, rr, departments=depts,
                                 andre_approval_key=env.get("ONBOARDING_ANDRE_APPROVAL_KEY") or None,
                                 log=log_, dir_lock=lock, lock_token=token)
    except BaseException:
        if lock is not None:
            lock.release_claim(token)
        raise


def _sanitize_validation_errors(errors: list[dict]) -> list[dict]:
    # Every piece is cut to a bounded prefix before it is scrubbed (a
    # location can be a client-chosen dict key of any length).
    out = []
    for e in errors[:50]:
        out.append({
            "loc": [scrub(cap_text(str(x), 128)) if isinstance(x, str) else x for x in list(e.get("loc", ()))[:16]],
            "msg": scrub(cap_text(str(e.get("msg", "")), 1024)),
            "type": e.get("type"),
        })
    return out


def _plain_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


class ServiceBusy(RuntimeError):
    """No scan budget within the queue limits: nothing was done."""

    def __init__(self, retry_after_s: int):
        super().__init__("busy")
        self.retry_after_s = retry_after_s


class ScanAdmission:
    """Weighted concurrency limiter on request-body validation and credential
    scanning (fix wave 6, N4; fix wave 5's ``HeavyScanGate`` was a step at
    64 KiB, so 40 concurrent 58 KB bodies bypassed it, filled the 40-thread
    pool and starved light GETs and /health of threads and the GIL).

    Every scanning request takes ``cost(nbytes) = min(max(nbytes, min_cost),
    budget)`` bytes out of a shared ``budget`` of in-flight scanned bytes and
    gives them back when its scan ends, so many medium bodies fill the budget
    exactly like one large one and a body larger than the budget takes all
    of it. The defaults (config.py) are set by measurement, not by throughput
    arithmetic: scanning is CPU-bound under one GIL, so running N scans at
    once gains nothing and every extra CPU-bound thread lengthens the event
    loop's wait for the GIL (40x16 KB bodies at 4 concurrent scans: /health
    p50 300 ms, light GET p50 790 ms; at 1 concurrent scan: 16 ms / 70 ms).

    At most ``max_waiting`` requests wait for budget, each at most ``wait_s``
    seconds; beyond that the request is refused as BUSY (503 + Retry-After,
    nothing done), never as a failed check (422). Waiters block a threadpool
    thread each, so ``max_waiting`` also keeps threads free for light
    requests (the pool has 40). Budget is granted to waiters oldest-first,
    skipping a waiter that does not fit yet (with a budget above the minimum
    cost a small body can therefore pass a queued large one; with the
    defaults every body takes the whole budget and they wait in order);
    each waiter is woken once, when its grant is made (never a thundering
    herd). Under sustained overload a max-size body can therefore reach its
    wait limit and be answered busy (retry later). Small bodies do not come
    here at all with the defaults: ``ScanLanes`` gives them a lane of their
    own (fix wave 7, NEW-5)."""

    class _Waiter:
        __slots__ = ("cost", "event", "granted")

        def __init__(self, cost: int):
            self.cost, self.event, self.granted = cost, threading.Event(), False

    def __init__(self, budget_bytes: int, min_cost_bytes: int, max_waiting: int, wait_s: float):
        self._lock = threading.Lock()
        self.budget = budget_bytes
        self.min_cost = min(min_cost_bytes, budget_bytes)
        self._available = budget_bytes
        self._waiters: list[ScanAdmission._Waiter] = []
        self._max_waiting = max_waiting
        self._wait_s = wait_s
        self.retry_after_s = max(1, min(30, math.ceil(wait_s / 4)))

    def cost(self, nbytes: int) -> int:
        return min(max(int(nbytes), self.min_cost), self.budget)

    @property
    def available(self) -> int:
        with self._lock:
            return self._available

    @property
    def waiting(self) -> int:
        with self._lock:
            return len(self._waiters)

    def _grant(self) -> None:
        # caller holds the lock: hand freed budget to the oldest waiters that fit
        still = []
        for w in self._waiters:
            if w.cost <= self._available:
                self._available -= w.cost
                w.granted = True
                w.event.set()
            else:
                still.append(w)
        self._waiters = still

    def _release(self, c: int) -> None:
        with self._lock:
            self._available += c
            self._grant()

    @contextmanager
    def hold(self, nbytes: int):
        c = self.cost(nbytes)
        with self._lock:
            if self._available >= c and not self._waiters:
                self._available -= c
                w = None
            else:
                if len(self._waiters) >= self._max_waiting:
                    raise ServiceBusy(self.retry_after_s)
                w = self._Waiter(c)
                self._waiters.append(w)
                self._grant()  # budget may already fit it (it was queued behind larger waiters)
        if w is not None and not w.event.wait(self._wait_s):
            with self._lock:
                if not w.granted:  # not granted in the meantime: give up the place
                    self._waiters.remove(w)
                    raise ServiceBusy(self.retry_after_s)
        try:
            yield c
        finally:
            self._release(c)

    # tests only: take / give back budget without a request
    def try_hold_for_test(self, nbytes: Optional[int] = None) -> bool:
        c = self.budget if nbytes is None else self.cost(nbytes)
        with self._lock:
            if self._available < c:
                return False
            self._available -= c
            self._held_for_test = getattr(self, "_held_for_test", []) + [c]
            return True

    def release_for_test(self) -> None:
        self._release(self._held_for_test.pop())


class ScanLanes:
    """Two ``ScanAdmission`` lanes (fix wave 7, NEW-5). With the measured
    defaults the large lane runs one scan at a time, so its queue is a line:
    one client's 416 KB bodies back-to-back put every other client's 40-byte
    message at p50 294 ms (4 uploaders 1.6 s, 12 uploaders 6 s), and once 16
    large bodies were queued a tiny message was 503. A body of at most
    ``scan_small_body_bytes`` (~1 ms of scan at 16 KiB) is admitted by the
    small lane instead: its own in-flight budget (``scan_small_inflight``
    bodies) and its own queue (``scan_small_max_waiting``), so it never
    waits for a large scan and no flood of large bodies can fill its queue.
    Large bodies stay serialized in the large lane. A small request does
    share the GIL with the large scan in flight, one switch interval per
    turn (1 ms under the launcher, src/serve.py; 5 ms is the interpreter's
    default), which is what remains of its latency beside a scan."""

    def __init__(self, cfg):
        self.large = ScanAdmission(cfg.scan_inflight_bytes, cfg.scan_min_cost_bytes, cfg.scan_max_waiting, cfg.scan_wait_seconds)
        self.small_bytes = cfg.scan_small_body_bytes
        self.small: Optional[ScanAdmission] = None
        if self.small_bytes > 0:
            self.small = ScanAdmission(self.small_bytes * cfg.scan_small_inflight, self.small_bytes,
                                       cfg.scan_small_max_waiting, cfg.scan_wait_seconds)

    def lane(self, nbytes: int) -> ScanAdmission:
        return self.small if self.small is not None and nbytes <= self.small_bytes else self.large

    def hold(self, nbytes: int):
        return self.lane(nbytes).hold(nbytes)


class InputLimits:
    """Outermost ASGI middleware (fix wave 4, R1): refuse an over-long
    request target (414) and an over-size body (413) before any route,
    parser or scanner sees them. The body cap is checked on Content-Length
    and again on the bytes actually received (chunked bodies too): the body
    is read here, at most ``max_body_bytes`` of it, and handed on in one
    piece — a body that goes past the cap is answered 413 at once, and the
    rest of it is never read."""

    # Request line + header block (fix wave 5, NEW-3). The launcher's h11
    # limit refuses a head that is still incomplete past 16 KiB (so a huge
    # head is never buffered), but a head that arrives whole in one socket
    # read (up to 256 KiB) is parsed; this check makes 16 KiB exact.
    MAX_HEAD_BYTES = 16 * 1024

    def __init__(self, app, max_body_bytes: int, max_target_bytes: int, body_timeout_s: float = 30.0):
        self.app = app
        self.max_body = max_body_bytes
        self.max_target = max_target_bytes
        self.body_timeout = body_timeout_s

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        target = len(scope.get("raw_path") or scope.get("path", "").encode("utf-8", "surrogatepass")) + len(scope.get("query_string") or b"")
        if target > self.max_target:
            return await _plain_response(414, f"request target longer than {self.max_target} bytes; refused")(scope, receive, send)
        head = target + sum(len(k) + len(v) + 4 for k, v in scope.get("headers") or ())
        if head > self.MAX_HEAD_BYTES:
            return await _plain_response(431, f"request head larger than {self.MAX_HEAD_BYTES} bytes; refused")(scope, receive, send)
        too_large = _plain_response(413, f"request body larger than {self.max_body} bytes; refused")
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                if not value.isdigit():
                    return await _plain_response(400, "invalid Content-Length")(scope, receive, send)
                if int(value) > self.max_body:
                    return await too_large(scope, receive, send)
        chunks, size = [], 0
        # The whole body must arrive within body_timeout (fix wave 5, NEW-3
        # sweep): a client trickling its body held the connection forever.
        deadline = time.monotonic() + self.body_timeout
        while True:
            try:
                message = await asyncio.wait_for(receive(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return await _plain_response(408, f"request body not received within {self.body_timeout:g}s; refused")(
                    scope, receive, send)
            if message["type"] != "http.request":  # client went away
                return
            chunk = message.get("body") or b""
            size += len(chunk)
            if size > self.max_body:
                return await too_large(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body, replayed = b"".join(chunks), False
        scope.setdefault("state", {})["onb_body_bytes"] = len(body)

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        with scan_memo():  # one verdict memo for the whole request (fix wave 7)
            await self.app(scope, replay, send)


def create_app(service: OnboardingService, required_token: str) -> FastAPI:
    app = FastAPI(
        title="ZBM/ZBC Onboarding",
        description="NON-LIVE: no platform, vault, push channel or other department is wired; stand-ins fail closed.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    refuse_shared_andre_key(getattr(service, "_andre_key", None), required_token)
    auth = [Depends(make_require_auth(required_token))]
    app.state.service = service
    cfg = service.config
    lanes = ScanLanes(cfg)
    app.state.scan_lanes = lanes
    app.state.scan_admission = lanes.large  # the large lane (tests hold its budget)
    app.state.scan_admission_small = lanes.small

    def cpu_budget(nbytes: int) -> float:
        return cfg.scan_budget_seconds + cfg.scan_cpu_ms_per_kb * (nbytes / 1024) / 1000

    def body(model: type[BaseModel], optional: bool = False) -> Callable:
        """The request body, validated IN THE THREADPOOL (a sync dependency;
        fix wave 4, R1). FastAPI validates declared body models on the event
        loop, so credential scanning there stalled every request, /health
        included. Field constraints run first, then the credential checks
        under the per-request CPU budget; every body first takes its share
        of the scan admission budget of its lane (fix wave 6, N4; lanes in
        fix wave 7, NEW-5)."""

        def parse(request: Request, payload: Any = Body(default=None)) -> Any:
            if payload is None and optional:
                yield None
                return
            nbytes = request.scope.get("state", {}).get("onb_body_bytes", 0)

            def validate() -> Any:
                with scan_budget(cpu_budget(nbytes)):
                    try:
                        return model.model_validate(payload)
                    except ValidationError as exc:
                        raise RequestValidationError(
                            [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
                        ) from None

            lane = lanes.lane(nbytes)
            with lane.hold(nbytes):
                parsed = validate()
                if lane is lanes.large:
                    # A large body's heavy work is not only its check: the
                    # service redacts the same text (~1.5 s of CPU for 416 KB
                    # of profile fields) and the response is scrubbed. The
                    # large lane is held through the handler (this is a
                    # generator dependency: its exit runs after the response),
                    # so that work stays one-at-a-time too (fix wave 7,
                    # NEW-5 class). The small lane covers only the check.
                    yield parsed
                    return
            yield parsed

        return parse
    # Output-side scrub (third credential layer, F10): every route's return
    # value passes through scrub_obj on its way out.
    svc = _ScrubbedResponses(service)

    # Exception handlers are SYNC (fix wave 4, R1): Starlette runs them in the
    # threadpool, so their scrubbing never runs on the event loop either.
    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _sanitize_validation_errors(exc.errors())})

    @app.exception_handler(ScanBudgetExceeded)
    def _scan_budget(_: Request, exc: ScanBudgetExceeded):
        log.warning("request body refused: credential checks exceeded their CPU budget")
        return JSONResponse(status_code=422, content={"detail": [{
            "loc": ["body"], "type": "scan_budget_exceeded",
            "msg": "the request could not be checked for credentials within its CPU budget, so it was not accepted"}]})

    @app.exception_handler(ServiceBusy)
    def _busy(_: Request, exc: ServiceBusy):
        log.warning("request refused: busy (scan admission queue full or wait limit reached)")
        return JSONResponse(status_code=503, headers={"Retry-After": str(exc.retry_after_s)}, content={
            "detail": "the service is busy checking other requests; nothing was done — retry the identical "
                      f"request after {exc.retry_after_s}s",
            "proceeded": False,
        })

    @app.exception_handler(OnboardingError)
    def _onboarding(_: Request, exc: OnboardingError):
        return JSONResponse(status_code=exc.status_code, content=scrub_obj({"detail": exc.detail, **exc.body}))

    # Fix wave 5 (NEW-4): a ledger write whose outcome is UNKNOWN (reply lost
    # after sending, 5xx other than 503, 409) is never reported as "did not
    # proceed". Event ids are deterministic, so retrying the identical
    # request is safe: an event the ledger already holds is its 200, and the
    # staged state was not committed, so the retry finishes the operation.
    retry_identical = ("retry the identical request (same body, same path): the event ids are deterministic, so "
                       "anything the ledger already recorded is recognised and not recorded twice, and the "
                       "operation is finished by the retry")

    def _ledger_outcome(exc: LedgerWriteError) -> dict:
        if exc.outcome == "unknown":
            return {"ledger_write": "unknown", "retry": exc.retry_hint or retry_identical}
        return {"ledger_write": "not_recorded"}

    @app.exception_handler(EvidenceLineOwed)
    def _evidence_owed(_: Request, exc: EvidenceLineOwed):
        # Bug sweep D (D-1): the action TOOK EFFECT (state applied, ledger events written); only the local line that
        # names its evidence is owed. Never "did not proceed", and never "retry" (a repeat would act twice).
        log.error("evidence line owed (%s)", exc.outcome)
        return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content={
            "detail": ("the action took effect; the local evidence line naming its ledger records could not be "
                       "written yet and is written before the next action (until then its evidence reads "
                       "'attempted' in /onboarding/audit/evidence)"),
            "proceeded": True, "completed": True, "evidence": "pending", "outside_effects_done": list(exc.effects),
            "ledger_write": exc.outcome, "retry": "do not repeat this action; it took effect",
        })

    @app.exception_handler(LedgerWriteAfterEffects)
    def _ledger_after_effects(_: Request, exc: LedgerWriteAfterEffects):
        # Honest partial result: outside effects already happened (each was
        # recorded before it was made); the record of a later step failed
        # (or its fate is unknown), so nothing further happened. Never "did
        # not proceed".
        log.error("ledger write failed (%s) after outside effects %s; stopped", exc.outcome, exc.effects)
        record = "may or may not have been recorded" if exc.outcome == "unknown" else "failed"
        content = {
            "detail": (f"an evidence ledger write {record} ({scrub(str(exc))}) after these outside effects had "
                       f"already happened: {', '.join(exc.effects)}; nothing further was done"),
            "proceeded": True,
            "completed": False,
            "outside_effects_done": list(exc.effects),
            **_ledger_outcome(exc),
        }
        content.setdefault("retry", retry_identical)
        return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content=content)

    @app.exception_handler(LedgerWriteError)
    def _ledger(_: Request, exc: LedgerWriteError):
        if exc.outcome == "unknown":
            log.error("ledger write outcome unknown; action not completed: %s", exc)
            return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content={
                "detail": (f"the evidence ledger write may or may not have been recorded ({scrub(str(exc))}); "
                           "the action was not completed here and it is not known whether its record exists"),
                "proceeded": "unknown",
                **_ledger_outcome(exc),
            })
        log.error("ledger write failed; action refused: %s", exc)
        return JSONResponse(status_code=503, content={
            "detail": f"evidence ledger write failed ({scrub(str(exc))}); the action did not proceed",
            "proceeded": False,
            **_ledger_outcome(exc),
        })

    @app.exception_handler(OutboundBlocked)
    def _outbound(_: Request, exc: OutboundBlocked):
        return JSONResponse(status_code=409, content={"detail": "client-facing text blocked by a guardrail; nothing was sent",
                                                      "rule": scrub(str(exc))})

    @app.middleware("http")
    async def _unhandled(request: Request, call_next):
        # A middleware, not an Exception handler: Starlette re-raises after
        # running a 500 handler, and the server would then log the full
        # traceback — whose message may contain input. Here the exception
        # stops, and only its TYPE is logged.
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            log.error("unhandled error: %s", type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.add_middleware(InputLimits, max_body_bytes=cfg.max_body_bytes, max_target_bytes=cfg.max_request_target_bytes,
                       body_timeout_s=cfg.body_read_timeout_seconds)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "onboarding-py", "data_source": "non-live", **service.store_health()}

    @app.get("/onboarding/audit/evidence", dependencies=auth)
    def audit_evidence(limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0, le=10_000_000),
                       event_type: Optional[str] = Query(default=None, pattern=r"^[a-z0-9_]{1,64}$")) -> dict:
        """Bug sweep D (D-1): every Onboarding event on the ledger, committed (named by an anchored local line) or
        attempted (recorded first, never committed)."""
        return svc.audit_evidence(limit, offset, event_type)

    @app.get("/intelligences", dependencies=auth)
    def intelligences() -> list[dict]:
        return registry()

    # --- client lane (and ZBC brand lane front end) ---------------------------------

    @app.post("/onboarding/clients", dependencies=auth, status_code=201)
    def start(req: rq.StartClientRequest = Depends(body(rq.StartClientRequest))) -> dict:
        return svc.start_client(req)

    @app.get("/onboarding/clients/{client_id}", dependencies=auth)
    def view(client_id: str) -> dict:
        return svc.view(client_id)

    @app.post("/onboarding/clients/{client_id}/intake/facts", dependencies=auth)
    def facts(client_id: str, req: rq.FactsRequest = Depends(body(rq.FactsRequest))) -> dict:
        return svc.add_facts(client_id, req)

    @app.post("/onboarding/clients/{client_id}/intake/documents", dependencies=auth)
    def documents(client_id: str, req: rq.DocumentRequest = Depends(body(rq.DocumentRequest))) -> dict:
        return svc.add_document(client_id, req)

    @app.post("/onboarding/clients/{client_id}/messages", dependencies=auth)
    def message(client_id: str, req: rq.MessageRequest = Depends(body(rq.MessageRequest))) -> dict:
        return svc.message(client_id, req)

    @app.post("/onboarding/clients/{client_id}/recap", dependencies=auth)
    def recap(client_id: str) -> dict:
        return svc.recap(client_id)

    @app.post("/onboarding/clients/{client_id}/access/website-scan", dependencies=auth)
    def website_scan(client_id: str, req: rq.WebsiteScanRequest = Depends(body(rq.WebsiteScanRequest))) -> dict:
        return svc.website_scan(client_id, req)

    @app.post("/onboarding/clients/{client_id}/access/grants", dependencies=auth)
    def grant(client_id: str, req: AccessGrantIn = Depends(body(AccessGrantIn))) -> dict:
        return svc.add_grant(client_id, req)

    @app.post("/onboarding/clients/{client_id}/access/credentials", dependencies=auth)
    def credentials(client_id: str) -> Any:
        # The request body is deliberately never read or parsed.
        return svc.offer_credential(client_id)

    @app.post("/onboarding/clients/{client_id}/audit", dependencies=auth)
    def audit(client_id: str, req: rq.AuditRequest = Depends(body(rq.AuditRequest))) -> dict:
        return svc.audit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/plan", dependencies=auth)
    def plan(client_id: str, req: rq.PlanRequest = Depends(body(rq.PlanRequest))) -> dict:
        return svc.plan(client_id, req)

    @app.post("/onboarding/clients/{client_id}/plan/choices", dependencies=auth)
    def plan_choice(client_id: str, req: rq.PlanChoiceRequest = Depends(body(rq.PlanChoiceRequest))) -> dict:
        return svc.plan_choice(client_id, req)

    @app.post("/onboarding/clients/{client_id}/setup-plan", dependencies=auth)
    def setup_plan(client_id: str) -> dict:
        return svc.setup_plan(client_id)

    @app.post("/onboarding/clients/{client_id}/account-changes", dependencies=auth)
    def account_change(client_id: str, req: rq.AccountChangeRequest = Depends(body(rq.AccountChangeRequest))) -> dict:
        return svc.account_change(client_id, req)

    @app.post("/onboarding/clients/{client_id}/momentum", dependencies=auth)
    def momentum(client_id: str) -> dict:
        return svc.momentum(client_id)

    @app.post("/onboarding/clients/{client_id}/first-win", dependencies=auth)
    def first_win(client_id: str, req: rq.FirstWinRequest = Depends(body(rq.FirstWinRequest))) -> dict:
        return svc.first_win(client_id, req)

    @app.post("/onboarding/clients/{client_id}/recommend-score", dependencies=auth)
    def recommend(client_id: str, req: rq.RecommendScoreRequest = Depends(body(rq.RecommendScoreRequest))) -> dict:
        return svc.recommend_score_submit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/tick", dependencies=auth)
    def tick(client_id: str) -> dict:
        return svc.tick(client_id)

    @app.post("/onboarding/clients/{client_id}/issues/{issue_id}/outcome", dependencies=auth)
    def issue_outcome(client_id: str, issue_id: str, req: rq.IssueOutcomeRequest = Depends(body(rq.IssueOutcomeRequest))) -> dict:
        return svc.issue_outcome(client_id, issue_id, req)

    @app.post("/onboarding/clients/{client_id}/escalations/{escalation_id}/acknowledge", dependencies=auth)
    def ack(client_id: str, escalation_id: str,
            req: Optional[rq.EscalationAckRequest] = Depends(body(rq.EscalationAckRequest, optional=True))) -> dict:
        # Andre-only: needs his approval token in the body; the shared
        # service token alone is 403 (F4).
        return svc.acknowledge_escalation(client_id, escalation_id, req)

    @app.post("/onboarding/clients/{client_id}/escalations/{escalation_id}/resolve", dependencies=auth)
    def resolve(client_id: str, escalation_id: str, req: rq.EscalationResolveRequest = Depends(body(rq.EscalationResolveRequest))) -> dict:
        return svc.resolve_escalation(client_id, escalation_id, req)

    @app.get("/onboarding/clients/{client_id}/health", dependencies=auth)
    def client_health(client_id: str) -> dict:
        return svc.health(client_id)

    @app.delete("/onboarding/clients/{client_id}/memory", dependencies=auth)
    def delete_memory(client_id: str) -> dict:
        return svc.delete_memory(client_id)

    @app.post("/onboarding/clients/{client_id}/exit", dependencies=auth)
    def exit_(client_id: str, req: rq.ExitRequest = Depends(body(rq.ExitRequest))) -> dict:
        return svc.exit(client_id, req)

    @app.post("/onboarding/clients/{client_id}/activate", dependencies=auth)
    def activate(client_id: str) -> dict:
        return svc.activate_client(client_id)

    @app.get("/onboarding/escalations", dependencies=auth)
    def escalations() -> list[dict]:
        return svc.list_escalations()

    # --- ZBC creator lane ---------------------------------------------------------------

    @app.post("/zbc/creators/applications", dependencies=auth, status_code=201)
    def apply(req: ClipperApplication = Depends(body(ClipperApplication))) -> dict:
        return svc.apply_creator(req)

    @app.post("/zbc/creators/{creator_id}/w9", dependencies=auth)
    def w9(creator_id: str, req: rq.CreatorFlagRequest = Depends(body(rq.CreatorFlagRequest))) -> dict:
        return svc.creator_flag(creator_id, "w9", req)

    @app.post("/zbc/creators/{creator_id}/disclosure-training", dependencies=auth)
    def training(creator_id: str, req: rq.CreatorFlagRequest = Depends(body(rq.CreatorFlagRequest))) -> dict:
        return svc.creator_flag(creator_id, "disclosure_training", req)

    @app.post("/zbc/creators/{creator_id}/activate", dependencies=auth)
    def activate_creator(creator_id: str) -> dict:
        return svc.activate_creator(creator_id)

    @app.post("/zbc/creators/{creator_id}/payments", dependencies=auth)
    def payment(creator_id: str, req: rq.CreatorPaymentRequest = Depends(body(rq.CreatorPaymentRequest))) -> dict:
        return svc.creator_payment(creator_id, req)

    @app.post("/zbc/creators/{creator_id}/posts/check", dependencies=auth)
    def post_check(creator_id: str, req: rq.CaptionRequest = Depends(body(rq.CaptionRequest))) -> dict:
        return svc.creator_post_check(creator_id, req)

    # --- ZBC brand lane -------------------------------------------------------------------

    @app.post("/zbc/brands/{brand_id}/campaigns", dependencies=auth, status_code=201)
    def campaign(brand_id: str, req: rq.CampaignRequest = Depends(body(rq.CampaignRequest))) -> dict:
        return svc.plan_campaign(brand_id, req)

    @app.post("/zbc/brands/{brand_id}/campaigns/{campaign_id}/approve", dependencies=auth)
    def approve(brand_id: str, campaign_id: str, req: rq.CampaignApproveRequest = Depends(body(rq.CampaignApproveRequest))) -> dict:
        return svc.approve_campaign(brand_id, campaign_id, req)

    @app.post("/zbc/brands/{brand_id}/campaigns/{campaign_id}/proving-result", dependencies=auth)
    def proving(brand_id: str, campaign_id: str, req: rq.ProvingResultRequest = Depends(body(rq.ProvingResultRequest))) -> dict:
        return svc.proving_result(brand_id, campaign_id, req)

    # --- playbook + learning loop ------------------------------------------------------------

    @app.get("/playbook", dependencies=auth)
    def playbook() -> dict:
        return svc.playbook_view()

    @app.post("/playbook/rules", dependencies=auth)
    def playbook_rule(req: rq.PlaybookRuleRequest = Depends(body(rq.PlaybookRuleRequest))) -> dict:
        return svc.change_playbook(req)

    @app.get("/learning/proposals", dependencies=auth)
    def proposals() -> dict:
        return svc.propose_rules()

    return app


_REQUIRED_TOKEN = _load_required_token()
app = create_app(build_service_from_env(), _REQUIRED_TOKEN)


def main() -> None:
    # Fix wave 5 (NEW-3): the hardened launcher (h11, 16 KiB request-head
    # cap, request-head deadline, keep-alive timeout, limit_concurrency).
    # Default uvicorn (httptools) buffered a 100-200 MB header and never
    # closed idle or half-sent connections.
    import serve

    host = os.environ.get("ONBOARDING_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("ONBOARDING_PORT", "8200"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
