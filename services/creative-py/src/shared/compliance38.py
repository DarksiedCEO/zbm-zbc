"""
HTTP client for Compliance (38) — ``services/compliance-py``,
``POST /compliance/v1/review`` (Compliance spec §F.1, ADR 0006).

Implements the existing ``Compliance38Port`` protocol
(``review(subject_kind, subject_id, facts) -> GateResult``). Used only when
COMPLIANCE_SERVICE_URL, COMPLIANCE_SERVICE_TOKEN and COMPLIANCE_CALLER_TOKEN
are all set; otherwise the fail-closed ``NotBuiltCompliance38`` stays.
No fallback to "allowed": any non-200, timeout, transport error or
unparseable/inconsistent answer is ``GateResult("compliance_38", False,
"Compliance (38) unreachable or refused (<status>): not allowed")``.
Timeout 10 s is the TOTAL wall-clock budget of one call, both attempts
included, however slowly the server dribbles bytes (AEGIS N14-7); one retry
with the SAME request_id on a transport error or 5xx while budget remains.
The answer is capped at 1 MiB before it is parsed, and any parse failure is
"not allowed", never an exception (N14-8). The answer must name the same
subject_id, subject_kind and gate as the request, else "not allowed" (N14-10).
AEGIS N15-8: the answer must also echo this call's request_id and the
SHA-256 of the canonical JSON of the facts sent (``facts_sha256``: sorted
keys, separators ``,`` and ``:``, ASCII escapes — ``json.dumps(facts,
sort_keys=True, separators=(",", ":"))``), and a ruling with ``seed_pinned``
not true is refused unless the caller's environment sets
COMPLIANCE_ACCEPT_UNPINNED=1 (default off).

Creative's ZBM gate passes its opaque ``export`` and ``rights`` objects in
``facts``; Compliance's schema is strict, so this client carries them in
``caller_context`` (stored there only as a SHA-256, never interpreted).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Optional

import httpx

from shared.departments import GateResult, NotBuiltCompliance38

TIMEOUT_S = 10.0                    # total per call (both attempts), wall clock
MAX_RESPONSE_BYTES = 1024 * 1024    # an answer larger than this is refused unparsed
_ROUTE = "/compliance/v1/review"
_CONTEXT_KEYS = ("export", "rights")
_GATE = {"zbc_clip": "payout", "zbm_work": "publish"}


def _refused(status: str) -> GateResult:
    return GateResult("compliance_38", False, f"Compliance (38) unreachable or refused ({status}): not allowed")


class HttpCompliance38:
    def __init__(self, base_url: str, service_token: str, caller_token: str,
                 transport: Optional[httpx.BaseTransport] = None, timeout: float = TIMEOUT_S,
                 accept_unpinned: bool = False):
        if not (base_url and service_token and caller_token):
            raise ValueError("HttpCompliance38 needs a URL, the service token and the caller token")
        self._url = base_url.rstrip("/") + _ROUTE
        self._headers = {"Authorization": f"Bearer {service_token}", "X-Compliance-Caller-Token": caller_token}
        self._transport = transport
        self._timeout = timeout
        self._accept_unpinned = bool(accept_unpinned)

    def review(self, subject_kind: str, subject_id: str, facts: dict) -> GateResult:
        facts = dict(facts)
        context = {k: facts.pop(k) for k in _CONTEXT_KEYS if k in facts}
        body = {"request_id": "cre-" + uuid.uuid4().hex, "subject_kind": subject_kind, "subject_id": subject_id,
                "facts": facts}
        if context:
            body["caller_context"] = context
        deadline = time.monotonic() + self._timeout
        status = "no answer"
        for _attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _refused("deadline exceeded")
            res = _run_bounded(lambda cancel, r=remaining: _exchange(self._url, body, self._headers, self._transport,
                                                                      r, cancel), remaining)
            if res is None:
                return _refused("deadline exceeded")
            kind, code, content = res
            if kind == "error":
                status = str(code)
                continue
            if kind == "too_large":
                return _refused("answer too large")
            if code >= 500:
                status = str(code)
                continue
            if code != 200:
                return _refused(str(code))
            return _parse(content, subject_kind, subject_id, body["request_id"], facts_sha256(facts),
                          self._accept_unpinned)
        return _refused(status)


def facts_sha256(facts: dict) -> str:
    """The digest Compliance computes over the facts it evaluated (its ``canonical``): sorted keys, compact
    separators, ASCII escapes (AEGIS N15-8)."""
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def _parse(content: bytes, subject_kind: str, subject_id: str, request_id: str = "", facts_sha: str = "",
           accept_unpinned: bool = False) -> GateResult:
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return _refused("unparseable answer")
        allowed, reason, ref = data["allowed"], data["reason"], data["reference"]
        gate, sid, kind, lines = data.get("gate"), data.get("subject_id"), data.get("subject_kind"), data.get("unmet_lines")
        echo_rid, echo_facts, pinned = data.get("request_id"), data.get("facts_sha256"), data.get("seed_pinned")
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - ValueError, KeyError, TypeError, RecursionError...: never raises, never allowed
        return _refused("unparseable answer")
    if (not isinstance(allowed, bool) or not isinstance(reason, str) or not isinstance(ref, str)
            or gate != _GATE.get(subject_kind) or sid != subject_id or kind != subject_kind
            or not isinstance(lines, list) or (allowed and lines) or (not allowed and not lines)
            or (allowed and not reason.startswith("allowed under register v"))
            or echo_rid != request_id or echo_facts != facts_sha or not isinstance(pinned, bool)):
        return _refused("inconsistent answer")
    if pinned is not True and not accept_unpinned:
        return _refused("ruling from an unpinned, non-production seed; set COMPLIANCE_ACCEPT_UNPINNED=1 to accept")
    return GateResult("compliance_38", allowed, reason[:1000], ref)


def _run_bounded(fn, seconds: float):
    """Run ``fn(cancel)`` in a daemon thread and wait at most ``seconds`` (wall clock). Returns its result,
    ("error", <name>, None) if it raised, or None when the deadline passed (the thread is told to stop and
    ends by itself within its own httpx timeout, which is never longer than the time that was left)."""
    box: dict = {}
    cancel = threading.Event()

    def run():
        try:
            box["r"] = fn(cancel)
        except BaseException as exc:  # noqa: BLE001 - nothing escapes to the caller: fail closed
            box["r"] = ("error", type(exc).__name__, None)

    t = threading.Thread(target=run, name="compliance38-call", daemon=True)
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        cancel.set()
        return None
    return box.get("r", ("error", "no answer", None))


def _exchange(url: str, body: dict, headers: dict, transport, remaining: float, cancel: threading.Event):
    """One POST, streamed: ("ok", status, bytes) | ("too_large", status, None) | ("error", name, None)."""
    try:
        with httpx.Client(timeout=httpx.Timeout(max(0.01, remaining)), transport=transport,
                          follow_redirects=False) as c:
            with c.stream("POST", url, json=body, headers={**headers, "Accept-Encoding": "identity"}) as resp:
                if resp.status_code != 200:
                    return "ok", resp.status_code, b""
                enc = resp.headers.get("content-encoding", "identity").strip().lower()
                if enc not in ("", "identity"):
                    return "error", "encoded answer", None
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    return "too_large", resp.status_code, None
                buf = bytearray()
                for chunk in resp.iter_bytes():  # identity only (checked above): bytes as sent
                    if cancel.is_set():
                        return "error", "deadline", None
                    buf += chunk
                    if len(buf) > MAX_RESPONSE_BYTES:
                        return "too_large", resp.status_code, None
                return "ok", resp.status_code, bytes(buf)
    except httpx.HTTPError as exc:
        return "error", type(exc).__name__, None


def compliance_from_env(env: dict):
    url, tok, caller = env.get("COMPLIANCE_SERVICE_URL"), env.get("COMPLIANCE_SERVICE_TOKEN"), env.get("COMPLIANCE_CALLER_TOKEN")
    if url and tok and caller:
        return HttpCompliance38(url, tok, caller,
                                accept_unpinned=(env.get("COMPLIANCE_ACCEPT_UNPINNED") or "").strip() == "1")
    return NotBuiltCompliance38()
