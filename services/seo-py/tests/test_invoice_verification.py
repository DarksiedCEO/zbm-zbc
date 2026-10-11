"""Wave 3, W3-2: the Finance (31) invoice verification client and the paid-run gate (ADR 0017).

Finance is a fake behind httpx.MockTransport (no socket): it answers like finance-py's ``GET /fin/v1/invoices/{id}``
(the record ``svc_books.draft_invoice`` builds, plus the fields later decisions add), checks the service bearer token
and the ``seo_02`` caller token, and can be made to go down, time out, answer 503 / garbage / another invoice / an
oversized or encoded body. finance-py's own suite checks the parser against Finance's REAL representation
(services/finance-py/tests/test_seo_invoice_contract.py). No wall-clock assertion: retries sleep through an injected
function and the overall deadline runs on an injected monotonic clock."""

from __future__ import annotations

import json

import httpx
import pytest

import config as config_mod
import finance_client as fc
from fixture_server import home_html, install_site
from helpers import FakeLedger, Harness, base_env, derived, invoice_id, rid
from ports import Ports

pytestmark = pytest.mark.local_http

BASE = "http://finance.internal:8410"
FIN_SERVICE = derived("finance service token")
FIN_CALLER = derived("finance seo_02 caller token")
CLIENT = "acme-party-1"


def finance_invoice(iid: str, status: str = "paid", entity: str = "zbm", client: str = CLIENT, **extra) -> dict:
    """finance-py's invoice record shape (svc_books.draft_invoice + decide_invoice + the payment that marks it paid)."""
    inv = {"invoice_id": iid, "entity": entity, "client_id": client, "campaign_id": None, "kind": "service",
           "lines": [{"line_code": "strategy_services", "quantity": 1, "unit_price": "2000.00", "amount": "2000.00",
                      "description": "SEO audit"}],
           "total": "2000.00", "currency": "USD", "payment_methods": ["ach"], "due_days": 15,
           "legal_ref": {"doc_id": "msa-1", "version": 1, "doc_sha256": "b" * 64, "acceptance_id": "acc-1"},
           "recurring": None, "notes_sha256": None, "template_vars": {"contact": "Pat"}, "drafted_by": "onboarding",
           "drafted_at": "2026-10-01T17:00:00Z", "status": status,
           "tax_treatment": {"status": "verified", "basis_row": "FIN-CQ-11"}, "content_sha256": "c" * 64}
    if status in ("issued", "paid"):
        inv.update(approved_by="andre", issued_at="2026-10-01T18:00:00Z", due_at="2026-10-16")
    if status == "paid":
        inv["paid_at"] = "2026-10-02T17:00:00Z"
    inv.update(extra)
    return inv


class FakeFinance:
    def __init__(self):
        self.invoices: dict = {}
        self.calls: list = []
        self.sleeps: list = []
        self.mode = None
        self.on_call = None
        self.now = 0.0
        self.cost = 0.0                                    # simulated seconds each call takes (injected clock)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.now += self.cost
        if self.on_call is not None:
            hook, self.on_call = self.on_call, None
            hook(request)
        mode = self.mode.pop(0) if isinstance(self.mode, list) else self.mode
        if mode == "down":
            raise httpx.ConnectError("refused", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if mode in (500, 502, 503, 504, 429, 418):
            return httpx.Response(mode, json={"detail": "busy"})
        if mode == "garbage":
            return httpx.Response(200, content=b"<html>proxy error</html>")
        if mode == "redirect":
            return httpx.Response(302, headers={"Location": "http://evil.test/"})
        if mode == "huge":
            return httpx.Response(200, content=b'{"x": "' + b"a" * (fc.MAX_RESPONSE_BYTES + 10) + b'"}')
        if mode == "gzip":
            return httpx.Response(200, content=b"\x1f\x8b", headers={"Content-Encoding": "gzip"})
        if mode == "route404":
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.headers.get("authorization") != f"Bearer {FIN_SERVICE}":
            return httpx.Response(401, json={"detail": "invalid token"})
        if request.headers.get(fc.FIN_CALLER_HEADER) != FIN_CALLER:
            return httpx.Response(403, json={"detail": "caller token missing or not recognised"})
        iid = request.url.path.rsplit("/", 1)[-1]
        inv = self.invoices.get(iid)
        if mode == "other":                                # an answer about a different invoice (a replayed body)
            inv = finance_invoice(invoice_id(999))
        if inv is None:
            return httpx.Response(404, json={"detail": "no such invoice"})
        return httpx.Response(200, json=inv)

    def mono(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def client(self, **kw) -> fc.FinanceClient:
        args = {"transport": httpx.MockTransport(self.handler), "sleep": self.sleep, "monotonic": self.mono}
        args.update(kw)
        return fc.FinanceClient(BASE, FIN_SERVICE, FIN_CALLER, **args)

    def add(self, n: int = 1, **kw) -> str:
        iid = invoice_id(n)
        self.invoices[iid] = finance_invoice(iid, **kw)
        return iid


# ================================================================================================ the client


def test_paid_invoice_keeps_only_the_minimal_facts_and_sends_both_tokens():
    f = FakeFinance()
    iid = f.add()
    facts = f.client().lookup(iid)
    assert facts == {"invoice_id": iid, "status": "paid", "entity": "zbm", "client_id": CLIENT, "kind": "service",
                     "currency": "USD", "refunded": False, "charged_back": False, "paid_at": "2026-10-02T17:00:00Z"}
    assert fc.assess(facts, CLIENT) == fc.PAID
    (req,) = f.calls
    assert req.method == "GET" and str(req.url) == f"{BASE}/fin/v1/invoices/{iid}"
    assert req.headers["authorization"] == f"Bearer {FIN_SERVICE}" and req.headers[fc.FIN_CALLER_HEADER] == FIN_CALLER
    assert req.headers["accept-encoding"] == "identity"
    assert "2000.00" not in json.dumps(facts) and "Pat" not in json.dumps(facts)


def test_finance_down_is_retried_a_bounded_number_of_times_then_unavailable():
    f = FakeFinance()
    f.mode = "down"
    with pytest.raises(fc.FinanceLookupError) as e:
        f.client().lookup(f.add())
    assert e.value.code == "FINANCE_UNAVAILABLE" and len(f.calls) == 3 and f.sleeps == [0.2, 0.5]


@pytest.mark.parametrize("transient", ["timeout", 503, 502, 504, 500, 429])
def test_a_transient_failure_then_an_answer_succeeds(transient):
    f = FakeFinance()
    iid = f.add()
    f.mode = [transient, None]
    assert f.client().lookup(iid)["status"] == "paid" and len(f.calls) == 2


def test_the_overall_deadline_stops_retries_early():
    f = FakeFinance()
    f.mode = "down"
    f.cost = 4.0                                        # each attempt uses 4 simulated seconds
    c = f.client(timeout_s=1, backoff_s=(5.0, 5.0))     # overall deadline 3 * 1 + 5 + 5 = 13 simulated seconds
    with pytest.raises(fc.FinanceLookupError) as e:
        c.lookup(f.add())
    assert e.value.code == "FINANCE_UNAVAILABLE" and len(f.calls) == 2 and f.sleeps == [5.0]


@pytest.mark.parametrize("mode,code", [("garbage", "FINANCE_RESPONSE_INVALID"), ("redirect", "FINANCE_RESPONSE_INVALID"),
                                       ("huge", "FINANCE_RESPONSE_INVALID"), ("gzip", "FINANCE_RESPONSE_INVALID"),
                                       ("other", "FINANCE_RESPONSE_INVALID"), ("route404", "FINANCE_RESPONSE_INVALID"),
                                       (418, "FINANCE_RESPONSE_INVALID")])
def test_malformed_answers_are_unverifiable_and_never_retried(mode, code):
    f = FakeFinance()
    iid = f.add()
    f.mode = mode
    with pytest.raises(fc.FinanceLookupError) as e:
        f.client().lookup(iid)
    assert e.value.code == code and len(f.calls) == 1


def test_wrong_tokens_and_unknown_invoices():
    f = FakeFinance()
    iid = f.add()
    for c, code in ((fc.FinanceClient(BASE, derived("wrong"), FIN_CALLER, transport=httpx.MockTransport(f.handler)),
                     "FINANCE_AUTH_REFUSED"),
                    (fc.FinanceClient(BASE, FIN_SERVICE, derived("wrong"), transport=httpx.MockTransport(f.handler)),
                     "FINANCE_AUTH_REFUSED")):
        with pytest.raises(fc.FinanceLookupError) as e:
            c.lookup(iid)
        assert e.value.code == code
    with pytest.raises(fc.FinanceLookupError) as e:
        f.client().lookup(invoice_id(77))
    assert e.value.code == "INVOICE_NOT_FOUND"
    n = len(f.calls)
    with pytest.raises(fc.FinanceLookupError) as e:
        f.client().lookup("fin-inv-../../admin")
    assert e.value.code == "FINANCE_RESPONSE_INVALID" and len(f.calls) == n      # never sent


@pytest.mark.parametrize("change", [{"status": "unknown"}, {"total": 2000.0}, {"total": "-1.00"}, {"client_id": "a b"},
                                    {"entity": "acme"}, {"currency": "usd"}, {"refunded": 5}, {"paid_at": 3},
                                    {"kind": ""}])
def test_parser_refuses_any_malformed_field(change):
    iid = invoice_id(5)
    with pytest.raises(fc.FinanceLookupError) as e:
        fc.parse_invoice(finance_invoice(iid, **change), iid)
    assert e.value.code == "FINANCE_RESPONSE_INVALID"
    with pytest.raises(fc.FinanceLookupError):
        fc.parse_invoice([finance_invoice(iid)], iid)


@pytest.mark.parametrize("kw,verdict", [({"entity": "zbc"}, "INVOICE_ENTITY_MISMATCH"),
                                        ({"client": "someone-else"}, "INVOICE_TENANT_MISMATCH"),
                                        ({"currency": "EUR"}, "INVOICE_CURRENCY_UNSUPPORTED"),
                                        ({"status": "draft"}, "INVOICE_NOT_PAID"),
                                        ({"status": "issued"}, "INVOICE_NOT_PAID"),
                                        ({"status": "void"}, "INVOICE_NOT_PAID"),
                                        ({"status": "returned"}, "INVOICE_NOT_PAID"),
                                        ({"refunded": "100.00"}, "INVOICE_REFUNDED"),
                                        ({"charged_back": "2000.00"}, "INVOICE_REFUNDED"),
                                        ({"refunded": "0.00"}, "PAID")])
def test_verdicts(kw, verdict):
    iid = invoice_id(6)
    assert fc.assess(fc.parse_invoice(finance_invoice(iid, **kw), iid), CLIENT) == verdict


def test_ownership_is_judged_before_payment():
    iid = invoice_id(7)
    facts = fc.parse_invoice(finance_invoice(iid, status="draft", client="someone-else"), iid)
    assert fc.assess(facts, CLIENT) == "INVOICE_TENANT_MISMATCH"


def test_not_connected_finance_is_unverifiable():
    with pytest.raises(fc.FinanceLookupError) as e:
        fc.NotConnectedFinance().lookup(invoice_id())
    assert e.value.code == "FINANCE_NOT_CONFIGURED"


# ================================================================================================ settings


def test_settings_default_to_finance_and_refuse_half_configured_finance():
    s = config_mod.load(base_env(SEO_INVOICE_VERIFICATION=None))
    assert s.invoice_verification == "finance" and s.finance_url is None
    assert config_mod.load(base_env()).invoice_verification == "trust"
    good = {"SEO_INVOICE_VERIFICATION": None, "SEO_FINANCE_URL": BASE, "SEO_FINANCE_TOKEN": FIN_SERVICE,
            "SEO_FINANCE_CALLER_TOKEN": FIN_CALLER}
    s = config_mod.load(base_env(**good))
    assert (s.finance_url, s.finance_token, s.finance_caller_token, s.finance_timeout_s) == \
        (BASE, FIN_SERVICE, FIN_CALLER, 5)
    assert "finance_token" not in repr(s) or FIN_SERVICE not in repr(s)
    p = Ports.default(s)
    assert isinstance(p.finance, fc.FinanceClient) and p.status()["finance"] == "connected"
    assert "finance" not in p.not_connected()        # a business gate, never an audit report's not_connected
    for bad in ({"SEO_INVOICE_VERIFICATION": "maybe"}, {**good, "SEO_FINANCE_TOKEN": None},
                {**good, "SEO_FINANCE_URL": "http://user:pw@finance.internal"},
                {**good, "SEO_FINANCE_URL": "ftp://finance.internal"},
                {**good, "SEO_FINANCE_URL": BASE + "/?x=1"},
                {**good, "SEO_FINANCE_TOKEN": "short"},
                {**good, "SEO_FINANCE_CALLER_TOKEN": FIN_SERVICE},
                {**good, "SEO_FINANCE_TOKEN": derived("service token")},
                {**good, "SEO_FINANCE_TIMEOUT_SECONDS": "0"}):
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(**bad))


# ================================================================================================ the paid-run gate


def harness(tmp_path, srv, f: FakeFinance | None, data_dir=None, ledger=None, **env):
    install_site(srv)
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    if f is not None:
        ports.finance = f.client()
    env.setdefault("SEO_INVOICE_VERIFICATION", None)
    return Harness(tmp_path, data_dir=data_dir, ports=ports, ledger=ledger, **env)


def client_tenant(h, tid="acme", bind=CLIENT):
    h.tenant(tid, domains=("site.test",))
    if bind:
        h.ok(h.post(f"/tenants/{tid}/finance-client", {"request_id": rid(), "finance_client_id": bind}, andre=True))


def paid_audit(h, tid="acme", andre=True, **body):
    b = {"request_id": rid(), "domain": "site.test", "scheme": "http", "paths": ["/"], **body}
    return h.post(f"/tenants/{tid}/audits", b, caller="dashboard", andre=andre)


def test_binding_a_finance_client_is_andre_s_and_only_for_client_tenants(tmp_path, srv):
    h = harness(tmp_path, srv, FakeFinance())
    h.tenant("acme", domains=("site.test",))
    body = {"request_id": rid(), "finance_client_id": CLIENT}
    h.refused(h.post("/tenants/acme/finance-client", body, caller="dashboard"), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(h.post("/tenants/acme/finance-client", body, caller="seo_agent"), 403, "CALLER_NOT_ALLOWED")
    h.refused(h.post("/tenants/zbm/finance-client", {**body, "request_id": rid()}, andre=True), 422,
              "TENANT_KIND_INVALID")
    assert h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "a b"},
                  andre=True).status_code == 422
    t = h.ok(h.post("/tenants/acme/finance-client", body, andre=True))
    assert t["finance_client_id"] == CLIENT
    assert h.ledger.of_type("tenant_finance_client_set")


def test_a_paid_invoice_lets_the_run_proceed_and_is_recorded(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    a = h.ok(paid_audit(h, invoice_id=iid), 201)
    assert a["status"] == "completed" and a["invoice_verification"]["verdict"] == "PAID"
    ev = h.ledger.of_type("invoice_verified")[0]["_payload"]
    assert ev["invoice_id"] == iid and ev["audit_id"] == a["audit_id"] and ev["verdict"] == "PAID"
    exported = json.dumps(h.ok(h.get("/audit/export?limit=1000", caller="compliance_38")))
    assert "2000.00" not in exported and "Pat" not in exported and "SEO audit" not in exported
    st = h.ok(h.get("/status"))["invoice_verification"]
    assert st["mode"] == "finance" and st["finance"] == "connected"


def test_nobody_but_andre_can_make_this_service_look_an_invoice_up(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    h.refused(paid_audit(h, andre=False, invoice_id=iid), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(paid_audit(h, andre=False, invoice_id=iid, invoice_override="FINANCE_OUTAGE"), 403,
              "ANDRE_APPROVAL_REQUIRED")
    h.refused(paid_audit(h, invoice_id=None), 409, "INVOICE_REQUIRED")
    h.refused(h.post("/tenants/acme/audits", {"request_id": rid(), "domain": "other.test", "invoice_id": iid},
                     caller="dashboard", andre=True), 403, "DOMAIN_NOT_AUTHORIZED")
    assert f.calls == []


def test_unbound_tenant_is_refused_before_finance_is_asked(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h, bind=None)
    h.refused(paid_audit(h, invoice_id=f.add()), 409, "TENANT_FINANCE_CLIENT_UNBOUND")
    assert f.calls == [] and h.ledger.of_type("audit_requested") == []


@pytest.mark.parametrize("mode,code", [("down", "FINANCE_UNAVAILABLE"), ("garbage", "FINANCE_RESPONSE_INVALID"),
                                       ("other", "FINANCE_RESPONSE_INVALID"), (503, "FINANCE_UNAVAILABLE")])
def test_unverifiable_refuses_with_nothing_recorded_and_andre_may_override(tmp_path, srv, mode, code):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    f.mode = mode
    r = h.refused(paid_audit(h, invoice_id=iid), 503, code)
    assert r["override_allowed"] is True
    assert h.ledger.of_type("audit_requested") == [] and srv.seen == []
    a = h.ok(paid_audit(h, invoice_id=iid, invoice_override="FINANCE_OUTAGE"), 201)
    v = a["invoice_verification"]
    assert v["verdict"] == "OVERRIDDEN" and v["cause"] == code and v["override"] == "FINANCE_OUTAGE"
    ev = h.ledger.of_type("invoice_verification_overridden_by_andre")[0]
    assert ev["actor"] == "andre" and ev["_payload"]["cause"] == code
    assert h.ledger.of_type("invoice_verified") == []


def test_finance_not_configured_is_the_safe_default(tmp_path, srv):
    h = harness(tmp_path, srv, None)              # SEO_INVOICE_VERIFICATION unset, no Finance client
    h.tenant("acme", domains=("site.test",))
    r = h.refused(paid_audit(h, invoice_id=invoice_id(3)), 503, "FINANCE_NOT_CONFIGURED")
    assert r["override_allowed"] is True
    st = h.ok(h.get("/status"))["invoice_verification"]
    assert st["finance"] == "NOT_CONNECTED" and "refused" in st["effect"]
    a = h.ok(paid_audit(h, invoice_id=invoice_id(3), invoice_override="ANDRE_CONFIRMED_PAYMENT"), 201)
    assert a["invoice_verification"]["cause"] == "FINANCE_NOT_CONFIGURED"


@pytest.mark.parametrize("kw,code", [({"client": "someone-else"}, "INVOICE_TENANT_MISMATCH"),
                                     ({"entity": "zbc"}, "INVOICE_ENTITY_MISMATCH"),
                                     ({"status": "issued"}, "INVOICE_NOT_PAID"),
                                     ({"status": "draft"}, "INVOICE_NOT_PAID"),
                                     ({"refunded": "2000.00"}, "INVOICE_REFUNDED"),
                                     ({"charged_back": "2000.00"}, "INVOICE_REFUNDED")])
def test_a_definitive_answer_from_finance_cannot_be_overridden(tmp_path, srv, kw, code):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add(**kw)
    for override in (None, "ANDRE_CONFIRMED_PAYMENT"):
        r = h.refused(paid_audit(h, invoice_id=iid, invoice_override=override), 409, code)
        assert r["override_allowed"] is False
    h.refused(paid_audit(h, invoice_id=invoice_id(55)), 409, "INVOICE_NOT_FOUND")
    assert h.ledger.of_type("audit_requested") == [] and srv.seen == []


def test_an_invoice_pays_for_one_run_and_a_replay_is_refused_whatever_the_override(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    first = {"request_id": rid(), "domain": "site.test", "scheme": "http", "paths": ["/"], "invoice_id": iid}
    a = h.ok(h.post("/tenants/acme/audits", first, caller="dashboard", andre=True), 201)
    again = h.ok(h.post("/tenants/acme/audits", first, caller="dashboard", andre=True), 201)
    assert again["audit_id"] == a["audit_id"]                                  # the same request: idempotent
    n = len(f.calls)
    h.refused(paid_audit(h, invoice_id=iid), 409, "INVOICE_ALREADY_USED")
    f.mode = "down"
    h.refused(paid_audit(h, invoice_id=iid, invoice_override="FINANCE_OUTAGE"), 409, "INVOICE_ALREADY_USED")
    assert len(f.calls) == n                                                    # refused before Finance is asked
    h.tenant("globex", domains=("other.test",))
    h.ok(h.post("/tenants/globex/finance-client", {"request_id": rid(), "finance_client_id": CLIENT}, andre=True))
    h.refused(h.post("/tenants/globex/audits", {"request_id": rid(), "domain": "other.test", "invoice_id": iid},
                     caller="dashboard", andre=True), 409, "INVOICE_ALREADY_USED")


def test_a_concurrent_replay_during_the_finance_call_is_caught_after_it(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    inner = {}
    f.on_call = lambda req: inner.setdefault("r", paid_audit(h, invoice_id=iid))
    h.refused(paid_audit(h, invoice_id=iid), 409, "INVOICE_ALREADY_USED")
    assert inner["r"].status_code == 201 and len(h.ledger.of_type("audit_requested")) == 1


def test_a_kill_switch_engaged_during_the_finance_call_stops_the_run(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    f.on_call = lambda req: h.svc.set_switch("compliance_38", {"request_id": rid(), "switch": "capability:audit",
                                                               "engaged": True}, andre=False)
    h.refused(paid_audit(h, invoice_id=iid), 403, "KILLED_CAPABILITY")
    assert h.ledger.of_type("audit_requested") == [] and srv.seen == []


def test_an_interrupted_run_gives_its_invoice_back(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()

    def home(handler):
        h.svc.set_switch("dashboard", {"request_id": rid(), "switch": "tenant:acme", "engaged": True}, andre=False)
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = home
    a = h.ok(paid_audit(h, invoice_id=iid), 201)
    assert a["status"] == "interrupted"
    h.ok(h.switch("tenant:acme", engaged=False, andre=True))
    install_site(srv)
    b = h.ok(paid_audit(h, invoice_id=iid), 201)
    assert b["status"] == "completed" and b["invoice_verification"]["verdict"] == "PAID"


def test_override_where_there_is_nothing_to_override_is_refused(tmp_path, srv):
    h = harness(tmp_path, srv, FakeFinance())
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    h.refused(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                             "invoice_override": "FINANCE_OUTAGE"}, caller="dashboard", andre=True),
              422, "INVOICE_OVERRIDE_NOT_ALLOWED")
    t = harness(tmp_path / "t", srv, FakeFinance(), SEO_INVOICE_VERIFICATION="trust")
    t.tenant("acme", domains=("site.test",))
    t.refused(paid_audit(t, invoice_id=invoice_id(), invoice_override="FINANCE_OUTAGE"), 422,
              "INVOICE_OVERRIDE_NOT_ALLOWED")
    a = t.ok(paid_audit(t, invoice_id=invoice_id()), 201)
    assert a["invoice_verification"] == {"mode": "trust", "verdict": "UNCHECKED"}
    assert t.ok(t.get("/status"))["invoice_verification"]["mode"] == "trust"


def test_bodies_with_personal_data_or_unknown_override_reasons_are_refused(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    assert paid_audit(h, invoice_id=f.add(), invoice_override="BECAUSE").status_code == 422
    assert paid_audit(h, invoice_id=f.add(), card_number="4111111111111111").status_code == 422
    assert f.calls == []


# ================================================================================================ schedules


def schedule(h, tid="acme", andre=True, **body):
    b = {"request_id": rid(), "domain": "site.test", "scheme": "http", "paths": ["/"], "every_days": 7, **body}
    return h.post(f"/tenants/{tid}/schedules", b, caller="dashboard", andre=andre)


def tick(h):
    return h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))


def test_a_schedule_is_verified_at_creation_and_again_before_every_slot(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    s = h.ok(schedule(h, invoice_id=iid), 201)
    assert s["invoice_verification"]["verdict"] == "PAID" and h.ledger.of_type("invoice_verified")
    h.refused(paid_audit(h, invoice_id=iid), 409, "INVOICE_ALREADY_USED")       # the schedule owns it
    h.refused(schedule(h, invoice_id=iid), 409, "INVOICE_ALREADY_USED")
    t = tick(h)
    assert t["ran"] == 1
    slot0 = h.ok(h.get(f"/tenants/acme/schedules/{s['schedule_id']}"))["slots"]["0"]
    a0 = h.ok(h.get(f"/tenants/acme/audits/{slot0['audit_id']}"))
    assert a0["invoice_verification"]["verdict"] == "PAID"
    # Finance down at the next slot: the slot waits (not consumed), and runs once Finance answers again
    h.clock.advance(days=7)
    f.mode = "down"
    t = tick(h)
    assert t["ran"] == 0 and t["invoice_unverifiable"] == 1
    assert "1" not in h.ok(h.get(f"/tenants/acme/schedules/{s['schedule_id']}"))["slots"]
    f.mode = None
    assert tick(h)["ran"] == 1
    # refunded since: the next slot is skipped with Finance's reason, recorded
    h.clock.advance(days=7)
    f.invoices[iid]["refunded"] = "2000.00"
    t = tick(h)
    assert t["ran"] == 0 and t["refused"] == 1
    slot2 = h.ok(h.get(f"/tenants/acme/schedules/{s['schedule_id']}"))["slots"]["2"]
    assert slot2["status"] == "skipped" and slot2["reason"] == "INVOICE_REFUNDED"


def test_schedule_creation_refusals(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    h.refused(schedule(h, invoice_id=f.add(status="issued")), 409, "INVOICE_NOT_PAID")
    f.mode = "down"
    iid = f.add(2)
    h.refused(schedule(h, invoice_id=iid), 503, "FINANCE_UNAVAILABLE")
    s = h.ok(schedule(h, invoice_id=iid, invoice_override="FINANCE_OUTAGE"), 201)
    assert s["invoice_verification"]["verdict"] == "OVERRIDDEN"
    assert h.ledger.of_type("invoice_verification_overridden_by_andre")[0]["_payload"]["schedule_id"] == \
        s["schedule_id"]


# ================================================================================================ restart


def test_consumption_and_binding_survive_a_restart(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f, data_dir=str(tmp_path / "data"), ledger=FakeLedger())
    client_tenant(h)
    iid = f.add()
    h.ok(paid_audit(h, invoice_id=iid), 201)
    h2 = h.restart(SEO_INVOICE_VERIFICATION=None)
    assert h2.ok(h2.get("/tenants/acme"))["finance_client_id"] == CLIENT
    h2.refused(paid_audit(h2, invoice_id=iid), 409, "INVOICE_ALREADY_USED")
    assert h2.ok(paid_audit(h2, invoice_id=f.add(2)), 201)["invoice_verification"]["verdict"] == "PAID"
