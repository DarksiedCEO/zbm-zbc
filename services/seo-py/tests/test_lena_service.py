"""Lena in the service (ADR 0050): OFF by default with no outbound call, start refused when enabled without a key
file, the record-first read inside an audit, budgets (per run, per day; 0 = OFF), the provider:crux kill switch, the
department manager's restriction, tenant isolation on the page-experience route, the overall audit outcome left
alone, the key never logged or recorded, and the client report's area. No network: a fake CrUX."""

from __future__ import annotations

import json
import logging
import os

import pytest
from crux_fakes import TEST_KEY, FakeCrux, write_key_file
from helpers import Harness, base_env, rid

import client_report
import config as config_mod
from ports import Ports, field_data_from_settings


def crux_harness(tmp_path, fake: FakeCrux, calls_per_run="40", calls_per_day="200", data_dir=None,
                 tenant_per_day="200", **env):
    keyfile = write_key_file(tmp_path / "crux.key")
    full = {"SEO_CRUX_ENABLED": "1", "SEO_CRUX_API_KEY_FILE": keyfile, "SEO_CRUX_CALLS_PER_RUN": calls_per_run,
            "SEO_CRUX_CALLS_PER_DAY": calls_per_day, "SEO_CRUX_TENANT_CALLS_PER_DAY": tenant_per_day, **env}
    settings = config_mod.load(base_env(**full))
    ports = Ports.default(settings)
    ports.fetcher = None                                       # no crawling here: Lena's section only
    c = fake.clock
    ports.field_data = field_data_from_settings(settings, transport=fake.transport(), monotonic=c.mono,
                                                sleep=c.sleep)
    h = Harness(tmp_path, data_dir=data_dir, ports=ports, **full)
    h.crux_env = full
    return h


def audit(h, tid, domain, paths=("/",)):
    body = {"request_id": rid(), "domain": domain, "paths": list(paths)}
    return h.ok(h.post(f"/tenants/{tid}/audits", body, andre=True), 201)


def section(view):
    return next(e for e in view["report"]["agents"] if e["agent"] == "lena" and e["task"] == "page_experience")


# ====================================================================== off by default; start rules

def test_not_connected_by_default_with_no_outbound_call(h):
    h.ports.fetcher = None
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert (e["outcome"], e["reason"], e["facts"]["calls"], e["findings"]) == ("NOT_CONNECTED", "CRUX_NOT_CONNECTED",
                                                                               0, [])
    assert view["report"]["page_experience"] == {"outcome": "NOT_CONNECTED", "reason": "CRUX_NOT_CONNECTED",
                                                 "read_started": False, "section": None}
    assert "field_data:crux" in view["report"]["not_connected"]
    st = h.ok(h.get("/status"))
    assert st["page_experience"]["state"] == "NOT_CONNECTED" and st["page_experience"]["enabled"] is False
    assert st["page_experience"]["calls_per_run"] == 0 and st["page_experience"]["calls_per_day"] == 0
    assert st["page_experience"]["tenant_calls_per_day"] == 0
    assert st["page_experience"]["pacing"]["queries_per_minute_max"] == 100
    assert st["ports"]["field_data"] == "NOT_CONNECTED" and st["kill_switches"]["providers"]["crux"] is False
    assert not h.ledger.of_type("crux_read_started")
    pe = h.ok(h.get(f"/tenants/acme/audits/{view['audit_id']}/page-experience"))
    assert pe["outcome"] == "NOT_CONNECTED" and pe["page_experience"]["section"] is None


def test_enabled_without_a_key_file_refuses_start(tmp_path):
    with pytest.raises(RuntimeError, match="SEO_CRUX_API_KEY_FILE"):
        config_mod.load(base_env(SEO_CRUX_ENABLED="1"))
    for name, mode, value in (("open.key", 0o644, TEST_KEY), ("bad.key", 0o600, "not a key!")):
        p = write_key_file(tmp_path / name, value=value, mode=mode)
        with pytest.raises(RuntimeError) as e:
            config_mod.load(base_env(SEO_CRUX_ENABLED="1", SEO_CRUX_API_KEY_FILE=p))
        assert "not a key" not in str(e.value) and TEST_KEY not in str(e.value)
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SEO_CRUX_ENABLED="1", SEO_CRUX_API_KEY_FILE=str(tmp_path / "missing.key")))
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SEO_CRUX_ENABLED="yes"))
    for name, bad in (("SEO_CRUX_CALLS_PER_RUN", "121"), ("SEO_CRUX_HISTORY_PERIODS", "41"),
                      ("SEO_CRUX_TENANT_CALLS_PER_DAY", "20001"), ("SEO_CRUX_TENANT_CALLS_PER_DAY", "-1"),
                      ("SEO_CRUX_DEADLINE_SECONDS", "1")):
        with pytest.raises(RuntimeError, match=name):
            config_mod.load(base_env(**{name: bad}))


def test_disabled_does_not_read_the_key_file_and_stays_not_connected(tmp_path):
    p = write_key_file(tmp_path / "open.key", mode=0o644)          # would refuse start if it were read
    s = config_mod.load(base_env(SEO_CRUX_API_KEY_FILE=p))
    assert s.crux_key is None and not field_data_from_settings(s).connected
    s = config_mod.load(base_env(SEO_CRUX_ENABLED="1", SEO_CRUX_API_KEY_FILE=write_key_file(tmp_path / "k")))
    assert field_data_from_settings(s).connected and TEST_KEY not in repr(s)


# ====================================================================== the read

def test_full_read_is_record_first_and_lives_outside_the_overall_outcome(tmp_path):
    fake = FakeCrux()
    seen = []
    h = None

    def on(fk, req):
        seen.append(len(h.ledger.of_type("crux_read_started")))
    fake.on_request = on
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    view = audit(h, "acme", "acme.test", paths=("/", "/pricing"))
    e = section(view)
    assert view["status"] == "completed" and (e["outcome"], e["reason"]) == ("OK", None) and e["findings"] == []
    assert seen and all(n == 1 for n in seen)                     # recorded before the first request left
    sec = view["report"]["page_experience"]["section"]
    assert sec["calls"] == len(fake.requests) == 18 and [t["kind"] for t in sec["targets"]] == ["origin", "url", "url"]
    started = h.ledger.of_type("crux_read_started")[0]["_payload"]
    assert started["calls_reserved"] == 40 and "origin" not in started and len(started["origin_sha256"]) == 64
    rec = h.ledger.of_type("crux_read_recorded")[0]["_payload"]
    assert rec["calls"] == 18 and rec["outcome"] == "OK" and rec["counts"]["with_data"] == 18
    # Lena's outcome never relabels the audit
    from svc_audits import overall_outcome
    assert view["report"]["outcome"] == overall_outcome([x for x in view["report"]["agents"]
                                                         if x["agent"] != "lena"])
    pe = h.ok(h.get(f"/tenants/acme/audits/{view['audit_id']}/page-experience", caller="seo_agent"))
    assert pe["page_experience"]["section"] == sec and pe["outcome"] == "OK"
    assert h.ok(h.get("/status"))["page_experience"]["calls_used_last_24h"] == 18


def test_origin_without_field_data_is_insufficient_not_fast_or_slow(tmp_path):
    fake = FakeCrux()
    fake.missing |= {("origin", "https://acme.test"), ("url", "https://acme.test/")}
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert (e["outcome"], e["reason"]) == ("INSUFFICIENT_EVIDENCE", "CRUX_INSUFFICIENT_FIELD_DATA")
    blob = json.dumps(view["report"]["page_experience"])
    assert '"rating"' not in blob and "GOOD" not in blob and "POOR" not in blob and len(fake.requests) == 3


def test_budgets_default_off_and_the_daily_cap_spans_audits(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake, calls_per_run="0", calls_per_day="0", tenant_per_day="0")
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("BLOCKED", "CRUX_BUDGET_EXHAUSTED") and fake.requests == []
    d = tmp_path / "daily"
    d.mkdir()
    fake = FakeCrux()
    h = crux_harness(d, fake, calls_per_run="12", calls_per_day="20")
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    h.tenant("beta", "own", ("beta.test", "www.beta.test"))
    first = section(audit(h, "acme", "acme.test"))
    assert first["outcome"] == "OK" and len(fake.requests) == 12
    second = section(audit(h, "beta", "beta.test"))                  # 8 left today, across tenants
    assert (second["outcome"], second["reason"]) == ("PARTIAL", "CRUX_BUDGET_EXHAUSTED") and len(fake.requests) == 20
    third = section(audit(h, "acme", "acme.test"))
    assert (third["outcome"], third["reason"]) == ("BLOCKED", "CRUX_BUDGET_EXHAUSTED") and len(fake.requests) == 20
    h.clock.advance(days=1, minutes=1)
    assert section(audit(h, "acme", "acme.test"))["outcome"] == "OK"


def test_the_daily_budget_survives_a_restart(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake, calls_per_run="12", calls_per_day="12", data_dir=str(tmp_path / "data"))
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    assert section(audit(h, "acme", "acme.test"))["outcome"] == "OK" and len(fake.requests) == 12
    h2 = h.restart(**h.crux_env)
    assert h2.ok(h2.get("/status"))["page_experience"]["calls_used_last_24h"] == 12
    e = section(audit(h2, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("BLOCKED", "CRUX_BUDGET_EXHAUSTED") and len(fake.requests) == 12


def test_kill_switch_crux_before_and_during_a_read(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    h.ok(h.switch("provider:crux"))
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"], e["facts"]["switch"]) == ("KILLED", "CRUX_KILLED", "KILLED_PROVIDER")
    assert fake.requests == [] and not h.ledger.of_type("crux_read_started")
    assert h.ok(h.get("/status"))["page_experience"]["killed"] is True
    h.ok(h.switch("provider:crux", engaged=False, andre=True))

    def on(fk, req):
        if len(fk.requests) == 3:
            h.svc.switches["provider"]["crux"] = True             # as an engage would, mid-read
    fake.on_request = on
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert (e["outcome"], e["reason"]) == ("KILLED", "CRUX_KILLED") and len(fake.requests) == 3
    assert view["status"] == "completed"
    d = tmp_path / "env"
    d.mkdir()
    h2 = crux_harness(d, FakeCrux(), SEO_KILLED_PROVIDERS="crux")
    h2.tenant("acme", "own", ("acme.test", "www.acme.test"))
    assert section(audit(h2, "acme", "acme.test"))["reason"] == "CRUX_KILLED"


def test_no_record_no_request(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    orig = h.svc._commit

    def failing(kind, *a, **k):
        if kind == "crux_read_started":
            from errors import Unavailable
            raise Unavailable("LEDGER_UNAVAILABLE")
        return orig(kind, *a, **k)
    h.svc._commit = failing
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("BLOCKED", "LEDGER_UNAVAILABLE") and fake.requests == []


def test_errors_reach_the_report_as_outcomes_not_500s(tmp_path):
    for script, outcome, reason in (([(503, {})] * 3, "FAILED", "CRUX_UNAVAILABLE"),
                                    ([(429, {})] * 3, "FAILED", "CRUX_RATE_LIMITED"),
                                    ([(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}})], "BLOCKED",
                                     "CRUX_AUTH_REFUSED"),
                                    ([(200, b"<html>")] * 12, "FAILED", "CRUX_PROTOCOL_ERROR")):
        fake = FakeCrux()
        fake.script = list(script)
        d = tmp_path / reason
        d.mkdir()
        h = crux_harness(d, fake)
        h.tenant("acme", "own", ("acme.test", "www.acme.test"))
        view = audit(h, "acme", "acme.test")
        e = section(view)
        assert (view["status"], e["outcome"], e["reason"]) == ("completed", outcome, reason)
        assert h.ledger.of_type("crux_read_recorded")[0]["_payload"]["reason"] == reason


def test_an_unexpected_exception_fails_the_section_not_the_audit(tmp_path, monkeypatch):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    from agents import lena

    def boom(*a, **k):
        raise ZeroDivisionError("quoting " + TEST_KEY)
    monkeypatch.setattr(lena, "run", boom)
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert view["status"] == "completed" and (e["outcome"], e["reason"]) == ("FAILED", "CRUX_PROTOCOL_ERROR")
    assert e["facts"]["error_type"] == "ZeroDivisionError" and TEST_KEY not in json.dumps(view)


def test_the_key_is_never_logged_recorded_or_shown(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeCrux()
    fake.answers[("record", "origin", "https://acme.test", "DESKTOP")] = (
        400, {"error": {"code": 400, "message": "API key not valid: " + TEST_KEY, "status": "INVALID_ARGUMENT",
                        "details": [{"reason": "API_KEY_INVALID"}]}})
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    view = audit(h, "acme", "acme.test")
    assert section(view)["reason"] == "CRUX_AUTH_REFUSED"
    assert all(TEST_KEY not in str(r.url) for r in fake.requests)
    blobs = [json.dumps(view), json.dumps(h.ok(h.get("/status"))),
             json.dumps(h.ok(h.get("/audit/export", params={"limit": 1000}))),
             json.dumps(h.ledger.entries(), default=str), caplog.text,
             b"\n".join(h.svc.log._lines).decode("utf-8", "replace"),
             repr(h.settings), repr(h.ports.field_data)]
    for b in blobs:
        assert TEST_KEY not in b


def test_tenant_isolation_on_the_page_experience_route(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    h.tenant("beta", "own", ("beta.test", "www.beta.test"))
    aid = audit(h, "acme", "acme.test")["audit_id"]
    assert all(json.loads(r.content).get("origin", "https://acme.test").startswith("https://acme.test")
               for r in fake.requests)
    h.refused(h.get(f"/tenants/beta/audits/{aid}/page-experience"), 404, "AUDIT_NOT_FOUND")
    assert h.ok(h.get(f"/tenants/acme/audits/{aid}/page-experience", caller="hub", tenant="acme"))["outcome"] == "OK"
    h.refused(h.get(f"/tenants/acme/audits/{aid}/page-experience", caller="hub", tenant="globex"), 404)
    h.refused(h.get(f"/tenants/acme/audits/{aid}/page-experience", caller="finance_31"), 403)


def test_a_restricted_lena_does_not_run(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    h.ok(h.post("/agents/lena/lifecycle", {"request_id": rid(), "expected_state": "active", "to": "restricted",
                                           "reason": "QUALITY_REVIEW"}, caller="dashboard", andre=True))
    view = audit(h, "acme", "acme.test")
    e = section(view)
    assert (e["outcome"], e["reason"]) == ("RESTRICTED", "AGENT_RESTRICTED") and fake.requests == []
    assert view["report"]["page_experience"]["outcome"] == "RESTRICTED"
    card = h.ok(h.get("/department"))["agents"]["lena"]
    assert card["state"] == "restricted" and card["scorecard"]["restricted_runs"] == 1


# ====================================================================== the client report

def test_client_report_shows_the_area_with_values_and_omits_it_for_older_audits(tmp_path):
    fake = FakeCrux()
    fake.missing.add(("url", "https://acme.test/pricing"))
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    view = audit(h, "acme", "acme.test", paths=("/", "/pricing"))
    model = client_report.build(view, "acme")
    area = next(s for s in model["sections"] if s["key"] == "page_experience")
    assert area["status"]["label"] == "CHECKED" and area["groups"] == []
    rows = area["field"]["rows"]
    assert rows[0]["target"] == "whole site, acme.test (audited origin)" and rows[0]["values"][0] == "2,100 ms (good)"
    assert rows[0]["values"][2] == "0.05 (good)" and rows[0]["cwv"] == "passed"
    assert any(r["target"] == "/pricing" and r["cwv"] == "no field data for this exact address" for r in rows)
    md = client_report.render_markdown(model)
    html = client_report.render_html(model)
    for doc in (md, html):
        assert "Real-user page experience (Chrome UX Report)" in doc and "not a fast or a slow result" in doc
        assert TEST_KEY not in doc
    assert "Google field data for the 28 days up to 2026-10-07" in md
    old = json.loads(json.dumps(view))
    old["report"]["agents"] = [e for e in old["report"]["agents"] if e["agent"] != "lena"]
    old["report"].pop("page_experience")
    assert not any(s["key"] == "page_experience" for s in client_report.build(old, "acme")["sections"])


def test_client_report_says_not_connected_plainly(h):
    h.ports.fetcher = None
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    model = client_report.build(audit(h, "acme", "acme.test"), "acme")
    area = next(s for s in model["sections"] if s["key"] == "page_experience")
    assert area["status"]["label"] == "NOT CONNECTED" and area["field"] is None
    assert "Chrome UX Report (real-user page experience)." in model["not_connected"]
    assert "The Chrome UX Report is not connected" in client_report.render_markdown(model)
    assert os.environ.get("SEO_CRUX_ENABLED") is None


# ====================================================================== review fixes: twins, per-tenant cap, wording

def test_only_one_form_registered_asks_the_twin_and_uses_its_host_for_urls(tmp_path):
    fake = FakeCrux()
    fake.missing |= {("origin", "https://acme.test"), ("url", "https://acme.test/")}
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test",))                         # www.acme.test not registered
    view = audit(h, "acme", "acme.test")
    sec = view["report"]["page_experience"]["section"]
    assert sec["twin"] == "https://www.acme.test" and sec["url_origin"] == "https://www.acme.test"
    assert [(t["kind"], t["role"], t["target"]) for t in sec["targets"]] == [
        ("origin", "audited", "https://acme.test"), ("origin", "twin", "https://www.acme.test"),
        ("url", "twin", "https://www.acme.test/")]
    assert len(fake.requests) == 6 + 3 + 6 and section(view)["outcome"] == "OK"
    model = client_report.build(view, "acme")
    area = next(s for s in model["sections"] if s["key"] == "page_experience")
    labels = {r["target"]: r for r in area["field"]["rows"]}
    assert labels["whole site, acme.test (audited origin)"]["cwv"] == "no field data for this exact address"
    assert labels["whole site, www.acme.test (other form, not registered; shown separately)"]["cwv"] == "passed"
    assert "/ on www.acme.test" in labels
    assert "never combined" in client_report.render_markdown(model)


def test_both_forms_registered_asks_only_the_audited_origin(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    sec = audit(h, "acme", "acme.test")["report"]["page_experience"]["section"]
    assert sec["twin"] is None and all(not k[2].startswith("https://www.") for k in fake.keys())


def test_per_tenant_daily_cap(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake, calls_per_run="12", calls_per_day="200", tenant_per_day="12")
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    h.tenant("beta", "own", ("beta.test", "www.beta.test"))
    assert section(audit(h, "acme", "acme.test"))["outcome"] == "OK" and len(fake.requests) == 12
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"]) == ("BLOCKED", "CRUX_BUDGET_EXHAUSTED") and len(fake.requests) == 12
    assert e["facts"]["tenant_calls_left_today"] == 0
    assert section(audit(h, "beta", "beta.test"))["outcome"] == "OK" and len(fake.requests) == 24


def test_no_data_wording_names_the_exact_origin():
    assert client_report.REASON_TEXT["CRUX_INSUFFICIENT_FIELD_DATA"].startswith(
        "Google has no field data for this exact origin.")
    from agents import lena
    assert not any("too few visits" in x for x in lena.LIMITS)


def test_a_non_registrable_subdomain_gets_no_twin(tmp_path):
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("shop.acme.test",))
    sec = audit(h, "acme", "shop.acme.test")["report"]["page_experience"]["section"]
    assert sec["twin"] is None and not any("www." in k[2] for k in fake.keys())
    assert len([k for k in fake.keys() if k[1] == "origin" and k[0] == "record"]) == 3



def test_lena_runs_one_at_a_time_and_a_run_that_cannot_start_is_blocked_unrecorded(tmp_path, monkeypatch):
    import svc_crux

    class Busy:
        def acquire(self, timeout=None):
            assert timeout == 60.0                          # waits at most its own deadline
            return False

        def release(self):
            raise AssertionError("never acquired")
    fake = FakeCrux()
    h = crux_harness(tmp_path, fake)
    h.tenant("acme", "own", ("acme.test", "www.acme.test"))
    monkeypatch.setattr(svc_crux, "_RUN_LOCK", Busy())
    e = section(audit(h, "acme", "acme.test"))
    assert (e["outcome"], e["reason"], e["facts"]["state"]) == ("BLOCKED", "CRUX_DEADLINE", "BUSY")
    assert fake.requests == [] and not h.ledger.of_type("crux_read_started")
    monkeypatch.undo()
    assert section(audit(h, "acme", "acme.test"))["outcome"] == "OK" and svc_crux._RUN_LOCK.acquire(blocking=False)
    svc_crux._RUN_LOCK.release()                             # released after every run
