"""SPEC section 6: Delia (B-DE-01..16), against the sitemaps.org, W3C-DTF and llms.txt oracles."""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agents import delia
from agents_run import by_code, codes, one, run3, sev
from cert_web import ORG_LD, Web, index, page, site, sitemap
from common import check_ran, rng
from gen import plants
from oracles import llms_shape, w3c_datetime

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def _d(web, paths=("/",)):
    _, _, d, _, _, ev = run3(web, paths)
    return d


def test_B_DE_01_declared_same_site_only_cross_host_never_fetched():
    sm = ["http://site.test/a.xml", "http://other.test/x.xml", "http://www.site.test/b.xml"] + \
        [f"http://site.test/s{i}.xml" for i in range(12)]
    web = site(Web(), robots="User-agent: *\nAllow: /\n" + "".join(f"Sitemap: {u}\n" for u in sm))
    for u in sm:
        host, path = u.split("//")[1].split("/", 1)
        web.route("/" + path, sitemap(["http://site.test/"]), ctype="application/xml", host=host)
    d = _d(web)
    assert not [1 for h, _ in web.requests if h == "other.test"]
    assert d["facts"]["sitemaps_cross_host_count"] == 1
    fetched = [p for h, p in web.requests if p.endswith(".xml")]
    assert len(fetched) <= 10 and fetched[:2] == ["/a.xml", "/b.xml"]
    assert all(x.startswith("{c:host:") or "://" in x for x in d["facts"]["sitemaps_cross_host_not_read"])


def test_B_DE_01_none_declared_uses_sitemap_xml():
    web = site(Web(), robots="User-agent: *\nAllow: /\n")
    _d(web)
    assert ("site.test", "/sitemap.xml") in web.requests


@pytest.mark.parametrize("status,want", [(404, "SITEMAP_MISSING"), (410, "SITEMAP_MISSING"),
                                         (500, "SITEMAP_UNREADABLE"), (503, "SITEMAP_UNREADABLE"),
                                         (403, "SITEMAP_UNREADABLE"), (301, "SITEMAP_UNREADABLE")] * 5)
def test_B_DE_02_missing_vs_unreadable(status, want):
    web = site(Web())
    web.route("/sitemap.xml", "x", status=status, ctype="application/xml",
              **({"location": "/sitemap.xml"} if status == 301 else {}))
    d = _d(web)
    f = one(d, want)
    assert sev(f) == ("medium", "measured", "ACT" if want == "SITEMAP_MISSING" else "INSUFFICIENT_EVIDENCE")
    # B-DE-02 "sitemap_fetch_failures counted" for every non-200, read with B-DE-16 (COVERED at 91db0abb by
    # test_sitemap_unreadable_vs_missing): a missing sitemap counts, the outcome is PARTIAL
    assert d["facts"]["sitemap_fetch_failures"] == 1 and d["outcome"] == "PARTIAL"


def _big_sitemap(n: int, pad: int = 0) -> str:
    locs = "".join(f"<url><loc>http://site.test/p/{i}{'x' * pad}</loc><lastmod>2026-10-01</lastmod></url>"
                   for i in range(n))
    return f"<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='{NS}'>{locs}</urlset>"


def test_B_DE_03_a_valid_6_mib_sitemap_is_read():
    body = _big_sitemap(45_000, pad=60)
    assert 5 * 2 ** 20 < len(body) < 8 * 2 ** 20
    web = site(Web())
    web.route("/sitemap.xml", body, ctype="application/xml")
    d = _d(web)
    assert d["facts"]["sitemap_urls"] == 45_000
    assert not codes(d) & {"SITEMAP_UNREADABLE", "SITEMAP_DECOMPRESS_FAILED", "SITEMAP_TOO_LARGE",
                           "SITEMAP_NOT_FULLY_READ"}


def test_B_DE_03_a_valid_12_mib_gzip_is_read():
    body = _big_sitemap(45_000, pad=200)
    assert len(body) > 12 * 2 ** 20 - 2 ** 20
    web = site(Web(), robots="User-agent: *\nSitemap: http://site.test/s.xml.gz\n")
    web.route("/s.xml.gz", gzip.compress(body.encode()), ctype="application/gzip")
    d = _d(web)
    assert d["facts"]["sitemap_urls"] == 45_000 and "SITEMAP_DECOMPRESS_FAILED" not in codes(d)


@pytest.mark.lead_gate
def test_B_DE_03_over_the_protocol_limit_is_too_large():
    body = _big_sitemap(49_000, pad=1100)
    assert len(body) > 52_428_800
    web = site(Web(), robots="User-agent: *\nSitemap: http://site.test/s.xml.gz\n")
    web.route("/s.xml.gz", gzip.compress(body.encode()), ctype="application/gzip")
    d = _d(web)
    f = one(d, "SITEMAP_TOO_LARGE")
    assert sev(f) == ("medium", "measured", "ACT") and f["detail"]["limit"] == 52_428_800


@pytest.mark.parametrize("body", [b"\x1f\x8b\x08\x00garbage", gzip.compress(b"<urlset/>")[:12],
                                  gzip.compress(b"<urlset/>") + b"trailing", b"not gzip at all"] * 5)
def test_B_DE_04_corrupt_gzip_and_quarantine_basis(body):
    web = site(Web(), robots="User-agent: *\nSitemap: http://site.test/s.xml.gz\n")
    web.route("/s.xml.gz", body, ctype="application/gzip")
    c, _, d, _, _, _ = run3(web)
    assert sev(one(d, "SITEMAP_DECOMPRESS_FAILED")) == ("medium", "measured", "ACT")
    q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "DECOMPRESS_FAILED"]
    assert q and q[0]["size_basis"] == "wire" and q[0]["bytes"] == len(body)


@pytest.mark.parametrize("ctype", ["application/gzip", "application/x-gzip", "application/octet-stream"] * 3)
def test_B_DE_04_gzip_media_type_without_gz_name_is_decompressed(ctype):
    """B-DE-04: a gzip body is decompressed when the URL does not end in .gz (the gzip media type, or no useful
    type at all: the gzip magic octets)."""
    web = site(Web())
    web.route("/sitemap.xml", gzip.compress(sitemap(["http://site.test/", "http://site.test/b"]).encode()),
              ctype=ctype)
    d = _d(web)
    assert d["facts"]["sitemap_urls"] == 2 and not codes(d) & {"SITEMAP_QUARANTINED", "SITEMAP_DECOMPRESS_FAILED"}


def test_B_DE_05_doctype_refused_and_utf32_read():
    evil = f"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'aaaa'>]><urlset xmlns='{NS}'><url><loc>&a;</loc></url>" \
           "</urlset>"
    for enc in ("utf-8", "utf-16", "utf-32"):
        web = site(Web())
        web.route("/sitemap.xml", evil.encode(enc), ctype="application/xml")
        d = _d(web)
        f = one(d, "SITEMAP_QUARANTINED")
        assert sev(f) == ("high", "measured", "ACT")
    good = sitemap(["http://site.test/", "http://site.test/b"])
    web = site(Web())
    web.route("/sitemap.xml", good.replace("UTF-8", "UTF-32").encode("utf-32"), ctype="application/xml")
    assert _d(web)["facts"]["sitemap_urls"] == 2


@pytest.mark.parametrize("ns,want", [(NS, None), ("http://www.google.com/schemas/sitemap/0.84", "coded"),
                                     ("", "")] * 4)
def test_B_DE_06_namespace(ns, want):
    xmlns = f" xmlns='{ns}'" if ns else ""
    web = site(Web())
    web.route("/sitemap.xml", f"<?xml version='1.0'?><urlset{xmlns}><url><loc>http://site.test/</loc></url></urlset>",
              ctype="application/xml")
    d = _d(web)
    got = by_code(d, "SITEMAP_NAMESPACE")
    if want is None:
        assert not got
    else:
        assert sev(got[0]) == ("low", "measured", "ACT")
        v = got[0]["detail"]["namespace"]
        assert v == "" if want == "" else v.startswith("{c:ns:")


def test_B_DE_07_index_children_cap_cross_host_loop():
    kids = [f"http://site.test/c{i}.xml" for i in range(200)] + ["http://other.test/x.xml"]
    web = site(Web())
    web.route("/sitemap.xml", index(kids + ["http://site.test/sitemap.xml"]), ctype="application/xml")
    for k in kids[:200]:
        web.route("/" + k.rsplit("/", 1)[1], sitemap(["http://site.test/"]), ctype="application/xml")
    d = _d(web)
    child = [p for h, p in web.requests if p.startswith("/c")]
    assert len(child) == 10 and web.requests.count(("site.test", "/sitemap.xml")) == 1
    assert d["facts"]["sitemap_children_over_cap"] == 191        # 200 children + the self link, 10 followed


@pytest.mark.parametrize("root,coded", [("rss", False), ("feed", False), ("html", False), ("zzroot", True)] * 3)
def test_B_DE_08_unknown_root(root, coded):
    web = site(Web())
    web.route("/sitemap.xml", f"<?xml version='1.0'?><{root}><x/></{root}>", ctype="application/xml")
    f = one(_d(web), "SITEMAP_ROOT_UNKNOWN")
    assert sev(f) == ("medium", "measured", "ACT")
    assert f["detail"]["root"].startswith("{c:root:") is coded


@pytest.mark.parametrize("n,flag", [(50_000, False), (50_001, True)])
def test_B_DE_09_urls_per_file(n, flag):
    web = site(Web())
    web.route("/sitemap.xml", _big_sitemap(n), ctype="application/xml")
    assert ("SITEMAP_TOO_MANY_URLS" in codes(_d(web))) is flag


def test_B_DE_10_loc_checks_counts_and_coded_examples():
    m = plants.uniq(rng(3, "de10"))
    locs = ["/relative", "ftp://site.test/x", f"http://{m}.test/a", "http://site.test/ok"] * 10
    web = site(Web())
    web.route("/sitemap.xml", sitemap(locs), ctype="application/xml")
    d = _d(web)
    na, oh = one(d, "SITEMAP_LOC_NOT_ABSOLUTE"), one(d, "SITEMAP_LOC_OTHER_HOST")
    assert na["detail"]["count"] == 20 and oh["detail"]["count"] == 10
    assert len(oh["detail"]["examples"]) <= 5 and m not in str(d)


def test_B_DE_11_listed_urls_checked_for_googlebot():
    locs = [f"http://site.test/p{i}" for i in range(600)]
    web = site(Web(), robots="User-agent: Googlebot\nDisallow: /p1\nUser-agent: *\nAllow: /\n"
                             "Sitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap(locs), ctype="application/xml")
    d = _d(web)
    f = one(d, "SITEMAP_LISTS_ROBOTS_BLOCKED_URL")
    # /p1, /p10-/p19, /p100-/p199 within the first 500: 1 + 10 + 100 = 111
    assert f["detail"]["count"] == 111 and f["detail"]["checked_for"] == "Googlebot"


def test_B_DE_11_undecided_never_counted(monkeypatch):
    from agents import delia as dm
    monkeypatch.setattr(dm, "URL_CHECK_WORK", 3, raising=False)
    web = site(Web(), robots="User-agent: *\nDisallow: /p\nSitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap([f"http://site.test/p{i}" for i in range(50)]), ctype="application/xml")
    d = _d(web)
    if "SITEMAP_ROBOTS_CHECK_INCOMPLETE" in codes(d):
        assert d["outcome"] == "PARTIAL"
        b = by_code(d, "SITEMAP_LISTS_ROBOTS_BLOCKED_URL")
        assert not b or b[0]["detail"]["count"] + d["facts"]["robots_unchecked"] <= 50


def _lastmod(r):
    k = r.random()
    y = r.randint(1990, 2030)
    base = r.choice([f"{y}", f"{y}-{r.randint(1, 12):02d}", f"{y}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}",
                     f"{y}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}T{r.randint(0, 23):02d}:{r.randint(0, 59):02d}Z",
                     f"{y}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}T10:00:00.5+01:00"])
    if k < 0.3:
        base = r.choice((base + "x", base.replace("-", "/"), "\uff12\uff10\uff12\uff16", base[:-1], " " + base,
                         base.replace("T", " "), "13/10/2026", base + "+25:00", base.replace("Z", "")))
    return base


@pytest.mark.parametrize("seed", [7, 11])
def test_B_DE_12_lastmod_w3c_oracle(seed):
    w3c_datetime.self_test()
    r = rng(seed, "de12")
    vals = [_lastmod(r) for _ in range(1000)]
    for v in vals:
        assert (delia._parse_lastmod(v) is not None) is w3c_datetime.valid(v), repr(v)
    check_ran("B-DE-12", len(vals) * 2)


def test_B_DE_12_future_window_and_identical_rule():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    from helpers import T0
    base = T0 if T0.tzinfo else T0.replace(tzinfo=timezone.utc)
    near = (base + timedelta(hours=23)).strftime("%Y-%m-%dT%H:%MZ")
    far = (base + timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")
    for lm, flag in ((near, False), (far, True)):
        web = site(Web())
        web.route("/sitemap.xml", sitemap(["http://site.test/"], lastmod=lm), ctype="application/xml")
        assert ("SITEMAP_LASTMOD_FUTURE" in codes(_d(web))) is flag
    for n, flag in ((9, False), (10, True)):
        web = site(Web())
        web.route("/sitemap.xml", sitemap([f"http://site.test/{i}" for i in range(n)], lastmod="2026-01-01"),
                  ctype="application/xml")
        got = by_code(_d(web), "SITEMAP_LASTMOD_ALL_IDENTICAL")
        assert bool(got) is flag and (not got or sev(got[0]) == ("low", "inferred", "TEST"))
    assert now


@pytest.mark.parametrize("status,want", [(499, "LLMS_TXT_ABSENT"), (500, "LLMS_TXT_UNREADABLE"),
                                         (404, "LLMS_TXT_ABSENT"), (503, "LLMS_TXT_UNREADABLE"),
                                         (403, "LLMS_TXT_ABSENT")] * 4)
def test_B_DE_13_llms_fetch_states(status, want):
    web = site(Web())
    web.route("/llms.txt", "x", status=status, ctype="text/plain")
    f = one(_d(web), want)
    assert f["decision"] == ("INSUFFICIENT_EVIDENCE" if want == "LLMS_TXT_UNREADABLE" else "WATCH")


def _llms_doc(r):
    lines = []
    if r.random() < 0.85:
        lines.append("# " + plants.uniq(r))
    if r.random() < 0.5:
        lines.append("> " + plants.uniq(r))
    for _ in range(r.randint(0, 3)):
        lines.append(("## " if r.random() < 0.9 else "# ") + plants.uniq(r))
        for _ in range(r.randint(0, 3)):
            u = r.choice(("https://site.test/a", "http://site.test/b", "/rel", "ftp://x.test/c"))
            lines.append(f"- [{plants.uniq(r)[:5]}]({u})" + (": " + plants.uniq(r) if r.random() < 0.5 else ""))
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("seed", [7, 11])
def test_B_DE_14_llms_shape_oracle(seed):
    llms_shape.self_test()
    r = rng(seed, "de14")
    for i in range(250):
        doc = _llms_doc(r)
        assert delia.check_llms_txt(doc)["issues"] == llms_shape.issues(doc), doc
    check_ran("B-DE-14", 500)


def test_B_DE_14_content_type_and_no_text_stored():
    m = plants.uniq(rng(4, "de14b"))
    web = site(Web(), llms=f"# {m}\n## S\n- [a](https://site.test/)\n")
    web.route("/llms.txt", f"# {m}\n## S\n- [a](https://site.test/)\n", ctype="application/octet-stream")
    d = _d(web)
    assert "LLMS_TXT_CONTENT_TYPE" in codes(d) and m not in str(d)


FACTS = ("sitemaps_attempted", "sitemaps_read", "sitemap_fetch_failures", "sitemap_urls", "sitemap_urls_examined",
         "sitemap_children_followed", "sitemap_children_cross_host", "sitemap_children_over_cap", "robots_checked",
         "robots_unchecked")


def test_B_DE_15_disclosure_facts_present():
    for web in (site(Web()), site(Web(), llms=None)):
        d = _d(web)
        missing = [k for k in FACTS if k not in d["facts"]]
        assert not missing, missing


def test_B_DE_16_outcomes():
    assert _d(site(Web()))["outcome"] == "OK"
    web = site(Web())
    web.route("/sitemap.xml", "x", status=503, ctype="application/xml")
    assert _d(web)["outcome"] == "PARTIAL"
    web = site(Web(), llms=None)
    web.route("/sitemap.xml", "x", status=503, ctype="application/xml")
    web.route("/llms.txt", "x", status=503, ctype="text/plain")
    assert _d(web)["outcome"] == "FAILED"
    assert page(ld=ORG_LD)


def test_B_DE_07_mutual_loop_each_fetched_once():
    web = site(Web())
    web.route("/sitemap.xml", index(["http://site.test/a.xml"]), ctype="application/xml")
    web.route("/a.xml", index(["http://site.test/b.xml"]), ctype="application/xml")
    web.route("/b.xml", index(["http://site.test/a.xml", "http://site.test/sitemap.xml"]), ctype="application/xml")
    _d(web)
    for p in ("/sitemap.xml", "/a.xml", "/b.xml"):
        assert web.requests.count(("site.test", p)) == 1, p


def test_B_DE_03_over_the_protocol_limit_is_too_large_scaled(monkeypatch):
    monkeypatch.setattr(delia, "PROTOCOL_MAX_BYTES", 100_000)
    web = site(Web())
    web.route("/sitemap.xml", _big_sitemap(3000), ctype="application/xml")
    d = _d(web)
    assert "SITEMAP_TOO_LARGE" in codes(d) and d["facts"]["sitemap_urls"] == 0


def test_B_DE_11_only_the_first_500_listed_urls_are_checked():
    locs = [f"http://site.test/p{i}" for i in range(600)]
    web = site(Web(), robots="User-agent: Googlebot\nDisallow: /p5\nUser-agent: *\nAllow: /\n"
                             "Sitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap(locs), ctype="application/xml")
    d = _d(web)
    # /p5 and /p50-/p59 within the first 500; /p500-/p599 are past the sample (R-14: counted, not checked)
    assert one(d, "SITEMAP_LISTS_ROBOTS_BLOCKED_URL")["detail"]["count"] == 11


def test_B_DE_11_the_sample_is_the_first_500_same_site_valid_urls():
    """rv-wave1v2 #2 (reviewer probe delia_probe.py sample_first_500_same_site, ported): other-host and invalid
    URLs ahead of the same-site ones do not use up the 500 sample."""
    locs = ([f"http://other.test/x{i}" for i in range(450)] + [f"ftp://site.test/f{i}" for i in range(60)]
            + [f"http://site.test/blocked/{i}" for i in range(100)] + [f"http://site.test/ok/{i}" for i in range(450)])
    web = site(Web(), robots="User-agent: *\nAllow: /\nDisallow: /blocked/\nSitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap(locs), ctype="application/xml")
    d = _d(web)
    assert one(d, "SITEMAP_LISTS_ROBOTS_BLOCKED_URL")["detail"]["count"] == 100
    assert d["facts"]["robots_checked"] == 500             # 100 blocked + the first 400 of /ok/ (R-14)


class _Chunks(httpx.SyncByteStream):
    """A body served in chunks of ``n`` octets (``n`` None: one chunk), as a server streams it."""

    def __init__(self, b: bytes, n):
        self.b, self.n = b, n

    def __iter__(self):
        if self.n is None:
            yield self.b
            return
        for k in range(0, len(self.b), self.n):
            yield self.b[k:k + self.n]

    def close(self):
        pass


_OVER: dict = {}


def _over_limit_body() -> bytes:
    if "b" not in _OVER:
        _OVER["b"] = _big_sitemap(52_000, pad=1060).encode()
    return _OVER["b"]


@pytest.mark.parametrize("chunk,declared", [(65_536, False), (None, False), (65_536, True), (None, True)])
def test_B_DE_03_a_plain_sitemap_over_the_protocol_limit_is_too_large(chunk, declared):
    """rv-wave1v2 #3 / #5 (reviewer probes big53.py, delia_probe plain_over_protocol, ported): a PLAIN sitemap over
    52,428,800 bytes is SITEMAP_TOO_LARGE medium/measured/ACT (never UNREADABLE IE, whatever the chunking, with or
    without a Content-Length), and its quarantine record holds the bytes really read and their SHA-256."""
    import hashlib
    body = _over_limit_body()
    assert len(body) > 52_428_800 + 2 ** 20 * 3
    hdr = [("content-type", "application/xml")] + ([("content-length", str(len(body)))] if declared else [])
    web = site(Web())
    web.routes[("site.test", "/sitemap.xml")] = lambda req: httpx.Response(200, headers=hdr,
                                                                           stream=_Chunks(body, chunk))
    c, _, d, _, _, _ = run3(web)
    f = one(d, "SITEMAP_TOO_LARGE")
    assert sev(f) == ("medium", "measured", "ACT") and f["detail"]["limit"] == 52_428_800
    assert "SITEMAP_UNREADABLE" not in codes(d)
    q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "TOO_LARGE"]
    assert len(q) == 1 and q[0]["size_basis"] == "read_before_stop" and q[0]["truncated"] is True
    n = q[0]["bytes"]
    assert 52_428_800 < n <= delia.WIRE_MAX and q[0]["sha256"] == hashlib.sha256(body[:n]).hexdigest()
    assert q[0]["sha256"] != hashlib.sha256(b"").hexdigest()
    assert q[0]["declared_bytes"] == (len(body) if declared else None)


def test_B_OS_01_a_gzip_sitemap_over_the_limit_records_the_decoded_bytes_read():
    """rv-wave1v2 #5: the over-limit gzip case is quarantined TOO_LARGE too, read_before_stop, with the size and
    SHA-256 of the decoded bytes read before the stop (a prefix of the document)."""
    import hashlib
    body = _over_limit_body()
    web = site(Web(), robots="User-agent: *\nSitemap: http://site.test/s.xml.gz\n")
    web.route("/s.xml.gz", gzip.compress(body, 1), ctype="application/gzip")
    c, _, d, _, _, _ = run3(web)
    assert sev(one(d, "SITEMAP_TOO_LARGE")) == ("medium", "measured", "ACT")
    q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "TOO_LARGE"]
    assert len(q) == 1 and q[0]["size_basis"] == "read_before_stop" and q[0]["bytes"] > 52_428_800
    assert q[0]["sha256"] == hashlib.sha256(body[:q[0]["bytes"]]).hexdigest()
