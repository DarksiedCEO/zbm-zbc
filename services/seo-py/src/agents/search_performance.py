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

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import lookalikes
from agents import logs as logs_mod

AGENT, TASK = "osei", "search_performance"
VERSION = "2026-11-08.6"                     # query classes, templating and the aggregation rules below
# 2026-11-08.1 (Priya review round 1, M3): street addresses, unit / apartment numbers, PO boxes, ZIP+4 and a ZIP next
#    to a US state are pii_suspect (looks_like_address)
# 2026-11-08.2 (review round 2): the address rule needs address structure (it over-dropped service queries)
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


def looks_like_address(folded: str) -> bool:
    """A postal address or part of one, on the lookalike-folded text (rules and their deliberate gaps above). Personal
    names are NOT detected. Text over ``MAX_QUERY_CHARS`` counts as one (it is not examined)."""
    t = folded.strip()
    if len(t) > MAX_QUERY_CHARS:
        return True
    return any(p.search(t) for p in _ADDRESS)


_JWT = re.compile(r"[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}")
_SLASH_TOKEN = re.compile(r"[a-z0-9_\-=+.~/]{12,}")
_PLAIN_SEGMENT = re.compile(r"[a-z]+(?:[-.][a-z]+)*")
# a URL path of ordinary slugs under a host name: docs.example.com/v2/api/reference, acme.com/blog/5-ways-to-save
_HOST_PATH = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+[a-z]{2,24}(?:/[a-z0-9-]{1,24})*/?")


def _slash_secret(w: str) -> bool:
    """A base64 / AWS-secret-like word holding "/", "+" or "=" (review round 5): 20+ such characters with a digit, a
    "+" or an "=" (``wjalr.../k7mdeng/...``, ``dghpcy9pcy9h/c2vjcmv0ignszxk=``), or 12+ with two of "/", "+", "="
    (``ywjj/zgvm+z2hp``). A path of plain words (``example.com/blog/how-to-clean``), a host name followed by short slug
    segments (``docs.example.com/v2/api/reference``, ``mysite.com/page2/contact``; review round 6) and a short tire
    size (``225/65r17``) are not. Not caught: base64 under 12 characters."""
    if not _SLASH_TOKEN.fullmatch(w) or not any(c in w for c in "/+="):
        return False
    if all(_PLAIN_SEGMENT.fullmatch(seg) for seg in w.split("/") if seg):
        return False
    if _HOST_PATH.fullmatch(w):            # a host name, then short slug segments of letters, digits and hyphens
        return False
    marks = sum(1 for c in "/+=" if c in w)
    return (len(w) >= 20 and (any(c.isdigit() for c in w) or "+" in w or "=" in w)) or marks >= 2
_ALNUM_TOKEN = re.compile(r"[a-z0-9_\-=+~]+")


def _transitions(w: str) -> int:
    """How often a word switches between letters and digits (``a1b2c3`` 5, ``24acc636a003`` 5, ``f150`` 1)."""
    kinds = [c.isdigit() for c in w if c.isalnum()]
    return sum(1 for x, y in zip(kinds, kinds[1:]) if x != y)


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
            or _slash_secret(w):
        return True
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


def classify_query(q: str, brands: list) -> str:
    """One query -> one class from ``QUERY_CLASSES``; the text is not kept. PII first (judged on the lookalike fold,
    after invisible characters are removed, like the log templater)."""
    if len(q or "") > MAX_QUERY_CHARS:          # too long to classify cheaply: dropped as personal data, counted
        return "pii_suspect"
    t = lookalikes.strip_invisible(unicodedata.normalize("NFKC", q or ""))
    if len(t) > MAX_QUERY_CHARS:                # NFKC can expand a character many times (U+FDFA is 18)
        return "pii_suspect"
    folded = _LOOKALIKE.fold_cased(t)
    words = folded.split()
    if "@" in folded or logs_mod._EMAIL.search(folded) or logs_mod._AT_WORD.search(folded) \
            or longest_digit_run(folded) >= PII_DIGIT_RUN or any(_word_identifies(w) for w in words) \
            or looks_like_address(folded):
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
