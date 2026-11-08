"""Independent review of PR 47 (CONDITIONAL), findings H1..L8, each with success, failure and boundary cases:

  H1  the sitemap area is CHECKED only when every sitemap was read or seen missing AND llms.txt is known present or
      absent; otherwise PARTIAL with the reason (never "Complete audit");
  H2  401 / 403 / 405 / 429 are inconclusive (info, out of the executive summary, content not judged); only 404 / 410
      / 5xx are errors visitors see; older records are reclassified in the document;
  H3  our crawler refused while a browser identity is served is CRAWLER_REFUSED: one honest note, not cloaking, not a
      visitor error;
  M4  a document recorded under an earlier renderer replays byte-identically (frozen renderer, a4af277 goldens);
  L5  the discovery skip list holds against encodings, extensions, parameters and dot-folders;
  L6  a politeness wait between requests (floor, robots.txt Crawl-delay up to a cap), request and time budgets;
  L7  internal names are reworded, never dropped;
  L8  a canonical check our own request could not read surfaces as inconclusive in the client document.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import client_report as cr
import config
import report_fixtures as fx
import agents as agents_mod
from agents import delia, selene
from envelope import finding
from fixture_server import home_html, install_site
from helpers import Harness, base_env, rid
from ports import Ports
from primitives import diff as diff_mod
from primitives import fetch as fetch_mod
from primitives import robots as robots_mod
from test_report_sellable import U, _ctx, _f, _links, _with_selene_findings, _x


def _ports(srv):
    p = Ports.default()
    p.fetcher = srv.site_fetcher()
    return p


# ---------------------------------------------------------------------------------------------------------- H1

@pytest.mark.local_http
@pytest.mark.parametrize("llms,outcome,reason", [
    ("present", "OK", None), ("absent", "OK", None), ("unreadable", "PARTIAL", "LLMS_TXT_UNREAD")])
def test_sitemap_404_with_each_llms_state(srv, llms, outcome, reason):
    install_site(srv, robots="User-agent: *\nAllow: /\n",
                 llms="# T\n\n## S\n- [a](http://site.test/)\n" if llms == "present" else None)
    srv.text("site.test", "/sitemap.xml", "x", status=404)
    if llms == "unreadable":
        srv.routes[("site.test", "/llms.txt")] = lambda hd: hd._send(200, {"Content-Type": "text/plain",
                                                                           "Content-Encoding": "br"}, b"x")
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    env = delia.run(ctx)
    assert env["outcome"] == outcome and env["reason"] == reason


@pytest.mark.local_http
def test_sitemap_unreadable_with_llms_known_is_partial_with_its_reason(srv):
    install_site(srv, robots="User-agent: *\nAllow: /\n")
    srv.text("site.test", "/sitemap.xml", "x", status=503)
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    env = delia.run(ctx)
    assert env["outcome"] == "PARTIAL" and env["reason"] == "SITEMAP_UNREAD"


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_unknown_llms_txt_never_reads_as_a_complete_audit(fmt):
    v = fx.fresh("full")
    d = next(e for e in v["report"]["agents"] if e["agent"] == "delia")
    d["outcome"], d["reason"] = "PARTIAL", "LLMS_TXT_UNREAD"
    t, m = cr.render(v, "acme", fmt)
    t = t.decode()
    assert not m["coverage"]["complete"] and "Complete audit" not in t
    assert "checked only in part." in t and "could not be read (no usable answer), so whether it is published" in t


# ---------------------------------------------------------------------------------------------- H2 and H3

@pytest.mark.local_http
@pytest.mark.parametrize("status", [401, 403, 405, 429])
def test_a_refused_discovered_page_is_info_and_unread(srv, status):
    install_site(srv)
    srv.html("site.test", "/", _links("/members"))
    srv.html("site.test", "/members", "<title>Denied</title><p>no</p>", status=status)
    env, pages = selene.run(_ctx(srv))
    got = [f for f in env["findings"] if f["url"] and f["url"].endswith("/members")]
    assert [f["code"] for f in got] == ["PAGE_ACCESS_REFUSED"]
    assert got[0]["severity"] == "info" and got[0]["decision"] == "INSUFFICIENT_EVIDENCE"
    assert pages["/members"]["extract"] is None and pages["/members"]["refused"]
    assert env["outcome"] == "PARTIAL"                               # a page we could not read is not "checked"


@pytest.mark.local_http
@pytest.mark.parametrize("status,code", [(404, "PAGE_HTTP_ERROR"), (410, "PAGE_HTTP_ERROR"),
                                         (500, "PAGE_HTTP_ERROR"), (400, "PAGE_HTTP_ERROR_OTHER")])
def test_only_404_410_and_5xx_are_errors_visitors_see(srv, status, code):
    install_site(srv)
    srv.html("site.test", "/", _links("/x"))
    srv.html("site.test", "/x", "<title>Error page</title><h1>oops</h1>", status=status)
    env, pages = selene.run(_ctx(srv))
    got = [f for f in env["findings"] if f["url"] and f["url"].endswith("/x")]
    assert [f["code"] for f in got] == [code]                       # no canonical / noindex judged on an error page
    assert pages["/x"]["extract"] is None


def test_summary_states_visitor_impact_only_for_real_errors():
    v = _with_selene_findings(
        finding("PAGE_ACCESS_REFUSED", "info", "measured", "INSUFFICIENT_EVIDENCE", url=U + "/a", capability="P2",
                detail={"status": 403}),
        finding("PAGE_HTTP_ERROR", "medium", "measured", "ACT", url=U + "/b", detail={"status": 403}),  # old record
        finding("PAGE_HTTP_ERROR", "medium", "measured", "ACT", url=U + "/c", detail={"status": 404}))
    m = cr.build(v, "acme")
    visitor = [i for i in m["summary"]["issues"] if i["impact"].startswith("Visitors and search engines get an error")]
    assert len(visitor) == 1 and visitor[0]["where"] == 1
    crawl = next(s for s in m["sections"] if s["key"] == "crawlability")
    refused = next(g for g in crawl["groups"] if g["code"] == "PAGE_ACCESS_REFUSED")
    assert refused["severity"] == "info" and len(refused["locations"]) == 2      # the old 403 record reclassified
    err = next(g for g in crawl["groups"] if g["code"] == "PAGE_HTTP_ERROR")
    assert [x["text"] for x in err["locations"]] == ["/c"]
    assert all(i["title"] != refused["title"] for i in m["summary"]["issues"])


@pytest.mark.parametrize("status", [401, 403, 405, 429, 503])
def test_crawler_refused_while_browser_served_is_its_own_state(status):
    a = diff_mod.access_diff(_f(status=status), _f(), _x(title="Access denied"), _x())
    assert a["state"] == "CRAWLER_REFUSED" and a["signals"] == [] and a["bot_status"] == status
    assert diff_mod.access_diff(_f(status=403), _f(status=403), _x(), _x())["state"] == "INCONCLUSIVE"
    assert diff_mod.access_diff(_f(status=403), _f(status=404), _x(), _x())["state"] == "BOT_DIFFERENTIAL"


@pytest.mark.local_http
@pytest.mark.parametrize("status", [403, 503])
def test_crawler_refused_is_a_note_not_cloaking_and_not_a_visitor_error(srv, status):
    install_site(srv)

    def refuse_bots(hd):
        if fetch_mod.PRODUCT_TOKEN in (hd.headers.get("User-Agent") or ""):
            hd._send(status, {"Content-Type": "text/html"}, b"<title>Access denied</title><h1>No bots</h1>")
        else:
            hd._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[("site.test", "/")] = refuse_bots
    env, pages = selene.run(_ctx(srv, max_pages=0))
    codes = [f["code"] for f in env["findings"] if f["url"] == "http://site.test/"]
    assert codes == ["PAGE_ACCESS_REFUSED"]
    f = next(f for f in env["findings"] if f["code"] == "PAGE_ACCESS_REFUSED")
    assert f["detail"] == {"status": status, "browser_served": True} and f["severity"] == "info"
    assert pages["/"]["access_diff"]["state"] == "CRAWLER_REFUSED" and env["outcome"] == "BLOCKED"
    t = cr.render(_with_selene_findings(f, outcome="BLOCKED"), "acme", "markdown")[0].decode()
    assert "Crawlers are served different content" not in t and "Visitors and search engines get an error" not in t


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_refused_note_says_what_cannot_be_known(fmt):
    v = _with_selene_findings(finding("PAGE_ACCESS_REFUSED", "info", "measured", "INSUFFICIENT_EVIDENCE", url=U + "/",
                                      capability="P2", detail={"status": 403, "browser_served": True}))
    t = cr.render(v, "acme", fmt)[0].decode()
    assert "Our audit crawler was refused (inconclusive)" in t
    assert "Whether Google, Bing or AI crawlers are refused too cannot be seen from here" in t
    assert "browser served: yes" in t


# ---------------------------------------------------------------------------------------------------------- M4

@pytest.mark.parametrize("name", ["empty", "full", "hostile", "long", "not_connected"])
@pytest.mark.parametrize("fmt,ext", [("markdown", "md"), ("html", "html")])
def test_frozen_renderer_is_byte_identical_to_the_pre_upgrade_golden_files(name, fmt, ext):
    gold = Path(__file__).resolve().parent / "golden" / "client_report_legacy" / f"{name}.{ext}"
    data, model = cr.render_as("seo-client-report/2", fx.fresh(name), "acme", fmt)
    assert data == gold.read_bytes() and model["renderer_version"] in cr.LEGACY_RENDERER_VERSIONS


def test_render_as_dispatch():
    v = fx.fresh("full")
    assert cr.render_as("seo-client-report/6", v, "acme", "html") == cr.render(v, "acme", "html")
    for bad in ("seo-client-report/1", "seo-client-report/4", "seo-client-report/5", "seo-client-report/9", "", None):
        with pytest.raises(cr.ReportInputError):
            cr.render_as(bad, v, "acme", "html")


def test_replay_by_recorded_renderer_and_unknown_version_refuses(tmp_path, monkeypatch):
    from errors import Conflict
    h = Harness(tmp_path)
    h.tenant("acme", kind="own", domains=("acme.test",))
    v = fx.fresh("full")
    monkeypatch.setattr(h.svc, "audit_view", lambda *a, **kw: copy.deepcopy(v))
    req = {"request_id": rid(), "format": "markdown"}
    first = h.svc.render_client_report("acme", v["audit_id"], req)
    assert first["renderer_version"] == "seo-client-report/6"
    assert h.svc.render_client_report("acme", v["audit_id"], req) == first          # current renderer replays too
    h.svc.client_reports[first["report_id"]]["renderer_version"] = "seo-client-report/1"
    with pytest.raises(Conflict) as exc:
        h.svc.render_client_report("acme", v["audit_id"], req)
    assert "REPORT_CHANGED" in str(exc.value)


# ---------------------------------------------------------------------------------------------------------- L5

@pytest.mark.local_http
@pytest.mark.parametrize("href", [
    "/logout.php", "/logout;x=1", "/user/login.html", "/%61dmin", "/my-account", "/account/delete", "/api/v1/delete",
    "/.git/config", "/.env", "/.well-known/security.txt", "/wp-json/wp/v2/users", "/sign-in", "/signin",
    "/register", "/reset/password", "/%2561dmin", "/a%00b", "/Admin/", "/LOGIN", "/cart.html", "/x/delete-me"])
def test_unsafe_or_action_paths_are_never_discovered(srv, href):
    install_site(srv)
    srv.html("site.test", "/", _links(href))
    env, pages = selene.run(_ctx(srv))
    assert env["facts"]["pages_discovered"] == [] and list(pages) == ["/"]
    assert {s["path"] for s in srv.seen} <= {"/robots.txt", "/"}


@pytest.mark.local_http
@pytest.mark.parametrize("href", ["/accounting-services", "/services/seo", "/about-us/", "/blog/post.html"])
def test_ordinary_pages_are_still_discovered(srv, href):
    install_site(srv)
    srv.html("site.test", "/", _links(href))
    env, _ = selene.run(_ctx(srv))
    assert env["facts"]["pages_discovered"] == [href]


# ---------------------------------------------------------------------------------------------------------- L6

class _Clock:
    def __init__(self):
        self.t, self.waits = 0.0, []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.waits.append(s)
        self.t += s


def _polite_ctx(srv, delay, max_pages=10):
    c = _Clock()
    ctx = _ctx(srv, max_pages=max_pages)
    ctx.crawl_delay_s, ctx.sleep, ctx.monotonic = delay, c.sleep, c.monotonic
    return ctx, c


@pytest.mark.local_http
def test_every_request_after_the_first_waits_the_crawl_delay(srv):
    install_site(srv)
    srv.html("site.test", "/", _links("/a", "/b"))
    srv.html("site.test", "/a", home_html(canonical="http://site.test/a"))
    srv.html("site.test", "/b", home_html(canonical="http://site.test/b"))
    ctx, c = _polite_ctx(srv, 1.0)
    selene.run(ctx)
    delia.run(ctx)
    assert ctx.requests == len(srv.seen) and ctx.requests >= 8
    assert len(c.waits) == ctx.requests - 1 and all(abs(w - 1.0) < 1e-9 for w in c.waits)


@pytest.mark.local_http
def test_robots_crawl_delay_raises_the_wait_up_to_the_cap(srv):
    install_site(srv, robots="User-agent: *\nCrawl-delay: 2.5\nAllow: /\n")
    srv.html("site.test", "/", _links("/a"))
    ctx, c = _polite_ctx(srv, 1.0)
    env, pages = selene.run(ctx)
    assert env["facts"]["crawl_delay_seconds"] == 2.5 and all(abs(w - 2.5) < 1e-9 for w in c.waits)
    assert "/a" in pages and not any(f["code"] == "ROBOTS_LINES_IGNORED" for f in env["findings"])


@pytest.mark.local_http
def test_a_crawl_delay_above_the_cap_reads_only_the_requested_pages(srv):
    install_site(srv, robots="User-agent: ZBM-SEO-Audit\nCrawl-delay: 30\nAllow: /\n")
    srv.html("site.test", "/", _links("/a"))
    ctx, c = _polite_ctx(srv, 1.0)
    env, pages = selene.run(ctx)
    assert list(pages) == ["/"] and env["facts"]["discovery_stopped"] == "crawl_delay_above_cap"
    assert max(c.waits) == agents_mod.CRAWL_DELAY_CAP == 3.0


@pytest.mark.local_http
def test_discovery_stops_inside_the_time_budget(srv):
    install_site(srv, robots="User-agent: *\nCrawl-delay: 3\nAllow: /\n")
    srv.html("site.test", "/", _links(*[f"/p{i}" for i in range(24)]))
    for i in range(24):
        srv.html("site.test", f"/p{i}", home_html(title=f"P{i}", canonical=f"http://site.test/p{i}"))
    ctx, c = _polite_ctx(srv, 1.0, max_pages=25)
    env, pages = selene.run(ctx)
    assert env["facts"]["discovery_stopped"] == "time_budget"
    assert 1 < len(pages) < 25
    # the discovery loop never starts a page that could end past the budget
    assert sum(c.waits) <= selene.DISCOVERY_TIME_BUDGET_S + 4 * 3.0


@pytest.mark.local_http
def test_the_request_budget_bounds_a_crawl(srv):
    install_site(srv)
    srv.html("site.test", "/", _links(*[f"/p{i}" for i in range(30)]))
    ctx, _ = _polite_ctx(srv, 0.0, max_pages=25)
    env, pages = selene.run(ctx)
    budget = selene.REQUESTS_PER_PAGE * 25 + selene.CANONICAL_CHECK_MAX + 2
    assert ctx.requests <= budget and len(pages) == 25 and env["facts"]["requests"] == ctx.requests


def test_crawl_delay_setting_bounds():
    assert config.load(base_env(SEO_CRAWL_DELAY_MS=None)).crawl_delay_ms == 1000
    assert config.load(base_env(SEO_CRAWL_DELAY_MS="3000")).crawl_delay_ms == 3000
    assert config.load(base_env(SEO_CRAWL_DELAY_MS="0")).crawl_delay_ms == 0
    for bad in ("3001", "10000", "-1", "1.5", "x"):
        with pytest.raises(RuntimeError):
            config.load(base_env(SEO_CRAWL_DELAY_MS=bad))


def test_robots_crawl_delay_parsing():
    r = robots_mod.parse("User-agent: ZBM-SEO-Audit\nCrawl-delay: 3\n\nUser-agent: *\nCrawl-delay: 1\n"
                         "Crawl-delay: abc\n")
    assert r.crawl_delay("ZBM-SEO-Audit") == 3.0 and r.crawl_delay("Googlebot") == 1.0
    assert r.lines_ignored == 1
    assert robots_mod.parse("Crawl-delay: 4\n").crawl_delay("x") is None


# ---------------------------------------------------------------------------------------------------------- L7

def test_internal_names_are_reworded_never_dropped():
    for line in ("Zero-Day is a port.", "ORCA Publish later.", "The volume adapter contract is not defined in Wave 1.",
                 "Department 28 clientfix executes fixes.", "AEGIS signs off."):
        t = cr.client_text(line)
        assert t and not any(x in t for x in ("Zero-Day", "ORCA", "Wave 1", "clientfix", "Department 28", "AEGIS"))
    v = fx.fresh("full")
    v["report"]["limitations"].append("The volume adapter contract is not defined in Wave 1.")
    t = cr.render(v, "acme", "markdown")[0].decode()
    assert "How often people ask these questions cannot be measured yet" in t


# ---------------------------------------------------------------------------------------------------------- L8

@pytest.mark.local_http
@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_a_canonical_check_our_request_could_not_read_surfaces_as_inconclusive(tmp_path, srv, fmt):
    install_site(srv, robots="User-agent: *\nAllow: /\nUser-agent: ZBM-SEO-Audit\nDisallow: /preferred\n")
    srv.html("site.test", "/", home_html(canonical="http://site.test/preferred"))
    h = Harness(tmp_path, ports=_ports(srv))
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    a = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http"}), 201)
    assert a["report"]["report_version"] == "seo-audit-report/2"
    t = h.ok(h.post(f"/tenants/zbm/audits/{a['audit_id']}/client-report", {"request_id": rid(), "format": fmt},
                    caller="dashboard", andre=True))["content"]
    assert "Inconclusive (not reported as findings)" in t
    assert "1 canonical link target(s) could not be checked" in t
    assert "Canonical target does not answer 200" not in t


def test_no_inconclusive_block_when_nothing_was_inconclusive():
    m = cr.build(fx.fresh("full"), "acme")
    assert all(s["inconclusive"] == [] for s in m["sections"])
    assert "Inconclusive" not in cr.render(fx.fresh("full"), "acme", "markdown")[0].decode()


# ---------------------------------------------------------------------------------------------- schema version

def test_both_report_versions_render_and_others_refuse():
    v = fx.fresh("full")
    v["report"]["report_version"] = "seo-audit-report/2"
    v["report"]["discovered_paths"] = ["/team"]
    assert cr.build(v, "acme")["discovered_pages"] == ["/team"]
    v["report"]["report_version"] = "seo-audit-report/3"
    with pytest.raises(cr.ReportInputError):
        cr.build(v, "acme")


# ================================================================ re-review of PR 47 (0cf5b3c)

# ---------------------------------------------------------------- 1. llms.txt is absent only on 404 / 410

@pytest.mark.local_http
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_sitemap_404_with_llms_refused_or_failing_is_partial(srv, status):
    install_site(srv, robots="User-agent: *\nAllow: /\n", llms=None)
    srv.text("site.test", "/sitemap.xml", "x", status=404)
    srv.text("site.test", "/llms.txt", "x", status=status)
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    env = delia.run(ctx)
    assert env["outcome"] == "PARTIAL" and env["reason"] == "LLMS_TXT_UNREAD"
    assert env["facts"]["llms_txt"] == {"state": "unreadable", "status": status}
    assert "LLMS_TXT_ABSENT" not in {f["code"] for f in env["findings"]}


@pytest.mark.local_http
@pytest.mark.parametrize("status", [404, 410])
def test_llms_404_and_410_are_absent(srv, status):
    install_site(srv, robots="User-agent: *\nAllow: /\n", llms=None)
    srv.text("site.test", "/llms.txt", "x", status=status)
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    env = delia.run(ctx)
    assert env["outcome"] == "OK" and env["facts"]["llms_txt"]["state"] == "absent"


# ---------------------------------------------------------------- 2. one overall deadline over every site request

def _deadline_ctx(srv, deadline, reserve=0.0, delay=1.0, max_pages=10):
    ctx, c = _polite_ctx(srv, delay, max_pages=max_pages)
    ctx.deadline, ctx.fetch_reserve_s = deadline, reserve
    return ctx, c


@pytest.mark.local_http
def test_no_request_starts_that_could_end_after_the_deadline(srv):
    install_site(srv)
    srv.html("site.test", "/", _links(*[f"/p{i}" for i in range(9)]))
    ctx, c = _deadline_ctx(srv, deadline=4.2, reserve=0.5)
    s_env, pages = selene.run(ctx)
    d_env = delia.run(ctx)
    assert ctx.deadline_hit and ctx.requests == len(srv.seen)
    assert ctx.last_request + ctx.fetch_reserve_s <= ctx.deadline       # the last request could still finish in time
    # discovery is optional scope: Selene stays OK, and the cut is recorded and named in the client report
    assert s_env["outcome"] == "OK" and s_env["facts"]["discovery_stopped"] == "time_budget"
    assert s_env["facts"]["deadline_cut"] == {"discovery": 9}
    assert d_env["outcome"] == "PARTIAL" and d_env["reason"] == "AUDIT_DEADLINE" and d_env["facts"]["deadline_reached"]
    assert d_env["facts"]["sitemaps_read"] == 1 and "llms_txt" not in d_env["facts"]


@pytest.mark.local_http
def test_deadline_during_requested_pages_is_partial_with_the_reason(srv):
    install_site(srv)
    for p in ("/a", "/b", "/c"):
        srv.html("site.test", p, home_html(canonical=f"http://site.test{p}"))
    ctx = _ctx(srv, paths=("/", "/a", "/b", "/c"), max_pages=0)
    c = _Clock()
    ctx.crawl_delay_s, ctx.sleep, ctx.monotonic, ctx.deadline = 1.0, c.sleep, c.monotonic, 5.0
    env, pages = selene.run(ctx)
    assert env["outcome"] == "PARTIAL" and env["reason"] == "AUDIT_DEADLINE" and env["facts"]["deadline_reached"]
    assert 0 < len(pages) < 4
    assert all(p["access_diff"]["state"] in ("SAME", "INCONCLUSIVE") for p in pages.values() if "access_diff" in p)


@pytest.mark.local_http
def test_deadline_before_robots_is_failed_not_a_crash(srv):
    install_site(srv)
    ctx, _ = _deadline_ctx(srv, deadline=0.5, reserve=1.0)
    env, pages = selene.run(ctx)
    assert env["outcome"] == "FAILED" and env["reason"] == "AUDIT_DEADLINE" and pages == {} and srv.seen == []


@pytest.mark.local_http
def test_deadline_on_a_canonical_check_is_inconclusive(srv):
    install_site(srv)
    srv.html("site.test", "/", home_html(canonical="http://site.test/preferred"))
    ctx, _ = _deadline_ctx(srv, deadline=1.5, reserve=0.0, max_pages=0)       # robots, page, then no room
    env, _ = selene.run(ctx)
    assert env["facts"]["canonical_checks_inconclusive"] == [{"path": "/", "fetch_state": "DEADLINE"}]
    assert "CANONICAL_TARGET_NOT_200" not in {f["code"] for f in env["findings"]}


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_deadline_reason_reads_plainly_in_the_report(fmt):
    v = fx.fresh("full")
    d = next(e for e in v["report"]["agents"] if e["agent"] == "delia")
    d["outcome"], d["reason"] = "PARTIAL", "AUDIT_DEADLINE"
    t, m = cr.render(v, "acme", fmt)
    assert "time limit was reached before this check finished" in t.decode() and not m["coverage"]["complete"]


def test_deadline_setting_bounds_and_service_wiring(tmp_path):
    assert config.load(base_env()).audit_deadline_s == 90
    assert config.load(base_env(SEO_AUDIT_DEADLINE_SECONDS="30",
                                SEO_FETCH_TIMEOUT_SECONDS="7")).audit_deadline_s == 30
    assert config.load(base_env(SEO_AUDIT_DEADLINE_SECONDS="600")).audit_deadline_s == 600
    for bad in ("29", "601", "0", "x"):
        with pytest.raises(RuntimeError):
            config.load(base_env(SEO_AUDIT_DEADLINE_SECONDS=bad))


@pytest.mark.local_http
def test_the_service_passes_one_deadline_and_a_bounded_reserve(tmp_path, srv, monkeypatch):
    seen = {}
    real = selene.run

    def spy(ctx):
        seen["deadline"], seen["reserve"], seen["delay"] = ctx.deadline, ctx.fetch_reserve_s, ctx.crawl_delay_s
        seen["now"] = ctx.monotonic()
        return real(ctx)
    monkeypatch.setattr(selene, "run", spy)
    install_site(srv)
    h = Harness(tmp_path, ports=_ports(srv), SEO_AUDIT_DEADLINE_SECONDS="40", SEO_FETCH_TIMEOUT_SECONDS="10")
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http"}), 201)
    assert 35 < seen["deadline"] - seen["now"] <= 40 and seen["reserve"] == 20 and seen["delay"] == 0


# ================================================================ re-review of 9d0d2b4

# ---------------------------------------------------------------- 1. a whole worst-case fetch before the deadline

@pytest.mark.parametrize("timeout,deadline", [("15", "90"), ("22", "90"), ("7", "30"), ("60", "240"), ("10", "90")])
def test_timeout_and_deadline_pairs_that_keep_the_promise_start(timeout, deadline):
    s = config.load(base_env(SEO_FETCH_TIMEOUT_SECONDS=timeout, SEO_AUDIT_DEADLINE_SECONDS=deadline))
    assert 2 * s.fetch_timeout_s <= s.audit_deadline_s / 2


@pytest.mark.parametrize("timeout,deadline", [("10", "30"), ("8", "30"), ("23", "90"), ("60", "239"), ("30", "90")])
def test_a_pair_that_could_break_the_deadline_refuses_start_naming_both(timeout, deadline):
    with pytest.raises(RuntimeError) as exc:
        config.load(base_env(SEO_FETCH_TIMEOUT_SECONDS=timeout, SEO_AUDIT_DEADLINE_SECONDS=deadline))
    msg = str(exc.value)
    assert "SEO_FETCH_TIMEOUT_SECONDS" in msg and "SEO_AUDIT_DEADLINE_SECONDS" in msg
    assert f"({timeout})" in msg and f"({deadline})" in msg


@pytest.mark.local_http
@pytest.mark.parametrize("timeout,deadline", [("15", "90"), ("7", "30")])
def test_the_service_reserves_one_full_worst_case_fetch(tmp_path, srv, monkeypatch, timeout, deadline):
    seen = {}
    real = selene.run

    def spy(ctx):
        seen["reserve"] = ctx.fetch_reserve_s
        return real(ctx)
    monkeypatch.setattr(selene, "run", spy)
    install_site(srv)
    h = Harness(tmp_path, ports=_ports(srv), SEO_AUDIT_DEADLINE_SECONDS=deadline, SEO_FETCH_TIMEOUT_SECONDS=timeout)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http"}), 201)
    assert seen["reserve"] == 2 * int(timeout)                      # the fetcher's own hard bound, not a fraction


class _SlowClock(_Clock):
    """Every request takes ``latency`` seconds of (fake) time."""

    def __init__(self, latency):
        super().__init__()
        self.latency = latency


@pytest.mark.local_http
def test_with_slow_responses_no_request_ends_after_the_deadline(srv):
    install_site(srv)
    srv.html("site.test", "/", _links(*[f"/p{i}" for i in range(9)]))
    c = _SlowClock(4.0)
    ctx = _ctx(srv, max_pages=10)
    ctx.crawl_delay_s, ctx.sleep, ctx.monotonic = 1.0, c.sleep, c.monotonic
    ctx.deadline, ctx.fetch_reserve_s = 60.0, 4.0                  # a fetch here takes at most its reserve
    real = ctx.fetcher.fetch, ctx.fetcher.robots

    def slow(fn):
        def run(*a, **kw):
            out = fn(*a, **kw)
            c.t += c.latency
            assert c.t <= ctx.deadline + 1e-9                       # every request ended in time
            return out
        return run
    ctx.fetcher.fetch, ctx.fetcher.robots = slow(real[0]), slow(real[1])
    selene.run(ctx)
    delia.run(ctx)
    assert c.t <= ctx.deadline


# ---------------------------------------------------------------- 2. optional work cut short names the time limit

@pytest.mark.local_http
def test_a_comparison_cut_by_the_deadline_makes_the_area_partial_with_the_reason(srv):
    install_site(srv)
    ctx, _ = _deadline_ctx(srv, deadline=1.5, reserve=0.0, max_pages=0)       # robots, page, then no room
    env, pages = selene.run(ctx)
    assert env["outcome"] == "PARTIAL" and env["reason"] == "AUDIT_DEADLINE"
    assert env["facts"]["deadline_cut"] == {"comparisons": 1}
    assert pages["/"]["access_diff"]["reason"] == "AUDIT_DEADLINE"


@pytest.mark.local_http
def test_a_canonical_check_cut_by_the_deadline_is_recorded_as_such(srv):
    install_site(srv)
    srv.html("site.test", "/", home_html(canonical="http://site.test/preferred"))
    ctx, _ = _deadline_ctx(srv, deadline=1.5, reserve=0.0, max_pages=0)
    env, _ = selene.run(ctx)
    assert env["facts"]["deadline_cut"] == {"canonical_checks": 1, "comparisons": 1}
    assert env["outcome"] == "PARTIAL" and env["reason"] == "AUDIT_DEADLINE"


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_the_report_names_the_time_limit_for_cut_work(fmt):
    v = _with_selene_findings()
    sel = next(e for e in v["report"]["agents"] if e["agent"] == "selene")
    sel["facts"] = {"deadline_cut": {"comparisons": 2, "canonical_checks": 1, "discovery": 4},
                    "canonical_checks_inconclusive": [{"path": "/", "fetch_state": "DEADLINE"},
                                                      {"path": "/a", "fetch_state": "TIMEOUT"}]}
    v["report"]["pages"] = {"/": {"access_diff": "INCONCLUSIVE"}, "/a": {"access_diff": "INCONCLUSIVE"},
                            "/b": {"access_diff": "INCONCLUSIVE"}}
    t = cr.render(v, "acme", fmt)[0].decode()
    assert ("Cut short by the audit time limit: 2 comparison(s) of what our crawler and a browser were served; "
            "1 canonical link check(s); 4 page(s) found by following links were not done.") in t
    assert "1 canonical link target(s) could not be checked" in t          # the TIMEOUT one, not the deadline one
    assert "1 comparison(s) of what our crawler and a browser were served were inconclusive" in t


def test_nothing_cut_means_no_time_limit_line():
    m = cr.build(fx.fresh("full"), "acme")
    assert not any("time limit" in x for s in m["sections"] for x in s["inconclusive"])


# ---------------------------------------------------------------- 3. Delia's reserve counts real response time

@pytest.mark.local_http
def test_slow_responses_shrink_discovery_so_delias_requests_keep_their_room(srv):
    install_site(srv)
    srv.html("site.test", "/", _links(*[f"/p{i}" for i in range(9)]))
    for i in range(9):
        srv.html("site.test", f"/p{i}", home_html(title=f"P{i}", canonical=f"http://site.test/p{i}"))

    def run(latency):
        c = _SlowClock(latency)
        ctx = _ctx(srv, max_pages=10)
        ctx.crawl_delay_s, ctx.sleep, ctx.monotonic = 1.0, c.sleep, c.monotonic
        ctx.deadline, ctx.fetch_reserve_s = 90.0, 20.0
        f0, r0 = ctx.fetcher.fetch, ctx.fetcher.robots

        def slow(fn):
            def go(*a, **kw):
                out = fn(*a, **kw)
                c.t += latency
                return out
            return go
        ctx.fetcher.fetch, ctx.fetcher.robots = slow(f0), slow(r0)
        s_env, _ = selene.run(ctx)
        return s_env, delia.run(ctx), ctx
    fast_s, fast_d, fast = run(0.1)
    slow_s, slow_d, slow = run(3.0)
    # (the wrapped robots() fetch goes through the wrapped fetch(), so it is measured at twice the latency)
    assert fast.per_request_estimate() == 1.0 and slow.per_request_estimate() >= 3.0
    assert len(fast_s["facts"]["pages_discovered"]) > len(slow_s["facts"]["pages_discovered"])
    assert fast_d["outcome"] == "OK" and slow_d["outcome"] == "OK"     # Delia finished in both


# ================================================================ round-3 review of ed79935

# ---------------------------------------------------------------- 1. name resolution inside the fetch deadline

@pytest.fixture(autouse=True)
def _own_dns_gate(monkeypatch):
    """Each test gets its own DNS gate: a deliberately hung lookup keeps its slot until it returns (by design), and
    must not hold a slot another test needs."""
    import threading
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", threading.BoundedSemaphore(fetch_mod.DNS_WORKERS))


def _slow_resolver(seconds, table=("127.0.0.1",), slow_hosts=None):
    import time as _t

    def resolve(host, port):
        if slow_hosts is None or host in slow_hosts:
            _t.sleep(seconds)
        return list(table)
    return resolve


class _BlockedResolver:
    """A resolver that does not answer until the test releases it, recording the hosts asked. No wall-clock bound is
    asserted anywhere (hygiene rule L1): a fetch that returns while this is still blocked did not wait for DNS."""

    def __init__(self, real=None, hosts=None):
        import threading
        self.release, self.asked, self.real, self.hosts = threading.Event(), [], real, hosts

    def __call__(self, host, port):
        self.asked.append(host)
        if self.hosts is None or host in self.hosts:
            self.release.wait(60)
        return self.real(host, port) if self.real else ["127.0.0.1"]


def test_a_hung_resolver_ends_the_fetch_by_its_own_deadline():
    r = _BlockedResolver()
    f = fetch_mod.Fetcher(timeout_s=1, resolver=r, policy=lambda h, ip, p: True)
    try:
        out = f.fetch("http://slow.test/", honor_robots=False)
        assert out.state == "TIMEOUT" and r.asked == ["slow.test"] and not r.release.is_set()
        assert fetch_mod._DNS_GATE.acquire(blocking=False)          # the waiting fetch holds no slot of its own
        fetch_mod._DNS_GATE.release()
    finally:
        r.release.set()


def test_no_lookup_starts_once_the_fetch_deadline_has_passed():
    import time as _t
    r = _BlockedResolver()
    f = fetch_mod.Fetcher(timeout_s=1, resolver=r, policy=lambda h, ip, p: True)
    fetch_mod._BUDGET.deadline = _t.monotonic() - 1
    try:
        with pytest.raises(fetch_mod._Refused) as exc:
            f._resolve("late.test", 80)
        assert exc.value.state == "TIMEOUT" and r.asked == []
    finally:
        fetch_mod._BUDGET.deadline = None
        r.release.set()


def test_a_resolver_that_fails_is_dns_failed_and_frees_its_slot():
    def boom(host, port):
        raise OSError("nxdomain")
    f = fetch_mod.Fetcher(timeout_s=1, resolver=boom, policy=lambda h, ip, p: True)
    assert f.fetch("http://nx.test/", honor_robots=False).state == "DNS_FAILED"
    slots = [fetch_mod._DNS_GATE.acquire(blocking=False) for _ in range(fetch_mod.DNS_WORKERS)]
    assert all(slots)
    for _ in slots:
        fetch_mod._DNS_GATE.release()


class _FakeGate:
    """A DNS gate under test control: the first ``allow`` acquisitions get a slot, later ones find every slot busy.
    Records the time each caller offered to wait (no clock is read, no wall-clock bound is asserted)."""

    def __init__(self, allow=0):
        import threading
        self.allow, self.offered, self.released = allow, [], threading.Event()

    def acquire(self, blocking=True, timeout=None):
        self.offered.append(timeout)
        if self.allow > 0:
            self.allow -= 1
            return True
        return False

    def release(self):
        self.released.set()


def test_a_full_dns_gate_waits_within_the_deadline_then_is_our_resource_limit(monkeypatch):
    gate = _FakeGate(allow=0)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", gate)
    r = _BlockedResolver()
    f = fetch_mod.Fetcher(timeout_s=1, resolver=r, policy=lambda h, ip, p: True)
    try:
        out = f.fetch("http://busy.test/", honor_robots=False)
        assert out.state == "RESOURCE_LIMIT" and "our capacity" in out.detail and r.asked == []
        assert len(gate.offered) == 1 and 0 < gate.offered[0] <= 2 * f.timeout_s     # waited inside the deadline
    finally:
        r.release.set()


def test_a_slot_that_frees_while_waiting_lets_the_fetch_proceed(monkeypatch):
    gate = _FakeGate(allow=1)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", gate)
    f = fetch_mod.Fetcher(timeout_s=1, resolver=lambda h, p: ["93.184.216.34"], policy=lambda h, ip, p: False)
    out = f.fetch("http://free.test/", honor_robots=False)
    assert out.state == "REFUSED_ADDRESS" and gate.released.wait(5)        # resolved, then the SSRF policy decided


def test_an_ip_literal_takes_no_dns_slot_and_still_meets_the_ssrf_policy(monkeypatch):
    gate = _FakeGate(allow=0)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", gate)
    for url in ("http://127.0.0.1/", "http://[::1]/", "http://93.184.215.14/"):
        out = fetch_mod.Fetcher(timeout_s=1, policy=lambda h, ip, p: ip not in ("127.0.0.1", "::1", "93.184.215.14")) \
            .fetch(url, honor_robots=False)
        assert out.state == "REFUSED_ADDRESS" and gate.offered == []     # never waited on our DNS gate


@pytest.mark.local_http
def test_an_ip_literal_is_fetched_without_a_dns_slot(srv, monkeypatch):
    gate = _FakeGate(allow=0)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", gate)
    f = srv.site_fetcher(policy=lambda h, ip, p: True)      # this test's SSRF policy admits the loopback fixture
    asked = []
    real = f.resolver
    f.resolver = lambda h, p: asked.append(h) or real(h, p)
    out = f.fetch("http://127.0.0.1/", honor_robots=False)
    assert out.state == "OK" and asked == ["127.0.0.1"] and gate.offered == []


@pytest.mark.local_http
def test_gate_full_on_the_robots_fetch_is_service_busy_and_never_cached(srv, monkeypatch):
    install_site(srv)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", _FakeGate(allow=0))
    ctx = _ctx(srv, max_pages=0)
    env, pages = selene.run(ctx)
    assert env["outcome"] == "FAILED" and env["reason"] == "SERVICE_BUSY" and env["findings"] == []
    assert pages == {} and ctx.robots is None and ctx.robots_cache == {}
    assert env["facts"]["robots"]["status_class"] == "not_checked"
    cache = {}
    out = ctx.fetcher.fetch("http://site.test/", robots_cache=cache, guard=None)
    assert out.state == "RESOURCE_LIMIT" and cache == {}                  # never "unreachable / disallow all"
    from svc_audits import _skipped
    d = _skipped("delia", "sitemaps_and_llms_txt", env)
    assert d["reason"] == "SERVICE_BUSY"


@pytest.mark.local_http
def test_gate_full_on_a_page_fetch_is_service_busy_and_never_a_finding(srv, monkeypatch):
    install_site(srv)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", _FakeGate(allow=1))      # robots.txt gets the one slot
    ctx = _ctx(srv, paths=("/",), max_pages=0)
    env, pages = selene.run(ctx)
    assert env["outcome"] == "FAILED" and env["reason"] == "SERVICE_BUSY"
    assert env["facts"]["service_busy_pages"] == ["/"]
    assert not {"PAGE_UNREACHABLE", "PAGE_HTTP_ERROR"} & {f["code"] for f in env["findings"]}
    assert pages["/"]["fetch"].state == "RESOURCE_LIMIT"


@pytest.mark.local_http
def test_gate_full_on_delias_requests_is_service_busy_and_never_a_finding(srv, monkeypatch):
    install_site(srv)
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", _FakeGate(allow=0))
    env = delia.run(ctx)
    assert env["outcome"] == "FAILED" and env["reason"] == "SERVICE_BUSY" and env["findings"] == []


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_the_report_says_our_service_was_busy_and_blames_nothing_on_the_site(fmt):
    v = _with_selene_findings(
        finding("PAGE_UNREACHABLE", "high", "measured", "INSUFFICIENT_EVIDENCE", url=U + "/a",
                detail={"fetch_state": "RESOURCE_LIMIT"}), outcome="PARTIAL")
    sel = next(e for e in v["report"]["agents"] if e["agent"] == "selene")
    sel["reason"] = "SERVICE_BUSY"
    t, m = cr.render(v, "acme", fmt)
    t = t.decode()
    assert "Our audit service was busy and could not complete this check" in t
    assert "Page could not be reached" not in t and "did not resolve" not in t
    assert not m["coverage"]["complete"]


@pytest.mark.local_http
def test_a_hung_resolver_on_a_redirect_hop_ends_inside_the_same_fetch(srv):
    install_site(srv)
    srv.redirect("site.test", "/", "http://www.site.test/")
    f = srv.site_fetcher(timeout_s=1)
    r = _BlockedResolver(real=f.resolver, hosts={"www.site.test"})
    f.resolver = r
    try:
        out = f.fetch("http://site.test/", robots_cache={}, honor_robots=True)
        assert out.state == "TIMEOUT" and "www.site.test" in r.asked and not r.release.is_set()
        assert out.redirects and out.redirects[0]["status"] == 301
    finally:
        r.release.set()


@pytest.mark.local_http
def test_with_hung_dns_the_audit_never_requests_past_its_deadline(srv):
    import time as _t
    install_site(srv)
    srv.html("site.test", "/", _links("/a", "/b"))
    f = srv.site_fetcher(timeout_s=1)
    r = _BlockedResolver(real=f.resolver)
    f.resolver = r
    ctx = _ctx(srv, max_pages=10)
    ctx.fetcher, ctx.crawl_delay_s = f, 0.0
    ctx.deadline, ctx.fetch_reserve_s = _t.monotonic() + 7.0, 2.0
    try:
        s_env, _ = selene.run(ctx)
        d_env = delia.run(ctx)
        assert not r.release.is_set()                                  # nothing waited for the hung resolver
        assert ctx.last_request is not None and ctx.last_request + ctx.fetch_reserve_s <= ctx.deadline
        assert s_env["outcome"] in ("FAILED", "PARTIAL", "BLOCKED") and d_env["outcome"] in ("FAILED", "PARTIAL")
    finally:
        r.release.set()


# ---------------------------------------------------------------- 2. Delia's reserve is its true maximum

def test_delias_reserve_is_derived_from_its_own_bounds():
    assert delia.MAX_REQUESTS == 2 * delia.MAX_CHILDREN + 1 == 21
    assert selene.DELIA_RESERVE_REQUESTS == delia.MAX_REQUESTS


@pytest.mark.local_http
def test_delia_never_makes_more_requests_than_its_maximum(srv):
    declared = "".join(f"Sitemap: http://site.test/s{i}.xml\n" for i in range(15))
    install_site(srv, robots="User-agent: *\nAllow: /\n" + declared)
    idx = ("<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
           + "".join(f"<sitemap><loc>http://site.test/c{i}.xml</loc></sitemap>" for i in range(15))
           + "</sitemapindex>")
    for i in range(15):
        srv.text("site.test", f"/s{i}.xml", idx, ctype="application/xml")
        srv.text("site.test", f"/c{i}.xml", "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/"
                 "sitemap/0.9'><url><loc>http://site.test/</loc></url></urlset>", ctype="application/xml")
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    before = ctx.requests
    delia.run(ctx)
    assert 0 < ctx.requests - before <= delia.MAX_REQUESTS
    assert ctx.requests - before == delia.MAX_REQUESTS                # this site uses all of it


# ---------------------------------------------------------------- 3. a page costs up to four requests

@pytest.mark.local_http
def test_one_page_can_cost_four_requests_and_the_constant_says_so(srv):
    from fixture_server import by_ua
    install_site(srv)
    srv.routes[("site.test", "/")] = by_ua(home_html(canonical="http://site.test/preferred"),
                                           home_html(canonical="http://site.test/other"))
    srv.html("site.test", "/preferred", home_html(canonical="http://site.test/preferred"))
    ctx = _ctx(srv, max_pages=0)
    selene.run(ctx)
    assert ctx.requests - 1 == selene.REQUESTS_PER_PAGE == 4          # robots.txt, then the page's four


def test_readme_states_the_startup_refusal_for_short_deadlines():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "30..39 s with the default 10 s timeout" in readme and "21 requests" in readme


def test_a_slot_goes_back_to_the_gate_it_came_from(monkeypatch):
    """Event-driven (no sleeps): a lookup outlives its fetch, the gate is replaced (as after a fork), and the slot
    is released to the gate it was taken from, never to the new one."""
    old, new = _FakeGate(allow=1), _FakeGate(allow=1)
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", old)
    r = _BlockedResolver()
    f = fetch_mod.Fetcher(timeout_s=1, resolver=r, policy=lambda h, ip, p: True)
    import time as _t
    fetch_mod._BUDGET.deadline = _t.monotonic() + 0.05                   # the fetch's own deadline, already short
    try:
        with pytest.raises(fetch_mod._Refused) as exc:
            f._resolve("slow.test", 80)
        assert exc.value.state == "TIMEOUT" and r.asked == ["slow.test"]
    finally:
        fetch_mod._BUDGET.deadline = None
    monkeypatch.setattr(fetch_mod, "_DNS_GATE", new)                      # replaced while the lookup still runs
    r.release.set()                                                       # the lookup now returns
    assert old.released.wait(5) and not new.released.is_set()
