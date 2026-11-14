"""
Farrah - revenue attribution with a confidence band (farrah-spec/1; CLARIFICATIONS v1.1 GF-1..GF-5).

ONE job: for one tenant on a Growth or Full plan (or ZBM's own tenant, Q-FA-4) and one CLOSED calendar month, state
how much revenue the first-party records attribute to organic search and to AI-answer referrals, as a figure with a
band [lower, upper] and a closed confidence LABEL, with every assumption shown. Farrah reads ONLY Osei's datasets
(``osei.dataset/1``), Osei's reconciliation (``osei.reconciliation/1``) and panel freshness
(``osei.panel_freshness/1``); it never reads a source, never joins people across sources, never claims an incremental
or causal effect (``effect_class: attributed``, "credit is not causation"), never forecasts, prices or invoices, calls
no port and spends nothing.

This module is PURE: constants, the consumer validators of Osei's contracts (CB-19, a frozen copy of Osei's key sets:
Osei full is not on the base, GF-2), the channel mapping, and the arithmetic. Every amount is a ``Decimal`` (ADR 0003,
GF-4): sums are exact (a context wide enough for every bounded amount, with ``Inexact`` trapped, so an inexact sum
raises instead of rounding); the one division (the reconciliation share, B-FA-24) uses the module context of
precision 40 and ROUND_HALF_EVEN; rounding to cents happens only in the display (``display``). No float is accepted
or produced anywhere on the path: a float in an input refuses the document.

The confidence label thresholds (10% / 35% of the upper bound; 30 / 10 revenue rows) are a rule of this service:
``heuristic: true, calibrated: false`` (Q-FA-1). No numeric confidence score is emitted (X-FA-1, B-FA-26).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, DivisionByZero, Inexact, InvalidOperation, Overflow, ROUND_HALF_EVEN
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AGENT = "farrah"
CONTRACT = "farrah.revenue_attribution/1"
RULES_VERSION = "farrah-rules/2026-11-13.1"
AI_REFERRERS_VERSION = "farrah-ai-referrers/2026-11-13.1"
# B-FA-11 / Q-FA-3: closed, versioned, labelled "not complete" (AI answers often send no referrer, R-FA-2)
AI_REFERRERS = ("chatgpt.com", "chat.openai.com", "perplexity.ai", "gemini.google.com", "copilot.microsoft.com",
                "claude.ai")
AI_REFERRERS_COMPLETE = False
CHANNELS = ("organic_search", "ai_referral", "other_known", "unknown")          # B-FA-10, closed
ATTRIBUTED = ("organic_search", "ai_referral")                                  # the channels a figure is stated for
LABELS = ("HIGH", "MEDIUM", "LOW", "INSUFFICIENT_EVIDENCE")                     # B-FA-25, closed
BASES = ("analytics", "crm")                                                    # B-FA-13 / Q-FA-2
THRESHOLDS = {"high_width_share": "0.10", "medium_width_share": "0.35", "high_rows": 30, "medium_rows": 10,
              "heuristic": True, "calibrated": False}
HIGH_WIDTH, MEDIUM_WIDTH = Decimal("0.10"), Decimal("0.35")
HIGH_ROWS, MEDIUM_ROWS = 30, 10
DATA_LAG_DAYS = 3                                                               # B-FA-29: the source's data lag
REFRESH_TIER = "monthly"                                                        # osei-spec/1 B-OS-30
# B-FA-27: listed in every result (closed codes). SESSION_SCOPED_LAST_CLICK only on the analytics basis.
ATTRIBUTED_NOT_INCREMENTAL = "ATTRIBUTED_NOT_INCREMENTAL"
SESSION_SCOPED_LAST_CLICK = "SESSION_SCOPED_LAST_CLICK"
CRM_SOURCE_FIELDS_AS_RECORDED = "CRM_SOURCE_FIELDS_AS_RECORDED"
AI_REFERRERS_LIST_NOT_COMPLETE = "AI_REFERRERS_LIST_NOT_COMPLETE"
AI_ANSWERS_OFTEN_SEND_NO_REFERRER = "AI_ANSWERS_OFTEN_SEND_NO_REFERRER"
UNKNOWN_COUNTED_IN_UPPER_BOUND = "UNKNOWN_COUNTED_IN_UPPER_BOUND"
RECONCILE_DIFFERENCE_WIDENED = "RECONCILE_DIFFERENCE_WIDENED"                   # B-FA-24 (a widening code)
ASSUMPTIONS = (ATTRIBUTED_NOT_INCREMENTAL, SESSION_SCOPED_LAST_CLICK, CRM_SOURCE_FIELDS_AS_RECORDED,
               AI_REFERRERS_LIST_NOT_COMPLETE, AI_ANSWERS_OFTEN_SEND_NO_REFERRER, UNKNOWN_COUNTED_IN_UPPER_BOUND)
# the completeness widenings of B-FA-22 / B-FA-23 are listed as Osei's own label strings (osei-spec/1 B-OS-23:
# ``incomplete:<flag>``); the spec names no other widening code (open question QB-FA-5)
COMPLETENESS_FLAGS = ("sampled", "thresholded", "other_row_share", "rows_dropped_cap")
INCOMPLETE = tuple(f"incomplete:{f}" for f in COMPLETENESS_FLAGS)
RESULT_KEYS = frozenset(("contract", "tenant_id", "run_id", "month", "timezone", "basis", "channels", "totals",
                         "assumptions", "inputs", "data_class", "effect_class", "produced_at", "refresh_tier",
                         "evidence_ref"))
CHANNEL_KEYS = frozenset(("figure", "lower", "upper", "label", "rows"))
TOTALS_KEYS = frozenset(("basis_revenue", "unknown_revenue"))
# the reasons a run states when it cannot state a figure (machine codes, the run's ``limitations``)
NOT_CONNECTED = "NOT_CONNECTED"                     # Osei full is not on the base: no dataset store (GF-2)
MONTH_NOT_COVERED = "MONTH_NOT_COVERED"             # no VALID, recorded basis dataset covers the whole month
BASIS_STATE = "BASIS_INPUT_"                        # + the basis input's freshness state (STALE / FAILED_LAST / ...)
NOTHING_TO_ATTRIBUTE = "NOTHING_TO_ATTRIBUTE"       # U = 0: the figure is null, never 0

# ------------------------------------------------------------------------------------------------ money (ADR 0003)

AMOUNT_DIGITS = 40                                  # significant digits of one amount (GF-4: precision 40)
AMOUNT_EXP = 40                                     # |adjusted exponent| of one amount: below 10^41, above 10^-41
CTX = Context(prec=40, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
# a sum of at most MAX_ROWS amounts, each bounded as above, is exact at this precision; Inexact is trapped, so a sum
# that would round raises (it cannot, by the bounds: a guard, never a silent rounding)
SUM_CTX = Context(prec=2 * AMOUNT_EXP + AMOUNT_DIGITS + 16, rounding=ROUND_HALF_EVEN,
                  traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])
CENT = Decimal("0.01")
_AMOUNT = re.compile(r"[+-]?(?:[0-9]{1,80}(?:\.[0-9]{0,80})?|\.[0-9]{1,80})(?:[eE][+-]?[0-9]{1,3})?")


class InputRefused(ValueError):
    """A consumed document refused whole (CB-19). ``code`` is a closed machine code; never a value."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def amount(value) -> Decimal:
    """One money amount as an exact Decimal. Only a decimal STRING is taken (an int is taken too: exact); a float,
    a bool, NaN / Infinity, more than 40 significant digits or an exponent beyond +-40 refuses the document."""
    if type(value) is int:
        value = str(value)
    if type(value) is not str or len(value) > 96 or not _AMOUNT.fullmatch(value):
        raise InputRefused("AMOUNT_INVALID")
    d = Decimal(value)
    if not d.is_finite():
        raise InputRefused("AMOUNT_INVALID")
    if d.is_zero():
        return Decimal(0)
    t = d.as_tuple()
    if len(t.digits) > AMOUNT_DIGITS or abs(d.adjusted()) > AMOUNT_EXP or t.exponent < -(AMOUNT_EXP + AMOUNT_DIGITS):
        raise InputRefused("AMOUNT_INVALID")
    return d


def exact_sum(values) -> Decimal:
    """The exact sum of Decimals (SUM_CTX: Inexact trapped)."""
    total = Decimal(0)
    for v in values:
        total = SUM_CTX.add(total, v)
    return total


def text(d: Optional[Decimal]) -> Optional[str]:
    """An exact amount as the string a document carries (plain notation, -0 written 0). None stays None."""
    if d is None:
        return None
    if d.is_zero():
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def display(value: Optional[str]) -> Optional[str]:
    """The DISPLAY of an amount: rounded half-even to cents (only here, B-FA-20 section 3)."""
    if value is None:
        return None
    return str(Decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN, context=Context(prec=200)))


# ------------------------------------------------------------------------------------------------ months and time

_MONTH = re.compile(r"(20[0-9]{2})-(0[1-9]|1[0-2])")
_DATE = re.compile(r"([0-9]{4})-?([0-9]{2})-?([0-9]{2})")
_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?"
                       r"(?:Z|[+-][0-9]{2}:[0-9]{2})?")


def month_bounds(month: str) -> tuple[date, date]:
    """(first day, last day) of ``YYYY-MM``. Raises ``InputRefused('MONTH_INVALID')``."""
    if type(month) is not str or not _MONTH.fullmatch(month):
        raise InputRefused("MONTH_INVALID")
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def month_closed(month: str, now: datetime, tz: str = "UTC") -> bool:
    """B-FA-29: CLOSED when the month's last day plus the source's data lag (3 days) is before ``now`` (the injected
    clock), both read in the dataset's timezone: today >= last day + 4 days."""
    _first, last = month_bounds(month)
    today = now.astimezone(zone(tz)).date()
    return today > last + timedelta(days=DATA_LAG_DAYS)


def zone(name) -> ZoneInfo:
    if type(name) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]{0,30}(?:/[A-Za-z0-9_+-]{1,30}){0,2}",
                                                 name):
        raise InputRefused("TIMEZONE_INVALID")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputRefused("TIMEZONE_INVALID") from None


def parse_date(value) -> date:
    m = _DATE.fullmatch(value) if type(value) is str else None
    if m is None or (len(value) == 10) != (value.count("-") == 2):
        raise InputRefused("DATE_INVALID")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise InputRefused("DATE_INVALID") from None


def local_day(value, tz: ZoneInfo) -> date:
    """The calendar day of a row in the DATASET's timezone (A-FA-02): a date (``YYYY-MM-DD`` / ``YYYYMMDD``) is
    already local; a date-time with an offset is converted into the dataset's timezone; one without is local."""
    if type(value) is not str:
        raise InputRefused("DATE_INVALID")
    if "T" not in value:
        return parse_date(value)
    if len(value) > 40 or not _DATETIME.fullmatch(value):
        raise InputRefused("DATE_INVALID")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InputRefused("DATE_INVALID") from None
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(tz).date()


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _norm(value) -> Optional[str]:
    """A-FA-04: the exact normalised value (case and surrounding whitespace only; no confusable fold, no NFKC)."""
    if value is None:
        return None
    if type(value) is not str:
        raise InputRefused("ROW_INVALID")
    return value.strip().lower()


Verifier = Callable[[str, dict, str], bool]


# ------------------------------------------------------------------------------------------------ Osei's contracts
# A FROZEN COPY of osei-spec/1's key sets (B-OS-11, B-OS-13, B-OS-23, B-OS-45; v1.1 GO-2 analytics fields, B-OS-19 CRM
# columns): Osei full is not on the base (GF-2). tests/conformance/data/osei_dataset_v1.json holds the same sets; a
# test compares the two. When Osei lands, its own constants replace these (CB-19).

OSEI_DATASET = "osei.dataset/1"
OSEI_RECONCILIATION = "osei.reconciliation/1"
OSEI_FRESHNESS = "osei.panel_freshness/1"
DATASET_KEYS = frozenset(("contract", "tenant_id", "dataset_id", "source", "read_id", "window", "grain", "surface",
                          "validation", "completeness", "totals", "rows", "produced_at", "refresh_tier",
                          "evidence_ref", "data_class", "labels"))
WINDOW_KEYS = frozenset(("start", "end", "timezone"))
SURFACE_KEYS = frozenset(("fingerprint", "parts"))
VALIDATION_KEYS = frozenset(("state", "reason", "counts"))
COMPLETENESS_KEYS = frozenset(COMPLETENESS_FLAGS)
EVIDENCE_REF_KEYS = frozenset(("run_id", "contract", "item_key"))
DROPPED_KEYS = frozenset(("rows", "revenue"))
SOURCES = ("search_console", "bing_webmaster", "analytics", "crm")
RECORD_KIND = {"analytics": "source_read_recorded", "crm": "crm_import_recorded"}
QUARANTINE_KIND = {"analytics": "source_read_quarantined", "crm": "crm_import_quarantined"}
RECORD_KEYS = frozenset(("kind", "tenant_id", "dataset_id", "read_id", "validation_state", "dataset_hmac", "at"))
# rows: the GO-2 analytics allowlist (dimensions + metrics) and the B-OS-19 CRM columns (open question QB-FA-1: the
# row keys of osei.dataset/1 are not fixed by osei-spec/1 beyond these allowlists)
ANALYTICS_DIMENSIONS = ("date", "sessionSource", "sessionMedium", "sessionDefaultChannelGroup", "landingPage")
ANALYTICS_METRICS = ("sessions", "keyEvents", "purchaseRevenue", "totalRevenue")
ANALYTICS_ROW_KEYS = frozenset(ANALYTICS_DIMENSIONS + ANALYTICS_METRICS)
CRM_ROW_KEYS = frozenset(("deal_ref", "created_date", "closed_date", "stage", "amount", "currency", "lead_source",
                          "utm_source", "utm_medium", "utm_campaign", "landing_path"))
CRM_STAGES = ("open", "won", "lost")
REVENUE_FIELD = {"analytics": "purchaseRevenue", "crm": "amount"}
DAY_FIELD = {"analytics": "date", "crm": "closed_date"}
OTHER_ROW = "(other)"                               # GA4's documented "other" row (B-OS-23, B-FA-22)
MAX_ROWS = 50_000                                   # Osei's per-read row cap (B-OS-18): a larger dataset is refused
FRESHNESS_STATES = ("NEVER", "NOT_CONNECTED", "FRESH", "DUE", "STALE", "FAILED_LAST")   # osei-spec/1 B-OS-31
INSUFFICIENT_STATES = ("STALE", "FAILED_LAST", "NOT_CONNECTED")                        # B-FA-25
RECON_KEYS = frozenset(("contract", "tenant_id", "items", "produced_at"))
RECON_ITEM_KEYS = frozenset(("rule", "datasets", "window", "values", "result", "label", "evidence_ref"))
RC4 = "RC-4"
RC4_VALUES = frozenset(("crm_won_revenue", "analytics_revenue", "difference"))
FRESHNESS_KEYS = frozenset(("contract", "tenant_id", "panels", "produced_at"))
PANEL_KEYS = frozenset(("panel", "state", "freshness_timestamp", "inputs"))
PANEL_INPUT_KEYS = frozenset(("producer", "contract", "run_id", "produced_at", "tier", "state"))
REVENUE_PANEL = "revenue"
_DATASET_ID = re.compile(r"ds-[0-9a-f]{16}")
_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_SHORT = re.compile(r"[A-Za-z0-9_.:/-]{1,64}")


def _strict(obj, keys: frozenset, code: str) -> dict:
    """Exactly ``keys`` (an extra or a missing key, or a non-dict, refuses the document whole)."""
    if type(obj) is not dict or set(obj) != keys:
        raise InputRefused(code)
    return obj


def _short(value, code: str) -> str:
    if type(value) is not str or not _SHORT.fullmatch(value):
        raise InputRefused(code)
    return value


def _count(value, code: str, cap: int = 10 ** 9) -> int:
    if type(value) is not int or not 0 <= value <= cap:
        raise InputRefused(code)
    return value


def _timestamp(value, code: str) -> str:
    if type(value) is not str or not _TS.fullmatch(value):
        raise InputRefused(code)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InputRefused(code) from None
    return value


def _evidence_ref(value, code: str) -> dict:
    ref = _strict(value, EVIDENCE_REF_KEYS, code)
    for k in EVIDENCE_REF_KEYS:
        _short(ref[k], code)
    return ref


def _row(row: dict, source: str) -> dict:
    keys = ANALYTICS_ROW_KEYS if source == "analytics" else CRM_ROW_KEYS
    if type(row) is not dict or not set(row) <= keys or REVENUE_FIELD[source] not in row or DAY_FIELD[source] not in row:
        raise InputRefused("ROW_INVALID")
    for k, v in row.items():
        if k in ("sessions", "keyEvents"):
            if v is not None and (type(v) is not int or not 0 <= v <= 10 ** 9):
                raise InputRefused("ROW_INVALID")
            continue
        if v is not None and type(v) is not str:
            raise InputRefused("ROW_INVALID")               # a float, a bool, a list: never taken
        if type(v) is str and len(v) > 2048:
            raise InputRefused("ROW_INVALID")
    if source == "crm":
        if row.get("stage") not in CRM_STAGES or row.get("currency") not in (None, "USD"):
            raise InputRefused("ROW_INVALID")
        if row["stage"] == "won" and row.get("closed_date") is None:
            raise InputRefused("ROW_INVALID")
    return row


def validate_dataset(doc, tid: str, source: str) -> dict:
    """CB-19 / B-FA-04: an ``osei.dataset/1`` document of THIS tenant and source, VALID, with strict keys and every
    amount an exact decimal string. Returns the document; refuses it WHOLE otherwise (never half-used)."""
    d = _strict(doc, DATASET_KEYS, "DATASET_INVALID")
    if d["contract"] != OSEI_DATASET:
        raise InputRefused("DATASET_INVALID")
    if d["tenant_id"] != tid:
        raise InputRefused("DATASET_TENANT_MISMATCH")
    if d["source"] != source or type(d["dataset_id"]) is not str or not _DATASET_ID.fullmatch(d["dataset_id"]):
        raise InputRefused("DATASET_INVALID")
    _short(d["read_id"], "DATASET_INVALID")
    w = _strict(d["window"], WINDOW_KEYS, "DATASET_INVALID")
    zone(w["timezone"])
    if parse_date(w["start"]) > parse_date(w["end"]):
        raise InputRefused("DATASET_INVALID")
    s = _strict(d["surface"], SURFACE_KEYS, "DATASET_INVALID")
    if type(s["parts"]) is not dict or len(s["parts"]) > 50:
        raise InputRefused("DATASET_INVALID")
    _short(s["fingerprint"], "DATASET_INVALID")
    v = _strict(d["validation"], VALIDATION_KEYS, "DATASET_INVALID")
    if v["state"] != "VALID":
        raise InputRefused("DATASET_NOT_VALID")
    if v["reason"] is not None:
        _short(v["reason"], "DATASET_INVALID")
    if type(v["counts"]) is not dict or len(v["counts"]) > 50:
        raise InputRefused("DATASET_INVALID")
    for k, n in v["counts"].items():
        _short(k, "DATASET_INVALID")
        _count(n, "DATASET_INVALID")
    c = _strict(d["completeness"], COMPLETENESS_KEYS, "DATASET_INVALID")
    if type(c["sampled"]) is not bool or type(c["thresholded"]) is not bool:
        raise InputRefused("DATASET_INVALID")
    if c["other_row_share"] is not None:
        share = amount(c["other_row_share"])
        if not Decimal(0) <= share <= Decimal(1):
            raise InputRefused("DATASET_INVALID")
    dropped = c["rows_dropped_cap"]
    if type(dropped) is dict:
        _strict(dropped, DROPPED_KEYS, "DATASET_INVALID")
        _count(dropped["rows"], "DATASET_INVALID")
        if dropped["revenue"] is not None:
            amount(dropped["revenue"])
    else:
        _count(dropped, "DATASET_INVALID")
    if type(d["totals"]) is not dict or not set(d["totals"]) <= set(ANALYTICS_METRICS) | {"amount"}:
        raise InputRefused("DATASET_INVALID")
    for k, t in d["totals"].items():
        if t is not None and k in ("purchaseRevenue", "totalRevenue", "amount"):
            amount(t)
    rows = d["rows"]
    if type(rows) is not list or len(rows) > MAX_ROWS:
        raise InputRefused("DATASET_INVALID")
    for r in rows:
        _row(r, source)
    _timestamp(d["produced_at"], "DATASET_INVALID")
    _evidence_ref(d["evidence_ref"], "DATASET_INVALID")
    if d["data_class"] != "measured" or type(d["labels"]) is not list or \
            not all(type(x) is str and _SHORT.fullmatch(x) for x in d["labels"]):
        raise InputRefused("DATASET_INVALID")
    _short(d["grain"], "DATASET_INVALID")
    _short(d["refresh_tier"], "DATASET_INVALID")
    return d


def recorded(doc: dict, records, tid: str, source: str, verify: Verifier) -> bool:
    """B-FA-04 / AC-11: the dataset is RECORDED by Osei: a ``source_read_recorded`` (``crm_import_recorded`` for an
    import) record of this tenant naming this dataset, state VALID, whose keyed HMAC matches the document (Osei's
    W-OS-2 seal field ``dataset``), and no quarantine record of the same read."""
    if type(records) is not list:
        return False
    hit = False
    for r in records:
        if type(r) is not dict or set(r) != RECORD_KEYS or r.get("tenant_id") != tid:
            continue
        if r["kind"] == QUARANTINE_KIND[source] and r.get("read_id") == doc["read_id"]:
            return False                                     # a dataset of a quarantined read: never used
        if r["kind"] == RECORD_KIND[source] and r.get("dataset_id") == doc["dataset_id"] \
                and r.get("read_id") == doc["read_id"] and r.get("validation_state") == "VALID" \
                and type(r.get("dataset_hmac")) is str and verify(tid, doc, r["dataset_hmac"]):
            hit = True
    return hit


def covers(doc: dict, month: str) -> bool:
    """The dataset's window covers the WHOLE month in the dataset's own timezone (B-FA-04), and that timezone is
    the property's own (a dataset whose window is read in another timezone than its property: not covered,
    A-FA-02)."""
    first, last = month_bounds(month)
    w = doc["window"]
    tz = ((doc["surface"].get("parts") or {}).get("property_settings") or {})
    tz = tz.get("timezone") if isinstance(tz, dict) else None
    if tz is not None and tz != w["timezone"]:
        return False
    return parse_date(w["start"]) <= first and parse_date(w["end"]) >= last


def validate_reconciliation(doc, tid: str) -> dict:
    d = _strict(doc, RECON_KEYS, "RECONCILIATION_INVALID")
    if d["contract"] != OSEI_RECONCILIATION:
        raise InputRefused("RECONCILIATION_INVALID")
    if d["tenant_id"] != tid:
        raise InputRefused("RECONCILIATION_TENANT_MISMATCH")
    _timestamp(d["produced_at"], "RECONCILIATION_INVALID")
    if type(d["items"]) is not list or len(d["items"]) > 1000:
        raise InputRefused("RECONCILIATION_INVALID")
    for it in d["items"]:
        _strict(it, RECON_ITEM_KEYS, "RECONCILIATION_INVALID")
        _short(it["rule"], "RECONCILIATION_INVALID")
        _short(it["result"], "RECONCILIATION_INVALID")
        if type(it["datasets"]) is not list or len(it["datasets"]) != 2 or \
                not all(type(x) is str and _DATASET_ID.fullmatch(x) for x in it["datasets"]):
            raise InputRefused("RECONCILIATION_INVALID")
        w = _strict(it["window"], WINDOW_KEYS, "RECONCILIATION_INVALID")
        zone(w["timezone"])
        parse_date(w["start"]), parse_date(w["end"])
        _evidence_ref(it["evidence_ref"], "RECONCILIATION_INVALID")
        if type(it["values"]) is not dict:
            raise InputRefused("RECONCILIATION_INVALID")
        if it["rule"] == RC4 and not it["result"].startswith("NOT_COMPARABLE"):
            _strict(it["values"], RC4_VALUES, "RECONCILIATION_INVALID")
            for k in RC4_VALUES:
                amount(it["values"][k])
    return d


def validate_freshness(doc, tid: str) -> dict:
    d = _strict(doc, FRESHNESS_KEYS, "FRESHNESS_INVALID")
    if d["contract"] != OSEI_FRESHNESS:
        raise InputRefused("FRESHNESS_INVALID")
    if d["tenant_id"] != tid:
        raise InputRefused("FRESHNESS_TENANT_MISMATCH")
    _timestamp(d["produced_at"], "FRESHNESS_INVALID")
    if type(d["panels"]) is not list or len(d["panels"]) > 100:
        raise InputRefused("FRESHNESS_INVALID")
    for p in d["panels"]:
        _strict(p, PANEL_KEYS, "FRESHNESS_INVALID")
        if p["state"] not in FRESHNESS_STATES or type(p["inputs"]) is not list or len(p["inputs"]) > 100:
            raise InputRefused("FRESHNESS_INVALID")
        _short(p["panel"], "FRESHNESS_INVALID")
        for i in p["inputs"]:
            _strict(i, PANEL_INPUT_KEYS, "FRESHNESS_INVALID")
            if i["state"] not in FRESHNESS_STATES:
                raise InputRefused("FRESHNESS_INVALID")
            _short(i["producer"], "FRESHNESS_INVALID")
    return d


def basis_state(freshness: Optional[dict], basis: str) -> Optional[str]:
    """The basis source's freshness state on the ``revenue`` panel (osei-spec/1 B-OS-31), or None when Osei states
    none (then the label cannot be HIGH: the input is not shown CONNECTED and fresh)."""
    if freshness is None:
        return None
    for p in freshness["panels"]:
        if p["panel"] == REVENUE_PANEL:
            for i in p["inputs"]:
                if i["producer"] == basis:
                    return i["state"]
    return None


# ------------------------------------------------------------------------------------------------ channel mapping

def analytics_channel(row: dict) -> str:
    """B-FA-11, in this order: organic medium (or the source's organic search channel group) -> organic_search; a
    session source host EXACTLY on the closed AI-referrer list -> ai_referral; ``(not set)``, unassigned, or direct
    with no landing data -> unknown; everything else -> other_known (lookalike hosts included, A-FA-04)."""
    medium, source = _norm(row.get("sessionMedium")), _norm(row.get("sessionSource"))
    group, landing = _norm(row.get("sessionDefaultChannelGroup")), _norm(row.get("landingPage"))
    if medium == "organic" or group == "organic search":
        return "organic_search"
    if source in AI_REFERRERS:
        return "ai_referral"
    if medium is None and source is None and group is None:
        return "unknown"                                    # no channel field at all: (not set)
    if "(not set)" in (medium, source, group) or group == "unassigned":
        return "unknown"
    direct = source == "(direct)" or medium == "(none)" or group == "direct"
    if direct and landing in (None, "", "(not set)"):
        return "unknown"
    return "other_known"


def crm_channel(row: dict, mapping: dict) -> str:
    """B-FA-12: ``utm_medium`` / ``utm_source`` first (organic medium -> organic_search, a source host on the
    AI-referrer list -> ai_referral, any other recorded utm value -> other_known); without utm values, ``lead_source``
    through the tenant's declared closed mapping; a value outside the mapping -> unknown (never guessed)."""
    medium, source = _norm(row.get("utm_medium")), _norm(row.get("utm_source"))
    if medium or source:
        if medium == "organic":
            return "organic_search"
        if source in AI_REFERRERS:
            return "ai_referral"
        return "other_known"
    lead = _norm(row.get("lead_source"))
    if lead:
        return mapping.get(lead, "unknown")
    return "unknown"


def normalised_map(channel_map: Optional[dict]) -> dict:
    """The tenant's declared lead_source mapping as {normalised value: channel}; a value declared under two channels
    maps to neither (``unknown``: never guessed)."""
    out: dict = {}
    clash: set = set()
    for ch in ("organic_search", "ai_referral", "other_known"):
        for v in (channel_map or {}).get(ch) or ():
            k = _norm(v)
            if k in out and out[k] != ch:
                clash.add(k)
            out[k] = ch
    return {k: v for k, v in out.items() if k not in clash}


def is_other_row(row: dict) -> bool:
    return any(row.get(k) == OTHER_ROW for k in ANALYTICS_DIMENSIONS if k != "date")


# ------------------------------------------------------------------------------------------------ the plan gate

ALLOWED_PLANS = ("growth", "full")                  # pricing ruling: not in Entry, included from Growth up


def plan_allows(tid: str, plan_name: Optional[str], own_tenant: str) -> bool:
    """B-FA-02 / Q-FA-4: Growth and Full only; Entry (or no plan) never; ZBM's own tenant regardless of plan."""
    return tid == own_tenant or plan_name in ALLOWED_PLANS


def plan_blocked_by(tid: str, plan_name: Optional[str], own_tenant: str) -> Optional[str]:
    """What Osei's refresh plan lists for Farrah's panel (osei-spec/1 B-OS-34): ``PLAN_EXCLUDES`` or nothing."""
    return None if plan_allows(tid, plan_name, own_tenant) else "PLAN_EXCLUDES"


# ------------------------------------------------------------------------------------------------ selection

def _in_window(doc: dict, source: str) -> None:
    """Every row's day lies inside the dataset's window, in its timezone (Osei quarantines otherwise, B-OS-20; the
    consumer refuses such a document whole)."""
    tz = zone(doc["window"]["timezone"])
    start, end = parse_date(doc["window"]["start"]), parse_date(doc["window"]["end"])
    for r in doc["rows"]:
        v = r.get(DAY_FIELD[source])
        if v is None:
            continue
        if not start <= local_day(v, tz) <= end:
            raise InputRefused("DATASET_INVALID")
        if r.get(REVENUE_FIELD[source]) is not None:
            amount(r[REVENUE_FIELD[source]])


def select(tid: str, month: str, basis: str, view, verify: Verifier) -> dict:
    """The inputs of one run: the latest VALID, recorded basis dataset of this tenant whose window covers the whole
    month (B-FA-04), Osei's freshness state of the basis input, and the RC-4 reconciliation item comparing that
    dataset with a VALID, recorded dataset of the other source for exactly this month (B-FA-24). Every document that
    fails is refused whole and counted by code; nothing of it is used."""
    other = "crm" if basis == "analytics" else "analytics"
    refused: dict = {}

    def refuse(code: str) -> None:
        refused[code] = refused.get(code, 0) + 1
    month_bounds(month)
    datasets = getattr(view, "datasets", None)
    records = getattr(view, "records", None)
    valid: dict = {basis: [], other: []}
    for doc in datasets if type(datasets) is list else []:
        src = doc.get("source") if type(doc) is dict else None
        if src not in (basis, other):
            refuse("DATASET_INVALID") if src not in SOURCES else None
            continue
        try:
            validate_dataset(doc, tid, src)
            _in_window(doc, src)
        except InputRefused as e:
            refuse(e.code)
            continue
        if not recorded(doc, records, tid, src, verify):
            refuse("DATASET_NOT_RECORDED")
            continue
        valid[src].append(doc)
    covering = [d for d in valid[basis] if covers(d, month)]
    chosen = max(covering, key=lambda d: (d["produced_at"], d["dataset_id"])) if covering else None
    freshness = None
    if getattr(view, "freshness", None) is not None:
        try:
            freshness = validate_freshness(view.freshness, tid)
        except InputRefused as e:
            refuse(e.code)
    recon, other_id = None, None
    if chosen is not None and getattr(view, "reconciliation", None) is not None:
        try:
            rec = validate_reconciliation(view.reconciliation, tid)
        except InputRefused as e:
            refuse(e.code)
            rec = None
        others = {d["dataset_id"]: d for d in valid[other]}
        first, last = month_bounds(month)
        best = None
        for it in (rec or {}).get("items", []):
            if it["rule"] != RC4 or it["result"].startswith("NOT_COMPARABLE"):
                continue
            ids = it["datasets"]
            if chosen["dataset_id"] not in ids:
                continue
            oid = ids[1] if ids[0] == chosen["dataset_id"] else ids[0]
            w = it["window"]
            if oid not in others or w["timezone"] != chosen["window"]["timezone"] or \
                    parse_date(w["start"]) != first or parse_date(w["end"]) != last:
                refuse("RECONCILIATION_NOT_FOR_MONTH")
                continue
            d = abs(amount(it["values"]["difference"]))
            if best is None or d > best[0]:
                best = (d, oid)
        if best is not None:
            recon, other_id = best
    return {"basis": basis, "dataset": chosen, "covering": len(covering), "valid": len(valid[basis]),
            "refused": dict(sorted(refused.items())), "state": basis_state(freshness, basis),
            "reconcile_difference": recon, "other_dataset_id": other_id}


def signature(sel: dict) -> dict:
    """What a run was computed from, as machine values (a month whose inputs changed gets a new run, B-FA-29)."""
    ds = sel["dataset"]
    return {"basis": sel["basis"], "dataset_id": ds["dataset_id"] if ds else None, "state": sel["state"],
            "other_dataset_id": sel["other_dataset_id"], "reconcile_difference": text(sel["reconcile_difference"])}


# ------------------------------------------------------------------------------------------------ the figure and band

def _label(lower: Decimal, upper: Decimal, rows: int, flagged: bool, force_low: bool, state: Optional[str]) -> str:
    """B-FA-25 (thresholds heuristic, uncalibrated: Q-FA-1)."""
    width = SUM_CTX.subtract(upper, lower)
    if not force_low and not flagged and state == "FRESH" and rows >= HIGH_ROWS \
            and width <= SUM_CTX.multiply(HIGH_WIDTH, upper):
        return "HIGH"
    if not force_low and rows >= MEDIUM_ROWS and width <= SUM_CTX.multiply(MEDIUM_WIDTH, upper):
        return "MEDIUM"
    return "LOW"


def compute(tid: str, run_id: str, month: str, sel: dict, channel_map: Optional[dict], produced_at: str,
            connected: bool = True) -> tuple[dict, dict]:
    """The ``farrah.revenue_attribution/1`` document and the run's machine facts (no amount in the facts)."""
    basis, doc, state = sel["basis"], sel["dataset"], sel["state"]
    reasons: list = []
    if not connected:
        reasons.append(NOT_CONNECTED)
    elif doc is None:
        reasons.append(MONTH_NOT_COVERED)
    if doc is not None and state in INSUFFICIENT_STATES:
        reasons.append(BASIS_STATE + state)
    channels: dict = {}
    sums = {c: Decimal(0) for c in CHANNELS}
    rows = {c: 0 for c in CHANNELS}
    refunds = {c: 0 for c in CHANNELS}
    assumptions = [ATTRIBUTED_NOT_INCREMENTAL] + ([SESSION_SCOPED_LAST_CLICK] if basis == "analytics" else []) + [
        CRM_SOURCE_FIELDS_AS_RECORDED, AI_REFERRERS_LIST_NOT_COMPLETE, AI_ANSWERS_OFTEN_SEND_NO_REFERRER,
        UNKNOWN_COUNTED_IN_UPPER_BOUND]
    totals = {"basis_revenue": None, "unknown_revenue": None}
    inputs: list = []
    tzname = None
    if doc is not None:
        tzname = doc["window"]["timezone"]
        tz = zone(tzname)
        mapping = normalised_map(channel_map)
        other_rows = Decimal(0)
        has_other_row = False
        field, day = REVENUE_FIELD[basis], DAY_FIELD[basis]
        parts: dict = {c: [] for c in CHANNELS}
        others: list = []
        for r in doc["rows"]:
            if basis == "crm" and r["stage"] != "won":
                continue
            if r.get(field) is None or r.get(day) is None or month_of(local_day(r[day], tz)) != month:
                continue
            v = amount(r[field])
            c = analytics_channel(r) if basis == "analytics" else crm_channel(r, mapping)
            if basis == "analytics" and is_other_row(r):
                has_other_row = True
                others.append(v)
            parts[c].append(v)
            if not v.is_zero():
                rows[c] += 1
            if v < 0:
                refunds[c] += 1
        for c in CHANNELS:
            sums[c] = exact_sum(parts[c])
        other_rows = exact_sum(others)
        basis_total = exact_sum(sums[c] for c in CHANNELS)
        r_unknown = sums["unknown"]
        totals = {"basis_revenue": text(basis_total), "unknown_revenue": text(r_unknown)}
        comp = doc["completeness"]
        widen = Decimal(0)
        applied: list = []
        flagged = any(x.startswith("incomplete:") for x in doc["labels"])
        force_low = False
        # B-FA-22: the documented other / thresholded share
        if has_other_row:
            widen = SUM_CTX.add(widen, max(other_rows, Decimal(0)))
            applied.append("incomplete:other_row_share")
        elif comp["other_row_share"] is not None and amount(comp["other_row_share"]) > 0:
            tot = doc["totals"].get(field)
            base = amount(tot) if tot is not None else basis_total
            widen = SUM_CTX.add(widen, max(SUM_CTX.multiply(amount(comp["other_row_share"]), base), Decimal(0)))
            applied.append("incomplete:other_row_share")
        if comp["thresholded"]:
            applied.append("incomplete:thresholded")
        if comp["sampled"]:
            # GO-2's sampling metadata carries no bounds: the bounds are never "given" in v1, so the label is LOW
            force_low = True
            applied.append("incomplete:sampled")
        # B-FA-23: rows dropped by Osei's caps
        dropped = comp["rows_dropped_cap"]
        n_dropped = dropped["rows"] if type(dropped) is dict else dropped
        if n_dropped:
            rev = dropped.get("revenue") if type(dropped) is dict else None
            if rev is None:
                force_low = True
            else:
                widen = SUM_CTX.add(widen, max(amount(rev), Decimal(0)))
            applied.append("incomplete:rows_dropped_cap")
        flagged = flagged or bool(applied)
        inputs = [doc["dataset_id"]]
        recon = sel["reconcile_difference"]
        recon_used = False
        for c in ATTRIBUTED:
            lower = sums[c]
            upper = SUM_CTX.add(SUM_CTX.add(lower, r_unknown), widen)
            if recon is not None and not recon.is_zero() and not basis_total.is_zero():
                # B-FA-24: U += |D| x (F(c) / total basis revenue): the one division, precision 40, half-even
                term = CTX.divide(SUM_CTX.multiply(recon, lower), basis_total)
                if term > 0:
                    upper = SUM_CTX.add(upper, term)
                    recon_used = True
            if reasons or upper.is_zero():
                label = "INSUFFICIENT_EVIDENCE"
                channels[c] = {"figure": None, "lower": None, "upper": None, "label": label, "rows": rows[c]}
                continue
            label = _label(lower, upper, rows[c], flagged, force_low, state)
            channels[c] = {"figure": text(lower), "lower": text(lower), "upper": text(upper), "label": label,
                           "rows": rows[c]}
        assumptions += [a for a in INCOMPLETE if a in applied]
        if recon_used:
            assumptions.append(RECONCILE_DIFFERENCE_WIDENED)
            inputs.append(sel["other_dataset_id"])
    else:
        for c in ATTRIBUTED:
            channels[c] = {"figure": None, "lower": None, "upper": None, "label": "INSUFFICIENT_EVIDENCE", "rows": 0}
    if doc is not None and all(channels[c]["label"] == "INSUFFICIENT_EVIDENCE" for c in ATTRIBUTED) \
            and not reasons:
        reasons.append(NOTHING_TO_ATTRIBUTE)
    result = {"contract": CONTRACT, "tenant_id": tid, "run_id": run_id, "month": month, "timezone": tzname,
              "basis": basis, "channels": channels, "totals": totals, "assumptions": assumptions, "inputs": inputs,
              "data_class": "measured", "effect_class": "attributed", "produced_at": produced_at,
              "refresh_tier": REFRESH_TIER, "evidence_ref": {"run_id": run_id, "contract": CONTRACT,
                                                             "item_key": "channels"}}
    labels = {c: channels[c]["label"] for c in ATTRIBUTED}
    if not connected:
        outcome = "NOT_CONNECTED"
    elif all(v == "INSUFFICIENT_EVIDENCE" for v in labels.values()):
        outcome = "INSUFFICIENT_EVIDENCE"
    elif any(v == "INSUFFICIENT_EVIDENCE" for v in labels.values()):
        outcome = "PARTIAL"
    else:
        outcome = "OK"
    facts = {"outcome": outcome, "labels": labels, "rows": {c: rows[c] for c in ATTRIBUTED},
             "unknown_rows": rows["unknown"], "refund_rows": {c: refunds[c] for c in CHANNELS},
             "assumptions": list(assumptions), "inputs": list(inputs), "limitations": reasons,
             "inputs_refused": dict(sel.get("refused") or {})}
    return result, facts


def validate_result(doc) -> bool:
    """B-FA-28: the strict key set of ``farrah.revenue_attribution/1`` (used by the tests and by a consumer)."""
    try:
        _strict(doc, RESULT_KEYS, "RESULT_INVALID")
        if doc["contract"] != CONTRACT or set(doc["channels"]) != set(ATTRIBUTED):
            return False
        for c in ATTRIBUTED:
            _strict(doc["channels"][c], CHANNEL_KEYS, "RESULT_INVALID")
            if doc["channels"][c]["label"] not in LABELS:
                return False
        _strict(doc["totals"], TOTALS_KEYS, "RESULT_INVALID")
        _evidence_ref(doc["evidence_ref"], "RESULT_INVALID")
        return doc["data_class"] == "measured" and doc["effect_class"] == "attributed" \
            and doc["refresh_tier"] == REFRESH_TIER and all(
                a in ASSUMPTIONS + INCOMPLETE + (RECONCILE_DIFFERENCE_WIDENED,) for a in doc["assumptions"])
    except InputRefused:
        return False
