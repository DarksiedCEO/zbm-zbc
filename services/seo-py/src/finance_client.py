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
    operation and the body loop; M1; the Finance host's name lookup too, on at most two resolver threads in the
    process, Wave 3 round 2), at most ``attempts`` tries of this idempotent read, a response body cap, no
    redirects followed;
  - retried only when a retry can help (connect error, timeout, 429, 500, 502, 503, 504); a 4xx is never retried;
  - minimal: Finance answers only the fields the verdict needs (id, status, entity, client id, kind, currency,
    whether anything was refunded or charged back, paid_at, and, optionally, whether Compliance holds the payment:
    ``held``, AEGIS r4); an answer with any other field (the full record) is refused. Nothing of it reaches this service's log or ledger but the verdict and its SHA-256.

Reason codes (a closed set; ``reasons.py`` lists every one):
  UNVERIFIABLE  FINANCE_NOT_CONFIGURED, FINANCE_UNAVAILABLE, FINANCE_AUTH_REFUSED, FINANCE_RESPONSE_INVALID
  DEFINITIVE    INVOICE_NOT_FOUND, INVOICE_ENTITY_MISMATCH, INVOICE_TENANT_MISMATCH, INVOICE_CURRENCY_UNSUPPORTED,
                INVOICE_NOT_PAID, INVOICE_REFUNDED, INVOICE_HELD
  PAID          the one verdict that lets a paid run proceed

This module is standalone (standard library and httpx only) so finance-py's contract test can load it by file path
and run its parser and verdict against Finance's real invoice representation.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import threading
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
OPTIONAL_STATUS_FIELDS = frozenset({"held"})                # AEGIS r4 (ADR 0024): a payment held by Compliance
_NOT_FOUND_DETAIL = "no such invoice"                       # finance-py svc_books.get_invoice

UNVERIFIABLE = frozenset({"FINANCE_NOT_CONFIGURED", "FINANCE_UNAVAILABLE", "FINANCE_AUTH_REFUSED",
                          "FINANCE_RESPONSE_INVALID"})
DEFINITIVE = frozenset({"INVOICE_NOT_FOUND", "INVOICE_ENTITY_MISMATCH", "INVOICE_TENANT_MISMATCH",
                        "INVOICE_CURRENCY_UNSUPPORTED", "INVOICE_NOT_PAID", "INVOICE_REFUNDED", "INVOICE_HELD"})
PAID = "PAID"


def _system_resolve(host: str, port: int) -> list:
    return [i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


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
        if not isinstance(obj, dict) or obj.get("invoice_id") != invoice_id \
                or not STATUS_FIELDS <= set(obj) <= STATUS_FIELDS | OPTIONAL_STATUS_FIELDS:
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
        if not isinstance(obj.get("held", False), bool):
            raise ValueError("held")
        paid_at = obj.get("paid_at")
        if paid_at is not None and (not isinstance(paid_at, str) or not 10 <= len(paid_at) <= 40):
            raise ValueError("paid_at")
    except ValueError:
        raise FinanceLookupError("FINANCE_RESPONSE_INVALID") from None
    return {"invoice_id": invoice_id, "status": status, "entity": entity, "client_id": client, "kind": kind,
            "currency": cur, "refunded": obj["refunded"], "charged_back": obj["charged_back"], "paid_at": paid_at,
            "held": obj.get("held", False)}


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
    if facts.get("held"):
        return "INVOICE_HELD"           # paid, but held by Compliance (38): never releases a run (ADR 0024, AEGIS r4)
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
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 resolve: Callable[[str, int], list] = _system_resolve, dns_gate: Optional["_DnsGate"] = None):
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
        self._resolver = _Resolver(resolve, dns_gate)          # the URL's host, resolved inside the deadline

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
            pool._network_backend = _DeadlineBackend(self._mono, end, resolver=self._resolver)
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


class _DnsGate:
    """The process-wide bound on Finance name lookups in flight (AEGIS SEO Wave 3 round 2). A lookup takes a slot
    before its resolver thread starts and the thread gives it back when getaddrinfo returns or raises, so at most
    ``workers`` resolver threads exist at any time, whatever a hung resolver does; with every slot taken a lookup is
    refused at once (fail closed), never queued. Every slot is released exactly once (``_Slot``)."""

    def __init__(self, workers: int = 2):
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.workers = workers
        self._cond = threading.Condition(threading.Lock())
        self._busy = 0

    @property
    def busy(self) -> int:
        with self._cond:
            return self._busy

    def try_take(self) -> Optional["_Slot"]:
        with self._cond:
            if self._busy >= self.workers:
                return None
            self._busy += 1
        return _Slot(self)

    def _give_back(self) -> None:
        with self._cond:
            self._busy -= 1
            self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """True once no slot is held (tests and shutdown; never on a lookup's path)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._busy == 0, timeout)

    def _after_fork_in_child(self) -> None:
        # a forked child has none of the parent's resolver threads: their slots would never come back
        self._cond = threading.Condition(threading.Lock())
        self._busy = 0


class _Slot:
    def __init__(self, gate: _DnsGate):
        self._gate, self._lock, self._held = gate, threading.Lock(), True

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            self._gate._give_back()


_DNS_GATE = _DnsGate()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_DNS_GATE._after_fork_in_child)


class _Resolver:
    """Name resolution for the Finance URL INSIDE the lookup's deadline (AEGIS SEO Wave 3 round 2). getaddrinfo runs
    on its own daemon thread under a slot of the shared ``_DnsGate`` and is waited for only for the time left; late,
    failed, refused (every slot busy) or empty answers are ConnectTimeout / ConnectError, which the client reports
    FINANCE_UNAVAILABLE (fail closed). A hung resolver keeps its thread and slot until the system resolver gives up,
    never the lookup. An IP literal is used as it is, without any lookup. Returns the addresses (validated IP
    literals, in the resolver's order, without repeats); the backend connects to those, never to the name."""

    _thread = threading.Thread

    def __init__(self, resolve: Callable[[str, int], list] = _system_resolve, gate: Optional[_DnsGate] = None):
        self._resolve = resolve
        self._gate = gate if gate is not None else _DNS_GATE

    def __call__(self, host: str, port: int, timeout: float) -> list:
        literal = _ip_literal(host)
        if literal is not None:
            return [literal]
        if timeout <= 0:
            raise httpcore.ConnectTimeout("the lookup's overall deadline has passed")
        slot = self._gate.try_take()
        if slot is None:
            raise httpcore.ConnectError("every Finance DNS worker is busy")
        box: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["addrs"] = self._resolve(host, port)
            except BaseException as exc:  # noqa: BLE001 - reported to the waiting lookup as a connect error
                box["error"] = exc
            finally:
                slot.release()
                done.set()

        try:
            self._thread(target=work, name="seo-finance-dns", daemon=True).start()
        except BaseException:
            slot.release()                       # never started (or interrupted while starting): give the slot back
            raise httpcore.ConnectError("could not start a Finance DNS lookup") from None
        if not done.wait(timeout):
            raise httpcore.ConnectTimeout("the Finance host did not resolve within the deadline")
        if "error" in box:
            raise httpcore.ConnectError("the Finance host does not resolve")
        out: list = []
        try:
            for a in box.get("addrs") or ():
                ip = _ip_literal(a) if isinstance(a, str) else None
                if ip is None:
                    raise httpcore.ConnectError("the Finance host resolved to something that is not an address")
                if ip not in out:
                    out.append(ip)
        except TypeError:
            raise httpcore.ConnectError("the Finance host resolved to something that is not an address") from None
        if not out:
            raise httpcore.ConnectError("the Finance host does not resolve")
        return out


def _ip_literal(host: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host))
    except ValueError:
        return None


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, monotonic: Callable[[], float], end: float, inner=None, resolver: Optional[_Resolver] = None):
        self._b = inner or httpcore.SyncBackend()
        self._mono, self._end = monotonic, end
        self._resolver = resolver or _Resolver()

    def left(self, timeout: Optional[float], exc: type) -> float:
        left = self._end - self._mono()
        if left <= 0:
            raise exc("the lookup's overall deadline has passed")
        return left if timeout is None else min(timeout, left)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        """Resolve ``host`` inside the deadline, then connect to the RESOLVED addresses in order, each attempt bounded
        by the time left. The socket layer is handed IP literals only, so it makes no second name lookup; httpcore
        keeps the URL's name for the Host header, TLS SNI and certificate verification (the origin, not this host
        argument), so HTTPS behaves exactly as before."""
        addrs = self._resolver(host, port, self.left(timeout, httpcore.ConnectTimeout))
        last: Optional[Exception] = None
        for ip in addrs:
            left = self.left(timeout, httpcore.ConnectTimeout)            # past the deadline: no further address
            try:
                inner = self._b.connect_tcp(ip, port, left, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc                                                  # the next address, if time is left
                continue
            return _DeadlineStream(inner, self.left)
        raise last if last is not None else httpcore.ConnectError("the Finance host does not resolve")

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
