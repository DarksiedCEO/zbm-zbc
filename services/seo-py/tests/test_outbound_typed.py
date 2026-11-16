"""C2-3c: outbound request bodies typed (core-spec/2 T-1 / T-3 / T-5 / T-6, Wave-1 D-02m) with the one closed exemption
E-LLM (owner decision 14:57 PT, lead ruling F3-4). Synthetic values only; every request goes to an httpx.MockTransport
(no network host is ever called)."""
import base64
import json
from urllib.parse import quote

import httpx
import pytest

import answer_adapter as aa
import anthropic_engine as an
import google_engine as gg
import openai_engine as oa
import outbound
import perplexity as pp
import sink_fields
import sink_guard
import test_perplexity
from agents import callum
from test_answer_engines import KEYS

ENGINES = {"openai": oa, "anthropic": an, "google": gg}
CLASSES = {"openai": oa.OpenAIEngine, "anthropic": an.AnthropicEngine, "google": gg.GoogleEngine}
KINDS = ("openai", "perplexity", "perplexity.sonar", "anthropic", "google")
# synthetic canaries, one per free-text source of the core (T-6): never a real value
CANARY = {"page_text": "zqx-page-7f3a91", "llm_answer": "zqx-answer-2b8c40", "title": "zqx-title-9d1e77",
          "hint": "zqx-hint-4c6a12", "operator_text": "zqx-optext-8e2f05", "excerpt": "zqx-excerpt-1a9b33",
          "other_tenant_prompt": "zqx-tenantb-6d4c28 best cat harness"}
PROMPTS_A = ("best hairless cat sweater brand", "where to buy a warm cat jumper")


def bodies():
    """(kind, url, body) of every request body the engines can build (every model / protocol)."""
    out = []
    for m in oa.MODELS:
        out.append(("openai", oa.BASE_URL + "/v1/responses", oa.request_body(m, PROMPTS_A[0])))
    for m in an.MODELS:
        out.append(("anthropic", an.BASE_URL + "/v1/messages", an.request_body(m, PROMPTS_A[0])))
    for m in gg.MODELS:
        out.append(("google", gg.BASE_URL + "/v1beta/models/" + m + ":generateContent", gg.request_body(m, PROMPTS_A[0])))
    for m in pp.MODELS:
        out.append(("perplexity", pp.BASE_URL + "/v1/agent", pp.request_body(pp.AGENT, m, PROMPTS_A[0])))
    for m in pp.SONAR_MODELS:
        out.append(("perplexity.sonar", pp.BASE_URL + "/chat/completions", pp.request_body(pp.SONAR, m, PROMPTS_A[0])))
    return out


def forms(v: str) -> set:
    """The T-6 / Wave-1 D-05 forms this test scans (raw, lower / upper, hex, base64 / base64url at 3 alignments,
    percent, JSON-escaped)."""
    b = v.encode()
    out = {v, v.lower(), v.upper(), b.hex(), quote(v), json.dumps(v)[1:-1]}
    for i in range(3):
        out |= {base64.b64encode(b"x" * i + b).decode()[4 if i else 0:][:12],
                base64.urlsafe_b64encode(b"x" * i + b).decode()[4 if i else 0:][:12]}
    # C2-3c2 (ruling 16:58 PT F3c-5): + digits-only and punycode forms
    digits = "".join(ch for ch in v if ch.isdigit())
    out |= {v.encode("punycode").decode()} | ({digits} if len(digits) >= 4 else set())
    return {f for f in out if f}


def test_table_hosts_constant_and_exemption_are_closed():
    assert "outbound" in sink_fields.SINKS and set(sink_fields.OUTBOUND_HOSTS) == set(KINDS)
    assert sink_fields.OUTBOUND_HOSTS["openai"] == oa.HOST and sink_fields.OUTBOUND_HOSTS["anthropic"] == an.HOST
    assert sink_fields.OUTBOUND_HOSTS["google"] == gg.HOST
    assert sink_fields.OUTBOUND_HOSTS["perplexity"] == sink_fields.OUTBOUND_HOSTS["perplexity.sonar"] == pp.HOST
    assert outbound.SYSTEM_PROMPT == aa.SYSTEM_PROMPT
    # C2-3c2 re-pin (ruling 16:58 PT F3c-2, G-7): sonar's shared path is role-bound ("role"), no "system|prompt"
    assert set(sink_fields.OUTBOUND_E_LLM.values()) <= {"system", "prompt", "role"}
    assert dict(sink_fields.OUTBOUND_ROLES) == {"system": "system", "user": "prompt"}
    assert {k for k, _p in sink_fields.OUTBOUND_E_LLM} == set(KINDS)
    for (kind, path) in sink_fields.OUTBOUND_E_LLM:          # E-LLM paths are not T-1 fields
        assert ("outbound", kind, path) not in sink_fields.SINK_FIELDS
    typed = [(k, p) for s, k, p in sink_fields.SINK_FIELDS if s == "outbound"]
    assert typed and all(f.types <= set(sink_guard.CLASSES) for (s, *_x), f in sink_fields.SINK_FIELDS.items()
                         if s == "outbound")


@pytest.mark.parametrize("kind,url,body", bodies(), ids=lambda x: x if isinstance(x, str) and len(x) < 20 else None)
def test_every_engine_body_has_a_decision_and_passes_for_the_bound_prompt_set(kind, url, body):
    with outbound.prompt_scope(PROMPTS_A):
        assert outbound.violation(kind, url, body) is None
    doc = json.loads(body)
    paths = {p for p, _v in outbound._leaves(doc)}
    decided = {p for k, p in sink_fields.OUTBOUND_E_LLM if k == kind} | \
        {p for s, k, p in sink_fields.SINK_FIELDS if s == "outbound" and k == kind}
    assert paths <= decided and paths                     # T-4 static half: every built path decided


@pytest.mark.parametrize("kind,url,body", bodies(), ids=lambda x: x if isinstance(x, str) and len(x) < 20 else None)
def test_unbound_other_text_and_other_host_are_refused(kind, url, body):
    assert outbound.violation(kind, url, body)[1] == "e_llm"            # no bound prompt set: fail closed
    with outbound.prompt_scope(PROMPTS_A):
        assert outbound.violation(kind, "https://audited-site.test/x", body)[1] == "host"
        assert outbound.violation(kind, "https://api.openai.com.evil.test/x", body)[1] == "host"
        for src, c in CANARY.items():
            planted = body.replace(PROMPTS_A[0].encode(), c.encode())
            assert outbound.violation(kind, url, planted)[1] == "e_llm", src
            sys_planted = body.replace(aa.SYSTEM_PROMPT.encode(), (aa.SYSTEM_PROMPT + " " + c).encode())
            assert outbound.violation(kind, url, sys_planted)[1] == "e_llm", src   # C2-3c2: sonar role-bound too


PROSE = ["see the page text here", "user@example.test", "{c:text:0123456789abcdef}", "a b", "zqx canary value 1",
         "Answer the question concisely and cite your sources."]


def test_every_typed_outbound_field_refuses_prose_and_untyped_paths_are_refused():
    n = 0
    for (s, kind, path), f in sink_fields.SINK_FIELDS.items():
        if s != "outbound":
            continue
        for v in PROSE + list(CANARY.values()):
            assert not sink_fields.matches(f, v, sink_fields.leaf_key(path)), (kind, path, v)
            n += 1
    assert n >= 13 * len(PROSE)
    with outbound.prompt_scope(PROMPTS_A):
        for kind, url, body in bodies():
            doc = json.loads(body)
            doc["note"] = "zqx-page-7f3a91"                 # a new field: no decision -> refused
            assert outbound.violation(kind, url, doc) == ("/note", "untyped")


def _engine(name, seen, **kw):
    def handler(req):
        seen.append(req)
        return httpx.Response(500)
    return CLASSES[name](aa.ApiKey(KEYS[name], "env"), transport=httpx.MockTransport(handler), sleep=lambda s: None,
                         rand=lambda: 0.0, attempts=1, **kw)


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_refused_body_is_never_sent_and_is_counted(name):
    seen = []
    e = _engine(name, seen)
    before = outbound.COUNTS[(name, "e_llm")]
    with pytest.raises(aa.EngineError) as ei:
        e.ask(PROMPTS_A[0])                                 # unbound: refused at write
    assert (ei.value.code, ei.value.detail) == ("ENGINE_PROTOCOL_ERROR", "REQUEST_REFUSED") and seen == []
    with outbound.prompt_scope(PROMPTS_A):
        with pytest.raises(aa.EngineError):
            e.ask(CANARY["other_tenant_prompt"])            # another tenant's prompt: refused, not sent
    assert seen == [] and outbound.COUNTS[(name, "e_llm")] == before + 2

    class Moved(CLASSES[name]):
        BASE_URL = "https://audited-site.test"
        ALLOWED_HOSTS = frozenset({"audited-site.test"})
    m = Moved(aa.ApiKey(KEYS[name], "env"), transport=httpx.MockTransport(lambda r: seen.append(r)), attempts=1)
    with outbound.prompt_scope(PROMPTS_A), pytest.raises(aa.EngineError):
        m.ask(PROMPTS_A[0])                                 # a prompt to a non-allowlisted host: refused
    assert seen == []


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_a_probe_run_sends_only_the_tenant_prompts_and_no_canary(name):
    """D-02m per client: a run whose prompt set is tenant A's sends A's prompts to the provider host only; canaries of
    every free-text source (page text, answers, titles, hints, operator text, excerpts, tenant B) reach 0 bodies."""
    seen = []
    e = _engine(name, seen)
    ps = {"prompts": list(PROMPTS_A)}
    # C2-3c2: the engines are METERED, so collect() asks only inside a budget reservation (its contract; without one
    # it returns BLOCKED and sends nothing) -> the suite's unlimited fake budget (test_perplexity._UnlimitedBudget).
    callum.collect(name, e, ps, 1, lambda **_k: None, budget=test_perplexity._UnlimitedBudget())
    assert seen and {r.url.host for r in seen} == {ENGINES[name].HOST}
    blob = b"\n".join(r.content for r in seen).decode() + "\n".join(str(r.url) for r in seen)
    for c in CANARY.values():
        assert not any(f in blob for f in forms(c)), c
    sent = []
    for r in seen:
        doc = json.loads(r.content)
        sent += [v for (k, p), rule in sink_fields.OUTBOUND_E_LLM.items() if k == name and rule == "prompt"
                 for q, v in outbound._leaves(doc) if q == p]
    assert set(sent) == set(PROMPTS_A) and outbound._PROMPTS.get() is None   # the scope ends with the run


def test_sonar_messages_are_role_bound():
    """C2-3c2 (lead ruling 16:58 PT F3c-2): role system -> SYSTEM_PROMPT only; role user -> the bound prompt set only;
    any other role refused."""
    url = pp.BASE_URL + "/chat/completions"
    body = json.loads(pp.request_body(pp.SONAR, pp.SONAR_MODELS[0], PROMPTS_A[0]))
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    with outbound.prompt_scope(PROMPTS_A):
        assert outbound.violation("perplexity.sonar", url, body) is None
        swapped = copy_doc(body)
        swapped["messages"][0]["content"], swapped["messages"][1]["content"] = PROMPTS_A[0], aa.SYSTEM_PROMPT
        assert outbound.violation("perplexity.sonar", url, swapped) == ("/messages[]/content", "e_llm")
        for role in ("assistant", "tool", "developer", None):
            other = copy_doc(body)
            other["messages"][1]["role"] = role
            assert outbound.violation("perplexity.sonar", url, other) is not None, role


def copy_doc(doc):
    return json.loads(json.dumps(doc))
