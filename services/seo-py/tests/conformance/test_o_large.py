"""O-06: large real-shaped files read without approximation or undecided decisions (the 50 MiB sitemap: lead gate)."""

from __future__ import annotations

import time

import pytest

from agents import logs as logs_mod
from agents_run import codes, run3
from cert_web import Web, site, sitemap
from primitives import robots as robots_mod


def _big_robots() -> str:
    lines = ["User-agent: *"]
    lines += [f"Disallow: /section-{i:04d}/private-area/" for i in range(1500)]
    lines += [f"Disallow: /*?filter{i}=" for i in range(250)] + [f"Disallow: /*/tag-{i}/*.pdf$" for i in range(250)]
    text = "\n".join(lines) + "\n"
    pad = 119_906 - len(text)
    return text + "#" * (pad - 1) + "\n" if pad > 1 else text


def test_O_06_120kb_robots_nothing_approximate_or_undecided():
    text = _big_robots()
    assert abs(len(text) - 119_906) <= 2
    web = site(Web(), robots=text + "Sitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap([f"http://site.test/section-{i:04d}/page" for i in range(500)]),
              ctype="application/xml")
    _, s, d, _, _, _ = run3(web, ["/"])
    assert not codes(s) & {"ROBOTS_BLOCK_APPROXIMATE", "ROBOTS_CHECK_INCOMPLETE"}
    assert not codes(d) & {"SITEMAP_ROBOTS_CHECK_INCOMPLETE"} and d["facts"]["robots_unchecked"] == 0
    rob = robots_mod.parse(text)
    lines = "\n".join(f'192.0.2.5 - - [10/Oct/2026:13:55:36 -0700] "GET /section-{i:04d}/x?filter{i % 250}=1 '
                      f'HTTP/1.1" 200 1 "-" "Googlebot/2.1"' for i in range(450)).encode() + b"\n"
    t0 = time.thread_time()
    delta = logs_mod.process_chunk("combined", lines, b"k" * 32, lambda ip, t: "unverified", {}, {"left": 0},
                                   rob, {}, robots_expected=True)
    assert delta["robots_unchecked"] == 0 and time.thread_time() - t0 < 2.0


@pytest.mark.lead_gate
def test_O_06_50_mib_sitemap_read():
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    pad = "p" * 1000                     # 50,000 URLs: 52.2 MB, just under the 52,428,800-byte limit
    body = f"<?xml version='1.0'?><urlset xmlns='{ns}'>" + "".join(
        f"<url><loc>http://site.test/{i}/{pad}</loc></url>" for i in range(50_000)) + "</urlset>"
    assert 49 * 2 ** 20 < len(body) <= 52_428_800
    web = site(Web())
    web.route("/sitemap.xml", body, ctype="application/xml")
    _, _, d, _, _, _ = run3(web)
    assert d["facts"]["sitemap_urls"] == 50_000 and "SITEMAP_TOO_LARGE" not in codes(d)
