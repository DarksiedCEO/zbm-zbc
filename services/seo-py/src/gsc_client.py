"""
Google Search Console connector (go-live plan days 4-7; ADR 0017 section "Search Console (first-party truth)").

A client adds this service's Google service account (its ``client_email``) as a user of the client's own Search
Console property; this service then authenticates AS that service account and reads aggregate search performance for
that one property. Read-only scope, two fixed Google hosts, nothing written to Google, ever.

Facts this module relies on, each verified against Google's documentation (URLs in ADR 0017):

  token     the service-account JWT bearer flow: POST https://oauth2.googleapis.com/token, form-encoded,
            ``grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`` and ``assertion=<JWT>``; the JWT header is
            ``{"alg": "RS256", "typ": "JWT"}`` (``kid`` optional), its claims ``iss`` (the service account e-mail),
            ``scope``, ``aud`` = the token URL, ``iat`` and ``exp`` (at most one hour after ``iat``), signed with
            RSASSA-PKCS1-v1_5 / SHA-256; the answer carries ``access_token``, ``token_type`` "Bearer" and
            ``expires_in`` (3600 in Google's example); errors are ``{"error": ..., "error_description": ...}``.
  scope     ``https://www.googleapis.com/auth/webmasters.readonly`` (one of the two scopes both methods accept).
  sites     GET https://www.googleapis.com/webmasters/v3/sites -> ``{"siteEntry": [{"siteUrl", "permissionLevel"}]}``,
            permissionLevel one of siteOwner, siteFullUser, siteRestrictedUser, siteUnverifiedUser.
  query     POST https://www.googleapis.com/webmasters/v3/sites/{siteUrl}/searchAnalytics/query; startDate / endDate
            YYYY-MM-DD in Pacific Time, inclusive; dimensions (date, hour, country, device, page, query,
            searchAppearance); type (default web); aggregationType (auto, byPage, byProperty, ...); rowLimit 1..25,000
            (default 1,000); startRow zero-based, past the end = success with zero rows; dataState final (default) /
            all / hourly_all; rows[] of keys[], clicks, impressions, ctr (0..1), position; responseAggregationType;
            metadata only for dataState all / hourly_all. Results sort by clicks descending (date-grouped: ascending);
            the API does not promise every row ("top rows"); dates without data are omitted.
  errors    ``{"error": {"code", "message", "errors": [{"domain", "reason", "message", ...}]}}``; 401 unauthorized /
            authError / expired; 403 insufficientPermissions, quotaExceeded, rateLimitExceeded,
            userRateLimitExceeded, accessNotConfigured, forbidden, ...; 404 notFound; 429 rateLimitExceeded; 500
            internalError; 503 backendError.
  quotas    Search Analytics: 1,200 QPM per site, 1,200 QPM per user, 40,000 QPM / 30,000,000 QPD per project, plus
            short-term (10-minute) and long-term (1-day) load limits; page + query grouping is the most expensive.

Rules here (the Finance client's, finance_client.py, applied to Google):
  - a fixed host allowlist (``ALLOWED_HOSTS``) checked on every URL before anything is sent; https on 443 only; no
    redirect is ever followed; the resolved addresses must be globally routable;
  - one hard deadline per read over everything: DNS (on a gated resolver thread), connect, TLS, every read and
    write (``_DeadlineBackend``), and the body loop; the run's kill-switch guard before every socket operation;
  - retries only on connect errors, timeouts, 429, 500/502/503/504 and Google's per-minute rate reasons, bounded by
    ``attempts`` and the deadline; a request budget per read (``max_requests``);
  - bounded pagination (rows per page, pages, rows in total) that knows when it was truncated;
  - strict parsing: an answer that is not exactly the documented shape is GSC_PROTOCOL_ERROR, never data;
  - the private key never leaves ``ServiceAccountKey`` (no repr, no pickling, no error text); the access token
    lives in ``_Secret`` (redacted repr), cached until shortly before it expires.

Reason codes: GSC_NOT_CONNECTED, GSC_AUTH_REFUSED, GSC_PROPERTY_NOT_GRANTED, GSC_RATE_LIMITED, GSC_UNAVAILABLE,
GSC_PROTOCOL_ERROR, GSC_BUDGET_EXHAUSTED, GSC_KILLED.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import re
import time
from datetime import date
from typing import Callable, Iterator, Optional
from urllib.parse import quote, urlencode

import httpcore
import httpx

from finance_client import _DeadlineBackend, _DnsGate, _ip_literal, _Resolver, _system_resolve
from primitives import Killed

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/webmasters/v3"
ALLOWED_HOSTS = frozenset({"oauth2.googleapis.com", "www.googleapis.com"})
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
JWT_LIFETIME_S = 3600                         # Google: exp at most one hour after iat
ROW_LIMIT_MAX = 25_000                        # Google: rowLimit 1..25,000
GRANTED_LEVELS = frozenset({"siteOwner", "siteFullUser", "siteRestrictedUser"})
PERMISSION_LEVELS = GRANTED_LEVELS | {"siteUnverifiedUser"}
DIMENSIONS = ("date", "page", "query")         # the only ones this connector ever asks for
AGGREGATIONS = ("auto", "byPage", "byProperty")
MAX_TOKEN_BODY = 16 * 1024
MAX_SITES_BODY = 512 * 1024
MAX_QUERY_BODY = 8 * 1024 * 1024
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})          # per-minute: a retry may help
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})                # load limits: wait, never retry now
AUTH_REASONS = frozenset({"accessNotConfigured", "accountDisabled", "accountDeleted", "accountUnverified",
                          "unknownAuth", "authError", "unauthorized", "expired", "sslRequired"})
TOKEN_ERRORS = frozenset({"invalid_grant", "unauthorized_client", "access_denied", "admin_policy_enforced",
                          "invalid_client", "deleted_client", "disabled_client", "invalid_scope", "org_internal"})

CODES = ("GSC_NOT_CONNECTED", "GSC_AUTH_REFUSED", "GSC_PROPERTY_NOT_GRANTED", "GSC_RATE_LIMITED", "GSC_UNAVAILABLE",
         "GSC_PROTOCOL_ERROR", "GSC_BUDGET_EXHAUSTED", "GSC_KILLED")

_EMAIL = re.compile(r"[a-z][a-z0-9-]{4,29}@[a-z0-9][a-z0-9.-]{2,120}\.iam\.gserviceaccount\.com")
_KEY_ID = re.compile(r"[0-9a-f]{16,64}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# a Search Console property: a Domain property (sc-domain:<host>) or a URL-prefix property (http(s)://<host>/...)
PROPERTY = re.compile(r"sc-domain:(?P<d>[a-z0-9.-]{4,253})|(?P<s>https?)://(?P<h>[a-z0-9.-]{4,253})/(?P<p>[\x21-\x7e]{0,200})")


class GscError(Exception):
    """A read that did not produce data. ``code`` is one of ``CODES``; ``detail`` a short closed-vocabulary hint
    (never text from Google, never a credential)."""

    def __init__(self, code: str, detail: Optional[str] = None):
        if code not in CODES:
            raise AssertionError(f"not a GSC reason code: {code}")
        super().__init__(code)
        self.code = code
        self.detail = detail


class _Secret:
    """A credential held in memory with a redacted repr; never pickled, copied or printed."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a credential is never pickled")

    def __copy__(self):
        raise TypeError("a credential is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("a credential is never copied")


class ServiceAccountKey:
    """The service account's identity and its RSA signing key. Only ``sign`` uses the key; the PEM text is parsed once
    and dropped (Python cannot zero a string; the key itself lives in OpenSSL's memory through ``cryptography``)."""

    __slots__ = ("client_email", "key_id", "_signer")

    def __init__(self, client_email: str, key_id: str, signer):
        self.client_email = client_email
        self.key_id = key_id
        self._signer = signer

    def sign(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return self._signer.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def __repr__(self) -> str:
        return f"ServiceAccountKey(client_email={self.client_email!r}, key=<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a service-account key is never pickled")

    def __copy__(self):
        raise TypeError("a service-account key is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("a service-account key is never copied")


class KeyFileInvalid(Exception):
    """The key file is not a usable Google service-account key. The message is from a closed set and never quotes the
    file."""


def parse_service_account_key(raw: bytes) -> ServiceAccountKey:
    """A Google service-account JSON key (``type`` service_account, ``client_email``, ``private_key_id``,
    ``private_key`` PEM, ``token_uri``) -> ``ServiceAccountKey``. ``token_uri`` must be Google's token URL exactly:
    this module never sends an assertion anywhere else."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise KeyFileInvalid("not a JSON key file") from None
    if not isinstance(obj, dict) or obj.get("type") != "service_account":
        raise KeyFileInvalid("not a service-account key (type must be service_account)")
    email, kid, pem = obj.get("client_email"), obj.get("private_key_id"), obj.get("private_key")
    if not isinstance(email, str) or not _EMAIL.fullmatch(email):
        raise KeyFileInvalid("client_email is not a service-account e-mail")
    if not isinstance(kid, str) or not _KEY_ID.fullmatch(kid):
        raise KeyFileInvalid("private_key_id is missing or malformed")
    if obj.get("token_uri", TOKEN_URL) != TOKEN_URL:
        raise KeyFileInvalid("token_uri is not https://oauth2.googleapis.com/token")
    if not isinstance(pem, str) or "PRIVATE KEY-----" not in pem or len(pem) > 8192:
        raise KeyFileInvalid("private_key is missing or not PEM")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        signer = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except Exception:  # noqa: BLE001 - whatever cryptography says, the message is ours and quotes nothing
        raise KeyFileInvalid("private_key cannot be loaded") from None
    finally:
        pem = None
        obj = None
    if not isinstance(signer, rsa.RSAPrivateKey) or signer.key_size < 2048:
        raise KeyFileInvalid("private_key is not an RSA key of at least 2048 bits")
    return ServiceAccountKey(email, kid, signer)


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def build_assertion(key: ServiceAccountKey, now: int, scope: str = SCOPE) -> bytes:
    """The signed JWT for the token request (Google's service-account flow; RS256)."""
    header = {"alg": "RS256", "typ": "JWT", "kid": key.key_id}
    claims = {"iss": key.client_email, "scope": scope, "aud": TOKEN_URL, "iat": int(now),
              "exp": int(now) + JWT_LIFETIME_S}
    enc = lambda o: _b64url(json.dumps(o, separators=(",", ":"), sort_keys=True).encode("ascii"))  # noqa: E731
    signing_input = enc(header) + b"." + enc(claims)
    return signing_input + b"." + _b64url(key.sign(signing_input))


def property_host(prop: str) -> Optional[str]:
    """The host a property is about (``sc-domain:example.com`` -> example.com), or None when malformed."""
    m = PROPERTY.fullmatch(prop or "")
    if m is None:
        return None
    return m.group("d") or m.group("h")


def _finite(v, lo: float = 0.0, hi: float = 1e15) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def parse_token(obj) -> tuple[str, int]:
    """The token endpoint's 200 answer -> (access_token, expires_in). Strict: exactly the documented fields."""
    if not isinstance(obj, dict) or not set(obj) <= {"access_token", "expires_in", "token_type", "scope", "id_token"}:
        raise GscError("GSC_PROTOCOL_ERROR", "token_shape")
    tok, exp, typ = obj.get("access_token"), obj.get("expires_in"), obj.get("token_type")
    if not isinstance(tok, str) or not re.fullmatch(r"[\x21-\x7e]{16,4096}", tok):
        raise GscError("GSC_PROTOCOL_ERROR", "token_value")
    if not isinstance(typ, str) or typ.lower() != "bearer":
        raise GscError("GSC_PROTOCOL_ERROR", "token_type")
    if not isinstance(exp, int) or isinstance(exp, bool) or not 60 <= exp <= 86_400:
        raise GscError("GSC_PROTOCOL_ERROR", "token_expiry")
    if "scope" in obj and (not isinstance(obj["scope"], str) or SCOPE not in obj["scope"].split()):
        raise GscError("GSC_PROTOCOL_ERROR", "token_scope")
    return tok, exp


def parse_sites(obj, prop: str) -> str:
    """sites.list -> the permission level for ``prop``; raises GSC_PROPERTY_NOT_GRANTED when absent or unverified."""
    if not isinstance(obj, dict) or not set(obj) <= {"siteEntry"}:
        raise GscError("GSC_PROTOCOL_ERROR", "sites_shape")
    entries = obj.get("siteEntry", [])
    if not isinstance(entries, list) or len(entries) > 10_000:
        raise GscError("GSC_PROTOCOL_ERROR", "sites_shape")
    level = None
    for e in entries:
        if not isinstance(e, dict) or set(e) != {"siteUrl", "permissionLevel"} \
                or not isinstance(e["siteUrl"], str) or e["permissionLevel"] not in PERMISSION_LEVELS:
            raise GscError("GSC_PROTOCOL_ERROR", "site_entry")
        if e["siteUrl"] == prop:
            level = e["permissionLevel"]
    if level not in GRANTED_LEVELS:
        raise GscError("GSC_PROPERTY_NOT_GRANTED", "not_listed" if level is None else "unverified")
    return level


def parse_rows(obj, dims: tuple, row_limit: int, aggregation: str, start: str, end: str) -> list:
    """One searchAnalytics.query answer -> rows as (keys tuple, clicks int, impressions int, ctr, position). Strict:
    documented top-level fields only; every row has exactly ``len(dims)`` string keys; dates inside the window;
    clicks and impressions whole, finite, non-negative, clicks <= impressions; ctr in 0..1; position >= 0;
    no more rows than asked for; no repeated key."""
    if not isinstance(obj, dict) or not set(obj) <= {"rows", "responseAggregationType", "metadata"}:
        raise GscError("GSC_PROTOCOL_ERROR", "query_shape")
    agg = obj.get("responseAggregationType")
    if agg is not None and (agg not in AGGREGATIONS or (aggregation != "auto" and agg != aggregation)):
        raise GscError("GSC_PROTOCOL_ERROR", "aggregation")
    meta = obj.get("metadata")
    if meta is not None and (not isinstance(meta, dict)
                             or not set(meta) <= {"first_incomplete_date", "first_incomplete_hour"}
                             or not all(isinstance(v, str) and len(v) <= 40 for v in meta.values())):
        raise GscError("GSC_PROTOCOL_ERROR", "metadata")
    rows = obj.get("rows", [])
    if not isinstance(rows, list) or len(rows) > row_limit:
        raise GscError("GSC_PROTOCOL_ERROR", "row_count")
    out, seen = [], set()
    di = dims.index("date") if "date" in dims else None
    for r in rows:
        if not isinstance(r, dict) or not set(r) <= {"keys", "clicks", "impressions", "ctr", "position"} \
                or not {"clicks", "impressions"} <= set(r):
            raise GscError("GSC_PROTOCOL_ERROR", "row_shape")
        keys = r.get("keys", [])
        if dims and (not isinstance(keys, list) or len(keys) != len(dims)
                     or not all(isinstance(k, str) and 0 < len(k) <= 4096 for k in keys)):
            raise GscError("GSC_PROTOCOL_ERROR", "row_keys")
        if not dims and keys:
            raise GscError("GSC_PROTOCOL_ERROR", "row_keys")
        keys = tuple(keys)
        if di is not None:
            d = keys[di]
            if not _DATE.fullmatch(d) or not start <= d <= end:
                raise GscError("GSC_PROTOCOL_ERROR", "row_date")
            try:
                date.fromisoformat(d)
            except ValueError:
                raise GscError("GSC_PROTOCOL_ERROR", "row_date") from None
        c, i = r["clicks"], r["impressions"]
        if not _finite(c) or not _finite(i) or c != int(c) or i != int(i) or c > i:
            raise GscError("GSC_PROTOCOL_ERROR", "row_counts")
        ctr, pos = r.get("ctr"), r.get("position")
        if ctr is not None and not _finite(ctr, 0.0, 1.0):
            raise GscError("GSC_PROTOCOL_ERROR", "row_ctr")
        if pos is not None and not _finite(pos, 0.0, 1e6):
            raise GscError("GSC_PROTOCOL_ERROR", "row_position")
        h = hashlib.sha256("\x00".join(keys).encode("utf-8", "surrogatepass")).digest()
        if h in seen:
            raise GscError("GSC_PROTOCOL_ERROR", "row_repeated")
        seen.add(h)
        out.append((keys, int(c), int(i), ctr, pos))
    return out


class _Retry(Exception):
    def __init__(self, code: str, detail: str, retry_after: Optional[float] = None):
        super().__init__(code)
        self.code, self.detail, self.retry_after = code, detail, retry_after


class _GuardedStream(httpcore.NetworkStream):
    """The run's kill-switch guard before every read, write and TLS step."""

    def __init__(self, inner, guard: Callable[[], None]):
        self._s, self._g = inner, guard

    def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        self._g()
        return self._s.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self._g()
        self._s.write(buffer, timeout)

    def close(self) -> None:
        self._s.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._g()
        return _GuardedStream(self._s.start_tls(ssl_context, server_hostname, timeout), self._g)

    def get_extra_info(self, info: str):
        return self._s.get_extra_info(info)


class _GuardedBackend(httpcore.NetworkBackend):
    def __init__(self, inner: httpcore.NetworkBackend, guard: Callable[[], None]):
        self._b, self._g = inner, guard

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self._g()
        if host not in ALLOWED_HOSTS:                       # defence in depth: the URL check came first
            raise httpcore.ConnectError("host not on the Search Console allowlist")
        return _GuardedStream(self._b.connect_tcp(host, port, timeout, local_address, socket_options), self._g)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never used")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _GlobalResolver:
    """The finance client's gated, deadline-bounded resolver, plus: every address must be globally routable (a
    poisoned or split-horizon answer pointing a Google name at a private address is refused, never connected to)."""

    def __init__(self, inner: _Resolver):
        self._inner = inner

    def __call__(self, host: str, port: int, timeout: float) -> list:
        if _ip_literal(host) is not None:
            raise httpcore.ConnectError("an address literal is never a Search Console host")
        addrs = self._inner(host, port, timeout)
        ok = [a for a in addrs if ipaddress.ip_address(a).is_global]
        if len(ok) != len(addrs) or not ok:
            raise httpcore.ConnectError("a Search Console host resolved to a non-global address")
        return ok


_GSC_DNS_GATE = _DnsGate(2)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_GSC_DNS_GATE._after_fork_in_child)


def check_url(url: str) -> httpx.URL:
    """The allowlist: https, a Google host from ``ALLOWED_HOSTS``, port 443, no credentials, no fragment."""
    try:
        u = httpx.URL(url)
    except Exception:  # noqa: BLE001
        raise GscError("GSC_PROTOCOL_ERROR", "url") from None
    if u.scheme != "https" or u.host not in ALLOWED_HOSTS or u.port not in (None, 443) or u.userinfo or u.fragment:
        raise GscError("GSC_PROTOCOL_ERROR", "host_not_allowed")
    return u


class GscClient:
    """One service account's reads. Thread-safe enough for this service: reads run one per tenant (the audit lock)
    and the token cache is replaced atomically."""

    connected = True

    def __init__(self, key: ServiceAccountKey, *, timeout_s: float = 10.0, deadline_s: float = 60.0,
                 attempts: int = 3, backoff_s: tuple = (1.0, 3.0), page_rows: int = 5000, max_pages: int = 10,
                 max_rows: int = 50_000, max_requests: int = 30,
                 transport: Optional[httpx.BaseTransport] = None, network_backend=None,
                 monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, resolve: Callable[[str, int], list] = _system_resolve,
                 dns_gate: Optional[_DnsGate] = None):
        if not isinstance(key, ServiceAccountKey):
            raise TypeError("GscClient needs a ServiceAccountKey")
        if not 1 <= attempts <= 5 or not 1 <= page_rows <= ROW_LIMIT_MAX or max_pages < 1 or max_rows < 1:
            raise ValueError("GscClient bounds out of range")
        self.key = key
        self.timeout_s, self.deadline_s = float(timeout_s), float(deadline_s)
        self.attempts, self.backoff_s = attempts, tuple(backoff_s)
        self.page_rows, self.max_pages, self.max_rows, self.max_requests = page_rows, max_pages, max_rows, max_requests
        self._transport = transport                  # tests only: replaces the deadline-bound production transport
        self._inner_backend = network_backend        # tests only: the socket layer under the deadline backend
        self._mono, self._wall, self._sleep = monotonic, wall, sleep
        self._resolver = _GlobalResolver(_Resolver(resolve, dns_gate if dns_gate is not None else _GSC_DNS_GATE))
        self._token: Optional[tuple] = None          # (_Secret, valid-until monotonic)
        self.requests_made = 0                       # every HTTP attempt this client ever sent (status, tests)

    def __repr__(self) -> str:
        return f"GscClient(client_email={self.key.client_email!r})"

    # ------------------------------------------------------------------ one read

    def open_read(self, guard: Callable[[], None]) -> "_Read":
        """A read session: one overall deadline and one request budget for everything it does."""
        return _Read(self, guard, self._mono() + self.deadline_s)

    # ------------------------------------------------------------------ token

    def _cached_token(self) -> Optional[_Secret]:
        t = self._token
        if t is not None and self._mono() < t[1]:
            return t[0]
        return None

    def invalidate_token(self) -> None:
        self._token = None


class _Read:
    def __init__(self, client: GscClient, guard: Callable[[], None], end: float):
        self.c, self.end, self.requests = client, end, 0
        self._guard_fn = guard

    def _guard(self) -> None:
        try:
            self._guard_fn()
        except Killed as k:
            raise GscError("GSC_KILLED", k.code if re.fullmatch(r"[A-Z_]{1,40}", k.code or "") else "killed") \
                from None

    def left(self) -> float:
        return self.end - self.c._mono()

    # ------------------------------------------------------------------ public steps

    def token(self) -> _Secret:
        tok = self.c._cached_token()
        if tok is not None:
            return tok
        started = self.c._mono()
        body = urlencode({"grant_type": GRANT_TYPE,
                          "assertion": build_assertion(self.c.key, int(self.c._wall())).decode("ascii")}).encode()
        status, raw = self._send("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body,
                                 MAX_TOKEN_BODY, token_call=True)
        if status != 200:
            raise GscError("GSC_PROTOCOL_ERROR", f"token_http_{status}")
        tok, expires_in = parse_token(_json(raw))
        margin = min(300, expires_in // 2)
        secret = _Secret(tok)
        self.c._token = (secret, started + expires_in - margin)
        return secret

    def check_property(self, prop: str) -> str:
        status, raw = self._api("GET", f"{API_BASE}/sites", None, MAX_SITES_BODY, site_scoped=False)
        return parse_sites(_json(raw), prop)

    def query(self, prop: str, start: str, end: str, dims: tuple, aggregation: str, row_limit: int,
              start_row: int) -> list:
        if not set(dims) <= set(DIMENSIONS) or len(set(dims)) != len(dims) or aggregation not in AGGREGATIONS:
            raise AssertionError("a dimension or aggregation this connector never asks for")
        body = {"startDate": start, "endDate": end, "dimensions": list(dims), "type": "web", "dataState": "final",
                "aggregationType": aggregation, "rowLimit": row_limit, "startRow": start_row}
        url = f"{API_BASE}/sites/{quote(prop, safe='')}/searchAnalytics/query"
        status, raw = self._api("POST", url, json.dumps(body, separators=(",", ":")).encode(), MAX_QUERY_BODY,
                                site_scoped=True)
        return parse_rows(_json(raw), dims, row_limit, aggregation, start, end)

    def pages(self, prop: str, start: str, end: str, dims: tuple, aggregation: str, stats: dict) -> Iterator[list]:
        """Bounded pagination: pages of at most ``page_rows`` rows from startRow 0; stops at a short (or empty) page,
        at ``max_pages`` pages or ``max_rows`` rows. ``stats`` gets ``rows_read``, ``pages_read`` and ``truncated``
        (a cap stopped it right after a FULL page, so more rows may exist). A key repeated across pages (the data
        moved between requests) is GSC_PROTOCOL_ERROR."""
        stats.update(rows_read=0, pages_read=0, truncated=False)
        seen: set = set()
        start_row = 0
        while True:
            if stats["pages_read"] >= self.c.max_pages or stats["rows_read"] >= self.c.max_rows:
                stats["truncated"] = True
                return
            limit = min(self.c.page_rows, self.c.max_rows - stats["rows_read"])
            rows = self.query(prop, start, end, dims, aggregation, limit, start_row)
            stats["pages_read"] += 1
            stats["rows_read"] += len(rows)
            for r in rows:
                h = hashlib.sha256("\x00".join(r[0]).encode("utf-8", "surrogatepass")).digest()
                if h in seen:
                    raise GscError("GSC_PROTOCOL_ERROR", "row_repeated_across_pages")
                seen.add(h)
            if rows:
                yield rows
            if len(rows) < limit:
                return
            start_row += len(rows)

    # ------------------------------------------------------------------ transport

    def _api(self, method: str, url: str, body: Optional[bytes], cap: int, site_scoped: bool) -> tuple[int, bytes]:
        """An authenticated call. A 401 with a cached token is retried once with a fresh token."""
        for fresh_try in (False, True):
            tok = self.token()
            headers = {"Authorization": f"Bearer {tok.reveal()}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            status, raw = self._send(method, url, headers, body, cap, site_scoped=site_scoped)
            if status == 401:
                self.c.invalidate_token()
                if not fresh_try:
                    continue
                raise GscError("GSC_AUTH_REFUSED", "api_401")
            if status != 200:
                raise GscError("GSC_PROTOCOL_ERROR", f"http_{status}")
            return status, raw
        raise GscError("GSC_AUTH_REFUSED", "api_401")      # pragma: no cover - the loop always returns or raises

    def _send(self, method: str, url: str, headers: dict, body: Optional[bytes], cap: int,
              token_call: bool = False, site_scoped: bool = False) -> tuple[int, bytes]:
        """One logical request with bounded retries. Returns (status, body) for 200 and 401; raises GscError for
        everything else (after retries where a retry can help)."""
        check_url(url)
        last: Optional[_Retry] = None
        for attempt in range(self.c.attempts):
            if self.left() <= 0:
                break
            if self.requests >= self.c.max_requests:
                raise GscError("GSC_BUDGET_EXHAUSTED", "requests_per_read")
            self._guard()
            self.requests += 1
            self.c.requests_made += 1
            try:
                status, raw, hdrs = self._once(method, url, headers, body, cap)
                if status in (200, 401) and not (token_call and status == 401):
                    return status, raw
                self._classify(status, raw, hdrs, token_call, site_scoped)
            except _Retry as r:
                last = r
            if attempt + 1 < self.c.attempts:
                pause = self.c.backoff_s[min(attempt, len(self.c.backoff_s) - 1)] if self.c.backoff_s else 0.0
                if last is not None and last.retry_after is not None:
                    pause = max(pause, last.retry_after)
                if self.c._mono() + pause >= self.end:
                    break
                self._sleep_guarded(pause)
        if last is None:
            raise GscError("GSC_UNAVAILABLE", "deadline")
        raise GscError(last.code, last.detail)

    def _sleep_guarded(self, pause: float) -> None:
        self._guard()
        self.c._sleep(pause)
        self._guard()

    def _classify(self, status: int, raw: bytes, hdrs: dict, token_call: bool, site_scoped: bool) -> None:
        """Raise the right outcome for a non-success status: _Retry (a retry may help) or GscError (final)."""
        ra = _retry_after(hdrs.get("retry-after"))
        if token_call:
            if status in (400, 401, 403):
                err = _json(raw, strict=False)
                e = err.get("error") if isinstance(err, dict) else None
                raise GscError("GSC_AUTH_REFUSED", e if e in TOKEN_ERRORS else "token_refused")
            if status == 429:
                raise _Retry("GSC_RATE_LIMITED", "http_429", ra)
            if status in RETRY_STATUSES:
                raise _Retry("GSC_UNAVAILABLE", f"http_{status}", ra)
            raise GscError("GSC_PROTOCOL_ERROR", f"token_http_{status}")
        if status == 429:
            raise _Retry("GSC_RATE_LIMITED", "http_429", ra)
        if status in RETRY_STATUSES:
            raise _Retry("GSC_UNAVAILABLE", f"http_{status}", ra)
        if status == 403:
            reason = _google_reason(raw)
            if reason in RATE_REASONS:
                raise _Retry("GSC_RATE_LIMITED", reason, ra)
            if reason in QUOTA_REASONS:
                raise GscError("GSC_RATE_LIMITED", reason)
            if reason in AUTH_REASONS:
                raise GscError("GSC_AUTH_REFUSED", reason)
            raise GscError("GSC_PROPERTY_NOT_GRANTED" if site_scoped else "GSC_AUTH_REFUSED",
                           reason if reason in ("insufficientPermissions", "forbidden") else "http_403")
        if status == 404 and site_scoped:
            raise GscError("GSC_PROPERTY_NOT_GRANTED", "http_404")
        raise GscError("GSC_PROTOCOL_ERROR", f"http_{status}")

    def _once(self, method: str, url: str, headers: dict, body: Optional[bytes], cap: int) -> tuple[int, bytes, dict]:
        left = self.left()
        if left <= 0:
            raise _Retry("GSC_UNAVAILABLE", "deadline")
        transport = self.c._transport
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False, retries=0)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise GscError("GSC_NOT_CONNECTED", "no_deadline_backend")   # never call Google without the deadline
            pool._network_backend = _GuardedBackend(
                _DeadlineBackend(self.c._mono, self.end, inner=self.c._inner_backend, resolver=self.c._resolver),
                self._guard)
        hdrs = {**headers, "Accept-Encoding": "identity", "User-Agent": "zbm-seo-py-gsc/1"}
        try:
            with httpx.Client(timeout=httpx.Timeout(min(self.c.timeout_s, left)), transport=transport,
                              follow_redirects=False, trust_env=False) as client:
                with client.stream(method, url, headers=hdrs, content=body) as resp:
                    enc = resp.headers.get("content-encoding", "identity").strip().lower()
                    if enc not in ("identity", ""):
                        raise GscError("GSC_PROTOCOL_ERROR", "content_encoding")
                    declared = resp.headers.get("content-length")
                    if declared is not None and (not declared.isdigit() or int(declared) > cap):
                        raise GscError("GSC_PROTOCOL_ERROR", "body_too_large")
                    chunks, size = [], 0
                    for chunk in resp.iter_raw():
                        if self.c._mono() >= self.end:
                            raise _Retry("GSC_UNAVAILABLE", "deadline")
                        self._guard()
                        size += len(chunk)
                        if size > cap:
                            raise GscError("GSC_PROTOCOL_ERROR", "body_too_large")
                        chunks.append(chunk)
                    keep = {k: resp.headers.get(k) for k in ("retry-after",) if resp.headers.get(k) is not None}
                    return resp.status_code, b"".join(chunks), keep
        except (GscError, _Retry):
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise _Retry("GSC_UNAVAILABLE", "network") from None
        except Exception:  # noqa: BLE001 - anything else is a malformed exchange, never a 500 and never a message
            raise GscError("GSC_PROTOCOL_ERROR", "exchange") from None


def _retry_after(v: Optional[str]) -> Optional[float]:
    if v is None or not re.fullmatch(r"[0-9]{1,4}", v.strip()):
        return None
    return float(min(int(v.strip()), 60))


def _google_reason(raw: bytes) -> Optional[str]:
    obj = _json(raw, strict=False)
    try:
        errs = obj["error"]["errors"]
        reason = errs[0]["reason"] if isinstance(errs, list) and errs else None
    except (KeyError, TypeError, IndexError):
        return None
    return reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9]{1,60}", reason) else None


def _json(body: bytes, strict: bool = True):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        if strict:
            raise GscError("GSC_PROTOCOL_ERROR", "json") from None
        return None
