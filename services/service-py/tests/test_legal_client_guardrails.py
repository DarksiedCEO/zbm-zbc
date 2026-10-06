"""The Legal (37) thin client (bounded, refs only, echo checked) and repository guardrails: no float in src, no LLM
SDK, stand-ins never pass, every reason code catalogued, every ledger name valid."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import httpx

import ledger
import legal_client
import ports as ports_mod
import reasons
from ports import HandoffRequest

SRC = Path(__file__).resolve().parents[1] / "src"
REQ = HandoffRequest("sv-hof-" + "1" * 40, "sv-tkt-" + "2" * 40, "zbm", "contract", "litigation_threat", "acct-1")


def _client(handler, timeout=10.0):
    return legal_client.HttpLegal("http://legal.test", "s" * 40, "c" * 40, transport=httpx.MockTransport(handler),
                                  timeout=timeout)


def test_delivered_with_the_matter_reference_and_refs_only():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(201, json={"request_id": REQ.handoff_id, "matter_id": "lg-mat-1", "status": "open"})
    res = _client(handler).handoff(REQ)
    assert res.status == "delivered" and res.reference == "lg-mat-1"
    assert seen["body"] == {"request_id": REQ.handoff_id, "channel": "department",
                            "requester_ref": f"svc:{REQ.ticket_id}", "kind": "litigation_threat",
                            "facts": {"dsar": False}, "subject_refs": [f"svc:ticket:{REQ.ticket_id}",
                                                                       "client:acct-1"]}
    assert seen["headers"]["x-legal-caller-token"] == "c" * 40


def test_an_answer_that_does_not_echo_our_request_is_not_delivery():
    res = _client(lambda r: httpx.Response(201, json={"request_id": "other", "matter_id": "m"})).handoff(REQ)
    assert res.status == "unavailable"
    res = _client(lambda r: httpx.Response(201, content=b"not json")).handoff(REQ)
    assert res.status == "unavailable"


def test_5xx_retried_once_4xx_refused():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)
    assert _client(handler).handoff(REQ).status == "unavailable" and len(calls) == 2
    assert _client(lambda r: httpx.Response(403)).handoff(REQ).status == "refused"


def test_transport_error_and_oversize_answer():
    def boom(request):
        raise httpx.ConnectError("down")
    assert _client(boom).handoff(REQ).status == "unavailable"
    big = httpx.Response(201, content=b"x" * (legal_client.MAX_RESPONSE_BYTES + 10))
    assert _client(lambda r: big).handoff(REQ).status == "unavailable"


def test_an_unknown_kind_is_never_sent():
    req = HandoffRequest(REQ.handoff_id, REQ.ticket_id, "zbm", "money", "money", None)
    assert _client(lambda r: (_ for _ in ()).throw(AssertionError("called"))).handoff(req).status == "refused"


# --------------------------------------------------------------------------------------------------- guardrails

ALLOWED_FLOAT = {"serve.py", "launch_guard.py", "graceful_close.py", "api.py", "ledger.py", "legal_client.py",
                 "clock.py"}       # timeouts and switch intervals; never money


def test_no_float_in_any_money_or_domain_path():
    for f in sorted(SRC.rglob("*.py")):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "float":
                assert f.name in ALLOWED_FLOAT, f"float() in {f}"
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                assert f.name in ALLOWED_FLOAT, f"float literal {node.value} in {f}:{node.lineno}"


def test_no_llm_sdk_imported():
    banned = re.compile(r"^\s*(import|from)\s+(anthropic|openai|google\.generativeai|langchain|llama|transformers|"
                        r"cohere|mistralai)\b", re.M)
    for f in sorted(SRC.rglob("*.py")):
        assert not banned.search(f.read_text()), f


def test_stand_ins_never_pass():
    p = ports_mod.Ports.default()
    assert all(s.send(None) == "not_wired" and s.wired is False for s in p.senders.values())
    assert p.alerts.send(None) == "not_wired"
    assert all(x.handoff(REQ).status == "not_wired" for x in p.handoffs.values())
    assert p.results.trend("a").available is False and p.finance.payment_status("a").available is False
    assert all(c.contract_end("a").available is False for c in p.contracts.values())
    for f in sorted(SRC.rglob("*.py")):
        assert "class Recording" not in f.read_text() and "class Fake" not in f.read_text()


def test_every_reason_code_used_is_catalogued():
    used = set()
    for f in sorted(SRC.rglob("*.py")):
        used |= set(re.findall(r'R\("([A-Z_]+)"\)', f.read_text()))
        used |= set(re.findall(r'_alert_effect\("([A-Z_]+)"', f.read_text()))
    assert used and used <= reasons.CODES
    import channels
    import inspect
    assert set(re.findall(r'"([A-Z_]{4,})"', inspect.getsource(channels.check))) <= reasons.CODES


def test_ledger_department_and_ids():
    assert ledger.DEPARTMENT == "service"
    assert re.fullmatch(r"sv-abc-[0-9a-f]{40}", ledger.derived_id("abc", "x"))


def test_typed_event_names_are_ledger_valid():
    import service
    for et in set(service.EVENTS.values()) | {"message_sent", "answer_queued", "log_anchor"}:
        assert re.fullmatch(r"[a-z0-9_]{1,64}", et)
