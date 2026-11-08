"""
Bing Webmaster Tools adapter (ADR 0047, "Bing Webmaster Tools"): Priya's second first-party source. FIXTURES ONLY in
this build: the adapter is built and tested against a fake Bing, but nothing wires it to a key — the environment
switch ``SEO_BING_WEBMASTER_KEY_FILE`` stays in ``config.NOT_BUILT`` (setting it refuses start), the port in
``ports.Ports.default`` stays the ``NotConnectedSource("bing_webmaster")`` it always was, and a client built without
an injected transport refuses to call Bing (``BING_NOT_CONNECTED`` ``no_deadline_backend``): the production
deadline-bound transport the Google connector has is not built here.

Facts this module relies on, read on Microsoft's pages on Nov 8 2026 (WebFetch; URLs in ADR 0047):

  method    ``GetQueryStats(siteUrl)``: "Get detailed traffic statistics for top queries"; "The data will be updated
            every week"; returns ``List<QueryStats>``. The page is marked prerelease documentation.
            https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getquerystats?view=bing-webmaster-dotnet
  JSON      ``GET https://ssl.bing.com/webmaster/api.svc/json/GetQueryStats?siteUrl=<site>&apikey=<key>``; the answer
            ``{"d": [{"__type": "QueryStats:#Microsoft.Bing.Webmaster.Api", "AvgClickPosition": 18,
            "AvgImpressionPosition": 17, "Clicks": 15, "Date": "/Date(1316156400000-0700)/", "Impressions": 100,
            "Query": "query"}]}`` (the page's sample).
  protocol  JSON/HTTP GET ``https://ssl.bing.com/webmaster/api.svc/json/METHOD_NAME?apikey=API_KEY&...``; SOAP and POX
            retire on Aug 31 2026, JSON/HTTP stays. https://learn.microsoft.com/en-us/bingwebmaster/api-protocols
  errors    "HTTP status code 400 will be returned for POX and JSON formats", body e.g.
            ``{"ErrorCode":3,"Message":"InvalidApiKey"}``. https://learn.microsoft.com/en-us/bingwebmaster/getting-started
  key       one API key per Bing Webmaster Tools USER (not per site), valid for every site that user has verified;
            deleting it and generating a new one is the rotation. https://learn.microsoft.com/en-us/bingwebmaster/getting-access

Not found on the pages read (so nothing here depends on them): quotas and rate limits; the period GetQueryStats
covers and the period one row's ``Date`` stands for; whether ``AvgImpressionPosition`` is on the same scale as
Search Console's position. Rows are therefore reported with their own date range, never merged with Search Console's
window, and positions are reported as Bing gave them.

``GetKeywordStats`` / ``GetKeyword`` (Bing's keyword research tool) exist in the interface
(https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi?view=bing-webmaster-dotnet)
but their member pages could not be read (the URL was too long for the fetcher); their answer shape comes from a
secondary source only. They would also send the client's query text to Microsoft. ``keyword_stats`` is therefore an
interface that answers ``BING_NOT_CONNECTED`` ``not_built``; it makes no request.

The API key travels in the URL (Bing's design). It lives in ``ApiKey`` (masked repr, no pickling); no URL, header,
answer text or exception message is ever logged, returned or recorded.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx

from primitives import Killed

API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"
ALLOWED_HOSTS = frozenset({"ssl.bing.com"})
METHODS = ("GetQueryStats",)                        # the only method this adapter ever calls
CODES = ("BING_NOT_CONNECTED", "BING_AUTH_REFUSED", "BING_REFUSED", "BING_RATE_LIMITED", "BING_UNAVAILABLE",
         "BING_PROTOCOL_ERROR", "BING_BUDGET_EXHAUSTED", "BING_KILLED")
BLOCKED = frozenset({"BING_AUTH_REFUSED", "BING_REFUSED", "BING_BUDGET_EXHAUSTED"})
MAX_BODY = 8 * 1024 * 1024
MAX_ROWS = 50_000
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ROW_FIELDS = frozenset({"__type", "AvgClickPosition", "AvgImpressionPosition", "Clicks", "Date", "Impressions",
                        "Query"})
ROW_REQUIRED = frozenset({"Clicks", "Date", "Impressions", "Query"})
ROW_MAX_FIELDS = 64                                  # unknown fields are ignored, but a row is never unbounded
_DATE = re.compile(r"/Date\((-?[0-9]{1,15})([+-][0-9]{4})?\)/")
_INT_MAX = 10 ** 15
SITE_URL = re.compile(r"https?://[a-z0-9.-]{4,253}/")


class BingError(Exception):
    """A closed reason code and a short closed detail; never Bing's text, a URL or the key."""

    def __init__(self, code: str, detail: str | None = None):
        if code not in CODES:
            raise AssertionError(f"unknown Bing code {code}")
        super().__init__(code)
        self.code, self.detail = code, detail


class ApiKey:
    """The Bing Webmaster API key: masked repr, never pickled or copied."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{16,128}", value):
            raise ValueError("a Bing Webmaster API key is 16..128 letters and digits")
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "ApiKey(***)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("ApiKey cannot be pickled")

    def __copy__(self):
        raise TypeError("ApiKey cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("ApiKey cannot be copied")


class NotConnectedBing:
    """The port without an adapter: never a request, never invented data."""

    connected = False
    name = "bing_webmaster"

    def query_stats(self, site_url: str, guard: Callable[[], None]):
        raise BingError("BING_NOT_CONNECTED", "no_key")


def site_url_for(domain: str) -> str:
    """The Bing site URL for one of the tenant's registered domains (Bing's own sample spells it ``http://example.com``;
    the request names the scheme the site is verified under, https by default)."""
    return f"https://{domain}/"


def check_url(url: str) -> None:
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.port not in (None, 443) or p.username \
            or p.password or p.fragment or not p.path.startswith("/webmaster/api.svc/json/"):
        raise BingError("BING_PROTOCOL_ERROR", "url_not_allowed")


def _finite(v, lo: float = 0.0, hi: float = 1e15) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    if isinstance(v, int) and abs(v) > _INT_MAX:
        return False
    try:
        return math.isfinite(v) and lo <= v <= hi
    except (OverflowError, ValueError):
        return False


def parse_date(v) -> str:
    """``/Date(1316156400000-0700)/`` -> the calendar date in the offset Bing gave (UTC when none), ISO format."""
    m = _DATE.fullmatch(v) if isinstance(v, str) else None
    if m is None:
        raise BingError("BING_PROTOCOL_ERROR", "row_date")
    ms = int(m.group(1))
    off = m.group(2)
    try:
        tz = timezone.utc
        if off:
            sign = -1 if off[0] == "-" else 1
            hh, mm = int(off[1:3]), int(off[3:5])
            if hh > 14 or mm > 59:
                raise ValueError
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        d = datetime.fromtimestamp(ms / 1000, tz).date()
    except (ValueError, OverflowError, OSError):
        raise BingError("BING_PROTOCOL_ERROR", "row_date") from None
    if not 2000 <= d.year <= 2100:
        raise BingError("BING_PROTOCOL_ERROR", "row_date")
    return d.isoformat()


def parse_query_stats(obj) -> list:
    """GetQueryStats' JSON answer -> rows (query, date, clicks, impressions, avg_click_position,
    avg_impression_position). Strict: exactly ``{"d": [...]}``; every row an object holding the required documented
    fields (Query, Date, Clicks, Impressions), each validated, and the optional documented ones validated when
    present; fields Bing adds that are not documented are ignored (never read, never passed on), up to
    ``ROW_MAX_FIELDS`` per row; whole, finite, non-negative counts with clicks <= impressions; positions finite and
    >= 0 or absent; a (query, date) pair once."""
    if not isinstance(obj, dict) or set(obj) != {"d"}:
        raise BingError("BING_PROTOCOL_ERROR", "answer_shape")
    rows = obj["d"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise BingError("BING_PROTOCOL_ERROR", "row_count")
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict) or len(r) > ROW_MAX_FIELDS or not ROW_REQUIRED <= set(r):
            raise BingError("BING_PROTOCOL_ERROR", "row_shape")
        t = r.get("__type")
        if t is not None and (not isinstance(t, str) or not t.startswith("QueryStats:")):
            raise BingError("BING_PROTOCOL_ERROR", "row_type")
        q = r["Query"]
        if not isinstance(q, str) or not 0 < len(q) <= 4096:
            raise BingError("BING_PROTOCOL_ERROR", "row_query")
        c, i = r["Clicks"], r["Impressions"]
        if not _finite(c) or not _finite(i) or c != int(c) or i != int(i) or c > i:
            raise BingError("BING_PROTOCOL_ERROR", "row_counts")
        pos = []
        for k in ("AvgClickPosition", "AvgImpressionPosition"):
            v = r.get(k)
            if v is not None and not _finite(v, 0.0, 1e6):
                raise BingError("BING_PROTOCOL_ERROR", "row_position")
            pos.append(v)
        d = parse_date(r["Date"])
        h = hashlib.sha256(f"{q}\x00{d}".encode("utf-8", "surrogatepass")).digest()
        if h in seen:
            raise BingError("BING_PROTOCOL_ERROR", "row_repeated")
        seen.add(h)
        out.append((q, d, int(c), int(i), pos[0], pos[1]))
    return out


class BingWebmasterClient:
    """One API key's reads, fixtures only (see the module docstring). ``transport`` is required: without it the client
    refuses to call Bing, because the production deadline-bound transport is not built."""

    connected = True
    name = "bing_webmaster"

    def __init__(self, key: ApiKey, *, transport: httpx.BaseTransport | None = None, timeout_s: float = 10.0,
                 deadline_s: float = 30.0, attempts: int = 3, backoff_s: tuple = (1.0, 3.0), max_requests: int = 6,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        if not isinstance(key, ApiKey):
            raise TypeError("BingWebmasterClient needs an ApiKey")
        if not 1 <= attempts <= 5 or not 1 <= max_requests <= 50:
            raise ValueError("BingWebmasterClient bounds out of range")
        self.key = key
        self._transport = transport
        self.timeout_s, self.deadline_s = float(timeout_s), float(deadline_s)
        self.attempts, self.backoff_s, self.max_requests = attempts, tuple(backoff_s), max_requests
        self._mono, self._sleep = monotonic, sleep
        self.requests_made = 0

    def __repr__(self) -> str:
        return "BingWebmasterClient(key=***)"

    def keyword_stats(self, q: str, country: str, language: str, guard: Callable[[], None]):
        """GetKeywordStats (Bing's keyword research tool): an interface only, never called (module docstring)."""
        raise BingError("BING_NOT_CONNECTED", "not_built")

    def query_stats(self, site_url: str, guard: Callable[[], None]) -> tuple[list, dict]:
        """GetQueryStats for one site. Returns (rows, stats) with ``requests``. Raises BingError."""
        if not isinstance(site_url, str) or not SITE_URL.fullmatch(site_url):
            raise BingError("BING_PROTOCOL_ERROR", "site_url")
        url = f"{API_BASE}/GetQueryStats?" + urlencode({"siteUrl": site_url, "apikey": self.key.reveal()})
        check_url(url)
        end = self._mono() + self.deadline_s
        stats = {"requests": 0}
        raw = self._send(url, guard, end, stats)
        return parse_query_stats(_json(raw)), stats

    # ------------------------------------------------------------------ transport

    def _guard(self, guard) -> None:
        try:
            guard()
        except Killed as k:
            raise BingError("BING_KILLED", k.code if re.fullmatch(r"[A-Z_]{1,40}", k.code or "") else "killed") \
                from None

    def _send(self, url: str, guard, end: float, stats: dict) -> bytes:
        last: tuple | None = None
        for attempt in range(self.attempts):
            if self._mono() >= end:
                break
            if stats["requests"] >= self.max_requests:
                raise BingError("BING_BUDGET_EXHAUSTED", "requests_per_read")
            self._guard(guard)
            stats["requests"] += 1
            self.requests_made += 1
            status, raw = self._once(url, guard, end)
            if status == 200:
                return raw
            last = self._classify(status, raw)
            if attempt + 1 < self.attempts:
                pause = self.backoff_s[min(attempt, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
                if self._mono() + pause >= end:
                    break
                self._guard(guard)
                self._sleep(pause)
        if last is None:
            raise BingError("BING_UNAVAILABLE", "deadline")
        raise BingError(*last)

    @staticmethod
    def _classify(status: int, raw: bytes) -> tuple:
        """A non-200 answer -> (code, detail) to retry with, or raises BingError when a retry cannot help."""
        if status == 429:
            return ("BING_RATE_LIMITED", "http_429")
        if status in RETRY_STATUSES:
            return ("BING_UNAVAILABLE", f"http_{status}")
        if status in (401, 403):
            raise BingError("BING_AUTH_REFUSED", f"http_{status}")
        if status == 400:
            err = _json(raw, strict=False)
            if not isinstance(err, dict) or not set(err) <= {"ErrorCode", "Message"}:
                raise BingError("BING_PROTOCOL_ERROR", "error_shape")
            code, msg = err.get("ErrorCode"), err.get("Message")
            if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 9999 \
                    or not isinstance(msg, str):
                raise BingError("BING_PROTOCOL_ERROR", "error_shape")
            if msg == "InvalidApiKey":
                raise BingError("BING_AUTH_REFUSED", "InvalidApiKey")
            raise BingError("BING_REFUSED", f"error_code_{code}")
        raise BingError("BING_PROTOCOL_ERROR", f"http_{status}")

    def _once(self, url: str, guard, end: float) -> tuple[int, bytes]:
        left = end - self._mono()
        if left <= 0:
            raise BingError("BING_UNAVAILABLE", "deadline")
        if self._transport is None:
            raise BingError("BING_NOT_CONNECTED", "no_deadline_backend")   # never call Bing unbounded
        try:
            with httpx.Client(timeout=httpx.Timeout(min(self.timeout_s, left)), transport=self._transport,
                              follow_redirects=False, trust_env=False) as client, \
                    client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                                       "User-Agent": "zbm-seo-py-bing/1"}) as resp:
                enc = resp.headers.get("content-encoding", "identity").strip().lower()
                if enc not in ("identity", ""):
                    raise BingError("BING_PROTOCOL_ERROR", "content_encoding")
                declared = resp.headers.get("content-length")
                if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
                    raise BingError("BING_PROTOCOL_ERROR", "body_too_large")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    if self._mono() >= end:
                        raise BingError("BING_UNAVAILABLE", "deadline")
                    self._guard(guard)
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise BingError("BING_PROTOCOL_ERROR", "body_too_large")
                    chunks.append(chunk)
                return resp.status_code, b"".join(chunks)
        except BingError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            return 503, b""                                  # retried like a 503; the detail says unavailable
        except Exception:  # noqa: BLE001 - a malformed exchange is never a 500 and never quotes the URL (the key)
            raise BingError("BING_PROTOCOL_ERROR", "exchange") from None


def _json(body: bytes, strict: bool = True):
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        if strict:
            raise BingError("BING_PROTOCOL_ERROR", "json") from None
        return None


def _no_constant(name: str):
    raise ValueError("NaN / Infinity are not JSON")
