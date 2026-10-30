"""Compliance (38) activation gate for Revenue Recovery store reads (ADR 0024, founder decision Oct 30, 2026).

Andre's approved connection (``RR_STORE_CONNECTIONS_FILE``) says WHICH store may be read; Compliance says whether the
client may be served at all. Before every sync (an outbound read of the client's store) and before every snapshot
handed to the scan path, the store service asks Compliance ``GET /compliance/v1/clients/{client_id}/activation``
(caller ``revenue_recovery``). Only a current ALLOW from a Compliance on its pinned (production) seed lets the read
happen; anything else is a refusal with no store call made:

- ``COMPLIANCE_NOT_ACTIVATED`` (409): Compliance answered, and it is not a current ALLOW;
- ``COMPLIANCE_UNAVAILABLE`` (503): not configured, unreachable, a token refused, or an answer not understood.

Settings: ``DETECTION_COMPLIANCE_URL`` (https, or ``http://<service>.railway.internal[:port]``, or loopback) and the
two token FILES the image's entrypoint writes from Railway variables (``DETECTION_COMPLIANCE_TOKEN_FILE``: Compliance's
service token; ``DETECTION_COMPLIANCE_CALLER_TOKEN_FILE``: this service's caller token), read with the same strict
rules as every other secret file (config.read_secret_file). Unset: every client is refused (fail closed).
"""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from .config import Secret, read_secret_file
from .reasons import StoreError

CALLER_HEADER = "X-Compliance-Caller-Token"
TIMEOUT_S = 5.0
MAX_RESPONSE_BYTES = 64 * 1024
_URL = re.compile(r"https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?|http://[a-z0-9-]{1,63}\.railway\.internal(:[0-9]{1,5})?"
                  r"|http://(127\.0\.0\.1|localhost)(:[0-9]{1,5})?")
_RULING = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_JURIS = re.compile(r"US-[A-Z]{2}|PR|GU")


class NotConfiguredCompliance:
    connected = False

    def require_allow(self, client_id: str) -> dict:
        raise StoreError("COMPLIANCE_UNAVAILABLE")


class HttpCompliance:
    connected = True

    def __init__(self, base_url: str, service_token: Secret, caller_token: Secret,
                 transport: Optional[httpx.BaseTransport] = None, timeout_s: float = TIMEOUT_S):
        self._base = base_url.rstrip("/")
        self._service, self._caller = service_token, caller_token
        self._transport, self._timeout = transport, timeout_s

    def __repr__(self) -> str:
        return f"HttpCompliance({self._base!r})"

    def _get(self, path: str) -> Optional[tuple[int, bytes]]:
        headers = {"Authorization": f"Bearer {self._service.reveal()}", CALLER_HEADER: self._caller.reveal(),
                   "Accept-Encoding": "identity", "Accept": "application/json"}
        for _attempt in range(2):
            try:
                with httpx.Client(timeout=httpx.Timeout(self._timeout), transport=self._transport,
                                  follow_redirects=False, trust_env=False) as c:
                    with c.stream("GET", self._base + path, headers=headers) as resp:
                        if resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                            return None
                        buf = bytearray()
                        for chunk in resp.iter_bytes():
                            buf += chunk
                            if len(buf) > MAX_RESPONSE_BYTES:
                                return None
                        if resp.status_code >= 500:
                            continue
                        return resp.status_code, bytes(buf)
            except httpx.HTTPError:
                continue
        return None

    def require_allow(self, client_id: str) -> dict:
        """{ruling_id, jurisdiction} for a current ALLOW; StoreError otherwise (never a pass)."""
        got = self._get(f"/compliance/v1/clients/{client_id}/activation")
        if got is None or got[0] != 200:
            raise StoreError("COMPLIANCE_UNAVAILABLE")
        try:
            d = json.loads(got[1])
            if not isinstance(d, dict) or d.get("client_id") != client_id or d.get("lane") != "client":
                raise ValueError("not this client")
            rid, juris = d.get("ruling_id"), d.get("jurisdiction")
            if d.get("decision") not in ("ALLOW", "REFUSE") or not isinstance(d.get("current"), bool) \
                    or not isinstance(d.get("seed_pinned"), bool):
                raise ValueError("decision")
            if (rid is not None and not (isinstance(rid, str) and _RULING.fullmatch(rid))) or \
                    (juris is not None and not (isinstance(juris, str) and _JURIS.fullmatch(juris))):
                raise ValueError("fields")
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise StoreError("COMPLIANCE_UNAVAILABLE") from None
        if d["decision"] != "ALLOW" or d["current"] is not True or d["seed_pinned"] is not True or not rid or not juris:
            raise StoreError("COMPLIANCE_NOT_ACTIVATED")
        return {"ruling_id": rid, "jurisdiction": juris}


def compliance_from_env(env):
    url = (env.get("DETECTION_COMPLIANCE_URL") or "").strip().rstrip("/")
    tok = (env.get("DETECTION_COMPLIANCE_TOKEN_FILE") or "").strip()
    call = (env.get("DETECTION_COMPLIANCE_CALLER_TOKEN_FILE") or "").strip()
    if not (url and tok and call) or not _URL.fullmatch(url):
        return NotConfiguredCompliance()
    try:
        return HttpCompliance(url, read_secret_file(tok), read_secret_file(call))
    except (StoreError, ValueError):
        return NotConfiguredCompliance()


__all__ = ["HttpCompliance", "NotConfiguredCompliance", "compliance_from_env"]
