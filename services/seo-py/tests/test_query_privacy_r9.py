"""Review round 9 (search_performance 2026-11-08.9): combining marks and format characters, split hex / base64,
cards split by filler letters, IPv6 / hex IPv4 / script schemes / spaced and "dot" hosts, ideographic full stops.
Every attack from the round is a permanent DROP test; secret and card shapes are built from parts."""

from __future__ import annotations

import json

import pytest

from agents import priya
from agents import search_performance as sp

J = "".join
DIAER, ACUTE, KEYCAP, UNDER = "̈", "́", "⃣", "̲"
CARD = J(("4539", "1488", "0343", "6467"))                        # Luhn-valid test number


def marked(text: str, mark: str, sep: str = " ") -> str:
    return sep.join(c + mark for c in text)


ROUND9_DROP = [
    # 1. combining marks, keycaps, underlines, format characters
    marked(CARD, DIAER), marked(CARD, KEYCAP), marked(CARD, UNDER, ""), marked(CARD, KEYCAP, ""),
    "555" + ACUTE + " 010" + ACUTE + " 0199", "1" + ACUTE + " 23-45-67" + ACUTE + " 89",
    marked(J(("AKIA", "QWERTYUI", "OP123456")), ACUTE), J(("AKIA", "QWERTYUI", "OP123456")).replace("I", "I‍"),
    J(("4539", "️", "1488", "⁠", "0343", "­", "6467")),
    # 2. split hex, split base64 text, filler-letter cards
    " ".join(("9f86d081", "884c7d65", "9a2feaa0", "c55ad015")), " ".join(("amFuZS5kb2", "VAZXhhbXBs", "ZS5jb20=")),
    " x ".join(("4539", "1488", "0343", "6467")), "x".join(("4539", "1488", "0343", "6467")),
    # 3. URL-like: IPv6, ideographic / fullwidth dots, spaced and "dot" hosts, script schemes, hex / decimal IPv4
    "2001:db8::1", "fe80::1ff:fe23:4567:890a", "::1", "::ffff:c000:280", "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
    "example。com", "example．com/x", "example｡com", "example . com", "example dot com",
    "example (dot) com", "example [.] com", "javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
    "vbscript:msgbox", "0xc0a80101", "3232235777",
]
ROUND9_KEEP = ["the dot com bubble", "dot com boom", "café prices", "東京 ホテル 安い", "10:30 am appointment",
               "3 bed 2 bath 1500 sq ft", "McDonalds iPhone 15 Pro Max 256GB", "data science course",
               "deadbeef cafe bad face", "file a claim", "a/b testing", "24/7 plumber", "50% off rug cleaning"]


@pytest.mark.parametrize("q", ROUND9_DROP)
def test_round9_attacks_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", repr(q)


@pytest.mark.parametrize("q", ROUND9_KEEP)
def test_round9_ordinary_queries_are_kept(q):
    assert sp.classify_query(q, ["acme"]) != "pii_suspect", q


def test_the_card_is_luhn_valid_and_filler_letters_do_not_hide_it():
    assert sp._luhn(CARD) and sp.card_like(" x ".join(("4539", "1488", "0343", "6467")))


def test_strip_marks_is_the_detection_form_and_normalize_strips_stray_marks_only():
    assert sp.strip_marks("A" + ACUTE + "K" + ACUTE) == "AK"
    assert priya.normalize("555" + ACUTE + " rug") == "555 rug"                 # stray mark on ASCII: removed
    assert priya.normalize("4" + KEYCAP + " 1" + UNDER) == "4 1"
    assert priya.normalize("Café") == "café"                                   # NFKC-composed: kept
    assert priya.normalize("हिंदी") == "हिंदी"                                  # script marks: kept


def test_no_round9_attack_ever_reaches_a_ledger():
    rows = [(q, 0, 25, 0.0, 5.0) for q in ROUND9_DROP] + [("rug cleaner", 1, 30, 0.03, 4.0)]
    led = priya.build("acme", "seo-kwl-" + "0" * 40, "2026-11-08T12:00:00+00:00", ["acme"],
                      priya.SourceResult("search_console", "OK", "OK", rows,
                                         {"window": {"start": "2026-10-09", "end": "2026-11-05"},
                                          "truncated": False, "unattributed": {"clicks": 0, "impressions": 0}}),
                      priya.SourceResult("bing_webmaster", "NOT_REQUESTED", "NOT_REQUESTED"))
    assert [r["query"]["text"] for r in led["rows"] if r["origin"] == "observed"] == ["rug cleaner"]
    blob = json.dumps(led, ensure_ascii=False)
    for frag in ("4539", "0199", "9f86d081", "amFuZS5", "2001:db8", "fe80", "0xc0a8", "3232235777"):
        assert frag not in blob, frag
