"""War room fixes (ADR 0017 W3-3; devtools/warroom/findings.md WR-F008, WR-F009, WR-F010; replay library
devtools/warroom/replay/seo-py.json): whatever a hostile site serves, the audit ends in a recorded state, and log
path templating sees through invisible characters and letter lookalikes. Real sockets against the in-process
fixture server, as the rest of the suite."""

from __future__ import annotations

import pytest

from agents import logs as logs_mod
from fixture_server import home_html, install_site
from primitives.parse import parse_html
from test_audits import audit, harness, own

pytestmark = pytest.mark.local_http
UNREADABLE = ["javascript:alert(1)", "http://[fec0::1/]/", "http://[ｆe80::１]/", "http://[feff::1?x=1]/"]


# ---------------------------------------------------------------------------------------------- WR-F008

@pytest.mark.parametrize("location", [u for u in UNREADABLE if u.isascii()])     # a header is latin-1 on the wire
def test_wr_f008_a_redirect_httpx_cannot_parse_is_refused_not_raised(srv, location):
    srv.redirect("site.test", "/go", location)
    r = srv.fetcher().fetch(srv.url(path="/go"))
    assert r.state in ("REFUSED_URL", "PROTOCOL_ERROR") and r.body is None


@pytest.mark.parametrize("where", ["/", "/sitemap.xml", "/llms.txt", "/robots.txt"])
def test_wr_f008_audit_completes_whatever_redirect_a_site_serves(tmp_path, srv, where):
    install_site(srv)
    srv.redirect("site.test", where, "javascript:alert(1)")
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h), 201)
    assert a["status"] == "completed"


# ---------------------------------------------------------------------------------------------- WR-F009

@pytest.mark.parametrize("href", UNREADABLE)
def test_wr_f009_an_unreadable_canonical_is_counted_never_kept_as_none(href):
    x = parse_html(home_html(canonical=href), "http://site.test/")
    assert None not in x["canonicals"] and x["canonicals"] == [] and x["canonicals_invalid"] == 1


@pytest.mark.parametrize("href", UNREADABLE)
def test_wr_f009_audit_completes_with_an_invalid_canonical_finding(tmp_path, srv, href):
    install_site(srv, home=home_html(canonical=href))
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h, paths=["/"]), 201)
    assert a["status"] == "completed"
    codes = {f["code"] for e in a["report"]["agents"] for f in e["findings"]}
    assert "CANONICAL_INVALID" in codes and "CANONICAL_MISSING" not in codes


# ---------------------------------------------------------------------------------------------- WR-F010

@pytest.mark.parametrize("bad", ["http://[feff::1/]/", "http://[64:ｆｆ9b::7f00:1]/", "http://[::1"])
def test_wr_f010_invalid_urls_in_robots_and_sitemaps_never_stop_delia(tmp_path, srv, bad):
    index = ("<?xml version='1.0'?><sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
             f"<sitemap><loc>{bad}</loc></sitemap><sitemap><loc>http://site.test/s1.xml</loc></sitemap>"
             "</sitemapindex>")
    install_site(srv, robots=f"User-agent: *\nAllow: /\nSitemap: {bad}\nSitemap: http://site.test/sitemap.xml\n",
                 sitemap=index)
    srv.text("site.test", "/s1.xml", "<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/"
                                     f"0.9'><url><loc>{bad}</loc></url><url><loc>http://site.test/</loc></url>"
                                     "</urlset>", ctype="application/xml")
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h, paths=["/"]), 201)
    delia = next(e for e in a["report"]["agents"] if e["agent"] == "delia")
    assert a["status"] == "completed" and delia["outcome"] in ("OK", "PARTIAL")
    assert delia["facts"]["sitemap_urls"] >= 1


# ---------------------------------------------------------------------------------------------- defence in depth

def test_a_run_that_crashes_is_closed_interrupted_run_failed_not_left_running(tmp_path, srv, monkeypatch):
    from agents import roman
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)

    def boom(*a, **k):
        raise RuntimeError("a bug in an agent, with a secret-looking message sk-should-not-appear")
    monkeypatch.setattr(roman, "run", boom)
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["interrupted_reason"] == "RUN_FAILED" and a["report"] is None
    assert h.ledger.of_type("audit_interrupted")[0]["_payload"]["reason"] == "RUN_FAILED"
    assert h.ledger.of_type("audit_report_recorded") == []
    assert "sk-should-not-appear" not in str(h.ledger.events)
    monkeypatch.undo()
    assert h.ok(audit(h), 201)["status"] == "completed"           # nothing stuck: the next run proceeds


# ---------------------------------------------------------------------------------------------- log templating

@pytest.mark.parametrize("path,template", [
    ("/u/jan‌e%EF%B​C%A0exa‌mple.com", "/u/{email}"),        # zero-width inside a percent escape
    ("/img/ja﻿ne.s​m‍ith.j‌p‌g", "/img/{file}.jpg"),  # invisible characters in a file name
    ("/u/ja­ne%20a­t%20example%20d­ot%20com", "/u/{email}"),    # soft hyphens in "at ... dot"
    ("/files/јαne-dοe-rеsumе.pԁf", "/files/{file}.pdf"),   # lookalikes, even in the ext
    ("/u/јane[αT]εхaмple.сoм", "/u/{email}"),         # Greek alpha in [at]
    ("/call/５５５-１２３-４５６７", "/call/{number}"),
    ("/blog/how-to-rank", "/blog/how-to-rank"),
    ("/о-нас/контакты",       # a real Russian path
     "/о-нас/контакты"),     # is kept as written
    ("/café/menu", "/café/menu"),
])
def test_templating_sees_through_invisible_characters_and_lookalikes(path, template):
    assert logs_mod.template_path(path) == template


def test_the_shared_fold_is_the_one_every_service_carries():
    import hashlib
    from pathlib import Path
    here = Path(__file__).resolve().parents[1] / "src" / "lookalikes.py"
    other = Path(__file__).resolve().parents[2] / "service-py" / "src" / "lookalikes.py"
    assert hashlib.sha256(here.read_bytes()).hexdigest() == hashlib.sha256(other.read_bytes()).hexdigest()
