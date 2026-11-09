"""
Callum — Citation Source agent (ADR 0046). ONE job: classify where and how AI answer engines cite the client.

For every tracked prompt x engine answer Callum traces the exact URLs the engine attributed to the answer (its
structured citations only, through the multi-engine adapter, answer_adapter.py) and classifies each one:

  STRENGTH_URL          a page on the client's own site (exact host, the www. twin or a subdomain, IDNA-aware:
                        agents/domain_match.py) — after following the cited URL's redirects when resolution is on.
                        Never a URL cited on a lookalike host, wherever it redirects (review round 1 M1)
  CANONICAL_TO_CLIENT   a THIRD-PARTY page whose canonical names the client's page (syndicated copy). The third party
                        controls that tag, so it is never a STRENGTH_URL: counted and reported apart, excluded from
                        cited_strength (review round 1 M2)
  OPPORTUNITY_URL       a third-party page cited where the client's could be (competitor pages flagged)
  LOOKALIKE_URL         the cited host (or where it lands) folds to the client's domain on the shared lookalike table
                        but is not it: never counted as the client (brand-impersonation risk, reported apart); one that
                        redirects ONTO the client is flagged REDIRECTED_ONTO_CLIENT, a risk signal of its own
  INCONCLUSIVE          the citation's target is unknown: any URL on a redirector host (vertexaisearch.cloud.google.com,
                        any path) not resolved, or a URL that cannot be read

and, per answer, the metric of its own:

  MENTIONED_NOT_CITED   a brand term is named in the answer text and no citation of that answer is a STRENGTH_URL
                        (an answer with an INCONCLUSIVE citation cannot be MNC: it is INCONCLUSIVE, reason
                        MENTION_WITH_UNRESOLVED_CITATIONS). Brand matching: agents/brand_mention.py. A mention made
                        only by a term that may be a common word, with no disambiguation configured, is
                        ``confidence: LOW_CONFIDENCE``: reported apart, never in the headline rate or the MNC list

An engine call that failed is an INCONCLUSIVE row with the engine's code — never "not cited". A refusal is counted
apart and excluded from every rate, as are errors. Every rate carries its n and a 95% Wilson interval; under
``MIN_USABLE`` usable answers it is marked ``directional``. No single visibility score.

The Mentioned-Not-Cited priority list (``CONTRACT_MNC``) is Callum's OUTPUT CONTRACT for Roman (content structure)
and Adaeze (content gap); neither consumer is built (ADR 0046).

Provider text is DATA: scanned in memory for brand terms; only a bounded excerpt (``EXCERPT_MAX`` characters, marked
untrusted, withheld when it looks like personal data) is kept for the dashboard drill-down. Cited URLs are kept
without their query string or fragment (``query_dropped``), a path that looks like personal data is withheld.

No agent touches service state, the ledger or the log: the service records what this module returns.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import unquote, urljoin, urlunsplit

from agents import brand_mention, domain_match, secret_guard, split_url
from agents.probes import (
    MIN_USABLE,
    REDIRECTOR_HOSTS,
    WARN_COMMON,
    _switch_code,
    extract_citations,
    wilson,
)
from agents.prompt_guard import personal_data
from answer_adapter import PROTOCOL_ERROR_STOP_AFTER, STOP_CODES, EngineError
from clock import iso
from envelope import envelope, finding, observed
from ports import NOT_CONNECTED, ProviderAnswer
from primitives import Killed

AGENT, TASK = "callum", "citation_source_rows"
VERSION = "callum-rules/2026-11-08.2"
CONTRACT_MNC = "callum.mnc_priority/2"           # /2: cited_instead[].canonical_to_client (review round 1 M2)
CLASSES = ("STRENGTH_URL", "CANONICAL_TO_CLIENT", "OPPORTUNITY_URL", "LOOKALIKE_URL", "MENTIONED_NOT_CITED",
           "INCONCLUSIVE")
REFRESH_TIER = "near_real_time"
REFRESH_INTERVAL = timedelta(hours=1)             # a citation run is due again after this (near-real-time tier)
EXCERPT_MAX = 160
URL_MAX = 500
MAX_ROWS = 2000
MAX_EVIDENCE_PER_ITEM = 3
# hosts whose URLs are a redirect to the cited page, not the page itself (google_engine.py: the grounding chunk
# URIs of the Gemini API). The WHOLE host is a redirector, whatever the path (review round 1 M3): the format of
# Gemini's grounding URIs is UNVERIFIED until a first live call (the docs show truncated examples only), so no path
# is trusted to be the page. A citation on one is INCONCLUSIVE until resolved, never OPPORTUNITY. The host list is
# REDIRECTOR_HOSTS in agents/probes.py (one list for the audit and the citation runs).
LIMITS = [
    ("One sample of N answers per prompt and engine, at one time, from one account: answer engines vary by user, "
     "location, time and model version. Observations, not search truth."),
    ("Cited means attributed by the engine's own structured citations (each engine's rule is in its adapter); "
     "whether a reader clicks it is not measured."),
    ("A cited URL's redirects and canonical are followed only when SEO_CALLUM_RESOLVE_URLS=1 (one fetch per "
     "distinct URL, robots.txt honoured, bounded); otherwise a redirector URL is INCONCLUSIVE and every other "
     "URL is classified by its own host."),
    ("Gemini's grounding URI format is unverified until a first live call: every URL on the redirector host "
     "(any path) is treated as a redirect whose target is unknown until resolved."),
    ("A brand mention is a whole-word match (possessives yes, plurals no); a single-word term may be an ordinary "
     "word, so without the tenant's disambiguation its mentions are LOW_CONFIDENCE and kept out of the headline "
     "rate. Matching is lexical: it does not understand the answer."),
    "Query strings and fragments of cited URLs are not kept; two URLs that differ only there count as one page.",
    "No prompt-volume source is connected: how often real users ask these prompts is unknown.",
    "Nothing here is calibrated against traffic or revenue; no single visibility score is produced.",
]


# ---------------------------------------------------------------------------------------------- collection

def collect(engine: str, port, prompt_set: dict, n: int, guard: Callable, budget=None) -> dict:
    """Ask one engine every prompt ``n`` times through the adapter. Returns {"engine", "outcome", "reason",
    "reason_detail", "samples": [...], "usage", "prompts_not_asked"}. The run rules are the probe framework's
    (agents/probes.py): a METERED port only inside a recorded reservation, the stop codes, two protocol errors in a
    row stop the engine, the 4x cost guard, prompts with personal data never sent. A sample's ``text`` lives in
    memory only (classify() reads it; the service never records it)."""
    if port is None or not getattr(port, "connected", False):
        return {"engine": engine, "outcome": NOT_CONNECTED, "reason": None, "reason_detail": None, "samples": [],
                "usage": None, "prompts_not_asked": len(prompt_set["prompts"])}
    metered = bool(getattr(port, "metered", False))
    takes_guard = bool(getattr(port, "accepts_guard", False))
    attempts = int(getattr(port, "attempts", 1) or 1)
    if metered and budget is None:
        return {"engine": engine, "outcome": "BLOCKED", "reason": "ENGINE_BUDGET_EXHAUSTED",
                "reason_detail": "NO_BUDGET_HOOK", "samples": [], "usage": None,
                "prompts_not_asked": len(prompt_set["prompts"])}
    run = budget.open(engine, getattr(port, "model", None)) if metered else None
    samples: list = []
    stop = stop_detail = None
    answers = errors = 0
    protocol_run = 0
    asked = 0
    try:
        for pi, prompt in enumerate(prompt_set["prompts"]):
            if stop is not None:
                break
            asked += 1
            if personal_data(prompt) is not None:          # never sent; the row does not quote it either
                samples.append(_failed(pi, 0, "PROMPT_REFUSED", "PERSONAL_DATA"))
                continue
            meter = None
            if run is not None:
                try:
                    meter = run.reserve(pi, n * attempts)
                except EngineError as e:
                    if e.code == "ENGINE_KILLED":
                        raise Killed(_switch_code(e)) from None
                    stop, stop_detail = e.code, e.detail
                    asked -= 1
                    break
            for si in range(n):
                guard(capability="ai_probe", provider=engine)
                try:
                    a = port.ask(prompt, guard=guard, meter=meter) if takes_guard else port.ask(prompt)
                except Killed:
                    raise
                except EngineError as e:
                    errors += 1
                    if e.code == "ENGINE_KILLED":
                        raise Killed(_switch_code(e)) from None
                    samples.append(_failed(pi, si, e.code, e.detail))
                    if meter is not None and getattr(meter, "over_reserve", False):
                        stop, stop_detail = "ENGINE_BUDGET_EXHAUSTED", "COST_ABOVE_RESERVE"
                        break
                    if e.code == "ENGINE_PROTOCOL_ERROR":
                        protocol_run += 1
                        if protocol_run >= PROTOCOL_ERROR_STOP_AFTER:
                            stop, stop_detail = e.code, "REPEATED_PROTOCOL_ERRORS"
                            break
                    if e.code in STOP_CODES:
                        stop, stop_detail = e.code, e.detail
                        break
                    continue
                except Exception:  # noqa: BLE001 - a provider failure is counted, never raised
                    errors += 1
                    samples.append(_failed(pi, si, "ENGINE_UNAVAILABLE", "ADAPTER_FAILED"))
                    continue
                if not isinstance(a, ProviderAnswer) or not isinstance(a.text, str) \
                        or not isinstance(a.citations, list) or a.status not in ("ANSWER", "REFUSAL", "ERROR",
                                                                                 NOT_CONNECTED):
                    errors += 1
                    samples.append(_failed(pi, si, "ENGINE_PROTOCOL_ERROR", "MALFORMED_ANSWER"))
                    continue
                if a.status == NOT_CONNECTED:
                    return {"engine": engine, "outcome": NOT_CONNECTED, "reason": None, "reason_detail": None,
                            "samples": [], "usage": None, "prompts_not_asked": len(prompt_set["prompts"])}
                if a.status == "ERROR":
                    errors += 1
                    samples.append(_failed(pi, si, "ENGINE_UNAVAILABLE", "ENGINE_REPORTED_ERROR", a))
                    continue
                samples.append({"prompt_index": pi, "sample": si, "status": a.status, "text": a.text or "",
                                "answer": a})
                if a.status == "ANSWER":
                    answers += 1
                    protocol_run = 0
                if meter is not None and getattr(meter, "over_reserve", False):
                    stop, stop_detail = "ENGINE_BUDGET_EXHAUSTED", "COST_ABOVE_RESERVE"
                    break
        if stop is None and not answers and errors:
            codes: dict = {}
            for s in samples:
                if s["status"] == "FAILED":
                    codes[s["error_code"]] = codes.get(s["error_code"], 0) + 1
            stop = min(codes.items(), key=lambda kv: (-kv[1], kv[0]))[0] if codes else "ENGINE_UNAVAILABLE"
            stop_detail = "NO_ANSWERS"
    finally:
        usage = run.settle(answers, stop) if run is not None else None
    if stop_detail == "NO_ANSWERS":
        outcome = "FAILED"
    elif stop is None:
        outcome = "OK"
    elif answers:
        outcome = "PARTIAL"
    else:
        outcome = {"ENGINE_BUDGET_EXHAUSTED": "BLOCKED", "ENGINE_NOT_CONNECTED": NOT_CONNECTED}.get(stop, "FAILED")
    return {"engine": engine, "outcome": outcome, "reason": stop, "reason_detail": stop_detail, "samples": samples,
            "usage": usage, "prompts_not_asked": len(prompt_set["prompts"]) - asked}


def _failed(pi: int, si: int, code: str, detail: str | None, a: ProviderAnswer | None = None) -> dict:
    return {"prompt_index": pi, "sample": si, "status": "FAILED", "error_code": code, "error_detail": detail,
            "text": "", "answer": a}


# ---------------------------------------------------------------------------------------------- URL resolution

class Resolver:
    """Follows a cited URL's redirects (and reads its canonical) through the service's SSRF-safe fetcher
    (primitives/fetch.py: public addresses only, ports 80 / 443, redirects by hand with a cap, robots.txt honoured
    for ZBM-SEO-Audit, the run's kill switches before every hop). OFF unless SEO_CALLUM_RESOLVE_URLS=1; at most
    ``max_urls`` distinct URLs per run. Bodies are parsed in memory and dropped."""

    def __init__(self, fetcher, guard: Callable, enabled: bool, max_urls: int):
        self.fetcher, self.guard = fetcher, guard
        self.enabled = bool(enabled) and fetcher is not None
        self.max_urls = max_urls
        self.cache: dict = {}
        self.robots: dict = {}
        self.fetches = 0

    def resolve(self, url: str) -> dict:
        if url in self.cache:
            return self.cache[url]
        if not self.enabled:
            out = {"state": "OFF"}
        elif self.fetches >= self.max_urls:
            out = {"state": "CAP_REACHED"}
        else:
            self.fetches += 1
            out = self._fetch(url)
        self.cache[url] = out
        return out

    def _fetch(self, url: str) -> dict:
        from primitives.parse import parse_html
        try:
            r = self.fetcher.fetch(url, robots_cache=self.robots, guard=self.guard)
        except Killed:
            r = None
        except Exception:  # noqa: BLE001 - a resolution failure is reported, never raised
            return {"state": "FAILED", "detail": "FETCH_ERROR"}
        if r is None or r.state == "KILLED":
            # a switch on fetching (provider:web, capability:fetch, or a run-wide one): no further URL is fetched in
            # this run; the engines' answers stand, and a run-wide switch is caught again before the result is
            # recorded (svc_citations.py)
            self.enabled = False
            return {"state": "KILLED"}
        if r.state != "OK":
            # WR-F014: the hosts of a chain that stopped are every followed hop, where it stopped, and a refused
            # redirect's target (raw Unicode included: domain_match converts it with IDNA) — never dropped
            hops = _hop_hosts(r.redirects)
            if r.redirects and r.final_url:
                hops.append(_host(r.final_url)[:253])
            refused = getattr(r, "refused_redirect", None)
            if isinstance(refused, dict) and isinstance(refused.get("location"), str):
                try:
                    target = urljoin(r.final_url or url, refused["location"])
                except ValueError:
                    target = refused["location"]
                hops = (hops or [_host(url)[:253]]) + [_target_host(target)]
            return {"state": "FAILED", "detail": r.state, "redirects": len(r.redirects) + (1 if refused else 0),
                    "hop_hosts": hops[:20], "redirect_refused": bool(refused)}
        canonical = None
        if (r.content_type or "").startswith("text/html") and r.body:
            try:
                ex = parse_html(r.text(), r.final_url or url)
                if len(ex.get("canonicals") or []) == 1 and not ex.get("canonicals_invalid"):
                    canonical = ex["canonicals"][0]
            except Exception:  # noqa: BLE001 - an unreadable page has no canonical
                canonical = None
        return {"state": "RESOLVED", "final_url": r.final_url or url, "redirects": len(r.redirects),
                "hop_hosts": _hop_hosts(r.redirects),
                "status": r.status, "canonical_url": canonical}


# ---------------------------------------------------------------------------------------------- classification

def is_redirector(url: str) -> bool:
    p = split_url(url)
    if p is None or not p.hostname:
        return False
    return p.hostname.lower().rstrip(".") in REDIRECTOR_HOSTS


def url_has_secret(url: str | None) -> bool:
    """A URL whose path, query, fragment or user part looks like it carries a credential: never fetched (WR-F013)."""
    p = split_url(url) if url else None
    if p is None:
        return False
    return secret_guard.url_has_secret(url)


_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)|\+\d{10,15}(?!\d)")
# E.164, any country code: "+" then 7-15 digits, single spaces / dots / dashes / brackets allowed between digits
_E164 = re.compile(r"(?<![\d+])\+[1-9](?:[\s.\-()]{0,2}\d){6,14}(?!\d)")
# an e-mail address needs an alphabetic top-level domain (jsDelivr's ``pkg@1.2.3`` is a package version, not one)
_EMAIL_SEG = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![A-Za-z0-9-])")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _segment_personal(seg: str) -> str | None:
    """One path segment's personal data: an e-mail or IP (the prompt guard), or a number only when it is shaped like
    a phone number, an SSN or a payment card (13-19 digits passing Luhn); a date or an id is not."""
    forms = secret_guard.decodings(seg)
    x = forms[-1]
    if any(_E164.search(f) for f in forms):
        return "number"                                   # an international phone number, any country code
    xr = _separator_reading(x)
    kind = personal_data(x) or personal_data(xr)
    if kind == "email":
        return "email" if _EMAIL_SEG.search(x) else None
    if kind == "ip":
        return kind
    if kind is None:
        return None
    for form in (x, xr):
        if _SSN.search(form) or _PHONE.search(form):
            return "number"
        for m in _CARD.finditer(form):
            d = re.sub(r"\D", "", m.group(0))
            if 13 <= len(d) <= 19 and _luhn(d) and (len(d) in (15, 16) or m.group(0) != d):
                return "number"
    return None


def stored_url(url: str | None) -> dict | None:
    """What a row keeps of a URL: scheme, host and path (query and fragment dropped), bounded; a path that looks like
    personal data is withheld."""
    if not url:
        return None
    p = split_url(url)
    if p is None or not p.hostname:
        return {"url": None, "host": None, "query_dropped": False, "withheld": "UNREADABLE"}
    path = p.path or "/"
    withheld = None
    segs = path.split("/")
    bad, _ = secret_guard.url_secret_segments(url)
    if bad:
        # WR-F013: a secret-shaped path segment is replaced, never kept; host and the other segments stay (the
        # allowed shapes — commit SHAs on code hosts, hashed asset names, document ids, shorteners — are kept)
        path, withheld = "/".join("{secret}" if i in bad else x for i, x in enumerate(segs)), "SECRET_REDACTED"
    # final review: only the OFFENDING segment is withheld, and a date or a numeric / alphanumeric id (DOI, arXiv,
    # item ids, status ids, question ids, news date paths) is not personal data unless the phone / SSN / card shapes
    # fire. An e-mail or an IP address anywhere in the path still withholds the whole path.
    segs = path.split("/")
    pii = [i for i, x in enumerate(segs) if x and _segment_personal(x)]
    if pii:
        path = "/".join("{withheld}" if i in pii else x for i, x in enumerate(segs))
        withheld = withheld or "PERSONAL_DATA"
    whole = personal_data(unquote(path))
    if whole == "ip" or (whole == "email" and _EMAIL_SEG.search(unquote(path))):
        path, withheld = "/{withheld}", "PERSONAL_DATA"
    host = p.hostname.lower()[:253]
    netloc = host if p.port in (None, 80, 443) else f"{host}:{p.port}"
    kept = urlunsplit((p.scheme.lower(), netloc, path, "", ""))[:URL_MAX]
    return {"url": kept, "host": host, "query_dropped": bool(p.query or p.fragment), "withheld": withheld}


def _hop_hosts(redirects) -> list:
    """The host of every URL the fetcher was redirected FROM, in order (the cited URL first; round 2 item 1). Hosts
    only, bounded: the paths of intermediate hops are not kept."""
    out = []
    for h in redirects or ():
        u = h.get("url") if isinstance(h, dict) else None
        out.append(_host(u)[:253])
    return out[:20]


def _target_host(url: str) -> str:
    """The host of a refused redirect target, as written (raw Unicode kept; never fetched)."""
    h = _host(url)
    if not h:
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#@]*@)?([^/?#:]+)", url or "")
        h = m.group(2).lower() if m else ""
    return h[:253]


def _host(url: str | None) -> str:
    p = split_url(url) if url else None
    return (p.hostname or "").lower() if p is not None else ""


def classify_citation(url: str, domain: str, competitors: list, res: dict) -> dict:
    """One cited URL -> its class, the URL that decided it, how (direct / redirect / canonical) and the flags."""
    chain = {"cited": stored_url(url), "resolution": res.get("state"),
             "final": stored_url(res.get("final_url")) if res.get("state") == "RESOLVED" else None,
             "canonical": stored_url(res.get("canonical_url")) if res.get("canonical_url") else None,
             "redirects": res.get("redirects"), "hop_hosts": list(res.get("hop_hosts") or [])}
    flags: list = []
    cited_host = _host(url)
    hop_lookalike = any(h and domain_match.match(h, domain) == domain_match.LOOKALIKE
                        for h in (res.get("hop_hosts") or [])[1:])
    if res.get("state") == "RESOLVED":
        decide, via = res["final_url"], "redirect" if res.get("redirects") else "direct"
    elif is_redirector(url):
        return {"class": "INCONCLUSIVE", "reason": "CITED_URL_UNRESOLVED", "via": None, "chain": chain,
                "flags": ["REDIRECTOR_" + ("NOT_FOLLOWED" if res.get("state") in ("OFF", "CAP_REACHED", "KILLED",
                                                                                     "NOT_FETCHED_SECRET")
                                           else "FAILED")] + (["LOOKALIKE_HOP"] if hop_lookalike else []),
                "competitor": False}
    elif res.get("state") == "NOT_FETCHED_SECRET":
        # WR-F013: resolution is on but this URL carries a secret, so it is never fetched: where it leads is unknown
        return {"class": "INCONCLUSIVE", "reason": "CITED_URL_NOT_FETCHED", "via": None, "chain": chain,
                "flags": [], "competitor": False}
    else:
        decide, via = url, "direct"
        if res.get("state") in ("FAILED", "KILLED"):
            flags.append("RESOLUTION_" + res["state"])
            if hop_lookalike:                         # round 3: the chain passed a lookalike before it failed
                flags.append("LOOKALIKE_HOP")
    host = _host(decide)
    if not host:
        return {"class": "INCONCLUSIVE", "reason": "CITED_URL_UNREADABLE", "via": None, "chain": chain,
                "flags": flags, "competitor": False}
    if is_redirector(decide):                         # resolved, but it landed on the redirector itself: target unknown
        return {"class": "INCONCLUSIVE", "reason": "CITED_URL_UNRESOLVED", "via": None, "chain": chain,
                "flags": ["REDIRECTOR_DID_NOT_REDIRECT"], "competitor": False}
    m = domain_match.match(host, domain)
    on_client = m in (domain_match.EXACT, domain_match.SUBDOMAIN)
    cited_lookalike = bool(cited_host) and domain_match.match(cited_host, domain) == domain_match.LOOKALIKE
    if cited_lookalike or (hop_lookalike and res.get("state") == "RESOLVED"):
        # M1 / round 2 item 1: a citation on a lookalike host, or one whose redirect chain passes through a lookalike
        # host at ANY hop, is never the client wherever it lands; landing on the client is a risk signal (who
        # controls a lookalike that forwards to the client?), surfaced, never credited
        flags.append("CITED_HOST_LOOKALIKE" if cited_lookalike else "LOOKALIKE_HOP")
        if on_client and via == "redirect":
            flags.append("REDIRECTED_ONTO_CLIENT")
        return {"class": "LOOKALIKE_URL", "reason": None, "via": via, "chain": chain, "flags": flags,
                "competitor": False}
    if on_client and res.get("state") == "FAILED" and res.get("redirects"):
        # WR-F014: the client's URL redirected and the chain stopped (or the target was refused): where it lands is
        # unknown. Through a lookalike: a lookalike, never the client; elsewhere off the client: INCONCLUSIVE
        if res.get("redirect_refused"):
            flags.append("REDIRECT_TARGET_REFUSED")
        hops = [h for h in (res.get("hop_hosts") or [])[1:] if h]
        if hop_lookalike:
            return {"class": "LOOKALIKE_URL", "reason": None, "via": "redirect", "chain": chain,
                    "flags": flags + ["REDIRECTED_OFF_CLIENT"], "competitor": False}
        if hops and not domain_match.on_site(hops[-1], domain):
            return {"class": "INCONCLUSIVE", "reason": "REDIRECT_NOT_RESOLVED", "via": None, "chain": chain,
                    "flags": flags + ["REDIRECTED_OFF_CLIENT"], "competitor": False}
    if on_client and res.get("state") == "FAILED" and res.get("detail") == "REFUSED_URL" \
            and not res.get("redirect_refused"):
        # WR-F014: the chain from the client's URL was refused by URL rules at some hop, a redirect whose target
        # could not even be read (or a hop the fetcher will not request): where it lands is unknown, never a credit
        return {"class": "INCONCLUSIVE", "reason": "REDIRECT_NOT_RESOLVED", "via": None, "chain": chain,
                "flags": flags, "competitor": False}
    if on_client and res.get("state") == "RESOLVED" and isinstance(res.get("status"), int) and res["status"] >= 400:
        # war room (WR-F014 chains): the client page answered an error instead of a page (or instead of the rest of
        # its redirect chain): nothing citable was found there, so it is never a credit
        return {"class": "INCONCLUSIVE", "reason": "LANDING_PAGE_ERROR", "via": via, "chain": chain,
                "flags": flags, "competitor": False}
    if on_client:
        if domain_match.on_site(cited_host, domain) is False and via == "redirect":
            flags.append("REDIRECTED_ONTO_CLIENT")
        return {"class": "STRENGTH_URL", "reason": None, "via": via, "chain": chain, "flags": flags,
                "competitor": False, "match": m}
    if m == domain_match.LOOKALIKE:
        if domain_match.on_site(cited_host, domain):
            flags.append("REDIRECTED_OFF_CLIENT")
        return {"class": "LOOKALIKE_URL", "reason": None, "via": via, "chain": chain, "flags": flags,
                "competitor": False}
    if domain_match.on_site(cited_host, domain):
        flags.append("REDIRECTED_OFF_CLIENT")
    comp = any(domain_match.on_site(host, d) for d in competitors)
    canon = res.get("canonical_url")
    if canon and domain_match.on_site(_host(canon), domain):
        # M2: the third party controls its own canonical tag, so this is never a STRENGTH_URL: a class of its own
        return {"class": "CANONICAL_TO_CLIENT", "reason": None, "via": "canonical", "chain": chain,
                "flags": flags + ["THIRD_PARTY_PAGE"], "competitor": comp,
                "match": domain_match.match(_host(canon), domain)}
    return {"class": "OPPORTUNITY_URL", "reason": None, "via": via, "chain": chain, "flags": flags,
            "competitor": comp}


_BASE_SEPARATORS = frozenset("().+/,_-")
_X_BETWEEN = re.compile(r"(?<=\d)[xX](?=\d)")


def _separator_reading(text: str) -> str:
    """The text with every Unicode punctuation / symbol / separator the base personal-data screen does not already
    read as a separator (``•`` ``·`` ``|`` ``:`` ``*`` ``#`` …) read as a space, and ``x``/``X`` between two digits as
    a space, so a phone / card / SSN written with an unusual separator is still seen (TNZ separator screen, upstreamed)."""
    out = []
    for c in text or "":
        cat = unicodedata.category(c)
        if c not in _BASE_SEPARATORS and not c.isspace() and cat[0] in "PSZ" and cat not in ("Pd", "Pc"):
            out.append(" ")
        else:
            out.append(c)
    return _X_BETWEEN.sub(" ", "".join(out))


def personal_or_separator(text: str) -> str | None:
    """Personal data on the text as written, else on its Unicode-separator reading."""
    if not text:
        return None
    return personal_data(text) or personal_data(_separator_reading(text))


def _safe_hint(hint) -> str | None:
    """A cited engine's unverified hint/title: credentials redacted, then withheld entirely if it reads as personal
    data (by the base screen or the Unicode-separator reading)."""
    if not isinstance(hint, str):
        return None
    red = secret_guard.redact_text(hint[:253])[0]
    return None if personal_or_separator(red) is not None else red


def excerpt(text: str, terms: list) -> dict:
    """A short span of the answer for the drill-down: around the first brand mention, else the answer's opening.
    Bounded, marked untrusted (envelope.observed), and withheld when it looks like personal data."""
    # WR-F012: secrets are redacted from the whole answer BEFORE a span is chosen, so a cut key never survives
    t, redacted = secret_guard.redact_text(" ".join((text or "").split()))
    basis, start = "answer_opening", 0
    for term in terms:
        m = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", t, re.IGNORECASE)
        if m is not None and (basis == "answer_opening" or m.start() < start):
            basis, start = "brand_mention", m.start()
    lo = max(0, start - 60) if basis == "brand_mention" else 0
    span = t[lo:lo + EXCERPT_MAX]
    kind = personal_or_separator(span)
    if kind is not None:
        return {"basis": basis, "excerpt": None, "withheld": f"personal_data:{kind}"}
    out = {"basis": basis, "excerpt": observed(span, EXCERPT_MAX), "withheld": None}
    if redacted:
        out["secret_redacted"] = True
    return out


def prompt_id(ps: dict, pi: int) -> str:
    return f"{ps['prompt_set_id']}:v{ps['version']}:{pi}"


def classify(engines: dict, prompt_set: dict, domain: str, resolver: Resolver, run_id: str, observed_at: str,
             refresh_due: str) -> dict:
    """Rows, metrics and the MNC priority list from the collected samples of every engine (``engines``: name ->
    collect() result). Deterministic for the same input."""
    terms, competitors = prompt_set["brand_terms"], list(prompt_set.get("competitor_domains") or [])
    term_rules = prompt_set.get("brand_term_rules")
    rows: list = []
    per: dict = {}                                   # (engine, prompt_index) -> tallies
    truncated = 0

    def add(row: dict) -> None:
        nonlocal truncated
        if len(rows) < MAX_ROWS:
            rows.append(row)
        else:
            truncated += 1

    for eng, r in engines.items():
        for s in r["samples"]:
            pi = s["prompt_index"]
            k = per.setdefault((eng, pi), {"samples": 0, "usable": 0, "refusals": 0, "failed": 0, "strength": 0,
                                          "mnc": 0, "mnc_low": 0, "inconclusive_answers": 0,
                                          "opportunity_urls": 0, "lookalike_urls": 0, "inconclusive_urls": 0,
                                          "strength_urls": 0, "canonical_to_client_urls": 0,
                                          "lookalike_onto_client": 0,
                                          "zero_citation_answers": 0, "versions": set(), "cited_instead": {},
                                          "evidence": []})
            k["samples"] += 1
            a: ProviderAnswer | None = s.get("answer")
            base = {"engine": eng, "model": getattr(a, "model", None), "model_version": getattr(a, "model_version",
                                                                                                None),
                    "provider": getattr(a, "provider", None) or eng, "prompt_id": prompt_id(prompt_set, pi),
                    "prompt_index": pi, "sample": s["sample"], "observed_at": observed_at,
                    "provider_asked_at": getattr(a, "asked_at", None), "refresh_tier": REFRESH_TIER,
                    "refresh_due": refresh_due, "run_id": run_id}
            if a is not None and a.model:
                k["versions"].add(f"{a.model}@{a.model_version or '?'}")
            if s["status"] == "FAILED":
                k["failed"] += 1
                add({**base, "class": "INCONCLUSIVE", "reason": s["error_code"], "reason_detail": s["error_detail"],
                     "url": None, "evidence": None, "position": None})
                continue
            if s["status"] == "REFUSAL":
                k["refusals"] += 1
                continue
            k["usable"] += 1
            text = s["text"]
            cites = extract_citations(a)
            if not cites:
                k["zero_citation_answers"] += 1
            ev = excerpt(text, terms)
            basis = getattr(a, "citation_basis", None)
            hints = getattr(a, "citation_hints", None) or {}
            classes = []
            for c in cites:
                # WR-F013: a URL carrying a secret is never fetched, whatever the resolution setting
                secret_url = url_has_secret(c["url"])
                res = {"state": "NOT_FETCHED_SECRET"} if secret_url and resolver.enabled else (
                    {"state": "OFF"} if secret_url else resolver.resolve(secret_guard.fetch_url(c["url"])))
                cl = classify_citation(c["url"], domain, competitors, res)
                if secret_url or any((cl["chain"].get(x) or {}).get("withheld") == "SECRET_REDACTED"
                                     for x in ("cited", "final", "canonical")):
                    cl["flags"] = cl["flags"] + ["SECRET_REDACTED"]
                classes.append(cl["class"])
                key = {"STRENGTH_URL": "strength_urls", "OPPORTUNITY_URL": "opportunity_urls",
                       "LOOKALIKE_URL": "lookalike_urls", "INCONCLUSIVE": "inconclusive_urls",
                       "CANONICAL_TO_CLIENT": "canonical_to_client_urls"}[cl["class"]]
                k[key] += 1
                if cl["class"] == "LOOKALIKE_URL" and "REDIRECTED_ONTO_CLIENT" in cl["flags"]:
                    k["lookalike_onto_client"] += 1
                if cl["class"] in ("OPPORTUNITY_URL", "CANONICAL_TO_CLIENT"):
                    dec = (cl["chain"]["final"] or cl["chain"]["cited"]) or {}
                    # keyed on a hash of the ORIGINAL URL's identity (canonical host, path and identity-bearing query:
                    # every non-tracking parameter), so two different pages — two articles whose stored forms are both
                    # shortened, or watch?v=A and watch?v=B — never merge; the stored (redacted) URL is what is shown
                    orig = res.get("final_url") if res.get("state") == "RESOLVED" else c["url"]
                    okey = hashlib.sha256(secret_guard.canonical_key(str(orig)).encode("utf-8", "surrogatepass")
                                          ).hexdigest()
                    ci = k["cited_instead"].setdefault(okey, {
                        "url": dec.get("url"), "host": dec.get("host"), "answers": 0, "competitor": cl["competitor"],
                        "canonical_to_client": cl["class"] == "CANONICAL_TO_CLIENT"})
                    ci["answers"] += 1
                hint = hints.get(c["url"])
                add({**base, "class": cl["class"], "reason": cl["reason"], "url": cl["chain"]["cited"]["url"]
                     if cl["chain"]["cited"] else None, "position": c["position"], "via": cl["via"],
                     "chain": cl["chain"], "flags": cl["flags"], "competitor": cl["competitor"],
                     "citation_basis": basis,
                     "engine_hint_unverified": _safe_hint(hint),
                     "evidence": {"url": cl["chain"]["cited"]["url"] if cl["chain"]["cited"] else None, **ev}})
            strength = "STRENGTH_URL" in classes
            if strength:
                k["strength"] += 1
            said = brand_mention.mention(text, terms, term_rules, domain=domain, client_cited=strength)
            if said["mentioned"] and not strength:
                if "INCONCLUSIVE" in classes:
                    k["inconclusive_answers"] += 1
                    add({**base, "class": "INCONCLUSIVE", "reason": "MENTION_WITH_UNRESOLVED_CITATIONS",
                         "url": None, "position": None, "evidence": {"url": None, **ev}})
                elif said["confidence"] == brand_mention.LOW:
                    # L1: only a term that may be a common word, with no disambiguation configured: reported apart,
                    # never in the headline rate or the MNC list
                    k["mnc_low"] += 1
                    add({**base, "class": "MENTIONED_NOT_CITED", "confidence": "LOW_CONFIDENCE",
                         "reason": "BRAND_TERM_MAY_BE_COMMON_WORD", "url": None, "position": None,
                         "citations_in_answer": len(cites), "evidence": {"url": None, **ev}})
                else:
                    k["mnc"] += 1
                    add({**base, "class": "MENTIONED_NOT_CITED", "confidence": "HIGH", "reason": None, "url": None,
                         "position": None, "citations_in_answer": len(cites),
                         "canonical_to_client_citations": classes.count("CANONICAL_TO_CLIENT"),
                         "evidence": {"url": None, **ev}})
                    if len(k["evidence"]) < MAX_EVIDENCE_PER_ITEM:
                        k["evidence"].append({"engine": eng, "sample": s["sample"], "observed_at": observed_at,
                                              **ev})
    metrics = _metrics(per, prompt_set, run_id, engines)
    return {"rows": rows, "rows_truncated": truncated, "metrics": metrics,
            "mnc_priority": mnc_priority(per, prompt_set, run_id, domain, observed_at)}


def _rate(k: int, n: int) -> dict:
    """A measured rate with its provenance-independent confidence: n, the 95% Wilson interval, directional."""
    return {"value": None if n == 0 else round(k / n, 4), "k": k, "n": n, "ci95": wilson(k, n),
            "interval": "wilson_95", "directional": n < MIN_USABLE}


def _mnc_rate(mnc: int, low: int, n: int) -> dict:
    """The headline Mentioned-Not-Cited rate; UNKNOWN (value null), never 0%, when the only not-cited mentions are
    unconfirmed (review round 3)."""
    r = _rate(mnc, n)
    if mnc == 0 and low:
        r = {**r, "value": None, "ci95": None, "unknown": "MENTIONS_UNCONFIRMED"}   # no interval for an unknown
    return r


def _metrics(per: dict, ps: dict, run_id: str, engines: dict) -> dict:
    out: dict = {"by_engine_prompt": [], "by_engine": {}, "totals": {}}
    tot: dict = {"usable": 0, "mnc": 0, "mnc_low": 0, "strength": 0, "refusals": 0, "failed": 0,
                 "opportunity_urls": 0, "strength_urls": 0, "canonical_to_client_urls": 0, "lookalike_urls": 0,
                 "lookalike_onto_client": 0, "inconclusive_urls": 0, "inconclusive_answers": 0}
    for (eng, pi), k in sorted(per.items()):
        prov = {"data_class": "measured", "source": "answer_engine_samples", "run_id": run_id, "engine": eng,
                "prompt_id": prompt_id(ps, pi), "prompt_set_sha256": ps["sha256"], "rules": VERSION,
                "model_versions": sorted(k["versions"])}
        out["by_engine_prompt"].append({
            "engine": eng, "prompt_index": pi, "prompt_id": prompt_id(ps, pi), "samples": k["samples"],
            "usable_answers": k["usable"], "refusals": k["refusals"], "inconclusive_calls": k["failed"],
            "strength_urls": k["strength_urls"], "opportunity_urls": k["opportunity_urls"],
            "canonical_to_client_urls": k["canonical_to_client_urls"],
            "lookalike_urls": k["lookalike_urls"], "lookalike_redirects_onto_client": k["lookalike_onto_client"],
            "inconclusive_urls": k["inconclusive_urls"], "zero_citation_answers": k["zero_citation_answers"],
            "mentioned_not_cited": _mnc_rate(k["mnc"], k["mnc_low"], k["usable"]), "cited_strength": _rate(k["strength"], k["usable"]),
            "mentioned_not_cited_low_confidence": {**_rate(k["mnc_low"], k["usable"]), "headline": False},
            "mention_with_unresolved_citations": k["inconclusive_answers"],
            "mixed_model_versions": len(k["versions"]) > 1, "provenance": prov})
        e = out["by_engine"].setdefault(eng, {**{x: 0 for x in tot}, "outcome": engines[eng]["outcome"]})
        for x in tot:
            e[x] += k[x]
            tot[x] += k[x]
    for eng, e in out["by_engine"].items():
        e["mentioned_not_cited"] = _mnc_rate(e["mnc"], e["mnc_low"], e["usable"])
        e["cited_strength"] = _rate(e["strength"], e["usable"])
        e["mentioned_not_cited_low_confidence"] = {**_rate(e["mnc_low"], e["usable"]), "headline": False}
        e["provenance"] = {"data_class": "measured", "source": "answer_engine_samples", "run_id": run_id,
                           "engine": eng, "prompt_set_sha256": ps["sha256"], "rules": VERSION,
                           "note": "pooled over prompts: prompts are not a random sample, so the interval is "
                                   "directional"}
    out["totals"] = {**tot, "mentioned_not_cited": _mnc_rate(tot["mnc"], tot["mnc_low"], tot["usable"]),
                     "cited_strength": _rate(tot["strength"], tot["usable"]),
                     "mentioned_not_cited_low_confidence": {**_rate(tot["mnc_low"], tot["usable"]),
                                                            "headline": False},
                     "provenance": {"data_class": "measured", "source": "answer_engine_samples", "run_id": run_id,
                                    "prompt_set_sha256": ps["sha256"], "rules": VERSION,
                                    "note": "pooled over engines and prompts: never a visibility score"}}
    for eng, r in engines.items():                    # an engine that produced no sample still shows its outcome
        out["by_engine"].setdefault(eng, {**{x: 0 for x in tot}, "outcome": r["outcome"],
                                          "mentioned_not_cited": _rate(0, 0), "cited_strength": _rate(0, 0)})
    return out


ORDER_RULE = ("items with at least one MENTIONED_NOT_CITED answer, ordered by: Mentioned-Not-Cited answers "
              "(descending), then whether a competitor page was cited instead, then how many engines showed it, then "
              "prompt index. An ordering rule, not a score.")


def mnc_priority(per: dict, ps: dict, run_id: str, domain: str, generated_at: str) -> dict:
    """The output contract for Roman (content structure) and Adaeze (content gap): ``callum.mnc_priority/2``."""
    by_prompt: dict = {}
    for (eng, pi), k in per.items():
        b = by_prompt.setdefault(pi, {"engines": [], "mnc": 0, "usable": 0, "cited_instead": {}, "evidence": []})
        b["usable"] += k["usable"]
        if k["mnc"] == 0:
            continue
        b["mnc"] += k["mnc"]
        b["engines"].append({"engine": eng, "mnc_answers": k["mnc"], "usable_answers": k["usable"],
                             "mentioned_not_cited": _rate(k["mnc"], k["usable"]),
                             "model_versions": sorted(k["versions"])})
        for u, ci in k["cited_instead"].items():
            x = b["cited_instead"].setdefault(u, {**ci, "answers": 0})
            x["answers"] += ci["answers"]
        for ev in k["evidence"]:
            if len(b["evidence"]) < MAX_EVIDENCE_PER_ITEM:
                b["evidence"].append(ev)
    items = []
    for pi, b in by_prompt.items():
        if b["mnc"] == 0:
            continue
        instead = sorted(b["cited_instead"].values(), key=lambda x: (-x["answers"], str(x["url"])))[:10]
        comp = any(x["competitor"] for x in instead)
        items.append({"prompt_id": prompt_id(ps, pi), "prompt_index": pi,
                      "prompt": observed(ps["prompts"][pi], 500) if personal_data(ps["prompts"][pi]) is None else None,
                      "mnc_answers": b["mnc"], "usable_answers": b["usable"],
                      "mentioned_not_cited": _rate(b["mnc"], b["usable"]),
                      "engines": sorted(b["engines"], key=lambda e: e["engine"]), "cited_instead": instead,
                      "competitor_cited_instead": comp, "evidence": b["evidence"],
                      "for_roman": {"task": "content_structure",
                                    "question": "the brand is named but not attributed: is the client page that "
                                                "should answer this prompt structured so an engine can extract and "
                                                "cite it?"},
                      "for_adaeze": {"task": "content_gap",
                                     "gap": "pages cited instead of the client's for this prompt",
                                     "hosts": sorted({x["host"] for x in instead if x["host"]})}})
    items.sort(key=lambda i: (-i["mnc_answers"], not i["competitor_cited_instead"], -len(i["engines"]),
                              i["prompt_index"]))
    for rank, it in enumerate(items, start=1):
        it["rank"] = rank
    return {"contract": CONTRACT_MNC, "producer": AGENT, "consumers": {"roman": "content_structure",
                                                                       "adaeze": "content_gap"},
            "run_id": run_id, "domain": domain, "generated_at": generated_at, "rules": VERSION,
            "prompt_set": {"prompt_set_id": ps["prompt_set_id"], "version": ps["version"], "sha256": ps["sha256"]},
            "order_rule": ORDER_RULE, "items": items,
            "excluded": "INCONCLUSIVE answers (an unresolved citation, or a failed engine call) and LOW_CONFIDENCE "
                        "mentions (a brand term that may be a common word, no disambiguation configured) are never "
                        "listed as Mentioned-Not-Cited",
            "data_class": "measured", "effect_class": None}


# ---------------------------------------------------------------------------------------------- the envelope

def run(engines: dict, prompt_set: dict | None, domain: str, n: int, guard: Callable, budget, resolver: Resolver,
        run_id: str, clock) -> dict:
    """Callum's citation-source run: collect every engine of the prompt set, classify, return the envelope."""
    method = (f"Each prompt of prompt set {{ps}} v{{ver}} asked {n} times per engine through the multi-engine adapter; "
              "every URL the engine attributed to an answer is classified STRENGTH_URL / CANONICAL_TO_CLIENT / "
              "OPPORTUNITY_URL / LOOKALIKE_URL / INCONCLUSIVE, and every answer that names a brand term without a "
              "STRENGTH_URL citation is MENTIONED_NOT_CITED (its own metric; LOW_CONFIDENCE apart when the term may "
              "be a common word). Domain matching: exact host, www. twin or subdomain, IDNA-aware; a lookalike host "
              "never counts as the client, wherever it redirects; a third-party page whose canonical names the "
              "client is CANONICAL_TO_CLIENT, never a strength. A failed engine call is INCONCLUSIVE, never "
              f"'not cited'. Rates exclude refusals and failures; 95% Wilson intervals; under {MIN_USABLE} usable "
              f"answers a rate is directional. Rules {VERSION}.")
    if prompt_set is None:
        return envelope(AGENT, TASK, "INSUFFICIENT_EVIDENCE", [], methodology="No prompt set was given.",
                        limitations=LIMITS, reason="NO_PROMPT_SET")
    methodology = method.format(ps=prompt_set["name"], ver=prompt_set["version"])
    common = brand_mention.warnings(prompt_set["brand_terms"], prompt_set.get("brand_term_rules"))
    if common:                                        # round 3: never a silent change
        methodology += WARN_COMMON.format(terms=", ".join(common))
    now = clock.now()
    observed_at, refresh_due = iso(now), iso(now + REFRESH_INTERVAL)
    collected: dict = {}
    try:
        for eng in prompt_set["engines"]:
            collected[eng] = collect(eng, engines.get(eng), prompt_set, n, guard, budget)
        result = classify({e: r for e, r in collected.items() if r["outcome"] != NOT_CONNECTED}, prompt_set, domain,
                          resolver, run_id, observed_at, refresh_due)
    except Killed as k:
        return envelope(AGENT, TASK, "KILLED", [], methodology=methodology, limitations=LIMITS, reason=k.code,
                        facts={"engines": {e: {"outcome": r["outcome"]} for e, r in collected.items()}})
    not_connected = {f"answer_engine:{e}" for e, r in collected.items() if r["outcome"] == NOT_CONNECTED}
    engines_view = {e: {"outcome": r["outcome"], "reason": r["reason"], "reason_detail": r["reason_detail"],
                        "usage": r["usage"], "prompts_not_asked": r["prompts_not_asked"],
                        "samples": len(r["samples"])} for e, r in collected.items()}
    observed_ = [e for e, r in collected.items() if r["outcome"] in ("OK", "PARTIAL")]
    stopped = {e: r for e, r in collected.items() if r["outcome"] in ("BLOCKED", "FAILED")}
    facts = {"rules": VERSION, "prompt_set": {"prompt_set_id": prompt_set["prompt_set_id"],
                                              "version": prompt_set["version"], "sha256": prompt_set["sha256"]},
             "samples_per_prompt": n, "engines": engines_view, "observed_at": observed_at,
             "refresh_tier": REFRESH_TIER, "refresh_due": refresh_due,
             "resolution": {"enabled": resolver.enabled, "fetches": resolver.fetches, "cap": resolver.max_urls},
             **result}
    findings = _findings(result, stopped)
    if not observed_ and stopped:
        _, r = min(stopped.items())
        return envelope(AGENT, TASK, r["outcome"], findings, methodology=methodology, limitations=LIMITS,
                        not_connected=not_connected, reason=r["reason"], facts=facts)
    if not observed_:
        return envelope(AGENT, TASK, "NOT_CONNECTED", [], methodology=methodology, limitations=LIMITS,
                        not_connected=not_connected, facts={"engines": engines_view, "rules": VERSION},
                        reason="no answer-engine port is connected; no engine was asked anything")
    outcome = "OK" if all(collected[e]["outcome"] == "OK" for e in collected) else "PARTIAL"
    return envelope(AGENT, TASK, outcome, findings, methodology=methodology, limitations=LIMITS,
                    not_connected=not_connected, facts=facts)


def _findings(result: dict, stopped: dict) -> list:
    out = []
    for m in result["metrics"]["by_engine_prompt"]:
        d = {"engine": m["engine"], "prompt_id": m["prompt_id"], "usable_answers": m["usable_answers"]}
        mnc = m["mentioned_not_cited"]
        if mnc["k"]:
            out.append(finding("CALLUM_MENTIONED_NOT_CITED", "medium", "measured",
                               "INSUFFICIENT_EVIDENCE" if mnc["directional"] else "TEST", capability="P4",
                               detail={**d, "mentioned_not_cited": mnc}))
        if m["lookalike_urls"]:
            out.append(finding("CALLUM_LOOKALIKE_DOMAIN_CITED", "high", "measured", "WATCH", capability="P4",
                               detail={**d, "lookalike_urls": m["lookalike_urls"]}))
        if m["lookalike_redirects_onto_client"]:
            out.append(finding("CALLUM_LOOKALIKE_REDIRECTS_ONTO_CLIENT", "high", "measured", "WATCH",
                               capability="P4",
                               detail={**d, "lookalike_redirects_onto_client": m["lookalike_redirects_onto_client"]}))
        if m["canonical_to_client_urls"]:
            out.append(finding("CALLUM_CANONICAL_TO_CLIENT", "info", "measured", "WATCH", capability="P4",
                               detail={**d, "canonical_to_client_urls": m["canonical_to_client_urls"]}))
        low = m["mentioned_not_cited_low_confidence"]
        if low["k"]:
            out.append(finding("CALLUM_MENTIONED_NOT_CITED_LOW_CONFIDENCE", "low", "measured",
                               "INSUFFICIENT_EVIDENCE", capability="P4",
                               detail={**d, "mentioned_not_cited_low_confidence": low}))
        if m["opportunity_urls"] and not m["strength_urls"]:
            out.append(finding("CALLUM_OPPORTUNITY_URLS", "medium", "measured",
                               "INSUFFICIENT_EVIDENCE" if m["usable_answers"] < MIN_USABLE else "TEST",
                               capability="P4", detail={**d, "opportunity_urls": m["opportunity_urls"]}))
        if m["strength_urls"]:
            out.append(finding("CALLUM_STRENGTH_URLS", "info", "measured", "WATCH", capability="P4",
                               detail={**d, "strength_urls": m["strength_urls"], "cited_strength": m["cited_strength"]}))
    for e, r in sorted(stopped.items()):
        out.append(finding("ENGINE_RUN_STOPPED", "low", "measured", "WATCH", capability="P4",
                           detail={"engine": e, "reason": r["reason"], "outcome": r["outcome"]}))
    return out

