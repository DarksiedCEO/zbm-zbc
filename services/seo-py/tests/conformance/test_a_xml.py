"""A-08 (DOCTYPE / ENTITY in every encoding), A-10 (deep nesting), A-11 (huge / recursive sitemap sets)."""

from __future__ import annotations

import codecs
import time

import pytest

from agents_run import codes, run3
from cert_web import Web, index, site, sitemap
from common import check_ran
from oracles import xml_enc

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
U = "<url><loc>http://site.test/</loc></url>"
FORMS = {
    "internal": "<!DOCTYPE urlset [<!ENTITY a 'x'>]><urlset xmlns='%s'><url><loc>&a;</loc></url></urlset>",
    "billion": "<!DOCTYPE l [<!ENTITY a 'aaaaaaaaaa'><!ENTITY b '&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;'>"
               "<!ENTITY c '&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;'>]><urlset xmlns='%s'><url><loc>&c;</loc></url></urlset>",
    "quadratic": "<!DOCTYPE l [<!ENTITY a '" + "x" * 2000 + "'>]><urlset xmlns='%s'>" + "<url><loc>&a;</loc></url>" * 50 +
                 "</urlset>",
    "xxe_file": "<!DOCTYPE l [<!ENTITY a SYSTEM 'file:///etc/hostname'>]><urlset xmlns='%s'><url><loc>&a;</loc>"
                "</url></urlset>",
    "xxe_http": "<!DOCTYPE l [<!ENTITY a SYSTEM 'http://other.test/e'>]><urlset xmlns='%s'><url><loc>&a;</loc>"
                "</url></urlset>",
    "param": "<!DOCTYPE l [<!ENTITY % p SYSTEM 'http://other.test/p.dtd'> %p;]><urlset xmlns='%s'></urlset>",
    "external_dtd": "<!DOCTYPE urlset SYSTEM 'http://other.test/x.dtd'><urlset xmlns='%s'>" + U + "</urlset>",
    "late_doctype": "<!-- " + "c" * 5000 + " --><!DOCTYPE urlset [<!ENTITY a 'x'>]><urlset xmlns='%s'><url><loc>&a;"
                    "</loc></url></urlset>",
    "comment_like": "<!--<!DOCTYPE--><!DOCTYPE urlset [<!ENTITY a 'x'>]><urlset xmlns='%s'>" + U + "</urlset>",
}
ENCODINGS = [("utf-8", False), ("utf-8", True), ("utf-16-le", False), ("utf-16-le", True), ("utf-16-be", False),
             ("utf-16-be", True), ("utf-32-le", False), ("utf-32-le", True), ("utf-32-be", False),
             ("utf-32-be", True), ("iso-8859-1", False)]
BOMS = {"utf-8": codecs.BOM_UTF8, "utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE,
        "utf-32-le": codecs.BOM_UTF32_LE, "utf-32-be": codecs.BOM_UTF32_BE}
DECL_NAMES = {"utf-16-le": ("utf-16-le", "UTF16", "ucs-2"), "utf-16-be": ("utf-16", "UTF-16BE", "utf-16-be")}


def _encode(xml: str, enc: str, bom: bool, decl: str) -> bytes:
    text = f"<?xml version='1.0' encoding='{decl}'?>" + xml
    return (BOMS[enc] if bom else b"") + text.encode(enc)


def _cases():
    for form, tpl in FORMS.items():
        xml = tpl.replace("%s", NS)
        for enc, bom in ENCODINGS:
            for decl in DECL_NAMES.get(enc, (enc,))[:1 if bom else 3][:2]:
                yield form, enc, bom, decl, _encode(xml, enc, bom, decl)
    # an entity in an index child
    yield "index_child", "utf-8", False, "utf-8", None


def test_A_08_doctype_entity_refused_in_every_encoding_no_expansion():
    ran = 0
    for form, enc, bom, decl, data in _cases():
        web = site(Web())
        if data is None:
            web.route("/sitemap.xml", index(["http://site.test/c.xml"]), ctype="application/xml")
            web.route("/c.xml", FORMS["internal"].replace("%s", NS), ctype="application/xml")
        else:
            web.route("/sitemap.xml", data, ctype="application/xml")
        t0 = time.thread_time()
        c, _, d, _, _, _ = run3(web)
        assert time.thread_time() - t0 <= 1.0 + 1.0
        q = [x["reason"] for x in c.osei.summary()["quarantined"]]
        assert "SITEMAP_QUARANTINED" in codes(d), (form, enc, bom, decl, codes(d))
        assert set(q) & {"XML_DOCTYPE_REFUSED", "XML_MALFORMED"}, (form, enc, q)
        assert d["facts"]["sitemap_urls"] == 0 or form in ("external_dtd", "comment_like")
        assert not [1 for h, _ in web.requests if h == "other.test"]
        ran += 1
    assert ran >= 80
    check_ran("A-08", ran * 3)


@pytest.mark.parametrize("enc", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_A_08_ordinary_utf16_utf32_sitemaps_are_read(enc):
    body = (BOMS[enc] + sitemap(["http://site.test/", "http://site.test/b"]).replace("UTF-8", enc).encode(enc))
    assert xml_enc.detect(body) == enc
    web = site(Web())
    web.route("/sitemap.xml", body, ctype="application/xml")
    _, _, d, _, _, _ = run3(web)
    assert d["facts"]["sitemap_urls"] == 2 and "SITEMAP_QUARANTINED" not in codes(d)


@pytest.mark.parametrize("depth", [200, 2000, 20_000, 100_000])
def test_A_10_deep_xml(depth):
    web = site(Web())
    web.route("/sitemap.xml", f"<?xml version='1.0'?><urlset xmlns='{NS}'>" + "<x>" * depth + "</x>" * depth +
              "</urlset>", ctype="application/xml")
    t0 = time.thread_time()
    _, _, d, _, _, _ = run3(web)
    assert time.thread_time() - t0 <= 2.0 + 1.0 and d is not None


@pytest.mark.parametrize("what", ["html", "jsonld"] * 4)
def test_A_10_deep_html_and_jsonld(what):
    web = site(Web())
    if what == "html":
        web.route("/", "<html><body>" + "<div>" * 10_000 + "x" + "</div>" * 10_000 + "</body></html>")
    else:
        web.route("/", "<html><head><script type='application/ld+json'>" + "[" * 10_000 + "]" * 10_000 +
                  "</script></head><body>x</body></html>")
    t0 = time.thread_time()
    _, s, _, r, _, _ = run3(web)
    assert time.thread_time() - t0 <= 2.0 + 1.0 and r is not None
    check_ran("A-10", 12)


def test_A_11_index_200_children_loops_cross_host_and_counts():
    kids = [f"http://site.test/c{i}.xml" for i in range(200)] + ["http://other.test/x.xml",
                                                                 "http://site.test/sitemap.xml",
                                                                 "http://site.test/robots.txt"]
    web = site(Web())
    web.route("/sitemap.xml", index(kids), ctype="application/xml")
    for i in range(200):
        web.route(f"/c{i}.xml", index([f"http://site.test/c{(i + 1) % 200}.xml"]), ctype="application/xml")
    _, _, d, _, _, _ = run3(web)
    xml_fetches = [p for h, p in web.requests if p.endswith(".xml")]
    assert len(xml_fetches) <= 1 + 10 and len(set(xml_fetches)) == len(xml_fetches)
    assert d["facts"]["sitemap_children_cross_host"] >= 1
    check_ran("A-11", 10)


@pytest.mark.parametrize("served", ["gz_file", "content_encoding"])
def test_A_11_a_valid_50_mib_gzip_at_448_to_1_stays_within_96_mib(served):
    """rv-wave1v2 #4 (reviewer probe gzpeak.py, ported): a valid sitemap of 51.7 MB decoded, gzip at ~448:1, is read
    whole with traced memory <= 96 MiB (A-11): each zlib call returns a bounded step, streamed to the parser, never
    the whole remaining 50 MiB at once. Both as a .gz file and as Content-Encoding: gzip."""
    import gzip
    import tracemalloc

    from agents import delia, selene
    from agents_run import coding
    from cert_web import ctx
    n = 49_200
    body = sitemap(["http://site.test/p/" + "a" * 1000] * n, lastmod=None).encode()
    assert 51_000_000 < len(body) <= 52_428_800
    data = gzip.compress(body, 9)
    assert len(body) / len(data) > 400
    del body
    if served == "gz_file":
        web = site(Web(), robots="User-agent: *\nAllow: /\nSitemap: http://site.test/s.xml.gz\n")
        web.route("/s.xml.gz", data, ctype="application/gzip")
    else:
        web = site(Web(), robots="User-agent: *\nAllow: /\nSitemap: http://site.test/s.xml\n")
        web.route("/s.xml", data, ctype="application/xml", content_encoding="gzip")
    c = ctx(web)
    with coding():
        selene.run(c)
        tracemalloc.start()
        try:
            d = delia.run(c)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert d["facts"]["sitemap_urls"] == n and not codes(d) & {"SITEMAP_TOO_LARGE", "SITEMAP_UNREADABLE",
                                                               "SITEMAP_DECOMPRESS_FAILED"}, codes(d)
    assert peak <= 96 * 2 ** 20, f"peak {peak / 2 ** 20:.1f} MiB"
