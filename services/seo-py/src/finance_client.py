"""
Finance (31) invoice verification client (ADR 0017 Wave 3, W3-2).

The paid-audit and paid-schedule flows used to take a Finance (31) invoice id on trust. This module asks Finance:
``GET /fin/v1/invoices/{invoice_id}/status`` (finance-py ``api.invoice_status``, the narrow read made for this
service, AEGIS SEO Wave 3 M2: the service bearer token plus a caller token in ``X-FIN-Caller-Token``; this service's
caller name at Finance is ``seo_02``, and it reaches no other route there). Nothing else is called, and nothing is
ever written to Finance.

Rules:
  - fail closed: anything that is not a well-formed answer for exactly the invoice asked about is UNVERIFIABLE
    (never "paid");
  - bounded: a per-phase timeout, an overall deadline over every attempt AND within each attempt (every socket
    operation and the body loop; M1), at most ``attempts`` tries of this idempotent read, a response body cap, no
    redirects followed;
  - retried only when a retry can help (connect error, timeout, 429, 500, 502, 503, 504); a 4xx is never retried;
  - minimal: Finance answers only the fields the verdict needs (id, status, entity, client id, kind, currency,
    whether anything was refunded or charged back, paid_at); an answer with any other field (the full record) is
    refused. Nothing of it reaches this service's log or ledger but the verdict and its SHA-256.

Reason codes (a closed set; ``reasons.py`` lists every one):
  UNVERIFIABLE  FINANCE_NOT_CONFIGURED, FINANCE_UNAVAILABLE, FINANCE_AUTH_REFUSED, FINANCE_RESPONSE_INVALID
  DEFINITIVE    INVOICE_NOT_FOUND, INVOICE_ENTITY_MISMATCH, INVOICE_TENANT_MISMATCH, INVOICE_CURRENCY_UNSUPPORTED,
                INVOICE_NOT_PAID, INVOICE_REFUNDED
  PAID          the one verdict that lets a paid run proceed

This module is standalone (standard library and httpx only) so finance-py's contract test can load it by file path
and run its parser and verdict against Finance's real invoice representation.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Optional

import httpcore
import httpx

CALLER_NAME_AT_FINANCE = "seo_02"
FIN_CALLER_HEADER = "X-FIN-Caller-Token"
INVOICE_ID = re.compile(r"fin-inv-[0-9A-HJKMNP-TV-Z]{26}")
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,100}")           # finance-py models.CLIENT_ID_RE
STATUSES = ("draft", "issued", "paid", "void", "returned")  # every status finance-py writes on an invoice
ENTITIES = ("zbm", "zbc")
SEO_ENTITY = "zbm"                                          # Search & Answer Intelligence is Z Best Media's
MAX_RESPONSE_BYTES = 64 * 1024
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
STATUS_FIELDS = frozenset({"invoice_id", "entity", "client_id", "kind", "status", "currency", "paid_at", "refunded",
                           "charged_back"})                 # finance-py svc_books.invoice_status
_NOT_FOUND_DETAIL = "no such invoice"                       # finance-py svc_books.get_invoice

UNVERIFIABLE = frozenset({"FINANCE_NOT_CONFIGURED", "FINANCE_UNAVAILABLE", "FINANCE_AUTH_REFUSED",
                          "FINANCE_RESPONSE_INVALID"})
DEFINITIVE = frozenset({"INVOICE_NOT_FOUND", "INVOICE_ENTITY_MISMATCH", "INVOICE_TENANT_MISMATCH",
                        "INVOICE_CURRENCY_UNSUPPORTED", "INVOICE_NOT_PAID", "INVOICE_REFUNDED"})
PAID = "PAID"


class FinanceLookupError(Exception):
    """The lookup did not produce a usable answer. ``code`` is one of UNVERIFIABLE or INVOICE_NOT_FOUND."""

    def __init__(self, code: str):
        if code not in UNVERIFIABLE and code != "INVOICE_NOT_FOUND":
            raise AssertionError(f"not a lookup code: {code}")
        super().__init__(code)
        self.code = code


def parse_invoice(obj, invoice_id: str) -> dict:
    """Finance's invoice-status answer (``GET /fin/v1/invoices/{id}/status``) -> the minimal facts. Anything
    malformed, an extra field, or an answer about any other invoice is FINANCE_RESPONSE_INVALID (unverifiable,
    never "paid")."""
    try:
        if not isinstance(obj, dict) or obj.get("invoice_id") != invoice_id or set(obj) != STATUS_FIELDS:
            raise ValueError("not this invoice's status")
        status, entity, client, kind, cur = (obj.get(k) for k in ("status", "entity", "client_id", "kind", "currency"))
        if status not in STATUSES or entity not in ENTITIES or not isinstance(kind, str) or not 1 <= len(kind) <= 40:
            raise ValueError("status, entity or kind")
        if not isinstance(client, str) or not CLIENT_ID.fullmatch(client):
            raise ValueError("client")
        if not isinstance(cur, str) or not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError("currency")
        if not isinstance(obj["refunded"], bool) or not isinstance(obj["charged_back"], bool):
            raise ValueError("refund flags")
        paid_at = obj.get("paid_at")
        if paid_at is not None and (not isinstance(paid_at, str) or not 10 <= len(paid_at) <= 40):
            raise ValueError("paid_at")
    except ValueError:
        raise FinanceLookupError("FINANCE_RESPONSE_INVALID") from None
    return {"invoice_id": invoice_id, "status": status, "entity": entity, "client_id": client, "kind": kind,
            "currency": cur, "refunded": obj["refunded"], "charged_back": obj["charged_back"], "paid_at": paid_at}


def assess(facts: dict, expected_client_id: str, entity: str = SEO_ENTITY) -> str:
    """The verdict for a paid run: PAID, or the first DEFINITIVE reason that holds. Ownership is checked before
    payment, so the answer about someone else's invoice never says whether it was paid."""
    if facts["entity"] != entity:
        return "INVOICE_ENTITY_MISMATCH"
    if facts["client_id"] != expected_client_id:
        return "INVOICE_TENANT_MISMATCH"
    if facts["currency"] != "USD":
        return "INVOICE_CURRENCY_UNSUPPORTED"
    if facts["status"] != "paid":
        return "INVOICE_NOT_PAID"
    if facts["refunded"] or facts["charged_back"]:
        return "INVOICE_REFUNDED"
    return PAID


class NotConnectedFinance:
    """No Finance (31) client configured: every lookup is FINANCE_NOT_CONFIGURED (unverifiable)."""

    connected = False

    def lookup(self, invoice_id: str) -> dict:
        raise FinanceLookupError("FINANCE_NOT_CONFIGURED")


class FinanceClient:
    connected = True

    def __init__(self, base_url: str, service_token: str, caller_token: str, timeout_s: float = 5.0,
                 attempts: int = 3, backoff_s: tuple = (0.2, 0.5), transport: Optional[httpx.BaseTransport] = None,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
        if not base_url or not service_token or not caller_token:
            raise ValueError("FinanceClient needs a base URL, the Finance service token and this caller's token")
        if not 1 <= attempts <= 5:
            raise ValueError("attempts must be 1..5")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {service_token}", FIN_CALLER_HEADER: caller_token,
                         "Accept": "application/json", "Accept-Encoding": "identity"}
        self.timeout_s = float(timeout_s)
        self.attempts = attempts
        self.backoff_s = tuple(backoff_s)
        self.deadline_s = self.timeout_s * attempts + sum(self.backoff_s[:attempts - 1])
        self._transport = transport
        self._sleep = sleep
        self._mono = monotonic

    def lookup(self, invoice_id: str) -> dict:
        """The minimal facts of one invoice, or ``FinanceLookupError``. Never raises anything else for a network or
        protocol outcome."""
        if not isinstance(invoice_id, str) or not INVOICE_ID.fullmatch(invoice_id):
            raise FinanceLookupError("FINANCE_RESPONSE_INVALID")
        end = self._mono() + self.deadline_s
        last = "FINANCE_UNAVAILABLE"
        for attempt in range(self.attempts):
            left = end - self._mono()
            if left <= 0:
                break
            try:
                status, body = self._get(f"{self._base}/fin/v1/invoices/{invoice_id}/status",
                                         min(self.timeout_s, left), end)
            except _Retry:
                last = "FINANCE_UNAVAILABLE"
            else:
                if status == 200:
                    return parse_invoice(_json(body), invoice_id)
                if status == 404:
                    detail = _json(body, strict=False)
                    if isinstance(detail, dict) and detail.get("detail") == _NOT_FOUND_DETAIL:
                        raise FinanceLookupError("INVOICE_NOT_FOUND")
                    raise FinanceLookupError("FINANCE_RESPONSE_INVALID")    # a wrong base path is not "not found"
                if status in (401, 403):
                    raise FinanceLookupError("FINANCE_AUTH_REFUSED")
                if status not in RETRY_STATUSES:
                    raise FinanceLookupError("FINANCE_RESPONSE_INVALID")
                last = "FINANCE_UNAVAILABLE"
            if attempt + 1 < self.attempts:
                pause = self.backoff_s[min(attempt, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
                if self._mono() + pause >= end:
                    break
                self._sleep(pause)
        raise FinanceLookupError(last)

    def _get(self, url: str, timeout: float, end: float) -> tuple[int, bytes]:
        """One GET. AEGIS SEO Wave 3 M1: the overall deadline ``end`` holds WITHIN the attempt too — every socket
        operation (connect, TLS, each read and write) of the production transport is bounded by the time left
        (``_DeadlineBackend``), and the body loop stops at the deadline whatever the transport (a body trickled a
        byte at a time is FINANCE_UNAVAILABLE at the deadline, never minutes later)."""
        transport = self._transport
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise FinanceLookupError("FINANCE_NOT_CONFIGURED")     # never look up without a hard deadline
            pool._network_backend = _DeadlineBackend(self._mono, end)
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout), transport=transport, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("GET", url, headers=self._headers) as resp:
                    if resp.headers.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
                        raise FinanceLookupError("FINANCE_RESPONSE_INVALID")    # never asked for; never decoded
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():             # identity coding: these are the bytes received
                        if self._mono() >= end:
                            raise FinanceLookupError("FINANCE_UNAVAILABLE")      # M1: the deadline, mid-body
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise FinanceLookupError("FINANCE_RESPONSE_INVALID")
                        chunks.append(chunk)
                    return resp.status_code, b"".join(chunks)
        except FinanceLookupError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise _Retry() from None
        except Exception:                                       # anything else is a malformed answer, never a 500
            raise FinanceLookupError("FINANCE_RESPONSE_INVALID") from None


class _Retry(Exception):
    pass


class _DeadlineStream(httpcore.NetworkStream):
    """Every operation bounded by the time left until the lookup's overall deadline (the fetcher's pattern,
    primitives/fetch.py, kept here so this module stays standalone)."""

    def __init__(self, inner, left: Callable[[Optional[float], type], float]):
        self._s, self._left = inner, left

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        return self._s.read(max_bytes, self._left(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self._s.write(buffer, self._left(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._s.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(self._s.start_tls(ssl_context, server_hostname,
                                                 self._left(timeout, httpcore.ConnectTimeout)), self._left)

    def get_extra_info(self, info: str):
        return self._s.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, monotonic: Callable[[], float], end: float, inner=None):
        self._b = inner or httpcore.SyncBackend()
        self._mono, self._end = monotonic, end

    def left(self, timeout: Optional[float], exc: type) -> float:
        left = self._end - self._mono()
        if left <= 0:
            raise exc("the lookup's overall deadline has passed")
        return left if timeout is None else min(timeout, left)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _DeadlineStream(self._b.connect_tcp(host, port, self.left(timeout, httpcore.ConnectTimeout),
                                                   local_address, socket_options), self.left)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never used")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _json(body: bytes, strict: bool = True):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        if strict:
            raise FinanceLookupError("FINANCE_RESPONSE_INVALID") from None
        return None
