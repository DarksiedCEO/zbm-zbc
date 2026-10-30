"""ADR 0024 (founder decisions, Oct 30, 2026): a Revenue Recovery store is read, and its snapshot handed to the scan
path, only while Compliance (38) answers a CURRENT ALLOW for the client. The client is checked against compliance-py's
exact answer shape (``activation_status`` in compliance-py ``src/service.py``). No network: httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from _store_fakes import NOW, TOKEN, AllowCompliance, FakeShopify, order, write_secret
from store_connector.compliance import HttpCompliance, NotConfiguredCompliance, compliance_from_env
from store_connector.config import Secret
from store_connector.reasons import NO_OUTBOUND, REASONS, StoreError
from test_store_connector import _service

CLIENT = "acme"


def doc(**over):
    d = {"client_id": CLIENT, "lane": "client", "decision": "ALLOW", "current": True, "ruling_id": "cmp-rul-abc",
         "evaluated_at": "2026-10-30T12:00:00Z", "register_version": 1, "jurisdiction": "US-CA",
         "valid_until": "2027-01-28T12:00:00Z", "reasons": [], "seed_pinned": True}
    d.update(over)
    return d


def client(answer=None, status=200, seen=None, exc=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        if exc is not None:
            raise exc("x", request=req)
        return httpx.Response(status, content=json.dumps(answer if answer is not None else doc()).encode())
    return HttpCompliance("http://compliance.railway.internal:8380", Secret("s" * 40), Secret("c" * 40),
                          transport=httpx.MockTransport(handler), timeout_s=1)


def code_of(fn):
    with pytest.raises(StoreError) as e:
        fn()
    return e.value.code


def test_a_current_pinned_allow_passes_with_both_tokens():
    seen = []
    assert client(seen=seen).require_allow(CLIENT) == {"ruling_id": "cmp-rul-abc", "jurisdiction": "US-CA"}
    req = seen[0]
    assert req.url.path == f"/compliance/v1/clients/{CLIENT}/activation"
    assert req.headers["authorization"] == "Bearer " + "s" * 40 and req.headers["x-compliance-caller-token"] == "c" * 40


@pytest.mark.parametrize("over", [{"decision": "REFUSE", "current": False}, {"current": False},
                                  {"seed_pinned": False}, {"jurisdiction": None}, {"ruling_id": None}])
def test_anything_but_a_current_pinned_allow_is_not_activated(over):
    assert code_of(lambda: client(doc(**over)).require_allow(CLIENT)) == "COMPLIANCE_NOT_ACTIVATED"


@pytest.mark.parametrize("kw", [{"status": 500}, {"status": 503}, {"status": 403}, {"status": 404},
                                {"exc": httpx.ConnectError}, {"answer": doc(client_id="bravo")},
                                {"answer": doc(lane="zbc_brand")}, {"answer": doc(jurisdiction="CA-ON")},
                                {"answer": doc(decision="MAYBE")}, {"answer": ["not", "a", "dict"]}])
def test_no_usable_answer_is_unavailable(kw):
    assert code_of(lambda: client(**kw).require_allow(CLIENT)) == "COMPLIANCE_UNAVAILABLE"


def test_the_gate_codes_are_no_outbound_refusals():
    assert {"COMPLIANCE_NOT_ACTIVATED", "COMPLIANCE_UNAVAILABLE"} <= NO_OUTBOUND
    assert REASONS["COMPLIANCE_NOT_ACTIVATED"][0] == 409 and REASONS["COMPLIANCE_UNAVAILABLE"][0] == 503


def test_compliance_down_refuses_the_sync_and_no_store_call_is_made(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    cmp_ = AllowCompliance()
    cmp_.unavailable = True
    svc = _service(tmp_path, fake, compliance=cmp_)
    assert code_of(lambda: svc.sync(CLIENT)) == "COMPLIANCE_UNAVAILABLE"
    assert fake.requests == []                      # no store call was made
    assert cmp_.asked == [CLIENT]


def test_the_production_default_refuses_every_client(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    svc = _service(tmp_path, fake, compliance=None)
    assert code_of(lambda: svc.sync(CLIENT)) == "COMPLIANCE_UNAVAILABLE"
    assert isinstance(NotConfiguredCompliance(), NotConfiguredCompliance)


def test_a_lapsed_activation_stops_the_snapshot_handed_to_the_scan_path(tmp_path):
    fake = FakeShopify(orders=[order(1)])
    cmp_ = AllowCompliance()
    svc = _service(tmp_path, fake, compliance=cmp_)
    svc.sync(CLIENT)
    assert svc.snapshot(CLIENT)
    cmp_.refuse.add(CLIENT)
    assert code_of(lambda: svc.snapshot(CLIENT)) == "COMPLIANCE_NOT_ACTIVATED"
    assert code_of(lambda: svc.sync(CLIENT)) == "COMPLIANCE_NOT_ACTIVATED"


def test_a_port_that_raises_is_unavailable(tmp_path):
    class Boom:
        def require_allow(self, client_id):
            raise RuntimeError("boom")
    svc = _service(tmp_path, FakeShopify(orders=[order(1)]), compliance=Boom())
    assert code_of(lambda: svc.sync(CLIENT)) == "COMPLIANCE_UNAVAILABLE"


def test_settings_from_env(tmp_path):
    tok = write_secret(tmp_path, "cmp.token", "s" * 40)
    call = write_secret(tmp_path, "cmp.caller", "c" * 40)
    env = {"DETECTION_COMPLIANCE_URL": "http://compliance.railway.internal:8380",
           "DETECTION_COMPLIANCE_TOKEN_FILE": tok, "DETECTION_COMPLIANCE_CALLER_TOKEN_FILE": call}
    assert isinstance(compliance_from_env(env), HttpCompliance)
    for bad in ({**env, "DETECTION_COMPLIANCE_URL": ""}, {**env, "DETECTION_COMPLIANCE_TOKEN_FILE": ""},
                {**env, "DETECTION_COMPLIANCE_URL": "http://evil.example:8380"},
                {**env, "DETECTION_COMPLIANCE_CALLER_TOKEN_FILE": str(tmp_path / "missing")}, {}):
        assert isinstance(compliance_from_env(bad), NotConfiguredCompliance), bad
    assert "s" * 40 not in repr(compliance_from_env(env))
