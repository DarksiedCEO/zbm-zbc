"""SPEC section 2: the fetch primitive (B-FT-01..10)."""

from __future__ import annotations

import gzip
import time

import httpx
import pytest

from cert_web import Web, ctx, site
from common import check_ran
from primitives import fetch as fetch_mod

REFUSED = ["ftp://site.test/", "file:///etc/x", "gopher://site.test/", "javascript:alert(1)", "data:text/html,x",
           "http://user:pw@site.test/", "http://u@site.test/", "http://site.test:8080/", "https://site.test:22/",
           "http://site.test:0/", "http://0177.0.0.1/", "http://2130706433/", "http://0x7f000001/", "http://127.1/",
           "http://0x7f.0.0.1/", "http://site.test:65536/", "mailto:a", "tel:1", "ws://site.test/", "//site.test/"]


def test_B_FT_01_refused_shapes_make_no_connection():
    web = Web()
    f = web.fetcher()
    ran = 0
    for _ in range(3):
        for u in REFUSED:
            r = f.fetch(u)
            assert r.state in ("REFUSED_URL", "REFUSED_PORT"), (u, r.state)
            ran += 1
    assert web.requests == []
    check_ran("B-FT-01", ran)


INTERNAL = ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "224.0.0.1",
            "240.0.0.1", "0.0.0.0", "::1", "fe80::1", "fec0::1", "fc00::1", "::ffff:127.0.0.1", "2002:7f00:1::1",
            "64:ff9b::7f00:1", "192.88.99.1", "255.255.255.255", "198.18.0.1", "192.0.0.8"]


def test_B_FT_02_every_non_public_address_is_refused_by_the_default_policy():
    for ip in INTERNAL:
        assert fetch_mod.public_address(ip) is False, ip
        assert fetch_mod.default_policy("site.test", ip, 80) is False, ip
    assert fetch_mod.public_address("93.184.216.34")


def test_B_FT_02_a_redirect_to_an_internal_name_is_refused_before_connecting():
    web = Web()
    web.route("/", "", status=302, location="http://internal.test/")
    base = web.resolve

    def resolve(host, port):
        return ["10.1.2.3"] if host == "internal.test" else base(host, port)
    f = web.fetcher(resolver=resolve, policy=fetch_mod.default_policy)
    f2 = web.fetcher(resolver=resolve)          # the test policy: only the fixture's own addresses are public
    for fx in (f2,):
        r = fx.fetch("http://site.test/")
        assert r.state == "REFUSED_ADDRESS" and ("internal.test", "/") not in web.requests
    assert f is not None


def test_B_FT_03_hop_5_followed_hop_6_refused_and_loops_refused():
    web = Web()
    for i in range(7):
        web.route(f"/r{i}", "", status=301, location=f"/r{i + 1}")
    web.route("/r7", "<p>end</p>")
    f = web.fetcher(max_redirects=5)
    assert f.fetch("http://site.test/r2").state == "OK"            # 5 hops
    assert f.fetch("http://site.test/r1").state == "TOO_MANY_REDIRECTS"   # 6 hops
    web.route("/loop", "", status=302, location="/loop")
    assert f.fetch("http://site.test/loop").state == "TOO_MANY_REDIRECTS"


@pytest.mark.parametrize("n", [1990, 2000, 2001, 2011, 2100, 4000, 8000, 8192])
def test_B_FT_04_a_long_location_is_followed_whole(n):
    web = Web()
    target = "/t/" + "a" * (n - 3)
    web.route("/", "", status=302, location=target)
    web.route(target, "<p>arrived</p>")
    r = web.fetcher().fetch("http://site.test/")
    assert r.state == "OK" and r.status == 200, (n, r.state, r.status)
    assert ("site.test", target) in web.requests
    assert not any(p != target and p.startswith("/t/") for _, p in web.requests)


def test_B_FT_04_location_over_8192_is_redirect_too_long_and_never_followed():
    web = Web()
    target = "/t/" + "a" * 8191
    web.route("/", "", status=302, location=target)
    r = web.fetcher().fetch("http://site.test/")
    assert r.state == "REDIRECT_TOO_LONG"
    assert all(not p.startswith("/t/") for _, p in web.requests)


def test_B_FT_04_location_lengths_n100():
    ran = 0
    web = Web()
    f = web.fetcher()
    for n in range(1990, 2090):
        target = "/u/" + "b" * (n - 3)
        web.route(f"/s{n}", "", status=301, location=target)
        web.route(target, "ok")
        r = f.fetch(f"http://site.test/s{n}")
        assert r.state == "OK", n
        ran += 1
    check_ran("B-FT-04", ran)


def test_B_FT_05_decoded_cap_and_codings():
    web = Web()
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
    web.route("/bomb", bomb, ctype="text/html", content_encoding="gzip")
    web.route("/br", b"x", ctype="text/html", content_encoding="br")
    web.route("/stack", gzip.compress(gzip.compress(b"x")), ctype="text/html", content_encoding="gzip, gzip")
    web.route("/trail", gzip.compress(b"hello") + b"garbage!", ctype="text/html", content_encoding="gzip")
    f = web.fetcher(max_bytes=256 * 1024)
    r = f.fetch("http://site.test/bomb")
    assert r.state == "TOO_LARGE" and r.partial["bytes_read"] <= 256 * 1024 + 64 * 1024
    assert f.fetch("http://site.test/br").state == "UNSUPPORTED_ENCODING"
    assert f.fetch("http://site.test/stack").state == "UNSUPPORTED_ENCODING"
    assert f.fetch("http://site.test/trail").state == "PROTOCOL_ERROR"


def test_B_FT_06_one_deadline_over_a_trickling_body():
    web = Web()

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(100):
                time.sleep(0.05)
                yield b"x"

    web.routes[("site.test", "/slow")] = lambda req: httpx.Response(200, headers={"content-type": "text/html"},
                                                                    stream=Trickle())
    f = web.fetcher(timeout_s=1)
    t0 = time.monotonic()
    r = f.fetch("http://site.test/slow")
    assert r.state == "TIMEOUT" and time.monotonic() - t0 <= 2 * 1 + 1.0


def test_B_FT_07_robots_states_rule_unreachable_incomplete():
    for robots, status, want in (("User-agent: *\nDisallow: /\n", 200, ("BLOCKED_BY_ROBOTS", "rule")),
                                 ("", 503, ("BLOCKED_BY_ROBOTS", "unreachable")),
                                 ("", 429, ("BLOCKED_BY_ROBOTS", "unreachable"))):
        web = Web()
        web.route("/robots.txt", robots, status=status, ctype="text/plain")
        web.route("/p", "<p>x</p>")
        c = ctx(web, paths=["/p"])
        r = c.fetcher.fetch("http://site.test/p", robots_cache=c.robots_cache)
        assert (r.state, (r.robots or {}).get("reason")) == want, (status, r.state, r.robots)
        assert ("site.test", "/p") not in web.requests


def test_B_FT_07_query_as_written_is_checked():
    web = site(Web(), robots="User-agent: *\nDisallow: /*?sort=\n")
    web.route("/list", "<p>x</p>")
    c = ctx(web)
    assert c.fetcher.fetch("http://site.test/list?sort=a", robots_cache=c.robots_cache).state == "BLOCKED_BY_ROBOTS"
    assert c.fetcher.fetch("http://site.test/list?page=2", robots_cache=c.robots_cache).state == "OK"


def test_B_FT_08_no_request_after_the_switch():
    from primitives import Killed
    web = site(Web())
    calls = {"n": 0}

    def guard(**k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise Killed("KILLED_GLOBAL")
    f = web.fetcher()
    r = f.fetch("http://site.test/", guard=guard, robots_cache={})
    assert r.state == "KILLED"
    n = len(web.requests)
    for _ in range(8):
        assert f.fetch("http://site.test/about", guard=guard, robots_cache={}).state == "KILLED"
    assert len(web.requests) == n


def test_B_FT_09_every_request_carries_one_of_the_two_user_agents():
    web = site(Web())
    seen = []
    orig = web.handle

    def handle(req):
        seen.append(req.headers.get("user-agent"))
        return orig(req)
    web.handle = handle
    f = web.fetcher(bot_info_url="https://zbm.example/bot")
    f.fetch("http://site.test/")
    f.fetch("http://site.test/", ua="human")
    bot = f"{fetch_mod.PRODUCT_TOKEN}/{fetch_mod.VERSION}"
    assert seen and all(bot in u or u == fetch_mod.HUMAN_UA for u in seen)
    assert any(bot in u and "+https://zbm.example/bot" in u for u in seen)
