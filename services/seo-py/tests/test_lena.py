"""Lena's CrUX adapter and her shaping (ADR 0050): the documented request and answer shapes, the key only in a header,
Google's 404 as INSUFFICIENT_FIELD_DATA, partial and malformed metrics, malformed histograms, 429 / 5xx / timeouts and
the deadline on an injected clock, the call cap, the host allowlist, thresholds and the trend. No network: a fake
CrUX behind httpx.MockTransport, or a fake socket layer under the production deadline backend."""

from __future__ import annotations

import copy
import json
import pickle

import httpx
import pytest
from crux_fakes import NOT_FOUND, TEST_KEY, FakeCrux, history_answer, record_answer
from gsc_fakes import Clock, FakeNetwork, http_response

import crux_client as cx
from agents import lena
from primitives import Killed

O = "https://acme.test"
U = "https://acme.test/pricing"
GLOBAL_IP = "142.250.80.42"


def client(fake: FakeCrux, max_calls: int = 100, **kw) -> cx.CruxClient:
    c = fake.clock
    return cx.CruxClient(cx.ApiKey(TEST_KEY), transport=fake.transport(), monotonic=c.mono, sleep=c.sleep,
                         max_calls=max_calls, **kw)


def ok_guard():
    return None


def rd(fake, **kw):
    return client(fake, **kw).open_read(ok_guard)


def err(fn) -> cx.CruxError:
    with pytest.raises(cx.CruxError) as e:
        fn()
    return e.value


def run(fake, paths=("/pricing",), periods=25, twin=None, **kw):
    return lena.run(rd(fake, **kw), O, list(paths), periods, "2026-10-09", "2026-10-09T18:00:00Z", twin=twin)


# ====================================================================== the request

def test_request_is_the_documented_post_with_the_key_only_in_the_header():
    fake = FakeCrux()
    r = rd(fake).record("origin", O, "PHONE")
    req = fake.requests[0]
    assert req.method == "POST" and str(req.url) == "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
    assert req.headers[cx.KEY_HEADER] == TEST_KEY and TEST_KEY not in str(req.url) and not req.url.query
    body = json.loads(req.content)
    assert body == {"origin": O, "formFactor": "PHONE", "metrics": list(cx.METRICS)}
    assert "first_input_delay" not in body["metrics"]
    assert r["state"] == "OK" and r["collection_period"] == {"first": "2026-09-10", "last": "2026-10-07"}
    rd(fake).history("url", U, "TABLET", 25)
    hreq = fake.requests[1]
    assert str(hreq.url).endswith("/v1/records:queryHistoryRecord")
    assert json.loads(hreq.content) == {"url": U, "formFactor": "TABLET", "metrics": list(cx.METRICS),
                                        "collectionPeriodCount": 25}


def test_record_values_cls_strings_and_histograms():
    r = rd(FakeCrux()).record("origin", O, "DESKTOP")
    m = r["metrics"]
    assert m["largest_contentful_paint"]["p75"] == 2100.0 and m["cumulative_layout_shift"]["p75"] == 0.05
    hist = m["largest_contentful_paint"]["histogram"]
    assert [(b["start"], b["end"]) for b in hist] == [(0.0, 2500.0), (2500.0, 4000.0), (4000.0, None)]


# ====================================================================== 404 and partial data

def test_googles_404_is_insufficient_field_data_never_fast_or_slow():
    fake = FakeCrux()
    fake.missing.add(("origin", O))
    r = rd(fake).record("origin", O, "PHONE")
    assert r == {"state": "INSUFFICIENT_FIELD_DATA", "kind": "origin", "target": O, "form_factor": "PHONE"}
    sec = run(fake, paths=())
    shaped = sec["targets"][0]["form_factors"]["PHONE"]["current"]
    assert shaped["state"] == "INSUFFICIENT_FIELD_DATA" and "metrics" not in shaped and "rating" not in json.dumps(shaped)
    assert lena.outcome(sec) == ("INSUFFICIENT_EVIDENCE", "CRUX_INSUFFICIENT_FIELD_DATA")


def test_a_404_that_is_not_googles_no_data_answer_is_a_protocol_error():
    for payload in ({"error": {"code": 404, "status": "SOMETHING_ELSE"}}, b"<html>not found</html>", {}):
        fake = FakeCrux()
        fake.script = [(404, payload)]
        assert err(lambda f=fake: rd(f).record("origin", O, "PHONE")).code == "CRUX_PROTOCOL_ERROR"


def test_partial_metrics_and_the_core_web_vitals_assessment():
    fake = FakeCrux()
    fake.answers[("record", "origin", O, "PHONE")] = (200, record_answer("origin", O, "PHONE",
                                                                        drop=("interaction_to_next_paint",)))
    fake.answers[("record", "origin", O, "DESKTOP")] = (200, record_answer(
        "origin", O, "DESKTOP", p75={"largest_contentful_paint": 4100}, drop=("interaction_to_next_paint",)))
    sec = run(fake, paths=())
    ph = sec["targets"][0]["form_factors"]["PHONE"]["current"]
    assert ph["metrics"]["INP"] == {"state": "ABSENT"}
    assert ph["core_web_vitals"]["assessment"] == "INSUFFICIENT_FIELD_DATA" and ph["core_web_vitals"]["missing"] == ["INP"]
    dk = sec["targets"][0]["form_factors"]["DESKTOP"]["current"]
    assert dk["metrics"]["LCP"]["rating"] == "POOR" and dk["core_web_vitals"]["assessment"] == "FAILED"
    tb = sec["targets"][0]["form_factors"]["TABLET"]["current"]
    assert tb["core_web_vitals"]["assessment"] == "PASSED"


def test_url_level_missing_but_origin_present():
    fake = FakeCrux()
    fake.missing.add(("url", U))
    sec = run(fake)
    origin, url = sec["targets"]
    assert all(v["current"]["state"] == "OK" for v in origin["form_factors"].values())
    assert all(v["current"]["state"] == "INSUFFICIENT_FIELD_DATA" for v in url["form_factors"].values())
    assert all(v["history"] == {"state": "NOT_QUERIED", "reason": "NO_CURRENT_FIELD_DATA"}
               for v in url["form_factors"].values())
    assert lena.outcome(sec) == ("OK", None)


def test_no_origin_data_skips_url_queries_and_says_so():
    fake = FakeCrux()
    fake.missing |= {("origin", O), ("url", U)}
    sec = run(fake)
    assert len(fake.requests) == 3                                # the three origin records only
    url = sec["targets"][1]["form_factors"]["PHONE"]
    assert url["current"] == {"state": "NOT_QUERIED", "reason": "ORIGIN_HAS_NO_FIELD_DATA"}
    assert lena.outcome(sec) == ("INSUFFICIENT_EVIDENCE", "CRUX_INSUFFICIENT_FIELD_DATA")


# ====================================================================== malformed answers

def _bad_metric(name, **change):
    m = {"histogram": [{"start": 0, "end": 2500, "density": 0.8}, {"start": 2500, "end": 4000, "density": 0.15},
                       {"start": 4000, "density": 0.05}], "percentiles": {"p75": 2100}}
    for k, v in change.items():
        if k == "hist":
            m["histogram"] = v
        elif k == "p75":
            m["percentiles"] = {"p75": v}
        else:
            m[k] = v
    return {name: m}


LCP = "largest_contentful_paint"
BAD_METRICS = [
    _bad_metric(LCP, hist=[{"start": 0, "end": 2500, "density": 0.5}, {"start": 2500, "density": 0.1}]),   # sum
    _bad_metric(LCP, hist=[{"start": 0, "end": 2500, "density": 0.8}, {"start": 2600, "density": 0.2}]),   # gap
    _bad_metric(LCP, hist=[{"start": 0, "end": 0, "density": 0.8}, {"start": 0, "density": 0.2}]),         # empty bin
    _bad_metric(LCP, hist=[{"start": 0, "density": 0.8}, {"start": 2500, "density": 0.2}]),               # open middle
    _bad_metric(LCP, hist=[{"start": 0, "end": 2500, "density": 1.8}, {"start": 2500, "density": -0.8}]),  # range
    _bad_metric(LCP, hist=[{"start": 10, "end": 2500, "density": 0.8}, {"start": 2500, "density": 0.2}]),  # not at 0
    _bad_metric(LCP, hist="many"),
    _bad_metric(LCP, hist=[{"start": 0, "end": 2500, "density": True}, {"start": 2500, "density": 0.2}]),
    _bad_metric(LCP, p75="2100"),                                   # a string where a number is documented
    _bad_metric(LCP, p75=float("inf")),
    _bad_metric(LCP, p75=10 ** 400),
    {"cumulative_layout_shift": {"percentiles": {"p75": 0.05}}},    # CLS is a string in CrUX
    {"cumulative_layout_shift": {"percentiles": {"p75": "1e3"}}},
    {LCP: {"histogram": []}},                                       # no percentiles
    {LCP: "x"},
]


@pytest.mark.parametrize("metrics", BAD_METRICS)
def test_a_malformed_metric_is_set_aside_and_the_others_are_kept(metrics):
    name = next(iter(metrics))
    fake = FakeCrux()
    fake.script = [(200, record_answer("origin", O, "PHONE", metrics=metrics))]
    r = rd(fake).record("origin", O, "PHONE")
    assert r["metrics"][name] == {"state": "MALFORMED"}
    assert all(v["state"] == "OK" for k, v in r["metrics"].items() if k != name)


BAD_RECORDS = [
    b"not json", {"record": {}}, {"recordz": {}}, {"record": {"key": {}, "metrics": {}}},
    lambda a: a["record"]["key"].update(origin="https://evil.test"),
    lambda a: a["record"]["key"].update(formFactor="DESKTOP"),
    lambda a: a.update({f"x{i}": 1 for i in range(17)}),                       # past the extra-field bound
    lambda a: a["record"]["key"].update(url=U),                                  # both key kinds: ambiguous
    lambda a: a["record"].pop("metrics"),
    lambda a: a["record"].update(collectionPeriod={"firstDate": {"year": 2026, "month": 13, "day": 1},
                                                   "lastDate": {"year": 2026, "month": 10, "day": 7}}),
    lambda a: a["record"]["collectionPeriod"].update(firstDate={"year": 2026, "month": 10, "day": 9}),
    lambda a: a["record"].update(metrics={f"m{i}": {} for i in range(100)}),
    lambda a: a["record"].pop("collectionPeriod"),
]


@pytest.mark.parametrize("bad", BAD_RECORDS)
def test_record_level_shape_errors_are_protocol_errors_never_data(bad):
    payload = bad
    if callable(bad):
        payload = record_answer("origin", O, "PHONE")
        bad(payload)
    fake = FakeCrux()
    fake.script = [(200, payload)]
    assert err(lambda: rd(fake).record("origin", O, "PHONE")).code == "CRUX_PROTOCOL_ERROR"


def test_url_normalisation_is_accepted_only_on_the_same_origin():
    fake = FakeCrux()
    norm = U + "/"
    fake.script = [(200, record_answer("url", norm, "PHONE", norm={"originalUrl": U, "normalizedUrl": norm}))]
    assert rd(fake).record("url", U, "PHONE")["reported_url"] == norm
    evil = "https://evil.test/pricing"
    fake.script = [(200, record_answer("url", evil, "PHONE", norm={"originalUrl": U, "normalizedUrl": evil}))]
    assert err(lambda: rd(fake).record("url", U, "PHONE")).code == "CRUX_PROTOCOL_ERROR"
    fake.script = [(200, record_answer("url", norm, "PHONE"))]                 # changed without saying so
    assert err(lambda: rd(fake).record("url", U, "PHONE")).code == "CRUX_PROTOCOL_ERROR"


def test_history_nan_and_null_periods_and_malformed_series():
    s = [2000.0] * 20 + [None] * 5
    fake = FakeCrux()
    fake.script = [(200, history_answer("origin", O, "PHONE", series={LCP: s}))]
    h = rd(fake).history("origin", O, "PHONE", 25)
    assert h["metrics"][LCP]["p75s"][-5:] == [None] * 5 and h["metrics"][LCP]["histogram"][0]["densities"][-1] is None
    short = history_answer("origin", O, "PHONE")
    short["record"]["metrics"][LCP]["percentilesTimeseries"]["p75s"] = [1.0] * 24          # one value short
    halfnan = history_answer("origin", O, "PHONE")
    halfnan["record"]["metrics"][LCP]["histogramTimeseries"][0]["densities"][3] = "NaN"   # one bin NaN, not all
    for bad in (short, halfnan):
        fake.script = [(200, bad)]
        hh = rd(fake).history("origin", O, "PHONE", 25)
        assert hh["metrics"][LCP] == {"state": "MALFORMED"} and hh["metrics"]["first_contentful_paint"]["state"] == "OK"
    unordered = history_answer("origin", O, "PHONE")
    cps = unordered["record"]["collectionPeriods"]
    cps[3], cps[4] = cps[4], cps[3]
    toomany = history_answer("origin", O, "PHONE", n=30)
    for bad in (unordered, toomany):
        fake.script = [(200, bad)]
        assert err(lambda: rd(fake).history("origin", O, "PHONE", 25)).code == "CRUX_PROTOCOL_ERROR"


# ====================================================================== errors, retries, deadline, cap

def _gerr(status, gstatus, reason=None):
    e = {"code": status, "message": "echoed " + TEST_KEY, "status": gstatus}
    if reason:
        e["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]
    return (status, {"error": e})


@pytest.mark.parametrize("answer,code,detail", [
    (_gerr(400, "INVALID_ARGUMENT", "API_KEY_INVALID"), "CRUX_AUTH_REFUSED", "API_KEY_INVALID"),
    (_gerr(403, "PERMISSION_DENIED", "SERVICE_DISABLED"), "CRUX_AUTH_REFUSED", "SERVICE_DISABLED"),
    (_gerr(403, "PERMISSION_DENIED"), "CRUX_AUTH_REFUSED", "PERMISSION_DENIED"),
    (_gerr(401, "UNAUTHENTICATED"), "CRUX_AUTH_REFUSED", "UNAUTHENTICATED"),
    (_gerr(403, "RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"), "CRUX_RATE_LIMITED", "RATE_LIMIT_EXCEEDED"),
    (_gerr(400, "INVALID_ARGUMENT"), "CRUX_PROTOCOL_ERROR", "http_400"),
    ((301, {}, {"location": "https://evil.test/"}), "CRUX_PROTOCOL_ERROR", "http_301"),
    ((418, {}), "CRUX_PROTOCOL_ERROR", "http_418"),
])
def test_error_answers_map_to_closed_codes_and_never_echo_google(answer, code, detail):
    fake = FakeCrux()
    fake.script = [answer]
    e = err(lambda: rd(fake).record("origin", O, "PHONE"))
    assert (e.code, e.detail) == (code, detail) and len(fake.requests) == 1
    assert TEST_KEY not in repr(e) and TEST_KEY not in str(e) and "echoed" not in str(e.detail)


def test_429_backs_off_within_bounds_then_rate_limited_and_recovers():
    fake = FakeCrux()
    fake.script = [(429, {}, {"retry-after": "7"})] * 3
    e = err(lambda: rd(fake).record("origin", O, "PHONE"))
    assert e.code == "CRUX_RATE_LIMITED" and len(fake.requests) == 3 and fake.clock.sleeps == [7.0, 7.0]
    fake.script = [(429, {})]
    assert rd(fake).record("origin", O, "PHONE")["state"] == "OK"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retried_then_unavailable(status):
    fake = FakeCrux()
    fake.script = [(status, {})] * 3
    assert err(lambda: rd(fake).record("origin", O, "PHONE")).code == "CRUX_UNAVAILABLE" and len(fake.requests) == 3


def test_timeouts_are_retried_then_unavailable_and_retries_stop_at_the_deadline():
    fake = FakeCrux()
    fake.raise_on = [httpx.ReadTimeout("slow")] * 3
    assert err(lambda: rd(fake).record("origin", O, "PHONE")).code == "CRUX_UNAVAILABLE"
    assert len(fake.requests) == 3
    fake = FakeCrux()
    fake.script = [(503, {}, {"retry-after": "30"})] * 3
    e = err(lambda: rd(fake, deadline_s=20).record("origin", O, "PHONE"))
    assert e.code == "CRUX_UNAVAILABLE" and len(fake.requests) == 1          # a 30 s pause would pass the deadline


def test_the_call_cap_counts_retries_and_stops_the_run():
    fake = FakeCrux()
    fake.script = [(503, {})]
    sec = run(fake, max_calls=4)
    assert len(fake.requests) == 4 and sec["calls"] == 4
    assert sec["stopped"] == {"code": "CRUX_BUDGET_EXHAUSTED", "detail": "calls_per_run"}
    # priority order: the origin's current data first
    assert fake.keys()[:3] == [("record", "origin", O, "PHONE"), ("record", "origin", O, "PHONE"),
                               ("record", "origin", O, "DESKTOP")]
    later = sec["targets"][1]["form_factors"]["PHONE"]
    assert later["current"] == {"state": "NOT_QUERIED", "reason": "CRUX_BUDGET_EXHAUSTED"}
    assert lena.outcome(sec) == ("PARTIAL", "CRUX_BUDGET_EXHAUSTED")


def test_a_stopping_error_ends_the_run_and_lists_what_was_not_asked():
    fake = FakeCrux()
    fake.answers[("record", "origin", O, "DESKTOP")] = _gerr(403, "PERMISSION_DENIED")
    sec = run(fake)
    assert len(fake.requests) == 2 and sec["stopped"]["code"] == "CRUX_AUTH_REFUSED"
    assert sec["counts"]["with_data"] == 1 and sec["counts"]["not_queried"] == sec["counts"]["queries"] - 2
    assert lena.outcome(sec) == ("PARTIAL", "CRUX_AUTH_REFUSED")
    fake = FakeCrux()
    fake.script = [_gerr(403, "PERMISSION_DENIED")]
    assert lena.outcome(run(fake)) == ("BLOCKED", "CRUX_AUTH_REFUSED")


def test_one_malformed_answer_does_not_stop_the_run():
    fake = FakeCrux()
    fake.answers[("record", "origin", O, "PHONE")] = (200, b"<html>")
    sec = run(fake)
    assert sec["stopped"] is None and sec["counts"]["failed"] == 1 and sec["counts"]["with_data"] > 1
    assert lena.outcome(sec) == ("PARTIAL", "CRUX_PROTOCOL_ERROR")


def test_kill_switch_inside_a_read_stops_it():
    fake = FakeCrux()
    calls = {"n": 0}

    def guard():
        calls["n"] += 1
        if len(fake.requests) >= 2:
            raise Killed("KILLED_PROVIDER")
    sec = lena.run(client(fake).open_read(guard), O, ["/pricing"], 25, "2026-10-09", "2026-10-09T18:00:00Z")
    assert len(fake.requests) == 2 and sec["stopped"]["code"] == "CRUX_KILLED"
    assert lena.outcome(sec) == ("KILLED", "CRUX_KILLED")


def test_host_allowlist_and_the_key_never_in_a_url():
    for url in ("https://evil.test/v1/records:queryRecord", "http://chromeuxreport.googleapis.com/v1/records:queryRecord",
                "https://chromeuxreport.googleapis.com:8443/v1/records:queryRecord",
                "https://chromeuxreport.googleapis.com/v1/records:queryRecord?key=x",
                "https://chromeuxreport.googleapis.com/v1/other"):
        assert err(lambda u=url: cx.check_url(u)).code == "CRUX_PROTOCOL_ERROR"
    assert cx.check_url(cx.RECORD_URL) and cx.check_url(cx.HISTORY_URL)


def test_body_caps_and_encodings():
    fake = FakeCrux()
    fake.script = [(200, b"x" * (cx.MAX_BODY + 1))]
    assert err(lambda: rd(fake).record("origin", O, "PHONE")).detail == "body_too_large"
    fake.script = [(200, b"\x1f\x8b", {"content-encoding": "gzip"})]
    assert err(lambda: rd(fake).record("origin", O, "PHONE")).code == "CRUX_PROTOCOL_ERROR"


def _prod_client(clock, net, resolve=lambda h, p: [GLOBAL_IP], **kw):
    from finance_client import _DnsGate

    class SyncThread:                       # the resolver's thread, run inline (deterministic, no wall clock)
        def __init__(self, target, name, daemon):
            self.t = target

        def start(self):
            self.t()
    c = cx.CruxClient(cx.ApiKey(TEST_KEY), network_backend=net, monotonic=clock.mono, sleep=clock.sleep,
                      resolve=resolve, dns_gate=_DnsGate(2), max_calls=10, **kw)
    c._resolver._inner._thread = SyncThread
    return c


def test_production_transport_goes_through_the_deadline_backend_to_a_global_address():
    clock = Clock()
    net = FakeNetwork([http_response(200, record_answer("origin", O, "PHONE"))], clock)
    r = _prod_client(clock, net).open_read(ok_guard).record("origin", O, "PHONE")
    assert r["state"] == "OK" and net.connects[0][0] == GLOBAL_IP and net.connects[0][1] == 443
    assert ("tls", "chromeuxreport.googleapis.com") in net.log


def test_production_transport_refuses_non_global_answers_and_a_slow_body():
    clock = Clock()
    net = FakeNetwork([], clock)
    for answer in (["10.0.0.5"], ["127.0.0.1"], [GLOBAL_IP, "169.254.169.254"], []):
        c = _prod_client(clock, net, resolve=lambda h, p, a=answer: a, attempts=1)
        assert err(lambda cl=c: cl.open_read(ok_guard).record("origin", O, "PHONE")).code == "CRUX_UNAVAILABLE"
    assert net.connects == []
    slow = FakeNetwork([http_response(200, record_answer("origin", O, "PHONE"))], clock, step_s=0.4, chunk=1)
    c = _prod_client(clock, slow, deadline_s=5, attempts=1)
    assert err(lambda: c.open_read(ok_guard).record("origin", O, "PHONE")).code == "CRUX_UNAVAILABLE"


def test_the_key_object_masks_and_refuses_pickling():
    k = cx.ApiKey(TEST_KEY)
    assert TEST_KEY not in repr(k) and TEST_KEY not in str(k)
    c = cx.CruxClient(k, max_calls=1)
    assert TEST_KEY not in repr(c) and not hasattr(k, "__dict__")
    for fn in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            fn(k)
    for bad in ("short", "has space in it 0123456789", "x" * 200, 12345678901234567890):
        with pytest.raises(ValueError):
            cx.ApiKey(bad)


def test_not_connected_never_sends():
    with pytest.raises(cx.CruxError) as e:
        cx.NotConnectedFieldData().open_read(ok_guard)
    assert e.value.code == "CRUX_NOT_CONNECTED"


# ====================================================================== thresholds and trend

@pytest.mark.parametrize("metric,value,rating", [
    (LCP, 2500, "GOOD"), (LCP, 2501, "NEEDS_IMPROVEMENT"), (LCP, 4000, "NEEDS_IMPROVEMENT"), (LCP, 4001, "POOR"),
    ("interaction_to_next_paint", 200, "GOOD"), ("interaction_to_next_paint", 501, "POOR"),
    ("cumulative_layout_shift", 0.1, "GOOD"), ("cumulative_layout_shift", 0.25, "NEEDS_IMPROVEMENT"),
    ("cumulative_layout_shift", 0.26, "POOR"), ("first_contentful_paint", 1800, "GOOD"),
    ("first_contentful_paint", 3001, "POOR"), ("experimental_time_to_first_byte", 800, "GOOD"),
    ("experimental_time_to_first_byte", 1801, "POOR"),
])
def test_ratings_follow_web_dev_thresholds_at_the_boundaries(metric, value, rating):
    assert lena.rate(metric, float(value)) == rating


def test_trend_directions_with_honest_uncertainty_and_no_forecast():
    worse = [2000.0] * 13 + [3000.0] * 12
    t = lena.trend(LCP, worse)
    assert t["direction"] == "WORSENING" and t["rating_early"] == "GOOD" and t["rating_late"] == "NEEDS_IMPROVEMENT"
    assert lena.trend(LCP, list(reversed(worse)))["direction"] == "IMPROVING"
    assert lena.trend(LCP, [2000.0] * 12 + [2050.0] * 13)["direction"] == "NO_CLEAR_CHANGE"     # under 100 ms
    gappy = [2000.0] * 6 + [None] * 14 + [3000.0] * 5
    assert lena.trend(LCP, gappy)["direction"] == "INSUFFICIENT_HISTORY"
    assert "forecast" not in json.dumps(t) and "nothing is forecast" in lena.TREND_METHOD


def test_every_number_in_a_section_carries_its_provenance():
    sec = run(FakeCrux())
    for t in sec["targets"]:
        for ff in t["form_factors"].values():
            for part in ("current", "history"):
                e = ff[part]
                assert e["provenance"]["source"] == cx.SOURCE and e["provenance"]["collection_period"]
                for m in e["metrics"].values():
                    assert m["provenance"]["method"] in ("records:queryRecord", "records:queryHistoryRecord")
                    assert m["provenance"]["retrieved_at"] == "2026-10-09T18:00:00Z"
    cur = sec["targets"][0]["form_factors"]["PHONE"]["current"]
    assert cur["freshness_days"] == 2 and cur["stale"] is False
    assert sec["not_requested"] == {"first_input_delay": lena.NOT_REQUESTED["first_input_delay"]}
    assert sec["counts"]["queries"] == 12 and sec["calls"] == 12 and lena.outcome(sec) == ("OK", None)


def test_stale_collection_period_is_flagged():
    fake = FakeCrux()
    fake.answers[("record", "origin", O, "PHONE")] = (200, record_answer(
        "origin", O, "PHONE", period=((2026, 8, 1), (2026, 8, 28))))
    cur = run(fake, paths=())["targets"][0]["form_factors"]["PHONE"]["current"]
    assert cur["freshness_days"] == 42 and cur["stale"] is True


def test_not_found_constant_is_googles_documented_body():
    assert cx.is_not_found(json.dumps(NOT_FOUND).encode())


# ====================================================================== review fixes (M1 twins, M2 pacing, L2, L3)

W = "https://www.acme.test"


def test_the_twin_origin_is_asked_and_reported_separately_never_merged():
    fake = FakeCrux()
    fake.missing |= {("origin", O), ("url", U)}                        # the site lives on www.
    sec = run(fake, twin=W)
    origin, twin, url = sec["targets"]
    assert (origin["role"], origin["target"], origin["has_field_data"]) == ("audited", O, False)
    assert (twin["role"], twin["target"], twin["has_field_data"]) == ("twin", W, True)
    assert all(v["current"]["state"] == "INSUFFICIENT_FIELD_DATA" for v in origin["form_factors"].values())
    assert all(v["history"]["state"] == "NOT_QUERIED" for v in origin["form_factors"].values())   # history: twin only
    assert all(v["current"]["state"] == "OK" for v in twin["form_factors"].values())
    assert sec["url_origin"] == W and url["target"] == W + "/pricing" and url["origin"] == W
    keys = fake.keys()
    assert [k for k in keys if k[0] == "record" and k[1] == "origin"] == \
        [("record", "origin", O, ff) for ff in cx.FORM_FACTORS] + [("record", "origin", W, ff) for ff in cx.FORM_FACTORS]
    assert not any(k[0] == "history" and k[2] == O for k in keys)
    assert all(k[2].startswith(W) for k in keys if k[1] == "url")
    assert lena.outcome(sec) == ("OK", None)
    assert len(fake.requests) == 6 + 3 + 6                             # +3 records for the twin, history for it only


def test_both_origins_with_data_keep_the_urls_on_the_audited_host():
    fake = FakeCrux()
    sec = run(fake, twin=W)
    assert sec["url_origin"] == O and [t["target"] for t in sec["targets"]] == [O, W, U]
    assert sec["targets"][0]["form_factors"]["PHONE"]["current"] is not sec["targets"][1]["form_factors"]["PHONE"]["current"]


def test_neither_origin_has_data():
    fake = FakeCrux()
    fake.missing |= {("origin", O), ("origin", W)}
    sec = run(fake, twin=W)
    assert len(fake.requests) == 6 and sec["url_origin"] == O
    assert lena.outcome(sec) == ("INSUFFICIENT_EVIDENCE", "CRUX_INSUFFICIENT_FIELD_DATA")


def test_lena_twin_of():
    assert lena.twin_of("acme.test") == "www.acme.test" and lena.twin_of("www.acme.test") == "acme.test"


def test_unknown_extra_fields_are_ignored_counted_and_bounded():
    a = record_answer("origin", O, "PHONE")
    a["newTopLevel"] = {"x": 1}
    a["record"]["key"]["effectiveConnectionType"] = "4G"
    a["record"]["metrics"][LCP]["percentiles"]["p90"] = 3000
    a["record"]["metrics"][LCP]["newField"] = True
    a["record"]["metrics"][LCP]["histogram"][0]["label"] = "good"
    fake = FakeCrux()
    fake.script = [(200, a)]
    r = rd(fake).record("origin", O, "PHONE")
    assert r["metrics"][LCP]["state"] == "OK" and r["metrics"][LCP]["p75"] == 2100.0
    assert r["extra_fields_ignored"] == 5 and "newTopLevel" not in json.dumps(r) and "p90" not in json.dumps(r)
    over = record_answer("origin", O, "PHONE")
    over["record"]["metrics"][LCP].update({f"f{i}": 1 for i in range(cx.MAX_EXTRA_FIELDS + 1)})
    fake.script = [(200, over)]
    assert rd(fake).record("origin", O, "PHONE")["metrics"][LCP] == {"state": "MALFORMED"}
    h = history_answer("origin", O, "PHONE")
    h["record"]["metrics"][LCP]["percentilesTimeseries"]["p90s"] = [1] * 25
    fake.script = [(200, h)]
    hh = rd(fake).history("origin", O, "PHONE", 25)
    assert hh["metrics"][LCP]["state"] == "OK" and hh["extra_fields_ignored"] == 1
    sec = run(FakeCrux(), paths=())
    assert sec["counts"]["extra_fields_ignored"] == 0                 # the go-live canary expects 0


def _window_max(times: list, span: float = 60.0) -> int:
    best, j = 0, 0
    for i, t in enumerate(times):
        while times[j] <= t - span:
            j += 1
        best = max(best, i - j + 1)
    return best


def test_pacer_never_allows_more_than_100_in_any_minute():
    clock = Clock()
    p = cx._Pacer(clock.mono)
    times = []
    for _ in range(400):
        w = p.take()
        if w > 0:
            clock.sleep(w)
        times.append(clock.t)
    assert cx.PACE_MAX_PER_MINUTE == 100 and _window_max(times) <= 100
    assert times[:10] == [times[0]] * 10                               # the burst goes at once
    assert _window_max(times) >= 95                                    # and the pace is not needlessly slow


def test_client_requests_are_paced_across_reads():
    fake = FakeCrux()
    stamps = []
    fake.on_request = lambda fk, req: stamps.append(fk.clock.t)
    c = client(fake, max_calls=120, deadline_s=300)
    for _ in range(3):                                                  # three runs back to back share one bucket
        lena.run(c.open_read(ok_guard), O, ["/a", "/b", "/c", "/d", "/e"], 25, "2026-10-09", "x", twin=W)
    assert len(stamps) > 100 and _window_max(stamps) <= 100
    assert all(s > 0 for s in fake.clock.sleeps)


def test_pacing_that_would_pass_the_deadline_stops_without_sending():
    fake = FakeCrux()
    c = client(fake, max_calls=120, deadline_s=5)
    for _ in range(cx.PACE_BURST + 10):                                 # 10 already queued: ~7 s to wait
        c._pacer.take()
    e = err(lambda: c.open_read(ok_guard).record("origin", O, "PHONE"))
    assert (e.code, e.detail) == ("CRUX_RATE_LIMITED", "client_pacing") and fake.requests == []


def _chain(e):
    out, seen = [], set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        out.append(e)
        e = e.__cause__ or e.__context__
    return out


@pytest.mark.parametrize("boom", ["timeout", "other"])
def test_wrapped_errors_carry_no_request_and_no_key(boom):
    fake = FakeCrux()

    def handler(request):
        if boom == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        raise RuntimeError(request)                                     # an exception holding the request object
    c = cx.CruxClient(cx.ApiKey(TEST_KEY), transport=httpx.MockTransport(handler), monotonic=fake.clock.mono,
                      sleep=fake.clock.sleep, max_calls=10)
    e = err(lambda: c.open_read(ok_guard).record("origin", O, "PHONE"))
    chain = _chain(e)
    assert chain == [e] and e.__cause__ is None and e.__context__ is None and e.__suppress_context__
    assert TEST_KEY not in repr(e.__dict__) and not any(isinstance(v, httpx.Request) for v in vars(e).values())
    fake.script = [(200, b"not json")]
    je = err(lambda: rd(fake).record("origin", O, "PHONE"))
    assert je.__context__ is None and je.__cause__ is None


# ====================================================================== round 2: sizing, twin rule, traceback locals

@pytest.mark.parametrize("n", range(0, 9))
def test_calls_needed_is_every_query_of_a_clean_run(n):
    paths = [f"/p{i}" for i in range(n)]
    need = lena.calls_needed(n, twin=True)
    assert need == 12 + 6 * n and lena.calls_needed(n, twin=False) == need - 6
    sec = run(FakeCrux(), paths=paths, twin=W, max_calls=need)
    assert sec["stopped"] is None and sec["counts"]["not_queried"] == 0 and sec["calls"] == need


def _retry_script(fake, failures: int):
    """``failures`` retryable 503s, at most R per query (a query with R failures still gets its last attempt): the
    first query uses both retries, then the next, and so on."""
    st = {"spent": 0, "query": None, "here": 0}

    def on(fk, req):
        if req.content != st["query"]:
            st["query"], st["here"] = req.content, 0
        if st["spent"] < failures and st["here"] < lena.RETRIES_PER_QUERY:
            st["spent"] += 1
            st["here"] += 1
            fk.script.insert(0, (503, {}))
    fake.on_request = on


@pytest.mark.parametrize("n", [0, 3, 8])
@pytest.mark.parametrize("failures", [1, 2, 5, 10])
def test_retryable_errors_at_the_recommended_cap_still_send_every_query(n, failures):
    paths = [f"/p{i}" for i in range(n)]
    cap = lena.recommended_cap(n, twin=True)
    assert cap == (12 + 6 * n) + lena.RETRIES_PER_QUERY * lena.RETRIED_QUERIES_TOLERATED == 12 + 6 * n + 10
    assert lena.RETRIES_PER_QUERY == cx.DEFAULT_ATTEMPTS - 1 == 2 and cap <= 120
    fake = FakeCrux()
    _retry_script(fake, failures)                    # 10 failures = 5 queries each using both retries (R x K)
    sec = run(fake, paths=paths, twin=W, max_calls=cap, deadline_s=120)   # the recommended deadline
    assert sec["stopped"] is None and sec["counts"]["not_queried"] == 0 and sec["counts"]["failed"] == 0
    assert sec["counts"]["with_data"] == lena.calls_needed(n) and sec["calls"] == lena.calls_needed(n) + failures


@pytest.mark.parametrize("n", [0, 3, 8])
def test_one_call_short_the_skipped_query_is_not_queried_never_failed(n):
    paths = [f"/p{i}" for i in range(n)]
    cap = lena.recommended_cap(n, twin=True)
    fake = FakeCrux()
    _retry_script(fake, 11)                          # one retry more than the headroom
    sec = run(fake, paths=paths, twin=W, max_calls=cap, deadline_s=120)   # the recommended deadline
    assert sec["calls"] == cap and sec["counts"]["failed"] == 0 and sec["counts"]["not_queried"] == 1
    last = sec["targets"][-1]["form_factors"]["TABLET"]["history"]
    assert last["state"] == "NOT_QUERIED" and last["reason"] == "CRUX_BUDGET_EXHAUSTED"
    fake = FakeCrux()
    sec = run(fake, paths=paths, twin=W, max_calls=lena.calls_needed(n) - 1)      # no errors, one call short
    last = sec["targets"][-1]["form_factors"]["TABLET"]["history"]
    assert last == {"state": "NOT_QUERIED", "reason": "CRUX_BUDGET_EXHAUSTED", "attempts_sent": 0}
    assert sec["counts"]["failed"] == 0 and lena.outcome(sec) == ("PARTIAL", "CRUX_BUDGET_EXHAUSTED")


def test_a_query_cut_off_mid_retries_by_the_cap_is_not_queried_with_its_attempts():
    fake = FakeCrux()
    fake.script = [(503, {})]
    sec = run(fake, paths=(), max_calls=1)
    first = sec["targets"][0]["form_factors"]["PHONE"]["current"]
    assert first == {"state": "NOT_QUERIED", "reason": "CRUX_BUDGET_EXHAUSTED", "attempts_sent": 1}


@pytest.mark.parametrize("domain,twin", [
    ("acme.test", "www.acme.test"), ("www.acme.test", "acme.test"),
    ("example.co.uk", "www.example.co.uk"), ("www.example.co.uk", "example.co.uk"),
    ("foo.github.io", "www.foo.github.io"),
    ("shop.example.com", None), ("www.shop.example.com", None), ("a.b.example.co.uk", None),
    ("www.co.uk", None), ("co.uk", None), ("www.com", None), ("com", None), ("github.io", None),
    ("www.github.io", None), ("Example.com", None), ("", None),
])
def test_twins_only_for_a_registrable_domain_or_its_www_form(domain, twin):
    assert lena.twin_of(domain) == twin


def _holds_key(v, depth=0) -> bool:
    if depth > 3:
        return False
    if isinstance(v, str):
        return TEST_KEY in v
    if isinstance(v, (bytes, bytearray)):
        return TEST_KEY.encode() in v
    if isinstance(v, (httpx.Request, httpx.Response)):
        return TEST_KEY in str(v.headers) or (isinstance(v, httpx.Response) and _holds_key(v.request, depth + 1))
    if isinstance(v, httpx.Headers):
        return TEST_KEY in str(v)
    if isinstance(v, dict):
        return any(_holds_key(k, depth + 1) or _holds_key(x, depth + 1) for k, x in v.items())
    if isinstance(v, (list, tuple, set)):
        return any(_holds_key(x, depth + 1) for x in v)
    return False


@pytest.mark.parametrize("boom", ["timeout", "other", "status", "gzip", "kill"])
def test_no_traceback_frame_local_holds_the_key_after_a_forced_error(boom):
    fake = FakeCrux()

    def handler(request):
        if boom == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if boom == "other":
            raise RuntimeError(request)
        if boom == "gzip":
            return httpx.Response(200, content=b"\x1f\x8b", headers={"content-encoding": "gzip"})
        return httpx.Response(503 if boom == "status" else 200, content=b"{}")
    calls = {"n": 0}

    def guard():
        calls["n"] += 1
        if boom == "kill" and calls["n"] > 1:
            raise Killed("KILLED_PROVIDER")
    c = cx.CruxClient(cx.ApiKey(TEST_KEY), transport=httpx.MockTransport(handler), monotonic=fake.clock.mono,
                      sleep=fake.clock.sleep, max_calls=10)
    with pytest.raises(cx.CruxError) as ei:
        c.open_read(guard).record("origin", O, "PHONE")
    e = ei.value
    tb, frames = e.__traceback__, 0
    while tb is not None:
        frames += 1
        for name, v in tb.tb_frame.f_locals.items():
            assert not _holds_key(v), (boom, tb.tb_frame.f_code.co_name, name)
        tb = tb.tb_next
    assert frames >= 1 and e.__context__ is None and e.__cause__ is None


def test_recommended_cap_against_the_config_ceiling():
    assert lena.recommended_cap(10) == 82 and lena.recommended_cap(16) == 118 <= 120 < lena.recommended_cap(17)
