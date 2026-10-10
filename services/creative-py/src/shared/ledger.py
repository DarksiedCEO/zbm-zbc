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
  — and ledger-rust's own load-shed 503 (fix wave 5, LOW-D): its
  `shed()` answers 503 with the fixed body LEDGER_SHED_BODY WITHOUT
  reading the request, before any ledger work (services/ledger-rust/src/
  bin/server.rs `serve` -> `shed`), so that exact answer certainly did not
  append. Any other 503 (e.g. from a proxy in between) stays "unknown";
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

import contextlib
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

from shared.errors import CreativeError, ValidationFailed

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


class LedgerQueryFailed(LedgerNotRecorded):
    """A READ of the ledger (find_event) failed; nothing was changed."""


# Wave F: reads are size-capped, and paged + filtered through ledger-rust's ``GET /ledger/entries?after_seq=&limit=
# &department=&event_type=`` (fix-ledger, sweep F-2; its page maximum is 10 000)
LEDGER_ENTRIES_MAX_BYTES = 256 * 1024 * 1024
ENTRIES_PAGE_SIZE = 1000


class LedgerPagingUnsupported(Exception):
    """The ledger has no paged read (before fix-ledger: 404; a stricter one: 400)."""


def select_paged(get_page, department: str, event_type: str | None, page_size: int = ENTRIES_PAGE_SIZE,
                 want: set | None = None, read_all=None, after_seq: int | None = None) -> list[dict]:
    """This department's entries (of one event type), page by page, in ledger order. With ``want`` (event ids) the
    read stops at the page where every wanted id has been seen. ``get_page(params)`` raises
    ``LedgerPagingUnsupported`` on a ledger without the paged read: then ``read_all()`` (the whole ledger) is filtered
    here. An older ledger that ignores the query answers the whole ledger: recognised (more than a page, or a page
    that does not move past ``after_seq``)."""
    def keep(e):
        return isinstance(e, dict) and e.get("department") == department and (
            event_type is None or e.get("event_type") == event_type)
    out: list[dict] = []
    after = after_seq
    start = after_seq
    while True:
        params = {"limit": str(page_size), "department": department}
        if event_type is not None:
            params["event_type"] = event_type
        if after is not None:
            params["after_seq"] = str(after)
        try:
            page = get_page(params)
        except LedgerPagingUnsupported:
            if read_all is None:
                raise LedgerQueryFailed("the ledger has no paged read") from None
            return [e for e in read_all() if keep(e) and (start is None or e.get("seq", 0) > start)]
        seqs = [e.get("seq") for e in page]
        if not all(isinstance(q, int) and not isinstance(q, bool) for q in seqs):
            raise LedgerQueryFailed("ledger entries carry no integer seq")
        if len(page) > page_size or (after is not None and page and seqs[0] <= after):
            # an older ledger ignored the query: the whole ledger
            return [e for e in page if keep(e) and (start is None or e.get("seq", 0) > start)]
        out += [e for e in page if keep(e)]
        if len(page) < page_size:
            return out
        if want is not None and want <= {e.get("event_id") for e in out}:
            return out
        after = seqs[-1]


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


class EvidenceLinePending(LedgerRecordError):
    """Bug sweep D (D-2): the decision TOOK EFFECT in this service (its records are on the ledger, its state is
    committed) but the local evidence line naming those records could not be anchored or appended yet. The line is
    owed: the next decision writes it first (and is refused while it cannot). Never "did not take effect", never
    "retry" (a repeat would decide twice)."""

    took_effect = True

    def __init__(self, message: str, outcome: bool | str, outside_calls: list[str]):
        super().__init__(message)
        self.outcome = outcome
        self.outside_calls = list(outside_calls)


class LedgerFieldInvalid(ValidationFailed):
    """A derived ledger field (subject_id, event_type, actor, summary) would
    be rejected by ledger-rust. Raised BEFORE the ledger is called and
    before any work: a 422 about the input, never a fake "ledger failure"
    503 while the ledger is healthy."""


def _now_iso() -> str:
    """The line's ``at`` (local log only; never part of an event id or an event payload)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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


# ledger-rust's load-shed answer (server.rs `shed`): status 503, this JSON body.
LEDGER_SHED_BODY = {"error": "ledger-rust is at its connection limit; retry shortly"}


def _is_ledger_shed(resp: httpx.Response) -> bool:
    if resp.status_code != 503:
        return False
    try:
        return resp.json() == LEDGER_SHED_BODY
    except ValueError:
        return False


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

    def find_event(self, event_id: str) -> dict | None:
        """The ledger entry recorded under `event_id`, or None if the ledger
        holds none. Raises LedgerQueryFailed if the ledger can't be read."""
        ...


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
        if _is_ledger_shed(resp):
            raise LedgerNotRecorded("ledger shed the request at its connection limit (HTTP 503, request not read)")
        raise LedgerRecordError(f"ledger answered HTTP {resp.status_code} (the ledger may have recorded it)")

    def find_event(self, event_id: str) -> dict | None:
        """GET /ledger/entries (ledger-rust has no by-id read) and look for
        the event. A read, so any failure is LedgerQueryFailed."""
        for e in self.entries():
            if isinstance(e, dict) and e.get("event_id") == event_id:
                return e
        return None

    def entries(self) -> list[dict]:
        """GET /ledger/entries: every entry in ledger order (bug sweep D: /audit/evidence), size-capped (Wave F).
        LedgerQueryFailed."""
        return self._get_list(None)

    def entries_filtered(self, department: str, event_type: str | None = None, page_size: int = ENTRIES_PAGE_SIZE,
                         want: set | None = None, after_seq: int | None = None) -> list[dict]:
        """Wave F: this department's entries (of one type) through ledger-rust's paged, filtered read
        (``select_paged``); a ledger without it is read in full and filtered here."""
        return select_paged(self._get_list, department, event_type, page_size, want, self.entries, after_seq)

    def _get_list(self, params: dict | None) -> list[dict]:
        try:
            with httpx.Client(timeout=max(self._timeout, 30.0), transport=self._transport) as client:
                with client.stream("GET", f"{self._base_url}/ledger/entries", params=params,
                                   headers={"Authorization": f"Bearer {self._token}"}) as resp:
                    if params and resp.status_code in (400, 404, 405):
                        raise LedgerPagingUnsupported("the ledger has no paged read")
                    if resp.status_code != 200:
                        raise LedgerQueryFailed(f"ledger could not be read: HTTP {resp.status_code}")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > LEDGER_ENTRIES_MAX_BYTES:
                            raise LedgerQueryFailed("ledger entries larger than the read cap")
                        chunks.append(chunk)
            entries = json.loads(b"".join(chunks))
        except (LedgerQueryFailed, LedgerPagingUnsupported):
            raise
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise LedgerQueryFailed(f"ledger could not be read: {type(exc).__name__}") from exc
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise LedgerQueryFailed("ledger entries were not a list of objects")
        return entries


class UnconfiguredLedgerClient:
    """Default when the ledger isn't configured: every record fails."""

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        raise LedgerNotRecorded(
            "evidence ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
            "no decision can take effect without a ledger record"
        )

    def find_event(self, event_id: str) -> dict | None:
        raise LedgerQueryFailed("evidence ledger not configured; it can't be read")


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

    def find_event(self, event_id: str) -> dict | None:
        if self.fail_all:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        for e in self.events:
            if e["event_id"] == event_id:
                return dict(e)
        return None

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == event_type]

    def entries(self) -> list[dict]:
        if self.fail_all:
            raise LedgerQueryFailed("simulated ledger outage (test double)")
        return [{k: v for k, v in e.items() if k != "payload"} | {"seq": i + 1} for i, e in enumerate(self.events)]

    def entries_filtered(self, department: str, event_type: str | None = None, page_size: int = ENTRIES_PAGE_SIZE,
                         want: set | None = None, after_seq: int | None = None) -> list[dict]:
        """ledger-rust's paged filtered read, with the same paging (``pages_read`` counts the pages served)."""
        def page(params):
            if self.fail_all:
                raise LedgerQueryFailed("simulated ledger outage (test double)")
            self.pages_read = getattr(self, "pages_read", 0) + 1
            after = int(params.get("after_seq", 0))
            sel = [e for e in FakeLedgerClient.entries(self) if e["seq"] > after
                   and e["department"] == params["department"]
                   and ("event_type" not in params or e["event_type"] == params["event_type"])]
            return sel[:int(params["limit"])]
        return select_paged(page, department, event_type, page_size, want, after_seq=after_seq)


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
    # Bug sweep D (D-2): the local anchored evidence log (``shared.journal``). Every record a decision writes is
    # collected and named by ONE anchored line when the decision returns (or refuses, or partly took effect);
    # a record no anchored line names is ``attempted`` (GET /audit/evidence). None = an in-memory log.
    journal: Any = field(default=None, repr=False)
    # name -> callable returning a server-assigned id counter; every line carries them, a restart resumes past them
    counters: dict = field(default_factory=dict, repr=False)
    closed: bool = False
    # Wave F (M-1): the service's consequential state (``shared.statelog.StateTracker``); every line carries the state
    # change since the previous one, and start-up replays it. None = no state is carried (unit tests of the recorder)
    state: Any = field(default=None, repr=False)
    _depth: int = field(default=0, repr=False)
    _written: list = field(default_factory=list, repr=False)
    _calls: list = field(default_factory=list, repr=False)
    _owed_after: list = field(default_factory=list, repr=False)
    _saved_counters: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.journal is None:
            from shared.store import RecordLog
            self.journal = self.make_journal(RecordLog(None))

    def make_journal(self, log):
        from shared.journal import EvidenceJournal
        return EvidenceJournal(
            log, DEPARTMENT,
            lambda eid, et, actor, sid, payload, summary: self.client.record_event(
                event_id=eid, department=DEPARTMENT, event_type=et, actor=actor, subject_id=sid, payload=payload,
                summary=_clean_summary(summary)),
            payload_hash=payload_sha256, id_prefix="cp:anc-")

    def _counters_now(self) -> dict:
        return {k: int(fn()) for k, fn in sorted(self.counters.items())}

    @contextlib.contextmanager
    def op(self, name: str):
        """One decision (``serialized``): flush any owed line first; at the end name every record it wrote in one
        anchored line. Nested calls join the outer decision."""
        with self.lock:
            outer = self._depth == 0
            if outer:
                if self.closed:
                    raise LedgerNotRecorded("SERVICE_CLOSED: this creative-py instance is closed; nothing was recorded")
                self._flush_owed()
                self._written, self._calls = [], []
            self._depth += 1
            ok, partial = False, False
            partial_exc: OutcomeNotRecorded | None = None
            try:
                yield
                ok = True
            except CreativeError:
                ok = True                    # a refusal is a decision: its records (if any) are committed
                raise
            except OutcomeNotRecorded as exc:
                partial = True               # an outside call was made: what was recorded is named
                partial_exc = exc
                raise
            except LedgerRecordError as exc:
                if outer and self._calls:
                    # bug sweep D (D-2): an outside department WAS called before this record failed: never "did not
                    # take effect" -- the request went out; the answer is not applied
                    partial = True
                    partial_exc = OutcomeNotRecorded(
                        f"{', '.join(self._calls)} WAS called (its request recorded first), then a record failed "
                        f"({exc}); the answer is not applied in this service",
                        {"outside_calls_made": len(self._calls), "departments": list(self._calls),
                         "failed_record": "not_recorded" if exc.took_effect is False else "unknown"})
                    raise partial_exc from exc
                raise
            finally:
                self._depth -= 1
                pending = None
                if outer:
                    if ok or partial:
                        pending = self._commit_line(name, partial)
                    elif self._counters_now() != self._saved_counters:
                        self._commit_line(name, partial=False, ids_only=True)   # a burned id survives a restart
                    self._written, self._calls = [], []
                if pending is not None:
                    if partial_exc is not None:
                        # AEGIS C-1: the decision only PARTLY took effect (the answer is not applied): that stays the
                        # error; the owed evidence line is a note on it, never a "took effect, do not repeat"
                        partial_exc.effect["evidence"] = "pending"
                    else:
                        raise pending

    def _flush_owed(self) -> None:
        if not self.journal.owed:
            return
        try:
            self.journal.flush()
        except LedgerRecordError as exc:
            raise LedgerNotRecorded(f"an owed evidence line could not be written ({exc}); nothing was done") from None
        except Exception as exc:  # noqa: BLE001 - StoreWriteError: the local log refused
            raise LedgerNotRecorded(f"an owed evidence line could not be written to the local log "
                                    f"({type(exc).__name__}); nothing was done") from None

    def _commit_line(self, name: str, partial: bool, ids_only: bool = False):
        written = [] if ids_only else list(self._written)
        counters = self._counters_now()
        state, mark_held = (None, None)
        if not ids_only and self.state is not None:
            state, mark_held = self.state.delta()
        if not written and counters == self._saved_counters and state is None:
            return None
        extra = {"op": name, "outside_calls": list(self._calls), "counters": counters}
        if partial:
            extra["partial"] = True
        if state is not None:
            extra["state"] = state
        kind = "ids" if ids_only else ("partial" if partial else "decision")
        try:
            self.journal.commit(kind, _now_iso(), name, written, extra, owe_on_failure=True)
        except Exception as exc:  # noqa: BLE001 - ledger or local log: the line is owed
            if ids_only:
                return None
            if mark_held is not None:
                mark_held()      # owed: the identical line (with this state) is written before anything else
            return EvidenceLinePending(
                f"the decision took effect; its local evidence line could not be written yet ({type(exc).__name__})",
                getattr(exc, "took_effect", False), list(self._calls))
        if mark_held is not None:
            mark_held()
        self._saved_counters = counters
        return None

    def write_intent(self, kind: str, data: dict) -> None:
        """Wave F: an anchored local line written BEFORE a record whose effect a restart must never lose (Andre's kit
        signature). Raises LedgerNotRecorded when it cannot be written: then nothing is recorded or changed."""
        try:
            self.journal.commit(kind, _now_iso(), kind, [], {kind: data})
        except Exception as exc:  # noqa: BLE001 - the ledger (anchor) or the local log refused
            raise LedgerNotRecorded(f"the {kind.replace('_', ' ')} could not be written ({type(exc).__name__}); "
                                    "nothing was recorded or changed") from None

    def note_call(self, department: str) -> None:
        """RecordedPort: an outside department is about to be called (its request is already recorded)."""
        if self._depth:
            self._calls.append(department)

    def event_id_for(self, event_type: str, actor: str, subject_id: str, op: Any) -> str:
        seq = self._seq.get((event_type, subject_id), 0)
        ident = _canonical({"i": self.instance_id, "d": DEPARTMENT, "t": event_type, "a": actor, "s": subject_id,
                            "n": seq, "op": op})
        return "cp:" + hashlib.sha256(ident.encode("utf-8", "surrogatepass")).hexdigest()

    def planned_event_id(self, event_type: str, actor: str, subject_id: str, payload: dict[str, Any],
                         summary: str = "", op_key: Any = None) -> str:
        """The event id `record()` with these arguments would send now."""
        with self.lock:
            return self.event_id_for(event_type, actor, subject_id, payload if op_key is None else op_key)

    def record(self, event_type: str, actor: str, subject_id: str, payload: dict[str, Any], summary: str,
               op_key: Any = None, event_id: str | None = None) -> str:
        """`op_key` (optional) is the operation's identity when the caller
        has a client-chosen idempotency key (e.g. a clip's submission_id);
        by default the canonical payload is the identity."""
        clean = _clean_summary(summary)
        with self.lock:
            if event_id is None:
                event_id = self.event_id_for(event_type, actor, subject_id, payload if op_key is None else op_key)
            problems = event_field_problems(event_id, DEPARTMENT, event_type, actor, subject_id, clean)
            if problems:
                raise LedgerFieldInvalid(
                    f"this decision can't be recorded on the evidence ledger: {', '.join(problems)} "
                    "outside ledger-rust's field rules; nothing was recorded or changed", problems)
            if self.closed:
                raise LedgerNotRecorded("SERVICE_CLOSED: this creative-py instance is closed; nothing was recorded")
            if self._depth == 0 and self.journal.owed:
                self._flush_owed()
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
            named = {"event_id": event_id, "event_type": event_type, "subject_id": subject_id,
                     "payload_sha256": payload_sha256(payload)}
            if self._depth:
                self._written.append(named)
            else:
                # a record outside any decision (registry / rights writes): its own line, now
                # (the caller commits its state after this returns: a line that could not be written is owed and
                # written before the next record, never an error that would leave the record unapplied)
                self._written = [named]
                try:
                    self._commit_line(event_type, partial=False)
                finally:
                    self._written = []
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


def stable_event_id(*parts: Any) -> str:
    """Wave F (AEGIS F-3): an event id that does not depend on this process (no instance id, no sequence): the same
    decision about the same object always records the same event (the ledger answers 200 for a repeat)."""
    return "cp:s-" + hashlib.sha256(_canonical(list(parts)).encode("utf-8", "surrogatepass")).hexdigest()


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
    """Run a workflow method under the recorder's lock, as one decision (bug sweep D: ``EvidenceRecorder.op``)."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self.recorder.op(fn.__name__):
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
            self._recorder.note_call(self._department)
            return fn(*args, **kwargs)

        return call


def _plain(v: Any) -> Any:
    import dataclasses

    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    if hasattr(v, "model_dump"):
        return v.model_dump(mode="json")
    return v
