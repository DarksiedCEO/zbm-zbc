"""
Chrome UX Report (CrUX) adapter for Lena, the real-user page-experience agent (ADR 0050). Field data only: what real
Chrome users experienced on a public origin or URL over a rolling 28-day window, as Google aggregates it. This module
never runs a lab test, never loads the audited site, and never writes anything to Google.

Vendor independence: Lena reads FIELD DATA through a small provider interface (``open_read(guard)`` -> a read with
``record(kind, target, form_factor)`` and ``history(kind, target, form_factor, periods)``, both answering the neutral
shape documented on ``parse_record`` / ``parse_history``). ``CruxClient`` is the only provider built; another field
data source would be a second class with the same two methods. ``NotConnectedFieldData`` is the port without a
provider: it never sends anything and never invents data.

Facts this module relies on, read on Google's pages on Nov 8 2026 (WebFetch; URLs and page dates in ADR 0050):

  record    POST https://chromeuxreport.googleapis.com/v1/records:queryRecord (developer.chrome.com/docs/crux/api,
            page updated 2026-10-13). Body: ``origin`` OR ``url`` (a union field), optional ``formFactor`` (DESKTOP,
            PHONE, TABLET; omitted = all form factors aggregated), optional ``metrics`` (omitted = all). Answer
            ``{"record": {"key": {formFactor, origin|url}, "metrics": {<name>: {"histogram": [{start, end, density}],
            "percentiles": {"p75": ...}}}, "collectionPeriod": {"firstDate": {year, month, day}, "lastDate": ...}},
            "urlNormalizationDetails": {originalUrl, normalizedUrl}}``; the last bin has no ``end``; CLS values are
            strings (a double encoded as a string, two decimals).
  history   POST https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord (developer.chrome.com/docs/crux/
            history-api, page updated 2025-04-11). Same key fields plus ``collectionPeriodCount`` 1..40, default 25.
            Answer: ``record.metrics.<name>.histogramTimeseries`` [{start, end, densities[]}] and
            ``percentilesTimeseries.p75s[]``; ``record.collectionPeriods`` [{firstDate, lastDate}] ascending; a period
            without data is ``"NaN"`` in densities and ``null`` in p75s. Updated each Monday ~04:00 UTC.
  metrics   largest_contentful_paint, interaction_to_next_paint, cumulative_layout_shift, first_contentful_paint,
            experimental_time_to_first_byte (and others Lena does not ask for). ``first_input_delay`` is retired:
            "the CrUX API and CrUX History API will stop serving data for the first_input_delay metric" (web.dev/blog/
            fid, Sep 10 2024); Lena never asks for it.
  not found HTTP 404 ``{"error": {"code": 404, "message": "chrome ux report data not found", "status":
            "NOT_FOUND"}}`` — the origin or URL is not publicly discoverable or has too few samples
            (developer.chrome.com/docs/crux/guides/crux-api). Lena reports that as INSUFFICIENT_FIELD_DATA: no data,
            never "fast" and never "slow".
  quota     150 queries per minute per Google Cloud project, shared by both APIs, free, cannot be raised by paying.
  data      the daily dataset is a 28-day rolling window, published ~04:00 UTC, about two days behind.
  key       the CrUX pages show ``?key=<API key>`` in the URL. Google's system-parameters page
            (docs.cloud.google.com/apis/docs/system-parameters, updated 2026-10-26) documents the same API key as the
            HTTP header ``X-Goog-Api-Key``. This adapter sends the key ONLY in that header, so the key never appears in
            a URL, a log line, an error or anything recorded. That the CrUX endpoint honours the header is UNVERIFIED
            against the live API (fixtures only in this build): if it does not, every read is CRUX_AUTH_REFUSED, never
            a fallback to the query parameter.

Rules here (the Search Console connector's, gsc_client.py):
  - a fixed host allowlist (``ALLOWED_HOSTS``) checked on every URL before anything is sent and again at connect;
    https on 443 only; no redirect is followed; every resolved address must be globally routable;
  - one hard deadline per read over DNS, connect, TLS, every socket read and write (finance_client._DeadlineBackend)
    and the body loop; the run's kill-switch guard before every request and every socket operation;
  - retries only on connect errors, timeouts, 429 and 500/502/503/504, bounded by ``attempts`` and the deadline;
    a call cap per read (``max_calls``, retries included); client-side pacing to at most PACE_MAX_PER_MINUTE (100)
    requests a minute across the process (a token bucket), under Google's 150 per project;
  - strict parsing of every documented REQUIRED field: an answer without them, or with them malformed, is
    CRUX_PROTOCOL_ERROR, never data; unknown extra fields are accepted and ignored (at most MAX_EXTRA_FIELDS per
    object) and counted (``extra_fields_ignored``) — the go-live canary call must show 0, or the new fields are read
    on Google's page before any budget is raised; one malformed
    metric is marked MALFORMED and dropped, the metrics around it are kept;
  - the API key lives in ``ApiKey`` (masked repr, never pickled or copied) and leaves it only as the header value.

Reason codes: CRUX_NOT_CONNECTED, CRUX_AUTH_REFUSED, CRUX_RATE_LIMITED, CRUX_UNAVAILABLE, CRUX_PROTOCOL_ERROR,
CRUX_BUDGET_EXHAUSTED, CRUX_KILLED. INSUFFICIENT_FIELD_DATA is a data state (Google answered: no data), not an error.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation

import httpcore
import httpx

from finance_client import _DeadlineBackend, _DnsGate, _Resolver, _system_resolve
from gsc_client import _GlobalResolver, _GuardedStream
from primitives import Killed

PROVIDER = "chrome_ux_report"
SOURCE = "Google Chrome UX Report (CrUX) API"
API_BASE = "https://chromeuxreport.googleapis.com/v1"
RECORD_URL = API_BASE + "/records:queryRecord"
HISTORY_URL = API_BASE + "/records:queryHistoryRecord"
ALLOWED_HOSTS = frozenset({"chromeuxreport.googleapis.com"})
KEY_HEADER = "X-Goog-Api-Key"
DOCS = {
    "api": "https://developer.chrome.com/docs/crux/api (updated 2026-10-13)",
    "history_api": "https://developer.chrome.com/docs/crux/history-api (updated 2025-04-11)",
    "not_found": "https://developer.chrome.com/docs/crux/guides/crux-api",
    "fid_retired": "https://web.dev/blog/fid (2024-09-10)",
    "api_key_header": "https://docs.cloud.google.com/apis/docs/system-parameters (updated 2026-10-26)",
}
API_VERSION = "v1"
# the metrics Lena asks for: CrUX API name -> short name. first_input_delay is retired (web.dev/blog/fid).
METRICS = {"largest_contentful_paint": "LCP", "interaction_to_next_paint": "INP", "cumulative_layout_shift": "CLS",
           "first_contentful_paint": "FCP", "experimental_time_to_first_byte": "TTFB"}
RETIRED_METRICS = ("first_input_delay",)
STRING_VALUED = frozenset({"cumulative_layout_shift"})            # CrUX encodes CLS as strings
FORM_FACTORS = ("PHONE", "DESKTOP", "TABLET")
KINDS = ("origin", "url")
HISTORY_PERIODS_MAX = 40                                           # Google: collectionPeriodCount 1..40
MAX_BODY = 1024 * 1024
MAX_BINS = 10
MAX_METRICS_IN_ANSWER = 64                                         # unknown metric names are ignored, but bounded
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
AUTH_STATUSES = frozenset({"PERMISSION_DENIED", "UNAUTHENTICATED"})
AUTH_REASONS = frozenset({"API_KEY_INVALID", "SERVICE_DISABLED", "API_KEY_SERVICE_BLOCKED",
                          "API_KEY_HTTP_REFERRER_BLOCKED", "API_KEY_IP_ADDRESS_BLOCKED",
                          "API_KEY_ANDROID_APP_BLOCKED", "API_KEY_IOS_APP_BLOCKED", "CONSUMER_SUSPENDED"})
RATE_REASONS = frozenset({"RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"})
# Fields Google may add later: an object with unknown fields is accepted and they are ignored (never read, never
# passed on), at most MAX_EXTRA_FIELDS per object, while every documented required field is strictly validated. The
# count is returned with each answer (``extra_fields_ignored``) so the go-live canary call shows any drift at once.
MAX_EXTRA_FIELDS = 16
DENSITY_TOLERANCE = 0.011              # densities are rounded (history: 4 decimals); a histogram sums to 1 within this

CODES = ("CRUX_NOT_CONNECTED", "CRUX_AUTH_REFUSED", "CRUX_RATE_LIMITED", "CRUX_UNAVAILABLE", "CRUX_PROTOCOL_ERROR",
         "CRUX_BUDGET_EXHAUSTED", "CRUX_KILLED")
# a read that ends with one of these cannot usefully continue: the rest of the run's queries are not sent
STOPPING = frozenset({"CRUX_AUTH_REFUSED", "CRUX_RATE_LIMITED", "CRUX_UNAVAILABLE", "CRUX_BUDGET_EXHAUSTED",
                      "CRUX_KILLED", "CRUX_NOT_CONNECTED"})

ORIGIN = re.compile(r"https?://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_KEY = re.compile(r"[A-Za-z0-9_-]{20,128}")
_TOKEN = re.compile(r"[A-Za-z0-9_.]{1,60}")
_INT_MAX = 10 ** 9


class CruxError(Exception):
    """A query that produced no data. ``code`` is one of ``CODES``; ``detail`` a short closed-vocabulary hint (never
    Google's text, a URL or the key)."""

    def __init__(self, code: str, detail: str | None = None):
        if code not in CODES:
            raise AssertionError(f"not a CrUX reason code: {code}")
        super().__init__(code)
        self.code, self.detail = code, detail


class ApiKey:
    """The CrUX API key: masked repr, never pickled or copied; it leaves only through ``reveal`` into one header."""

    __slots__ = ("_v",)

    def __init__(self, value: str):
        if not isinstance(value, str) or not _KEY.fullmatch(value):
            raise ValueError("a Google API key is 20..128 letters, digits, '-' or '_'")
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "ApiKey(***)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("an API key is never pickled")

    def __copy__(self):
        raise TypeError("an API key is never copied")

    def __deepcopy__(self, memo):
        raise TypeError("an API key is never copied")


class NotConnectedFieldData:
    """The field-data port without a provider (the default): never a request, never invented data."""

    connected = False
    provider = None
    requests_made = 0

    def open_read(self, guard):
        raise CruxError("CRUX_NOT_CONNECTED", "no_provider")


# ------------------------------------------------------------------------------------------------ parsing

def _finite(v, lo: float = 0.0, hi: float = 1e9) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    if isinstance(v, int) and abs(v) > _INT_MAX:
        return False
    try:
        return math.isfinite(v) and lo <= v <= hi
    except (OverflowError, ValueError):
        return False


def _value(metric: str, v):
    """A bin edge or a percentile -> a float, or None when malformed. CLS is a decimal string; the others are whole
    or fractional milliseconds (JSON numbers). A string where a number is documented (or the reverse) is malformed."""
    if metric in STRING_VALUED:
        if not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,6}(\.[0-9]{1,6})?", v):
            return None
        try:
            return float(Decimal(v))
        except (InvalidOperation, ValueError):
            return None
    if not _finite(v, 0.0, 1e9):
        return None
    return float(v)


def _fits(d, required: frozenset | set, optional: frozenset | set = frozenset(), ig: list | None = None) -> bool:
    """``d`` is an object holding every ``required`` field; fields neither required nor ``optional`` are ignored, at
    most MAX_EXTRA_FIELDS of them (counted into ``ig[0]``)."""
    if not isinstance(d, dict) or not set(required) <= set(d):
        return False
    extra = len(set(d) - set(required) - set(optional))
    if extra > MAX_EXTRA_FIELDS:
        return False
    if ig is not None:
        ig[0] += extra
    return True


_YMD = frozenset({"year", "month", "day"})
_FL = frozenset({"firstDate", "lastDate"})


def _date(d, ig: list | None = None) -> str | None:
    if not _fits(d, _YMD, ig=ig):
        return None
    y, m, dd = d["year"], d["month"], d["day"]
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (y, m, dd)):
        return None
    try:
        out = date(y, m, dd)
    except ValueError:
        return None
    return out.isoformat() if 2015 <= out.year <= 2100 else None


def _period(p, ig: list | None = None) -> dict | None:
    if not _fits(p, _FL, ig=ig):
        return None
    f, last = _date(p["firstDate"], ig), _date(p["lastDate"], ig)
    if f is None or last is None or f > last:
        return None
    return {"first": f, "last": last}


def _bins(metric: str, raw, key: str, n: int | None = None, ig: list | None = None) -> list | None:
    """A histogram (record: ``density``; history: ``densities`` of length ``n``) -> [{start, end, density(ies)}], or
    None when malformed: 1..MAX_BINS bins, the first starting at 0, contiguous, ascending, only the last open-ended,
    densities in [0, 1] (history: or "NaN" for a period without data)."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_BINS:
        return None
    out, prev_end = [], 0.0
    for i, b in enumerate(raw):
        last = i == len(raw) - 1
        if not _fits(b, {"start", key}, {"end"}, ig):
            return None
        start = _value(metric, b["start"])
        end = _value(metric, b["end"]) if "end" in b else None
        if start is None or start != prev_end or ("end" in b and (end is None or end <= start)) \
                or (not last and "end" not in b):
            return None
        if n is None:
            d = b[key]
            if not _finite(d, 0.0, 1.0):
                return None
        else:
            ds = b[key]
            if not isinstance(ds, list) or len(ds) != n:
                return None
            d = []
            for x in ds:
                if x == "NaN":
                    d.append(None)
                elif _finite(x, 0.0, 1.0):
                    d.append(float(x))
                else:
                    return None
        out.append({"start": start, "end": end, ("density" if n is None else "densities"): d})
        prev_end = end if end is not None else prev_end
    return out


def _density_sum_ok(densities: list) -> bool:
    return abs(sum(densities) - 1.0) <= DENSITY_TOLERANCE


def _error_obj(raw: bytes):
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return obj.get("error") if isinstance(obj, dict) and isinstance(obj.get("error"), dict) else None


def is_not_found(raw: bytes) -> bool:
    """Google's documented "no data" answer: HTTP 404 with ``error.status`` NOT_FOUND and ``error.code`` 404."""
    e = _error_obj(raw)
    return e is not None and e.get("status") == "NOT_FOUND" and e.get("code") == 404


def _google_reason(raw: bytes) -> tuple[str | None, str | None]:
    """(status, reason) from a Google error body, each only when it is a plain closed-shape token."""
    e = _error_obj(raw)
    if e is None:
        return None, None
    status = e.get("status") if isinstance(e.get("status"), str) and _TOKEN.fullmatch(e["status"]) else None
    reason = None
    details = e.get("details")
    if isinstance(details, list):
        for d in details[:10]:
            r = d.get("reason") if isinstance(d, dict) else None
            if isinstance(r, str) and _TOKEN.fullmatch(r):
                reason = r
                break
    return status, reason


def _check_key(rec: dict, kind: str, target: str, form_factor: str, norm: dict | None,
               ig: list | None = None) -> str | None:
    """The record's key must be the one asked for. A URL may come back normalised (``urlNormalizationDetails``):
    accepted only when the original is what was asked and the normalised URL is on the same origin. Returns the URL
    Google reports for (None for an origin)."""
    k = rec.get("key")
    other = "url" if kind == "origin" else "origin"
    # the key's own fields are strict (the one asked for, never both); fields Google adds beside them are ignored
    if not _fits(k, {"formFactor", kind}, ig=ig) or other in k or k.get("formFactor") != form_factor:
        raise CruxError("CRUX_PROTOCOL_ERROR", "record_key")
    if kind == "origin":
        if k["origin"] != target:
            raise CruxError("CRUX_PROTOCOL_ERROR", "record_key")
        return None
    got = k.get("url")
    if not isinstance(got, str) or len(got) > 2048:
        raise CruxError("CRUX_PROTOCOL_ERROR", "record_key")
    if got == target:
        return got
    if not isinstance(norm, dict) or norm.get("originalUrl") != target or norm.get("normalizedUrl") != got \
            or origin_of(got) != origin_of(target):
        raise CruxError("CRUX_PROTOCOL_ERROR", "record_key")
    return got


def origin_of(url: str) -> str | None:
    m = re.match(r"(https?://[^/?#]+)", url or "")
    return m.group(1) if m else None


def parse_record(obj, kind: str, target: str, form_factor: str) -> dict:
    """queryRecord's 200 answer -> ``{"state": "OK", "kind", "target", "reported_url", "form_factor",
    "collection_period": {"first", "last"}, "metrics": {name: {"state": "OK", "p75", "histogram"} | {"state":
    "MALFORMED"|"ABSENT"}}}``. Record-level shape errors are CRUX_PROTOCOL_ERROR; a malformed metric is MALFORMED."""
    ig = [0]
    if not _fits(obj, {"record"}, {"urlNormalizationDetails"}, ig):
        raise CruxError("CRUX_PROTOCOL_ERROR", "answer_shape")
    rec = obj["record"]
    if not _fits(rec, {"key", "metrics", "collectionPeriod"}, ig=ig):
        raise CruxError("CRUX_PROTOCOL_ERROR", "record_shape")
    reported = _check_key(rec, kind, target, form_factor, obj.get("urlNormalizationDetails"), ig)
    period = _period(rec.get("collectionPeriod"), ig)
    if period is None:
        raise CruxError("CRUX_PROTOCOL_ERROR", "collection_period")
    ms = rec["metrics"]
    if not isinstance(ms, dict) or len(ms) > MAX_METRICS_IN_ANSWER:
        raise CruxError("CRUX_PROTOCOL_ERROR", "metrics_shape")
    metrics = {}
    for name in METRICS:
        if name not in ms:
            metrics[name] = {"state": "ABSENT"}
            continue
        metrics[name] = _record_metric(name, ms[name], ig)
    return {"state": "OK", "kind": kind, "target": target, "reported_url": reported, "form_factor": form_factor,
            "collection_period": period, "metrics": metrics, "extra_fields_ignored": ig[0]}


def _record_metric(name: str, m, ig: list | None = None) -> dict:
    bad = {"state": "MALFORMED"}
    if not _fits(m, {"percentiles"}, {"histogram"}, ig):
        return bad
    pc = m["percentiles"]
    if not _fits(pc, {"p75"}, ig=ig):
        return bad
    p75 = _value(name, pc["p75"])
    if p75 is None:
        return bad
    hist = None
    if "histogram" in m:
        hist = _bins(name, m["histogram"], "density", ig=ig)
        if hist is None or not _density_sum_ok([b["density"] for b in hist]):
            return bad
    return {"state": "OK", "p75": p75, "histogram": hist}


def parse_history(obj, kind: str, target: str, form_factor: str, periods: int) -> dict:
    """queryHistoryRecord's 200 answer -> ``{"state": "OK", ..., "collection_periods": [{"first", "last"}],
    "metrics": {name: {"state": "OK", "p75s": [float|None], "histogram": [{start, end, densities}] | None} | ...}}``.
    Periods must be ascending and at most the number asked for; every series has one value per period."""
    ig = [0]
    if not _fits(obj, {"record"}, {"urlNormalizationDetails"}, ig):
        raise CruxError("CRUX_PROTOCOL_ERROR", "answer_shape")
    rec = obj["record"]
    if not _fits(rec, {"key", "metrics", "collectionPeriods"}, ig=ig):
        raise CruxError("CRUX_PROTOCOL_ERROR", "record_shape")
    reported = _check_key(rec, kind, target, form_factor, obj.get("urlNormalizationDetails"), ig)
    cps = rec["collectionPeriods"]
    if not isinstance(cps, list) or not 1 <= len(cps) <= periods:
        raise CruxError("CRUX_PROTOCOL_ERROR", "collection_periods")
    plist = [_period(p, ig) for p in cps]
    if any(p is None for p in plist) or any(a["last"] >= b["last"] for a, b in zip(plist, plist[1:])):
        raise CruxError("CRUX_PROTOCOL_ERROR", "collection_periods")
    n = len(plist)
    ms = rec["metrics"]
    if not isinstance(ms, dict) or len(ms) > MAX_METRICS_IN_ANSWER:
        raise CruxError("CRUX_PROTOCOL_ERROR", "metrics_shape")
    metrics = {}
    for name in METRICS:
        metrics[name] = {"state": "ABSENT"} if name not in ms else _history_metric(name, ms[name], n, ig)
    return {"state": "OK", "kind": kind, "target": target, "reported_url": reported, "form_factor": form_factor,
            "collection_periods": plist, "metrics": metrics, "extra_fields_ignored": ig[0]}


def _history_metric(name: str, m, n: int, ig: list | None = None) -> dict:
    bad = {"state": "MALFORMED"}
    if not _fits(m, {"percentilesTimeseries"}, {"histogramTimeseries"}, ig):
        return bad
    pt = m["percentilesTimeseries"]
    if not _fits(pt, {"p75s"}, ig=ig) or not isinstance(pt["p75s"], list) or len(pt["p75s"]) != n:
        return bad
    p75s = []
    for v in pt["p75s"]:
        if v is None:
            p75s.append(None)
            continue
        x = _value(name, v)
        if x is None:
            return bad
        p75s.append(x)
    hist = None
    if "histogramTimeseries" in m:
        hist = _bins(name, m["histogramTimeseries"], "densities", n, ig)
        if hist is None:
            return bad
        for i in range(n):
            col = [b["densities"][i] for b in hist]
            if all(c is None for c in col):
                continue
            if any(c is None for c in col) or not _density_sum_ok(col):
                return bad
    return {"state": "OK", "p75s": p75s, "histogram": hist}


# ------------------------------------------------------------------------------------------------ transport

def check_url(url: str) -> httpx.URL:
    """The egress allowlist: https, ``chromeuxreport.googleapis.com``, port 443, the two documented methods only, no
    query string (the key never travels in the URL), no credentials, no fragment."""
    try:
        u = httpx.URL(url)
    except Exception:  # noqa: BLE001
        u = None
    if u is None:
        raise _detached(CruxError("CRUX_PROTOCOL_ERROR", "url"))
    if u.scheme != "https" or u.host not in ALLOWED_HOSTS or u.port not in (None, 443) or u.userinfo or u.fragment \
            or u.query or str(u) not in (RECORD_URL, HISTORY_URL):
        raise CruxError("CRUX_PROTOCOL_ERROR", "host_not_allowed")
    return u


class _GuardedBackend(httpcore.NetworkBackend):
    def __init__(self, inner: httpcore.NetworkBackend, guard: Callable[[], None]):
        self._b, self._g = inner, guard

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self._g()
        if host not in ALLOWED_HOSTS:                       # defence in depth: the URL check came first
            raise httpcore.ConnectError("host not on the CrUX allowlist")
        return _GuardedStream(self._b.connect_tcp(host, port, timeout, local_address, socket_options), self._g)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are never used")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_CRUX_DNS_GATE = _DnsGate(2)

# Client-side pacing (review M2), under Google's 150 queries per minute per project: a token bucket of PACE_BURST
# tokens refilled at PACE_REFILL_PER_MINUTE, so any 60-second window holds at most PACE_BURST + PACE_REFILL_PER_MINUTE
# = PACE_MAX_PER_MINUTE requests (retries included), across every read of the process.
PACE_BURST = 10
PACE_REFILL_PER_MINUTE = 90
PACE_MAX_PER_MINUTE = PACE_BURST + PACE_REFILL_PER_MINUTE


class _Pacer:
    """The token bucket. ``take`` reserves one token and answers how long to wait for it (0 when one is there); a
    reservation that will not be used is given back with ``refund``. Thread-safe; time is the injected monotonic
    clock."""

    def __init__(self, monotonic: Callable[[], float], burst: int = PACE_BURST,
                 refill_per_minute: int = PACE_REFILL_PER_MINUTE):
        import threading
        self._mono, self.burst, self.rate = monotonic, float(burst), refill_per_minute / 60.0
        self._tokens, self._t = float(burst), monotonic()
        self._lock = threading.Lock()

    def take(self) -> float:
        with self._lock:
            now = self._mono()
            self._tokens = min(self.burst, self._tokens + (now - self._t) * self.rate)
            self._t = now
            self._tokens -= 1.0
            return 0.0 if self._tokens >= 0 else -self._tokens / self.rate

    def refund(self) -> None:
        with self._lock:
            self._tokens = min(self.burst, self._tokens + 1.0)


class _Retry(Exception):
    def __init__(self, code: str, detail: str, retry_after: float | None = None):
        super().__init__(code)
        self.code, self.detail, self.retry_after = code, detail, retry_after


class CruxClient:
    """The CrUX provider. One ``open_read`` per Lena run: one deadline and one call cap for the whole run."""

    connected = True
    provider = PROVIDER

    def __init__(self, key: ApiKey, *, timeout_s: float = 10.0, deadline_s: float = 60.0, attempts: int = 3,
                 backoff_s: tuple = (1.0, 3.0), max_calls: int = 0,
                 transport: httpx.BaseTransport | None = None, network_backend=None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 resolve: Callable[[str, int], list] = _system_resolve, dns_gate: _DnsGate | None = None):
        if not isinstance(key, ApiKey):
            raise TypeError("CruxClient needs an ApiKey")
        if not 1 <= attempts <= 5 or max_calls < 0:
            raise ValueError("CruxClient bounds out of range")
        self._key = key
        self.timeout_s, self.deadline_s = float(timeout_s), float(deadline_s)
        self.attempts, self.backoff_s, self.max_calls = attempts, tuple(backoff_s), max_calls
        self._transport = transport                  # tests only: replaces the deadline-bound production transport
        self._inner_backend = network_backend        # tests only: the socket layer under the deadline backend
        self._mono, self._sleep = monotonic, sleep
        self._resolver = _GlobalResolver(_Resolver(resolve, dns_gate if dns_gate is not None else _CRUX_DNS_GATE))
        self._pacer = _Pacer(monotonic)
        self.requests_made = 0

    def __repr__(self) -> str:
        return "CruxClient(key=***)"

    def open_read(self, guard: Callable[[], None], max_calls: int | None = None) -> "_Read":
        cap = self.max_calls if max_calls is None else min(self.max_calls, max_calls)
        return _Read(self, guard, self._mono() + self.deadline_s, cap)


class _Read:
    def __init__(self, client: CruxClient, guard: Callable[[], None], end: float, max_calls: int):
        self.c, self.end, self.max_calls = client, end, max_calls
        self.requests = 0
        self._guard_fn = guard

    def _guard(self) -> None:
        try:
            self._guard_fn()
            return
        except Killed as k:
            code = k.code if re.fullmatch(r"[A-Z_]{1,40}", k.code or "") else "killed"
        raise _detached(CruxError("CRUX_KILLED", code))

    def left(self) -> float:
        return self.end - self.c._mono()

    @staticmethod
    def _body(kind: str, target: str, form_factor: str, periods: int | None = None) -> bytes:
        if kind not in KINDS or form_factor not in FORM_FACTORS:
            raise AssertionError("a key kind or form factor this adapter never asks for")
        b = {kind: target, "formFactor": form_factor, "metrics": list(METRICS)}
        if periods is not None:
            if not 1 <= periods <= HISTORY_PERIODS_MAX:
                raise AssertionError("collectionPeriodCount outside 1..40")
            b["collectionPeriodCount"] = periods
        return json.dumps(b, separators=(",", ":")).encode()

    def record(self, kind: str, target: str, form_factor: str) -> dict:
        status, raw = self._send(RECORD_URL, self._body(kind, target, form_factor))
        if status == 404:
            return {"state": "INSUFFICIENT_FIELD_DATA", "kind": kind, "target": target, "form_factor": form_factor}
        return parse_record(_json(raw), kind, target, form_factor)

    def history(self, kind: str, target: str, form_factor: str, periods: int) -> dict:
        status, raw = self._send(HISTORY_URL, self._body(kind, target, form_factor, periods))
        if status == 404:
            return {"state": "INSUFFICIENT_FIELD_DATA", "kind": kind, "target": target, "form_factor": form_factor}
        return parse_history(_json(raw), kind, target, form_factor, periods)

    def _send(self, url: str, body: bytes) -> tuple[int, bytes]:
        """One logical query with bounded retries. Returns (200, body) or (404, body) for Google's documented
        not-found answer; raises CruxError for everything else."""
        check_url(url)
        last: _Retry | None = None
        for attempt in range(self.c.attempts):
            if self.left() <= 0:
                break
            if self.requests >= self.max_calls:
                raise CruxError("CRUX_BUDGET_EXHAUSTED", "calls_per_run")
            wait = self.c._pacer.take()
            if wait > 0:
                if self.c._mono() + wait >= self.end:
                    self.c._pacer.refund()
                    raise CruxError("CRUX_RATE_LIMITED", "client_pacing")
                self._guard()
                self.c._sleep(wait)
            self._guard()
            self.requests += 1
            self.c.requests_made += 1
            try:
                status, raw, hdrs = self._once(url, body)
                if status == 200:
                    return status, raw
                if status == 404:
                    if is_not_found(raw):
                        return status, raw
                    raise CruxError("CRUX_PROTOCOL_ERROR", "http_404")
                self._classify(status, raw, hdrs)
            except _Retry as r:
                last = r
            if attempt + 1 < self.c.attempts:
                pause = self.c.backoff_s[min(attempt, len(self.c.backoff_s) - 1)] if self.c.backoff_s else 0.0
                if last is not None and last.retry_after is not None:
                    pause = max(pause, last.retry_after)
                if self.c._mono() + pause >= self.end:
                    break
                self._guard()
                self.c._sleep(pause)
                self._guard()
        if last is None:
            raise _detached(CruxError("CRUX_UNAVAILABLE", "deadline"))
        raise _detached(CruxError(last.code, last.detail))

    @staticmethod
    def _classify(status: int, raw: bytes, hdrs: dict) -> None:
        ra = _retry_after(hdrs.get("retry-after"))
        gstatus, reason = _google_reason(raw)
        if status == 429:
            raise _Retry("CRUX_RATE_LIMITED", "http_429", ra)
        if status in RETRY_STATUSES:
            raise _Retry("CRUX_UNAVAILABLE", f"http_{status}", ra)
        if status in (400, 401, 403):
            if reason in AUTH_REASONS:
                raise CruxError("CRUX_AUTH_REFUSED", reason)
            if reason in RATE_REASONS or gstatus == "RESOURCE_EXHAUSTED":
                raise CruxError("CRUX_RATE_LIMITED", reason or "resource_exhausted")
            if status in (401, 403) or gstatus in AUTH_STATUSES:
                raise CruxError("CRUX_AUTH_REFUSED", gstatus if gstatus in AUTH_STATUSES else f"http_{status}")
        raise CruxError("CRUX_PROTOCOL_ERROR", f"http_{status}")

    def _once(self, url: str, body: bytes) -> tuple[int, bytes, dict]:
        left = self.left()
        if left <= 0:
            raise _Retry("CRUX_UNAVAILABLE", "deadline")
        transport = self.c._transport
        if transport is None:
            transport = httpx.HTTPTransport(trust_env=False, retries=0)
            pool = getattr(transport, "_pool", None)
            if pool is None or not hasattr(pool, "_network_backend"):
                raise CruxError("CRUX_NOT_CONNECTED", "no_deadline_backend")   # never call Google without it
            pool._network_backend = _GuardedBackend(
                _DeadlineBackend(self.c._mono, self.end, inner=self.c._inner_backend, resolver=self.c._resolver),
                self._guard)
        hdrs = {KEY_HEADER: self.c._key.reveal(), "Content-Type": "application/json", "Accept": "application/json",
                "Accept-Encoding": "identity", "User-Agent": "zbm-seo-py-crux/1"}
        try:
            with httpx.Client(timeout=httpx.Timeout(min(self.c.timeout_s, left)), transport=transport,
                              follow_redirects=False, trust_env=False) as client, \
                    client.stream("POST", url, headers=hdrs, content=body) as resp:
                enc = resp.headers.get("content-encoding", "identity").strip().lower()
                if enc not in ("identity", ""):
                    raise CruxError("CRUX_PROTOCOL_ERROR", "content_encoding")
                declared = resp.headers.get("content-length")
                if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
                    raise CruxError("CRUX_PROTOCOL_ERROR", "body_too_large")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    if self.c._mono() >= self.end:
                        raise _Retry("CRUX_UNAVAILABLE", "deadline")
                    self._guard()
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise CruxError("CRUX_PROTOCOL_ERROR", "body_too_large")
                    chunks.append(chunk)
                keep = {k: resp.headers.get(k) for k in ("retry-after",) if resp.headers.get(k) is not None}
                return resp.status_code, b"".join(chunks), keep
        except (CruxError, _Retry) as ours:
            failure: Exception = ours
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            failure = _Retry("CRUX_UNAVAILABLE", "network")
        except Exception:  # noqa: BLE001 - anything else is a malformed exchange, never a 500 and never a message
            failure = CruxError("CRUX_PROTOCOL_ERROR", "exchange")
        # raised OUTSIDE the except blocks, detached: an httpx exception carries the request (and so the key header);
        # it must never ride along as __context__ / __cause__ into a log, a traceback print or a caller (review L3)
        raise _detached(failure)


def _retry_after(v: str | None) -> float | None:
    if v is None or not re.fullmatch(r"[0-9]{1,4}", v.strip()):
        return None
    return float(min(int(v.strip()), 60))


def _detached(exc: Exception) -> Exception:
    """``exc`` with no chained exception: nothing from the failed exchange (an httpx request with its headers, a
    response, Google's text) stays reachable from what this module raises."""
    exc.__cause__ = None
    exc.__context__ = None
    exc.__suppress_context__ = True
    return exc


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        pass
    raise _detached(CruxError("CRUX_PROTOCOL_ERROR", "json"))
