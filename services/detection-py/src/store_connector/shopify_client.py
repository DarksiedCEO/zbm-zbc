"""Read-only Shopify GraphQL Admin API client (ADR 0020 section 4).

One ``ShopifySession`` is one sync of one store: it has ONE hard deadline that covers every request of the sync —
the client-credentials exchange, the preflight, every orders page, every throttle wait and every name lookup.

Rules:
  - host: only ``https://<handle>.myshopify.com`` (shop_domain.py); addresses must be public (net.py); no proxy from
    the environment, no redirects followed, ``Accept-Encoding: identity`` (a compressed answer is refused, so the
    size cap counts the bytes received);
  - read-only: the only operations sent are the two query constants in shopify_models.py and the token exchange;
    the granted scopes are checked before any order is read, and a credential with a write scope or any scope beyond
    ``read_orders`` / ``read_all_orders`` is refused;
  - bounded: per-response size cap, per-request timeout within the deadline, at most ``max_throttle_retries`` waits
    per request (each 1-10 s, from ``extensions.cost``), at most ``max_unavailable_retries`` retries of a 5xx or a
    connection failure, at most ``max_orders`` orders, at most ``max_cost_points`` points per sync;
  - strict: every answer is parsed by the models in shopify_models.py; anything else is STORE_RESPONSE_INVALID;
  - secret-safe: the token lives in a ``Secret``; no exception, log line or return value carries it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx
from pydantic import ValidationError

from . import shopify_models as m
from .config import Secret
from .net import DNS_GATE, DeadlineBackend, DnsGate, Resolver, system_resolve
from .reasons import StoreError
from .shop_domain import graphql_url, shop_host, token_url

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ALLOWED_SCOPES = frozenset({"read_orders", "read_all_orders"})
REQUIRED_SCOPES = frozenset({"read_orders"})
READ_ORDERS_WINDOW_DAYS = 60            # S10: read_orders alone covers orders created in the last 60 days
MIN_THROTTLE_WAIT_S = 1.0               # S7: "The recommended backoff time is one second"
MAX_THROTTLE_WAIT_S = 10.0
RETRY_STATUSES = frozenset({500, 502, 503, 504})


class _Retry(Exception):
    """A transport failure or 5xx a retry may cure."""


class _Throttled(Exception):
    def __init__(self, wait_s: float):
        super().__init__("throttled")
        self.wait_s = wait_s


class _TooCostly(Exception):
    pass


@dataclass
class ReadStats:
    requests: int = 0
    pages: int = 0
    throttle_waits: int = 0
    cost_points: float = 0.0
    page_size_final: int = m.ORDERS_PAGE_SIZE
    api_version: str = m.API_VERSION
    granted_scopes: list = field(default_factory=list)


@dataclass
class OrdersRead:
    orders: list                       # list[m.OrderNode], newest first
    truncated_by: Optional[str]        # None, "max_orders" or "cost_budget"
    window_start: datetime             # the lookback the query asked for
    lookback_days: int
    lookback_limited_by_scope: bool
    stats: ReadStats


def _throttle_wait(cost: Optional[m.Cost]) -> float:
    if cost is None or cost.requestedQueryCost is None:
        return MIN_THROTTLE_WAIT_S
    deficit = cost.requestedQueryCost - cost.throttleStatus.currentlyAvailable
    wait = deficit / cost.throttleStatus.restoreRate if deficit > 0 else 0.0
    return min(MAX_THROTTLE_WAIT_S, max(MIN_THROTTLE_WAIT_S, wait))


class ShopifySession:
    def __init__(self, handle: str, *, deadline_s: float, transport: Optional[httpx.BaseTransport] = None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 resolve: Callable[[str, int], list] = system_resolve, dns_gate: Optional[DnsGate] = None,
                 request_timeout_s: float = 30.0, max_throttle_retries: int = 8, max_unavailable_retries: int = 2,
                 kill_check: Callable[[], bool] = lambda: False, max_response_bytes: int = MAX_RESPONSE_BYTES):
        if not 1.0 <= deadline_s <= 900.0:
            raise ValueError("deadline_s must be 1..900")
        self.host = shop_host(handle)
        self.handle = handle
        self._mono, self._sleep = monotonic, sleep
        self._end = monotonic() + deadline_s
        self._transport = transport
        self._resolver = Resolver(resolve, dns_gate if dns_gate is not None else DNS_GATE)
        self._timeout = request_timeout_s
        self._max_throttle = max_throttle_retries
        self._max_unavail = max_unavailable_retries
        self._kill = kill_check
        self._max_bytes = max_response_bytes
        self._token: Optional[Secret] = None
        self.stats = ReadStats()

    def __repr__(self) -> str:
        return f"ShopifySession(host={self.host!r})"

    # ---- plumbing -------------------------------------------------------------------------------------------

    def _left(self) -> float:
        return self._end - self._mono()

    def _check_alive(self) -> None:
        if self._kill():
            raise StoreError("STORE_READS_DISABLED")
        if self._left() <= 0:
            raise StoreError("STORE_DEADLINE_EXCEEDED")

    def _wait(self, seconds: float) -> None:
        if self._mono() + seconds >= self._end:
            raise StoreError("STORE_DEADLINE_EXCEEDED")
        self._sleep(seconds)
        self._check_alive()

    def _post(self, url: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        """One POST bounded by the session deadline in every phase. Returns (status, selected headers, body)."""
        self._check_alive()
        transport = self._transport
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise StoreError("STORE_CONFIG_INVALID")        # never read without a hard deadline
            pool._network_backend = DeadlineBackend(self._mono, self._end, resolver=self._resolver)
        self.stats.requests += 1
        try:
            with httpx.Client(timeout=httpx.Timeout(min(self._timeout, max(self._left(), 0.001))),
                              transport=transport, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", url, headers=headers, content=body) as resp:
                    enc = resp.headers.get("content-encoding", "identity").strip().lower()
                    if enc not in ("identity", ""):
                        raise StoreError("STORE_RESPONSE_INVALID")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        if self._mono() >= self._end:
                            raise StoreError("STORE_DEADLINE_EXCEEDED")
                        size += len(chunk)
                        if size > self._max_bytes:
                            raise StoreError("STORE_RESPONSE_INVALID")
                        chunks.append(chunk)
                    picked = {"content-type": resp.headers.get("content-type", ""),
                              "x-shopify-api-version": resp.headers.get("x-shopify-api-version")}
                    return resp.status_code, picked, b"".join(chunks)
        except StoreError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if self._left() <= 0:
                raise StoreError("STORE_DEADLINE_EXCEEDED") from None
            raise _Retry() from None
        except Exception:                                           # anything else is a malformed exchange
            raise StoreError("STORE_RESPONSE_INVALID") from None

    def _with_retries(self, once: Callable[[], object]):
        """Run ``once`` with bounded throttle waits and bounded retries of transport failures / 5xx."""
        throttles = unavailable = 0
        while True:
            try:
                return once()
            except _Throttled as t:
                throttles += 1
                self.stats.throttle_waits += 1
                if throttles > self._max_throttle:
                    raise StoreError("STORE_THROTTLED") from None
                self._wait(t.wait_s)
            except _Retry:
                unavailable += 1
                if unavailable > self._max_unavail:
                    raise StoreError("STORE_UNAVAILABLE") from None
                self._wait(MIN_THROTTLE_WAIT_S)

    # ---- credentials ----------------------------------------------------------------------------------------

    def use_static_token(self, token: Secret) -> None:
        self._token = token

    def exchange_client_credentials(self, client_id: Secret, client_secret: Secret) -> None:
        """S2: POST /admin/oauth/access_token (form-encoded). The 24 h token is held for this session only."""
        body = urlencode({"grant_type": "client_credentials", "client_id": client_id.reveal(),
                          "client_secret": client_secret.reveal()}).encode("ascii")
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                   "Accept-Encoding": "identity"}

        def once():
            status, picked, raw = self._post(token_url(self.handle), headers, body)
            if status == 200:
                if not picked["content-type"].lower().startswith("application/json"):
                    raise StoreError("STORE_RESPONSE_INVALID")
                try:
                    tok = m.TokenResponse.model_validate_json(raw)
                except (ValidationError, ValueError):
                    raise StoreError("STORE_RESPONSE_INVALID") from None
                return tok
            if status in (400, 401, 403):
                raise StoreError("STORE_AUTH_REFUSED")
            if status in (402, 423):
                raise StoreError("STORE_SHOP_UNAVAILABLE")
            if status == 429:
                raise _Throttled(MIN_THROTTLE_WAIT_S)
            if status in RETRY_STATUSES:
                raise _Retry()
            raise StoreError("STORE_RESPONSE_INVALID")

        tok = self._with_retries(once)
        if tok.scope:
            check_scopes({s.strip() for s in tok.scope.split(",") if s.strip()})
        self._token = Secret(tok.access_token)

    # ---- GraphQL --------------------------------------------------------------------------------------------

    def _graphql(self, query: str, variables: dict, model):
        if self._token is None:
            raise StoreError("STORE_CREDENTIAL_UNREADABLE")
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity",
                   "X-Shopify-Access-Token": self._token.reveal()}
        url = graphql_url(self.handle, m.API_VERSION)

        def once():
            status, picked, raw = self._post(url, headers, body)
            if status in (401, 403):
                raise StoreError("STORE_AUTH_REFUSED")
            if status in (402, 423):
                raise StoreError("STORE_SHOP_UNAVAILABLE")
            if status == 429:
                raise _Throttled(MIN_THROTTLE_WAIT_S)
            if status in RETRY_STATUSES:
                raise _Retry()
            if status != 200:
                raise StoreError("STORE_RESPONSE_INVALID")
            if not picked["content-type"].lower().startswith("application/json"):
                raise StoreError("STORE_RESPONSE_INVALID")
            if picked["x-shopify-api-version"] != m.API_VERSION:
                raise StoreError("STORE_API_VERSION_MISMATCH")   # S4: Shopify fell forward; the schema may differ
            try:
                env = m.Envelope.model_validate_json(raw)
            except (ValidationError, ValueError):
                raise StoreError("STORE_RESPONSE_INVALID") from None
            cost = env.extensions.cost if env.extensions else None
            if cost is not None:
                self.stats.cost_points += (cost.actualQueryCost if cost.actualQueryCost is not None
                                           else cost.requestedQueryCost or 0.0)
            if env.errors:
                codes = {(e.extensions.code if e.extensions else None) for e in env.errors}
                if "THROTTLED" in codes:
                    raise _Throttled(_throttle_wait(cost))
                if "ACCESS_DENIED" in codes:
                    raise StoreError("STORE_AUTH_REFUSED")
                if "SHOP_INACTIVE" in codes:
                    raise StoreError("STORE_SHOP_UNAVAILABLE")
                if "MAX_COST_EXCEEDED" in codes:
                    raise _TooCostly()
                if "INTERNAL_SERVER_ERROR" in codes:
                    raise _Retry()
                raise StoreError("STORE_RESPONSE_INVALID")       # never a partial answer
            if env.data is None:
                raise StoreError("STORE_RESPONSE_INVALID")
            try:
                parsed = model.model_validate_json(json.dumps(env.data))
            except (ValidationError, ValueError, RecursionError):
                raise StoreError("STORE_RESPONSE_INVALID") from None
            return parsed, cost

        return self._with_retries(once)

    def preflight(self) -> m.PreflightData:
        """Scopes, shop identity and currency, before any order is read."""
        data, _ = self._graphql(m.PREFLIGHT_QUERY, {}, m.PreflightData)
        granted = {s.handle for s in data.currentAppInstallation.accessScopes}
        check_scopes(granted)
        self.stats.granted_scopes = sorted(granted)
        if data.shop.myshopifyDomain.lower() != self.host:
            raise StoreError("STORE_SHOP_MISMATCH")
        if data.shop.currencyCode != "USD":
            raise StoreError("STORE_CURRENCY_UNSUPPORTED")
        return data

    def read_orders(self, *, now: datetime, lookback_days: int, max_orders: int, max_cost_points: float,
                    max_pages: int = 1000) -> OrdersRead:
        """Orders created in the lookback window, newest first, capped. ``preflight`` must have run."""
        limited = False
        if lookback_days > READ_ORDERS_WINDOW_DAYS and "read_all_orders" not in self.stats.granted_scopes:
            lookback_days, limited = READ_ORDERS_WINDOW_DAYS, True
        start = (now - timedelta(days=lookback_days)).replace(microsecond=0)
        q = f"created_at:>='{start.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        orders: list = []
        after: Optional[str] = None
        size = m.ORDERS_PAGE_SIZE
        truncated: Optional[str] = None
        if self.stats.cost_points >= max_cost_points:
            raise StoreError("STORE_COST_BUDGET_EXHAUSTED")
        for _ in range(max_pages):
            self._check_alive()
            want = min(size, max_orders - len(orders))
            try:
                data, cost = self._graphql(m.ORDERS_QUERY, {"first": want, "after": after, "q": q}, m.OrdersData)
            except _TooCostly:
                if size == 1:
                    raise StoreError("STORE_QUERY_TOO_COSTLY") from None
                size = max(1, size // 2)
                self.stats.page_size_final = size
                continue
            self.stats.pages += 1
            conn = data.orders
            if len(conn.nodes) > want:
                raise StoreError("STORE_RESPONSE_INVALID")
            orders.extend(conn.nodes)
            if not conn.pageInfo.hasNextPage:
                break
            if not conn.pageInfo.endCursor or conn.pageInfo.endCursor == after:
                raise StoreError("STORE_RESPONSE_INVALID")       # a cursor that does not advance would loop
            if len(orders) >= max_orders:
                truncated = "max_orders"
                break
            if self.stats.cost_points >= max_cost_points:
                truncated = "cost_budget"
                break
            after = conn.pageInfo.endCursor
            if cost is not None and cost.requestedQueryCost is not None:
                deficit = cost.requestedQueryCost - cost.throttleStatus.currentlyAvailable
                if deficit > 0:                                   # pace before the bucket runs dry
                    self._wait(min(MAX_THROTTLE_WAIT_S, deficit / cost.throttleStatus.restoreRate))
        else:
            truncated = "max_pages"
        return OrdersRead(orders=orders, truncated_by=truncated, window_start=start, lookback_days=lookback_days,
                          lookback_limited_by_scope=limited, stats=self.stats)


def check_scopes(granted: set) -> None:
    """Refuse anything but the minimum read scopes (ADR 0020 section 4)."""
    if any(s.startswith("write_") for s in granted):
        raise StoreError("STORE_TOKEN_HAS_WRITE_SCOPE")
    if granted - ALLOWED_SCOPES:
        raise StoreError("STORE_TOKEN_EXCESS_SCOPE")
    if not REQUIRED_SCOPES <= granted:
        raise StoreError("STORE_SCOPE_MISSING")
