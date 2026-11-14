"""A-09 (decompression bombs), A-13 (long Location), A-15 (huge pages and lying sizes)."""

from __future__ import annotations

import gzip
import tracemalloc
import zlib

import httpx
import pytest

from agents_run import codes, one, run3
from cert_web import ORG_LD, Web, page, site
from common import check_ran

CAP = 256 * 1024


def _deflate_raw(b: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(b) + c.flush()


BOMBS = {
    "gzip": (gzip.compress(b"\0" * (20 * 2 ** 20)), "gzip", "TOO_LARGE"),
    "deflate": (zlib.compress(b"\0" * (20 * 2 ** 20)), "deflate", "TOO_LARGE"),
    "raw_deflate": (_deflate_raw(b"\0" * (20 * 2 ** 20)), "deflate", "TOO_LARGE"),
    "multi_member": (gzip.compress(b"\0" * (2 ** 20)) * 30, "gzip", "TOO_LARGE"),
    "stacked": (gzip.compress(gzip.compress(b"x")), "gzip, gzip", "UNSUPPORTED_ENCODING"),
    "br": (b"\x0b\x02\x80", "br", "UNSUPPORTED_ENCODING"),
    "zstd": (b"\x28\xb5\x2f\xfd", "zstd", "UNSUPPORTED_ENCODING"),
    "compress": (b"\x1f\x9d", "compress", "UNSUPPORTED_ENCODING"),
    "corrupt": (b"\x1f\x8b\x08\x00" + b"\xff" * 50, "gzip", "PROTOCOL_ERROR"),
    "trailing": (gzip.compress(b"<p>x</p>") + b"junkjunk", "gzip", "PROTOCOL_ERROR"),
    "truncated12": (gzip.compress(b"<p>x</p>")[:12], "gzip", "PROTOCOL_ERROR"),
    "zeros_10_members": (gzip.compress(b"\0" * (6 * 2 ** 20)) * 10, "gzip", "TOO_LARGE"),
    "identity_then_gzip": (b"<p>x</p>", "identity, gzip", "PROTOCOL_ERROR"),   # identity is no coding
}


@pytest.mark.parametrize("name", sorted(BOMBS) * 2)
def test_A_09_bombs_as_content_encoding(name):
    body, enc, want = BOMBS[name]
    web = Web()
    web.route("/", body, ctype="text/html", content_encoding=enc)
    tracemalloc.start()
    r = web.fetcher(max_bytes=CAP).fetch("http://site.test/")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert r.state == want, (name, r.state)
    if r.partial:
        assert r.partial["bytes_read"] <= CAP + 64 * 1024
    assert peak <= 96 * 2 ** 20, (name, peak)
    check_ran("A-09", 26)


@pytest.mark.parametrize("n", list(range(1990, 2001, 1)) + [2011, 2050, 2100, 4000, 8192])
def test_A_13_long_location_relative_and_absolute(n):
    web = site(Web())
    for absolute in (False, True):
        path = "/l/" + "c" * (n - 3 - (len("http://site.test") if absolute else 0))
        target = ("http://site.test" if absolute else "") + path
        web.route("/", "", status=301, location=target)
        web.route(path, page(canonical="http://site.test" + path, ld=ORG_LD))
        _, s, _, _, _, _ = run3(web)
        assert "PAGE_HTTP_ERROR" not in codes(s) and "PAGE_UNREACHABLE" not in codes(s), (n, absolute)
    check_ran("A-13", 20)


@pytest.mark.parametrize("n", [8193, 9000, 65536])
def test_A_13_over_8192_is_redirect_too_long_and_ie(n):
    web = site(Web())
    web.route("/", "", status=302, location="/l/" + "d" * (n - 3))
    _, s, _, _, _, _ = run3(web)
    f = one(s, "PAGE_UNREACHABLE")
    assert f["detail"]["fetch_state"] == "REDIRECT_TOO_LONG" and f["decision"] == "INSUFFICIENT_EVIDENCE"
    assert not [p for _, p in web.requests if p.startswith("/l/")]


def test_A_13_multi_hop_long_locations():
    web = site(Web())
    a, b = "/h1/" + "e" * 5000, "/h2/" + "f" * 6000
    web.route("/", "", status=301, location=a)
    web.route(a, "", status=301, location=b)
    web.route(b, page(canonical="http://site.test" + b, ld=ORG_LD))
    _, s, _, _, _, _ = run3(web)
    assert "REDIRECT_CHAIN" in codes(s) and "PAGE_HTTP_ERROR" not in codes(s)


LIES = ["over", "declared_huge", "declared_zero", "endless", "many_1mib"]


@pytest.mark.parametrize("kind", LIES * 2)
def test_A_15_huge_pages_and_lying_sizes(kind):
    web = site(Web())
    if kind == "over":
        web.route("/", b"a" * (CAP + 1), ctype="text/html")
    elif kind == "declared_huge":
        web.route("/", b"<p>x</p>", ctype="text/html", content_length=str(10 * 2 ** 30))
    elif kind == "declared_zero":
        web.route("/", page(ld=ORG_LD), ctype="text/html")
    elif kind == "endless":
        class Endless(httpx.SyncByteStream):
            def __iter__(self):
                while True:
                    yield b"z" * 65536
        web.routes[("site.test", "/")] = lambda req: httpx.Response(200, headers={"content-type": "text/html"},
                                                                    stream=Endless())
    else:
        web.route("/", b"b" * (2 ** 20), ctype="text/html")
    c, s, _, r, pages, _ = run3(web)
    st = pages["/"]["fetch"].state
    if kind in ("over", "declared_huge", "endless", "many_1mib"):
        assert st == "TOO_LARGE", (kind, st)
        f = one(s, "PAGE_UNREACHABLE")
        assert f["severity"] == "high" and f["decision"] == "INSUFFICIENT_EVIDENCE"
        q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "TOO_LARGE"]
        assert q and q[0]["size_basis"] == "read_before_stop"
        assert not [x for x in r["findings"] if x["code"].startswith("TITLE")]
    check_ran("A-15", 10)
