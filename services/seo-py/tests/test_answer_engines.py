"""The multi-engine answer adapter (ADR 0046): OpenAI (Responses API + web_search), Google (Gemini generateContent +
Google Search grounding) and Anthropic (Messages API + web search tool) behind answer_adapter.HttpAnswerEngine, and
their configuration. No network and no key: every response is a SYNTHETIC fixture built from the documented shapes
(tests/fixtures/answer_engines/, NOT captured from the real APIs), served through httpx.MockTransport or, for the
socket layer, a fake network backend under httpcore's real pool. Clocks are injected; no elapsed-time assertion."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

import anthropic_engine as an
import answer_adapter as aa
import config as config_mod
import google_engine as gg
import openai_engine as oa
import perplexity as px
import ports as ports_mod
import test_perplexity as base
from helpers import base_env, derived

FIX = Path(__file__).parent / "fixtures" / "answer_engines"
KEYS = {e: "test-only-" + derived(f"{e} api key") for e in ("openai", "anthropic", "google")}
MODS = {"openai": oa, "anthropic": an, "google": gg}
CLASSES = {"openai": oa.OpenAIEngine, "anthropic": an.AnthropicEngine, "google": gg.GoogleEngine}


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}_completed.json").read_text())


def make(name: str, fake, clock=None, **kw):
    clock = clock or base.Clock()
    return CLASSES[name](aa.ApiKey(KEYS[name], "env"), transport=httpx.MockTransport(fake.handler),
                         monotonic=clock, sleep=clock.sleep, rand=lambda: 0.0, **kw)


def fake(name: str, body=None, *more):
    return base.FakePerplexity(base.ok(fixture(name) if body is None else body), *more)


def code_of(fn):
    with pytest.raises(aa.EngineError) as e:
        fn()
    return e.value.code, e.value.detail


ENGINES = ("openai", "anthropic", "google")


# ---------------------------------------------------------------------------------------------- the interface

def test_every_engine_implements_the_one_interface_and_perplexity_is_on_it_too():
    for name in ENGINES:
        e = make(name, fake(name))
        assert isinstance(e, aa.HttpAnswerEngine) and isinstance(e, aa.AnswerEngine)
        assert e.name == name and e.connected and e.metered and e.accepts_guard
        assert e.ALLOWED_HOSTS == frozenset({MODS[name].HOST})
        assert MODS[name].DOCS["url"].startswith("https://") and MODS[name].DOCS["read"] == "2026-11-08"
    p = base.engine(base.FakePerplexity())
    assert isinstance(p, aa.HttpAnswerEngine) and isinstance(p, aa.AnswerEngine)
    assert px.EngineError is aa.EngineError and px.ApiKey is aa.ApiKey      # one error type, one key holder
    assert px._GuardedDeadlineBackend is aa._GuardedDeadlineBackend


@pytest.mark.parametrize("name", ENGINES)
def test_an_answer_carries_text_cited_urls_and_provider_model_time_metadata(name):
    from datetime import datetime, timezone
    e = make(name, fake(name), wall=lambda: datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc))
    a = e.ask("who recovers marketplace revenue in long beach")
    assert a.status == "ANSWER" and a.provider == name and a.asked_at == "2026-11-08T12:00:00Z"
    assert "Z Best Media" in a.text and a.cite_from_text is False and a.citations
    assert a.model and a.model_version and a.answer_sha256 == hashlib.sha256(a.text.encode()).hexdigest()
    assert a.usage["total_cost_usd"] is None                      # no provider reports a cost: charged at the reserve


def test_perplexity_answers_are_stamped_too_without_changing_what_the_parser_returns():
    a = base.engine(base.FakePerplexity()).ask("q?")
    assert a.provider == "perplexity" and isinstance(a.asked_at, str)
    b = px.parse_response(json.dumps(base.sonar()).encode())
    assert b.provider is None and b.asked_at is None                      # the parsers stay pure


# ---------------------------------------------------------------------------------------------- the requests

def test_openai_request_is_the_documented_responses_call_and_carries_only_the_prompt():
    f = fake("openai")
    make("openai", f).ask("best revenue recovery agency")
    r = f.requests[0]
    assert r["method"] == "POST" and r["url"] == "https://api.openai.com/v1/responses"
    assert r["auth"] == "Bearer " + KEYS["openai"]
    assert r["body"] == {"model": "gpt-6-astra", "instructions": aa.SYSTEM_PROMPT, "input": "best revenue recovery agency",
                         "tools": [{"type": "web_search"}], "include": ["web_search_call.action.sources"],
                         "store": False}


def test_google_request_puts_the_allow_listed_model_in_the_path_and_the_key_in_a_header():
    seen = {}
    f = fake("google")
    f.on_request = lambda req: seen.update(key=req.headers.get("x-goog-api-key"), query=req.url.query)
    make("google", f).ask("best revenue recovery agency")
    r = f.requests[0]
    assert r["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    assert seen == {"key": KEYS["google"], "query": b""} and r["auth"] is None      # never in the URL
    assert r["body"] == {"systemInstruction": {"parts": [{"text": aa.SYSTEM_PROMPT}]},
                         "contents": [{"role": "user", "parts": [{"text": "best revenue recovery agency"}]}],
                         "tools": [{"google_search": {}}]}
    with pytest.raises(aa.EngineError):
        make("google", f)._path("../../evil")


def test_anthropic_request_is_the_basic_web_search_tool_with_max_uses():
    seen = {}
    f = fake("anthropic")
    f.on_request = lambda req: seen.update(key=req.headers.get("x-api-key"), ver=req.headers.get("anthropic-version"))
    make("anthropic", f, max_searches=2).ask("best revenue recovery agency")
    r = f.requests[0]
    assert r["url"] == "https://api.anthropic.com/v1/messages" and r["auth"] is None
    assert seen == {"key": KEYS["anthropic"], "ver": an.API_VERSION}
    assert r["body"] == {"model": "claude-sonnet-5-5", "max_tokens": an.MAX_TOKENS, "system": aa.SYSTEM_PROMPT,
                         "messages": [{"role": "user", "content": "best revenue recovery agency"}],
                         "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]}


# ---------------------------------------------------------------------------------------------- citations

def test_openai_cites_annotations_its_own_search_returned_and_strips_the_engine_tracking_pair():
    a = make("openai", fake("openai")).ask("q?")
    assert a.citations == ["https://site.test/services", "https://competitor.test/recovery"]
    assert a.unverified_citations == 1 and a.citation_basis == "annotations_in_search_sources"
    assert a.model == "gpt-6-astra-2026-09-30" and a.model_version == "2026-09-30" and a.unmodelled_keys == ()


def test_openai_without_sources_counts_annotations_and_says_so():
    body = fixture("openai")
    del body["output"][0]["action"]["sources"]
    a = make("openai", fake("openai", body)).ask("q?")
    assert a.citation_basis == "annotations_only" and "https://planted.test/not-in-search" in a.citations


def test_google_cites_grounding_chunks_in_order_and_keeps_titles_only_as_unverified_hints():
    a = make("google", fake("google")).ask("q?")
    assert a.citations == ["https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAAAsynthetic1",
                           "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAAAsynthetic2"]
    assert a.text == "Z Best Media is a revenue recovery agency in Long Beach."      # the thought part is not text
    assert a.citation_hints[a.citations[0]] == "site.test" and a.usage["search_queries"] == 1


def test_anthropic_cites_text_citations_its_search_returned_only():
    a = make("anthropic", fake("anthropic")).ask("q?")
    assert a.citations == ["https://www.site.test/about", "https://competitor.test/recovery"]
    assert a.unverified_citations == 1 and a.usage["web_search_requests"] == 1
    assert a.text.startswith("I'll search") and "Competitor Co" in a.text


@pytest.mark.parametrize("name", ENGINES)
def test_an_answer_without_any_citation_is_a_zero_source_answer_never_invented(name):
    body = fixture(name)
    if name == "openai":
        body["output"][1]["content"][0]["annotations"] = []
    elif name == "google":
        del body["candidates"][0]["groundingMetadata"]
    else:
        for b in body["content"]:
            b.pop("citations", None)
    a = make(name, fake(name, body)).ask("q?")
    assert a.status == "ANSWER" and a.citations == []


# ---------------------------------------------------------------------------------------------- adversarial shapes

def _mutations(name):
    b = fixture(name)
    out = []
    if name == "openai":
        for path, val in ((("object",), "chat.completion"), (("status",), "incomplete"), (("model",), "gpt-4o"),
                          (("created_at",), "yesterday"), (("output",), "nope"), (("usage", "total_tokens"), -1)):
            x = copy.deepcopy(b)
            d = x
            for k in path[:-1]:
                d = d[k]
            d[path[-1]] = val
            out.append(x)
        x = copy.deepcopy(b)
        x["output"][1]["content"][0]["text"] = "   "
        out.append(x)
        x = copy.deepcopy(b)
        x["output"][1]["content"][0]["annotations"][0]["url"] = 42
        out.append(x)
    elif name == "google":
        for k, v in (("modelVersion", "gemini-1.0-pro"), ("candidates", "x"), ("usageMetadata", {"totalTokenCount": "9"})):
            x = copy.deepcopy(b)
            x[k] = v
            out.append(x)
        for reason in ("MAX_TOKENS", "OTHER", "MALFORMED_FUNCTION_CALL", "lower", None):
            x = copy.deepcopy(b)
            x["candidates"][0]["finishReason"] = reason
            out.append(x)
        x = copy.deepcopy(b)
        x["candidates"] = []
        out.append(x)
        x = copy.deepcopy(b)
        x["candidates"][0]["groundingMetadata"]["groundingChunks"][0]["web"]["uri"] = ["x"]
        out.append(x)
    else:
        for k, v in (("type", "completion"), ("role", "user"), ("model", "claude-2"), ("stop_reason", "max_tokens"),
                     ("stop_reason", "pause_turn"), ("stop_reason", "tool_use"), ("content", {}),
                     ("usage", {"input_tokens": "1", "output_tokens": 1})):
            x = copy.deepcopy(b)
            x[k] = v
            out.append(x)
        x = copy.deepcopy(b)
        x["usage"]["server_tool_use"]["web_search_requests"] = "1"
        out.append(x)
        x = copy.deepcopy(b)
        x["content"][2]["content"] = {"type": "something_else"}
        out.append(x)
    return out


@pytest.mark.parametrize("name", ENGINES)
def test_malformed_responses_are_protocol_errors_never_answers(name):
    for body in _mutations(name):
        code, _ = code_of(lambda: make(name, fake(name, body), attempts=1).ask("q?"))
        assert code == "ENGINE_PROTOCOL_ERROR", (name, body)


@pytest.mark.parametrize("name", ENGINES)
def test_non_json_duplicate_keys_nan_and_oversize_are_refused(name):
    body = fixture(name)
    good = json.dumps(body).encode()
    first = next(iter(body))
    dup = b'{"' + first.encode() + b'": 1, ' + good[1:]                    # the first key twice
    token = {"openai": b'"input_tokens": 312', "anthropic": b'"input_tokens": 6039',
             "google": b'"promptTokenCount": 21'}[name]
    assert token in good
    nan = good.replace(token, token.split(b":")[0] + b": NaN")
    for body, ctype in ((b"<html>", "text/html"), (b"not json", "application/json"), (dup, "application/json"),
                        (nan, "application/json"), (b"x" * (aa.MAX_RESPONSE_BYTES + 1), "application/json")):
        f = base.FakePerplexity(base.raw(200, {"content-type": ctype}, body))
        code, _ = code_of(lambda: make(name, f, attempts=1).ask("q?"))
        assert code == "ENGINE_PROTOCOL_ERROR"
    f = base.FakePerplexity(base.raw(200, {"content-type": "application/json", "content-encoding": "gzip"}, good))
    assert code_of(lambda: make(name, f, attempts=1).ask("q?"))[0] == "ENGINE_PROTOCOL_ERROR"


def test_refusals_are_counted_as_refusals():
    g = fixture("google")
    g["candidates"][0]["finishReason"] = "SAFETY"
    assert make("google", fake("google", g)).ask("q?").status == "REFUSAL"
    blocked = {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "modelVersion": "gemini-3.8-flash"}
    assert make("google", fake("google", blocked)).ask("q?").status == "REFUSAL"
    a = fixture("anthropic")
    a["stop_reason"] = "refusal"
    assert make("anthropic", fake("anthropic", a)).ask("q?").status == "REFUSAL"
    o = fixture("openai")
    o["output"][1]["content"] = [{"type": "refusal", "refusal": "I can't help with that."}]
    assert make("openai", fake("openai", o)).ask("q?").status == "REFUSAL"


def test_anthropic_search_tool_errors_inside_a_200():
    a = fixture("anthropic")
    a["content"][2]["content"] = {"type": "web_search_tool_result_error", "error_code": "too_many_requests"}
    assert code_of(lambda: make("anthropic", fake("anthropic", a)).ask("q?")) == ("ENGINE_RATE_LIMITED",
                                                                                   "SEARCH_RATE_LIMITED")
    a["content"][2]["content"] = {"type": "web_search_tool_result_error", "error_code": "unavailable"}
    assert code_of(lambda: make("anthropic", fake("anthropic", a)).ask("q?")) == ("ENGINE_UNAVAILABLE",
                                                                                   "SEARCH_TOOL_ERROR")


# ---------------------------------------------------------------------------------------------- HTTP outcomes

@pytest.mark.parametrize("name", ENGINES)
def test_auth_rate_limit_and_server_errors_map_to_the_closed_codes(name):
    for status, want in ((401, "ENGINE_AUTH_REFUSED"), (403, "ENGINE_AUTH_REFUSED"), (402, "ENGINE_AUTH_REFUSED"),
                         (400, "ENGINE_PROTOCOL_ERROR"), (404, "ENGINE_PROTOCOL_ERROR")):
        f = base.FakePerplexity(httpx.Response(status, json={"error": {"message": KEYS[name]}}))
        code, _ = code_of(lambda: make(name, f).ask("q?"))
        assert code == want and len(f.requests) == 1                 # 4xx never retried, the error body never read
    clock = base.Clock()
    f = base.FakePerplexity(httpx.Response(429, headers={"retry-after": "2"}), base.ok(fixture(name)))
    assert make(name, f, clock).ask("q?").status == "ANSWER" and clock.slept == [2.0]
    f = base.FakePerplexity(httpx.Response(503), httpx.Response(502), base.ok(fixture(name)))
    assert make(name, f, attempts=3).ask("q?").status == "ANSWER" and len(f.requests) == 3
    f = base.FakePerplexity(httpx.Response(429))
    assert code_of(lambda: make(name, f, attempts=2).ask("q?"))[0] == "ENGINE_RATE_LIMITED" and len(f.requests) == 2
    f = base.FakePerplexity(httpx.Response(500))
    assert code_of(lambda: make(name, f, attempts=1).ask("q?"))[0] == "ENGINE_UNAVAILABLE"
    f = base.FakePerplexity(httpx.ConnectError("down"))
    assert code_of(lambda: make(name, f, attempts=2).ask("q?"))[0] == "ENGINE_UNAVAILABLE" and len(f.requests) == 2


@pytest.mark.parametrize("name", ENGINES)
def test_a_retry_after_past_the_deadline_is_answered_at_once_never_slept(name):
    clock = base.Clock()
    f = base.FakePerplexity(httpx.Response(429, headers={"retry-after": "500"}), base.ok(fixture(name)))
    assert code_of(lambda: make(name, f, clock, timeout_s=10).ask("q?"))[0] == "ENGINE_RATE_LIMITED"
    assert clock.slept == [] and len(f.requests) == 1


@pytest.mark.parametrize("name", ENGINES)
def test_the_deadline_mid_body_is_a_timeout(name):
    clock = base.Clock()

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"a":'
            clock.t += 1000
            yield b"1}"

    f = base.FakePerplexity(lambda req: httpx.Response(200, headers={"content-type": "application/json"},
                                                       stream=Slow()))
    assert code_of(lambda: make(name, f, clock, attempts=1).ask("q?"))[0] == "ENGINE_UNAVAILABLE"


@pytest.mark.parametrize("name", ENGINES)
def test_the_key_never_appears_in_repr_errors_or_describe(name):
    f = base.FakePerplexity(httpx.Response(401, json={"echo": KEYS[name]}))
    e = make(name, f)
    with pytest.raises(aa.EngineError) as err:
        e.ask("q?")
    for text in (repr(e), str(e.describe()), repr(err.value), str(err.value), repr(e._key)):
        assert KEYS[name] not in text
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(e._key)


# ---------------------------------------------------------------------------------------------- budget meter

class Meter:
    def __init__(self, calls=10, per_call="0.10"):
        self.left, self.per_call, self.charged, self.over_reserve = calls, per_call, [], False

    def before(self):
        if self.left <= 0:
            raise aa.EngineError("ENGINE_BUDGET_EXHAUSTED", "RESERVATION_USED")
        self.left -= 1

    def after(self, cost):
        self.charged.append(cost)


@pytest.mark.parametrize("name", ENGINES)
def test_every_request_is_metered_and_charged_at_the_reserve_when_cost_is_unknown(name):
    m = Meter()
    f = base.FakePerplexity(httpx.Response(503), base.ok(fixture(name)))
    make(name, f, attempts=2).ask("q?", meter=m)
    assert m.charged == [None, None] and m.left == 8         # both requests charged (unknown cost = the reserve)
    m2 = Meter(calls=0)
    f2 = fake(name)
    assert code_of(lambda: make(name, f2).ask("q?", meter=m2))[0] == "ENGINE_BUDGET_EXHAUSTED" and f2.requests == []


# ---------------------------------------------------------------------------------------------- kill switches

@pytest.mark.parametrize("name", ENGINES)
def test_a_kill_switch_stops_the_engine_before_any_request(name):
    from primitives import Killed
    seen = []

    def guard(capability=None, provider=None, agent=None):
        seen.append((capability, provider))
        raise Killed("KILLED_PROVIDER")
    f = fake(name)
    assert code_of(lambda: make(name, f).ask("q?", guard=guard)) == ("ENGINE_KILLED", "KILLED_PROVIDER")
    assert f.requests == [] and seen == [("ai_probe", name)]


# ---------------------------------------------------------------------------------------------- egress / SSRF

@pytest.mark.parametrize("name", ENGINES)
def test_the_socket_layer_goes_only_to_the_allow_listed_host_on_public_addresses(name):
    clock = base.Clock()
    stream = base._FakeStream(clock, base._http(json.dumps(fixture(name)).encode()))
    backend = base._FakeBackend(stream, clock)
    e = CLASSES[name](aa.ApiKey(KEYS[name], "file"), network_backend=backend, monotonic=clock, sleep=clock.sleep,
                      resolve=lambda h, p: ["104.18.26.48"], dns_gate=px._DnsGate(2), rand=lambda: 0.0)
    assert e.ask("q?").status == "ANSWER"
    assert backend.hosts == [("104.18.26.48", 443)] and stream.sni == MODS[name].HOST
    assert f"Host: {MODS[name].HOST}".encode() in stream.sent
    for bad in (["10.0.0.5"], ["169.254.169.254"], ["104.18.26.48", "127.0.0.1"]):
        b2 = base._FakeBackend(base._FakeStream(clock, b""), clock)
        e2 = CLASSES[name](aa.ApiKey(KEYS[name], "file"), network_backend=b2, monotonic=clock, sleep=clock.sleep,
                           resolve=lambda h, p, bad=bad: bad, dns_gate=px._DnsGate(2), rand=lambda: 0.0, attempts=1)
        assert code_of(lambda: e2.ask("q?"))[0] == "ENGINE_UNAVAILABLE" and b2.hosts == []


def test_the_backend_refuses_a_host_that_is_not_on_the_allow_list():
    import httpcore
    clock = base.Clock()
    b = aa._GuardedDeadlineBackend(clock, 100.0, lambda: None, lambda h, p, t: ["104.18.26.48"],
                                   inner=base._FakeBackend(base._FakeStream(clock, b""), clock),
                                   hosts=frozenset({"api.openai.com"}))
    for host, port in (("evil.test", 443), ("api.openai.com", 80), ("api.openai.com.evil.test", 443)):
        with pytest.raises(httpcore.ConnectError):
            b.connect_tcp(host, port)
    assert b.connect_tcp("api.openai.com", 443) is not None


def test_a_base_url_off_the_allow_list_is_never_sent():
    class Rogue(oa.OpenAIEngine):
        BASE_URL = "https://api.openai.com.evil.test"
    f = fake("openai")
    e = Rogue(aa.ApiKey(KEYS["openai"], "env"), transport=httpx.MockTransport(f.handler))
    assert code_of(lambda: e.ask("q?")) == ("ENGINE_NOT_CONNECTED", "HOST_NOT_ALLOWED") and f.requests == []
    for url in ("http://api.openai.com/v1", "https://api.openai.com:8443/v1", "https://u:p@api.openai.com/v1",
                "https://api.openai.com/v1#x", "https://API.openai.com.evil/v1"):
        assert not aa.allowed_url(url, frozenset({"api.openai.com"}))
    assert aa.allowed_url("https://api.openai.com/v1/responses", frozenset({"api.openai.com"}))


def test_redirects_are_never_followed():
    f = base.FakePerplexity(httpx.Response(302, headers={"location": "https://evil.test/"}))
    assert code_of(lambda: make("openai", f).ask("q?"))[0] == "ENGINE_PROTOCOL_ERROR" and len(f.requests) == 1


# ---------------------------------------------------------------------------------------------- configuration

def _env(name, **over):
    return base_env(**over)


@pytest.mark.parametrize("name", ENGINES)
def test_every_engine_is_off_by_default_and_not_connected(name):
    s = config_mod.load(base_env())
    cfg = s.answer_engines[name]
    assert cfg["enabled"] is False and cfg["key"] is None
    assert all(v in (0, "0.000000") for k, v in cfg["budget"].items() if k != "reserve_usd_per_call")
    port = ports_mod.answer_engine(s, name)
    assert port.connected is False and port.ask("q?").status == "NOT_CONNECTED"
    d = port.describe()
    assert d["state"] == "NOT_CONNECTED" and d["docs"]["url"] == MODS[name].DOCS["url"]


@pytest.mark.parametrize("name", ENGINES)
def test_enabled_without_a_key_or_a_key_without_enabled_refuses_start(name):
    n = config_mod.ENGINE_ENV[name]
    with pytest.raises(RuntimeError, match="no key"):
        config_mod.load(base_env(**{n["enabled"]: "1"}))
    with pytest.raises(RuntimeError, match="is not 1"):
        config_mod.load(base_env(**{n["key"]: KEYS[name]}))
    with pytest.raises(RuntimeError, match="both set"):
        config_mod.load(base_env(**{n["enabled"]: "1", n["key"]: KEYS[name], n["key_file"]: "/x"}))
    with pytest.raises(RuntimeError) as e:
        config_mod.load(base_env(**{n["enabled"]: "1", n["key"]: "short key"}))
    assert "short key" not in str(e.value)


@pytest.mark.parametrize("name", ENGINES)
def test_enabled_with_a_key_connects_but_budgets_stay_off_until_all_are_set(name, tmp_path):
    n = config_mod.ENGINE_ENV[name]
    s = config_mod.load(base_env(**{n["enabled"]: "1", n["key"]: KEYS[name]}))
    port = ports_mod.answer_engine(s, name)
    assert port.connected and isinstance(port, CLASSES[name]) and KEYS[name] not in repr(s)
    assert s.answer_engines[name]["key"].source == "env"
    keyfile = tmp_path / "k"
    keyfile.write_text(KEYS[name])
    keyfile.chmod(0o600)
    s2 = config_mod.load(base_env(**{n["enabled"]: "1", n["key_file"]: str(keyfile)}))
    assert s2.answer_engines[name]["key"].source == "file"
    keyfile.chmod(0o644)
    with pytest.raises(RuntimeError, match="group or others"):
        config_mod.load(base_env(**{n["enabled"]: "1", n["key_file"]: str(keyfile)}))


@pytest.mark.parametrize("name", ENGINES)
def test_an_unknown_model_leaves_only_that_engine_not_connected_never_echoed(name):
    n = config_mod.ENGINE_ENV[name]
    s = config_mod.load(base_env(**{n["enabled"]: "1", n["key"]: KEYS[name], n["model"]: "secret-ish-model"}))
    port = ports_mod.answer_engine(s, name)
    assert port.connected is False and port.describe()["reason"] == "MODEL_NOT_SUPPORTED"
    assert "secret-ish-model" not in json.dumps(port.describe())


def test_one_key_for_two_engines_or_equal_to_a_token_is_refused():
    o, a = config_mod.ENGINE_ENV["openai"], config_mod.ENGINE_ENV["anthropic"]
    with pytest.raises(RuntimeError, match="must differ"):
        config_mod.load(base_env(**{o["enabled"]: "1", o["key"]: KEYS["openai"], a["enabled"]: "1",
                                    a["key"]: KEYS["openai"]}))
    with pytest.raises(RuntimeError, match="must differ"):
        config_mod.load(base_env(**{o["enabled"]: "1", "SEO_PERPLEXITY_API_KEY": KEYS["openai"],
                                    o["key"]: KEYS["openai"]}))


def test_budget_settings_are_validated_and_never_negative():
    o = config_mod.ENGINE_ENV["openai"]
    for k, v in ((o["daily_calls"], "-1"), (o["run_max_spend_usd"], "abc"), (o["reserve_usd_per_call"], "0"),
                 (o["timeout"], "1000"), (o["attempts"], "4")):
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(**{k: v}))


def test_switching_provider_is_configuration_only():
    """The same ports factory yields each engine from settings; nothing else changes."""
    env = {}
    for name in ENGINES:
        n = config_mod.ENGINE_ENV[name]
        env.update({n["enabled"]: "1", n["key"]: KEYS[name]})
    s = config_mod.load(base_env(**env))
    p = ports_mod.Ports.default(s)
    assert {n: type(p.engines[n]).__name__ for n in ENGINES} == {"openai": "OpenAIEngine",
                                                                 "anthropic": "AnthropicEngine",
                                                                 "google": "GoogleEngine"}
    assert all(p.status()["answer_engines"][n] == "connected" for n in ENGINES)


def test_google_attributes_an_answer_only_to_the_exact_model_and_fails_closed_without_one():
    # review round 1 L2: exact match (or an explicit alias in gg.MODEL_ALIASES, none yet); a missing modelVersion
    # cannot be attributed to a model, so the answer is refused, never counted under the asked model's name
    def parse(version):
        b = fixture("google")
        if version is None:
            b.pop("modelVersion")
        else:
            b["modelVersion"] = version
        return lambda: gg.parse_response(json.dumps(b).encode(), ("gemini-3.8-flash",))
    a = parse("gemini-3.8-flash")()
    assert (a.model, a.model_version) == ("gemini-3.8-flash", "?")
    for v in ("gemini-3.8-flash-anything", "gemini-3.8-flash-001", "gemini-3.8-flash2", "GEMINI-3.8-FLASH"):
        assert code_of(parse(v)) == ("ENGINE_PROTOCOL_ERROR", "MODEL_NOT_ALLOWED"), v
    assert code_of(parse(None)) == ("ENGINE_PROTOCOL_ERROR", "MODEL_UNVERIFIED")
    assert gg.model_matches(None, "gemini-3.8-flash") is None
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    assert code_of(lambda: gg.parse_response(json.dumps(blocked).encode())) == ("ENGINE_PROTOCOL_ERROR",
                                                                                "MODEL_UNVERIFIED")
