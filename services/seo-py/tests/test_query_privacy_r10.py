"""Review round 10 (search_performance 2026-11-08.10): one shared character normalisation for the classifier and
Priya's stored key, the full word rules on secret-chunked queries, URL-safe base64 with a tell, cards across short
filler tokens, spelled-out digits, and the table-driven attack corpus (tests/attack_corpus.py)."""

from __future__ import annotations

import base64
import json

import attack_corpus as ac
import pytest
import test_query_privacy_r8 as r8
import test_query_privacy_r9 as r9

from agents import priya
from agents import search_performance as sp

J = "".join
CARD = J(("4111", "1111", "1111", "1111"))                         # the public Visa test number
GROUPS = ("4111", "1111", "1111", "1111")


def _urlsafe_payload() -> str:
    """A URL-safe base64 string that really uses '-' or '_' (searched, not hand-written)."""
    for n in range(200):
        raw = J(("jane.doe+", str(n), "?>@example.com")).encode()
        enc = base64.urlsafe_b64encode(raw).decode()
        if "-" in enc or "_" in enc:
            return enc
    raise AssertionError("no URL-safe payload found")


ROUND10_DROP = [
    # 1. the full word rules on the separator-stripped text of a secret-chunked query
    " ".join(("x9k2m4p7", "q1w3e5r8", "t6y0z1a2")), " ".join(("a1b2c3", "d4e5f6", "g7h8i9")),
    " ".join(("k7md3ng2", "bpx9fic4", "q2w8e7r1")),
    # 2. URL-safe base64, split or not; standard base64 still
    _urlsafe_payload(), " ".join(_urlsafe_payload()[i:i + 8] for i in range(0, len(_urlsafe_payload()), 8)),
    # 3. control / private-use / space variants between card groups
    *[sep.join(GROUPS) for sep in ("\x00", "\x01", "\x1f", "\x7f", "", " ", " ", " ",
                                   " ", "　", "\t")],
    # 4. cards across filler tokens of up to three characters
    "-ab-".join(GROUPS), " xy ".join(GROUPS), "/q/".join(GROUPS), ".ab.".join(GROUPS),
    # spelled-out digits
    " ".join(ac._WORDS[int(c)] for c in CARD), "-".join(ac._WORDS[int(c)] for c in J(("555", "010", "0199"))),
]

# 5. mixed-case natural English / Spanish queries made of base64 letters only: never base64_text
MIXED_CASE_KEEP = [
    "PlayStation Plus Games", "iPhone Pro Max Price", "YouTube TV Channels List", "LinkedIn Learning Courses",
    "McDonalds Breakfast Hours", "PowerPoint Templates Free", "JavaScript Tutorial For Beginners",
    "WordPress Hosting Comparison", "QuickBooks Online Login Help", "GoPro Hero Accessories", "DoorDash Driver Pay",
    "FedEx Tracking Number Lookup", "HubSpot CRM Pricing", "TikTok Shop Seller", "OpenTable Reservations Tonight",
    "AirPods Pro Battery Life", "MacBook Air Charger", "SharePoint Online Training", "PayPal Business Account",
    "Mejor Restaurante En Madrid", "Como Limpiar Una Alfombra", "Precio Del Seguro De Auto",
    "Clases De Ingles Online Gratis", "Tienda De Muebles Cerca", "Abogado De Inmigracion Gratis",
    "Plomero Urgente Las Palmas", "Recetas Faciles Para Cenar", "Vuelos Baratos A Cancun", "Hoteles En Barcelona",
    "Peluqueria Para Hombres", "Taller Mecanico Cerca De Mi", "Dentista Infantil Madrid", "BlackFriday TV Deals",
]


@pytest.mark.parametrize("q", ROUND10_DROP)
def test_round10_attacks_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", repr(q)


@pytest.mark.parametrize("q", MIXED_CASE_KEEP)
def test_mixed_case_natural_language_is_not_base64(q):
    assert not sp.base64_text(sp.decode_query(q)), q
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


def test_base64_decodes_both_alphabets_and_needs_a_tell_or_a_sensitive_decode():
    std = base64.b64encode(b"jane.doe@example.com").decode()         # padded: a tell
    assert sp.base64_text(std) and sp.base64_text(_urlsafe_payload())
    plain = base64.b64encode(b"just some words here").decode().rstrip("=")   # readable, harmless, no tell
    assert len(plain) % 4 != 0 or not sp.base64_text(plain) or "+" in plain or "/" in plain


@pytest.mark.parametrize("base,obf,text", list(ac.cases()), ids=lambda v: v if isinstance(v, str) and
                         len(v) < 40 and v.isascii() and v.isprintable() else None)
def test_attack_corpus_every_case_drops(base, obf, text):
    assert sp.classify_query(text, ["acme"]) == "pii_suspect", (base, obf, repr(text))


@pytest.mark.parametrize("base,obf,text", list(ac.gaps()))
def test_attack_corpus_known_gaps_are_the_disclosed_ones(base, obf, text):
    assert sp.classify_query(text, ["acme"]) != "pii_suspect", f"{base}/{obf} is now caught: update KNOWN_GAPS"


def _all_drop_cases():
    yield from ROUND10_DROP
    yield from r9.ROUND9_DROP
    yield from r8.ROUND8_ATTACKS
    yield from r8.ROUND8_SMUGGLES
    yield from (t for _b, _n, t in ac.cases())


def test_one_shared_normalisation_no_drop_case_ever_becomes_a_ledger_key():
    cases = list(_all_drop_cases())
    for q in cases:                                              # the stored key form is itself dropped
        assert sp.classify_query(priya.normalize(q), ["acme"]) == "pii_suspect", repr(q)
    rows = [(q, 0, 25, 0.0, 5.0) for q in cases] + [("rug cleaner", 1, 30, 0.03, 4.0)]
    led = priya.build("acme", "seo-kwl-" + "0" * 40, "2026-11-08T12:00:00+00:00", ["acme"],
                      priya.SourceResult("search_console", "OK", "OK", rows,
                                         {"window": {"start": "2026-10-09", "end": "2026-11-05"},
                                          "truncated": False, "unattributed": {"clicks": 0, "impressions": 0}}),
                      priya.SourceResult("bing_webmaster", "NOT_REQUESTED", "NOT_REQUESTED"))
    assert [r["query"]["text"] for r in led["rows"] if r["origin"] == "observed"] == ["rug cleaner"]
    assert CARD not in json.dumps(led) and "4111 1111" not in json.dumps(led)


def test_normalize_and_the_detection_form_treat_controls_and_spaces_alike():
    q = "\x00".join(GROUPS)
    assert priya.normalize(q) == "4111 1111 1111 1111" and sp.decode_query(q) == "4111 1111 1111 1111"
    assert priya.normalize("rug cleaner nearme") == "rug cleaner near me"
    assert priya.normalize("Café") == "café" and priya.normalize("हिंदी") == "हिंदी"
