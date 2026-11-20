"""Priya's keyword ledger, the pure part (agents/priya.py; ADR 0047): tiers, per-source provenance, Poisson bands,
the property's own CTR curve, PII dropping, unicode and near-duplicates, deterministic EMERGING variants with no
volume, honest statuses, and the Theo / Naomi hand-off contract. Synthetic rows only."""

from __future__ import annotations

import base64
import json
import re

import pytest

import vault
from agents import priya
from agents import priya_tenant as pt
from agents.priya import SourceResult

WINDOW = {"start": "2026-10-09", "end": "2026-11-05", "timezone": "America/Los_Angeles"}
BRANDS = ["acme"]
# Synthetic vault key for the codes of WITHHELD rows (priya-spec/2 F-7; P2-4 I-18: build() refuses VAULT_UNAVAILABLE without a
# coder when a key is withheld). 32 high-variety bytes made up here; never a real key.
VAULT_KEY = base64.urlsafe_b64encode(bytes((i * 37 + 11) % 256 for i in range(32))).decode()
VAULT_KEY_ID = "k-test-priya"
VAULT_ENV = {"SEO_VAULT_KEY": VAULT_KEY, "SEO_VAULT_KEY_ID": VAULT_KEY_ID}
CODE_RE = re.compile(r"^\{c:query:[0-9a-f]{16}\}$")  # F-7 code shape


def coder(tenant="acme"):
    """The coder the service would hand build() (svc_keywords: vault.Coder(key, tid)), from the synthetic key."""
    return vault.Coder(vault.load_vault_key(VAULT_ENV), tenant)


def gsc(rows, **meta):
    m = {"window": WINDOW, "property_sha256": "p" * 64, "truncated": False,
         "unattributed": {"clicks": 0, "impressions": 0}}
    m.update(meta)
    return SourceResult("search_console", "OK", "OK", rows, m)


def off(name="bing_webmaster", state="NOT_REQUESTED", outcome="NOT_REQUESTED"):
    return SourceResult(name, state, outcome)


def build(g=None, b=None, t=None, c="default"):
    """priya.build for tenant acme; c = the coder (default: the synthetic-key coder; None = no coder, as I-18)."""
    return priya.build("acme", "seo-kwl-" + "0" * 40, "2026-11-08T12:00:00+00:00", BRANDS,
                       g if g is not None else off("search_console"), b if b is not None else off(),
                       tenant_vocab=t, coder=coder() if c == "default" else c)


def by_text(ledger):
    return {r["query"]["text"]: r for r in ledger["rows"]}


def tvocab(*words):
    """A tenant word list T (priya-spec/2 section 2.4) of operator-allowed words (T-1 b); each passes T-2.
    Without it build() uses T = empty (T-4), the certification setting."""
    for w in words:
        assert pt.t2_reason(w) is None and pt.one_word(w) == w
    return pt.TenantVocab("acme", frozenset(words), pt.tenant_vocab_sha256(words), "OFF")


def observed_impressions(ledger):
    return sum(r["metrics"]["search_console"]["impressions"] for r in ledger["rows"] if r["origin"] == "observed")


# A small synthetic site: established heads, a long tail with repeated frames, a brand query, PII.
SITE = [
    ("rug cleaner", 30, 1200, 0.025, 3.2),
    ("carpet cleaner", 20, 900, 0.022, 3.4),
    ("sofa cleaner", 6, 300, 0.02, 3.0),
    ("tile cleaner", 5, 250, 0.02, 3.3),
    ("qzvrexil cleaner", 4, 220, 0.018, 3.1),     # synthetic non-word (not in V T=2.5, not in T): OG-6 path
    ("acme pricing", 40, 200, 0.2, 1.1),
    ("rug cleaner near me", 2, 12, 0.16, 4.0),
    ("carpet cleaner near me", 1, 8, 0.12, 5.0),
    ("how to choose a rug cleaner", 0, 5, 0.0, 9.0),
    ("how to choose a carpet cleaner", 0, 4, 0.0, 9.5),
    ("jane.doe@example.com", 1, 3, 0.33, 1.0),
    ("call 555 010 0199", 0, 2, 0.0, 2.0),
]


def test_tiers_follow_measured_impressions_and_numbers_carry_provenance():
    led = build(gsc(SITE))
    rows = by_text(led)
    est = rows["rug cleaner"]
    assert est["tier"] == "ESTABLISHED" and est["volume_status"] == "MEASURED_ON_THIS_PROPERTY"
    m = est["metrics"]["search_console"]
    assert (m["clicks"], m["impressions"], m["position"]) == (30, 1200, 3.2)
    assert m["source"] == "first_party:search_console" and m["data_class"] == "measured"
    assert m["metric"] == "impressions_on_this_property"
    assert m["date_range"] == {"start": "2026-10-09", "end": "2026-11-05"}
    lo, hi = m["impressions_poisson_95"]
    assert lo < 1200 < hi                                          # a range, kept as a range
    assert est["market_volume"] is None and est["difficulty"] is None
    assert est["confidence"] == "HIGH"
    assert est["evidence"]["pointers"][0]["source"] == "first_party:search_console"
    thin = rows["rug cleaner near me"]
    assert thin["tier"] == "EMERGING" and thin["origin"] == "observed" and thin["volume_status"] == "BELOW_THRESHOLD"
    assert "BELOW_THRESHOLD" in thin["uncertainty_flags"] and thin["confidence"] == "NONE"
    assert thin["metrics"]["search_console"]["impressions"] == 12          # measured, kept as measured
    assert "constraint" in thin["emerging_kinds"]
    assert rows["how to choose a rug cleaner"]["emerging_kinds"][0] == "question"
    assert led["market_volume"]["measured"] is False and led["difficulty"]["computed"] is False
    assert led["headline_score"] is None and led["status"] == "OK"


def test_confidence_bands():
    led = build(gsc([("a b", 1, 20, 0.05, 5.0), ("c d", 1, 150, 0.01, 5.0), ("e f", 1, 5000, 0.0, 5.0)]))
    r = by_text(led)
    assert (r["a b"]["confidence"], r["c d"]["confidence"], r["e f"]["confidence"]) == ("LOW", "MEDIUM", "HIGH")
    assert "LOW_IMPRESSION_COUNT" in r["a b"]["uncertainty_flags"]
    trunc = by_text(build(gsc([("e f", 1, 5000, 0.0, 5.0)], truncated=True)))["e f"]
    assert trunc["confidence"] == "MEDIUM" and "GSC_ROWS_TRUNCATED" in trunc["uncertainty_flags"]


def test_pii_suspect_queries_never_enter_the_ledger_only_their_counts():
    led = build(gsc(SITE))
    blob = json.dumps(led)
    assert "jane.doe" not in blob and "555 010 0199" not in blob and "example.com" not in blob
    # v1 pin: pii_suspect_dropped == {queries 2, clicks 1, impressions 5}. priya-spec/2 F-5 + LP-2 (G-7): the same two
    # rows are WITHHELD with a reason code and conserved metrics. With T empty (T-4) "qzvrexil cleaner" is WITHHELD
    # too: OG-6, the synthetic non-word "qzvrexil" is in neither V nor T (lead ruling 15:05 PT: it replaced the v1
    # fixture word "grout", which is in V since T = 2.5, so the OG-6 intent of this test is kept).
    w = led["counts"]["withheld"]
    assert w["by_reason"] == {"OG-1": {"queries": 1, "clicks": 1, "impressions": 3},
                              "S2:pii_suspect": {"queries": 1, "clicks": 0, "impressions": 2},
                              "OG-6": {"queries": 1, "clicks": 4, "impressions": 220}}
    assert (w["queries"], w["clicks"], w["impressions"]) == (3, 5, 225) and "qzvrexil" not in blob
    assert observed_impressions(led) + w["impressions"] == sum(r[2] for r in SITE)           # metrics conserved
    # with "qzvrexil" as a tenant word (T-1 b) the v1 numbers hold exactly: only the two personal rows are withheld
    w = build(gsc(SITE), t=tvocab("qzvrexil"))["counts"]["withheld"]
    assert (w["queries"], w["clicks"], w["impressions"]) == (2, 1, 5) and set(w["by_reason"]) == {"OG-1",
                                                                                                "S2:pii_suspect"}
    # fullwidth digits and lookalike spellings are caught too (the normalised text is classified as well)
    led = build(gsc([("ｃａｌｌ ５５５ ０１０ ０１９９", 0, 2, 0.0, 1.0), ("jane．doe＠example．com", 0, 1, 0.0, 1.0)]))
    assert led["rows"] == [] and led["counts"]["withheld"]["queries"] == 2
    assert led["counts"]["withheld"]["impressions"] == 3 and "jane" not in json.dumps(led)


@pytest.mark.parametrize("q", ["123 main st springfield", "45 oak avenue apt 3", "1200 5th ave new york",
                               "9 n elm blvd.", "po box 1234", "springfield il 62701", "austin, tx 78701",
                               "90210-1234", "plumber at 123 main st", "42 Wallaby Way Sydney", "123 main road",
                               "123 main st springfield il 62701 hours", "55 water st 12th floor", "boston ma 02108",
                               "rr 2 box 15", "1 main st pmb 123", "x" * 600, "17 old mill road",
                               "100 old country rd", "300 OLD MILL RD", "88 old post road", "12 new street",
                               "40 good hope rd", "sw1a 1aa", "m5v 3l9", "350 rue sherbrooke montreal",
                               "1 market plaza", "6 place de la concorde", "m1 1ae manchester",
                               "example.com/blog/how-to-clean-rugs", "e.com%2fx%3fpassword%3dhunter2222",
                               "4000 2019 2020 2021"])
def test_street_addresses_are_dropped_as_personal_data(q):
    led = build(gsc([(q, 1, 30, 0.03, 4.0)]))
    assert led["rows"] == [] and led["counts"]["pii_suspect_dropped"] == {"queries": 1, "clicks": 1,
                                                                          "impressions": 30}
    # priya-spec/2 F-5/F-7 (G-7: v1 DROP -> WITHHELD, not weaker): one coded row, closed reason, metrics kept, no text.
    w = led["counts"]["withheld"]
    assert (w["queries"], w["clicks"], w["impressions"]) == (1, 1, 30)
    (row,) = led["withheld_rows"]
    assert CODE_RE.match(row["code"]) and row["reason"] in priya.REASON_CODES
    assert (row["metrics"]["search_console"]["clicks"], row["metrics"]["search_console"]["impressions"]) == (1, 30)
    assert q not in json.dumps(led, ensure_ascii=False)


@pytest.mark.parametrize("q", ["rug cleaner near me", "4 way stop", "top 10 rug cleaners", "rug cleaner 90210",
                               "#1 rug cleaner", "24 hour emergency plumber near dr",
                               "2 bedroom flat close to station", "10 best hiking trail", "2 lane road",
                               "cars under 30000 in 50000 miles", "ohio 45000 jobs",
                               "apt 12 carpet cleaning", "3 days in st louis", "ca 90210 zip code",
                               "top 5 pizza places 5th ave", "2 rd round draft", "2.5 ton heat pump",
                               "tires 225/65r17",
                               "food court mall hours", "bmw x5 3rd row", "ipad pro m4 1tb",
                               "3 new dr pepper flavors", "2 old court cases", "5 route options"])
def test_ordinary_queries_with_numbers_are_kept(q):
    assert by_text(build(gsc([(q, 1, 30, 0.03, 4.0)])))[q]["origin"] == "observed"


# v1 KEEP cases that V (T = 2.5, owner decision 14:45 PT) makes READABLE again with T empty (T-4): "btu" and
# "ecoboost" are in V. Lead ruling 15:05 PT: regression tests that these stay readable (NB-12 over-tightening).
OG6_V1_READABLE = ["ac unit 12000 btu", "or 50000 btu", "2012 ford f150 3.5 ecoboost"]


@pytest.mark.parametrize("q", OG6_V1_READABLE)
def test_v1_kept_queries_are_readable_with_v_t25_and_t_empty(q):
    led = build(gsc([(q, 1, 30, 0.03, 4.0)]))
    assert by_text(led)[q]["origin"] == "observed"
    assert led["counts"]["withheld"]["queries"] == 0


# Queries still WITHHELD by priya-spec/2 OG-6 with T empty (T-4). Second column: the tenant words (T-1 b) that make
# the query READABLE again, or None where T cannot (OG-6's alphanumeric clause: no safe shape, T not consulted).
# "vexquorn" is a synthetic alpha non-word, checked absent from V (T = 2.5) and T (lead ruling 15:05 PT).
OG6_WITHHELD = [("vexquorn filter size", ("vexquorn",)), ("carrier 24acc636a003 manual", None)]


@pytest.mark.parametrize(("q", "words"), OG6_WITHHELD)
def test_queries_with_unknown_tokens_are_withheld_og6_when_t_is_empty(q, words):
    led = build(gsc([(q, 1, 30, 0.03, 4.0)]))
    one = {"queries": 1, "clicks": 1, "impressions": 30}
    assert led["rows"] == [] and led["counts"]["withheld"] == {**one, "by_reason": {"OG-6": one}}
    assert q not in json.dumps(led)
    if words is None:
        led = build(gsc([(q, 1, 30, 0.03, 4.0)]), t=tvocab("24acc636a003"))
        assert led["rows"] == [] and set(led["counts"]["withheld"]["by_reason"]) == {"OG-6"}
    else:
        assert by_text(build(gsc([(q, 1, 30, 0.03, 4.0)]), t=tvocab(*words)))[q]["origin"] == "observed"


def test_dropped_queries_are_counted_once_across_sources_and_bing_weeks():
    b = SourceResult("bing_webmaster", "OK", "OK",
                     [("jane.doe@example.com", "2026-10-05", 1, 4, None, None),
                      ("jane.doe@example.com", "2026-10-12", 0, 3, None, None),
                      ("JANE.DOE@example.com", "2026-10-19", 0, 2, None, None),
                      ("123 main st", "2026-10-05", 0, 6, None, None)], {"site_sha256": "s" * 64})
    led = build(gsc([("jane.doe@example.com", 1, 5, 0.2, 1.0)]), b)
    assert led["counts"]["pii_suspect_dropped"] == {"queries": 2, "clicks": 2, "impressions": 20}
    # priya-spec/2 F-5/F-7 (G-7): the same two keys are WITHHELD rows, one each across sources and weeks, coded, no text.
    w = led["counts"]["withheld"]
    assert (w["queries"], w["clicks"], w["impressions"]) == (2, 2, 20)
    codes = [r["code"] for r in led["withheld_rows"]]
    assert len(codes) == 2 == len(set(codes)) and all(CODE_RE.match(c) for c in codes)
    blob = json.dumps(led, ensure_ascii=False).lower()
    assert "jane" not in blob and "123 main st" not in blob


def test_ctr_curve_is_the_propertys_own_and_inconclusive_without_support():
    # T empty (T-4): "qzvrexil cleaner" is WITHHELD (OG-6, NB-12) and a withheld row never feeds the curve (F-7: only a
    # coded row + metrics), so bucket "3" keeps 4 queries, under the support minimum.
    b3 = build(gsc(SITE))["ctr_curve"]["buckets"]["3"]
    assert b3 == {"ctr": None, "queries": 4, "impressions": 2650, "clicks": 61, "status": "INSUFFICIENT_SUPPORT"}
    # the v1 assertions, unchanged, with "qzvrexil" as a tenant word (T-1 b)
    led = build(gsc(SITE), t=tvocab("qzvrexil"))
    b3 = led["ctr_curve"]["buckets"]["3"]
    # rug, carpet, sofa, tile, qzvrexil: 5 non-brand established rows in bucket "3" (positions 3.0-3.4)
    assert b3["status"] == "COMPUTED" and b3["queries"] == 5 and b3["impressions"] == 2870
    assert b3["ctr"] == round(65 / 2870, 6)
    assert led["ctr_curve"]["buckets"]["1"]["status"] == "INSUFFICIENT_SUPPORT"      # only the brand row is there
    # leave-one-out: with the row itself out, 4 queries remain in bucket "3", under the support minimum
    opp = by_text(led)["sofa cleaner"]["opportunity"]
    assert opp["status"] == "INCONCLUSIVE" and "without this row" in opp["why"] and opp["data_class"] == "modeled"
    assert opp["expected_ctr"] is None and opp["ctr_95"] is not None
    assert by_text(led)["acme pricing"]["opportunity"]["status"] == "NOT_APPLICABLE"     # brand
    small = build(gsc([("a b", 1, 50, 0.02, 3.0), ("c d", 1, 60, 0.02, 3.0)]))
    assert by_text(small)["a b"]["opportunity"]["status"] == "INCONCLUSIVE"
    assert "not_used" in led["ctr_curve"]


# Bucket "3": six peers at CTR 5% (1,000 impressions each), one row far below, one row on the curve.
PEERS = [(f"peer{i} cleaner", 50, 1000, 0.05, 3.2) for i in range(6)]


def test_ctr_gap_has_a_wilson_interval_and_leaves_the_row_out():
    led = build(gsc(PEERS + [("dull cleaner", 5, 1000, 0.005, 3.1), ("fair cleaner", 48, 1000, 0.048, 3.3)]))
    t = by_text(led)
    dull = t["dull cleaner"]["opportunity"]
    # its own expected CTR is the peers' and fair's (row left out): (300 + 48) / 7000
    assert dull["expected_ctr"] == round(348 / 7000, 6) and dull["status"] == "COMPUTED"
    lo, hi = dull["ctr_95"]
    assert lo < 0.005 < hi and lo > 0 and hi < 0.012                       # Wilson score interval on 5 / 1000
    glo, ghi = dull["ctr_gap_95"]
    assert 0 < glo < dull["ctr_gap"] < ghi and dull["direction"] == "below_curve"
    assert dull["click_gap_modeled_95"][0] <= dull["click_gap_modeled"] <= dull["click_gap_modeled_95"][1]
    fair = t["fair cleaner"]["opportunity"]
    assert fair["status"] == "INCONCLUSIVE" and "includes 0" in fair["why"]      # not distinguishable from the curve
    assert fair["ctr_gap_95"][0] < 0 < fair["ctr_gap_95"][1]
    peer = t["peer0 cleaner"]["opportunity"]                                  # the peer's expected CTR excludes itself
    assert peer["expected_ctr"] == round((250 + 5 + 48) / 7000, 6)


def test_a_thin_row_never_gets_a_computed_ctr_gap():
    # 4 impressions at position 1.2 against a well-supported bucket "1": INCONCLUSIVE, never COMPUTED
    top = [(f"top{i} cleaner", 300, 1000, 0.3, 1.1) for i in range(6)]
    led = build(gsc(top + [("tiny cleaner", 0, 4, 0.0, 1.2)]))
    r = by_text(led)["tiny cleaner"]
    assert r["tier"] == "EMERGING" and r["opportunity"]["status"] == "INCONCLUSIVE"
    assert "too little data" in r["opportunity"]["why"] and r["opportunity"]["ctr_gap"] is None
    assert r["opportunity"]["ctr_95"][0] == 0.0 and r["opportunity"]["ctr_95"][1] > 0.4    # wide, said as wide


def test_the_19_and_20_impression_boundary():
    # peers at CTR 30%: 0 clicks in 20 impressions (Wilson upper bound 16%) is distinguishably below the curve
    hi = [(f"peer{i} cleaner", 300, 1000, 0.3, 3.2) for i in range(6)]
    rows = hi + [("nineteen cleaner", 0, 19, 0.0, 3.2), ("twenty cleaner", 0, 20, 0.0, 3.2)]
    t = by_text(build(gsc(rows)))
    n19, n20 = t["nineteen cleaner"], t["twenty cleaner"]
    assert n19["tier"] == "EMERGING" and "BELOW_THRESHOLD" in n19["uncertainty_flags"]
    assert n20["tier"] == "ESTABLISHED" and "LOW_IMPRESSION_COUNT" in n20["uncertainty_flags"]
    assert n19["opportunity"]["status"] == "INCONCLUSIVE" and "too little data" in n19["opportunity"]["why"]
    assert n20["opportunity"]["status"] == "COMPUTED" and n20["opportunity"]["direction"] == "below_curve"
    # at CTR 5% the same 0 / 20 is not distinguishable: INCONCLUSIVE for the interval, not for thin data
    low = by_text(build(gsc(PEERS + [("twenty cleaner", 0, 20, 0.0, 3.2)])))["twenty cleaner"]["opportunity"]
    assert low["status"] == "INCONCLUSIVE" and "includes 0" in low["why"]
    led = build(gsc(rows))
    assert led["ctr_curve"]["buckets"]["3"]["queries"] == 7                  # the 20 is in the curve, the 19 is not
    assert led["ctr_curve"]["buckets"]["3"]["impressions"] == 6020


def test_poisson_band_is_the_exact_garwood_interval():
    assert priya.poisson_band(0) == [0.0, 3.69]
    assert priya.poisson_band(1) == [0.02, 5.58]
    assert priya.poisson_band(4) == [1.08, 10.25]
    assert priya.poisson_band(20) == [12.21, 30.89]
    assert priya.poisson_band(1000) == [938.97, 1063.96]
    # above the exact range the Wilson-Hilferty form; reference values: chi-square quantiles (scipy, offline)
    for n, lo, hi in ((5000, 4862.359188694899, 5140.5489485350045), (10 ** 6, 998040.9833402939, 1001961.9119454322)):
        b = priya.poisson_band(n)
        assert b[0] <= lo and b[1] >= hi and lo - b[0] < 0.02 and b[1] - hi < 0.02
    assert priya.wilson_interval(0, 0) is None
    lo, hi = priya.wilson_interval(0, 20)
    assert lo == 0.0 and abs(hi - priya.Z95 ** 2 / (20 + priya.Z95 ** 2)) < 1e-12
    assert any("not sampling error" in x for x in priya.LIMITS)


def test_emerging_variants_are_derived_deterministically_with_no_volume():
    led = build(gsc(SITE))
    rows = by_text(led)
    derived = [r for r in led["rows"] if r["origin"] == "derived"]
    texts = {r["query"]["text"] for r in derived}
    # "{head} near me" and "how to choose a {head}" were each seen around two heads, so they transfer to the others
    assert {"sofa cleaner near me", "tile cleaner near me", "how to choose a sofa cleaner"} <= texts
    assert "rug cleaner near me" not in texts                       # already observed: not derived again
    assert not any("acme" in t for t in texts)                       # brand heads are not used
    for r in derived:
        assert r["tier"] == "EMERGING" and r["volume_status"] == "UNKNOWN_NOT_MEASURED"
        assert r["metrics"] == {"search_console": None, "bing_webmaster": None}
        assert r["confidence"] == "NONE" and r["uncertainty_flags"] == ["NOT_MEASURED"]
        assert r["market_volume"] is None and r["evidence"]["supporting_rows"]
    kept = {f["frame"]["text"] for f in led["frames"]["kept"]}
    assert {"{head} near me", "how to choose a {head}"} <= kept
    assert rows["sofa cleaner near me"]["query"]["untrusted"] is True
    assert build(gsc(SITE)) == led                                   # deterministic
    assert build(gsc(list(reversed(SITE))))["rows"] == led["rows"]    # independent of row order


def test_frames_are_never_built_from_name_like_words():
    heads = [("rug cleaner", 30, 1200, 0.025, 3.2), ("carpet cleaner", 20, 900, 0.022, 3.4),
             ("sofa cleaner", 6, 300, 0.02, 3.0)]
    tail = [("rug cleaner john smith", 0, 5, 0.0, 9.0), ("carpet cleaner john smith", 0, 4, 0.0, 9.0),  # not vocab
            ("Rug Cleaner Dallas", 0, 5, 0.0, 9.0), ("Carpet Cleaner Dallas", 0, 4, 0.0, 9.0),          # capitalised
            ("best rug cleaner", 0, 5, 0.0, 9.0), ("best carpet cleaner", 0, 4, 0.0, 9.0)]
    led = build(gsc(heads + tail))
    kept = {f["frame"]["text"] for f in led["frames"]["kept"]}
    assert kept == {"best {head}"}
    texts = {r["query"]["text"] for r in led["rows"] if r["origin"] == "derived"}
    assert texts == {"best sofa cleaner"}
    assert not any("john" in x or "dallas" in x for x in texts)
    assert any("Personal names cannot be reliably detected" in x for x in priya.LIMITS)


def test_unicode_queries_are_kept_and_normalised():
    led = build(gsc([("東京 ホテル 安い", 3, 80, 0.04, 6.0), ("Café  Prices", 2, 40, 0.05, 4.0),
                     ("ＲＵＧ CLEANER", 1, 30, 0.03, 4.0), ("rug\u200bcleaner tips", 0, 5, 0.0, 9.0)]))
    t = by_text(led)
    assert "東京 ホテル 安い" in t and "café prices" in t and "rug cleaner" in t
    # v1: "rugcleaner tips" in t. priya-spec/2: the key is unchanged (F-2, the zero-width space is removed, not a
    # space), but with T empty (T-4) the glued word is in neither V nor T -> WITHHELD OG-6 (NB-12, reported).
    assert priya.normalize("rug\u200bcleaner tips") == "rugcleaner tips" and "rugcleaner tips" not in t
    assert led["counts"]["withheld"] == {"queries": 1, "clicks": 0, "impressions": 5,
                                         "by_reason": {"OG-6": {"queries": 1, "clicks": 0, "impressions": 5}}}
    assert all(r["query"]["untrusted"] for r in led["rows"])
    led = build(gsc([("rug\u200bcleaner tips", 0, 5, 0.0, 9.0)]), t=tvocab("rugcleaner"))
    assert "rugcleaner tips" in by_text(led)                          # READABLE through T (T-1 b)


def test_duplicate_spellings_merge_and_near_duplicates_are_flagged_not_summed():
    led = build(gsc([("Rug Cleaner", 5, 100, 0.05, 4.0), ("rug  cleaner", 1, 50, 0.02, 6.0),
                     ("cleaner for rugs", 2, 40, 0.05, 5.0), ("rug cleaners", 1, 25, 0.04, 7.0)]))
    t = by_text(led)
    assert t["rug cleaner"]["metrics"]["search_console"]["impressions"] == 150     # one normalised query
    assert led["counts"]["duplicate_spellings_merged"] == 1
    g = {t[k]["near_duplicate"]["group"] for k in ("rug cleaner", "cleaner for rugs", "rug cleaners")}
    assert len(g) == 1 and led["counts"]["near_duplicate_groups"] == 1
    assert t["cleaner for rugs"]["metrics"]["search_console"]["impressions"] == 40  # never summed into the group
    assert "NEAR_DUPLICATE" in t["rug cleaners"]["uncertainty_flags"]


def test_inconclusive_when_nothing_was_read_never_empty_as_zero():
    led = build(off("search_console", "GSC_NOT_CONNECTED", "NOT_CONNECTED"))
    assert led["status"] == "INCONCLUSIVE" and led["rows"] == []
    assert "not that there are no keywords" in led["status_meaning"]
    assert led["sources"]["search_console"]["state"] == "GSC_NOT_CONNECTED"
    assert led["sources"]["search_console"]["rows_received"] == 0


def test_empty_site_is_a_measurement_with_its_anonymised_remainder():
    led = build(gsc([], unattributed={"clicks": 3, "impressions": 90}, totals={"clicks": 3, "impressions": 90}))
    assert led["status"] == "EMPTY_MEASURED" and led["rows"] == []
    assert led["sources"]["search_console"]["unattributed"]["impressions"] == 90
    only_pii = build(gsc([("jane.doe@example.com", 1, 30, 0.03, 1.0)]))
    assert only_pii["status"] == "EMPTY_MEASURED"


def test_partial_when_one_requested_source_failed():
    led = build(gsc(SITE), off("bing_webmaster", "BING_UNAVAILABLE", "FAILED"))
    assert led["status"] == "PARTIAL" and led["sources"]["bing_webmaster"]["state"] == "BING_UNAVAILABLE"
    assert all(r["metrics"]["bing_webmaster"] is None for r in led["rows"])


def test_bing_numbers_are_kept_apart_from_search_consoles():
    b = SourceResult("bing_webmaster", "OK", "OK",
                     [("rug cleaner", "2026-10-05", 4, 60, 7.0, 6.0), ("rug cleaner", "2026-10-12", 3, 40, 8.0, 9.0),
                      ("bing only query", "2026-10-05", 1, 25, None, None)], {"site_sha256": "s" * 64})
    led = build(gsc(SITE), b)
    t = by_text(led)
    r = t["rug cleaner"]
    assert r["metrics"]["search_console"]["impressions"] == 1200 and r["metrics"]["bing_webmaster"]["impressions"] == 100
    assert r["metrics"]["bing_webmaster"]["date_range"] == {"start": "2026-10-05", "end": "2026-10-12"}
    assert r["metrics"]["bing_webmaster"]["avg_impression_position"] == round((6.0 * 60 + 9.0 * 40) / 100, 2)
    assert "BING_WINDOW_DIFFERS" in r["uncertainty_flags"] and "SINGLE_SOURCE" not in r["uncertainty_flags"]
    bo = t["bing only query"]
    # Bing-only: measured (25 impressions over Bing's own dates) but never tiered against the 28-day threshold
    assert bo["tier"] == "EMERGING" and bo["metrics"]["search_console"] is None
    assert bo["volume_status"] == "MEASURED_NOT_TIERED" and bo["confidence"] == "NONE"
    assert "BING_ONLY_UNTIERED" in bo["uncertainty_flags"] and "BELOW_THRESHOLD" not in bo["uncertainty_flags"]
    assert bo["metrics"]["bing_webmaster"]["avg_impression_position"] is None
    assert "SINGLE_SOURCE" in bo["uncertainty_flags"] and bo["opportunity"]["status"] == "NOT_APPLICABLE"
    item = next(i for i in led["handoff"]["items"] if i["key"] == bo["key"])
    assert item["observed_counts"]["source"] == "first_party:bing_webmaster" and priya.validate_handoff(
        led["handoff"]) == []


def test_the_tier_is_search_consoles_when_it_measured_the_query():
    # Bing's weekly rows summed (12 weeks x 10 = 120) never lift a Search Console row of 5 impressions to ESTABLISHED
    weeks = [("thin rug query", f"2026-08-{d:02d}", 0, 10, None, 5.0) for d in range(1, 29, 7)] + \
            [("thin rug query", f"2026-09-{d:02d}", 0, 10, None, 5.0) for d in range(1, 29, 7)] + \
            [("thin rug query", f"2026-10-{d:02d}", 0, 10, None, 5.0) for d in range(1, 29, 7)]
    b = SourceResult("bing_webmaster", "OK", "OK", weeks, {"site_sha256": "s" * 64})
    r = by_text(build(gsc([("thin rug query", 0, 5, 0.0, 9.0)]), b))["thin rug query"]
    assert r["metrics"]["bing_webmaster"]["impressions"] == 120
    assert r["tier"] == "EMERGING" and r["volume_status"] == "BELOW_THRESHOLD"


def test_row_cap_keeps_established_first_and_says_how_many_were_dropped(monkeypatch):
    monkeypatch.setattr(priya, "ROWS_CAP", 3)
    led = build(gsc([(f"w{i} x", 1, 100 - i, 0.01, 5.0) for i in range(4)] + [("thin z", 0, 2, 0.0, 9.0)]))
    assert led["counts"]["rows"] == 3 and led["counts"]["rows_dropped_past_cap"] == 2
    assert all(r["tier"] == "ESTABLISHED" for r in led["rows"])


def test_handoff_contract_for_theo_and_naomi():
    led = build(gsc(SITE))
    h = led["handoff"]
    assert h["schema"] == priya.HANDOFF_SCHEMA and priya.validate_handoff(h) == []
    assert h["consumers"]["theo"]["state"] == "NOT_BUILT" and h["consumers"]["naomi"]["state"] == "NOT_CONNECTED"
    assert {i["key"] for i in h["items"]} == {r["key"] for r in led["rows"] if r["tier"] == "EMERGING"}
    for i in h["items"]:
        if i["origin"] == "derived":
            assert i["observed_counts"] is None and i["volume_status"] == "UNKNOWN_NOT_MEASURED"
        else:
            assert i["observed_counts"]["impressions"] < priya.ESTABLISHED_MIN_IMPRESSIONS
    bad = json.loads(json.dumps(h))
    bad["items"][0]["volume_status"] = "MEASURED_ON_THIS_PROPERTY"
    bad["items"][-1]["observed_counts"] = {"impressions": 5000}
    assert priya.validate_handoff(bad)
    assert priya.validate_handoff({"schema": "x"}) == ["schema"]


@pytest.mark.parametrize("q,kinds", [("how much does rug cleaning cost", ["question", "conversational", "constraint"]),
                                     ("rug", ["long_tail"]), ("best rug cleaner", ["constraint"])])
def test_emerging_kinds(q, kinds):
    assert priya.emerging_kinds(q) == kinds


def test_card_is_one_task_with_inputs_and_outputs():
    c = priya.CARD
    assert c["agent"] == "priya" and "one task" in c["job"] and c["inputs"] and c["outputs"]
    assert "estimate market volume" in c["does_not"] and "compute keyword difficulty" in c["does_not"]
