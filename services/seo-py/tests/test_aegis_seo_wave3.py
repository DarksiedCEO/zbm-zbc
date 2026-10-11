"""AEGIS SEO Wave 3 (f9f750d) findings: M1 (the Finance lookup's deadline holds within one attempt), M3 (an invalid
canonical still counts for CANONICAL_MULTIPLE), L1 (a Finance binding changed during the call), L2 (RUN_FAILED
names the step and a safe location; a failed slot is counted ``failed``), L3 (a redirect to a host httpx cannot
encode is REFUSED_URL). M2 is finance-py's (tests/test_seo_route_allowlist.py there). No wall-clock assertion: the
deadline tests run on an injected monotonic clock."""

from __future__ import annotations

import httpcore
import httpx
import pytest

import finance_client as fc
from fixture_server import home_html, install_site
from helpers import rid
from primitives import fetch as fetch_mod
from primitives.parse import parse_html
from test_aegis_00cc66b import _raw_server  # noqa: F401  (the suite's raw socket server)
from test_audits import audit, harness, own
from test_invoice_verification import CLIENT, FakeFinance, client_tenant, finance_invoice, paid_audit, schedule, tick
from test_invoice_verification import harness as paid_harness

pytestmark = pytest.mark.local_http


# ---------------------------------------------------------------------------------------------- M1

class _Trickle(httpx.SyncByteStream):
    """A body arriving one byte per 0.4 simulated seconds, forever (the clock is the test's, not the wall's)."""

    def __init__(self, f: FakeFinance):
        self.f, self.n = f, 0

    def __iter__(self):
        while True:
            self.f.now += 0.4
            self.n += 1
            yield b"{"


def test_m1_a_trickled_body_stops_at_the_overall_deadline_within_one_attempt():
    f = FakeFinance()
    stream = _Trickle(f)
    c = fc.FinanceClient("http://finance.internal", "s" * 40, "c" * 40, timeout_s=1, attempts=1,
                         transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream)),
                         sleep=f.sleep, monotonic=f.mono)
    with pytest.raises(fc.FinanceLookupError) as e:
        c.lookup("fin-inv-" + "0" * 26)
    assert e.value.code == "FINANCE_UNAVAILABLE"
    assert stream.n <= 3                     # 1 s at 0.4 s a byte: stopped at the deadline, not after 27 s


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _InnerStream:
    def __init__(self, clock):
        self.clock, self.timeouts = clock, []

    def read(self, n, timeout=None):
        self.timeouts.append(timeout)
        self.clock.t += 0.4
        return b"x"

    def write(self, b, timeout=None):
        self.timeouts.append(timeout)

    def close(self):
        pass


class _InnerBackend:
    def __init__(self, clock):
        self.clock, self.stream, self.connect_timeouts = clock, _InnerStream(clock), []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connect_timeouts.append(timeout)
        return self.stream


def test_m1_every_socket_operation_is_bounded_by_the_time_left():
    clock = _Clock()
    inner = _InnerBackend(clock)
    b = fc._DeadlineBackend(clock, end=1.0, inner=inner)
    s = b.connect_tcp("127.0.0.1", 8410, timeout=5.0)
    assert inner.connect_timeouts == [1.0]                  # min(5 s asked, 1 s left)
    s.write(b"GET", timeout=5.0)
    s.read(1, timeout=5.0)                                  # the clock is at 0.4 after this read
    s.read(1, timeout=5.0)
    assert inner.stream.timeouts[0] == 1.0 and abs(inner.stream.timeouts[2] - 0.6) < 1e-9
    clock.t = 1.0
    with pytest.raises(httpcore.ReadTimeout):
        s.read(1, timeout=5.0)
    with pytest.raises(httpcore.ConnectTimeout):
        b.connect_tcp("127.0.0.1", 8410, timeout=5.0)


def test_m1_the_production_client_installs_the_deadline_backend(monkeypatch):
    seen = {}

    class Probe(httpx.HTTPTransport):
        def handle_request(self, request):
            seen["backend"] = type(self._pool._network_backend).__name__
            return httpx.Response(503)

    monkeypatch.setattr(fc.httpx, "HTTPTransport", Probe)
    c = fc.FinanceClient("http://finance.internal", "s" * 40, "c" * 40, attempts=1, sleep=lambda s: None)
    with pytest.raises(fc.FinanceLookupError):
        c.lookup("fin-inv-" + "0" * 26)
    assert seen["backend"] == "_DeadlineBackend"


# ---------------------------------------------------------------------------------------------- M3

@pytest.mark.parametrize("valid,invalid,multiple", [(1, 1, True), (0, 2, True), (2, 0, True), (1, 0, False),
                                                    (0, 1, False)])
def test_m3_invalid_canonicals_count_for_multiple_and_are_always_reported(tmp_path, srv, valid, invalid, multiple):
    links = "".join("<link rel='canonical' href='http://site.test/'>" for _ in range(valid)) + \
        "".join("<link rel='canonical' href='javascript:alert(1)'>" for _ in range(invalid))
    html = home_html(canonical=None, extra_head=links)
    x = parse_html(html, "http://site.test/")
    assert x["canonicals_valid"] == valid and x["canonicals_invalid"] == invalid
    install_site(srv, home=html)
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h, paths=["/"]), 201)
    codes = [f["code"] for e in a["report"]["agents"] if e["agent"] == "selene" for f in e["findings"]]
    assert ("CANONICAL_MULTIPLE" in codes) is multiple
    assert ("CANONICAL_INVALID" in codes) is bool(invalid)
    assert "CANONICAL_MISSING" not in codes


# ---------------------------------------------------------------------------------------------- L1

def test_l1_a_binding_changed_during_the_finance_call_refuses_the_run(tmp_path, srv):
    f = FakeFinance()
    h = paid_harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    f.on_call = lambda req: h.svc.set_finance_client("acme", {"request_id": rid(), "finance_client_id": "other-1"})
    h.refused(paid_audit(h, invoice_id=iid), 409, "TENANT_FINANCE_CLIENT_CHANGED")
    assert h.ledger.of_type("audit_requested") == []


def test_l1_at_a_tick_the_slot_waits_instead_of_being_consumed(tmp_path, srv):
    f = FakeFinance()
    h = paid_harness(tmp_path, srv, f)
    client_tenant(h)
    iid = f.add()
    s = h.ok(schedule(h, invoice_id=iid), 201)
    f.on_call = lambda req: h.svc.set_finance_client("acme", {"request_id": rid(), "finance_client_id": "other-1"})
    t = tick(h)
    assert t["ran"] == 0 and t["refused"] == 1
    assert "0" not in h.ok(h.get(f"/tenants/acme/schedules/{s['schedule_id']}"))["slots"]
    h.svc.set_finance_client("acme", {"request_id": rid(), "finance_client_id": CLIENT})
    f.invoices[iid] = finance_invoice(iid)
    assert tick(h)["ran"] == 1


# ---------------------------------------------------------------------------------------------- L2

def test_l2_run_failed_names_the_step_and_a_safe_location(tmp_path, srv, monkeypatch):
    from agents import delia
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)

    def boom(*a):
        raise ValueError("crawled text http://evil.test/ must never be logged")
    monkeypatch.setattr(delia, "_llms", boom)
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["interrupted_reason"] == "RUN_FAILED"
    assert a["failure"]["agent"] == "delia" and a["failure"]["error_type"] == "ValueError"
    assert a["failure"]["where"].startswith("agents.delia:run:")      # innermost frame in src/
    ev = h.ledger.of_type("audit_interrupted")[0]["_payload"]
    assert ev["agent"] == "delia" and ev["where"] == a["failure"]["where"]
    assert "evil.test" not in str(h.ledger.events) and "evil.test" not in str(a)


def test_l2_a_failed_scheduled_slot_is_counted_failed_not_ran(tmp_path, srv, monkeypatch):
    from agents import roman
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    h.ok(h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                           "paths": ["/"], "every_days": 7}), 201)
    monkeypatch.setattr(roman, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    t = h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))
    assert t["failed"] == 1 and t["ran"] == 0


# ---------------------------------------------------------------------------------------------- L3

@pytest.mark.parametrize("location", ["http://１２７.０.０.１/".encode(), b"http://xn--/",
                                      "http://[Fｅ80::１]/".encode()])
def test_l3_a_redirect_to_a_host_httpx_cannot_encode_is_refused_not_connect_failed(location):
    hits = []

    def handler(c):
        hits.append(1)
        c.sendall(b"HTTP/1.1 302 Found\r\nLocation: " + location + b"\r\nContent-Length: 0\r\nConnection: close"
                  b"\r\n\r\n")
    s, port, stop = _raw_server(handler)
    try:
        f = fetch_mod.Fetcher(timeout_s=2, ports=(port, 80), resolver=lambda h, p: ["127.0.0.1"] if h == "x.test"
                              else fetch_mod.system_resolver(h, p) if fetch_mod._is_ip_literal(h) else [],
                              policy=lambda h, ip, p: h == "x.test" or fetch_mod.default_policy(h, ip, p))
        r = f.fetch(f"http://x.test:{port}/", honor_robots=False)
    finally:
        stop.set()
        s.close()
    assert r.state in ("REFUSED_URL", "REFUSED_ADDRESS", "DNS_FAILED") and r.state != "CONNECT_FAILED"
    assert len(hits) == 1                                    # only the first hop reached a server
