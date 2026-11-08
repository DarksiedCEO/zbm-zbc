"""
Idris — Competitor Visibility agent (ADR 0048). ONE job: measure the client's AI share of voice against its competitors
in AI answers, and detect emerging competitors.

Idris asks no engine itself. It reads the SAME answers Callum's citation runs collect through the multi-engine adapter
(answer_adapter.py): while a citation run's answers are in memory, ``Observer`` scores them (``observe``) and the
service records the derived observations next to Callum's result; the answer text is never stored. A competitor-
visibility run then either REUSES a recent citation run's observations (no engine call) or asks for a fresh citation
run (svc_competitors.py: the reuse rules).

Per usable answer Idris records, for the client and each tracked competitor (at most ``MAX_TRACKED``, the tenant's
configured list), whether the answer names it (``agents/brand_mention.py``: whole words, possessives yes, plurals no,
the tenant's context / negative terms, and the common-word rule — a competitor called ``Ring`` or ``Target`` with no
disambiguation is LOW confidence and kept out of every share, exactly like a client brand), whether a lexical negative
cue sits next to the name (``NEGATIVE_CUES``: reported, never subtracted — share of voice is presence, not
endorsement, and Idris measures no sentiment), and which tracked brand's domain each structured citation lands on
(``callum.classify_citation`` decides the client part: a lookalike host is never the client; an unresolved redirector
is unresolved, never anybody's).

Emerging competitors (the differentiator) come from a DETERMINISTIC candidate extractor, never a model: brand-like
proper-noun runs of 1-4 words (``candidates``) in answers to the client's prompts, after the exclusions below, with
brand signals (a list head, bold, mixed case, a corporate suffix, or the name's own domain cited in the same answer).
A candidate is ``EMERGING_COMPETITOR`` when it was seen in at least ``EMERGING_MIN_ANSWERS`` usable answers over the
look-back AND on at least ``EMERGING_MIN_SPREAD`` engines OR in at least ``EMERGING_MIN_SPREAD`` runs AND it carries
at least one brand signal; otherwise it is ``WATCH`` (listed with its counts, never folded in). Up to
``MAX_EMERGING_FOLDED`` emerging competitors are folded into the benchmark automatically (origin ``emerging``; their
citation share is UNKNOWN: no domain is verified for them). The owner (dashboard) or the tenant (hub, its own tenant)
can dismiss one; a dismissed candidate that reappears is listed under ``dismissed_seen`` with its counts, never
flagged again and never silently dropped.

Excluded from candidates (each counted by reason, ``excluded``): the client's own brand terms and LOOKALIKES of them
(the shared lookalike fold, punctuation and spacing ignored, or one edit away for names of 6+ characters:
``CLIENT_LOOKALIKE``, listed apart as a brand-impersonation signal) and longer names that contain one
(``CLIENT_VARIANT``); tracked competitors (``TRACKED``); generic words (the bundled common-word list, ``STOPWORDS``,
``GENERIC_TERMS``: ``GENERIC``); single words that only ever start a sentence with no brand signal
(``SENTENCE_INITIAL``); runs of more than 4 capitalised words and heading / title-case lines (``TITLE_RUN``); the answer
engines' own names (``ENGINE_SELF``); names of known publishers (``KNOWN_PUBLISHERS``, self-authored: a citation host
on that table is a PUBLISHER, never a brand, and its name is never a candidate: ``PUBLISHER``); names whose every word
is in the prompt (the prompt's topic, e.g. "Amazon" in "best amazon reimbursement agency": ``TOPIC_TERM``); and the
tenant's own exclusions (``EXCLUDED_BY_TENANT``). A tracked competitor whose term or domain is the client's (or a
lookalike of it) is excluded from the benchmark at run time (``excluded: CLIENT_LOOKALIKE``) and reported.

Share of voice is two measures, never one score: MENTION share (answers naming brand B / brand-answer pairs over every
benchmark brand) and CITATION share (citations on B's domains / citations on any tracked brand's domain), each with a
95% Wilson interval, plus per-brand PRESENCE (answers naming B / usable answers). UNKNOWN (``value: null``, never 0%)
when there are fewer than ``MIN_USABLE`` usable answers, when no benchmark brand was named, when a brand's only
mentions are unconfirmed, when every citation is unresolved, or (citation share) for a brand with no verified domain.
Every row carries engine, model, prompt id, timestamp and an evidence pointer into Callum's citation run.

Output is framed as opportunity: ``opportunities`` lists the prompt x engine scopes where a competitor appears more
often than the client, with the gap in answers and — against the previous comparable run — whether it is closing;
``strengths`` lists where the client leads. Pure functions; no agent touches service state, the ledger or the log.
"""

from __future__ import annotations

import bisect
import functools
import re
from typing import Optional

import lookalikes
from agents import brand_mention, callum, domain_match
from agents.probes import MIN_USABLE, _answer_hash, extract_citations, wilson
from agents.prompt_guard import personal_data
from envelope import envelope, finding, observed, sha

AGENT, TASK = "idris", "competitor_visibility"
VERSION = "idris-rules/2026-11-08.1"
CONTRACT = "idris.share_of_voice/1"
REFRESH_TIER = "near_real_time"
MAX_TRACKED = 20                       # configured competitors per tenant (the 21st is refused: COMPETITOR_LIMIT)
MAX_EMERGING_FOLDED = 10               # emerging competitors folded into one benchmark (beyond the 20 tracked)
MAX_WATCH_LISTED = 20
EMERGING_MIN_ANSWERS = 3
EMERGING_MIN_SPREAD = 2                # distinct engines, or distinct runs
HIGH_MIN_ANSWERS = 6
MAX_CANDIDATES_PER_ANSWER = 25
MAX_CANDIDATES_PER_RUN = 200
MAX_POINTERS = 50
MAX_EVIDENCE = 3
MAX_ROWS = 2000
MAX_WORDS = 4
CUE_WINDOW = 40
EXCERPT_MAX = 160
SIGNALS = ("LIST_HEAD", "BOLD", "MIXED_CASE", "CORPORATE_SUFFIX", "DOMAIN_HINT")
EXCLUSIONS = ("GENERIC", "SENTENCE_INITIAL", "TITLE_RUN", "ENGINE_SELF", "CLIENT_LOOKALIKE", "CLIENT_VARIANT",
              "TRACKED", "PUBLISHER", "TOPIC_TERM", "EXCLUDED_BY_TENANT")
_TABLE = lookalikes.Table()

# a lexical cue next to a brand name: reported as ``negative_cue``, never a sentiment score and never subtracted
NEGATIVE_CUES = ("avoid", "scam", "scams", "fraud", "complaints", "lawsuit", "lawsuits", "unreliable", "worse",
                 "worst", "overpriced", "beware", "not recommended", "stay away", "steer clear")
STOPWORDS = frozenset("""
a an the this that these those our your their its my we you they it he she i in on at for with by from to of and or
but if when while as also however here there what which who whom why how best top some many most other another each
every both all no not yes is are was were be been use using try consider choose note step option options tip tips pros
cons overall finally first second third next then new key why choosing popular leading great good better more less
""".split())
GENERIC_TERMS = frozenset("""
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september
october november december ai seo aeo geo llm faq faqs ceo cto cfo usa us uk eu api apis url urls roi b2b b2c llc inc
ltd pdf sku skus kpi kpis crm ppc fba asin asins saas diy usd faq q a ok
""".split())
ENGINE_SELF = frozenset({"openai", "chatgpt", "gpt", "anthropic", "claude", "gemini", "perplexity", "google",
                         "bing", "copilot"})
CORPORATE_SUFFIXES = frozenset("""
inc llc ltd corp co company group agency labs media partners solutions consulting technologies software systems
studio studios digital marketing analytics capital holdings gmbh ag sa plc
""".split())
# Self-authored (no third-party list, no licence): hosts that PUBLISH or HOST content about brands. A citation landing
# on one is a PUBLISHER citation, never a brand's; the publisher's name is never an emerging-competitor candidate.
# A tenant whose real competitor is one of these lists it as a tracked competitor (tracked domains are checked first).
KNOWN_PUBLISHERS = {
    "reddit.com": "Reddit", "wikipedia.org": "Wikipedia", "youtube.com": "YouTube", "quora.com": "Quora",
    "medium.com": "Medium", "linkedin.com": "LinkedIn", "forbes.com": "Forbes", "nytimes.com": "New York Times",
    "wsj.com": "Wall Street Journal", "bloomberg.com": "Bloomberg", "reuters.com": "Reuters",
    "techcrunch.com": "TechCrunch", "businessinsider.com": "Business Insider", "investopedia.com": "Investopedia",
    "nerdwallet.com": "NerdWallet", "g2.com": "G2", "capterra.com": "Capterra", "trustpilot.com": "Trustpilot",
    "yelp.com": "Yelp", "clutch.co": "Clutch", "bbb.org": "BBB", "github.com": "GitHub", "x.com": "X",
    "twitter.com": "Twitter", "facebook.com": "Facebook", "instagram.com": "Instagram", "tiktok.com": "TikTok",
    "pinterest.com": "Pinterest", "producthunt.com": "Product Hunt", "crunchbase.com": "Crunchbase",
    "glassdoor.com": "Glassdoor", "indeed.com": "Indeed", "upwork.com": "Upwork", "fiverr.com": "Fiverr",
    "substack.com": "Substack", "wordpress.com": "WordPress",
}
LIMITS = ["Idris reads the answers of Callum's citation runs: one sample of N answers per prompt and engine, at one "
          "time, from one account. Observations, not search truth; answer engines vary by user, location and time.",
          "A mention is a lexical whole-word match (agents/brand_mention.py); Idris does not understand the answer. A "
          "negative cue next to a name is reported, never a sentiment measure, and never removes the mention.",
          "Emerging competitors come from a deterministic proper-noun extractor with stated thresholds: it can miss "
          "lower-case or unusual names and can list a non-brand (a place, a product line). WATCH candidates are never "
          "folded into the benchmark; any candidate can be dismissed.",
          "Shares are over the benchmark brands only (the client, the tracked competitors and the folded emerging "
          "ones); an untracked brand does not count until it is detected. Brand mentions within one answer are not "
          "independent, so a share's Wilson interval is approximate (directional).",
          "Citation share counts structured citations on the brands' configured domains; unresolved redirector "
          "citations are excluded and counted; an emerging competitor has no verified domain (citation share UNKNOWN).",
          "No prompt-volume source is connected: how often real users ask these prompts is unknown. Nothing here is "
          "calibrated against traffic or revenue; no single visibility score is produced."]


# ---------------------------------------------------------------------------------------------- names

def _ws(s: str) -> str:
    return " ".join((s or "").split())


@functools.lru_cache(maxsize=4096)
def compact(s: str) -> str:
    """A name compared without case, spacing or punctuation, through the shared lookalike fold (homoglyphs,
    full-width forms, invisible characters)."""
    return re.sub(r"[\W_]+", "", _TABLE.fold_cased(_ws(s)))


def _within_one(a: str, b: str) -> bool:
    """One substitution, insertion, deletion or adjacent transposition apart (linear)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]
                                  and a[diff[1]] == b[diff[0]])
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def lookalike_name(name: str, of: str) -> bool:
    """``name`` is ``of`` or a lookalike of it: equal after the fold (case, spacing, punctuation, homoglyphs), or — for
    names of 6+ characters — one edit away (``Z Bset Media``)."""
    a, b = compact(name), compact(of)
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 6 and _within_one(a, b))


def _words(s: str) -> list:
    return [w for w in re.split(r"[\W_]+", _TABLE.fold_cased(_ws(s))) if w]


def _contains_words(name: str, part: str) -> bool:
    n, p = _words(name), _words(part)
    return bool(p) and len(n) > len(p) and any(n[i:i + len(p)] == p for i in range(len(n) - len(p) + 1))


def registrable(host: str) -> Optional[str]:
    """The registrable domain of ``host`` by the vendored Public Suffix List (``acme.co.uk`` for ``www.acme.co.uk``)."""
    h = domain_match._ascii(host or "")
    if h is None:
        return None
    labels = h.split(".")
    for i in range(len(labels)):
        if domain_match._suffix(".".join(labels[i:])):
            return ".".join(labels[i - 1:]) if i > 0 else None
    return None


def publisher(host: str) -> Optional[str]:
    """The publisher's name when ``host`` is on (a subdomain of) a KNOWN_PUBLISHERS host."""
    for d, name in KNOWN_PUBLISHERS.items():
        if domain_match.on_site(host, d):
            return name
    return None


_PUBLISHER_KEYS = frozenset(compact(n) for n in KNOWN_PUBLISHERS.values())


# ---------------------------------------------------------------------------------------------- the benchmark brands

def brands(competitor_set: dict, prompt_set: dict, domain: str) -> tuple:
    """(benchmark brands, excluded tracked competitors). The client first; a tracked competitor whose term or domain is
    the client's (or a lookalike of it) is excluded at run time, never measured as a competitor."""
    client = {"brand_id": "client", "name": prompt_set["brand_terms"][0], "origin": "client",
              "terms": list(prompt_set["brand_terms"]), "rules": prompt_set.get("brand_term_rules"),
              "domains": [domain]}
    out, excluded = [client], []
    for c in competitor_set.get("competitors") or []:
        why = None
        if any(lookalike_name(t, ct) for t in c["terms"] for ct in client["terms"]):
            why = "CLIENT_LOOKALIKE"
        elif any(domain_match.match(d, domain) is not None for d in c.get("domains") or []):
            why = "CLIENT_DOMAIN"
        if why:
            excluded.append({"brand_id": c["competitor_id"], "name": c["name"], "excluded": why})
            continue
        out.append({"brand_id": c["competitor_id"], "name": c["name"], "origin": "tracked", "terms": list(c["terms"]),
                    "rules": c.get("term_rules") or None, "domains": list(c.get("domains") or [])})
    return out, excluded


def set_warnings(competitor_set: dict) -> list:
    """Tracked competitor terms that may be common words with no disambiguation rule (same rule as the client)."""
    out = []
    for c in competitor_set.get("competitors") or []:
        for t in brand_mention.warnings(c["terms"], c.get("term_rules")):
            out.append({"code": "BRAND_TERM_MAY_BE_COMMON_WORD", "competitor_id": c["competitor_id"], "term": t,
                        "effect": "this competitor's mentions count only when the answer confirms them (its domain, "
                                  "a citation of it, or another of its terms that is not a common word); add "
                                  "context_terms, negative_terms or case_sensitive, or common_word: false"})
    return out


# ---------------------------------------------------------------------------------------------- candidate extraction

_WORD = re.compile(r"[^\W_](?:[\w'’&-]*[^\W_])?")
_LIST_PREFIX = re.compile(r"[ \t]*(?:>[ \t]*)*(?:[-*+•]|\d{1,3}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?[*_\"'“‘(\[]*$")
_MIXED = re.compile(r"[a-z][A-Z]")


def _brandlike(w: str) -> bool:
    return w[0].isupper() or (w[0].islower() and any(c.isupper() for c in w[1:])) \
        or (w[0].isdigit() and any(c.isupper() for c in w))


def _strip_possessive(w: str) -> str:
    for s in ("'s", "’s", "'", "’"):
        if w.endswith(s) and len(w) > len(s):
            return w[:-len(s)]
    return w


_LIST_ITEM = re.compile(r"^[ \t]*(?:>[ \t]*)*(?:[-*+\u2022]|\d{1,3}[.)])[ \t]")


def _skipped_lines(text: str) -> list:
    """(start, end) of heading lines, and of title lines — not a list item, 4+ words, no closing punctuation, every
    word of 4+ letters capitalised: titles, not names."""
    out, pos = [], 0
    for line in text.split("\n"):
        end = pos + len(line)
        ws = _WORD.findall(line)
        title = not _LIST_ITEM.match(line) and len(ws) >= 4 and not line.rstrip().endswith((".", "!", "?", ":", ";")) \
            and all(_brandlike(w) for w in ws if len(w) >= 4)
        if line.lstrip().startswith("#") or title:
            out.append((pos, end))
        pos = end + 1
    return out


def candidates(text: str, ctx: dict, excluded: Optional[dict] = None) -> list:
    """The brand-like names in one answer: ``[{"key", "display", "signals", "at"}]`` (at most
    ``MAX_CANDIDATES_PER_ANSWER``, first occurrence order). ``ctx``: {"client_terms", "client_label", "tracked_terms",
    "exclude_keys", "prompt_words"}. ``excluded`` (optional) is incremented per exclusion reason; ``ctx`` may carry
    ``lookalikes_seen`` (dict) to collect client lookalikes. Deterministic; linear in the answer."""
    t = brand_mention._ws_lines(text)
    skipped = _skipped_lines(t)
    groups: list = []
    cur: list = []

    def close():
        if cur:
            groups.append(list(cur))
            cur.clear()
    si = 0
    title_lines: set = set()
    for m in _WORD.finditer(t):
        while si < len(skipped) and skipped[si][1] < m.start():
            si += 1
        in_skip = si < len(skipped) and skipped[si][0] <= m.start() <= skipped[si][1]
        w = m.group(0)
        if in_skip or not _brandlike(w):
            close()
            if in_skip and _brandlike(w):
                title_lines.add(si)                  # a heading or title line: counted once per line
            continue
        if cur and t[cur[-1][2]:m.start()] not in (" ", " & "):
            close()
        cur.append((w, m.start(), m.end()))
        if cur[-1][0] != _strip_possessive(w):       # a possessive ends the name
            close()
    close()

    breaks = [i for i, ch in enumerate(t) if ch == "\n"]
    counted: set = set()

    def bump(reason, key=None):
        """Exclusions are counted once per name per answer (a title line once per line)."""
        if excluded is not None and (reason, key) not in counted:
            counted.add((reason, key))
            excluded[reason] = excluded.get(reason, 0) + 1
    for li in sorted(title_lines):
        bump("TITLE_RUN", ("line", li))
    found: dict = {}
    order: list = []
    for g in groups:
        ws = [(_strip_possessive(w), s, s + len(_strip_possessive(w))) for w, s, _e in g]
        while ws and ws[0][0].lower() in STOPWORDS:
            ws = ws[1:]
        while ws and ws[-1][0].lower() in STOPWORDS:
            ws = ws[:-1]
        if not ws:
            continue
        if len(ws) > MAX_WORDS:
            bump("TITLE_RUN", compact(t[ws[0][1]:ws[-1][2]]))
            continue
        start, end = ws[0][1], ws[-1][2]
        display = t[start:end]
        key = compact(display)
        lows = [w.lower() for w, _s, _e in ws]
        if len(key) < 2 or len(key) > 60 or key.isdigit() \
                or all(x in STOPWORDS or x in GENERIC_TERMS for x in lows) \
                or (len(ws) == 1 and lows[0] in brand_mention.common_words()):
            bump("GENERIC", key)
            continue
        if key in ENGINE_SELF:
            bump("ENGINE_SELF", key)
            continue
        if any(lookalike_name(display, c) for c in ctx["client_terms"]) or \
                (ctx.get("client_label") and key == ctx["client_label"]):
            bump("CLIENT_LOOKALIKE", key)
            seen = ctx.get("lookalikes_seen")
            if seen is not None and (key in seen or len(seen) < 10):
                seen.setdefault(key, {"display": display, "answers": 0})
                ctx.setdefault("lookalikes_this_answer", set()).add(key)
            continue
        if any(_contains_words(display, c) for c in ctx["client_terms"]):
            bump("CLIENT_VARIANT", key)
            continue
        if any(key == compact(tt) or _contains_words(display, tt) for tt in ctx["tracked_terms"]):
            bump("TRACKED", key)
            continue
        if key in _PUBLISHER_KEYS:
            bump("PUBLISHER", key)
            continue
        if key in ctx["exclude_keys"]:
            bump("EXCLUDED_BY_TENANT", key)
            continue
        if all(x in ctx["prompt_words"] for x in _words(display)):
            bump("TOPIC_TERM", key)
            continue
        signals = set()
        li = bisect.bisect_left(breaks, start)                # the line holding the name (no rescan: linear)
        line_start = breaks[li - 1] + 1 if li else 0
        if start - line_start <= 32 and _LIST_PREFIX.match(t, line_start, start):
            signals.add("LIST_HEAD")
        if any(t.startswith(mk, start - 2) and t.startswith(mk, end) for mk in ("**", "__")):
            signals.add("BOLD")
        if any(_MIXED.search(w) for w, _s, _e in ws):
            signals.add("MIXED_CASE")
        if len(ws) >= 2 and lows[-1] in CORPORATE_SUFFIXES:
            signals.add("CORPORATE_SUFFIX")
        initial_only = len(ws) == 1 and not signals and brand_mention._sentence_initial(t, start)
        f = found.get(key)
        if f is None:
            f = found[key] = {"key": key, "display": display, "signals": set(), "at": start, "initial_only": True}
            order.append(key)
        f["signals"] |= signals
        f["initial_only"] = f["initial_only"] and initial_only
    out = []
    for key in order:
        f = found[key]
        if f["initial_only"]:
            bump("SENTENCE_INITIAL", key)
            continue
        out.append({"key": key, "display": f["display"], "signals": sorted(f["signals"]), "at": f["at"]})
        if len(out) >= MAX_CANDIDATES_PER_ANSWER:
            break
    return out


def _negative_cue(text: str, b: dict) -> bool:
    """A NEGATIVE_CUES phrase (whole words) within CUE_WINDOW characters of one of the brand's term matches."""
    rules = {r["term"]: r for r in brand_mention.normalise(b["terms"], b.get("rules"))}
    cues = sorted(m.span() for c in NEGATIVE_CUES for m in brand_mention._pattern(c, False).finditer(text))
    if not cues:
        return False
    for term in b["terms"]:
        for m in brand_mention._pattern(term, rules[term]["case_sensitive"]).finditer(text):
            if any(ce > m.start() - CUE_WINDOW and cs < m.end() + CUE_WINDOW for cs, ce in cues):
                return True
    return False


def _excerpt(text: str, at: int) -> dict:
    t = " ".join((text or "").split())
    lo = max(0, min(at, len(t)) - 60)
    span = t[lo:lo + EXCERPT_MAX]
    kind = personal_data(span)
    if kind is not None:
        return {"excerpt": None, "withheld": f"personal_data:{kind}"}
    return {"excerpt": observed(span, EXCERPT_MAX), "withheld": None}


# ---------------------------------------------------------------------------------------------- observation

class Observer:
    """Called by Callum's run (``callum.run(..., observer=)``) with the collected samples while their text is in
    memory. Never raises: a failure is kept in ``error`` (the citation run is recorded regardless)."""

    def __init__(self, competitor_set: dict, prompt_set: dict, domain: str, run_id: str):
        self.cs, self.ps, self.domain, self.run_id = competitor_set, prompt_set, domain, run_id
        self.result: Optional[dict] = None
        self.error: Optional[BaseException] = None

    def __call__(self, engines: dict, resolver, observed_at: str, refresh_due: str) -> None:
        try:
            self.result = observe(engines, resolver, self.cs, self.ps, self.domain, self.run_id, observed_at,
                                  refresh_due)
        except Exception as exc:  # noqa: BLE001 - Idris failing never loses Callum's run
            self.error = exc


def _owner(url: str, domain: str, tracked: list, res: dict) -> tuple:
    """Who a structured citation belongs to: ("client" | "client_lookalike" | "unresolved" | <brand_id> |
    "competitor_lookalike" | "publisher" | "canonical_to_client" | "other", the deciding host)."""
    cl = callum.classify_citation(url, domain, [], res)
    dec = (cl["chain"].get("final") or cl["chain"].get("cited")) or {}
    host = dec.get("host") or ""
    if cl["class"] == "STRENGTH_URL":
        return "client", host
    if cl["class"] == "LOOKALIKE_URL":
        return "client_lookalike", host
    if cl["class"] == "INCONCLUSIVE":
        return "unresolved", host
    if cl["class"] == "CANONICAL_TO_CLIENT":
        return "canonical_to_client", host
    for b in tracked:
        if any(domain_match.on_site(host, d) for d in b["domains"]):
            return b["brand_id"], host
    for b in tracked:
        if any(domain_match.match(host, d) == domain_match.LOOKALIKE for d in b["domains"]):
            return "competitor_lookalike", host
    if publisher(host):
        return "publisher", host
    return "other", host


def observe(engines: dict, resolver, competitor_set: dict, prompt_set: dict, domain: str, run_id: str,
            observed_at: str, refresh_due: str) -> dict:
    """Idris's observations of one citation run's answers (no text kept: counts, pointers, bounded excerpts for
    candidates only). Deterministic for the same input."""
    bl, excluded_tracked = brands(competitor_set, prompt_set, domain)
    tracked = [b for b in bl if b["origin"] == "tracked"]
    reg = registrable(domain)
    ctx = {"client_terms": list(prompt_set["brand_terms"]),
           "client_label": compact(reg.split(".")[0]) if reg else None,
           "tracked_terms": [t for c in competitor_set.get("competitors") or [] for t in c["terms"]],
           "exclude_keys": {compact(x) for x in competitor_set.get("exclude_terms") or []},
           "lookalikes_seen": {}}
    detect = competitor_set.get("emerging_detection", True)
    tallies: dict = {}
    cands: dict = {}
    excluded: dict = {}
    rows: list = []
    truncated = 0
    cache = getattr(resolver, "cache", {}) if resolver is not None else {}

    def add(row):
        nonlocal truncated
        if len(rows) < MAX_ROWS:
            rows.append(row)
        else:
            truncated += 1
    for eng, r in sorted(engines.items()):
        for s in r["samples"]:
            pi = s["prompt_index"]
            pid = callum.prompt_id(prompt_set, pi)
            k = tallies.setdefault((eng, pi), {"engine": eng, "prompt_index": pi, "prompt_id": pid, "samples": 0,
                                               "usable": 0, "refusals": 0, "failed": 0, "citations": 0,
                                               "unresolved_citations": 0, "publisher_citations": 0,
                                               "client_lookalike_citations": 0, "versions": set(),
                                               "brands": {b["brand_id"]: {"mentions": 0, "low": 0, "negative_cue": 0,
                                                                          "cited_answers": 0, "citations": 0}
                                                          for b in bl}})
            k["samples"] += 1
            a = s.get("answer")
            if a is not None and getattr(a, "model", None):
                k["versions"].add(f"{a.model}@{a.model_version or '?'}")
            ptr = {"citation_run_id": run_id, "engine": eng, "prompt_id": pid, "prompt_index": pi,
                   "sample": s["sample"], "model": getattr(a, "model", None),
                   "model_version": getattr(a, "model_version", None),
                   "provider_asked_at": getattr(a, "asked_at", None),
                   "answer_sha256": _answer_hash(a) if a is not None and s["status"] != "FAILED" else None}
            base = {"engine": eng, "model": ptr["model"], "model_version": ptr["model_version"],
                    "provider": getattr(a, "provider", None) or eng, "prompt_id": pid, "prompt_index": pi,
                    "sample": s["sample"], "observed_at": observed_at, "provider_asked_at": ptr["provider_asked_at"],
                    "refresh_tier": REFRESH_TIER, "refresh_due": refresh_due, "citation_run_id": run_id}
            if s["status"] == "FAILED":
                k["failed"] += 1
                add({**base, "class": "INCONCLUSIVE", "reason": s.get("error_code"), "brand_id": None,
                     "evidence": {**ptr, "answer_sha256": None}})
                continue
            if s["status"] == "REFUSAL":
                k["refusals"] += 1
                continue
            k["usable"] += 1
            text = s["text"] or ""
            owners: dict = {}
            hosts: list = []
            for c in extract_citations(a):
                own, host = _owner(c["url"], domain, tracked, cache.get(c["url"]) or {"state": "OFF"})
                k["citations"] += 1
                if own == "unresolved":
                    k["unresolved_citations"] += 1
                elif own == "publisher":
                    k["publisher_citations"] += 1
                elif own == "client_lookalike":
                    k["client_lookalike_citations"] += 1
                elif own == "other" and host:
                    hosts.append(host)
                owners[own] = owners.get(own, 0) + 1
            norm = brand_mention._ws_lines(text)
            client_named = False
            for b in bl:
                bt = k["brands"][b["brand_id"]]
                cited = owners.get(b["brand_id"], 0)
                m = brand_mention.mention(text, b["terms"], b["rules"], domain=(b["domains"] or [None])[0],
                                          client_cited=bool(cited))
                if m["mentioned"] and m["confidence"] == brand_mention.LOW and len(b["domains"]) > 1:
                    for d in b["domains"][1:]:
                        if brand_mention.mention(text, b["terms"], b["rules"], domain=d)["confidence"] == \
                                brand_mention.HIGH:
                            m = {**m, "confidence": brand_mention.HIGH}
                            break
                neg = bool(m["mentioned"]) and _negative_cue(norm, b)
                if m["mentioned"] and m["confidence"] == brand_mention.HIGH:
                    bt["mentions"] += 1
                    client_named = client_named or b["brand_id"] == "client"
                elif m["mentioned"]:
                    bt["low"] += 1
                if neg:
                    bt["negative_cue"] += 1
                if cited:
                    bt["cited_answers"] += 1
                    bt["citations"] += cited
                if m["mentioned"] or cited:
                    add({**base, "class": "BRAND_OBSERVED", "brand_id": b["brand_id"], "brand_origin": b["origin"],
                         "mentioned": bool(m["mentioned"]),
                         "confidence": None if not m["mentioned"] else
                         ("HIGH" if m["confidence"] == brand_mention.HIGH else "LOW_CONFIDENCE"),
                         "negative_cue": neg, "citations": cited, "evidence": ptr})
            if not detect:
                continue
            actx = {**ctx, "prompt_words": set(_words(prompt_set["prompts"][pi]))}
            found = candidates(text, actx, excluded)
            labels = {}
            for h in hosts:
                rg = registrable(h)
                if rg:
                    labels.setdefault(compact(rg.split(".")[0]), rg)
            for f in found:
                c = cands.setdefault(f["key"], {"key": f["key"], "display": {}, "answers": 0, "by_scope": {},
                                                "engines": set(), "prompts": set(), "signals": set(),
                                                "co_mentioned_with_client": 0, "co_by_scope": {},
                                                "domain_hints": {}, "evidence": [], "pointers": []})
                c["display"][f["display"]] = c["display"].get(f["display"], 0) + 1
                c["answers"] += 1
                sk = f"{eng}|{pi}"
                c["by_scope"][sk] = c["by_scope"].get(sk, 0) + 1
                c["engines"].add(eng)
                c["prompts"].add(pid)
                c["signals"] |= set(f["signals"])
                if f["key"] in labels:
                    c["signals"].add("DOMAIN_HINT")
                    c["domain_hints"][labels[f["key"]]] = c["domain_hints"].get(labels[f["key"]], 0) + 1
                if client_named:
                    c["co_mentioned_with_client"] += 1
                    c["co_by_scope"][sk] = c["co_by_scope"].get(sk, 0) + 1
                if len(c["pointers"]) < MAX_POINTERS:
                    c["pointers"].append(ptr)
                if len(c["evidence"]) < MAX_EVIDENCE:
                    c["evidence"].append({**ptr, **_excerpt(text, _approx_at(text, f["display"]))})
            for key in actx.get("lookalikes_this_answer", ()):
                ctx["lookalikes_seen"][key]["answers"] += 1
    kept = sorted(cands.values(), key=lambda c: (-c["answers"], c["key"]))[:MAX_CANDIDATES_PER_RUN]
    return {"rules": VERSION, "citation_run_id": run_id, "domain": domain, "observed_at": observed_at,
            "refresh_due": refresh_due, "emerging_detection": bool(detect),
            "prompt_set": {"prompt_set_id": prompt_set["prompt_set_id"], "version": prompt_set["version"],
                           "sha256": prompt_set["sha256"]},
            "competitor_set": {"version": competitor_set.get("version"), "sha256": competitor_set.get("sha256")},
            "brands": [{k2: v for k2, v in b.items() if k2 != "rules"} for b in bl],
            "excluded_tracked": excluded_tracked,
            "engines": {e: {"outcome": r["outcome"], "reason": r.get("reason")} for e, r in sorted(engines.items())},
            "tallies": [{**{k2: v for k2, v in k.items() if k2 != "versions"}, "model_versions": sorted(k["versions"])}
                        for _key, k in sorted(tallies.items())],
            "candidates": [{**{k2: v for k2, v in c.items() if k2 not in ("engines", "prompts", "signals")},
                            "engines": sorted(c["engines"]), "prompts": sorted(c["prompts"]),
                            "signals": sorted(c["signals"])} for c in kept],
            "candidates_truncated": max(0, len(cands) - len(kept)),
            "excluded": dict(sorted(excluded.items())),
            "client_lookalikes_seen": [{"key": k2, **v} for k2, v in sorted(ctx["lookalikes_seen"].items())],
            "rows": rows, "rows_truncated": truncated}


def narrow(obs: dict, engines: set) -> dict:
    """The observations restricted to ``engines`` (a request may narrow a reused run's engines, never widen them)."""
    if set(obs["engines"]) <= set(engines):
        return obs
    keep = set(engines)
    cands = []
    for c in obs.get("candidates") or []:
        by = {k: v for k, v in c["by_scope"].items() if k.split("|", 1)[0] in keep}
        if not by:
            continue
        co = {k: v for k, v in c.get("co_by_scope", {}).items() if k.split("|", 1)[0] in keep}
        prompts = sorted({f"{obs['prompt_set']['prompt_set_id']}:v{obs['prompt_set']['version']}:{k.split('|', 1)[1]}"
                          for k in by})
        cands.append({**c, "by_scope": by, "co_by_scope": co, "answers": sum(by.values()),
                      "co_mentioned_with_client": sum(co.values()), "prompts": prompts,
                      "engines": sorted({k.split("|", 1)[0] for k in by}),
                      "pointers": [p for p in c["pointers"] if p["engine"] in keep],
                      "evidence": [e for e in c["evidence"] if e["engine"] in keep]})
    return {**obs, "engines": {e: v for e, v in obs["engines"].items() if e in keep},
            "tallies": [t for t in obs["tallies"] if t["engine"] in keep], "candidates": cands,
            "rows": [r for r in obs.get("rows") or [] if r["engine"] in keep], "narrowed_to": sorted(keep)}


def _approx_at(text: str, display: str) -> int:
    t = " ".join((text or "").split())
    i = t.find(display)
    return max(0, i)


# ---------------------------------------------------------------------------------------------- rates

def _rate(k: int, n: int, unknown: Optional[str] = None) -> dict:
    if unknown is not None or n <= 0:
        return {"value": None, "k": k, "n": n, "ci95": None, "interval": "wilson_95",
                "unknown": unknown or "NO_DATA"}
    return {"value": round(k / n, 4), "k": k, "n": n, "ci95": wilson(k, n), "interval": "wilson_95",
            "unknown": None}


def _scope_metrics(usable: int, unresolved: int, stats: dict, bl: list, prov: dict) -> dict:
    """Per brand: presence, mention share, citation share (UNKNOWN, never 0%, when it cannot be told)."""
    total_k = sum(stats[b["brand_id"]]["mentions"] for b in bl)
    total_c = sum(stats[b["brand_id"]]["citations"] for b in bl if b["origin"] != "emerging")
    few = usable < MIN_USABLE
    out = {}
    for b in bl:
        st = stats[b["brand_id"]]
        k, low = st["mentions"], st["low"]
        unconfirmed = "MENTIONS_UNCONFIRMED" if k == 0 and low else None
        presence = _rate(k, usable, "INSUFFICIENT_ANSWERS" if few else unconfirmed)
        share = _rate(k, total_k, "INSUFFICIENT_ANSWERS" if few else unconfirmed
                      or ("NO_BRAND_MENTIONED" if total_k == 0 else None))
        if share["unknown"] is None:
            share["note"] = "approximate: brand mentions within one answer are not independent (directional)"
        if b["origin"] == "emerging" or not b.get("domains"):
            cite = _rate(st["citations"], total_c, "NO_VERIFIED_DOMAIN")
        elif few:
            cite = _rate(st["citations"], total_c, "INSUFFICIENT_ANSWERS")
        elif total_c == 0:
            cite = _rate(0, 0, "CITATIONS_UNRESOLVED" if unresolved else "NO_TRACKED_BRAND_CITED")
        else:
            cite = {**_rate(st["citations"], total_c), "incomplete": unresolved > 0,
                    "unresolved_citations_excluded": unresolved}
        out[b["brand_id"]] = {"brand_id": b["brand_id"], "origin": b["origin"], "presence": presence,
                              "mention_share": share, "citation_share": cite, "low_confidence_mentions": low,
                              "negative_cue_mentions": st["negative_cue"], "cited_answers": st["cited_answers"],
                              "provenance": prov}
    return out


# ---------------------------------------------------------------------------------------------- emerging

EMERGING_RULE = (f"a candidate is EMERGING_COMPETITOR when it appeared in at least {EMERGING_MIN_ANSWERS} usable "
                 f"answers over the look-back AND on at least {EMERGING_MIN_SPREAD} engines or in at least "
                 f"{EMERGING_MIN_SPREAD} runs AND carries at least one brand signal ({', '.join(SIGNALS)}); "
                 f"confidence HIGH when also seen in at least {HIGH_MIN_ANSWERS} answers, on 2+ engines, in 2+ runs, "
                 "with its own domain cited (DOMAIN_HINT), else MEDIUM; anything less is WATCH (never folded in)")


def emerging(current: dict, history: list, dismissals: dict, tracked_terms: list, client_terms: list) -> dict:
    """The emerging-competitor view over the current run's observations and earlier ones (``history``)."""
    agg: dict = {}
    for obs in [current] + list(history):
        for c in obs.get("candidates") or []:
            a = agg.setdefault(c["key"], {"key": c["key"], "display": {}, "answers": 0, "engines": set(),
                                          "prompts": set(), "signals": set(), "runs": set(),
                                          "co_mentioned_with_client": 0, "domain_hints": {}, "evidence": [],
                                          "first_seen_at": obs["observed_at"], "answers_this_run": 0})
            for d, n in c["display"].items():
                a["display"][d] = a["display"].get(d, 0) + n
            a["answers"] += c["answers"]
            a["engines"] |= set(c["engines"])
            a["prompts"] |= set(c["prompts"])
            a["signals"] |= set(c["signals"])
            a["runs"].add(obs["citation_run_id"])
            a["co_mentioned_with_client"] += c["co_mentioned_with_client"]
            for h, n in c["domain_hints"].items():
                a["domain_hints"][h] = a["domain_hints"].get(h, 0) + n
            a["first_seen_at"] = min(a["first_seen_at"], obs["observed_at"])
            if obs is current:
                a["answers_this_run"] = c["answers"]
            for ev in c["evidence"]:
                if len(a["evidence"]) < MAX_EVIDENCE:
                    a["evidence"].append(ev)
    items, watch, dismissed_seen = [], [], []
    for key, a in sorted(agg.items()):
        if any(key == compact(t) or _contains_words(_best(a["display"]), t) for t in tracked_terms):
            continue                                  # tracked now (the set changed since): not emerging
        if any(lookalike_name(_best(a["display"]), t) for t in client_terms):
            continue
        view = {"key": key, "name": _best(a["display"]), "answers": a["answers"],
                "answers_this_run": a["answers_this_run"], "engines": sorted(a["engines"]),
                "prompts": sorted(a["prompts"]), "runs": len(a["runs"]), "signals": sorted(a["signals"]),
                "co_mentioned_with_client": a["co_mentioned_with_client"],
                "domain_hints_unverified": sorted(a["domain_hints"], key=lambda h: (-a["domain_hints"][h], h))[:3],
                "first_seen_at": a["first_seen_at"], "evidence": a["evidence"]}
        if key in dismissals:
            if a["answers_this_run"]:
                dismissed_seen.append({**view, "state": "DISMISSED", "dismissed": dismissals[key]})
            continue
        spread = len(a["engines"]) >= EMERGING_MIN_SPREAD or len(a["runs"]) >= EMERGING_MIN_SPREAD
        if a["answers"] >= EMERGING_MIN_ANSWERS and spread and a["signals"]:
            high = a["answers"] >= HIGH_MIN_ANSWERS and len(a["engines"]) >= 2 and len(a["runs"]) >= 2 \
                and "DOMAIN_HINT" in a["signals"]
            items.append({**view, "state": "EMERGING_COMPETITOR", "confidence": "HIGH" if high else "MEDIUM"})
        else:
            watch.append({**view, "state": "WATCH", "confidence": "LOW",
                          "missing": [x for x, ok in (("answers", a["answers"] >= EMERGING_MIN_ANSWERS),
                                                      ("spread", spread), ("brand_signal", bool(a["signals"])))
                                      if not ok]})
    order = lambda v: (-v["answers"], -v["answers_this_run"], v["key"])  # noqa: E731
    items.sort(key=order)
    watch.sort(key=order)
    for i, it in enumerate(items):
        it["folded_into_benchmark"] = i < MAX_EMERGING_FOLDED
    return {"rule": EMERGING_RULE, "items": items, "watch": watch[:MAX_WATCH_LISTED],
            "watch_total": len(watch), "dismissed_seen": sorted(dismissed_seen, key=order),
            "runs_considered": 1 + len(history)}


def _best(display: dict) -> str:
    return sorted(display.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if display else ""


# ---------------------------------------------------------------------------------------------- the benchmark

ORDER_RULE = ("opportunities ordered by the gap in answers between the most-present competitor and the client "
              "(descending), then prompt index, then engine. An ordering rule, not a score.")


def compute(obs: dict, history: list, dismissals: dict, previous: Optional[dict], run: dict, visibility_run_id: str,
            generated_at: str, source: str) -> dict:
    """Idris's envelope for one competitor-visibility run. ``obs``: the citation run's recorded observations;
    ``history``: earlier observations of the tenant's domain (look-back); ``dismissals``: key -> record;
    ``previous``: the previous comparable visibility result's facts (or None); ``run``: the citation run (id,
    outcome, completed_at)."""
    method = ("Idris read the answers of citation run {cid} (source: {src}); per usable answer: which benchmark brand "
              "is named (agents/brand_mention.py, the common-word rule applies to competitor names too) and which "
              "tracked brand's domain each structured citation lands on (Callum's classification decides the client "
              "part). Presence = answers naming the brand / usable answers; mention share = answers naming it / "
              "brand-answer pairs; citation share = citations on its domains / citations on any tracked brand's "
              f"domain; 95% Wilson intervals; UNKNOWN under {MIN_USABLE} usable answers or when it cannot be told. "
              "Emerging competitors: a deterministic proper-noun extractor and stated thresholds (no model). "
              f"Rules {VERSION}.").format(cid=obs.get("citation_run_id"), src=source)
    tracked_terms = [t for b in obs["brands"] if b["origin"] == "tracked" for t in b["terms"]]
    client_terms = next(b["terms"] for b in obs["brands"] if b["origin"] == "client")
    em = emerging(obs, history, dismissals, tracked_terms, client_terms) if obs.get("emerging_detection") else \
        {"rule": EMERGING_RULE, "items": [], "watch": [], "watch_total": 0, "dismissed_seen": [], "runs_considered": 0,
         "off": "emerging detection is off for this competitor set"}
    folded = [{"brand_id": f"emg:{it['key']}", "name": it["name"], "origin": "emerging", "terms": [it["name"]],
               "domains": [], "key": it["key"]} for it in em["items"] if it["folded_into_benchmark"]]
    bl = list(obs["brands"]) + folded
    by_key = {c["key"]: c for c in obs.get("candidates") or []}
    scopes, by_engine, tot = [], {}, {"usable": 0, "unresolved": 0, "stats": {b["brand_id"]: _zero() for b in bl}}
    for t in obs["tallies"]:
        stats = {bid: dict(v) for bid, v in t["brands"].items()}
        for f in folded:
            stats[f["brand_id"]] = {**_zero(), "mentions": by_key.get(f["key"], {}).get("by_scope", {}).get(
                f"{t['engine']}|{t['prompt_index']}", 0)}
        prov = {"data_class": "measured", "source": "callum_citation_run_answers",
                "citation_run_id": obs["citation_run_id"], "engine": t["engine"], "prompt_id": t["prompt_id"],
                "prompt_set_sha256": obs["prompt_set"]["sha256"], "competitor_set_sha256": obs["competitor_set"]["sha256"],
                "rules": VERSION, "model_versions": t["model_versions"], "observed_at": obs["observed_at"]}
        scopes.append({"engine": t["engine"], "prompt_id": t["prompt_id"], "prompt_index": t["prompt_index"],
                       "usable_answers": t["usable"], "refusals": t["refusals"], "inconclusive_calls": t["failed"],
                       "citations": t["citations"], "unresolved_citations": t["unresolved_citations"],
                       "publisher_citations": t["publisher_citations"],
                       "client_lookalike_citations": t["client_lookalike_citations"],
                       "brands": _scope_metrics(t["usable"], t["unresolved_citations"], stats, bl, prov)})
        e = by_engine.setdefault(t["engine"], {"usable": 0, "unresolved": 0, "failed": 0,
                                               "stats": {b["brand_id"]: _zero() for b in bl}})
        for agg in (e, tot):
            agg["usable"] += t["usable"]
            agg["unresolved"] += t["unresolved_citations"]
            for bid, v in stats.items():
                for x in v:
                    agg["stats"][bid][x] += v[x]
        e["failed"] += t["failed"]
    pooled_note = "pooled over prompts (and engines for the totals): prompts are not a random sample; directional"
    engines_view = {}
    for eng, e in sorted(by_engine.items()):
        prov = {"data_class": "measured", "source": "callum_citation_run_answers",
                "citation_run_id": obs["citation_run_id"], "engine": eng, "rules": VERSION, "note": pooled_note}
        engines_view[eng] = {"usable_answers": e["usable"], "inconclusive_calls": e["failed"],
                             "unresolved_citations": e["unresolved"],
                             "outcome": (obs["engines"].get(eng) or {}).get("outcome"),
                             "brands": _scope_metrics(e["usable"], e["unresolved"], e["stats"], bl, prov)}
    for eng, info in sorted(obs["engines"].items()):
        engines_view.setdefault(eng, {"usable_answers": 0, "inconclusive_calls": 0, "unresolved_citations": 0,
                                      "outcome": info["outcome"], "brands": {}})
    totals = {"usable_answers": tot["usable"], "unresolved_citations": tot["unresolved"],
              "brands": _scope_metrics(tot["usable"], tot["unresolved"], tot["stats"], bl,
                                       {"data_class": "measured", "source": "callum_citation_run_answers",
                                        "citation_run_id": obs["citation_run_id"], "rules": VERSION,
                                        "note": pooled_note})}
    opps, strengths = _opportunities(scopes, bl, previous)
    facts = {"contract": CONTRACT, "rules": VERSION, "visibility_run_id": visibility_run_id, "source": source,
             "citation_run_id": obs["citation_run_id"], "citation_run_outcome": run.get("outcome"),
             "observed_at": obs["observed_at"], "refresh_tier": REFRESH_TIER, "refresh_due": obs["refresh_due"],
             "generated_at": generated_at, "domain": obs["domain"], "prompt_set": obs["prompt_set"],
             "competitor_set": obs["competitor_set"],
             "brands": [{k: v for k, v in b.items() if k != "terms"} for b in bl],
             "excluded_tracked": obs.get("excluded_tracked") or [],
             "benchmark": {"by_engine_prompt": scopes, "by_engine": engines_view, "totals": totals},
             "opportunities": {"order_rule": ORDER_RULE, "items": opps}, "strengths": strengths,
             "emerging": em, "candidate_exclusions": obs.get("excluded") or {},
             "client_lookalikes_seen": obs.get("client_lookalikes_seen") or [],
             "rows": _rows(obs, folded, by_key), "rows_truncated": obs.get("rows_truncated", 0),
             "data_class": "measured", "effect_class": None}
    outcome = run.get("outcome") or "INSUFFICIENT_EVIDENCE"
    if outcome in ("NOT_CONNECTED", "KILLED", "RESTRICTED", "QUARANTINED"):
        return envelope(AGENT, TASK, outcome, [], methodology=method, limitations=LIMITS, facts=facts,
                        reason="the citation run observed nothing; no share of voice can be told")
    return envelope(AGENT, TASK, outcome, _findings(facts, obs), methodology=method, limitations=LIMITS, facts=facts)


def _zero() -> dict:
    return {"mentions": 0, "low": 0, "negative_cue": 0, "cited_answers": 0, "citations": 0}


def _rows(obs: dict, folded: list, by_key: dict) -> list:
    rows = list(obs.get("rows") or [])
    for f in folded:
        for p in by_key.get(f["key"], {}).get("pointers", []):
            if len(rows) >= MAX_ROWS:
                break
            rows.append({"engine": p["engine"], "model": p["model"], "model_version": p["model_version"],
                         "prompt_id": p["prompt_id"], "prompt_index": p["prompt_index"], "sample": p["sample"],
                         "observed_at": obs["observed_at"], "provider_asked_at": p["provider_asked_at"],
                         "refresh_tier": REFRESH_TIER, "citation_run_id": obs["citation_run_id"],
                         "class": "BRAND_OBSERVED", "brand_id": f["brand_id"], "brand_origin": "emerging",
                         "mentioned": True, "confidence": "EXTRACTED", "negative_cue": None, "citations": None,
                         "evidence": p})
    return rows


def _overlap(a: Optional[list], b: Optional[list]) -> bool:
    return a is None or b is None or not (a[1] < b[0] or b[1] < a[0])


def _opportunities(scopes: list, bl: list, previous: Optional[dict]) -> tuple:
    prev = {}
    for p in ((previous or {}).get("opportunities") or {}).get("items") or []:
        prev[(p["engine"], p["prompt_id"])] = p
    for p in (previous or {}).get("strengths") or []:
        prev.setdefault((p["engine"], p["prompt_id"]), p)
    names = {b["brand_id"]: b["name"] for b in bl}
    opps, strengths = [], []
    for s in scopes:
        n = s["usable_answers"]
        if n < MIN_USABLE:
            continue
        client = s["brands"]["client"]["presence"]
        if client["unknown"] is not None:
            continue
        others = [(bid, m["presence"]) for bid, m in s["brands"].items() if bid != "client"
                  and m["presence"]["unknown"] is None]
        if not others:
            continue
        lid, lead = sorted(others, key=lambda x: (-x[1]["k"], x[0]))[0]
        gap = round(lead["value"] - client["value"], 4)
        p = prev.get((s["engine"], s["prompt_id"]))
        trend, within = "NO_PREVIOUS", None
        if p is not None:
            pg = p.get("gap", 0.0)
            within = _overlap(p.get("client_presence", {}).get("ci95"), client["ci95"])
            if gap <= 0 < pg:
                trend = "GAP_CLOSED"
            elif gap < pg:
                trend = "GAP_CLOSING"
            elif gap > pg:
                trend = "GAP_WIDENING"
            else:
                trend = "UNCHANGED"
        base = {"engine": s["engine"], "prompt_id": s["prompt_id"], "prompt_index": s["prompt_index"],
                "client_presence": client, "gap": gap, "trend": trend, "within_noise": within,
                "previous_gap": None if p is None else p.get("gap")}
        if lead["k"] > client["k"]:
            sep = not _overlap(lead["ci95"], client["ci95"])
            opps.append({**base, "leader": {"brand_id": lid, "name": names.get(lid), "presence": lead},
                         "gap_answers": lead["k"] - client["k"],
                         "decision": "TEST" if sep else "INSUFFICIENT_EVIDENCE",
                         "headline": f"Room to grow: {names.get(lid)} appears in {lead['k']} of {n} {s['engine']} "
                                     f"answers to this prompt, you in {client['k']}."
                                     + (" The gap is closing since the last run." if trend in ("GAP_CLOSING",)
                                        else "")})
        elif client["k"] > 0:
            strengths.append({**base, "headline": f"You lead: named in {client['k']} of {n} {s['engine']} answers "
                                                  "to this prompt, at least as often as any tracked competitor."
                                                  + (" Gap closed since the last run." if trend == "GAP_CLOSED"
                                                     else "")})
    opps.sort(key=lambda o: (-o["gap_answers"], o["prompt_index"], o["engine"]))
    for rank, o in enumerate(opps, start=1):
        o["rank"] = rank
    return opps, strengths


def _findings(facts: dict, obs: dict) -> list:
    out = []
    for o in facts["opportunities"]["items"]:
        out.append(finding("IDRIS_SHARE_OF_VOICE_GAP", "medium", "measured", o["decision"], capability="P9",
                           detail={"engine": o["engine"], "prompt_id": o["prompt_id"], "leader": o["leader"],
                                   "client_presence": o["client_presence"], "gap_answers": o["gap_answers"],
                                   "trend": o["trend"]}))
    for o in facts["opportunities"]["items"] + facts["strengths"]:
        if o["trend"] in ("GAP_CLOSING", "GAP_CLOSED"):
            out.append(finding("IDRIS_GAP_CLOSING", "info", "measured",
                               "INSUFFICIENT_EVIDENCE" if o["within_noise"] else "WATCH", capability="P9",
                               detail={"engine": o["engine"], "prompt_id": o["prompt_id"], "trend": o["trend"],
                                       "gap": o["gap"], "previous_gap": o["previous_gap"],
                                       "within_noise": o["within_noise"]}))
    for it in facts["emerging"]["items"]:
        out.append(finding("IDRIS_EMERGING_COMPETITOR", "medium", "inferred", "WATCH", capability="P9",
                           detail={"key": it["key"], "name": observed(it["name"], 80), "confidence": it["confidence"],
                                   "answers": it["answers"], "engines": it["engines"], "runs": it["runs"],
                                   "signals": it["signals"], "folded_into_benchmark": it["folded_into_benchmark"]}))
    for x in facts["client_lookalikes_seen"]:
        out.append(finding("IDRIS_CLIENT_LOOKALIKE_NAMED", "medium", "measured", "WATCH", capability="P9",
                           detail={"key": x["key"], "name": observed(x["display"], 80)}))
    for x in facts["excluded_tracked"]:
        out.append(finding("IDRIS_TRACKED_COMPETITOR_EXCLUDED", "low", "measured", "WATCH", capability="P9",
                           detail=dict(x)))
    for e, info in sorted(obs["engines"].items()):
        if info["outcome"] in ("BLOCKED", "FAILED"):
            out.append(finding("ENGINE_RUN_STOPPED", "low", "measured", "WATCH", capability="P9",
                               detail={"engine": e, "reason": info.get("reason"), "outcome": info["outcome"]}))
    return out



def competitor_id(name: str) -> str:
    """A tracked competitor's id: derived from its compact name (stable across set versions)."""
    return "cmp-" + sha(compact(name))[:16]


def candidate_key_valid(key: str) -> bool:
    """A dismissal key is a candidate key: already in compact form."""
    return isinstance(key, str) and 2 <= len(key) <= 60 and compact(key) == key
