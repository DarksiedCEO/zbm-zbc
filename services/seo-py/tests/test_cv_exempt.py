"""core-spec/2 CV-5 (span display + codes), CV-6 (closed exempt list), CV-7 (URL / e-mail handoff). Card C2-8.

Every positive is a token that WOULD be valueish by CV-3 (>= 8 code points, >= 2 classes), so only CV-6 keeps it shown;
every negative is a near miss of the same shape and is masked. Synthetic values only (seeded at run time)."""
from __future__ import annotations

import hashlib
import random
import string

import pytest

from agents import callum, cv_shape
from agents import secret_guard as sg
from primitives import codes
import vault

# (shape, positive core, negative near miss) - CV-6, one row per entry of the closed list
SHAPES = [
    ("iso_date", "2026-11-08", "2026-11-0a"),
    ("iso_date", "2026-11-08T10:00:00Z", "2026-11-08T10:00:00Q"),
    ("dmy_date", "11/08/2026", "11/08/20261"),
    ("dmy_date", "8.11.2026", "8.11.2026a"),
    ("ymd_slash", "2026/11/08", "2026/11/8a"),
    ("time", "10:30:00pm", "10:30:00xm"),
    ("amount", "$1,299.99", "$1,299.999"),
    ("amount", "EUR1.234,56", "ABC1.234,56"),
    ("amount", "12,345.67usd", "12,345.67usx"),
    ("percent", "1234.56%", "1234.5%%"),
    ("version", "v1.2.3.4", "v1.2.3.4.5"),
    ("ordinal", "1000000th", "1000000xx"),
    ("range", "2024-2025", "2024-2025-2026"),
    ("dimension", "1920x1080px", "1920x1080pxxx"),
    ("dimension", "30×40×50cm", "30x40x50x60"),
    ("unit", "500000mah", "500000mahx"),
    ("unit", "1000.5ghz", "1000.5ghzz"),
    ("tyre", "205/55zr16", "205/55zq16"),
    ("isbn_ean", "978-3-16-148410-0", "978-3-16-148410-1"),
    ("isbn_ean", "0-306-40615-2", "0-306-40615-3"),
    ("isbn_ean", "080442957X", "080442958X"),
]


@pytest.mark.parametrize("shape,good,_bad", SHAPES)
def test_each_exempt_shape_is_shown(shape, good, _bad):
    assert cv_shape.exempt_name(good) == shape
    assert cv_shape.value_spans(f"shipped {good} today") == []
    assert "value_shape" not in sg.client_view_findings(f"shipped {good} today")


@pytest.mark.parametrize("shape,_good,bad", SHAPES)
def test_each_near_miss_is_not_exempt_and_is_masked(shape, _good, bad):
    assert not cv_shape.exempt_shape(bad), shape
    text = f"shipped {bad} today"
    spans = cv_shape.value_spans(text)
    assert spans and text[spans[0][0]:spans[0][1]] == bad, shape
    assert "value_shape" in sg.client_view_findings(text)


def test_exempt_is_case_folded_and_on_the_core_only():
    for c in ("2026-11-08t10:00:00z", "eur1.234,56", "V1.2.3.4", "1920X1080PX", "205/55ZR16", "10:30PM"):
        assert cv_shape.exempt_shape(c), c
    assert cv_shape.value_spans('("2026-11-08T10:00:00Z"),') == []        # wrap characters are not the core
    assert not cv_shape.exempt_shape("on 2026-11-08")                      # the WHOLE core must be the shape


def test_ean13_and_isbn_checksums():
    assert cv_shape._isbn_ean("4006381333931") and not cv_shape._isbn_ean("4006381333932")
    assert cv_shape._isbn_ean("9783161484100") and not cv_shape._isbn_ean("9783161484101")
    assert cv_shape._isbn_ean("0306406152") and not cv_shape._isbn_ean("0306406153")
    assert not cv_shape._isbn_ean("978-3-16--148410-0") and not cv_shape._isbn_ean("-978316148410-0")


def _uuid(r):
    h = "".join(r.choice("0123456789abcdef") for _ in range(32))
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:]}"


@pytest.mark.parametrize("seed", [20261114, 51, 7])
def test_not_exempt_ids_are_masked(seed):
    r = random.Random(seed)
    up = string.ascii_uppercase
    sku = "SKU-" + "".join(r.choice(up) for _ in range(2)) + str(r.randint(10, 99)) + "-" + str(r.randint(10, 99))
    order = f"{r.randint(100, 999)}-{r.randint(10**6, 10**7 - 1)}-{r.randint(10**6, 10**7 - 1)}"
    values = {
        "uuid": _uuid(r), "uuid_upper": _uuid(r).upper(),
        "sha1": hashlib.sha1(str(r.random()).encode()).hexdigest(),
        "sha256": hashlib.sha256(str(r.random()).encode()).hexdigest(),
        "sku": sku, "model": "WH-" + str(r.randint(1000, 9999)) + "XM" + str(r.randint(2, 9)),
        "order": order, "coupon": "SAVE" + str(r.randint(10, 99)) + "NOW" + r.choice(up),
    }
    for name, val in values.items():
        assert not cv_shape.exempt_shape(val), name
        text = f"ref {val} ok"
        assert cv_shape.value_spans(text), name
        assert sg.client_view_findings(text) and sg.client_view_kind(sg.client_view_findings(text)) == sg.CREDENTIAL
    for name in ("UA1234", "BA2490"):                                     # flight numbers: not exempt (CV-6)
        assert not cv_shape.exempt_shape(name)


def _coder(name):
    return vault.Coder(vault.ephemeral_vault_key(), name)


def test_display_renders_each_span_and_lists_codes():
    r = random.Random(20261114)
    a, b = _uuid(r), _uuid(r)
    text = f"first {a} then {b} done"
    with codes.coding(_coder("tenant-a")):
        d = callum._display("answer", text)
    assert d["alert"] is True and d["check"] == "value_shape" and d["reason"] == callum.MASK_REASON["credential"]
    m = callum.MASK["credential"]
    assert d["value"] == f"first {m} then {m} done"
    assert a not in d["value"] and b not in d["value"]
    assert [(s["start"], s["end"]) for s in d["spans"]] == [(text.index(a), text.index(a) + len(a)),
                                                            (text.index(b), text.index(b) + len(b))]
    for s in d["spans"]:
        assert codes.CODE_RX.fullmatch(s["code"]) and s["code"].startswith("{c:text:")
        assert s["code"] not in d["value"]                                 # the text never carries the code


def test_span_codes_equal_for_equal_text_and_differ_across_tenants():
    v = _uuid(random.Random(51))
    ta, tb = _coder("tenant-a"), _coder("tenant-b")
    with codes.coding(ta):
        one = cv_shape.display(f"x {v} y {v}", "M")[1]
        again = cv_shape.display(f"other {v}", "M")[1]
    with codes.coding(tb):
        other = cv_shape.display(f"x {v} y {v}", "M")[1]
    assert one[0]["code"] == one[1]["code"] == again[0]["code"]
    assert other[0]["code"] != one[0]["code"]
    assert cv_shape.display("plain words only", "M") == ("plain words only", [])


URLS = ["https://x.com/acme/status/1852960912345678901", "https://www.aliexpress.com/item/1005006123456789.html",
        "https://github.com/acme/site/commit/8f14e45fceea167a5a36dedd4bea2543c1b2d3e4",  # v1 URLS_KEPT
        "https://cdn.jsdelivr.net/npm/acme-widget@1.2.3/dist/acme.min.js", "www.etsy.com/listing/1523456789/rug"]


@pytest.mark.parametrize("url", URLS)
def test_a_url_token_is_rendered_by_the_url_lane_and_not_double_masked(url):
    full = url if "://" in url else "https://" + url
    assert sg.client_view_url(full) == (full, None)                        # the URL lane itself: no VS inside it
    text = f"see ({url}), then 2026-11-08"
    assert cv_shape.value_spans(text) == []
    assert sg.client_view_findings(text) == []
    assert callum._display("answer", text) == {"value": text, "alert": False, "reason": None}


def test_a_url_token_with_a_value_is_judged_by_the_url_lane():
    r = random.Random(51030)
    key = "AK" + "IA" + "".join(r.choice(string.ascii_uppercase + string.digits) for _ in range(16))
    text = f"see https://site.test/u/{key}/page now"
    assert cv_shape.value_spans(text) == []                                # handed off (CV-7), not VS
    f = sg.client_view_findings(text)
    assert "url_credential" in f and sg.client_view_kind(f) == sg.CREDENTIAL


def test_an_email_token_is_handed_to_k5():
    r = random.Random(7)
    user = "".join(r.choice(string.ascii_lowercase) for _ in range(6)) + str(r.randint(10, 99))
    text = f"write to {user}@example.test today"
    assert cv_shape.value_spans(text) == []
    assert "email" in sg.client_view_findings(text) and "value_shape" not in sg.client_view_findings(text)
