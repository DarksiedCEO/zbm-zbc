"""SPEC section 5: the Selene log task (B-LG-01..09), through the real service path."""

from __future__ import annotations

import json

import pytest

from agents import logs as logs_mod
from agents_run import by_code, codes
from cert_web import Web, site, sitemap
from common import check_ran, rng
from gen import plants
from harness.service import GOOGLE_UA, Svc, clf
from oracles import rfc9309

BING_UA = "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"
GPT_UA = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.1; +https://openai.com/gptbot)"


@pytest.fixture
def svc(tmp_path):
    web = site(Web(), robots="User-agent: *\nDisallow: /*?sort=\nDisallow: /search?\nDisallow: /%2541\n"
                             "Sitemap: http://site.test/sitemap.xml\n")
    web.route("/sitemap.xml", sitemap(["http://site.test/", "http://site.test/about", "http://site.test/never",
                                       "http://other.test/elsewhere"]), ctype="application/xml")
    s = Svc(tmp_path, web)
    yield s
    s.close()


def _env(done):
    return done["report"]["envelope"]


def test_B_LG_01_formats_and_exact_quarantine_counts(svc):
    r = rng(7, "lg01")
    lines, want = [], {}
    for i in range(1000):
        k = r.random()
        if k < 0.05:
            lines.append("x" * 8193)
            want["LINE_TOO_LONG"] = want.get("LINE_TOO_LONG", 0) + 1
        elif k < 0.1:
            lines.append("garbage line without structure")
            want["MALFORMED"] = want.get("MALFORMED", 0) + 1
        elif k < 0.13:
            lines.append(clf("999.1.1.1", "/a"))
            want["BAD_IP"] = want.get("BAD_IP", 0) + 1
        else:
            lines.append(clf("192.0.2.%d" % r.randint(1, 250), "/p%d" % r.randint(0, 50)))
    done = svc.log_ingest(lines)
    q = _env(done)["facts"]["quarantined"]
    assert q == want, (q, want)
    assert "LOG_LINES_QUARANTINED" in codes(_env(done))
    check_ran("B-LG-01", len(lines))


def test_B_LG_01_jsonl_and_common(svc, tmp_path):
    rows = [json.dumps({"ip": "192.0.2.4", "path": "/a", "status": 200, "ua": GOOGLE_UA}) for _ in range(10)]
    import base64
    from helpers import rid
    iid = svc.h.ok(svc.post("/tenants/zbm/log-ingests", {"request_id": rid(), "domain": "site.test",
                                                          "scheme": "http", "format": "jsonl"}), 201)["ingest_id"]
    data = base64.b64encode(("\n".join(rows) + "\n").encode()).decode()
    svc.h.ok(svc.post(f"/tenants/zbm/log-ingests/{iid}/chunks", {"request_id": rid(), "seq": 1,
                                                                 "data_b64": data, "last": True}))
    done = svc.h.ok(svc.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    assert _env(done)["facts"]["lines"] == 10


def test_B_LG_02_family_by_token_and_claimed_without_dns(svc):
    lines = [clf("192.0.2.5", "/", ua=GOOGLE_UA)] * 100 + [clf("192.0.2.6", "/", ua=BING_UA)] * 60 + \
        [clf("192.0.2.7", "/", ua=GPT_UA)] * 40
    f = _env(svc.log_ingest(lines))["facts"]["families"]
    assert f["Googlebot"]["requests"] == 100 and f["Bingbot"]["requests"] == 60 and f["GPTBot"]["requests"] == 40
    assert all(v["claimed"] == v["requests"] for v in f.values())
    check_ran("B-LG-02", len(lines))


def test_B_LG_03_robots_matched_once_on_path_and_query(svc):
    svc.audit(["/"])
    reqs = ["/list?sort=a", "/search?q=x", "/%2541", "/%41x", "/list?page=2", "/plain"]
    done = svc.log_ingest([clf("192.0.2.5", p) for p in reqs])
    g = _env(done)["facts"]["families"]["Googlebot"]
    assert g["robots_disallowed_hits"] == 3, g          # %2541 decodes once to %41, never to 'A'
    rb = svc.web.routes[("site.test", "/robots.txt")][1].encode()
    want = sum(1 for p in reqs if not rfc9309.allowed(rb, "Googlebot", p.encode()))
    assert want == 3


@pytest.mark.parametrize("seed", [7, 11])
def test_B_LG_03_oracle_agreement_on_generated_lines(seed, tmp_path):
    from gen import robots_gen as rg
    r = rng(seed, "lg03")
    total = 0
    for k in range(8):
        data = rg.ruleset(r, r.choice(("plain", "query", "pct_reserved")))
        text = data.decode("latin-1")
        web = site(Web(), robots=text)
        (tmp_path / f"s{k}").mkdir()
        s = Svc(tmp_path / f"s{k}", web)
        try:
            paths = [rg.path(r, "query").decode("latin-1") for _ in range(250)]
            paths = [p for p in paths if " " not in p and '"' not in p]
            done = s.log_ingest([clf("192.0.2.5", p) for p in paths])
            fam = _env(done)["facts"]["families"].get("Googlebot")
            want = sum(1 for p in paths if not rfc9309.allowed(data, "Googlebot", p.encode("latin-1")))
            assert (fam or {}).get("robots_disallowed_hits", 0) == want, (text, seed, k)
            total += len(paths)
        finally:
            s.close()
    check_ran("B-LG-03", total)


@pytest.mark.parametrize("n,errors,flag", [(19, 19, False), (20, 1, False), (20, 2, True), (1000, 50, False),
                                           (1000, 51, True)])
def test_B_LG_04_search_crawler_5xx_boundaries(svc, n, errors, flag):
    lines = [clf("192.0.2.5", "/", 503 if i < errors else 200) for i in range(n)]
    env = _env(svc.log_ingest(lines))
    assert ("SEARCH_CRAWLER_SERVER_ERRORS" in codes(env)) is flag


def test_B_LG_05_uncrawled_sample_by_code_never_raw(svc):
    svc.audit(["/"])
    env = _env(svc.log_ingest([clf("192.0.2.5", "/"), clf("192.0.2.5", "/about")]))
    f = by_code(env, "IMPORTANT_URLS_NOT_CRAWLED")
    assert len(f) == 1 and f[0]["detail"]["count"] == 1
    assert f[0]["detail"]["of_sitemap_sample"] == 3          # the other-host loc is not in the sample
    assert "never" not in json.dumps(env)                # the uncrawled path is shown coded


def test_B_LG_05_without_an_audit_the_sample_is_missing(svc):
    env = _env(svc.log_ingest([clf("192.0.2.5", "/")]))
    assert "IMPORTANT_URLS_NOT_CRAWLED" not in codes(env) and env["facts"]["uncrawled_important_paths"] is None


def test_B_LG_06_no_search_crawler(svc):
    env = _env(svc.log_ingest([clf("192.0.2.5", "/", ua=GPT_UA)] * 5))
    assert "SEARCH_CRAWLER_ABSENT" in codes(env)


def test_B_LG_07_no_ip_derived_value_in_any_output(svc):
    r = rng(7, "lg07")
    ips = [plants.shape_value("ipv4", r) for _ in range(50)]
    svc.log_ingest([clf(ip, "/") for ip in ips])
    out = svc.stored_outputs()
    assert not [ip for ip in ips if ip in out]


def test_B_LG_08_templates_capped_and_counted(svc):
    r = rng(7, "lg08")
    lines = [clf("192.0.2.5", "/" + plants.uniq(r)) for _ in range(logs_mod.TEMPLATES_PER_FAMILY + 40)]
    g = _env(svc.log_ingest(lines))["facts"]["families"]["Googlebot"]
    assert g["path_templates_seen"] <= logs_mod.TEMPLATES_PER_FAMILY and g["templates_over_cap"] >= 40


def test_B_LG_09_inputs_only_in_the_vault(svc):
    m = plants.uniq(rng(9, "lg09"))
    svc.web.route("/robots.txt", f"# {m}\nUser-agent: *\nDisallow: /{m}\n", ctype="text/plain")
    svc.audit(["/"])
    svc.log_ingest([clf("192.0.2.5", f"/{m}/x"), clf("192.0.2.5", "/")])
    assert m not in svc.stored_outputs().lower()


def _chunk(lines, robots=None, fmt="combined"):
    from agents_run import coding
    data = ("\n".join(lines) + "\n").encode()
    with coding():
        return logs_mod.process_chunk(fmt, data, b"k" * 32, lambda ip, t: "unverified", {}, {"left": 0},
                                      robots=robots, robots_expected=robots is not None)


def test_B_LG_01_status_range_and_counters_exact():
    """B-LG-01: a status outside 100-599 is quarantined BAD_STATUS; a line without a User-Agent counts
    no_user_agent; a non-bot User-Agent counts non_bot; bot lines are neither."""
    d = _chunk([clf("192.0.2.5", "/a", 600), clf("192.0.2.5", "/a", 700), clf("192.0.2.5", "/a", 599),
                clf("192.0.2.5", "/a", 100)])
    assert d["quarantined"].get("BAD_STATUS") == 2 and d["families"]["Googlebot"]["requests"] == 2
    h = _chunk([clf("192.0.2.5", "/a", 200, ua="Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0")] * 3 +
               [clf("192.0.2.5", "/a", 200)])
    assert h["non_bot"] == 3 and h["no_user_agent"] == 0
    c = _chunk(['192.0.2.5 - - [10/Oct/2026:13:55:36 -0700] "GET /a HTTP/1.1" 200 12'] * 4, fmt="common")
    assert c["no_user_agent"] == 4 and c["non_bot"] == 0


def test_B_LG_01_templates_per_family_are_bounded():
    """B-LG-01: at most TEMPLATES_PER_FAMILY distinct templates a family; the rest still counted as requests."""
    r = rng(3, "lgtpl")
    n = logs_mod.TEMPLATES_PER_FAMILY + 60
    d = _chunk([clf("192.0.2.5", "/" + plants.uniq(r)) for _ in range(n)])
    g = d["families"]["Googlebot"]
    assert len(g["templates"]) == logs_mod.TEMPLATES_PER_FAMILY and g["requests"] == n


def test_B_LG_03_undecided_lines_are_unchecked_never_blocked():
    """B-LG-03 / A-17: a line whose robots decision ran out of work is counted robots_unchecked, never as a
    disallowed hit (never wrong)."""
    from gen import robots_gen
    from primitives import robots as robots_mod
    rb = robots_mod.parse(robots_gen.needle(99).decode())
    d = _chunk([clf("192.0.2.5", "/" + "a" * 2047 + str(i)) for i in range(200)], robots=rb)
    assert d["robots_unchecked"] > 0 and d["families"]["Googlebot"]["robots_blocked"] == 0
