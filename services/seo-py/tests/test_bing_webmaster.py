"""Bing Webmaster Tools adapter (ADR 0047): strict parsing of GetQueryStats' documented JSON shape, Bing's error
answers, retries and the request budget, the host allowlist, the key never leaking, the kill-switch guard, and the
fail-closed defaults (no transport = no call; the key setting stays NOT_BUILT). No network: a fake Bing behind
httpx.MockTransport; time is an injected clock."""

from __future__ import annotations

import copy
import json
import pickle

import httpx
import pytest
from bing_fakes import KEY_TEXT, FakeBing, key, row
from helpers import base_env

import bing_webmaster as bw
import config as config_mod
from ports import Ports
from primitives import Killed

SITE = "https://acme.test/"


def ok():
    return None


def code_of(fn) -> bw.BingError:
    with pytest.raises(bw.BingError) as ei:
        fn()
    return ei.value


def test_query_stats_reads_the_documented_shape_and_request():
    fake = FakeBing()
    rows, stats = fake.client().query_stats(SITE, ok)
    assert stats == {"requests": 1} and len(rows) == 3
    q, d, c, i, cpos, ipos = rows[0]
    assert (q, c, i, cpos, ipos) == ("rug cleaner", 4, 60, 7.0, 7.0)
    assert d == bw.parse_date(row("x", 0, 0)["Date"])
    r = fake.requests[0]
    assert (r["host"], r["path"], r["method"]) == ("ssl.bing.com", "/webmaster/api.svc/json/GetQueryStats", "GET")
    assert r["query"] == {"siteUrl": SITE, "apikey": KEY_TEXT}


def test_dates_are_parsed_in_the_offset_bing_gives():
    assert bw.parse_date("/Date(1316156400000-0700)/") == "2011-09-16"        # Microsoft's own sample
    assert bw.parse_date("/Date(1316156400000)/") == "2011-09-16"             # no offset: UTC
    for bad in ("2011-09-16", "/Date(abc)/", "/Date(1316156400000-2500)/", "/Date(99999999999999)/", 5, None,
                "/Date(0)/"):
        assert code_of(lambda: bw.parse_date(bad)).code == "BING_PROTOCOL_ERROR"


@pytest.mark.parametrize("answer", [
    [],                                                       # not {"d": ...}
    {"d": [], "extra": 1},
    {"d": {"Query": "x"}},
    {"d": [{"Query": "x", "Clicks": 1, "Impressions": 2}]},   # no Date
    {"d": [{**row("x", 1, 2), **{f"Extra{n}": n for n in range(bw.ROW_MAX_FIELDS)}}]},   # unbounded row
    {"d": [{**row("x", 1, 2), "AvgClickPosition": "high", "Surprise": 1}]},   # a documented field still checked
    {"d": [{**row("x", 1, 2), "__type": "KeywordStats:#X"}]},
    {"d": [row("x", 3, 2)]},                                   # clicks > impressions
    {"d": [row("x", -1, 2)]},
    {"d": [row("x", 1.5, 2)]},
    {"d": [{**row("x", 1, 2), "Impressions": 10 ** 400}]},     # a 400-digit integer: refused before any float
    {"d": [{**row("x", 1, 2), "Impressions": True}]},
    {"d": [{**row("x", 1, 2), "AvgImpressionPosition": -3}]},
    {"d": [{**row("x", 1, 2), "Query": ""}]},
    {"d": [{**row("x", 1, 2), "Query": 7}]},
    {"d": [row("x", 1, 2), row("x", 1, 2)]},                   # one (query, date) twice
])
def test_malformed_answers_are_protocol_errors(answer):
    fake = FakeBing()
    fake.script = [(200, answer)]
    e = code_of(lambda: fake.client().query_stats(SITE, ok))
    assert e.code == "BING_PROTOCOL_ERROR"


def test_unknown_extra_fields_are_ignored_and_required_fields_still_validated():
    fake = FakeBing()
    fake.script = [(200, {"d": [{**row("rug cleaner", 4, 60), "Surprise": {"nested": [1]}, "Country": "us"}]})]
    rows, _ = fake.client().query_stats(SITE, ok)
    assert len(rows) == 1 and rows[0][:4] == ("rug cleaner", rows[0][1], 4, 60) and len(rows[0]) == 6
    for missing in ("Query", "Date", "Clicks", "Impressions"):
        r = {**row("x", 1, 2), "Surprise": 1}
        del r[missing]
        fake = FakeBing()
        fake.script = [(200, {"d": [r]})]
        assert code_of(lambda: fake.client().query_stats(SITE, ok)).detail == "row_shape"


def test_nan_and_non_json_and_gzip_and_oversized_bodies_are_refused():
    for payload, headers in ((b'{"d": [{"Clicks": NaN}]}', {}), (b"<html>", {}), (b"\x1f\x8b", {"content-encoding": "gzip"}),
                             (b"{}", {"content-length": str(bw.MAX_BODY + 1)})):
        fake = FakeBing()
        fake.script = [(200, payload, headers)]
        assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_PROTOCOL_ERROR"


def test_invalid_key_and_other_documented_error_bodies():
    fake = FakeBing()
    fake.script = [(400, {"ErrorCode": 3, "Message": "InvalidApiKey"})]
    assert (lambda e: (e.code, e.detail))(code_of(lambda: fake.client().query_stats(SITE, ok))) == \
        ("BING_AUTH_REFUSED", "InvalidApiKey")
    fake = FakeBing()
    e = code_of(lambda: fake.client().query_stats("https://notmine.test/", ok))     # the fake's "not authorized"
    assert (e.code, e.detail) == ("BING_REFUSED", "error_code_14")
    for body in ({"ErrorCode": "3", "Message": "x"}, {"Message": 3}, {"ErrorCode": 1, "Message": "x", "More": 1},
                 [1]):
        fake = FakeBing()
        fake.script = [(400, body)]
        assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_PROTOCOL_ERROR"
    for status in (401, 403):
        fake = FakeBing()
        fake.script = [(status, {})]
        assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_AUTH_REFUSED"
    fake = FakeBing()
    fake.script = [(302, b"", {"location": "https://evil.test/"})]
    assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_PROTOCOL_ERROR"
    assert len(fake.requests) == 1                                               # never followed


def test_429_and_5xx_retry_with_backoff_then_give_up_or_recover():
    fake = FakeBing()
    fake.script = [(429, {}), (503, {})]
    rows, stats = fake.client().query_stats(SITE, ok)
    assert stats["requests"] == 3 and fake.clock.sleeps == [1.0, 3.0] and rows
    fake = FakeBing()
    fake.script = [(500, {})] * 3
    assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_UNAVAILABLE"
    fake = FakeBing()
    fake.script = [(429, {})] * 3
    assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_RATE_LIMITED"


def test_network_errors_are_retried_as_unavailable():
    def boom(request):
        raise httpx.ConnectError("refused")
    fake = FakeBing()
    fake.script = [boom, boom, boom]
    assert code_of(lambda: fake.client().query_stats(SITE, ok)).code == "BING_UNAVAILABLE"


def test_request_budget_and_deadline():
    fake = FakeBing()
    fake.script = [(503, {})] * 5
    e = code_of(lambda: fake.client(attempts=5, max_requests=2).query_stats(SITE, ok))
    assert e.code == "BING_BUDGET_EXHAUSTED" and len(fake.requests) == 2
    fake = FakeBing()
    fake.script = [(503, {})] * 3
    e = code_of(lambda: fake.client(deadline_s=2.0).query_stats(SITE, ok))      # the 3 s backoff would pass it
    assert e.code == "BING_UNAVAILABLE" and len(fake.requests) == 2


def test_kill_switch_guard_stops_before_the_request():
    def killed():
        raise Killed("KILLED_PROVIDER")
    fake = FakeBing()
    e = code_of(lambda: fake.client().query_stats(SITE, killed))
    assert (e.code, e.detail) == ("BING_KILLED", "KILLED_PROVIDER") and fake.requests == []


def test_no_transport_means_no_call_and_the_site_url_is_checked():
    c = bw.BingWebmasterClient(key())
    assert code_of(lambda: c.query_stats(SITE, ok)).detail == "no_deadline_backend"
    fake = FakeBing()
    for bad in ("acme.test", "https://acme.test/path", "ftp://acme.test/", "https://ACME.test/"):
        assert code_of(lambda: fake.client().query_stats(bad, ok)).code == "BING_PROTOCOL_ERROR"
    assert fake.requests == []
    assert code_of(lambda: fake.client().keyword_stats("rug", "us", "en-US", ok)).detail == "not_built"
    assert fake.requests == []


def test_host_allowlist():
    for url in ("http://ssl.bing.com/webmaster/api.svc/json/GetQueryStats", "https://evil.test/webmaster/api.svc/json/x",
                "https://ssl.bing.com:8443/webmaster/api.svc/json/x", "https://u:p@ssl.bing.com/webmaster/api.svc/json/x",
                "https://ssl.bing.com/webmaster/api.svc/soap", "https://ssl.bing.com/webmaster/api.svc/json/x#f"):
        assert code_of(lambda: bw.check_url(url)).code == "BING_PROTOCOL_ERROR"
    bw.check_url("https://ssl.bing.com/webmaster/api.svc/json/GetQueryStats?siteUrl=x")


def test_the_key_never_leaks():
    k = key()
    assert KEY_TEXT not in repr(k) and KEY_TEXT not in str(k)
    for fn in (lambda: pickle.dumps(k), lambda: copy.copy(k), lambda: copy.deepcopy(k)):
        with pytest.raises(TypeError):
            fn()
    fake = FakeBing()
    c = fake.client()
    assert KEY_TEXT not in repr(c)

    def explode(request):
        raise RuntimeError(f"boom {request.url}")                 # an exception whose text carries the URL (the key)
    fake.script = [explode]
    e = code_of(lambda: c.query_stats(SITE, ok))
    assert KEY_TEXT not in str(e) and KEY_TEXT not in repr(e) and e.detail == "exchange"
    for bad in ("short", "has space " + "x" * 20, "x" * 200, 5):
        with pytest.raises(ValueError):
            bw.ApiKey(bad)


def test_the_key_setting_stays_not_built_and_the_port_not_connected():
    assert "SEO_BING_WEBMASTER_KEY_FILE" in config_mod.NOT_BUILT
    with pytest.raises(RuntimeError, match="SEO_BING_WEBMASTER_KEY_FILE"):
        config_mod.load(base_env(SEO_BING_WEBMASTER_KEY_FILE="/tmp/x"))
    p = Ports.default(config_mod.load(base_env()))
    assert p.first_party["bing_webmaster"].connected is False
    assert json.dumps(p.status()["first_party"]["bing_webmaster"]) == '"NOT_CONNECTED"'
    assert bw.NotConnectedBing().connected is False
    assert code_of(lambda: bw.NotConnectedBing().query_stats(SITE, ok)).code == "BING_NOT_CONNECTED"
