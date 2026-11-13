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
    data (an e-mail in any spelling, one phone-like run of 7+ digits not an ISO date, an id- or token-like word, a street
    address (number, street name, suffix; a unit only right after one), PO box, ZIP+4 or a final ZIP after a US
    state) is counted as ``pii_suspect`` — the class
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

import base64
import binascii
import hashlib
import itertools
import math
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import lookalikes
from agents import logs as logs_mod

AGENT, TASK = "osei", "search_performance"
VERSION = "2026-11-08.18"                     # query classes, templating and the aggregation rules below
# 2026-11-08.18 (review round 18): the CLOSED threat list of ADR 0047 - the digit stream (any gap of up to three
#    non-digit characters, letters included; UATP cards), spelled digits across such gaps, vanity toll-free numbers
#    with any separator, e-mails spelled / bracketed / rot13 / reversed / base64 / base32 / hex / URL-encoded, whole
#    or chunked, in surrounding text; addresses in surrounding text; software names as version keywords (generic)
# 2026-11-08.1 (Priya review round 1, M3): street addresses, unit / apartment numbers, PO boxes, ZIP+4 and a ZIP next
#    to a US state are pii_suspect (looks_like_address)
# 2026-11-08.2 (review round 2): the address rule needs address structure (it over-dropped service queries)
# 2026-11-08.16 (review round 16): detector separators by Unicode category; 7-digit local and vanity toll-free
#    numbers; detectors also before percent-decoding; the " at " e-mail rule narrowed to e-mail shape
# 2026-11-08.15 (review round 15): STRUCTURAL: card / phone / SSN detectors always run on the raw text with no
#    exemption; exemptions only touch the generic rules; issuer-prefix check for cards; date + time is one date token
# 2026-11-08.14 (review round 14): dates are exempt only when they stand alone (never hide a card, phone or SSN);
#    year-only card windows, ZIP + year, spaced / compact / ranged dates, version numbers and decades kept
# 2026-11-08.13 (review round 13): one spelled word counts only as a whole word with 9+ real digits, after the
#    date and year-list exemptions (round 12 over-dropped year / date queries)
# 2026-11-08.12 (review round 12): filler digit runs on the homoglyph view; one spelled word enough in a 9+ digit
#    run; spelled digits looked for again with a repeated filler token or an interleaved character removed
# 2026-11-08.11 (review round 11): no length rule on joined letter-digit text (spec queries kept); 9+ digits across
#    filler tokens; homoglyph and enclosed digits; Spanish and mixed spelled digits; spaced / "dot" IPv4
# 2026-11-08.10 (review round 10): one shared character normalisation (clean_text) for the classifier and Priya's
#    key; the full word rules on secret-chunked queries with separators removed; URL-safe base64 decoded, base64
#    needs a tell or a sensitive decode; cards across up to three filler characters; spelled-out digit runs
# 2026-11-08.9 (review round 9): combining marks / format characters stripped before every check; ideographic and
#    fullwidth full stops; IPv6, hex IPv4, script schemes, spaced / "dot" hosts URL-like; split hex and base64 text;
#    cards split by single filler letters
# 2026-11-08.8 (review round 8): fail safe: URL-like queries dropped whole (no path exemptions); up to three
#    percent-decodes first; Luhn check over 13-19 digit runs; split secrets judged with separators removed
# 2026-11-08.7 (review round 7): the round-6 host exemption was too wide (secrets in paths passed): now only plain
#    slugs and short version tokens are exempt; path pieces, query-string values and known secret prefixes are judged
#    by the token rules
# 2026-11-08.6 (review round 6): a host name with short slug segments is not a "/" secret (URL-path queries kept)
# 2026-11-08.5 (review round 5): UK postcodes need a real area and a non-ordinal, non-unit inward part; French place /
#    route need an article; old / new / good alone as a street name only at the end; base64 / AWS-like words with "/"
# 2026-11-08.4 (review round 4): old / new / good allowed as street names; plaza; UK and Canadian postcodes; French
#    leading forms; the id / token word rule narrowed (model numbers, tire sizes, decimals, years are not identifiers)
# 2026-11-08.3 (review round 3): ordinary-word suffixes, text after a strong-suffix address, more state codes, unit
#    words after a state + ZIP, PMB and rural routes; queries over MAX_QUERY_CHARS are pii_suspect
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
FILLER_DIGIT_RUN = 9           # round 11: SSN (9) and phone (10) digits across short filler tokens
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
    count. A phone number holding one such group (``555 010 2024``) is still caught. Calendar dates that stand alone
    (``2024-10-24``, ``11/08/2025``; ``isolated_dates_removed``) are taken out first and break a run; a run of two
    groups, a year and a number of at most five digits (``90210 2024``), counts the number only. GENERIC rule:
    since round 15 a real SSN regrouped 5 + 4 (``12345 2019``) is the SSN detector's, which no exemption reaches.
    Round 18: a number of one or two digits glued to a letter at either END of a run of two or more groups is part of
    that word (a model name or a unit: ``ddr5 6400 64gb`` counts 4); a lone group always counts. The detectors are not affected."""
    best = 0
    s = isolated_dates_removed(text)
    for m in _DIGIT_RUN.finditer(s):
        groups = re.findall(r"\d+", m.group())
        if len(groups) > 1 and len(groups[-1]) <= 2 and s[m.end():m.end() + 1].isalpha():
            groups = groups[:-1]
        if len(groups) > 1 and len(groups[0]) <= 2 and s[max(m.start() - 1, 0):m.start()].isalpha():
            groups = groups[1:]
        years = [g for g in groups if _YEAR.fullmatch(g)]
        n = sum(len(g) for g in groups if len(years) < 2 or not _YEAR.fullmatch(g))
        if len(groups) == 2 and len(years) == 1 and max(len(g) for g in groups if not _YEAR.fullmatch(g)) <= 5:
            n -= 4                       # ZIP + year: a GENERIC exemption only; the SSN detector is never exempted
        best = max(best, n)
    return best


# Address rule (review rounds 2 and 3): an address needs address STRUCTURE, because this classifier also splits
# Osei's audit queries and service businesses' ordinary queries carry numbers ("24 hour emergency plumber near dr",
# "2 lane road", "ac unit 12000 btu", "ohio 45000 jobs"). The parts:
#   - a house number (at the start, or after a space not preceded by "top" / "best"), an optional direction, 1-3
#     street-name tokens (none a stop or quantity word: "near", "in", "best", "hour", "bedroom", "lane", "way" ...),
#     then a suffix;
#   - STRONG suffixes (abbreviations that are not English words, and street / avenue / boulevard): anything may
#     follow ("123 main st 2nd floor", "123 main st springfield il 62701 hours");
#   - ORDINARY-WORD suffixes (road, lane, drive, court, way, terrace, place, loop): only up to three place words, an
#     optional state, an optional ZIP and, after a ZIP, anything, to the end of the query ("12 kings road chelsea");
#   - "apt / unit / suite / # N" only after such a street, never alone;
#   - state + ZIP: a state name or a state code that is not one of the English words in, or, me, ok (so "portland or
#     97201" is NOT caught), a 5-digit ZIP, and not followed by a unit word (btu, miles, jobs, sq, zip ...);
#   - PO boxes, PMB numbers, rural routes ("rr 2 box 15", "hc 1 box 9") and ZIP+4 anywhere.
#   - outside the US: UK postcodes (a real postcode area, a space, an inward part that is not an ordinal or a size
#     unit), Canadian postal codes, French / Quebec leading forms ("350 rue sherbrooke"; place / route / avenue /
#     boulevard only with a French article);
#   - "old" / "new" / "good" may start a street name ("17 old mill road"); alone as the name ("12 new street") the
#     suffix must end the query or be followed only by a unit, state and ZIP.
# Known gaps (disclosed in ADR 0047 and ADR 0017 GSC-12): the rule is US-centric (other countries' formats are not
# covered); bare named streets with no suffix ("1200 broadway", "123 n main"), landmark
# or building-name addresses, addresses with more than three street-name tokens or an unlisted suffix ("10 best
# hiking trail" is kept on purpose), "portland or 97201". Known over-drops: a non-address shaped like
# "<number> <word> st|ave|dr|ct|pl ..." or "<number> <word> court|place|way ..." at the end of a query
# ("5 food court ideas").
_STRONG_SUFFIX = (r"street|avenue|boulevard|st|ave|av|rd|blvd|ln|dr|ct|pl|ter|cir|hwy|pkwy|trl|cres|aly|plz|rte|fwy|"
                  r"expy|tpke")
_ORDINARY_SUFFIX = r"road|lane|drive|court|way|terrace|place|loop|plaza"
_STREET_SUFFIX = _STRONG_SUFFIX + "|" + _ORDINARY_SUFFIX
_NOT_NAME = (r"a|an|the|of|for|in|on|at|to|and|or|my|your|with|near|by|from|best|top|cheap|cheapest|"
             r"hour|hours|hr|hrs|day|days|week|weeks|month|months|year|years|minute|minutes|bedroom|bedrooms|bed|"
             r"beds|bath|baths|car|cars|star|stars|person|people|room|rooms|story|stories|way|ways|things|tips|step|"
             r"steps|lane|lanes|door|doors|inch|inches|ft|foot|feet|mile|miles|unit|units|flat|apt|suite|ste|places|"
             r"minute|easy|simple|quick|great|ton|amp|gallon|season|stroke|percent|sq|ct|pl|ln|rd|dr|st|old|new|good")
_ADJ = r"(?:old|new|good)"        # a street name may START with one of these, but never be only one (see below)
_STATE_CODES = (r"al|ak|az|ar|ca|co|ct|de|dc|fl|ga|hi|id|il|ia|ks|ky|la|ma|md|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|"
                r"nd|oh|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy")          # in, or, me, ok left out: English words
_STATE_NAMES = (r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
                r"idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
                r"minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|"
                r"north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
                r"south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming")
_STATE = r"(?:" + _STATE_CODES + "|" + _STATE_NAMES + r")"
_DASH = r"[-‐-―−]"
_ZIP = r"\d{5}(?:\s*" + _DASH + r"\s*\d{4})?"
_UNIT_WORDS = (r"btu|btus|miles|mile|mi|km|kms|dollars|dollar|usd|jobs|job|sq|sqft|ft|feet|watts|watt|lumens|rpm|"
               r"lbs|lb|pounds|people|units|gallons|gal|views|followers|zip|code|codes|population|calories|psi|cfm")
_NAME_TOKEN = r"(?!(?:" + _NOT_NAME + r")(?![\w'.-]))(?:[a-z][a-z'.-]*|\d{1,4}(?:st|nd|rd|th))"
_DIRECTION = r"(?:n|s|e|w|ne|nw|se|sw|north|south|east|west)\.?"
_NUMBER_HEAD = r"(?:^|(?<!\btop)(?<!\bbest)\s)\d{1,6}[a-z]?\s+(?:" + _DIRECTION + r"\s+)?"
_STREET_HEAD = _NUMBER_HEAD + r"(?:" + _ADJ + r"\s+)?(?:" + _NAME_TOKEN + r"\s+){1,3}"     # 17 old mill road
_UNIT = r"(?:\s*,?\s*(?:apt|apartment|suite|ste|unit|#)\.?\s*#?\s*\d+[a-z]?)"
_UK_AREAS = (r"ab|al|ba|bb|bd|bh|bl|bn|br|bs|bt|b|ca|cb|cf|ch|cm|co|cr|ct|cv|cw|da|dd|de|dg|dh|dl|dn|dt|dy|ec|eh|en|"
             r"ex|e|fk|fy|gl|gu|gy|g|ha|hd|hg|hp|hr|hs|hu|hx|ig|im|ip|iv|je|ka|kt|kw|ky|la|ld|le|ll|ln|ls|lu|l|me|mk|"
             r"ml|m|ne|ng|nn|np|nr|nw|n|ol|ox|pa|pe|ph|pl|po|pr|rg|rh|rm|sa|se|sg|sk|sl|sm|sn|so|sp|sr|ss|st|sw|sy|s|"
             r"ta|td|tf|tn|tq|tr|ts|tw|ub|wa|wc|wd|wf|wn|wr|ws|wv|w|yo|ze")
# an inward part that is really an ordinal or a size unit: "x5 3rd row", "m4 1tb", "s9 2nd hand"
_UK_NOT_INWARD = r"(?:st|nd|rd|th|tb|gb|kb|pb|hp|lb|ft|hz|dr|bd)"
_ADDRESS = (
    # strong suffix, anything after: 123 main st springfield il 62701 hours | 55 water st 12th floor | 9 n elm blvd.
    re.compile(_STREET_HEAD + r"(?:" + _STRONG_SUFFIX + r")\.?(?=[\s,#]|$)"),
    # ordinary-word suffix, then only place words / state / ZIP to the end (or anything after a ZIP)
    # only "old" / "new" / "good" as the name ("12 new street"): the suffix must end the query, or be followed only by
    # a unit, a state and a ZIP ("3 new dr pepper flavors", "2 old court cases", "10 good place to eat" are kept)
    re.compile(_NUMBER_HEAD + _ADJ + r"\s+(?:" + _STREET_SUFFIX + r")\.?(?:\s+" + _DIRECTION + r")?" + _UNIT
               + r"?(?:\s*,?\s*" + _STATE + r")?(?:\s*,?\s*" + _ZIP + r")?\s*$"),
    re.compile(_STREET_HEAD + r"(?:" + _ORDINARY_SUFFIX + r")(?:\s+" + _DIRECTION + r")?" + _UNIT + r"?"
               r"(?:\s*,?\s*[a-z]+){0,3}(?:\s*,?\s*" + _STATE + r")?(?:\s*,?\s*" + _ZIP + r"(?!\d)(?:\s.*)?)?\s*$"),
    # round 18 (an address inside surrounding text): the same street, then up to three tokens of any kind to the end
    # ("12 kings road x2", "17 elm way tonight"); the text is ``_address_core`` (punctuation, emoji, CJK set aside)
    re.compile(_STREET_HEAD + r"(?:" + _ORDINARY_SUFFIX + r")(?:\s+" + _DIRECTION + r")?" + _UNIT + r"?"
               r"(?:\s+[a-z0-9]+){0,3}\s*$"),
    re.compile(r"(?<!\w)p\.?\s*o\.?\s*box\s*#?\s*\d+"),                                         # po box 1234
    re.compile(r"(?<!\w)pmb\s*#?\s*\d+"),                                                       # pmb 123
    re.compile(r"(?<!\w)(?:rr|hc|rural route)\s*#?\s*\d+\s*,?\s*box\s*#?\s*\d+"),               # rr 2 box 15
    re.compile(r"(?<!\d)\d{5}\s*" + _DASH + r"\s*\d{4}(?!\d)"),                                  # zip+4: 90210-1234
    # UK postcode: a real postcode area (the ~120 areas), the district, a space, the inward part (a digit and two
    # letters, never c i k m o v, and not an ordinal or size unit): sw1a 1aa, ox1 4ag, m1 1ae
    re.compile(r"(?<![a-z0-9])(?:" + _UK_AREAS + r")\d[a-z0-9]?\s+\d(?!" + _UK_NOT_INWARD
               + r"(?![a-z0-9]))[abd-hjlnp-uw-z]{2}(?![a-z0-9])"),
    # Canadian postal code (letters exclude d f i o q u; w z not first): m5v 3l9, m5h 2n2
    re.compile(r"(?<![a-z0-9])[abceghj-nprstvxy]\d[abceghj-nprstv-z]\s?\d[abceghj-nprstv-z]\d(?![a-z0-9])"),
    # French / Quebec leading forms: 350 rue sherbrooke, 12 chemin du lac, 4 bis rue de la paix
    re.compile(r"(?:^|(?<!\btop)(?<!\bbest)\s)\d{1,5}(?:\s*(?:bis|ter))?\s*,?\s+(?:"
               r"(?:rue|chemin|impasse|allee|all\u00e9e|quai)\s+(?:de\s+la\s+|du\s+|des\s+|de\s+|d'|l')?"
               # place / route / avenue / boulevard only with a French article: "6 place setting", "5 route options"
               r"|(?:place|route|avenue|av|boulevard|bd)\s+(?:de\s+la\s+|du\s+|des\s+|de\s+|d'))[a-z][a-z'.-]+"),
    re.compile(r"(?:^|[\s,])" + _STATE + r"\.?\s*,?\s*\d{5}(?!\d)(?!\s*(?:" + _UNIT_WORDS + r")(?!\w))"),  # boise id 83702
)
MAX_QUERY_CHARS = 512        # longer text is not classified (it is pii_suspect): bounds the regex work per query


_ADDR_TOKEN_EDGE = re.compile(r"^[^a-z0-9#]+|[^a-z0-9]+$")


def _address_core(folded: str) -> str:
    """Round 18: the text with every token that holds no ASCII letter or digit (emoji, CJK, punctuation) left out and
    punctuation trimmed from the ends of the others, so surrounding text of another script or symbols never stops the
    address rule ("17 elm way \u3067\u3059", "\u300c 62 oak lane, \U0001f600")."""
    out = []
    for tok in folded.split():
        tok = _ADDR_TOKEN_EDGE.sub("", tok)
        if any(c.isascii() and c.isalnum() for c in tok):
            out.append(tok)
    return " ".join(out)


def looks_like_address(folded: str) -> bool:
    """A postal address or part of one, on the lookalike-folded text (rules and their deliberate gaps above). Personal
    names are NOT detected. Text over ``MAX_QUERY_CHARS`` counts as one (it is not examined)."""
    t = folded.strip()
    if len(t) > MAX_QUERY_CHARS:
        return True
    return any(p.search(t) for p in _ADDRESS)


_JWT = re.compile(r"[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}")
_SLASH_TOKEN = re.compile(r"[a-z0-9_\-=+.~/]{12,}")
# known secret prefixes (case-folded): AWS access key ids, Google API keys, GitHub / Slack / Stripe-style tokens
_SECRET_PREFIX = re.compile(r"(?:akia|asia)[a-z0-9]{12,}|aiza[a-z0-9_-]{20,}|(?:ghp|gho|ghu|ghs|github_pat|xox[abprs]|"
                            r"sk_live|sk_test|rk_live|pk_live)[_-][a-z0-9_-]{8,}")
# a query-string parameter whose value is a credential: token=, key=, password= ... with 6+ characters
_SECRET_PARAM = re.compile(r"(?:^|[?&;#/])(?:[a-z_]*token|[a-z_]*key|secret|password|passwd|pwd|pass|auth|sig|"
                           r"signature|session|sessionid|sid|code|otp)=[^&#\s]{6,}")
_URL_SPLIT = re.compile(r"[/?&=#;:]")


def _secret_piece(piece: str) -> bool:
    """One piece of a URL-ish word (split at / ? & = # ; :) judged by the token rules."""
    if not piece:
        return False
    if _SECRET_PREFIX.match(piece) or logs_mod._HEX.fullmatch(piece) or logs_mod._TOKENISH.fullmatch(piece):
        return True
    letters, digits = sum(c.isalpha() for c in piece), sum(c.isdigit() for c in piece)
    if letters and digits >= 2:
        if len(piece) >= 16:
            return True
        if len(piece) >= 8 and _transitions(piece) >= 0.6 * (len(piece) - 1):
            return True
    return False


def _slash_secret(w: str) -> bool:
    """A base64 / AWS-secret-like word holding "/", "+" or "=" (review round 5): 20+ such characters with a digit, a
    "+" or an "=" (``wjalr.../k7mdeng/...``, ``dghpcy9pcy9h/c2vjcmv0ignszxk=``), or 12+ with two of "/", "+", "="
    (``ywjj/zgvm+z2hp``). No exemption (review round 8: URL-like queries are dropped before this, see
    ``url_like``); a short tire size (``225/65r17``) is under the length thresholds. Not caught: base64 under 12
    characters."""
    if not _SLASH_TOKEN.fullmatch(w) or not any(c in w for c in "/+="):
        return False
    marks = sum(1 for c in "/+=" if c in w)
    return (len(w) >= 20 and (any(c.isdigit() for c in w) or "+" in w or "=" in w)) or marks >= 2
_ALNUM_TOKEN = re.compile(r"[a-z0-9_\-=+~]+")


def _transitions(w: str) -> int:
    """How often a word switches between letters and digits (``a1b2c3`` 5, ``24acc636a003`` 5, ``f150`` 1)."""
    kinds = [c.isdigit() for c in w if c.isalnum()]
    return sum(1 for x, y in itertools.pairwise(kinds) if x != y)


def _word_identifies(w: str) -> bool:
    """A word that looks like an identifier or a secret (review round 4: narrowed from the log templater's rule, which
    also dropped model numbers, tire sizes, decimals and years such as ``24acc636a003``, ``225/65r17``, ``3.5``,
    ``2012``). Kept as identifying: an e-mail in any spelling; a UUID; 16+ hex characters; a JWT-like dotted token; 20+
    token characters; anything over 40 characters; a letters-and-digits word of 16+ characters; a letters-and-digits
    word of 8+ characters that switches between letters and digits at least 60% of the time (``a1b2c3d4e5f6g7``); a
    file name (a stem with a letter and a 2-5 letter extension, not a code extension: ``jane_cv.pdf``). Digit runs
    (phone numbers, SSNs, card and account numbers) are the digit-run rule's (7+ digits in one run)."""
    kind = logs_mod._kind(w)
    if kind == "{email}" or kind == "{uuid}":
        return True
    if logs_mod._HEX.fullmatch(w) or _JWT.search(w) or len(w) > 40 or logs_mod._TOKENISH.fullmatch(w) \
            or _slash_secret(w) or _SECRET_PREFIX.match(w) or _SECRET_PARAM.search(w):
        return True
    if _URL_SPLIT.search(w) and any(_secret_piece(x) for x in _URL_SPLIT.split(w)):
        return True                      # a secret inside a path or a query string (evil.com/akia..., ?token=...)
    m = logs_mod._FILE.fullmatch(w)
    if m and m.group(2).isalpha() and len(m.group(2)) >= 2 and any(c.isalpha() for c in m.group(1)) \
            and m.group(2).lower() not in logs_mod.STATIC_EXT:
        return True
    letters, digits = sum(c.isalpha() for c in w), sum(c.isdigit() for c in w)
    if letters and digits >= 2 and _ALNUM_TOKEN.fullmatch(w):
        if len(w) >= 16:
            return True
        if len(w) >= 8 and _transitions(w) >= 0.6 * (len(w) - 1):
            return True
    return False


# ---------------------------------------------------------------- review round 8: fail safe on URL-like text
# Path exemptions were an arms race (rounds 6-7), so a query that looks like a URL is dropped whole (URL-shaped search
# queries are rare and of little value in a keyword ledger). URL-like, after up to three percent-decodes, NFKC and the
# lookalike fold: a scheme or "://"; "www."; a percent escape still left; an IPv4 address; a host name (label.tld,
# the gTLDs below or any two-letter country code except file-type words such as js / py / md); host:port; a word
# holding "/" other than a fraction (1/2, 24/7), a tire size (225/65r17) or one of a few word pairs (a/b, and/or).
_GTLDS = (r"com|net|org|info|biz|edu|gov|mil|int|io|ai|app|dev|xyz|online|site|shop|store|tech|blog|cloud|page|"
          r"link|live|news|club|top|pro|art|design|agency|digital|media|world|life|space|website|email|group|"
          r"services|solutions|company|business|center|network|systems|today|tips|guru|ninja|rocks|zone|global|"
          r"local|nyc|london|berlin|museum|travel|jobs|mobi|name|asia|aero|coop|tel|cat|post|xxx")
_NOT_TLD = r"js|ts|py|rb|md|sh|gz|cs|db|ps|vb|rs|go|pl1|jsx|tsx"
_HOSTNAME = re.compile(r"(?<![\w@-])(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+(?:" + _GTLDS + r"|(?!(?:" + _NOT_TLD
                       + r")(?![a-z]))[a-z]{2})\.?(?![a-z0-9-])")
_URL_MARKS = re.compile(r"[a-z][a-z0-9+.-]{0,15}://|://|(?<![a-z0-9])www\.|%[0-9a-f]{2}|"
                        r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])|[a-z0-9]\.[a-z]{2,24}:\d{2,5}(?!\d)|"
                        # round 9: script-ish schemes, hex IPv4 (0xc0a80101), IPv6 literals (2001:db8::1, fe80::1)
                        r"(?<![a-z])(?:javascript|vbscript|data|file|blob|mailto|ftp|sftp|ssh)\s*:|"
                        r"(?<![0-9a-z])0x[0-9a-f]{8}(?![0-9a-z])|"
                        r"(?<![0-9a-z:])(?:[0-9a-f]{1,4}:){1,7}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?(?![0-9a-z:])|"
                        r"(?<![0-9a-z:])(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}(?![0-9a-z:])|"
                        r"(?<![0-9a-z:])::(?:ffff:)?[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6}(?![0-9a-z:])")
# round 9: a host written with spaces or "dot" ("example . com", "example dot com"), common TLDs only; the label
# may not be a stop word ("the dot com bubble" is kept)
# round 11: an IPv4 address written with spaces or "dot" ("192 . 0 . 2 . 1", "192 dot 0 dot 2 dot 1")
_SPACED_IP = re.compile(r"(?<![\d.])\d{1,3}(?:\s*(?:\.|\(\.\)|\[\.\]|\(dot\)|\[dot\]|\bdot\b|\bpunto\b)\s*\d{1,3}){3}"
                        r"(?![\d.])")
_SPACED_HOST = re.compile(r"(?<![a-z0-9-])(?!(?:the|a|an|of|in|on|at|to|and|or|is)\s)[a-z0-9][a-z0-9-]{0,62}"
                          r"\s*(?:\.|\(\.\)|\[\.\]|\s\(?dot\)?|\[dot\])\s*"
                          r"(?:com|net|org|io|co|uk|us|ca|de|fr|au|info|biz|edu|gov|app|dev|ai)(?![a-z0-9])")
_SLASH_OK = re.compile(r"\d+(?:/\d+)+|\d{3}/\d{2}[a-z]{0,2}\d{2}|a/b|and/or|w/o|n/a|on/off|yes/no|his/her|he/she|"
                       r"s/he|b/w|i/o|a/c|ac/dc|w/|p/e|r/c|f/x")
URL_LIKE_RULE = ("scheme or '://', 'www.', a percent escape left after three decodes, an IPv4 address, a host name "
                 "(label.tld), host:port, or a word holding '/' that is not a fraction, a tire size or a listed word "
                 "pair (a/b, and/or, w/o, n/a ...)")


# review round 9: marks and format characters that can hide a digit or letter from every rule ("5\u0301 5 5",
# "4\u20e3", "1\u0332"): nonspacing (Mn) and enclosing (Me) marks, format characters (Cf), variation selectors
_DOTS = str.maketrans({"\u3002": ".", "\uff61": ".", "\uff0e": ".", "\u2024": ".", "\ufe52": "."})


_SPACE_CATS = frozenset(("Cc", "Co", "Cs", "Cn", "Zs", "Zl", "Zp"))


def clean_text(t: str, keep_script_marks: bool) -> str:
    """The ONE character normalisation shared by the classifier and Priya's stored key (review round 10). NFKC;
    format characters (Cf) and default-ignorable code points deleted; variation selectors deleted; control (Cc),
    private-use (Co), surrogate, unassigned and every space or line / paragraph separator (Zs, Zl, Zp: U+00A0,
    U+2007, U+202F ...) made one ASCII space; ideographic / fullwidth full stops made '.'. Combining marks (Mn, Me):
    in the detection form (``keep_script_marks=False``) the text is NFKD-decomposed first and every mark removed
    (``Á`` is judged as ``A``) and other-script decimal digits are made ASCII; in the key form only marks sitting on an
    ASCII character are removed, so script marks (Devanagari, Thai, Hebrew points) and NFKC-composed letters
    (``café``) are kept."""
    t = unicodedata.normalize("NFKD" if not keep_script_marks else "NFKC", t or "")
    t = lookalikes.strip_invisible(t)
    out, base_ascii = [], False
    for c in t:
        cat = unicodedata.category(c)
        if "︀" <= c <= "️" or "\U000e0100" <= c <= "\U000e01ef" or cat == "Cf":
            continue
        if cat in _SPACE_CATS:
            out.append(" ")
            base_ascii = True
            continue
        if cat in ("Mn", "Me"):
            if not keep_script_marks or base_ascii:
                continue
            out.append(c)
            continue
        base_ascii = ord(c) < 128
        if not keep_script_marks and cat == "Nd" and not c.isascii():
            c = str(unicodedata.digit(c))          # detection form: other-script digits (٤, ४) judged as ASCII
        elif not keep_script_marks and cat == "No":
            v = unicodedata.numeric(c, None)       # round 11: dingbat / negative circled / enclosed numbers (❶, ➀)
            if v is not None and float(v).is_integer() and 0 <= v <= 20:
                c = str(int(v))
        out.append(c)
    return unicodedata.normalize("NFKC", "".join(out)).translate(_DOTS)


def strip_marks(t: str) -> str:
    """The detection form of ``t`` (``clean_text`` without script marks). Never stored."""
    return clean_text(t, keep_script_marks=False)


def decode_query(q: str) -> str:
    """Up to three percent-decodes (a double-encoded secret is decoded too), then the detection form
    (``clean_text``)."""
    t = q or ""
    for _ in range(3):
        u = unquote(t)
        if u == t:
            break
        t = u
    return strip_marks(t)


def url_like(folded: str) -> bool:
    """Whether decoded, folded query text looks like a URL (dropped whole; review round 8)."""
    if _URL_MARKS.search(folded) or _HOSTNAME.search(folded) or _SPACED_HOST.search(folded) \
            or _SPACED_IP.search(folded):
        return True
    return any("/" in w and not _SLASH_OK.fullmatch(w) and not _date_range(w) for w in folded.split())


def _date_range(w: str) -> bool:
    """A word that is only calendar dates joined by '/' or '-' ("2025-11-08/2025-11-09"; round 14)."""
    rest = w
    for a, b in reversed(date_spans(w)):
        rest = rest[:a] + rest[b:]
    return bool(date_spans(w)) and re.fullmatch(r"[/\-]*", rest) is not None


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


# digit groups joined by separators and at most one short filler token of up to three characters between them
# ("4539 x 1488", "4111-ab-1111", "4111 xy 1111"; round 10)
_GSEP = r"[\s\-/._,:;()\[\]]"
_GFILL = r"[^\d\s\-/._,:;()\[\]]{1,3}"
# between two digit groups: separators, and up to three filler tokens of 1-3 characters, each set apart by a separator
# (so a word of four letters such as "ford" never counts as filler)
_DIGIT_GROUPS = re.compile(r"\d+(?:" + _GSEP + r"{0,3}(?:" + _GFILL + r"(?:" + _GSEP + r"{1,3}" + _GFILL + r"){0,2})?"
                           + _GSEP + r"{0,3}\d+)*")
# the stricter form for the 9+ digit rule (no Luhn check behind it): filler tokens must be set apart from the digits
# by separators, so a unit glued to a number ("6000mhz cl30") never joins two numbers
_DIGIT_GROUPS_STRICT = re.compile(r"\d+(?:(?:" + _GSEP + r"{1,3}" + _GFILL + r"(?:" + _GSEP + r"{1,3}" + _GFILL
                                  + r"){0,2}" + _GSEP + r"{1,3}|" + _GSEP + r"{0,3})\d+)*")
_NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9",
                 # round 11: Spanish
                 "cero": "0", "uno": "1", "una": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
                 "seis": "6", "siete": "7", "ocho": "8", "nueve": "9"}
_SPELLED = re.compile(r"(?:(?:\b(?:" + "|".join(_NUMBER_WORDS) + r")\b|\d+|\b[lioszb|]\b)[\s\-,.()\[\]]*"
                      r"(?:(?!(?:" + "|".join(_NUMBER_WORDS) + r")\b)[a-z]{1,3}\b[\s\-,.()\[\]]*)?){7,}")
_HOMO1 = {"l": "1", "i": "1", "|": "1", "o": "0", "s": "5", "b": "8", "z": "2"}
# number words run together once every separator is gone ("f i v e f i v e ...", reversed too; round 11)
_NUMWORD_CONCAT = re.compile(r"(?:" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r"|\d){7,}")


_MON = r"(?:0?[1-9]|1[0-2])"
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_Y4 = r"(?:19|20)\d\d"


def _date_alternatives() -> str:
    """Calendar dates in every common order, each with ONE separator used twice (/ . - or a space), months and days
    in range: 2025/11/08, 11/08/2025, 08.11.2025, 12-25-2024, 2025 01 01; and compact 20251108 (round 14)."""
    alts = []
    for sep in ("/", ".", "-", " "):
        e = re.escape(sep)
        alts += [_Y4 + e + _MON + e + _DAY, _MON + e + _DAY + e + _Y4, _DAY + e + _MON + e + _Y4]
    alts.append(_Y4 + r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
    return "|".join(f"(?:{x})" for x in alts)


_SLASH_DATE = re.compile(r"(?<!\d)(?:" + _date_alternatives() + r")(?!\d)")
_DATE_NEIGHBOURHOOD = 12      # characters either side within which another digit makes a date part of a longer run


_TIME_AFTER = re.compile(r"(?:t|\s+(?:at\s+|@\s*)?)(?:(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\.\d+)?"
                         r"(?:\s*(?:am|pm|a\.m\.|p\.m\.))?|(?:1[0-2]|0?[1-9])\s*(?:am|pm|a\.m\.|p\.m\.))"
                         r"(?:\s*(?:z|utc|gmt|[+-]\d\d:?\d\d))?(?![a-z\d])")
_TIME_ALONE = re.compile(r"(?<!\d)(?:(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s*(?:am|pm|a\.m\.|p\.m\.))?|"
                         r"(?:1[0-2]|0?[1-9])\s*(?:am|pm|a\.m\.|p\.m\.))(?![a-z\d])")


def date_spans(text: str) -> list:
    """Calendar dates, each with a time right after it (``2025-11-08 10am``, ``2025-11-08T09:30``, ``11/10/2025
    3:30``; round 15), and times standing on their own (``7pm``, ``09:30``): one date token for the GENERIC rules."""
    spans = [m.span() for m in _ISO_DATE.finditer(text)]
    spans += [m.span() for m in _SLASH_DATE.finditer(text) if not any(a <= m.start() < b for a, b in spans)]
    out = []
    for a, b in sorted(spans):
        t = _TIME_AFTER.match(text, b)
        out.append((a, t.end() if t else b))
    out += [m.span() for m in _TIME_ALONE.finditer(text) if not any(a <= m.start() < b for a, b in out)]
    return sorted(out)


def isolated_dates_removed(text: str) -> str:
    """Round 14: a calendar date is taken out of the digit rules ONLY when it stands alone — every digit within
    ``_DATE_NEIGHBOURHOOD`` characters on either side belongs to a date too (``2025-01-01 2025-12-31 hotel``). A date
    with other digits next to it is left in, so it can never hide a card, phone or SSN (``22230 03/12/2003 222``,
    ``2025/5/5 0199``, ``2 1909/9/9 99``)."""
    spans = date_spans(text)
    if not spans:
        return text
    in_date = [False] * len(text)
    for a, b in spans:
        for i in range(a, b):
            in_date[i] = True
    out = list(text)
    for a, b in spans:
        lo, hi = max(0, a - _DATE_NEIGHBOURHOOD), min(len(text), b + _DATE_NEIGHBOURHOOD)
        if all(in_date[i] or not text[i].isdigit() for i in range(lo, hi)):
            for i in range(a, b):
                out[i] = " " if i > a else "#"
    return "".join(out)

_YEAR_WORD = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")


def _without_dates_and_years(folded: str) -> str:
    """Round 13: the same exemptions the other digit rules use — ISO and slash / dot / dash dates removed, and
    year-like numbers (1900-2099) removed when there are two or more of them (``2023 2024``, ``2024-2025``)."""
    t = isolated_dates_removed(folded)                 # round 14: only dates that stand alone
    if len(_YEAR_WORD.findall(t)) >= 2:
        t = _YEAR_WORD.sub(" # ", t)
    return t


def spelled_digits(folded: str) -> str:
    """Runs of 7+ tokens that are spelled-out digits (English or Spanish), digits or digit homoglyphs, as digits
    ("four one one one ..." -> "4111...", "4 one 1 one ..."; rounds 10-13), else "". A run needs two spelled number
    words; ONE is enough only when it is a whole word in the query (never part of another word, never found in
    run-together or reversed text) and the run holds 9+ digits (digit characters, or single homoglyph letters such
    as ``l``) once dates and year lists are taken out (round 13: ``iphone 2023 2024`` and ``2024-12-25 hotel deals``
    were dropped by round 12's token count)."""
    out = []
    text = _without_dates_and_years(folded)

    def enough(toks, whole_words: bool) -> bool:
        words = sum(1 for t in toks if t in _NUMBER_WORDS)
        if words >= 2:
            return True
        digits = sum(len(t) for t in toks if t.isdigit()) + sum(1 for t in toks if t in _HOMO1)
        return whole_words and words == 1 and digits >= 9
    for m in _SPELLED.finditer(text):
        toks = [t for t in re.findall(r"[a-z]+|\d+|\|", m.group())
                if t in _NUMBER_WORDS or t.isdigit() or t in _HOMO1]           # filler tokens dropped
        if enough(toks, whole_words=True):
            out.append("".join(_NUMBER_WORDS.get(t) or _HOMO1.get(t, t) for t in toks))
    for view in _defilled_views(text):
        squashed = re.sub(r"[^a-z0-9]", "", view)
        for joined in (squashed, squashed[::-1]):
            for m in _NUMWORD_CONCAT.finditer(joined):
                toks = re.findall("|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r"|\d", m.group())
                if enough(toks, whole_words=False):
                    out.append("".join(_NUMBER_WORDS.get(t, t) for t in toks))
    return " ".join(out)


def _defilled_views(folded: str) -> list:
    """The query as it is, and (round 12) with a repeated filler removed: the most repeated short token that is not a
    number word or digits (" x ", "-ab-": "cuat x ro u x no" -> "cuat ro u no"), and the most frequent character when
    it makes up a quarter or more of the letters (interleaving: "fxoxuxrx" -> "four"). Used only to look for
    spelled-out digit runs, which need 7+ number tokens in a row."""
    views = [folded]
    toks = [t for t in re.split(r"[\s\-]+", folded) if t]
    short = [t for t in toks if len(t) <= 3 and t not in _NUMBER_WORDS and not t.isdigit()]
    if short:
        tok, n = max(((t, short.count(t)) for t in set(short)), key=lambda x: (x[1], x[0]))
        if n >= 3:
            views.append(" ".join(t for t in toks if t != tok))
    letters = [c for c in folded if c.isalpha()]
    if letters:
        ch, n = max(((c, letters.count(c)) for c in set(letters)), key=lambda x: (x[1], x[0]))
        if n >= 0.25 * len(letters) and n >= 6:
            views.append(folded.replace(ch, ""))

            def deinterleave(w: str) -> str:   # positional: "sxixxx" -> "six" (a global removal would give "si")
                for start in (1, 0):
                    if len(w) >= 3 and all(c == ch for c in w[start::2]):
                        return w[1 - start::2]
                return w.replace(ch, "")
            views.append(" ".join(deinterleave(w) for w in folded.split()))
    return views


# round 11: letters used as digits ("4lll llll", "SSS-OlO-Ol99"): l i | -> 1, o -> 0, s -> 5, b -> 8, z -> 2
_HOMOGLYPH_DIGITS = str.maketrans({"l": "1", "i": "1", "|": "1", "o": "0", "s": "5", "b": "8", "z": "2"})
_HOMOGLYPH_SET = frozenset("lio|sbz")


def homoglyph_digits(folded: str) -> str:
    """A view of the query with homoglyph letters read as digits, only inside digit-like words: a word with a real
    digit whose letters are all homoglyphs, and all-homoglyph words of 2+ characters next to such a word. Used only
    by the 9+ digit-run and Luhn rules, so ``lol 2024`` (7 digits) is not hit. Round 16: punctuation and symbols
    glued to a word ("(l", "\U0001f4del") are not part of it."""
    words = "".join(" " if unicodedata.category(c)[0] in "SZM" or (unicodedata.category(c)[0] == "P"
                                                                    and c not in "-._/|:") else c
                    for c in folded).split()

    def letters_ok(w):
        return all(not c.isalpha() or c in _HOMOGLYPH_SET for c in w if c not in "-._/")

    def plural(w):                         # round 14: "1960s", "90s", "2010s" are decades, not "19605"; round 15:
        return re.fullmatch(r"\d+'?s|(?:\d{1,2}:){1,2}\d{2}z?", w) is not None     # a time "10:30:00z" is not "002"
    mapped = [any(c.isdigit() for c in w) and letters_ok(w) and any(c in _HOMOGLYPH_SET for c in w) and not plural(w)
              for w in words]
    seed = [any(c.isdigit() for c in w) and letters_ok(w) and not plural(w) for w in words]

    def near_seed(i):                    # a seed next door, or one word further across a short filler word
        for j in (i - 1, i + 1):
            if 0 <= j < len(words) and seed[j]:
                return True
        for j, k in ((i - 2, i - 1), (i + 2, i + 1)):
            if 0 <= j < len(words) and seed[j] and len(words[k]) <= 3 and not seed[k]:
                return True
        return False
    changed = True
    while changed:
        changed = False
        for i, w in enumerate(words):
            if seed[i] or not all(c in _HOMOGLYPH_SET or c in "-._/" for c in w):
                continue
            if near_seed(i):
                seed[i] = mapped[i] = changed = True
    if not any(mapped):
        return ""
    return " ".join(w.translate(_HOMOGLYPH_DIGITS) if seed[i] else w for i, w in enumerate(words))


_FILLER_STOP = re.compile(r"\b(?:in|on|at|to|of|or|and|vs|by|for|the|is|are|per|than|from|an|a|with|into|via|"
                          r"y|o|en|de|con|por|para|km|mi|ft|lb|kg|mph|usd)\b")


def filler_digit_run(folded: str) -> int:
    """The most digits in consecutive digit groups once separators and short filler tokens (up to three characters)
    between them are removed ("123 x 45 x 6789", "1234 -ab- 5678 -ab- 9"; round 11); ordinary short words and units
    (``in``, ``to``, ``vs``, ``km`` ...) never join two numbers ("cars under 30000 in 50000 miles" is kept), and
    year-like groups do not count when there are two or more of them, as in ``longest_digit_run``."""
    best = 0
    # ordinary words never join numbers; dates are NOT taken out here (round 14): the caller passes the text with
    # only the dates that stand alone removed, so a date can never hide a phone or SSN next to it
    text = _FILLER_STOP.sub(" #### ", folded)
    for m in _DIGIT_GROUPS_STRICT.finditer(text):
        groups = re.findall(r"\d+", m.group())
        years = [g for g in groups if _YEAR.fullmatch(g)]
        n = sum(len(g) for g in groups if len(years) < 2 or not _YEAR.fullmatch(g))
        if len(groups) == 2 and len(years) == 1 and max(len(g) for g in groups if not _YEAR.fullmatch(g)) <= 5:
            n -= 4                       # ZIP + year: a GENERIC exemption only; the SSN detector is never exempted
        best = max(best, n)
    return best


def card_like(folded: str) -> bool:
    """A Luhn-valid run of 13-19 digits made of consecutive digit groups, whatever separates the groups (separators
    and at most one filler token of up to three characters: spaces, '-', '/', '.', ' x ', '-ab-') and whatever the
    groups look like (``4000 2019 2020 2021``, ``4539 x 1488 x 0343 x 6467``); every window of consecutive groups is
    tried. Round 15: the number must also start with a card network's issuer prefix (``card_iin``) — a property of
    card numbers, not an exemption — so ``2001 2002 2003 2004`` (no network issues 20xx numbers) is not a card while
    every test card of every network is. A DETECTOR (see ``pii_detectors``): it is never given exempted text."""
    for m in _DIGIT_GROUPS.finditer(folded):
        groups = re.findall(r"\d+", m.group())
        for i in range(len(groups)):
            d = ""
            for g in groups[i:]:
                d += g
                if len(d) > 19:
                    break
                if len(d) >= 13 and card_iin(d) and _luhn(d):
                    return True
    return False


# issuer prefixes (IIN ranges) of the card networks: Visa 4; Mastercard 51-55, 2221-2720; Amex 34, 37; Discover 6011,
# 644-649, 65; Diners 300-305, 36, 38, 39; JCB 3528-3589; UnionPay 62, 81; Maestro 50, 56-58, 6304, 67; Mir 2200-2204;
# RuPay 60, 65, 81, 82, 508; Verve 506, 650; Troy 9792
# round 18: Diners 36 was missing (the class read 3[4789]); the list's UATP prefix (1, 15 digits) is ``stream_card``'s
_IIN = re.compile(r"4|5[0-8]|2(?:2[2-9]|[3-6]\d|7[01])\d|2720|3[46789]|30[0-5]|35(?:2[89]|[3-8]\d)|6\d|8[12]|220[0-4]|"
                  r"9792")


def card_iin(digits: str) -> bool:
    return _IIN.match(digits) is not None


def _entropy(w: str) -> float:
    if not w:
        return 0.0
    n = len(w)
    return -sum((k / n) * math.log2(k / n) for k in (w.count(c) for c in set(w)))


def _strip_symbols(w: str) -> str:
    """Round 16: a word with every punctuation, symbol and mark character removed (by Unicode category), so a
    secret split by bullets, dashes or middle dots ("a\u2022k\u2022i\u2022a ...") is judged as one token. '=', '+'
    and '/' (base64) are symbols too: the base64 rules look at the cased text separately."""
    return "".join(c for c in w if unicodedata.category(c)[0] not in "PSM")


def split_secret(folded: str) -> bool:
    """A secret split by separators (rounds 8-10): each space-separated word with '/', '-', '_' and '.' removed is
    judged by the word rules (``abcd-1234-efgh-5678-ijkl-9012``); the whole query with spaces and separators removed
    by the prefix, alternation and hex rules always; and, when it is chunked like a secret (at least two words
    mixing letters and digits, more than half of all words), by an entropy check (16+ characters at 4.1+ bits) and
    an equal-length-chunks check (``ab12 cd34 ef56 gh78``). Round 11: the length rules (20+ token characters, 16+
    mixed) are NOT applied to the joined text: spec queries such as ``12v 100ah lifepo4 battery`` or ``r410a r22
    refrigerant`` join into long letter-digit strings and were dropped."""
    for w in folded.split():
        j = _strip_symbols(w)                    # round 16: every P / S / M character, by category
        if j != w and j and _word_identifies(j):
            return True
    whole = _strip_symbols(re.sub(r"\s", "", folded))
    if _SECRET_PREFIX.search(whole) or _SECRET_PREFIX.search(whole[::-1]):
        return True
    # round 11: a run of 12+ one-character words is a value spelled out character by character ("A K I A ...");
    # no ordinary query has one, so the joined run gets the FULL word rules, forwards and reversed
    for m in re.finditer(r"(?:(?<!\S)\S(?!\S)\s*){12,}", folded):
        run = re.sub(r"\s", "", m.group())
        if _word_identifies(run) or _word_identifies(run[::-1]):
            return True
    # round 11: base64 padding, case-folded or not ("amfuzs5kb2 vazxhhbxbs zs5jb20="): 12+ base64 characters with a
    # digit, ending in '=' or '=='
    if re.fullmatch(r"[a-z0-9+/]{12,}={1,2}", re.sub(r"\s", "", folded)) and any(c.isdigit() for c in whole):
        return True
    alnum = re.sub(r"[^a-z0-9]", "", whole)
    letters, digits = sum(c.isalpha() for c in alnum), sum(c.isdigit() for c in alnum)
    if letters and digits >= 4 and len(alnum) >= 12 and _transitions(alnum) >= 0.6 * (len(alnum) - 1):
        return True
    # round 9: hex split by spaces ("9f86d081 884c7d65 ..."): 16+ hex characters with 4+ digits and 4+ letters
    for m in re.finditer(r"[0-9a-f]{16,}", whole):
        h = m.group()
        if sum(c.isdigit() for c in h) >= 4 and sum(c.isalpha() for c in h) >= 4:
            return True
    words = folded.split()
    mixed = [w for w in words if any(c.isalpha() for c in w) and any(c.isdigit() for c in w)]
    if len(words) >= 2 and len(mixed) >= 2 and 2 * len(mixed) > len(words):
        # round 11: no length rule on the joined text (it dropped "12v 100ah lifepo4 battery"); only entropy, at a
        # bar ordinary spec queries stay under (they peak near 4.0 bits), and uniform secret-like chunks
        if len(alnum) >= 16 and _entropy(alnum) >= 4.1:
            return True
        if len(mixed) >= 3 and len(mixed) == len(words) and len({len(w) for w in words}) == 1 and len(words[0]) >= 4:
            return True                                  # "ab12 cd34 ef56 gh78": equal-length letter-digit chunks
    return False


def _b64_candidates(cased: str) -> list:
    """Spaceless candidates, case kept (round 10): the URL-safe alphabet mapped to the standard one BEFORE anything
    else ('-' to '+', '_' to '/'), and, separately, '-', '_' and '.' read as separators and removed."""
    spaceless = re.sub(r"\s", "", cased)
    return [spaceless.replace("-", "+").replace("_", "/"), re.sub(r"[\-_.]", "", spaceless)]


def base64_text(cased: str, _depth: int = 0) -> bool:
    """Base64 (standard or URL-safe), split or not, whose decoded bytes are 9+ and nearly all printable, AND that
    shows a base64 tell or decodes to something sensitive (round 10, against over-drop): a tell is '=' padding, a
    '+' or '/' inside, or a mixed-case-and-digit body of 16+ characters with entropy 3.5+ whose length is a multiple
    of 4; sensitive is decoded text the classifier itself calls personal data (one level deep). Mixed-case English
    ("PlayStation Plus Games") decodes to noise or shows no tell, so it is kept."""
    for whole in _b64_candidates(cased):
        if len(whole) < 12 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", whole):
            continue
        body = whole.rstrip("=")
        try:
            raw = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw) < 9 or sum(32 <= b < 127 for b in raw) < 0.95 * len(raw):
            continue
        tell = whole.endswith("=") or "+" in body or "/" in body or (
            len(body) >= 16 and len(body) % 4 == 0 and any(c.isdigit() for c in body)
            and any(c.isupper() for c in body) and any(c.islower() for c in body) and _entropy(body) >= 3.5)
        if tell:
            return True
        if _depth == 0 and classify_query(raw.decode("ascii", "replace"), [], _depth=1) == "pii_suspect":
            return True
    return False


# ---------------------------------------------------------------- round 15: structural detectors
# THE RULE (review round 15; ADR 0047): the card (Luhn 13-19 + issuer prefix), phone (NANP and E.164 shapes) and SSN
# detectors ALWAYS run on the raw normalised text (``folded``: decoded, cleaned, lookalike-folded) and on its
# de-separated digit runs, with NO exemption of any kind — no date, time, year, version, keyword or ZIP rule ever
# changes what they see. Exemptions (dates, times, year lists, version numbers, ZIP + year) may ONLY suppress the
# generic digit-run and token rules. Three rounds in a row an exemption reopened a leak; this split is structural.
_NANP = re.compile(r"(?<!\d)(?:\+?1[\s.\-]{0,3})?\(?[2-9]\d\d\)?[\s.\-]{0,3}\d{3}[\s.\-]{0,3}\d{4}(?!\d)")
_E164 = re.compile(r"\+\d(?:[\s.\-()]{0,2}\d){7,14}(?!\d)")
_SSN = re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}[\s.\-]{0,3}(?!00)\d{2}[\s.\-]{0,3}(?!0000)\d{4}(?!\d)")


def _valid_ssn(d: str) -> bool:
    return len(d) == 9 and d[:3] not in ("000", "666") and d[0] != "9" and d[3:5] != "00" and d[5:] != "0000"


def _valid_phone(d: str) -> bool:
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789"


def _explained_spans(text: str) -> list:
    """Dates (with a time after them), times and year-like numbers: digits that a date, time or year accounts for."""
    return date_spans(text) + [m.span() for m in _YEAR_WORD.finditer(text)]


def _deseparated_hit(text: str, explained_from: tuple | None = None) -> bool:
    """A phone or SSN whose digits are spread over consecutive digit groups ("2025/5/5 0100 2024", "2025 550100
    2024"): in every run of digit groups joined by separators or short fillers (ordinary words never join numbers),
    every window of whole consecutive groups is read as one number. Searched in every run that holds at least one
    digit group a date, time or year does not account for — a run of nothing but dates, times and years cannot
    spell a number someone chose. Never given exempted text. ``explained_from`` (raw text, view-to-raw index map)
    takes the dates, times and years from the RAW text when ``text`` is the separator view (round 16: "3:30" in the
    view is "3 30", which no time rule recognises)."""
    if explained_from is not None:
        raw, idx = explained_from
        raw_explained = [False] * len(raw)
        for a, b in _explained_spans(raw):
            for i in range(a, min(b, len(raw))):
                raw_explained[i] = True
        explained = [raw_explained[k] for k in idx]
    else:
        explained = [False] * len(text)
        for a, b in _explained_spans(text):
            for i in range(a, min(b, len(text))):
                explained[i] = True
    # ordinary words never join numbers: each becomes a " #### " barrier; the explained mask is spliced with it
    for m in reversed(list(_FILLER_STOP.finditer(text))):
        a, b = m.span()
        text = text[:a] + " #### " + text[b:]
        explained = explained[:a] + [False] * 6 + explained[b:]
    def in_letter_word(pos: int, n: int) -> bool:
        # a digit inside a mostly-letter word ("y2k", "mp3") is part of that word, not a number group
        a, b = pos, pos + n
        while a > 0 and not text[a - 1].isspace():
            a -= 1
        while b < len(text) and not text[b].isspace():
            b += 1
        w = text[a:b]
        letters = sum(c.isalpha() for c in w)
        return text[pos - 1:pos].isalpha() and text[pos + n:pos + n + 1].isalpha() and letters > sum(
            c.isdigit() for c in w)
    for m in _DIGIT_GROUPS_STRICT.finditer(text):     # fillers must be set apart: "256gb 2024" never joins
        groups = [(g.group(), g.start() + m.start()) for g in re.finditer(r"\d+", m.group())]
        groups = [(g, p) for g, p in groups if not in_letter_word(p, len(g))]
        if not groups or all(all(explained[p + k] for k in range(len(g))) for g, p in groups):
            continue
        nums = [g for g, _ in groups]
        for i in range(len(nums)):
            d = ""
            for g in nums[i:]:
                d += g
                if len(d) > 11:
                    break
                if _valid_ssn(d) or _valid_phone(d):
                    return True
    return False


_VIEW_RUN = 16                 # NFKC can expand one symbol into four (U+2057 -> four primes), and separators stack


def detector_view(text: str, with_map: bool = False):
    """Round 16: the separator view for the detectors. Every run of up to three characters whose Unicode category is
    punctuation (P*, every dash Pd included), symbol (S*) or space (Z*) — up to ``_VIEW_RUN`` of them, after NFKC — that
    sits between two letters or digits (round 16: fillers such as "4111\u2022x\u20221111" too) becomes one
    ASCII space, so ``934\u2022333\u20222089``, ``878\u201370\u20133759`` and ``202\u30fb555\u30fb0100`` read like
    ``934 333 2089``. Categories, never a list of characters ('+' between two digits too; a leading '+' follows no
    digit, so E.164 is kept). With ``with_map`` it also
    returns, for each character of the view, the index of the raw character it came from."""
    out, idx, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if out and out[-1].isalnum() and unicodedata.category(c)[0] in "PSZ":
            j = i
            # a run continues through marks (M*): symbols such as U+1D15E decompose into a base and a spacing mark
            while j < n and j - i < _VIEW_RUN and unicodedata.category(text[j])[0] in "PSZM":
                j += 1
            if j < n and text[j].isalnum():
                if not (out[-1].isalpha() and text[j].isalpha()):       # between two letters the run joins them
                    out.append(" ")                                     # ("a\u2022b" -> "ab"); elsewhere a space
                    idx.append(i)
                i = j
                continue
        out.append(c)
        idx.append(i)
        i += 1
    view = "".join(out)
    return (view, idx) if with_map else view


# a 7-digit local number (555-0199, 814.0202, 934\u2022 0202) written with a punctuation or symbol separator; a plain
# space ("500 2024") is left to the generic digit-run rule, which already counts it
_LOCAL7 = re.compile(r"(?<![\d.,])[2-9]\d\d(?=[^\w\s]|\s+[^\w\s])[^\w]{1,3}\d{4}(?![\d.,]?\d)")
# vanity toll-free numbers: 1-800-flo-wers, 1 (888) callnow, 800-go-fedex: an 8xx toll-free code, then seven
# letters or digits (at least two letters), in at most three groups
_VANITY = re.compile(r"(?<![\da-z])(?:1[\s.\-]{0,3})?\(?8(?:00|33|44|55|66|77|88)\)?[\s.\-]{0,3}"
                     r"(?=(?:[\s.\-]{0,3}[a-z0-9]){7}(?![a-z0-9]))(?=[\s.\-a-z0-9]*[a-z][\s.\-a-z0-9]*[a-z])"
                     r"(?:[\s.\-]{0,3}[a-z0-9]){7}(?![a-z0-9])")


def pii_detectors(raw: str) -> bool:
    """The structural detectors (THE RULE above): card, phone (NANP, E.164, 7-digit local, vanity toll-free) and
    SSN, on the raw text and on its separator view (``detector_view``), including the de-separated runs. Round 18:
    the digit stream (``digit_stream_hit``: any gap of up to three non-digit characters, letters included) and the
    vanity toll-free rule over any separators (``_VANITY_ANY``)."""
    view, idx = detector_view(raw, with_map=True)
    for t, expl in ((raw, None), (view, (raw, idx))):
        if card_like(t) or _NANP.search(t) or _E164.search(t) or _VANITY.search(t) \
                or digit_stream_hit(t) or _vanity_any(t) \
                or _deseparated_hit(t, expl) \
                or any(_valid_ssn(re.sub(r"\D", "", m.group())) for m in _SSN.finditer(t)):
            return True
    return bool(_LOCAL7.search(raw))


# ---------------------------------------------------------------- round 18: the closed threat list (ADR 0047)
# The owner-accepted method (decision 2026-11-08): certification is against the CLOSED list in ADR 0047, section
# "Personal data". Each rule below is one item of that list; none has an exemption (THE RULE).
STREAM_GAP = 3                 # the list: any gap of up to three non-digit characters between two digits
_STREAM_DIGITS = re.compile(r"\d+")


def stream_card(d: str) -> bool:
    """A card number of the list: 13-19 digits, Luhn-valid, a network's issuer prefix (``card_iin``) or the UATP
    prefix (1, 15 digits)."""
    return 13 <= len(d) <= 19 and (card_iin(d) or (len(d) == 15 and d[0] == "1")) and _luhn(d)


def nanp_phone(d: str) -> bool:
    """A NANP number: 10 digits (or 11 after a 1) whose area code and exchange both start with 2-9."""
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return len(d) == 10 and d[0] in "23456789" and d[3] in "23456789"


def digit_stream_hit(text: str) -> bool:
    """THE DIGIT STREAM (round 18, the list's first item). Digit groups whose gaps are at most ``STREAM_GAP`` non-digit
    characters of ANY kind (separators, ASCII letters, letters NFKC made ASCII, other scripts' letters) form one
    stream ("4111a1111b1111c1111", "555x010yz0199"); every window of whole consecutive groups of a stream is read as
    one number and is a card (``stream_card``), an SSN (``_valid_ssn``), a NANP phone (``nanp_phone``) or, after a
    '+', an E.164 phone of 8-15 digits. ``text`` is the detection form (decoded, NFKC, marks removed, other-script
    digits made ASCII, lookalike-folded), so fullwidth and other-script digits are already digits here. No date,
    year, version or keyword exemption exists for this rule. Linear: each window stops at 19 digits."""
    gs = [(m.start(), m.end(), m.group()) for m in _STREAM_DIGITS.finditer(text)]
    for a, (start, _end, first) in enumerate(gs):
        lo = max(gs[a - 1][1] if a else 0, start - STREAM_GAP)
        plus = "+" in text[lo:start]
        d = ""
        for b in range(a, len(gs)):
            if b > a and gs[b][0] - gs[b - 1][1] > STREAM_GAP:
                break
            d += gs[b][2]
            n = len(d)
            if n > 19:
                break
            if (n == 9 and _valid_ssn(d)) or (n in (10, 11) and nanp_phone(d)) or stream_card(d) \
                    or (plus and 8 <= n <= 15):
                return True
    return False


_NUMBER_WORD_RE = re.compile(r"\b(?:" + "|".join(_NUMBER_WORDS) + r")\b")


def spelled_stream(folded: str) -> str:
    """Round 18 (spelled digits with any gap of the list): the text split at every separator (P*, S*, Z*, C*
    character but '+'), each word that is a number word made its digit, and the words joined with NO separator, so
    the gaps the stream counts are the letters between digits ("cinco, a, W, cero" -> "5aW0"). Only when the text
    holds two or more whole number words; else ""."""
    if len(_NUMBER_WORD_RE.findall(folded)) < 2:
        return ""
    out, cur = [], []
    for c in folded + " ":
        if c != "+" and unicodedata.category(c)[0] in "PSZC":
            if cur:
                w = "".join(cur)
                out.append(_NUMBER_WORDS.get(w, w))
                cur = []
            continue
        cur.append(c)
    return "".join(out)


# vanity toll-free numbers (round 18): 1 (optional), an 8xx toll-free code, then seven keypad characters with at least
# one letter; up to three non-alphanumeric characters (any separator of the list) before the code, after it and
# between any two of the seven
_VANITY_ANY = re.compile(r"(?<![a-z0-9])(?:1[^a-z0-9]{0,3})?8(?:00|33|44|55|66|77|88)[^a-z0-9]{0,3}"
                         r"([a-z0-9](?:[^a-z0-9]{0,3}[a-z0-9]){6})(?![a-z0-9])")


def _vanity_any(t: str) -> bool:
    return any(any(c.isalpha() for c in m.group(1)) for m in _VANITY_ANY.finditer(t))


# ---- e-mail views (round 18): spelled / bracketed, rot13, reversed, encoded, whole or chunked
# The detector reads a "marker string": every space and bracket becomes one '|', so a spelled keyword ("at", "dot",
# Spanish "arroba", "punto", in brackets or not) counts only between markers (never inside a word: "what", "dotted"),
# and a chunk space inside a keyword or a label ("a t", "exa mple") is tolerated.
_MARK_CHARS = frozenset("[](){}<>")
_KW_AT = re.compile(r"(?<=\|)(?:a\|?t|a\|?r\|?r\|?o\|?b\|?a)(?=\|)")
_KW_DOT = re.compile(r"(?<=\|)(?:d\|?o\|?t|p\|?u\|?n\|?t\|?o)(?=\|)")
# the at-keyword in every view (as written, rot13, reversed, both): the cheap gate before any view is built
_KW_AT_ANY = re.compile(r"@|(?<=\|)(?:a\|?t|t\|?a|n\|?g|g\|?n|a\|?r\|?r\|?o\|?b\|?a|a\|?b\|?o\|?r\|?r\|?a|"
                        r"n\|?e\|?e\|?b\|?o\|?n|n\|?o\|?b\|?e\|?e\|?n)(?=\|)")
_EM_TLD_WORDS = ("com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "name", "pro", "mobi", "io", "ai",
                 "app", "dev", "xyz", "online", "site", "shop", "store", "tech", "blog", "cloud", "email", "mail",
                 "live", "news", "club", "top", "art", "design", "agency", "media", "world", "life", "space",
                 "website", "group", "company", "business", "network", "systems", "today", "tips", "zone", "global",
                 "local", "travel", "jobs", "asia", "museum", "digital", "solutions", "services")


def _tld_alternation() -> str:
    # a TLD of the list, or any two letters (country codes), each letter optionally followed by a chunk marker
    words = sorted(_EM_TLD_WORDS, key=len, reverse=True)
    return "(?:" + "|".join(r"\|?".join(w) for w in words) + r"|[a-z]\|?[a-z])"


_LABEL = r"(?:[a-z0-9\-]\|?){1,63}"
_MAILBOX = re.compile(r"[a-z0-9_+\-]\|?@\|?" + _LABEL + r"(?:\.\|?" + _LABEL + r"){0,4}?\.\|?" + _tld_alternation()
                      + r"(?=\||$)")
_ROT13 = str.maketrans("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
                       "nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM")
_EMAIL_CORE = re.compile(r"[a-z0-9_.+\-]@[a-z0-9\-]")
# a whole mailbox at the start of a decoded prefix (the plain-word base32 check: the bytes after it are context's)
_MAILBOX_AT_START = re.compile(r"[a-z0-9._%+\-]{1,64}@[a-z0-9\-]{1,63}(?:\.[a-z0-9\-]{1,63}){0,4}\.[a-z]{2,24}")
_ASCII_MARKS = re.compile(r"[\s\[\](){}<>!\"#$&'*,/:;=?\\^`|~]+")


def _is_marker(c: str) -> bool:
    # a space, a bracket, or any other punctuation / symbol character except those an address is made of
    return c.isspace() or c in _MARK_CHARS or (c not in ".-_+@%" and unicodedata.category(c)[0] in "PSZ")


def _marker_string(t: str) -> str:
    """Every run of spaces, brackets and other punctuation / symbols (not . - _ + @ %) made one '|', with a '|' at
    both ends."""
    if t.isascii():
        return "|" + _ASCII_MARKS.sub("|", t).strip("|") + "|"
    out, prev = ["|"], True
    for c in t:
        if _is_marker(c):
            if not prev:
                out.append("|")
            prev = True
        else:
            out.append(c)
            prev = False
    if not prev:
        out.append("|")
    return "".join(out)


def _despelled(m: str) -> str:
    """The marker string with spelled keywords made '@' and '.' (markers kept)."""
    return _KW_DOT.sub(".", _KW_AT.sub("@", m))


def rot13(t: str) -> str:
    return t.translate(_ROT13)


def _mailbox_any(d: str) -> bool:
    # the despelled view as written, rot13'd and reversed ("spelled, then rot13 / reversed")
    return bool(_MAILBOX.search(d) or _MAILBOX.search(rot13(d)) or _MAILBOX.search(d[::-1]))


def email_views_hit(folded: str) -> bool:
    """A spelled or bracketed e-mail, plain, rot13 or reversed (whole text or each word), whole or chunked, in any
    surrounding text (round 18). On the marker string of the text (``_marker_string``) and its views (rot13; reversed
    whole; each written word reversed in place), spelled keywords are read as '@' / '.' (``_despelled``), and the result, its rot13
    or its reverse must show a mailbox shape (``_MAILBOX``): a name, '@', a domain, '.', then a TLD of the list or
    any two letters ending at a marker. Gate: no at-keyword in any view and no '@' means no e-mail here."""
    m = _marker_string(folded)
    if not _KW_AT_ANY.search(m):
        return False
    words = _marker_string(" ".join(w[::-1] for w in folded.split()))       # each written word reversed in place
    for base in (m, rot13(m), words, rot13(words)):
        for v in (base, base[::-1]):
            if ("@" in v or _KW_AT.search(v)) and _mailbox_any(_despelled(v)):
                return True
    return False


# the characters of base64 (standard and URL-safe), base32, hex and percent encoding
_ENC_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-%]+")
_ENC_EDGE = re.compile(r"^[^A-Za-z0-9+/=_\-%]+|[^A-Za-z0-9+/=_\-%]+$")
_PLAIN_TOKEN = re.compile(r"[a-z]+")
_B64_BODY = re.compile(r"[A-Za-z0-9+/_\-]+={0,2}")
_B32_BODY = re.compile(r"(?:[A-Z2-7]+|[a-z2-7]+)=*")
_HEX_BODY = re.compile(r"(?:[0-9a-fA-F]{2})+")
ENC_SPAN_TOKENS = 12           # chunks joined into one candidate at most
ENC_SPAN_CHARS = 400


def _printable(raw: bytes) -> str | None:
    if len(raw) < 5 or sum(32 <= b < 127 for b in raw) < 0.9 * len(raw):
        return None
    return raw.decode("ascii", "replace")


def _decodings(s: str):
    """Every decoding of one candidate the list names: percent, base64 (standard / URL-safe, padded or not), base32
    (either case, padded or not), hex; only those whose output is printable text."""
    if "%" in s:
        u = unquote(s)
        if u != s:
            yield u
    if len(s) >= 8 and _B64_BODY.fullmatch(s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
        body = s.rstrip("=").replace("-", "+").replace("_", "/")
        try:
            u = _printable(base64.b64decode(body + "=" * (-len(body) % 4), validate=True))
        except (binascii.Error, ValueError):
            u = None
        if u:
            yield u
    if len(s) >= 8 and _B32_BODY.fullmatch(s):
        body = s.rstrip("=").upper()
        try:
            u = _printable(base64.b32decode(body + "=" * (-len(body) % 8)))
        except (binascii.Error, ValueError):
            u = None
        if u:
            yield u
    if len(s) >= 10 and _HEX_BODY.fullmatch(s):
        u = _printable(bytes.fromhex(s))
        if u:
            yield u


def _decoded_email(u: str, depth: int) -> bool:
    low = _LOOKALIKE.fold_cased(u)
    if "%" in low:
        low = unquote(low)
    if _EMAIL_CORE.search(low) or _EMAIL_CORE.search(rot13(low)) or email_views_hit(low):
        return True
    return depth == 0 and encoded_email(u, 1)


def encoded_email(cased: str, depth: int = 0) -> bool:
    """An e-mail base64 / base32 / hex / percent encoded, as written, rot13'd or reversed, whole or in chunks, in any
    surrounding text (round 18). Candidates: every run of up to ``ENC_SPAN_TOKENS`` consecutive words made only of
    encoding characters, joined, that holds at least one word which is not a plain lower-case word (an encoded
    e-mail always has an upper-case letter, a digit, '%', '+', '/' or '='); each is decoded as written, rot13'd
    and reversed (``_decodings``), and the decoded text is judged for an e-mail ('@' shape, its rot13, spelled
    views), then decoded once more (two encodings). The one encoding that can be all lower-case letters, base32 in
    lower case, is also tried from each word of an all-plain run to the run's end (``_plain_base32``)."""
    toks = [_ENC_EDGE.sub("", t) for t in cased.split()]     # "wcm90b24ubWU=," -> "wcm90b24ubWU="
    n = len(toks)
    ok = [_ENC_TOKEN.fullmatch(t) is not None for t in toks]
    plain = [ok[i] and _PLAIN_TOKEN.fullmatch(toks[i]) is not None for i in range(n)]
    for i in range(n):
        if not ok[i]:
            continue
        s, special = "", False
        for j in range(i, min(n, i + ENC_SPAN_TOKENS)):
            if not ok[j]:
                break
            s += toks[j]
            special = special or not plain[j]
            if len(s) > ENC_SPAN_CHARS:
                break
            if len(s) < 8 or not special:
                continue
            for v in (s, rot13(s), s[::-1]):
                for u in _decodings(v):
                    if _decoded_email(u, depth):
                        return True
    return _plain_base32(toks, plain)


def _plain_base32(toks: list, plain: list) -> bool:
    """Lower-case base32 written as plain words (the one encoding with no tell): in each run of plain words, the run
    from each word to its end (at most ``ENC_SPAN_TOKENS`` words) as written and rot13'd, and the run from its first
    word to each word reversed and reversed-then-rot13'd, decoded as base32 (cut to the longest valid length); a whole
    mailbox at the start of what decodes (the bytes after the e-mail are the other words', so only a prefix needs to
    be text)."""
    n, i = len(toks), 0
    while i < n:
        if not plain[i]:
            i += 1
            continue
        j = i
        while j < n and plain[j] and j - i < ENC_SPAN_TOKENS:
            j += 1
        run = toks[i:j]
        cands = []
        for k in range(len(run)):
            s = "".join(run[k:])
            cands += [s, rot13(s)]
            r = "".join(run[:k + 1])[::-1]
            cands += [r, rot13(r)]
        if any(_b32_mailbox(c) for c in cands):
            return True
        i = j
    return False


def _b32_mailbox(s: str) -> bool:
    k = len(s) - (1 if len(s) % 8 in (1, 3, 6) else 0)       # the longest valid unpadded length
    if k < 16:
        return False
    try:
        raw = base64.b32decode(s[:k].upper() + "=" * (-k % 8))
    except (binascii.Error, ValueError):
        return False
    return _MAILBOX_AT_START.match(raw.decode("ascii", "replace").lower()) is not None


# rounds 14-15: a software / standard version number of realistic dotted shape (2-4 parts, each at most 5 digits)
# after its keyword, and a standard's number ("rfc 9110"). They only ever suppress the GENERIC rules and the IP check;
# the detectors never see them.
_VERSION = re.compile(r"\b(version|ver|firmware|fw|build|release|kernel|bios|driver|update|patch|ios|android|rfc|"
                      r"iso|ieee|ecma|win(?:dows)?|v|"
                      # round 18: software names before their version number ("chrome 124.0.6367.91"); GENERIC rules
                      # only, like every keyword here (the detectors never see an exemption)
                      r"chrome|chromium|firefox|edge|safari|opera|python|node|nodejs|java|jdk|php|ruby|perl|golang|"
                      r"rust|django|rails|react|angular|vue|macos|ubuntu|debian|fedora|centos|wordpress|excel|office|"
                      r"minecraft|unity|blender|nvidia|docker|kubernetes|npm|pip|git)"
                      r"\s*\.?\s*v?(?:\d{1,5}(?:\.\d{1,5}){1,3})(?![\d.])")
_RFC_NUMBER = re.compile(r"\b(rfc|iso|ieee|ecma)\s*\d{1,5}(?!\d)")


# round 16: an e-mail spelled with "at" — this classifier's own rule, narrowed from ``logs._AT_WORD`` (which the log
# templater keeps, unchanged): "<name> at <domain> dot|. <tld>" in any bracket spelling, or "<name> at <mail
# provider>". "deals at walmart", "meeting at 2pm" are not e-mails; "john at gmail dot com", "john (at) gmail (dot)
# com", "john at gmail.com", "jane at gmail" are.
_AT = r"(?:\bat\b|[\[(<{]\s*at\s*[\])>}]|\(\s*@\s*\)|\[\s*@\s*\])"
_DOT = r"(?:\.|\bdot\b|[\[(<{]\s*(?:dot|\.)\s*[\])>}])"
_MAIL_PROVIDERS = (r"gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|icloud|me|mac|aol|proton(?:mail)?|pm|gmx|"
                   r"mail|yandex|zoho|fastmail|tutanota|qq|163|126|comcast|verizon|att|sbcglobal|web|t-online")
_AT_EMAIL = re.compile(r"[a-z0-9][\w.+\-]{0,63}\s*" + _AT + r"\s*(?:[a-z0-9][a-z0-9\-]{0,62}\s*" + _DOT
                       + r"\s*)+[a-z]{2,24}\b|[a-z0-9][\w.+\-]{0,63}\s*" + _AT + r"\s*(?:" + _MAIL_PROVIDERS
                       + r")\b|%40|&#0*64;|&commat;")


_HOMO_LETTERS = frozenset("liosbzLIOSBZ|")


def _ordinary(tok: str) -> bool:
    """A context token that cannot be part of a disguised value: no letter or digit at all (emoji, punctuation), or
    a word of 2+ letters in one ordinary case (or a script without case, such as 東京), not a number word and not
    made only of letters that read as digits."""
    if not any(c.isalnum() for c in tok):
        return True
    if not tok.isalpha() or len(tok) < 2 or all(c in _HOMO_LETTERS for c in tok):
        return False
    if tok.casefold() in _NUMBER_WORDS:
        return False
    return tok.islower() or tok.istitle() or (tok.isupper() and len(tok) <= 6) or not any(
        c.lower() != c.upper() for c in tok)


def _suspicious_windows(q: str) -> list:
    """Round 16 (war room WR-F020 / WR-F021): every maximal run of non-ordinary tokens, so words, tabs, emoji and CJK
    around a disguised value ("rug cleaner amFuZS5kb2 VAZXhhbXBs ZS5jb20=", "東京 <value>") never stop a match."""
    toks = (q or "").split()
    out, cur = [], []
    for tok in toks + [""]:
        if tok and not _ordinary(tok):
            cur.append(tok)
            continue
        if cur and len(cur) < len(toks):
            out.append(" ".join(cur))
        cur = []
    return out


def classify_query(q: str, brands: list, _depth: int = 0) -> str:
    """One query -> one class from ``QUERY_CLASSES``; the text is not kept. PII first: the whole query, and (round
    16) every maximal run of non-ordinary tokens in it (``_suspicious_windows``), so context never hides a value."""
    cls = _classify_core(q, brands, _depth)
    if cls == "pii_suspect" or _depth or len(q or "") > MAX_QUERY_CHARS:
        return cls
    if any(_window_hit(w) for w in _suspicious_windows(q)):
        return "pii_suspect"
    return cls


def _window_hit(w: str) -> bool:
    """A window gets the DETECTORS only (card / phone / SSN on the raw text and its spelled and homoglyph views) and
    the base64-of-text check — never the generic rules, whose exemptions need the whole query's context (a version
    keyword, a date next to a word)."""
    t = decode_query(w)
    folded = _LOOKALIKE.fold_cased(t)
    if pii_detectors(folded) or pii_detectors(_LOOKALIKE.fold_cased(strip_marks(w))) or base64_text(t, 1):
        return True
    spelled, glyphs = spelled_digits(folded), homoglyph_digits(folded)
    if (spelled and pii_detectors(spelled)) or (glyphs and pii_detectors(glyphs)):
        return True
    return any(pii_detectors(v) for v in _defilled_views(folded)[1:])     # interleaved: "1x x2x x3x ..."


def _classify_core(q: str, brands: list, _depth: int = 0) -> str:
    """The classifier on one text (judged on the lookalike fold, after invisible characters are removed, like the
    log templater)."""
    if len(q or "") > MAX_QUERY_CHARS:          # too long to classify cheaply: dropped as personal data, counted
        return "pii_suspect"
    raw_form = strip_marks(q or "")              # the detection form before percent-decoding
    t = raw_form if "%" not in (q or "") else decode_query(q)     # (no '%': decoding changes nothing)
    if len(t) > MAX_QUERY_CHARS:                # NFKC can expand a character many times (U+FDFA is 18)
        return "pii_suspect"
    folded = _LOOKALIKE.fold_cased(t)
    # THE RULE (round 15): detectors first, on the raw normalised text and its de-obfuscated views, never exempted.
    # Round 16: also on the text before percent-decoding ("202%555%0100": "%55" would decode into a letter).
    undecoded = folded if raw_form == t else _LOOKALIKE.fold_cased(raw_form)
    spelled = spelled_digits(folded)
    glyphs = homoglyph_digits(folded)
    if pii_detectors(folded) or (undecoded != folded and pii_detectors(undecoded)) \
            or (spelled and pii_detectors(spelled)) \
            or (glyphs and pii_detectors(glyphs)) or any(pii_detectors(v) for v in _defilled_views(folded)[1:]):
        return "pii_suspect"
    # round 18: the list's e-mail items (spelled / bracketed / rot13 / reversed / encoded, whole or chunked) and the
    # address rule with surrounding text set aside; detectors like the above, never exempted
    core = _address_core(folded)
    if digit_stream_hit(t) or (raw_form != t and digit_stream_hit(raw_form)) \
            or digit_stream_hit(spelled_stream(folded)) \
            or email_views_hit(folded) or (undecoded != folded and email_views_hit(undecoded)) \
            or encoded_email(t) or (raw_form != t and encoded_email(raw_form)) \
            or (core != folded and looks_like_address(core)):
        return "pii_suspect"
    # exemptions below this line touch ONLY the generic digit-run / token rules (and the IP check)
    versioned = _RFC_NUMBER.sub(lambda m: m.group(1) + " #", _VERSION.sub(lambda m: m.group(1) + " #", folded))
    undated = isolated_dates_removed(versioned)
    gview = detector_view(undated)              # round 16: the generic digit rules read separators by category too
    if url_like(versioned) or base64_text(t, _depth) or "@" in folded or logs_mod._EMAIL.search(folded) \
            or _AT_EMAIL.search(folded) \
            or longest_digit_run(gview) >= PII_DIGIT_RUN or any(_word_identifies(w) for w in undated.split()) \
            or card_like(gview) or split_secret(undated) or looks_like_address(folded) \
            or (spelled and (longest_digit_run(spelled) >= PII_DIGIT_RUN or card_like(spelled))) \
            or filler_digit_run(gview) >= FILLER_DIGIT_RUN \
            or (glyphs and (longest_digit_run(glyphs) >= 9 or card_like(glyphs)
                            or filler_digit_run(glyphs) >= FILLER_DIGIT_RUN)):
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
