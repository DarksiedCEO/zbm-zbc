"""
Evidence ledger client — BUILD_CONTRACTS.md section 2, copied from
security-py (src/ledger.py, itself from verification-py and compliance-py), department ``sales``.

``record_event(event_id, department, event_type, actor, subject_id, payload,
summary) -> None`` raises ``LedgerRecordError`` on ANY failure. Sales records
FIRST and only then lets anything take effect: every local log line is anchored
on the ledger before it is written, and every send, consent change, suppression,
price approval and proposal approval has its own typed event recorded before
that. On failure the API answers 503 and nothing took effect (fail closed).

Event ids are deterministic (``sl-<abbrev>-<40 hex>``): every caller
of ``Recorder.record`` passes an id derived from the operation's own identity
(request_id, record id), so a retry sends the same id and gets the ledger's
200 (identical content) instead of a duplicate.

Implementations here: ``HttpLedgerClient`` (real HTTP against ledger-rust)
and ``UnconfiguredLedgerClient`` (default when LEDGER_SERVICE_URL /
LEDGER_SERVICE_TOKEN are unset: every record fails, so nothing is ever
issued unrecorded). The in-memory fake lives in tests/helpers.py only.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any, Protocol

import httpx

DEPARTMENT = "sales"
SUMMARY_MAX = 280
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NAME_RE = re.compile(r"[a-z0-9_]{1,64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


class LedgerRecordError(Exception):
    """The record failed; the action that needed it did not take effect here.
    ``took_effect`` says whether the LEDGER may hold it ("unknown") or
    certainly does not (False)."""

    took_effect: bool | str = "unknown"


class LedgerQueryFailed(Exception):
    """The ledger could not be READ (GET /ledger/entries): nothing can be verified against it."""


class LedgerNotRecorded(LedgerRecordError):
    took_effect = False


class LedgerConflict(LedgerRecordError):
    """409: the ledger holds a DIFFERENT record under this deterministic id."""

    took_effect = "unknown"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def payload_sha256(payload: dict) -> str:
    """BUILD_CONTRACTS §2 formula, exactly."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def event_field_problems(event_id, department, event_type, actor, subject_id, summary) -> list[str]:
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


def clean_summary(summary: str) -> str:
    cleaned = _SURROGATE_RE.sub(" ", _CONTROL_RE.sub(" ", str(summary)))
    cleaned = " ".join(cleaned.split()) or "(no summary)"
    return cleaned[:SUMMARY_MAX]


LEDGER_ENTRIES_MAX_BYTES = 256 * 1024 * 1024
LEDGER_SHED_BODY = {"error": "ledger-rust is at its connection limit; retry shortly"}


class LedgerClient(Protocol):
    def record_event(self, event_id: str, department: str, event_type: str, actor: str, subject_id: str,
                     payload: dict, summary: str) -> None: ...

    def verify(self) -> bool:
        """True only when GET /ledger/verify answered 200 {"valid": true}."""
        ...

    def entries(self) -> list[dict]:
        """Every ledger entry in ledger order (GET /ledger/entries); raises LedgerQueryFailed."""
        ...


class HttpLedgerClient:
    """Real client for ledger-rust ``POST /ledger/events`` and ``GET /ledger/verify``."""

    def __init__(self, base_url: str, token: str, timeout: float = 5, transport: httpx.BaseTransport | None = None):
        if not base_url or not token:
            raise ValueError("HttpLedgerClient needs both a base URL and a token")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        problems = event_field_problems(event_id, department, event_type, actor, subject_id, summary)
        if problems:
            raise LedgerNotRecorded(f"ledger event fails contract validation on: {', '.join(problems)}")
        body = {"event_id": event_id, "department": department, "event_type": event_type, "actor": actor,
                "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resp = client.post(f"{self._base_url}/ledger/events", json=body,
                                   headers={"Authorization": f"Bearer {self._token}"})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.InvalidURL, httpx.UnsupportedProtocol, ValueError) as exc:
            raise LedgerNotRecorded(f"ledger unreachable: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            raise LedgerRecordError(f"ledger response lost: {type(exc).__name__} (the ledger may have recorded it)") from exc
        if resp.status_code in (200, 201):
            return
        if resp.status_code == 409:
            raise LedgerConflict("ledger already holds a DIFFERENT record under this event id (409)")
        if 400 <= resp.status_code < 500:
            raise LedgerNotRecorded(f"ledger refused the event: HTTP {resp.status_code}")
        try:
            shed = resp.status_code == 503 and resp.json() == LEDGER_SHED_BODY
        except ValueError:
            shed = False
        if shed:
            raise LedgerNotRecorded("ledger shed the request at its connection limit (HTTP 503)")
        raise LedgerRecordError(f"ledger answered HTTP {resp.status_code} (the ledger may have recorded it)")

    def verify(self) -> bool:
        try:
            with httpx.Client(timeout=max(self._timeout, 30), transport=self._transport) as client:
                resp = client.get(f"{self._base_url}/ledger/verify", headers={"Authorization": f"Bearer {self._token}"})
            return resp.status_code == 200 and resp.json().get("valid") is True
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    def entries(self) -> list[dict]:
        """GET /ledger/entries (ledger-rust has no filtered read), size-capped. Used to verify the local
        log against the ledger at start-up and by GET /sales/v1/audit/integrity."""
        try:
            with httpx.Client(timeout=max(self._timeout, 30), transport=self._transport) as client:
                with client.stream("GET", f"{self._base_url}/ledger/entries",
                                   headers={"Authorization": f"Bearer {self._token}"}) as resp:
                    if resp.status_code != 200:
                        raise LedgerQueryFailed(f"ledger could not be read: HTTP {resp.status_code}")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
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
    """Default when the ledger isn't configured: every record fails (fail closed)."""

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary) -> None:
        raise LedgerNotRecorded("evidence ledger not configured (LEDGER_SERVICE_URL / LEDGER_SERVICE_TOKEN unset); "
                                "nothing can take effect without a ledger record")

    def verify(self) -> bool:
        return False

    def entries(self) -> list[dict]:
        raise LedgerQueryFailed("evidence ledger not configured; it can't be read")


def ledger_from_env(env: dict) -> LedgerClient:
    if env.get("LEDGER_SERVICE_URL") and env.get("LEDGER_SERVICE_TOKEN"):
        return HttpLedgerClient(env["LEDGER_SERVICE_URL"], env["LEDGER_SERVICE_TOKEN"])
    return UnconfiguredLedgerClient()


def derived_id(prefix: str, *parts: Any) -> str:
    """``sl-<prefix>-<40 hex>``: deterministic from the operation identity."""
    digest = hashlib.sha256(canonical(list(parts)).encode("utf-8", "surrogatepass")).hexdigest()[:40]
    return f"sl-{prefix}-{digest}"


class Recorder:
    """Every ledger write of the service goes through here .
    Validates the §2 field rules locally first, so a bad field is a loud
    local failure, not an opaque 400."""

    def __init__(self, client: LedgerClient):
        self.client = client
        self.lock = threading.RLock()

    def record(self, event_id: str, event_type: str, actor: str, subject_id: str, payload: dict, summary: str) -> str:
        clean = clean_summary(summary)
        problems = event_field_problems(event_id, DEPARTMENT, event_type, actor, subject_id, clean)
        if problems:
            raise LedgerNotRecorded(f"ledger event would be refused: {', '.join(problems)}")
        self.client.record_event(event_id=event_id, department=DEPARTMENT, event_type=event_type, actor=actor,
                                 subject_id=subject_id, payload=payload, summary=clean)
        return event_id

    def try_record(self, *a, **k) -> str | None:
        """Best effort, for REFUSALS only (a refusal stands whether or not it was recorded)."""
        try:
            return self.record(*a, **k)
        except LedgerRecordError:
            return None


def env_flag(env: dict, name: str) -> bool:
    return (env.get(name) or "").strip() == "1"


__all__ = ["DEPARTMENT", "HttpLedgerClient", "LedgerClient", "LedgerConflict", "LedgerNotRecorded", "LedgerQueryFailed",
           "LedgerRecordError",
           "Recorder", "UnconfiguredLedgerClient", "canonical", "derived_id", "ledger_from_env", "payload_sha256"]
