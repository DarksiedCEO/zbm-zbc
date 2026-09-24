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


class LedgerWriteError(RuntimeError):
    """The ledger write failed; the action must not proceed."""


class LedgerWriteAfterEffects(LedgerWriteError):
    """A ledger write failed AFTER one or more outside effects of the same
    operation had already happened (each of them authorized by a record
    written before it). Nothing further happens; the API reports exactly
    which effects were done instead of claiming the action did not proceed."""

    def __init__(self, message: str, effects: list[str]):
        super().__init__(message)
        self.effects = list(effects)


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


class HttpLedgerClient:
    """POST /ledger/events on ledger-rust. 201 (new) and 200 (idempotent
    retry) are success; anything else — 409, 400, 401, 5xx, timeout,
    connection refused — is a LedgerWriteError. Error messages never
    include the token or the payload."""

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
        except httpx.HTTPError as exc:
            raise LedgerWriteError(f"ledger unreachable ({type(exc).__name__})") from None
        if r.status_code in (200, 201):
            return
        if r.status_code == 409:
            raise LedgerWriteError("ledger rejected event: same event_id with different content (409)")
        raise LedgerWriteError(f"ledger rejected event (HTTP {r.status_code})")


class UnconfiguredLedgerClient:
    """Default when LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN are unset:
    every write fails, so every action that needs a record is refused."""

    def record_event(self, *args, **kwargs) -> None:
        raise LedgerWriteError(
            "ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
            "refusing to proceed without an evidence record"
        )


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
