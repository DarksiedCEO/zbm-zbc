"""AEGIS review of golive-gsc (0c9a96a): regressions for every finding fixed on this branch. Each test here fails on
0c9a96a. No network: the fake Google of gsc_fakes.py; time is an injected clock."""

from __future__ import annotations

import json
import logging

import pytest
from gsc_fakes import FakeGoogle
from helpers import rid
from test_gsc import PROP, W, audit, bind, client, code_of, gsc_harness, section

import gsc_client as g
from agents import search_performance as sp

BIG = "1" + "0" * 400                           # a 400-digit JSON integer: json.loads makes it an int


def _setup(tmp_path, fake, **env):
    h = gsc_harness(tmp_path, fake, **env)
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    return h


def _first_query_answers(fake, raw: bytes):
    armed = [True]

    def on(fk, req):
        if req.url.raw_path.decode().endswith("/searchAnalytics/query") and armed:
            armed.pop()
            fk.script.append((200, raw))
    fake.on_request = on


# ====================================================================== M1: malformed answers never crash the audit

def test_m1_permission_level_that_is_not_a_string_is_a_protocol_error():
    for level in (["siteOwner"], {"x": 1}, 7, None):
        fake = FakeGoogle()
        fake.sites = [{"siteUrl": PROP, "permissionLevel": level}]
        e = code_of(lambda f=fake: client(f).open_read(lambda: None).check_property(PROP))
        assert (e.code, e.detail) == ("GSC_PROTOCOL_ERROR", "site_entry"), level


def test_m1_token_error_that_is_not_a_string_is_a_protocol_error():
    for err in ({"code": 400, "message": "bad"}, ["invalid_grant"], 400):
        fake = FakeGoogle()
        fake.token_script = [(400, {"error": err})]
        e = code_of(lambda f=fake: client(f).open_read(lambda: None).token())
        assert (e.code, e.detail) == ("GSC_PROTOCOL_ERROR", "token_error_shape"), err
    fake = FakeGoogle()                         # the documented string form still maps to the closed set
    fake.token_script = [(400, {"error": "invalid_grant"})]
    assert code_of(lambda: client(fake).open_read(lambda: None).token()).code == "GSC_AUTH_REFUSED"


@pytest.mark.parametrize("field", ["clicks", "impressions", "ctr", "position"])
def test_m1_a_huge_integer_metric_is_a_protocol_error(field):
    row = {"keys": [W[1]], "clicks": 1, "impressions": 2, "ctr": 0.5, "position": 3.0}
    raw = json.dumps({"rows": [row]}).replace(f'"{field}": {row[field]}', f'"{field}": {BIG}').encode()
    fake = FakeGoogle()
    _first_query_answers(fake, raw)
    rd = client(fake).open_read(lambda: None)
    e = code_of(lambda: sp.read_aggregates(rd, PROP, *W, {"acme.test"}, ["acme"]))
    assert e.code == "GSC_PROTOCOL_ERROR" and e.detail in ("row_counts", "row_ctr", "row_position")


def test_m1_finite_bounds():
    assert g._finite(10 ** 15) and not g._finite(10 ** 15 + 1) and not g._finite(-(10 ** 400), lo=-1e300)
    assert not g._finite(int(BIG)) and not g._finite(float("inf")) and not g._finite(float("nan"))
    assert not g._finite(True) and g._finite(0.5, 0.0, 1.0)


def _token_dict(fake):
    fake.token_script = [(400, {"error": {"code": 400, "message": "bad"}})]


def _level_list(fake):
    fake.sites = [{"siteUrl": PROP, "permissionLevel": ["siteOwner"]}]


def _bigint(fake):
    _first_query_answers(fake, ('{"rows":[{"keys":["%s"],"clicks":%s,"impressions":2}]}' % (W[1], BIG)).encode())


@pytest.mark.parametrize("arm", [_token_dict, _level_list, _bigint], ids=["token_dict", "level_list", "bigint"])
def test_m1_the_audit_completes_with_the_section_failed(tmp_path, arm):
    fake = FakeGoogle()
    arm(fake)
    h = _setup(tmp_path, fake)
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert view["status"] == "completed" and view["report"] is not None
    assert (e["outcome"], e["reason"]) == ("FAILED", "GSC_PROTOCOL_ERROR")


def test_m1_any_other_exception_in_the_read_is_a_failed_section_and_only_its_type_is_logged(tmp_path, monkeypatch,
                                                                                         caplog):
    fake = FakeGoogle()
    h = _setup(tmp_path, fake)

    def boom(*a, **k):
        raise ValueError("GOOGLE-SAID jane.doe@example.com")
    monkeypatch.setattr(sp, "read_aggregates", boom)
    with caplog.at_level(logging.DEBUG):
        view = audit(h, "acme", "acme.test")
    e = section(view)
    assert view["status"] == "completed"
    assert (e["outcome"], e["reason"], e["facts"]["error_type"]) == ("FAILED", "GSC_PROTOCOL_ERROR", "ValueError")
    assert "GOOGLE-SAID" not in caplog.text and "ValueError" in caplog.text
    assert "GOOGLE-SAID" not in json.dumps(view) + json.dumps([ev["_payload"] for ev in h.ledger.events])
    assert h.ledger.of_type("gsc_read_recorded")[0]["_payload"]["reason"] == "GSC_PROTOCOL_ERROR"


# ====================================================================== M2: the GSC section never decides the audit

def _without_gsc(tmp_path) -> str:
    """The overall outcome of the same audit on a service with no Search Console configured."""
    from helpers import Harness
    d = tmp_path / "no-gsc"
    d.mkdir()
    h0 = Harness(d)
    h0.ports.fetcher = None
    h0.tenant("acme", "own", ("acme.test",))
    return audit(h0, "acme", "acme.test")["report"]["outcome"]


def _ok(h):
    return audit(h, "acme", "acme.test")


def _killed(h):
    h.ok(h.switch("provider:gsc"))
    return audit(h, "acme", "acme.test")


def _budget(h):
    audit(h, "acme", "acme.test")
    return audit(h, "acme", "acme.test")


def _outage(h):
    return audit(h, "acme", "acme.test")


@pytest.mark.parametrize("run,env,script,want", [
    (_ok, {}, [], ("OK", None)),
    (_killed, {}, [], ("KILLED", "GSC_KILLED")),
    (_budget, {"SEO_GSC_READS_PER_DAY": "1"}, [], ("BLOCKED", "GSC_BUDGET_EXHAUSTED")),
    (_outage, {}, [(503, {})] * 3, ("FAILED", "GSC_UNAVAILABLE")),
], ids=["ok", "killed", "budget_spent", "503x3"])
def test_m2_the_overall_outcome_is_what_it_would_be_without_search_console(tmp_path, run, env, script, want):
    baseline = _without_gsc(tmp_path)
    fake = FakeGoogle()
    fake.script = list(script)
    d = tmp_path / "gsc"
    d.mkdir()
    h = _setup(d, fake, **env)
    view = run(h)
    e = section(view)
    assert (e["outcome"], e["reason"]) == want
    assert view["report"]["search_performance"]["outcome"] == want[0]       # kept inside its own section
    assert view["report"]["outcome"] == baseline, (view["report"]["outcome"], baseline)
    assert "search_performance" in view["report"]["outcome_rule"]


# ====================================================================== L1: no URL survives as a template

def test_l1_slugs_and_identifiers_are_collapsed():
    hosts = {"acme.test", "www.acme.test"}
    cases = {"https://acme.test/users/jane-smith/profile": "/users/{slug}/profile",
             "https://acme.test/patients/john-q-public-dob-1980-01-02": "/patients/{number}",
             "https://acme.test/people/john.q.public": "/people/{slug}",
             "https://acme.test/p/jane_smith/": "/p/{slug}/",
             "https://acme.test/blog/post-1": "/blog/{slug}",
             "https://acme.test/w/Zoë": "/w/{slug}",
             "https://acme.test/u/jane.doe@example.com": "/u/{email}",
             "https://acme.test/index.html": "/index.html",
             "https://acme.test/doc/cv.pdf": "/doc/{file}.pdf",
             "https://acme.test/": "/",
             "https://acme.test/pricing": "/pricing"}
    for url, want in cases.items():
        assert sp.page_template(url, hosts) == want, url
    assert not sp.has_literal("/{slug}/{id}") and not sp.has_literal("/") and sp.has_literal("/users/{slug}")


def test_l1_templates_of_fewer_than_three_urls_with_a_literal_word_go_to_other():
    fake = FakeGoogle()
    fake.pages = [("https://acme.test/users/jsmith", 7, 70, 2.0),            # a plain word that may be a name
                  ("https://acme.test/users/jane-smith/profile", 1, 10, 3.0),
                  ("https://acme.test/users/john-doe/profile", 1, 10, 3.0),
                  *[(f"https://acme.test/blog/post-{i}", 2, 20, 4.0) for i in range(3)],
                  ("https://acme.test/x-y-z", 1, 10, 1.0)]                 # placeholder only: always kept
    rd = client(fake).open_read(lambda: None)
    out = sp.read_aggregates(rd, PROP, *W, {"acme.test"}, ["acme"])["by_page_template"]
    got = {t["template"]: (t["clicks"], t["impressions"]) for t in out["templates"]}
    assert got == {"/blog/{slug}": (6, 60), "/{slug}": (1, 10), "{other}": (9, 90)}
    assert out["templates_collapsed_into_other"] == 2 and out["min_urls_per_template"] == 3
    assert "jsmith" not in json.dumps(out)


def test_l1_the_method_text_no_longer_claims_no_url_is_kept():
    import svc_gsc
    assert "no URL is kept" not in svc_gsc.METHOD and "{slug}" in svc_gsc.METHOD


# ====================================================================== L2: retention also binds the export

def test_l2_export_serves_expired_sections_as_totals_only(tmp_path):
    fake = FakeGoogle()
    fake.pages += [("https://acme.test/blog/post-3", 1, 5, 2.0)]
    h = _setup(tmp_path, fake, SEO_GSC_RETENTION_DAYS="1")
    audit(h, "acme", "acme.test")

    def exported():
        ev = h.ok(h.get("/audit/export", params={"limit": 1000}))["events"]
        return [e["data"]["report"]["search_performance"]["section"] for e in ev if e["kind"] == "audit_completed"]
    fresh = exported()
    assert fresh and "by_query_class" in fresh[0] and "by_page_template" in fresh[0]
    h.clock.advance(days=2)
    (old,) = exported()
    assert old["expired"] is True and "by_query_class" not in old and "by_page_template" not in old
    assert "by_date" not in old and old["totals"]["clicks"] == fresh[0]["totals"]["clicks"]
    assert "/blog/{slug}" not in json.dumps(h.ok(h.get("/audit/export", params={"limit": 1000})))


# ====================================================================== L3: drift

def _rep(outcome="OK", reason=None, clicks=100, impr=1000, start="2026-09-01", end="2026-09-28", days=28):
    sec = {"property": PROP, "window": {"start": start, "end": end, "days": days, "data_state": "final",
                                        "search_type": "web"},
           "totals": {"clicks": clicks, "impressions": impr, "ctr": None, "position": None},
           "versions": {"search_performance": sp.VERSION}, "by_date": [], "by_query_class": {}}
    return {"search_performance": {"outcome": outcome, "reason": reason, "section": sec if outcome == "OK" else None}}


def test_l3_not_connected_is_not_a_tool_failure():
    from agents import drift
    c = drift.compare_search_performance
    for nc in (_rep("NOT_CONNECTED", "GSC_NOT_CONNECTED"), _rep("NOT_CONNECTED", "GSC_PROPERTY_UNBOUND"), {}):
        r = c(_rep(), nc) if nc else c(nc, _rep())
        assert r["metrics"]["clicks"]["class"] == "NOT_CONNECTED" and r["metrics"]["clicks"]["evidence"]["rule"] == "SP0"
    for bad in (_rep("FAILED", "GSC_UNAVAILABLE"), _rep("KILLED", "GSC_KILLED"), _rep("BLOCKED", "GSC_BUDGET_EXHAUSTED")):
        r = c(_rep(), bad)
        assert r["metrics"]["clicks"]["class"] == "TOOL_FAILURE" and r["metrics"]["clicks"]["evidence"]["rule"] == "SP1"


def test_l3_overlapping_windows_compare_only_the_fresh_days():
    from agents import drift
    c = drift.compare_search_performance
    # 28-day windows a week apart: 21 shared days, 7 fresh. 100 -> 120 clicks is inside the naive band
    # 2*sqrt(220) = 29.7, but the difference comes from 7 fresh days only: the band 2*sqrt(220 * 7/28) = 14.8 calls it
    # REAL_CHANGE. 100 -> 110 stays SAMPLING_NOISE.
    wk = {"start": "2026-09-08", "end": "2026-10-05"}
    r = c(_rep(clicks=100), _rep(clicks=120, **wk))
    m = r["metrics"]["clicks"]
    assert r["windows_overlap_days"] == 21 and m["evidence"]["fresh_days"] == 7
    assert m["class"] == "REAL_CHANGE" and m["evidence"]["band"] == round(2 * (220 * 7 / 28) ** 0.5, 2)
    assert c(_rep(clicks=100), _rep(clicks=110, **wk))["metrics"]["clicks"]["class"] == "SAMPLING_NOISE"
    # a daily schedule: one fresh day is not compared at all
    d1 = c(_rep(), _rep(clicks=500, start="2026-09-02", end="2026-09-29"))["metrics"]["clicks"]
    assert d1["class"] == "UNKNOWN" and d1["evidence"]["fresh_days"] == 1
    # disjoint windows: the plain band
    far = c(_rep(clicks=100), _rep(clicks=120, start="2026-09-29", end="2026-10-26"))["metrics"]["clicks"]
    assert far["class"] == "SAMPLING_NOISE" and far["evidence"]["band"] == round(2 * 220 ** 0.5, 2)


def test_l3_api_and_scheduler_classify_the_same_pair_the_same_way_after_expiry(tmp_path):
    from agents import drift
    fake = FakeGoogle()
    h = _setup(tmp_path, fake, SEO_GSC_RETENTION_DAYS="1")
    a1 = audit(h, "acme", "acme.test")["audit_id"]
    h.clock.advance(days=8)
    a2 = audit(h, "acme", "acme.test")["audit_id"]
    with h.svc.lock:                                    # what the scheduler compares (raw records, retention applied)
        raw = [h.svc.gsc_retained(h.svc.audits[x]["report"], h.svc.audits[x]["completed_at"]) for x in (a1, a2)]
    sched = drift.compare(*raw)["search_performance"]
    api = h.ok(h.get(f"/tenants/acme/audits/{a2}/drift", params={"against": a1}))["search_performance"]
    assert raw[0]["search_performance"]["section"]["expired"] is True       # the older one has expired
    assert api == sched
    assert api["metrics"]["clicks"]["class"] == "SAMPLING_NOISE"            # totals still compared after expiry
    assert api["windows_overlap_days"] == 20


def test_l3_scheduler_drift_uses_the_retained_view(tmp_path):
    fake = FakeGoogle()
    h = _setup(tmp_path, fake, SEO_GSC_RETENTION_DAYS="1")
    sch = h.ok(h.post("/tenants/acme/schedules", {"request_id": rid(), "domain": "acme.test", "paths": ["/"],
                                                   "every_days": 7}), 201)
    h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    h.clock.advance(days=7, minutes=1)
    tick = h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    assert tick["drift_recorded"] == 1
    sv = h.ok(h.get(f"/tenants/acme/schedules/{sch['schedule_id']}"))
    d = sv["drifts"][-1]
    sched = d["drift"]["search_performance"]
    api = h.ok(h.get(f"/tenants/acme/audits/{d['newer']}/drift", params={"against": d["older"]}))
    assert api["search_performance"] == sched and sched["metrics"]["clicks"]["class"] == "SAMPLING_NOISE"


# ====================================================================== L4: the PII digit rule

@pytest.mark.parametrize("q", ["iphone 15 pro 256gb 2024", "best laptops 2025 under 1000", "24/7 plumber 90210",
                               "covid 19 2020 2021", "2020-2021 tax rates"])
def test_l4_digits_in_separate_words_are_not_added_up(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect"


@pytest.mark.parametrize("q", ["555 010 0199", "(555) 010-0199", "+44 20 7946 0958", "555-0199", "555.010.0199",
                               "５５５０１００１９９", "0161 496 0000", "07700 900123", "ssn 123-45-6789", "123 45 6789",
                               "555 010 2024", "acme 5550100199", "4111 1111 1111 1111", "order 1234567",
                               "a1b2c3d4e5f6g7", "jane doe at gmail dot com"])
def test_l4_real_phone_numbers_and_identifiers_are_still_caught(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect"


def test_l4_longest_digit_run():
    assert sp.longest_digit_run("call (555) 010-0199 now") == 10
    assert sp.longest_digit_run("iphone 15 pro 256gb 2024") == 4
    assert sp.longest_digit_run("covid 19 2020 2021") == 2


# ====================================================================== L5: the request budget covers the page caps

def test_l5_startup_refuses_page_caps_the_request_budget_cannot_serve():
    import config as config_mod
    from helpers import base_env
    with pytest.raises(RuntimeError) as ei:
        config_mod.load(base_env(SEO_GSC_MAX_PAGES="40", SEO_GSC_PAGE_ROWS="1000", SEO_GSC_MAX_REQUESTS="30"))
    assert "83" in str(ei.value) and "SEO_GSC_MAX_REQUESTS" in str(ei.value)
    s = config_mod.load(base_env(SEO_GSC_MAX_PAGES="40", SEO_GSC_PAGE_ROWS="1000", SEO_GSC_MAX_REQUESTS="83"))
    assert s.gsc_max_requests == 83
    d = config_mod.load(base_env())                     # the defaults: 10 pages each, 23 of 30 requests
    assert config_mod.gsc_requests_needed(d.gsc_max_pages, d.gsc_page_rows, d.gsc_max_rows) == 23
    assert config_mod.gsc_requests_needed(40, 5000, 50_000) == 23     # max_rows bounds the pages too


def test_l5_at_the_largest_accepted_caps_a_read_ends_truncated_not_over_budget():
    import config as config_mod
    from helpers import base_env
    s = config_mod.load(base_env(SEO_GSC_MAX_PAGES="40", SEO_GSC_PAGE_ROWS="100", SEO_GSC_MAX_ROWS="4000",
                                 SEO_GSC_MAX_REQUESTS="83"))
    fake = FakeGoogle()
    fake.queries = [(f"q{i}", 1, 10, 3.0) for i in range(5000)]
    fake.pages = [(f"https://acme.test/p{i}", 1, 10, 3.0) for i in range(5000)]
    rd = client(fake, page_rows=s.gsc_page_rows, max_pages=s.gsc_max_pages, max_rows=s.gsc_max_rows,
                max_requests=s.gsc_max_requests).open_read(lambda: None)
    out = sp.read_aggregates(rd, PROP, *W, {"acme.test"}, ["acme"])
    assert out["by_page_template"]["truncated"] and out["by_query_class"]["truncated"] and rd.requests == 83
