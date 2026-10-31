"""
Compliance's thin client to Legal (37) (ADR 0024, HR-15). Wired only when COMPLIANCE_LEGAL_URL, _TOKEN and
_CALLER_TOKEN are all set (config.py refuses a partial set); otherwise ports.NotBuiltLegal37 answers unavailable.
Caller name at Legal: ``compliance_38`` (legal-py CALLER_NAMES). Reads only, never writes:

- ``GET /legal/v1/documents/{doc_id}/current`` (legal-py ``current_answer``, the section E.1 protocol answer):
  ``current_version`` and ``doc_sha256`` of the highest approved, effective, unexpired version;
- ``GET /legal/v1/acceptances/{acceptance_id}`` (legal-py ``acceptance_view``): the acceptance's doc, version,
  hash, party and whether its evidence is sufficient.

Fail closed (finance-py ``clients.py`` pattern): a 10 s total wall-clock budget per read, both attempts included
(the exchange runs in a daemon thread and the caller waits at most the remaining budget); one retry on a transport
error or a 5xx while budget remains; a 256 KiB answer cap; a compressed answer, a redirect, any non-200, a parse
failure, an answer about another document or acceptance, or a Legal running on an unpinned (non-production) rules
seed -> unavailable or NO, never a pass.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

import httpx

from ports import ClientDocument, DocVersion

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 256 * 1024
CALLER_HEADER = "X-LEGAL-Caller-Token"
_DOC = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}")
_ACC = re.compile(r"lg-[a-z]{3}-[0-9A-Z]{26}")
_SHA = re.compile(r"[0-9a-f]{64}")
_PARTY = re.compile(r"[A-Za-z0-9._:-]{1,140}")


class HttpLegal37:
    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", CALLER_HEADER: caller_token,
                         "Accept-Encoding": "identity"}
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:                      # never the tokens
        return f"HttpLegal37({self.base_url!r})"

    # --- one bounded read -------------------------------------------------------------------------------------------

    def _exchange(self, path: str, remaining: float, cancel: threading.Event):
        try:
            with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=self._transport,
                              follow_redirects=False) as c:
                with c.stream("GET", self.base_url + path, headers=self._headers) as resp:
                    if resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                        return "error", None
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        if cancel.is_set():
                            return "error", None
                        buf += chunk
                        if len(buf) > MAX_RESPONSE_BYTES:
                            return "error", None
                    return resp.status_code, bytes(buf)
        except Exception:  # noqa: BLE001 - any transport failure is "unavailable"
            return "transport", None

    def _get(self, path: str) -> Optional[dict]:
        deadline = time.monotonic() + self._timeout
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            box: dict = {}
            cancel = threading.Event()
            t = threading.Thread(target=lambda: box.setdefault("r", self._exchange(path, remaining, cancel)),
                                 name="cmp-legal-client", daemon=True)
            t.start()
            t.join(max(0.0, remaining))
            if t.is_alive():
                cancel.set()
                return None
            code, raw = box.get("r", ("error", None))
            if code == 200:
                try:
                    d = json.loads(raw)
                except ValueError:
                    return None
                return d if isinstance(d, dict) else None
            if code == "transport" or (isinstance(code, int) and code >= 500):
                continue
            return None
        return None

    # --- Legal37Port ------------------------------------------------------------------------------------------------

    def current_version(self, doc_id: str) -> DocVersion:
        if not isinstance(doc_id, str) or not _DOC.fullmatch(doc_id):
            return DocVersion(False, reason="not a Legal document id")
        d = self._get(f"/legal/v1/documents/{doc_id}/current")
        try:
            if d is None or d["doc_id"] != doc_id or d["rules_pinned"] is not True or d["available"] is not True:
                return DocVersion(False, reason="Legal (37) cannot confirm the current version")
            return DocVersion(True, d["current_version"] if isinstance(d["current_version"], str) else None)
        except (KeyError, TypeError):
            return DocVersion(False, reason="Legal's answer was not understood")

    def client_document(self, doc_id: str, version: str, doc_sha256: str, acceptance_id: str,
                        party_ref: str) -> ClientDocument:
        if not (isinstance(doc_id, str) and _DOC.fullmatch(doc_id) and isinstance(version, str)
                and _VERSION.fullmatch(version) and isinstance(doc_sha256, str) and _SHA.fullmatch(doc_sha256)
                and isinstance(party_ref, str) and _PARTY.fullmatch(party_ref)):
            return ClientDocument(True, False, False, reason="not a Legal document reference")
        cur = self._get(f"/legal/v1/documents/{doc_id}/current")
        try:
            if cur is None or cur["doc_id"] != doc_id or cur["available"] is not True:
                return ClientDocument(False, reason="Legal (37) cannot confirm the current version")
            if cur["rules_pinned"] is not True:
                return ClientDocument(False, reason="Legal is running on an unpinned (non-production) rules seed")
            current = cur["current_version"] == version and cur["doc_sha256"] == doc_sha256
        except (KeyError, TypeError):
            return ClientDocument(False, reason="Legal's current-version answer was not understood")
        if not (isinstance(acceptance_id, str) and _ACC.fullmatch(acceptance_id)):
            return ClientDocument(True, current, False, reason="not a Legal acceptance id")
        a = self._get(f"/legal/v1/acceptances/{acceptance_id}")
        try:
            if a is None:
                return ClientDocument(False, reason="Legal (37) cannot be read (or holds no such acceptance)")
            if a["rules_pinned"] is not True:
                return ClientDocument(False, reason="Legal is running on an unpinned (non-production) rules seed")
            accepted = (a["acceptance_id"] == acceptance_id and a["doc_id"] == doc_id and a["version"] == version
                        and a["doc_sha256"] == doc_sha256 and a["evidence_sufficient"] is True
                        and a["party_ref"] == party_ref)
        except (KeyError, TypeError):
            return ClientDocument(False, reason="Legal's acceptance answer was not understood")
        return ClientDocument(True, current, accepted)


__all__ = ["HttpLegal37"]
