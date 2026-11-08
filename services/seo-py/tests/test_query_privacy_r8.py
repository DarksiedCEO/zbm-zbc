"""Review round 8 (search_performance 2026-11-08.8): fail safe on URL-like text, percent-encoding, split secrets and
Luhn-valid card numbers, for Osei's query classes and Priya's ledger alike. The attack strings are the round-8
reviewer's (scratch attack scripts), every secret shape built from parts so no literal reaches a scanner. URL-like
queries are dropped whole by design (disclosed in ADR 0047, ADR 0017 GSC-12, Priya LIMITS, the client report)."""

from __future__ import annotations

import pytest

from agents import priya
from agents import search_performance as sp

J = "".join
AK = J(("AKIA", "QWERTYUI", "OP123456"))
GH = J(("ghp_", "aB3dE5fG7h", "J9kL1mN3pQ5r", "S7tU9vW1xY3z"))
SK = J(("sk_live_", "51Hx9QaZ", "bCdEfGhIj"))
ROUND8_ATTACKS = [
 "e.com/abcdefghijklmno-pqrstuvwxyzabcd",      # two 15-letter random words (spec-exempt)
 "e.com/qzxwvkjhplmnbtr-yfdgsaoeiucvxzq-wplkmnjhgtrfdse",
 "e.com/1234-5678-9012-3456",                  # card
 "e.com/4111-1111-1111-1111",
 "e.com/4111/1111/1111/1111",
 "e.com/4111.1111.1111.1111",
 "e.com/4111-2019-2020-2021",                  # card with year-like groups
 "e.com/2019-2020-2021-4111",
 "e.com/123-45-6789",                          # SSN
 "e.com/ssn/123/45/6789",
 "e.com/call-555-010-0199",                    # phone
 "e.com/555-0100/199",
 "e.com/v12/v12/v12/v12/v12/v12",
 "e.com/v12/" + AK.lower(),
 "E.COM/" + AK,                                # uppercase
 "e.com/AkIaQwErTyUiOp123456",
 "e.com%2F" + AK,                              # url-encoded slash
 "e.com/a%2F" + AK.lower(),
 "e.com/docs#token=" + J(("abcd","efgh","ij12")),
 "e.com/docs#" + J(("x9k2m4","p7q1w3","e5r8t6y0")),
 "user:" + J(("hunter","22secret","99x")) + "@e.com/path",
 "https://admin:" + J(("p4ss","w0rd","zz")) + "@e.com",
 "admin:pa55word123@host",
 "e.com:8080/" + J(("a1b2c3d4","e5f6g7h8")),
 "192.168.1.10/" + J(("a1b2c3","d4e5f6g7")),
 "10.0.0.1:8443/api?key=" + J(("abcd","ef12")),
 "e.com//" + J(("x9k2m4p7","q1w3e5r8","t6y0")),
 "e.com./" + J(("k7mdeng2","bpxrficy")),
 "e.com/" + J(("k7mdeng2","bpxrfіcy")),      # Cyrillic i homoglyph
 "e.com/" + J(("аkia","qwertyui","op123456")),  # Cyrillic a in AKIA
 "e.com/" + GH,
 "e.com/x/" + SK,
 "e.com/u/jane.doe@gmail.com",
 "e.com/u/jane.doe(at)gmail(dot)com",
 "e.com/u/jane-doe-at-gmail-dot-com",
 "e.com/users/jane.doe%40gmail.com",
 "e.com/?email=jane%40ex.com",
 "e.com/p?ssn=123456789",
 "e.com/login?otp=482913",
 "e.com/x?access_token=" + J(("ya29","a0AfH6","SMBx")),
 "e.com/x?sig=" + J(("Zm9v","YmFy","MTIz")),
 "e.com/x;jsessionid=" + J(("A1B2C3","D4E5F6")),
 "e.com/reset/" + J(("ab12cd34","ef56")),
 "e.com/a/b/c/d/e/f/" + J(("k2","m4","p7","q1")),
 "e.com/" + J(("dGhpc","yBp")) + "/" + J(("cyBh","IHNl")),
 "e.com/" + "+".join(("Zm9v","YmFy","YmF6")),
 "e.com/" + J(("Zm9vYmFy","YmF6cXV4")) + "==",
 "e.com/p4ss-w0rd",
 "e.com/" + "-".join(("ab1","cd2","ef3","gh4","ij5")),
 "e.com/" + "-".join(("1a2b","3c4d","5e6f")),
 "e.com/ab12/cd34/ef56/gh78/ij90",
 "e.com/" + "-".join(("abcd","1234","efgh","5678","ijkl","9012")),
 "e.com/" + "-".join(["9876"]*4),
 "e.com/9876/5432/1098/7654",
 "e.com/12-34-56-78",
 "e.com/" + "-".join(("eyJhbGci","OiJIUzI1","NiJ9")),
 "e.com/" + ".".join(("eyJhbGciOi","eyJzdWIiOi","SflKxwRJSM")),
 "e.com/" + J(("x7","y8","z9")),
 "e.com/ABCDEFGHIJKLMNO-PQRSTUVWXYZABCD",
 "e.com/" + J(("aaaaaa","bbbbbb","1"))+"/x",
 "e.com/" + J(("q1","w2e3r4"))+"/"+J(("t5","y6u7i8")),
]
REVIEWER_URLS = [
    "docs.example.com/v2/api/reference", "acme.com/blog/5-ways-to-save", "example.org/docs/v3/getting-started",
    "mysite.com/page2/contact", "shop.com/search?q=rugs", "example.com/?page=2", "github.com/python/cpython",
    "en.wikipedia.org/wiki/search-engine-optimization", "developer.mozilla.org/en-us/docs/web/http",
    "moz.com/learn/seo/http2", "example.com/blog/2024-11-08-rug-care", "www.bbc.co.uk/news",
    "nytimes.com/section/technology", "amazon.com/best-sellers", "reddit.com/r/seo",
    "stackoverflow.com/questions/tagged/python", "youtube.com/watch?v=dqw4w9wgxcq", "google.com/search?q=plumber",
    "example.com/about-us/", "shop.com/category/rugs/wool", "docs.python.org/3/library/re.html",
    "apple.com/iphone-15-pro", "example.com/products/blue-rug", "blog.example.com/2023/05/seo-tips",
    "example.com/en/pricing", "site.com/page/3", "cdn.example.com/css/main.css", "example.com/index.php?id=12",
    "w3schools.com/html/default.asp", "example.com/faq#shipping", "how to rank on google 2024",
    "best rug cleaner near me", "nike.com/w/mens-shoes-nik1zy7ok",
    "example.com/blog/how-to-clean-a-wool-rug-at-home",
]

CARD = J(("4000", "2019", "2020", "2021"))                     # Luhn-valid, every group year-like but the first
ROUND8_SMUGGLES = [
    "e.com/" + "-".join(("4000", "2019", "2020", "2021")), " ".join(("4000", "2019", "2020", "2021")),
    "%2F" + AK.lower(), "e.com%2f" + AK.lower(), "x%20" + AK.lower(),
    "e.com/x%3Ftoken%3D" + J(("abcdefghij", "0123456789")), "e.com/x?token%3D" + J(("abcdefghij", "0123456789")),
    "e.com%2Fx%3Fpassword%3D" + J(("hunter", "2222")), "%3Fpassword%3D" + J(("hunter", "2222")),
    "-".join(("abcd", "1234", "efgh", "5678", "ijkl", "9012")),
    "e.com/" + "-".join(("abcd", "1234", "efgh", "5678", "ijkl", "9012")),
    "-".join(("abcdefghijklmno", "pqrstuvwxyzabcd")), "e.com/" + "/".join(("ab12", "cd34", "ef56", "gh78", "ij90")),
    J(("ab12cd34", "ef56gh78", "ij90")), "e.com/" + "/".join(("abc12", "def34", "ghi56", "jkl78")),
    "e.com/" + "-".join(("qwert", "12", "asdfg", "34", "zxcvb", "56")),
    "e.com/" + "/".join((J(("dGhpcyBp",)), J(("cyBhIHNl",)))),
]
# non-URL queries with '/', '%' or dots that stay KEPT
KEEP_NON_URL = ["a/b testing for landing pages", "and/or in contracts", "1/2 hp sump pump", "3/4 inch plywood",
                "24/7 plumber", "tires 225/65r17", "50% off rug cleaning", "100% wool rug", "node.js tutorial",
                "u.s flag", "st. louis plumber", "2.5 ton heat pump", "how to rank on google 2024",
                "best rug cleaner near me", "dr. pepper 12 pack", "w/o drain", "n/a meaning"]


@pytest.mark.parametrize("q", ROUND8_ATTACKS + ROUND8_SMUGGLES)
def test_round8_attacks_and_smuggles_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", repr(q)


@pytest.mark.parametrize("q", [u for u in REVIEWER_URLS if "." in u.split()[0]])
def test_url_like_queries_drop_whole_by_design(q):
    assert sp.url_like(sp._LOOKALIKE.fold_cased(sp.decode_query(q))) and sp.classify_query(q, ["acme"]) == "pii_suspect"


@pytest.mark.parametrize("q", KEEP_NON_URL)
def test_non_url_slashes_percents_and_dots_are_kept(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


def test_percent_decoding_is_bounded_and_repeated():
    assert sp.decode_query("%25252F") == "/"                       # three decodes
    assert sp.decode_query("%2525252F") == "%2F"                   # ... and no more
    assert sp.classify_query("%2525252F" + AK.lower(), ["acme"]) == "pii_suspect"   # escape left: URL-like


def test_luhn_runs_drop_whatever_their_groups():
    assert sp._luhn(CARD) and sp.card_like(" ".join(("4000", "2019", "2020", "2021")))
    assert not sp.card_like("best cars 2019 2020")                 # 8 digits: not a card
    assert sp.classify_query("4000.2019.2020.2021", ["acme"]) == "pii_suspect"


def test_priya_drops_round8_smuggles_and_counts_them():
    rows = [(q, 0, 25, 0.0, 5.0) for q in ROUND8_SMUGGLES] + [("rug cleaner", 1, 30, 0.03, 4.0)]
    led = priya.build("acme", "seo-kwl-" + "0" * 40, "2026-11-08T12:00:00+00:00", ["acme"],
                      priya.SourceResult("search_console", "OK", "OK", rows,
                                         {"window": {"start": "2026-10-09", "end": "2026-11-05"},
                                          "truncated": False, "unattributed": {"clicks": 0, "impressions": 0}}),
                      priya.SourceResult("bing_webmaster", "NOT_REQUESTED", "NOT_REQUESTED"))
    assert [r["query"]["text"] for r in led["rows"] if r["origin"] == "observed"] == ["rug cleaner"]
    assert led["counts"]["pii_suspect_dropped"]["queries"] == len({priya.normalize(q) for q in ROUND8_SMUGGLES})


def test_the_host_path_exemption_is_gone():
    assert not hasattr(sp, "_plain_url") and not hasattr(sp, "_HOST_PATH") and not hasattr(sp, "_VERSION_TOKEN")
