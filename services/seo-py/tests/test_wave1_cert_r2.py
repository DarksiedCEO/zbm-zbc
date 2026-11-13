"""AEGIS Wave-1 certification round 2 (re-review of 89fae32b): one test (or more) per finding, each failing on
89fae32b. Runs on the in-memory web of cert_web.py (the real Fetcher behind httpx.MockTransport; no socket).
Planted values are synthetic and made at run time (cert_web.marker / letters_marker)."""

from __future__ import annotations

import codecs
import hashlib
import json
import random
import re
import time

import pytest

from agents import delia, drift, entity_check, logs as logs_mod, roman, selene
from cert_web import ORG_LD, Web, ctx, page, site, sitemap
from primitives import fetch as fetch_mod
from primitives import robots as robots_mod
from primitives import safe_url as su
from primitives.parse import MAX_JSONLD_BYTES, parse_html


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


def letters_marker(n: int = 12) -> str:
    """A letters-only synthetic value (the shape of a name slug or a secret word), made at run time."""
    rnd = random.SystemRandom()
    return "zq" + "".join(rnd.choice("bcdfghjklmnpqrstvwxz") for _ in range(n))


# ============================================================================================ M1 percent-encoding

UNRES = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
HEXB = set(b"0123456789abcdefABCDEF")


def _octets(raw: bytes) -> list:
    out, i = [], 0
    while i < len(raw):
        b = raw[i]
        if b == 0x25 and i + 2 < len(raw) and raw[i + 1] in HEXB and raw[i + 2] in HEXB:
            v = int(raw[i + 1:i + 3], 16)
            out.extend([v] if v in UNRES else list(b"%" + raw[i + 1:i + 3].upper()))
            i += 3
            continue
        if b == 0x25:
            out.extend(b"%25")
        elif b >= 0x80 or b < 0x21 or b == 0x7F or b in b'"<>\\^`{|}*$':
            out.extend(b"%%%02X" % b)
        else:
            out.append(b)
        i += 1
    return out


def _oracle(rules: list, path: str) -> bool:
    """RFC 9309 by bytes and dynamic programming (independent of the greedy matcher)."""
    p = _octets(path.encode("utf-8", "surrogateescape"))
    if bytes(p) == b"/robots.txt":
        return True
    best = None
    for allow, value in rules:
        v = value.encode("utf-8", "surrogateescape")
        while b"**" in v:
            v = v.replace(b"**", b"*")
        end = v.endswith(b"$")
        toks = []
        for k, part in enumerate((v[:-1] if end else v).split(b"*")):
            if k:
                toks.append(-1)
            toks.extend(_octets(part))
        reach = [True] + [False] * len(p)
        for t in toks:
            if t == -1:
                seen, new = False, []
                for x in reach:
                    seen = seen or x
                    new.append(seen)
            else:
                new = [False] + [reach[j] and p[j] == t for j in range(len(p))]
            reach = new
        ok = reach[len(p)] if end else any(reach)
        if ok:
            key = (len(toks) + (1 if end else 0), allow)
            best = key if best is None or key > best else best
    return True if best is None else best[1]


ATOMS = ["/", "a", "b", "%2A", "%2a", "%2F", "%2f", "%3F", "?", "%24", "%41", "A", "%7E", "~", "%25", "%",
         "%C3%A9", "\u00e9", "%C3%28", "%FF", "\udcff", "%E3%83%84", "\u30c4", "=", "-", "%20"]


def test_percent_encoding_agrees_with_an_rfc9309_octet_oracle():
    rnd = random.Random(20261113)
    disagree = allows_disallowed = 0
    for _ in range(3000):
        rules = []
        for _ in range(rnd.randint(1, 5)):
            body = "/" + "".join(rnd.choice(ATOMS + ["*", "$"]) for _ in range(rnd.randint(0, 5)))
            rules.append((rnd.random() < 0.4, body + ("$" if rnd.random() < 0.2 else "")))
        text = "User-agent: *\n" + "".join(("Allow: " if a else "Disallow: ") + v + "\n" for a, v in rules)
        rb = robots_mod.parse(text.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape"))
        for _ in range(4):
            path = "/" + "".join(rnd.choice(ATOMS + ["*", "$"]) for _ in range(rnd.randint(0, 7)))
            mine, ref = rb.allowed("Googlebot", path), _oracle(rules, path)
            disagree += mine != ref
            allows_disallowed += bool(mine and not ref)
    assert (disagree, allows_disallowed) == (0, 0)


@pytest.mark.parametrize("rule,path,allowed", [
    ("/%2A", "/foo", True), ("/%2A", "/*", False), ("/%2A", "/%2a", False),          # %2A is a literal *, never a wildcard
    ("/a%2Fb", "/a/b", True), ("/a%2Fb", "/a%2fb", False),                           # %2F is not /
    ("/s%3Fq", "/s?q", True), ("/x%24", "/x$", False), ("/x%24", "/x", True),         # %3F is not ?, %24 not an anchor
    ("/%7Euser", "/~user", False), ("/%41b", "/Ab", False),                          # unreserved: decoded
    ("/caf\u00e9", "/caf%C3%A9", False), ("/caf%c3%a9", "/caf%C3%A9", False),         # non-ASCII as UTF-8 octets
    ("/%FF", "/%FE", True), ("/%C3%28", "/%EF%BF%BD", True),                          # invalid UTF-8: no collision
    ("/\udcff", "/%FF", False), ("/a$b", "/a$b", False), ("/%", "/%25", False),
])
def test_rfc9309_octet_comparison(rule, path, allowed):
    rb = robots_mod.parse(f"User-agent: *\nDisallow: {rule}\n")
    assert rb.allowed("Googlebot", path) is allowed


def test_reserved_encoding_never_yields_a_rule_not_in_the_file():
    web = site(Web(), robots="User-agent: *\nDisallow: /%2A\nSitemap: http://site.test/sitemap.xml\n")
    _, s, _, _, pages = run3(web, ["/", "/about"])
    assert "ROBOTS_BLOCKS_SEARCH_CRAWLER" not in codes(s)            # 89fae32b: critical, quoting "/*"
    assert pages["/about"]["fetch"].state == "OK"


def test_an_encoded_allow_never_lets_our_crawler_fetch_a_disallowed_path():
    web = site(Web(), robots="User-agent: *\nDisallow: /\nAllow: /%2A\n")
    f = web.fetcher().fetch("http://site.test/about", robots_cache={})
    assert f.state == "BLOCKED_BY_ROBOTS"                               # 89fae32b: "/%2A" read as "/*", fetched
    assert ("site.test", "/about") not in web.requests


def test_our_crawler_honours_a_reserved_encoded_disallow():
    web = site(Web(), robots="User-agent: *\nDisallow: /a%2Fb\n")
    web.route("/a%2Fb", page())
    f = web.fetcher().fetch("http://site.test/a%2Fb", robots_cache={})
    assert f.state == "BLOCKED_BY_ROBOTS"
    g = web.fetcher().fetch("http://site.test/about", robots_cache={})
    assert g.state == "OK"


def test_specificity_counts_canonical_octets():
    # "/caf<e-acute>" written raw and the same path percent-encoded are equally specific: allow wins the tie
    rb = robots_mod.parse("User-agent: *\nDisallow: /caf\u00e9\nAllow: /caf%C3%A9\n")
    assert rb.allowed("x", "/caf%C3%A9") is True
    assert robots_mod._compile(False, "/caf\u00e9").length == robots_mod._compile(True, "/caf%C3%A9").length == 10


# ============================================================================================ M2 counted work, not a clock

def test_robots_matching_uses_no_clock():
    assert not hasattr(robots_mod, "time") and not hasattr(robots_mod, "Budget")
    assert not hasattr(fetch_mod, "ROBOTS_CHECK_BUDGET_S") and not hasattr(selene, "MATRIX_BUDGET_S")


def test_slow_ordinary_site_is_never_blocked_or_unreadable():
    """Allow: / and 0.7 s responses: at 89fae32b the 5 s wall-clock budget ran out mid-run and our own crawler
    refused pages (BLOCKED_BY_ROBOTS, CANONICAL_TARGET_NOT_200 high/ACT, SITEMAP_UNREADABLE)."""
    web = site(Web())
    paths = ["/", "/about"] + [f"/p{i}" for i in range(4)]
    for p in paths[2:]:
        web.route(p, page(canonical=f"http://site.test{p}-c"))
        web.route(f"{p}-c", page(canonical=f"http://site.test{p}-c"))
    real = web.handle

    def slow(request):
        time.sleep(0.35)
        return real(request)
    web.handle = slow
    _, s, d, _, pages = run3(web, paths)
    states = {p: v["fetch"].state for p, v in pages.items()}
    assert set(states.values()) == {"OK"}, states
    assert not codes(s) & {"PAGE_BLOCKED_FOR_OUR_CRAWLER", "CANONICAL_TARGET_NOT_200", "ROBOTS_CHECK_INCOMPLETE",
                           "PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE", "CANONICAL_TARGET_UNCHECKED"}
    assert not codes(d) & {"SITEMAP_UNREADABLE", "LLMS_TXT_UNREADABLE"} and d["outcome"] == "OK"


def test_large_site_all_sitemaps_read_on_a_slow_run():
    web = site(Web(), robots="User-agent: *\nDisallow: /private/\n" +
               "".join(f"Sitemap: http://site.test/sm{i}.xml\n" for i in range(8)))
    for i in range(8):
        web.route(f"/sm{i}.xml", sitemap(f"http://site.test/c{i}/item-{j}" for j in range(3000)),
                  ctype="application/xml")
    real = web.handle

    def slow(request):
        time.sleep(0.7)
        return real(request)
    web.handle = slow
    _, _, d, _, _ = run3(web, ["/"])
    f = d["facts"]
    assert (f["sitemaps_read"], f["sitemaps_attempted"], f["sitemap_fetch_failures"]) == (8, 8, 0)
    assert f["sitemap_urls"] == 24000 and f["robots_checked"] == delia.BLOCK_SAMPLE and d["outcome"] == "OK"


def test_counted_work_spent_is_its_own_state_never_blocked(monkeypatch):
    monkeypatch.setattr(fetch_mod, "ROBOTS_CHECK_WORK", 0)
    web = site(Web())
    c, s, _, _, pages = run3(web, ["/", "/about"])
    assert {p["fetch"].state for p in pages.values()} == {"ROBOTS_CHECK_INCOMPLETE"}
    assert s["outcome"] == "INSUFFICIENT_EVIDENCE" and "PAGE_BLOCKED_FOR_OUR_CRAWLER" not in codes(s)
    for f in by_code(s, "PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE"):
        assert f["decision"] == "INSUFFICIENT_EVIDENCE" and "disallows" not in json.dumps(f)


def test_work_is_deterministic():
    text = "User-agent: *\n" + "".join(f"Disallow: /*a*a*a*b{i}\n" for i in range(3000))
    rb1, rb2 = robots_mod.parse(text), robots_mod.parse(text)
    w1, w2 = robots_mod.Work(50_000), robots_mod.Work(50_000)
    r1 = [rb1.allowed("Googlebot", f"/{'a' * 50}{i}", w1) for i in range(40)]
    r2 = [rb2.allowed("Googlebot", f"/{'a' * 50}{i}", w2) for i in range(40)]
    assert r1 == r2 and (w1.units, w1.undecided) == (w2.units, w2.undecided) and w1.undecided > 0
    assert None in r1 and r1.index(None) == r2.index(None)


# ============================================================================================ M3 limits, never measured

def test_filler_rules_then_a_real_disallow_is_found():
    text = "User-agent: *\n" + "".join(f"Disallow: /filler{i}/\n" for i in range(4000)) + "Disallow: /about\n"
    web = site(Web(), robots=text)
    _, s, _, _, pages = run3(web, ["/", "/about"])
    f = [x for x in by_code(s, "ROBOTS_BLOCKS_SEARCH_CRAWLER") if x["detail"]["token"] == "Googlebot"]
    assert f and f[0]["detail"]["blocked_paths"] == ["/about"] and f[0]["detail"]["rules"] == ["disallow: /about"]
    assert pages["/"]["fetch"].state == "OK" and pages["/about"]["fetch"].state == "BLOCKED_BY_ROBOTS"
    assert "ROBOTS_LIMITS_EXCEEDED" not in codes(s)


def test_a_long_allow_is_used_and_nothing_is_refused():
    long_allow = "/" + "a" * 2500
    web = site(Web(), robots=f"User-agent: *\nAllow: {long_allow}\nDisallow: /nothing\n")
    _, s, _, _, pages = run3(web, ["/", "/about"])
    assert s["outcome"] == "OK" and {p["fetch"].state for p in pages.values()} == {"OK"}
    assert "ROBOTS_LIMITS_EXCEEDED" not in codes(s)


def test_a_long_anchored_disallow_is_quoted_as_written_and_exact():
    pat = "/x" + "y" * 2500
    rb = robots_mod.parse(f"User-agent: *\nDisallow: {pat}$\n")
    d = rb.decide("Googlebot", pat)
    assert d["allowed"] is False and d["rule"] == f"disallow: {pat}$" and d["complete"] is True
    assert rb.allowed("Googlebot", pat + "z") is True                  # 89fae32b: shortened prefix, $ dropped
    many_wild = "/" + "*a" * 60
    rb = robots_mod.parse(f"User-agent: *\nDisallow: {many_wild}\n")
    assert rb.allowed("x", "/" + "a" * 59) is True and rb.allowed("x", "/" + "a" * 60) is False


def test_truncated_file_gives_approximate_decisions_with_the_cap_named():
    head = "User-agent: Googlebot\nDisallow: /about\n\nUser-agent: *\nAllow: /\n"
    fill = "# " + "x" * 200 + "\n"
    body = head + fill * ((robots_mod.MAX_BYTES - len(head)) // len(fill) + 1) + "User-agent: Bingbot\nDisallow: /a\n"
    web = site(Web(), robots=body)
    c, s, _, _, pages = run3(web, ["/", "/about"], max_bytes=4 * robots_mod.MAX_BYTES)
    lim = by_code(s, "ROBOTS_LIMITS_EXCEEDED")
    assert lim and lim[0]["detail"]["truncated"] is True and c.robots["parsed"].truncated
    assert "ROBOTS_BLOCKS_SEARCH_CRAWLER" not in codes(s)
    approx = by_code(s, "ROBOTS_BLOCK_APPROXIMATE")
    assert approx and all(f["decision"] == "INSUFFICIENT_EVIDENCE" and f["detail"]["cap"] == "robots_bytes"
                          for f in approx)
    assert pages["/"]["fetch"].state == "OK"                             # RFC 9309 2.5: the first 500 KiB honoured


def test_a_rule_cut_by_the_parse_limit_is_not_read():
    text = "User-agent: *\n" + "#" * (robots_mod.MAX_BYTES - 30) + "\nDisallow: /abcdefghijklmnopqrstuvwxyz\n"
    rb = robots_mod.parse(text)
    assert rb.truncated and rb.allowed("x", "/abc") is True and rb.allowed("x", "/abcdefghijklmnopqrstuvwxyz") is True


def test_undecided_paths_are_never_counted_blocked(monkeypatch):
    monkeypatch.setattr(selene, "MATRIX_WORK", 12)
    web = site(Web(), robots="User-agent: *\nDisallow: /\n")
    _, s, _, _, _ = run3(web, ["/", "/about", "/x"])
    m = s["facts"]["robots_matrix"]
    undecided = sum(1 for fam in m.values() for d in fam["paths"].values() if d["allowed"] is None)
    blocked = sum(len(f["detail"]["blocked_paths"]) for f in s["findings"] if f["code"].startswith("ROBOTS_BLOCKS"))
    decided_false = sum(1 for fam in m.values() for d in fam["paths"].values() if d["allowed"] is False)
    assert undecided > 0 and blocked == decided_false and "ROBOTS_CHECK_INCOMPLETE" in codes(s)


def test_a_robots_rule_reaches_a_finding_only_in_its_safe_form():
    name = letters_marker()
    web = site(Web(), robots=f"User-agent: Googlebot\nDisallow: /{name}/\nDisallow: /about\n\nUser-agent: *\nAllow: /\n")
    with su.keyed(KEY):
        _, s, _, _, _ = run3(web, ["/", "/about", f"/{name}/x"])      # the audited path is the client's own input
    rules = [r for f in by_code(s, "ROBOTS_BLOCKS_SEARCH_CRAWLER") for r in f["detail"]["rules"]]
    assert "disallow: /about" in rules and name not in json.dumps(rules)
    assert any(re.fullmatch(r"disallow: /\{seg:[0-9a-f]{8}\}/", r) for r in rules)


def test_canonical_target_our_fetch_failed_is_insufficient_evidence():
    web = site(Web())
    web.route("/", page(canonical="http://site.test/canon", ld=ORG_LD))
    web.routes[("site.test", "/canon")] = lambda req: (_ for _ in ()).throw(fetch_mod.httpx.ConnectError("down"))
    _, s, _, _, _ = run3(web)
    assert "CANONICAL_TARGET_NOT_200" not in codes(s)
    f = by_code(s, "CANONICAL_TARGET_UNCHECKED")[0]
    assert (f["severity"], f["decision"], f["detail"]["fetch_state"]) == ("info", "INSUFFICIENT_EVIDENCE",
                                                                          "CONNECT_FAILED")
    web.route("/canon", page(), status=404)
    _, s, _, _, _ = run3(web)
    assert by_code(s, "CANONICAL_TARGET_NOT_200")[0]["decision"] == "ACT"   # a measured 404 is still a defect


def test_robots_unreachable_note_differs_from_a_rule():
    web = site(Web(), robots="x")
    web.route("/robots.txt", "down", status=503, ctype="text/plain")
    _, s, _, _, _ = run3(web, ["/about"])
    f = by_code(s, "PAGE_BLOCKED_FOR_OUR_CRAWLER")[0]
    assert f["detail"]["reason"] == "unreachable" and "disallows" not in f["detail"]["note"]


def test_gptbot_meta_noindex_is_reported_for_gptbot_only():
    web = site(Web())
    web.route("/", page(head="<meta name='GPTBot' content='noindex'>", ld=ORG_LD))
    _, s, _, _, _ = run3(web)
    assert "NOINDEX" not in codes(s)
    f = by_code(s, "NOINDEX_FOR_BOT")[0]
    assert f["detail"]["bots"] == ["GPTBot"] and f["decision"] == "WATCH"


def test_robots_over_the_parse_limit_has_a_limit_entry():
    body = "User-agent: *\nAllow: /\n" + "#" * (robots_mod.MAX_BYTES + 10)
    web = site(Web(), robots=body)
    c, s, _, _, _ = run3(web, max_bytes=4 * robots_mod.MAX_BYTES)
    assert c.robots["fetch"]["truncated"] is True and "ROBOTS_LIMITS_EXCEEDED" in codes(s)


def test_robots_fetch_still_too_large_stays_fail_closed(monkeypatch):
    # a robots body the fetcher could not read at all (no longer produced: robots.txt is read up to the parse
    # limit) keeps the round-1 behaviour: unreachable, critical, and our crawler does not fetch
    web = site(Web())
    real = fetch_mod.Fetcher.fetch

    def too_large(self, url, **kw):
        r = real(self, url, **kw)
        if url.endswith("/robots.txt"):
            r.state, r.body, r.partial = "TOO_LARGE", None, {"bytes_read": 9, "sha256": "0" * 64, "declared": None}
        return r
    monkeypatch.setattr(fetch_mod.Fetcher, "fetch", too_large)
    c, s, _, _, pages = run3(web, ["/about"])
    assert by_code(s, "ROBOTS_UNREACHABLE")[0]["severity"] == "critical"
    assert pages["/about"]["fetch"].state == "ROBOTS_CHECK_INCOMPLETE"


# ============================================================================================ M4 log robots check

def test_log_robots_check_matches_path_with_query_and_decodes_once():
    rb = robots_mod.parse("User-agent: *\nDisallow: /*?sort=\nDisallow: /search?\nDisallow: /%2541\n")
    lines = "\n".join(f'192.0.2.5 - - [10/Oct/2026:13:55:36 -0700] "GET {p} HTTP/1.1" 200 1 "-" "Googlebot/2.1"'
                      for p in ("/shop?sort=price", "/search?q=x", "/search?", "/%2541", "/A", "/shop"))
    d = logs_mod.process_chunk("combined", lines.encode(), b"k" * 32, lambda ip, t: "unverified", {},
                               {"left": 0}, robots=rb)
    assert d["families"]["Googlebot"]["robots_blocked"] == 4 and d["robots_unchecked"] == 0


# ============================================================================================ M5 the word rule

KEY = b"r2-test-key-0123456789abcdefghij"


@pytest.mark.parametrize("path,readable", [
    ("/blog/how-to-choose-running-shoes", True), ("/about-us", True), ("/collections/winter-jackets", True),
    ("/2024/05/best-coffee-grinder/", True), ("/blog/brown-rice-recipe", True), ("/shop/page/3/", True),
])
def test_ordinary_paths_stay_readable(path, readable):
    with su.keyed(KEY):
        out = su.safe_path(path)
    assert ("{seg" not in out) is readable


def test_letters_only_values_are_digests_keyed_per_tenant():
    v = letters_marker()
    with su.keyed(KEY):
        a = su.safe_path(f"/blog/{v}")
        a2 = su.safe_path(f"/people/{v}")
    with su.keyed(su.tenant_key(KEY, "other-tenant")):
        b = su.safe_path(f"/blog/{v}")
    assert v not in a + b and re.fullmatch(r"/blog/\{seg:[0-9a-f]{8}\}", a) and a != b
    assert a.split("/")[2] == a2.split("/")[2]                         # stable within a tenant
    assert su.safe_path(f"/blog/{v}") == "/blog/{seg}"                  # no key: no digest, never the value


@pytest.mark.parametrize("parent", ["team", "author", "user", "token", "reset", "unsubscribe"])
def test_a_segment_after_a_person_or_secret_context_is_a_digest(parent):
    with su.keyed(KEY):
        out = su.safe_path(f"/{parent}/sales")
    assert out.startswith(f"/{parent}/{{seg:")


def test_a_name_hidden_before_matrix_parameters_is_a_digest():
    v = letters_marker()
    with su.keyed(KEY):
        out = su.safe_path(f"/{v};jsessionid=1")
    assert v not in out


def test_every_surface_stores_letters_only_values_as_digests():
    v = letters_marker()
    robots = (f"User-agent: *\nAllow: /\nSitemap: http://site.test/sitemaps/{v}.xml\n"
              f"Sitemap: http://other.test/{v}/sm.xml\n")
    web = site(Web(), robots=robots)
    web.route(f"/sitemaps/{v}.xml", sitemap([f"http://site.test/team/{v}", f"http://other.test/{v}",
                                             "http://site.test/about"], lastmod=v), ctype="application/xml")
    web.route("/", "", status=301, ctype="text/plain", location=f"/landing/{v}")
    web.route(f"/landing/{v}", page(canonical=f"http://site.test/{v}-{v}", ld=ORG_LD))
    with su.keyed(KEY):
        c, s, d, r, pages = run3(web, ["/", "/about"], path_key=KEY)
        blob = json.dumps([s, d, r, c.osei.summary(), {p: v_["fetch"].summary() for p, v_ in pages.items()}])
    assert v not in blob and "{seg:" in blob


def test_log_path_templates_store_letters_only_values_as_digests():
    v = letters_marker()
    line = f'192.0.2.5 - - [10/Oct/2026:13:55:36 -0700] "GET /author/{v} HTTP/1.1" 200 1 "-" "Googlebot/2.1"'
    with su.keyed(KEY):
        d = logs_mod.process_chunk("combined", line.encode(), b"k" * 32, lambda ip, t: "unverified", {},
                                   {"left": 0})
    tpl = list(d["families"]["Googlebot"]["templates"])
    assert v not in json.dumps(tpl) and tpl[0].startswith("/author/{seg:")


def test_word_list_file_is_ascii_and_disclosed():
    with open(su.WORDS_FILE, "rb") as fh:
        raw = fh.read()
    assert raw.isascii() and b"[ordinary]" in raw and b"[exempt]" in raw and b"# counts:" in raw
    words, exempt = su._load_words()
    assert len(words) > 200_000 and exempt <= words


# ============================================================================================ Delia

def test_llms_txt_status_exactly_500_is_unreadable_not_absent():
    web = site(Web())
    web.route("/llms.txt", "err", status=500, ctype="text/plain")
    _, _, d, _, _ = run3(web)
    assert d["facts"]["llms_txt"]["state"] == "unreadable" and "LLMS_TXT_ABSENT" not in codes(d)


@pytest.mark.parametrize("enc", ["utf-32-le", "utf-32-be", "utf-32"])
def test_utf32_sitemap_is_read_not_called_malformed(enc):
    xml = sitemap(["http://site.test/", "http://site.test/about"]).replace("encoding='UTF-8'", "encoding='UTF-32'")
    web = site(Web())
    web.route("/sitemap.xml", xml.encode(enc), ctype="application/xml")
    c, _, d, _, _ = run3(web)
    assert d["facts"]["sitemap_urls"] == 2 and not c.osei.quarantined


def test_utf32_sitemap_with_a_doctype_is_still_refused():
    xml = ("<?xml version='1.0' encoding='UTF-32'?><!DOCTYPE x [<!ENTITY a 'b'>]>"
           "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>&a;</loc></url></urlset>")
    web = site(Web())
    web.route("/sitemap.xml", codecs.BOM_UTF32_LE + xml.encode("utf-32-le"), ctype="application/xml")
    c, _, d, _, _ = run3(web)
    assert c.osei.quarantined[0]["reason"] == "XML_DOCTYPE_REFUSED"


# ============================================================================================ Roman survivors

def _roman(web, paths=("/",)):
    return run3(web, paths)[3]


def test_title_minimum_boundary():
    web = site(Web())
    web.route("/", page(title="t" * 10, ld=ORG_LD))
    assert "TITLE_LENGTH" not in codes(_roman(web))
    web.route("/", page(title="t" * 9, ld=ORG_LD))
    assert "TITLE_LENGTH" in codes(_roman(web))


def test_one_invalid_jsonld_block_beside_a_valid_one_is_reported():
    web = site(Web())
    web.route("/", page(ld=ORG_LD, head="<script type='application/ld+json'>{broken</script>"))
    r = _roman(web)
    assert by_code(r, "STRUCTURED_DATA_INVALID_JSON")[0]["detail"]["blocks"] == 1


def test_undecided_crawler_access_is_not_a_pass(monkeypatch):
    web = site(Web(), robots="User-agent: *\nDisallow: /x\n")
    c = ctx(web, ["/"])
    _, pages = selene.run(c)
    monkeypatch.setattr(roman, "READINESS_WORK", 0)
    r = roman.run(c, pages)
    chk = r["facts"]["engine_readiness_heuristic"]["/"]["google"]["checks"]
    assert chk["crawler_allowed"] == "undecided"


def test_title_vs_h1_whitespace_normalised():
    web = site(Web())
    web.route("/", page(title="Z  Best\n Media", h1="Z Best Media", ld=ORG_LD))
    assert _roman(web)["facts"]["pages"]["/"]["title_same_as_h1"] is True


@pytest.mark.parametrize("lang", ["", "   "])
def test_empty_lang_is_missing(lang):
    web = site(Web())
    web.route("/", page(lang=lang, ld=ORG_LD))
    r = _roman(web)
    assert "LANG_MISSING" in codes(r) and r["facts"]["pages"]["/"]["lang"] is None


@pytest.mark.parametrize("tag,stored", [("zh-Hant-TW", "zh-Hant-TW"), ("sr-Latn", "sr-Latn"), ("en-Abcd-US", "en-US"),
                                        ("de-Zzzz", "de-Zzzz"), ("x-Qaab", "invalid")])
def test_script_subtag_is_validated_against_iso15924(tag, stored):
    assert roman.safe_lang(tag) == stored


# ============================================================================================ entity check

def test_entity_mismatch_stores_the_comparison_not_the_page_text():
    v = letters_marker()
    x = parse_html(page(ld='{"@context":"https://schema.org","@type":"Organization","name":"' + v + '"}'),
                   "http://site.test/")
    web = site(Web())
    c = ctx(web, ["/"])
    entity = {"entity_id": "e", "version": 1,
              "fields": {"name": {"value": "Z Best Media", "source": "owner", "authority": "owner"}}}
    with su.keyed(KEY):
        env = entity_check.run(c, {"/": {"fetch": None, "extract": x}}, entity)
    f = by_code(env, "ENTITY_FIELD_MISMATCH")[0]
    assert v not in json.dumps(env) and "on_site" not in f["detail"]
    cmp_ = f["detail"]["comparison"]
    assert cmp_["result"] == "mismatch" and cmp_["on_site_length"] == len(v) and len(cmp_["on_site_digest"]) == 8


# ============================================================================================ Osei

def _report(aid, envs, pages=None):
    return {"audit_id": aid, "tenant_id": "t", "domain": "site.test", "scheme": "http", "agents": envs,
            "pages": pages or {}, "versions": {"bot_families": "v", "schema_rules": "v"}, "prompt_set": None}


def _env(agent, outcome, findings, facts=None):
    return {"agent": agent, "task": "x", "outcome": outcome, "findings": findings, "facts": facts or {}}


def _f(code, url=None, sev="low", dec="ACT", **detail):
    return {"code": code, "url": url, "severity": sev, "decision": dec, "detail": detail}


@pytest.mark.parametrize("state", ["CONNECT_FAILED", "ROBOTS_CHECK_INCOMPLETE", "TIMEOUT"])
def test_drift_finding_with_a_failed_own_fetch_is_tool_failure(state):
    url = "http://site.test/x"
    a = _report("a", [_env("selene", "OK", [])])
    b = _report("b", [_env("selene", "OK", [_f("CANONICAL_TARGET_NOT_200", url, "high", "ACT", fetch_state=state,
                                               canonical="http://site.test/c")])])
    ch = drift.compare(a, b)["changes"]
    assert [c["class"] for c in ch] == ["TOOL_FAILURE"] and ch[0]["evidence"]["fetch_state_after"] == state


def test_drift_own_fetch_failed_in_the_older_run_only():
    url = "http://site.test/x"
    a = _report("a", [_env("selene", "OK", [_f("CANONICAL_TARGET_NOT_200", url, "high", "ACT",
                                               fetch_state="TIMEOUT")])])
    b = _report("b", [_env("selene", "OK", [_f("CANONICAL_TARGET_NOT_200", url, "medium", "ACT", fetch_state="OK")])])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["TOOL_FAILURE"]


def test_drift_robots_too_large_is_tool_failure():
    url = "http://site.test/robots.txt"
    a = _report("a", [_env("selene", "OK", [_f("ROBOTS_BLOCKS_SEARCH_CRAWLER", url, token="Googlebot")],
                           {"robots": {"status_class": "ok", "text_sha256": "1"}})])
    b = _report("b", [_env("selene", "OK", [], {"robots": {"status_class": "too_large", "text_sha256": "2"}})])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["TOOL_FAILURE"]


def test_drift_missing_llms_state_is_tool_failure():
    u = "http://site.test/llms.txt"
    facts = {"sitemaps_read": 1, "sitemap_urls": 2, "sitemap_fetch_failures": 0}
    a = _report("a", [_env("delia", "OK", [_f("LLMS_TXT_FORMAT", u, "low", "TEST")],
                           {**facts, "llms_txt": {"state": "present"}})])
    b = _report("b", [_env("delia", "OK", [], facts)])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["TOOL_FAILURE"]


def test_drift_sitemap_wide_change_with_robots_unchecked_is_tool_failure():
    facts = {"sitemaps_read": 1, "sitemap_urls": 2, "sitemap_fetch_failures": 0, "llms_txt": {"state": "present"}}
    a = _report("a", [_env("delia", "OK", [], facts)])
    b = _report("b", [_env("delia", "PARTIAL", [_f("SITEMAP_LISTS_ROBOTS_BLOCKED_URL", None, "medium")],
                           {**facts, "robots_unchecked": 3})])
    assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["TOOL_FAILURE"]


def test_drift_ruler_and_page_both_moved_is_unknown_r3():
    url = "http://site.test/"
    pa = {"/": {"fetch": {"state": "OK", "status": 200}, "fingerprint": {"title": "1"}}}
    pb = {"/": {"fetch": {"state": "OK", "status": 200}, "fingerprint": {"title": "2"}}}
    a = _report("a", [_env("roman", "OK", [_f("TITLE_LENGTH", url)])], pa)
    b = _report("b", [_env("roman", "OK", [])], pb)
    b["versions"] = {"bot_families": "v2", "schema_rules": "v"}
    ch = drift.compare(a, b)["changes"][0]
    assert ch["class"] == "UNKNOWN" and ch["evidence"]["rule"] == "R3"


def test_drift_key_separates_bot_tokens():
    url = "http://site.test/robots.txt"
    facts = {"robots": {"status_class": "ok", "text_sha256": "1"}}
    a = _report("a", [_env("selene", "OK", [_f("ROBOTS_BLOCKS_SEARCH_CRAWLER", url, token="Googlebot")], facts)])
    b = _report("b", [_env("selene", "OK", [_f("ROBOTS_BLOCKS_SEARCH_CRAWLER", url, token="Bingbot")], facts)])
    out = drift.compare(a, b)
    assert sorted(c["change"] for c in out["changes"]) == ["APPEARED", "DISAPPEARED"]


def test_oversized_jsonld_is_recorded_truncated_with_its_whole_size():
    big = '{"@context":"https://schema.org","@type":"Organization","name":"' + "n" * (MAX_JSONLD_BYTES + 5000) + '"}'
    web = site(Web())
    web.route("/", page(ld=big))
    c, _, _, r, _ = run3(web, max_bytes=2 * 1024 * 1024)
    q = [x for x in c.osei.quarantined if x["kind"] == "structured_data"][0]
    whole = len(big.encode())
    prefix = big[:MAX_JSONLD_BYTES].encode()
    assert q["reason"] == "JSONLD_TOO_LARGE" and q["truncated"] is True and q["total_bytes"] == whole
    assert q["bytes"] == len(prefix) and q["sha256"] == hashlib.sha256(prefix).hexdigest()
    assert "prefix" in q["size_basis"] and "STRUCTURED_DATA_INVALID_JSON" not in codes(r)
    assert by_code(r, "STRUCTURED_DATA_BLOCK_TOO_LARGE")[0]["detail"]["total_bytes"] == [whole]


def test_homepage_phone_observation_stores_redirect_targets_safe():
    v = letters_marker()
    web = site(Web())
    web.route("/", "", status=301, ctype="text/plain", location=f"/landing/{v}")
    web.route(f"/landing/{v}", page(ld=ORG_LD))
    c, _, _, _, pages = run3(web)
    entity = {"entity_id": "e", "version": 1,
              "fields": {"telephone": {"value": "".join(str(random.randint(2, 9)) for _ in range(10)),
                                     "source": "owner", "authority": "owner"}}}
    with su.keyed(KEY):
        env = entity_check.run(c, pages, entity)
    obs = env["facts"]["homepage_phone_observation"]
    assert obs["reason"] == "FINAL_URL_DIFFERS" and v not in json.dumps(env)


def test_invalid_octet_in_a_fetched_robots_file_is_kept_as_itself():
    # the fetcher decodes robots.txt with surrogateescape: a raw 0xFF octet in the file matches %FF, never %FE
    web = site(Web())
    web.route("/robots.txt", b"User-agent: *\nDisallow: /a\xff\n", ctype="text/plain")
    web.route("/a%FE", page())
    assert web.fetcher().fetch("http://site.test/a%FF", robots_cache={}).state == "BLOCKED_BY_ROBOTS"
    assert web.fetcher().fetch("http://site.test/a%FE", robots_cache={}).state == "OK"


# ============================================================================================ mutant survivors (M4)

def test_a_single_cjk_character_is_not_an_ascii_letter():
    with su.keyed(KEY):
        out = su.safe_path("/blog/\u9f98")                  # one rare Han character: a word only if listed
    assert out.startswith("/blog/{seg:")


def test_index_children_cap_is_ten():
    from cert_web import index
    web = site(Web())
    kids = [f"http://site.test/k{i}.xml" for i in range(12)]
    web.route("/sitemap.xml", index(kids), ctype="application/xml")
    for k in kids:
        web.route(k.removeprefix("http://site.test"), sitemap(["http://site.test/"]), ctype="application/xml")
    _, _, d, _, _ = run3(web)
    assert (d["facts"]["sitemap_children_followed"], d["facts"]["sitemap_children_over_cap"]) == (10, 2)


@pytest.mark.parametrize("n,flag", [(50_000, False), (50_001, True)])
def test_sitemap_url_limit_boundary(n, flag):
    web = site(Web())
    web.route("/sitemap.xml", sitemap((f"http://site.test/{i}" for i in range(n)), lastmod=None),
              ctype="application/xml")
    _, _, d, _, _ = run3(web, max_bytes=8 * 1024 * 1024)
    assert ("SITEMAP_TOO_MANY_URLS" in codes(d)) is flag


def test_sitemaps_attempted_counts_failed_ones():
    web = site(Web(), robots="User-agent: *\nAllow: /\nSitemap: http://site.test/a.xml\nSitemap: http://site.test/b.xml\n")
    web.route("/a.xml", sitemap(["http://site.test/"]), ctype="application/xml")
    web.route("/b.xml", "down", status=503, ctype="text/plain")
    _, _, d, _, _ = run3(web)
    assert (d["facts"]["sitemaps_attempted"], d["facts"]["sitemaps_read"]) == (2, 1)


def test_readiness_from_a_truncated_robots_file_is_undecided():
    body = "User-agent: *\nAllow: /\n" + "#" * (robots_mod.MAX_BYTES + 10)
    web = site(Web(), robots=body)
    _, _, _, r, _ = run3(web, max_bytes=4 * robots_mod.MAX_BYTES)
    assert r["facts"]["engine_readiness_heuristic"]["/"]["google"]["checks"]["crawler_allowed"] == "undecided"


def test_roman_compares_title_and_h1_after_folding_white_space():
    web = site(Web())
    c = ctx(web, ["/"])
    _, pages = selene.run(c)
    x = pages["/"]["extract"]
    x["title"] = "Z  Best\tMedia"                            # an extract as handed to Roman, not re-folded
    x["headings"] = [{"level": 1, "text": "Z Best Media"}]
    assert roman.run(c, pages)["facts"]["pages"]["/"]["title_same_as_h1"] is True


def test_white_space_lang_is_absent_not_invalid():
    assert roman.safe_lang("   ") is None and roman.safe_lang("\t") is None


def test_drift_failed_agent_outcome_is_tool_failure():
    facts = {"sitemaps_read": 1, "sitemap_urls": 2, "sitemap_fetch_failures": 0, "llms_txt": {"state": "present"}}
    a = _report("a", [_env("roman", "OK", [_f("TITLE_LENGTH", "http://site.test/")], facts)])
    b = _report("b", [_env("roman", "FAILED", [], facts)])
    ch = drift.compare(a, b)["changes"][0]
    assert ch["class"] == "TOOL_FAILURE" and ch["evidence"]["outcome_after"] == "FAILED"


def test_a_compressed_robots_bomb_is_cut_at_the_parse_limit():
    import gzip
    web = site(Web())
    web.routes[("site.test", "/robots.txt")] = (200, gzip.compress(b"User-agent: *\nAllow: /\n" + b"#" * 5_000_000),
                                                {"content-type": "text/plain", "content-encoding": "gzip"})
    rb = web.fetcher().robots("http://site.test")
    assert rb["fetch"]["truncated"] is True and rb["fetch"]["bytes"] == robots_mod.MAX_BYTES
    assert rb["parsed"].truncated and rb["parsed"].allowed("x", "/a") is True
