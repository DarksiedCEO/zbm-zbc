"""
Evidence ledger client for fulfillment-py (bug sweep D, Oct 9 2026). BUILD_CONTRACTS.md section 2: ledger-rust
``POST /ledger/events`` and ``GET /ledger/entries``.

Before bug sweep D this department recorded nothing: a callback dial and a write-back to a client's system of record
left no evidence anywhere. Every consequential action now records first (``record_event``), and the local anchored
log (``journal.py``) says which records were committed.

Outcome classes (the rule creative-py / onboarding-py apply, ADR 0005 / ADR 0004):
* ``LedgerNotRecorded`` -- certainly not recorded: local validation failed, the ledger is not configured, the
  connection was never made, a 4xx other than 409, or ledger-rust's own load-shed 503 (its exact body);
* ``LedgerRecordError`` (``took_effect = "unknown"``) -- sent, and the ledger may hold it: reply lost, any other 5xx,
  409 (a different record under this id).
Payloads never leave this process: the ledger receives their SHA-256 only.
"""

from __future__ import annotations

import hashlib
import json
import os
import string
from dataclasses import dataclass, field
from typing import Optional

import httpx

DEPARTMENT = "fulfillment"
LEDGER_SHED_BODY = {"error": "ledger-rust is at its connection limit; retry shortly"}
LEDGER_ENTRIES_MAX_BYTES = 256 * 1024 * 1024
# ledger-rust's field rules (event.rs), as plain character sets: no regex (fix wave 4's linear-time inventory)
_ID_CHARS = frozenset(string.ascii_letters + string.digits + "._:-")
_NAME_CHARS = frozenset(string.ascii_lowercase + string.digits + "_")


def is_ledger_id(v: object, max_len: int = 128) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= max_len and all(c in _ID_CHARS for c in v)


def is_ledger_name(v: object) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= 64 and all(c in _NAME_CHARS for c in v)


class LedgerRecordError(Exception):
    """The record failed; whether the ledger holds it is UNKNOWN unless a subclass says otherwise."""

    took_effect: object = "unknown"


class LedgerNotRecorded(LedgerRecordError):
    took_effect = False


class LedgerQueryFailed(Exception):
    """A read of the ledger failed (GET /ledger/entries)."""


def payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def clean_summary(summary: str) -> str:
    s = "".join(" " if (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F) else c for c in summary).strip() or "-"
    return s[:280]


def field_problems(event_id: str, event_type: str, actor: str, subject_id: str) -> list[str]:
    out = []
    if not is_ledger_id(event_id):
        out.append("event_id")
    if not is_ledger_name(event_type):
        out.append("event_type")
    if not is_ledger_name(actor):
        out.append("actor")
    if not is_ledger_id(subject_id):
        out.append("subject_id")
    return out


def body_for(event_id, event_type, actor, subject_id, payload, summary) -> dict:
    return {"event_id": event_id, "department": DEPARTMENT, "event_type": event_type, "actor": actor,
            "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": clean_summary(summary)}


class HttpLedgerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0, transport: Optional[httpx.BaseTransport] = None):
        if not base_url or not token:
            raise ValueError("HttpLedgerClient needs LEDGER_SERVICE_URL and LEDGER_SERVICE_TOKEN")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def record_event(self, event_id, event_type, actor, subject_id, payload, summary) -> None:
        bad = field_problems(event_id, event_type, actor, subject_id)
        if bad:
            raise LedgerNotRecorded(f"event fails the ledger contract on {', '.join(bad)}; not sent")
        body = body_for(event_id, event_type, actor, subject_id, payload, summary)
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as c:
                r = c.post(f"{self._base}/ledger/events", json=body, headers={"Authorization": f"Bearer {self._token}"})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.InvalidURL,
                httpx.UnsupportedProtocol, httpx.LocalProtocolError) as exc:
            raise LedgerNotRecorded(f"ledger unreachable ({type(exc).__name__}); nothing was sent") from None
        except httpx.HTTPError as exc:
            raise LedgerRecordError(f"no reply from the ledger after sending ({type(exc).__name__})") from None
        if r.status_code in (200, 201):
            return
        if r.status_code == 409:
            raise LedgerRecordError("ledger holds a DIFFERENT record under this event id (409)")
        if 400 <= r.status_code < 500:
            raise LedgerNotRecorded(f"ledger refused the event (HTTP {r.status_code})")
        if r.status_code == 503:
            try:
                if r.json() == LEDGER_SHED_BODY:
                    raise LedgerNotRecorded("ledger shed the request at its connection limit (request not read)")
            except ValueError:
                pass
        raise LedgerRecordError(f"ledger answered HTTP {r.status_code}; the event may or may not be recorded")

    def entries(self) -> list[dict]:
        try:
            with httpx.Client(timeout=max(self._timeout, 30.0), transport=self._transport) as c:
                with c.stream("GET", f"{self._base}/ledger/entries",
                              headers={"Authorization": f"Bearer {self._token}"}) as r:
                    if r.status_code != 200:
                        raise LedgerQueryFailed(f"ledger could not be read: HTTP {r.status_code}")
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > LEDGER_ENTRIES_MAX_BYTES:
                            raise LedgerQueryFailed("ledger entries larger than the read cap")
                        chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        except LedgerQueryFailed:
            raise
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            raise LedgerQueryFailed(f"ledger could not be read: {type(exc).__name__}") from None
        if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
            raise LedgerQueryFailed("ledger entries were not a list of objects")
        return data


class UnconfiguredLedgerClient:
    """LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset: every record fails, so no consequential action happens."""

    def record_event(self, *a, **k) -> None:
        raise LedgerNotRecorded("evidence ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
                                "no dial or write-back happens without a record")

    def entries(self) -> list[dict]:
        raise LedgerQueryFailed("ledger not configured")


@dataclass
class FakeLedgerClient:
    """Test double with ledger-rust's observable rules: validation, 200 on an identical retry, 409 on a conflict."""

    events: list = field(default_factory=list)
    fail: bool = False

    def record_event(self, event_id, event_type, actor, subject_id, payload, summary) -> None:
        if self.fail:
            raise LedgerNotRecorded("fake ledger configured to fail")
        if field_problems(event_id, event_type, actor, subject_id):
            raise LedgerNotRecorded("ledger refused the event (HTTP 400)")
        body = body_for(event_id, event_type, actor, subject_id, payload, summary)
        for e in self.events:
            if e["event_id"] == event_id:
                if e == body:
                    return
                raise LedgerRecordError("ledger holds a DIFFERENT record under this event id (409)")
        self.events.append(body)

    def entries(self) -> list[dict]:
        if self.fail:
            raise LedgerQueryFailed("fake ledger configured to fail")
        return [{**e, "seq": i + 1} for i, e in enumerate(self.events)]

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.events]


def ledger_from_env() -> object:
    url, token = os.environ.get("LEDGER_SERVICE_URL"), os.environ.get("LEDGER_SERVICE_TOKEN")
    return HttpLedgerClient(url, token) if url and token else UnconfiguredLedgerClient()
