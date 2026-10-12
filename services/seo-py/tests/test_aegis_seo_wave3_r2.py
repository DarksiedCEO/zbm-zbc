"""AEGIS SEO Wave 3, round 2 Lows.

1. Host strictness in the fetcher: after IDNA a host is an IP literal or plain LDH labels, or it is REFUSED_URL
   before any resolver call.
2. The Finance client resolves its host INSIDE the lookup's deadline, on a bounded number of resolver threads, and
   connects to the resolved address (the socket layer makes no second lookup; Host, SNI and certificate checks keep
   the URL's name).

No wall-clock assertion: outcomes only. Deadlines are the injected clock's; a blocked resolver is released by the
test itself, and "no slot leaked" is read from the gate after it is idle."""

from __future__ import annotations

import http.server
import ipaddress
import json
import socket
import threading

import httpcore
import httpx
import pytest

import finance_client as fc
from primitives import fetch as fetch_mod

INVOICE = "fin-inv-" + "0" * 26


class _Recorder:
    def __init__(self, answer=("93.184.215.14",)):
        self.calls, self.answer = [], list(answer)

    def __call__(self, host, port):
        self.calls.append(host)
        return list(self.answer)


# ============================================================================== 1. fetch: host strictness

NOT_A_HOST_NAME = "host is not a valid domain name"


@pytest.mark.parametrize("url", [
    # whitespace and control characters: glibc's inet_aton reads "0177.0.0.1\x0b" as 127.0.0.1
    "http://127.0.0.1\x0bevil/", "http://0177.0.0.1\x0b/", "http://127.0.0.1\x0cevil/", "http://site\x1ftest/",
    "http://site\x7ftest/", "http://site.test\x0b/",
    # Unicode spaces IDNA maps to an ASCII space
    "http://127.0.0.1　/", "http://127.0.0.1 /", "http://site test/", "http://site test/",
    "http://si te.test/", "http://　site.test/",
    # not LDH
    "http://a_b.test/", "http://_dmarc.site.test/", "http://-a.test/", "http://a-.test/", "http://site.test%0a/",
    "http://site*.test/", "http://site!.test/", "http://xn--a.te_st/",
    # lengths: 255 characters in all, every label within 63
    "http://" + ".".join(["a" * 63] * 4) + "/",
])
def test_a_host_that_is_not_ldh_after_idna_is_refused_before_any_resolver_call(url):
    rec = _Recorder()
    r = fetch_mod.Fetcher(resolver=rec, policy=lambda h, ip, p: True).fetch(url, honor_robots=False)
    assert (r.state, r.detail) == ("REFUSED_URL", NOT_A_HOST_NAME), (url, r.state, r.detail)
    assert rec.calls == []


@pytest.mark.parametrize("url", ["http://a..test/", "http://" + "a" * 64 + ".test/", "http://[fe80::1%25eth0]/"])
def test_empty_or_long_labels_and_zone_ids_are_refused_before_any_resolver_call(url):
    rec = _Recorder()
    r = fetch_mod.Fetcher(resolver=rec, policy=lambda h, ip, p: True).fetch(url, honor_robots=False)
    assert r.state == "REFUSED_URL" and rec.calls == [], (url, r.state, r.detail)


@pytest.mark.parametrize("url", ["http://0177.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f000001/",
                                 "http://０１７７.0.0.1/"])
def test_the_legacy_numeric_refusal_still_holds(url):
    rec = _Recorder()
    r = fetch_mod.Fetcher(resolver=rec, policy=lambda h, ip, p: True).fetch(url, honor_robots=False)
    assert (r.state, r.detail) == ("REFUSED_URL", "a numeric host must be a canonical IP address"), url
    assert rec.calls == []


@pytest.mark.parametrize("url,host", [
    ("http://bücher.example/", "xn--bcher-kva.example"),
    ("http://مثال.test/", "xn--mgbh0fb.test"),
    ("http://SITE.Test./", "site.test"),
    ("http://web2.site-3.example/", "web2.site-3.example"),
    ("http://123abc.example/", "123abc.example"),
    ("http://xn--bcher-kva.example/", "xn--bcher-kva.example"),
    ("http://" + ".".join(["a" * 63] * 3) + "." + "b" * 61 + "/", ".".join(["a" * 63] * 3) + "." + "b" * 61),
    # IDNA nameprep maps a zero-width space to nothing: the same name, never a different one
    ("http://exa​mple.test/", "example.test"),
    # full-width digits fold to a canonical address, which then meets the address policy like any answer
    ("http://９３.184.215.14/", "93.184.215.14"),
    ("http://93.184.215.14/", "93.184.215.14"),
])
def test_real_names_and_literals_still_reach_the_resolver(url, host):
    rec = _Recorder()
    r = fetch_mod.Fetcher(resolver=rec, policy=lambda h, ip, p: False).fetch(url, honor_robots=False)
    assert rec.calls == [host]
    assert r.state == "REFUSED_ADDRESS"            # got as far as the address policy (refusing all, here)


def test_an_ipv6_literal_is_normalised_and_bracketed_in_the_host_header():
    rec, seen = _Recorder(("2606:4700::6810:84e5",)), {}

    def handler(request):
        seen["host"], seen["url"] = request.headers["host"], str(request.url)
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"), headers={"content-type": "text/plain"})

    f = fetch_mod.Fetcher(resolver=rec, transport=httpx.MockTransport(handler))
    r = f.fetch("http://[2606:4700:0::6810:84E5]/x", honor_robots=False)
    assert r.state == "OK", r.detail
    assert rec.calls == ["2606:4700::6810:84e5"]
    assert seen == {"host": "[2606:4700::6810:84e5]", "url": "http://[2606:4700::6810:84e5]/x"}


def test_an_ipv4_mapped_literal_is_one_address_on_every_python():
    rec = _Recorder()
    fetch_mod.Fetcher(resolver=rec, policy=lambda h, ip, p: False).fetch("http://[::ffff:5db8:d70e]/",
                                                                           honor_robots=False)
    assert [ipaddress.ip_address(c) for c in rec.calls] == [ipaddress.ip_address("::ffff:93.184.215.14")]


# ============================================================================== 2. Finance client DNS

class _Blocked:
    """A resolver that blocks until the test releases it, counting calls and how many run at once."""

    def __init__(self, answer=("127.0.0.1",)):
        self.go, self.answer = threading.Event(), list(answer)
        self.calls = self.running = self.peak = 0
        self._cond = threading.Condition(threading.Lock())

    def __call__(self, host, port):
        with self._cond:
            self.calls += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
            self._cond.notify_all()
        try:
            self.go.wait()
            return list(self.answer)
        finally:
            with self._cond:
                self.running -= 1

    def wait_running(self, n, timeout=30):
        with self._cond:
            return self._cond.wait_for(lambda: self.running >= n, timeout)


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _idle(gate):
    assert gate.wait_idle(30), "a resolver slot was never given back"
    assert gate.busy == 0


def test_a_slow_resolver_is_a_connect_timeout_bounded_by_the_deadline_and_its_slot_comes_back():
    gate, slow = fc._DnsGate(), _Blocked()
    try:
        b = fc._DeadlineBackend(_Clock(0.0), end=0.05, resolver=fc._Resolver(slow, gate))
        with pytest.raises(httpcore.ConnectTimeout):
            b.connect_tcp("finance.internal", 8410, timeout=5.0)
        assert slow.calls == 1 and gate.busy == 1          # the hung lookup still holds its one slot
    finally:
        slow.go.set()
    _idle(gate)


def test_no_lookup_once_the_deadline_has_passed_and_none_for_an_ip_literal():
    gate, rec = fc._DnsGate(), _Recorder()
    b = fc._DeadlineBackend(_Clock(1.0), end=1.0, resolver=fc._Resolver(rec, gate))
    with pytest.raises(httpcore.ConnectTimeout):
        b.connect_tcp("finance.internal", 8410, timeout=5.0)
    r = fc._Resolver(rec, gate)
    assert r("10.1.2.3", 8410, 1.0) == ["10.1.2.3"]
    assert r("::1", 8410, 1.0) == ["::1"] and r("[fd00::7]", 8410, 1.0) == ["fd00::7"]
    assert r("10.1.2.3", 8410, 0.0) == ["10.1.2.3"]          # a literal needs no time at all
    assert rec.calls == [] and gate.busy == 0


def test_a_busy_gate_refuses_at_once_and_never_runs_more_resolver_threads_than_its_slots():
    gate, slow = fc._DnsGate(workers=2), _Blocked()
    r = fc._Resolver(slow, gate)
    outcomes, lock = [], threading.Lock()

    def one():
        try:
            out = r("finance.internal", 8410, 30.0)
        except Exception as exc:  # noqa: BLE001
            out = type(exc).__name__
        with lock:
            outcomes.append(out)

    holders = [threading.Thread(target=one) for _ in range(2)]
    for t in holders:
        t.start()
    try:
        assert slow.wait_running(2)
        # with both slots held, a lookup is refused (ConnectError, not a wait ending in ConnectTimeout) without
        # starting a resolver; however many ask
        for _ in range(8):
            with pytest.raises(httpcore.ConnectError) as e:
                r("finance.internal", 8410, 30.0)
            assert not isinstance(e.value, httpcore.ConnectTimeout)
        assert slow.calls == 2 and slow.peak == 2 and gate.busy == 2
    finally:
        slow.go.set()
        for t in holders:
            t.join(30)
    assert outcomes == [["127.0.0.1"], ["127.0.0.1"]]
    _idle(gate)
    assert r("finance.internal", 8410, 30.0) == ["127.0.0.1"]   # the slots came back: lookups run again
    _idle(gate)


class _Boom(BaseException):
    pass


def _raise(exc):
    def resolve(host, port):
        raise exc
    return resolve


@pytest.mark.parametrize("resolve", [
    pytest.param(_raise(OSError("NXDOMAIN")), id="oserror"),
    pytest.param(_raise(socket.gaierror(socket.EAI_NONAME, "Name or service not known")), id="gaierror"),
    pytest.param(_raise(_Boom()), id="baseexception"),
    pytest.param(lambda h, p: [], id="empty"),
    pytest.param(lambda h, p: None, id="none"),
    pytest.param(lambda h, p: ["finance.internal"], id="a-name-not-an-address"),
    pytest.param(lambda h, p: [b"127.0.0.1"], id="bytes"),
    pytest.param(lambda h, p: 7, id="not-a-list"),
])
def test_a_failed_or_malformed_answer_is_a_connect_error_and_never_leaks_a_slot(resolve):
    gate = fc._DnsGate()
    with pytest.raises(httpcore.ConnectError):
        fc._Resolver(resolve, gate)("finance.internal", 8410, 30.0)
    assert gate.busy == 0                  # given back before the waiting lookup is woken: no wait needed


def test_a_resolver_thread_that_cannot_start_gives_its_slot_back():
    gate = fc._DnsGate()

    class NoThread:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    r = fc._Resolver(_Recorder(), gate)
    r._thread = NoThread
    with pytest.raises(httpcore.ConnectError):
        r("finance.internal", 8410, 30.0)
    assert gate.busy == 0


def test_a_slot_is_given_back_exactly_once_and_a_forked_child_starts_with_every_slot_free():
    gate = fc._DnsGate(workers=1)
    s = gate.try_take()
    assert gate.try_take() is None and gate.busy == 1
    s.release()
    s.release()
    assert gate.busy == 0
    gate.try_take()
    gate._after_fork_in_child()
    assert gate.busy == 0 and gate.try_take() is not None
    with pytest.raises(ValueError):
        fc._DnsGate(workers=0)


def test_the_shared_gate_is_used_by_default():
    assert fc._Resolver()._gate is fc._DNS_GATE and fc._DNS_GATE.workers == 2


class _FakeStream:
    def __init__(self, response=b""):
        self.response, self.sent, self.sni, self.tls = response, b"", None, False

    def read(self, max_bytes, timeout=None):
        out, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return out

    def write(self, buffer, timeout=None):
        self.sent += buffer

    def close(self):
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls, self.sni = True, server_hostname
        return self

    def get_extra_info(self, info):
        return None


class _FakeInner:
    def __init__(self, fail=(), clock=None, advance=0.0, response=b""):
        self.hosts, self.fail, self.clock, self.advance = [], set(fail), clock, advance
        self.stream = _FakeStream(response)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        if self.clock is not None:
            self.clock.t += self.advance
        if host in self.fail:
            raise httpcore.ConnectError("refused")
        return self.stream


def test_the_connection_goes_to_the_resolved_addresses_in_order_never_to_the_name():
    gate = fc._DnsGate()
    rec = _Recorder(("10.0.0.7", "10.0.0.7", "10.0.0.8"))
    inner = _FakeInner(fail={"10.0.0.7"})
    b = fc._DeadlineBackend(_Clock(0.0), end=10.0, inner=inner, resolver=fc._Resolver(rec, gate))
    b.connect_tcp("finance.internal", 8410, timeout=5.0)
    assert rec.calls == ["finance.internal"] and inner.hosts == ["10.0.0.7", "10.0.0.8"]   # repeats dropped
    inner.fail = {"10.0.0.7", "10.0.0.8"}
    with pytest.raises(httpcore.ConnectError):
        b.connect_tcp("finance.internal", 8410, timeout=5.0)
    _idle(gate)


def test_no_further_address_is_tried_once_the_deadline_has_passed():
    clock, gate = _Clock(0.0), fc._DnsGate()
    inner = _FakeInner(fail={"10.0.0.7"}, clock=clock, advance=10.0)
    b = fc._DeadlineBackend(clock, end=10.0, inner=inner,
                            resolver=fc._Resolver(_Recorder(("10.0.0.7", "10.0.0.8")), gate))
    with pytest.raises(httpcore.ConnectTimeout):
        b.connect_tcp("finance.internal", 8410, timeout=5.0)
    assert inner.hosts == ["10.0.0.7"]
    _idle(gate)


def test_https_keeps_the_url_name_for_sni_and_host_while_connecting_to_the_resolved_address():
    gate = fc._DnsGate()
    inner = _FakeInner(response=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    backend = fc._DeadlineBackend(_Clock(0.0), end=10.0, inner=inner,
                                  resolver=fc._Resolver(_Recorder(("10.0.0.7",)), gate))
    with httpcore.ConnectionPool(network_backend=backend) as pool:
        r = pool.request("GET", "https://finance.internal:8410/fin/v1/invoices/x/status")
    assert r.status == 200 and r.content == b"ok"
    assert inner.hosts == ["10.0.0.7"]
    assert inner.stream.tls and inner.stream.sni == "finance.internal"
    assert b"\r\nHost: finance.internal:8410\r\n" in inner.stream.sent
    _idle(gate)


@pytest.mark.local_http
def test_the_socket_layer_makes_no_second_name_lookup(monkeypatch):
    """A real connect through httpcore's own SyncBackend: the socket layer is handed the resolved literal, so a
    getaddrinfo that refuses every name (only numeric hosts pass) does not stop it."""
    real = socket.getaddrinfo
    asked = []

    def numeric_only(host, *a, **kw):
        asked.append(host)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "a name reached the socket layer") from None
        return real(host, *a, **kw)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
        gate = fc._DnsGate()
        b = fc._DeadlineBackend(_Clock(0.0), end=10.0, resolver=fc._Resolver(lambda h, p: ["127.0.0.1"], gate))
        s = b.connect_tcp("finance.internal", port, timeout=5.0)
        s.close()
        assert asked and all(h == "127.0.0.1" for h in asked)
        _idle(gate)
    finally:
        srv.close()


def _client(url, **kw):
    kw.setdefault("timeout_s", 1)
    kw.setdefault("attempts", 1)
    return fc.FinanceClient(url, "s" * 40, "c" * 40, sleep=lambda s: None, **kw)


def test_the_client_reports_a_hung_resolver_as_finance_unavailable():
    gate, slow = fc._DnsGate(), _Blocked()
    try:
        c = _client("http://finance.internal:8410", timeout_s=0.05, monotonic=_Clock(0.0), resolve=slow,
                    dns_gate=gate)
        with pytest.raises(fc.FinanceLookupError) as e:
            c.lookup(INVOICE)
        assert e.value.code == "FINANCE_UNAVAILABLE" and slow.calls == 1
    finally:
        slow.go.set()
    _idle(gate)


def test_the_client_reports_a_name_that_does_not_resolve_and_a_busy_gate_as_finance_unavailable():
    gate = fc._DnsGate()
    with pytest.raises(fc.FinanceLookupError) as e:
        _client("http://finance.internal:8410", resolve=_raise(socket.gaierror(socket.EAI_NONAME, "NXDOMAIN")),
                dns_gate=gate).lookup(INVOICE)
    assert e.value.code == "FINANCE_UNAVAILABLE"
    _idle(gate)
    full = fc._DnsGate(workers=1)
    held = full.try_take()
    rec = _Recorder(("127.0.0.1",))
    with pytest.raises(fc.FinanceLookupError) as e:
        _client("http://finance.internal:8410", resolve=rec, dns_gate=full).lookup(INVOICE)
    assert e.value.code == "FINANCE_UNAVAILABLE" and rec.calls == []
    held.release()
    _idle(full)


@pytest.mark.local_http
def test_the_client_resolves_its_host_and_reads_the_invoice_from_the_resolved_address():
    seen = {}
    status = {"invoice_id": INVOICE, "entity": "zbm", "client_id": "acme-1", "kind": "audit", "status": "paid",
              "currency": "USD", "paid_at": "2026-10-12T00:00:00Z", "refunded": False, "charged_back": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"], seen["path"] = self.headers.get("Host"), self.path
            body = json.dumps(status).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05})
    t.start()
    try:
        port = srv.server_address[1]
        gate, rec = fc._DnsGate(), _Recorder(("127.0.0.1",))
        facts = _client(f"http://finance.internal:{port}", timeout_s=10, resolve=rec, dns_gate=gate).lookup(INVOICE)
        assert facts["status"] == "paid" and fc.assess(facts, "acme-1") == fc.PAID
        assert rec.calls == ["finance.internal"]
        assert seen == {"host": f"finance.internal:{port}", "path": f"/fin/v1/invoices/{INVOICE}/status"}
        _idle(gate)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(30)
