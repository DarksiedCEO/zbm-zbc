"""
Osei's ``search_performance`` task (ADR 0017, section "Search Console (first-party truth)"): first-party search
performance for ONE tenant's ONE bound Search Console property, read through the GSC connector (gsc_client.py) and
reduced at once to aggregates. This is the first first-party source Osei integrates.

What is kept (evidence class: OBSERVED, first-party; data class: measured):
  - property totals by date (Search Analytics grouped by ``date``, aggregated by property): clicks, impressions,
    CTR (clicks / impressions) and position as Google reports it;
  - window totals: clicks and impressions summed over the dates; CTR recomputed from those sums (never an average of
    CTRs); position impression-weighted over the dates (Google's per-date positions; an approximation, said so);
  - by PAGE TEMPLATE (grouped by ``page``, aggregated by page): each URL reduced to a template with the log rules
    (``agents.logs.template_path``: e-mails, ids, numbers, tokens, file names replaced), then the slug rule
    (``slug_segment``: every CamelCase segment, and every segment that is not one plain word of ASCII letters, a placeholder, or a word with a
    static extension becomes ``{slug}``, so ``/users/jane-smith/profile`` is ``/users/{slug}/profile``); a URL not
    on the property's hosts counts as ``{offsite}``. A template that still holds a literal path word and was produced
    by fewer than ``TEMPLATE_MIN_URLS`` distinct pages (``page_key``: the lower-cased path without its trailing slash;
    host, scheme and query ignored) is counted in ``{other}`` (no kept template describes one
    or two pages); at most ``TEMPLATE_CAP`` templates, the rest in ``{other}``;
  - by QUERY CLASS (grouped by ``query``, aggregated by property): each query is classified into a closed set and
    dropped. No query text, no hash of one, and no per-query row is ever kept. A query that looks like personal
    data (an e-mail in any spelling, one phone-like run of 7+ digits not an ISO date, an id- or token-like word) is counted as ``pii_suspect`` — the class
    is checked FIRST, so such a query never counts as brand or anything else. Dropping beats hashing: a hash of a
    low-entropy name or e-mail is reversed by a dictionary.
  - the remainder: property totals minus the query-class sums is reported as ``unattributed`` (Google's anonymised
    queries and rows past the caps), never spread over the classes. Page-template sums are Google's by-page
    aggregation and are NOT comparable with property totals (Google counts impressions differently by page); they
    are reported as they are, with no remainder.

No single score: the section reports these numbers side by side with the Goodhart pairing (revenue: analytics / CRM
NOT_CONNECTED, so every decision drawn from them is INSUFFICIENT_EVIDENCE). It produces no findings.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import lookalikes
from agents import logs as logs_mod

AGENT, TASK = "osei", "search_performance"
VERSION = "2026-10-24.3"                     # query classes, templating and the aggregation rules below
# .3 (AEGIS gl-gsc certification lows): {other} counts distinct normalised pages; CamelCase segments are {slug};
#    the digit-run separators widened and ISO dates not counted
# .2 (AEGIS gl-gsc L1, L4): the slug rule and the low-count collapse for page templates; the PII digit rule counts one
#    digit run (phone separators allowed), not digits summed across words
QUERY_CLASSES = ("pii_suspect", "brand", "question", "local", "commercial", "other")
TEMPLATE_CAP = 200
TEMPLATE_MIN_URLS = 3                         # a template with a literal word needs at least this many distinct URLs
_TEMPLATE_TRACK = 5000                        # distinct templates tracked during a read before new ones go to {other}
_WORD = re.compile(r"[A-Za-z]{1,30}")
_CAMEL = re.compile(r"[a-z][A-Z]")              # JaneSmith, janeSmith: a capital after a small letter
_WORD_EXT = re.compile(r"([A-Za-z]{1,30})\.([A-Za-z0-9]{1,5})")
_PLACEHOLDER = re.compile(r"\{(?:email|uuid|number|id|token|slug)\}|\{file\}\.[a-z0-9]{1,5}")
LAG_DAYS = 3                                  # Google: "Data is typically available after 2-3 days"
_QUESTION = re.compile(r"^(who|what|when|where|why|how|which|can|could|does|do|is|are|should|will|would)\b|\?$")
_LOCAL = re.compile(r"\b(near me|nearby|near by|open now|directions|closest|around me)\b")
_COMMERCIAL = re.compile(r"\b(price|prices|pricing|cost|costs|buy|cheap|cheapest|best|top|review|reviews|vs|versus|"
                         r"deal|deals|quote|quotes|hire|agency|agencies|service|services|company|companies)\b")
_LOOKALIKE = lookalikes.Table()
GOODHART = {"rule": "SEO metrics are reported only beside revenue; there is no single score",
            "revenue": "NOT_CONNECTED (analytics and CRM ports)",
            "decision": "INSUFFICIENT_EVIDENCE"}
EVIDENCE = {"evidence_class": "OBSERVED", "source_class": "first_party", "source": "first_party:search_console",
            "data_class": "measured", "effect_class": None,
            "note": "what Google Search Console reported for the property; no effect is claimed"}


def pacific_today(now: datetime) -> date:
    """Search Analytics dates are Pacific Time (Google). zoneinfo when the system has the zone; otherwise UTC-8 (off by
    at most one hour in summer, irrelevant behind the 3-day lag)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - no tz database: the fixed standard offset
        tz = timezone(timedelta(hours=-8))
    return now.astimezone(tz).date()


def window(now: datetime, days: int) -> tuple[str, str]:
    end = pacific_today(now) - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def brand_tokens(domains: Iterable[str], entity_name: str | None) -> list:
    """Brand words for the query classifier: each registered domain's first label (``www.`` dropped) and the entity
    record's name with spaces removed; at least four characters."""
    out = set()
    for d in domains or ():
        d = d.removeprefix("www.")
        lab = d.split(".")[0]
        if len(lab) >= 4:
            out.add(lab)
    if entity_name:
        n = re.sub(r"[^a-z0-9]", "", _LOOKALIKE.fold_cased(entity_name))
        if len(n) >= 4:
            out.add(n)
    return sorted(out)


PII_DIGIT_RUN = 7
# phone separators: space, dot, parentheses, slash, underscore, comma, plus, the ASCII hyphen, the Unicode hyphens and
# dashes (U+2010..U+2015: hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar), the minus sign
# and the middle dot; up to five in a row (" -- ", ") - ") between two digits (AEGIS gl-gsc certification L4)
_SEP = " .()/_,+\\-\u2010-\u2015\u2212\u00b7"
_DIGIT_RUN = re.compile(r"\d(?:[" + _SEP + r"]{0,5}\d)*")
_YEAR = re.compile(r"(?:19|20)\d\d")
# an ISO calendar date (YYYY-MM-DD, also with / . or a Unicode dash, the same separator twice): not a phone number
_ISO_DATE = re.compile(r"(?<!\d)(?:19|20)\d\d([-/.\u2010-\u2015\u2212])(?:0?[1-9]|1[0-2])\1"
                       r"(?:0?[1-9]|[12]\d|3[01])(?!\d)")


def longest_digit_run(text: str) -> int:
    """The most digits in one phone-like run: ``555 010 0199``, ``(555) 010-0199``, ``+44 20 7946 0958`` count 10, 10
    and 12. Digits in different runs (``iphone 15 pro 256gb 2024``) are never added up (AEGIS gl-gsc L4). A run with
    two or more year-like groups (19xx / 20xx: ``covid 19 2020 2021``) is a list of years: its year groups do not
    count. A phone number holding one such group (``555 010 2024``) is still caught. ISO dates (``2024-10-24``) are
    taken out first and break a run."""
    best = 0
    for m in _DIGIT_RUN.finditer(_ISO_DATE.sub(" # ", text)):
        groups = re.findall(r"\d+", m.group())
        years = [g for g in groups if _YEAR.fullmatch(g)]
        n = sum(len(g) for g in groups if len(years) < 2 or not _YEAR.fullmatch(g))
        best = max(best, n)
    return best


def _word_identifies(w: str) -> bool:
    """A word the log templater would replace (e-mail, UUID, file name, token, a letters-and-digits mix). A word of
    digits and separators only (``2020-2021``) is left to the digit-run rule instead of the templater's total count."""
    kind = logs_mod._kind(w)
    if kind == "{number}" and not any(c.isalpha() for c in w):
        return False
    return kind not in (None, "{id}")


def classify_query(q: str, brands: list) -> str:
    """One query -> one class from ``QUERY_CLASSES``; the text is not kept. PII first (judged on the lookalike fold,
    after invisible characters are removed, like the log templater)."""
    t = lookalikes.strip_invisible(unicodedata.normalize("NFKC", q or ""))
    folded = _LOOKALIKE.fold_cased(t)
    words = folded.split()
    if "@" in folded or logs_mod._EMAIL.search(folded) or logs_mod._AT_WORD.search(folded) \
            or longest_digit_run(folded) >= PII_DIGIT_RUN or any(_word_identifies(w) for w in words):
        return "pii_suspect"
    squashed = re.sub(r"[^a-z0-9]", "", folded)
    if any(b in squashed for b in brands):
        return "brand"
    if _QUESTION.search(folded):
        return "question"
    if _LOCAL.search(folded):
        return "local"
    if _COMMERCIAL.search(folded):
        return "commercial"
    return "other"


def slug_segment(seg: str) -> str:
    """One segment of a log template (``agents.logs.template_path`` output) -> itself when it is empty, a placeholder,
    one plain word of ASCII letters (``blog``, ``users``), or such a word with a static extension (``index.html``);
    otherwise ``{slug}``. Hyphenated, dotted, underscored, spaced, digit-bearing and non-ASCII segments are the ones
    that carry names and titles (``jane-smith``, ``john.q.public``, ``post-1``), so none of them is kept."""
    if seg == "" or _PLACEHOLDER.fullmatch(seg):
        return seg
    if _CAMEL.search(seg):                       # CamelCase joins words: JaneSmith is a name (AEGIS gl-gsc cert L1)
        return "{slug}"
    if _WORD.fullmatch(seg):
        return seg
    m = _WORD_EXT.fullmatch(seg)
    if m and m.group(2).lower() in logs_mod.STATIC_EXT:
        return seg
    return "{slug}"


def has_literal(template: str) -> bool:
    """Whether a page template still holds a literal path word (something not a placeholder)."""
    if template.startswith("{"):
        return False
    return any(s and not _PLACEHOLDER.fullmatch(s) for s in template.split("/")[1:])


def page_template(url: str, hosts: set) -> str:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
    except ValueError:
        return "{offsite}"
    if p.scheme not in ("http", "https") or host not in hosts:
        return "{offsite}"
    t = logs_mod.template_path(p.path or "/")
    if t is None:
        return "{unreadable}"
    # one page, one template: literal words lower-cased (after the CamelCase check) and no trailing slash, the same
    # normalisation as page_key (AEGIS gl-gsc certification L1)
    segs = [slug_segment(x) for x in t.split("/")[1:]]
    out = "/" + "/".join(x if x.startswith("{") else x.lower() for x in segs)
    return out.rstrip("/") or "/"


def page_key(url: str) -> bytes:
    """What makes two Search Console page URLs the same page for the ``{other}`` threshold: the path decoded once,
    lower-cased, without a trailing slash; host (www or not), scheme, port, query and fragment ignored. Only an 8-byte
    hash of it is held, in memory, during the read (AEGIS gl-gsc cert L1: one page under several URLs is one page)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    norm = unquote(path).lower().rstrip("/")
    return hashlib.blake2b(norm.encode("utf-8", "surrogatepass"), digest_size=8).digest()


def _metric(clicks: int, impressions: int, pos_weighted: float) -> dict:
    return {"clicks": clicks, "impressions": impressions,
            "ctr": None if impressions == 0 else round(clicks / impressions, 6),
            "position": None if impressions == 0 else round(pos_weighted / impressions, 2)}


class _Acc:
    __slots__ = ("c", "i", "pw")

    def __init__(self):
        self.c = self.i = 0
        self.pw = 0.0

    def add(self, c: int, i: int, pos: float | None) -> None:
        self.c += c
        self.i += i
        if pos is not None:
            self.pw += pos * i

    def view(self) -> dict:
        return _metric(self.c, self.i, self.pw)


def read_aggregates(rd, prop: str, start: str, end: str, hosts: set, brands: list) -> dict:
    """The three queries of one read, reduced to aggregates as each page arrives (raw rows never outlive their page).
    ``rd`` is a ``gsc_client._Read`` (the deadline, the request budget and the kill-switch guard are its)."""
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    rd.check_property(prop)
    # 1. property totals by date (one page: at most ``days`` rows)
    by_date = {}
    for keys, c, i, _ctr, pos in rd.query(prop, start, end, ("date",), "byProperty", days, 0):
        acc = by_date.setdefault(keys[0], _Acc())
        acc.add(c, i, pos)
    total = _Acc()
    for acc in by_date.values():
        total.add(acc.c, acc.i, None)
        total.pw += acc.pw
    # 2. page templates (aggregated by page)
    templates: dict = {}
    urls: dict = {}                               # template -> hashes of the distinct pages (page_key) behind it
    pstats: dict = {}
    for rows in rd.pages(prop, start, end, ("page",), "byPage", pstats):
        for keys, c, i, _ctr, pos in rows:
            t = page_template(keys[0], hosts)
            if t not in templates and len(templates) >= _TEMPLATE_TRACK:
                t = "{other}"
            templates.setdefault(t, _Acc()).add(c, i, pos)
            urls.setdefault(t, set()).add(page_key(keys[0]))
        rows.clear()
    templates, collapsed = _collapse(templates, urls)
    # 3. query classes (aggregated by property); the query text is classified and dropped here
    classes = {k: _Acc() for k in QUERY_CLASSES}
    qstats: dict = {}
    for rows in rd.pages(prop, start, end, ("query",), "byProperty", qstats):
        for keys, c, i, _ctr, pos in rows:
            classes[classify_query(keys[0], brands)].add(c, i, pos)
        rows.clear()
    attributed_c = sum(a.c for a in classes.values())
    attributed_i = sum(a.i for a in classes.values())
    return {
        "property": prop,
        "window": {"start": start, "end": end, "days": days, "timezone": "America/Los_Angeles",
                   "data_state": "final", "search_type": "web", "lag_days": LAG_DAYS},
        "totals": {**total.view(), "aggregation": "byProperty",
                   "position_note": "impression-weighted mean of Google's per-date positions (an approximation)"},
        "by_date": [{"date": d, **by_date[d].view()} for d in sorted(by_date)],
        "dates_without_data": days - len(by_date),
        "by_page_template": {
            "aggregation": "byPage", "comparable_with_totals": False, **pstats,
            "min_urls_per_template": TEMPLATE_MIN_URLS, "templates_collapsed_into_other": collapsed,
            "templates": sorted(({"template": t, **a.view()} for t, a in templates.items()),
                                key=lambda r: (-r["clicks"], -r["impressions"], r["template"]))},
        "by_query_class": {
            "aggregation": "byProperty", **qstats,
            "classes": {k: classes[k].view() for k in QUERY_CLASSES},
            "unattributed": {"clicks": total.c - attributed_c, "impressions": total.i - attributed_i,
                             "why": "anonymised queries Google does not return, and rows past the caps"},
            "rows_exceed_totals": attributed_c > total.c or attributed_i > total.i,
            "query_text_kept": False},
        "versions": {"search_performance": VERSION, "template_rules": "agents.logs.template_path"},
        "evidence": dict(EVIDENCE), "pairing": dict(GOODHART), "headline_score": None,
    }


def _collapse(templates: dict, urls: dict) -> tuple[dict, int]:
    """Low-count templates with a literal word, then everything past ``TEMPLATE_CAP`` (fewest clicks first), are
    merged into ``{other}``. Returns the kept templates and how many were merged."""
    kept: dict = {}
    other = templates.get("{other}") or _Acc()
    n = 0
    for t, acc in templates.items():
        if t == "{other}":
            continue
        if has_literal(t) and len(urls.get(t, ())) < TEMPLATE_MIN_URLS:
            other.add(acc.c, acc.i, None)
            other.pw += acc.pw
            n += 1
        else:
            kept[t] = acc
    if len(kept) > TEMPLATE_CAP - 1:
        order = sorted(kept, key=lambda t: (-kept[t].c, -kept[t].i, t))
        for t in order[TEMPLATE_CAP - 1:]:
            acc = kept.pop(t)
            other.add(acc.c, acc.i, None)
            other.pw += acc.pw
            n += 1
    if n or "{other}" in templates:
        kept["{other}"] = other
    return kept, n


def totals_only(section: dict) -> dict:
    """What an expired section is served as (retention): the window and its totals; breakdowns removed."""
    return {"property": section.get("property"), "window": section.get("window"), "totals": section.get("totals"),
            "versions": section.get("versions"), "evidence": section.get("evidence"), "pairing": section.get("pairing"), "headline_score": None,
            "expired": True}
