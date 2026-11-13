"""
Priya — the keyword agent (ADR 0047). ONE task: produce the client's keyword ledger from first-party data. She does
not crawl, rank, write content, buy data or score anything; she runs no model.

The ledger has two tiers:

  ESTABLISHED  queries with real measured demand ON THIS PROPERTY: at least ``ESTABLISHED_MIN_IMPRESSIONS``
               impressions in one source's window (Search Console, or Bing Webmaster Tools when wired). Each source's
               numbers are kept apart, with the source, the date range and an exact (Garwood) Poisson band on the
               count. The tier is decided on Search Console's count when Search Console measured the query.
  EMERGING     queries with no reliable volume yet: (a) OBSERVED long-tail rows below the threshold, their thin
               counts kept as measured but flagged ``BELOW_THRESHOLD``; queries only Bing measured (Bing's rows cover
               their own multi-week dates, not Search Console's window, so they are never tiered against the
               threshold: ``MEASURED_NOT_TIERED``, flag ``BING_ONLY_UNTIERED``); (b) DERIVED variants — conversational,
               question and constraint frames seen on the site's own long tail, transferred deterministically to the
               site's established head terms — with NO volume at all (``UNKNOWN_NOT_MEASURED``), never an estimate.

What a number in the ledger is, and is not:
  - impressions / clicks / CTR / position are what the engine reported for THIS property in the stated window. They
    are not market search volume; ``market_volume`` is always null (no paid vendor; Keyword Planner not built);
  - ``opportunity`` is a CTR gap against the property's OWN position curve (impression-weighted CTR per position
    bucket over its own established, non-brand Search Console queries in the same window, the row itself left out);
    a bucket with too little support is INCONCLUSIVE, never filled from a published industry curve. The gap carries
    a 95% interval from the row's Wilson score CTR interval; a row under the threshold, or a gap interval that
    includes 0, is INCONCLUSIVE. It is ``modeled`` data, not traffic;
  - ``difficulty`` is null: no owned data supports an Ahrefs-style keyword difficulty, and none is invented.

Personal data: a query that looks like personal data (``search_performance.classify_query`` -> ``pii_suspect``:
e-mails, phone-like digit runs, id- or token-like words, address-shaped text, PO boxes, ZIP+4, a final ZIP after a
state; the rules and their gaps are in ``search_performance``) never enters
the ledger; only how many distinct queries there were and their summed counts. Derived variants are checked the same
way, and a frame is built only from a query with no capitalised word whose added words are all in a closed vocabulary
(``FRAME_VOCAB``): personal names cannot be reliably detected, so a word that could be one is never transferred.

The input is plain data; this module touches no service state, ledger, log or network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata

import lookalikes
from agents import search_performance as sp
from envelope import observed

AGENT, TASK = "priya", "keyword_ledger"
VERSION = "2026-11-08.2"                       # tiers, thresholds, curve, near-duplicate and frame rules below
# .2 (review round 1): Wilson CTR interval and gap interval, thin rows INCONCLUSIVE; leave-one-out curve; exact
#    Garwood band; tier from Search Console, Bing-only rows untiered; frame vocabulary; distinct PII query count
LEDGER_SCHEMA = "priya-keyword-ledger/1"
HANDOFF_SCHEMA = "priya-emerging-handoff/1"
TIERS = ("ESTABLISHED", "EMERGING")
ORIGINS = ("observed", "derived")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW", "NONE")
VOLUME_STATUS = ("MEASURED_ON_THIS_PROPERTY", "BELOW_THRESHOLD", "MEASURED_NOT_TIERED", "UNKNOWN_NOT_MEASURED")
EMERGING_KINDS = ("question", "conversational", "constraint", "long_tail")
FLAGS = (
    "BELOW_THRESHOLD",            # measured, but fewer impressions than the established threshold
    "LOW_IMPRESSION_COUNT",       # established, but under MEDIUM_MIN impressions: the Poisson band is wide
    "SINGLE_SOURCE",              # only one engine measured it (always true until Bing is wired)
    "GSC_ROWS_TRUNCATED",         # the Search Console read stopped at a row cap: the long tail past it is unread
    "ANONYMISED_REMAINDER",       # Search Console's totals exceed its query rows: some demand is hidden from the rows
    "NOT_MEASURED",               # a derived variant: no engine has measured it
    "NEAR_DUPLICATE",             # another ledger row is the same query up to word order, plurals and stop words
    "BING_WINDOW_DIFFERS",        # Bing's rows cover their own dates, not Search Console's window
    "BING_ONLY_UNTIERED",         # only Bing measured it: its summed weekly rows are not compared with the threshold
)
STATUSES = ("OK", "PARTIAL", "EMPTY_MEASURED", "INCONCLUSIVE")

ESTABLISHED_MIN_IMPRESSIONS = 20               # founder-pending default (ADR 0047): unmeasured against a real client
MEDIUM_MIN, HIGH_MIN = 100, 1000               # impressions for MEDIUM / HIGH confidence
Z95 = 1.959963984540054                        # the two-sided 95% normal quantile (Wilson; Wilson-Hilferty)
GARWOOD_EXACT_MAX = 1000                       # exact incomplete-gamma bounds up to this count; Wilson-Hilferty above
CURVE_MIN_QUERIES, CURVE_MIN_IMPRESSIONS = 5, 200   # support a position bucket needs before it is used
BUCKETS = (("1", 1.5), ("2", 2.5), ("3", 3.5), ("4-5", 5.5), ("6-10", 10.5), ("11-20", 20.5), ("21+", math.inf))
HEADS_CAP, FRAMES_CAP, VARIANTS_CAP = 25, 20, 200
FRAME_MIN_HEADS = 2                            # a frame must be seen around at least this many distinct heads
FRAME_MAX_EXTRA_WORDS = 6
ROWS_CAP = 1000                                # rows in one ledger; past it the fewest-impression rows are counted
STOP_WORDS = frozenset({"a", "an", "the", "of", "for", "in", "on", "to", "and", "or", "my", "your", "with"})
_CONSTRAINT = re.compile(r"\b(near me|nearby|open now|under|below|without|with|for|vs|versus|cheap|cheapest|best|"
                         r"top|affordable|free|same day|24/7|emergency|price|cost)\b")
_LOOKALIKE = lookalikes.Table()
PII_CLASS = "pii_suspect"
# The only words a frame may add around a head (conservative, closed): stop words, question words, the constraint and
# commercial words above, and a short list of generic verbs and modifiers. A frame with any other word is not used,
# because that word could be a name, a place or an identifier that no rule here can classify.
FRAME_VOCAB = STOP_WORDS | frozenset({
    "who", "what", "when", "where", "why", "how", "which", "can", "could", "does", "do", "is", "are", "should",
    "will", "would", "much", "many", "long", "often", "near", "me", "nearby", "open", "now", "under", "below",
    "without", "vs", "versus", "cheap", "cheapest", "best", "top", "affordable", "free", "same", "day", "emergency",
    "price", "prices", "pricing", "cost", "costs", "buy", "review", "reviews", "deal", "deals", "quote", "quotes",
    "hire", "service", "services", "company", "companies", "local", "professional", "choose", "find", "get", "clean",
    "cleaning", "fix", "repair", "install", "installation", "replace", "replacement", "use", "make", "need", "rent",
    "rental", "diy", "tips", "guide", "ideas", "types", "kit", "it", "worth", "good", "bad", "new", "used",
    "small", "large", "commercial", "residential", "home", "house", "online", "i", "you"})

CARD = {
    "agent": AGENT, "job": "produce the client's keyword ledger (one task)",
    "inputs": [
        ("Search Console Search Analytics for the tenant's bound property: property totals by date and query rows "
        "(dataState final, type web, aggregated by property), through gsc_client"),
        ("Bing Webmaster Tools GetQueryStats for one of the tenant's registered domains (adapter built, fixtures "
        "only; NOT_CONNECTED until wired)"),
        "the tenant's brand tokens (registered domains, canonical entity name)",
    ],
    "outputs": [
        (f"{LEDGER_SCHEMA}: ESTABLISHED and EMERGING rows, each with query, tier, per-source metrics with source and "
        "date range, confidence, uncertainty flags and an evidence pointer"),
        (f"{HANDOFF_SCHEMA}: the EMERGING tier for Theo (conversation mining) and Naomi (prompt volume); contract "
        "only, neither consumer is built"),
    ],
    "does_not": ["crawl", "rank", "estimate market volume", "compute keyword difficulty", "call a model",
                 "buy data", "store query text"],
}

LIMITS = [
    ("Impressions, clicks, CTR and position are what the engine reported for this property; they are not market "
    "search volume, which is not measured (no paid vendor; Google Ads Keyword Planner not built)."),
    ("Search Console returns top rows only and omits anonymised queries; the anonymised share is reported, never "
    "spread over the rows."),
    ("Impressions are the engine's full count for this property in the window, not a sample. The 95% band on them "
    "(exact Garwood Poisson interval) says how far such a count could move from one window to the next if the rate "
    "behind it stayed the same; it is a model of period-to-period variability, not sampling error, and not a "
    "forecast."),
    "Position is an average over the window as the engine reports it.",
    ("The CTR curve is this property's own, from the same window; a row's expected CTR leaves the row out, and a "
    "bucket without enough support is INCONCLUSIVE."),
    ("A CTR gap carries a 95% interval from the row's Wilson score CTR interval against the expected CTR (the curve's "
    "own uncertainty is not added, so the true uncertainty is larger). A row under the established threshold, or a "
    "gap whose interval includes 0, is INCONCLUSIVE."),
    ("The tier is decided on Search Console's count in its window. Bing's rows cover their own multi-week dates, so a "
    "query only Bing measured is kept as measured but not tiered (MEASURED_NOT_TIERED, BING_ONLY_UNTIERED)."),
    ("Derived EMERGING variants are pattern transfers from the site's own long tail; they have no volume and are not "
    "evidence of demand."),
    ("Personal names cannot be reliably detected in queries: a query naming a person can appear in the ledger as the "
    "engine reported it. Dropped as personal data: e-mails, phone-like digit runs, id- or secret-like words (not "
    "model numbers, tire sizes, decimals or years; base64 / AWS-like words with '/' are), street addresses "
    "(house number, street name and suffix, a unit only after one), PO boxes, PMB numbers, rural routes, ZIP+4, a "
    "state with a ZIP, UK postcodes (written with their space) and Canadian postal codes, French leading forms "
    "('350 rue ...'), and any query over 512 characters; the dropped count is of distinct queries. The address "
    "rule is US-centric. Addresses it does NOT catch, which can appear in the ledger as the "
    "engine reported them: bare named streets with no suffix ('1200 broadway', '123 n main'), landmark or "
    "building-name addresses, streets with more than three name words or an unlisted suffix ('trail', 'pike'), a "
    "state code that is also an English word with its ZIP ('portland or 97201'), and other countries' postal "
    "formats. Secrets it does NOT catch: base64 under 12 characters, and a random password written as a plain "
    "word (for example after 'reset'). Every URL-like query is dropped whole (a host name, label.tld, "
    "a scheme or 'www.', a percent escape, an IP address, or a word with '/' other than a fraction, a tire size or "
    "a pair such as 'a/b'), so no URL ever appears in the ledger, ordinary ones included. Text is percent-decoded "
    "(up to three times) before every check; a Luhn-valid run of 13-19 digits is dropped as a card number, "
    "whatever its groups look like or single filler letters between them (a list of years can be dropped too). "
    "Combining marks, keycaps, underlines and format characters are stripped before every check, and stray ones "
    "on ASCII are removed from the stored key; IPv6, hex IPv4, script schemes (javascript:, data:, file:) and "
    "hosts written 'example . com' or 'example dot com' count as URLs; hex and base64 text split by spaces is "
    "caught. One character normalisation is shared by the classifier and the stored key (controls, private-use "
    "and space variants become spaces), so a split value cannot reappear joined in the ledger; spelled-out digit "
    "runs, URL-safe base64 and cards across short filler tokens are caught. Still not caught: dotted octal or hex "
    "IPv4 ('0300.0250.1.1'), a 'dot' host with an uncommon TLD, base64 of binary data split into short words, an "
    "address spaced out character by character, and obfuscations beyond the tested corpus (every single "
    "obfuscation and every ordered pair of two are tested and dropped). Phones and SSNs across filler tokens, "
    "homoglyph, enclosed and Spanish spelled digits, and spaced or 'dot' IPv4 addresses are caught. Year lists, "
    "calendar dates that stand alone ('2023 2024 honda civic', '11/08/2025 hotel', '2025-11-08/2025-11-09'), "
    "version numbers after their keyword ('firmware 2.3.11.4') and spec or model-number queries ('12v 100ah "
    "lifepo4 battery') are kept; a date with other digits next to it is never exempt: card, phone and SSN "
    "detection always runs on the query as written, with no exemption of any kind, and exemptions only relax the "
    "generic digit rules; separators are read by Unicode category, and words, emoji or CJK around a value never "
    "hide it. Since round 18 the detection is certified against a closed threat list (ADR 0047): digits joined "
    "across any gap of up to three non-digit characters (letters included), digits in any script, zero-width and "
    "bidi characters, spelled digits, vanity toll-free numbers, and e-mails spelled, bracketed, base64 / base32 / "
    "hex / URL-encoded, rot13 or reversed, whole or chunked, inside surrounding text. Over-dropped on purpose: any "
    "query whose digits, read across such gaps, are a valid card, phone or SSN number however they are grouped "
    "('10001 2025', 'version 10.0.19045.3803', a date followed by a time, a tire size with its load index). The "
    "rule also drops some non-addresses shaped like '<number> <word> st' "
    "or '<number> <word> court'. Derived "
    "variants are never built from a query with a capitalised word, nor from a frame adding a word outside a "
    "closed vocabulary."),
    "The ledger is computed on request and handed out once; it is not stored (only its SHA-256 and counts).",
]


# ====================================================================== text

def normalize(q: str) -> str:
    """The ledger's key text: ``search_performance.clean_text`` in its key form — the same character normalisation
    the classifier uses (round 10: NFKC; format characters, default-ignorables and variation selectors deleted;
    controls, private-use and every space variant made a space; stray combining marks on ASCII removed, script marks
    kept) — then case-folded, whitespace collapsed. Unicode is kept (``café``, ``東京 ホテル``)."""
    return " ".join(sp.clean_text(q or "", keep_script_marks=True).casefold().split())


def near_key(norm: str) -> str:
    """Near-duplicate key: the lookalike fold, word characters only, stop words dropped, a plural ``s`` dropped from
    words over three letters (not ``ss``), the words sorted and de-duplicated. ``rug cleaners`` and ``cleaner for
    rugs`` share a key; scripts without spaces keep their whole folded text."""
    folded = _LOOKALIKE.fold_cased(norm)
    words = []
    for w in re.findall(r"\w+", folded):
        if w in STOP_WORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(sorted(set(words))) or folded


def row_key(norm: str) -> str:
    return "kw-" + hashlib.sha256(norm.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def emerging_kinds(norm: str) -> list:
    out = []
    if sp._QUESTION.search(norm):
        out.append("question")
    if len(norm.split()) >= 6:
        out.append("conversational")
    if _CONSTRAINT.search(norm):
        out.append("constraint")
    return out or ["long_tail"]


# ====================================================================== numbers

def _gamma_p(a: float, x: float) -> float:
    """The regularised lower incomplete gamma P(a, x) (series below a + 1, Lentz continued fraction above)."""
    if x <= 0:
        return 0.0
    lg = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1:
        s = t = 1.0 / a
        ap = a
        for _ in range(100_000):
            ap += 1
            t *= x / ap
            s += t
            if t < s * 1e-15:
                break
        return min(1.0, s * math.exp(lg))
    b, c = x + 1 - a, 1e300
    d = 1 / b
    h = d
    for i in range(1, 100_000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = d if abs(d) > 1e-300 else 1e-300
        c = b + an / c
        c = c if abs(c) > 1e-300 else 1e-300
        d = 1 / d
        h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return max(0.0, 1 - math.exp(lg) * h)


def _gamma_quantile(a: float, p: float) -> float:
    """x with P(a, x) = p, by bisection (a unit-scale gamma quantile; half a chi-square quantile with 2a d.f.)."""
    lo, hi = 0.0, a + 20 * math.sqrt(a) + 20
    for _ in range(200):
        m = (lo + hi) / 2
        if _gamma_p(a, m) < p:
            lo = m
        else:
            hi = m
        if hi - lo < 1e-9 * max(1.0, hi):
            break
    return (lo + hi) / 2


def _wilson_hilferty(a: float, z: float) -> float:
    k = 2 * a
    return k * (1 - 2 / (9 * k) + z * math.sqrt(2 / (9 * k))) ** 3 / 2


def garwood_bounds(n: int) -> tuple[float, float]:
    """The exact (Garwood) two-sided 95% interval for a Poisson mean given an observed count n: lower
    chi2(0.025; 2n) / 2 (0 for n = 0), upper chi2(0.975; 2n + 2) / 2. Exact (incomplete gamma) up to
    ``GARWOOD_EXACT_MAX``; above it the Wilson-Hilferty cube-root form of the same quantiles, which agreed with exact
    values to within 0.003 and erred outward at every count checked from 1,001 to 10^12 when this was written."""
    if n <= GARWOOD_EXACT_MAX:
        lo = 0.0 if n == 0 else _gamma_quantile(n, 0.025)
        return lo, _gamma_quantile(n + 1, 0.975)
    return _wilson_hilferty(n, -Z95), _wilson_hilferty(n + 1, Z95)


def poisson_band(n: int) -> list:
    """The exact Garwood 95% band on a count, rounded outward to 0.01. Impressions are the engine's full count for
    the window, not a sample: the band says how far the count could move from one window to the next if the rate
    behind it stayed the same (a Poisson model of period-to-period variability). It is not a forecast."""
    lo, hi = garwood_bounds(n)
    return [max(0.0, math.floor(lo * 100) / 100), math.ceil(hi * 100) / 100]


def wilson_interval(clicks: int, impressions: int) -> list | None:
    """The Wilson score 95% interval for a CTR (clicks / impressions); null without impressions."""
    if impressions <= 0:
        return None
    n, p, z2 = impressions, clicks / impressions, Z95 * Z95
    den = 1 + z2 / n
    mid = (p + z2 / (2 * n)) / den
    half = Z95 * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / den
    return [max(0.0, mid - half), min(1.0, mid + half)]


def bucket(position: float | None) -> str | None:
    if position is None:
        return None
    for name, upper in BUCKETS:
        if position < upper:
            return name
    return None


def in_curve(g: dict | None, brand: bool) -> bool:
    """Whether a Search Console row is one of the curve's: established (at least the threshold), non-brand, with a
    position bucket."""
    return g is not None and not brand and g["impressions"] >= ESTABLISHED_MIN_IMPRESSIONS \
        and bucket(g["position"]) is not None


def ctr_curve(rows: list) -> dict:
    """This property's own CTR by position bucket, from established, non-brand Search Console rows: impression-
    weighted (clicks summed over impressions summed). A bucket under the support minimums is null. The sums are
    kept so that a row's own expected CTR can leave the row out (``opportunity``)."""
    acc: dict = {}
    for r in rows:
        g = r["gsc"]
        if not in_curve(g, r["intent_class"] == "brand"):
            continue
        a = acc.setdefault(bucket(g["position"]), [0, 0, 0])
        a[0] += g["clicks"]
        a[1] += g["impressions"]
        a[2] += 1
    out = {}
    for name, _ in BUCKETS:
        c, i, n = acc.get(name, (0, 0, 0))
        ok = n >= CURVE_MIN_QUERIES and i >= CURVE_MIN_IMPRESSIONS
        out[name] = {"ctr": round(c / i, 6) if ok else None, "queries": n, "impressions": i, "clicks": c,
                     "status": "COMPUTED" if ok else "INSUFFICIENT_SUPPORT"}
    return out


def opportunity(g: dict | None, curve: dict, brand: bool) -> dict:
    """The CTR gap of one Search Console row against the property's own curve (modeled; not traffic). The expected
    CTR leaves the row itself out of its bucket (leave-one-out), and the support minimums apply to what is left.
    The gap's 95% interval is the expected CTR minus the row's Wilson score CTR interval; the curve's own
    uncertainty is not added, so the interval is a lower bound on the uncertainty. COMPUTED only when the row has at
    least the established threshold of impressions and the interval excludes 0."""
    base = {"data_class": "modeled", "basis": "this property's own CTR curve (ctr_curve), leave-one-out",
            "position_bucket": None, "expected_ctr": None, "ctr_95": None, "ctr_gap": None, "ctr_gap_95": None,
            "direction": None, "click_gap_modeled": None, "click_gap_modeled_95": None}
    if g is None:
        return {**base, "status": "NOT_APPLICABLE", "why": "no Search Console measurement"}
    if brand:
        return {**base, "status": "NOT_APPLICABLE", "why": "brand query: brand CTR is not comparable with the curve"}
    b = bucket(g["position"])
    if b is None or g["impressions"] == 0:
        return {**base, "status": "INCONCLUSIVE", "why": "no position or no impressions"}
    ci = wilson_interval(g["clicks"], g["impressions"])
    base = {**base, "position_bucket": b, "ctr_95": [round(ci[0], 6), round(ci[1], 6)]}
    if g["impressions"] < ESTABLISHED_MIN_IMPRESSIONS:
        return {**base, "status": "INCONCLUSIVE",
                "why": f"fewer than {ESTABLISHED_MIN_IMPRESSIONS} impressions: too little data for a CTR gap"}
    cb = curve[b]
    c, i, n = cb["clicks"], cb["impressions"], cb["queries"]
    if in_curve(g, brand):                                  # leave the row out of its own expected CTR
        c, i, n = c - g["clicks"], i - g["impressions"], n - 1
    if n < CURVE_MIN_QUERIES or i < CURVE_MIN_IMPRESSIONS:
        return {**base, "status": "INCONCLUSIVE",
                "why": "the property's curve has too little support in this position bucket without this row"}
    exp = c / i
    gap = exp - g["ctr"]
    glo, ghi = exp - ci[1], exp - ci[0]
    out = {**base, "expected_ctr": round(exp, 6), "ctr_gap": round(gap, 6),
           "ctr_gap_95": [round(glo, 6), round(ghi, 6)],
           "click_gap_modeled": max(0, round(gap * g["impressions"])),
           "click_gap_modeled_95": [max(0, round(glo * g["impressions"])), max(0, round(ghi * g["impressions"]))]}
    if glo <= 0 <= ghi:
        return {**out, "status": "INCONCLUSIVE",
                "why": "the gap's 95% interval includes 0: the row's CTR is not distinguishable from the curve"}
    return {**out, "status": "COMPUTED", "direction": "below_curve" if glo > 0 else "above_curve",
            "why": "expected CTR at this position on this property (row left out) minus the row's CTR, times its "
                   "impressions; the interval is from the row's Wilson CTR interval"}


# ====================================================================== the ledger

class SourceResult:
    """One source's read as Priya receives it. ``state`` OK or the source's reason code; ``rows`` per source:
    Search Console (query, clicks, impressions, ctr, position); Bing (query, date, clicks, impressions,
    avg_click_position, avg_impression_position)."""

    def __init__(self, name: str, state: str, outcome: str, rows: list | None = None, meta: dict | None = None):
        self.name, self.state, self.outcome = name, state, outcome
        self.rows = rows or []
        self.meta = meta or {}

    @property
    def ok(self) -> bool:
        return self.state == "OK"


def _gsc_metrics(c: int, i: int, pos_weighted: float, window: dict) -> dict:
    return {"source": "first_party:search_console", "data_class": "measured",
            "metric": "impressions_on_this_property", "clicks": c, "impressions": i,
            "ctr": None if i == 0 else round(c / i, 6), "position": None if i == 0 else round(pos_weighted / i, 2),
            "impressions_poisson_95": poisson_band(i), "date_range": {"start": window["start"], "end": window["end"]},
            "timezone": window.get("timezone"), "aggregation": "byProperty", "data_state": "final"}


def _bing_metrics(c: int, i: int, pw: float, pi: int, dates: set) -> dict:
    return {"source": "first_party:bing_webmaster", "data_class": "measured",
            "metric": "impressions_on_this_property", "clicks": c, "impressions": i,
            "ctr": None if i == 0 else round(c / i, 6),
            "avg_impression_position": None if pi == 0 else round(pw / pi, 2),
            "position_note": "as Bing reports it (AvgImpressionPosition); its scale is not verified against Search "
                             "Console's",
            "impressions_poisson_95": poisson_band(i),
            "date_range": {"start": min(dates), "end": max(dates)} if dates else None}


def build(tenant_id: str, run_id: str, generated_at: str, brands: list, gsc: SourceResult,
          bing: SourceResult) -> dict:
    """The keyword ledger. Deterministic: the same inputs give the same ledger."""
    pii = {"queries": 0, "clicks": 0, "impressions": 0}
    pii_seen: set = set()             # distinct dropped queries (in memory only; a query in both sources, or on many
    merged: dict = {}                 #  Bing dates, is one query). norm -> {...}
    dup_merged = 0

    def drop(raw: str, c: int, i: int) -> None:
        k = hashlib.sha256(normalize(raw).encode("utf-8", "surrogatepass")).digest()
        if k not in pii_seen:
            pii_seen.add(k)
            pii["queries"] += 1
        pii["clicks"] += c
        pii["impressions"] += i

    def slot(raw: str) -> dict | None:
        nonlocal dup_merged
        norm = normalize(raw)
        if not norm:
            return None
        cls = sp.classify_query(raw, brands)
        if cls != PII_CLASS:
            cls = sp.classify_query(norm, brands)        # the normalised text too (fullwidth digits, case)
        if cls == PII_CLASS:
            return None
        e = merged.get(norm)
        if e is None:
            e = merged[norm] = {"norm": norm, "raw": raw, "intent_class": cls, "gsc": None, "bing": None,
                                "_g": [0, 0, 0.0, 0], "_b": [0, 0, 0.0, 0, set(), 0]}
        return e

    gwin = gsc.meta.get("window") or {}
    if gsc.ok:
        for q, c, i, _ctr, pos in gsc.rows:
            e = slot(q)
            if e is None:
                if normalize(q):
                    drop(q, c, i)
                continue
            g = e["_g"]
            if g[3]:
                dup_merged += 1                           # two engine spellings, one normalised query
            g[0] += c
            g[1] += i
            g[2] += (pos or 0.0) * i
            g[3] += 1
    if bing.ok:
        for q, d, c, i, _cpos, ipos in bing.rows:
            e = slot(q)
            if e is None:
                if normalize(q):
                    drop(q, c, i)
                continue
            b = e["_b"]
            b[0] += c
            b[1] += i
            if ipos is not None:
                b[2] += ipos * i
                b[3] += i
            b[4].add(d)
    for e in merged.values():
        g, b = e.pop("_g"), e.pop("_b")
        if g[3]:
            e["gsc"] = _gsc_metrics(g[0], g[1], g[2], gwin)
        if b[4]:
            e["bing"] = _bing_metrics(b[0], b[1], b[2], b[3], b[4])

    entries = sorted(merged.values(), key=lambda e: e["norm"])
    curve = ctr_curve(entries)
    truncated = bool(gsc.meta.get("truncated"))
    unattributed = gsc.meta.get("unattributed") or {}
    anonymised = bool(unattributed.get("impressions", 0) > 0)

    # near-duplicate groups (more than one ledger row sharing a key)
    groups: dict = {}
    for e in entries:
        groups.setdefault(near_key(e["norm"]), []).append(e["norm"])
    ndg = {}
    for k, members in groups.items():
        if len(members) > 1:
            gid = "ndg-" + hashlib.sha256(k.encode("utf-8", "surrogatepass")).hexdigest()[:12]
            for n in members:
                ndg[n] = (gid, sorted(row_key(m) for m in members if m != n))

    rows = []
    for e in entries:
        # the tier is Search Console's count against the threshold (its window is the threshold's); a query only Bing
        # measured is never tiered: Bing's rows are summed over its own multi-week dates, not comparable
        bing_only = e["gsc"] is None
        imp = (e["bing"] if bing_only else e["gsc"])["impressions"]
        established = not bing_only and imp >= ESTABLISHED_MIN_IMPRESSIONS
        flags = []
        if bing_only:
            flags.append("BING_ONLY_UNTIERED")
        elif not established:
            flags.append("BELOW_THRESHOLD")
        elif imp < MEDIUM_MIN:
            flags.append("LOW_IMPRESSION_COUNT")
        if (e["gsc"] is None) != (e["bing"] is None):
            flags.append("SINGLE_SOURCE")
        if e["gsc"] is not None and truncated:
            flags.append("GSC_ROWS_TRUNCATED")
        if e["gsc"] is not None and anonymised:
            flags.append("ANONYMISED_REMAINDER")
        if e["bing"] is not None:
            flags.append("BING_WINDOW_DIFFERS")
        if e["norm"] in ndg:
            flags.append("NEAR_DUPLICATE")
        conf = "NONE" if not established else "HIGH" if imp >= HIGH_MIN and not truncated else \
            "MEDIUM" if imp >= MEDIUM_MIN else "LOW"
        rows.append({
            "key": row_key(e["norm"]), "query": observed(e["norm"]), "tier": "ESTABLISHED" if established else
            "EMERGING", "origin": "observed", "intent_class": e["intent_class"],
            "emerging_kinds": None if established else emerging_kinds(e["norm"]),
            "metrics": {"search_console": e["gsc"], "bing_webmaster": e["bing"]},
            "volume_status": "MEASURED_ON_THIS_PROPERTY" if established else "MEASURED_NOT_TIERED" if bing_only
            else "BELOW_THRESHOLD",
            "market_volume": None,
            "opportunity": opportunity(e["gsc"], curve, e["intent_class"] == "brand"),
            "difficulty": None, "confidence": conf,
            "uncertainty_flags": [f for f in FLAGS if f in flags],
            "near_duplicate": None if e["norm"] not in ndg else {"group": ndg[e["norm"]][0],
                                                                 "others": ndg[e["norm"]][1]},
            "evidence": _evidence(e, run_id, gsc, bing)})

    derived, frames = _derive(entries, rows, brands, run_id)
    rows += derived
    rows.sort(key=_order)
    dropped = max(0, len(rows) - ROWS_CAP)
    if dropped:
        keep = sorted(rows, key=_weight)[:ROWS_CAP]
        rows = sorted(keep, key=_order)

    status = _status(gsc, bing, rows, pii)
    counts = {"rows": len(rows), "rows_dropped_past_cap": dropped,
              "established": sum(1 for r in rows if r["tier"] == "ESTABLISHED"),
              "emerging_observed": sum(1 for r in rows if r["tier"] == "EMERGING" and r["origin"] == "observed"),
              "emerging_derived": sum(1 for r in rows if r["origin"] == "derived"),
              "pii_suspect_dropped": pii, "duplicate_spellings_merged": dup_merged,
              "near_duplicate_groups": len({v[0] for v in ndg.values()})}
    ledger = {
        "schema": LEDGER_SCHEMA, "version": VERSION, "agent": AGENT, "task": TASK, "tenant_id": tenant_id,
        "run_id": run_id, "generated_at": generated_at, "status": status,
        "status_meaning": STATUS_MEANING[status],
        "sources": {"search_console": _source_view(gsc), "bing_webmaster": _source_view(bing),
                    "google_ads_keyword_planner": {"state": "NOT_BUILT",
                                                   "why": "interface only (keyword_planner.py); founder decision"}},
        "thresholds": {"established_min_impressions": ESTABLISHED_MIN_IMPRESSIONS,
                       "established_on": "Search Console's count in its window; Bing-only queries are not tiered",
                       "medium_min": MEDIUM_MIN,
                       "high_min": HIGH_MIN, "curve_min_queries": CURVE_MIN_QUERIES,
                       "curve_min_impressions": CURVE_MIN_IMPRESSIONS, "status": "founder-pending defaults"},
        "ctr_curve": {"source": "this property's own Search Console query rows in the window above: established, "
                                "non-brand, impression-weighted; a row's own expected CTR leaves the row out",
                      "buckets": curve,
                      "not_used": "no published industry CTR curve is used"},
        "difficulty": {"computed": False, "why": "no owned data supports a keyword difficulty (no backlink or SERP "
                                                 "data); none is fabricated"},
        "market_volume": {"measured": False, "why": "no paid keyword vendor; Google Ads Keyword Planner adapter not "
                                                    "built; engine impressions are not market volume"},
        "frames": frames, "counts": counts, "rows": rows,
        "handoff": handoff(tenant_id, run_id, generated_at, rows),
        "limitations": list(LIMITS), "headline_score": None,
    }
    return ledger


STATUS_MEANING = {
    "OK": "every requested source was read",
    "PARTIAL": "at least one source was read and at least one requested source was not",
    "EMPTY_MEASURED": "a source was read and returned no usable query rows; this is a measurement, not 'no demand' "
                      "(see the anonymised remainder)",
    "INCONCLUSIVE": "no source could be read; an empty ledger here means nothing was measured, not that there are "
                    "no keywords",
}


def _status(gsc: SourceResult, bing: SourceResult, rows: list, pii: dict) -> str:
    requested = [s for s in (gsc, bing) if s.state != "NOT_REQUESTED"]
    read = [s for s in requested if s.ok]
    if not read:
        return "INCONCLUSIVE"
    if not any(r["origin"] == "observed" for r in rows):
        return "EMPTY_MEASURED"
    return "OK" if len(read) == len(requested) else "PARTIAL"


def _source_view(s: SourceResult) -> dict:
    return {"state": s.state, "outcome": s.outcome, "rows_received": len(s.rows) if s.ok else 0,
            **{k: v for k, v in s.meta.items() if k in ("window", "rows_read", "pages_read", "truncated", "totals",
                                                        "query_rows_sum", "unattributed", "property_sha256",
                                                        "requests", "detail", "site_sha256", "date_range")}}


def _evidence(e: dict, run_id: str, gsc: SourceResult, bing: SourceResult) -> dict:
    ev = {"run_id": run_id, "rule": f"priya/{VERSION}", "pointers": []}
    if e["gsc"] is not None:
        ev["pointers"].append({"source": "first_party:search_console",
                               "call": "searchAnalytics.query dimensions=[query] aggregationType=byProperty "
                                       "type=web dataState=final",
                               "property_sha256": gsc.meta.get("property_sha256"),
                               "window": gsc.meta.get("window")})
    if e["bing"] is not None:
        ev["pointers"].append({"source": "first_party:bing_webmaster", "call": "GetQueryStats",
                               "site_sha256": bing.meta.get("site_sha256"),
                               "date_range": e["bing"]["date_range"]})
    return ev


def _order(r: dict):
    m = r["metrics"] or {}
    imp = max((m.get("search_console") or {}).get("impressions", 0), (m.get("bing_webmaster") or {}).get("impressions",
                                                                                                        0))
    return (TIERS.index(r["tier"]), ORIGINS.index(r["origin"]), -imp, r["key"])


def _weight(r: dict):
    """Which rows survive the cap: established first, then by impressions; derived variants last."""
    return _order(r)


# ====================================================================== EMERGING: derived variants

def _derive(entries: list, rows: list, brands: list, run_id: str) -> tuple[list, dict]:
    """Frames from the site's own long tail transferred to its established head terms. A frame is an observed query
    with an established head replaced by ``{head}`` (``how to clean {head}``, ``{head} near me``); it is kept when it
    was seen around at least FRAME_MIN_HEADS distinct heads. Each kept frame is applied to the other heads; a variant
    already observed is not derived again. No volume is given to any variant."""
    by_norm = {r["query"]["text"]: r for r in rows}
    capitalised = {e["norm"] for e in entries if _has_capital(e["raw"])}
    heads = [r for r in rows if r["tier"] == "ESTABLISHED" and r["intent_class"] != "brand"
             and 1 <= len(r["query"]["text"].split()) <= 4 and r["query"]["text"] not in capitalised]
    heads.sort(key=_order)
    heads = heads[:HEADS_CAP]
    head_text = [h["query"]["text"] for h in heads]
    support: dict = {}
    for e in entries:
        q = e["norm"]
        if q in capitalised:                       # a capitalised word may be a name: never a frame's source
            continue
        padded = f" {q} "
        for h in head_text:
            if h == q or f" {h} " not in padded:
                continue
            frame = " ".join(padded.replace(f" {h} ", " {head} ", 1).split())
            extra = len(frame.split()) - 1
            if extra < 1 or extra > FRAME_MAX_EXTRA_WORDS or frame.count("{head}") != 1:
                continue
            if any(w not in FRAME_VOCAB for w in frame.split() if w != "{head}"):
                continue                           # a word no rule here can classify could be a name
            support.setdefault(frame, {"heads": set(), "rows": set()})
            support[frame]["heads"].add(h)
            support[frame]["rows"].add(row_key(q))
    kept = sorted((f for f, s in support.items() if len(s["heads"]) >= FRAME_MIN_HEADS),
                  key=lambda f: (-len(support[f]["heads"]), f))[:FRAMES_CAP]
    frames = {"rule": f"frame_transfer/{VERSION}", "min_heads": FRAME_MIN_HEADS, "heads_considered": len(heads),
              "vocabulary": "closed (FRAME_VOCAB): a frame adding any other word is not used",
              "kept": [{"frame": observed(f), "seen_with_heads": len(support[f]["heads"]),
                        "supporting_rows": sorted(support[f]["rows"])[:20]} for f in kept],
              "candidates_seen": len(support)}
    out = []
    seen = set(by_norm)
    for f in kept:
        for h in head_text:
            if len(out) >= VARIANTS_CAP:
                break
            if h in support[f]["heads"]:
                continue
            v = " ".join(f.replace("{head}", h).split())
            if v in seen or sp.classify_query(v, brands) == PII_CLASS:
                continue
            seen.add(v)
            out.append({
                "key": row_key(v), "query": observed(v), "tier": "EMERGING", "origin": "derived",
                "intent_class": sp.classify_query(v, brands), "emerging_kinds": emerging_kinds(v),
                "metrics": {"search_console": None, "bing_webmaster": None},
                "volume_status": "UNKNOWN_NOT_MEASURED", "market_volume": None,
                "opportunity": {"status": "NOT_APPLICABLE", "why": "not measured", "data_class": "modeled",
                                "basis": None, "position_bucket": None, "expected_ctr": None, "ctr_95": None,
                                "ctr_gap": None, "ctr_gap_95": None, "direction": None, "click_gap_modeled": None,
                                "click_gap_modeled_95": None},
                "difficulty": None, "confidence": "NONE", "uncertainty_flags": ["NOT_MEASURED"],
                "near_duplicate": None,
                "evidence": {"run_id": run_id, "rule": frames["rule"], "frame_sha256": hashlib.sha256(
                    f.encode("utf-8", "surrogatepass")).hexdigest(), "head_key": row_key(h),
                    "supporting_rows": sorted(support[f]["rows"])[:20], "pointers": []}})
    return out, frames


def _has_capital(raw: str) -> bool:
    """Whether the engine reported the query with a capitalised word (engines mostly report lower case, so a
    capital is a sign of a proper noun). Conservative: any word starting with an upper-case letter counts."""
    return any(w[:1].isupper() for w in unicodedata.normalize("NFKC", raw or "").split())


# ====================================================================== the Theo / Naomi hand-off contract

HANDOFF_CONSUMERS = {"theo": {"task": "conversation mining", "state": "NOT_BUILT"},
                     "naomi": {"task": "prompt volume", "state": "NOT_CONNECTED"}}
HANDOFF_ITEM_KEYS = ("key", "query", "origin", "emerging_kinds", "volume_status", "observed_counts", "evidence",
                     "requested_measurements")


HANDOFF_RECORD_CONTRACT = "priya.handoff_record/1"


def handoff_sha(doc: dict) -> str:
    """The SHA-256 of the canonical hand-off document (contract ``priya.handoff_record/1``, the consumer's
    ``priya_handoff.canonical_sha``): JSON, keys sorted, separators (',', ':'), ensure_ascii false, UTF-8, hex."""
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8", "surrogatepass")).hexdigest()


def handoff(tenant_id: str, run_id: str, generated_at: str, rows: list) -> dict:
    """The EMERGING tier as Theo and Naomi will receive it (contract only: neither is built). Every item says it has
    no reliable volume; observed thin counts travel as they were measured, never as a volume."""
    items = []
    for r in rows:
        if r["tier"] != "EMERGING":
            continue
        m = r["metrics"] or {}
        g = m.get("search_console") or m.get("bing_webmaster")        # Bing's only for a Bing-only (untiered) row
        items.append({"key": r["key"], "query": r["query"], "origin": r["origin"],
                      "emerging_kinds": list(r["emerging_kinds"] or []), "volume_status": r["volume_status"],
                      "observed_counts": None if g is None else {"impressions": g["impressions"],
                                                                 "clicks": g["clicks"],
                                                                 "date_range": g["date_range"],
                                                                 "source": g["source"]},
                      "evidence": r["evidence"], "requested_measurements": ["conversation_mining", "prompt_volume"]})
    return {"schema": HANDOFF_SCHEMA, "tenant_id": tenant_id, "run_id": run_id, "generated_at": generated_at,
            "consumers": {k: dict(v) for k, v in HANDOFF_CONSUMERS.items()}, "items": items,
            "rule": "an item is a candidate to measure, not a measured demand"}


def validate_handoff(doc) -> list:
    """Problems with a hand-off document (empty = valid). Theo and Naomi are to call this on what they receive."""
    p = []
    if not isinstance(doc, dict) or doc.get("schema") != HANDOFF_SCHEMA:
        return ["schema"]
    for k in ("tenant_id", "run_id", "generated_at"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            p.append(k)
    items = doc.get("items")
    if not isinstance(items, list):
        return p + ["items"]
    for n, it in enumerate(items):
        if not isinstance(it, dict) or tuple(sorted(it)) != tuple(sorted(HANDOFF_ITEM_KEYS)):
            p.append(f"items[{n}].keys")
            continue
        if it["origin"] not in ORIGINS:
            p.append(f"items[{n}].origin")
        if it["volume_status"] not in ("BELOW_THRESHOLD", "MEASURED_NOT_TIERED", "UNKNOWN_NOT_MEASURED"):
            p.append(f"items[{n}].volume_status")
        if it["origin"] == "derived" and it["observed_counts"] is not None:
            p.append(f"items[{n}].observed_counts")
        if not isinstance(it["query"], dict) or it["query"].get("untrusted") is not True:
            p.append(f"items[{n}].query")
        if not it["emerging_kinds"] or not set(it["emerging_kinds"]) <= set(EMERGING_KINDS):
            p.append(f"items[{n}].emerging_kinds")
    return p
