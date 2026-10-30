"""AEGIS A2 H-1: the fetcher connected only to the FIRST resolved address (text-sorted, so an IPv6 address often came
first); a container with no outbound IPv6 then failed every dual-stack site, robots.txt read as unreachable and the
audit came back BLOCKED. Every resolved address is still vetted (any non-public one refuses the host); the vetted
addresses are tried IPv4 first, alternating families, at most ``MAX_ADDRESS_ATTEMPTS``; the next one only when no
connection to the previous one was made, and only inside the fetch's one overall deadline."""

from __future__ import annotations

import errno
import socket
from types import SimpleNamespace

import httpx
import pytest

from fixture_server import stall
from primitives import fetch as fetch_mod

pytestmark = pytest.mark.local_http
PAGE = "<html lang='en'><head><title>Home</title></head><body><h1>Hello</h1></body></html>"
V6 = "2606:4700::6810:84e5"          # public, so the production policy allows it
V4 = "93.184.216.34"


def _ok(body: bytes = b"<title>t</title>", status: int = 200):
    return httpx.Response(status, headers={"Content-Type": "text/html"}, stream=httpx.ByteStream(body))


def _no_ipv6_route(monkeypatch):
    """The container has no outbound IPv6: a TCP connect to any IPv6 address fails ENETUNREACH (nothing is sent).
    Wraps the conftest's loopback-only ``create_connection``; records every address a connect was attempted to."""
    inner, tried = socket.create_connection, []

    def create_connection(address, *a, **k):
        tried.append(address[0])
        if ":" in address[0]:
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        return inner(address, *a, **k)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return tried


# ------------------------------------------------------------------------------------- (1) v6 first, no v6 route

def test_ipv6_first_and_unreachable_uses_ipv4_real_socket(srv, monkeypatch):
    """The real socket backend: the resolver answers IPv6 first (as ``system_resolver``'s text sort does for e.g.
    "2606:..." vs "76.76.21.21"); IPv6 is unreachable; the page is fetched over IPv4, still by the checked IP."""
    assert sorted({"2606:4700::1", "76.76.21.21"})[0] == "2606:4700::1"     # the H-1 precondition
    srv.html("site.test", "/", PAGE)
    tried = _no_ipv6_route(monkeypatch)
    f = srv.fetcher(resolver=srv.resolver({"site.test": [V6, "127.0.0.1"]}))
    r = f.fetch(srv.url())
    assert r.state == "OK" and r.status == 200 and "Hello" in r.text()
    assert tried == ["127.0.0.1"]                                   # IPv4 first: the IPv6 route is never needed
    assert srv.seen[0]["host"] == "site.test"                     # the name in Host, not the IP


def test_ipv4_failing_then_ipv6_unreachable_is_connect_failed_and_both_were_tried(monkeypatch):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.host)
        raise httpx.ConnectError("[Errno 101] Network is unreachable")
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: sorted({V6, V4}), transport=httpx.MockTransport(handler))
    r = f.fetch("http://dual.test/")
    assert r.state == "CONNECT_FAILED" and seen == [V4, V6]


def test_ipv6_unreachable_then_ipv4_ok_and_the_audit_is_not_blocked(srv, monkeypatch):
    """The H-1 effect end to end: robots.txt is read (over IPv4), so the page is not BLOCKED_BY_ROBOTS."""
    srv.text("site.test", "/robots.txt", "User-agent: *\nDisallow:\n")
    srv.html("site.test", "/", PAGE)
    _no_ipv6_route(monkeypatch)
    f = srv.fetcher(resolver=srv.resolver({"site.test": [V6, "127.0.0.1"]}))
    cache: dict = {}
    r = f.fetch(srv.url(), robots_cache=cache)
    assert r.state == "OK" and r.status == 200
    assert next(iter(cache.values()))["status_class"] == "ok"


def test_ipv6_only_first_attempt_fails_then_next_address_is_used():
    """Families alternate IPv4 first; a connect failure on one address moves to the next vetted one, which gets the
    request by IP literal with the NAME in Host and SNI."""
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        if request.url.host == V4:
            raise httpx.ConnectError("[Errno 111] Connection refused")
        return _ok()
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: [V6, V4], transport=httpx.MockTransport(handler))
    r = f.fetch("https://Dual.Example/x")
    assert r.state == "OK"
    assert seen == [(V4, "dual.example", "dual.example"), (V6, "dual.example", "dual.example")]


# ---------------------------------------------------------------------------------------------- (2) all fail

def test_all_addresses_fail_is_the_same_state_and_detail_as_one_failing_address():
    seen = []

    def down(request: httpx.Request):
        seen.append(request.url.host)
        raise httpx.ConnectError("x")
    one = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: [V4], transport=httpx.MockTransport(down))
    r1 = one.fetch("http://down.test/")
    seen.clear()
    many = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: [V6, V4, "93.184.216.35"],
                             transport=httpx.MockTransport(down))
    r2 = many.fetch("http://down.test/")
    assert (r2.state, r2.detail, r2.status, r2.body) == (r1.state, r1.detail, r1.status, r1.body) \
        == ("CONNECT_FAILED", "connection failed", None, None)
    assert seen == [V4, V6, "93.184.216.35"]


def test_all_addresses_unreachable_robots_is_unreachable_as_before():
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: [V6, V4],
                          transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))))
    assert f.robots("http://down.test")["status_class"] == "unreachable"


# ---------------------------------------------------------------------------------- (3) no fallback on an answer

@pytest.mark.parametrize("status", [404, 500, 503])
def test_an_http_error_status_is_an_answer_and_is_not_retried_elsewhere(status):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.host)
        return _ok(b"err", status) if request.url.host == V4 else _ok()
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: [V6, V4], transport=httpx.MockTransport(handler))
    r = f.fetch("http://dual.test/")
    assert r.state == "OK" and r.status == status and seen == [V4]


def test_tls_failure_is_not_retried_on_another_address():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.host)
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: [V6, V4], transport=httpx.MockTransport(handler))
    assert f.fetch("https://dual.test/").state == "TLS_FAILED" and seen == [V4]


def test_a_failure_after_the_connection_is_up_is_never_retried(srv, monkeypatch):
    """Real socket backend: the first address accepts the connection and the request is sent, then the server stalls
    past the deadline. The second vetted address is never connected to (nothing is sent twice)."""
    srv.routes[("site.test", "/stall")] = stall(3)
    tried = _no_ipv6_route(monkeypatch)
    f = srv.fetcher(resolver=srv.resolver({"site.test": ["127.0.0.1", V4]}),
                    policy=lambda h, ip, p: ip in ("127.0.0.1", V4))
    r = f.fetch(srv.url(path="/stall"))
    assert r.state == "TIMEOUT" and tried == ["127.0.0.1"]


# ------------------------------------------------------------------------------------- (4) private in the answer

@pytest.mark.parametrize("answer", [[V6, "10.0.0.1", V4], [V4, "fd00::1"], ["127.0.0.1", V4], [V4, "169.254.169.254"]])
def test_any_non_public_address_in_the_answer_still_refuses_the_host(answer):
    seen = []
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: list(answer),
                          transport=httpx.MockTransport(lambda r: seen.append(r.url.host) or _ok()))
    r = f.fetch("http://mixed.test/")
    assert r.state == "REFUSED_ADDRESS" and seen == []


# ---------------------------------------------------------------------------------------------- (5) one deadline
# A fake monotonic clock (the fetcher's own ``time`` name): a blackholed connect advances it instead of sleeping, so
# the deadline arithmetic is checked exactly and nothing here depends on the machine's load.

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


def _fake_clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(fetch_mod, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=lambda s: None))
    return clock


def test_connect_budget_is_split_from_the_time_left_not_multiplied(monkeypatch):
    """Every address blackholes (each connect waits its whole connect timeout, then times out). Each attempt's connect
    timeout is a share of the time LEFT, so every vetted address gets a turn and the fetch still ends at its one
    overall deadline (2 x timeout_s), not 4 x timeout_s."""
    clock, budgets = _fake_clock(monkeypatch), []

    def blackhole(request: httpx.Request):
        c = request.extensions["timeout"]["connect"]
        budgets.append(c)
        clock.t += c
        raise httpx.ConnectTimeout("timed out")
    addrs = [V4, V6, "93.184.216.35", "2606:4700::6810:84e6"]
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: addrs, transport=httpx.MockTransport(blackhole))
    t0 = clock.t
    r = f.fetch("http://slow.test/")
    assert r.state == "TIMEOUT"
    assert budgets == pytest.approx([0.5, 0.5, 0.5, 0.5])
    assert clock.t - t0 == pytest.approx(2.0)                       # the one deadline: 2 x timeout_s


def test_one_or_two_addresses_keep_the_full_connect_timeout(monkeypatch):
    """With the default budget (deadline 2 x timeout_s) one or two addresses each keep timeout_s to connect: the
    common case is exactly as before."""
    clock, budgets = _fake_clock(monkeypatch), []

    def blackhole(request: httpx.Request):
        budgets.append(request.extensions["timeout"]["connect"])
        clock.t += budgets[-1]
        raise httpx.ConnectTimeout("timed out")
    f = fetch_mod.Fetcher(timeout_s=3, resolver=lambda h, p: [V6, V4], transport=httpx.MockTransport(blackhole))
    assert f.fetch("http://slow.test/").state == "TIMEOUT" and budgets == pytest.approx([3, 3])


def test_no_attempt_starts_after_the_deadline(monkeypatch):
    """A connect that ignores its share and eats more than the whole timeout: once the overall deadline has passed,
    no further address is tried."""
    clock, seen = _fake_clock(monkeypatch), []

    def slow(request: httpx.Request):
        seen.append(request.url.host)
        clock.t += 1.05
        raise httpx.ConnectTimeout("timed out")
    addrs = [V4, V6, "93.184.216.35", "2606:4700::6810:84e6"]
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: addrs, transport=httpx.MockTransport(slow))
    r = f.fetch("http://slow.test/")
    assert r.state == "TIMEOUT" and seen == [V4, V6]


# ---------------------------------------------------------------------------------------------- (6) attempt cap

def test_at_most_max_address_attempts_in_alternating_order():
    seen = []

    def down(request: httpx.Request):
        seen.append(request.url.host)
        raise httpx.ConnectError("x")
    v4 = ["93.184.216.34", "93.184.216.35", "93.184.216.36"]
    v6 = ["2606:4700::1", "2606:4700::2", "2606:4700::3"]
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: sorted(v4 + v6), transport=httpx.MockTransport(down))
    r = f.fetch("http://many.test/")
    assert fetch_mod.MAX_ADDRESS_ATTEMPTS == 4
    assert r.state == "CONNECT_FAILED"
    assert seen == ["93.184.216.34", "2606:4700::1", "93.184.216.35", "2606:4700::2"]
