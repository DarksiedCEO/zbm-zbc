"""
HTTP thin clients for the three departments that have (or will have) a CN
contract: Verification and Integrity, Compliance (38), Creative Production
(spec §E "Thin clients"). Pattern: creative-py ``shared/compliance38.py``
(ADR 0006 decision 11 + N14-7/8/10, N15-8):

- 10 s TOTAL wall-clock budget per call, both attempts included, however
  slowly bytes arrive; one retry with the SAME request (same request_id) on a
  transport error or a 5xx while budget remains;
- answers over 1 MiB are refused unparsed; a compressed answer is refused;
  redirects are not followed;
- any non-2xx, timeout, parse failure or inconsistent answer (a missing echo
  of the request_id / facts_sha256 / subject, a wrong type) maps to the
  port's NEGATIVE answer (``available=False``) — never a pass, never an
  exception;
- ``rules_pinned`` / ``seed_pinned`` false is refused unless the operator
  set CN_VI_ACCEPT_UNPINNED / CN_COMPLIANCE_ACCEPT_UNPINNED=1.

Each is used only when its URL, service token (and caller token, for V&I and
Compliance) are ALL set (config.py refuses a partial set); otherwise the
fail-closed stand-in in ports.py stays.

Contract notes. V&I: this client reads V&I's real wire shapes and the
routes it needs exist in verification-py (AEGIS N16-6: ``status`` and
``attestation_id`` on age answers, ``GET /vi/v1/findings/{id}``, ``GET
/vi/v1/certifications?clipper_id=``, request_id echo on every write);
``tests/test_contract_vi.py`` runs it against V&I's app. Still listed in ADR
0008 "Changes other services must make": Compliance ``GET
/compliance/v1/activations/{lane}/{subject_id}/latest`` and the
``clipper_network`` caller name; Creative ``GET /zbc/campaigns/{id}/kit``.
Until they exist the answer is non-200 → unavailable → blocked.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ports import (Ack, AgeAnswer, Certification, CertificationsAnswer, CompleteAnswer, ComplianceRuling, Connection,
                   ConnectionsAnswer, Finding, FindingAnswer, IdentityAnswer, IntegrityAnswer, JurisdictionAnswer,
                   KitAnswer, RulebookAnswer, StartAnswer, Strike, StrikeFeed)

TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_RULE_LINE_MAX = 400


def facts_sha256(facts: Any) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def _is_id(v) -> bool:
    return isinstance(v, str) and bool(_ID.fullmatch(v))


def _ids(v, limit=100) -> Optional[tuple[str, ...]]:
    if not isinstance(v, list) or len(v) > limit or not all(_is_id(x) for x in v):
        return None
    return tuple(v)


def _run_bounded(fn, seconds: float):
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="cn-port-call", daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        cancel.set()
        return None
    return box.get("r", ("error", "no answer", None))


def _exchange(method: str, url: str, body, params, headers: dict, transport, remaining: float,
              cancel: threading.Event):
    try:
        with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=transport,
                          follow_redirects=False) as c:
            kw: dict = {"headers": {**headers, "Accept-Encoding": "identity"}}
            if body is not None:
                kw["json"] = body
            if params:
                kw["params"] = params
            with c.stream(method, url, **kw) as resp:
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


class _Http:
    def __init__(self, base_url: str, headers: dict, transport=None, timeout: float = TIMEOUT_S):
        self._base = base_url.rstrip("/")
        self._headers = headers
        self._transport = transport
        self._timeout = timeout

    def call(self, method: str, path: str, body=None, params=None,
             extra_headers: Optional[dict] = None) -> tuple[Optional[int], Any, bytes]:
        """(status, parsed JSON or None, raw bytes). status None = no usable answer. ``extra_headers`` are sent
        with this call only (never kept on the client)."""
        headers = {**self._headers, **(extra_headers or {})}
        deadline = time.monotonic() + self._timeout
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None, b""
            res = _run_bounded(lambda cancel, r=remaining: _exchange(method, self._base + path, body, params,
                                                                      headers, self._transport, r, cancel),
                               remaining)
            if res is None:
                return None, None, b""
            kind, code, content = res
            if kind == "error":
                continue
            if kind == "too_large":
                return None, None, b""
            if code >= 500:
                continue
            try:
                data = json.loads(content) if content else None
            except MemoryError:
                raise
            except Exception:  # noqa: BLE001 - unparseable = no usable answer
                data = None
            return code, data, content
        return None, None, b""


def _pinned(data: dict, key: str, accept_unpinned: bool) -> bool:
    v = data.get(key)
    return isinstance(v, bool) and (v or accept_unpinned)


# ================================================================== Verification and Integrity


class HttpVerificationIntegrity:
    """Caller ``clipper_network`` on V&I (X-VI-Caller-Token). AEGIS N16-6: every method reads V&I's ACTUAL
    wire shape (verification-py ``service.py``) and ``tests/test_contract_vi.py`` runs this client against V&I's
    real app in-process, so a drift on either side fails a test. Every answer must state ``rules_pinned`` and
    echo what identifies the request (request_id on writes, the subject on reads)."""

    _CONN_STATUSES = ("pending", "active", "revoked", "expired", "refused")

    def __init__(self, base_url: str, service_token: str, caller_token: str, transport=None,
                 accept_unpinned: bool = False, timeout: float = TIMEOUT_S):
        self._h = _Http(base_url, {"Authorization": f"Bearer {service_token}", "X-VI-Caller-Token": caller_token},
                        transport, timeout)
        self._unpinned = accept_unpinned

    def _ok(self, data) -> bool:
        return isinstance(data, dict) and _pinned(data, "rules_pinned", self._unpinned)

    @staticmethod
    def _lines(v, limit=10) -> tuple[str, ...]:
        return tuple(str(x)[:200] for x in v[:limit]) if isinstance(v, list) else ()

    def age_subject(self, clipper_id):
        """``GET /vi/v1/age/subjects/{id}`` → ``{allowed, status, attestation_id, unmet, detail, subject_id,
        rules_pinned}``. ``allowed`` is the authority (N16-9): adult only when ``allowed`` is true AND ``status``
        says adult; any contradiction between them is not adult (unavailable)."""
        code, data, _ = self._h.call("GET", f"/vi/v1/age/subjects/{quote(clipper_id, safe='')}")
        if code != 200 or not self._ok(data):
            return AgeAnswer(False, reason=f"V&I age answer unusable ({code})")
        allowed, status, att = data.get("allowed"), data.get("status"), data.get("attestation_id")
        if not isinstance(allowed, bool) or status not in ("adult", "minor", "unknown") \
                or data.get("subject_id") != clipper_id or (att is not None and not _is_id(att)) \
                or (status != "unknown" and att is None):
            return AgeAnswer(False, reason="V&I age answer inconsistent")
        if allowed != (status == "adult"):
            return AgeAnswer(False, reason="V&I age answer contradicts itself (allowed vs status): not adult")
        return AgeAnswer(True, status, att, "")

    def age_check(self, request_id, clipper_id, dob, dob_field_neutral, method, provider_session_ref):
        """``POST /vi/v1/age/checks`` → ``{request_id, facts_sha256, rules_pinned, attestation_id, subject_id,
        status, result, ...}``. ``facts_sha256`` covers what was sent WITHOUT the DOB (a hash of a DOB is
        guessable, so it is never echoed)."""
        facts = {"subject_id": clipper_id, "dob_field_neutral": dob_field_neutral, "method": method,
                 "provider_session_ref": provider_session_ref}
        code, data, _ = self._h.call("POST", "/vi/v1/age/checks", {"request_id": request_id, "dob": dob, **facts})
        if code != 200 or not self._ok(data):
            return AgeAnswer(False, reason=f"V&I age answer unusable ({code})")
        status, att = data.get("status"), data.get("attestation_id")
        if status not in ("adult", "minor", "unknown") or data.get("subject_id") != clipper_id or not _is_id(att):
            return AgeAnswer(False, reason="V&I age answer inconsistent")
        if data.get("request_id") != request_id or data.get("facts_sha256") != facts_sha256(facts):
            return AgeAnswer(False, reason="V&I age answer does not echo this request")
        return AgeAnswer(True, status, att, "")

    def identity_check(self, request_id, clipper_id, email):
        """``POST /vi/v1/identity/checks`` → ``{request_id, rules_pinned, clipper_id, status: clear | finding |
        incomplete, clear, findings}``; V&I's ``finding`` is CN's ``duplicate``."""
        code, data, _ = self._h.call("POST", "/vi/v1/identity/checks",
                                     {"request_id": request_id, "clipper_id": clipper_id, "email": email})
        if code != 200 or not self._ok(data) or data.get("request_id") != request_id or data.get("clipper_id") != clipper_id:
            return IdentityAnswer(False, reason=f"V&I identity answer unusable ({code})")
        st, fids, clear = data.get("status"), _ids(data.get("findings")), data.get("clear")
        if st not in ("clear", "finding", "incomplete") or fids is None or clear is not (st == "clear") \
                or (st == "finding") != bool(fids):
            return IdentityAnswer(False, reason="V&I identity answer inconsistent")
        return IdentityAnswer(True, "duplicate" if st == "finding" else st, fids)

    def connections(self, clipper_id):
        """``GET /vi/v1/connections?clipper_id=`` → ``{clipper_id, items: [connection views], rules_pinned}``."""
        code, data, _ = self._h.call("GET", "/vi/v1/connections", params={"clipper_id": clipper_id})
        if code != 200 or not self._ok(data) or data.get("clipper_id") != clipper_id \
                or not isinstance(data.get("items"), list) or len(data["items"]) > 50:
            return ConnectionsAnswer(False, reason=f"V&I connections answer unusable ({code})")
        out = []
        for c in data["items"]:
            if not isinstance(c, dict) or not _is_id(c.get("connection_id")) or not isinstance(c.get("platform"), str) \
                    or c.get("status") not in self._CONN_STATUSES or c.get("clipper_id") != clipper_id:
                return ConnectionsAnswer(False, reason="V&I connections answer inconsistent")
            out.append(Connection(c["connection_id"], c["platform"][:32], c["status"]))
        return ConnectionsAnswer(True, tuple(out))

    def connection_start(self, request_id, clipper_id, platform, redirect_uri):
        """``POST /vi/v1/connections/start`` → ``{request_id, rules_pinned, clipper_id, started, connection_id,
        authorization_url, state_expires_at}`` or ``{..., started: false, reason_lines}``."""
        code, data, _ = self._h.call("POST", "/vi/v1/connections/start", {"request_id": request_id,
                                                                          "clipper_id": clipper_id, "platform": platform,
                                                                          "redirect_uri": redirect_uri})
        if code != 200 or not self._ok(data) or data.get("request_id") != request_id \
                or data.get("clipper_id") != clipper_id or not isinstance(data.get("started"), bool):
            return StartAnswer(False, reasons=(f"V&I connection start unusable ({code})",))
        if data["started"] is False:
            return StartAnswer(True, False, reasons=self._lines(data.get("reason_lines")))
        url = data.get("authorization_url")
        if not _is_id(data.get("connection_id")) or not isinstance(url, str) or not url.startswith("https://") \
                or len(url) > 2048 or not isinstance(data.get("state_expires_at"), str):
            return StartAnswer(False, reasons=("V&I connection start inconsistent",))
        return StartAnswer(True, True, data["connection_id"], url, data["state_expires_at"][:40])

    def connection_complete(self, request_id, state, code_):
        """``POST /vi/v1/connections/complete`` → ``{request_id, rules_pinned, connection: {connection_id, status,
        reason_lines, ...}}``."""
        code, data, _ = self._h.call("POST", "/vi/v1/connections/complete",
                                     {"request_id": request_id, "state": state, "code": code_})
        conn = data.get("connection") if isinstance(data, dict) else None
        if code != 200 or not self._ok(data) or data.get("request_id") != request_id or not isinstance(conn, dict) \
                or conn.get("status") not in self._CONN_STATUSES or not _is_id(conn.get("connection_id")):
            return CompleteAnswer(False, reasons=(f"V&I connection complete unusable ({code})",))
        return CompleteAnswer(True, conn["connection_id"], conn["status"], self._lines(conn.get("reason_lines")))

    def connection_revoke(self, request_id, connection_id):
        """``POST /vi/v1/connections/{id}/revoke`` → ``{request_id, rules_pinned, connection: {..., status}}``."""
        code, data, _ = self._h.call("POST", f"/vi/v1/connections/{quote(connection_id, safe='')}/revoke",
                                     {"request_id": request_id})
        conn = data.get("connection") if isinstance(data, dict) else None
        ok = code == 200 and self._ok(data) and data.get("request_id") == request_id and isinstance(conn, dict) \
            and conn.get("connection_id") == connection_id and conn.get("status") == "revoked"
        return Ack(ok, ok, connection_id if ok else None, "" if ok else f"V&I revoke unusable ({code})")

    def integrity(self, clipper_id):
        """``GET /vi/v1/clippers/{id}/integrity`` → V&I's integrity FACTS (strikes_active, banned, open_holds, ...,
        rules_pinned). CN's admission check (spec C.2 "no open V&I S3 / hold on identity") is clear only when the
        clipper is not banned, has no active S3 and no open clipper-level hold."""
        code, data, _ = self._h.call("GET", f"/vi/v1/clippers/{quote(clipper_id, safe='')}/integrity")
        if code != 200 or not self._ok(data) or data.get("clipper_id") != clipper_id:
            return IntegrityAnswer(False, reasons=(f"V&I integrity answer unusable ({code})",))
        active, banned, holds = data.get("strikes_active"), data.get("banned"), data.get("open_holds")
        s3 = active.get("S3") if isinstance(active, dict) else None
        if not isinstance(banned, bool) or not isinstance(s3, int) or isinstance(s3, bool) or s3 < 0 \
                or _ids(holds, 500) is None:
            return IntegrityAnswer(False, reasons=("V&I integrity answer inconsistent",))
        reasons = ((["banned at V&I"] if banned else []) + ([f"{s3} active S3 strike(s)"] if s3 else [])
                   + ([f"{len(holds)} open hold(s) on the clipper"] if holds else []))
        return IntegrityAnswer(True, not reasons, tuple(reasons))

    def strikes(self, cursor):
        """``GET /vi/v1/strikes?cursor=`` → ``{items: [strike + evidence_ids + subject_refs], next_cursor: int |
        null, rules_pinned}``. An ``active`` strike whose ``expires_at`` has passed is read as ``expired``."""
        if cursor is not None and not (isinstance(cursor, str) and cursor.isdigit() and len(cursor) <= 13):
            return StrikeFeed(False, reason="strike feed cursor invalid")
        code, data, _ = self._h.call("GET", "/vi/v1/strikes", params={"cursor": cursor} if cursor else None)
        if code != 200 or not self._ok(data) or not isinstance(data.get("items"), list) or len(data["items"]) > 500:
            return StrikeFeed(False, reason=f"V&I strike feed unusable ({code})")
        now = datetime.now(timezone.utc)
        out = []
        for s in data["items"]:
            try:
                fids, eids = _ids(s["finding_ids"], 50), _ids(s["evidence_ids"], 50)
                refs = _ids(s.get("subject_refs", []), 50)
                exp = s.get("expires_at")
                if not (_is_id(s["strike_id"]) and _is_id(s["clipper_id"]) and s["class"] in ("S1", "S2", "S3")
                        and s["status"] in ("active", "expired", "overturned") and _is_id(s["rule_id"])
                        and fids is not None and eids is not None and refs is not None
                        and isinstance(s["issued_at"], str) and (exp is None or isinstance(exp, str))):
                    return StrikeFeed(False, reason="V&I strike feed inconsistent")
                status = s["status"]
                if status == "active" and exp is not None and _utc(exp) is not None and _utc(exp) <= now:
                    status = "expired"
                out.append(Strike(s["strike_id"], s["clipper_id"], s["class"], status, s["rule_id"], fids, eids,
                                  s["issued_at"][:40], refs))
            except (KeyError, TypeError):
                return StrikeFeed(False, reason="V&I strike feed inconsistent")
        nxt = data.get("next_cursor")
        if nxt is not None and (not isinstance(nxt, int) or isinstance(nxt, bool) or nxt < 0):
            return StrikeFeed(False, reason="V&I strike feed cursor inconsistent")
        return StrikeFeed(True, tuple(out), str(nxt) if nxt is not None else None)

    def finding(self, finding_id):
        """``GET /vi/v1/findings/{id}`` → ``{finding_id, clipper_id, kind, status, evidence_ids, subject_ref,
        rules_pinned, ...}``; 404 = V&I does not know it."""
        code, data, _ = self._h.call("GET", f"/vi/v1/findings/{quote(finding_id, safe='')}")
        if code == 404:
            return FindingAnswer(True, None, "V&I does not know this finding")
        if code != 200 or not self._ok(data) or data.get("finding_id") != finding_id:
            return FindingAnswer(False, reason=f"V&I finding answer unusable ({code})")
        eids = _ids(data.get("evidence_ids"), 50)
        ref = data.get("subject_ref")
        if not _is_id(data.get("clipper_id")) or data.get("status") not in ("open", "upheld", "overturned") \
                or eids is None or not isinstance(data.get("kind"), str) or (ref is not None and not _is_id(ref)):
            return FindingAnswer(False, reason="V&I finding answer inconsistent")
        return FindingAnswer(True, Finding(finding_id, data["clipper_id"], data["kind"][:40], data["status"], eids, ref))

    def certifications(self, clipper_id):
        """``GET /vi/v1/certifications?clipper_id=`` → ``{clipper_id, certifications: [certification views +
        revision_watch_end], rules_pinned}``."""
        code, data, _ = self._h.call("GET", "/vi/v1/certifications", params={"clipper_id": clipper_id})
        if code != 200 or not self._ok(data) or data.get("clipper_id") != clipper_id \
                or not isinstance(data.get("certifications"), list) or len(data["certifications"]) > 10_000:
            return CertificationsAnswer(False, reason=f"V&I certifications unusable ({code})")
        out = []
        for c in data["certifications"]:
            try:
                views = c["certified_views"]
                if not (_is_id(c["certification_id"]) and _is_id(c["submission_id"]) and _is_id(c["campaign_id"])
                        and isinstance(c["platform"], str) and c.get("clipper_id") == clipper_id
                        and c["status"] in ("pending", "certified", "not_certified", "revised", "voided", "suspended")
                        and (views is None or (isinstance(views, int) and not isinstance(views, bool) and views >= 0))):
                    return CertificationsAnswer(False, reason="V&I certifications inconsistent")
                rwe = c.get("revision_watch_end")
                out.append(Certification(c["certification_id"], c["submission_id"], c["campaign_id"], c["platform"][:32],
                                         c["status"], views, rwe[:40] if isinstance(rwe, str) else None))
            except (KeyError, TypeError):
                return CertificationsAnswer(False, reason="V&I certifications inconsistent")
        return CertificationsAnswer(True, tuple(out))

    def ban(self, request_id, clipper_id, cn_decision_id, approved_at, andre_token):
        """``POST /vi/v1/bans`` with the clipper_network caller token AND Andre's own approval token (N16-2), passed
        through from Andre's ban-decision request (never stored). No token → nothing is sent."""
        if not isinstance(andre_token, str) or not andre_token:
            return Ack(False, False, None, "no Andre approval token: V&I's ban route is not asked")
        code, data, _ = self._h.call("POST", "/vi/v1/bans", {"request_id": request_id, "clipper_id": clipper_id,
                                                             "cn_decision_id": cn_decision_id, "approved_at": approved_at},
                                     extra_headers={"X-Andre-Approval-Token": andre_token})
        ok = code in (200, 201) and self._ok(data) and data.get("request_id") == request_id \
            and data.get("clipper_id") == clipper_id
        return Ack(ok, ok, cn_decision_id if ok else None, "" if ok else f"V&I ban propagation unusable ({code})")


def _utc(v: str):
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t.astimezone(timezone.utc) if t.tzinfo else None


# ================================================================== Compliance (38)


class HttpCompliance:
    """Caller ``clipper_network`` on Compliance (X-Compliance-Caller-Token)."""

    def __init__(self, base_url: str, service_token: str, caller_token: str, transport=None,
                 accept_unpinned: bool = False, timeout: float = TIMEOUT_S):
        self._h = _Http(base_url, {"Authorization": f"Bearer {service_token}",
                                   "X-Compliance-Caller-Token": caller_token}, transport, timeout)
        self._unpinned = accept_unpinned

    def resolve_person(self, request_id, declared_country, declared_region, attested, attestation_ref):
        person = {"declared_country": declared_country, "declared_region": declared_region, "attested": attested,
                  "attestation_ref": attestation_ref}
        code, data, _ = self._h.call("POST", "/compliance/v1/jurisdictions/resolve",
                                     {"request_id": request_id, "person": person})
        if code != 200 or not isinstance(data, dict) or not isinstance(data.get("register_version"), int) \
                or not _is_id(data.get("resolution_id")) or not isinstance(data.get("answers"), list) \
                or len(data["answers"]) != 1:
            return JurisdictionAnswer(False, reason=f"Compliance resolve unusable ({code})")
        a = data["answers"][0]
        if not isinstance(a, dict) or a.get("who") != "person" or a.get("code") not in (declared_region, declared_country) \
                or a.get("class") not in ("operate", "conditional", "refuse"):
            return JurisdictionAnswer(False, reason="Compliance resolve inconsistent")
        return JurisdictionAnswer(True, a["class"], data["resolution_id"], str(a.get("reason", ""))[:200])

    def _ruling(self, code, data, subject_id, lane=None, kind=None, request_id=None, facts=None) -> ComplianceRuling:
        if code != 200 or not isinstance(data, dict):
            return ComplianceRuling(False, reason=f"Compliance answer unusable ({code})")
        allowed, lines, rid = data.get("allowed"), data.get("unmet_lines"), data.get("ruling_id")
        if (not isinstance(allowed, bool) or not isinstance(lines, list) or not _is_id(rid)
                or data.get("subject_id") != subject_id or (allowed and lines) or (not allowed and not lines)
                or not all(isinstance(x, str) for x in lines)
                or (lane is not None and data.get("lane") != lane)
                or (kind is not None and data.get("subject_kind") != kind)
                or (request_id is not None and (data.get("request_id") != request_id
                                                or data.get("facts_sha256") != facts_sha256(facts)))
                or not _pinned(data, "seed_pinned", self._unpinned)):
            return ComplianceRuling(False, reason="Compliance answer inconsistent")
        return ComplianceRuling(True, allowed, rid, tuple(x[:_RULE_LINE_MAX] for x in lines[:50]))

    def creator_activation(self, request_id, clipper_id, facts):
        code, data, _ = self._h.call("POST", "/compliance/v1/rule", {"request_id": request_id, "subject_id": clipper_id,
                                                                     "lane": "zbc_creator", "facts": facts})
        return self._ruling(code, data, clipper_id, lane="zbc_creator", request_id=request_id, facts=facts)

    def latest_activation(self, lane, subject_id):
        code, data, _ = self._h.call("GET", f"/compliance/v1/activations/{quote(lane, safe='')}/"
                                            f"{quote(subject_id, safe='')}/latest")
        return self._ruling(code, data, subject_id, lane=lane)

    def review_email_campaign(self, request_id, subject_id, facts):
        code, data, _ = self._h.call("POST", "/compliance/v1/review", {"request_id": request_id, "subject_kind": "zbm_work",
                                                                       "subject_id": subject_id, "facts": facts})
        return self._ruling(code, data, subject_id, kind="zbm_work", request_id=request_id, facts=facts)


# ================================================================== Creative Production


class HttpCreative:
    """creative-py has one bearer today (no caller identity; §"Changes" 1)."""

    MAX_PAGES = 50

    def __init__(self, base_url: str, token: str, transport=None, timeout: float = TIMEOUT_S):
        self._h = _Http(base_url, {"Authorization": f"Bearer {token}"}, transport, timeout)

    def live_rulebook(self, campaign_id):
        live, offset = [], 0
        for _ in range(self.MAX_PAGES):
            code, data, _ = self._h.call("GET", f"/zbc/campaigns/{quote(campaign_id, safe='')}/rulebooks",
                                         params={"offset": offset})
            if code != 200 or not isinstance(data, dict) or data.get("campaign_id") != campaign_id \
                    or not isinstance(data.get("versions"), list):
                return RulebookAnswer(False, reason=f"Creative rulebooks unusable ({code})")
            for v in data["versions"]:
                if not isinstance(v, dict) or not isinstance(v.get("version"), int) or not isinstance(v.get("status"), str):
                    return RulebookAnswer(False, reason="Creative rulebooks inconsistent")
                if v["status"] == "live":
                    live.append(v["version"])
            nxt = data.get("next_offset")
            if nxt is None:
                break
            if not isinstance(nxt, int) or nxt <= offset:
                return RulebookAnswer(False, reason="Creative rulebooks paging inconsistent")
            offset = nxt
        else:
            return RulebookAnswer(False, reason="Creative rulebooks: too many pages")
        if len(live) > 1:
            return RulebookAnswer(False, reason="Creative reports more than one live rulebook version")
        return RulebookAnswer(True, live[0] if live else None)

    def kit(self, campaign_id):
        code, data, raw = self._h.call("GET", f"/zbc/campaigns/{quote(campaign_id, safe='')}/kit")
        if code != 200 or not isinstance(data, dict) or data.get("campaign_id") != campaign_id \
                or not _is_id(data.get("kit_id")) or not isinstance(data.get("rulebook_version"), int) \
                or not isinstance(data.get("status"), str):
            return KitAnswer(False, reason=f"Creative kit unusable ({code})")
        return KitAnswer(True, data["kit_id"], data["rulebook_version"], data["status"][:20],
                         hashlib.sha256(raw).hexdigest())


def clients_from_settings(settings, ports) -> None:
    """Replace a stand-in only when its client is fully configured (config.py refuses a partial set)."""
    if settings.vi:
        ports.vi = HttpVerificationIntegrity(settings.vi.url, settings.vi.service_token, settings.vi.caller_token,
                                             accept_unpinned=settings.vi.accept_unpinned)
    if settings.compliance:
        ports.compliance = HttpCompliance(settings.compliance.url, settings.compliance.service_token,
                                          settings.compliance.caller_token,
                                          accept_unpinned=settings.compliance.accept_unpinned)
    if settings.creative:
        ports.creative = HttpCreative(settings.creative.url, settings.creative.service_token)
