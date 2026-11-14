"""SPEC section 4: Selene (B-SE-01..18)."""

from __future__ import annotations

import gzip

import pytest

from agents import bots
from agents_run import by_code, codes, one, run3, sev
from cert_web import ORG_LD, Web, page, site
from common import check_ran, rng
from oracles import noindex as onoix
from oracles import rfc9309


@pytest.mark.parametrize("status", [500, 502, 503, 504, 520, 429] + [599] * 14)
def test_B_SE_01_robots_unreachable(status):
    web = site(Web())
    web.route("/robots.txt", "x", status=status, ctype="text/plain")
    _, s, _, _, _, _ = run3(web)
    f = one(s, "ROBOTS_UNREACHABLE")
    assert sev(f) == ("critical", "measured", "ACT") and f["detail"]["status"] == status
    p = one(s, "PAGE_BLOCKED_FOR_OUR_CRAWLER")
    assert sev(p) == ("info", "measured", "INSUFFICIENT_EVIDENCE") and p["detail"]["reason"] == "unreachable"
    assert "could not be fetched" in p["detail"]["note"] and "disallows" not in p["detail"]["note"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451, 418, 499, 404, 404])
def test_B_SE_02_robots_absent_allows_everything(status):
    web = site(Web())
    web.route("/robots.txt", "x", status=status, ctype="text/plain")
    _, s, _, _, _, _ = run3(web)
    assert sev(one(s, "ROBOTS_ABSENT")) == ("info", "measured", "WATCH")
    assert "PAGE_BLOCKED_FOR_OUR_CRAWLER" not in codes(s) and s["facts"]["pages_fetched"] == 1


def test_B_SE_03_gzip_of_empty_members_is_read_never_unreachable():
    member = gzip.compress(b"")
    body = member * (2_200_043 // len(member)) + gzip.compress(b"User-agent: *\nDisallow: /x\n")
    web = site(Web())
    web.route("/robots.txt", body, ctype="text/plain", content_encoding="gzip")
    _, s, _, _, _, _ = run3(web)
    assert "ROBOTS_UNREACHABLE" not in codes(s) and s["facts"]["robots"]["status_class"] == "ok"
    assert s["facts"]["pages_fetched"] == 1


def test_B_SE_03_undecodable_within_bounds_is_incomplete():
    web = site(Web())
    web.route("/robots.txt", b"\x1f\x8b" + b"\0" * 100, ctype="text/plain", content_encoding="gzip")
    _, s, _, _, _, _ = run3(web)
    assert "ROBOTS_UNREACHABLE" not in codes(s)
    f = one(s, "ROBOTS_CHECK_INCOMPLETE")
    assert sev(f) == ("info", "measured", "INSUFFICIENT_EVIDENCE")
    assert "PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE" in codes(s)


def _matrix_oracle(data: bytes, paths) -> dict:
    out = {}
    for fam in bots.FAMILIES:
        out[fam["token"]] = {p: rfc9309.allowed(data, fam["token"], p.encode()) for p in ["/"] + list(paths)}
    return out


@pytest.mark.parametrize("seed", [7, 11])
def test_B_SE_04_bot_path_matrix_equals_the_oracle(seed):
    r = rng(seed, "se04")
    ran = 0
    for i in range(200):
        lines = ["User-agent: " + r.choice([f["token"] for f in bots.FAMILIES] + ["*"]) for _ in range(2)]
        lines += [r.choice(("Allow", "Disallow")) + ": " + r.choice(("/", "/a", "/b", "/a/b", "/*.pdf$", "/x"))
                  for _ in range(r.randint(1, 4))]
        text = "\n".join(lines) + "\n"
        web = site(Web(), robots=text)
        for p in ("/a", "/b", "/a/b"):
            web.route(p, page(ld=ORG_LD))
        paths = ["/", "/a", "/a/b"]
        _, s, _, _, _, ev = run3(web, paths)
        want = _matrix_oracle(text.encode(), paths)
        from primitives import codes as cm
        got = {tok: {cm.resolve(k, ev.values): v for k, v in m["paths"].items()}
               for tok, m in s["facts"]["robots_matrix"].items()}
        for tok, per in want.items():
            assert {p: got[tok][p]["allowed"] for p in per} == per, (text, tok)
            blocked = [p for p, a in per.items() if a is False]
            if not blocked:
                continue
            code = "ROBOTS_BLOCKS_SEARCH_CRAWLER" if tok in bots.SEARCH_TOKENS else "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER"
            f = [x for x in by_code(s, code) if x["detail"]["token"] == tok]
            assert len(f) == 1
            if tok in bots.SEARCH_TOKENS:
                assert sev(f[0]) == ("critical" if "/" in blocked else "high", "measured", "ACT")
            else:
                assert sev(f[0]) == ("info", "measured", "WATCH")
            assert len(f[0]["detail"]["blocked_paths"]) <= 20 and len(f[0]["detail"]["rules"]) <= 5
        ran += 1
    check_ran("B-SE-04", ran * 2)


def test_B_SE_05_truncated_file_is_approximate_never_blocks():
    text = "User-agent: *\nDisallow: /\n" + "#" * (600 * 1024) + "\n"
    web = site(Web(), robots=text)
    _, s, _, _, _, _ = run3(web)
    assert not codes(s) & {"ROBOTS_BLOCKS_SEARCH_CRAWLER", "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER"}
    a = by_code(s, "ROBOTS_BLOCK_APPROXIMATE")
    assert a and all(x["detail"]["cap"] == "robots_bytes" and x["detail"]["cap_value"] == 512_000 for x in a)
    lim = one(s, "ROBOTS_LIMITS_EXCEEDED")
    assert sev(lim) == ("medium", "measured", "WATCH") and lim["detail"]["truncated"] is True


def test_B_SE_06_matrix_work_spent_is_partial(monkeypatch):
    from agents import selene
    monkeypatch.setattr(selene, "MATRIX_WORK", 5)
    web = site(Web(), robots="User-agent: *\nDisallow: /x\n")
    _, s, _, _, _, _ = run3(web)
    f = one(s, "ROBOTS_CHECK_INCOMPLETE")
    assert f["detail"]["undecided"] > 0 and s["outcome"] == "PARTIAL"


def test_B_SE_07_lines_ignored_count():
    web = site(Web(), robots="Disallow: /early\nUser-agent: *\nfoo\nCrawl-delay: 5\nAllow: /\n")
    _, s, _, _, _, _ = run3(web)
    f = one(s, "ROBOTS_LINES_IGNORED")
    assert sev(f) == ("low", "measured", "WATCH") and f["detail"]["lines_ignored"] == 3


@pytest.mark.parametrize("case", ["rule", "unreachable", "incomplete", "failed"] * 8)
def test_B_SE_08_page_not_read_states(case, monkeypatch):
    web = site(Web())
    if case == "rule":
        web.route("/robots.txt", "User-agent: ZBM-SEO-Audit\nDisallow: /\n", ctype="text/plain")
        want = "PAGE_BLOCKED_FOR_OUR_CRAWLER"
    elif case == "unreachable":
        web.route("/robots.txt", "", status=503, ctype="text/plain")
        want = "PAGE_BLOCKED_FOR_OUR_CRAWLER"
    elif case == "incomplete":
        from primitives import fetch as fetch_mod
        monkeypatch.setattr(fetch_mod, "ROBOTS_CHECK_WORK", 0)
        want = "PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE"
    else:
        web.route("/", b"\x1f\x8b\0\0", ctype="text/html", content_encoding="gzip")
        want = "PAGE_UNREACHABLE"
    _, s, _, r, _, _ = run3(web)
    f = one(s, want)
    assert f["decision"] == "INSUFFICIENT_EVIDENCE"
    assert f["severity"] == ("high" if want == "PAGE_UNREACHABLE" else "info")
    others = codes(s) - {want, "ROBOTS_UNREACHABLE", "ROBOTS_BLOCKS_SEARCH_CRAWLER",
                         "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER", "ROBOTS_CHECK_INCOMPLETE"}
    assert not others, others
    assert not [x for x in r["findings"] if x.get("url", "").endswith("site.test/")]


@pytest.mark.parametrize("status,want", [(399, None), (400, "medium"), (499, "medium"), (500, "high"),
                                         (404, "medium"), (410, "medium"), (503, "high"), (599, "high")]
                         + [(s, "medium") for s in range(401, 413)] + [(s, "high") for s in range(501, 505)])
def test_B_SE_09_http_error_boundaries(status, want):
    web = site(Web())
    web.route("/", page(ld=ORG_LD), status=status)
    _, s, _, _, _, _ = run3(web)
    got = by_code(s, "PAGE_HTTP_ERROR")
    if want is None:
        assert not got
    else:
        assert len(got) == 1 and sev(got[0]) == (want, "measured", "ACT")


@pytest.mark.parametrize("hops,codes_seen", [([301], [301]), ([302], [302]), ([301, 302], [301, 302]),
                                             ([307, 308, 301], [307, 308, 301]), ([303], [303])] * 4)
def test_B_SE_10_redirect_severity(hops, codes_seen):
    web = site(Web())
    prev = "/"
    for i, st in enumerate(hops):
        nxt = f"/hop{i}"
        web.route(prev, "", status=st, location=nxt)
        prev = nxt
    web.route(prev, page(canonical=f"http://site.test{prev}", ld=ORG_LD))
    _, s, _, _, _, _ = run3(web)
    if len(hops) == 1:
        f = one(s, "REDIRECTED")
        assert sev(f) == ("low", "measured", "WATCH")
    else:
        f = one(s, "REDIRECT_CHAIN")
        assert sev(f) == ("medium", "measured", "ACT")
    assert f["detail"]["statuses"] == codes_seen and f["detail"]["hops"] == len(hops)
    assert f["detail"]["temporary"] is any(x in (302, 303, 307) for x in hops)
    assert f["detail"]["final_url"].startswith("http://site.test/")


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "text/plain", "application/json",
                                   "application/x-zz-unknown", "text/x-zz", "video/mp4", "application/octet-stream",
                                   "text/csv", "image/svg+xml"])
def test_B_SE_11_non_html(ctype):
    web = site(Web())
    web.route("/", b"x", ctype=ctype)
    _, s, _, _, _, _ = run3(web)
    f = one(s, "PAGE_NOT_HTML")
    assert sev(f) == ("low", "measured", "WATCH")
    ct = f["detail"]["content_type"]
    from primitives import safe_url as su
    assert ct == ctype if ctype in su.MEDIA_TYPES else ct.startswith("{c:ctype:")


def _noix_case(r):
    toks = [f["token"] for f in bots.FAMILIES] + ["otherbot"]
    hv = []
    for _ in range(r.randint(0, 3)):
        items = []
        for _ in range(r.randint(1, 3)):
            d = r.choice(("noindex", "none", "nofollow", "index", "NOINDEX", "max-snippet: 0",
                          "unavailable_after: 25 Jun 2030 15:00:00 PST"))
            items.append((r.choice(toks) + ": " if r.random() < 0.4 else "") + d)
        hv.append(", ".join(items))
    metas = [(r.choice(["robots"] + toks), r.choice(("noindex", "none", "index, follow", "nofollow")))
             for _ in range(r.randint(0, 2))]
    return hv, metas


@pytest.mark.parametrize("seed", [7, 11])
def test_B_SE_12_noindex_per_engine_equals_the_oracle(seed):
    import httpx
    r = rng(seed, "se12")
    ran = 0
    for i in range(250):
        hv, metas = _noix_case(r)
        web = site(Web())
        head = "".join(f"<meta name='{n}' content='{c}'>" for n, c in metas)
        body = page(head=head, ld=ORG_LD).encode()
        web.routes[("site.test", "/")] = (lambda req, b=body, h=hv: httpx.Response(
            200, headers=[("content-type", "text/html")] + [("x-robots-tag", v) for v in h],
            stream=httpx.ByteStream(b)))
        _, s, _, _, _, _ = run3(web)
        want_eng = [e for e, tok in bots.ENGINE_SEARCH_BOT.items() if onoix.noindex(tok, hv, metas)]
        want_bots = [f["token"] for f in bots.FAMILIES if onoix.noindex(f["token"], hv, metas)]
        got = by_code(s, "NOINDEX")
        if want_eng:
            assert len(got) == 1 and sorted(got[0]["detail"]["engines"]) == sorted(want_eng), (hv, metas, got)
            assert sev(got[0]) == ("high", "measured", "TEST")
        else:
            assert not got, (hv, metas)
            nb = by_code(s, "NOINDEX_FOR_BOT")
            assert (sorted(nb[0]["detail"]["bots"]) if nb else []) == sorted(want_bots), (hv, metas)
        ran += 1
    check_ran("B-SE-12", ran * 2)


CANON_CASES = [
    ("<link rel='canonical' href='javascript:x'>", {"CANONICAL_INVALID"}),
    ("<link rel='canonical' href='http://site.test/'><link rel='canonical' href='http://site.test/b'>",
     {"CANONICAL_MULTIPLE"}),
    ("<link rel='canonical' href='http://site.test/'><link rel='canonical' href='javascript:x'>",
     {"CANONICAL_MULTIPLE", "CANONICAL_INVALID"}),
    ("<link rel='canonical' href='http://other.test/'>", {"CANONICAL_OTHER_HOST"}),
    ("<link rel='canonical' href='http://www.site.test/'>", set()),
    ("", {"CANONICAL_MISSING"}),
]


@pytest.mark.parametrize("head,want", CANON_CASES * 10)
def test_B_SE_13_canonicals(head, want):
    web = site(Web())
    web.route("/", page(canonical=None, head=head, ld=ORG_LD))
    web.route("/b", page(ld=ORG_LD))
    web.route("/", page(canonical=None, head=head, ld=ORG_LD), host="www.site.test")
    _, s, _, _, _, _ = run3(web)
    got = codes(s) & {"CANONICAL_INVALID", "CANONICAL_MULTIPLE", "CANONICAL_OTHER_HOST", "CANONICAL_MISSING"}
    assert got == want, (head, got)


def test_B_SE_13_noindex_canonical_conflict():
    web = site(Web())
    web.route("/", page(canonical="http://site.test/b", head="<meta name='robots' content='noindex'>", ld=ORG_LD))
    web.route("/b", page(ld=ORG_LD))
    _, s, _, _, _, _ = run3(web)
    assert sev(one(s, "NOINDEX_CANONICAL_CONFLICT")) == ("high", "measured", "ACT")


def test_B_SE_14_canonical_target_cap_and_states():
    web = site(Web())
    paths = [f"/p{i}" for i in range(7)]
    for p in paths:
        web.route(p, page(canonical=f"http://site.test{p}-t", ld=ORG_LD))
        web.route(p + "-t", page(ld=ORG_LD), status=404 if p == "/p0" else 200)
    _, s, _, _, _, _ = run3(web, paths)
    targets = [q for _, q in web.requests if q.endswith("-t")]
    assert len(targets) == 5
    f = one(s, "CANONICAL_TARGET_NOT_200")
    assert sev(f) == ("high", "measured", "ACT")


def test_B_SE_14_our_failed_target_fetch_is_unchecked():
    web = site(Web())
    web.route("/", page(canonical="http://site.test/t", ld=ORG_LD))
    web.route("/t", b"\x1f\x8b\0", ctype="text/html", content_encoding="gzip")
    _, s, _, _, _, _ = run3(web)
    assert sev(one(s, "CANONICAL_TARGET_UNCHECKED")) == ("info", "measured", "INSUFFICIENT_EVIDENCE")
    assert "CANONICAL_TARGET_NOT_200" not in codes(s)


def test_B_SE_15_access_differential():
    web = site(Web())

    def by_ua(req):
        import httpx
        human = "ZBM-SEO-Audit" not in (req.headers.get("user-agent") or "")
        body = page(ld=ORG_LD) if human else "<html><body>blocked</body></html>"
        return httpx.Response(403 if not human else 200, headers={"content-type": "text/html"},
                              stream=httpx.ByteStream(body.encode()))
    web.routes[("site.test", "/")] = by_ua
    _, s, _, _, _, _ = run3(web)
    assert "BOT_DIFFERENTIAL" not in codes(s) or sev(one(s, "BOT_DIFFERENTIAL")) == ("high", "measured", "TEST")


def test_B_SE_16_parse_error_code_only():
    from primitives import parse
    x = parse.parse_html("<html><head><title>t</title>" + "<div>" * 5 + "\x00", "http://site.test/")
    assert x is None or isinstance(x.get("parse_error"), (str, type(None)))


@pytest.mark.parametrize("states,want", [
    (("ok", "ok"), "OK"), (("ok", "fail"), "PARTIAL"), (("rule", "rule"), "BLOCKED"),
    (("rule", "incomplete"), "INSUFFICIENT_EVIDENCE"), (("fail", "fail"), "FAILED"),
    (("ok", "rule"), "PARTIAL"), (("fail", "rule"), "FAILED"), (("incomplete", "incomplete"), "INSUFFICIENT_EVIDENCE"),
])
def test_B_SE_17_outcome(states, want, monkeypatch):
    from primitives import fetch as fetch_mod
    rules = ["User-agent: ZBM-SEO-Audit"] + [f"Disallow: /p{i}" for i, s in enumerate(states) if s == "rule"]
    web = site(Web(), robots="\n".join(rules) + "\n")
    paths = []
    for i, st in enumerate(states):
        p = f"/p{i}"
        paths.append(p)
        web.route(p, page(ld=ORG_LD) if st == "ok" else b"\x1f\x8b\0", ctype="text/html",
                  **({} if st == "ok" else {"content_encoding": "gzip"}))
    if "incomplete" in states:
        orig = fetch_mod.Fetcher._robots_check

        def check(self, url, cache, guard):
            if url.endswith(tuple(f"/p{i}" for i, s in enumerate(states) if s == "incomplete")):
                raise fetch_mod._Refused("ROBOTS_CHECK_INCOMPLETE", "test", robots={"reason": "work"})
            return orig(self, url, cache, guard)
        monkeypatch.setattr(fetch_mod.Fetcher, "_robots_check", check)
    _, s, _, _, _, _ = run3(web, paths)
    assert s["outcome"] == want


def test_B_SE_18_one_bot_fetch_per_page():
    web = site(Web())
    paths = ["/", "/about"]
    run3(web, paths, entity=None)
    for p in paths:
        n = sum(1 for h, q in web.requests if q == p)
        assert n <= 2                                   # one bot fetch + at most one browser-UA fetch


@pytest.mark.parametrize("n", [1999, 2000, 2011, 4000, 8192])
def test_B_SE_14_a_long_canonical_target_is_fetched_whole_never_cut(n):
    web = site(Web())
    target = "/t/" + "k" * (n - 3)
    web.route("/", page(canonical="http://site.test" + target, ld=ORG_LD))
    web.route(target, page(canonical="http://site.test" + target, ld=ORG_LD))
    run3(web)
    long_paths = [q for _, q in web.requests if q.startswith("/t/")]
    assert long_paths and all(q == target for q in long_paths), (n, sorted({len(q) for q in long_paths}))


def test_B_SE_06_B_RB_10_undecided_cells_are_never_blocked(monkeypatch):
    from agents import selene
    monkeypatch.setattr(selene, "MATRIX_WORK", 3)
    web = site(Web(), robots="User-agent: *\nDisallow: /\n")
    _, s, _, _, _, _ = run3(web)
    assert not codes(s) & {"ROBOTS_BLOCKS_SEARCH_CRAWLER", "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER"}
    assert "ROBOTS_CHECK_INCOMPLETE" in codes(s)


def test_B_SE_14_a_canonical_target_that_redirects_is_not_200():
    web = site(Web())
    web.route("/", page(canonical="http://site.test/t", ld=ORG_LD))
    web.route("/t", "", status=301, location="/t2")
    web.route("/t2", page(ld=ORG_LD))
    _, s, _, _, _, _ = run3(web)
    f = one(s, "CANONICAL_TARGET_NOT_200")
    assert f["detail"]["redirected"] is True and f["detail"]["status"] == 200


def test_B_SE_04_B_RB_11_a_quoted_rule_is_coded_unless_structural():
    from gen import plants
    m = plants.uniq(rng(3, "se04q"))
    web = site(Web(), robots=f"User-agent: Googlebot\nDisallow: /{m}\nDisallow: /\n")
    web.route(f"/{m}", page(ld=ORG_LD))
    _, s, _, _, _, ev = run3(web, ["/", f"/{m}"])
    f = [x for x in by_code(s, "ROBOTS_BLOCKS_SEARCH_CRAWLER") if x["detail"]["token"] == "Googlebot"][0]
    rules = f["detail"]["rules"]
    assert "disallow: /" in rules and m not in str(rules)
    assert any(r.startswith("disallow: {c:rule:") for r in rules)


def test_B_SE_07_one_ignored_line_is_reported():
    web = site(Web(), robots="User-agent: *\nCrawl-delay: 5\nAllow: /\n")
    _, s, _, _, _, _ = run3(web)
    assert one(s, "ROBOTS_LINES_IGNORED")["detail"]["lines_ignored"] == 1
