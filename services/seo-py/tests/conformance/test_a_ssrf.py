"""A-12 (SSRF-shaped URLs in 7 positions) and A-21 (own-domain lookalikes)."""

from __future__ import annotations

import pytest

from agents_run import codes, run3
from cert_web import ORG_LD, Web, index, page, site, sitemap
from common import check_ran

TARGETS = ["http://127.0.0.1/", "http://10.0.0.5/", "http://169.254.169.254/latest/", "http://[::1]/",
           "http://[::ffff:127.0.0.1]/", "http://0177.0.0.1/", "http://2130706433/", "http://0x7f000001/",
           "http://site.test:22/", "ftp://site.test/", "http://user:pw@site.test/", "http://internal.test/",
           "http://100.64.0.9/", "http://[fec0::1]/"]
POSITIONS = ["redirect", "canonical", "robots_sitemap", "sitemap_loc", "index_child", "llms_link", "homepage_link"]


def _resolver(web):
    base = web.resolve

    def resolve(host, port):
        if host == "internal.test":
            return ["10.9.9.9"]
        return base(host, port)
    return resolve


@pytest.mark.parametrize("pos", POSITIONS)
def test_A_12_no_connection_to_a_non_public_address(pos):
    ran = 0
    for t in TARGETS:
        web = site(Web())
        if pos == "redirect":
            web.route("/", "", status=302, location=t)
        elif pos == "canonical":
            web.route("/", page(canonical=t, ld=ORG_LD))
        elif pos == "robots_sitemap":
            web.route("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {t}\n", ctype="text/plain")
        elif pos == "sitemap_loc":
            web.route("/sitemap.xml", sitemap([t, "http://site.test/"]), ctype="application/xml")
        elif pos == "index_child":
            web.route("/sitemap.xml", index([t]), ctype="application/xml")
        elif pos == "llms_link":
            web.route("/llms.txt", f"# x\n## d\n- [a]({t})\n", ctype="text/plain")
        else:
            web.route("/", page(body=f"<a href='{t}'>x</a>" + "<p>w</p>" * 100, ld=ORG_LD))
        _, s, d, _, _, _ = run3(web, ["/"], resolver=_resolver(web))
        bad = [(h, p) for h, p in web.requests if h not in ("site.test", "www.site.test", "other.test")]
        assert not bad, (pos, t, bad)
        ran += 1
    check_ran("A-12", ran * len(POSITIONS))


LOOKALIKES = ["site.test.evil.test", "evilsite.test", "xn--sit-wqa.test", "site.tes", "www-site.test",
              "evil.test", "s\u0456te.test", "site.test-evil.test"]


@pytest.mark.parametrize("host", LOOKALIKES * 5)
def test_A_21_lookalike_canonical_is_other_host_and_coded(host):
    web = site(Web())
    web.route("/", page(canonical=f"http://{host}/", ld=ORG_LD))
    _, s, _, _, _, _ = run3(web)
    f = [x for x in s["findings"] if x["code"] == "CANONICAL_OTHER_HOST"]
    assert len(f) == 1, (host, codes(s))
    stored = f[0]["detail"]["canonical"]
    assert stored.startswith("http://{c:host:") and "site" not in stored.split("{c:host:")[1], stored
    check_ran("A-21", 40)
