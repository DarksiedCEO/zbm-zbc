"""A-04 (hostile percent-encodings: in test_b_robots B-RB-04), A-05 (text encodings and line shapes), A-06 (size and
wire shapes), A-07 (status / redirect hostility)."""

from __future__ import annotations

import gzip

import pytest

from agents_run import codes, run3
from cert_web import Web, site
from common import check_ran, rng
from oracles import rfc9309
from primitives import robots as robots_mod


def _shapes_a05(r):
    base = "User-agent: *\nDisallow: /x\nAllow: /x/y\n"
    out = [("bom", b"\xef\xbb\xbf" + base.encode()),
           ("utf16le_bom", b"\xff\xfe" + base.encode("utf-16-le")),
           ("utf16be", base.encode("utf-16-be")),
           ("latin1", "User-agent: *\nDisallow: /caf\u00e9\n".encode("latin-1")),
           ("cr_only", base.replace("\n", "\r").encode()), ("crlf", base.replace("\n", "\r\n").encode()),
           ("mixed", b"User-agent: *\r\nDisallow: /x\rAllow: /x/y\n"),
           ("nul", b"User-agent: *\nDisallow: /a\x00b\n"),
           ("ctl_vt", b"User-agent: *\nDisallow: /a\x0bDisallow: /b\n"),
           ("ctl_ff", b"User-agent: *\nDisallow: /a\x0cb\n"),
           ("u2028", "User-agent: *\nDisallow: /a\u2028b\n".encode()),
           ("no_final_newline", base.rstrip("\n").encode()),
           ("case_spacing", b"USER-AGENT   :   *\n  disallow:/x  \n"),
           ("comment_in_value", b"User-agent: * # all\nDisallow: /x # private\n"),
           ("long_line", b"User-agent: *\nDisallow: /" + b"q" * (600 * 1024) + b"\n")]
    return out


def test_A_05_text_encodings_and_line_shapes_agree_with_the_oracle():
    r = rng(7, "a05")
    ran = 0
    for _ in range(4):
        for name, data in _shapes_a05(r):
            p = robots_mod.parse(data.decode("utf-8", "surrogateescape"))
            for path in (b"/x", b"/x/y", b"/a", b"/b", b"/a\x00b", "/caf\u00e9".encode("latin-1"),
                         b"/a%00b", b"/a%0Bb", "/a\u2028b".encode()):
                if name == "long_line":
                    continue
                want = rfc9309.allowed(data, "Googlebot", path)
                got = p.decide("Googlebot", path.decode("utf-8", "surrogateescape"))["allowed"]
                assert got is want, (name, path, got, want)
            if name.startswith("utf16"):
                assert p.lines_ignored > 0 or not p.groups or not any(g.rules for g in p.groups)
            ran += 1
    check_ran("A-05", ran)


@pytest.mark.parametrize("kib,char", [(499, "a"), (500, "a"), (501, "a"), (2048, "a"), (499, "\u30c4"),
                                      (501, "\u30c4")])
def test_A_06_sizes_read_to_512000_decoded_octets(kib, char):
    head = "User-agent: *\nDisallow: /early\n"
    fill = "#" + char * ((kib * 1024 - len(head)) // len(char.encode())) + "\n"
    web = site(Web(), robots=head + fill + "Disallow: /late\n")
    c, s, _, _, _, _ = run3(web, ["/"])
    rb = c.robots
    assert rb["status_class"] == "ok" and "ROBOTS_UNREACHABLE" not in codes(s)
    parsed = rb["parsed"]
    assert parsed.bytes_read <= 512_000
    assert parsed.decide("x", "/early")["allowed"] is False


def test_A_06_gzip_empty_members_and_declared_10_gib():
    member = gzip.compress(b"")
    web = site(Web())
    web.route("/robots.txt", member * 110_000 + gzip.compress(b"User-agent: *\nAllow: /\n"), ctype="text/plain",
              content_encoding="gzip")
    c, s, _, _, _, _ = run3(web)
    assert c.robots["status_class"] == "ok" and "ROBOTS_UNREACHABLE" not in codes(s)
    web = site(Web())
    web.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain", content_length=str(10 * 2 ** 30))
    c, s, _, _, _, _ = run3(web)
    assert "ROBOTS_UNREACHABLE" not in codes(s)
    web = site(Web())
    web.route("/robots.txt", gzip.compress(b"#" * (4 * 2 ** 20) + b"\nUser-agent: *\nDisallow: /\n"),
              ctype="text/plain", content_encoding="gzip")
    c, s, _, _, _, _ = run3(web)
    assert c.robots["status_class"] == "ok" and c.robots["parsed"].truncated
    check_ran("A-06", 12)


@pytest.mark.parametrize("status,klass", [(500, "unreachable"), (502, "unreachable"), (503, "unreachable"),
                                          (429, "unreachable"), (401, "unavailable"), (403, "unavailable"),
                                          (404, "unavailable"), (410, "unavailable"), (451, "unavailable")] * 2)
def test_A_07_statuses(status, klass):
    web = site(Web())
    web.route("/robots.txt", "x", status=status, ctype="text/plain")
    c, s, _, _, _, _ = run3(web)
    assert c.robots["status_class"] == klass, (status, c.robots["status_class"])


@pytest.mark.parametrize("target,want", [("http://other.test/robots.txt", "ok"), ("http://10.0.0.1/robots.txt",
                                                                                   "unreachable"),
                                         ("/robots.txt", "unreachable")] * 2)
def test_A_07_redirects(target, want):
    web = site(Web())
    web.route("/robots.txt", "", status=301, ctype="text/plain", location=target)
    web.route("/robots.txt", "User-agent: *\nAllow: /\n", ctype="text/plain", host="other.test")
    c, s, _, _, _, _ = run3(web)
    assert c.robots["status_class"] == want
    assert not [1 for h, _ in web.requests if h.startswith("10.")]
    check_ran("A-07", 24)


def test_A_06_a_rule_deep_in_the_file_is_read():
    head = "User-agent: *\n" + "".join(f"Disallow: /filler{i}\n" for i in range(5000))
    web = site(Web(), robots=head + "Disallow: /deep-rule\n")
    c, _, _, _, _, _ = run3(web, ["/"])
    assert len(head) > 60_000 and c.robots["parsed"].decide("x", "/deep-rule")["allowed"] is False
