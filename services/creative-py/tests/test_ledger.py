"""LedgerClient per BUILD_CONTRACTS section 2 — HTTP client via httpx.MockTransport (no network)."""

import hashlib
import json

import httpx
import pytest

from shared.ledger import (
    DEPARTMENT,
    EvidenceRecorder,
    FakeLedgerClient,
    HttpLedgerClient,
    LedgerRecordError,
    UnconfiguredLedgerClient,
    payload_sha256,
)

ARGS = dict(event_id="cp:x:1", department="creative_production", event_type="brief_approved",
            actor="zbm_creative_lead", subject_id="brief-0001", payload={"b": 2, "a": [1, "x"]}, summary="ok")


def _client(handler):
    return HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))


def test_payload_hash_matches_contract():
    expected = hashlib.sha256(json.dumps({"b": 2, "a": [1, "x"]}, sort_keys=True, separators=(",", ":"),
                                         default=str).encode()).hexdigest()
    assert payload_sha256({"b": 2, "a": [1, "x"]}) == expected


def test_http_client_posts_exact_contract_body_and_bearer():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"seq": 1})

    _client(handler).record_event(**ARGS)
    assert seen["url"] == "http://ledger.test/ledger/events"
    assert seen["auth"] == "Bearer tok"
    assert set(seen["body"]) == {"event_id", "department", "event_type", "actor", "subject_id", "payload_sha256", "summary"}
    assert seen["body"]["payload_sha256"] == payload_sha256(ARGS["payload"])


@pytest.mark.parametrize("code", [200, 201])
def test_http_client_accepts_new_and_idempotent(code):
    _client(lambda r: httpx.Response(code, json={})).record_event(**ARGS)


@pytest.mark.parametrize("code", [400, 401, 409, 500, 503])
def test_http_client_raises_on_refusal(code):
    with pytest.raises(LedgerRecordError):
        _client(lambda r: httpx.Response(code, json={})).record_event(**ARGS)


def test_http_client_raises_when_unreachable():
    def boom(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LedgerRecordError, match="unreachable"):
        _client(boom).record_event(**ARGS)


def test_local_contract_validation_before_sending():
    calls = []
    c = _client(lambda r: calls.append(r) or httpx.Response(201))
    for bad in ({"department": "Creative-Production"}, {"actor": "Bad Actor"}, {"subject_id": "has space"},
                {"summary": "x" * 281}, {"summary": "line\nbreak"}, {"event_id": ""}):
        with pytest.raises(LedgerRecordError):
            c.record_event(**{**ARGS, **bad})
    assert calls == []


def test_fake_has_contract_idempotency():
    f = FakeLedgerClient()
    f.record_event(**ARGS)
    f.record_event(**ARGS)  # identical retry is fine
    assert len(f.events) == 1
    with pytest.raises(LedgerRecordError):
        f.record_event(**{**ARGS, "summary": "different"})


def test_unconfigured_always_fails():
    with pytest.raises(LedgerRecordError, match="not configured"):
        UnconfiguredLedgerClient().record_event(**ARGS)


def test_recorder_always_uses_creative_production_and_cleans_summary():
    f = FakeLedgerClient()
    EvidenceRecorder(f).record("clip_reviewed", "zbc_clip_review", "clip_1", {}, "a\nb" + "y" * 400)
    e = f.events[0]
    assert e["department"] == DEPARTMENT == "creative_production"
    assert len(e["summary"]) == 280 and "\n" not in e["summary"]


def test_from_env_needs_both_vars(monkeypatch):
    monkeypatch.delenv("LEDGER_SERVICE_URL", raising=False)
    monkeypatch.setenv("LEDGER_SERVICE_TOKEN", "t")
    with pytest.raises(ValueError):
        HttpLedgerClient.from_env()
