"""
Finance's thin clients to the two departments that exist (spec §D.2): Verification and Integrity and Compliance (38).
Wired only when all three settings of a client are present (``FIN_VI_*`` / ``FIN_COMPLIANCE_*``); otherwise the
fail-closed stand-ins of ``ports.py`` answer.

Pattern of verification-py ``compliance_client.py`` (AEGIS N14-7 / N16-1): a 10 s TOTAL WALL-CLOCK budget per call,
both attempts included (the exchange runs in a daemon thread and the caller waits at most the remaining budget, so a
server dripping bytes cannot stretch it); one retry on a transport error or 5xx while budget remains; 1 MiB answer
cap; a compressed answer is refused; no redirects; any non-200, parse failure or inconsistent answer (a different
subject id than asked) -> the NEGATIVE answer (unavailable), never a pass.

Not reachable yet (reported in ADR 0009 "changes other services must make"): Compliance has no sanctions *status*
read for ``finance_31`` (``GET /compliance/v1/sanctions/status`` is proposed), so ``sanctions_status`` answers
unavailable even when wired; the jurisdiction answer uses ``POST /compliance/v1/jurisdictions/resolve`` only when
Compliance lets ``finance_31`` call it (today it does not: unavailable).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from clock import parse_iso

from ports import (Certification, ClawbackPage, ComplianceRuling, HoldsAnswer, JurisdictionAnswer, LegalAnswer,
                   RegisterRow, SanctionsAnswer)

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_OID = re.compile(r"[A-Z0-9][A-Z0-9-]{1,39}")
_LEGAL_DOC = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LEGAL_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}")
_LEGAL_ACC = re.compile(r"lg-[a-z]{3}-[0-9A-Z]{26}")
_SHA = re.compile(r"[0-9a-f]{64}")


def _run_bounded(fn, seconds: float):
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="fin-thin-client", daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        cancel.set()
        return None
    return box.get("r", ("error", "no answer", None))


def _exchange(method: str, url: str, headers: dict, transport, remaining: float, cancel: threading.Event,
              params: Optional[dict] = None):
    try:
        with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=transport, follow_redirects=False) as c:
            with c.stream(method, url, headers={**headers, "Accept-Encoding": "identity"}, params=params) as resp:
                enc = resp.headers.get("content-encoding", "identity").strip().lower()
                if enc not in ("", "identity"):
                    return "error", "encoded answer", None
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    return "too_large", resp.status_code, None
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    if cancel.is_set():
                        return "error", "deadline", None
                    buf += chunk
                    if len(buf) > MAX_RESPONSE_BYTES:
                        return "too_large", resp.status_code, None
                return "ok", resp.status_code, bytes(buf)
    except httpx.HTTPError as exc:
        return "error", type(exc).__name__, None


class _Base:
    CALLER_HEADER = ""

    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S):
        if not (base_url and service_token and caller_token):
            raise ValueError("a thin client needs a URL, the service token and the caller token")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", self.CALLER_HEADER: caller_token}
        self._transport = transport
        self._timeout = timeout

    def _get(self, path: str, params: Optional[dict] = None) -> Optional[object]:
        """The parsed JSON of a 200 answer, or None (negative) on anything else."""
        deadline = time.monotonic() + self._timeout
        url = f"{self._base}{path}"
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            res = _run_bounded(lambda cancel, r=remaining: _exchange("GET", url, self._headers, self._transport, r,
                                                                     cancel, params), remaining)
            if res is None:
                break
            kind, status, body = res
            if kind == "error":
                continue
            if kind == "too_large":
                return None
            if status >= 500:
                continue
            if status != 200:
                return None
            try:
                return json.loads(body)
            except ValueError:
                return None
        return None


class HttpVerification(_Base):
    """V&I reads for caller ``finance_31`` (it already exists in verification-py)."""

    CALLER_HEADER = "X-VI-Caller-Token"

    def certification(self, submission_id: str) -> Certification:
        if not _ID.fullmatch(submission_id or ""):
            return Certification(False)
        d = self._get(f"/vi/v1/submissions/{submission_id}/certification")
        try:
            if not isinstance(d, dict) or d["submission_id"] != submission_id:
                return Certification(False)
            views = d.get("certified_views")
            if views is not None and (isinstance(views, bool) or not isinstance(views, int)):
                return Certification(False)
            w = d.get("window") or {}
            return Certification(True, submission_id, d["certification_id"], d["status"], views, d["campaign_id"],
                                 d["clipper_id"], d.get("platform"), w.get("create_time"), d.get("certified_at"),
                                 bool(d.get("status") in ("certified", "revised") and d.get("reasons")
                                      and any(r.get("code") not in ("REVISED_DOWN",) for r in d["reasons"])),
                                 d.get("rules_version"))
        except (KeyError, TypeError, AttributeError):
            return Certification(False)

    def clawbacks(self, cursor: int) -> ClawbackPage:
        d = self._get("/vi/v1/clawbacks", {"cursor": int(cursor)})
        try:
            if not isinstance(d, dict) or not isinstance(d["items"], list):
                return ClawbackPage(False)
            nxt = d.get("next_cursor")
            if nxt is not None and (isinstance(nxt, bool) or not isinstance(nxt, int)):
                return ClawbackPage(False)
            items = tuple({k: x.get(k) for k in ("clawback_id", "certification_id", "views_delta", "cause", "rule_id")}
                          for x in d["items"] if isinstance(x, dict))
            return ClawbackPage(True, items, nxt)
        except (KeyError, TypeError, AttributeError):
            return ClawbackPage(False)


class HttpCompliance(_Base):
    """Compliance reads for caller ``finance_31`` (it already exists in compliance-py)."""

    CALLER_HEADER = "X-Compliance-Caller-Token"

    def ruling(self, ruling_id: str) -> ComplianceRuling:
        if not _ID.fullmatch(ruling_id or ""):
            return ComplianceRuling(False)
        d = self._get(f"/compliance/v1/rulings/{ruling_id}")
        try:
            if not isinstance(d, dict) or d["ruling_id"] != ruling_id or not isinstance(d["allowed"], bool):
                return ComplianceRuling(False)
            return ComplianceRuling(True, ruling_id, d["gate"], d["subject_id"], d["allowed"], d.get("evaluated_at"),
                                    d.get("register_version"))
        except (KeyError, TypeError):
            return ComplianceRuling(False)

    def holds(self, subjects: tuple) -> HoldsAnswer:
        d = self._get("/compliance/v1/holds")
        if not isinstance(d, list):
            return HoldsAnswer(False)
        ids = {s[1] for s in subjects}
        try:
            open_ = tuple(sorted(h["hold_id"] for h in d if isinstance(h, dict) and h.get("status") == "open"
                                 and h.get("subject_id") in ids))
        except (KeyError, TypeError):
            return HoldsAnswer(False)
        return HoldsAnswer(True, open_)

    def sanctions_status(self, subject_id: str, role: str) -> SanctionsAnswer:
        return SanctionsAnswer(False, reason="Compliance has no sanctions status read for finance_31 yet")

    def row(self, obligation_id: str) -> RegisterRow:
        if not _OID.fullmatch(obligation_id or ""):
            return RegisterRow(False, str(obligation_id)[:40])
        d = self._get(f"/compliance/v1/register/{obligation_id}")
        try:
            row = d["row"]
            if row["id"] != obligation_id:
                return RegisterRow(False, obligation_id)
            return RegisterRow(True, obligation_id, row["effective_status"], row.get("expires_at"), d["register_version"])
        except (KeyError, TypeError):
            return RegisterRow(False, obligation_id)

    def jurisdiction(self, country: str, region: Optional[str]) -> JurisdictionAnswer:
        return JurisdictionAnswer(False, reason="Compliance's resolve route is not open to finance_31 yet")


class HttpLegal(_Base):
    """Legal (37) reads for caller ``finance_31``: the contract version and the client's acceptance of it.

    Legal's acceptance rule for "in force" (legal-py ``_in_force``) is applied to the version Legal returns: status
    ``approved`` and ``effective_at <= now < review_by``. That is per version, not Legal's "current" (the highest in
    force): per-party fills share a doc_id, so an older version a client accepted stays valid until Legal retires it or
    its ``review_by`` passes -- retiring is the control, and after ``review_by`` invoices under it are refused until
    Legal renews it. Anything Legal does not confirm -- a different hash,
    another entity's document, an acceptance by another party, insufficient evidence, a Legal instance running on an
    unpinned (non-production) rules seed -- is a NO; a Legal that cannot be read, or that answers 404 (a document or
    acceptance it does not hold), is "unavailable" (reason DEPENDENCY_UNAVAILABLE:legal_37). Either way: refused."""

    CALLER_HEADER = "X-LEGAL-Caller-Token"

    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S, now=None):
        super().__init__(base_url, service_token, caller_token, transport, timeout)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def document_status(self, doc_id, version, doc_sha256, acceptance_id, party_ref=None, entity=None):
        ver = f"{version}.0" if isinstance(version, int) and not isinstance(version, bool) else version
        if not (isinstance(doc_id, str) and _LEGAL_DOC.fullmatch(doc_id) and isinstance(ver, str)
                and _LEGAL_VERSION.fullmatch(ver) and isinstance(doc_sha256, str) and _SHA.fullmatch(doc_sha256)):
            return LegalAnswer(True, False, False, reason="not a Legal document reference")
        v = self._get(f"/legal/v1/documents/{doc_id}/versions/{ver}")
        if not isinstance(v, dict):
            return LegalAnswer(False, reason="Legal could not be read (or has no such version)")
        try:
            now = self._now()
            current = (v["doc_id"] == doc_id and v["version"] == ver and v["sha256"] == doc_sha256
                       and v["status"] == "approved" and isinstance(v.get("effective_at"), str)
                       and isinstance(v.get("review_by"), str)
                       and parse_iso(v["effective_at"]) <= now < parse_iso(v["review_by"])
                       and (entity is None or v["entity"] == entity))
        except (KeyError, TypeError, ValueError):
            return LegalAnswer(False, reason="Legal's version answer was not understood")
        if not (isinstance(acceptance_id, str) and _LEGAL_ACC.fullmatch(acceptance_id)):
            return LegalAnswer(True, current, False, reason="not a Legal acceptance id")
        a = self._get(f"/legal/v1/acceptances/{acceptance_id}")
        if not isinstance(a, dict):
            return LegalAnswer(False, reason="Legal could not be read (or has no such acceptance)")
        try:
            if a["rules_pinned"] is not True:
                return LegalAnswer(False, reason="Legal is running on an unpinned (non-production) rules seed")
            accepted = (a["acceptance_id"] == acceptance_id and a["doc_id"] == doc_id and a["version"] == ver
                        and a["doc_sha256"] == doc_sha256 and a["evidence_sufficient"] is True
                        and (party_ref is None or a["party_ref"] == party_ref))
        except (KeyError, TypeError):
            return LegalAnswer(False, reason="Legal's acceptance answer was not understood")
        return LegalAnswer(True, current, accepted)
