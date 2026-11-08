"""The SEO Technical Audit as a sellable client document (Gate 4 run against zbestmedia.com, 2026-11-08):

  1. the cover shows a client-facing coverage statement ("Partial audit: N of 6 areas checked", with each unchecked
     area and why), never the internal run outcome (FAILED); it never claims an area was checked that was not;
  2. a normal audit reads a bounded number of pages: same-site links of the requested pages (one level, robots.txt
     honoured for our crawler, no query strings, no other hosts or ports) up to SEO_AUDIT_MAX_PAGES, through the same
     SSRF-guarded fetcher; a sitemap observed missing (404) is a finding in a checked area, not a failed area;
  3. an inconclusive comparison never becomes a client-facing finding: a bot-versus-browser difference must hold on
     a repeat request by our crawler; a browser request that was challenged (403 / 429 / 503) is inconclusive;
     canonical URLs are compared normalised; a canonical target our own request could not read is not a finding;
  4. no internal jargon reaches the client: no "Wave 1", "ORCA Publish", "Zero-Day", "NOT_CONNECTED", rule or reason
     codes in the body (codes only in the appendix's reference table); each limitation line is stated once;
  5. an executive summary at the top: what was checked, the top issues ranked by business impact, what to do first;
     deterministic and derived only from the findings.
"""

from __future__ import annotations

import copy
import re

import pytest

import client_report as cr
import report_fixtures as fx
from agents import RunContext, delia, osei, selene
from clock import FixedClock
from envelope import envelope, finding
from fixture_server import by_ua, home_html, install_site
from helpers import T0, Harness, rid
from ports import Ports
from primitives import diff as diff_mod
from primitives import fetch as fetch_mod

U = "https://acme.test"
JARGON = re.compile(r"Wave[- ]?\d|ORCA|Zero-Day|NOT_CONNECTED|founder-pending|clientfix|Department \d|Run outcome")
CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def _ctx(srv, paths=("/",), max_pages=10):
    clock = FixedClock(T0)
    return RunContext(srv.site_fetcher(), None, lambda **k: None, clock, osei.Osei(clock), "site.test",
                      scheme="http", paths=list(paths), max_pages=max_pages)


def _links(*hrefs, nofollow=()):
    a = "".join(f"<a href='{x}'>{x}</a>" for x in hrefs) + "".join(f"<a rel='nofollow' href='{x}'>n</a>"
                                                                    for x in nofollow)
    return home_html(body="<p>" + "Revenue recovery for brands across every marketplace. " * 10 + "</p>" + a)


def _body(doc: str) -> str:
    """The document without its appendix (where reference codes are allowed)."""
    i = doc.find("Appendix: audit record")
    return doc if i < 0 else doc[:i]


# ------------------------------------------------------------------------------------------- 2. page discovery

@pytest.mark.local_http
def test_discovery_reads_same_site_links_and_skips_everything_unsafe_or_not_a_page(srv):
    install_site(srv, robots="User-agent: *\nAllow: /\nUser-agent: ZBM-SEO-Audit\nDisallow: /private\n")
    srv.html("site.test", "/", _links(
        "/services", "/about", "/services/", "http://www.site.test/team", "/contact?ref=nav", "/files/deck.pdf",
        "/logo.png", "http://other.test/x", "http://169.254.169.254/latest/meta-data", "http://site.test:8080/admin",
        "/private/area", "/logout", "/u/555-010-0199", "mailto:a@b.test", "/blog/post.html",
        nofollow=("/nofollow-page",)))
    for p in ("/services", "/team", "/blog/post.html"):
        srv.html("site.test", p, home_html(title=f"Page {p}", canonical=f"http://site.test{p}"))
    env, pages = selene.run(_ctx(srv))
    disc = env["facts"]["pages_discovered"]
    assert disc == ["/services", "/about", "/team", "/blog/post.html"]
    assert list(pages) == ["/"] + disc and env["outcome"] == "OK"
    seen = {s["path"].split("?")[0] for s in srv.seen}
    for never in ("/contact", "/files/deck.pdf", "/logo.png", "/x", "/latest/meta-data", "/admin", "/private/area",
                  "/logout", "/u/555-010-0199", "/nofollow-page"):
        assert never not in seen, never
    assert {s["host"] for s in srv.seen} == {"site.test"}          # no other host was ever contacted


@pytest.mark.local_http
@pytest.mark.parametrize("cap,expected", [(0, 0), (1, 0), (2, 1), (3, 2), (25, 4)])
def test_discovery_never_exceeds_the_per_audit_page_cap(srv, cap, expected):
    install_site(srv)
    srv.html("site.test", "/", _links("/a", "/b", "/c", "/d"))
    for p in "abcd":
        srv.html("site.test", "/" + p, home_html(title=f"Page {p}", canonical=f"http://site.test/{p}"))
    env, pages = selene.run(_ctx(srv, max_pages=cap))
    assert len(env["facts"]["pages_discovered"]) == expected and len(pages) == 1 + expected


@pytest.mark.local_http
def test_no_discovery_when_robots_txt_is_unreadable(srv):
    install_site(srv, robots=None)
    srv.text("site.test", "/robots.txt", "down", status=503)
    srv.html("site.test", "/", _links("/a"))
    env, pages = selene.run(_ctx(srv))
    assert env["facts"].get("pages_discovered", []) == [] and list(pages) == ["/"]


@pytest.mark.local_http
def test_a_broken_internal_link_is_a_finding_and_the_area_stays_checked(srv):
    install_site(srv)
    srv.html("site.test", "/", _links("/gone"))
    srv.html("site.test", "/gone", "<p>not here</p>", status=404)
    env, pages = selene.run(_ctx(srv))
    assert env["outcome"] == "OK" and "/gone" in pages
    assert any(f["code"] == "PAGE_HTTP_ERROR" and f["url"].endswith("/gone") for f in env["findings"])


@pytest.mark.local_http
def test_default_audit_reads_more_than_the_homepage_and_records_the_discovered_pages(tmp_path, srv):
    install_site(srv)
    srv.html("site.test", "/", _links("/about", "/services"))
    srv.html("site.test", "/services", home_html(title="Services", canonical="http://site.test/services"))
    h = _harness(tmp_path, srv)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    a = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http"}), 201)
    r = a["report"]
    assert r["paths"] == ["/"] and r["discovered_paths"] == ["/about", "/services"]
    assert set(r["pages"]) == {"/", "/about", "/services"}
    doc = h.ok(h.post(f"/tenants/zbm/audits/{a['audit_id']}/client-report",
                      {"request_id": rid(), "format": "markdown"}, caller="dashboard", andre=True))["content"]
    assert "3 page(s): 1 requested and 2 found by following links on your site" in doc
    assert "**Pages found by following links (2):** ` /about `, ` /services `" in doc


@pytest.mark.local_http
def test_the_page_cap_setting_bounds_discovery_end_to_end(tmp_path, srv):
    install_site(srv)
    srv.html("site.test", "/", _links("/about", "/a", "/b"))
    h = _harness(tmp_path, srv, SEO_AUDIT_MAX_PAGES="2")
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    a = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http"}), 201)
    assert a["report"]["discovered_paths"] == ["/about"] and len(a["report"]["pages"]) == 2


@pytest.mark.local_http
@pytest.mark.parametrize("status,outcome,code", [(404, "OK", "SITEMAP_MISSING"), (410, "OK", "SITEMAP_MISSING"),
                                                 (503, "PARTIAL", "SITEMAP_UNREADABLE")])
def test_a_sitemap_observed_missing_is_a_finding_not_an_unchecked_area(srv, status, outcome, code):
    install_site(srv, robots="User-agent: *\nAllow: /\n")
    srv.text("site.test", "/sitemap.xml", "x", status=status)
    ctx = _ctx(srv)
    selene.run(ctx)
    env = delia.run(ctx)
    assert env["outcome"] == outcome and code in {f["code"] for f in env["findings"]}


@pytest.mark.local_http
def test_sitemap_unreadable_and_llms_unreadable_still_fail_closed(srv):
    install_site(srv, robots="User-agent: *\nAllow: /\n", llms=None)
    srv.text("site.test", "/sitemap.xml", "x", status=503)
    srv.routes[("site.test", "/llms.txt")] = lambda hd: hd._send(200, {"Content-Type": "text/plain",
                                                                       "Content-Encoding": "br"}, b"x")
    ctx = _ctx(srv)
    selene.run(ctx)
    assert delia.run(ctx)["outcome"] == "FAILED"


# ------------------------------------------------------------------------- 3. inconclusive is never a finding

def _f(state="OK", status=200, final="http://site.test/"):
    return fetch_mod.FetchResult(url="http://site.test/", state=state, status=status, final_url=final)


def _x(title="T", canon=("http://site.test/",), text=1000):
    return {"title": title, "canonicals": list(canon), "robots_meta": {}, "headings": [{"level": 1, "text": "H"}],
            "text_length": text, "links": []}


def test_canonical_spelling_differences_are_not_a_differential():
    a = diff_mod.access_diff(_f(), _f(final="HTTP://Site.Test:80/"), _x(canon=["http://site.test/a/"]),
                             _x(canon=["HTTP://SITE.TEST:80/a"]))
    assert a["state"] == "SAME"


@pytest.mark.parametrize("status", [401, 403, 405, 429, 503])
def test_a_challenged_browser_request_is_inconclusive(status):
    a = diff_mod.access_diff(_f(), _f(status=status), _x(), _x(title="Just a moment..."))
    assert a["state"] == "INCONCLUSIVE" and a["signals"] == []


def test_confirm_keeps_only_stable_signals():
    first = {"state": "BOT_DIFFERENTIAL", "signals": [{"signal": "title"}, {"signal": "canonicals"}]}
    assert diff_mod.confirm(first, None)["state"] == "INCONCLUSIVE"
    assert diff_mod.confirm(first, {"state": "INCONCLUSIVE", "signals": []})["state"] == "INCONCLUSIVE"
    both = diff_mod.confirm(first, {"state": "BOT_DIFFERENTIAL", "signals": [{"signal": "title"},
                                                                             {"signal": "canonicals"}]})
    assert both["state"] == "INCONCLUSIVE"
    one = diff_mod.confirm(first, {"state": "BOT_DIFFERENTIAL", "signals": [{"signal": "title"}]})
    assert one == {"state": "BOT_DIFFERENTIAL", "signals": [{"signal": "canonicals"}], "confirmed": True,
                   "unstable": ["title"]}
    assert diff_mod.confirm(first, {"state": "SAME", "signals": []})["confirmed"] is True
    assert diff_mod.confirm({"state": "SAME", "signals": []}, None) == {"state": "SAME", "signals": []}


@pytest.mark.local_http
def test_rotating_content_is_not_reported_as_cloaking(srv):
    install_site(srv)
    n = {"i": 0}

    def rotating(hd):
        n["i"] += 1
        hd._send(200, {"Content-Type": "text/html"}, home_html(title=f"Deal of the minute {n['i']}").encode())
    srv.routes[("site.test", "/")] = rotating
    env, pages = selene.run(_ctx(srv, max_pages=0))
    assert pages["/"]["access_diff"]["state"] == "INCONCLUSIVE"
    assert "BOT_DIFFERENTIAL" not in {f["code"] for f in env["findings"]}


@pytest.mark.local_http
def test_a_challenged_browser_fetch_is_not_a_finding(srv):
    install_site(srv)

    def challenge(hd):
        if fetch_mod.PRODUCT_TOKEN in (hd.headers.get("User-Agent") or ""):
            hd._send(200, {"Content-Type": "text/html"}, home_html().encode())
        else:
            hd._send(403, {"Content-Type": "text/html"}, b"<title>Just a moment...</title>")
    srv.routes[("site.test", "/")] = challenge
    env, pages = selene.run(_ctx(srv, max_pages=0))
    assert pages["/"]["access_diff"]["state"] == "INCONCLUSIVE"
    assert "BOT_DIFFERENTIAL" not in {f["code"] for f in env["findings"]}


@pytest.mark.local_http
def test_stable_cloaking_is_still_reported_and_confirmed(srv):
    install_site(srv)
    srv.routes[("site.test", "/")] = by_ua(home_html(canonical="http://site.test/"),
                                           home_html(canonical="http://site.test/elsewhere"))
    env, _ = selene.run(_ctx(srv, max_pages=0))
    bd = [f for f in env["findings"] if f["code"] == "BOT_DIFFERENTIAL"]
    assert len(bd) == 1 and bd[0]["detail"]["confirmed"] is True
    assert {s["signal"] for s in bd[0]["detail"]["signals"]} == {"canonicals"}


@pytest.mark.local_http
def test_a_canonical_target_our_crawler_may_not_read_is_inconclusive_not_a_finding(srv):
    install_site(srv, robots="User-agent: *\nAllow: /\nUser-agent: ZBM-SEO-Audit\nDisallow: /preferred\n")
    srv.html("site.test", "/", home_html(canonical="http://site.test/preferred"))
    env, _ = selene.run(_ctx(srv, max_pages=0))
    assert "CANONICAL_TARGET_NOT_200" not in {f["code"] for f in env["findings"]}
    assert env["facts"]["canonical_checks_inconclusive"] == [{"path": "/", "fetch_state": "BLOCKED_BY_ROBOTS"}]


@pytest.mark.local_http
def test_a_canonical_target_that_answers_404_is_still_a_finding(srv):
    install_site(srv)
    srv.html("site.test", "/", home_html(canonical="http://site.test/old-home"))
    env, _ = selene.run(_ctx(srv, max_pages=0))
    f = [x for x in env["findings"] if x["code"] == "CANONICAL_TARGET_NOT_200"]
    assert len(f) == 1 and f[0]["detail"]["status"] == 404 and f[0]["detail"]["fetch_state"] == "OK"


@pytest.mark.local_http
def test_a_canonical_spelled_differently_is_not_fetched_again(srv):
    install_site(srv)
    srv.html("site.test", "/", home_html(canonical="HTTP://SITE.TEST:80"))
    env, _ = selene.run(_ctx(srv, max_pages=0))
    assert not {"CANONICAL_TARGET_NOT_200", "CANONICAL_OTHER_HOST"} & {f["code"] for f in env["findings"]}
    assert sum(1 for s in srv.seen if s["path"] == "/") == 2          # our crawler and the browser comparison only


def _with_selene_findings(*fl, outcome="OK"):
    v = fx.fresh("full")
    agents = [e for e in v["report"]["agents"] if e["agent"] != "selene"]
    agents.insert(0, envelope("selene", "crawlability", outcome, list(fl), methodology="Fetched robots.txt.",
                              limitations=["One location."]))
    v["report"]["agents"] = agents
    return v


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_recorded_inconclusive_comparisons_never_reach_the_client(fmt):
    """Reports recorded before the confirmation existed (the Gate 4 report) are re-rendered safely: an unconfirmed
    BOT_DIFFERENTIAL, a canonical target our request could not read, and any finding marked inconclusive are left
    out of the document and of the summary, with a note saying so."""
    v = _with_selene_findings(
        finding("BOT_DIFFERENTIAL", "high", "measured", "TEST", url=U + "/", capability="P2",
                detail={"signals": [{"signal": "canonicals"}]}),
        finding("CANONICAL_TARGET_NOT_200", "high", "measured", "ACT", url=U + "/",
                detail={"canonical": U + "/x", "fetch_state": "TIMEOUT", "status": None, "redirected": False}),
        finding("CANONICAL_OTHER_HOST", "medium", "measured", "TEST", url=U + "/",
                detail={"canonical": "https://other.test/", "state": "INCONCLUSIVE"}))
    doc, model = cr.render(v, "acme", fmt)
    t = doc.decode()
    assert "Crawlers are served different content" not in t and "Canonical target does not answer" not in t
    assert "Canonical points to another host" not in t
    assert "3 comparison(s) could not be confirmed" in t
    crawl = next(s for s in model["sections"] if s["key"] == "crawlability")
    assert crawl["groups"] == [] and crawl["status"]["checked"]
    assert all(i["area"] != "Crawlability" for i in model["summary"]["issues"])


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_confirmed_differential_and_real_canonical_error_are_shown_in_plain_words(fmt):
    v = _with_selene_findings(
        finding("BOT_DIFFERENTIAL", "high", "measured", "TEST", url=U + "/", capability="P2",
                detail={"signals": [{"signal": "canonicals"}, {"signal": "title"}], "confirmed": True}),
        finding("CANONICAL_TARGET_NOT_200", "high", "measured", "ACT", url=U + "/",
                detail={"canonical": U + "/x", "fetch_state": "OK", "status": 404, "redirected": False}))
    t = cr.render(v, "acme", fmt)[0].decode()
    assert "Crawlers are served different content than browsers" in t
    assert "what differed: canonical link; page title" in t
    assert "Canonical target does not answer 200" in t and "status: 404" in t
    assert "could not be confirmed" not in t


# --------------------------------------------------------------------------- 1. coverage, never FAILED on the cover

def _gate4_like(outcome="FAILED"):
    """The Gate 4 shape: crawl and content checked, sitemaps failed, entity not covered (no business record on
    file), Search Console not connected, no market questions; the recorded run outcome FAILED."""
    v = fx.fresh("not_connected")
    rep = v["report"]
    selene_env = envelope("selene", "crawlability", "OK", [
        finding("CANONICAL_MISSING", "low", "measured", "WATCH", url=U + "/")],
        methodology="Fetched robots.txt.", limitations=list(selene.LIMITS))
    delia_env = envelope("delia", "sitemaps_and_llms_txt", "FAILED", [
        finding("SITEMAP_UNREADABLE", "medium", "measured", "INSUFFICIENT_EVIDENCE", url=U + "/sitemap.xml",
                capability="P3", detail={"fetch_state": "TIMEOUT", "status": None})],
        methodology="Sitemaps.", limitations=list(delia.LIMITS))
    from agents import entity_check, roman
    roman_env = envelope("roman", "content_structure", "OK", [
        finding("STRUCTURED_DATA_ERRORS", "medium", "measured", "ACT", url=U + "/", capability="P3",
                detail={"issues": [{"code": "REQUIRED_MISSING", "level": "error", "type": "ProfessionalService",
                                    "property": "address"}]}),
        finding("ENTITY_SAMEAS_MISSING", "low", "measured", "TEST", url=U + "/", capability="P3"),
        finding("META_DESCRIPTION_MISSING", "low", "measured", "TEST", url=U + "/", capability="P3")],
        methodology=roman.METHODOLOGY, limitations=list(roman.LIMITS))
    entity_env = envelope("entity_check", "on_site_entity_consistency", "INSUFFICIENT_EVIDENCE", [],
                          methodology=entity_check.METHODOLOGY, limitations=list(entity_check.LIMITS),
                          reason="this tenant has no canonical entity record")
    rest = [e for e in rep["agents"] if e["agent"] not in ("selene", "entity_check")]
    rep["agents"] = [selene_env, delia_env, roman_env, entity_env] + rest
    from svc_audits import GLOBAL_LIMITS
    rep["limitations"] = GLOBAL_LIMITS + sorted({x for e in rep["agents"] for x in e["limitations"]})
    rep["not_connected"] = sorted(set(rep["not_connected"]) | {"zero_day", "orca_publish", "clientfix"})
    rep["outcome"] = outcome
    return v


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_partial_run_cover_is_client_facing_and_honest(fmt):
    t, model = cr.render(_gate4_like(), "acme", fmt)
    t = t.decode()
    assert model["run_outcome"] == "FAILED"                        # still in the model for the record
    assert "FAILED" not in t and "Run outcome" not in t
    assert "Partial audit: 2 of 6 areas checked" in t
    for area in ("Sitemaps and", "Entity consistency", "Search performance (Google Search Console)",
                 "AI visibility (answer engines)"):
        assert re.search(re.escape(area) + r".{0,40}not checked\.", t), area
    assert "Your business details (name, address, phone) are not on file" in t
    assert model["coverage"]["checked"] == 2 and model["coverage"]["total"] == 6


def test_coverage_boundaries():
    full = cr.build(fx.fresh("full"), "acme")
    assert full["coverage"]["label"] == "Complete audit: all 6 areas checked" and full["coverage"]["complete"]
    none = cr.build(fx.fresh("not_connected"), "acme")
    assert none["coverage"]["label"] == "No area could be checked (0 of 6)" and none["summary"]["issues"] == []
    assert "Nothing could be checked" in none["summary"]["do_first"]
    v = fx.fresh("full")
    next(e for e in v["report"]["agents"] if e["agent"] == "roman")["outcome"] = "PARTIAL"
    part = cr.build(v, "acme")
    assert part["coverage"]["label"] == "Partial audit: 6 of 6 areas checked (1 of them only in part)"
    assert not part["coverage"]["complete"]


# ---------------------------------------------------------------------------------------------- 4. no jargon

@pytest.mark.parametrize("fmt", ["markdown", "html"])
@pytest.mark.parametrize("name", ["gate4", "full", "not_connected", "empty"])
def test_no_internal_jargon_or_codes_in_the_client_body(fmt, name):
    v = _gate4_like() if name == "gate4" else fx.fresh(name)
    t = cr.render(v, "acme", fmt)[0].decode().replace("\\_", "_")
    body = _body(t)
    assert not JARGON.search(t), JARGON.search(t).group(0)
    assert not CODE.search(body), CODE.search(body).group(0)
    assert "(rule " not in body and "(reason " not in body and "**Rule:**" not in body


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_each_limitation_line_is_stated_once(fmt):
    t = cr.render(_gate4_like(), "acme", fmt)[0].decode()
    items = re.findall(r"^- (.+)$", t, re.M) if fmt == "markdown" else re.findall(r"<li>(.*?)</li>", t)
    lim = [x for x in items if "JavaScript" in x or "network location" in x or "Only the audited pages" in x]
    assert lim and len(lim) == len(set(lim)), lim
    m = cr.build(_gate4_like(), "acme")
    every = m["global_limits"] + [x for meth in m["methods"] for x in meth["limits"]]
    assert len(every) == len({cr._key(x) for x in every})


def test_client_text_rewrites_and_drops_internal_lines():
    assert cr.client_text("Listed URLs are not checked in Wave 1.") == "Listed URLs are not checked in this audit."
    assert cr.client_text("Wave 1 reads public pages only.").startswith("This audit reads")
    assert cr.client_text("ports only in Wave 1 (founder-pending flag 5)") == "ports only in this audit"
    assert cr.client_text("Classes: STRENGTH, MENTIONED_NOT_CITED.") == "Classes: STRENGTH, mentioned not cited."
    assert cr.client_text("Zero-Day is a port.") is None and cr.client_text("ORCA Publish later.") is None
    assert cr.client_text(None) is None


def test_structured_data_issue_reads_as_a_sentence_with_crawled_names_quoted():
    t = cr.render(_gate4_like(), "acme", "markdown")[0].decode()
    assert "issues: required property ` address ` is missing from ` ProfessionalService `" in t
    h = cr.render(_gate4_like(), "acme", "html")[0].decode()
    assert "required property <code>address</code> is missing from <code>ProfessionalService</code>" in h


def test_crawled_issue_values_stay_quoted_and_screened():
    items = cr._issue_text({"code": "REQUIRED_MISSING", "type": "<b>x</b>", "property": "jane.doe@example.com"})
    assert ("c", cr.WITHHELD) in items and all(k in ("t", "c") for k, _ in items)
    assert cr._issue_text("NO_H1_TITLE") == [("t", "no # title line")]
    assert cr._issue_text({"code": "SOMETHING_NEW"}) == [("c", "SOMETHING_NEW")]
    assert cr._issue_text(42) == [("t", "issue")]


# ------------------------------------------------------------------------------------------ 5. executive summary

def test_executive_summary_ranks_by_impact_and_keeps_the_gate4_findings():
    m = cr.build(_gate4_like(), "acme")
    titles = [i["title"] for i in m["summary"]["issues"]]
    assert titles[0] == "Structured data has errors"
    assert "Organisation markup has no sameAs links" in titles
    assert titles.index("Organisation markup has no sameAs links") < titles.index("No meta description")
    assert m["summary"]["do_first"].startswith("Structured data has errors (Content structure).")
    assert "Canonical" not in " ".join(titles)                     # WATCH-only canonical note is below the fold
    assert m["summary"]["checked_areas"] == ["Crawlability", "Content structure"]


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_executive_summary_is_first_and_deterministic(fmt):
    a, _ = cr.render(_gate4_like(), "acme", fmt)
    b, _ = cr.render(copy.deepcopy(_gate4_like()), "acme", fmt)
    assert a == b
    t = a.decode()
    assert t.index("Executive summary") < t.index("What was checked:") < t.index("Audit details") \
        < t.index("Methodology and limits")
    assert "What to do first:" in t and "Top issues, most important first:" in t


def test_summary_excludes_info_and_no_change_findings_and_caps_at_five():
    m = cr.build(fx.fresh("full"), "acme")
    s = m["summary"]
    assert len(s["issues"]) == cr.SUMMARY_MAX and s["more"] > 0
    assert all(i["severity"] != "info" for i in s["issues"])
    sev = [cr.SEVERITY_ORDER.index(i["severity"]) for i in s["issues"]]
    assert sev == sorted(sev)
    e = cr.build(fx.fresh("empty"), "acme")["summary"]
    assert e["issues"] == [] and e["do_first"].startswith("No issue needing a change was found")
    assert "not covered by this result" in e["do_first"]


def test_reference_codes_live_only_in_the_appendix():
    t = cr.render(fx.fresh("full"), "acme", "markdown")[0].decode().replace("\\_", "_")
    i = t.index("## Appendix: audit record")
    assert "STRUCTURED_DATA_ERRORS" in t[i:] and "STRUCTURED_DATA_ERRORS" not in t[:i]
    assert "Audit ID" in t[i:] and "SHA-256" in t[i:]


# ----------------------------------------------------------------------------- end to end: a Gate 4-like site

def _harness(tmp_path, srv, **env):
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    return Harness(tmp_path, ports=ports, **env)


PRO_LD = ('{"@context":"https://schema.org","@type":"ProfessionalService","name":"Z Best Media",'
          '"url":"http://site.test/","telephone":"(562) 248-6617"}')


@pytest.mark.local_http
@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_gate4_like_site_end_to_end_yields_a_sellable_report(tmp_path, srv, fmt):
    install_site(srv, robots="User-agent: *\nAllow: /\n", llms=None)
    srv.text("site.test", "/sitemap.xml", "not found", status=404)
    home = home_html(ld=PRO_LD, body="<p>" + "Z Best Media recovers revenue for brands. " * 10 + "</p>"
                     "<a href='/services'>Services</a><a href='/contact'>Contact</a>")
    srv.html("site.test", "/", home)
    srv.html("site.test", "/services", home_html(title="Services | Z Best Media", ld=PRO_LD,
                                                 canonical="http://site.test/services"))
    srv.html("site.test", "/contact", home_html(title="Contact | Z Best Media", ld=PRO_LD,
                                                canonical="http://site.test/contact"))
    h = _harness(tmp_path, srv)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    a = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http"}), 201)
    agents = {e["agent"]: e for e in a["report"]["agents"]}
    assert agents["delia"]["outcome"] == "OK" and agents["selene"]["outcome"] == "OK"
    assert len(a["report"]["pages"]) == 3
    r = h.ok(h.post(f"/tenants/zbm/audits/{a['audit_id']}/client-report", {"request_id": rid(), "format": fmt},
                    caller="dashboard", andre=True))
    t = r["content"]
    assert t.index("Executive summary") < t.index("Audit details")
    assert "Structured data has errors" in t and "Organisation markup has no sameAs links" in t
    assert "No sitemap found" in t
    assert "FAILED" not in t and "Run outcome" not in t and not JARGON.search(t)
    assert "Partial audit: 4 of 6 areas checked" in t                # GSC + AI not connected, honestly stated
    assert "Crawlers are served different content" not in t
