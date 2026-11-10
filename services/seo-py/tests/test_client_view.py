"""ADR 0051 job 3: the client view's precise masking (secret_guard.client_view_findings / client_view_url).

The owner's decrypted view of its own citation evidence masks a field only on HIGH-PRECISION findings; the strict
filter (secret_guard.check and the URL rules) is unchanged and stays the only filter for logs, outbound data and
cross-tenant surfaces. Every credential-, card-, SSN-, phone- or e-mail-shaped value here is BUILT AT RUN TIME from
fragments and seeded random draws (the generators of leak_corpus_v2 / secret_corpus); no assertion message prints
one (names and counts only)."""

from __future__ import annotations

import random
import string
import time

import leak_corpus_v2 as lc
import pytest
import secret_corpus

from agents import secret_guard as sg

RESIDUAL = frozenset(("aws_secret", "aws_secret_mixed", "hex32_unlabelled", "hex64_unlabelled", "b64_unlabelled"))


def _klass(family: str) -> str:
    if family in RESIDUAL:
        return "residual"
    if family in ("card_visa", "card_amex"):
        return "card"
    if family in ("phone_us", "phone_intl"):
        return "phone"
    if family in ("ssn", "email"):
        return family
    if family in ("labelled", "pin", "passphrase_words"):
        return "labelled"
    return "strong_key"


def _luhn_card(r: random.Random, prefix: str, n: int) -> str:
    body = prefix + "".join(r.choice(string.digits) for _ in range(n - len(prefix) - 1))
    return next(body + c for c in string.digits if sg._luhn(body + c))


# ------------------------------------------------------------------------------------------------ recall (100%)

def test_text_recall_is_complete_for_every_class_but_the_documented_residual():
    tot: dict = {}
    missed: dict = {}
    for c in lc.corpus():
        k = _klass(c.family)
        tot[k] = tot.get(k, 0) + 1
        if not sg.client_view_findings(c.carrier):
            missed.setdefault(k, []).append(c.name)
    for k in ("strong_key", "labelled", "card", "ssn", "phone", "email"):
        assert tot[k] > 0 and not missed.get(k), (k, tot[k], sorted(missed.get(k, []))[:10])
    # residual (ADR 0051 decision 13): unlabelled random tokens are not masked in the client view
    assert len(missed.get("residual", [])) > 0


def test_url_recall_is_complete_and_the_value_never_shows():
    missed = []
    n = 0
    for c in lc.url_cases():
        if _klass(c.family) == "residual":
            continue
        n += 1
        shown, kind = sg.client_view_url(c.carrier)
        if kind is None or (shown is not None and any(p in shown for p in c.probes)):
            missed.append(c.name)
    assert n > 300 and missed == [], sorted(missed)[:10]


def test_secret_corpus_strong_keys_and_labelled_credentials_are_masked():
    keys = secret_corpus.strong_keys(random.Random(5050))
    missed = [k for k, v in keys.items() if _klass(k) != "residual"
              and not sg.client_view_findings(f"Z Best Media note: {v} end.")]
    assert missed == [], missed
    lab = secret_corpus.labelled(random.Random(5051))
    missed = [name for name, text, _v, _w in lab if not sg.client_view_findings("Z Best Media. " + text)]
    assert len(lab) >= 20 and missed == [], missed


# ------------------------------------------------------------------------------------------------ precision

PROSE_KEPT = [
    "Use a password manager and turn on two-factor sign-in.", "The token economy explained for small sellers.",
    "Our cookie policy changed in 2026.", "API keys explained: what they are and how to rotate them.",
    "Pass the holiday rush with these 12 tips.", "The secret menu items at the cafe are listed below.",
    "Bearer bonds were common in the 1920s.", "Authorization letter template (free download).",
    "The key takeaway is revenue growth.", "PIN codes and passcodes: a primer for shop owners.",
    "Order 112-1234567-1234567 shipped on 2026-11-08.", "ISBN 978-3-16-148410-0 is in stock.",
    "Prices from $1,299.99 to $2,499.00 in 2024-2025 (12 items).", "Call us between 9:00 and 17:30, Monday-Friday.",
    "Revenue grew 1,250,000 units in Q3 2026; the target is 3,000,000.", "Item 550e8400-e29b-41d4-a716-446655440000 ok.",
    "commit 8f14e45fceea167a5a36dedd4bea2543c1b2d3e4 fixed it", "Meet us (at) the venue on Friday.",
    "The password would be reset after 90 days.", "Klaviyo vs Omnisend: pricing, flows and reviews for 2026.",
]


@pytest.mark.parametrize("text", PROSE_KEPT)
def test_bare_label_words_and_ordinary_numbers_are_shown_in_full(text):
    assert sg.client_view_findings(text) == [], sg.client_view_findings(text)


def test_the_strict_lane_is_unchanged_by_the_client_view():
    # the bare label word still withholds a field from the log (check (a)); only the owner's view shows it
    assert sg.check(PROSE_KEPT[0]) == "a_label" and sg.check(PROSE_KEPT[2]) == "a_label"
    assert sg.client_view_findings(PROSE_KEPT[0]) == []


URLS_KEPT = [
    "https://x.com/acme/status/1852960912345678901", "https://www.aliexpress.com/item/1005006123456789.html",
    "https://github.com/acme/site/commit/8f14e45fceea167a5a36dedd4bea2543c1b2d3e4",
    "https://site.test/password-reset-guide", "https://site.test/api-key-best-practices",
    "https://site.test/?gclid=Cj0KCQiA6Ou5BhCrARIsAPoTxrB8x1",
    "https://docs.google.com/document/d/1A2b3C4d5E6f7G8h9I0jK1lM2nO3pQ4rS5tU6vW7xY8z/edit",
    "https://www.amazon.com/dp/B0C1234567", "https://site.test/products/12345678901234567890",
    "https://cdn.jsdelivr.net/npm/acme-widget@1.2.3/dist/acme.min.js", "https://site.test/search?q=password+protection",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.etsy.com/listing/1523456789/rug",
]


@pytest.mark.parametrize("url", URLS_KEPT)
def test_ordinary_urls_are_shown_exactly_as_given(url):
    assert sg.client_view_url(url) == (url, None)


def test_only_the_offending_url_part_is_hidden():
    r = random.Random(51030)
    key = "AK" + "IA" + "".join(r.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    card = _luhn_card(r, "4", 16)
    tok = "".join(r.choice(string.ascii_letters) for _ in range(20)) + "".join(r.choice(string.digits) for _ in range(14))
    cases = {
        f"https://site.test/u/{key}/page": ("https://site.test/u/{hidden}/page", sg.CREDENTIAL),
        f"https://site.test/order/{card}?x=1": ("https://site.test/order/{hidden}?x=1", sg.PERSONAL),
        "https://site.test/p?id=7&access_token=" + tok[:8]: ("https://site.test/p?id=7&access_token={hidden}",
                                                            sg.CREDENTIAL),
        f"https://site.test/p?ref=a&state={tok}": ("https://site.test/p?ref=a&state={hidden}", sg.CREDENTIAL),
        f"https://site.test/p?gclid={tok}": (None, None),                     # analytics key: entropy rule exempt
        "https://ad" + "min:" + tok[:10] + "@site.test/x": ("https://{hidden}@site.test/x", sg.CREDENTIAL),
        f"https://{key.lower()}.site.test/p": (None, sg.CREDENTIAL),          # a host carrying a value: whole URL
    }
    for url, (shown, kind) in cases.items():
        got = sg.client_view_url(url)
        want = (url, None) if kind is None else (shown, kind)
        ok = got == want and (kind is None or (key not in (got[0] or "") and card not in (got[0] or "")))
        assert ok, (shown, got[1])                                    # never the value itself


def test_labelled_forms_and_formatted_numbers_are_masked():
    r = random.Random(51031)
    v = "".join(r.choice(string.ascii_lowercase) for _ in range(6)) + str(r.randint(10, 99))
    area = r.randint(100, 665)
    ssn = f"{area}-{r.randint(10, 99)}-{r.randint(1000, 9999)}"
    phone = f"({r.randint(201, 989)}) {r.randint(200, 999)}-{r.randint(1000, 9999)}"
    mail = "".join(r.choice(string.ascii_lowercase) for _ in range(8)) + " at " + "example" + " dot com"
    card = _luhn_card(r, "4", 16)
    want = {f"password: {v}": "label_value", f"the token is {v}": "label_value", f'{{"api_key": "{v}"}}': "label_value",
            f"DB_PASS={v}": "label_value", f"ssn {ssn} on file": "ssn", f"call {phone} today": "phone",
            f"write to {mail}": "email", f"card {' '.join(card[i:i + 4] for i in (0, 4, 8, 12))}": "card"}
    for text, name in want.items():
        found = sg.client_view_findings(text)
        assert name in found, (name, found)
    bad_area = f"{666}-{r.randint(10, 99)}-{r.randint(1000, 9999)}"
    found = sg.client_view_findings(f"ref {bad_area}")
    assert "ssn" not in found, found


def test_unreadable_inputs_fail_closed():
    assert sg.client_view_findings(7) == ["type"]
    assert sg.client_view_findings("x" * (sg.MAX_INPUT + 1)) == ["cap"]
    assert sg.client_view_url("https://site.test/" + "a" * sg.URL_MAX_INPUT) == (None, sg.CREDENTIAL)
    assert sg.client_view_url("https://site.test/p?" + "&".join(["a=1"] * (sg.CV_URL_PARTS_MAX + 1)))[1] == sg.CREDENTIAL
    assert sg.client_view_url(None) == (None, None)


# ------------------------------------------------------------------------------------------------ timing

def _fill(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


ADVERSARIAL = {
    "slug_accent": lambda n: _fill("guide-thequickbrownfoxjumpsov\u00e9 ", n),
    "digits_spaced": lambda n: _fill("4 1 2 3 ", n), "digit_dashes": lambda n: _fill("123-45-678-", n),
    "digit_parens": lambda n: _fill("(201) 3(", n), "plus_digits": lambda n: _fill("+1 2 3 4 5 6 7 ", n),
    "label_split": lambda n: _fill("p a s s w o r ", n), "label_seps": lambda n: _fill("pass_._", n),
    "at_dot_tail": lambda n: _fill("x (at) " + "a." * 40 + " ", n), "scheme_run": lambda n: _fill("a://b:", n),
    "mno_dots": lambda n: _fill("M" + "a1B." * 20 + " ", n),
    "distinct_accent": lambda n: " ".join(f"gu\u00e9de-{i}-w\u00f6rd-{i * 7}" for i in range(n // 20))[:n],
}


def _timed(fn, arg) -> float:
    best = None
    for _ in range(2):
        t = time.perf_counter()
        fn(arg)
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_client_view_findings_is_linear_on_adversarial_inputs(name):
    gen = ADVERSARIAL[name]
    t16 = _timed(sg.client_view_findings, gen(16 * 1024))
    t64 = _timed(sg.client_view_findings, gen(64 * 1024))
    assert t64 < 1.0                                                  # 200 ms measured bar; 1 s here for slow CI
    assert t64 < 8 * t16 + 0.05, (t16, t64)                           # linear: 4x the input, about 4x the time
