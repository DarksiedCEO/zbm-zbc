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
    assert t.count("No findings for the parts of this area that were checked.") == 4
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
    args = ["fetch", "--api", "http://testserver", "--tenant", "zbm", "--audit", aid, "--format", "markdown",
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
