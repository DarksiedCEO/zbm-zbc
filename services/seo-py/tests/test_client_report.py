"""The client SEO audit report (client_report.py, svc_reports.py, devtools/render_report.py; ADR 0017 "Client report").

Golden files for Markdown and HTML on fixture audits (every section, nothing connected, no findings, hostile crawled
text, very long inputs); escaping checked structurally (HTML tags and attributes, Markdown outside code spans); no
personal data, prompt, answer or query text in any output; no score or superiority wording; determinism (same audit,
same bytes, whatever the key order); the API route (Andre only, tenant-scoped, record-first, idempotent, kill
switches, retention change refused on replay) over real audits against the fixture server and a fake Google; the CLI.

Regenerate the golden files after an intended change with ``UPDATE_GOLDEN=1`` and review the diff.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path

import pytest

import client_report as cr
import config as config_mod
import report_fixtures as fx
from fixture_server import install_site
from helpers import ANDRE, CALLERS, SERVICE_TOKEN, Harness, base_env, rid
from ports import Ports, ProviderAnswer

GOLDEN = Path(__file__).resolve().parent / "golden" / "client_report"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"
FORMATS = (("markdown", "md"), ("html", "html"))
ALL = [(n, f) for n in sorted(fx.FIXTURES) for f, _ in FORMATS]


def render(name: str, fmt: str, **kw) -> str:
    data, _ = cr.render(fx.fresh(name), "acme", fmt, **kw)
    return data.decode("utf-8")


# ---------------------------------------------------------------------------------------------- golden files

@pytest.mark.parametrize("name,fmt,ext", [(n, f, e) for n in sorted(fx.FIXTURES) for f, e in FORMATS])
def test_golden(name, fmt, ext):
    data, _ = cr.render(fx.fresh(name), "acme", fmt)
    path = GOLDEN / f"{name}.{ext}"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert data == path.read_bytes(), f"{path.name} differs from its golden file (UPDATE_GOLDEN=1 to regenerate)"


# ---------------------------------------------------------------------------------------------- determinism

def _reorder(x):
    if isinstance(x, dict):
        return {k: _reorder(x[k]) for k in reversed(list(x))}
    if isinstance(x, list):
        return [_reorder(v) for v in x]
    return x


@pytest.mark.parametrize("name,fmt", ALL)
def test_same_audit_same_bytes_whatever_the_key_order(name, fmt):
    a = cr.render(fx.fresh(name), "acme", fmt)[0]
    b = cr.render(fx.fresh(name), "acme", fmt)[0]
    c = cr.render(_reorder(fx.fresh(name)), "acme", fmt)[0]
    assert a == b == c


def test_findings_order_in_the_report_does_not_change_the_document():
    v = fx.fresh("full")
    for e in v["report"]["agents"]:
        e["findings"] = list(reversed(e["findings"]))
    assert cr.render(v, "acme", "html")[0] == cr.render(fx.fresh("full"), "acme", "html")[0]


# ---------------------------------------------------------------------------------------------- honesty

def _section(text: str, title: str, md: bool) -> str:
    if md:
        m = re.search(r"^## " + re.escape(title) + r"\n(.*?)(?=^## )", text, re.S | re.M)
    else:
        m = re.search(r"<h2>" + re.escape(title) + r"</h2>(.*?)</section>", text, re.S)
    assert m, title
    return m.group(1)


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_nothing_connected_is_never_shown_clean(fmt):
    t = render("not_connected", fmt)
    md = fmt == "markdown"
    expect = {"Crawlability": "NOT CONNECTED", "Sitemaps and llms.txt" if not md else "Sitemaps and ` llms.txt `":
              "NOT COVERED", "Content structure": "NOT COVERED", "Entity consistency": "NOT COVERED",
              "Search performance (Google Search Console)": "NOT CONNECTED",
              "AI visibility (answer engines)": "NOT COVERED"}
    if not md:
        expect = {k.replace("` llms.txt `", "llms.txt"): v for k, v in expect.items()}
    for title, label in expect.items():
        body = _section(t, title, md)
        assert label in body, (title, label)
        assert cr.NOT_CLEAN.replace(".", "\\.") in body or cr.NOT_CLEAN in body, title
        assert "No findings for the parts" not in body, title
    assert "Google Search Console is not connected to this service." in t
    assert "No set of market questions was chosen" in t
    assert "Prompt volume: " in t and "NOT CONNECTED" in t
    for nc in ("Google Search Console (your own search data).", "Web fetching: no page could be read."):
        assert nc in t


def test_empty_findings_are_stated_per_checked_area_and_unbound_gsc_is_not_connected():
    t = render("empty", "markdown")
    assert t.count("No findings in this area.") == 4
    gsc = _section(t, "Search performance (Google Search Console)", True)
    assert "NOT CONNECTED" in gsc and "No Search Console property is linked" in gsc and cr.NOT_CLEAN in gsc
    ai = _section(t, "AI visibility (answer engines)", True)
    assert "NOT CONNECTED" in ai and "| ` perplexity ` | NOT CONNECTED |" in ai


_SCORE_OK = ("not a score", "no overall score", "No single visibility score is produced")
_BANNED = re.compile(r"guarantee|#1\b|number one|better than|outperform|rank first|top of google|best[- ]in[- ]class|"
                     r"/100\b|\bpowered by\b|\bgrade [a-f]\b|\bhealth score\b", re.I)


@pytest.mark.parametrize("name,fmt", ALL)
def test_no_score_and_no_superiority_or_guarantee_wording(name, fmt):
    t = render(name, fmt)
    rest = t
    for ok in _SCORE_OK:
        rest = rest.replace(ok, "")
    assert not re.search(r"\bscore\b", rest, re.I)
    assert not _BANNED.search(t)
    assert "does not promise rankings, traffic or citations" in t


def test_estimated_and_inferred_say_so_with_their_basis():
    t = render("full", "markdown")
    assert "| Average position | 7.5 | ESTIMATED (assumption: the impression-weighted mean" in t
    assert "INFERRED (drawn by a stated rule from what was observed, not measured directly: every URL carries the " \
           "same lastmod" in t
    h = render("hostile", "markdown")
    assert "ESTIMATED (an estimate; the assumption is not part of this finding's record, so treat it as " \
           "unverified)" in h


def test_every_finding_has_severity_evidence_rule_and_fix():
    t = render("full", "markdown")
    blocks = re.findall(r"^### .*?(?=^### |^## )", t, re.S | re.M)
    findings = [b for b in blocks if "**Rule:**" in b]
    assert len(findings) == 14
    for b in findings:
        assert re.search(r"\*\*Severity:\*\* (critical|high|medium|low|info)", b)
        assert re.search(r"\*\*Evidence:\*\* (OBSERVED|INFERRED|ESTIMATED|MODELED|UNKNOWN) \(", b)
        assert re.search(r"\*\*Rule:\*\* ` [A-Z0-9_]+ `", b) and "**Fix:**" in b


def test_operational_spend_findings_stay_out_of_the_client_report():
    for fmt, _ in FORMATS:
        t = render("full", fmt)
        assert "ENGINE_COST_ABOVE_RESERVE" not in t and "9.99" not in t and "spend" not in t.lower()


def test_severity_order_within_a_section():
    t = render("full", "markdown")
    crawl = _section(t, "Crawlability", True)
    sev = re.findall(r"\*\*Severity:\*\* (\w+)", crawl)
    assert sev == sorted(sev, key=cr.SEVERITY_ORDER.index) and sev[0] == "high"


# ---------------------------------------------------------------------------------------------- escaping

class _Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.text = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [(tag, k, v) for k, v in attrs]

    def handle_data(self, data):
        self.text.append(data)


ALLOWED_TAGS = {"html", "head", "meta", "title", "style", "body", "main", "div", "header", "h1", "h2", "h3", "h4",
                "p", "dl", "dt", "dd", "code", "section", "table", "thead", "tbody", "tr", "th", "td", "span", "ul",
                "li", "strong", "a", "footer"}
ALLOWED_ATTRS = {("meta", "charset"), ("meta", "name"), ("meta", "content"), ("meta", "http-equiv"),
                 ("html", "lang"), ("header", "class"), ("p", "class"), ("dl", "class"), ("span", "class"),
                 ("div", "class"), ("section", "class"), ("section", "id"), ("a", "href"), ("a", "rel"),
                 ("td", "colspan")}


@pytest.mark.parametrize("name", sorted(fx.FIXTURES))
def test_html_carries_only_our_markup(name):
    v = fx.fresh(name)
    t = render(name, "html")
    p = _Tags()
    p.feed(t)
    assert set(p.tags) <= ALLOWED_TAGS, set(p.tags) - ALLOWED_TAGS
    assert {(tag, k) for tag, k, _ in p.attrs} <= ALLOWED_ATTRS
    pages = {f"{v['report']['scheme']}://{v['report']['domain']}{x}" for x in v["report"]["paths"]}
    hrefs = [val for tag, k, val in p.attrs if k == "href"]
    assert hrefs and set(hrefs) <= pages               # links only to the audited pages on the registered domain
    assert p.tags.count("style") == 1 and "script" not in p.tags and "img" not in p.tags
    assert "@import" not in t and "url(" not in t
    assert "Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'" in t


def test_hostile_text_is_inert_in_html():
    t = render("hostile", "html")
    for raw in ("<script>", "<img", "<!-- c -->", "<b>"):
        assert raw not in t
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in t        # quoted as data, escaped
    assert "javascript:" not in "".join(v for _t, k, v in (lambda p: (p.feed(t), p.attrs)[1])(_Tags()) if v)


def _outside_code_spans(md: str) -> str:
    out, i = [], 0
    while i < len(md):
        if md[i] == "`":
            j = i
            while j < len(md) and md[j] == "`":
                j += 1
            fence = md[i:j]
            k = md.find(fence, j)
            while k != -1 and k + len(fence) < len(md) and md[k + len(fence)] == "`":
                k = md.find(fence, k + len(fence) + 1)
            if k == -1:
                out.append(md[i:])
                break
            i = k + len(fence)
            continue
        out.append(md[i])
        i += 1
    return "".join(out)


@pytest.mark.parametrize("name", sorted(fx.FIXTURES))
def test_markdown_has_no_live_markup_outside_code_spans(name):
    raw = render(name, "markdown")
    for line in raw.splitlines():
        if line.startswith("#"):
            assert re.match(r"#{1,3} \S", line), line          # only our own headings start a line with #
    t = _outside_code_spans(raw)
    t = t.replace("\\\\", "")
    assert not re.search(r"(?<!\\)<", t)                       # no raw HTML
    assert not re.search(r"(?<!\\)\[", t)                      # no link or image opener
    assert not re.search(r"(?<!\\)!(?=\\?\[)", t)
    assert "://" not in t and "javascript:" not in t.lower() and not re.search(r"\bwww\.", t)
    assert "@" not in t                                        # no autolinked e-mail


def test_markdown_escape_function_neutralises_each_construct():
    for raw in ("[x](http://e.test)", "![i](http://e.test/i.png)", "<b>x</b>", "**b** _i_ ~s~", "# h", "a | b",
                "www.e.test", "jane@e.test", "`tick` ``two``", "https://e.test/a|b"):
        out = _outside_code_spans(cr.md(raw)).replace("\\\\", "")
        assert not re.search(r"(?<![\\])[\[<*_~#|]", out), (raw, cr.md(raw))
        assert "://" not in out and "@" not in out and "www." not in out


# ---------------------------------------------------------------------------------------------- retention / PII

_NEEDLES = fx.PII + (fx.PROMPT_NEEDLE, "PROMPTNEEDLE", fx.ANSWER_NEEDLE, fx.QUERY_NEEDLE, "(562) 248-6617",
                     "(562) 555-0100", "jane")


@pytest.mark.parametrize("name,fmt", ALL)
def test_no_personal_data_prompt_answer_or_query_text(name, fmt):
    t = render(name, fmt)
    for needle in _NEEDLES:
        assert needle not in t, needle
    assert "answers_sha256" not in t and "e" * 64 not in t


def test_crawled_urls_are_templates_and_request_paths_stay_as_written():
    t = render("hostile", "markdown")
    assert "` /{slug}/{slug}/{email}/{number} `" in t
    assert "` {offsite} (evil.test) `" in t and "` {unreadable} `" in t
    f = render("full", "markdown")
    assert "**Pages audited (3):** ` / `, ` /about `, ` /pricing `" in f
    assert "final url: ` /{slug} `" in f                       # /about-us: a hyphenated slug never kept


def test_expired_search_console_breakdowns_are_not_rendered():
    for fmt, _ in FORMATS:
        t = cr.render(fx.with_expired_gsc(), "acme", fmt)[0].decode()
        assert "past their retention period" in t
        assert "By query class" not in t and "/blog/{slug}" not in t and "1,400" in t


# ---------------------------------------------------------------------------------------------- bounds

@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_very_long_inputs_are_bounded(fmt):
    t = render("long", fmt)
    assert len(t.encode()) < 200_000
    assert "and 5 more" in t                                    # 25 pages affected, 20 listed
    assert "Evidence for 395 more instances is in the audit record." in t
    assert "x" * (cr.TEXT_MAX + 1) not in t and "m" * 1600 not in t and "l" * 700 not in t
    assert "` {offsite} (" + "h" * 60 + ".example) `" in t or "{offsite} (" + "h" * 60 + ".example)" in t
    assert "100,000" in t


# ---------------------------------------------------------------------------------------------- input refusal

def test_refuses_what_is_not_a_completed_audit_of_this_tenant():
    with pytest.raises(cr.ReportInputError):
        cr.render(fx.fresh("full"), "globex", "html")
    for mutate in (lambda v: v.update(status="interrupted"), lambda v: v.update(report=None),
                   lambda v: v["report"].update(report_version="seo-audit-report/0"),
                   lambda v: v["report"].update(tenant_id="globex"), lambda v: v["report"].update(audit_id="x"),
                   lambda v: v["report"].update(started_at="yesterday"),
                   lambda v: v["report"].update(finished_at="2026-10-24T09:00:00"),
                   lambda v: v["report"].update(domain="<evil>"), lambda v: v["report"].update(paths=["no-slash"]),
                   lambda v: v.update(report_sha256="nope")):
        v = fx.fresh("full")
        mutate(v)
        with pytest.raises(cr.ReportInputError):
            cr.render(v, "acme", "html")
    with pytest.raises(cr.ReportInputError):
        cr.render(fx.fresh("full"), "acme", "pdf")


# ---------------------------------------------------------------------------------------------- white label

def test_branding_is_configuration_and_validated():
    b = cr.Branding("Acme Partners", "#123456")
    for fmt, _ in FORMATS:
        t = render("full", fmt, branding=b)
        assert "Prepared by Acme Partners" in t and "Z Best Media" not in t
    assert "--accent:#123456" in render("full", "html", branding=b)
    for bad in (("", "#123456"), ("<b>", "#123456"), ("x" * 61, "#123456"), ("Acme", "red"),
                ("Acme", "#12345g"), ("Acme", "#123456;}body{")):
        with pytest.raises(ValueError):
            cr.Branding(*bad)
    s = config_mod.load(base_env(SEO_REPORT_BRAND_NAME="Acme Partners", SEO_REPORT_BRAND_ACCENT="#0a0b0c"))
    assert s.report_branding == cr.Branding("Acme Partners", "#0a0b0c")
    assert config_mod.load(base_env()).report_branding == cr.DEFAULT_BRANDING
    with pytest.raises(RuntimeError, match="SEO_REPORT_BRAND"):
        config_mod.load(base_env(SEO_REPORT_BRAND_ACCENT="javascript:alert(1)"))


# ---------------------------------------------------------------------------------------------- the API


class Engine:
    connected = True

    def ask(self, prompt, model=None):
        return ProviderAnswer("ANSWER", "ANSWERTEXT Z Best Media in Long Beach, call 555 010 0199.",
                              ["http://site.test/", "https://yelp.com/biz/x"], "m", "1")


def _harness(tmp_path, srv, data_dir=None, engines=None, **env):
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    if engines:
        ports.engines.update(engines)
    return Harness(tmp_path, data_dir=data_dir, ports=ports, **env)


def _audit(h, **body):
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    b = {"request_id": rid(), "domain": "site.test", "scheme": "http", "paths": ["/", "/about"], **body}
    return h.ok(h.post("/tenants/zbm/audits", b), 201)


def _report(h, tid, aid, fmt="html", request_id=None, caller="dashboard", andre=True, tenant=None):
    return h.post(f"/tenants/{tid}/audits/{aid}/client-report", {"request_id": request_id or rid(), "format": fmt},
                  caller=caller, andre=andre, tenant=tenant)


@pytest.mark.local_http
def test_route_renders_a_real_audit_recorded_first_with_its_hash(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv)
    a = _audit(h)
    r = h.ok(_report(h, "zbm", a["audit_id"]))
    data = r["content"].encode()
    assert r["document_sha256"] == hashlib.sha256(data).hexdigest() and r["bytes"] == len(data)
    assert r["audit_report_sha256"] == a["report_sha256"] and r["media_type"] == "text/html; charset=utf-8"
    assert r["renderer_version"] == cr.RENDERER_VERSION and r["format"] == "html"
    ev = h.ledger.of_type("client_report_rendered")
    assert len(ev) == 1
    p = ev[0]["_payload"]
    assert p["document_sha256"] == r["document_sha256"] and p["audit_report_sha256"] == a["report_sha256"]
    assert p["tenant_id"] == "zbm" and p["audit_id"] == a["audit_id"] and p["format"] == "html"
    assert p["branding_sha256"] == cr.DEFAULT_BRANDING.digest()
    assert "content" not in json.dumps(p) and "site.test" not in json.dumps(p)
    st = {(e["event_type"], e["status"]) for e in h.ok(h.get("/audit/evidence", caller="compliance_38"))["evidence"]}
    assert ("client_report_rendered", "committed") in st
    # the service's document is exactly the deterministic render of the stored audit view
    view = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    assert cr.render(view, "zbm", "html")[0] == data
    md = h.ok(_report(h, "zbm", a["audit_id"], fmt="markdown"))
    assert md["content"].startswith("# SEO technical audit: ` site.test `")
    listed = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}/client-reports"))
    assert sorted(x["format"] for x in listed) == ["html", "markdown"] and all("content" not in x for x in listed)
    assert h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}/client-reports", caller="compliance_38")) == listed
    h.refused(h.get(f"/tenants/zbm/audits/{a['audit_id']}/client-reports", caller="hub", tenant="zbm"), 403)


@pytest.mark.local_http
def test_route_is_andre_only(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv)
    aid = _audit(h)["audit_id"]
    h.refused(_report(h, "zbm", aid, andre=False), 403, "ANDRE_APPROVAL_REQUIRED")
    h.refused(_report(h, "zbm", aid, caller="seo_agent", andre=False), 403, "CALLER_NOT_ALLOWED")
    h.refused(_report(h, "zbm", aid, caller="hub", andre=False, tenant="zbm"), 403, "CALLER_NOT_ALLOWED")
    h.refused(_report(h, "zbm", aid, caller="compliance_38", andre=False), 403, "CALLER_NOT_ALLOWED")
    bad = h.client.post(f"/seo/v1/tenants/zbm/audits/{aid}/client-report", json={"request_id": rid(), "format": "html"},
                        headers={"Authorization": f"Bearer {SERVICE_TOKEN}", "X-SEO-Caller-Token": CALLERS["dashboard"],
                                 "X-Andre-Approval-Token": ANDRE[::-1]})
    h.refused(bad, 403, "ANDRE_APPROVAL_INVALID")
    h.refused(h.post(f"/tenants/zbm/audits/{aid}/client-report", {"request_id": rid(), "format": "pdf"},
                     caller="dashboard", andre=True), 422)
    assert h.ledger.of_type("client_report_rendered") == []


@pytest.mark.local_http
def test_route_is_tenant_scoped_with_the_same_404_as_a_missing_audit(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv)
    aid = _audit(h)["audit_id"]
    h.tenant("acme", domains=("acme.example",))
    wrong = h.refused(_report(h, "acme", aid), 404, "AUDIT_NOT_FOUND")
    missing = h.refused(_report(h, "acme", "seo-aud-" + "0" * 40), 404, "AUDIT_NOT_FOUND")
    assert wrong == missing
    h.refused(_report(h, "nobody", aid), 404, "TENANT_NOT_FOUND")
    h.refused(h.get(f"/tenants/acme/audits/{aid}/client-reports"), 404, "AUDIT_NOT_FOUND")
    assert h.ledger.of_type("client_report_rendered") == []


@pytest.mark.local_http
def test_route_record_first_idempotent_and_switches(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv)
    aid = _audit(h)["audit_id"]
    req = rid()
    h.ledger.fail = True
    h.refused(_report(h, "zbm", aid, request_id=req), 503, "LEDGER_UNAVAILABLE")      # no record, no document
    assert h.svc.client_reports == {}
    h.ledger.fail = False
    first = h.ok(_report(h, "zbm", aid, request_id=req))
    again = h.ok(_report(h, "zbm", aid, request_id=req))
    assert again == first and len(h.ledger.of_type("client_report_rendered")) == 1
    h.refused(_report(h, "zbm", aid, fmt="markdown", request_id=req), 409, "REQUEST_ID_REUSED")
    h.ok(h.switch("capability:reports"))
    h.refused(_report(h, "zbm", aid), 403, "KILLED_CAPABILITY")
    h.ok(h.switch("capability:reports", engaged=False, andre=True))
    h.ok(h.switch("tenant:zbm"))
    h.refused(_report(h, "zbm", aid), 403, "KILLED_TENANT")
    h.ok(h.switch("tenant:zbm", engaged=False, andre=True))
    h.ok(h.switch("capability:audit"))
    h.refused(_report(h, "zbm", aid), 403, "KILLED_CAPABILITY")
    h.ok(h.switch("capability:audit", engaged=False, andre=True))
    h.ok(_report(h, "zbm", aid))


@pytest.mark.local_http
def test_route_refuses_an_audit_that_did_not_complete(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    h.ledger.fail = False
    real_run = h.svc._run_audit
    h.svc._run_audit = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))     # closed interrupted RUN_FAILED
    a = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                             "paths": ["/"]}), 201)
    h.svc._run_audit = real_run
    assert a["status"] == "interrupted"
    h.refused(_report(h, "zbm", a["audit_id"]), 409, "AUDIT_NOT_COMPLETED")


@pytest.mark.local_http
def test_route_survives_restart_and_replays(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv, data_dir=str(tmp_path / "data"))
    aid = _audit(h)["audit_id"]
    req = rid()
    first = h.ok(_report(h, "zbm", aid, request_id=req, fmt="markdown"))
    h2 = h.restart()
    assert h2.ok(_report(h2, "zbm", aid, request_id=req, fmt="markdown")) == first
    assert len(h2.ok(h2.get(f"/tenants/zbm/audits/{aid}/client-reports"))) == 1


@pytest.mark.local_http
def test_answer_and_prompt_text_never_reach_the_document(tmp_path, srv):
    install_site(srv)
    h = _harness(tmp_path, srv, engines={"openai": Engine()}, SEO_PROBE_SAMPLES="3")
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test"]}, andre=True))
    ps = h.ok(h.post("/tenants/zbm/prompt-sets", {"request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"],
                                                  "prompts": ["PROMPTTEXT who recovers revenue", "second question"],
                                                  "engines": ["openai"]}), 201)
    a = h.ok(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": "site.test", "scheme": "http",
                                             "paths": ["/"], "prompt_set_id": ps["prompt_set_id"]}), 201)
    for fmt in ("html", "markdown"):
        t = h.ok(_report(h, "zbm", a["audit_id"], fmt=fmt))["content"]
        assert "PROMPTTEXT" not in t and "who recovers" not in t and "second question" not in t
        assert "ANSWERTEXT" not in t and "Long Beach" not in t and "555" not in t.replace("#555", "")
        assert "Question 1" in t and "Question 2" in t


def test_search_console_queries_never_reach_the_document_and_retention_changes_are_refused_on_replay(tmp_path):
    from gsc_fakes import FakeGoogle
    from test_gsc import PROP, bind, gsc_harness
    fake = FakeGoogle()
    h = gsc_harness(tmp_path, fake, SEO_GSC_RETENTION_DAYS="30")
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    aid = h.ok(h.post("/tenants/acme/audits", {"request_id": rid(), "domain": "acme.test", "paths": ["/"]},
                      andre=True), 201)["audit_id"]
    req = rid()
    first = h.ok(_report(h, "acme", aid, request_id=req, fmt="markdown"))
    t = first["content"]
    assert "**By query class**" in t and "| brand |" in t
    for q, *_ in fake.queries:
        assert q not in t, q
    for u, *_ in fake.pages:
        assert u not in t and "jane" not in t
    h.clock.advance(days=31)
    h.refused(_report(h, "acme", aid, request_id=req, fmt="markdown"), 409, "REPORT_CHANGED")
    later = h.ok(_report(h, "acme", aid, fmt="markdown"))
    assert later["document_sha256"] != first["document_sha256"] and "past their retention period" in later["content"]
    assert "**By query class**" not in later["content"]


# ---------------------------------------------------------------------------------------------- the CLI

def _cli():
    path = Path(__file__).resolve().parents[1] / "devtools" / "render_report.py"
    spec = importlib.util.spec_from_file_location("render_report_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_preview_is_marked_and_refuses_what_it_cannot_render(tmp_path, capsys):
    cli = _cli()
    src = tmp_path / "audit.json"
    src.write_text(json.dumps(fx.fresh("full")))
    out = tmp_path / "r.html"
    assert cli.main(["preview", str(src), "--tenant", "acme", "--format", "html", "--out", str(out)]) == 0
    t = out.read_text()
    assert "PREVIEW. Rendered offline and not recorded on the ledger." in t
    meta = json.loads(capsys.readouterr().out)
    assert meta["recorded"] is False and meta["document_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert cli.main(["preview", str(src), "--tenant", "acme", "--format", "markdown", "--out", str(out),
                     "--brand-name", "Acme Partners", "--brand-accent", "#123456"]) == 0
    assert "Prepared by Acme Partners." in out.read_text() and out.read_text().startswith("> **PREVIEW.**")
    assert cli.main(["preview", str(src), "--tenant", "globex", "--format", "html", "--out", str(out)]) == 2
    bad = fx.fresh("full")
    bad["status"] = "running"
    src.write_text(json.dumps(bad))
    assert cli.main(["preview", str(src), "--tenant", "acme", "--format", "html", "--out", str(out)]) == 2
    assert "refused" in capsys.readouterr().err


@pytest.mark.local_http
def test_cli_fetch_goes_through_the_recorded_route(tmp_path, srv, capsys):
    install_site(srv)
    h = _harness(tmp_path, srv)
    aid = _audit(h)["audit_id"]
    files = {}
    for name, tok in (("service", SERVICE_TOKEN), ("caller", CALLERS["dashboard"]), ("andre", ANDRE)):
        files[name] = tmp_path / f"{name}.tok"
        files[name].write_text(tok + "\n")
    out = tmp_path / "r.md"
    args = ["fetch", "--api", "http://127.0.0.1:8500", "--tenant", "zbm", "--audit", aid, "--format", "markdown",
            "--out", str(out), "--service-token-file", str(files["service"]), "--caller-token-file",
            str(files["caller"]), "--andre-token-file", str(files["andre"]), "--request-id", rid()]
    cli = _cli()
    assert cli.main(args, client=h.client) == 0
    meta = json.loads(capsys.readouterr().out)
    assert meta["recorded"] is True and "PREVIEW" not in out.read_text()
    ev = h.ledger.of_type("client_report_rendered")
    assert [e["_payload"]["document_sha256"] for e in ev] == [meta["document_sha256"]]
    assert hashlib.sha256(out.read_bytes()).hexdigest() == meta["document_sha256"]
    files["andre"].write_text("x" * 40)
    assert cli.main(args[:-1] + [rid()], client=h.client) == 2
    assert "403" in capsys.readouterr().err
    other = copy.copy(args)
    other[other.index(aid)] = "not-an-id"
    assert cli.main(other, client=h.client) == 2


# ---------------------------------------------------------------------------------------------- AEGIS 3abca49

from envelope import envelope, finding  # noqa: E402

_NOT_OBSERVED = [o for o in cr.STATUS if o not in ("OK", "PARTIAL")]      # None = the area is missing entirely
_MAY_CARRY_FINDINGS = {"BLOCKED", "FAILED", "INSUFFICIENT_EVIDENCE", "PARTIAL", "OK"}


def _with_crawl(outcome, findings=True):
    """``full()`` with Selene's envelope replaced by one of the given outcome (None: the envelope removed)."""
    v = fx.fresh("full")
    agents = [e for e in v["report"]["agents"] if e["agent"] != "selene"]
    if outcome is not None:
        fl = [finding("PAGE_BLOCKED_FOR_OUR_CRAWLER", "info", "measured", "INSUFFICIENT_EVIDENCE",
                      url="https://acme.test/", detail={"note": "x"})] \
            if findings and outcome in _MAY_CARRY_FINDINGS else []
        agents.insert(0, envelope("selene", "crawlability", outcome, fl, reason="KILLED_GLOBAL" if outcome == "KILLED"
                                  else None, methodology="Fetched robots.txt.", limitations=["One location."]))
    v["report"]["agents"] = agents
    return v


@pytest.mark.parametrize("fmt", ["markdown", "html"])
@pytest.mark.parametrize("outcome", _NOT_OBSERVED, ids=lambda o: str(o))
@pytest.mark.parametrize("with_findings", [True, False])
def test_no_unobserved_status_ever_reads_clean(outcome, with_findings, fmt):
    """AEGIS M-2: every not-observed outcome (stopped, blocked, failed, suspended, input set aside, not connected,
    not covered, missing) says it was not checked, shows no findings count and no 'No findings' line, and lists no
    finding (L-6: what its run recorded is a note under the limits)."""
    t = cr.render(_with_crawl(outcome, with_findings), "acme", fmt)[0].decode()
    md = fmt == "markdown"
    if md:
        t = t.replace("` robots.txt `", "robots.txt").replace("\\_", "_")
    body = _section(t, "Crawlability", md)
    label = cr.STATUS[outcome][0]
    assert (md and f"**Status: {label}.**" in body) or (not md and f">{label}</span>" in body), label
    assert cr.NOT_CLEAN in body
    assert "No findings" not in body and "not a clean result" in body
    assert ("**Rule:**" not in body) if md else ('class="finding"' not in body)
    row = re.search(r"\| Crawlability \| \*\*[^|]*\*\* \| ([^|]*) \|", t) if md else \
        re.search(r"<tr><td>Crawlability</td><td>.*?</td><td>([^<]*)</td>", t)
    assert row and row.group(1).strip() == cr.DASH
    if with_findings and outcome in _MAY_CARRY_FINDINGS:
        assert "Notes from the run are listed under Methodology and limits." in body
        assert "Run note: Page not read: robots.txt disallows our crawler (rule PAGE_BLOCKED_FOR_OUR_CRAWLER)" in t


@pytest.mark.parametrize("fmt", ["markdown", "html"])
@pytest.mark.parametrize("with_findings", [True, False])
def test_partly_checked_never_reads_clean(with_findings, fmt):
    t = cr.render(_with_crawl("PARTIAL", with_findings), "acme", fmt)[0].decode().replace("` robots.txt `",
                                                                                          "robots.txt")
    body = _section(t, "Crawlability", fmt == "markdown")
    assert "PARTLY CHECKED" in body and "Part of this area could not be checked" in body
    assert "No findings in this area." not in body
    if with_findings:
        assert "Page not read: robots.txt disallows our crawler" in body and "No findings" not in body
    else:
        assert "No findings in the part that could be checked. The rest of this area was not checked, so this is " \
               "not a clean result." in body


def test_only_ok_reads_no_findings_in_this_area():
    t = cr.render(_with_crawl("OK", False), "acme", "markdown")[0].decode()
    assert "No findings in this area." in _section(t, "Crawlability", True)


def _ai(engines, samples=5):
    v = fx.fresh("full")
    for e in v["report"]["agents"]:
        if e["agent"] == "callum":
            e["facts"]["engines"] = engines
            e["facts"]["samples_per_prompt"] = samples
    return v


def test_sampling_is_claimed_per_engine_and_only_when_every_question_was_asked():
    """AEGIS M-1: no blanket 'each question was asked N times per engine'."""
    full = fx._engine("perplexity", 2)
    partial = {**fx._engine("perplexity", 2), "outcome": "PARTIAL", "reason": "ENGINE_BUDGET_EXHAUSTED",
               "prompts_not_asked": 1}
    stopped = {"engine": "anthropic", "outcome": "BLOCKED", "reason": "ENGINE_BUDGET_EXHAUSTED", "prompts": [],
               "prompts_not_asked": 3}
    refused = {**fx._engine("google", 1), "prompts": fx._engine("google", 1)["prompts"]
               + [{"prompt_index": 1, "prompt": None, "prompt_refused": "personal_data:email"}]}
    engines = {"perplexity": partial, "openai": "NOT_CONNECTED", "anthropic": stopped, "google": refused,
               "zfull": full}
    for fmt in ("markdown", "html"):
        t = cr.render(_ai(engines), "acme", fmt)[0].decode()
        assert "Each question was asked" not in t
        assert "The method above is the plan." in t
        assert "Sampling did not finish: 2 question(s) were asked, each up to 5 times. 1 question(s) were not " \
               "asked." in t
        assert "openai</code>: Nothing was asked." in t or "` openai `: Nothing was asked." in t
        assert ("anthropic</code>: Nothing was asked." in t) or ("` anthropic `: Nothing was asked." in t)
        assert "Each of the 1 question(s) asked was asked 5 times. 1 question(s) were not sent because they " \
               "looked like personal data." in t
        assert "Each of the 2 question(s) asked was asked 5 times." in t
    # an OK engine without a recorded not-asked count is not claimed complete
    t = cr.render(_ai({"perplexity": {k: v for k, v in full.items() if k != "prompts_not_asked"}}), "acme",
                  "markdown")[0].decode()
    assert "How many questions were left unasked is not recorded." in t and "Each of the" not in t


def test_cli_refuses_plain_http_except_loopback():
    """AEGIS M-3."""
    cli = _cli()
    for ok in ("https://seo.internal", "https://seo.internal:8443/", "http://127.0.0.1:8500", "http://[::1]:8500",
               "http://localhost:8500", "http://127.9.9.9"):
        assert cli.api_base(ok) == ok.rstrip("/")
    for bad in ("http://seo.internal", "http://10.0.0.5:8500", "ftp://127.0.0.1", "https://u:p@seo.internal",
                "https://seo.internal/?x=1", "https://seo.internal/#f", "http://localhost.evil.test", "seo.internal",
                "http://[::1"):
        with pytest.raises(ValueError):
            cli.api_base(bad)


def test_cli_fetch_refuses_plain_http_before_sending_anything(tmp_path, capsys):
    cli = _cli()
    sent = []

    class Spy:
        def post(self, *a, **k):
            sent.append(a)

        get = post
    tok = tmp_path / "t.tok"
    tok.write_text("x" * 40)
    args = ["fetch", "--api", "http://seo.example", "--tenant", "zbm", "--audit", "seo-aud-" + "0" * 40, "--format",
            "html", "--out", str(tmp_path / "o.html"), "--service-token-file", str(tok), "--caller-token-file",
            str(tok), "--andre-token-file", str(tok)]
    assert cli.main(args, client=Spy()) == 2 and sent == []
    assert "https" in capsys.readouterr().err and not (tmp_path / "o.html").exists()


def test_code_shaped_values_hosts_and_templates_are_checked_for_personal_data():
    """AEGIS L-1, L-2, L-3."""
    v = fx.fresh("full")
    sel = v["report"]["agents"][0]
    sel["findings"] += [
        finding("PAGE_NOT_HTML", "low", "measured", "WATCH", url="https://acme.test/",
                detail={"content_type": "tel:+15550100199"}),
        finding("CANONICAL_OTHER_HOST", "medium", "measured", "TEST", url="https://acme.test/",
                detail={"canonical": "https://jane-doe-5550100199.evil.test/x"})]
    v["report"]["search_performance"]["section"]["by_page_template"]["templates"].insert(
        0, {"template": "{x}/jane.doe@example.com/5550100199", "clicks": 1, "impressions": 2, "ctr": 0.5})
    for fmt in ("markdown", "html"):
        t = cr.render(v, "acme", fmt)[0].decode()
        assert "5550100199" not in t and "jane" not in t
        assert cr.WITHHELD in t and "{offsite}" in t


def test_request_paths_never_show_a_query_string_or_personal_data():
    """AEGIS L-4."""
    v = fx.fresh("full")
    v["report"]["paths"] = ["/", "/account?email=jane.doe@example.com", "/u/555-010-0199"]
    for fmt in ("markdown", "html"):
        t = cr.render(v, "acme", fmt)[0].decode()
        assert "jane" not in t and "email=" not in t and "555-010-0199" not in t
        assert "/account" in t and "/u/{number}" in t
    p = _Tags()
    p.feed(cr.render(v, "acme", "html")[0].decode())
    assert {val for _t, k, val in p.attrs if k == "href"} == {"https://acme.test/", "https://acme.test/account"}


def test_white_label_does_not_attribute_the_ledger_to_the_reseller():
    """AEGIS L-5."""
    for fmt in ("markdown", "html"):
        t = render("full", fmt, branding=cr.Branding("Acme Partners", "#123456"))
        assert "Acme Partners's" not in t and "Acme Partners'" not in t
        assert "recorded on a tamper-evident ledger when the audit completed" in t


def test_search_console_freshness_without_a_window():
    """AEGIS L-7."""
    v = fx.fresh("full")
    for e in v["report"]["agents"]:
        if e["task"] == "search_performance":
            e["outcome"] = "PARTIAL"
    v["report"]["search_performance"]["section"] = None
    t = cr.render(v, "acme", "markdown")[0].decode()
    assert "None to None" not in t and "no Search Console data in this report" in t


@pytest.mark.parametrize("field,value", [("severity", "CRITICAL"), ("severity", None), ("data_class", "guessed"),
                                         ("decision", "MAYBE")])
def test_a_finding_outside_the_vocabularies_is_refused_not_downgraded(field, value):
    """AEGIS L-10."""
    v = fx.fresh("full")
    v["report"]["agents"][0]["findings"][0][field] = value
    with pytest.raises(cr.ReportInputError):
        cr.render(v, "acme", "html")


def test_service_refuses_to_start_without_valid_branding(tmp_path):
    """AEGIS L-8: a Settings built without load() carries the default; anything else refuses start."""
    assert config_mod.Settings(service_token="x" * 40).report_branding == cr.DEFAULT_BRANDING
    from ledger import Recorder
    from service import SeoService
    from store import RecordLog
    from helpers import FakeLedger
    s = config_mod.load(base_env())
    s.report_branding = None
    with pytest.raises(RuntimeError, match="report_branding"):
        SeoService(s, Recorder(FakeLedger()), RecordLog(None))


@pytest.mark.local_http
@pytest.mark.parametrize("switch,code", [("global", "KILLED_GLOBAL"), ("write", "KILLED_WRITE")])
def test_route_global_and_write_switches(tmp_path, srv, switch, code):
    install_site(srv)
    h = _harness(tmp_path, srv)
    aid = _audit(h)["audit_id"]
    req = rid()
    first = h.ok(_report(h, "zbm", aid, request_id=req))
    h.ok(h.switch(switch))
    h.refused(_report(h, "zbm", aid), 403, code)
    h.refused(_report(h, "zbm", aid, request_id=req), 403, code)            # a replay too
    assert len(h.ledger.of_type("client_report_rendered")) == 1
    h.ok(h.switch(switch, engaged=False, andre=True))
    assert h.ok(_report(h, "zbm", aid, request_id=req)) == first



# ---------------------------------------------------------------------------------------------- AEGIS d525a5d

@pytest.mark.parametrize("fmt", ["markdown", "html"])
@pytest.mark.parametrize("outcome", ["BLOCKED", "FAILED", "INSUFFICIENT_EVIDENCE", "KILLED", "NOT_CONNECTED"])
def test_dropped_findings_line_only_under_a_checked_area(outcome, fmt):
    """AEGIS N-1."""
    v = _with_crawl(outcome)
    v["report"]["agents"][0]["findings_dropped"] = 7
    body = _section(cr.render(v, "acme", fmt)[0].decode(), "Crawlability", fmt == "markdown")
    assert "more findings are in the audit record" not in body and "7" not in body
    ok = _with_crawl("OK")
    ok["report"]["agents"][0]["findings_dropped"] = 7
    assert "7 more findings are in the audit record" in _section(cr.render(ok, "acme", fmt)[0].decode(),
                                                                 "Crawlability", fmt == "markdown")


def test_percent_encoded_personal_data_in_a_request_path_is_templated():
    """AEGIS N-4."""
    v = fx.fresh("full")
    v["report"]["paths"] = ["/", "/u/555%2D010%2D0199"]
    t = cr.render(v, "acme", "html")[0].decode()
    assert "555%2D010%2D0199" not in t and "https://acme.test/u/" not in t


def test_cli_api_base_refuses_empty_query_fragment_userinfo_and_control_characters():
    """AEGIS N-3."""
    cli = _cli()
    for bad in ("https://seo.internal?", "https://seo.internal#", "http://@127.0.0.1", "https://@seo.internal",
                "https://seo.in\tternal", "https://seo.internal\n", "https://seo.internal\r/x", "https://se o.internal",
                "https://höst.example", "https://seo.internal\x00", ""):
        with pytest.raises(ValueError):
            cli.api_base(bad)


class _FakeService:
    """The two calls the CLI makes, answered from a fixed render; ``listed`` is what the recorded list returns."""

    def __init__(self, content: bytes, listed):
        import httpx
        self.httpx = httpx
        self.rep = {"report_id": "seo-rpt-" + "1" * 40, "audit_id": "seo-aud-" + "0" * 40, "format": "html",
                    "document_sha256": hashlib.sha256(content).hexdigest(), "audit_report_sha256": "b" * 64,
                    "renderer_version": cr.RENDERER_VERSION, "bytes": len(content), "content": content.decode()}
        self.listed = listed(self.rep) if callable(listed) else listed

    def post(self, url, **k):
        return self.httpx.Response(200, json=self.rep)

    def get(self, url, **k):
        return self.httpx.Response(200, json=self.listed)


def _fetch_args(tmp_path, out):
    tok = tmp_path / "t.tok"
    tok.write_text("x" * 40)
    return ["fetch", "--api", "https://seo.internal", "--tenant", "zbm", "--audit", "seo-aud-" + "0" * 40,
            "--format", "html", "--out", str(out), "--service-token-file", str(tok), "--caller-token-file", str(tok),
            "--andre-token-file", str(tok)]


@pytest.mark.parametrize("listed", [
    [], [{"report_id": "seo-rpt-" + "2" * 40, "document_sha256": "0" * 64}],
    lambda r: [{"report_id": r["report_id"], "document_sha256": "0" * 64}],
    lambda r: [{"report_id": "seo-rpt-" + "2" * 40, "document_sha256": r["document_sha256"]}],
    {"detail": "x"}])
def test_cli_fetch_refuses_a_render_the_service_has_not_recorded(tmp_path, capsys, listed):
    """AEGIS N-2: the document is written only when the recorded-renders list holds its id AND hash."""
    out = tmp_path / "r.html"
    cli = _cli()
    assert cli.main(_fetch_args(tmp_path, out), client=_FakeService(b"<p>doc</p>", listed)) == 2
    assert not out.exists() and "recorded renders do not list" in capsys.readouterr().err


def test_cli_fetch_writes_0600_also_over_an_existing_file_and_never_through_a_symlink(tmp_path, capsys):
    """AEGIS N-2."""
    cli = _cli()
    ok = lambda r: [{"report_id": r["report_id"], "document_sha256": r["document_sha256"]}]  # noqa: E731
    new = tmp_path / "new.html"
    assert cli.main(_fetch_args(tmp_path, new), client=_FakeService(b"<p>doc</p>", ok)) == 0
    assert new.read_bytes() == b"<p>doc</p>" and new.stat().st_mode & 0o777 == 0o600
    old = tmp_path / "old.html"
    old.write_text("previous")
    old.chmod(0o644)
    assert cli.main(_fetch_args(tmp_path, old), client=_FakeService(b"<p>doc</p>", ok)) == 0
    assert old.read_bytes() == b"<p>doc</p>" and old.stat().st_mode & 0o777 == 0o600
    target = tmp_path / "target.txt"
    target.write_text("keep")
    link = tmp_path / "link.html"
    link.symlink_to(target)
    capsys.readouterr()
    assert cli.main(_fetch_args(tmp_path, link), client=_FakeService(b"<p>doc</p>", ok)) == 2
    assert target.read_text() == "keep" and "cannot write --out" in capsys.readouterr().err


class _MustNotBeCalled:
    def post(self, url, **k):
        raise AssertionError("the service was called although --out could not be written")

    get = post


_RID = "0b5c4b7e-3d1a-4c55-9f0e-2a8f6c1d9e10"
_OK = lambda r: [{"report_id": r["report_id"], "document_sha256": r["document_sha256"]}]  # noqa: E731


def test_cli_checks_the_out_directory_before_calling_the_service(tmp_path, capsys, monkeypatch):
    """AEGIS wave3 L-C: a render is recorded before the service answers, so --out is checked first: a missing
    directory, a directory that is a file, an unwritable directory or a symlink is refused with no call made."""
    cli = _cli()
    missing = tmp_path / "client-reports" / "r.html"
    assert cli.main(_fetch_args(tmp_path, missing), client=_MustNotBeCalled()) == 2
    err = capsys.readouterr().err
    assert "cannot write --out" in err and "does not exist" in err and "mkdir -p -m 700" in err
    assert not missing.parent.exists()
    afile = tmp_path / "afile"
    afile.write_text("x")
    assert cli.main(_fetch_args(tmp_path, afile / "r.html"), client=_MustNotBeCalled()) == 2
    assert "cannot write --out" in capsys.readouterr().err
    real_access = cli.os.access
    monkeypatch.setattr(cli.os, "access", lambda p, m: False if str(p) == str(tmp_path) else real_access(p, m))
    assert cli.main(_fetch_args(tmp_path, tmp_path / "r.html"), client=_MustNotBeCalled()) == 2
    assert "not writable" in capsys.readouterr().err and not (tmp_path / "r.html").exists()
    monkeypatch.setattr(cli.os, "access", real_access)
    (tmp_path / "t.txt").write_text("keep")
    (tmp_path / "l.html").symlink_to(tmp_path / "t.txt")
    assert cli.main(_fetch_args(tmp_path, tmp_path / "l.html"), client=_MustNotBeCalled()) == 2
    assert (tmp_path / "t.txt").read_text() == "keep" and "request id" not in capsys.readouterr().err


def test_cli_prints_the_request_id_on_a_failure_after_the_render_was_recorded(tmp_path, capsys, monkeypatch):
    """AEGIS wave3 L-C: after the POST, any failure names the request id so the documented retry
    (--request-id) returns the same document instead of recording a second render. Nothing is left at --out."""
    cli = _cli()
    out = tmp_path / "r.html"
    # the recorded list does not hold the render: refused, the explicit request id is printed, no file is left
    args = _fetch_args(tmp_path, out) + ["--request-id", _RID]
    assert cli.main(args, client=_FakeService(b"<p>doc</p>", [])) == 2
    err = capsys.readouterr().err
    assert "recorded renders do not list" in err and f"--request-id {_RID}" in err and not out.exists()
    # without --request-id the generated one is printed (a UUID)
    assert cli.main(_fetch_args(tmp_path, out), client=_FakeService(b"<p>doc</p>", [])) == 2
    m_ = re.search(r"request id: ([0-9a-f-]{36}) ", capsys.readouterr().err)
    assert m_ and str(uuid.UUID(m_.group(1))) == m_.group(1)
    # a write that fails after the render was recorded also prints it; an existing file keeps its content
    out.write_text("previous")

    def boom(fd, n):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cli.os, "ftruncate", boom)
    assert cli.main(args, client=_FakeService(b"<p>doc</p>", _OK)) == 2
    err = capsys.readouterr().err
    assert "cannot write --out" in err and f"--request-id {_RID}" in err and out.read_text() == "previous"


def test_cli_refused_before_the_post_prints_no_request_id(tmp_path, capsys):
    cli = _cli()
    args = _fetch_args(tmp_path, tmp_path / "r.html")
    args[args.index("seo-aud-" + "0" * 40)] = "not-an-id"
    assert cli.main(args, client=_MustNotBeCalled()) == 2
    err = capsys.readouterr().err
    assert "malformed" in err and "request id" not in err and not (tmp_path / "r.html").exists()


# ---------------------------------------------------------------------------------------------- golive-wave2

def test_ai_visibility_renders_the_search_based_citation_definition_from_a_real_agent_api_audit(tmp_path):
    """A real audit through the Perplexity adapter on the Agent API (a fake Perplexity, no network): the tenant URL
    only in an annotation (search never returned it) and the tenant URL with a non-web scheme are NOT cited, so the
    report shows "Mentioned but not cited" with the competitor cited instead, and both the AI visibility section and
    Callum's methodology state the definition: cited = returned by search."""
    import test_perplexity_agent as pa
    body = pa.agent_body(text="Z Best Media recovers marketplace revenue.",
                         urls=("https://competitor.test/a", "ftp://site.test/files"))
    body["output"][1]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://site.test/never-searched"}]
    f = pa.FakePerplexity(pa.ok(body))
    h = pa.agent_harness(tmp_path, f)
    aid = pa.run_audit(h, pa.setup_zbm(h))["audit_id"]
    c = pa.callum(h.ok(h.get(f"/tenants/zbm/audits/{aid}")))
    p = c["facts"]["engines"]["perplexity"]["prompts"][0]
    assert p["class"] == "MENTIONED_NOT_CITED" and p["cited"] == 0 and len(f.requests) == 3
    assert "cited = returned by search" in c["methodology"]
    md = h.ok(_report(h, "zbm", aid, fmt="markdown"))["content"]
    html = h.ok(_report(h, "zbm", aid, fmt="html"))["content"]
    ai = md[md.index("## AI visibility (answer engines)"):md.index("## Methodology and limits")]
    method = md[md.index("### AI visibility (answer engines)", md.index("## Methodology and limits")):]
    assert "cited = returned by search" in ai and "it does not mean the answer's text linked" in ai
    assert "cited = returned by search" in method and "Whether the answer's text points at that page is not " \
        "measured" in method
    assert "Mentioned but not cited" in ai and "competitor.test" in ai
    for doc in (md, html):
        assert "never-searched" not in doc and "ftp://" not in doc and "Cited in at least half" not in doc
    assert html.count("cited = returned by search") == 2 and md.count("cited = returned by search") == 2


def test_callum_methodology_is_shown_even_when_the_prompt_set_hash_has_a_digit_run():
    """The prompt set's 16-hex hash in Callum's methodology (here with the digit run 07408562) used to make the
    personal-data check withhold the whole method; the client document drops the hash and shows the method."""
    from agents import probes
    method = probes.METHODOLOGY.format(ps="core", ver=1, sha="4e07408562bedb8b", n=3, domain="site.test",
                                       m=probes.MIN_USABLE)
    assert cr.safe_text(method, 1500) == cr.WITHHELD                         # the old behaviour, for the record
    shown = cr.safe_text(cr._PS_HASH.sub("", method), 1500)
    assert shown != cr.WITHHELD and "4e07408562bedb8b" not in shown and "prompt set core v1 sent 3 times" in shown
    assert "cited = returned by search" in shown
    # removal only: a phone number elsewhere in the text is still withheld
    assert cr.safe_text(cr._PS_HASH.sub("", method + " Call 562 248 6617."), 1500) == cr.WITHHELD
