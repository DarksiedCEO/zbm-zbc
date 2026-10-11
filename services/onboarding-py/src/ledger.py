"""
LedgerClient — BUILD_CONTRACTS.md section 2.

Every cross-department crossing and every gate ruling in Onboarding is
written through ``LedgerClient.record_event`` with department
"onboarding". If the write fails, the action that required it does NOT
proceed: callers write first, then make outside effects and change state,
and a ``LedgerWriteError`` propagates to the API, which answers 503 with
``proceeded: false`` (or, if an earlier outside effect of the same
operation already happened, ``LedgerWriteAfterEffects``: 503 with
``proceeded: true, completed: false`` and the list of effects done).

``clean_summary``/``validate_event``/``ledger_rust_accepts`` follow
services/ledger-rust/src/event.rs exactly (C0, DEL and C1 are all control
characters there; ids are fullmatched), and the test fake enforces the
same rules plus the ledger's 200/409 idempotency.

Payloads never contain credentials: the service only ever passes
secret-free structures, and ``record_event`` additionally runs the
payload and summary through ``redaction.scrub`` as defense in depth.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx

from redaction import scrub, scrub_obj

DEPARTMENT = "onboarding"

_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NAME = re.compile(r"[a-z0-9_]{1,64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


NOT_RECORDED = "not_recorded"
UNKNOWN = "unknown"


class LedgerWriteError(RuntimeError):
    """The ledger write did not succeed; the action must not proceed.

    ``outcome`` (fix wave 5, NEW-4) says what is known about the record:
    ``"not_recorded"`` — certainly not recorded (never sent, or refused by a
    status that means nothing was appended); ``"unknown"`` — sent, and the
    ledger may have recorded it (the reply was lost, or a status that does
    not rule it out). The API reports "did not proceed" only for the first.
    ``retry_hint`` overrides the API's default retry instruction."""

    def __init__(self, message: str, outcome: str = NOT_RECORDED, retry_hint: Optional[str] = None):
        super().__init__(message)
        if outcome not in (NOT_RECORDED, UNKNOWN):
            raise ValueError("outcome must be 'not_recorded' or 'unknown'")
        self.outcome = outcome
        self.retry_hint = retry_hint


class LedgerWriteAfterEffects(LedgerWriteError):
    """A ledger write failed AFTER one or more outside effects of the same
    operation had already happened (each of them authorized by a record
    written before it). Nothing further happens; the API reports exactly
    which effects were done instead of claiming the action did not proceed."""

    def __init__(self, message: str, effects: list[str], outcome: str = NOT_RECORDED, retry_hint: Optional[str] = None):
        super().__init__(message, outcome, retry_hint)
        self.effects = list(effects)


class EvidenceLineOwed(LedgerWriteAfterEffects):
    """Bug sweep D (D-1): the operation COMPLETED (its state is applied, its ledger events are written) but the local
    evidence line that names those events could not be anchored or appended. The line is owed: the next operation
    writes it first and is refused while it cannot. Until then the events read ``attempted`` in /audit/evidence.
    The API never asks for a retry of the operation itself (it took effect)."""


class LedgerQueryFailed(RuntimeError):
    """The ledger could not be read (GET /ledger/entries)."""


LEDGER_ENTRIES_MAX_BYTES = 256 * 1024 * 1024
# Wave F: one page of ledger-rust's paged, filtered read (``GET /ledger/entries?after_seq=&limit=&department=
# &event_type=``, fix-ledger sweep F-2; its maximum is 10 000)
ENTRIES_PAGE_SIZE = 1000


class LedgerPagingUnsupported(Exception):
    """The ledger has no paged read (before fix-ledger: 404; a stricter one: 400)."""


def select_paged(get_page, department: str, event_type: Optional[str], page_size: int = ENTRIES_PAGE_SIZE,
                 want: Optional[set] = None, read_all=None, after_seq: Optional[int] = None) -> list[dict]:
    """This department's entries (of one event type), page by page, in ledger order. With ``want`` (event ids) the
    read stops at the page where every wanted id has been seen, so its cost is bounded by where they are, not by the
    size of the ledger. ``get_page(params)`` raises ``LedgerPagingUnsupported`` on a ledger without the paged read:
    then ``read_all()`` (the whole ledger, size-capped) is filtered here. An older ledger that ignores the query
    answers the whole ledger: recognised (more than a page, or a page that does not move past ``after_seq``)."""
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


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_event_id() -> str:
    """Random id — kept only for callers outside the service layer. The
    service derives DETERMINISTIC ids (``derive_event_id``) so an
    identical retry is an idempotent replay (ADR 0004, "Event ids")."""
    return f"onb-{uuid.uuid4().hex}"


def derive_event_id(epoch: str, department: str, event_type: str, subject_id: str, subject_seq: int,
                    occurrence: int, payload_hash: str) -> str:
    """Event id derived from the operation: SHA-256 over the service epoch,
    department, event type, subject, the subject's operation sequence
    number, the occurrence of this exact event within the operation, and
    the scrubbed payload's SHA-256. A retry of the same operation (the
    sequence number only advances when an operation completes) produces
    the same ids, so the ledger answers 200 and the event is recorded once;
    the same id with a different actor/summary is the ledger's 409."""
    material = json.dumps([epoch, department, event_type, subject_id, subject_seq, occurrence, payload_hash],
                          separators=(",", ":"))
    return "onb-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _rust_is_control(ch: str) -> bool:
    # Rust's char::is_control == Unicode general category Cc: C0 (U+0000-001F),
    # DEL (U+007F) and C1 (U+0080-009F).
    o = ord(ch)
    return o <= 0x1F or 0x7F <= o <= 0x9F


def clean_summary(summary: str) -> str:
    """Make a summary the real ledger accepts (services/ledger-rust
    src/event.rs ``EventInput::validate``): 1-280 Unicode scalar values and
    no control character (C0, DEL or C1). Lone surrogates are not Unicode
    scalar values (they cannot even be encoded as UTF-8), so they go too."""
    s = scrub(summary) if isinstance(summary, str) else ""
    s = "".join(" " if (_rust_is_control(ch) or 0xD800 <= ord(ch) <= 0xDFFF) else ch for ch in s).strip()
    return s[:280].strip() or "(no summary)"


def ledger_rust_accepts(body: dict) -> bool:
    """An independent mirror of ledger-rust's validation (event.rs), used
    by the test fake so tests can never be looser than production. Charset
    checks are ASCII-only on BYTES, like the Rust code (``$``-style regex
    end anchors are not used: a trailing newline is invalid)."""
    try:
        if set(body) != {"event_id", "department", "event_type", "actor", "subject_id", "payload_sha256", "summary"}:
            return False
        if not all(isinstance(v, str) for v in body.values()):
            return False

        id_bytes = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
        slug_bytes = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789_")

        def ident(v: str, mx: int) -> bool:
            b = v.encode("utf-8")
            return 0 < len(b) <= mx and all(x in id_bytes for x in b)

        def slug(v: str, mx: int) -> bool:
            b = v.encode("utf-8")
            return 0 < len(b) <= mx and all(x in slug_bytes for x in b)

        s = body["summary"]
        return (
            ident(body["event_id"], 128) and slug(body["department"], 64) and slug(body["event_type"], 64)
            and slug(body["actor"], 64) and ident(body["subject_id"], 128)
            and _HEX64.fullmatch(body["payload_sha256"]) is not None
            and 1 <= len(s) <= 280 and not any(_rust_is_control(ch) for ch in s)
            and not any(0xD800 <= ord(ch) <= 0xDFFF for ch in s)
        )
    except UnicodeEncodeError:
        return False


def validate_event(event_id: str, department: str, event_type: str, actor: str, subject_id: str) -> None:
    # fullmatch, not match+$: Python's $ also matches before a trailing
    # newline, which ledger-rust rejects (400).
    if not isinstance(event_id, str) or not _ID.fullmatch(event_id):
        raise LedgerWriteError("invalid event_id for ledger contract")
    if not all(isinstance(x, str) and _NAME.fullmatch(x) for x in (department, event_type, actor)):
        raise LedgerWriteError("invalid department/event_type/actor for ledger contract")
    if not isinstance(subject_id, str) or not _ID.fullmatch(subject_id):
        raise LedgerWriteError("invalid subject_id for ledger contract")


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


def build_body(event_id, department, event_type, actor, subject_id, payload, summary) -> dict:
    validate_event(event_id, department, event_type, actor, subject_id)
    safe_payload = scrub_obj(payload)
    return {
        "event_id": event_id,
        "department": department,
        "event_type": event_type,
        "actor": actor,
        "subject_id": subject_id,
        "payload_sha256": payload_sha256(safe_payload),
        "summary": clean_summary(summary),
    }


# Transport errors raised before any byte of the request left this process:
# the ledger cannot have recorded anything (fix wave 5, NEW-4).
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
             httpx.LocalProtocolError, httpx.InvalidURL)
_RETRY_409 = ("the ledger already holds a DIFFERENT event under this operation's id; retrying will not resolve it — "
              "an operator must reconcile the ledger before this action is attempted again")

# ledger-rust's load-shed answer (bin/server.rs ``shed``): status 503 with
# exactly this JSON body, written before the request is read. The SAME rule
# as services/creative-py/src/shared/ledger.py ``LEDGER_SHED_BODY`` (ADR 0004
# / ADR 0005): only that exact answer proves nothing was appended.
LEDGER_SHED_BODY = {"error": "ledger-rust is at its connection limit; retry shortly"}


def is_ledger_shed(resp: httpx.Response) -> bool:
    """True only for ledger-rust's own load-shed 503: the exact body. A 503
    with any other body (a proxy or gateway in front of the ledger, which
    may have forwarded the request before answering) is not one."""
    if resp.status_code != 503:
        return False
    try:
        return resp.json() == LEDGER_SHED_BODY
    except ValueError:
        return False


class HttpLedgerClient:
    """POST /ledger/events on ledger-rust. 201 (new) and 200 (idempotent
    retry) are success; anything else is a LedgerWriteError, classified
    (fix wave 5, NEW-4) by what is known about the record:

    certainly NOT recorded (``outcome="not_recorded"``):
      - local contract validation failed (nothing sent);
      - the connection was never made (refused, connect/pool timeout);
      - 4xx other than 409 (400 invalid, 401 token, 408 body too slow, 413):
        ledger-rust answers these without appending;
      - ledger-rust's load-shed 503 — status 503 WITH its exact body
        (``LEDGER_SHED_BODY``), written BEFORE the request is read
        (bin/server.rs ``shed``), so nothing is appended.
    UNKNOWN (``outcome="unknown"``):
      - the request was (or may have been) sent and no reply arrived: read
        timeout, reset, "server disconnected", write error/timeout;
      - any other 5xx, INCLUDING a 503 with a different body (fix wave 6,
        N6): only ledger-rust itself answers the shed body; an intermediary's
        503 may have been sent after it forwarded the request, and a 500
        "failed to persist" or a gateway error means the append may be on
        disk. This is the rule creative-py applies (ADR 0005, LOW-D);
      - 409: an event with this id and different content is already there.
    Error messages never include the token or the payload."""

    def __init__(self, base_url: str, token: str, timeout_s: float = 5.0, transport: Optional[httpx.BaseTransport] = None):
        if not base_url or not token:
            raise ValueError("HttpLedgerClient needs LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_s,
            transport=transport,
        )

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        body = build_body(event_id, department, event_type, actor, subject_id, payload, summary)
        if not ledger_rust_accepts(body):  # would be a 400; never send what the ledger must refuse
            raise LedgerWriteError("event does not meet the ledger contract; not sent")
        try:
            r = self._client.post("/ledger/events", json=body)
        except _NOT_SENT as exc:
            raise LedgerWriteError(f"ledger unreachable ({type(exc).__name__}); nothing was sent") from None
        except httpx.HTTPError as exc:
            raise LedgerWriteError(f"no reply from the ledger after sending ({type(exc).__name__})", UNKNOWN) from None
        if r.status_code in (200, 201):
            return
        if r.status_code == 409:
            raise LedgerWriteError("ledger rejected event: same event_id with different content (409)", UNKNOWN, _RETRY_409)
        if 400 <= r.status_code < 500:
            raise LedgerWriteError(f"ledger rejected event (HTTP {r.status_code}); nothing was recorded")
        if is_ledger_shed(r):
            raise LedgerWriteError("ledger shed the request at its connection limit (HTTP 503, request not read); "
                                   "nothing was recorded")
        raise LedgerWriteError(f"ledger answered HTTP {r.status_code}; the event may or may not be recorded", UNKNOWN)

    def entries(self) -> list[dict]:
        """GET /ledger/entries (the whole ledger), size-capped (bug sweep D: /audit/evidence)."""
        return self._get_list(None)

    def entries_filtered(self, department: str, event_type: Optional[str] = None, page_size: int = ENTRIES_PAGE_SIZE,
                         want: Optional[set] = None, after_seq: Optional[int] = None) -> list[dict]:
        """Wave F: this department's entries of one type through ledger-rust's paged, filtered read (``select_paged``);
        a ledger without it is read in full and filtered here (bounded by LEDGER_ENTRIES_MAX_BYTES)."""
        return select_paged(self._get_list, department, event_type, page_size, want, self.entries, after_seq)

    def _get_list(self, params: Optional[dict]) -> list[dict]:
        try:
            with self._client.stream("GET", "/ledger/entries", params=params,
                                     timeout=max(self._client.timeout.read or 5.0, 30.0)) as r:
                if params and r.status_code in (400, 404, 405):
                    raise LedgerPagingUnsupported("the ledger has no paged read")
                if r.status_code != 200:
                    raise LedgerQueryFailed(f"ledger could not be read: HTTP {r.status_code}")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > LEDGER_ENTRIES_MAX_BYTES:
                        raise LedgerQueryFailed("ledger entries larger than the read cap")
                    chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        except (LedgerQueryFailed, LedgerPagingUnsupported):
            raise
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise LedgerQueryFailed(f"ledger could not be read: {type(exc).__name__}") from None
        if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
            raise LedgerQueryFailed("ledger entries were not a list of objects")
        return data


class UnconfiguredLedgerClient:
    """Default when LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN are unset:
    every write fails, so every action that needs a record is refused."""

    def record_event(self, *args, **kwargs) -> None:
        raise LedgerWriteError(
            "ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
            "refusing to proceed without an evidence record"
        )

    def entries(self) -> list[dict]:
        raise LedgerQueryFailed("ledger not configured")

    def entries_filtered(self, *args, **kwargs) -> list[dict]:
        raise LedgerQueryFailed("ledger not configured")


@dataclass
class FakeLedgerClient:
    """Test double with the SAME observable rules as ledger-rust:
    validation (``ledger_rust_accepts``, a mirror of event.rs — HTTP 400),
    idempotent identical retry (200: no second entry) and conflicting
    content under an existing event_id (409). Stores the request body AND
    the scrubbed payload so tests can assert no secret reaches the ledger."""

    fail: bool = False
    events: list[dict] = field(default_factory=list)
    payloads: list[dict] = field(default_factory=list)

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        body = build_body(event_id, department, event_type, actor, subject_id, payload, summary)
        if self.fail:
            raise LedgerWriteError("fake ledger configured to fail")
        if not ledger_rust_accepts(body):
            raise LedgerWriteError("ledger rejected event (HTTP 400)")
        for existing in self.events:
            if existing["event_id"] == body["event_id"]:
                if existing == body:
                    return  # 200: identical retry, nothing new recorded
                raise LedgerWriteError("ledger rejected event: same event_id with different content (409)")
        self.events.append(body)
        self.payloads.append(scrub_obj(payload))

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.events]

    def entries(self) -> list[dict]:
        """What GET /ledger/entries answers (the stored bodies, in ledger order, with their 1-based seq)."""
        if self.fail:
            raise LedgerQueryFailed("fake ledger configured to fail")
        return [{**e, "seq": i + 1} for i, e in enumerate(self.events)]

    def entries_filtered(self, department: str, event_type: Optional[str] = None, page_size: int = ENTRIES_PAGE_SIZE,
                         want: Optional[set] = None, after_seq: Optional[int] = None) -> list[dict]:
        """ledger-rust's paged filtered read, with the same paging (``pages_read`` counts the pages served)."""
        def page(params):
            if self.fail:
                raise LedgerQueryFailed("fake ledger configured to fail")
            self.pages_read = getattr(self, "pages_read", 0) + 1
            after = int(params.get("after_seq", 0))
            sel = [e for e in ({**x, "seq": i + 1} for i, x in enumerate(self.events)) if e["seq"] > after and e["department"] == params["department"]
                   and ("event_type" not in params or e["event_type"] == params["event_type"])]
            return sel[:int(params["limit"])]
        return select_paged(page, department, event_type, page_size, want, after_seq=after_seq)
