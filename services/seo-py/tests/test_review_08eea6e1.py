"""Review of 08eea6e1 (shared redaction core, both lanes; the citation vault's torn-tail repair): defanged e-mails
(a real "@" or a spelled " at " with bracketed dots, multi-label domains), the spelled e-mail variants (hyphens,
underscores, glued case-boundary AT / DOT, "period", "at sign", Spanish / French spellings), the 1-8 digit filler bridge
with its 1-digit limit, number words read as digits, base64 glued to an aligned prefix / suffix, the client view's SSN
separator set and its utility rules, a fragment decoded to the depth of a path, and the bounded, quarantined, durable
torn-tail repair.

Every sensitive-looking value here is GENERATED at run time from fragments (nothing credential-shaped or e-mail-shaped
is written in this file); failures report counts and case names only."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import stat
import string
import time
from urllib.parse import quote

import helpers
import pytest
import test_review_601e328a as r6
import test_vault_wiring as vw
import utility_corpus_v2 as uc

import vault
from agents import callum
from agents import secret_guard as sg
from ports import ProviderAnswer

SEED = 8082026
AT = chr(64)
LOW = string.ascii_lowercase
ALNUM = string.ascii_letters + string.digits
NEVER = re.compile(r"(?!x)x")
DIGIT_WORDS = dict(zip(string.digits, ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")))
# every defanged dot form: the four bracket pairs around "dot" or ".", any case, optional spaces
BRACKET_DOTS = ("[dot]", "(dot)", "{dot}", "<dot>", "[.]", "(.)", "{.}", "<.>", "[ DOT ]", "( . )", "{Dot}", "< . >")


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _words(d: str) -> str:
    return " ".join(DIGIT_WORDS[c] for c in d)


def _key(rng) -> str:
    return "gh" + "p_" + _r(rng, ALNUM, 36)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode().rstrip("=")


# prefixes / suffixes glued onto a base64 payload: aligned (4 characters) and 5-7 characters (offsets 4-7), and 1-3
# character suffixes (1-3 undecodable trailing bytes); chosen so their decoded bytes are not UTF-8
B64_PREFIXES = ("/x9z", "/x9zQ", "/x9zQ1", "/x9zQ1k")
B64_SUFFIXES = ("_", "_9", "_9z", "_9z/")


# ------------------------------------------------------------------------------------------------ generated cases

def email_cases(seed: int = SEED) -> dict:
    """name -> texts holding an address that BOTH lanes must withhold / mask (MUST 1, MUST 2, NIT)."""
    rng = random.Random(seed)
    out: dict = {}
    for b in BRACKET_DOTS:
        loc, dom = r6._mail(rng)
        out.setdefault("defanged_at", []).extend([f"{loc}{AT}{dom}{b}com", f"{loc} {AT} {dom} {b} com",
                                                  f"{loc}{AT}mail{b}{dom}{b}co{b}uk"])
        out.setdefault("defanged_spelled", []).extend([f"{loc} at {dom}{b}com", f"{loc} at {dom} {b} co {b} uk"])
    for _ in range(3):
        loc, dom = r6._mail(rng)
        out.setdefault("variant_hyphen_underscore", []).extend([
            f"{loc}-at-{dom}-dot-com", f"{loc}_at_{dom}_dot_com", f"{loc} _at_ {dom} _dot_ com", f"{loc}-at-{dom}.com",
            f"{loc}_at_{dom}.co.uk", f"{loc}-at-{dom}-period-com"])
        out.setdefault("variant_period_sign", []).extend([
            f"{loc} at {dom} period com", f"{loc} at sign {dom} dot com", f"{loc} at-sign {dom}.com",
            f"{loc}{AT}{dom} dot com", f"{loc} {AT} {dom} period co period uk"])
        out.setdefault("nit_spellings", []).extend([
            f"{loc} arroba {dom} punto com", f"{loc} arobase {dom} point fr", f"{loc} arroba {dom}.es",
            f"{loc}{AT}{dom} punto com"])
    for _ in range(4):
        loc, dom = r6._mail(rng)
        out.setdefault("variant_glued", []).extend([f"{loc}AT{dom}DOTcom", f"{loc}AT{dom}.com",
                                                    f"{loc}AT{dom}DOTcoDOTuk"])
    return {k: [f"write to {x} today" for x in v] for k, v in out.items()}


def number_cases(seed: int = SEED + 1) -> dict:
    """name -> texts holding a card / SSN that BOTH lanes must withhold / mask (MUST 3, SHOULD 3, SHOULD 4, NIT)."""
    rng = random.Random(seed)
    out: dict = {}
    for _ in range(3):
        v16 = r6._card(rng, "4", 16)
        a15 = r6._card(rng, "37", 15)
        m16 = r6._card(rng, "5" + str(rng.randint(1, 5)), 16)
        for f in ("then", "plus", "followed by", "and", "o"):
            out.setdefault("card_8_8_words", []).append(f"card {v16[:8]} {f} {v16[8:]} ok")
            out.setdefault("card_7_8_words", []).append(f"card {a15[:7]} {f} {a15[7:]} ok")
            out.setdefault("card_tail_1_digit", []).append(
                f"card {m16[:4]} {m16[4:8]} {m16[8:12]} {m16[12:15]} {f} {m16[15]} ok")
        out.setdefault("card_number_words", []).extend([f"card {_words(v16)} ok", f"card {_words(a15)} ok",
                                                        f"card {_words(m16)} ok", f"pay {_words(v16[:8])}, "
                                                        f"{_words(v16[8:])} now"])
    for _ in range(3):
        a, g, s = r6._ssn(rng)
        for sep in ("_", "/", "||", "--", " / ", ":", "~", "\\", "+", " _ ", "#", "*"):
            out.setdefault("ssn_strict_separators", []).append(f"ref {a}{sep}{g}{sep}{s} ok")
        out.setdefault("ssn_number_words", []).extend([f"id {_words(a)} - {_words(g)} - {_words(s)} ok",
                                                       f"id {_words(a)}-{_words(g)}-{_words(s)} ok",
                                                       f"ssn {_words(a + g + s).replace('zero', 'oh')} ok",
                                                       f"SSN: {_words(a)} {_words(g)} {_words(s)} ok"])
    return out


def b64_cases(seed: int = SEED + 2) -> dict:
    """name -> texts holding a base64 payload glued to an aligned prefix / suffix (MUST 4)."""
    rng = random.Random(seed)
    out: dict = {}
    for _ in range(3):
        loc, dom = r6._mail(rng)
        mail, key = _b64(loc + AT + dom + ".com"), _b64(_key(rng))
        for p in B64_PREFIXES:
            out.setdefault("b64_prefix_4_7", []).extend([f"see {p}{mail} ok", f"see {p}{key} ok"])
        for s in B64_SUFFIXES:
            out.setdefault("b64_suffix_1_3", []).extend([f"see {mail}{s} ok", f"see {key}{s} ok"])
        out.setdefault("b64_prefix_and_suffix", []).extend([f"see {p}{mail}{s} ok" for p, s in
                                                            zip(B64_PREFIXES, B64_SUFFIXES)])
    return out


def strict_only_cases(seed: int = SEED + 3) -> dict:
    """The strict lane's 9+ digit rule through a 1-digit group (no card / SSN shape: the client view does not mask)."""
    rng = random.Random(seed)
    return {"nine_digits_one_digit_tail": [f"ref {_r(rng, string.digits, 8)} {f} {rng.randint(1, 9)} ok"
                                           for f in ("then", "next", "followed by", "plus") for _ in range(4)]}


def all_cases() -> dict:
    return {**email_cases(), **number_cases(), **b64_cases()}


CLASS_OF = {"defanged_at": "email", "defanged_spelled": "email", "variant_hyphen_underscore": "email",
            "variant_period_sign": "email", "nit_spellings": "email", "variant_glued": "email",
            "card_8_8_words": "card", "card_7_8_words": "card", "card_tail_1_digit": "card", "card_number_words": "card",
            "ssn_strict_separators": "ssn", "ssn_number_words": "ssn",
            "b64_prefix_4_7": None, "b64_suffix_1_3": None, "b64_prefix_and_suffix": None}


# ------------------------------------------------------------------------------------------------ every entry point

@pytest.mark.parametrize("cls", sorted(all_cases()) + sorted(strict_only_cases()))
def test_strict_lane_withholds_every_new_shape_through_every_entry_point(cls):
    samples = {**all_cases(), **strict_only_cases()}[cls]
    assert len(samples) >= 12, cls
    assert r6._misses(samples, r6._strict_everywhere, r6.STRICT_ENTRIES) == {}, cls


@pytest.mark.parametrize("cls", sorted(all_cases()))
def test_client_view_masks_every_new_shape_through_every_entry_point(cls):
    samples = all_cases()[cls]
    assert r6._misses(samples, r6._client_everywhere, r6.CLIENT_ENTRIES) == {}, cls
    want = CLASS_OF[cls]
    if want is not None:                                               # the required class itself, not another one
        assert sum(1 for p in samples if want not in sg.client_view_findings(p)) == 0, cls


def test_strict_only_digit_cases_are_not_card_or_ssn_in_the_client_view():
    for p in strict_only_cases()["nine_digits_one_digit_tail"]:
        assert not {"card", "ssn"} & set(sg.client_view_findings(p))


def _is_bracketed(t: str) -> bool:
    return any(b in t for b in BRACKET_DOTS)


def test_defanged_forms_are_masked_even_after_prose_words_and_retailers():
    rng = random.Random(SEED + 4)
    texts = []
    for b in BRACKET_DOTS:
        texts += [f"It is available at amazon{b}com now.", f"Buy it at walmart {b} com today.",
                  f"Customers at {r6._mail(rng)[1]}{b}com rate it.", f"Sold at {r6._mail(rng)[1]} {b} co {b} uk."]
    assert sum(1 for t in texts if "email" not in sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in texts)


def test_spelled_variants_are_never_exempt_in_the_client_view():
    texts = [f"It is available{sep}amazon{d}com now." for sep in ("-at-", "_at_", " at sign ", " arroba ", " arobase ")
             for d in ("-dot-", " period ", " punto ", ".")]
    assert sum(1 for t in texts if "email" not in sg.client_view_findings(t)) == 0


# ------------------------------------------------------------------------------------------------ recall (required classes)

def recall_table() -> dict:
    """class / disguise -> (samples, strict misses, client misses of the REQUIRED class): the 601e328a recall corpus
    (every leak-corpus character disguise and pair, number / e-mail / label disguises) plus every case above."""
    table: dict = {}
    for cls, d, p in r6.recall_corpus():
        n, s, c = table.get(("601e328a", cls), (0, 0, 0))
        # c13529af (decision 22): the plain spelled form is shown to the owner (not a client-view miss)
        miss = not sg.client_view_findings(p) and not r6.plain_spelled(p)
        table[("601e328a", cls)] = (n + 1, s + (sg.check(p) is None), c + miss)
    for name, samples in all_cases().items():
        want = CLASS_OF[name]
        for p in samples:
            f = sg.client_view_findings(p)
            n, s, c = table.get(("08eea6e1", name), (0, 0, 0))
            table[("08eea6e1", name)] = (n + 1, s + (sg.check(p) is None), c + (not f if want is None else want not in f))
    return table


def test_recall_is_complete_in_both_lanes_on_every_required_class():
    table = recall_table()
    assert sum(n for n, _s, _c in table.values()) >= 3300
    assert {k: v for k, v in table.items() if v[1] or v[2]} == {}


# ------------------------------------------------------------------------------------------------ mutation bite

def _old_group(mp):
    mp.setattr(sg, "WORD_BRIDGE_GROUP", (2, 6))


def _no_variants(mp):
    mp.setattr(sg, "_VAR_AT", NEVER)
    mp.setattr(sg, "_VAR_DOT", NEVER)


def _no_glued(mp):
    mp.setattr(sg, "_GLUED_AT", NEVER)
    mp.setattr(sg, "_GLUED_DOT", NEVER)


def _no_trim_old_offsets(mp):
    mp.setattr(sg, "DECODE_TRIM", 0)
    mp.setattr(sg, "B64_OFFSETS", 4)


def _old_ssn_sep(mp):
    mp.setattr(sg, "_cv_ssn_sep", lambda x: sg._cv_sep(x, " "))


BITES = {
    # name: (patch, case class, lanes that must leak once the rule is removed)
    "defanged_view_at": (lambda mp: mp.setattr(sg, "_defanged", lambda v: v), "defanged_at", ("strict", "client")),
    "defanged_view_spelled": (lambda mp: mp.setattr(sg, "_defanged", lambda v: v), "defanged_spelled",
                              ("strict", "client")),
    "variants_hyphen_underscore": (_no_variants, "variant_hyphen_underscore", ("strict", "client")),
    "variants_period_sign": (_no_variants, "variant_period_sign", ("strict", "client")),
    "variants_nit_spellings": (_no_variants, "nit_spellings", ("strict", "client")),
    "variants_glued": (_no_glued, "variant_glued", ("strict", "client")),
    "bridge_1_8_groups_8_8": (_old_group, "card_8_8_words", ("strict", "client")),
    "bridge_1_8_groups_7_8": (_old_group, "card_7_8_words", ("strict", "client")),
    "bridge_one_digit_tail": (lambda mp: mp.setattr(sg, "BRIDGE_ONE_DIGIT_RUN", 10 ** 9), "card_tail_1_digit",
                              ("client",)),
    "bridge_one_digit_strict": (lambda mp: mp.setattr(sg, "BRIDGE_ONE_DIGIT_RUN", 10 ** 9),
                                "nine_digits_one_digit_tail", ("strict",)),
    "number_words_card": (lambda mp: mp.setattr(sg, "_number_word_view", lambda v: v), "card_number_words",
                          ("strict", "client")),
    "number_words_ssn": (lambda mp: mp.setattr(sg, "_number_word_view", lambda v: v), "ssn_number_words",
                         ("strict", "client")),
    "b64_trim_suffix": (lambda mp: mp.setattr(sg, "DECODE_TRIM", 0), "b64_suffix_1_3", ("client",)),
    "b64_offsets_and_trim_prefix": (_no_trim_old_offsets, "b64_prefix_4_7", ("client",)),
    "cv_ssn_separators": (_old_ssn_sep, "ssn_strict_separators", ("client",)),
}


def _cases(name: str) -> list:
    return {**all_cases(), **strict_only_cases()}[name]


@pytest.mark.parametrize("name", sorted(BITES))
def test_each_new_rule_is_load_bearing(monkeypatch, name):
    patch, cls, lanes = BITES[name]
    probes = _cases(cls)
    assert all(sg.check(p) is not None for p in probes), name
    patch(monkeypatch)
    if "strict" in lanes:
        assert sum(1 for p in probes if sg.check(p) is None) > 0, (name, "strict: removing this rule broke nothing")
    if "client" in lanes:
        want = CLASS_OF.get(cls)
        leaks = sum(1 for p in probes if (not sg.client_view_findings(p) if want is None
                                          else want not in sg.client_view_findings(p)))
        assert leaks > 0, (name, "client: removing this rule broke nothing")


def test_offsets_4_7_alone_read_aligned_prefixes(monkeypatch):
    probes = b64_cases()["b64_prefix_4_7"]
    monkeypatch.setattr(sg, "DECODE_TRIM", 0)                          # offsets alone (no trim) still read them
    assert sum(1 for p in probes if not sg.client_view_findings(p)) == 0
    monkeypatch.setattr(sg, "B64_OFFSETS", 4)
    assert sum(1 for p in probes if not sg.client_view_findings(p)) > 0


def test_decoding_stays_bounded_per_run():
    run = "A" * 64
    assert len(sg._b64_starts(run)) == sg.B64_OFFSETS
    run = "-".join(["AAAA"] * 40)
    assert len(sg._b64_starts(run)) <= sg.B64_OFFSETS + sg.DECODE_BOUNDARIES_MAX
    assert sg._printable(b"\xff\xfe\xfd" + b"x" * 40 + b"\xff\xfe\xfd") == "x" * 40
    # c0c09bd1 MUST 4: up to DECODE_TRIM (6) bytes are trimmed; one more garbage byte is not
    assert sg._printable(b"\xff" * (sg.DECODE_TRIM + 1) + b"x" * 40) is None
    assert sg._printable(b"x" * 20 + b"\xff" + b"x" * 20) is None       # nor a bad byte inside the body


def test_the_one_digit_limit_keeps_prose_numbers(monkeypatch):
    texts = ["Choose {} or {} sizes, pack {} or {}, from {} to {}.".format(*(str(random.Random(i).randint(1, 9))
                                                                         for _ in range(4)), 2000 + i % 27, 2001 + i % 25)
             for i in range(40)]
    assert sum(1 for t in texts if sg.check(t) is not None) == 0
    monkeypatch.setattr(sg, "BRIDGE_ONE_DIGIT_RUN", 0)                 # mutation: 1-digit groups always bridge
    assert sum(1 for t in texts if sg.check(t) is not None) > 0


def test_number_words_need_nine_in_a_row():
    rng = random.Random(SEED + 5)
    d = _r(rng, string.digits, 8)
    assert sg._number_word_view(f"call {_words(d)} now") == f"call {_words(d)} now"
    assert sg._number_word_view(f"call {_words(d + '1')} now") == f"call {d}1 now"
    assert sg.check("I have one or two cats, oh and one dog.") is None


def test_fragment_is_decoded_to_the_depth_of_a_path(monkeypatch):
    rng = random.Random(SEED + 6)
    loc, dom = r6._mail(rng)
    e = loc + AT + dom + ".com"
    deep = e
    for _ in range(4):
        deep = quote(deep, safe="")
    url = "https://site.test/a/b#" + deep
    assert sg.inspect_url(url)["fragment_kind"] is not None and deep not in sg.safe_url(url)
    assert sg.inspect_url("https://site.test/a/" + deep)["segments"][-1][1] is not None   # the path, same depth
    assert sg.client_view_url(url)[1] is not None
    monkeypatch.setattr(sg, "FRAGMENT_EXTRA_DECODE", 0)
    assert deep in sg.safe_url(url) and sg.client_view_url(url)[1] is None


# ------------------------------------------------------------------------------------------------ utility (client view)

RETAILERS = ("amazon.com", "walmart.com", "target.com", "chewy.com", "petco.com", "etsy.com", "ebay.com", "bestbuy.com",
             "amazon.co.uk", "costco.com", "homedepot.com", "sephora.com")
BRAND_SITES = ("brightpaws.com", "acmepets.com", "kittyhaven.com", "pawsome.co", "catcorner.shop", "furfriends.store",
               "sphynxwear.com", "purrfectfit.io", "zbestmedia.com", "tailwag.co.uk")
PLURALS = ("Customers", "Shoppers", "Buyers", "Users", "Reviewers", "People", "Fans", "Members", "Visitors", "Readers")
PROSE = ("available", "sold", "found", "listed", "stocked", "featured", "priced", "cheaper", "reviewed", "launched")
TEMPLATES = [
    "Buy it at {r} for ${p}.{c} with free shipping over ${q}.",
    "Buy them at {r} in packs of {n} or {m}.",
    "{pl} at {b} rate it {s} out of 5 after {k} reviews.",
    "{pl} at {r} gave it {s} stars in {y}.",
    "Use code SAVE{n} at checkout on {b} today.",
    "Apply the coupon at checkout on {r} for {n}% off.",
    "It is {w} at {b} for ${p}.{c} and ships in {n} to {m} days.",
    "The bed is {w} at {r2} dot com and {w2} at {b} as well.",
    "Sizes {n}, {m} and {o} fit cats from {n} to {k2} pounds.",
    "Dimensions are {n} x {m} x {o} inches and it weighs {s} pounds.",
    "Rated {s} by {k} shoppers between {y} and {y2}.",
    "We restocked {k} units in {y}, then {k2} more in {y2}.",
    "Order {n} for ${p} or {m} for ${q}; the {y} model is {w} at {b}.",
    "Pack of {n} at ${p}.{c} each, or {m} for ${q}.",
    "The {y} edition sold {k} copies, up from {k2} in {y2}.",
    "Item {k} ships from our warehouse in {n} days.",
    "Free returns within {n} days at {b} or {r}.",
    "Prices start at ${p} and go up to ${q} for the deluxe set.",
    "Shop the full line at {b}, from ${p} to ${q}.",
    "Our team visited {k2} stores in {y} and {n} cities in {y2}.",
]


def shop_prose(seed: int = SEED + 7, n: int = 480) -> list:
    """A generated e-commerce prose corpus: the four utility forms of SHOULD 5 (it / them before a retailer, plural
    nouns, "at checkout on <domain>", prose words before a brand site) among prices, sizes, years and quantities."""
    rng = random.Random(seed)
    return [TEMPLATES[i % len(TEMPLATES)].format(
        r=rng.choice(RETAILERS), r2=rng.choice(RETAILERS).split(".")[0], b=rng.choice(BRAND_SITES),
        pl=rng.choice(PLURALS), w=rng.choice(PROSE), w2=rng.choice(PROSE), p=rng.randint(5, 499),
        c=f"{rng.randint(0, 99):02d}", q=rng.randint(25, 999), n=rng.randint(1, 12), m=rng.randint(2, 24),
        o=rng.randint(3, 36), k=rng.randint(10, 99999), k2=rng.randint(10, 9999), y=rng.randint(1995, 2026),
        y2=rng.randint(1995, 2026), s=f"{rng.randint(3, 4)}.{rng.randint(0, 9)}") for i in range(n)]


def _excerpt_masked(answers: list, brand: str = "Z Best Media") -> int:
    masked = 0
    for a in answers:
        ex = callum.excerpt(a, [brand])
        d = callum._display("excerpt", callum._raw_excerpt(a, [brand]), ex["withheld"],
                            callum._excerpt_view_findings(a, [brand]))
        masked += bool(d and d["alert"])
    return masked


# measured at this commit (both lanes; the strict lane is unchanged from 08eea6e1 on every corpus)
CV_SHOP_PROSE_MASKED = 0.05
STRICT_SHOP_PROSE_WITHHELD = 366 / 480                 # 366 at 08eea6e1 too (the spelled " at <domain>" forms)


def test_client_view_utility_on_generated_shop_prose_is_under_the_bar():
    texts = shop_prose()
    assert len(texts) >= 400
    answers = ["Z Best Media recommends this bed. " + t for t in texts]
    assert _excerpt_masked(answers) / len(answers) <= CV_SHOP_PROSE_MASKED
    assert sum(1 for t in texts if sg.client_view_findings(t)) / len(texts) <= CV_SHOP_PROSE_MASKED
    assert sum(1 for t in texts if sg.check(t) is not None) / len(texts) <= STRICT_SHOP_PROSE_WITHHELD


def test_client_view_utility_on_the_repo_corpora_is_unchanged():
    ex = uc.excerpts()
    masked = 0
    for i, b, e in ex:
        d = callum._display("excerpt", callum._raw_excerpt(uc.ANSWERS[i], [b] if b else []), e["withheld"],
                            callum._excerpt_view_findings(uc.ANSWERS[i], [b] if b else []))
        masked += bool(d and d["alert"])
    assert masked / len(ex) <= 0.05
    assert sum(1 for _i, _b, e in ex if e["withheld"]) == 22                    # the strict lane: unchanged
    urls = [u for _, u in uc.urls()]
    assert sum(1 for u in urls if sg.client_view_url(u)[1]) / len(urls) <= 0.025


def test_the_four_utility_forms_are_shown_and_role_words_stay_masked():
    rng = random.Random(SEED + 8)
    shown = [f"Buy {p} at {r} today." for p in ("it", "them", "It") for r in RETAILERS]
    shown += [f"{pl} at {b} rate it 4.5 stars." for pl in PLURALS for b in BRAND_SITES[:3]]
    shown += [f"Use code SAVE10 at checkout on {b} today." for b in BRAND_SITES]
    shown += [f"It is {w} at {b} now." for w in PROSE for b in BRAND_SITES[:4]]
    # c13529af (decision 22): the plain form is shown for EVERY single word, role words and pronouns included (they
    # were masked until c13529af)
    shown += [f"Write {w} at {b} today." for w in sorted(sg.CV_ROLE_WORDS) for b in BRAND_SITES[:2]]
    shown += [f"Buy {p} at {b} today." for p in ("it", "them") for b in BRAND_SITES]
    assert sum(1 for t in shown if sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in shown)                   # the strict lane withholds them all
    loc, dom = r6._mail(rng)
    masked = [f"Write {w} at {b.replace('.', ' dot ')} today." for w in sorted(sg.CV_ROLE_WORDS)
              for b in BRAND_SITES[:2]]
    masked += [f"It is available at {loc}{AT}{dom}.com today.", f"Customers at {loc} at {dom} dot com rate it.",
               f"Write {loc}2 at {dom}.com today.", f"Write {loc}.x at {dom}.com today."]
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) == 0


# c13529af (decision 22): the "pronoun_before_retailer" and "plural_nouns" bites were removed with the lists they
# exercised (now inert); the plain-form rule that replaces them is bitten in tests/test_review_c13529af.py
UTILITY_BITES = {
    "join_stop_words": (lambda mp: mp.setattr(sg, "CV_JOIN_STOP", frozenset()),
                        [f"Use code SAVE10 at checkout on {b} today." for b in BRAND_SITES]),
}


@pytest.mark.parametrize("name", sorted(UTILITY_BITES))
def test_each_utility_rule_is_load_bearing(monkeypatch, name):
    patch, texts = UTILITY_BITES[name]
    assert sum(1 for t in texts if sg.client_view_findings(t)) == 0
    patch(monkeypatch)
    assert sum(1 for t in texts if sg.client_view_findings(t)) > 0, name


def test_the_defanged_exception_to_the_prose_rule_is_load_bearing(monkeypatch):
    texts = [f"It is available at kittyhaven{b}com now." for b in BRACKET_DOTS]
    assert sum(1 for t in texts if "email" not in sg.client_view_findings(t)) == 0
    monkeypatch.setattr(sg, "_cv_defanged_after", lambda v, i: False)
    monkeypatch.setattr(sg, "_defanged", lambda v: v)
    assert sum(1 for t in texts if "email" not in sg.client_view_findings(t)) > 0


# ------------------------------------------------------------------------------------------------ timing

def _fill(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


ADVERSARIAL = {
    "defanged_dots": lambda n: _fill("x" + AT + "y[.]z(.)", n),
    "defanged_spelled": lambda n: _fill("x at y [ . ] ", n),
    "variant_hyphen": lambda n: _fill("a-at-b-dot-", n),
    "variant_glued": lambda n: _fill("aATbDOT", n),
    "variant_words": lambda n: _fill("x at sign y period ", n),
    "variant_underscore": lambda n: _fill("_at_ _dot_ ", n),
    "nit_spellings": lambda n: _fill("x arroba y punto ", n),
    "bridge_8_1": lambda n: _fill("1234" + "5678 then 9 ", n),
    "bridge_ones": lambda n: _fill("1 and ", n),
    "bridge_7_o": lambda n: _fill("1234567 o ", n),
    "number_words": lambda n: _fill("four one ", n),
    "number_words_oh": lambda n: _fill("oh ", n),
    "number_words_break": lambda n: _fill("one two three four five six seven eight nine x", n),
    "b64_runs": lambda n: _fill(_b64("the quick brown fox jumps " * 20) + " ", n),
    "b64_one_run": lambda n: _fill(_b64("the quick brown fox jumps "), n),
    "ssn_seps": lambda n: _fill("12" + "3_4" + "5_67" + "89 ", n),
    "cv_unprose": lambda n: _fill("Customers at x.com Buy it at amazon.com at [.] ", n),
    "join_stop": lambda n: _fill("a at b c d e on ", n),
}


def _timed(fn, t):
    best = None
    for _ in range(2):
        t0 = time.perf_counter()
        fn(t)
        d = time.perf_counter() - t0
        best = d if best is None else min(best, d)
    return best


ENTRY_FNS = {"check": sg.check, "client_view": sg.client_view_findings, "guard_record": lambda t: sg.guard_record({"v": t}),
             "excerpt": lambda t: callum.excerpt(t, ["Z Best Media"]), "field_check": callum.field_check}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_every_new_rule_stays_linear(name):
    gen = ADVERSARIAL[name]
    for fname, fn in ENTRY_FNS.items():
        t16, t64 = _timed(fn, gen(16 * 1024)), _timed(fn, gen(64 * 1024))
        assert t64 < 1.0, (name, fname, t64)                          # 200 ms measured bar; 1 s here for slow CI
        assert t64 < 8 * t16 + 0.05, (name, fname, t16, t64)


# ------------------------------------------------------------------------------------------------ the real service

def _service_values() -> dict:
    rng = random.Random(SEED + 9)
    loc1, dom1 = r6._mail(rng)
    loc2, dom2 = r6._mail(rng)
    loc3, dom3 = r6._mail(rng)
    loc4, dom4 = r6._mail(rng)
    card = r6._card(rng, "4", 16)
    amex = r6._card(rng, "37", 15)
    a, g, s = r6._ssn(rng)
    key = _key(rng)
    return {"defanged": f"{loc1}{AT}{dom1}[.]com", "defanged_local": loc1,
            "variant": f"{loc2}-at-{dom2}-dot-com", "variant_local": loc2,
            "glued": f"{loc3}AT{dom3}DOTcom", "glued_local": loc3,
            "arroba": f"{loc4} arroba {dom4} punto com", "arroba_local": loc4,
            "card_88": f"{card[:8]} then {card[8:]}", "card_digits": card,
            "card_words": _words(amex), "amex_digits": amex,
            "ssn_seps": f"{a}_{g}_{s}", "ssn_digits": a + g + s,
            "b64_key": B64_PREFIXES[0] + _b64(key) + B64_SUFFIXES[2], "key": key}


def _service_answers(v: dict) -> list:
    cited = "https://competitor.test/blog/guide"
    a = ProviderAnswer("ANSWER", f"Z Best Media support: write to {v['defanged']} or {v['variant']} or {v['arroba']}, "
                                 f"ref {v['ssn_seps']}, card {v['card_88']} or {v['card_words']} today.",
                       [cited], "m", "1", cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
    a.citation_titles = {cited: "Z Best Media key " + v["b64_key"]}
    a.citation_hints = {cited: "Z Best Media " + v["glued"]}
    return [a]


SECRET_NAMES = ("defanged_local", "variant_local", "glued_local", "arroba_local", "card_digits", "card_words",
                "amex_digits", "ssn_seps", "ssn_digits", "key", "card_88")


def test_a_real_citation_run_writes_none_of_the_new_shapes_and_the_owner_view_masks_them(tmp_path, caplog):
    v = _service_values()
    secrets_ = {k: v[k] for k in SECRET_NAMES}
    h = vw.harness(tmp_path)
    h.ports.engines["openai"] = r6._Port(_service_answers(v))
    h.ok(h.post("/tenants/zbm/domains", {"request_id": vw.rid(), "domains": [vw.DOMAIN]}, andre=True))
    run = vw.citation_run(h)
    assert run["status"] == "completed" and run["result"]["outcome"] == "OK"
    leaks = vw.found(vw.written(h, caplog), secrets_)
    assert leaks == [], leaks                                          # names only: data dir, ledger, logs
    assert len(h.svc.vault) >= 1
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}"))
    shown = vw.found(json.dumps(got, ensure_ascii=False), secrets_)
    assert shown == [], shown                                          # the owner view masks every class
    ev = [r["evidence"] for r in got["result"]["facts"]["rows"] if r.get("evidence")]
    assert ev and all(e[k]["alert"] for e in ev for k in ("excerpt", "title", "hint") if e.get(k))
    for field, cls in (("defanged", "email"), ("variant", "email"), ("glued", "email"), ("arroba", "email"),
                       ("card_88", "card"), ("card_words", "card"), ("ssn_seps", "ssn")):
        assert cls in sg.client_view_findings(v[field]), field
    assert sg.client_view_kind(sg.client_view_findings(v["b64_key"])) == sg.CREDENTIAL


# ------------------------------------------------------------------------------------------------ the vault repair

def _vault(tmp_path, n: int = 3) -> tuple:
    key = vault.ephemeral_vault_key()
    d = str(tmp_path / "data")
    cv = vault.CitationVault(d, key)
    ids = [cv.put("zbm", {"i": i}) for i in range(n)]
    return key, d, cv.path, ids


def _side_files(d: str) -> list:
    return sorted(f for f in os.listdir(d) if f != vault.VAULT_NAME)


def test_a_vault_with_its_newlines_removed_refuses_to_open_and_nothing_is_cut(tmp_path):
    key, d, p, _ids = _vault(tmp_path)
    raw = open(p, "rb").read().replace(b"\n", b"")
    with open(p, "wb") as fh:
        fh.write(raw)
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    assert open(p, "rb").read() == raw and _side_files(d) == []


def test_merged_records_in_the_final_line_refuse_to_open(tmp_path):
    key, d, p, _ids = _vault(tmp_path)
    lines = open(p, "rb").read().split(b"\n")[:-1]
    raw = b"\n".join(lines[:-2]) + b"\n" + lines[-2] + lines[-1]         # the last two records merged, no newline
    with open(p, "wb") as fh:
        fh.write(raw)
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    raw2 = b"\n".join(lines) + b"\n" + lines[0][: len(lines[0]) // 2] + lines[1][:20]   # two torn starts merged
    with open(p, "wb") as fh:
        fh.write(raw2)
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    assert open(p, "rb").read() == raw2 and _side_files(d) == []


def test_a_fragment_longer_than_a_line_refuses(tmp_path):
    key, d, p, _ids = _vault(tmp_path, 1)
    with open(p, "ab") as fh:
        fh.write(b'{"v":1,"id":"' + b"A" * vault._MAX_LINE_BYTES)
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    assert _side_files(d) == []


def test_a_complete_last_record_without_its_newline_is_kept(tmp_path):
    key, d, p, ids = _vault(tmp_path)
    raw = open(p, "rb").read()
    with open(p, "wb") as fh:
        fh.write(raw[:-1])
    cv = vault.CitationVault(d, key)
    assert len(cv) == 3 and [cv.get("zbm", i)["i"] for i in ids] == [0, 1, 2]
    assert open(p, "rb").read() == raw
    rep = dict(cv.tail_repair)
    assert len(rep.pop("repair_id")) == 32
    assert rep == {"torn_lines": 0, "bytes_removed": 0, "records_removed": 0, "records_kept": 3, "newline_added": 1,
                   "quarantined": 0}
    nid = cv.put("zbm", {"i": 3})
    again = vault.CitationVault(d, key)
    assert len(again) == 4 and again.get("zbm", nid) == {"i": 3}


def test_a_genuine_torn_fragment_is_cut_quarantined_and_recorded_with_counts_only(tmp_path):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    vw.citation_run(h)
    n = len(h.svc.vault)
    p = h.svc.vault.path
    frag = b'{"v":1,"id":"' + _r(random.Random(SEED + 10), "ABCDEFGH", 11).encode()
    with open(p, "ab") as fh:
        fh.write(frag)
    h2 = h.restart()
    st = h2.svc.health()["citation_vault"]["tail_repair"]
    assert st["records_removed"] == 1 and st["records_kept"] == n and st["quarantined"] == 1 and st["recorded"]
    assert st["bytes_removed"] == len(frag) and open(p, "rb").read().endswith(b"\n")
    d = os.path.dirname(p)
    side = _side_files(d)
    q = [f for f in side if f.startswith(vault.VAULT_NAME + vault.QUARANTINE_SUFFIX)]
    assert len(q) == 1 and not any(f.endswith(vault.REPAIR_MARKER_SUFFIX) for f in side)   # marker gone once recorded
    qp = os.path.join(d, q[0])
    assert open(qp, "rb").read() == frag and stat.S_IMODE(os.stat(qp).st_mode) == 0o600
    evs = h2.ledger.of_type("citation_vault_tail_repaired")
    assert len(evs) == 1 and set(evs[0]["_payload"]) == set(vault.REPAIR_FIELDS)
    assert frag.decode()[12:] not in json.dumps(evs)                       # counts, never content
    assert len(h2.svc.vault) == n


def _torn_harness(tmp_path):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    vw.citation_run(h)
    n = len(h.svc.vault)
    with open(h.svc.vault.path, "ab") as fh:
        fh.write(b'{"v":1,"id":"' + b"B" * 9)
    h.svc.close()
    return h, n


def _again(h):
    return helpers.Harness(h.tmp, h.settings.data_dir, h.ledger, h.clock, h.ports, real_compliance=h.real_compliance)


def _forced_failure(h, monkeypatch):
    cls = type(h.svc)

    def boom(self, **k):
        raise RuntimeError("forced start failure")
    monkeypatch.setattr(cls, "_start", boom)
    with pytest.raises(RuntimeError):
        _again(h)
    monkeypatch.undo()


def test_a_start_that_fails_after_the_cut_records_the_repair_on_the_next_start(tmp_path, monkeypatch):
    h, n = _torn_harness(tmp_path)
    _forced_failure(h, monkeypatch)
    d = h.settings.data_dir
    assert any(f.endswith(vault.REPAIR_MARKER_SUFFIX) for f in _side_files(d))      # the durable marker
    assert h.ledger.of_type("citation_vault_tail_repaired") == []
    assert not open(os.path.join(d, vault.VAULT_NAME), "rb").read().endswith(b"B")   # the cut happened
    h2 = _again(h)
    evs = h2.ledger.of_type("citation_vault_tail_repaired")
    assert len(evs) == 1 and evs[0]["_payload"]["records_removed"] == 1 and evs[0]["_payload"]["records_kept"] == n
    assert not any(f.endswith(vault.REPAIR_MARKER_SUFFIX) for f in _side_files(d))
    h3 = h2.restart()
    assert len(h3.ledger.of_type("citation_vault_tail_repaired")) == 1 and h3.svc.vault.tail_repair is None


def test_a_marker_left_after_recording_is_the_same_event_next_time(tmp_path, monkeypatch):
    h, _n = _torn_harness(tmp_path)
    monkeypatch.setattr(vault.CitationVault, "clear_repair_marker", lambda self: None)
    h2 = _again(h)
    h3 = h2.restart()
    assert len(h3.ledger.of_type("citation_vault_tail_repaired")) == 1             # the ledger takes it as the same


def test_an_observe_only_start_with_a_marker_records_nothing(tmp_path, monkeypatch):
    h, _n = _torn_harness(tmp_path)
    _forced_failure(h, monkeypatch)
    key = h.settings.vault_key
    cv = vault.CitationVault(h.settings.data_dir, key, repair=False)
    assert cv.tail_repair is None and not cv.repairs


def test_the_marker_rule_is_load_bearing(tmp_path, monkeypatch):
    h, _n = _torn_harness(tmp_path)
    _forced_failure(h, monkeypatch)
    monkeypatch.setattr(vault.CitationVault, "_read_marker", lambda self: None)     # mutation: no marker read
    h2 = _again(h)
    assert h2.ledger.of_type("citation_vault_tail_repaired") == []                   # the repair would be lost


def test_the_fragment_bound_is_load_bearing(tmp_path, monkeypatch):
    key, d, p, _ids = _vault(tmp_path)
    raw = open(p, "rb").read().replace(b"\n", b"")
    with open(p, "wb") as fh:
        fh.write(raw)
    monkeypatch.setattr(vault, "_torn_fragment", lambda tail: True)                  # mutation: unbounded repair
    cv = vault.CitationVault(d, key)
    assert len(cv) == 0                                                            # every record would be cut


def test_the_complete_record_rule_is_load_bearing(tmp_path, monkeypatch):
    key, d, p, _ids = _vault(tmp_path)
    raw = open(p, "rb").read()
    with open(p, "wb") as fh:
        fh.write(raw[:-1])
    monkeypatch.setattr(vault, "_parse_line", lambda line, real=vault._parse_line: real(line) if line.endswith(b"}")
                        and line != raw[:-1].rsplit(b"\n", 1)[-1] else None)          # mutation: last line unread
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)                                                # it is not a fragment: refused
