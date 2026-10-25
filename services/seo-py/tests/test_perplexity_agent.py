"""ADR 0017 W4-8: the Perplexity adapter on the Agent API (``POST /v1/agent``), the default protocol since Oct 24 2026.

The response fixtures in tests/fixtures/perplexity_agent/ are built from the documented shape (the API reference's
``ResponsesResponse``, docs.perplexity.ai/api-reference/agent-post, read Oct 24 2026); they are NOT captures of a real
response (no key, no network here). Their ``fetch_url_results`` item's inner fields are placeholders: the adapter
ignores that item type, and the test proves it is ignored. The fakes, the clock and the service harness come from
tests/test_perplexity.py (the Sonar suite, which now proves the rollback protocol)."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest

import perplexity as px
import test_perplexity as base
from agents import osei, probes
from clock import FixedClock
from helpers import T0, Harness, derived, rid
from primitives import Killed

KEY = base.KEY
BUDGET = base.BUDGET
FIX = Path(__file__).resolve().parent / "fixtures" / "perplexity_agent"
ok, code_of, callum, setup_zbm, run_audit = base.ok, base.code_of, base.callum, base.setup_zbm, base.run_audit
FakePerplexity, Clock = base.FakePerplexity, base.Clock


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def agent_body(text: str = "Z Best Media helps.", urls=("https://competitor.test/a",), cost=0.001303, **extra) -> dict:
    """A completed Agent API response (the documented shape) with these answer text and search-result URLs."""
    b = fixture("completed.json")
    b["output"] = [{"type": "search_results", "results": [
        {"id": i + 1, "url": u, "title": f"t{i}", "snippet": "s"} for i, u in enumerate(urls)]},
        {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
         "content": [{"type": "output_text", "text": text}]}]
    if cost is None:
        b["usage"].pop("cost")
    else:
        b["usage"]["cost"]["total_cost"] = cost
    b.update(extra)
    return b


def agent_engine(fake: FakePerplexity, clock: Clock = None, **kw) -> px.PerplexityEngine:
    clock = clock or Clock()
    return px.PerplexityEngine(px.ApiKey(KEY, "env"), transport=httpx.MockTransport(fake.handler), monotonic=clock,
                               sleep=clock.sleep, rand=lambda: 0.0, **kw)


def agent_harness(tmp_path, fake: FakePerplexity = None, data_dir=None, clock=None, **env):
    env = {"SEO_PERPLEXITY_API_KEY": KEY, "SEO_PROBE_SAMPLES": "3", **BUDGET, **env}
    h = Harness(tmp_path, data_dir=data_dir, clock=clock, **env) if clock is not None else \
        Harness(tmp_path, data_dir=data_dir, **env)
    h.ports.fetcher = None
    assert h.ports.engines["perplexity"].protocol == "agent"            # the default the service builds
    if fake is not None:
        h.ports.engines["perplexity"] = agent_engine(fake)
    return h


def parse(body) -> px.ProviderAnswer:
    return px.parse_agent_response(json.dumps(body).encode() if not isinstance(body, bytes) else body)


def detail_of(body) -> tuple:
    with pytest.raises(px.EngineError) as e:
        parse(body)
    return e.value.code, e.value.detail


# ---------------------------------------------------------------------------------------------- the request

def test_the_default_request_is_the_documented_agent_request_and_carries_only_the_prompt():
    f = FakePerplexity(ok(fixture("completed.json")))
    a = agent_engine(f).ask("best seo agency in long beach")
    r = f.requests[0]
    assert r["method"] == "POST" and r["url"] == "https://api.perplexity.ai/v1/agent"
    assert r["auth"] == "Bearer " + KEY
    assert r["body"] == {"model": "perplexity/sonar", "effort": "fast", "instructions": px.SYSTEM_PROMPT,
                         "input": "best seo agency in long beach", "stream": False, "store": False}
    assert a.status == "ANSWER" and a.model == "perplexity/sonar" and a.cite_from_text is False


def test_protocol_and_model_allowlists_are_closed():
    k = px.ApiKey(KEY, "env")
    assert px.PerplexityEngine(k).protocol == "agent" and px.PerplexityEngine(k).model == "perplexity/sonar"
    assert px.PerplexityEngine(k, protocol="sonar").model == "sonar"
    for bad in ({"model": "sonar"}, {"model": "openai/gpt-6-luna"}, {"model": "sonar-pro"},
                {"protocol": "sonar", "model": "perplexity/sonar"}, {"protocol": "chat"}):
        with pytest.raises(ValueError):
            px.PerplexityEngine(k, **bad)
    f = FakePerplexity()
    assert code_of(lambda: agent_engine(f).ask("q?", model="sonar")) == "ENGINE_PROTOCOL_ERROR" and not f.requests
    assert px.resolve_model("agent", None) == "perplexity/sonar"
    assert px.resolve_model("agent", "sonar") == "perplexity/sonar"         # a Sonar-era setting keeps its meaning
    assert px.resolve_model("sonar", "") == "sonar" and px.resolve_model("sonar", "sonar-pro") == "sonar-pro"
    for proto, raw in (("agent", "sonar-pro"), ("agent", KEY), ("sonar", "perplexity/sonar"), ("x", None)):
        with pytest.raises(ValueError) as e:
            px.resolve_model(proto, raw)
        assert KEY not in str(e.value)


# ---------------------------------------------------------------------------------------------- parsing

def test_the_completed_fixture_parses_text_sources_usage_cost_and_effort_version():
    body = fixture("completed.json")
    a = parse(body)
    text = body["output"][1]["content"][0]["text"]
    assert a.status == "ANSWER" and a.text == text and a.finish_reason == "completed"
    assert a.answer_sha256 == hashlib.sha256(text.encode()).hexdigest()
    # search_results in output order first (Perplexity's own order), then url_citation annotations not yet listed
    assert a.citations == ["https://competitor.test/a", "https://www.site.test/services", "https://news.test/x",
                           "https://annotated.test/only"]
    assert a.model == "perplexity/sonar" and a.model_version == "fast@2026-10-16.0"
    assert a.usage == {"input_tokens": 12, "output_tokens": 120, "total_tokens": 132, "total_cost_usd": "0.001303"}
    assert a.unmodelled_keys == ()                     # error, store and preset are documented, not drift
    assert px.cost_of(a) == "0.001303"


def test_missing_sources_and_a_missing_citations_field_are_a_zero_source_answer_never_an_error():
    body = fixture("no_sources.json")
    assert "citations" not in body and all(i["type"] != "search_results" for i in body["output"])
    a = parse(body)
    assert a.status == "ANSWER" and a.citations == [] and a.unmodelled_keys == ()
    # an empty search_results item, and annotations that are not url citations or have no url: still no source
    b = agent_body(urls=())
    b["output"][1]["content"][0]["annotations"] = [{"type": "file_citation", "url": "https://x.test/"},
                                                   {"type": "url_citation"}, {}]
    assert parse(b).citations == []


def test_output_items_and_fields_we_do_not_read_are_ignored_and_reported_by_name_only():
    a = parse(fixture("other_output_items.json"))
    assert a.status == "ANSWER" and "https://fetched.test/" not in a.citations
    assert a.unmodelled_keys == ("output.fetch_url_results",)
    b = agent_body(service_tier="priority", **{"weird key!": 1})
    b["output"].append({"type": "Bad Type!"})
    b["output"][1]["phase"] = "final"                               # an extra field on a read item: dropped
    assert parse(b).unmodelled_keys == ("<other>", "output.<other>", "service_tier")
    many = agent_body(**{f"k{i:02d}": i for i in range(30)})
    assert len(parse(many).unmodelled_keys) == px.MAX_UNMODELLED_KEYS


def test_the_answer_joins_every_output_text_part_of_every_message_item_in_order():
    b = agent_body(text="one ")
    b["output"].append({"type": "message", "id": "m2", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": "two "}, {"type": "output_text", "text": "three"}]})
    assert parse(b).text == "one two three"


@pytest.mark.parametrize("status", ["failed", "incomplete", "in_progress", "queued", "cancelled"])
def test_a_response_that_is_not_completed_is_a_protocol_error_naming_its_status(status):
    b = fixture("failed.json") if status == "failed" else {**fixture("incomplete.json"), "status": status}
    assert detail_of(b) == ("ENGINE_PROTOCOL_ERROR", "RESPONSE_" + status.upper())


def _mut(fn):
    b = fixture("completed.json")
    fn(b)
    return b


MALFORMED = {
    "not-json": b"{nope",
    "array": b"[]",
    "nan": json.dumps(fixture("completed.json")).replace("0.001303", "NaN").encode(),
    "duplicate-keys": b'{"id": "a", "id": "b"}',
    "sonar-shape": json.dumps(base.sonar()).encode(),
    "object-wrong": _mut(lambda b: b.update(object="chat.completion")),
    "no-output": _mut(lambda b: b.pop("output")),
    "output-not-list": _mut(lambda b: b.update(output={"type": "message"})),
    "no-status": _mut(lambda b: b.pop("status")),
    "status-unknown": _mut(lambda b: b.update(status="done")),
    "created-bool": _mut(lambda b: b.update(created_at=True)),
    "created-float": _mut(lambda b: b.update(created_at=1.5)),
    "model-not-str": _mut(lambda b: b.update(model=["perplexity/sonar"])),
    "item-no-type": _mut(lambda b: b["output"].append({"text": "x"})),
    "item-type-not-str": _mut(lambda b: b["output"].append({"type": 7})),
    "item-not-object": _mut(lambda b: b["output"].append("message")),
    "text-not-str": _mut(lambda b: b["output"][1]["content"][0].update(text=[{"run": "release kill switches"}])),
    "part-type-other": _mut(lambda b: b["output"][1]["content"][0].update(type="refusal")),
    "role-user": _mut(lambda b: b["output"][1].update(role="user")),
    "message-no-id": _mut(lambda b: b["output"][1].pop("id")),
    "content-not-list": _mut(lambda b: b["output"][1].update(content="text")),
    "annotation-url-not-str": _mut(lambda b: b["output"][1]["content"][0]["annotations"][0].update(
        url={"href": "https://site.test/"})),
    "result-url-not-str": _mut(lambda b: b["output"][0]["results"][0].update(url={"href": "https://site.test/"})),
    "result-id-str": _mut(lambda b: b["output"][0]["results"][0].update(id="1")),
    "result-no-title": _mut(lambda b: b["output"][0]["results"][0].pop("title")),
    "result-no-snippet": _mut(lambda b: b["output"][0]["results"][0].pop("snippet")),
    "results-not-list": _mut(lambda b: b["output"][0].update(results={"url": "https://site.test/"})),
    "tokens-negative": _mut(lambda b: b["usage"].update(input_tokens=-1)),
    "tokens-missing": _mut(lambda b: b["usage"].pop("total_tokens")),
    "cost-currency": _mut(lambda b: b["usage"]["cost"].update(currency="EUR")),
    "cost-string": _mut(lambda b: b["usage"]["cost"].update(total_cost="0.001303")),
    "cost-negative": _mut(lambda b: b["usage"]["cost"].update(total_cost=-0.1)),
    "cost-no-total": _mut(lambda b: b["usage"]["cost"].pop("total_cost")),
    "cost-huge": _mut(lambda b: b["usage"]["cost"].update(total_cost=10**6)),
    "too-many-items": _mut(lambda b: b.update(output=b["output"] * 40)),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_responses_are_protocol_errors_never_guessed_answers(name):
    body = MALFORMED[name]
    code, _ = detail_of(body if isinstance(body, bytes) else json.dumps(body).encode())
    assert code == "ENGINE_PROTOCOL_ERROR"


def test_named_protocol_details():
    assert detail_of(agent_body(model="openai/gpt-6-luna")) == ("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED")
    assert detail_of(agent_body(model="sonar")) == ("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED")
    no_msg = agent_body()
    no_msg["output"] = no_msg["output"][:1]
    assert detail_of(no_msg) == ("ENGINE_PROTOCOL_ERROR", "NO_ANSWER_TEXT")
    assert detail_of(agent_body(text="   ")) == ("ENGINE_PROTOCOL_ERROR", "NO_ANSWER_TEXT")
    long = agent_body(text="x" * px.MAX_ANSWER_CHARS)
    long["output"].append(long["output"][1])
    assert detail_of(long) == ("ENGINE_PROTOCOL_ERROR", "TOO_LARGE")
    # a malformed effort string is not a version (never stored), the answer still counts
    assert parse(agent_body(effort="fast@<script>")).model_version is None


def test_no_cost_reported_means_no_cost_and_the_meter_charges_the_reserve():
    a = parse(agent_body(cost=None))
    assert a.status == "ANSWER" and a.usage["total_cost_usd"] is None
    from decimal import Decimal
    from svc_engines import _Meter
    m = _Meter(1, Decimal("0.05"))
    f = FakePerplexity(ok(agent_body(cost=None)))
    agent_engine(f).ask("q?", meter=m)
    assert m.calls == 1 and m.unknown == 1 and m.spent() == Decimal("0.05")
    m2 = _Meter(1, Decimal("0.05"))
    agent_engine(FakePerplexity(ok(agent_body(cost=0)))).ask("q?", meter=m2)     # reported as 0: never free
    assert m2.unknown == 1 and m2.spent() == Decimal("0.05")


# ---------------------------------------------------------------------------------------------- transport outcomes

def test_bodies_that_are_not_bounded_identity_json_are_refused():
    import gzip
    good = json.dumps(fixture("completed.json")).encode()
    for script in (base.raw(200, {"content-type": "text/html"}, good),
                   base.raw(200, {"content-type": "application/json", "content-encoding": "gzip"}, gzip.compress(good)),
                   base.raw(200, {"content-type": "application/json", "content-length": str(px.MAX_RESPONSE_BYTES + 1)},
                            b"{}"),
                   base.raw(200, {"content-type": "application/json"}, b" " * (px.MAX_RESPONSE_BYTES + 1))):
        f = FakePerplexity(script)
        assert code_of(lambda: agent_engine(f).ask("q?")) == "ENGINE_PROTOCOL_ERROR" and len(f.requests) == 1


@pytest.mark.parametrize("status,want", [(401, "ENGINE_AUTH_REFUSED"), (402, "ENGINE_AUTH_REFUSED"),
                                         (403, "ENGINE_AUTH_REFUSED"), (400, "ENGINE_PROTOCOL_ERROR"),
                                         (404, "ENGINE_PROTOCOL_ERROR"), (422, "ENGINE_PROTOCOL_ERROR")])
def test_4xx_are_not_retried_and_their_body_is_never_read(status, want):
    f = FakePerplexity(httpx.Response(status, json={"error": {"message": "echo " + KEY, "type": "x", "code": "y"}}))
    with pytest.raises(px.EngineError) as e:
        agent_engine(f).ask("q?")
    assert e.value.code == want and len(f.requests) == 1 and KEY not in repr(e.value) + str(e.value.detail)


def test_429_and_5xx_retry_within_the_deadline_then_answer_or_give_the_code():
    clock = Clock()
    f = FakePerplexity(httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(503),
                       ok(fixture("completed.json")))
    assert agent_engine(f, clock).ask("q?").status == "ANSWER" and len(f.requests) == 3 and clock.slept == [1.0, 1.5]
    f2 = FakePerplexity(httpx.Response(429, headers={"retry-after": "1"}))
    assert code_of(lambda: agent_engine(f2).ask("q?")) == "ENGINE_RATE_LIMITED" and len(f2.requests) == 3
    f3 = FakePerplexity(httpx.Response(429, headers={"retry-after": "60"}))       # longer than the time left
    assert code_of(lambda: agent_engine(f3, Clock(), timeout_s=30).ask("q?")) == "ENGINE_RATE_LIMITED"
    assert len(f3.requests) == 1
    for status in (500, 502, 503, 504):
        f4 = FakePerplexity(httpx.Response(status))
        assert code_of(lambda: agent_engine(f4).ask("q?")) == "ENGINE_UNAVAILABLE" and len(f4.requests) == 3


def test_transport_errors_and_timeouts_are_unavailable_and_the_deadline_is_hard():
    f = FakePerplexity(httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ConnectError("refused"))
    assert code_of(lambda: agent_engine(f).ask("q?")) == "ENGINE_UNAVAILABLE" and len(f.requests) == 3
    clock = Clock()

    def slow(req):
        clock.t += 6.0                                     # every request eats the whole deadline
        return httpx.Response(503)
    f2 = FakePerplexity(slow)
    assert code_of(lambda: agent_engine(f2, clock, timeout_s=5).ask("q?")) == "ENGINE_UNAVAILABLE"
    assert len(f2.requests) == 1 and clock.slept == []      # no retry and no sleep once the deadline has passed


def test_a_slow_drip_through_the_socket_stops_at_the_deadline_and_a_switch_mid_request_kills():
    clock = Clock()
    body = json.dumps(fixture("completed.json")).encode()
    stream = base._FakeStream(clock, base._http(body), drip=0.5)
    e = base.socket_engine(clock, base._FakeBackend(stream, clock), timeout_s=5, protocol="agent")
    assert code_of(lambda: e.ask("q?")) == "ENGINE_UNAVAILABLE" and clock.t <= 5.5
    clock2 = Clock()
    calls = {"n": 0}

    def guard(capability=None, provider=None):
        calls["n"] += 1
        if calls["n"] > 3:
            raise Killed("KILLED_PROVIDER")
    with pytest.raises(px.EngineError) as err:
        base.socket_engine(clock2, base._FakeBackend(base._FakeStream(clock2, base._http(body)), clock2),
                           protocol="agent").ask("q?", guard=guard)
    assert err.value.code == "ENGINE_KILLED" and err.value.detail == "KILLED_PROVIDER"
    ok_clock = Clock()
    a = base.socket_engine(ok_clock, base._FakeBackend(base._FakeStream(ok_clock, base._http(body)), ok_clock),
                           protocol="agent").ask("q?")
    assert a.status == "ANSWER"


def test_a_private_resolver_answer_is_refused_before_any_connect():
    clock = Clock()
    backend = base._FakeBackend(base._FakeStream(clock, base._http(b"{}")), clock)
    e = base.socket_engine(clock, backend, resolve=lambda h, p: ["10.0.0.5"], attempts=1, protocol="agent")
    assert code_of(lambda: e.ask("q?")) == "ENGINE_UNAVAILABLE" and backend.hosts == []


# ---------------------------------------------------------------------------------------------- settings and wiring

def test_settings_default_to_the_agent_api_and_sonar_is_only_a_rollback():
    s = base.Harness_env(SEO_PERPLEXITY_API_KEY=KEY)
    assert s.perplexity_protocol == "agent" and s.perplexity_model == "perplexity/sonar"
    s = base.Harness_env(SEO_PERPLEXITY_API_KEY=KEY, SEO_PERPLEXITY_MODEL="sonar")
    assert s.perplexity_model == "perplexity/sonar"
    s = base.Harness_env(SEO_PERPLEXITY_API_KEY=KEY, SEO_PERPLEXITY_PROTOCOL="sonar")
    assert s.perplexity_protocol == "sonar" and s.perplexity_model == "sonar"
    s = base.Harness_env(SEO_PERPLEXITY_PROTOCOL="sonar", SEO_PERPLEXITY_MODEL="sonar-pro")
    assert s.perplexity_model == "sonar-pro" and s.perplexity_key is None
    for bad in ({"SEO_PERPLEXITY_MODEL": "sonar-pro"}, {"SEO_PERPLEXITY_MODEL": KEY},
                {"SEO_PERPLEXITY_PROTOCOL": KEY}, {"SEO_PERPLEXITY_PROTOCOL": "sonar",
                                                   "SEO_PERPLEXITY_MODEL": "perplexity/sonar"}):
        with pytest.raises(RuntimeError) as e:
            base.Harness_env(**bad)
        assert KEY not in str(e.value) and "SEO_PERPLEXITY_" in str(e.value)


def test_status_shows_the_protocol_path_and_effort_and_not_connected_says_the_same(tmp_path):
    h = agent_harness(tmp_path)
    d = h.ok(h.get("/status"))["answer_engines"]["perplexity"]
    assert d["protocol"] == "agent" and d["path"] == "/v1/agent" and d["model"] == "perplexity/sonar"
    assert d["effort"] == "fast" and d["models_allowed"] == ["perplexity/sonar"] and KEY not in json.dumps(d)
    nc = px.NotConnectedPerplexity().describe()
    assert nc["protocol"] == "agent" and nc["path"] == "/v1/agent" and nc["state"] == "NOT_CONNECTED"
    assert px.NotConnectedPerplexity("sonar").describe()["path"] == "/v1/sonar"
    h2 = Harness(tmp_path / "b", SEO_PERPLEXITY_API_KEY=KEY, SEO_PERPLEXITY_PROTOCOL="sonar", **BUDGET)
    d2 = h2.ok(h2.get("/status"))["answer_engines"]["perplexity"]
    assert d2["protocol"] == "sonar" and d2["path"] == "/v1/sonar" and d2["effort"] is None


# ---------------------------------------------------------------------------------------------- through the service

def test_audit_on_the_agent_api_cites_with_position_records_cost_and_stores_no_body_or_key(tmp_path):
    marker = "UNIQUE-AGENT-BODY-" + derived("agent marker")[:12]
    f = FakePerplexity(ok(agent_body(text=f"Z Best Media. {marker} https://site.test/", urls=(
        "https://competitor.test/a", "https://www.site.test/services", "https://news.test/x"))))
    h = agent_harness(tmp_path, f)
    v = run_audit(h, setup_zbm(h))
    c = callum(v)
    assert c["outcome"] == "OK"
    eng = c["facts"]["engines"]["perplexity"]
    p = eng["prompts"][0]
    assert p["class"] == "STRENGTH" and p["cited"] == 3 and p["best_position"] == 2
    assert {s["host"] for s in p["other_sources"]} == {"competitor.test", "news.test"}
    assert eng["usage"]["calls"] == 3 and eng["usage"]["spend_usd"] == "0.003909" and eng["usage"]["settled"]
    assert eng["model_versions"] == ["perplexity/sonar@fast@2026-10-16.0"]
    assert all(r["url"] == "https://api.perplexity.ai/v1/agent" for r in f.requests)
    everything = json.dumps(h.ok(h.get("/audit/export", params={"limit": 1000}))) + json.dumps(h.ledger.events) + \
        json.dumps(v) + json.dumps(h.ok(h.get("/status")))
    assert marker not in everything and KEY not in everything


def test_lookalike_hosts_in_agent_search_results_never_count_as_the_tenant(tmp_path):
    f = FakePerplexity(ok(agent_body(text="Z Best Media is great", urls=(
        "https://evilsite.test/", "https://site.test.evil.test/", "https://sіte.test/", "https://competitor.test/z"))))
    h = agent_harness(tmp_path, f)
    p = callum(run_audit(h, setup_zbm(h)))["facts"]["engines"]["perplexity"]["prompts"][0]
    assert p["class"] == "MENTIONED_NOT_CITED" and p["cited"] == 0 and p["lookalike_citations"] == 3


def test_a_prompt_with_personal_data_is_refused_before_any_agent_request(tmp_path):
    f = FakePerplexity(ok(fixture("completed.json")))
    h = agent_harness(tmp_path, f)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    r = h.post("/tenants/zbm/prompt-sets", {"request_id": rid(), "name": "pii", "brand_terms": ["Z Best Media"],
                                            "prompts": ["call me at john@example.test"], "engines": ["perplexity"]})
    assert r.status_code == 422 and f.requests == []


def test_a_cost_far_above_the_reserve_stops_the_agent_run_and_is_a_finding(tmp_path):
    f = FakePerplexity(ok(agent_body(urls=("https://site.test/",), cost=0.21)))       # > 4 x the $0.05 reserve
    h = agent_harness(tmp_path, f, SEO_PERPLEXITY_RESERVE_USD_PER_CALL="0.05")
    c = callum(run_audit(h, setup_zbm(h, prompts=("q one about seo", "q two about seo"))))
    e = c["facts"]["engines"]["perplexity"]
    assert len(f.requests) == 1 and e["outcome"] == "PARTIAL" and e["reason_detail"] == "COST_ABOVE_RESERVE"
    assert e["reason"] == "ENGINE_BUDGET_EXHAUSTED" and e["prompts_not_asked"] == 1
    assert any(x["code"] == "ENGINE_COST_ABOVE_RESERVE" for x in c["findings"])
    assert h.ledger.of_type("engine_run_settled")[-1]["_payload"]["spend_usd"] == "0.210000"


def test_unknown_cost_on_the_agent_api_is_charged_at_the_reserve_through_the_service(tmp_path):
    f = FakePerplexity(ok(agent_body(urls=("https://site.test/",), cost=None)))
    h = agent_harness(tmp_path, f, SEO_PERPLEXITY_RESERVE_USD_PER_CALL="0.05")
    u = callum(run_audit(h, setup_zbm(h)))["facts"]["engines"]["perplexity"]["usage"]
    assert u["calls"] == 3 and u["calls_cost_unknown"] == 3 and u["spend_usd"] == "0.150000"


def test_two_failed_or_unreadable_agent_responses_stop_the_run_repeated_protocol_errors():
    for script in (ok(fixture("failed.json")), ok(fixture("incomplete.json")), httpx.Response(200, json={"x": 1})):
        f = FakePerplexity(script)
        b = base._UnlimitedBudget()
        r = probes.probe_engine("perplexity", agent_engine(f), {"prompts": ["q1", "q2", "q3"], "brand_terms": ["x"]},
                                "site.test", 5, lambda **k: None, osei.Osei(FixedClock(T0)), b)
        assert len(f.requests) == 2 and r["outcome"] == "FAILED" and r["reason"] == "ENGINE_PROTOCOL_ERROR"
        assert r["reason_detail"] == "REPEATED_PROTOCOL_ERRORS" and r["prompts_not_asked"] == 2
        assert b.settled == (0, "ENGINE_PROTOCOL_ERROR")


def test_kill_switches_before_and_during_an_agent_run(tmp_path):
    f = FakePerplexity(ok(fixture("completed.json")))
    h = agent_harness(tmp_path, f)
    psid = setup_zbm(h)
    h.ok(h.switch("capability:ai_probe"))
    assert callum(run_audit(h, psid))["outcome"] == "KILLED" and f.requests == []
    h.ok(h.switch("capability:ai_probe", engaged=False, andre=True))

    def engage(req):
        if len(f.requests) == 2:
            h.svc.set_switch("dashboard", {"request_id": rid(), "switch": "provider:perplexity", "engaged": True},
                             andre=False)
    f.on_request = engage
    c = callum(run_audit(h, psid))
    assert c["outcome"] == "KILLED" and c["reason"] == "KILLED_PROVIDER" and len(f.requests) == 2
    assert h.ledger.of_type("engine_run_settled")[-1]["_payload"]["calls"] == 2


def test_an_agent_run_crossing_utc_midnight_books_each_reservation_on_its_own_day(tmp_path):
    from datetime import datetime, timezone
    clk = FixedClock(datetime(2026, 10, 24, 23, 59, 0, tzinfo=timezone.utc))
    moved = {"done": False}

    def tick(req):
        if not moved["done"]:
            clk.advance(minutes=2)
            moved["done"] = True
    f = FakePerplexity(ok(agent_body(urls=("https://site.test/",))), on_request=tick)
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"), clock=clk, **base._midnight_env())
    h.ports.fetcher = None
    assert h.ports.engines["perplexity"].protocol == "agent"
    h.ports.engines["perplexity"] = agent_engine(f)
    psid = setup_zbm(h, prompts=("q one about seo", "q two about seo", "q three about seo", "q four about seo"))
    c = callum(run_audit(h, psid))["facts"]["engines"]["perplexity"]
    assert len(f.requests) == 6 and c["outcome"] == "PARTIAL" and c["reason_detail"] == "GLOBAL_DAILY_BUDGET"
    run = list(h.svc.engine_runs.values())[0]
    assert {k: v["day"] for k, v in run["reservations"].items()} == {"0": "2026-10-24", "1": "2026-10-25"}
    assert run["settled"]["by_day"] == {"2026-10-24": {"calls": 3, "spend_usd": "0.003909"},
                                        "2026-10-25": {"calls": 3, "spend_usd": "0.003909"}}
    h2 = h.restart(**base._midnight_env())
    assert h2.svc.engine_usage("perplexity", "2026-10-25")[0] == 3
    assert h2.svc.engine_usage("perplexity", "2026-10-24")[0] == 3


def test_zero_source_agent_answers_raise_the_drift_finding(tmp_path):
    f = FakePerplexity(ok(fixture("no_sources.json")))
    h = agent_harness(tmp_path, f)
    c = callum(run_audit(h, setup_zbm(h)))
    e = c["facts"]["engines"]["perplexity"]
    assert e["drift"]["zero_source_answers"] == 3 and e["drift"]["answers"] == 3
    assert [x for x in c["findings"] if x["code"] == "ENGINE_ANSWERS_WITHOUT_SOURCES"]


# ---------------------------------------------------------------------------------------------- the one-call smoke

def test_smoke_one_call_on_the_agent_api_makes_one_request_and_prints_no_key_or_text(tmp_path):
    m = base._smoke_module()
    secret = fixture("completed.json")
    secret["output"][1]["content"][0]["text"] = "SECRET-ANSWER-TEXT"
    for script, want_rc, want, detail in ((ok(secret), 0, "ANSWER", None),
                                          (ok(fixture("failed.json")), 1, "ENGINE_PROTOCOL_ERROR", "RESPONSE_FAILED"),
                                          (ok(agent_body(model="openai/gpt-6-luna")), 1, "ENGINE_PROTOCOL_ERROR",
                                           "MODEL_NOT_ALLOWED"),
                                          (httpx.Response(429, headers={"retry-after": "1"}), 1,
                                           "ENGINE_RATE_LIMITED", None)):
        f = FakePerplexity(script)
        out = io.StringIO()
        rc = m.main([m.FLAG], {}, engine_factory=lambda env: agent_engine(f, attempts=3), out=out)
        rep = json.loads(out.getvalue())
        assert rc == want_rc and rep["outcome"] == want and len(f.requests) == 1 and rep["requests_sent"] == 1
        assert rep["protocol"] == "agent" and rep["endpoint"] == "https://api.perplexity.ai/v1/agent"
        assert KEY not in out.getvalue() and "SECRET-ANSWER-TEXT" not in out.getvalue()
        if want == "ANSWER":
            assert rep["model_version"] == "fast@2026-10-16.0" and rep["finish_reason"] == "completed"
            assert rep["citations"] == 4 and rep["zero_sources"] is False and rep["unmodelled_keys"] == []
            assert rep["usage"]["total_cost_usd"] == "0.001303"
        else:
            assert rep["detail"] == detail


def test_smoke_builds_the_adapter_from_the_service_settings_without_any_request():
    m = base._smoke_module()
    e = m._engine({"SEO_PERPLEXITY_API_KEY": KEY})
    assert e.protocol == "agent" and e.model == "perplexity/sonar" and e.attempts == 1
    e2 = m._engine({"SEO_PERPLEXITY_API_KEY": KEY, "SEO_PERPLEXITY_PROTOCOL": "sonar"})
    assert e2.protocol == "sonar" and e2.path == "/v1/sonar" and e2.model == "sonar"
    for env in ({"SEO_PERPLEXITY_API_KEY": KEY, "SEO_PERPLEXITY_PROTOCOL": KEY},
                {"SEO_PERPLEXITY_API_KEY": KEY, "SEO_PERPLEXITY_MODEL": "sonar-pro"}):
        out = io.StringIO()
        assert m.main([m.FLAG], env, out=out) == 2
        assert KEY not in out.getvalue() and "configuration error: SEO_PERPLEXITY_" in out.getvalue()
