"""SPEC section 1: the robots.txt primitive (B-RB-01..11), against the independent RFC 9309 oracle."""

from __future__ import annotations

import time

import pytest

from common import check_ran, rng, seed_note, seeds
from gen import robots_gen as rg
from oracles import rfc9309
from primitives import robots as robots_mod

TOK = rg.TOKENS


def _svc(data: bytes):
    return robots_mod.parse(data.decode("utf-8", "surrogateescape"))


def _dec(parsed, token: str, path: bytes, work=None):
    return parsed.decide(token, path.decode("utf-8", "surrogateescape"), work)


def _differential(seed: int, n: int, regime_of) -> tuple:
    r = rng(seed, "rb-diff")
    ran = wrong = fail_open = 0
    for i in range(n):
        regime = regime_of(i, r)
        data, paths = rg.regime_case(r, regime)
        parsed = _svc(data)
        for p in paths:
            tok = r.choice(TOK + ("ZBM-SEO-Audit",))
            want = rfc9309.allowed(data, tok, p)
            got = _dec(parsed, tok, p)["allowed"]
            ran += 1
            if got is not want:
                wrong += 1
                if got is True and want is False:
                    fail_open += 1
                assert wrong < 5, (f"oracle disagreement {seed_note(seed, i)}: {data!r} {tok} {p!r} -> "
                                   f"service {got}, oracle {want}")
    return ran, wrong, fail_open


@pytest.mark.parametrize("seed", seeds())
def test_B_RB_01_B_RB_02_B_RB_03_group_selection_specificity_wildcards_agree_with_rfc9309(seed):
    rfc9309.self_test()
    ran, wrong, _ = _differential(seed, 4000, lambda i, r: r.choice(rg.REGIMES))
    assert wrong == 0
    check_ran("B-RB-01", ran // 6)


@pytest.mark.lead_gate
@pytest.mark.parametrize("seed", seeds(lead=True))
def test_B_RB_01_lead_gate_differential(seed):
    ran, wrong, _ = _differential(seed, 40000, lambda i, r: r.choice(rg.REGIMES))
    assert wrong == 0
    check_ran("B-RB-01", ran // 6, lead=True)


RFC_HAND = [  # (file, token, path, allowed) from RFC 9309 2.2.2 / 2.2.3 and Google's documented examples
    (b"User-agent: *\nDisallow: /fish\n", "x", b"/fish.html", False),
    (b"User-agent: *\nDisallow: /fish\n", "x", b"/Fish.asp", True),
    (b"User-agent: *\nDisallow: /fish*\n", "x", b"/fishheads/yummy.html", False),
    (b"User-agent: *\nDisallow: /fish/\n", "x", b"/fish", True),
    (b"User-agent: *\nDisallow: /*.php\n", "x", b"/folder/filename.php?parameters", False),
    (b"User-agent: *\nDisallow: /*.php$\n", "x", b"/filename.php?parameters", True),
    (b"User-agent: *\nDisallow: /fish*.php\n", "x", b"/Fish.PHP", True),
    (b"User-agent: *\nAllow: /p\nDisallow: /\n", "x", b"/page", True),
    (b"User-agent: *\nAllow: /folder\nDisallow: /folder\n", "x", b"/folder/page", True),
    (b"User-agent: *\nAllow: /page\nDisallow: /*.htm\n", "x", b"/page.htm", False),
    (b"User-agent: *\nAllow: /$\nDisallow: /\n", "x", b"/", True),
    (b"User-agent: *\nAllow: /$\nDisallow: /\n", "x", b"/page.htm", False),
    (b"User-agent: *\nDisallow: /a$b\n", "x", b"/a%24b", False),
    (b"User-agent: *\nDisallow: /a**b\n", "x", b"/axxb", False),
    (b"User-agent: *\nDisallow:\n", "x", b"/", True),
    (b"User-agent: *\nDisallow: /\n", "x", b"/robots.txt", True),
]


@pytest.mark.parametrize("data,tok,path,allowed", RFC_HAND)
def test_B_RB_03_hand_cases_from_rfc9309_and_google(data, tok, path, allowed):
    assert rfc9309.allowed(data, tok, path) is allowed
    assert _dec(_svc(data), tok, path)["allowed"] is allowed


def _gen_hand(seed: int, n: int) -> list:
    """200 hand-shaped cases (B-RB-03): every wildcard / anchor combination of short patterns."""
    r = rng(seed, "rb-hand")
    out = []
    for _ in range(n):
        pat = "/" + "".join(r.choice(["a", "b", "*", "**"]) for _ in range(r.randint(1, 4)))
        if r.random() < 0.4:
            pat += "$"
        if r.random() < 0.2:
            pat = pat[:2] + "$" + pat[2:]                # an inner '$' is the literal %24
        path = "/" + "".join(r.choice("ab$") for _ in range(r.randint(0, 6)))
        out.append((f"User-agent: *\nDisallow: {pat}\n".encode(), path.encode()))
    return out


@pytest.mark.parametrize("seed", seeds())
def test_B_RB_03_generated_wildcard_and_anchor_cases(seed):
    cases = _gen_hand(seed, 200)
    for i, (data, path) in enumerate(cases):
        assert _dec(_svc(data), "x", path)["allowed"] is rfc9309.allowed(data, "x", path), (data, path,
                                                                                         seed_note(seed, i))
    assert len(cases) >= 200


@pytest.mark.parametrize("seed", seeds())
def test_B_RB_04_A_04_reserved_and_invalid_utf8_encodings_never_fail_open(seed):
    # >= 25% of rule sets use a reserved encoding, >= 25% an invalid UTF-8 octet
    def regime(i, r):
        return ("pct_reserved", "invalid_utf8", r.choice(rg.REGIMES), r.choice(rg.REGIMES))[i % 4]
    ran, wrong, fail_open = _differential(seed, 4000, regime)
    assert wrong == 0 and fail_open == 0
    check_ran("B-RB-04", ran // 6)
    check_ran("A-04", ran // 6)


def test_B_RB_04_two_different_invalid_octets_never_equal():
    data = b"User-agent: *\nDisallow: /\xff\n"
    p = _svc(data)
    assert _dec(p, "x", b"/\xff")["allowed"] is False and _dec(p, "x", b"/\xfe")["allowed"] is True
    assert _dec(p, "x", b"/%FF")["allowed"] is False


def test_B_RB_04_our_crawler_never_fetches_a_disallowed_path():
    from cert_web import Web, ctx, site
    r = rng(7, "rb-fetch")
    fetched_disallowed = 0
    for i in range(60):
        rules = [b"Disallow: " + rg.pattern(r, r.choice(("pct_reserved", "plain", "pct_unreserved")))
                 for _ in range(3)] + [b"Allow: " + rg.pattern(r, "pct_reserved")]
        data = b"User-agent: *\n" + b"\n".join(rules) + b"\n"
        web = site(Web(), robots=data.decode("latin-1"))
        path = rg.path(r, r.choice(("pct_reserved", "plain")))
        if not path.startswith(b"/") or b"%" in path[-2:] or b"$" in path or b"*" in path:
            continue
        c = ctx(web, paths=[path.decode()])
        url = "http://site.test" + path.decode()
        web.route(path.decode().split("?")[0], "<p>x</p>")
        c.fetcher.fetch(url, robots_cache=c.robots_cache)
        if not rfc9309.allowed(data, "ZBM-SEO-Audit", path):
            fetched_disallowed += sum(1 for h, q in web.requests if q != "/robots.txt")
    assert fetched_disallowed == 0


def test_B_RB_05_bom_is_stripped_and_blocks_everything():
    p = _svc(b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n")
    assert _dec(p, "Googlebot", b"/a")["allowed"] is False and p.bom_stripped and p.lines_ignored == 0
    p2 = _svc(b"User-agent: *\n\xef\xbb\xbfDisallow: /\n")
    assert not p2.bom_stripped


@pytest.mark.parametrize("seed", seeds())
def test_B_RB_06_lines_ignored_count_is_exact(seed):
    r = rng(seed, "rb-lines")
    ran = 0
    for i in range(500):
        want = 0
        lines = []
        if r.random() < 0.5:
            lines.append("Disallow: /pre")                # a rule before any user-agent line
            want += 1
        lines.append(r.choice(("User-agent: *", "useragent: *", "user_agent: *", "USER-AGENT: *")))
        for _ in range(r.randint(0, 8)):
            k = r.random()
            if k < 0.2:
                lines.append("no colon here")
                want += 1
            elif k < 0.35:
                lines.append("Crawl-delay: 10")
                want += 1
            elif k < 0.45:
                lines.append("# only a comment")
            elif k < 0.55:
                lines.append("Sitemap: http://site.test/s.xml")
            else:
                lines.append(r.choice(("Allow", "Disallow", "allow", "DISALLOW")) + ": /x" + str(r.randint(0, 9)))
        sep = r.choice(("\n", "\r\n", "\r"))
        p = robots_mod.parse(sep.join(lines) + sep)
        assert p.lines_ignored == want, (lines, seed_note(seed, i))
        ran += 1
    check_ran("B-RB-06", ran)


def test_B_RB_07_parse_limit_counts_octets_not_characters():
    rule = "User-agent: *\nDisallow: /early\n"
    filler = "#" + "\u30c4" * 299_999 + "\n"             # 300,000 three-octet characters: 900,000+ octets
    text = rule + filler + "Disallow: /late\n"
    p = robots_mod.parse(text)
    assert p.truncated and p.bytes_read <= robots_mod.MAX_BYTES == 512_000
    assert p.decide("x", "/early")["allowed"] is False and p.decide("x", "/late")["allowed"] is True
    assert p.decide("x", "/early")["complete"] is False


def test_B_RB_07_the_cut_rule_is_absent():
    head = "User-agent: *\n" + "#" * (robots_mod.MAX_BYTES - 30) + "\n"
    text = head + "Disallow: /cut-in-the-middle-of-this-rule\n"
    p = robots_mod.parse(text)
    assert p.truncated and p.decide("x", "/cut-in-the-middle-of-this-rule")["allowed"] is True


def test_B_RB_09_work_is_deterministic_and_clock_free():
    data, path = rg.k_star(rng(7, "rb9"), 8, 2000)
    runs = []
    for _ in range(3):
        w = robots_mod.Work(robots_mod.STEP_WORK_UNITS)
        p = _svc(data)
        d = [_dec(p, "x", path + b"%d" % i, w)["allowed"] for i in range(20)]
        runs.append((d, w.units))
    assert runs[0] == runs[1] == runs[2]


def test_B_RB_09_undecided_when_the_charge_would_pass_the_limit():
    p = _svc(rg.flood(rng(7, "rb9b"), 2000, "shared"))
    d = p.decide("x", "/shared/prefix/000001", robots_mod.Work(3))
    assert d["allowed"] is None and d["undecided"] == "work"


def test_B_RB_10_undecided_and_incomplete_never_counted_by_any_caller():
    # every caller keeps None apart: Selene's matrix, Delia's listed URLs, the log task (their own tests);
    # here the primitive's contract: a None decision carries no rule and the truncated flag reaches complete
    p = robots_mod.parse("User-agent: *\nDisallow: /a\n", truncated=True)
    d = p.decide("x", "/a")
    assert d["allowed"] is False and d["complete"] is False
    d = p.decide("x", "/a", robots_mod.Work(0))
    assert d["allowed"] is None and d["rule"] is None


@pytest.mark.parametrize("seed", seeds())
def test_B_RB_11_a_quoted_rule_is_a_rule_of_the_file(seed):
    r = rng(seed, "rb11")
    for i in range(500):
        data, paths = rg.regime_case(r, r.choice(rg.REGIMES))
        p = _svc(data)
        text = data.decode("utf-8", "surrogateescape")
        values = set()
        for line in text.splitlines():
            k, sep, v = line.split("#", 1)[0].partition(":")
            if sep and k.strip().lower() in ("allow", "disallow"):
                values.add(k.strip().lower() + ": " + robots_mod._collapse(v.strip()))
        for path in paths:
            rule = _dec(p, "Googlebot", path)["rule"]
            assert rule is None or rule in values, (rule, seed_note(seed, i))


def test_B_RB_09_one_decision_is_fast_on_ordinary_files():
    data = rg.flood(rng(7, "rb9c"), 1500, "distinct")
    p = _svc(data)
    t0 = time.thread_time()
    for i in range(500):
        p.decide("Googlebot", "/p%06d/x" % i)
    assert (time.thread_time() - t0) / 500 < 0.005


def _robots_outcome(status=None, location=None, network=False, loop=False):
    from cert_web import Web
    web = Web()
    if network:
        f = web.fetcher(resolver=lambda h, p: (_ for _ in ()).throw(OSError("down")))
        return f.robots("http://site.test")["status_class"]
    if loop:
        web.route("/robots.txt", "", status=301, location="/robots.txt", ctype="text/plain")
    elif location:
        web.route("/robots.txt", "", status=status, location=location, ctype="text/plain")
        web.route("/r2.txt", "User-agent: *\nDisallow: /x\n", ctype="text/plain")
    else:
        web.route("/robots.txt", "User-agent: *\nDisallow: /x\n", status=status, ctype="text/plain")
    return web.fetcher().robots("http://site.test")["status_class"]


def test_B_RB_08_http_outcome_classes():
    ran = 0
    for st in list(range(200, 207)) * 3:
        assert _robots_outcome(st) == "ok"
        ran += 1
    for st in (301, 302, 303, 307, 308) * 4:
        assert _robots_outcome(st, location="/r2.txt") == "ok"
        ran += 1
    for st in [s for s in range(400, 500) if s != 429][:20]:
        assert _robots_outcome(st) == "unavailable", st
        ran += 1
    for st in list(range(500, 512)) + [429] * 8:
        assert _robots_outcome(st) == "unreachable", st
        ran += 1
    for _ in range(20):
        assert _robots_outcome(network=True) == "unreachable"
        assert _robots_outcome(loop=True) == "unreachable"
        ran += 2
    check_ran("B-RB-08", ran // 6)


@pytest.mark.parametrize("pattern,path,want", [
    (b"/*%41", b"//%7e*", True),            # 'A' never matches the hex digit inside the %2A of a raw '*'
    (b"/*A", b"/x*", True), (b"/*2A", b"/x*", True), (b"/*%2A", b"/x*", False), (b"/*F", b"/%FF", True),
    (b"/*%46", b"/%ff", True), (b"/*E3", b"/\xe3\x83\x84", True), (b"/a*3", b"/a%3F", True)] * 3)
def test_B_RB_04_a_literal_piece_matches_whole_octets_only(pattern, path, want):
    """B-RB-03 / B-RB-04 octet comparison (found by the lead-gate differential, seed 14): a literal piece never
    matches part of an escaped octet. ``want`` = allowed under ``Disallow: <pattern>``."""
    data = b"User-agent: *\nDisallow: " + pattern + b"\n"
    assert rfc9309.allowed(data, "Googlebot", path) is want
    assert _dec(_svc(data), "Googlebot", path)["allowed"] is want
