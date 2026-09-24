"""
Evidence ledger client — BUILD_CONTRACTS.md section 2, implemented as written.

`LedgerClient.record_event(event_id, department, event_type, actor,
subject_id, payload, summary) -> None` raises `LedgerRecordError` on ANY
failure (transport error, non-2xx, 409 conflict, local validation failure).
Every workflow in `zbm/` and `zbc/` records FIRST and only then commits
state, so on any failure nothing changed IN THIS SERVICE. Whether the
LEDGER holds the record is a separate question (fix wave 4, LOST), carried
in `took_effect`:
- `LedgerNotRecorded` (took_effect False): certainly not recorded — local
  validation failure, ledger not configured, connection refused / connect
  timeout (the request never reached the ledger), or a 4xx refusal other
  than 409;
- `LedgerConflict` (took_effect "unknown"): 409 — the ledger already holds
  a DIFFERENT record under this decision's deterministic id;
- `LedgerRecordError` itself (took_effect "unknown"): the request may have
  reached the ledger and been committed — read timeout, dropped
  connection, 5xx. Never reported as "did not take effect".

Implementations:
- `HttpLedgerClient`   — real HTTP against ledger-rust `POST /ledger/events`.
- `UnconfiguredLedgerClient` — the API default when LEDGER_SERVICE_URL /
  LEDGER_SERVICE_TOKEN are unset: every record fails, so every decision is
  refused (fail closed, never silently unrecorded).
- `FakeLedgerClient`   — in-memory test double that enforces ledger-rust's
  field rules (an independent port of `EventInput::validate` in
  services/ledger-rust/src/event.rs, so the client's own validator can't
  make tests looser than production) and its idempotency (201/200/409).

Event ids are DETERMINISTIC (fix wave 1, F11; ADR 0005 decision 15):
`cp:` + SHA-256 over (service instance, department, event_type, actor,
subject_id, per-(event_type, subject_id) sequence number, canonical
operation). The sequence number advances only when a record succeeds, so
a retry of an operation whose record failed — including a record the
ledger committed but whose response was lost — sends the SAME event id and
gets the ledger's 200 (identical) or 409 (different content), and the
decision takes effect exactly once. Two genuinely separate decisions with
identical content get different ids because the sequence has advanced.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from shared.errors import ValidationFailed

DEPARTMENT = "creative_production"

# Mirrors ledger-rust src/event.rs exactly: ids/slugs are ASCII-only and
# length-checked in bytes (== chars for ASCII); `fullmatch`, never `match`
# with `$` (which would accept a trailing newline the ledger rejects).
ID_MAX = 128
NAME_MAX = 64
SUMMARY_MAX = 280
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NAME_RE = re.compile(r"[a-z0-9_]{1,64}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
# Rust `char::is_control` == Unicode general category Cc: C0, DEL and C1.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


class LedgerRecordError(Exception):
    """The record call failed: the decision that needed it did not take
    effect in this service. Whether the ledger committed it is UNKNOWN
    (took_effect "unknown") unless a subclass says otherwise — e.g. the
    request reached the ledger, which committed it, and the response was
    lost."""

    took_effect: bool | str = "unknown"


class LedgerNotRecorded(LedgerRecordError):
    """Certainly NOT recorded: the ledger never got the request, or refused
    it outright (4xx other than 409), or the event failed local validation."""

    took_effect = False


class LedgerConflict(LedgerRecordError):
    """409: the ledger already holds a different record under this
    decision's deterministic event id. The outcome is uncertain/conflicting
    — never "did not take effect"."""

    took_effect = "unknown"


class OutcomeNotRecorded(LedgerRecordError):
    """The decision and its outside request WERE recorded and took effect,
    and the outside call was made; only the record of the outside party's
    answer failed. That answer is not applied. Reported as 503 with
    `took_effect: "partial"` — never as "did not take effect"."""

    took_effect = "partial"

    def __init__(self, message: str, effect: dict):
        super().__init__(message)
        self.effect = effect


class LedgerFieldInvalid(ValidationFailed):
    """A derived ledger field (subject_id, event_type, actor, summary) would
    be rejected by ledger-rust. Raised BEFORE the ledger is called and
    before any work: a 422 about the input, never a fake "ledger failure"
    503 while the ledger is healthy."""


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_field_problems(
    event_id: str, department: str, event_type: str, actor: str, subject_id: str, summary: str
) -> list[str]:
    """The section-2 field rules exactly as ledger-rust enforces them."""
    problems = []
    for name, value, rx in (("event_id", event_id, _EVENT_ID_RE), ("department", department, _NAME_RE),
                            ("event_type", event_type, _NAME_RE), ("actor", actor, _NAME_RE),
                            ("subject_id", subject_id, _EVENT_ID_RE)):
        if not isinstance(value, str) or not rx.fullmatch(value):
            problems.append(name)
    if (not isinstance(summary, str) or not (1 <= len(summary) <= SUMMARY_MAX)
            or _CONTROL_RE.search(summary) or _SURROGATE_RE.search(summary)):
        problems.append("summary")
    return problems


def validate_event_fields(
    event_id: str, department: str, event_type: str, actor: str, subject_id: str, summary: str
) -> None:
    """Local check of the section-2 field rules so a malformed event fails
    here, loudly, instead of as an opaque 400 from the ledger."""
    problems = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
    if problems:
        raise LedgerNotRecorded(f"ledger event fails contract validation on: {', '.join(problems)}")


class LedgerClient(Protocol):
    def record_event(
        self,
        event_id: str,
        department: str,
        event_type: str,
        actor: str,
        subject_id: str,
        payload: dict,
        summary: str,
    ) -> None: ...


class HttpLedgerClient:
    """Real client for ledger-rust `POST /ledger/events`."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        if not base_url or not token:
            raise ValueError("HttpLedgerClient needs both a base URL and a token")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_env(cls) -> "HttpLedgerClient":
        url = os.environ.get("LEDGER_SERVICE_URL")
        token = os.environ.get("LEDGER_SERVICE_TOKEN")
        if not url or not token:
            raise ValueError("LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN must both be set")
        return cls(url, token)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        validate_event_fields(event_id, department, event_type, actor, subject_id, summary)
        body = {
            "event_id": event_id,
            "department": department,
            "event_type": event_type,
            "actor": actor,
            "subject_id": subject_id,
            "payload_sha256": payload_sha256(payload),
            "summary": summary,
        }
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resp = client.post(
                    f"{self._base_url}/ledger/events",
                    json=body,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.InvalidURL, httpx.UnsupportedProtocol,
                ValueError) as exc:  # the request never reached the ledger
            raise LedgerNotRecorded(f"ledger unreachable: {type(exc).__name__} (the request never reached it)") from exc
        except httpx.HTTPError as exc:  # sent, answer lost: the ledger may have committed it (never a 500)
            raise LedgerRecordError(f"ledger response lost: {type(exc).__name__} (the ledger may have recorded it)") from exc
        if resp.status_code in (200, 201):
            return
        if resp.status_code == 409:
            raise LedgerConflict("ledger already holds a DIFFERENT record under this decision's event id (409)")
        if 400 <= resp.status_code < 500:
            raise LedgerNotRecorded(f"ledger refused the event: HTTP {resp.status_code}")
        raise LedgerRecordError(f"ledger answered HTTP {resp.status_code} (the ledger may have recorded it)")


class UnconfiguredLedgerClient:
    """Default when the ledger isn't configured: every record fails."""

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        raise LedgerNotRecorded(
            "evidence ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
            "no decision can take effect without a ledger record"
        )


def _rust_event_input_valid(body: dict) -> str | None:
    """Independent port of ledger-rust `EventInput::validate` (+ what its
    JSON parser refuses). Deliberately NOT sharing code with
    `event_field_problems`, so a bug there can't hide in the tests."""
    def check_id(v, mx):
        return isinstance(v, str) and 0 < len(v.encode("utf-8", "surrogatepass")) <= mx and all(
            (48 <= b <= 57) or (65 <= b <= 90) or (97 <= b <= 122) or b in b"._:-" for b in v.encode("utf-8", "surrogatepass"))

    def check_slug(v, mx):
        return isinstance(v, str) and 0 < len(v.encode("utf-8", "surrogatepass")) <= mx and all(
            (48 <= b <= 57) or (97 <= b <= 122) or b == 95 for b in v.encode("utf-8", "surrogatepass"))

    for k in ("event_id", "subject_id"):
        if not check_id(body[k], 128):
            return k
    for k in ("department", "event_type", "actor"):
        if not check_slug(body[k], 64):
            return k
    h = body["payload_sha256"]
    if not (isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h)):
        return "payload_sha256"
    s = body["summary"]
    if not isinstance(s, str):
        return "summary"
    try:
        s.encode("utf-8")  # a lone surrogate can't cross JSON into a Rust String
    except UnicodeEncodeError:
        return "summary"
    if not 1 <= len(s) <= 280 or any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f for c in s):
        return "summary"
    return None


@dataclass
class FakeLedgerClient:
    """Test double. Enforces ledger-rust's validation (independent port)
    and idempotency semantics; `fail_all` / `fail_next` simulate an
    unreachable ledger."""

    events: list[dict] = field(default_factory=list)
    fail_all: bool = False
    fail_next: bool = False
    calls: int = 0

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        self.calls += 1
        if self.fail_all or self.fail_next:
            self.fail_next = False
            raise LedgerNotRecorded("simulated ledger outage: connection refused (test double)")
        bad = _rust_event_input_valid({"event_id": event_id, "department": department, "event_type": event_type,
                                       "actor": actor, "subject_id": subject_id,
                                       "payload_sha256": payload_sha256(payload), "summary": summary})
        if bad:
            raise LedgerNotRecorded(f"ledger refused the event: HTTP 400 (invalid {bad}, test double)")
        entry = {
            "event_id": event_id,
            "department": department,
            "event_type": event_type,
            "actor": actor,
            "subject_id": subject_id,
            "payload_sha256": payload_sha256(payload),
            "summary": summary,
        }
        for existing in self.events:
            if existing["event_id"] == event_id:
                if {k: existing[k] for k in entry} == entry:
                    return  # idempotent retry
                raise LedgerConflict("ledger already holds a DIFFERENT record under this event id (409, test double)")
        self.events.append({**entry, "payload": payload})

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == event_type]


def _clean_summary(summary: str) -> str:
    """Make any text acceptable to ledger-rust: control chars (C0, DEL, C1)
    and lone surrogates become spaces; 1..280 Unicode scalar values."""
    cleaned = _SURROGATE_RE.sub(" ", _CONTROL_RE.sub(" ", str(summary)))
    cleaned = " ".join(cleaned.split()) or "(no summary)"
    return cleaned[:SUMMARY_MAX]


def check_subject(subject_id: str, what: str = "subject") -> str:
    """A derived ledger subject must fit ledger-rust's subject_id rule;
    otherwise the INPUT is refused (422) before any work."""
    if not isinstance(subject_id, str) or not _EVENT_ID_RE.fullmatch(subject_id):
        raise LedgerFieldInvalid(
            f"{what} {subject_id!r} can't be recorded on the evidence ledger (subject_id must be 1-{ID_MAX} "
            "characters of [A-Za-z0-9._:-]); nothing was recorded or changed", ["subject_id"])
    return subject_id


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class EvidenceRecorder:
    """Thin wrapper every workflow uses. Always department
    "creative_production". Returns the event_id on success; raises
    LedgerRecordError otherwise (callers must not commit).

    `lock` serialises every decision in the service (both workflows and
    the shared registry/rights writes), so check-then-record-then-commit
    can't interleave between concurrent requests."""

    client: LedgerClient
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _seq: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)

    def event_id_for(self, event_type: str, actor: str, subject_id: str, op: Any) -> str:
        seq = self._seq.get((event_type, subject_id), 0)
        ident = _canonical({"i": self.instance_id, "d": DEPARTMENT, "t": event_type, "a": actor, "s": subject_id,
                            "n": seq, "op": op})
        return "cp:" + hashlib.sha256(ident.encode("utf-8", "surrogatepass")).hexdigest()

    def record(self, event_type: str, actor: str, subject_id: str, payload: dict[str, Any], summary: str,
               op_key: Any = None) -> str:
        """`op_key` (optional) is the operation's identity when the caller
        has a client-chosen idempotency key (e.g. a clip's submission_id);
        by default the canonical payload is the identity."""
        clean = _clean_summary(summary)
        with self.lock:
            event_id = self.event_id_for(event_type, actor, subject_id, payload if op_key is None else op_key)
            problems = event_field_problems(event_id, DEPARTMENT, event_type, actor, subject_id, clean)
            if problems:
                raise LedgerFieldInvalid(
                    f"this decision can't be recorded on the evidence ledger: {', '.join(problems)} "
                    "outside ledger-rust's field rules; nothing was recorded or changed", problems)
            self.client.record_event(
                event_id=event_id,
                department=DEPARTMENT,
                event_type=event_type,
                actor=actor,
                subject_id=subject_id,
                payload=payload,
                summary=clean,
            )
            key = (event_type, subject_id)
            self._seq[key] = self._seq.get(key, 0) + 1
            return event_id

    def try_record(self, event_type: str, actor: str, subject_id: str, payload: dict[str, Any], summary: str) -> str | None:
        """Best-effort record for REFUSALS (guardrail trips). A refusal
        stands whether or not it could be recorded — failing to record a
        refusal must never turn it into an approval."""
        try:
            return self.record(event_type, actor, subject_id, payload, summary)
        except (LedgerRecordError, LedgerFieldInvalid):
            return None


MAX_PENDING_CREATIONS = 10_000


def content_sha256(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8", "surrogatepass")).hexdigest()


@dataclass
class PendingCreations:
    """Creations under a SERVER-ASSIGNED id (brief-0001, job-0002, kit-0003)
    whose ledger record failed with an UNKNOWN outcome (fix wave 4, LOST
    sweep). Such an id may already be taken on the ledger, so it is burned
    (never reused for other content), and the attempt — its id, the EXACT
    record and the object it would commit — is kept per (kind, content hash)
    so an identical retry, at any later time, replays that record (ledger
    200/201) and commits that object: one creation, no duplicate, no two
    different records under one id. Bounded; oldest dropped first."""

    held: "OrderedDict[tuple[str, str], tuple[str, tuple, dict, Any]]" = field(default_factory=OrderedDict)

    def get(self, kind: str, sha: str) -> tuple[str, tuple, dict, Any] | None:
        return self.held.get((kind, sha))

    def record(self, recorder: "EvidenceRecorder", kind: str, sha: str, obj_id: str, obj: Any,
               burn, *args, **kwargs) -> tuple[str, Any]:
        """Record the creation (or replay a pending one). Returns (event_id,
        the object to commit). `burn()` consumes the server id; it is called
        on success of a fresh attempt and on an UNCERTAIN failure."""
        pending = self.held.get((kind, sha))
        if pending is not None:
            _, args, kwargs, obj = pending
            eid = recorder.record(*args, **kwargs)
            self.held.pop((kind, sha), None)
            return eid, obj
        try:
            eid = recorder.record(*args, **kwargs)
        except LedgerRecordError as exc:
            if exc.took_effect is not False:
                burn()
                self.held[(kind, sha)] = (obj_id, args, kwargs, obj)
                while len(self.held) > MAX_PENDING_CREATIONS:
                    self.held.popitem(last=False)
            raise
        burn()
        return eid, obj


def serialized(fn):
    """Run a workflow method under the recorder's lock."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self.recorder.lock:
            return fn(self, *a, **k)
    return wrapper


class RecordedPort:
    """Record-first proxy for an outside department (F9). Every method call
    is recorded on the ledger as `crossing_<department>_requested` — with
    the request it carries — BEFORE it is made. If that record fails,
    LedgerRecordError propagates and the department is never called. The
    answer is recorded by the workflow's own decision record afterwards."""

    def __init__(self, port: Any, department: str, recorder: EvidenceRecorder, actor: str, subject_id: str):
        self._port, self._department, self._recorder = port, department, recorder
        self._actor, self._subject = actor, subject_id

    def __getattr__(self, name: str):
        fn = getattr(self._port, name)

        def call(*args, **kwargs):
            request = [_plain(a) for a in args]
            self._recorder.record(
                f"crossing_{self._department}_requested", self._actor, self._subject,
                {"department": self._department, "action": name, "args": request,
                 "kwargs": {k: _plain(v) for k, v in kwargs.items()}},
                f"Request to {self._department}: {name} for {self._subject}")
            return fn(*args, **kwargs)

        return call


def _plain(v: Any) -> Any:
    import dataclasses

    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    return v
