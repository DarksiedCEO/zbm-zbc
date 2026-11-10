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
untrusted) is kept for the dashboard drill-down. ADR 0051 (job 2, Option B): when the service passes a ``sink`` the
client's full evidence for each row (cited URL, excerpt, title, hint) is handed to it for the per-tenant encrypted
vault, and ``split_evidence`` turns the envelope into the form that is logged: host-only URLs, keyed per-field hashes
(HMAC-SHA256 under the vault's log-field sub-key, job 3) and the strict filter's verdict flags, never a path or a
text. ``client_view`` rebuilds the owner's view from the vault: a field with a HIGH-PRECISION client-view finding
(``secret_guard.client_view_findings`` / ``client_view_url``, job 3; never the strict flag) is masked (``alert``; a
URL only in its offending parts), every other field is shown in full. Without a sink (unit use) the rows are the
strict lane's stored forms, as before.

No agent touches service state, the ledger or the log: the service records what this module returns.
"""

from __future__ import annotations

import hashlib
import re
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
    """A URL whose host, path, query, fragment or user part looks like it carries a CREDENTIAL: never fetched (WR-F013).
    A personal-data finding alone does not block the fetch (ADR 0051 job 2)."""
    p = split_url(url) if url else None
    if p is None:
        return False
    return secret_guard.url_has_secret(url)


def stored_url(url: str | None) -> dict | None:
    """What a row keeps of a URL (ADR 0050 v2): scheme, host and only the path segments that pass the shared core's
    checks on their own; query and fragment never kept. A failing segment becomes ``{secret}`` (a credential check)
    or ``{withheld}`` (personal data: a long digit run, an e-mail, an IP address); a failing host label withholds the
    whole URL. Bounded."""
    if not url:
        return None
    p = split_url(url)
    u = secret_guard.inspect_url(url) if p is not None else None
    if u is None or not u["host"]:
        return {"url": None, "host": None, "query_dropped": False, "withheld": "UNREADABLE"}
    dropped = bool(p.query or p.fragment)
    if u["host_kind"] is not None:
        return {"url": None, "host": None, "query_dropped": dropped,
                "withheld": "SECRET_REDACTED" if u["host_kind"] == secret_guard.CREDENTIAL else "PERSONAL_DATA"}
    redirect = secret_guard.redirect_withheld(url)
    if redirect is not None:                            # a grounding redirect id is never stored (r2 part 3a)
        return {"url": redirect, "host": secret_guard.REDIRECT_HOST, "query_dropped": dropped,
                "withheld": "REDIRECT_ID"}
    segs = []
    kinds = set()
    for seg, kind in u["segments"]:
        if kind is None and seg and personal_data(unquote(seg)) == "ip":
            kind = secret_guard.PERSONAL
        kinds.add(kind)
        segs.append(secret_guard.URL_PLACEHOLDER[kind] if kind else seg)
    path = "/".join(segs) or "/"
    withheld = ("SECRET_REDACTED" if secret_guard.CREDENTIAL in kinds
                else "PERSONAL_DATA" if secret_guard.PERSONAL in kinds else None)
    host = u["host"][:253]
    h = f"[{host}]" if ":" in host else host            # an IPv6 literal keeps its brackets (a readable URL)
    netloc = h if u["port"] in (None, 80, 443) else f"{h}:{u['port']}"
    kept = urlunsplit((u["scheme"], netloc, path, "", ""))[:URL_MAX]
    return {"url": kept, "host": host, "query_dropped": dropped, "withheld": withheld}


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


# characters checked around the kept span, widened to whole words: BEFORE it, a credential label whose value starts
# inside the span (``the password for the admin account is`` + value) or a key cut by the left edge; AFTER it, a key
# cut by the right edge (or split by spaces across it)
EXCERPT_MARGIN = 48
EXCERPT_MARGIN_AFTER = 24
# review 601e328a MUST 6: the whole-word widening reads at most this many characters past each margin edge (an answer
# with no whitespace used to make the window the whole text: quadratic in the prompt guard, minutes at 64 KB). A word
# longer than this is read for this many characters on each side, which still holds the key's prefix or enough of a
# random run for check (d), the address's "@" and domain, or the label before a value.
EXCERPT_WORD_MAX = 1024


def _withheld_code(name: str | None) -> str | None:
    """The row's ``withheld`` code for a failing check of the shared core (ADR 0050 v2)."""
    if name is None:
        return None
    if name == "e_digits":
        return "personal_data:number"
    if name == "f_email":
        return "personal_data:email"
    if name == "url_personal":
        return "personal_data:url"
    return "credential"


def field_check(text) -> str | None:
    """Callum's one screen for any text it keeps: the shared core's checks (credentials, card / SSN / phone numbers
    through check (e), e-mail through (f)), plus an IP address by the prompt guard. The ``withheld`` code or None."""
    code = _withheld_code(secret_guard.check(text))
    if code is None and isinstance(text, str) and personal_data(text) == "ip":
        code = "personal_data:ip"
    return code


def _safe_hint(hint) -> str | None:
    """A cited engine's unverified hint / title: kept only when the WHOLE hint is clearly safe, then bounded;
    otherwise dropped (never cut)."""
    if not isinstance(hint, str):
        return None
    return None if field_check(hint) is not None else hint[:253]


def _excerpt_span(text: str, terms: list) -> tuple:
    """(basis, span, withheld code): the kept span and the strict check of it AND ``EXCERPT_MARGIN`` characters on
    each side (whole words)."""
    basis, span, window = _excerpt_window(text, terms)
    return basis, span, field_check(window) or field_check(span)


def _excerpt_view_findings(text: str, terms: list) -> list:
    """The CLIENT view's findings (ADR 0051 job 3) on the excerpt's span AND its margins (the same window the strict
    lane reads): a value cut by the span's edge is still found whole. Recorded in the vault record (never logged)."""
    _basis, span, window = _excerpt_window(text, terms)
    return sorted(set(secret_guard.client_view_findings(window)) | set(secret_guard.client_view_findings(span)))


def _excerpt_window(text: str, terms: list) -> tuple:
    """(basis, span, window): the kept span and the span with ``EXCERPT_MARGIN`` characters on each side (whole
    words)."""
    t = " ".join((text or "").split())
    basis, start = "answer_opening", 0
    for term in terms:
        m = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", t, re.IGNORECASE)
        if m is not None and (basis == "answer_opening" or m.start() < start):
            basis, start = "brand_mention", m.start()
    lo = max(0, start - 60) if basis == "brand_mention" else 0
    span = t[lo:lo + EXCERPT_MAX]
    a, b = max(0, lo - EXCERPT_MARGIN), lo + EXCERPT_MAX + EXCERPT_MARGIN_AFTER
    a0, b0 = a, b
    while a > 0 and a0 - a < EXCERPT_WORD_MAX and not t[a - 1].isspace():
        a -= 1                                       # whole words: a key cut by the margin edge is read whole
    while b < len(t) and b - b0 < EXCERPT_WORD_MAX and not t[b].isspace():
        b += 1
    return basis, span, t[a:b]


def excerpt(text: str, terms: list) -> dict:
    """A short span of the answer for the drill-down: around the first brand mention, else the answer's opening.
    Bounded and marked untrusted (envelope.observed). ADR 0050 v2: the span AND ``EXCERPT_MARGIN`` characters on each
    side are checked; if anything fails, the whole excerpt is withheld (``[withheld]`` for a credential, no excerpt
    for personal data). Nothing is cut out of it. (The strict lane's form; the client's full excerpt goes to the
    vault through the sink, ADR 0051.)"""
    basis, span, code = _excerpt_span(text, terms)
    if code == "credential":
        return {"basis": basis, "excerpt": observed(secret_guard.WITHHELD, EXCERPT_MAX), "withheld": code,
                "secret_redacted": True}
    if code is not None:
        return {"basis": basis, "excerpt": None, "withheld": code}
    return {"basis": basis, "excerpt": observed(span, EXCERPT_MAX), "withheld": None}


def prompt_id(ps: dict, pi: int) -> str:
    return f"{ps['prompt_set_id']}:v{ps['version']}:{pi}"


def classify(engines: dict, prompt_set: dict, domain: str, resolver: Resolver, run_id: str, observed_at: str,
             refresh_due: str, sink: list | None = None) -> dict:
    """Rows, metrics and the MNC priority list from the collected samples of every engine (``engines``: name ->
    collect() result). Deterministic for the same input. ``sink`` (ADR 0051): a list that receives each kept row's
    full evidence and verdict flags (``evidence_entry``); the row then names it by ``evidence_ref`` (its index)."""
    terms, competitors = prompt_set["brand_terms"], list(prompt_set.get("competitor_domains") or [])
    term_rules = prompt_set.get("brand_term_rules")
    rows: list = []
    per: dict = {}                                   # (engine, prompt_index) -> tallies
    truncated = 0

    def add(row: dict, entry: dict | None = None) -> int | None:
        nonlocal truncated
        if len(rows) < MAX_ROWS:
            if sink is not None and entry is not None:
                row["evidence_ref"] = len(sink)
                sink.append(entry)
            rows.append(row)
            return row.get("evidence_ref")
        truncated += 1
        return None

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
            raw_ex = _raw_excerpt(text, terms) if sink is not None else None
            raw_view = _excerpt_view_findings(text, terms) if sink is not None else None
            basis = getattr(a, "citation_basis", None)
            hints = getattr(a, "citation_hints", None) or {}
            titles = getattr(a, "citation_titles", None) or {}
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
                else:
                    ci = None
                hint = hints.get(c["url"])
                # r2 part 3a: a grounding redirect's id is never emitted: its row shows the resolved target (stored
                # form) or, unresolved, the withheld redirect form
                shown = (cl["chain"]["final"] if is_redirector(c["url"]) and cl["chain"]["final"]
                         else cl["chain"]["cited"]) or {}
                entry = evidence_entry(_shown_raw(c["url"], res), res, raw_ex, titles.get(c["url"]), hint,
                                       ev["withheld"], raw_view) \
                    if sink is not None else None
                ref = add({**base, "class": cl["class"], "reason": cl["reason"], "url": shown.get("url"),
                           "position": c["position"], "via": cl["via"],
                           "chain": cl["chain"], "flags": cl["flags"], "competitor": cl["competitor"],
                           "citation_basis": basis,
                           "engine_hint_unverified": _safe_hint(hint),
                           "evidence": {"url": shown.get("url"), **ev}}, entry)
                if ci is not None and ref is not None and "evidence_ref" not in ci:
                    ci["evidence_ref"] = ref                 # the client sees this page's URL from that row's record
            strength = "STRENGTH_URL" in classes
            if strength:
                k["strength"] += 1
            said = brand_mention.mention(text, terms, term_rules, domain=domain, client_cited=strength)
            if said["mentioned"] and not strength:
                entry = evidence_entry(None, {}, raw_ex, None, None, ev["withheld"], raw_view) \
                    if sink is not None else None
                if "INCONCLUSIVE" in classes:
                    k["inconclusive_answers"] += 1
                    add({**base, "class": "INCONCLUSIVE", "reason": "MENTION_WITH_UNRESOLVED_CITATIONS",
                         "url": None, "position": None, "evidence": {"url": None, **ev}}, entry)
                elif said["confidence"] == brand_mention.LOW:
                    # L1: only a term that may be a common word, with no disambiguation configured: reported apart,
                    # never in the headline rate or the MNC list
                    k["mnc_low"] += 1
                    add({**base, "class": "MENTIONED_NOT_CITED", "confidence": "LOW_CONFIDENCE",
                         "reason": "BRAND_TERM_MAY_BE_COMMON_WORD", "url": None, "position": None,
                         "citations_in_answer": len(cites), "evidence": {"url": None, **ev}}, entry)
                else:
                    k["mnc"] += 1
                    ref = add({**base, "class": "MENTIONED_NOT_CITED", "confidence": "HIGH", "reason": None,
                               "url": None, "position": None, "citations_in_answer": len(cites),
                               "canonical_to_client_citations": classes.count("CANONICAL_TO_CLIENT"),
                               "evidence": {"url": None, **ev}}, entry)
                    if len(k["evidence"]) < MAX_EVIDENCE_PER_ITEM:
                        k["evidence"].append({"engine": eng, "sample": s["sample"], "observed_at": observed_at,
                                              **ev, **({"evidence_ref": ref} if sink is not None else {})})
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
        run_id: str, clock, sink: list | None = None) -> dict:
    """Callum's citation-source run: collect every engine of the prompt set, classify, return the envelope. ``sink``:
    see ``classify`` (ADR 0051); the envelope then holds ``evidence_ref`` indexes for ``split_evidence``."""
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
                          resolver, run_id, observed_at, refresh_due, sink=sink)
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



# ---------------------------------------------------------------------------------------------- the vault split
# ADR 0051 (job 2, Option B). The client's full evidence for a row goes to the per-tenant encrypted vault; what is
# logged (data/seo_log.jsonl, the ledger, events) keeps only the vault reference, keyed HMAC-SHA256 field hashes (job
# 3), host-only URLs (the host itself checked by the strict filter), counts / codes and the strict filter's verdict
# flags. The owner's view is rebuilt from the vault: a field with a client-view finding (job 3) is masked with
# ``alert``, every other field is shown in full.

EVIDENCE_FIELDS = ("cited_url", "final_url", "canonical_url", "excerpt", "title", "hint")
URL_FIELDS = frozenset(("cited_url", "final_url", "canonical_url"))
RECORD_VERSION = 1
MASK = {"credential": "[hidden: looks like a credential]", "personal": "[hidden: looks like personal data]"}
MASK_REASON = {"credential": "LOOKS_LIKE_CREDENTIAL", "personal": "LOOKS_LIKE_PERSONAL_DATA"}


def url_flag(url) -> str | None:
    """The strict filter's verdict on a whole URL, as a ``withheld`` code: None, ``credential`` or
    ``personal_data:url`` (every part: host, user info, path, query (tracking parameters too), fragment; a URL that
    cannot be read is ``credential``). The service's own withheld grounding-redirect form is None (it carries no
    value)."""
    if url is None:
        return None
    if not isinstance(url, str):
        return "credential"
    if is_redirector(url) and url == secret_guard.redirect_withheld(url):
        return None
    try:
        v = secret_guard._url_verdict(url)
    except Exception:  # noqa: BLE001 - fail closed
        return "credential"
    return None if v is None else ("credential" if v == secret_guard.CREDENTIAL else "personal_data:url")


def _raw_excerpt(text: str, terms: list) -> str | None:
    """The client's excerpt (for the vault): the same span the strict lane judged, as displayed (control characters
    removed, bounded)."""
    _basis, span, _code = _excerpt_span(text, terms)
    o = observed(span, EXCERPT_MAX)
    return o["text"] if o is not None else None


def _shown_raw(url: str, res: dict) -> str:
    """The URL a row shows the client: a redirector's resolved target, a grounding redirect's withheld form (its id
    is never stored, not even in the vault), else the cited URL as the engine gave it."""
    if is_redirector(url) and res.get("state") == "RESOLVED" and isinstance(res.get("final_url"), str):
        return res["final_url"]
    return secret_guard.redirect_withheld(url) or url


def evidence_entry(cited_url, res: dict, excerpt_text, title, hint, excerpt_flag=None, excerpt_view=None) -> dict:
    """One row's vault record (full evidence) and the strict filter's verdict on each field. ``excerpt_flag``: the
    strict lane's verdict on the excerpt (its span AND the margins around it, on the text as the engine gave it); the
    excerpt is flagged when that or the same check on the kept text fails. ``excerpt_view``: the client view's
    findings on the same span and margins (ADR 0051 job 3), kept in the record (``excerpt_view``) for the owner's
    view, never logged."""
    def text(x):
        return x[:2000] if isinstance(x, str) else None
    final = res.get("final_url") if res.get("state") == "RESOLVED" and isinstance(res.get("final_url"), str) else None
    canon = res.get("canonical_url") if isinstance(res.get("canonical_url"), str) else None
    record = {"v": RECORD_VERSION, "cited_url": text(cited_url), "final_url": text(final),
              "canonical_url": text(canon), "excerpt": text(excerpt_text), "title": text(title), "hint": text(hint)}
    flags = {f: _flag(f, record[f]) for f in EVIDENCE_FIELDS}
    if record["excerpt"] is not None:
        flags["excerpt"] = excerpt_flag or flags["excerpt"]
        if excerpt_view:
            record["excerpt_view"] = sorted(str(x) for x in excerpt_view)[:16]
    return {"record": record, "flags": flags}


def _flag(name: str, value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return "credential"
    return url_flag(value) if name in URL_FIELDS else field_check(value)


def checked_host(host) -> dict:
    """A host for the log: kept only when the strict filter passes it (whole and label by label); else withheld."""
    if not isinstance(host, str) or not host:
        return {"host": None, "host_withheld": None}
    u = secret_guard.inspect_url("https://" + host + "/") if "/" not in host and "@" not in host else None
    if u is None or not u["host"] or u["host_kind"] is not None or secret_guard.check(host) is not None:
        kind = (u or {}).get("host_kind")
        return {"host": None, "host_withheld": "PERSONAL_DATA" if kind == secret_guard.PERSONAL else "SECRET_REDACTED"}
    return {"host": host[:253], "host_withheld": None}


def url_host(url) -> dict:
    """The host of ``url`` for the log (checked), or why it is withheld."""
    if not isinstance(url, str):
        return {"host": None, "host_withheld": None}
    u = secret_guard.inspect_url(url)
    if u is None or not u["host"]:
        return {"host": None, "host_withheld": "UNREADABLE"}
    if u["host_kind"] is not None:
        return {"host": None, "host_withheld": "SECRET_REDACTED" if u["host_kind"] == secret_guard.CREDENTIAL
                else "PERSONAL_DATA"}
    return checked_host(u["host"])


def _chain_part(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    h = checked_host(d.get("host")) if d.get("host") else {"host": None, "host_withheld": d.get("withheld")}
    return {**h, "query_dropped": bool(d.get("query_dropped"))}


def _log_fields(entry: dict) -> dict:
    """Each field of a row's evidence as logged: its keyed hash (``hmac_sha256``, computed by ``split_evidence`` under
    the vault's log-field sub-key; ADR 0051 job 3) and the strict filter's verdict flag. Never a plain hash: an entry
    without its keyed hashes raises (the run then fails closed)."""
    rec, flags, macs = entry["record"], entry["flags"], entry["hmacs"]
    return {f: {"hmac_sha256": macs[f], "flag": flags[f]} for f in EVIDENCE_FIELDS if isinstance(rec.get(f), str)}


_ROW_TEXT_KEYS = ("url", "chain", "evidence", "engine_hint_unverified", "evidence_ref")


def log_row(row: dict, vault_ref: str | None, entry: dict | None) -> dict:
    """The logged form of a row: no URL path, no text; the vault reference, hashes, host-only URLs, verdict flags."""
    out = {k: v for k, v in row.items() if k not in _ROW_TEXT_KEYS}
    ch = row.get("chain")
    if isinstance(ch, dict):
        out["chain"] = {"cited": _chain_part(ch.get("cited")), "resolution": ch.get("resolution"),
                        "final": _chain_part(ch.get("final")), "canonical": _chain_part(ch.get("canonical")),
                        "redirects": ch.get("redirects"),
                        "hop_hosts": [checked_host(h)["host"] for h in (ch.get("hop_hosts") or [])][:20]}
    if entry is None or vault_ref is None:
        out.update(host=None, host_withheld=None, evidence=None)
        return out
    out.update(url_host(entry["record"]["cited_url"]))
    ev = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    out["evidence"] = {"vault_id": vault_ref, "basis": ev.get("basis"), "hash_key_id": entry["hash_key_id"],
                       "fields": _log_fields(entry)}
    return out


def _log_mnc(mp, refs: dict, sink: dict):
    if not isinstance(mp, dict) or not isinstance(mp.get("items"), list):
        return mp
    items = []
    for it in mp["items"]:
        inst = []
        for x in it.get("cited_instead") or []:
            r = x.get("evidence_ref")
            inst.append({**{k: v for k, v in x.items() if k not in ("url", "host", "evidence_ref")},
                         **checked_host(x.get("host")), "vault_id": refs.get(r)})
        evs = []
        for e in it.get("evidence") or []:
            r = e.get("evidence_ref")
            entry = sink.get(r) if isinstance(r, int) and r in refs else None
            evs.append({"engine": e.get("engine"), "sample": e.get("sample"), "observed_at": e.get("observed_at"),
                        "basis": e.get("basis"), "vault_id": refs.get(r),
                        "excerpt": _log_fields(entry).get("excerpt") if entry else None})
        hosts = sorted({x["host"] for x in inst if x["host"]})
        items.append({**it, "cited_instead": inst, "evidence": evs,
                      "for_adaeze": {**(it.get("for_adaeze") or {}), "hosts": hosts}})
    return {**mp, "items": items}


def _hashed(entry: dict, field_hash: Callable) -> dict:
    """``entry`` with the keyed hash of each of its fields (``field_hash(field, value) -> (key id, hex)``)."""
    rec = entry["record"]
    macs, kids = {}, set()
    for f in EVIDENCE_FIELDS:
        if isinstance(rec.get(f), str):
            kid, macs[f] = field_hash(f, rec[f])
            kids.add(kid)
    if len(kids) > 1:
        raise ValueError("the log-field key changed during a run")
    return {**entry, "hmacs": macs, "hash_key_id": next(iter(kids), None)}


def split_evidence(env, sink: list, put: Callable, field_hash: Callable | None = None):
    """The envelope as it is logged: every row's full evidence ``put`` into the vault (``put(record) -> vault id``),
    the row reduced to ``log_row``; the MNC list likewise. Each logged field hash is ``field_hash(field, value) ->
    (key id, HMAC-SHA256 hex)`` (the vault's keyed log-field hash, ADR 0051 job 3); without one, a run with evidence
    fails. Raises on anything inconsistent (the run then fails)."""
    from vault import ref as vault_ref
    facts = env.get("facts") if isinstance(env, dict) else None
    if not isinstance(facts, dict) or not isinstance(facts.get("rows"), list):
        return env                                   # no rows (NOT_CONNECTED, KILLED): nothing to keep
    refs: dict = {}
    hashed: dict = {}
    rows = []
    for row in facts["rows"]:
        i = row.get("evidence_ref")
        if i is None:
            rows.append(log_row(row, None, None))
            continue
        if not isinstance(i, int) or not 0 <= i < len(sink) or i in refs:
            raise ValueError("a row names evidence that is not in the sink")
        if field_hash is None:
            raise ValueError("no keyed hash for the logged evidence fields")
        refs[i] = vault_ref(put(sink[i]["record"]))
        hashed[i] = _hashed(sink[i], field_hash)
        rows.append(log_row(row, refs[i], hashed[i]))
    return {**env, "facts": {**facts, "rows": rows,
                             "mnc_priority": _log_mnc(facts.get("mnc_priority"), refs, hashed)}}


def _display(name: str, value, logged_flag=None, recorded=None) -> dict | None:
    """One field of the owner's view (ADR 0051 job 3): in full, or masked with ``alert`` on a HIGH-PRECISION finding
    of the client view (``secret_guard.client_view_findings`` / ``client_view_url``), never on the strict lane's flag:
    ``logged_flag`` (the strict verdict recorded at the run, kept in the log) is not read here. A text field is masked
    whole; a URL keeps every part but the offending ones (``{hidden}``), or is masked whole when its host carries a
    value or it cannot be read. ``recorded``: client-view findings recorded with the value (an excerpt's margins)."""
    if value is None:
        return None
    if not isinstance(value, str):
        out = _mask("credential", "type")
    elif name in URL_FIELDS:
        shown, kind = secret_guard.client_view_url(value)
        if kind is None:
            out = {"value": value, "alert": False, "reason": None}
        else:
            k = "credential" if kind == secret_guard.CREDENTIAL else "personal"
            out = _mask(k, "url_part" if shown is not None else "url")
            if shown is not None:
                out["value"] = shown
    else:
        found = sorted(set(secret_guard.client_view_findings(value))
                       | ({str(x) for x in recorded} if isinstance(recorded, list) else set()))
        kind = secret_guard.client_view_kind(found)
        if kind is None:
            out = {"value": value, "alert": False, "reason": None}
        else:
            out = _mask("credential" if kind == secret_guard.CREDENTIAL else "personal", "+".join(found))
    if name == "excerpt":
        out["untrusted"] = True
    return out


def _mask(kind: str, check: str) -> dict:
    return {"value": MASK[kind], "alert": True, "reason": MASK_REASON[kind], "check": check}


def _shown(d) -> str | None:
    return d["value"] if isinstance(d, dict) and not d["alert"] else None


def client_row(row: dict, got: Callable) -> dict:
    """The owner's view of a logged row. ``got(vault_ref) -> (record, None) | (None, reason code)``."""
    out = dict(row)
    ev = row.get("evidence")
    if not isinstance(ev, dict) or not ev.get("vault_id"):
        out["url"] = None
        return out
    record, why = got(ev["vault_id"])
    if record is None:
        out["url"] = None
        out["engine_hint_unverified"] = None
        out["evidence"] = {"vault_id": ev["vault_id"], "basis": ev.get("basis"), "available": False, "reason": why}
        return out
    fields = ev.get("fields") or {}
    shown = {f: _display(f, record.get(f), (fields.get(f) or {}).get("flag"),
                         record.get("excerpt_view") if f == "excerpt" else None) for f in EVIDENCE_FIELDS}
    out["url"] = _shown(shown["cited_url"])
    out["engine_hint_unverified"] = _shown(shown["hint"])
    out["evidence"] = {"vault_id": ev["vault_id"], "basis": ev.get("basis"), "available": True, **shown}
    return out


def _client_mnc(mp, got: Callable):
    if not isinstance(mp, dict) or not isinstance(mp.get("items"), list):
        return mp
    items = []
    for it in mp["items"]:
        inst = []
        for x in it.get("cited_instead") or []:
            rec, _why = got(x["vault_id"]) if x.get("vault_id") else (None, None)
            u = None
            if rec is not None:
                name = "final_url" if rec.get("final_url") else "cited_url"
                u = _display(name, rec.get(name), None)
            inst.append({**x, "url": _shown(u), "url_evidence": u})
        evs = []
        for e in it.get("evidence") or []:
            rec, why = got(e["vault_id"]) if e.get("vault_id") else (None, None)
            flag = (e.get("excerpt") or {}).get("flag")
            evs.append({**e, "excerpt": _display("excerpt", rec.get("excerpt"), flag, rec.get("excerpt_view"))
                        if rec is not None else None,
                        **({} if rec is not None else {"reason": why})})
        items.append({**it, "cited_instead": inst, "evidence": evs})
    return {**mp, "items": items}


def client_view(env, got: Callable):
    """The owner's view of a logged envelope (rows and the MNC list rebuilt from the vault)."""
    facts = env.get("facts") if isinstance(env, dict) else None
    if not isinstance(facts, dict) or not isinstance(facts.get("rows"), list):
        return env
    return {**env, "facts": {**facts, "rows": [client_row(r, got) for r in facts["rows"]],
                             "mnc_priority": _client_mnc(facts.get("mnc_priority"), got)}}
