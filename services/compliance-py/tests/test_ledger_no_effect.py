"""Record-first: when the ledger (or the local store) cannot record, nothing takes effect (spec §G, C.9, B.7)."""

import copy

import httpx
import pytest

from helpers import ANDRE_TOKEN, Harness, client_facts, rid
from ledger import HttpLedgerClient, LedgerConflict, LedgerNotRecorded, LedgerRecordError, payload_sha256


def _snapshot(svc):
    return copy.deepcopy((svc.versions, svc.proposals, svc.rulings, svc.screens, svc.a11y, svc.control_state, svc.holds,
                          svc.control_defs, svc.snapshots, svc.last_cycle, svc.sanctions_list, len(svc.log)))


def _ops(h):
    """Each write route with a valid body."""
    seed = [p for p in h.svc.proposals.values() if p["kind"] == "seed"][0]
    row = dict(h.svc.current.by_id()["US-FTC-437-01"]) if h.svc.current else {}
    return [
        ("decision", lambda: h.post("/compliance/v1/register/decisions", {"request_id": rid(), "decisions": [
            {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]},
            andre=ANDRE_TOKEN)),
        ("gate", lambda: h.rule("client-1", "client", client_facts())),
        ("screen", lambda: h.post("/compliance/v1/sanctions/screen", {"request_id": rid(), "subject_id": "s", "role": "payee",
                                                                       "legal_name": "N", "country": "US", "region": "US-CA"},
                                  caller="onboarding")),
        ("a11y", lambda: h.post("/compliance/v1/accessibility/checks", {"request_id": rid(), "asset_ref": "a",
                                                                         "asset_type": "site", "content_sha256": "a" * 64,
                                                                         "owner_id": "o"}, caller="creative_production")),
        ("control", lambda: h.post("/compliance/v1/controls/C-08/results", {"request_id": rid(), "result": "pass",
                                                                             "tested_at": "2026-09-26T10:00:00Z", "evidence": []},
                                   caller="people_43")),
        ("internal", lambda: h.post("/compliance/v1/controls/internal/run", {"request_id": rid()}, caller="scheduler")),
        ("resolve", lambda: h.post("/compliance/v1/jurisdictions/resolve", {"request_id": rid(), "targets": ["US"]},
                                   caller="scheduler")),
        ("proposal", lambda: h.propose({"kind": "amend", "target_id": "US-FTC-437-01",
                                        "proposed_row": {**row, "status": "unverified", "verified_at": None}})),
        ("audit", lambda: h.get("/compliance/v1/audit/export")),
    ]


@pytest.mark.parametrize("name", ["decision", "gate", "screen", "a11y", "control", "internal", "resolve", "proposal", "audit"])
def test_ledger_down_every_write_is_503_and_changes_nothing(name):
    h = Harness()
    if name != "decision":
        h.approve_seed()
    op = dict(_ops(h))[name]
    before = _snapshot(h.svc)
    h.ledger.fail_all = True
    r = op()
    assert r.status_code == 503, (name, r.status_code, r.text)
    assert r.json()["issued"] is False
    assert _snapshot(h.svc) == before, name


@pytest.mark.parametrize("name", ["decision", "gate", "screen", "a11y", "control", "resolve", "proposal"])
def test_local_store_failure_is_503_and_changes_nothing(name):
    h = Harness()
    if name != "decision":
        h.approve_seed()
    op = dict(_ops(h))[name]
    before = _snapshot(h.svc)
    h.svc.log.fail_next_append = True
    r = op()
    assert r.status_code == 503, (name, r.text)
    after = _snapshot(h.svc)
    assert after == before, name


def test_hold_release_needs_its_record(hs):
    hs.ports.sanctions.result = "potential_match"
    s = hs.screen("clipper-1")
    hs.ledger.fail_all = True
    r = hs.post(f"/compliance/v1/holds/{s['hold_id']}/release", {"request_id": rid(), "reason": "ok"}, andre=ANDRE_TOKEN)
    assert r.status_code == 503 and hs.svc.holds[s["hold_id"]]["status"] == "open"


def test_seed_proposal_waits_for_the_ledger():
    from fakes import FakeLedgerClient
    led = FakeLedgerClient(fail_all=True)
    h = Harness(ledger=led)
    assert not h.svc.proposals
    assert h.get("/compliance/v1/inbox").status_code == 503
    led.fail_all = False
    assert [p["kind"] for p in h.inbox()] == ["seed"]


def test_refused_approval_stands_even_when_it_cannot_be_recorded(h):
    h.ledger.fail_all = True
    seed = [p for p in h.svc.proposals.values() if p["kind"] == "seed"][0]
    r = h.post("/compliance/v1/register/decisions", {"request_id": rid(), "decisions": [
        {"proposal_id": seed["proposal_id"], "content_sha256": seed["content_sha256"], "decision": "approve"}]}, andre="bad")
    assert r.status_code == 403 and h.svc.version_number is None


def test_a_port_that_raises_is_unavailable_never_a_pass(hs):
    from fakes import ExplodingPort
    hs.ports.verification = ExplodingPort()
    s = hs.screen("clipper-1")
    from helpers import creator_facts
    r = hs.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"])).json()
    assert ("HR-02", "dependency_unavailable:verification_integrity") in {(u["obligation_id"], u["code"]) for u in r["unmet"]}


# --- HttpLedgerClient against a mock transport (no network) -------------------------------------

def _client(status, seen=None, body=None):
    def handler(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(status, json=body if body is not None else {"seq": 1})
    return HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))


def test_http_ledger_posts_the_contract_body():
    seen = []
    _client(201, seen).record_event("cmp-a-1", "compliance", "activation_ruling", "intel_02_activation_gate", "client-1",
                                    {"x": 1}, "Activation blocked: 1 unmet")
    import json
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/ledger/events" and seen[0].headers["Authorization"] == "Bearer tok"
    assert body == {"event_id": "cmp-a-1", "department": "compliance", "event_type": "activation_ruling",
                    "actor": "intel_02_activation_gate", "subject_id": "client-1", "payload_sha256": payload_sha256({"x": 1}),
                    "summary": "Activation blocked: 1 unmet"}


@pytest.mark.parametrize("status, exc", [(400, LedgerNotRecorded), (401, LedgerNotRecorded), (409, LedgerConflict),
                                         (500, LedgerRecordError), (503, LedgerRecordError)])
def test_http_ledger_failures(status, exc):
    with pytest.raises(exc):
        _client(status).record_event("cmp-a-1", "compliance", "t", "a", "s", {}, "x")
    _client(200).record_event("cmp-a-1", "compliance", "t", "a", "s", {}, "x")  # idempotent retry is success


def test_http_ledger_unreachable_and_verify():
    def boom(req):
        raise httpx.ConnectError("refused")
    lc = HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(boom))
    with pytest.raises(LedgerNotRecorded):
        lc.record_event("cmp-a-1", "compliance", "t", "a", "s", {}, "x")
    assert lc.verify() is False
    assert _client(200, body={"valid": True, "entries": 3}).verify() is True
    assert _client(409, body={"valid": False}).verify() is False
    assert _client(200, body={"valid": "yes"}).verify() is False
