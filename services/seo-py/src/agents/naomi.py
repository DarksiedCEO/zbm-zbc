"""
Naomi — Prompt Volume (ADR 0049). ONE job: estimate how often the fireteam's topics are asked, ALWAYS as a band with
an honest confidence, never a bare number, and UNKNOWN — never 0 — when there is no signal.

No engine publishes how often a prompt is asked of it, and no paid panel is used (owner rule). Naomi therefore
estimates a DIRECTIONAL PROXY from owned / free signals only, and says so on every topic:

  signal                                                      | state on this branch
  ------------------------------------------------------------|-----------------------------------------------------
  Priya's EMERGING item for the topic (priya-emerging-handoff/1)| read when the caller passes the hand-off: its
    observed_counts = this property's impressions for that exact | ``observed_counts`` (Search Console or Bing, as
    query, below Priya's ESTABLISHED threshold                   | Priya measured them; NOT verified against a Priya
                                                                 | record: Priya stores only a hash)
  the latest completed audit's Search Console query-class totals | read when present: the property's impressions of
    (``search_performance.by_query_class.classes.question``)     | ALL question-form queries; context only, never
                                                                 | attributed to one topic
  Bing Webmaster Tools directly                                  | NOT_CONNECTED (Priya's adapter is not on this base)
  any paid panel / clickstream / "AI prompt volume" vendor       | not used, by owner rule

The estimator (``ESTIMATOR``), per topic:
  1. find the topic's Priya item (the item the topic came from, or an item whose text equals the prompt);
  2. a derived item, a refused item or no item -> ``status: UNKNOWN`` with the reason; band null; reliability NONE;
  3. an observed item -> one SIGNAL per source (never summed across sources: Search Console and Bing cover different
     windows): the observed impression count k, its window, and the exact Garwood 95% Poisson band on k (lower
     chi2(0.025; 2k)/2, upper chi2(0.975; 2k+2)/2, computed here by bisection on the Poisson CDF up to k = 1,000 and
     the Wilson-Hilferty form above, rounded OUTWARD to 0.01). The band models how far such a count could move from
     one window to the next at an unchanged rate (impressions are the engine's full count, not a sample);
  4. reliability from k: THIN (< 20, Priya's threshold), MODERATE (< 1,000), STRONG (>= 1,000); confidence LOW for
     THIN, MEDIUM otherwise — NEVER HIGH: search impressions on one property are a proxy for how often people ask an
     AI engine about the topic, not a measurement of it;
The estimate's unit is ``impressions_on_this_property`` over the signal's window: it is not market volume and not a
count of AI prompts. Naomi asks no engine and spends nothing.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

from agents import fireteam_topics, priya_handoff
from envelope import envelope, finding

AGENT, TASK = "naomi", "prompt_volume_estimate"
ESTIMATOR = "naomi-estimator/2026-11-08.1"
CONTRACT = "naomi.prompt_volume/1"
STATUSES = ("DIRECTIONAL_PROXY", "UNKNOWN")
RELIABILITY = ("NONE", "THIN", "MODERATE", "STRONG")
CONFIDENCE = ("NONE", "LOW", "MEDIUM")
UNKNOWN_REASONS = ("NO_OWNED_SIGNAL", "PRIYA_DERIVED_NOT_MEASURED", "PRIYA_ITEM_NOT_MEASURED", "TOPIC_REFUSED")
THIN_BELOW, STRONG_FROM = 20, 1000
GARWOOD_EXACT_MAX = 1000
UNIT = "impressions_on_this_property"
PROXY_FOR = "how often this topic is asked of AI answer engines (directional proxy; not measured anywhere)"
LIMITS = [
    "No source measures how often a prompt is asked of an AI engine; every estimate here is a directional proxy "
    "from owned search data (impressions on the client's own property), never prompt volume.",
    "Priya's hand-off carries only EMERGING items (below her 20-impression threshold), so a signal from it is THIN "
    "by construction; ESTABLISHED queries are not handed off.",
    "The hand-off arrives in the request (Priya's ledger is not stored), so its counts are not verified against a "
    "Priya record here.",
    "Search Console omits anonymised queries and returns top rows only; a topic with no row is UNKNOWN, not zero.",
    "The Garwood band models period-to-period variability of a full count at an unchanged rate; it is not a "
    "forecast and not sampling error.",
    "Bing Webmaster Tools is not connected directly; a Bing count reaches Naomi only through Priya's hand-off.",
]


# ---------------------------------------------------------------------------------------------- the Garwood band

def _pois_cdf(k: int, mu: float) -> float:
    """P(X <= k) for X ~ Poisson(mu), summed in log space (no underflow for large mu)."""
    if mu <= 0:
        return 1.0
    lm = math.log(mu)
    logs = [-mu + i * lm - math.lgamma(i + 1) for i in range(k + 1)]
    top = max(logs)
    return min(1.0, math.exp(top) * math.fsum(math.exp(x - top) for x in logs))


def _solve(k: int, target: float) -> float:
    """mu with P(X <= k; mu) = target (decreasing in mu), by bisection."""
    lo, hi = 0.0, k + 20.0 + 10.0 * math.sqrt(k + 1)
    for _ in range(200):
        mid = (lo + hi) / 2
        if _pois_cdf(k, mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _wh(k2: float, p: float) -> float:
    """chi-square quantile (Wilson-Hilferty) for k2 degrees of freedom, halved."""
    z = {0.025: -1.959963984540054, 0.975: 1.959963984540054}[p]
    return k2 * (1 - 2 / (9 * k2) + z * math.sqrt(2 / (9 * k2))) ** 3 / 2


def garwood(k: int) -> dict:
    if k < 0:
        raise ValueError("count")
    if k <= GARWOOD_EXACT_MAX:
        low = 0.0 if k == 0 else _solve(k - 1, 0.975)
        high = _solve(k, 0.025)
        how = "exact (Poisson CDF bisection)"
    else:
        low, high = _wh(2 * k, 0.025), _wh(2 * k + 2, 0.975)
        low, high = low * 0.999, high * 1.001             # outward margin over the approximation
        how = "Wilson-Hilferty"
    return {"low": math.floor(low * 100) / 100, "high": math.ceil(high * 100) / 100,
            "interval": "garwood_poisson_95", "method": how}


# ---------------------------------------------------------------------------------------------- the estimator

def _days(dr: dict) -> Optional[int]:
    try:
        return (date.fromisoformat(dr["end"]) - date.fromisoformat(dr["start"])).days + 1
    except (TypeError, ValueError, KeyError):
        return None


def _reliability(k: int) -> str:
    return "THIN" if k < THIN_BELOW else "MODERATE" if k < STRONG_FROM else "STRONG"


def _signal(c: dict, item: dict) -> dict:
    k = c["impressions"]
    band = garwood(k)
    days = _days(c["date_range"])
    per_day = None if not days or days <= 0 else {"low": math.floor(band["low"] / days * 1000) / 1000,
                                                  "high": math.ceil(band["high"] / days * 1000) / 1000}
    return {"source": f"priya_handoff:{c['source']}", "priya_key": item["key"], "observed_impressions": k,
            "window": dict(c["date_range"]), "window_days": days, "band": band, "band_per_day": per_day,
            "reliability": _reliability(k), "unit": UNIT, "data_class": "measured",
            "provenance": {"producer": "priya", "contract": priya_handoff.SCHEMA,
                           "volume_status": item["volume_status"], "verified_against_priya_record": False}}


def estimate_topic(t: dict, item: Optional[dict]) -> dict:
    base = {"topic_key": t["topic_key"], "proxy_for": PROXY_FOR, "estimator": ESTIMATOR}
    if t.get("refused"):
        return {**base, **_unknown("TOPIC_REFUSED")}
    if item is None:
        return {**base, **_unknown("NO_OWNED_SIGNAL")}
    if item["origin"] == "derived":
        return {**base, **_unknown("PRIYA_DERIVED_NOT_MEASURED")}
    c = item.get("observed_counts")
    if c is None:
        return {**base, **_unknown("PRIYA_ITEM_NOT_MEASURED")}
    sig = _signal(c, item)
    signals = [sig]
    flags = ["SINGLE_SOURCE", "PROXY_NOT_PROMPT_VOLUME", "HANDOFF_NOT_VERIFIED"]
    if c["source"] == "bing_webmaster":
        flags.append("BING_WINDOW_DIFFERS")
    rel = sig["reliability"]
    return {**base, "status": "DIRECTIONAL_PROXY", "reliability": rel,
            "confidence": "LOW" if rel == "THIN" else "MEDIUM", "band": dict(sig["band"]), "unit": UNIT,
            "window": dict(sig["window"]), "signals": signals, "flags": flags, "unknown_reason": None}


def _unknown(reason: str) -> dict:
    return {"status": "UNKNOWN", "reliability": "NONE", "confidence": "NONE", "band": None, "unit": UNIT,
            "window": None, "signals": [], "flags": [], "unknown_reason": reason}


def context_from_audit(report: Optional[dict], audit_id: Optional[str]) -> dict:
    """The property's question-class impressions from the latest completed audit (context, never per topic)."""
    sp = (report or {}).get("search_performance") or {}
    q = ((sp.get("by_query_class") or {}).get("classes") or {}).get("question")
    if not isinstance(q, dict) or not isinstance(q.get("impressions"), int):
        return {"state": "UNAVAILABLE", "why": "no completed audit with a Search Console read for this tenant",
                "source": "first_party:search_console"}
    return {"state": "OK", "source": "first_party:search_console", "audit_id": audit_id,
            "query_class": "question", "impressions": q["impressions"], "band": garwood(q["impressions"]),
            "window": {k: (sp.get("window") or {}).get(k) for k in ("start", "end", "days")}, "unit": UNIT,
            "note": "every question-form query on the property together; not attributable to any one topic, and "
                    "never used as a topic's estimate"}


def run(topics: list, handoff_items: list, tenant_id: str, run_id: str, context: dict,
        emit=None) -> dict:
    """Naomi's estimates for every topic (``emit(topic_key, estimate)`` as each is made, for Zara)."""
    out = []
    for t in topics:
        est = estimate_topic(t, fireteam_topics.item_for(t, handoff_items))
        out.append(est)
        if emit is not None:
            emit(t["topic_key"], est)
    return {"contract": CONTRACT, "producer": AGENT, "tenant_id": tenant_id, "run_id": run_id,
            "estimator": ESTIMATOR, "unit": UNIT, "topics": out, "context": context,
            "not_connected": ["bing_webmaster (direct)", "prompt_volume (no source exists; owner rule: no paid panel)"]}


def envelope_for(outcome: str, contract: Optional[dict], reason: Optional[str] = None) -> dict:
    method = (f"Directional proxy per topic from owned signals only ({ESTIMATOR}): the topic's Priya EMERGING item "
              "(its observed impressions on this property, one signal per source, never summed), with an exact "
              "Garwood 95% Poisson band; reliability THIN under 20, MODERATE under 1,000, STRONG from 1,000; "
              "confidence never HIGH (a proxy); UNKNOWN, never 0, without a signal. The latest audit's Search "
              "Console question-class impressions are context only.")
    if contract is None:
        return envelope(AGENT, TASK, outcome, [], methodology=method, limitations=LIMITS, reason=reason)
    fl = []
    unknown = sum(1 for t in contract["topics"] if t["status"] == "UNKNOWN")
    if unknown:
        fl.append(finding("NAOMI_VOLUME_UNKNOWN", "info", "unknown", "INSUFFICIENT_EVIDENCE", capability="P4",
                          detail={"topics_unknown": unknown, "topics": len(contract["topics"])}))
    thin = sum(1 for t in contract["topics"] if t["reliability"] == "THIN")
    if thin:
        fl.append(finding("NAOMI_VOLUME_THIN", "info", "estimated", "INSUFFICIENT_EVIDENCE", capability="P4",
                          detail={"topics_thin": thin}))
    return envelope(AGENT, TASK, outcome, fl, methodology=method, limitations=LIMITS, reason=reason,
                    not_connected=set(contract["not_connected"]), facts={"contract": contract})
