"""Google Search Console connector (ADR 0017, "Search Console (first-party truth)"): the JWT flow, the token cache,
Google's error answers, pagination and totals, strict parsing, the hard deadline, the host allowlist, the private key
never leaking, and the service rules (NOT_CONNECTED by default, Andre's binding, cross-tenant refusal, PII queries,
kill switch, budget, record-first, retention, Osei's drift). No network: a fake Google behind httpx.MockTransport or a
fake socket layer under the production deadline backend; time is an injected clock."""

from __future__ import annotations

import json
import os

import httpcore
import pytest
from gsc_fakes import (
    SA_EMAIL,
    SA_KEY_ID,
    Clock,
    Drip,
    Endless,
    FakeGoogle,
    FakeNetwork,
    b64url_decode,
    http_response,
    key_json,
    pem_fragments,
    rsa_key,
    write_key_file,
)
from helpers import Harness, base_env, rid

import config as config_mod
import gsc_client as g
from agents import drift
from agents import search_performance as sp
from ports import Ports, gsc_from_settings
from primitives import Killed

PROP = "sc-domain:acme.test"
W = ("2026-09-09", "2026-10-06")
GLOBAL_IP = "142.250.80.42"


def key():
    return g.parse_service_account_key(json.dumps(key_json()).encode())


def client(fake: FakeGoogle, **kw) -> g.GscClient:
    c = fake.clock
    return g.GscClient(key(), transport=fake.transport(), monotonic=c.mono, wall=c.wall, sleep=c.sleep, **kw)


def ok_guard():
    return None


def read(fake: FakeGoogle, guard=ok_guard, **kw) -> dict:
    rd = client(fake, **kw).open_read(guard)
    return sp.read_aggregates(rd, PROP, *W, {"acme.test", "www.acme.test"}, ["acme"])


def code_of(fn) -> g.GscError:
    with pytest.raises(g.GscError) as ei:
        fn()
    return ei.value


# ====================================================================== JWT and token

def test_assertion_is_rs256_with_googles_claims_and_verifies():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    jwt = g.build_assertion(key(), 1_791_000_000).decode()
    h, c, s = jwt.split(".")
    assert json.loads(b64url_decode(h)) == {"alg": "RS256", "typ": "JWT", "kid": SA_KEY_ID}
    claims = json.loads(b64url_decode(c))
    assert claims == {"iss": SA_EMAIL, "scope": "https://www.googleapis.com/auth/webmasters.readonly",
                      "aud": "https://oauth2.googleapis.com/token", "iat": 1_791_000_000, "exp": 1_791_003_600}
    rsa_key().public_key().verify(b64url_decode(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert "=" not in jwt and "\n" not in jwt


def test_token_request_is_the_jwt_bearer_grant_to_googles_token_url():
    fake = FakeGoogle()
    read(fake)
    tok = fake.requests[0]
    assert (tok["method"], tok["host"], tok["path"]) == ("POST", "oauth2.googleapis.com", "/token")
    assert tok["headers"]["content-type"] == "application/x-www-form-urlencoded"
    form = fake.form()
    assert set(form) == {"grant_type", "assertion"}
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = json.loads(b64url_decode(form["assertion"].split(".")[1]))
    assert claims["iat"] == int(fake.clock.wall()) and claims["exp"] - claims["iat"] == 3600
    assert {r["host"] for r in fake.requests} == {"oauth2.googleapis.com", "www.googleapis.com"}
    for r in fake.api_requests():
        assert r["headers"]["authorization"] == f"Bearer {fake.tokens[0]}"
        assert r["headers"]["accept-encoding"] == "identity"


def test_token_is_cached_until_shortly_before_expiry_then_refreshed():
    fake = FakeGoogle()
    c = client(fake)
    sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, [])
    sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, [])
    assert len(fake.tokens) == 1                                    # cached across reads
    fake.clock.advance(3600 - 300 - 1)
    sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, [])
    assert len(fake.tokens) == 1
    fake.clock.advance(2)                                           # past expires_in minus the 300 s margin
    sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, [])
    assert len(fake.tokens) == 2


def test_a_401_with_a_cached_token_refreshes_once_and_a_second_401_is_auth_refused():
    fake = FakeGoogle()
    c = client(fake)
    sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, [])
    fake.tokens.append("revoked-elsewhere-" + "r" * 20)              # Google no longer accepts the cached token
    sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, [])
    assert len(fake.tokens) == 3                                    # one fresh token, then success
    fake.script = [(401, {"error": {"code": 401, "message": "x", "errors": [{"reason": "authError"}]}})] * 2
    e = code_of(lambda: sp.read_aggregates(c.open_read(ok_guard), PROP, *W, {"acme.test"}, []))
    assert e.code == "GSC_AUTH_REFUSED"


@pytest.mark.parametrize("status,err,detail", [(400, "invalid_grant", "invalid_grant"),
                                               (401, "invalid_client", "invalid_client"),
                                               (400, "something_new", "token_refused")])
def test_token_endpoint_refusal_is_auth_refused_and_never_echoes_googles_text(status, err, detail):
    fake = FakeGoogle()
    fake.token_script = [(status, {"error": err, "error_description": "SECRET-ECHO Invalid JWT Signature"})]
    e = code_of(lambda: read(fake))
    assert (e.code, e.detail) == ("GSC_AUTH_REFUSED", detail) and "SECRET-ECHO" not in repr(e) + str(e)
    assert len(fake.api_requests()) == 0


@pytest.mark.parametrize("payload", [{"access_token": "short", "expires_in": 3600, "token_type": "Bearer"},
                                     {"access_token": "a" * 30, "expires_in": True, "token_type": "Bearer"},
                                     {"access_token": "a" * 30, "expires_in": 3600, "token_type": "MAC"},
                                     {"access_token": "a" * 30, "expires_in": 3600, "token_type": "Bearer", "x": 1},
                                     {"access_token": "a" * 30, "expires_in": 5, "token_type": "Bearer"},
                                     {"access_token": "a" * 30, "expires_in": 3600, "token_type": "Bearer",
                                      "scope": "https://www.googleapis.com/auth/other"}, [1, 2]])
def test_malformed_token_answers_are_protocol_errors(payload):
    fake = FakeGoogle()
    fake.token_script = [(200, payload)]
    assert code_of(lambda: read(fake)).code == "GSC_PROTOCOL_ERROR"


# ====================================================================== Google's errors

def _err(status, reason):
    return (status, {"error": {"code": status, "message": "m", "errors": [{"domain": "global", "reason": reason}]}})


def test_property_not_granted_three_ways():
    fake = FakeGoogle()
    fake.sites = [{"siteUrl": "sc-domain:other.test", "permissionLevel": "siteOwner"}]
    assert code_of(lambda: read(fake)).code == "GSC_PROPERTY_NOT_GRANTED"           # not in the site list
    fake = FakeGoogle(level="siteUnverifiedUser")
    e = code_of(lambda: read(fake))
    assert (e.code, e.detail) == ("GSC_PROPERTY_NOT_GRANTED", "unverified")
    fake = FakeGoogle()
    seq = []

    def on(fk, req):
        if req.url.raw_path.decode().endswith("/searchAnalytics/query") and not seq:
            seq.append(1)
            fk.script.append(_err(403, "insufficientPermissions"))
    fake.on_request = on
    e = code_of(lambda: read(fake))
    assert (e.code, e.detail) == ("GSC_PROPERTY_NOT_GRANTED", "insufficientPermissions")


@pytest.mark.parametrize("reason,code,attempts", [("accessNotConfigured", "GSC_AUTH_REFUSED", 1),
                                                  ("quotaExceeded", "GSC_RATE_LIMITED", 1),
                                                  ("rateLimitExceeded", "GSC_RATE_LIMITED", 3),
                                                  ("userRateLimitExceeded", "GSC_RATE_LIMITED", 3)])
def test_403_reasons_map_and_only_per_minute_limits_are_retried(reason, code, attempts):
    fake = FakeGoogle()
    fake.script = [_err(403, reason)] * 3
    e = code_of(lambda: read(fake))
    assert e.code == code and len(fake.api_requests()) == attempts


def test_429_backs_off_within_bounds_then_rate_limited_and_recovers_when_google_does():
    fake = FakeGoogle()
    fake.script = [(429, {"error": {"code": 429}})] * 3
    e = code_of(lambda: read(fake))
    assert e.code == "GSC_RATE_LIMITED" and fake.clock.sleeps == [1.0, 3.0] and len(fake.api_requests()) == 3
    fake = FakeGoogle()
    fake.script = [(429, {"error": {"code": 429}}, {"Retry-After": "7"})]
    out = read(fake)
    assert fake.clock.sleeps == [7.0] and out["totals"]["clicks"] == 5 * 28
    fake = FakeGoogle()
    fake.script = [(429, {}, {"Retry-After": "99999"})] * 3                        # capped at 60 s, then deadline
    assert code_of(lambda: read(fake)).code == "GSC_RATE_LIMITED" and max(fake.clock.sleeps) <= 60


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_retried_then_unavailable(status):
    fake = FakeGoogle()
    fake.script = [(status, {"error": {"code": status}})] * 3
    e = code_of(lambda: read(fake))
    assert e.code == "GSC_UNAVAILABLE" and len(fake.api_requests()) == 3


def test_retries_stop_at_the_deadline():
    fake = FakeGoogle()
    fake.script = [(503, {})] * 5
    e = code_of(lambda: read(fake, deadline_s=2.5, attempts=5))
    assert e.code == "GSC_UNAVAILABLE" and sum(fake.clock.sleeps) < 2.5 and len(fake.api_requests()) <= 2


@pytest.mark.parametrize("status", [301, 302, 307, 400, 405, 410])
def test_other_statuses_and_redirects_are_protocol_errors_never_followed(status):
    fake = FakeGoogle()
    fake.script = [(status, {}, {"Location": "https://evil.test/"})]
    e = code_of(lambda: read(fake))
    assert e.code == "GSC_PROTOCOL_ERROR" and all(r["host"] != "evil.test" for r in fake.requests)


def test_request_budget_per_read():
    fake = FakeGoogle()
    fake.script = [(503, {})] * 10
    e = code_of(lambda: read(fake, max_requests=3, attempts=5, backoff_s=(0.0,)))
    assert e.code == "GSC_BUDGET_EXHAUSTED" and len(fake.requests) == 3


# ====================================================================== pagination and totals

def test_pagination_pages_by_startrow_until_a_short_page():
    fake = FakeGoogle()
    fake.queries = [(f"rug {i}", 1, 10, 3.0) for i in range(7)]
    out = read(fake, page_rows=3)
    q = [json.loads(r["body"]) for r in fake.api_requests() if r["body"] and json.loads(r["body"])["dimensions"]
         == ["query"]]
    assert [(x["startRow"], x["rowLimit"]) for x in q] == [(0, 3), (3, 3), (6, 3)]
    assert out["by_query_class"]["rows_read"] == 7 and out["by_query_class"]["truncated"] is False
    assert out["by_query_class"]["classes"]["other"]["clicks"] == 7


def test_an_exact_multiple_ends_on_an_empty_page():
    fake = FakeGoogle()
    fake.queries = [(f"rug {i}", 1, 10, 3.0) for i in range(6)]
    out = read(fake, page_rows=3)
    assert out["by_query_class"]["pages_read"] == 3 and out["by_query_class"]["rows_read"] == 6
    assert out["by_query_class"]["truncated"] is False


def test_page_and_row_caps_truncate_and_say_so_while_totals_stay_the_propertys():
    fake = FakeGoogle()
    fake.queries = [(f"rug {i}", 1, 10, 3.0) for i in range(20)]
    out = read(fake, page_rows=3, max_pages=2)
    bq = out["by_query_class"]
    assert bq["truncated"] is True and bq["rows_read"] == 6 and bq["pages_read"] == 2
    assert out["totals"]["clicks"] == 140                                      # 28 days x 5: from the date query
    assert bq["unattributed"]["clicks"] == 140 - 6
    out = read(fake, page_rows=100, max_rows=5)
    assert out["by_query_class"]["rows_read"] == 5 and out["by_query_class"]["truncated"] is True


def test_totals_ctr_position_and_edge_cases():
    fake = FakeGoogle()
    out = read(fake)
    t = out["totals"]
    assert (t["clicks"], t["impressions"]) == (140, 1400) and t["ctr"] == 0.1 and t["position"] == 7.5
    assert out["window"] == {"start": W[0], "end": W[1], "days": 28, "timezone": "America/Los_Angeles",
                             "data_state": "final", "search_type": "web", "lag_days": 3}
    assert out["dates_without_data"] == 0 and out["headline_score"] is None
    assert out["evidence"]["evidence_class"] == "OBSERVED" and out["evidence"]["source_class"] == "first_party"
    assert out["pairing"]["decision"] == "INSUFFICIENT_EVIDENCE"
    fake = FakeGoogle()
    fake.dates_clicks = (0, 0)                                                   # no impressions at all
    fake.queries, fake.pages = [], []
    out = read(fake)
    assert out["totals"]["ctr"] is None and out["totals"]["position"] is None
    assert out["by_query_class"]["unattributed"] == {"clicks": 0, "impressions": 0,
                                                     "why": out["by_query_class"]["unattributed"]["why"]}
    fake = FakeGoogle()
    fake.queries = [("big", 999, 9999, 1.0)]                                     # rows larger than the totals
    out = read(fake)
    assert out["by_query_class"]["rows_exceed_totals"] is True


def test_missing_dates_are_counted_not_invented():
    fake = FakeGoogle()
    real = fake.query

    def sparse(q):
        r = real(q)
        if q["dimensions"] == ["date"]:
            r["rows"] = r["rows"][::2]
        return r
    fake.query = sparse
    out = read(fake)
    assert out["dates_without_data"] == 14 and len(out["by_date"]) == 14


def test_page_templates_redact_identifiers_and_offsite():
    fake = FakeGoogle()
    fake.pages += [("https://acme.test/blog/post-3", 1, 5, 2.0)]
    out = read(fake)
    ts = {t["template"] for t in out["by_page_template"]["templates"]}
    # three blog slugs share /blog/{slug}; the one e-mail page is a template of one URL and goes to {other}
    assert ts == {"/blog/{slug}", "{offsite}", "{other}"}
    assert out["by_page_template"]["comparable_with_totals"] is False


# ====================================================================== strict parsing, bodies, time

BAD_QUERY_ANSWERS = [
    b"not json", b"[]", {"rows": [], "surprise": 1}, {"rows": "x"},
    {"rows": [{"keys": ["a", "b"], "clicks": 1, "impressions": 1}]},
    {"rows": [{"keys": ["a"], "clicks": 2, "impressions": 1}]},
    {"rows": [{"keys": ["a"], "clicks": -1, "impressions": 1}]},
    {"rows": [{"keys": ["a"], "clicks": 1.5, "impressions": 3}]},
    {"rows": [{"keys": ["a"], "clicks": True, "impressions": 3}]},
    {"rows": [{"keys": ["a"], "clicks": 1, "impressions": 3, "ctr": 1.5}]},
    {"rows": [{"keys": ["a"], "clicks": 1, "impressions": 3, "position": -2}]},
    {"rows": [{"keys": ["a"], "clicks": 1, "impressions": 3}, {"keys": ["a"], "clicks": 1, "impressions": 3}]},
    {"rows": [{"keys": [7], "clicks": 1, "impressions": 3}]},
    {"rows": [{"keys": ["a"], "impressions": 3}]},
    {"rows": [], "responseAggregationType": "byPage"},
    {"rows": [], "metadata": {"first_incomplete_date": "2026-01-01", "x": "y"}},
    b'{"rows": [{"keys": ["a"], "clicks": NaN, "impressions": 3}]}',
]


@pytest.mark.parametrize("answer", BAD_QUERY_ANSWERS)
def test_malformed_or_unknown_query_answers_are_protocol_errors(answer):
    fake = FakeGoogle()

    def on(fk, req):
        if req.url.raw_path.decode().endswith("/searchAnalytics/query") and b'"query"' in req.content:
            fk.script.append((200, answer))
    fake.on_request = on
    assert code_of(lambda: read(fake)).code == "GSC_PROTOCOL_ERROR"


def test_date_rows_outside_the_window_or_too_many_are_refused():
    fake = FakeGoogle()
    fake.script = []

    def on(fk, req):
        if b'"date"' in req.content:
            fk.script.append((200, {"rows": [{"keys": ["2020-01-01"], "clicks": 1, "impressions": 2}]}))
    fake.on_request = on
    assert code_of(lambda: read(fake)).code == "GSC_PROTOCOL_ERROR"


def test_sites_answers_are_strict():
    for bad in ({"siteEntry": [{"siteUrl": PROP, "permissionLevel": "siteGod"}]}, {"siteEntry": {}}, {"x": []},
                {"siteEntry": [{"siteUrl": PROP, "permissionLevel": "siteOwner", "extra": 1}]}):
        fake = FakeGoogle()
        fake.script = [(200, bad)]
        assert code_of(lambda fk=fake: read(fk)).code == "GSC_PROTOCOL_ERROR"


def test_a_gzip_answer_and_a_huge_body_are_refused_without_reading_it_all():
    fake = FakeGoogle()
    fake.script = [(200, b"\x1f\x8b", {"Content-Encoding": "gzip"})]
    assert code_of(lambda: read(fake)).code == "GSC_PROTOCOL_ERROR"
    fake = FakeGoogle()
    fake.script = [(200, b"x", {"Content-Length": str(64 * 1024 * 1024)})]
    assert code_of(lambda: read(fake)).code == "GSC_PROTOCOL_ERROR"
    fake = FakeGoogle()
    endless = Endless()
    fake.script = [(200, endless)]
    assert code_of(lambda: read(fake)).code == "GSC_PROTOCOL_ERROR"
    assert endless.sent <= g.MAX_SITES_BODY // 65536 + 2                         # stopped at the sites cap


def test_a_slow_drip_body_stops_at_the_deadline_on_the_injected_clock():
    fake = FakeGoogle()
    drip = Drip(json.dumps({"siteEntry": [{"siteUrl": PROP, "permissionLevel": "siteOwner"}] * 50}).encode(),
                fake.clock, step_s=0.4)
    fake.script = [(200, drip)]
    e = code_of(lambda: read(fake, deadline_s=5, attempts=1))
    assert e.code == "GSC_UNAVAILABLE" and fake.clock.t - 1000.0 < 5.5 and drip.sent < 20


def _prod_client(clock, net, resolve=lambda h, p: [GLOBAL_IP], **kw):
    from finance_client import _DnsGate

    class SyncThread:                       # the resolver's thread, run inline (deterministic, no wall clock)
        def __init__(self, target, name, daemon):
            self.t = target

        def start(self):
            self.t()
    c = g.GscClient(key(), network_backend=net, monotonic=clock.mono, wall=clock.wall, sleep=clock.sleep,
                    resolve=resolve, dns_gate=_DnsGate(2), **kw)
    c._resolver._inner._thread = SyncThread
    return c


def test_production_transport_bounds_tls_and_every_read_by_the_deadline():
    clock = Clock()
    tok = http_response(200, {"access_token": "x" * 30, "expires_in": 3600, "token_type": "Bearer"})
    net = FakeNetwork([tok], clock, step_s=0.4, chunk=1)              # 1 byte per read, 0.4 s each
    c = _prod_client(clock, net, deadline_s=3, attempts=1)
    e = code_of(lambda: c.open_read(ok_guard).token())
    assert e.code == "GSC_UNAVAILABLE"
    assert net.connects[0][0] == GLOBAL_IP and net.connects[0][1] == 443     # the resolved address, port 443
    assert ("tls", "oauth2.googleapis.com") in net.log                         # SNI is Google's name
    assert all(t is not None and t <= 3 for kind, t in net.log if kind == "read")
    assert clock.t - 1000.0 <= 3.5


def test_production_transport_completes_a_read_through_the_deadline_backend():
    clock = Clock()
    tok = http_response(200, {"access_token": "y" * 30, "expires_in": 3600, "token_type": "Bearer"})
    sites = http_response(200, {"siteEntry": [{"siteUrl": PROP, "permissionLevel": "siteOwner"}]})
    net = FakeNetwork([tok, sites], clock)
    rd = _prod_client(clock, net).open_read(ok_guard)
    assert rd.check_property(PROP) == "siteOwner" and len(net.connects) == 2


def test_dns_inside_the_deadline_and_non_global_answers_refused():
    clock = Clock()
    net = FakeNetwork([], clock)
    for answer in (["10.0.0.5"], ["127.0.0.1"], [GLOBAL_IP, "169.254.169.254"], []):
        c = _prod_client(clock, net, resolve=lambda h, p, a=answer: a, attempts=1)
        assert code_of(lambda cl=c: cl.open_read(ok_guard).token()).code == "GSC_UNAVAILABLE"
    assert net.connects == []

    def slow(h, p):
        clock.advance(10)
        return [GLOBAL_IP]
    c = _prod_client(clock, net, resolve=slow, deadline_s=5, attempts=1)
    assert code_of(lambda: c.open_read(ok_guard).token()).code == "GSC_UNAVAILABLE"


def test_host_allowlist():
    for url in ("https://evil.test/token", "http://oauth2.googleapis.com/token", "https://www.googleapis.com:8443/x",
                "https://user:pw@www.googleapis.com/x", "https://oauth2.googleapis.com.evil.test/token",
                "https://142.250.80.42/x"):
        assert code_of(lambda u=url: g.check_url(u)).detail in ("host_not_allowed", "url")
    assert g.check_url(g.TOKEN_URL) and g.check_url(g.API_BASE + "/sites")
    assert g.ALLOWED_HOSTS == {"oauth2.googleapis.com", "www.googleapis.com"}


def test_kill_switch_inside_a_read_stops_it_at_the_next_request():
    fake = FakeGoogle()
    fake.queries = [(f"rug {i}", 1, 10, 3.0) for i in range(20)]
    state = {"n": 0}

    def guard():
        state["n"] += 1
        if len(fake.api_requests()) >= 3:
            raise Killed("KILLED_PROVIDER")
    e = code_of(lambda: read(fake, guard=guard, page_rows=3))
    assert (e.code, e.detail) == ("GSC_KILLED", "KILLED_PROVIDER") and len(fake.api_requests()) == 3


# ====================================================================== the key never leaks

def test_key_file_rules_and_messages_never_quote_the_file(tmp_path):
    env = base_env()
    p = tmp_path / "k.json"
    write_key_file(p, mode=0o644)
    with pytest.raises(RuntimeError) as ei:
        config_mod.load({**env, "SEO_GSC_SERVICE_ACCOUNT_FILE": str(p)})
    assert "chmod 600" in str(ei.value)
    link = tmp_path / "link.json"
    os.chmod(p, 0o600)
    link.symlink_to(p)
    with pytest.raises(RuntimeError):
        config_mod.load({**env, "SEO_GSC_SERVICE_ACCOUNT_FILE": str(link)})
    for over in ({"type": "authorized_user"}, {"client_email": "someone@gmail.com"}, {"private_key_id": None},
                 {"token_uri": "https://evil.test/token"}, {"private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n"}):
        q = tmp_path / "bad.json"
        write_key_file(q, **over)
        with pytest.raises(RuntimeError) as ei:
            config_mod.load({**env, "SEO_GSC_SERVICE_ACCOUNT_FILE": str(q)})
        msg = str(ei.value) + repr(ei.value.__cause__) + repr(ei.value.__context__)
        assert not any(f in msg for f in pem_fragments()), over
        q.unlink()
    q = tmp_path / "garbage.json"
    q.write_text("{" + pem_fragments()[0])
    os.chmod(q, 0o600)
    with pytest.raises(RuntimeError) as ei:
        config_mod.load({**env, "SEO_GSC_SERVICE_ACCOUNT_FILE": str(q)})
    assert pem_fragments()[0] not in str(ei.value) and ei.value.__cause__ is None
    with pytest.raises(RuntimeError) as ei:
        config_mod.load({**env, "SEO_SEARCH_CONSOLE_CREDENTIALS_FILE": "/x"})
    assert "SEO_GSC_SERVICE_ACCOUNT_FILE" in str(ei.value)


def test_key_and_token_objects_redact_and_refuse_pickling(tmp_path):
    import copy
    import pickle
    k = key()
    s = config_mod.load(base_env(SEO_GSC_SERVICE_ACCOUNT_FILE=write_key_file(tmp_path / "k.json")))
    c = gsc_from_settings(s)
    text = repr(k) + str(k) + repr(s) + repr(c) + repr(g._Secret("abc-secret-token"))
    assert not any(f in text for f in pem_fragments()) and "abc-secret-token" not in text
    for obj in (k, g._Secret("t")):
        with pytest.raises(TypeError):
            pickle.dumps(obj)
        with pytest.raises(TypeError):
            copy.deepcopy(obj)
    assert not hasattr(k, "__dict__")


# ====================================================================== the service

def gsc_harness(tmp_path, fake: FakeGoogle, data_dir=None, **env):
    keyfile = write_key_file(tmp_path / "sa.json")
    settings = config_mod.load(base_env(SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile, **env))
    ports = Ports.default(settings)
    ports.fetcher = None                                       # no crawling here: the GSC section only
    c = fake.clock
    ports.first_party["search_console"] = gsc_from_settings(settings, transport=fake.transport(), monotonic=c.mono,
                                                            wall=c.wall, sleep=c.sleep)
    h = Harness(tmp_path, data_dir=data_dir, ports=ports, SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile, **env)
    return h


def bind(h, tid, prop, andre=True):
    return h.post(f"/tenants/{tid}/gsc-property", {"request_id": rid(), "property": prop}, andre=andre,
                  caller="dashboard")


def audit(h, tid, domain, invoice=None):
    body = {"request_id": rid(), "domain": domain, "paths": ["/"]}
    if invoice:
        body["invoice_id"] = invoice
    return h.ok(h.post(f"/tenants/{tid}/audits", body, andre=True), 201)


def section(view):
    return next(e for e in view["report"]["agents"] if e["agent"] == "osei" and e["task"] == "search_performance")


def test_not_connected_by_default_with_no_outbound_call(h):
    h.ports.fetcher = None
    h.tenant("acme", "own", ("acme.test",))
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert (e["outcome"], e["reason"], e["facts"]["requests"]) == ("NOT_CONNECTED", "GSC_NOT_CONNECTED", 0)
    assert view["report"]["search_performance"] == {"outcome": "NOT_CONNECTED", "reason": "GSC_NOT_CONNECTED",
                                                    "read_started": False, "section": None}
    assert "first_party:search_console" in view["report"]["not_connected"]
    st = h.ok(h.get("/status"))["search_console"]
    assert st["state"] == "NOT_CONNECTED" and st["outbound_requests_made"] == 0 and st["service_account"] is None
    assert not h.ledger.of_type("gsc_read_started")


def test_unbound_tenant_is_not_read(tmp_path):
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test",))
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("NOT_CONNECTED", "GSC_PROPERTY_UNBOUND") and fake.requests == []
    assert h.ok(h.get("/status"))["search_console"]["service_account"] == SA_EMAIL


def test_binding_is_andres_and_must_be_the_tenants_own_domain(tmp_path):
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test",))
    h.tenant("globex", "client", ("globex.test",))
    h.refused(bind(h, "acme", PROP, andre=False), 403)
    h.refused(h.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": PROP}, caller="seo_agent"), 403)
    h.refused(bind(h, "acme", "sc-domain:globex.test"), 422, "GSC_PROPERTY_INVALID")
    h.refused(bind(h, "acme", "https://evil.test/"), 422, "GSC_PROPERTY_INVALID")
    h.refused(bind(h, "acme", "sc-domain:ACME.test"), 422)
    t = h.ok(bind(h, "acme", "https://www.acme.test/"))
    assert t["gsc_property"] == "https://www.acme.test/"
    t = h.ok(bind(h, "acme", PROP))
    assert t["gsc_property"] == PROP and h.ledger.of_type("tenant_gsc_property_set")
    assert h.ok(bind(h, "acme", None))["gsc_property"] is None


def test_a_property_belongs_to_one_tenant(h):
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    # a second tenant can never hold acme.test (DOMAIN_TAKEN), so it can never be bound to acme's property
    h.tenant("globex", "client", ())
    h.refused(h.post("/tenants/globex/domains", {"request_id": rid(), "domains": ["shop.acme.test"]}, andre=True),
              409, "DOMAIN_TAKEN")
    h.refused(bind(h, "globex", PROP), 422, "GSC_PROPERTY_INVALID")
    h.svc.tenants["globex"]["domains"] = ["shop.acme.test"]          # even if the domain rule were bypassed
    h.refused(bind(h, "globex", "sc-domain:shop.acme.test"), 409, "GSC_PROPERTY_TAKEN")


def test_a_read_uses_only_the_tenants_bound_property_and_cross_tenant_is_refused(tmp_path):
    fake = FakeGoogle()
    fake.sites.append({"siteUrl": "sc-domain:globex.test", "permissionLevel": "siteFullUser"})
    h = gsc_harness(tmp_path, fake, SEO_INVOICE_VERIFICATION="trust")
    h.tenant("acme", "own", ("acme.test",))
    h.tenant("globex", "client", ("globex.test",))
    h.ok(bind(h, "acme", PROP))
    h.ok(bind(h, "globex", "sc-domain:globex.test"))
    a = audit(h, "acme", "acme.test")
    paths = {r["path"] for r in fake.api_requests() if r["path"].endswith("/query")}
    assert paths == {"/webmasters/v3/sites/sc-domain%3Aacme.test/searchAnalytics/query"}
    assert section(a)["outcome"] == "OK"
    # globex's hub token cannot see acme's audit (the same 404 as a missing one)
    miss = h.get(f"/tenants/globex/audits/{a['audit_id']}", caller="hub", tenant="globex")
    other = h.get(f"/tenants/acme/audits/{a['audit_id']}", caller="hub", tenant="globex")
    assert miss.status_code == 404 and other.status_code == 404
    assert h.ok(h.get(f"/tenants/acme/audits/{a['audit_id']}", caller="hub", tenant="acme"))["audit_id"]
    # the binding is read from state at run time: a request cannot name a property
    h.refused(h.post("/tenants/acme/audits", {"request_id": rid(), "domain": "acme.test", "paths": ["/"],
                                              "property": "sc-domain:globex.test"}, andre=True), 422)
    # a binding whose domain the tenant no longer holds is not read
    h.ok(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": ["acme2.test"]}, andre=True))
    n = len(fake.requests)
    e = section(audit(h, "acme", "acme2.test"))
    assert e["reason"] == "GSC_PROPERTY_UNBOUND" and len(fake.requests) == n


def test_full_read_is_aggregates_only_record_first_and_pii_queries_dropped(tmp_path):
    fake = FakeGoogle()
    seen = []

    def on(fk, req):
        seen.append(len(h.ledger.of_type("gsc_read_started")))
    fake.on_request = on
    h = gsc_harness(tmp_path, fake, data_dir=str(tmp_path / "data"))
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    view = audit(h, "acme", "acme.test")
    assert seen and min(seen) == 1                              # the read was on the ledger before the first request
    sec = view["report"]["search_performance"]["section"]
    cls = sec["by_query_class"]["classes"]
    assert cls["pii_suspect"]["clicks"] == 2 and cls["brand"]["clicks"] == 20 and cls["question"]["clicks"] == 10
    assert cls["local"]["clicks"] == 5 and cls["other"]["clicks"] == 3
    assert h.ledger.of_type("gsc_read_recorded") and h.ledger.of_type("audit_report_recorded")
    blob = json.dumps(view)
    for p in (tmp_path / "data").rglob("*"):
        if p.is_file():
            blob += p.read_bytes().decode("utf-8", "replace")
    blob += json.dumps([e["_payload"] for e in h.ledger.events]) + json.dumps([e["summary"] for e in h.ledger.events])
    for raw in ("jane.doe@example.com", "555 010 0199", "how to clean a rug", "acme pricing", "elsewhere.test",
                fake.tokens[0]):
        assert raw not in blob, raw
    assert not any(f in blob for f in pem_fragments())
    st = json.dumps(h.ok(h.get("/status")))
    assert fake.tokens[0] not in st and not any(f in st for f in pem_fragments())
    ex = json.dumps(h.ok(h.get("/audit/export", params={"limit": 1000})))
    assert fake.tokens[0] not in ex and not any(f in ex for f in pem_fragments())


def test_kill_switch_gsc_stops_the_section_before_any_request(tmp_path):
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    h.ok(h.switch("provider:gsc"))
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert (e["outcome"], e["reason"], e["facts"]["switch"]) == ("KILLED", "GSC_KILLED", "KILLED_PROVIDER")
    assert fake.requests == [] and not h.ledger.of_type("gsc_read_started")
    assert h.ok(h.get("/status"))["search_console"]["killed"] is True
    h.refused(h.switch("provider:gsc", engaged=False), 403, "ANDRE_APPROVAL_REQUIRED")
    h.ok(h.switch("provider:gsc", engaged=False, andre=True))
    assert section(audit(h, "acme", "acme.test"))["outcome"] == "OK"


def test_kill_switch_engaged_mid_read_stops_it(tmp_path):
    fake = FakeGoogle()
    fake.queries = [(f"rug {i}", 1, 10, 3.0) for i in range(30)]
    h = gsc_harness(tmp_path, fake, SEO_GSC_PAGE_ROWS="100")

    def on(fk, req):
        if len(fk.api_requests()) == 3:
            h.svc.switches["provider"]["gsc"] = True               # as an engage would, mid-read
    fake.on_request = on
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("KILLED", "GSC_KILLED") and len(fake.api_requests()) == 3


def test_env_kill_switch_and_budget_including_scheduled_runs(tmp_path):
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake, SEO_GSC_READS_PER_DAY="2")
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    assert section(audit(h, "acme", "acme.test"))["outcome"] == "OK"
    sch = h.ok(h.post("/tenants/acme/schedules", {"request_id": rid(), "domain": "acme.test", "paths": ["/"],
                                                   "every_days": 1}), 201)
    tick = h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    assert tick["ran"] == 1
    sv = h.ok(h.get(f"/tenants/acme/schedules/{sch['schedule_id']}"))
    first = sv["slots"]["0"]["audit_id"]
    assert section(h.ok(h.get(f"/tenants/acme/audits/{first}")))["outcome"] == "OK"
    n = len(fake.requests)
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("BLOCKED", "GSC_BUDGET_EXHAUSTED") and len(fake.requests) == n
    h.clock.advance(days=1, minutes=1)
    tick = h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    assert tick["ran"] == 1 and tick["drift_recorded"] == 1
    sv = h.ok(h.get(f"/tenants/acme/schedules/{sch['schedule_id']}"))
    sp_drift = sv["drifts"][-1]["drift"]["search_performance"]
    # a daily schedule over a 28-day window: 27 shared days, one fresh day: not compared (AEGIS gl-gsc L3)
    assert sp_drift["metrics"]["clicks"]["class"] == "UNKNOWN" and sp_drift["windows_overlap_days"] == 27
    assert sp_drift["metrics"]["clicks"]["evidence"]["fresh_days"] == 1
    assert sp_drift["decision"] == "INSUFFICIENT_EVIDENCE"
    d = tmp_path / "env-kill"
    d.mkdir()
    h2 = gsc_harness(d, FakeGoogle(), SEO_KILLED_PROVIDERS="gsc")
    h2.tenant("acme", "own", ("acme.test",))
    h2.ok(bind(h2, "acme", PROP))
    assert section(audit(h2, "acme", "acme.test"))["reason"] == "GSC_KILLED"


def test_no_record_no_request(tmp_path):
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    orig = h.svc._commit

    def failing(kind, *a, **k):
        if kind == "gsc_read_started":
            from errors import Unavailable
            raise Unavailable("LEDGER_UNAVAILABLE")
        return orig(kind, *a, **k)
    h.svc._commit = failing
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("BLOCKED", "LEDGER_UNAVAILABLE") and fake.requests == []


def test_errors_reach_the_report_as_outcomes_not_500s(tmp_path):
    for script, outcome, reason in (([_err(403, "insufficientPermissions")], "BLOCKED", "GSC_PROPERTY_NOT_GRANTED"),
                                    ([_err(403, "accessNotConfigured")], "BLOCKED", "GSC_AUTH_REFUSED"),
                                    ([(503, {})] * 3, "FAILED", "GSC_UNAVAILABLE"),
                                    ([(200, b"<html>")], "FAILED", "GSC_PROTOCOL_ERROR")):
        fake = FakeGoogle()

        armed = [list(script)]

        def on(fk, req, armed=armed):
            if req.url.raw_path.decode().endswith("/searchAnalytics/query") and armed:
                fk.script.extend(armed.pop())               # once: from the first query on
        fake.on_request = on
        d = tmp_path / reason
        d.mkdir()
        h = gsc_harness(d, fake)
        h.tenant("acme", "own", ("acme.test",))
        h.ok(bind(h, "acme", PROP))
        view = audit(h, "acme", "acme.test")
        e = section(view)
        assert (view["status"], e["outcome"], e["reason"]) == ("completed", outcome, reason)
        assert h.ledger.of_type("gsc_read_recorded")[0]["_payload"]["reason"] == reason


def test_retention_serves_totals_only_after_the_window(tmp_path):
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake, SEO_GSC_RETENTION_DAYS="30")
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    aid = audit(h, "acme", "acme.test")["audit_id"]
    h.clock.advance(days=31)
    sec = h.ok(h.get(f"/tenants/acme/audits/{aid}"))["report"]["search_performance"]["section"]
    assert sec["expired"] is True and "by_query_class" not in sec and sec["totals"]["clicks"] == 140


def test_drift_rules_for_search_performance():
    def rep(outcome="OK", clicks=100, impr=1000, days=28, prop=PROP, expired=False, start="2026-09-01",
            end="2026-09-28"):
        sec = {"property": prop, "window": {"start": start, "end": end, "days": days,
                                            "data_state": "final", "search_type": "web"},
               "totals": {"clicks": clicks, "impressions": impr, "ctr": None, "position": None},
               "versions": {"search_performance": sp.VERSION}}
        if expired:
            sec["expired"] = True
        return {"search_performance": {"outcome": outcome, "reason": None, "section": sec}}
    c = drift.compare_search_performance
    later = {"start": "2026-09-29", "end": "2026-10-26"}                 # the next 28 days: no overlap
    assert c(rep(), rep(outcome="FAILED"))["metrics"]["clicks"]["class"] == "TOOL_FAILURE"
    assert c(rep(), rep(expired=True, **later))["metrics"]["clicks"]["class"] == "SAMPLING_NOISE"
    assert c(rep(), rep(days=7))["metrics"]["clicks"]["class"] == "MEASUREMENT_ERROR"
    assert c(rep(), rep(prop="sc-domain:b.test"))["metrics"]["impressions"]["class"] == "MEASUREMENT_ERROR"
    assert c(rep(), rep(clicks=110, **later))["metrics"]["clicks"]["class"] == "SAMPLING_NOISE"
    r = c(rep(), rep(clicks=200, **later))
    assert r["metrics"]["clicks"]["class"] == "REAL_CHANGE" and r["decision"] == "INSUFFICIENT_EVIDENCE"
    assert c({}, {}) is None


# Osei's query classes use the same address rule (search_performance 2026-11-08.3). The table is the round-3
# reviewer's probe set: ordinary service queries with numbers are KEPT in their class, address forms are
# pii_suspect, and the GAP rows are known misses, disclosed in ADR 0047 and ADR 0017 GSC-12 (an English-word state
# code; a bare named street with no suffix). A GAP row that starts being caught is fine; the test then says so.
ADDRESS_KEEP = (
    "#1 rug cleaner", "ac unit 12000 btu", "24 hour emergency plumber near dr", "2 bedroom flat close to station",
    "10 best hiking trail", "2 lane road", "cars under 30000 in 50000 miles", "or 50000 btu", "ohio 45000 jobs",
    "rug cleaner near me", "4 way stop", "top 10 rug cleaners", "rug cleaner 90210", "apt 12 carpet cleaning",
    "3days in st louis", "3 days in st louis", "10 things to do in st louis", "plumber 24 7", "best hvac 2024",
    "2 car garage door repair", "1 hour heating and air", "3 tab shingle roof cost", "5 star plumbing",
    "100 amp panel upgrade cost", "2x4 lumber price", "40 gallon water heater", "50 gallon water heater installed",
    "3 ton ac unit price", "dentist open saturday", "emergency dentist 24 hours", "1st street dental",
    "roof repair 2 story house", "5th avenue salon", "2 stroke oil", "24 hr locksmith", "7 eleven near me",
    "car wash on main st", "pizza delivery 77002", "dentist 10001", "window tint 35 percent",
    "1 bedroom apartment for rent", "15 minute oil change", "6 inch gutters", "3 season room cost", "20 amp outlet",
    "200 sq ft deck cost", "4 dr sedan", "2 ct diamond ring", "10 ft christmas tree", "12 pl tile saw",
    "3 st patricks day ideas", "5 dr hook songs", "2 rd round draft", "9 ln of code", "plumber in springfield il",
    "best roofer in austin tx", "hvac tulsa ok", "texas 2024 tax", "ca 90210 zip code", "10 best st louis bbq",
    "top 5 pizza places 5th ave", "route 66 diner", "i 95 traffic", "highway 101 closure", "1040 form",
    "covid 19 test near me", "suite 200 dental", "flat 4b rug", "interstate 10 accident", "area code 512",
    "zip code 78701", "call 911")
ADDRESS_DROP = (
    "123 main st springfield", "45 oak avenue apt 3", "po box 1234", "springfield il 62701", "90210-1234",
    "123 main road", "42 wallaby way sydney", "123 main rd", "123 oak blvd", "500 sunset blvd los angeles ca 90028",
    "77 maple lane", "12 elm ln", "1600 pennsylvania avenue nw washington dc", "1600 pennsylvania ave nw",
    "350 5th ave new york ny 10118", "100 n main st", "100 main st n", "2200 sw 1st ave portland",
    "123 main st suite 400", "123 main st #4", "123 main st apt 2b", "123 main st, austin, tx 78701",
    "123 main street", "9 n elm blvd.", "123 main st springfield il 62701 hours", "plumber at 123 main st",
    "rug cleaning 123 main st austin tx", "123 main st 2nd floor", "123 main st springfield il", "456 oak dr",
    "456 oak drive", "10 downing st london", "742 evergreen terrace", "742 evergreen ter",
    "1 infinite loop cupertino", "4059 mt lee dr hollywood", "233 s wacker dr chicago il 60606",
    "123 main st near me", "123 e 45th st new york ny", "po box 77 austin tx", "boise id 83702", "honolulu hi 96813",
    "columbus oh 43215", "austin tx 78701", "boston ma 02108", "12 kings road chelsea", "15 high street",
    "8 oak court", "3 park place", "1 apple park way", "22 baker st apt 3 dentist", "123 main st unit 5",
    "123 main st ste 100", "123 main st, apt 4, springfield, il 62701", "123 main st springfield missouri",
    "55 water st 12th floor", "1 main st pmb 123", "po box 1234 springfield il 62701", "rr 2 box 15", "hc 1 box 9",
    "123 main st springfield oregon 97477 phone")
ADDRESS_GAP = (
    "portland or 97201", "1200 broadway", "1200 broadway new york")
ADDRESS_TABLE = [(q, "KEEP") for q in ADDRESS_KEEP] + [(q, "DROP") for q in ADDRESS_DROP]


@pytest.mark.parametrize("q,want", ADDRESS_TABLE)
def test_osei_address_rule_keeps_service_queries_and_drops_addresses(q, want):
    assert (sp.classify_query(q, ["acme"]) == "pii_suspect") == (want == "DROP"), q


@pytest.mark.parametrize("q", ADDRESS_GAP)
def test_osei_address_rule_known_gaps_are_the_disclosed_ones(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", f"{q!r} is now caught: drop it from the disclosed gaps"


def test_osei_query_classes_count_addresses_as_pii_and_keep_the_rest():
    from collections import Counter
    got = Counter(sp.classify_query(q, ["acme"]) for q, _ in ADDRESS_TABLE)
    assert got["pii_suspect"] == len(ADDRESS_DROP) and sum(got.values()) == len(ADDRESS_TABLE)
    assert sp.classify_query("24 hour emergency plumber near dr", ["acme"]) == "other"
    assert sp.classify_query("ac unit 12000 btu", ["acme"]) == "other"


def test_overlong_queries_are_not_examined_and_count_as_personal_data():
    cap = sp.MAX_QUERY_CHARS
    ok = ("rug " * cap)[:cap]
    assert len(ok) == cap and sp.classify_query(ok, ["acme"]) != "pii_suspect"
    assert sp.classify_query(ok + "s", ["acme"]) == "pii_suspect"
    assert sp.classify_query(" " * 50_000 + "1", ["acme"]) == "pii_suspect"     # the reviewer's slow input, now capped
    assert sp.looks_like_address("a " * 300) is True


# Round-4 reviewer's probe set (search_performance 2026-11-08.4): service-business queries by trade, and US, UK,
# Canadian and French address forms. "123 n main" (a bare named street, no suffix) is a disclosed gap.
ROUND4_KEEP = (
    "plumber near me open now", "water heater 50 gal gas", "tankless water heater 199k btu",
    "leaky faucet repair cost", "sump pump 1/2 hp", "drain cleaning 24/7", "1 1/2 inch pvc pipe",
    "toilet flange 4 inch", "plumber on oak lane", "plumbing supply on harbor drive",
    "emergency plumber court street", "hvac repair near me", "2.5 ton heat pump", "16 seer ac unit price",
    "furnace 80000 btu", "mini split 12k", "ac not cooling 85 degrees", "carrier 24acc636a003 manual",
    "trane xr14 4 ton", "hvac tune up 89 special", "goodman 3 ton 14 seer", "duct cleaning 3 bedroom house",
    "house cleaning 3 bed 2 bath price", "move out cleaning 2 bedroom apartment", "carpet cleaning 3 rooms for 99",
    "maid service 2 hours", "deep clean 1500 sq ft", "office cleaning 5 days a week", "window washing 2 story",
    "pizza near me", "best tacos on 6th street", "sushi on main street", "brunch on king street",
    "thai food 2 for 1", "restaurants open till 2am", "diner on route 9", "bbq 3 meat plate",
    "wings 50 cent tuesday", "food court mall hours", "cafe on sunset boulevard", "steakhouse on park avenue",
    "hair salon near me", "nail salon open sunday", "balayage 2 hours", "kids haircut 10 dollars",
    "barber shop on water street", "salon on the way", "lash extensions 3 week fill", "oil change 5w-30",
    "tires 225/65r17", "brake pads 2015 honda civic", "transmission repair 4l60e", "auto body shop drive thru",
    "car wash 5 dollar tuesday", "battery group 24f", "2012 ford f150 3.5 ecoboost", "tow truck 10 miles cost",
    "mechanic on 5th street", "alignment 4 wheel cost", "dui lawyer near me",
    "divorce attorney 30 minute free consultation", "chapter 7 bankruptcy lawyer",
    "personal injury lawyer 33 percent", "will and trust 500 flat fee", "lawyer on court street",
    "law office on main way", "court date lookup", "small claims court 10000 limit",
    "dentist near me accepting medicaid", "teeth cleaning 99 special", "dental implant 3000", "invisalign 6 months",
    "emergency dentist open 24 hours", "wisdom teeth removal 4 teeth cost", "dentist on 3rd avenue",
    "roof replacement 2000 sq ft", "30 year shingles", "gutter cleaning 1 story", "fence 6 ft cedar",
    "concrete driveway 20x20", "pressure washing 2 car driveway", "garage door 16x7", "lane closures i 35",
    "4 lane highway construction", "3 way switch wiring", "2 way radio repair", "dead end drive in theater",
    "16 drive in movie", "cul de sac lot", "10 court cases", "3 court reporters near me", "2 drive shaft repair",
    "the 5 love languages", "best 2 way mirror", "1 way street ticket",
)
ROUND4_DROP = (
    "742 maple st", "1010 n oak ave", "88 w broad st apt 12", "2301 e camelback rd phoenix az 85016",
    "15 cedar lane", "4 birch court", "900 river road", "77 sunset way", "12 harbor drive", "600 elm place",
    "300 pine terrace", "1 market plaza", "5 elm loop", "20 grove st, boston, ma 02114", "1400 n state pkwy chicago",
    "45 park ave suite 2100 new york ny 10016", "3001 s congress ave austin tx 78704", "250 w 57th st",
    "17 old mill road", "102 spruce ln unit 4", "8 main st #3", "plumber 742 maple st", "dentist at 45 park ave",
    "450 oak dr springfield", "9 lakeview cir", "1800 n highland ave los angeles ca 90028", "po box 55",
    "p.o. box 9921 denver co 80201", "denver co 80202", "miami fl 33101", "seattle, wa 98101",
    "atlanta ga 30303-1234", "10 elm st apt 5 new haven ct 06510", "5500 n western ave", "16 wedgewood dr",
    "1225 chestnut street philadelphia pa", "61 bay st san francisco", "321 maple avenue", "100 old country rd",
    "221b baker street london", "10 downing street", "14 high st oxford ox1 4ag", "sw1a 1aa",
    "flat 3 22 queens road", "100 queen st w toronto on m5h 2n2", "m5v 3l9", "1 yonge st toronto",
    "350 rue sherbrooke montreal", "4 privet drive little whinging",
    "12 new street", "40 good hope rd", "88 old post road", "300 OLD MILL RD",
)
WORD_KEEP = ("2.5 ton heat pump", "tires 225/65r17", "2012 ford f150 3.5 ecoboost", "carrier 24acc636a003 manual",
             "trane xr14 4 ton", "oil change 5w-30", "transmission repair 4l60e", "battery group 24f", "u.s flag",
             "16 seer ac unit price", "goodman gsx140361 specs")
# the UUID below is a sample value, built from parts
WORD_DROP = ("a1b2c3d4e5f6g7", "token abcd1234efgh5678", "-".join(("f47ac10b", "58cc", "4372", "a567", "0e02b2c3d479")),
             "deadbeefdeadbeef00", "abcdefgh.ijklmnop.qrstuvwx", "x" * 41, "jane_cv.pdf", "q9w8e7r6t5", "order 1234567",
             "acct ab12345678")


@pytest.mark.parametrize("q,want", [(q, "KEEP") for q in ROUND4_KEEP + WORD_KEEP]
                         + [(q, "DROP") for q in ROUND4_DROP + WORD_DROP])
def test_osei_round4_keep_drop_table(q, want):
    assert (sp.classify_query(q, ["acme"]) == "pii_suspect") == (want == "DROP"), q


# Round-5 regressions: ordinals and storage units are not UK inward codes, French place / route need an article,
# old / new / good alone as a street name only at the end, and base64 / AWS-style secrets holding "/" drop again.
# The secret-shaped values are built from parts so that no scanner sees a literal.
ROUND5_KEEP = ("ps5 2tb ssd upgrade", "ipad pro m4 1tb", "bmw x5 3rd row", "audi q7 3rd row seating",
               "galaxy s9 2nd hand", "m3 2nd gen", "6 place setting dinnerware", "bus 12 route map", "5 route options",
               "3 new dr pepper flavors", "2 old court cases", "10 good place to eat", "5 good drive thru",
               "tires 225/65r17", "example.com/blog/how-to-clean-rugs", "acme.com/services/rug-cleaning tips",
               "5/8 inch plywood", "24/7 plumber", "1/2 hp sump pump",
               # round 6: URL-path queries with digits in a slug
               "docs.example.com/v2/api/reference", "api.stripe.com/v1/charges docs", "acme.com/blog/5-ways-to-save",
               "example.org/docs/v3/getting-started", "mysite.com/page2/contact", "moz.com/learn/seo/http2")
ROUND5_DROP = ("sw1a 1aa", "14 high st oxford ox1 4ag", "m1 1ae manchester", "12 new street", "6 place de la concorde",
               "12 chemin du lac", "12 avenue des champs", "/".join(("wJalrXUtnFEMI", "K7MDENG", "bPxRfiCYEXAMPLEKEY")),
               "/".join(("dGhpcy9pcy9h", "c2VjcmV0IGtleQ==")), "+".join(("YWJj/ZGVm", "Z2hp")))


@pytest.mark.parametrize("q,want", [(q, "KEEP") for q in ROUND5_KEEP] + [(q, "DROP") for q in ROUND5_DROP])
def test_osei_round5_keep_drop_table(q, want):
    assert (sp.classify_query(q, ["acme"]) == "pii_suspect") == (want == "DROP"), q


def test_osei_round4_disclosed_gap():
    assert sp.classify_query("123 n main", ["acme"]) != "pii_suspect"     # bare named street: disclosed, not caught


def test_nfkc_expansion_past_the_cap_is_personal_data():
    q = "\ufdfa" * 40                                                    # 40 characters, 720 after NFKC
    assert len(q) <= sp.MAX_QUERY_CHARS and sp.classify_query(q, ["acme"]) == "pii_suspect"


def test_query_classifier_pii_first_and_closed():
    b = ["acme"]
    for q in ("jane.doe@example.com", "jane (at) example dot com", "acme 5550100199", "call 555-010-0199",
              "ｊａｎｅ＠example.com", "order 1234567", "a1b2c3d4e5f6g7"):
        assert sp.classify_query(q, b) == "pii_suspect", q
    assert sp.classify_query("acme reviews", b) == "brand"
    assert sp.classify_query("how do rugs work", b) == "question"
    assert sp.classify_query("rug shop near me", b) == "local"
    assert sp.classify_query("cheap rugs", b) == "commercial"
    assert sp.classify_query("rugs", b) == "other"
    assert set(sp.QUERY_CLASSES) == {"pii_suspect", "brand", "question", "local", "commercial", "other"}


def test_window_is_pacific_and_lags_three_days():
    from datetime import datetime, timezone
    assert sp.window(datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc), 28) == W
    # 03:00 UTC on Oct 10 is still Oct 9 in Los Angeles
    assert sp.window(datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc), 28) == W


def test_property_forms():
    assert g.property_host("sc-domain:acme.test") == "acme.test"
    assert g.property_host("https://www.acme.test/shop/") == "www.acme.test"
    for bad in ("acme.test", "sc-domain:", "ftp://acme.test/", "https://acme.test", "sc-domain:a b.test"):
        assert g.property_host(bad) is None


def test_settings_bounds(tmp_path):
    for k, v in (("SEO_GSC_WINDOW_DAYS", "91"), ("SEO_GSC_PAGE_ROWS", "25001"), ("SEO_GSC_MAX_REQUESTS", "2"),
                 ("SEO_GSC_DEADLINE_SECONDS", "1")):
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(**{k: v}))
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SEO_GSC_SERVICE_ACCOUNT_FILE="relative.json"))
    assert config_mod.load(base_env()).gsc_key is None


def test_httpcore_is_what_the_deadline_backend_expects():
    """If httpx ever stops exposing the pool's network backend, the client refuses to call Google (never without the
    deadline) instead of calling it unbounded."""
    import httpx
    t = httpx.HTTPTransport(trust_env=False, retries=0)
    assert hasattr(t._pool, "_network_backend") and isinstance(t._pool._network_backend, httpcore.NetworkBackend)
