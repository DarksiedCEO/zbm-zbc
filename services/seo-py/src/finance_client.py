"""
Finance (31) invoice verification client (ADR 0017 Wave 3, W3-2).

The paid-audit and paid-schedule flows used to take a Finance (31) invoice id on trust. This module asks Finance:
``GET /fin/v1/invoices/{invoice_id}`` (finance-py ``api.invoice_get``: the service bearer token plus a caller token
in ``X-FIN-Caller-Token``; this service's caller name at Finance is ``seo_02``). Nothing else is called, and nothing
is ever written to Finance.

Rules:
  - fail closed: anything that is not a well-formed answer for exactly the invoice asked about is UNVERIFIABLE
    (never "paid");
  - bounded: a per-phase timeout, an overall deadline over every attempt, at most ``attempts`` tries of this
    idempotent read, a response body cap, no redirects followed;
  - retried only when a retry can help (connect error, timeout, 429, 500, 502, 503, 504); a 4xx is never retried;
  - minimal: of Finance's invoice record only the fields the verdict needs are kept (id, status, entity, client id,
    kind, currency, whether anything was refunded or charged back, paid_at). Lines, descriptions, amounts, template
    variables and legal references are validated for shape where needed and then dropped; none reaches this
    service's log or ledger.

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
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

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
_MONEY = re.compile(r"-?[0-9]{1,15}\.[0-9]{2}")
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


def _money(v) -> Optional[Decimal]:
    if v is None:
        return None
    if not isinstance(v, str) or not _MONEY.fullmatch(v):
        raise ValueError("money")
    try:
        return Decimal(v)
    except InvalidOperation:
        raise ValueError("money") from None


def parse_invoice(obj, invoice_id: str) -> dict:
    """Finance's invoice record -> the minimal facts. Anything malformed, or an answer about any other invoice, is
    FINANCE_RESPONSE_INVALID (unverifiable, never "paid")."""
    try:
        if not isinstance(obj, dict) or obj.get("invoice_id") != invoice_id:
            raise ValueError("not this invoice")
        status, entity, client, kind, cur = (obj.get(k) for k in ("status", "entity", "client_id", "kind", "currency"))
        if status not in STATUSES or entity not in ENTITIES or not isinstance(kind, str) or not 1 <= len(kind) <= 40:
            raise ValueError("status, entity or kind")
        if not isinstance(client, str) or not CLIENT_ID.fullmatch(client):
            raise ValueError("client")
        if not isinstance(cur, str) or not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError("currency")
        total = _money(obj.get("total"))
        if total is None or total < 0:
            raise ValueError("total")
        refunded, charged_back = _money(obj.get("refunded")), _money(obj.get("charged_back"))
        paid_at = obj.get("paid_at")
        if paid_at is not None and (not isinstance(paid_at, str) or not 10 <= len(paid_at) <= 40):
            raise ValueError("paid_at")
    except ValueError:
        raise FinanceLookupError("FINANCE_RESPONSE_INVALID") from None
    return {"invoice_id": invoice_id, "status": status, "entity": entity, "client_id": client, "kind": kind,
            "currency": cur, "refunded": bool(refunded), "charged_back": bool(charged_back), "paid_at": paid_at}


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
                status, body = self._get(f"{self._base}/fin/v1/invoices/{invoice_id}", min(self.timeout_s, left))
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

    def _get(self, url: str, timeout: float) -> tuple[int, bytes]:
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout), transport=self._transport, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream("GET", url, headers=self._headers) as resp:
                    if resp.headers.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
                        raise FinanceLookupError("FINANCE_RESPONSE_INVALID")    # never asked for; never decoded
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():             # identity coding: these are the bytes received
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


def _json(body: bytes, strict: bool = True):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        if strict:
            raise FinanceLookupError("FINANCE_RESPONSE_INVALID") from None
        return None
