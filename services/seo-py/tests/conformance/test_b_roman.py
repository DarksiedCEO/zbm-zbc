"""SPEC section 7: Roman (B-RO-01..11), against the heading and BCP 47 oracles."""

from __future__ import annotations

import json

import pytest

from agents import roman
from agents_run import by_code, codes, one, run3, sev
from cert_web import ORG_LD, Web, page, site
from common import check_ran, rng
from gen import plants
from oracles import bcp47, headings


def _r(web, paths=("/",)):
    return run3(web, paths)[3]


@pytest.mark.parametrize("n,flag", [(9, True), (10, False), (60, False), (61, True), (0, None)])
def test_B_RO_01_title_bounds_and_text_never_stored(n, flag):
    m = plants.uniq(rng(n, "ro01"))
    title = (m * 7)[:n]
    web = site(Web())
    web.route("/", page(title=title, ld=ORG_LD))
    r = _r(web)
    if n == 0:
        assert sev(one(r, "TITLE_MISSING")) == ("high", "measured", "ACT")
    else:
        assert ("TITLE_LENGTH" in codes(r)) is flag
    p = r["facts"]["pages"]["/"]
    assert p["title_length"] == n and m not in json.dumps(r)


def test_B_RO_01_same_as_h1_whitespace_folded():
    web = site(Web())
    web.route("/", page(title="Z  Best Media", h1="Z Best   Media", ld=ORG_LD))
    assert _r(web)["facts"]["pages"]["/"]["title_same_as_h1"] is True


def test_B_RO_02_meta_description():
    for i in range(10):
        web = site(Web())
        web.route("/", page(ld=ORG_LD).replace("<meta name='description' content='Revenue recovery.'>", ""))
        assert sev(one(_r(web), "META_DESCRIPTION_MISSING")) == ("low", "measured", "TEST")


@pytest.mark.parametrize("h1s,flag", [(0, True), (1, False), (2, True), (3, True)])
def test_B_RO_03_h1_count(h1s, flag):
    body = "".join(f"<h1>H{i}</h1>" for i in range(h1s)) + "<p>" + "text " * 200 + "</p>"
    web = site(Web())
    web.route("/", page(h1=None, body=body, ld=ORG_LD))
    assert ("H1_COUNT" in codes(_r(web))) is flag


@pytest.mark.parametrize("seed", [7, 11])
def test_B_RO_04_heading_skips_oracle(seed):
    headings.self_test()
    r = rng(seed, "ro04")
    for i in range(500):
        levels = [r.randint(1, 6) for _ in range(r.randint(1, 12))]
        body = "".join(f"<h{lv}>x</h{lv}>" for lv in levels) + "<p>" + "w " * 300 + "</p>"
        web = site(Web())
        web.route("/", page(h1=None, body=body, ld=ORG_LD).replace("<h2>What we do</h2>", ""))
        rr = _r(web)
        want = headings.skips(levels)
        got = by_code(rr, "HEADING_LEVEL_SKIP")
        assert (got[0]["detail"]["skips"] if got else []) == want, (levels, got)
    check_ran("B-RO-04", 1000)


def _tag(r):
    langs = ["en", "de", "fr", "zh", "sr", "pt", "es", "ja", "xx", "qq", "abc", "123", "e", "english"]
    t = r.choice(langs)
    if r.random() < 0.5:
        t += "-" + r.choice(("Latn", "Hant", "Cyrl", "Qqqq", "Zzzz", "Abcd"))
    if r.random() < 0.6:
        t += "-" + r.choice(("US", "GB", "TW", "419", "XK", "QQ", "ZZ", "AA", "BR"))
    if r.random() < 0.1:
        t += "-" + r.choice(("1996", "fonipa", "u-ca-gregory"))
    if r.random() < 0.1:
        t = t.upper()
    if r.random() < 0.05:
        t = t.replace("-", "_")
    return t


@pytest.mark.parametrize("seed", [7, 11])
def test_B_RO_05_lang_bcp47_and_cldr(seed):
    bcp47.self_test()
    r = rng(seed, "ro05")
    for i in range(2000):
        t = _tag(r)
        assert roman.safe_lang(t) == bcp47.stored(t), (t, roman.safe_lang(t), bcp47.stored(t))
    check_ran("B-RO-05", 4000)


def test_B_RO_05_lang_invalid_finding_and_readiness_fail():
    web = site(Web())
    web.route("/", page(lang="123", ld=ORG_LD))
    r = _r(web)
    assert sev(one(r, "LANG_INVALID")) == ("low", "measured", "ACT")
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    for eng in ("google", "bing", "openai", "anthropic", "perplexity"):
        assert rd[eng]["checks"]["lang_declared"] == "fail", (eng, rd[eng])


@pytest.mark.parametrize("lang", ["", " ", "\t"])
def test_B_RO_05_lang_missing(lang):
    web = site(Web())
    web.route("/", page(lang=lang, ld=ORG_LD))
    assert sev(one(_r(web), "LANG_MISSING")) == ("low", "measured", "ACT")


JSONLD = [
    ("{not json", "STRUCTURED_DATA_INVALID_JSON"),
    ('{"@context":"https://schema.org","@type":"Organization"}', "STRUCTURED_DATA_RECOMMENDED_MISSING"),
    ('{"@context":"https://schema.org","@type":"Product"}', "STRUCTURED_DATA_ERRORS"),
]


@pytest.mark.parametrize("ld,code", JSONLD * 30)
def test_B_RO_06_jsonld_rules(ld, code):
    web = site(Web())
    web.route("/", page(ld=ld))
    r = _r(web)
    assert code in codes(r), (ld, codes(r))


def test_B_RO_06_oversized_block_and_absent_severity():
    big = '{"@type":"Organization","name":"' + "a" * 262_200 + '"}'
    web = site(Web())
    web.route("/", page(ld=big))
    c, _, _, r, _, _ = run3(web, max_bytes=2 * 1024 * 1024)
    assert sev(one(r, "STRUCTURED_DATA_BLOCK_TOO_LARGE")) == ("medium", "measured", "TEST")
    q = [x for x in c.osei.summary()["quarantined"] if x["reason"] == "JSONLD_TOO_LARGE"][0]
    assert q["bytes"] == 262_144 and q["size_basis"] == "block_prefix" and q["total_bytes"] == len(big)
    web = site(Web())
    web.route("/", page())
    web.route("/about", page())
    r = _r(web, ("/", "/about"))
    sd = {f["url"]: f["severity"] for f in by_code(r, "STRUCTURED_DATA_ABSENT")}
    assert sorted(sd.values()) == ["low", "medium"]


def test_B_RO_07_types_readable_only_from_the_rule_table():
    m = plants.uniq(rng(7, "ro07")).capitalize()
    ld = '[{"@context":"https://schema.org","@type":"Organization","name":"x","sameAs":["https://e.test"]},' \
         '{"@context":"https://schema.org","@type":"' + m + '"}]'
    web = site(Web())
    web.route("/", page(ld=ld))
    r = _r(web)
    blob = json.dumps(r)
    assert m not in blob and "Organization" in blob
    p = r["facts"]["pages"]["/"]
    assert p["unknown_type"] == 1
    import os
    src = os.path.join(os.path.dirname(roman.__file__), "schema_types.py")
    assert not os.path.exists(src)                       # SPEC 3 / A-32: no schema.org vocabulary bundled


def test_B_RO_08_entity_markup():
    web = site(Web())
    web.route("/", page(ld='{"@context":"https://schema.org","@type":"Organization","name":"x"}'))
    assert sev(one(_r(web), "ENTITY_SAMEAS_MISSING")) == ("low", "measured", "TEST")
    web = site(Web())
    web.route("/", page(ld='{"@context":"https://schema.org","@type":"WebPage","name":"x"}'))
    assert sev(one(_r(web), "ENTITY_MARKUP_ABSENT")) == ("medium", "measured", "TEST")


@pytest.mark.parametrize("n,flag", [(499, True), (500, False)])
def test_B_RO_09_raw_text_boundary(n, flag):
    web = site(Web())
    # the title "t" and the page's one-character separator count: n - 2 x's make n visible characters
    web.route("/", page(title="t", h1=None, body="x" * (n - 2), ld=ORG_LD).replace("<h2>What we do</h2>", ""))
    assert ("LITTLE_TEXT_IN_RAW_HTML" in codes(_r(web))) is flag


def test_B_RO_10_readiness_labels_and_no_score():
    r = _r(site(Web()))
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    assert set(rd) >= {"google", "bing", "openai", "anthropic", "perplexity"}
    for e in rd.values():
        assert e["heuristic"] is True and e["calibrated"] is False and e.get("label")
        assert set(e["checks"].values()) <= {"pass", "fail", "undecided", "n/a"}
    import re
    assert not re.search(r'"[^"]*(score|grade|percent|rating)[^"]*":', json.dumps(r))


def test_B_RO_11_outcomes():
    assert _r(site(Web()))["outcome"] == "OK"
    web = site(Web())
    web.route("/about", b"\x1f\x8b\0", ctype="text/html", content_encoding="gzip")
    assert _r(web, ("/", "/about"))["outcome"] == "PARTIAL"
    web = site(Web())
    web.route("/", b"\x1f\x8b\0", ctype="text/html", content_encoding="gzip")
    assert _r(web)["outcome"] == "INSUFFICIENT_EVIDENCE"
