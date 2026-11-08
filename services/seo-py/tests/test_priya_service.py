"""Priya's keyword ledger in the service (svc_keywords.py; ADR 0047): NOT_CONNECTED / INCONCLUSIVE by default with no
outbound call, record-first, the ledger handed out once and never stored, the shared Search Console read budget, kill
switches before and during a run, client-tenant gates, tenant isolation, Google errors and partial pages, sampling
and truncation flags, Bing (injected fake) kept apart, replay, the department manager, restart. No network."""

from __future__ import annotations

import json

import httpx
import pytest
from bing_fakes import FakeBing
from gsc_fakes import FakeGoogle, pem_fragments
from helpers import rid
from test_gsc import PROP, bind, gsc_harness

from agents import priya
from envelope import sha

QUERIES = [("rug cleaner", 30, 1200, 0.025, 3.2), ("carpet cleaner", 20, 900, 0.022, 3.4),
           ("sofa cleaner", 6, 300, 0.02, 3.0), ("acme pricing", 40, 200, 0.2, 1.1),
           ("rug cleaner near me", 2, 12, 0.16, 4.0), ("carpet cleaner near me", 1, 8, 0.12, 5.0),
           ("jane.doe@example.com", 1, 3, 0.33, 1.0), ("東京 ホテル 安い", 1, 25, 0.04, 6.0)]
SECRET_TEXTS = ("rug cleaner", "carpet cleaner", "jane.doe", "東京", "acme pricing")


def fake_google():
    f = FakeGoogle()
    f.queries = [(q, c, i, p) for q, c, i, _ctr, p in QUERIES]
    f.dates_clicks = (10, 150)                    # 28 days: 280 clicks, 4,200 impressions (> the query rows)
    return f


def setup(tmp_path, data_dir=None, **env):
    fake = fake_google()
    h = gsc_harness(tmp_path, fake, data_dir=data_dir, **env)
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    return h, fake


def run(h, tid="acme", body=None, code=201, andre=False, caller="seo_agent", **kw):
    b = {"request_id": rid(), **(body or {})}
    r = h.post(f"/tenants/{tid}/keyword-ledger", b, caller=caller, andre=andre, **kw)
    return h.ok(r, code) if code < 400 else h.refused(r, code)


def every_stored_byte(h, tmp_path) -> str:
    blob = ""
    for p in (tmp_path / "data").rglob("*"):
        if p.is_file():
            blob += p.read_bytes().decode("utf-8", "replace")
    blob += json.dumps([e["_payload"] for e in h.ledger.events], ensure_ascii=False)
    blob += json.dumps([e["summary"] for e in h.ledger.events], ensure_ascii=False)
    blob += json.dumps(h.ok(h.get("/audit/export", params={"limit": 1000})), ensure_ascii=False)
    return blob


def test_not_connected_by_default_is_inconclusive_with_no_outbound_call(h):
    h.tenant("acme", "own", ("acme.test",))
    out = run(h)
    led = out["ledger"]
    assert led["status"] == "INCONCLUSIVE" and led["rows"] == []
    assert led["sources"]["search_console"]["state"] == "GSC_NOT_CONNECTED"
    assert out["envelope"]["outcome"] == "NOT_CONNECTED" and out["envelope"]["findings"] == []
    assert "first_party:search_console" in out["envelope"]["not_connected"]
    assert "keyword_planner:google_ads" in out["envelope"]["not_connected"]
    assert out["gsc_read"] is False and h.svc.gsc_reads.get("acme") is None      # no read budget spent
    assert h.ledger.of_type("keyword_ledger_started") and h.ledger.of_type("keyword_ledger_recorded")
    assert out["agent_card"]["agent"] == "priya" and out["ledger_retained"] is False


def test_full_run_is_record_first_handed_out_once_and_never_stored(tmp_path):
    seen = []
    h, fake = setup(tmp_path, data_dir=str(tmp_path / "data"))
    fake.on_request = lambda fk, req: seen.append(len(h.ledger.of_type("keyword_ledger_started")))
    out = run(h)
    assert seen and min(seen) == 1                                   # recorded before the first request
    led = out["ledger"]
    assert led["status"] == "OK" and out["envelope"]["outcome"] == "OK"
    t = {r["query"]["text"]: r for r in led["rows"]}
    assert t["rug cleaner"]["tier"] == "ESTABLISHED" and t["rug cleaner near me"]["tier"] == "EMERGING"
    assert "東京 ホテル 安い" in t and "jane.doe@example.com" not in t
    assert led["counts"]["pii_suspect_dropped"]["queries"] == 1
    paths = {r["path"] for r in fake.api_requests() if r["path"].endswith("/query")}
    assert paths == {"/webmasters/v3/sites/sc-domain%3Aacme.test/searchAnalytics/query"}
    # recorded: the ledger's hash and counts, never the ledger
    rec = h.ledger.of_type("keyword_ledger_recorded")[-1]["_payload"]
    assert rec["ledger_sha256"] == sha(led) and rec["status"] == "OK"
    blob = every_stored_byte(h, tmp_path)
    for s in SECRET_TEXTS:
        assert s not in blob, s
    assert not any(f in blob for f in pem_fragments()) and fake.tokens[0] not in blob
    # the list view shows the run without rows
    runs = h.ok(h.get("/tenants/acme/keyword-ledger-runs"))
    assert [r["run_id"] for r in runs] == [out["run_id"]] and "ledger" not in runs[0]
    assert runs[0]["ledger_sha256"] == sha(led) and runs[0]["status"] == "completed"


def test_replay_answers_the_summary_without_the_ledger_and_a_changed_body_conflicts(tmp_path):
    h, fake = setup(tmp_path)
    body = {"request_id": rid()}
    first = h.ok(h.post("/tenants/acme/keyword-ledger", body), 201)
    n = len(fake.requests)
    again = h.ok(h.post("/tenants/acme/keyword-ledger", body), 201)
    assert again["replay"] is True and again["ledger"] is None and again["run_id"] == first["run_id"]
    assert len(fake.requests) == n                                    # no second read
    h.refused(h.post("/tenants/acme/keyword-ledger", {**body, "sources": ["gsc", "bing"],
                                                      "bing_site_domain": "acme.test"}), 409, "REQUEST_ID_REUSED")


def test_the_read_budget_is_shared_with_audits(tmp_path):
    h, fake = setup(tmp_path, SEO_GSC_READS_PER_DAY="1")
    assert run(h)["ledger"]["status"] == "OK"
    n = len(fake.requests)
    out = run(h)
    assert out["ledger"]["sources"]["search_console"]["state"] == "GSC_BUDGET_EXHAUSTED"
    assert out["ledger"]["status"] == "INCONCLUSIVE" and out["envelope"]["outcome"] == "BLOCKED"
    assert len(fake.requests) == n
    a = h.ok(h.post("/tenants/acme/audits", {"request_id": rid(), "domain": "acme.test", "paths": ["/"]},
                    andre=True), 201)
    sp = next(e for e in a["report"]["agents"] if e["task"] == "search_performance")
    assert sp["reason"] == "GSC_BUDGET_EXHAUSTED"                     # the audit sees the same budget


def test_kill_switches_before_the_run(tmp_path):
    h, fake = setup(tmp_path)
    h.ok(h.switch("capability:keywords"))
    run(h, code=403)
    h.ok(h.switch("capability:keywords", engaged=False, andre=True))
    h.ok(h.switch("provider:gsc"))
    out = run(h)
    assert out["ledger"]["sources"]["search_console"]["state"] == "GSC_KILLED" and fake.requests == []
    assert out["envelope"]["outcome"] == "KILLED" and out["gsc_read"] is False
    h.ok(h.switch("provider:gsc", engaged=False, andre=True))
    h.ok(h.switch("tenant:acme"))
    run(h, code=403)


def test_a_switch_engaged_mid_read_interrupts_the_run_and_discards_the_ledger(tmp_path):
    h, fake = setup(tmp_path)

    def on(fk, req):
        if req.url.raw_path.decode().endswith("/query") and not h.svc.switches["capability"].get("keywords"):
            h.svc.switches["capability"]["keywords"] = True        # as an engaged switch is held in memory
    fake.on_request = on
    out = run(h)
    assert out["status"] == "interrupted" and out["interrupted_reason"] == "KILLED_CAPABILITY"
    assert out["ledger"] is None and not h.ledger.of_type("keyword_ledger_recorded")
    assert h.ledger.of_type("keyword_ledger_interrupted")
    assert len([r for r in fake.api_requests() if r["path"].endswith("/query")]) == 1   # stopped at the next request


def test_no_record_no_request(tmp_path):
    h, fake = setup(tmp_path)
    h.ledger.fail = True
    h.refused(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 503, "LEDGER_UNAVAILABLE")
    assert fake.requests == []


def test_client_tenants_need_andre_a_finance_client_and_compliance(tmp_path):
    fake = fake_google()
    h = gsc_harness(tmp_path, fake)
    h.tenant("globex", "client", ("globex.test",))
    fake.sites = [{"siteUrl": "sc-domain:globex.test", "permissionLevel": "siteRestrictedUser"}]
    h.ok(bind(h, "globex", "sc-domain:globex.test"))
    run(h, "globex", code=403)                                         # ANDRE_APPROVAL_REQUIRED
    assert run(h, "globex", code=409, andre=True)["detail"] == "TENANT_FINANCE_CLIENT_UNBOUND"
    h.ok(h.post("/tenants/globex/finance-client", {"request_id": rid(), "finance_client_id": "cl-globex"},
                andre=True))
    h.ports.compliance.refuse.add("cl-globex")
    assert run(h, "globex", code=409, andre=True)["detail"] == "COMPLIANCE_NOT_ACTIVATED"
    assert fake.requests == []
    h.ports.compliance.refuse.clear()
    out = run(h, "globex", andre=True)
    assert out["ledger"]["status"] == "OK" and out["andre_approved"] is True
    assert h.ledger.of_type("keyword_ledger_approved_by_andre")


def test_tenant_isolation_hub_cannot_run_and_other_tenants_see_404(tmp_path):
    h, fake = setup(tmp_path)
    h.tenant("globex", "own", ("globex.test",))
    out = run(h)
    h.refused(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}, caller="hub", tenant="acme"), 403,
              "CALLER_NOT_ALLOWED")
    h.refused(h.get("/tenants/acme/keyword-ledger-runs", caller="hub", tenant="acme"), 403)
    assert h.ok(h.get("/tenants/globex/keyword-ledger-runs")) == []
    with pytest.raises(Exception) as ei:
        h.svc.keyword_run_view("globex", out["run_id"])
    assert getattr(ei.value, "reason", None) == "KEYWORD_RUN_NOT_FOUND"
    # globex has no binding: its run reads nothing, and never acme's property
    n = len(fake.requests)
    g = run(h, "globex")
    assert g["ledger"]["sources"]["search_console"]["state"] == "GSC_PROPERTY_UNBOUND" and len(fake.requests) == n
    # a request cannot name a property
    h.refused(h.post("/tenants/acme/keyword-ledger", {"request_id": rid(), "property": "sc-domain:globex.test"}), 422)


def test_a_failed_page_mid_pagination_fails_the_source_never_half_reads_it(tmp_path):
    h, fake = setup(tmp_path, SEO_GSC_PAGE_ROWS="100")
    fake.queries = [(f"term {i} cleaner", 1, 30, 5.0) for i in range(150)]
    calls = {"n": 0}

    def on(fk, req):
        if req.url.raw_path.decode().endswith("/query") and b'"query"' in req.read():
            calls["n"] += 1
            if calls["n"] >= 2:                                        # every attempt at page 2 fails
                fk.script.append((500, {"error": {"code": 500, "message": "x"}}))
    fake.on_request = on
    out = run(h)
    gs = out["ledger"]["sources"]["search_console"]
    assert gs["state"] == "GSC_UNAVAILABLE" and gs["rows_received"] == 0
    assert out["ledger"]["status"] == "INCONCLUSIVE" and out["ledger"]["rows"] == []
    assert out["envelope"]["outcome"] == "FAILED"


@pytest.mark.parametrize("answer,state", [
    ((403, {"error": {"code": 403, "message": "x", "errors": [{"reason": "accessNotConfigured"}]}}),
     "GSC_AUTH_REFUSED"),
    ((200, {"rows": [{"keys": ["a", "b"], "clicks": 1, "impressions": 2}], "responseAggregationType": "byProperty"}),
     "GSC_PROTOCOL_ERROR"),
    ((200, b"not json"), "GSC_PROTOCOL_ERROR"),
])
def test_google_errors_reach_the_ledger_as_states_not_500s(tmp_path, answer, state):
    h, fake = setup(tmp_path)
    fake.script = [(200, {"siteEntry": fake.sites}), answer]
    out = run(h)
    assert out["ledger"]["sources"]["search_console"]["state"] == state
    assert out["ledger"]["status"] == "INCONCLUSIVE"


def test_sampling_remainder_and_truncation_are_flagged(tmp_path):
    h, fake = setup(tmp_path, SEO_GSC_PAGE_ROWS="100", SEO_GSC_MAX_PAGES="1")
    fake.queries = [(f"term {i} cleaner", 1, 30, 5.0) for i in range(150)]
    out = run(h)
    gs = out["ledger"]["sources"]["search_console"]
    assert gs["truncated"] is True and gs["rows_read"] == 100
    assert gs["totals"]["impressions"] == 4200 and gs["query_rows_sum"]["impressions"] == 3000
    assert gs["unattributed"]["impressions"] == 1200
    flags = out["ledger"]["rows"][0]["uncertainty_flags"]
    assert "GSC_ROWS_TRUNCATED" in flags and "ANONYMISED_REMAINDER" in flags


def test_bing_through_an_injected_port_is_kept_apart_and_its_failures_are_partial(tmp_path):
    h, fake = setup(tmp_path)
    bing = FakeBing()
    h.ports.first_party["bing_webmaster"] = bing.client()
    body = {"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"}
    out = run(h, body=body)
    led = out["ledger"]
    assert led["status"] == "OK" and led["sources"]["bing_webmaster"]["state"] == "OK"
    r = next(x for x in led["rows"] if x["query"]["text"] == "rug cleaner")
    assert r["metrics"]["bing_webmaster"]["impressions"] == 100
    assert r["metrics"]["search_console"]["impressions"] == 1200          # never summed
    assert bing.requests[0]["query"]["siteUrl"] == "https://acme.test/"
    blob = json.dumps([e["_payload"] for e in h.ledger.events])
    assert bing.requests[0]["query"]["apikey"] not in blob and "acme.test/" not in blob
    bing.script = [(500, {})] * 3
    out = run(h, body=body)
    assert out["ledger"]["status"] == "PARTIAL" and out["envelope"]["outcome"] == "PARTIAL"
    assert out["ledger"]["sources"]["bing_webmaster"]["state"] == "BING_UNAVAILABLE"
    n = len(bing.requests)
    h.ok(h.switch("provider:bing"))
    out = run(h, body=body)
    assert out["ledger"]["sources"]["bing_webmaster"]["state"] == "BING_KILLED" and len(bing.requests) == n
    run(h, body={"sources": ["gsc", "bing"], "bing_site_domain": "other.test"}, code=403)
    run(h, body={"sources": ["bing"]}, code=422)
    run(h, body={"sources": ["gsc", "gsc"]}, code=422)
    run(h, body={"sources": []}, code=422)


def test_bing_not_connected_in_production_wiring(h):
    h.tenant("acme", "own", ("acme.test",))
    out = run(h, body={"sources": ["bing"], "bing_site_domain": "acme.test"})
    assert out["ledger"]["sources"]["bing_webmaster"]["state"] == "BING_NOT_CONNECTED"
    assert out["ledger"]["status"] == "INCONCLUSIVE" and out["envelope"]["outcome"] == "NOT_CONNECTED"


def test_a_crash_in_the_agent_closes_the_run_as_interrupted(tmp_path, monkeypatch):
    h, fake = setup(tmp_path)

    def boom(*a, **k):
        raise ZeroDivisionError("rug cleaner")                          # text that must not be logged or recorded
    monkeypatch.setattr(priya, "build", boom)
    out = run(h)
    assert out["status"] == "interrupted" and out["interrupted_reason"] == "RUN_FAILED"
    assert out["error_type"] == "ZeroDivisionError" and out["ledger"] is None
    assert "rug cleaner" not in json.dumps([e["_payload"] for e in h.ledger.events])


def test_the_department_manager_lists_and_can_restrict_priya(tmp_path):
    h, fake = setup(tmp_path)
    run(h)
    d = h.ok(h.get("/department"))["agents"]["priya"]
    assert d["state"] == "active" and d["scorecard"]["keyword_runs"]["runs"] == 1
    assert d["scorecard"]["keyword_runs"]["outcomes"] == {"OK": 1}
    h.ok(h.post("/agents/priya/lifecycle", {"request_id": rid(), "to": "restricted", "expected_state": "active",
                                            "reason": "FOUNDER_DECISION"}, caller="dashboard", andre=True))
    n = len(fake.requests)
    assert run(h, code=403)["detail"] == "AGENT_RESTRICTED" and len(fake.requests) == n


def test_runs_and_the_budget_survive_a_restart(tmp_path):
    h, fake = setup(tmp_path, data_dir=str(tmp_path / "data"), SEO_GSC_READS_PER_DAY="1")
    out = run(h)
    h2 = h.restart(SEO_GSC_READS_PER_DAY="1")
    runs = h2.ok(h2.get("/tenants/acme/keyword-ledger-runs"))
    assert runs[0]["run_id"] == out["run_id"] and runs[0]["ledger_sha256"] == out["ledger_sha256"]
    again = h2.ok(h2.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 201)
    assert again["ledger"]["sources"]["search_console"]["state"] == "GSC_BUDGET_EXHAUSTED"


def test_an_unexpected_transport_failure_in_bing_is_a_source_failure(tmp_path):
    h, fake = setup(tmp_path)

    class Broken:
        connected = True

        def query_stats(self, site, guard):
            raise httpx.DecodingError("x")
    h.ports.first_party["bing_webmaster"] = Broken()
    out = run(h, body={"sources": ["gsc", "bing"], "bing_site_domain": "acme.test"})
    assert out["ledger"]["sources"]["bing_webmaster"]["state"] == "BING_PROTOCOL_ERROR"
    assert out["ledger"]["status"] == "PARTIAL"
