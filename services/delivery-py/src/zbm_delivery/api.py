"""
REST surface for Client Delivery & Operations (28) (spec §D, ADR 0011). Exactly the finance-py discipline (the
shared blocks below are copied from finance-py's api.py, itself verification-py's):
- fail-closed bearer auth on every route except /health; the service refuses to start without DLV_SERVICE_TOKEN;
  ``hmac.compare_digest`` wrapped in ``try/except TypeError`` (a non-ASCII token is a 401, never a 500);
- caller identity from ``X-DLV-Caller-Token`` (DLV_CALLER_TOKENS: aegis, andre_session, scheduler; SHA-256 digests
  compared against EVERY configured token, no early exit); a wrong or absent caller token on a route that needs
  one is a 403; the principal of a run is derived from it (there is no body field, §C.5);
- ``X-Andre-Approval-Token`` (FounderGate) on the reconcile route only (§C.3.3); a refusal is a 403 recorded as
  ``founder_approval_refused``;
- /docs, /redoc, /openapi.json disabled; bind 127.0.0.1 unless DLV_BIND_ADDR says otherwise; hardened launcher;
- request limits before any route: target 4 KiB (414), head 16 KiB (431), per-route body caps inside the 1 MiB cap
  (413), JSON content type only (415), JSON depth and member count bounded (422), body read deadline (408);
- every POST answer echoes ``request_id`` and ``facts_sha256``; every answer carries ``policy_version`` and
  ``prompts_manifest_sha256``.
This module imports nothing from ``deerflow`` (spec §D); the harness is wired in ``build_service``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from zbm_delivery import config as config_mod
from zbm_delivery import models as m
from zbm_delivery.clock import Clock, SystemClock
from zbm_delivery.errors import DlvError, Forbidden, FounderRefused, Invalid, Unavailable
from zbm_delivery.founder import FounderGate
from zbm_delivery.ledger import HttpLedgerClient, Recorder, UnconfiguredLedgerClient
from zbm_delivery.service import DeliveryService
from zbm_delivery.store import RecordLog

log = logging.getLogger("delivery.api")

CALLER_HEADER = "X-DLV-Caller-Token"
FOUNDER_HEADER = "X-Andre-Approval-Token"
MAX_BODY_BYTES = 1024 * 1024
MAX_TARGET_BYTES = 4096
MAX_HEAD_BYTES = 16 * 1024
BODY_READ_TIMEOUT_S = 30.0
MAX_JSON_DEPTH = 32
MAX_JSON_MEMBERS = 20_000
ERROR_MAX_ERRORS = 20
ERROR_MAX_STR = 120
EVIDENCE_MAX = 1024 * 1024

ROUTE_LIMITS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"^/dlv/v1/fix-runs$"), 1024 * 1024),                     # up to 200 findings x 4 KB fields
    (re.compile(r"^/dlv/v1/fix-runs/[A-Za-z0-9\-]+/review$"), 1024 * 1024),
    (re.compile(r"^/dlv/v1/fix-runs/[A-Za-z0-9\-]+/cancel$"), 8 * 1024),
    (re.compile(r"^/dlv/v1/reconcile$"), 1024 * 1024),
]
DEFAULT_ROUTE_LIMIT = 16 * 1024
_RUN_ID = re.compile(r"^dlv-run-[0-9A-HJKMNP-TV-Z]{26}$")
_EV_ID = re.compile(r"^dlv-ev-[0-9a-f]{26}$")
EVIDENCE_CONTENT_TYPES = {"brief": "text/markdown; charset=utf-8", "report": "text/markdown; charset=utf-8",
                          "diff": "text/x-diff; charset=utf-8", "review": "application/json"}


def route_limit(path: str) -> int:
    for rx, limit in ROUTE_LIMITS:
        if rx.match(path):
            return limit
    return DEFAULT_ROUTE_LIMIT


def _plain(status_code: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, **extra})


def json_shape_problem(body: bytes) -> Optional[str]:
    depth = members = 0
    in_str = esc = False
    for b in body:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
            continue
        if b == 0x22:
            in_str = True
        elif b in (0x7B, 0x5B):
            depth += 1
            members += 1
            if depth > MAX_JSON_DEPTH:
                return f"JSON nested deeper than {MAX_JSON_DEPTH}"
        elif b in (0x7D, 0x5D):
            depth -= 1
        elif b == 0x2C:
            members += 1
            if members > MAX_JSON_MEMBERS:
                return f"JSON with more than {MAX_JSON_MEMBERS} members"
    return None


def is_json_content_type(value: Optional[bytes]) -> bool:
    if not value:
        return False
    main = value.split(b";", 1)[0].strip().lower()
    return main == b"application/json" or (main.startswith(b"application/") and main.endswith(b"+json"))


class InputLimits:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        target = len(scope.get("raw_path") or path.encode("utf-8", "surrogatepass")) + len(scope.get("query_string") or b"")
        if target > MAX_TARGET_BYTES:
            return await _plain(414, f"request target longer than {MAX_TARGET_BYTES} bytes; refused")(scope, receive, send)
        headers = scope.get("headers") or ()
        if target + sum(len(k) + len(v) + 4 for k, v in headers) > MAX_HEAD_BYTES:
            return await _plain(431, f"request head larger than {MAX_HEAD_BYTES} bytes; refused")(scope, receive, send)
        limit = min(route_limit(path), MAX_BODY_BYTES)
        too_large = _plain(413, f"request body larger than {limit} bytes for this route; refused")
        ctype = None
        declared = None
        chunked = False
        for name, value in headers:
            if name == b"content-length":
                if not value.isdigit():
                    return await _plain(400, "invalid Content-Length")(scope, receive, send)
                declared = int(value)
                if declared > limit:
                    return await too_large(scope, receive, send)
            elif name == b"content-type":
                ctype = value
            elif name == b"transfer-encoding" and b"chunked" in value.lower():
                chunked = True
        if (declared or chunked) and not is_json_content_type(ctype):
            return await _plain(415, "request bodies must be application/json")(scope, receive, send)
        chunks, size = [], 0
        deadline = time.monotonic() + BODY_READ_TIMEOUT_S
        while True:
            try:
                message = await asyncio.wait_for(receive(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return await _plain(408, "request body not received in time; refused")(scope, receive, send)
            if message["type"] != "http.request":
                return
            chunk = message.get("body") or b""
            size += len(chunk)
            if size > limit:
                return await too_large(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        if body:
            if not is_json_content_type(ctype):
                return await _plain(415, "request bodies must be application/json")(scope, receive, send)
            problem = await run_in_threadpool(json_shape_problem, body)
            if problem:
                return await _plain(422, f"request body refused: {problem}")(scope, receive, send)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def make_require_auth(required_token: str) -> Callable:
    def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="missing or malformed Authorization header (expected: Bearer <token>)",
                                headers={"WWW-Authenticate": "Bearer"})
        supplied = authorization.removeprefix("Bearer ")
        try:
            valid = hmac.compare_digest(supplied, required_token)
        except TypeError:
            valid = False
        if not valid:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token",
                                headers={"WWW-Authenticate": "Bearer"})

    return require_auth


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).digest()


class Callers:
    def __init__(self, tokens: dict[str, str]):
        self._digests = {name: _digest(tok) for name, tok in tokens.items()}

    def identify(self, supplied: Optional[str]) -> Optional[str]:
        if not supplied:
            return None
        d = _digest(supplied)
        found = None
        for name, dg in self._digests.items():
            if hmac.compare_digest(d, dg):
                found = name
        return found


def _sanitize(errors: list[dict]) -> dict:
    out = []
    for e in errors[:ERROR_MAX_ERRORS]:
        loc = [str(x)[:ERROR_MAX_STR] if isinstance(x, str) else x for x in list(e.get("loc", ()))[:8]]
        out.append({"loc": loc, "msg": str(e.get("msg", ""))[:ERROR_MAX_STR], "type": e.get("type")})
    return {"detail": out, "errors_total": len(errors)}


def _run_id(value: str) -> str:
    if not _RUN_ID.fullmatch(value or ""):
        raise Invalid("run id format")
    return value


def create_app(service: DeliveryService, settings: config_mod.Settings) -> FastAPI:
    app = FastAPI(title="ZBM/ZBC Client Delivery & Operations (28)",
                  description="The AEGIS fix engine. NON-LIVE unless a Docker daemon, a provider key and the ledger are wired.",
                  version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    auth = [Depends(make_require_auth(settings.service_token))]
    callers = Callers(settings.caller_tokens)
    founder = FounderGate.build(settings.andre_token, settings.service_token, list(settings.caller_tokens.values()))
    app.state.service = service
    svc = service

    def caller(*allowed: str) -> Callable:
        def dep(x_dlv_caller_token: Optional[str] = Header(default=None)) -> str:
            name = callers.identify(x_dlv_caller_token)
            if name is None:
                raise Forbidden("caller token missing or not recognised")
            if allowed and name not in allowed:
                raise Forbidden("this caller is not authorized for this route")
            return name
        return dep

    def andre(route: str) -> Callable:
        def dep(x_andre_approval_token: Optional[str] = Header(default=None)) -> str:
            try:
                founder.verify(x_andre_approval_token)
            except FounderRefused as exc:
                svc.founder_refused(route, exc.reason)
                raise
            return "andre"
        return dep

    def body(model: type[BaseModel]) -> Callable:
        def parse(payload: Any = Body(default=None)) -> Any:
            if payload is None:
                raise RequestValidationError([{"loc": ("body",), "msg": "a JSON body is required", "type": "missing"}])
            try:
                return model.model_validate(payload)
            except ValidationError as exc:
                raise RequestValidationError(
                    [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors(include_url=False, include_input=False)]
                ) from None
        return parse

    def dump(x: BaseModel) -> dict:
        return json.loads(x.model_dump_json())

    def within_max_findings(n: int, field: str) -> None:
        """Spec D12 / §B.1 ("1..`DLV_MAX_FINDINGS` findings", strict schema → 422). Wave 25 (E-B): the setting was
        parsed and read by nothing — the request models' fixed cap of 200 per list was the only one, so a lower
        DLV_MAX_FINDINGS admitted more, and a failing review could open a run of up to 400 (200 reopened + 200
        new). A run over the cap is the same 422 schema answer an over-long list gets, before anything is recorded."""
        if n > settings.max_findings:
            raise RequestValidationError([{
                "loc": ("body", field), "type": "too_long",
                "msg": f"a run holds at most {settings.max_findings} findings (DLV_MAX_FINDINGS); this request makes {n}"}])

    @app.exception_handler(RequestValidationError)
    def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=_sanitize(exc.errors()))

    @app.exception_handler(DlvError)
    def _domain(_: Request, exc: DlvError):
        content = {"detail": exc.reason, **exc.body}
        if isinstance(exc, Unavailable):
            content["took_effect"] = False
            return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content=content)
        if exc.status_code == 503:
            content.setdefault("took_effect", False)
            return JSONResponse(status_code=503, headers={"Retry-After": "1"}, content=content)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.middleware("http")
    async def _unhandled(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - only the TYPE is logged (never input, never a secret)
            log.error("unhandled error: %s", type(exc).__name__)
            return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.add_middleware(InputLimits)

    @app.get("/health")
    async def health() -> dict:
        return svc.health()

    @app.post("/dlv/v1/fix-runs", dependencies=auth, status_code=202)
    def fix_runs(who: str = Depends(caller("aegis", "andre_session")),
                 req: m.FindingsDocument = Depends(body(m.FindingsDocument))) -> dict:
        # wave 26b (N25-D-4): the cap applies to a NEW request only — after the idempotency lookup (a recorded request
        # replays its recorded answer even if DLV_MAX_FINDINGS was lowered since), before anything is recorded
        return svc.create_fix_run(who, dump(req), cap_check=lambda: within_max_findings(len(req.findings), "findings"))

    @app.get("/dlv/v1/fix-runs/{run_id}", dependencies=auth)
    def fix_run(run_id: str, _: str = Depends(caller())) -> dict:
        return svc.run_view(_run_id(run_id))

    @app.get("/dlv/v1/fix-runs/{run_id}/findings", dependencies=auth)
    def findings(run_id: str, _: str = Depends(caller())) -> dict:
        return svc.findings_view(_run_id(run_id))

    @app.get("/dlv/v1/fix-runs/{run_id}/report", dependencies=auth)
    def report(run_id: str, _: str = Depends(caller())):
        text = svc.report_text(_run_id(run_id))
        if len(text.encode("utf-8")) > EVIDENCE_MAX:
            raise Invalid("report larger than 1 MiB; fetch it as evidence")
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                                 headers={"X-DLV-Policy-Version": str(config_mod.POLICY_VERSION),
                                          "X-DLV-Prompts-Manifest-SHA256": svc.gate.prompts_manifest_sha256})

    @app.get("/dlv/v1/fix-runs/{run_id}/evidence/{evidence_id}", dependencies=auth)
    def evidence(run_id: str, evidence_id: str, _: str = Depends(caller())):
        if not _EV_ID.fullmatch(evidence_id or ""):
            raise Invalid("evidence id format")
        meta = svc.evidence_meta(_run_id(run_id), evidence_id)
        data = svc.evidence_read(run_id, evidence_id)
        if len(data) > EVIDENCE_MAX:
            data = data[:EVIDENCE_MAX]
        return Response(content=data, media_type=EVIDENCE_CONTENT_TYPES.get(meta["kind"], "text/plain; charset=utf-8"),
                        headers={"X-DLV-Evidence-Kind": meta["kind"], "X-DLV-Evidence-SHA256": meta["sha256"],
                                 "X-DLV-Policy-Version": str(config_mod.POLICY_VERSION),
                                 "X-DLV-Prompts-Manifest-SHA256": svc.gate.prompts_manifest_sha256})

    @app.post("/dlv/v1/fix-runs/{run_id}/review", dependencies=auth)
    def review(run_id: str, who: str = Depends(caller("aegis")), req: m.ReviewRequest = Depends(body(m.ReviewRequest))) -> dict:
        # the run a fail opens; after the idempotency lookup, as for fix-runs (wave 26b, N25-D-4)
        return svc.review(who, _run_id(run_id), dump(req),
                          cap_check=lambda: within_max_findings(len(req.reopened) + len(req.new_findings), "new_findings"))

    @app.post("/dlv/v1/fix-runs/{run_id}/cancel", dependencies=auth)
    def cancel(run_id: str, who: str = Depends(caller("aegis", "andre_session")),
               req: m.CancelRequest = Depends(body(m.CancelRequest))) -> dict:
        return svc.cancel(who, _run_id(run_id), dump(req))

    @app.get("/dlv/v1/policy", dependencies=auth)
    def policy(_: str = Depends(caller())) -> dict:
        return svc.policy_view()

    @app.get("/dlv/v1/audit/export", dependencies=auth)
    def audit(who: str = Depends(caller()), since: Optional[str] = Query(default=None, max_length=40),
              until: Optional[str] = Query(default=None, max_length=40),
              cursor: int = Query(default=0, ge=0, le=10**12)) -> dict:
        try:
            return svc.audit_export(who, since, until, cursor)
        except ValueError:
            raise Invalid("since/until must be RFC 3339 timestamps with offset") from None

    @app.get("/dlv/v1/reconcile", dependencies=auth)
    def reconcile_plan(_: str = Depends(andre("reconcile"))) -> dict:
        return svc.reconcile_plan()

    @app.post("/dlv/v1/reconcile", dependencies=auth)
    def reconcile(_: str = Depends(andre("reconcile")), req: m.ReconcileRequest = Depends(body(m.ReconcileRequest))) -> dict:
        return svc.reconcile(req.request_id, req.head_sha256, list(req.void_lines), list(req.void_event_ids))

    return app


def build_service(settings: config_mod.Settings, env: Optional[dict] = None, *, clock: Optional[Clock] = None,
                  ledger=None, docker=None, chat_backend=None, git=None, harness_factory=None, provider_factory=None,
                  gate_report=None, docker_available=None, wire_harness: bool = True) -> DeliveryService:
    """Production wiring: the gate runs first; every port is its fail-closed stand-in unless configured."""
    from zbm_delivery import gate as gate_mod
    from zbm_delivery.adapters import sandbox as sandbox_mod
    from zbm_delivery.adapters.egress import EgressClient
    from zbm_delivery.adapters.model import backend_from_settings
    from zbm_delivery.engine.brief import load_prompts
    from zbm_delivery.gitport import GitPort
    from zbm_delivery.ports import NotWiredVault

    env = dict(os.environ) if env is None else env
    clock = clock or SystemClock()
    if gate_report is None:
        gate_report = gate_mod.run(settings, env)
    if ledger is None:
        ledger = (HttpLedgerClient(settings.ledger_url, settings.ledger_token)
                  if settings.ledger_url and settings.ledger_token else UnconfiguredLedgerClient())
    recorder = Recorder(ledger)
    prompts = load_prompts(settings.prompts_dir)
    with open(os.path.join(settings.seed_dir, "tool_policy_seed.json"), "rb") as fh:
        policy_seed = json.loads(fh.read())
    with open(os.path.join(settings.seed_dir, "test_commands_seed.json"), "rb") as fh:
        test_seed = json.loads(fh.read())
    if docker is None:
        docker = sandbox_mod.RealDockerCli()
    if docker_available is None:
        docker_available = lambda: sandbox_mod.daemon_available(docker)  # noqa: E731
    egress = EgressClient(settings.egress_allow_hosts, record=lambda *a, **k: None,
                          default_timeout_s=settings.egress_default_timeout_s,
                          llm_read_timeout_s=settings.egress_llm_read_timeout_s, env=env)
    if chat_backend is None:
        chat_backend = backend_from_settings(settings, egress, NotWiredVault(), env)
    log_ = RecordLog(settings.data_dir)
    if git is None:
        git = GitPort(settings.repo_path, record=lambda *a, **k: None)
    data_dir = settings.data_dir or _memory_home()
    if wire_harness:
        from zbm_delivery import harness
        harness.prepare_environment(settings, data_dir)
        if harness_factory is None:
            harness_factory = lambda thread_id, mws: harness.make_client(settings, thread_id, mws, manifest_skill_names=gate_report.manifest_skill_names)  # noqa: E731
        if provider_factory is None:
            provider_factory = harness.provider
    svc = DeliveryService(settings, recorder, log_, gate_report=gate_report, docker=docker, egress=egress,
                          chat_backend=chat_backend, git=git, harness_factory=harness_factory,
                          provider_factory=provider_factory, prompts=prompts, policy_seed=policy_seed,
                          test_seed=test_seed, clock=clock, docker_available=docker_available)
    # the egress client and the git port record through the service (record-first), now that it exists
    egress.record = svc._record_plain
    git.record = svc._record_plain
    return svc


_MEMORY_HOME: list = []


def _memory_home() -> str:
    """Wave 24 (E6, N23-D-9): with no DLV_DATA_DIR (in-memory mode) the harness's scratch home is a private temp
    dir of this process, removed when it exits — it used to be ``<cwd>/.dlv-mem``, i.e. the service directory
    whenever the suite or a launcher ran from there."""
    if not _MEMORY_HOME:
        import atexit
        import tempfile
        from zbm_delivery import fsops
        d = tempfile.mkdtemp(prefix="dlv-mem-")
        atexit.register(fsops.drop_own_temp, d)
        _MEMORY_HOME.append(d)
    return _MEMORY_HOME[0]


def _app_from_env() -> FastAPI:
    settings = config_mod.load()
    return create_app(build_service(settings), settings)


def main() -> None:
    from zbm_delivery import serve

    app = _app_from_env()
    host = os.environ.get("DLV_BIND_ADDR", "127.0.0.1")
    port = int(os.environ.get("DLV_PORT", "8430"))
    serve.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
