"""Evidence ledger client for store reads (ADR 0020 section 7; BUILD_CONTRACTS section 2, after seo-py ledger.py).

Record-first: ``rr_store_read_requested`` is on the ledger BEFORE the first outbound byte to a store; without it the
sync stops with LEDGER_UNAVAILABLE and nothing is read. The outcome is recorded as ``rr_store_read_completed`` (the
snapshot is kept only once this is recorded) or ``rr_store_read_failed`` (best effort, with the reason code).

Department ``rr_store`` (not ``revenue_recovery``: that department's ids and formats belong to orchestrator-go, ADR
0003 section 13). Event ids are deterministic (``rrs.<sync_id>.<step>``), so a retried append is the ledger's 200,
never a duplicate. Payloads hold counts, codes, the shop handle and the snapshot digest: never a token, a secret
path, an order line or a customer field. The ledger stores only the payload's SHA-256 and the summary.

Configuration: ``DETECTION_LEDGER_URL`` and ``DETECTION_LEDGER_TOKEN_FILE`` (``*_FILE`` rules). Unset: every record
fails, so no store can be read (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional, Protocol

import httpx

from .config import Secret, read_secret_file
from .reasons import StoreError

DEPARTMENT = "rr_store"
ACTOR = "detection_py"
SUMMARY_MAX = 280
_EVENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NAME = re.compile(r"[a-z0-9_]{1,64}")
_SUMMARY = re.compile(r"[\x20-\x7e]{1,280}")


class LedgerNotRecorded(Exception):
    pass


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


class Ledger(Protocol):
    def record(self, event_id: str, event_type: str, subject_id: str, payload: dict, summary: str) -> None: ...


def _check(event_id: str, event_type: str, subject_id: str, summary: str) -> None:
    if not (_EVENT_ID.fullmatch(event_id) and _NAME.fullmatch(event_type) and _EVENT_ID.fullmatch(subject_id)
            and _SUMMARY.fullmatch(summary)):
        raise LedgerNotRecorded("ledger event fails the contract")


class HttpLedger:
    def __init__(self, base_url: str, token: Secret, timeout: float = 5.0,
                 transport: Optional[httpx.BaseTransport] = None):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("ledger URL must be http(s)")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        return f"HttpLedger({self._base!r})"

    def record(self, event_id, event_type, subject_id, payload, summary) -> None:
        _check(event_id, event_type, subject_id, summary)
        body = {"event_id": event_id, "department": DEPARTMENT, "event_type": event_type, "actor": ACTOR,
                "subject_id": subject_id, "payload_sha256": payload_sha256(payload), "summary": summary}
        for _ in range(2):                        # deterministic id: a retry is idempotent on the ledger
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport, trust_env=False) as c:
                    r = c.post(f"{self._base}/ledger/events", json=body,
                               headers={"Authorization": f"Bearer {self._token.reveal()}"})
            except httpx.HTTPError:
                continue
            if r.status_code in (200, 201):
                return
            if r.status_code < 500:
                break
        raise LedgerNotRecorded("the ledger did not record the event")


class UnconfiguredLedger:
    def __repr__(self) -> str:
        return "UnconfiguredLedger()"

    def record(self, event_id, event_type, subject_id, payload, summary) -> None:
        raise LedgerNotRecorded("evidence ledger not configured")


def ledger_from_env(env) -> Ledger:
    url = (env.get("DETECTION_LEDGER_URL") or "").strip()
    token_file = (env.get("DETECTION_LEDGER_TOKEN_FILE") or "").strip()
    if not url or not token_file:
        return UnconfiguredLedger()
    try:
        return HttpLedger(url, read_secret_file(token_file))
    except (StoreError, ValueError):
        return UnconfiguredLedger()
