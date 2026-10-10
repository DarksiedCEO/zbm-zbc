"""AEGIS review of 00cc66b: H1 (stacked content-coding bomb), M1 (hard fetch deadline), M2 (domains unique across
tenants), M3 (audit / write switches stop a running audit and refuse its completion record), L1 (fec0::/10,
192.88.99.0/24), L2 (ports 80/443 only), L3 (the browser-identity fetch honours robots.txt). The reviewer's probes
(test_zz_aegis_probe.py, p1.py, p2.py) are ported here asserting bounded STATES — no memory, RSS or wall-clock
assertions."""

from __future__ import annotations

import gzip
import socket
import threading
import time
import zlib

import pytest

from fixture_server import home_html, install_site
from primitives import Killed
from primitives import fetch as fetch_mod
from test_audits import audit, harness, own
from helpers import rid

pytestmark = pytest.mark.local_http


def _gz_stream(chunks):
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    for ch in chunks:
        yield c.compress(ch)
    yield c.flush()


def _zeros(n: int):
    blk = b"\0" * (4 << 20)
    for _ in range(n // len(blk)):
        yield blk


def _raw_server(handler):
    """A raw-socket server on 127.0.0.1 (an OS-assigned port): ``handler(conn)`` writes whatever bytes it wants."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=_safe, args=(handler, c), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1], stop


def _safe(handler, c):
    try:
        c.recv(4096)
        handler(c)
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


def _fetcher(port, **kw):
    return fetch_mod.Fetcher(resolver=lambda h, p: ["127.0.0.1"], policy=lambda h, ip, p: True, ports=(port,), **kw)


def _serve_body(headers: bytes, body: bytes):
    def h(c):
        c.sendall(b"HTTP/1.1 200 OK\r\n" + headers + b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
                  + body)
    return h


@pytest.fixture
def raw():
    servers = []

    def make(handler):
        s, port, stop = _raw_server(handler)
        servers.append((s, stop))
        return port
    yield make
    for s, stop in servers:
        stop.set()
        s.close()


# ---------------------------------------------------------------------------------------------- H1

def test_h1_reviewer_nested_gzip_bomb_is_refused_unread(raw):
    inner = b"".join(_gz_stream(_zeros(256 << 20)))
    bomb = b"".join(_gz_stream([inner]))
    port = raw(_serve_body(b"Content-Encoding: gzip, gzip\r\nContent-Type: text/html\r\n", bomb))
    r = _fetcher(port, timeout_s=5, max_bytes=256 * 1024).fetch(f"http://x.test:{port}/", honor_robots=False)
    assert r.state == "UNSUPPORTED_ENCODING" and r.body is None


def test_h1_stacked_encoding_in_two_header_lines_refused(raw):
    port = raw(_serve_body(b"Content-Encoding: gzip\r\nContent-Encoding: gzip\r\n",
                           gzip.compress(gzip.compress(b"hi"))))
    assert _fetcher(port, timeout_s=2).fetch(f"http://x.test:{port}/", honor_robots=False).state == \
        "UNSUPPORTED_ENCODING"


@pytest.mark.parametrize("coding", [b"br", b"zstd", b"compress", b"gzip, identity, deflate"])
def test_h1_unknown_or_stacked_codings_refused(raw, coding):
    port = raw(_serve_body(b"Content-Encoding: " + coding + b"\r\n", b"\x00" * 64))
    assert _fetcher(port, timeout_s=2).fetch(f"http://x.test:{port}/", honor_robots=False).state == \
        "UNSUPPORTED_ENCODING"


def test_h1_single_gzip_bomb_stops_at_the_decoded_budget(raw):
    bomb = b"".join(_gz_stream(_zeros(64 << 20)))           # small on the wire, 64 MiB decoded
    port = raw(_serve_body(b"Content-Encoding: gzip\r\n", bomb))
    r = _fetcher(port, timeout_s=5, max_bytes=256 * 1024).fetch(f"http://x.test:{port}/", honor_robots=False)
    assert r.state == "TOO_LARGE" and r.body is None


@pytest.mark.parametrize("coding,payload", [
    (b"gzip", gzip.compress(b"<title>ok</title>")), (b"x-gzip", gzip.compress(b"<title>ok</title>")),
    (b"deflate", zlib.compress(b"<title>ok</title>")),
    (b"deflate", zlib.compress(b"<title>ok</title>")[2:-4]),           # raw deflate, as some servers send
    (b"identity", b"<title>ok</title>")])
def test_h1_supported_codings_decode(raw, coding, payload):
    port = raw(_serve_body(b"Content-Type: text/html\r\nContent-Encoding: " + coding + b"\r\n", payload))
    r = _fetcher(port, timeout_s=2).fetch(f"http://x.test:{port}/", honor_robots=False)
    assert r.state == "OK" and r.body == b"<title>ok</title>"


def test_h1_corrupt_gzip_is_a_state(raw):
    port = raw(_serve_body(b"Content-Encoding: gzip\r\n", b"\x1f\x8b\x08\x00garbage-not-gzip"))
    assert _fetcher(port, timeout_s=2).fetch(f"http://x.test:{port}/", honor_robots=False).state == "PROTOCOL_ERROR"


def test_h1_memory_error_becomes_a_state(raw, monkeypatch):
    port = raw(_serve_body(b"", b"<title>x</title>"))

    def boom(self, data):
        raise MemoryError()
    monkeypatch.setattr(fetch_mod._Decoder, "feed", boom)
    r = _fetcher(port, timeout_s=2).fetch(f"http://x.test:{port}/", honor_robots=False)
    assert r.state == "RESOURCE_LIMIT"


# ---------------------------------------------------------------------------------------------- M1

def _header_drip(c):
    for b in b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 60 + b"\r\nContent-Length: 2\r\n\r\nok":
        c.send(bytes([b]))
        time.sleep(0.2)


def test_m1_header_drip_hits_the_hard_deadline(raw):
    port = raw(_header_drip)
    assert _fetcher(port, timeout_s=1).fetch(f"http://x.test:{port}/", honor_robots=False).state == "TIMEOUT"


def test_m1_kill_switch_stops_a_fetch_inside_the_headers(raw):
    port = raw(_header_drip)
    calls = []

    def guard(**kw):
        calls.append(kw)
        if len(calls) > 4:                    # past the hop check, inside the socket reads
            raise Killed("KILLED_GLOBAL")
    r = _fetcher(port, timeout_s=30).fetch(f"http://x.test:{port}/", honor_robots=False, guard=guard)
    assert r.state == "KILLED" and r.detail == "KILLED_GLOBAL"


def test_m1_backend_is_installed_on_every_transport():
    import httpx
    t = fetch_mod.install_deadline_backend(httpx.HTTPTransport())
    assert t._pool._network_backend is fetch_mod._BACKEND
    with pytest.raises(RuntimeError):
        fetch_mod.install_deadline_backend(object())


# ---------------------------------------------------------------------------------------------- L1, L2

@pytest.mark.parametrize("ip", ["fec0::1", "feff::1", "192.88.99.1", "192.88.99.254"])
def test_l1_more_non_public_ranges(ip):
    assert fetch_mod.public_address(ip) is False


def test_l2_only_ports_80_and_443(srv):
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: ["93.184.216.34"])
    for u in ("http://x.test:8080/", "https://x.test:22/", "http://x.test:6379/"):
        assert f.fetch(u).state == "REFUSED_PORT"
    srv.redirect("site.test", "/go", "http://other.test:8443/")
    r = srv.fetcher(ports=(80, 443, srv.port)).fetch(srv.url(path="/go"))
    assert r.state == "REFUSED_PORT" and r.redirects[0]["status"] == 301


# ---------------------------------------------------------------------------------------------- M2

def test_m2_reviewer_probe_client_domain_cannot_be_audited_free(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    h.tenant("acme", "client", ("site.test",))
    h.refused(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True), 409,
              "DOMAIN_TAKEN")
    h.refused(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["www.site.test"]}, andre=True), 409,
              "DOMAIN_TAKEN")
    h.refused(audit(h, tid="zbm"), 403, "DOMAIN_NOT_AUTHORIZED")
    h.refused(audit(h, tid="acme"), 409, "INVOICE_REQUIRED")
    assert srv.seen == []
    h.ok(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": ["site.test", "acme.example"]},
                andre=True))                                   # re-registering its own domain is fine


# ---------------------------------------------------------------------------------------------- M3

@pytest.mark.parametrize("sw,code", [("capability:audit", "KILLED_CAPABILITY"), ("write", "KILLED_WRITE"),
                                     ("tenant:zbm", "KILLED_TENANT")])
def test_m3_reviewer_probe_switch_mid_run_stops_and_refuses_completion(tmp_path, srv, sw, code):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)

    def home(handler):
        h.svc.set_switch("dashboard", {"request_id": rid(), "switch": sw, "engaged": True}, andre=False)
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = home
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["interrupted_reason"] == code and a["report"] is None
    assert not any(s["path"] == "/about" for s in srv.seen)
    assert h.ledger.of_type("audit_report_recorded") == []
    assert h.ledger.of_type("audit_interrupted")[0]["_payload"]["reason"] == code


# ---------------------------------------------------------------------------------------------- L3

def test_l3_browser_identity_fetch_honours_robots_for_our_crawler(tmp_path, srv):
    install_site(srv, robots="User-agent: ZBM-SEO-Audit\nDisallow: /about\n")
    h = harness(tmp_path, srv)
    own(h)
    h.ok(audit(h), 201)
    about = [s for s in srv.seen if s["path"] == "/about"]
    assert about == []                                     # neither identity fetched a path our crawler may not
    assert any(s["path"] == "/" and fetch_mod.PRODUCT_TOKEN not in (s["ua"] or "") for s in srv.seen)
