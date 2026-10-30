"""ADR 0024 (founder decisions, Oct 30, 2026): a client tenant's paid audit, every slot of its schedule and its client
report need Compliance (38)'s CURRENT activation of the client; there is no override. The client (compliance_client.py)
is checked against compliance-py's exact answer shape (``activation_status`` in compliance-py ``src/service.py``)."""

from __future__ import annotations

import json

import httpx
import pytest

import compliance_client as cc
import config as config_mod
from helpers import Harness, PassingCompliance, base_env, derived, rid
from ports import Ports
from test_invoice_verification import CLIENT, FakeFinance, client_tenant, harness, paid_audit, schedule, tick

pytestmark = pytest.mark.local_http

BASE = "http://compliance.railway.internal:8380"
CMP_SERVICE = derived("compliance service token")
CMP_CALLER = derived("compliance seo_02 caller token")


def activation_doc(**over) -> dict:
    d = {"client_id": CLIENT, "lane": "client", "decision": "ALLOW", "current": True, "ruling_id": "cmp-rul-abc",
         "evaluated_at": "2026-10-30T12:00:00Z", "register_version": 1, "jurisdiction": "US-CA",
         "valid_until": "2027-01-28T12:00:00Z", "reasons": [], "seed_pinned": True}
    d.update(over)
    return d


class FakeCompliance:
    def __init__(self):
        self.doc = activation_doc()
        self.mode = None
        self.calls: list = []
        self.now = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if self.mode in (500, 503, 401, 403, 404):
            return httpx.Response(self.mode, json={"detail": "x"})
        if request.headers.get("authorization") != f"Bearer {CMP_SERVICE}" or \
                request.headers.get(cc.CMP_CALLER_HEADER) != CMP_CALLER:
            return httpx.Response(403, json={"detail": "caller token missing or not recognised"})
        return httpx.Response(200, json=self.doc)

    def client(self) -> cc.ComplianceClient:
        return cc.ComplianceClient(BASE, CMP_SERVICE, CMP_CALLER, transport=httpx.MockTransport(self.handler),
                                   sleep=lambda s: None, monotonic=lambda: self.now)


# --- the client --------------------------------------------------------------------------------------------------------

def test_a_current_pinned_allow_passes_and_both_tokens_are_sent():
    f = FakeCompliance()
    assert f.client().activation(CLIENT) == {"verdict": "ALLOW", "ruling_id": "cmp-rul-abc", "jurisdiction": "US-CA"}
    (req,) = f.calls
    assert req.method == "GET" and str(req.url) == f"{BASE}/compliance/v1/clients/{CLIENT}/activation"
    assert req.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("over", [{"decision": "REFUSE", "current": False}, {"current": False},
                                  {"seed_pinned": False}, {"jurisdiction": None}, {"ruling_id": None}])
def test_anything_but_a_current_pinned_allow_is_not_activated(over):
    f = FakeCompliance()
    f.doc = activation_doc(**over)
    with pytest.raises(cc.ComplianceCheckError) as e:
        f.client().activation(CLIENT)
    assert e.value.code == "COMPLIANCE_NOT_ACTIVATED"


@pytest.mark.parametrize("mode, code", [("down", "COMPLIANCE_UNAVAILABLE"), (503, "COMPLIANCE_UNAVAILABLE"),
                                        (401, "COMPLIANCE_AUTH_REFUSED"), (403, "COMPLIANCE_AUTH_REFUSED"),
                                        (404, "COMPLIANCE_RESPONSE_INVALID")])
def test_no_usable_answer_is_unverifiable(mode, code):
    f = FakeCompliance()
    f.mode = mode
    with pytest.raises(cc.ComplianceCheckError) as e:
        f.client().activation(CLIENT)
    assert e.value.code == code


@pytest.mark.parametrize("over", [{"client_id": "someone-else"}, {"lane": "zbc_brand"}, {"jurisdiction": "CA-ON"},
                                  {"decision": "MAYBE"}, {"current": "yes"}])
def test_a_malformed_or_foreign_answer_is_invalid(over):
    f = FakeCompliance()
    f.doc = activation_doc(**over)
    with pytest.raises(cc.ComplianceCheckError) as e:
        f.client().activation(CLIENT)
    assert e.value.code == "COMPLIANCE_RESPONSE_INVALID"


def test_the_production_default_is_not_connected_and_refuses():
    with pytest.raises(cc.ComplianceCheckError) as e:
        Ports.default().compliance.activation(CLIENT)
    assert e.value.code == "COMPLIANCE_NOT_CONFIGURED"
    with pytest.raises(AssertionError):
        FakeCompliance().client().lookup("fin-inv-0")


def test_settings_come_as_a_set_and_wire_the_client():
    env = base_env(SEO_COMPLIANCE_URL=BASE, SEO_COMPLIANCE_TOKEN=CMP_SERVICE, SEO_COMPLIANCE_CALLER_TOKEN=CMP_CALLER)
    s = config_mod.load(env)
    assert isinstance(Ports.default(s).compliance, cc.ComplianceClient)
    assert Ports.default(s).status()["compliance"] == "connected"
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SEO_COMPLIANCE_URL=BASE, SEO_COMPLIANCE_TOKEN=CMP_SERVICE))
    with pytest.raises(RuntimeError):
        config_mod.load(base_env(SEO_COMPLIANCE_URL=BASE, SEO_COMPLIANCE_TOKEN=CMP_SERVICE,
                                 SEO_COMPLIANCE_CALLER_TOKEN=CMP_SERVICE))


# --- the paid-run gate ----------------------------------------------------------------------------------------------

def test_compliance_down_refuses_a_paid_audit_even_with_andre_s_override(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    cmp_ = FakeCompliance()
    cmp_.mode = "down"
    h.ports.compliance = cmp_.client()
    client_tenant(h)
    iid = f.add()
    r = h.refused(paid_audit(h, invoice_id=iid), 503, "COMPLIANCE_UNAVAILABLE")
    assert r.get("override_allowed") is False
    f.mode = "down"
    h.refused(paid_audit(h, invoice_id=iid, invoice_override="FINANCE_OUTAGE"), 503,
              "COMPLIANCE_UNAVAILABLE")
    assert not h.ledger.of_type("audit_requested")


def test_not_activated_refuses_and_nothing_is_recorded(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    h.ports.compliance.refuse.add(CLIENT)
    client_tenant(h)
    h.refused(paid_audit(h, invoice_id=f.add()), 409, "COMPLIANCE_NOT_ACTIVATED")
    assert not h.ledger.of_type("audit_requested") and not f.calls      # Compliance first, Finance never asked


def test_not_connected_refuses_every_client_run(tmp_path, srv):
    f = FakeFinance()
    from test_invoice_verification import install_site
    install_site(srv)
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    ports.finance = f.client()
    h = Harness(tmp_path, ports=ports, real_compliance=True)
    client_tenant(h)
    h.refused(paid_audit(h, invoice_id=f.add()), 503, "COMPLIANCE_NOT_CONFIGURED")


def test_an_unbound_client_tenant_is_refused_in_trust_mode_too(tmp_path, srv):
    h = harness(tmp_path, srv, None, SEO_INVOICE_VERIFICATION="trust")
    client_tenant(h, bind=None)
    h.refused(paid_audit(h, invoice_id="fin-inv-01J00000000000000000000000"), 409, "TENANT_FINANCE_CLIENT_UNBOUND")


def test_an_allowed_run_records_the_activation(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    a = h.ok(paid_audit(h, invoice_id=f.add()), 201)
    assert a["invoice_verification"]["compliance"] == {"verdict": "ALLOW", "ruling_id": f"cmp-rul-{CLIENT}",
                                                       "jurisdiction": "US-CA"}
    ev = h.ledger.of_type("compliance_activation_confirmed")
    assert ev and ev[0]["subject_id"].startswith("audit:")
    assert h.ports.compliance.asked == [CLIENT]


def test_own_tenants_never_ask_compliance(tmp_path, srv):
    h = harness(tmp_path, srv, None)
    h.tenant("zbm-own", kind="own", domains=("site.test",))
    h.ok(paid_audit(h, tid="zbm-own", andre=False), 201)
    assert h.ports.compliance.asked == []


def test_a_lapsed_activation_stops_the_next_scheduled_slot(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    s = h.ok(schedule(h, invoice_id=f.add()), 201)
    assert tick(h)["ran"] == 1
    h.clock.advance(days=7)
    h.ports.compliance.refuse.add(CLIENT)
    t = tick(h)
    assert t["ran"] == 0 and t["refused"] == 1
    slots = h.ok(h.get(f"/tenants/acme/schedules/{s['schedule_id']}"))["slots"]
    assert "1" not in slots or slots["1"].get("audit_id") is None
    skipped = h.ledger.of_type("schedule_slot_skipped")
    assert skipped


def test_compliance_unreachable_at_a_tick_makes_the_slot_wait(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    h.ok(schedule(h, invoice_id=f.add()), 201)
    h.ports.compliance.error = "COMPLIANCE_UNAVAILABLE"
    t = tick(h)
    assert t["ran"] == 0 and t["invoice_unverifiable"] == 1
    h.ports.compliance.error = None
    assert tick(h)["ran"] == 1


def test_a_client_report_needs_a_current_allow(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    a = h.ok(paid_audit(h, invoice_id=f.add()), 201)
    body = {"request_id": rid(), "format": "markdown"}
    h.ports.compliance.error = "COMPLIANCE_UNAVAILABLE"
    h.refused(h.post(f"/tenants/acme/audits/{a['audit_id']}/client-report", body, andre=True), 503,
              "COMPLIANCE_UNAVAILABLE")
    h.ports.compliance.error = None
    h.ports.compliance.refuse.add(CLIENT)
    h.refused(h.post(f"/tenants/acme/audits/{a['audit_id']}/client-report", {**body, "request_id": rid()},
                     andre=True), 409, "COMPLIANCE_NOT_ACTIVATED")
    h.ports.compliance.refuse.clear()
    r = h.ok(h.post(f"/tenants/acme/audits/{a['audit_id']}/client-report", {**body, "request_id": rid()}, andre=True))
    assert r


def test_the_status_route_shows_compliance(tmp_path, srv):
    h = harness(tmp_path, srv, None)
    st = h.ok(h.get("/status"))
    assert "compliance" in json.dumps(st)
    assert isinstance(h.ports.compliance, PassingCompliance)
