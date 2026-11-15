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
    if r.random() < 0.08:
        # rv-wave1v2 #6: private use only and every grandfathered tag (RFC 5646 2.1), in any case
        t = r.choice(sorted(bcp47.GRANDFATHERED) + ["x-default", "x-a", "x-12345678-b", "X-Private", "x-toolongxx"])
        return t.upper() if r.random() < 0.2 else t
    langs = ["en", "de", "fr", "zh", "sr", "pt", "es", "ja", "xx", "qq", "abc", "123", "e", "english",
             "qaa", "qfy", "qtz", "qua"]
    t = r.choice(langs)
    if r.random() < 0.5:
        t += "-" + r.choice(("Latn", "Hant", "Cyrl", "Qqqq", "Zzzz", "Abcd", "Qaaa", "Qaap", "Qabx", "Qaby"))
    if r.random() < 0.6:
        t += "-" + r.choice(("US", "GB", "TW", "419", "XK", "QQ", "ZZ", "AA", "BR", "QM", "QZ", "XA", "XZ",
                                   "YY", "JX", "999"))    # the last three: not CLDR regions (dropped, ro-12)
    if r.random() < 0.2:
        # variants and extensions, sometimes repeated (RFC 5646 2.2.5 / 2.2.6: a repeat is invalid)
        t += "-" + r.choice(("1996", "fonipa", "u-ca-gregory", "1996-1996", "fonipa-1996", "a-bbb-a-ccc",
                             "a-bbb-b-ccc", "fonipa-FONIPA", "u-ca-gregory-x-u-x"))
    if r.random() < 0.15:
        t += "-x-" + r.choice(("southern", "a", "1996-1996", "a-a"))
    if r.random() < 0.1:
        t = t.upper()
    if r.random() < 0.05:
        t = t.replace("-", "_")
    return t


@pytest.mark.parametrize("seed", [7, 11])
def test_B_RO_05_lang_bcp47_and_cldr(seed):
    bcp47.self_test()
    from agents_run import coding
    from primitives import codes as codes_mod
    r = rng(seed, "ro05")
    kinds = set()
    with coding():
        for i in range(2000):
            t = _tag(r)
            want = bcp47.stored(t)
            if isinstance(want, tuple):
                want = codes_mod.code("lang", want[1])       # valid, nothing on the allowlist: its code (D-01)
                kinds.add("coded")
            else:
                kinds.add("invalid" if want == "invalid" else "stored" if want else "none")
            assert roman.safe_lang(t) == want, (t, roman.safe_lang(t), want)
    assert {"coded", "invalid", "stored"} <= kinds, kinds
    check_ran("B-RO-05", 4000)


@pytest.mark.parametrize("tag,want", [
    ("x-default", "coded"), ("i-klingon", "coded"), ("en-GB-oed", "coded"), ("sgn-BE-FR", "coded"),
    ("zh-min-nan", "coded"), ("qaa-Qaaa-QM-x-southern", "qaa-Qaaa-QM"), ("de-1996-1996", "invalid"),
    ("en-a-bbb-a-ccc", "invalid")])
def test_B_RO_05_private_use_grandfathered_and_repeats(tag, want):
    """rv-wave1v2 #6: valid private-use / grandfathered / qaa~qtz tags are declared (no LANG_INVALID, readiness
    pass); a repeated variant or singleton is LANG_INVALID."""
    web = site(Web())
    web.route("/", page(lang=tag, ld=ORG_LD))
    r = _r(web)
    lang = r["facts"]["pages"]["/"]["lang"]
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    if want == "invalid":
        assert "LANG_INVALID" in codes(r) and lang == "invalid"
        assert all(rd[e]["checks"]["lang_declared"] == "fail" for e in rd)
    else:
        assert "LANG_INVALID" not in codes(r) and "LANG_MISSING" not in codes(r)
        assert (lang.startswith("{c:lang:") and tag.lower() not in json.dumps(r).lower()) if want == "coded" \
            else lang == want
        assert all(rd[e]["checks"]["lang_declared"] == "pass" for e in rd), rd


def test_B_RO_05_lang_invalid_finding_and_readiness_fail():
    web = site(Web())
    web.route("/", page(lang="123", ld=ORG_LD))
    r = _r(web)
    assert sev(one(r, "LANG_INVALID")) == ("low", "measured", "ACT")
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    for eng in ("google", "bing", "openai", "anthropic", "perplexity"):
        assert rd[eng]["checks"]["lang_declared"] == "fail", (eng, rd[eng])


def _ext_tag(r):
    """A tag with extensions and / or private use, its subtags often repeating a variant's spelling (RFC 5646 2.2.5
    / 2.2.6: only a repeated VARIANT or a repeated SINGLETON is invalid; extension and private-use subtags may
    repeat anything)."""
    alnum = "abcdefghijklmnopqrstuvwxyz0123456789"

    def word(n):
        return "".join(r.choice(alnum) for _ in range(n))
    variants = r.sample(["1901", "1996", "fonipa", "abcde", "valencia"], r.randint(0, 2))
    t = r.choice(["en", "de", "de-DE", "sr-Latn", "zh-Hant-TW", "en-US"]) + "".join("-" + v for v in variants)
    pool = variants + ["abcde", "1901", "ca", "gregory", word(2), word(5), word(8)]
    singles = r.sample(list("abcdeuvt0129"), r.randint(1, 3))
    if r.random() < 0.15:
        singles.append(singles[0])                         # a repeated singleton: invalid
    for sg in singles:
        t += "-" + sg + "".join("-" + r.choice(pool) for _ in range(r.randint(1, 3)))
    if r.random() < 0.5:
        t += "-x" + "".join("-" + r.choice(pool + [word(1)]) for _ in range(r.randint(1, 3)))
    return t.upper() if r.random() < 0.1 else t


@pytest.mark.parametrize("tag", ["en-a-abcde-b-abcde", "de-DE-1901-u-1901", "de-1996-a-1996", "en-a-bbb-x-bbb",
                                 "en-fonipa-x-fonipa", "sr-Latn-u-ca-gregory-t-ca", "en-a-abcde-x-a-abcde"])
def test_B_RO_05_extension_subtags_may_repeat_a_variant(tag):
    """rv-wave1v2r2 #3 (reviewer mutant n17): an extension or private-use subtag that spells like a variant is not a
    variant; these RFC-valid tags are valid (stored language[-script][-region])."""
    assert bcp47.well_formed(tag) and bcp47.stored(tag) not in ("invalid", None)
    from agents_run import coding
    with coding():
        assert roman.safe_lang(tag) == bcp47.stored(tag), (tag, roman.safe_lang(tag))


@pytest.mark.parametrize("seed", [3, 5])
def test_B_RO_05_generated_extension_and_private_use_tags_equal_the_oracle(seed):
    from agents_run import coding
    from primitives import codes as codes_mod
    r = rng(seed, "ro05-ext")
    seen = set()
    with coding():
        for i in range(2000):
            t = _ext_tag(r)
            want = bcp47.stored(t)
            if isinstance(want, tuple):
                want = codes_mod.code("lang", want[1])
            seen.add("invalid" if want == "invalid" else "valid")
            assert roman.safe_lang(t) == want, (t, roman.safe_lang(t), want)
    assert seen == {"invalid", "valid"}, seen
    check_ran("B-RO-05", 4000)                     # two seeds of 2,000


@pytest.mark.parametrize("title,n", [("  Z   Best  ", 6), ("a\tb\nc", 5), ("   ", 0)])
def test_B_RO_01_title_measured_white_space_folded_in_the_extract(title, n):
    """rv-wave1v2r2 #5 ro-18: Roman folds white space in the title of the extract it is given (a stored / older
    extract), whatever the parser did to it."""
    from agents import selene
    from agents_run import coding
    from cert_web import ctx
    web = site(Web())
    web.route("/", page(ld=ORG_LD))
    c = ctx(web)
    with coding():
        _, pages = selene.run(c)
        pages["/"]["extract"]["title"] = title
        r = roman.run(c, pages)
    p = r["facts"]["pages"]["/"]
    assert p["title_length"] == n and p["title_empty"] is (n == 0)


LANG_PROBE_VALID = ["en-US-u-ca-gregory", "de-DE-u-co-phonebk", "en-a-bbbbb-b-ccc", "sr-Latn-RS-u-nu-latn",
                    "en-US-x-twain", "x-default", "x-whatever-abc", "zh-Hant-TW-u-nu-hanidec",
                    "ja-JP-u-ca-japanese-x-lvariant-JP", "en-u-abcde-fghij", "en-t-de-latn", "es-419-u-ca-buddhist",
                    "de-CH-1901-u-co-phonebk", "en-US-u-ca-gregory-va-posix", "en-a-myext-b-another", "en-u-12345",
                    "fr-FR-x-12345678", "en-GB-u-sd-gbsct", "en-t-0abc", "hy-Latn-IT-arevela-u-co-trad"]
LANG_PROBE_INVALID = ["en-US-u", "en-x", "en--US", "en-u-ca-u-nu", "de-1996-1996", "en-a-bbb-a-ccc", "e", "en-US-",
                      "123"]
LANG_PROBE_LONG = ["de-CH-1901-u-co-phonebk-ka-shifted-x-abc", "en-US-u-ca-gregory-nu-latn-x-a-bcdefgh",
                   "sr-Latn-RS-u-ca-gregory-nu-latn-x-1"]


def test_B_RO_05_the_reviewers_lang_probe_tags():
    """rv-wave1v2r3 F-2 (lang.py, ported): RFC 5646 valid tags are valid, malformed ones invalid."""
    from agents_run import coding
    with coding():
        assert [t for t in LANG_PROBE_VALID if roman.safe_lang(t) in (None, "invalid")] == []
        assert [t for t in LANG_PROBE_INVALID if roman.safe_lang(t) != "invalid"] == []


@pytest.mark.parametrize("tag", LANG_PROBE_LONG)
def test_B_RO_05_a_valid_tag_longer_than_35_characters_is_validated_whole(tag):
    """rv-wave1v2r3 F-2: the parser keeps the whole lang value (35 characters was a cut inside a subtag), so a valid
    long tag is valid: stored language[-script][-region] equal to the oracle, no LANG_INVALID."""
    from agents_run import coding
    from primitives.parse import parse_html
    assert len(tag) >= 35 and bcp47.well_formed(tag)
    x = parse_html(f"<html lang='{tag}'><head><title>t</title></head><body>b</body></html>", "http://site.test/")
    assert x["lang"] == tag
    with coding():
        assert roman.safe_lang(x["lang"]) == bcp47.stored(tag)
    web = site(Web())
    web.route("/", page(lang=tag, ld=ORG_LD))
    assert "LANG_INVALID" not in codes(_r(web))


@pytest.mark.parametrize("v,n,want", [
    ("en-US-u-ca-gregory", 9, "en-US"),            # cut inside 'ca', then the dangling singleton 'u' dropped
    ("en-US-u-ca-gregory", 10, "en-US-u-ca"),      # the cut falls on a hyphen: 'ca' is whole
    ("de-CH-1901", 8, "de-CH"),
    ("en-US", 5, "en-US"),
    ("abcdefghij", 4, "abcd"),                     # no whole subtag fits: the plain cut (invalid either way)
])
def test_B_RO_05_the_stored_lang_copy_drops_whole_subtags_only(v, n, want):
    """rv-wave1v2r3 F-2 / RFC 5646 4.4.2: past the bound the copy drops whole subtags (and a singleton left last)."""
    from primitives.parse import _lang_copy
    assert _lang_copy(v, n) == want


def test_B_RO_05_a_tag_past_the_string_bound_keeps_whole_subtags():
    from agents_run import coding
    from primitives.parse import STR_MAX, parse_html
    tag = "en-GB-x-" + "-".join(f"a{i:07d}" for i in range(80))
    assert len(tag) > STR_MAX and bcp47.well_formed(tag)
    x = parse_html(f"<html lang='{tag}'><head><title>t</title></head><body>b</body></html>", "http://site.test/")
    assert len(x["lang"]) <= STR_MAX and tag.startswith(x["lang"] + "-") and bcp47.well_formed(x["lang"])
    with coding():
        assert roman.safe_lang(x["lang"]) == "en-GB"


@pytest.mark.parametrize("lang", ["", " ", "\t"])
def test_B_RO_05_lang_missing(lang):
    web = site(Web())
    web.route("/", page(lang=lang, ld=ORG_LD))
    assert sev(one(_r(web), "LANG_MISSING")) == ("low", "measured", "ACT")


@pytest.mark.parametrize("lang", [" ", "\t", " \n\u3000 "])
def test_B_RO_05_white_space_lang_in_the_extract_is_missing(lang):
    """rv-wave1v2 #7 o01: Roman decides LANG_MISSING on the extract it is given, whatever the parser did to it: an
    extract whose lang is only white space (a stored / older extract) is missing, never valid or invalid."""
    from agents_run import coding
    from cert_web import ctx
    from agents import selene
    web = site(Web())
    web.route("/", page(ld=ORG_LD))
    c = ctx(web)
    with coding():
        _, pages = selene.run(c)
        pages["/"]["extract"]["lang"] = lang
        r = roman.run(c, pages)
    assert sev(one(r, "LANG_MISSING")) == ("low", "measured", "ACT") and "LANG_INVALID" not in codes(r)
    assert r["facts"]["pages"]["/"]["lang"] is None


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


# ---- rv-wave1v2r4 F-3 (B-RO-05, lead ruling 18:40 PT option (c)): validity is decided on the WHOLE raw attribute
def _fill(base: str, n: int, lo: int, hi: int) -> str:
    """``base`` plus ``-`` subtags of lo..hi 'a' characters, exactly ``n`` characters long (synthetic)."""
    s = base
    while len(s) < n:
        k = min(hi, n - len(s) - 1)
        if 0 < n - len(s) - 1 - k <= lo:      # leave room for one more whole subtag
            k = max(lo, k - lo - 1)
        s += "-" + "a" * k
    assert len(s) == n
    return s


def _valid(n):
    return _fill("en-GB-x", n, 1, 8)


def _invalid(n):                              # a 9-character private-use subtag last
    return _valid(n - 10) + "-" + "b" * 9


def _dup_singleton(n):                        # the extension singleton 'u' twice, the second near the end
    return _fill("en-u", n - 5, 2, 8) + "-u-ab"


F3_CASES = [("reviewer 600 invalid last subtag", _invalid(600), False),
            ("reviewer 693 duplicate singleton", _dup_singleton(693), False),
            ("500 valid", _valid(500), True), ("500 invalid", _invalid(500), False),
            ("501 valid", _valid(501), True), ("501 invalid", _invalid(501), False),
            ("2000 valid", _valid(2000), True), ("2000 invalid", _invalid(2000), False),
            ("2000 duplicate singleton", _dup_singleton(2000), False)]


@pytest.mark.parametrize("name,tag,ok", F3_CASES, ids=[c[0] for c in F3_CASES])
def test_B_RO_05_lang_validity_is_decided_on_the_whole_raw_attribute(name, tag, ok):
    """rv-wave1v2r4 F-3: valid / invalid follows the whole attribute (oracle: RFC 5646 well-formedness), never the
    500-character copy; > 500 invalid -> LANG_INVALID + lang_declared fail; > 500 valid -> pass. The stored copy is
    unchanged (whole subtags, RFC 5646 4.4.2) and the raw value is not kept in the extract."""
    from primitives.parse import STR_MAX, _lang_copy, parse_html
    assert bcp47.well_formed(tag) is ok, name
    x = parse_html(f"<html lang='{tag}'><head><title>t</title></head><body>b</body></html>", "http://site.test/")
    assert x["lang_valid"] is ok and x["lang"] == _lang_copy(tag) and len(x["lang"]) <= STR_MAX
    assert (len(tag) <= STR_MAX) == (x["lang"] == tag)
    assert [k for k, v in x.items() if isinstance(v, str) and len(v) > STR_MAX and v == tag] == []
    web = site(Web())
    web.route("/", page(lang=tag, ld=ORG_LD))
    r = _r(web)
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    assert ("LANG_INVALID" in codes(r)) is (not ok) and "LANG_MISSING" not in codes(r)
    assert r["facts"]["pages"]["/"]["lang"] == ("en-GB" if ok else "invalid")
    assert all(rd[e]["checks"]["lang_declared"] == ("pass" if ok else "fail") for e in rd), rd


@pytest.mark.parametrize("lang,want", [(None, None), ("", None), ("  ", None), ("en-US", True), ("123", False)])
def test_B_RO_05_the_parser_lang_validity_bit(lang, want):
    from primitives.parse import parse_html
    attr = "" if lang is None else f" lang='{lang}'"
    x = parse_html(f"<html{attr}><body>b</body></html>", "http://site.test/")
    assert x["lang_valid"] is want


def test_B_RO_05_lang_validity_is_linear_on_a_100_kb_attribute():
    """rv-wave1v2r4 F-3 / lead ruling: no extra lang bound; a 100 KB attribute is decided whole, in linear time."""
    import time
    from primitives.parse import parse_html
    for tag, ok in ((_valid(100_000), True), (_invalid(100_000), False), (_dup_singleton(100_000), False)):
        doc = f"<html lang='{tag}'><body>b</body></html>"
        t0 = time.monotonic()
        x = parse_html(doc, "http://site.test/")
        t1 = time.monotonic() - t0
        t0 = time.monotonic()
        parse_html(f"<html lang='{tag[:10_000]}'><body>b</body></html>", "http://site.test/")
        t2 = time.monotonic() - t0
        assert x["lang_valid"] is ok and t1 < 2.0 and t1 < 40 * t2 + 0.2, (t1, t2)


# ---- lead ruling 18:25 PT (C11b, B-RO-05): per-page lang agrees with the validity bit; codes are of the WHOLE value
def _pu_one_char(n):                          # private use, all 1-character subtags: valid whole, its copy is not
    s = ("x" + "-a" * n)[:n]
    return s if s[-1] != "-" else s[:-1] + "b"


C11B_CASES = [("501 private use 1-char", _pu_one_char(501), "whole_code"),
              ("2000 private use 1-char", _pu_one_char(2000), "whole_code"),
              ("501 valid copy valid", _valid(501), "en-GB"), ("2000 valid copy valid", _valid(2000), "en-GB"),
              ("501 invalid", _invalid(501), "invalid"), ("2000 invalid", _invalid(2000), "invalid"),
              ("693 duplicate singleton", _dup_singleton(693), "invalid"), ("123", "123", "invalid")]


@pytest.mark.parametrize("name,tag,want", C11B_CASES, ids=[c[0] for c in C11B_CASES])
def test_B_RO_05_page_lang_agrees_with_the_bit_and_codes_are_of_the_whole_value(name, tag, want):
    """C11b: bit valid -> the usual stored form when the whole-subtag copy is itself valid, else the ``{c:lang:..}``
    code of the WHOLE raw attribute (never ``invalid``); bit invalid -> ``invalid`` + LANG_INVALID whose detail is the
    code of the whole raw value, not of the copy. The raw text is not kept in the extract or the result."""
    from primitives import codes as pc
    from primitives.parse import STR_MAX, _lang_copy, parse_html
    ok = want != "invalid"
    assert bcp47.well_formed(tag) is ok, name
    x = parse_html(f"<html lang='{tag}'><body>b</body></html>", "http://site.test/")
    assert x["lang_valid"] is ok and x["lang"] == _lang_copy(tag)
    assert [k for k, v in x.items() if isinstance(v, str) and len(v) > STR_MAX and v == tag] == []
    web = site(Web())
    web.route("/", page(lang=tag, ld=ORG_LD))
    r = _r(web)
    from agents_run import coding
    with coding():
        whole, copy = pc.code("lang", tag), pc.code("lang", _lang_copy(tag))
    got = r["facts"]["pages"]["/"]["lang"]
    if want == "whole_code":
        assert len(tag) > STR_MAX and roman.safe_lang(_lang_copy(tag), coded=False) == "invalid"
        assert got == whole and got != copy and got.startswith("{c:lang:")
    else:
        assert got == want
    rd = r["facts"]["engine_readiness_heuristic"]["/"]
    assert all(rd[e]["checks"]["lang_declared"] == ("pass" if ok else "fail") for e in rd), rd
    assert ("LANG_INVALID" in codes(r)) is (not ok) and "LANG_MISSING" not in codes(r)
    if not ok:
        assert one(r, "LANG_INVALID")["detail"]["lang"] == whole
        assert len(tag) <= STR_MAX or whole != copy
    assert tag not in json.dumps(r) or len(tag) <= 3
