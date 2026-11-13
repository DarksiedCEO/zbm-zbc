"""AEGIS Wave-1 certification round 1 (Selene, Delia, Roman, Osei-lite): one test (or more) per finding the hostile
review raised at a4af277, and one per surviving mutant it listed, each failing on the code it reviewed. Runs on the
in-memory web of cert_web.py (the real Fetcher behind httpx.MockTransport; no socket)."""

from __future__ import annotations

import codecs
import gzip
import hashlib
import json
import logging
import random
import re
from datetime import timedelta

import pytest

from agents import bots, delia, drift, logs as logs_mod, osei, probes, roman, selene
from cert_web import ORG_LD, Web, ctx, index, marker, page, site, sitemap
from clock import FixedClock
from helpers import T0
from primitives import fetch as fetch_mod
from primitives import robots as robots_mod
from primitives import safe_url as su


def codes(env: dict) -> set:
    return {f["code"] for f in env["findings"]}


def by_code(env: dict, code: str) -> list:
    return [f for f in env["findings"] if f["code"] == code]


def run3(web: Web, paths=("/",), **kw):
    c = ctx(web, paths, **kw)
    s, pages = selene.run(c)
    d = delia.run(c) if c.robots is not None else None
    r = roman.run(c, pages)
    return c, s, d, r, pages


# ============================================================================================ robots (M-S1, S-S1)

def _regex_match_len(pattern: str, path: str):
    """The a4af277 matcher (regex per rule), kept here as the oracle for the linear matcher's answers."""
    anchored = pattern.endswith("$")
    body = robots_mod._norm(pattern[:-1] if anchored else pattern)
    rx = "".join(".*" if c == "*" else re.escape(c) for c in body)
    return len(pattern) if re.match(rx + ("$" if anchored else ""), path) else None


def _oracle_allowed(rules, path: str) -> bool:
    best = None
    p = robots_mod._norm(path or "/")
    if p == "/robots.txt":
        return True
    for allow, pattern in rules:
        if pattern == "" and not allow:
            continue
        n = _regex_match_len(pattern, p)
        if n is None:
            continue
        if best is None or n > best[0] or (n == best[0] and allow and not best[1]):
            best = (n, allow)
    return True if best is None else best[1]


def test_linear_matcher_agrees_with_the_regex_oracle_on_random_rules():
    rnd = random.Random(20261113)
    alphabet = "ab/.-*$?=%"
    disagreements = 0
    for _ in range(3000):
        rules = []
        for _ in range(rnd.randint(1, 6)):
            pat = "/" + "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 8)))
            pat = pat.replace("$", "") + ("$" if rnd.random() < 0.3 else "")
            rules.append((rnd.random() < 0.5, pat))
        text = "User-agent: *\n" + "".join(("Allow: " if a else "Disallow: ") + p + "\n" for a, p in rules)
        rb = robots_mod.parse(text)
        path = "/" + "".join(rnd.choice("ab/.-?=") for _ in range(rnd.randint(0, 12)))
        if rb.allowed("Googlebot", path) != _oracle_allowed(rules, path):
            disagreements += 1
    assert disagreements == 0


@pytest.mark.parametrize("pattern,path,expect", [
    ("/a*b$", "/aXb", False), ("/a*b$", "/aXbY", True), ("/*.php$", "/x.php", False), ("/*.php$", "/x.php?q", True),
    ("/fish*", "/fish.html", False), ("/fish*", "/Fish", True), ("/*?", "/a?b", False), ("/$", "/", False),
    ("/$", "/a", True), ("*", "/anything", False), ("/a**b", "/a-x-b", False), ("/a*$", "/a", False),
    ("/a*a$", "/a", True), ("/ab*b$", "/ab", True), ("/ab*b$", "/abb", False),     # the anchored tail cannot overlap
])
def test_rfc9309_wildcards(pattern, path, expect):
    rb = robots_mod.parse(f"User-agent: *\nDisallow: {pattern}\n")
    assert rb.allowed("Googlebot", path) is expect


def test_longest_match_and_allow_wins_ties():
    rb = robots_mod.parse("User-agent: *\nDisallow: /p\nAllow: /p/ok\nAllow: /q\nDisallow: /q\n")
    assert rb.allowed("x", "/p/ok/1") is True and rb.allowed("x", "/p/no") is False
    assert rb.allowed("x", "/q") is True                         # equal length: allow


def test_hostile_wildcards_are_linear_not_backtracking(monkeypatch):
    # the reviewer's case: 6 wildcards x 60-character path took 3.0 s per URL at a4af277 (a regex per rule). The
    # matcher uses no regular expression, and its work is counted, not timed: one str.find per piece at most.
    assert not hasattr(robots_mod, "re")
    calls = {"find": 0}
    real = robots_mod._match

    def counting(rule, path):
        calls["find"] += len(rule.pieces)
        return real(rule, path)
    monkeypatch.setattr(robots_mod, "_match", counting)
    rb = robots_mod.parse("User-agent: *\nDisallow: /*a*a*a*a*a*a*b\n")
    for i in range(200):
        assert rb.allowed("Googlebot", "/" + "a" * 60 + str(i)) is True
    assert calls["find"] == 200 * 8                 # 8 pieces, one search each, per URL
    text = "User-agent: *\n" + "".join(f"Disallow: /*a*a*a*a*a*a*a*a*a*b{i}\n" for i in range(4000))
    rb = robots_mod.parse(text)
    calls["find"] = 0
    rb.decide("Googlebot", "/" + "a" * 4000)
    assert calls["find"] <= 4000 * (robots_mod.MAX_WILDCARDS + 1)


def test_bounds_fail_closed_and_are_counted():
    over = "/x" + "y" * robots_mod.MAX_PATTERN
    rb = robots_mod.parse(f"User-agent: *\nDisallow: {over}$\nAllow: {over}z\n")
    lim = rb.limits()
    assert lim["patterns_shortened"] == 1 and lim["rules_not_used"] == 1 and rb.limits_exceeded()
    # the shortened disallow matches at least what it matched (it was anchored; now a prefix)
    assert rb.allowed("Googlebot", over) is False and rb.allowed("Googlebot", over + "more") is False
    d = rb.decide("Googlebot", "/elsewhere")
    assert d["allowed"] is True and d["complete"] is False      # an allow over a bound was not used
    wild = "/" + "*a" * (robots_mod.MAX_WILDCARDS + 3)
    rb = robots_mod.parse(f"User-agent: *\nDisallow: {wild}\n")
    assert rb.limits()["patterns_shortened"] == 1 and rb.allowed("x", "/" + "a" * 30) is False
    many = "User-agent: *\n" + "".join(f"Disallow: /r{i}\n" for i in range(robots_mod.MAX_RULES_PER_GROUP + 5))
    rb = robots_mod.parse(many)
    assert rb.limits()["rules_not_used"] == 5 and rb.decide("x", "/ok")["complete"] is False
    dup = robots_mod.parse("User-agent: *\n" + "Disallow: /x\n" * 40_000)
    assert not dup.limits_exceeded() and len(dup.groups[0].rules) == 1 and dup.allowed("x", "/x") is False


def test_budget_spent_leaves_decisions_undecided():
    rb = robots_mod.parse("User-agent: *\nDisallow: /a\n")
    b = robots_mod.Budget(0)
    d = rb.decide("x", "/a", b)
    assert d["allowed"] is None and d["undecided"] == "budget" and b.undecided == 1
    assert rb.decide("x", "/" + "p" * (robots_mod.MAX_PATH + 1))["allowed"] is None


def test_bom_before_the_first_group_is_stripped():
    rb = robots_mod.parse("\ufeffUser-agent: *\nDisallow: /\n")
    assert rb.bom_stripped and rb.allowed("Googlebot", "/page") is False
    web = site(Web(), robots="\ufeffUser-agent: *\nDisallow: /\n")
    _, s, _, _, pages = run3(web)
    assert "ROBOTS_BLOCKS_SEARCH_CRAWLER" in codes(s) and pages["/"]["fetch"].state == "BLOCKED_BY_ROBOTS"


def test_selene_matrix_budget_degrades_to_partial(monkeypatch):
    monkeypatch.setattr(selene, "MATRIX_BUDGET_S", 0.0)
    web = site(Web())
    _, s, _, _, _ = run3(web, ["/", "/about"])
    assert s["outcome"] == "PARTIAL" and "ROBOTS_CHECK_INCOMPLETE" in codes(s)
    assert s["facts"]["robots_matrix_undecided"] > 0


def test_fetcher_refuses_when_its_robots_check_cannot_decide(monkeypatch):
    monkeypatch.setattr(fetch_mod, "ROBOTS_CHECK_BUDGET_S", 0.0)
    web = site(Web())
    f = web.fetcher().fetch("http://site.test/about", robots_cache={})
    assert f.state == "BLOCKED_BY_ROBOTS" and "bounds" in f.detail


def test_fetcher_does_not_trust_an_allow_from_an_incomplete_rule_set():
    many = "User-agent: *\n" + "".join(f"Disallow: /r{i}\n" for i in range(robots_mod.MAX_RULES_PER_GROUP + 1))
    web = site(Web(), robots=many)
    c, s, _, _, pages = run3(web)
    assert pages["/"]["fetch"].state == "BLOCKED_BY_ROBOTS" and "ROBOTS_LIMITS_EXCEEDED" in codes(s)


def test_hostile_robots_audit_finishes_within_bound():
    text = "User-agent: *\n" + "".join(f"Disallow: /*a*a*a*a*a*a*a*a*a*q{i}\n" for i in range(3000))
    web = site(Web(), robots=text)
    _, s, d, _, _ = run3(web, ["/", "/about", "/" + "a" * 400])
    assert s["outcome"] in ("OK", "PARTIAL") and d["outcome"] in ("OK", "PARTIAL")


# ============================================================================================ Selene mutants (S-S3)

def test_search_crawler_block_severity_critical_on_root_high_elsewhere():
    web = site(Web(), robots="User-agent: Googlebot\nDisallow: /\n\nUser-agent: Bingbot\nDisallow: /about\n\n"
                             "User-agent: *\nAllow: /\n")
    _, s, _, _, _ = run3(web, ["/", "/about"])
    sev = {f["detail"]["token"]: f["severity"] for f in by_code(s, "ROBOTS_BLOCKS_SEARCH_CRAWLER")}
    assert sev == {"Googlebot": "critical", "Bingbot": "high"}


def test_canonical_other_host_only_for_another_site():
    web = site(Web())
    web.route("/", page(canonical="http://other.test/x"))
    web.route("/about", page(canonical="http://www.site.test/about"))
    _, s, _, _, _ = run3(web, ["/", "/about"])
    hits = by_code(s, "CANONICAL_OTHER_HOST")
    assert [f["url"] for f in hits] == ["http://site.test/"]


def test_redirect_severity_single_vs_chain():
    web = site(Web())
    web.route("/one", "", status=301, ctype="text/plain", location="/about")
    web.route("/two", "", status=301, ctype="text/plain", location="/one")
    _, s, _, _, _ = run3(web, ["/one", "/two"])
    one = by_code(s, "REDIRECTED")[0]
    two = by_code(s, "REDIRECT_CHAIN")[0]
    assert (one["severity"], one["decision"]) == ("low", "WATCH") and (two["severity"], two["decision"]) == \
        ("medium", "ACT")


@pytest.mark.parametrize("status,severity", [(400, "medium"), (404, "medium"), (500, "high"), (503, "high")])
def test_http_error_threshold_and_severity(status, severity):
    web = site(Web())
    web.route("/e", page(), status=status)
    _, s, _, _, _ = run3(web, ["/e"])
    f = by_code(s, "PAGE_HTTP_ERROR")
    assert len(f) == 1 and f[0]["severity"] == severity


def test_canonical_target_check_cap():
    web = site(Web())
    paths = [f"/p{i}" for i in range(selene.CANONICAL_CHECK_MAX + 2)]
    for p in paths:
        web.route(p, page(canonical=f"http://site.test{p}-canon"))
    run3(web, paths)
    fetched = [p for h, p in web.requests if p.endswith("-canon")]
    assert len(fetched) == selene.CANONICAL_CHECK_MAX


def test_selene_ok_vs_partial():
    web = site(Web())
    _, s, _, _, _ = run3(web, ["/", "/about"])
    assert s["outcome"] == "OK"
    web.routes[("site.test", "/about")] = lambda req: (_ for _ in ()).throw(fetch_mod.httpx.ConnectError("down"))
    _, s, _, _, _ = run3(web, ["/", "/about"])
    assert s["outcome"] == "PARTIAL"


def test_robots_too_large_is_robots_unreachable_with_quarantine_size():
    web = site(Web(), robots="User-agent: *\nAllow: /\n" + "#" * (fetch_mod.ROBOTS_MAX_BYTES + 10))
    c, s, _, _, _ = run3(web, max_bytes=fetch_mod.ROBOTS_MAX_BYTES)
    f = by_code(s, "ROBOTS_UNREACHABLE")
    assert f and f[0]["severity"] == "critical" and s["facts"]["robots"]["status_class"] == "too_large"
    q = [x for x in c.osei.quarantined if x["kind"] == "robots"][0]
    assert q["reason"] == "TOO_LARGE" and q["bytes"] is not None and q["sha256"] and "size_basis" in q


# ============================================================================================ noindex per engine (S-S2)

@pytest.mark.parametrize("header,meta,engines", [
    ("otherbot: noindex", None, []),
    ("googlebot: noindex", None, ["google"]),
    ("noindex", None, ["google", "bing", "openai", "anthropic", "perplexity"]),
    ("bingbot: none", None, ["bing"]),
    (None, ("bingbot", "noindex"), ["bing"]),
    (None, ("robots", "noindex, nofollow"), ["google", "bing", "openai", "anthropic", "perplexity"]),
    (None, ("gptbot", "noindex"), []),                   # GPTBot is not the OpenAI search crawler
    (None, ("oai-searchbot", "noindex"), ["openai"]),
])
def test_noindex_is_per_engine(header, meta, engines):
    web = site(Web())
    head = f"<meta name='{meta[0]}' content='{meta[1]}'>" if meta else ""
    extra = {"x_robots_tag": header} if header else {}
    web.route("/", page(head=head, ld=ORG_LD), **extra)
    _, s, _, r, _ = run3(web)
    hits = by_code(s, "NOINDEX")
    assert (hits[0]["detail"]["engines"] if hits else []) == engines
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    assert sorted(e for e, v in rd.items() if v["checks"]["indexable"] == "fail") == sorted(engines)


# ============================================================================================ URL rule (M-S2 ... M-O3)

def test_safe_url_rule():
    assert su.safe_url("HTTPS://User:Pw@Site.Test:443/blog/how-to-choose-running-shoes?q=1#f") == \
        "https://site.test/blog/how-to-choose-running-shoes"
    assert su.safe_url("http://site.test:8080/a") == "http://site.test:8080/a"
    assert su.safe_url("http://b\u00fccher.test/x") == "http://xn--bcher-kva.test/x"
    assert su.safe_url("javascript:alert(1)") == "javascript:"
    assert su.safe_url(None) is None and su.safe_url("::not a url") == "{unreadable}"
    m = marker()
    for raw in (f"/reset/{m}{m}", f"/u/{m}@example.com", f"/x;sid={m}", f"/a/b,{m}", f"/k={m}", f"/s/{m}9a"):
        assert m not in su.safe_url("http://site.test" + raw), raw
    assert su.query_present("http://x.test/a?b=1") and not su.query_present("http://x.test/a")


@pytest.mark.parametrize("path,template", [
    ("/x;jsessionid=Ab", "/x;{param}"), ("/a;b", "/a;{param}"), ("/tags/red,blue", "/tags/{param}"),
    ("/k=v", "/{param}"), ("/shop;u=jane@example.com", "/{email}"), ("/blog/top-10-tips", "/blog/top-10-tips"),
    ("/news/best-shoes-2024", "/news/best-shoes-2024"), ("/item-12345-blue", "/{token}"),
    ("/blog/how-to-choose-running-shoes-for-beginners", "/blog/how-to-choose-running-shoes-for-beginners"),
])
def test_template_rules_m_s5(path, template):
    assert logs_mod.template_path(path) == template


def test_crawled_urls_reach_no_report_field_raw():
    m = marker()
    web = Web()
    web.route("/robots.txt", f"User-agent: *\nAllow: /\nSitemap: http://site.test/sm-{m}.xml?k={m}\n"
                             f"Sitemap: http://other.test/{m}/s.xml\n", ctype="text/plain")
    web.route(f"/sm-{m}.xml", index([f"http://site.test/c-{m}.xml", f"http://other.test/{m}.xml"]),
              ctype="application/xml")
    web.route(f"/c-{m}.xml", sitemap([f"http://site.test/p/{m}{m}", f"http://other.test/{m}",
                                      f"notaurl-{m}"], lastmod=f"bad-{m}"), ctype="application/xml")
    web.route("/llms.txt", f"# {m}\n## s\n- [a](http://site.test/{m})\n", ctype="text/plain")
    web.route("/", page(canonical=f"http://other.test/c?{m}", head=f"<meta name='robots' content='{m}'>"),
              x_robots_tag=f"{m}: noindex, {m}")
    web.route("/r", "", status=302, ctype="text/plain", location=f"/dest/{m}{m}?t={m}")
    web.route(f"/dest/{m}{m}", page(title=f"T {m}", lang=f"{m}", ld=json.dumps(
        {"@context": "https://schema.org", "@type": f"{m}", "name": m})))
    c, s, d, r, pages = run3(web, ["/", "/r"], path_key=b"k" * 32)
    blob = json.dumps([s, d, r, {p: v["fetch"].summary() for p, v in pages.items()}, c.osei.summary()])
    assert m not in blob
    assert d["facts"]["sitemap_children_cross_host"] == 1 and d["facts"]["sitemaps_cross_host_count"] == 1


def test_fetch_summary_and_xrobots_safe_forms():
    assert su.safe_x_robots("googlebot: noindex, max-snippet:20, weird") == \
        "googlebot: noindex, googlebot: max-snippet:20, googlebot: {other}"
    assert su.safe_x_robots("unknownbot: noindex") == "{bot}: noindex"
    assert su.safe_ctype("text/html") == "text/html" and su.safe_ctype("text/html x") == "text/{other}"
    assert su.safe_ctype("weird") == "{other}"


def test_httpx_loggers_stay_at_warning():
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ============================================================================================ Delia (M-D1 ...)

LAUGHS = ("<?xml version='1.0'?><!DOCTYPE lolz [<!ENTITY lol 'lol'>"
          + "".join(f"<!ENTITY lol{i} '{('&lol' + (str(i - 1) if i > 1 else '') + ';') * 10}'>" for i in range(1, 9))
          + "]><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>&lol8;</loc></url></urlset>")


@pytest.mark.parametrize("enc,bom", [("utf-16-le", True), ("utf-16-be", True), ("utf-16-le", False),
                                     ("utf-16-be", False), ("utf-32-le", True), ("utf-32-be", False),
                                     ("utf-8", True), ("utf-8", False)])
def test_entity_quarantine_in_every_encoding(enc, bom):
    doc = LAUGHS.replace("<?xml version='1.0'?>", f"<?xml version='1.0' encoding='{enc.split('-')[0]}"
                         f"{'-' + enc.split('-')[1] if enc != 'utf-8' else '-8'}'?>")
    data = doc.encode(enc)
    if bom:
        data = {"utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE, "utf-32-le": codecs.BOM_UTF32_LE,
                "utf-32-be": codecs.BOM_UTF32_BE, "utf-8": codecs.BOM_UTF8}[enc] + data
    web = site(Web())
    web.route("/sitemap.xml", data, ctype="application/xml")
    c, _, d, _, _ = run3(web)
    q = [x for x in c.osei.quarantined if x["kind"] == "sitemap"]
    assert [x["reason"] for x in q] == ["XML_DOCTYPE_REFUSED"] and d["facts"]["sitemap_urls"] == 0
    assert q[0]["bytes"] == len(data) and q[0]["sha256"] == hashlib.sha256(data).hexdigest()


def test_expat_refuses_a_dtd_even_when_the_text_check_misses_it(monkeypatch):
    monkeypatch.setattr(delia, "_declares_dtd", lambda body: False)
    web = site(Web())
    web.route("/sitemap.xml", LAUGHS.encode("utf-16"), ctype="application/xml")
    c, _, d, _, _ = run3(web)
    assert [x["reason"] for x in c.osei.quarantined] == ["XML_DOCTYPE_REFUSED"] and d["facts"]["sitemap_urls"] == 0


def test_doctype_past_the_first_4096_bytes_is_refused():
    body = "<?xml version='1.0'?>" + "<!-- " + "x" * 5000 + " -->" + "<!DOCTYPE x []><urlset/>"
    web = site(Web())
    web.route("/sitemap.xml", body, ctype="application/xml")
    c, _, _, _, _ = run3(web)
    assert [x["reason"] for x in c.osei.quarantined] == ["XML_DOCTYPE_REFUSED"]


def test_xml_encoding_detection():
    assert delia.xml_encoding(codecs.BOM_UTF16_LE + b"<") == "utf-16-le"
    assert delia.xml_encoding("<?xml".encode("utf-16-be")) == "utf-16-be"
    assert delia.xml_encoding("<?xml".encode("utf-32-le")) == "utf-32-le"
    assert delia.xml_encoding(b"<?xml version='1.0' encoding='ISO-8859-1'?><a/>") == "iso8859-1"
    assert delia.xml_encoding(b"<a/>") == "utf-8"


def test_ordinary_utf16_sitemap_still_reads():
    web = site(Web())
    web.route("/sitemap.xml", sitemap(["http://site.test/", "http://site.test/about"]).replace(
        "encoding='UTF-8'", "encoding='UTF-16'").encode("utf-16"), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert d["facts"]["sitemap_urls"] == 2 and d["outcome"] == "OK"


def test_index_children_cap_cross_host_and_loop_guard():
    web = site(Web())
    kids = [f"http://site.test/s{i}.xml" for i in range(delia.MAX_CHILDREN + 2)] + ["http://other.test/x.xml",
                                                                                   "http://site.test/sitemap.xml"]
    web.route("/sitemap.xml", index(kids), ctype="application/xml")
    for i in range(delia.MAX_CHILDREN + 2):
        web.route(f"/s{i}.xml", sitemap([f"http://site.test/c{i}"]), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    f = d["facts"]
    assert f["sitemap_children_followed"] == delia.MAX_CHILDREN and f["sitemap_children_over_cap"] == 3
    assert f["sitemap_children_cross_host"] == 1 and f["sitemap_urls"] == delia.MAX_CHILDREN
    assert ("other.test", "/x.xml") not in web.requests
    assert web.requests.count(("site.test", "/sitemap.xml")) == 1      # the loop guard


def test_declared_sitemaps_same_site_only():
    web = site(Web(), robots="User-agent: *\nAllow: /\nSitemap: http://other.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap(["http://other.test/a"]), host="other.test", ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert ("other.test", "/sitemap.xml") not in web.requests and d["facts"]["sitemaps_cross_host_count"] == 1
    assert ("site.test", "/sitemap.xml") in web.requests


@pytest.mark.parametrize("hours,future", [(23, False), (25, True)])
def test_future_lastmod_window(hours, future):
    ts = (T0 + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    web = site(Web())
    web.route("/sitemap.xml", sitemap(["http://site.test/"], lastmod=ts), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert ("SITEMAP_LASTMOD_FUTURE" in codes(d)) is future


@pytest.mark.parametrize("n,same,flag", [(10, True, True), (9, True, False), (10, False, False)])
def test_identical_lastmod_rule(n, same, flag):
    web = site(Web())
    locs = [f"http://site.test/p{i}" for i in range(n)]
    body = sitemap(locs)
    if not same:
        body = body.replace("<lastmod>2026-10-01</lastmod>", "<lastmod>2026-10-02</lastmod>", 1)
    web.route("/sitemap.xml", body, ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert ("SITEMAP_LASTMOD_ALL_IDENTICAL" in codes(d)) is flag


def test_fullwidth_digits_are_not_a_w3c_date():
    web = site(Web())
    web.route("/sitemap.xml", sitemap(["http://site.test/"], lastmod="\uff12\uff10\uff12\uff16-10-01"),
              ctype="application/xml")
    _, _, d, _, _ = run3(web)
    f = by_code(d, "SITEMAP_LASTMOD_INVALID")
    assert len(f) == 1 and f[0]["detail"]["examples_shape"][0].endswith("-99-99")


@pytest.mark.parametrize("n,flag", [(delia.MAX_URLS_PER_FILE, False), (delia.MAX_URLS_PER_FILE + 1, True)])
def test_urls_per_file_limit(n, flag):
    web = site(Web())
    body = "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>" + \
        "<url><loc>http://site.test/a</loc></url>" * n + "</urlset>"
    web.route("/sitemap.xml", gzip.compress(body.encode()), ctype="application/gzip")
    _, _, d, _, _ = run3(web)
    assert ("SITEMAP_TOO_MANY_URLS" in codes(d)) is flag


def test_namespace_check():
    web = site(Web())
    web.route("/sitemap.xml", sitemap(["http://site.test/"]).replace("sitemaps.org/schemas", "example.test/s"),
              ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert "SITEMAP_NAMESPACE" in codes(d)
    web.route("/sitemap.xml", sitemap(["http://site.test/"]), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert "SITEMAP_NAMESPACE" not in codes(d)


@pytest.mark.parametrize("robots,flag", [("User-agent: Googlebot\nDisallow: /p\n\nUser-agent: *\nAllow: /\n", True),
                                         ("User-agent: Bingbot\nDisallow: /p\n\nUser-agent: *\nAllow: /\n", False)])
def test_listed_url_robots_check_is_for_googlebot(robots, flag):
    web = site(Web(), robots=robots)
    web.route("/sitemap.xml", sitemap(["http://site.test/p1"]), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert ("SITEMAP_LISTS_ROBOTS_BLOCKED_URL" in codes(d)) is flag


def test_listed_url_robots_check_budget(monkeypatch):
    monkeypatch.setattr(delia, "URL_CHECK_BUDGET_S", 0.0)
    web = site(Web())
    _, _, d, _, _ = run3(web)
    # "/" was decided by Selene's matrix already (kept, free); "/about" is left undecided
    assert d["outcome"] == "PARTIAL" and d["facts"]["robots_unchecked"] == 1
    assert "SITEMAP_ROBOTS_CHECK_INCOMPLETE" in codes(d)


@pytest.mark.parametrize("ctype,flag", [("text/plain", False), ("text/markdown", False), ("text/html", True)])
def test_llms_content_type(ctype, flag):
    web = site(Web())
    web.route("/llms.txt", "# T\n## S\n- [a](http://site.test/)\n", ctype=ctype)
    _, _, d, _, _ = run3(web)
    assert ("LLMS_TXT_CONTENT_TYPE" in codes(d)) is flag


def test_decompression_bound(monkeypatch):
    monkeypatch.setattr(delia, "DECOMPRESSED_MAX", 1000)
    web = site(Web())
    web.route("/sitemap.xml", gzip.compress(sitemap(["http://site.test/"]).encode() + b" " * 1001),
              ctype="application/gzip")
    c, _, d, _, _ = run3(web)
    assert "SITEMAP_DECOMPRESS_FAILED" in codes(d) and c.osei.quarantined[0]["reason"] == "DECOMPRESS_FAILED"
    web.route("/sitemap.xml", gzip.compress(sitemap(["http://site.test/"]).encode()), ctype="application/gzip")
    _, _, d, _, _ = run3(web)
    assert "SITEMAP_DECOMPRESS_FAILED" not in codes(d) and d["facts"]["sitemap_urls"] == 1


@pytest.mark.parametrize("status,code,decision", [(404, "SITEMAP_MISSING", "ACT"), (410, "SITEMAP_MISSING", "ACT"),
                                                  (503, "SITEMAP_UNREADABLE", "INSUFFICIENT_EVIDENCE"),
                                                  (403, "SITEMAP_UNREADABLE", "INSUFFICIENT_EVIDENCE")])
def test_sitemap_unreadable_vs_missing(status, code, decision):
    web = site(Web())
    web.route("/sitemap.xml", "x", status=status, ctype="text/plain")
    _, _, d, _, _ = run3(web)
    f = by_code(d, code)
    assert f and f[0]["decision"] == decision and d["outcome"] == "PARTIAL"


def test_llms_5xx_is_unreadable_and_h2_first_is_one_issue():
    web = site(Web())
    web.route("/llms.txt", "down", status=503, ctype="text/plain")
    _, _, d, _, _ = run3(web)
    assert "LLMS_TXT_UNREADABLE" in codes(d) and d["facts"]["llms_txt"]["state"] == "unreadable"
    assert delia.check_llms_txt("## Pages\n- [a](http://site.test/)\n")["issues"] == ["NO_H1_TITLE"]


class _Chunks(fetch_mod.httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)


def test_sitemap_too_large_quarantine_records_bytes_read():
    web = site(Web())
    chunks = [b"<urlset>" + b" " * 8192] * 40                    # streamed, no Content-Length
    web.routes[("site.test", "/sitemap.xml")] = lambda req: fetch_mod.httpx.Response(
        200, headers={"content-type": "application/xml"}, stream=_Chunks(chunks))
    c, _, d, _, _ = run3(web)
    q = [x for x in c.osei.quarantined if x["kind"] == "sitemap"][0]
    got = b"".join(chunks)[:q["bytes"]]
    assert q["reason"] == "TOO_LARGE" and 0 < q["bytes"] <= 256 * 1024
    assert q["sha256"] == hashlib.sha256(got).hexdigest() and "before the fetch was stopped" in q["size_basis"]
    web.route("/sitemap.xml", b"x" * (300 * 1024), ctype="application/xml", content_length=str(300 * 1024))
    c, _, d, _, _ = run3(web)
    q = [x for x in c.osei.quarantined if x["kind"] == "sitemap"][0]
    assert q["bytes"] == 0 and q["declared_bytes"] == 300 * 1024 and q["sha256"] == hashlib.sha256(b"").hexdigest()


def test_sample_digests_match_the_log_task():
    key = b"k" * 32
    web = site(Web())
    _, _, d, _, _ = run3(web, path_key=key)
    dg = d["facts"]["sitemap_paths_sample_digests"]
    assert d["facts"]["sitemap_paths_sample"] == ["/", "/about"]
    assert dg["digests"] == [logs_mod.path_digest(key, "/"), logs_mod.path_digest(key, "/about")]


# ============================================================================================ Roman (M-R1, S-R1)

def _roman(web, paths=("/",)):
    _, _, _, r, _ = run3(web, paths)
    return r


def test_title_text_is_withheld_and_measured():
    m = marker()
    web = site(Web())
    web.route("/", page(title=f"Z Best Media {m}", h1=f"Z Best Media {m}", ld=ORG_LD))
    r = _roman(web)
    p = r["facts"]["pages"]["/"]
    assert m not in json.dumps(r) and p["title_withheld"] is True and p["title_same_as_h1"] is True
    assert p["title_length"] == len(f"Z Best Media {m}") and p["title_empty"] is False


@pytest.mark.parametrize("lang,stored", [("en", "en"), ("en-us", "en-US"), ("zh-hant-tw", "zh-Hant-TW"),
                                         ("de-CH-1996", "de-CH"), ("en-US-x-private", "en-US"),
                                         ("not a tag", "invalid"), ("e", "invalid")])
def test_lang_is_a_bcp47_tag_or_invalid(lang, stored):
    assert roman.safe_lang(lang) == stored


def test_unknown_types_are_counted_not_stored():
    m = marker()
    ld = json.dumps([{"@context": "https://schema.org", "@type": "Organization", "name": "Z", "sameAs": ["x"]},
                     {"@context": "https://schema.org", "@type": [m, "WebSite"], "name": "Z", "url": "u"}])
    web = site(Web())
    web.route("/", page(ld=ld))
    r = _roman(web)
    p = r["facts"]["pages"]["/"]
    assert p["schema_types"] == ["Organization", "WebSite"] and p["unknown_type"] == 1 and m not in json.dumps(r)


def test_jsonld_invalid_quarantine_has_size_and_sha():
    block = "{not json " + marker()
    web = site(Web())
    web.route("/", page(ld=block))
    c, _, _, r, _ = run3(web)
    q = [x for x in c.osei.quarantined if x["reason"] == "JSONLD_INVALID"][0]
    assert q["bytes"] == len(block.encode()) and q["sha256"] == hashlib.sha256(block.encode()).hexdigest()


@pytest.mark.parametrize("n,flag", [(roman.TITLE_MAX, False), (roman.TITLE_MAX + 1, True),
                                    (roman.TITLE_MIN, False), (roman.TITLE_MIN - 1, True)])
def test_title_length_bounds(n, flag):
    web = site(Web())
    web.route("/", page(title="t" * n, ld=ORG_LD))
    assert ("TITLE_LENGTH" in codes(_roman(web))) is flag


@pytest.mark.parametrize("chars,flag", [(roman.RAW_TEXT_MIN - 3, True), (roman.RAW_TEXT_MIN - 2, False)])
def test_raw_text_min(chars, flag):
    # visible text = the title "t", a space, then the body: RAW_TEXT_MIN - 1 and RAW_TEXT_MIN characters
    web = site(Web())
    web.route("/", page(title="t", body="x" * chars, ld=ORG_LD, h1=None).replace("<h2>What we do</h2>", ""))
    r = _roman(web)
    assert ("LITTLE_TEXT_IN_RAW_HTML" in codes(r)) is flag
    assert r["facts"]["engine_readiness_heuristic"]["/"]["google"]["checks"]["text_in_raw_html"] == \
        ("fail" if flag else "pass")


def test_h1_count_zero_and_two():
    web = site(Web())
    web.route("/", page(h1=None, ld=ORG_LD))
    assert by_code(_roman(web), "H1_COUNT")[0]["detail"]["h1_count"] == 0
    web.route("/", page(h1="a</h1><h1>b", ld=ORG_LD))
    assert by_code(_roman(web), "H1_COUNT")[0]["detail"]["h1_count"] == 2
    web.route("/", page(ld=ORG_LD))
    assert "H1_COUNT" not in codes(_roman(web))


def test_heading_skip_rule():
    web = site(Web())
    web.route("/", page(ld=ORG_LD, body="<h4>deep</h4>" + "<p>" + "t " * 300 + "</p>"))
    assert by_code(_roman(web), "HEADING_LEVEL_SKIP")[0]["detail"]["skips"] == [[2, 4]]
    web.route("/", page(ld=ORG_LD))
    assert "HEADING_LEVEL_SKIP" not in codes(_roman(web))


def test_sameas_rule():
    web = site(Web())
    web.route("/", page(ld=ORG_LD.replace(', "sameAs": ["https://example.org/zbm"]', "")))
    assert "ENTITY_SAMEAS_MISSING" in codes(_roman(web))
    web.route("/", page(ld=ORG_LD))
    assert "ENTITY_SAMEAS_MISSING" not in codes(_roman(web))


def test_structured_data_absent_severity_and_entity_markup_absent():
    web = site(Web())
    web.route("/", page(ld=None))
    web.route("/about", page(ld=None, canonical="http://site.test/about"))
    r = _roman(web, ["/", "/about"])
    sev = {f["url"]: f["severity"] for f in by_code(r, "STRUCTURED_DATA_ABSENT")}
    assert sev == {"http://site.test/": "medium", "http://site.test/about": "low"}
    assert "ENTITY_MARKUP_ABSENT" in codes(r)
    web.route("/about", page(ld=ORG_LD, canonical="http://site.test/about"))
    assert "ENTITY_MARKUP_ABSENT" not in codes(_roman(web, ["/", "/about"]))


def test_roman_ok_vs_partial():
    web = site(Web())
    assert _roman(web, ["/", "/about"])["outcome"] == "OK"
    web.route("/about", "plain", ctype="text/plain")
    assert _roman(web, ["/", "/about"])["outcome"] == "PARTIAL"


# ============================================================================================ Osei (M-O3, M-O4, S-O1)

def test_osei_caps_are_counted():
    o = osei.Osei(FixedClock(T0))
    for i in range(osei.QUARANTINED_MAX + 5):
        o.quarantine("page", "TOO_LARGE", f"http://site.test/{i}", b"x")
    for i in range(osei.OBSERVATIONS_MAX + 7):
        o.observed("page", f"http://site.test/{i}", "OK")
    s = o.summary()
    assert (len(s["quarantined"]), s["quarantined_dropped"]) == (osei.QUARANTINED_MAX, 5)
    assert (len(s["observations"]), s["observations_dropped"]) == (osei.OBSERVATIONS_MAX, 7)


@pytest.mark.parametrize("kind,days", [("robots", 1), ("sitemap", 1), ("llms_txt", 7), ("page", 7),
                                       ("entity_record", 180), ("page_canonical_target", 7)])
def test_refresh_tiers(kind, days):
    o = osei.Osei(FixedClock(T0))
    rec = o.observed(kind, "http://site.test/", "OK")
    assert rec["refresh_due"] == (T0 + timedelta(days=days)).isoformat().replace("+00:00", "Z") or \
        rec["refresh_due"].startswith((T0 + timedelta(days=days)).date().isoformat())


def test_osei_records_keep_only_safe_urls():
    m = marker()
    o = osei.Osei(FixedClock(T0))
    o.quarantine("sitemap", "XML_MALFORMED", f"http://site.test/{m}{m}?{m}", b"<")
    o.observed("sitemap", f"http://u:{m}@site.test/{m}{m}#{m}", "OK")
    assert m not in json.dumps(o.summary())


def test_every_fetch_gets_an_observation():
    web = site(Web())
    web.route("/", page(canonical="http://site.test/canon", ld=ORG_LD))
    c, _, _, _, _ = run3(web)
    kinds = {o["kind"] for o in c.osei.observations}
    assert {"page_canonical_target", "page_browser_ua", "page", "robots", "sitemap", "llms_txt"} <= kinds


def test_provider_protocol_error_quarantine_has_size():
    from perplexity import EngineError, _with_body
    o = osei.Osei(FixedClock(T0))
    body = b'{"not": "the shape"}'
    probes._quarantine_error(o, _with_body(EngineError("ENGINE_PROTOCOL_ERROR"), body))
    probes._quarantine_malformed(o, object())
    q = o.summary()["quarantined"]
    assert q[0]["bytes"] == len(body) and q[0]["sha256"] == hashlib.sha256(body).hexdigest()
    assert isinstance(q[1]["bytes"], int) and len(q[1]["sha256"]) == 64


# ============================================================================================ drift (M-O1, S-O2, NIT)

def _report(aid, envs, pages=None, versions=None, ps=None):
    return {"audit_id": aid, "tenant_id": "t", "domain": "site.test", "scheme": "http", "agents": envs,
            "pages": pages or {}, "versions": versions or {"bot_families": "v", "schema_rules": "v"},
            "prompt_set": ps}


def _env(agent, outcome, findings, facts=None):
    return {"agent": agent, "task": "x", "outcome": outcome, "findings": findings, "facts": facts or {}}


def _f(code, url=None, sev="low", dec="ACT", **detail):
    return {"code": code, "url": url, "severity": sev, "decision": dec, "detail": detail}


def _delia_facts(failures=0, llms="present", urls=2):
    return {"sitemaps_read": 1, "sitemap_urls": urls, "sitemap_fetch_failures": failures,
            "llms_txt": {"state": llms}}


def test_drift_sitemap_503_is_tool_failure_not_change():
    a = _report("a", [_env("delia", "OK", [_f("SITEMAP_LOC_OTHER_HOST")], _delia_facts())])
    b = _report("b", [_env("delia", "PARTIAL", [_f("SITEMAP_UNREADABLE", "http://site.test/sitemap.xml",
                                                   "medium", "INSUFFICIENT_EVIDENCE")], _delia_facts(1, urls=0))])
    out = drift.compare(a, b)
    assert {c["class"] for c in out["changes"]} == {"TOOL_FAILURE"} and out["counts"]["REAL_CHANGE"] == 0


@pytest.mark.parametrize("before,after", [("unreadable", "present"), ("present", "unreadable")])
def test_drift_llms_connection_drop_either_side_is_tool_failure(before, after):
    fa = [_f("LLMS_TXT_FORMAT", "http://site.test/llms.txt", "low", "TEST")] if before == "present" else []
    fb = [_f("LLMS_TXT_FORMAT", "http://site.test/llms.txt", "low", "TEST")] if after == "present" else []
    a = _report("a", [_env("delia", "PARTIAL", fa, _delia_facts(llms=before))])
    b = _report("b", [_env("delia", "PARTIAL", fb, _delia_facts(llms=after))])
    out = drift.compare(a, b)
    assert [c["class"] for c in out["changes"]] == ["TOOL_FAILURE"]


def test_drift_real_sitemap_change_stays_real():
    a = _report("a", [_env("delia", "OK", [], _delia_facts(urls=2))])
    b = _report("b", [_env("delia", "OK", [_f("SITEMAP_LOC_OTHER_HOST")], _delia_facts(urls=3))])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["REAL_CHANGE"]


def test_drift_robots_unreachable_is_tool_failure():
    url = "http://site.test/robots.txt"
    fa = {"robots": {"status_class": "ok", "text_sha256": "1"}}
    fb = {"robots": {"status_class": "unreachable", "text_sha256": None}}
    a = _report("a", [_env("selene", "OK", [_f("ROBOTS_BLOCKS_SEARCH_CRAWLER", url, token="Googlebot")], fa)])
    b = _report("b", [_env("selene", "PARTIAL", [], fb)])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["TOOL_FAILURE"]


def _callum(ci_c, ci_m, mv="m@1"):
    return _env("callum", "OK", [_f("CITED", None, "low", "WATCH", engine="e", prompt_index=0)],
                {"engines": {"e": {"prompts": [{"model_versions": [mv], "citation_ci95": ci_c,
                                                "mention_ci95": ci_m}]}}})


@pytest.mark.parametrize("ci_b,cls", [([[0.1, 0.3], [0.1, 0.3]], "SAMPLING_NOISE"),
                                      ([[0.1, 0.3], [0.8, 0.9]], "SURFACE_DRIFT"),
                                      ([[0.8, 0.9], [0.1, 0.3]], "SURFACE_DRIFT")])
def test_drift_sampling_noise_needs_both_intervals_to_overlap(ci_b, cls):
    a = _report("a", [_callum([0.1, 0.3], [0.1, 0.3])], ps={"sha256": "p"})
    eb = _callum(*ci_b)
    eb["findings"][0]["severity"] = "high"
    b = _report("b", [eb], ps={"sha256": "p"})
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == [cls]


def test_overturned_counts_only_never_real_classes():
    assert set(drift.OVERTURNING) == {"MEASUREMENT_ERROR", "SAMPLING_NOISE"}
    a = _report("a", [_env("delia", "OK", [_f("SITEMAP_LOC_OTHER_HOST")], _delia_facts(urls=3))])
    b = _report("b", [_env("delia", "OK", [], _delia_facts(urls=2))])
    out = drift.compare(a, b)
    assert out["changes"][0]["class"] == "REAL_CHANGE" and out["overturned_by_agent"] == {}
    a2 = _report("a", [_callum([0.1, 0.3], [0.1, 0.3])], ps={"sha256": "p"})
    b2 = _report("b", [_env("callum", "OK", [], a2["agents"][0]["facts"])], ps={"sha256": "p"})
    out = drift.compare(a2, b2)
    assert out["changes"][0]["class"] == "SAMPLING_NOISE" and out["overturned_by_agent"] == {"callum": 1}


def test_drift_fingerprint_field_only_in_older_report_counts():
    url = "http://site.test/"
    pa = {"/": {"fetch": {"state": "OK", "status": 200}, "fingerprint": {"title": "1", "extra": "x"}}}
    pb = {"/": {"fetch": {"state": "OK", "status": 200}, "fingerprint": {"title": "1"}}}
    a = _report("a", [_env("roman", "OK", [_f("TITLE_LENGTH", url)])], pa)
    b = _report("b", [_env("roman", "OK", [])], pb)
    ch = drift.compare(a, b)["changes"][0]
    assert ch["class"] == "REAL_CHANGE" and ch["evidence"]["changed_fields"] == ["extra"]


# ============================================================================================ bot families sanity

def test_known_bots_cover_the_families():
    assert {f["token"].lower() for f in bots.FAMILIES} <= su.known_bots()


# ============================================================================================ service path (M-S2..M-S4)

import base64 as _b64  # noqa: E402
import os as _os  # noqa: E402

from helpers import Harness, rid  # noqa: E402
from ports import Ports  # noqa: E402

GOOGLE_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def _h(tmp_path, web, durable=True, **env):
    ports = Ports.default()
    ports.fetcher = web.fetcher()
    h = Harness(tmp_path, data_dir=str(tmp_path / "data") if durable else None, ports=ports, **env)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    return h


def _audit(h, paths=("/", "/about")):
    return h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                               "paths": list(paths)}), 201)


def _clf(ip, path, status=200, ua=GOOGLE_UA):
    return f'{ip} - - [10/Oct/2026:13:55:36 -0700] "GET {path} HTTP/1.1" {status} 2326 "-" "{ua}"'


def _send_log(h, lines, last=True, seq=1, iid=None):
    if iid is None:
        iid = h.ok(h.post("/tenants/zbm/log-ingests", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                                        "format": "combined"}), 201)["ingest_id"]
    data = _b64.b64encode(("\n".join(lines) + "\n").encode()).decode()
    h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/chunks", {"request_id": rid(), "seq": seq, "data_b64": data,
                                                           "last": last}))
    return iid


def _finish(h, iid):
    return h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))


def _everything(h, tmp_path) -> str:
    blobs = [json.dumps(h.ledger.events, default=str), json.dumps(h.ok(h.get("/audit/export",
                                                                            caller="compliance_38")))]
    for root, _, files in _os.walk(tmp_path / "data"):
        for f in files:
            with open(_os.path.join(root, f), "rb") as fh:
                blobs.append(fh.read().decode("utf-8", "replace"))
    return "\n".join(blobs)


def test_robots_text_and_sitemap_paths_never_reach_the_data_dir(tmp_path):
    m = marker()
    web = site(Web(), robots=f"# {m}\nUser-agent: *\nDisallow: /private-{m}\nSitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap(["http://site.test/", f"http://site.test/docs/{m}{m}", "http://site.test/about"]),
              ctype="application/xml")
    h = _h(tmp_path, web)
    a = _audit(h)
    iid = _send_log(h, [_clf("192.0.2.5", "/about"), _clf("192.0.2.5", f"/private-{m}")])
    done = _finish(h, iid)
    env = done["report"]["envelope"]
    blob = _everything(h, tmp_path) + json.dumps(a) + json.dumps(done)
    assert m not in blob
    g = env["facts"]["families"]["Googlebot"]
    assert g["robots_disallowed_hits"] == 1                       # the robots mutant: inverted counts 1 the other way
    un = env["facts"]["uncrawled_important_paths"]
    assert un[0] == "/" and re.fullmatch(r"/docs/\{(token|number)\}", un[1]) and len(un) == 2
    ex = [f for f in env["findings"] if f["code"] == "IMPORTANT_URLS_NOT_CRAWLED"][0]["detail"]["examples"]
    assert ex == un
    assert '"robots_text"' not in blob and '"robots_sha256"' in blob   # M-S3: the hash is recorded, not the text


def test_robots_not_in_memory_after_restart_is_counted_unchecked(tmp_path):
    web = site(Web(), robots="User-agent: *\nDisallow: /x\nSitemap: http://site.test/sitemap.xml\n")
    h = _h(tmp_path, web)
    _audit(h)
    iid = _send_log(h, [_clf("192.0.2.5", "/x")], last=False)
    h = h.restart()
    _send_log(h, [_clf("192.0.2.5", "/x"), _clf("192.0.2.5", "/y")], seq=2, iid=iid)
    env = _finish(h, iid)["report"]["envelope"]
    assert env["outcome"] == "PARTIAL" and env["facts"]["robots_unchecked_requests"] == 2
    assert env["facts"]["families"]["Googlebot"]["robots_disallowed_hits"] == 1
    assert "LOG_ROBOTS_CHECK_INCOMPLETE" in codes(env)


@pytest.mark.parametrize("n,errors,flag", [(20, 1, False), (20, 2, True), (19, 19, False), (40, 2, False),
                                           (40, 3, True)])
def test_search_crawler_5xx_threshold(tmp_path, n, errors, flag):
    web = site(Web())
    h = _h(tmp_path, web, durable=False)
    lines = [_clf("192.0.2.5", "/", 503 if i < errors else 200) for i in range(n)]
    env = _finish(h, _send_log(h, lines))["report"]["envelope"]
    assert ("SEARCH_CRAWLER_SERVER_ERRORS" in codes(env)) is flag


def test_log_chunk_robots_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(logs_mod, "ROBOTS_CHUNK_BUDGET_S", 0.0)
    web = site(Web(), robots="User-agent: *\nDisallow: /x\nSitemap: http://site.test/sitemap.xml\n")
    h = _h(tmp_path, web, durable=False)
    env = _finish(h, _send_log(h, [_clf("192.0.2.5", "/x"), _clf("192.0.2.5", "/y")]))["report"]["envelope"]
    assert env["facts"]["robots_unchecked_requests"] == 2 and env["outcome"] == "PARTIAL"


def test_audit_report_carries_no_crawled_url_raw(tmp_path):
    m = marker()
    web = site(Web(), robots=f"User-agent: *\nAllow: /\nSitemap: http://site.test/s-{m}.xml\n")
    web.route(f"/s-{m}.xml", sitemap([f"http://site.test/p/{m}{m}", "http://site.test/about"]),
              ctype="application/xml")
    web.route("/", "", status=302, ctype="text/plain", location=f"/landing/{m}{m}?u={m}")
    web.route(f"/landing/{m}{m}", page(title=m, canonical=f"http://other.test/{m}{m}"), x_robots_tag=f"{m}")
    h = _h(tmp_path, web)
    a = _audit(h)
    assert a["status"] == "completed"
    assert m not in _everything(h, tmp_path) + json.dumps(a)
    drift_view = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}/drift?against={_audit(h)['audit_id']}"))
    assert m not in json.dumps(drift_view)


# ============================================================================================ round-1 M4 survivors

def test_pinned_heuristic_constants():
    # the parametrized bounds above follow the constants; these pin the values themselves (M4: R1, R2, R15)
    assert (roman.TITLE_MAX, roman.TITLE_MIN, roman.RAW_TEXT_MIN) == (60, 10, 500)
    web = site(Web())
    web.route("/", page(title="t" * 61, ld=ORG_LD))
    assert "TITLE_LENGTH" in codes(_roman(web))
    web.route("/", page(title="t" * 9, ld=ORG_LD))
    assert "TITLE_LENGTH" in codes(_roman(web))
    web.route("/", page(title="t", body="x" * 497, ld=ORG_LD, h1=None).replace("<h2>What we do</h2>", ""))
    assert "LITTLE_TEXT_IN_RAW_HTML" in codes(_roman(web))       # 499 visible characters


def test_a_sitemap_listed_twice_is_read_once():
    web = site(Web())
    web.route("/sitemap.xml", index(["http://site.test/a.xml", "http://site.test/a.xml"]), ctype="application/xml")
    web.route("/a.xml", sitemap(["http://site.test/"]), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert web.requests.count(("site.test", "/a.xml")) == 1 and d["facts"]["sitemap_urls"] == 1


def test_drift_sitemap_unreadable_on_one_side_is_tool_failure_for_that_url():
    u = "http://site.test/s.xml"
    a = _report("a", [_env("delia", "OK", [_f("SITEMAP_NAMESPACE", u)], _delia_facts())])
    b = _report("b", [_env("delia", "OK", [_f("SITEMAP_UNREADABLE", u, "medium", "INSUFFICIENT_EVIDENCE")],
                           _delia_facts())])
    assert {c["code"]: c["class"] for c in drift.compare(a, b)["changes"]} == {
        "SITEMAP_NAMESPACE": "TOOL_FAILURE", "SITEMAP_UNREADABLE": "TOOL_FAILURE"}


def test_drift_unreachable_code_is_tool_failure():
    url = "http://site.test/robots.txt"
    facts = {"robots": {"status_class": "ok", "text_sha256": None}}
    a = _report("a", [_env("selene", "OK", [], facts)])
    b = _report("b", [_env("selene", "OK", [_f("ROBOTS_UNREACHABLE", url, "critical", "ACT")], facts)])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["TOOL_FAILURE"]
