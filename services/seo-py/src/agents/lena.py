"""
Lena — real-user page experience (ADR 0050). ONE task: report the tenant's real-user Core Web Vitals as Google's
Chrome UX Report (CrUX) has them — field data from real Chrome users over a rolling 28-day window — for the audited
origin and, where CrUX has URL-level data, the audited URLs, per form factor (phone, desktop, tablet), with the 75th
percentile, the distribution, the rating against web.dev's thresholds, the collection period, freshness, and a
25-period trend from the CrUX History API.

What Lena does NOT do: she runs no lab test (no Lighthouse, no synthetic load of the site), fetches nothing from the
audited site, fixes nothing, forecasts nothing, and produces no findings: the ratings are reported as data in her
section, never turned into a recommendation here. "No field data" (Google's 404) is INSUFFICIENT_FIELD_DATA — never
"fast", never "slow" — and every number carries its provenance (source, method, collection period, retrieval time).

The provider is behind an interface (crux_client.py: ``record`` / ``history`` returning a neutral shape); this
module does no I/O of its own.
"""

from __future__ import annotations

from statistics import median

import crux_client as cx

AGENT, TASK = "lena", "page_experience"

# web.dev thresholds, read on Nov 8 2026 (URLs and page dates in ADR 0050). Good: p75 <= good. Poor: p75 > poor.
# Between the two: needs improvement. Lower is better for all five metrics.
THRESHOLDS_VERSION = "2026-11-08.1"
THRESHOLDS = {
    "largest_contentful_paint": {"good": 2500.0, "poor": 4000.0, "unit": "ms", "core_web_vital": True,
                                 "source": "https://web.dev/articles/defining-core-web-vitals-thresholds (updated "
                                           "2025-05-07)"},
    "interaction_to_next_paint": {"good": 200.0, "poor": 500.0, "unit": "ms", "core_web_vital": True,
                                  "source": "https://web.dev/articles/defining-core-web-vitals-thresholds (updated "
                                            "2025-05-07); https://web.dev/articles/inp (updated 2025-09-02)"},
    "cumulative_layout_shift": {"good": 0.1, "poor": 0.25, "unit": "unitless", "core_web_vital": True,
                                "source": "https://web.dev/articles/defining-core-web-vitals-thresholds (updated "
                                          "2025-05-07)"},
    "first_contentful_paint": {"good": 1800.0, "poor": 3000.0, "unit": "ms", "core_web_vital": False,
                               "source": "https://web.dev/articles/fcp (updated 2023-12-06)"},
    "experimental_time_to_first_byte": {"good": 800.0, "poor": 1800.0, "unit": "ms", "core_web_vital": False,
                                        "source": "https://web.dev/articles/ttfb (updated 2025-11-28); web.dev calls "
                                                  "0.8 s 'a rough guide' and TTFB not a Core Web Vital"},
}
CORE = tuple(m for m, t in THRESHOLDS.items() if t["core_web_vital"])
RATINGS = ("GOOD", "NEEDS_IMPROVEMENT", "POOR")
# freshness: the daily dataset is ~2 days behind, the weekly history up to ~9; past these the data is called stale
STALE_DAYS = {"record": 7, "history": 14}
# trend: the medians of the earliest and the latest TREND_WINDOW periods with data; at least MIN_TREND_POINTS such
# periods; a change smaller than both MIN_CHANGE (absolute) and TREND_MIN_RELATIVE is NO_CLEAR_CHANGE
TREND_WINDOW = 6
MIN_TREND_POINTS = 12
TREND_MIN_RELATIVE = 0.10
MIN_CHANGE = {"largest_contentful_paint": 100.0, "interaction_to_next_paint": 20.0, "cumulative_layout_shift": 0.01,
              "first_contentful_paint": 100.0, "experimental_time_to_first_byte": 50.0}
TREND_METHOD = ("descriptive only: the median p75 of the earliest 6 weekly collection periods with data against the "
                "median of the latest 6; a change of less than 10% or less than the metric's minimum step is "
                "NO_CLEAR_CHANGE. Each period is a 28-day window and consecutive periods overlap, so the points are "
                "not independent samples; no significance test is made and nothing is forecast")
METHOD = ("Google's Chrome UX Report API (records:queryRecord) and CrUX History API (records:queryHistoryRecord), "
          "read-only with an API key, for the audited origin and each audited URL, per form factor (PHONE, DESKTOP, "
          "TABLET), asking only for LCP, INP, CLS, FCP and TTFB. When only one of the bare and www. forms of the "
          "domain is registered, the other form's https origin (its twin) is asked too and shown beside it, labelled, "
          "never merged; the URL queries use the audited origin's host, or the twin's when only the twin has field "
          "data. Each 75th percentile is rated against web.dev's thresholds (good: at or below the good limit; poor: "
          "above the poor limit). The Core Web Vitals assessment passes only when LCP, INP and CLS are all present "
          "and all good; it fails when any present one is not good; otherwise there is not enough field data. "
          "Queries are made in priority order (origins' current data, origins' history, then URLs' current data and "
          "history) until the run's call cap.")
LIMITS = [
    "CrUX is field data: real Chrome users who opted in, over a rolling 28-day window, about two days behind. It is "
    "not a lab test, does not cover other browsers, and cannot say why a page is slow.",
    "When Google has no field data for an exact origin or URL (it answers 'not found': too few samples, or the site "
    "is served on another form of the address, such as with or without www.), that is reported as no field data "
    "for that exact origin or URL and says nothing about speed in either direction.",
    "CrUX keys are exact: https://example.com and https://www.example.com are different origins (both are asked "
    "when only one is registered, and reported separately), and a URL may be reported under Google's normalised "
    "form.",
    "TTFB is labelled experimental by CrUX and web.dev calls its threshold a rough guide; FCP and TTFB are not Core "
    "Web Vitals. First Input Delay is retired (CrUX stopped serving it in September 2024) and is never asked for.",
    "The trend is descriptive (see the method); it is not a forecast and not a test of significance.",
]
NOT_REQUESTED = {"first_input_delay": "retired: the CrUX API and History API stopped serving it (web.dev/blog/fid, "
                                      "2024-09-10)"}


def rate(metric: str, p75: float) -> str:
    t = THRESHOLDS[metric]
    if p75 <= t["good"]:
        return "GOOD"
    if p75 > t["poor"]:
        return "POOR"
    return "NEEDS_IMPROVEMENT"


def _days_between(a: str, b: str) -> int:
    from datetime import date
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def assess(metrics: dict) -> dict:
    """The Core Web Vitals assessment from one record's rated metrics."""
    ratings = {m: metrics[cx.METRICS[m]].get("rating") for m in CORE}
    present = {m: r for m, r in ratings.items() if r is not None}
    if present and any(r != "GOOD" for r in present.values()):
        verdict = "FAILED"
    elif len(present) == len(CORE):
        verdict = "PASSED"
    else:
        verdict = "INSUFFICIENT_FIELD_DATA"
    return {"assessment": verdict, "metrics_considered": [cx.METRICS[m] for m in CORE],
            "missing": [cx.METRICS[m] for m in CORE if m not in present],
            "rule": "PASSED only when LCP, INP and CLS are all present and good at p75; FAILED when any present one "
                    "is not good; otherwise INSUFFICIENT_FIELD_DATA"}


def trend(metric: str, p75s: list) -> dict:
    vals = [v for v in p75s if v is not None]
    out = {"periods": len(p75s), "periods_with_data": len(vals), "method": "early_vs_late_median"}
    if len(vals) < MIN_TREND_POINTS:
        return {**out, "direction": "INSUFFICIENT_HISTORY",
                "why": f"fewer than {MIN_TREND_POINTS} weekly periods with data"}
    early, late = median(vals[:TREND_WINDOW]), median(vals[-TREND_WINDOW:])
    delta = late - early
    rel = None if early == 0 else delta / early
    small = abs(delta) < MIN_CHANGE[metric] or (rel is not None and abs(rel) < TREND_MIN_RELATIVE)
    direction = "NO_CLEAR_CHANGE" if small else ("IMPROVING" if delta < 0 else "WORSENING")
    return {**out, "direction": direction, "early_median_p75": round(early, 4), "late_median_p75": round(late, 4),
            "change": round(delta, 4), "relative_change": None if rel is None else round(rel, 4),
            "rating_early": rate(metric, early), "rating_late": rate(metric, late)}


def _provenance(method: str, period: dict | None, retrieved_at: str) -> dict:
    return {"source": cx.SOURCE, "provider": cx.PROVIDER, "api_version": cx.API_VERSION, "method": method,
            "collection_period": period, "retrieved_at": retrieved_at}


def shape_record(r: dict, today: str, retrieved_at: str) -> dict:
    """A provider record -> the section's current-data entry (ratings, assessment, freshness, provenance)."""
    if r["state"] != "OK":
        return {"state": r["state"], "provenance": _provenance("records:queryRecord", None, retrieved_at)}
    period = r["collection_period"]
    prov = _provenance("records:queryRecord", period, retrieved_at)
    metrics = {}
    for name, short in cx.METRICS.items():
        m = r["metrics"][name]
        if m["state"] != "OK":
            metrics[short] = {"state": m["state"]}
            continue
        metrics[short] = {"state": "OK", "metric": name, "p75": m["p75"], "unit": THRESHOLDS[name]["unit"],
                          "rating": rate(name, m["p75"]), "distribution": m["histogram"], "provenance": prov}
    fresh = _days_between(period["last"], today)
    return {"state": "OK", "reported_url": r.get("reported_url"), "collection_period": period,
            "extra_fields_ignored": r.get("extra_fields_ignored", 0),
            "freshness_days": fresh, "stale": fresh > STALE_DAYS["record"], "metrics": metrics,
            "core_web_vitals": assess(metrics), "provenance": prov}


def shape_history(r: dict, today: str, retrieved_at: str) -> dict:
    if r["state"] != "OK":
        return {"state": r["state"], "provenance": _provenance("records:queryHistoryRecord", None, retrieved_at)}
    periods = r["collection_periods"]
    span = {"first": periods[0]["first"], "last": periods[-1]["last"]}
    prov = _provenance("records:queryHistoryRecord", span, retrieved_at)
    metrics = {}
    for name, short in cx.METRICS.items():
        m = r["metrics"][name]
        if m["state"] != "OK":
            metrics[short] = {"state": m["state"]}
            continue
        metrics[short] = {"state": "OK", "metric": name, "p75s": m["p75s"], "unit": THRESHOLDS[name]["unit"],
                          "trend": trend(name, m["p75s"]), "provenance": prov}
    fresh = _days_between(span["last"], today)
    return {"state": "OK", "reported_url": r.get("reported_url"), "periods": len(periods),
            "extra_fields_ignored": r.get("extra_fields_ignored", 0),
            "collection_periods": periods, "freshness_days": fresh, "stale": fresh > STALE_DAYS["history"],
            "metrics": metrics, "provenance": prov}


def twin_of(domain: str) -> str | None:
    """The other form of a REGISTRABLE domain (eTLD+1) or of its ``www.`` form: ``www.example.com`` <->
    ``example.com``, ``example.co.uk`` <-> ``www.example.co.uk``. None for anything else: another subdomain
    (``shop.example.com`` has no twin here), and never a public suffix (``www.co.uk`` is not turned into ``co.uk``).
    The registrable domain is decided by the vendored Public Suffix List and local deny list already used for tenant
    domains (agents/domain_match.py; both the ICANN and PRIVATE sections count, so ``foo.github.io`` is
    registrable and ``github.io`` is not)."""
    from agents import domain_match as dm
    d = dm._ascii(domain)
    if d is None or d != domain:
        return None
    bare = d[4:] if d.startswith("www.") else d
    if "." not in bare or dm._suffix(bare) or not dm._suffix(bare.split(".", 1)[1]):
        return None
    return bare if d.startswith("www.") else "www." + bare


def calls_needed(paths: int, twin: bool = True) -> int:
    """The most queries one run makes when every query succeeds first time: per origin (the audited one, and its twin
    when asked) 3 records + 3 histories, then per audited path 3 records + 3 histories. Retries are extra calls on
    top (at most ``attempts - 1`` more per query). The recommended per-run cap, 12 + 6 x paths, is this with a twin."""
    return 6 * (2 if twin else 1) + 6 * paths


def plan(origin: str, urls: list, twin: str | None = None) -> list:
    """Every query Lena may make, in priority order: the origins' current data (audited, then its twin), the
    origins' history, then each URL's current data, then each URL's history; phone first. (Which origin the URLs
    use is decided during the run; ``urls`` here are on the audited origin.)"""
    origins = [origin] + ([twin] if twin else [])
    q = [("record", "origin", o, ff) for o in origins for ff in cx.FORM_FACTORS]
    q += [("history", "origin", o, ff) for o in origins for ff in cx.FORM_FACTORS]
    for u in urls:
        q += [("record", "url", u, ff) for ff in cx.FORM_FACTORS]
    for u in urls:
        q += [("history", "url", u, ff) for ff in cx.FORM_FACTORS]
    return q


def run(read, origin: str, paths: list, periods: int, today: str, retrieved_at: str,
        twin: str | None = None) -> dict:
    """Make the queries through ``read`` (a provider read session) and shape the section. Never raises a CruxError:
    a query that failed is recorded with its code; a stopping code ends the run and the queries not sent are listed
    as NOT_QUERIED with that code. ``twin`` (an https origin) is the other form of the audited domain, asked only
    when the tenant has not registered it; it is reported as its own target, never merged with the audited one."""
    results: dict = {}                       # (method, kind, target, ff) -> entry
    state = {"stopped": None}

    def ask(method: str, kind: str, target: str, ff: str, url_origin: str | None = None) -> None:
        k = (method, kind, target, ff)
        if state["stopped"] is not None:
            results[k] = {"state": "NOT_QUERIED", "reason": state["stopped"]["code"]}
            return
        cur = results.get(("record", kind, target, ff))
        if method == "history" and cur is not None and cur["state"] == "INSUFFICIENT_FIELD_DATA":
            results[k] = {"state": "NOT_QUERIED", "reason": "NO_CURRENT_FIELD_DATA"}
            return
        if kind == "url":
            o = results.get(("record", "origin", url_origin, ff))
            if o is not None and o["state"] == "INSUFFICIENT_FIELD_DATA":
                results[k] = {"state": "NOT_QUERIED", "reason": "ORIGIN_HAS_NO_FIELD_DATA"}
                return
        try:
            if method == "record":
                results[k] = shape_record(read.record(kind, target, ff), today, retrieved_at)
            else:
                results[k] = shape_history(read.history(kind, target, ff, periods), today, retrieved_at)
        except cx.CruxError as e:
            results[k] = {"state": "FAILED", "reason": e.code, "detail": e.detail}
            if e.code in cx.STOPPING:
                state["stopped"] = {"code": e.code, "detail": e.detail}

    origins = [("audited", origin)] + ([("twin", twin)] if twin else [])
    for _role, o in origins:
        for ff in cx.FORM_FACTORS:
            ask("record", "origin", o, ff)
    for _role, o in origins:
        for ff in cx.FORM_FACTORS:
            ask("history", "origin", o, ff)

    def has_data(o: str) -> bool:
        return any(results[("record", "origin", o, ff)]["state"] == "OK" for ff in cx.FORM_FACTORS)
    url_origin = twin if twin and not has_data(origin) and has_data(twin) else origin
    urls = [url_origin + p for p in paths]
    for u in urls:
        for ff in cx.FORM_FACTORS:
            ask("record", "url", u, ff, url_origin)
    for u in urls:
        for ff in cx.FORM_FACTORS:
            ask("history", "url", u, ff, url_origin)
    targets = []
    for role, o in origins:
        targets.append({"kind": "origin", "role": role, "target": o, "has_field_data": has_data(o),
                        "form_factors": {ff: {"current": results[("record", "origin", o, ff)],
                                              "history": results[("history", "origin", o, ff)]}
                                         for ff in cx.FORM_FACTORS}})
    for p, u in zip(paths, urls):
        targets.append({"kind": "url", "role": "audited" if url_origin == origin else "twin", "target": u,
                        "path": p, "origin": url_origin,
                        "form_factors": {ff: {"current": results[("record", "url", u, ff)],
                                              "history": results[("history", "url", u, ff)]}
                                         for ff in cx.FORM_FACTORS}})
    return {"source": cx.SOURCE, "provider": cx.PROVIDER, "api_version": cx.API_VERSION, "docs": dict(cx.DOCS),
            "thresholds_version": THRESHOLDS_VERSION,
            "thresholds": {cx.METRICS[m]: dict(t) for m, t in THRESHOLDS.items()},
            "metrics_requested": list(cx.METRICS), "not_requested": dict(NOT_REQUESTED),
            "history_periods_requested": periods, "retrieved_at": retrieved_at, "origin": origin,
            "twin": twin, "url_origin": url_origin,
            "twin_rule": "the other form (bare / www.) of the audited domain, https, asked only when the tenant has not "
                         "registered it; reported as its own target and never merged with the audited origin",
            "targets": targets, "calls": getattr(read, "requests", 0), "stopped": state["stopped"],
            "counts": counts(results)}


def counts(results: dict) -> dict:
    c = {"queries": len(results), "with_data": 0, "insufficient_field_data": 0, "failed": 0, "not_queried": 0,
         "malformed_metrics": 0, "extra_fields_ignored": 0}
    for v in results.values():
        st = v["state"]
        if st == "OK":
            c["with_data"] += 1
            c["extra_fields_ignored"] += v.get("extra_fields_ignored", 0)
            c["malformed_metrics"] += sum(1 for m in v["metrics"].values() if m["state"] == "MALFORMED")
        elif st == "INSUFFICIENT_FIELD_DATA":
            c["insufficient_field_data"] += 1
        elif st == "FAILED":
            c["failed"] += 1
        else:
            c["not_queried"] += 1
    return c


_STOP_OUTCOME = {"CRUX_KILLED": "KILLED", "CRUX_AUTH_REFUSED": "BLOCKED", "CRUX_BUDGET_EXHAUSTED": "BLOCKED",
                 "CRUX_NOT_CONNECTED": "NOT_CONNECTED"}


def outcome(section: dict) -> tuple[str, str | None]:
    """(outcome, reason) for Lena's envelope. Data and nothing missing: OK. Data with gaps (a failed query, a query
    not sent, a malformed metric): PARTIAL. Killed: KILLED. No data at all: the stopping or failing code, or
    INSUFFICIENT_EVIDENCE / CRUX_INSUFFICIENT_FIELD_DATA when Google simply has none."""
    c, stop = section["counts"], section["stopped"]
    if stop is not None and stop["code"] == "CRUX_KILLED":
        return "KILLED", "CRUX_KILLED"
    gaps = c["failed"] or c["malformed_metrics"] or any(
        ff[k].get("reason") not in (None, "NO_CURRENT_FIELD_DATA", "ORIGIN_HAS_NO_FIELD_DATA")
        for t in section["targets"] for ff in t["form_factors"].values() for k in ("current", "history")
        if ff[k]["state"] == "NOT_QUERIED")
    if c["with_data"]:
        return ("PARTIAL", stop["code"] if stop else ("CRUX_PROTOCOL_ERROR" if c["failed"] else None)) if gaps \
            else ("OK", None)
    if stop is not None:
        return _STOP_OUTCOME.get(stop["code"], "FAILED"), stop["code"]
    if c["failed"]:
        return "FAILED", "CRUX_PROTOCOL_ERROR"
    return "INSUFFICIENT_EVIDENCE", "CRUX_INSUFFICIENT_FIELD_DATA"
