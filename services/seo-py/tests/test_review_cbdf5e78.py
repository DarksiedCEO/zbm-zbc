"""The independent scoped review of commit cbdf5e78 (the fix round for the review of cd0a6c7d), closed.

MUST 1  Greek: ordinary three-word text, "code" + "to" + any word in bas- ("code to database"), read as the Greek
        password label: the strict lane flagged it, a cited URL holding it was not fetched, a host built from it
        was refused, the client view masked some. The letters-only view the key was matched on dropped the spaces,
        so the key was assembled from the letters of several words.
MUST 2  Thai: "code" next to an ordinary word whose letters equal the label's second half once the vowel and tone
        marks are ignored ("quilt code", glued as Thai is written) read as the label: the key was read through the
        inspection view, which drops those marks.
S-1     glued Hindi joins (the label inside a longer written word), S-2 Arabic phrases masked in the client view
        through the hamza / madda fold, S-3 the Hindi entry spelled one letter at a time, S-4 the ADR, N-1 two
        untested arms of the import check.

The cause, fixed (ADR 0051 decision 73): a script label in a script that writes words apart is a SEQUENCE OF WHOLE
WORDS, matched by ONE pattern both lanes read (``secret_guard._SCRIPT_SEQ`` on the word view): it starts at a word
start, ends at a word end, each label word is one whole word of the text, two label words stand apart by a space,
"_" or "-" (or nothing, for the labels declared so), a stem takes only its declared endings, a proclitic only its
declared letters. The Hindi and Thai entries, spelled with non-spacing marks, are read on the MARKED views (the
marks kept, in the entry and in the text). A label spelled one unit at a time with one separator is read again.
The measurement of this round also found the second lookalike pass of cd0a6c7d reading the accented letters of
ordinary Greek words as Latin letters across words ("apikey" out of three Greek words): it now reads only a lone
letter of another script inside a word.

THE BAR of this round: against ffd28ca8, nothing ordinary is newly flagged, masked, withheld or refused, in single
words and in two- and three-word joins, spaced, hyphenated and glued, in 18 languages, but the declared residuals.
The search itself (every candidate join of 20,000 words per language) is the builder's measurement, recorded in the
ADR; this file pins its findings and the classes it closed, with the words embedded as escapes (the tests import
no corpus package).

Every credential-shaped value is generated at run time; no assertion message prints a label or a value (indexes and
counts only). Non-ASCII text is written as escapes.
"""

from __future__ import annotations

import collections
import json
import random
import re
import string
import unicodedata
from urllib.parse import quote

import pytest

import test_review_2b26d980 as prev
import test_review_cd0a6c7d as cd0
import test_vault_wiring as vw
from agents import callum
from agents import secret_guard as sg
from helpers import rid
from ports import ProviderAnswer

SEED = 20261114
LOWER = string.ascii_lowercase
THAI = "\u0e23\u0e2b\u0e31\u0e2a\u0e1c\u0e48\u0e32\u0e19"
HINDI = "\u092a\u093e\u0938\u0935\u0930\u094d\u0921"
JAPANESE_LOANWORD = "\u30d0\u30ba\u30ef\u30fc\u30c9"
GREEK_CODE = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2"            # "code", the first word of the Greek label
GREEK_TO = "\u03c0\u03c1\u03bf\u03c2"                              # "to", "towards"
GREEK_THE = "\u03bf"
GREEK_LABEL = GREEK_CODE + " \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2"
RUSSIAN = "\u043f\u0430\u0440\u043e\u043b\u044c"
HEBREW = "".join(chr(o) for o in (0x5e1, 0x5d9, 0x5e1, 0x5de, 0x5d4))
KOREAN = "".join(chr(o) for o in (0xbe44, 0xbc00, 0xbc88, 0xd638))
SEPARATORS = (": ", "=", " is ", " IS ", " Was ", " -> ", " | ")


@pytest.fixture(autouse=True)
def _fresh_guard_caches():
    prev.clear_guard_caches()
    yield
    prev.clear_guard_caches()


def rs(r, chars: str, n: int) -> str:
    return "".join(r.choice(chars) for _ in range(n))


def value(r) -> str:
    return rs(r, LOWER, 5) + str(r.randint(10, 99)) + r.choice("!#%")


def masked(text: str) -> bool:
    return "label_value" in sg.client_view_findings(text)


def flagged(text: str) -> bool:
    return sg.check(text) == "a_label"


def host_accepted(word: str) -> bool:
    """A Unicode-form host built from ``word`` (spaces as hyphens) taken by the check and the checked-host lanes."""
    host = word.replace(" ", "-").lower() + ".site.test"
    return sg.check(host) is None and callum.checked_host(host) == {"host": host, "host_withheld": None}


def ordinary(text: str, r) -> list:
    """Every way ``text`` could be held against its owner: flagged, masked before a separator, a URL holding it in
    its raw path or its query not fetched, a host built from it refused. [] when none. (Its PERCENT-ENCODED path is
    read as a random token by check (d) when the word is long: pre-existing, the same at ffd28ca8, pinned by
    ``test_review_cd0a6c7d.py``; here only its reason is asserted, never a label.)"""
    slug = text.replace(" ", "-")
    out = []
    if sg.check(text) is not None:
        out.append("flagged")
    if sg.client_view_findings(f"{text}: {value(r)}") or sg.client_view_findings(text):
        out.append("masked")
    for u in (f"https://site.test/blog/{slug}/", f"https://site.test/s?q={text}&page=2"):
        if sg.url_has_secret(u):
            out.append("url")
    if sg.url_has_secret("https://site.test/blog/" + quote(text) + "/") and sg.check(quote(text)) != "d_token":
        out.append("url_encoded")
    if not host_accepted(text):
        out.append("host")
    return out


# Greek words in bas- ("base", "basic", "on the basis of"...), the 50 most frequent (wordfreq 3.1.1)
GREEK_BAS_WORDS = (
    "\u03b2\u03ac\u03c3\u03b7", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ac",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ac\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03ba\u03ac",
    "\u03b2\u03b1\u03c3\u03af\u03bb\u03b9\u03c3\u03c3\u03b1", "\u03b2\u03b1\u03c3\u03af\u03bb\u03b7\u03c3",
    "\u03b2\u03b1\u03c3\u03b9\u03ba\u03cc", "\u03b2\u03ac\u03c3\u03b5\u03b9",
    "\u03b2\u03b1\u03c3\u03af\u03b6\u03b5\u03c4\u03b1\u03b9", "\u03b2\u03b1\u03c3\u03af\u03bb\u03b7",
    "\u03b2\u03b1\u03c3\u03b9\u03ba\u03ae", "\u03b2\u03b1\u03c3\u03af\u03bb\u03b5\u03b9\u03bf",
    "\u03b2\u03ac\u03c3\u03b5\u03b9\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03ba\u03ad\u03c3",
    "\u03b2\u03b1\u03c3\u03b9\u03ba\u03cc\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b5\u03af\u03bf\u03c5",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ba\u03ae", "\u03b2\u03ac\u03c3\u03b7\u03c3",
    "\u03b2\u03b1\u03c3\u03af\u03b6\u03bf\u03bd\u03c4\u03b1\u03b9",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ba\u03cc", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b5\u03af\u03b1",
    "\u03b2\u03b1\u03c3\u03af\u03bb\u03b5\u03b9\u03bf\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03ba\u03ce\u03bd",
    "\u03b2\u03b1\u03c3\u03b9\u03c3\u03bc\u03ad\u03bd\u03b7", "\u03b2\u03ac\u03c3\u03b5\u03c9\u03bd",
    "\u03b2\u03b1\u03c3\u03af\u03bb\u03b9\u03c3\u03c3\u03b1\u03c3",
    "\u03b2\u03b1\u03c3\u03b1\u03bd\u03b9\u03c3\u03c4\u03ae\u03c1\u03b9\u03b1",
    "\u03b2\u03b1\u03c3\u03b9\u03ba\u03bf\u03af", "\u03b2\u03b1\u03c3\u03b9\u03c3\u03bc\u03ad\u03bd\u03bf",
    "\u03b2\u03b1\u03c3\u03b9\u03ba\u03bf\u03cd\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ba\u03ae\u03c3",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ac\u03b4\u03b5\u03c3", "\u03b2\u03b1\u03c3", "\u03b2\u03b1\u03c3\u03b7",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ba\u03bf\u03cd",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b5\u03af\u03b1\u03c3", "\u03b2\u03ac\u03c3\u03b1\u03bd\u03b1",
    "\u03b2\u03ac\u03c3\u03c9", "\u03b2\u03b1\u03c3\u03b9\u03ba\u03bf\u03cd",
    "\u03b2\u03b1\u03c3\u03af\u03c3\u03c4\u03b7\u03ba\u03b5",
    "\u03b2\u03b1\u03c3\u03b1\u03bd\u03af\u03b6\u03b5\u03b9", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03ad\u03b1",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03ad\u03c9\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b9\u03ba\u03cc\u03c3",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b5\u03b9\u03ac\u03b4\u03b7\u03c3",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b5\u03af\u03c3", "\u03b2\u03b1\u03c3\u03b9\u03bb\u03b5\u03cd\u03c3",
    "\u03b2\u03b1\u03c3\u03af\u03bb\u03b5\u03b9\u03b1", "\u03b2\u03b1\u03c3\u03af\u03bb\u03b5\u03b9\u03cc",
    "\u03b2\u03b1\u03c3\u03b9\u03bb\u03cc\u03c0\u03bf\u03c5\u03bb\u03bf\u03c3",
)

# Thai word pairs whose letters, marks ignored, hold the label across the boundary (pythainlp TNC, top 20,000)
THAI_PAIRS = (
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19", "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
)

# 40 of the 7,572 glued Thai triples cbdf5e78 flagged that do not hold the label as written
THAI_TRIPLES_GLUED = (
   
    "\u0e1e\u0e23\u0e30\u0e23\u0e32\u0e0a\u0e14\u0e33\u0e23\u0e34+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e35\u0e25",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e31\u0e01\u0e1a\u0e38\u0e0d",
    "\u0e40\u0e01\u0e29\u0e15\u0e23\u0e28\u0e32\u0e2a\u0e15\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e08\u0e23\u0e34\u0e22\u0e27\u0e31\u0e15\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e0b\u0e39\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e40\u0e14\u0e35\u0e22\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e23\u0e31\u0e15\u0e19\u0e42\u0e01\u0e2a\u0e34\u0e19\u0e17\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e16\u0e19\u0e2d\u0e21\u0e2d\u0e32\u0e2b\u0e32\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e21\u0e34\u0e40\u0e15\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e28\u0e31\u0e15\u0e23\u0e39+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e08\u0e39\u0e40\u0e19\u0e35\u0e22\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e43\u0e04\u0e23\u0e48+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e42\u0e04\u0e25\u0e31\u0e2a",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e04\u0e21",
    "\u0e04\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e15\u0e23\u0e31\u0e2a\u0e23\u0e39\u0e49+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e36\u0e01",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e34\u0e01\u0e02\u0e4c",
    "\u0e0b\u0e34\u0e25\u0e40\u0e27\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e49\u0e33\u0e15\u0e32",
    "\u0e15\u0e32\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e23\u0e35\u0e44\u0e17\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e34\u0e21\u0e19\u0e15\u0e4c",
    "\u0e0a\u0e19\u0e34\u0e19\u0e17\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e40\u0e17\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e27\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e31\u0e49\u0e19\u0e41\u0e25",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e27\u0e22\u0e2d\u0e23\u0e4c\u0e01",
    "\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e2d\u0e20\u0e34\u0e19\u0e34\u0e2b\u0e32\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e28\u0e31\u0e15\u0e23\u0e39+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e0a\u0e32\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e27\u0e34\u0e28\u0e27\u0e01\u0e23\u0e23\u0e21\u0e28\u0e32\u0e2a\u0e15\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e28\u0e34\u0e23\u0e34+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e31\u0e01\u0e27\u0e34\u0e0a\u0e32\u0e01\u0e32\u0e23",
    "\u0e08\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e23\u0e19\u0e31\u0e22",
    "\u0e08\u0e1b\u0e23.+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e25\u0e2d\u0e15\u0e40\u0e15\u0e2d\u0e23\u0e35\u0e48+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
)

# 40 of the 10,724 spaced Thai triples cbdf5e78 flagged (the letters view crossed the spaces)
THAI_TRIPLES_SPACED = (
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e48\u0e32\u0e19\u0e19\u0e49\u0e33",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e34\u0e0a\u0e17\u0e4c",
    "\u0e28\u0e34\u0e25\u0e1b\u0e28\u0e32\u0e2a\u0e15\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e23\u0e34\u0e28",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e49\u0e33\u0e17\u0e48\u0e32",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e32\u0e16",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e2a\u0e21\u0e4c",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e19\u0e17\u0e4c",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e32\u0e07\u0e41\u0e2d\u0e48\u0e19",
    "\u0e17\u0e23\u0e39+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e0b\u0e4c",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e49\u0e33\u0e2b\u0e21\u0e32\u0e01",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e40\u0e17\u0e28\u0e28\u0e32\u0e2a\u0e15\u0e23\u0e4c",
    "\u0e42\u0e1b\u0e23\u0e40\u0e1f\u0e2a\u0e40\u0e0b\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e18\u0e23\u0e23\u0e21\u0e01\u0e32\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e34\u0e49\u0e07",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e49\u0e33\u0e15\u0e32\u0e25\u0e17\u0e23\u0e32\u0e22",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e21\u0e02\u0e49\u0e19",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e32\u0e2a\u0e34\u0e01",
    "\u0e15\u0e31\u0e27\u0e2d\u0e31\u0e01\u0e29\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e40\u0e2a\u0e35\u0e22\u0e23\u0e39\u0e49+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e1a\u0e23\u0e34\u0e01\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e36\u0e01\u0e40\u0e2b\u0e47\u0e19",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e34\u0e27\u0e22\u0e2d\u0e23\u0e4c\u0e04",
    "\u0e2a\u0e22\u0e38\u0e21\u0e1e\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e30",
    "\u0e40\u0e01\u0e29\u0e35\u0e22\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e40\u0e08\u0e23\u0e34\u0e0d\u0e2d\u0e32\u0e2b\u0e32\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e2d\u0e30\u0e44\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e32\u0e19",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e32\u0e19\u0e31\u0e1b\u0e01\u0e32\u0e23",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e48\u0e32\u0e40\u0e2a\u0e35\u0e22\u0e14\u0e32\u0e22",
    "\u0e20\u0e32\u0e1e\u0e22\u0e19\u0e15\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19",
    "\u0e17\u0e23\u0e32\u0e19\u0e0b\u0e34\u0e2a\u0e40\u0e15\u0e2d\u0e23\u0e4c+\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32\u0e19",
    "\u0e1e\u0e40\u0e19\u0e08\u0e23+\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07",
    "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e32\u0e23\u0e2d\u0e14\u0e19\u0e34\u0e04",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e49\u0e32+\u0e19\u0e38\u0e48\u0e21\u0e19\u0e27\u0e25",
    "\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e49\u0e2d\u0e22",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e48\u0e32+\u0e19\u0e32\u0e23\u0e35",
    "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a+\u0e1c\u0e32+\u0e19\u0e35",
)

# the Hindi pairs cbdf5e78 flagged when glued (wordfreq 3.1.1, top 20,000): the label inside a longer written word
HINDI_GLUED_PAIRS = (
    "\u0906\u0938\u092a\u093e\u0938+\u0935\u0930\u094d\u0921", "\u0915\u092a\u093e\u0938+\u0935\u0930\u094d\u0921",
    "\u092a\u093e\u0938+\u0935\u0930\u094d\u0921", "\u092c\u093e\u0908\u092a\u093e\u0938+\u0935\u0930\u094d\u0921",
)

# the Arabic phrases cbdf5e78 masked in the client view and ffd28ca8 did not (wordfreq 3.1.1, top 20,000)
ARABIC_PHRASES = (
    "\u0627\u0644\u0643\u0644\u0645\u0629 \u0622\u0644 \u0633\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629 \u0622\u0644 \u0645\u0631\u0648\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629 \u0623\u0644 \u0633\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629 \u0623\u0644 \u0645\u0631\u0648\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629 \u0625\u0644 \u0633\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629 \u0625\u0644 \u0645\u0631\u0648\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629\u0622\u0644\u0633\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629\u0622\u0644\u0645\u0631\u0648\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629\u0623\u0644\u0633\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629\u0623\u0644\u0645\u0631\u0648\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629\u0625\u0644\u0633\u0631",
    "\u0627\u0644\u0643\u0644\u0645\u0629\u0625\u0644\u0645\u0631\u0648\u0631",
    "\u0628\u0643\u0644\u0645\u0629 \u0622\u0644 \u0633\u0631",
    "\u0628\u0643\u0644\u0645\u0629 \u0622\u0644 \u0645\u0631\u0648\u0631",
    "\u0628\u0643\u0644\u0645\u0629 \u0623\u0644 \u0633\u0631",
    "\u0628\u0643\u0644\u0645\u0629 \u0623\u0644 \u0645\u0631\u0648\u0631",
    "\u0628\u0643\u0644\u0645\u0629 \u0625\u0644 \u0633\u0631",
    "\u0628\u0643\u0644\u0645\u0629 \u0625\u0644 \u0645\u0631\u0648\u0631",
    "\u0628\u0643\u0644\u0645\u0629\u0622\u0644\u0633\u0631",
    "\u0628\u0643\u0644\u0645\u0629\u0622\u0644\u0645\u0631\u0648\u0631",
    "\u0628\u0643\u0644\u0645\u0629\u0623\u0644\u0633\u0631",
    "\u0628\u0643\u0644\u0645\u0629\u0623\u0644\u0645\u0631\u0648\u0631",
    "\u0628\u0643\u0644\u0645\u0629\u0625\u0644\u0633\u0631",
    "\u0628\u0643\u0644\u0645\u0629\u0625\u0644\u0645\u0631\u0648\u0631",
    "\u0643\u0644\u0645\u0629 \u0622\u0644 \u0633\u0631",
    "\u0643\u0644\u0645\u0629 \u0622\u0644 \u0645\u0631\u0648\u0631",
    "\u0643\u0644\u0645\u0629 \u0623\u0644 \u0633\u0631",
    "\u0643\u0644\u0645\u0629 \u0623\u0644 \u0645\u0631\u0648\u0631",
    "\u0643\u0644\u0645\u0629 \u0625\u0644 \u0633\u0631",
    "\u0643\u0644\u0645\u0629 \u0625\u0644 \u0645\u0631\u0648\u0631",
    "\u0643\u0644\u0645\u0629\u0622\u0644\u0633\u0631",
    "\u0643\u0644\u0645\u0629\u0622\u0644\u0645\u0631\u0648\u0631",
    "\u0643\u0644\u0645\u0629\u0623\u0644\u0633\u0631",
    "\u0643\u0644\u0645\u0629\u0623\u0644\u0645\u0631\u0648\u0631",
    "\u0643\u0644\u0645\u0629\u0625\u0644\u0633\u0631",
    "\u0643\u0644\u0645\u0629\u0625\u0644\u0645\u0631\u0648\u0631",
    "\u0644\u0643\u0644\u0645\u0629 \u0622\u0644 \u0633\u0631",
    "\u0644\u0643\u0644\u0645\u0629 \u0622\u0644 \u0645\u0631\u0648\u0631",
    "\u0644\u0643\u0644\u0645\u0629 \u0623\u0644 \u0633\u0631",
    "\u0644\u0643\u0644\u0645\u0629 \u0623\u0644 \u0645\u0631\u0648\u0631",
    "\u0644\u0643\u0644\u0645\u0629 \u0625\u0644 \u0633\u0631",
    "\u0644\u0643\u0644\u0645\u0629 \u0625\u0644 \u0645\u0631\u0648\u0631",
    "\u0644\u0643\u0644\u0645\u0629\u0622\u0644\u0633\u0631",
    "\u0644\u0643\u0644\u0645\u0629\u0622\u0644\u0645\u0631\u0648\u0631",
    "\u0644\u0643\u0644\u0645\u0629\u0623\u0644\u0633\u0631",
    "\u0644\u0643\u0644\u0645\u0629\u0623\u0644\u0645\u0631\u0648\u0631",
    "\u0644\u0643\u0644\u0645\u0629\u0625\u0644\u0633\u0631",
    "\u0644\u0643\u0644\u0645\u0629\u0625\u0644\u0645\u0631\u0648\u0631",
    "\u0648\u0643\u0644\u0645\u0629 \u0622\u0644 \u0633\u0631",
    "\u0648\u0643\u0644\u0645\u0629 \u0622\u0644 \u0645\u0631\u0648\u0631",
    "\u0648\u0643\u0644\u0645\u0629 \u0623\u0644 \u0633\u0631",
    "\u0648\u0643\u0644\u0645\u0629 \u0623\u0644 \u0645\u0631\u0648\u0631",
    "\u0648\u0643\u0644\u0645\u0629 \u0625\u0644 \u0633\u0631",
    "\u0648\u0643\u0644\u0645\u0629 \u0625\u0644 \u0645\u0631\u0648\u0631",
    "\u0648\u0643\u0644\u0645\u0629\u0622\u0644\u0633\u0631",
    "\u0648\u0643\u0644\u0645\u0629\u0622\u0644\u0645\u0631\u0648\u0631",
    "\u0648\u0643\u0644\u0645\u0629\u0623\u0644\u0633\u0631",
    "\u0648\u0643\u0644\u0645\u0629\u0623\u0644\u0645\u0631\u0648\u0631",
    "\u0648\u0643\u0644\u0645\u0629\u0625\u0644\u0633\u0631",
    "\u0648\u0643\u0644\u0645\u0629\u0625\u0644\u0645\u0631\u0648\u0631",
)

# 40 of the 134,379 spaced triples of Greek words (not the label's) cbdf5e78 flagged as a LATIN label (accented letters read as lookalikes)
GREEK_ACCENT_TRIPLES = (
    "\u03bc\u03c0\u03ac\u03c1\u03b9+\u03ba\u03b5+\u03b3\u03b5\u03b9\u03c4\u03bf\u03bd\u03b9\u03ad\u03c3",
    "\u03ac\u03c8\u03bf\u03b3\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03ba\u03c5\u03bf\u03c3",
    "\u03c4\u03c1\u03ac\u03c0\u03b5\u03b6\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03b9\u03bd\u03b5\u03c3",
    "\u03bd\u03bf\u03c3\u03ae\u03bc\u03b1\u03c4\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03b9\u03bd\u03b1\u03bd",
    "\u03c6\u03b1\u03bd\u03ac\u03c1\u03b9+\u03ba\u03b5+\u03b3\u03b1\u03b2\u03c1\u03b9\u03ae\u03bb",
    "\u03bf\u03b9\u03ba\u03bf\u03b3\u03ad\u03bd\u03b5\u03b9\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03b1\u03c4\u03b1\u03c3\u03c4\u03ac\u03c3\u03b5\u03b9\u03c3",
    "\u03b5\u03ba\u03b1\u03c4\u03bf\u03c3\u03c4\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03b3\u03bf\u03bd\u03cc\u03c3",
    "\u03c7\u03ac\u03c1\u03b9+\u03ba.\u03b5+\u03b3\u03ac\u03c4\u03b5\u03c3",
    "\u03c3\u03c9\u03c4\u03ae\u03c1\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03ba\u03bb\u03b7\u03bc\u03b1",
    "\u03b3\u03bd\u03c9\u03c1\u03af\u03c3\u03bc\u03b1\u03c4\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03b9\u03bd\u03b5",
    "\u03bb\u03b5\u03af\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c6\u03b1",
    "\u03c3\u03c5\u03bb\u03bb\u03bf\u03b3\u03b9\u03ba\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03bb\u03ae\u03bc\u03b1\u03c4\u03b1",
    "\u03c7\u03b1\u03c1\u03c4\u03b9\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03b1\u03c4\u03b1\u03c3\u03c4\u03b1\u03b8\u03b5\u03af",
    "\u03c6\u03b1\u03bd\u03ac\u03c1\u03b9+\u03ba\u03b5+\u03b3\u03b5\u03bd\u03b5\u03c4\u03b9\u03ba\u03cc",
    "\u03c7\u03b1\u03c1\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03b1\u03c4\u03b1\u03bb\u03b5\u03af\u03c0\u03bf\u03c5\u03bd",
    "\u03b1\u03bb\u03bb\u03b7\u03bb\u03bf\u03b3\u03c1\u03b1\u03c6\u03af\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03b3\u03c1\u03b1\u03c6\u03b1",
    "\u03b3\u03b5\u03c9\u03b3\u03c1\u03b1\u03c6\u03af\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c6\u03b1",
    "\u03b2\u03bf\u03ae\u03b8\u03b5\u03b9\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03b3\u03c1\u03b1\u03c6\u03ad\u03c3",
    "\u03b9\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03b3\u03c1\u03b1\u03c6\u03b1",
    "\u03bd\u1f70+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03b9\u03bd\u03b5",
    "\u03bc\u03b1\u03c1\u03af+\u03ba00+\u03b5\u03b3\u03ba\u03b5\u03c6\u03ac\u03bb\u03bf\u03c5",
    "\u03c0\u03b1\u03b9\u03b4\u03b5\u03af\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c8\u03b1\u03bd",
    "\u03c3\u03c5\u03c3\u03c4\u03b1\u03c4\u03b9\u03ba\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03c5\u03ba\u03bb\u03bf\u03c0\u03b1\u03af\u03b4\u03b5\u03b9\u03b1",
    "\u03b1\u03bd\u03b1\u03b4\u03c1\u03bf\u03bc\u03b9\u03ba\u03ac+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03ba\u03c1\u03b9\u03c3\u03b7",
    "\u03c8\u03ac\u03c1\u03b9+\u03ba00+\u03b5\u03b3\u03ba\u03b1\u03c4\u03b1\u03bb\u03b5\u03af\u03c0\u03bf\u03c5\u03bd",
    "\u03c0\u03c1\u03bf\u03c4\u03b5\u03c1\u03b1\u03b9\u03cc\u03c4\u03b7\u03c4\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c6\u03b1",
    "\u03c7\u03c1\u03cc\u03bd\u03b9\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c8\u03b5",
    "\u03ba\u03bf\u03bd\u03c4\u03ac+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c8\u03b5",
    "\u03c6\u03b9\u03bd\u03bb\u03b1\u03bd\u03b4\u03af\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c8\u03b1\u03bd",
    "\u03c4\u03b9\u03bc\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ce",
    "\u03cc\u03bd\u03b5\u03b9\u03c1\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03c1\u03b9\u03b8\u03b5\u03af",
    "\u03b4\u03b9\u03b1\u03c3\u03c4\u03ae\u03bc\u03b1\u03c4\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c8\u03b1",
    "\u03c7\u03b1\u03bc\u03c0\u03ac\u03c1\u03b9+\u03ba\u03b5+\u03b3\u03b9\u03ce\u03c1\u03b3\u03b7",
    "\u03c0\u03c1\u03c9\u03c4\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03b9\u03bd\u03b5\u03c3",
    "\u03b1\u03bd\u03b9\u03c8\u03b9\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03b5\u03c6\u03ac\u03bb\u03bf\u03c5",
    "\u03b1\u03b3\u03b1\u03b8\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03c1\u03ae\u03b3\u03bf\u03c1\u03c3\u03b7",
    "\u03b4\u03b9\u03c0\u03bb\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03b3\u03c5\u03ae\u03c3\u03b5\u03b9\u03c3",
    "\u03b4\u03b9\u03ac\u03b2\u03b1\u03c3\u03bc\u03b1+\u03c1\u03b9\u03ba+\u03ad\u03b3\u03c1\u03b1\u03c8\u03b5\u03c3",
    "\u03ba\u03b1\u03bb\u03bb\u03b9\u03c4\u03b5\u03c7\u03bd\u03b9\u03ba\u03ac+\u03c1\u03b9\u03ba+\u03b5\u03b3\u03ba\u03b5\u03c6\u03ac\u03bb\u03bf\u03c5",
    "\u03c0\u03b1\u03bb\u03b9\u03ba\u03ac\u03c1\u03b9+\u03ba.\u03b5+\u03b3\u03b1\u03bc\u03ae\u03c3\u03c9",
)


# ================================================================================================ MUST 1: Greek

def greek_phrases() -> list:
    """"code" + "to" + each word in bas-, and "the code to" + it: 100 three- and four-word phrases."""
    return [f"{GREEK_CODE} {GREEK_TO} {w}" for w in GREEK_BAS_WORDS] + \
        [f"{GREEK_THE} {GREEK_CODE} {GREEK_TO} {w}" for w in GREEK_BAS_WORDS]


def test_greek_code_to_base_is_not_the_label():
    r = random.Random(SEED + 1)
    phrases = greek_phrases()
    assert len(phrases) == 100 and len(set(GREEK_BAS_WORDS)) == 50
    bad = {}
    for p in phrases:
        for form in (p, p.upper(), p.title(), p.replace(" ", "-"), unicodedata.normalize("NFD", p)):
            got = ordinary(form, r)
            if got:
                bad[len(bad)] = got
    assert bad == {}, list(bad.values())[:5]
    # the strict lane's letters-only view does hold the label's letters in a row (what made them read as the label):
    # it is the prefilter now, and the boundary rule says no
    assert all(sg._SCRIPT_RX.search(sg._script_letters(sg.normalise(p))) for p in phrases[:50])
    assert not any(sg._SCRIPT_SEQ.search(sg._script_word_view(sg.normalise(p))) for p in phrases)


def test_mutation_with_the_letters_only_matching_back_code_to_base_reads_as_the_label(monkeypatch):
    """The boundary rule is load-bearing: matched on the letters-only view again (as at cbdf5e78), the phrases are
    flagged and their URLs are not fetched."""
    monkeypatch.setattr(sg, "_SCRIPT_UNSPACED_RX", sg._SCRIPT_RX)
    prev.clear_guard_caches()
    phrases = greek_phrases()
    assert sum(flagged(p) for p in phrases) >= 90
    assert sg.url_has_secret(f"https://site.test/blog/{phrases[0].replace(' ', '-')}/")


def test_the_greek_label_is_read_in_its_forms_and_nothing_glued_to_it():
    r = random.Random(SEED + 2)
    second = ("\u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2", "\u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7",
              "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03c9\u03c2", "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03b9\u03c2",
              "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03c9\u03bd", "\u03c0\u03c1\u03bf\u03c3\u03b2\u03b1\u03c3")
    forms = [f"{GREEK_CODE}{sep}{w}" for w in second for sep in (" ", "_", "-")]
    forms += [f.upper() for f in forms] + [unicodedata.normalize("NFD", f) for f in forms]
    for f in forms:
        for sep in SEPARATORS:
            t = f"{f}{sep}{value(r)}"
            assert flagged(t) and masked(t), (len(f), sep)
        assert flagged(f"note: {f}") and not masked(f"{f} {rs(r, LOWER, 6)}")
    # a value glued after a separator or a change of script is still read; a word glued to the label is not the
    # label. Review 0334eeae MUST A: the label's own two words glued are read again, AS WRITTEN (accents on the
    # letters the entry's written spellings carry them on); "code" + "to" + "basis" glued is not the label
    assert flagged(f"{GREEK_LABEL}:{value(r)}") and masked(f"{GREEK_LABEL}:{value(r)}")
    assert flagged(f"{GREEK_LABEL}{rs(r, LOWER, 3)}{r.randint(10, 99)}")
    glued = [GREEK_LABEL.replace(" ", ""), GREEK_CODE + "\u03c0\u03c1\u03bf\u03c2\u03b2\u03ac\u03c3\u03b7",
             GREEK_LABEL + "\u03c4\u03b1", "\u03c4\u03bf" + GREEK_LABEL.replace(" ", "")]
    # (round 7, decision 75: a label inside a longer written word is read before a value, as ffd28ca8 and cbdf5e78
    # read it: the strict lane with letters after it, both lanes with a word before it; bare, only the label itself)
    assert [flagged(f"{g}: {value(r)}") for g in glued] == [True, False, True, True]
    assert [masked(f"{g}: {value(r)}") for g in glued] == [True, False, False, True]
    assert [flagged(g) for g in glued] == [True, False, False, False]


# ================================================================================================ MUST 2: Thai

def thai_texts() -> list:
    """(text, holds the label as written) for every embedded pair in three forms and every embedded triple."""
    out = []
    for p in THAI_PAIRS:
        a, b = p.split("+")
        out += [(a + b, THAI in a + b), (f"{a} {b}", False), (f"{a}-{b}", False)]
    out += [("".join(t.split("+")), False) for t in THAI_TRIPLES_GLUED]
    out += [(" ".join(t.split("+")), False) for t in THAI_TRIPLES_SPACED]
    return out


def test_thai_joins_that_do_not_hold_the_label_as_written_are_not_the_label():
    r = random.Random(SEED + 3)
    texts = thai_texts()
    assert len(THAI_PAIRS) == 12 and len(texts) == 36 + 80
    plain = [t for t, lit in texts if not lit]
    literal = [t for t, lit in texts if lit]
    assert len(plain) == 110 and len(literal) == 6
    # what made them read as the label: its letters without the marks, in a row
    key = sg._script_letters(sg.normalise(THAI))
    assert all(key in sg._script_letters(sg.normalise(t)) for t in plain)
    bad = [i for i, t in enumerate(plain) if ordinary(t, r)]
    assert bad == []
    # declared residual (a) (ADR 0051 decision 73): a glued join that holds the label AS WRITTEN, its marks on their
    # letters, cannot be told from the label in a script written without spaces (no segmenter is used)
    assert [flagged(t) for t in literal] == [True] * 6


def test_the_thai_label_keeps_its_marks():
    r = random.Random(SEED + 4)
    for form in (THAI, unicodedata.normalize("NFD", THAI), f"**{THAI}**", THAI[:4] + "\u200b" + THAI[4:],
                 quote(THAI)):
        for sep in SEPARATORS:
            t = f"{form}{sep}{value(r)}"
            # (percent-encoded: the encoded view is read as a token first, check (d); the decoded one is the label)
            assert (flagged(t) and masked(t)) if "%" not in form else (sg.check(t) is not None and masked(t)), sep
    # declared residual (b) (decision 73): the label without its marks is another word, as the Japanese label
    # without its voicing marks and the Hindi one without its vowel signs
    bare = "".join(c for c in THAI if unicodedata.category(c) != "Mn")
    assert bare != THAI and not flagged(f"{bare}: {value(r)}") and not masked(f"{bare}: {value(r)}")
    # a mark more or other than the label's is another word
    other = THAI.replace("\u0e48", "\u0e49")
    assert not flagged(f"{other}: {value(r)}") and not masked(f"{other}: {value(r)}")
    # the marked view keeps the marks of the marked scripts only: a combining mark of another block typed into the
    # label (an acute, a grave) is dropped there as on every view, and the label is read
    for form in (THAI[:1] + "\u0301" + THAI[1:], THAI + "\u0301", THAI[:3] + "\u0300" + THAI[3:]):
        t = f"{form}: {value(r)}"
        assert flagged(t) and masked(t), len(form)


def test_mutation_without_the_marked_views_the_thai_joins_read_as_the_label(monkeypatch):
    """The marked view is load-bearing: made like every other view (the marks dropped), the joins are the label."""
    monkeypatch.setattr(sg, "_clean_marked", sg._clean)
    monkeypatch.setattr(sg, "_MARKED_SEQ", re.compile(re.escape(sg._script_letters(sg.normalise(THAI)))))
    prev.clear_guard_caches()
    assert sum(flagged(a + b) for a, b in (p.split("+") for p in THAI_PAIRS)) == 12


# ================================================================================================ S-1: Hindi

def test_hindi_glued_joins_are_not_the_label():
    r = random.Random(SEED + 5)
    joins = [p.split("+") for p in HINDI_GLUED_PAIRS]
    assert len(joins) == 4
    halves = [(a, b) for a, b in joins if a + b == HINDI]
    longer = [(a, b) for a, b in joins if a + b != HINDI]
    assert len(halves) == 1 and len(longer) == 3 and all(HINDI in a + b for a, b in longer)
    # the label inside a longer written word is another word (the boundary rule)
    assert [ordinary(a + b, r) for a, b in longer] == [[]] * 3
    # the label's own two halves glued ARE the label, as written
    (a, b), = halves
    assert flagged(f"{a}{b}: {value(r)}") and masked(f"{a}{b}: {value(r)}")
    for sep in (" ", "-", "_"):
        assert not flagged(f"{a}{sep}{b}") and not masked(f"{a}{sep}{b}: {value(r)}")
    # declared residual (c): ffd28ca8 did not read the label written as two words either


def test_the_hindi_label_is_read_with_its_marks_and_its_endings():
    r = random.Random(SEED + 6)
    forms = [HINDI, unicodedata.normalize("NFD", HINDI), HINDI[:6] + "\u200d" + HINDI[6:], HINDI + "\u094b\u0902",
             HINDI + "\u094d\u0938", f"<b>{HINDI}</b>"]
    for f in forms:
        for sep in SEPARATORS:
            t = f"{f}{sep}{value(r)}"
            assert flagged(t) and masked(t), (len(f), sep)
    # a nasal dot the inspection view would drop, a vowel sign more: another word on the marked view
    for f in (HINDI[:2] + "\u0902" + HINDI[2:], HINDI.replace("\u094d", "")):
        assert not flagged(f"{f}: {value(r)}") and not masked(f"{f}: {value(r)}")


# ================================================================================================ S-2: Arabic

def test_arabic_phrases_are_not_masked():
    r = random.Random(SEED + 7)
    assert len(ARABIC_PHRASES) == 60
    got = [i for i, p in enumerate(ARABIC_PHRASES) if masked(f"{p}: {value(r)}") or masked(f"{p} - {value(r)}")]
    assert got == []
    # nor flagged: the label's words are not whole words of the phrase (the hamza / madda fold is the same in both
    # lanes and the boundary rule says no)
    assert [i for i, p in enumerate(ARABIC_PHRASES) if flagged(p)] == []


def test_the_arabic_and_hebrew_labels_are_read_with_their_proclitics():
    r = random.Random(SEED + 8)
    word, secret = "\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631", "\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0631"
    hebrew = "\u05e1\u05d9\u05e1\u05de\u05d4"
    forms = [word, secret, word.replace(" ", "-"), word.replace(" ", "_"), "\u0628" + word, "\u0648\u0628" + word,
             "\u0644" + secret, hebrew, "\u05d4" + hebrew, "\u05d5\u05d4" + hebrew, "\u05d5\u05e9\u05d4" + hebrew,
             "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631", "\u0631\u0645\u0632\u200c\u0639\u0628\u0648\u0631",
             # review 0334eeae MUST A: the Arabic words glued, as written (with a proclitic too)
             word.replace(" ", ""), secret.replace(" ", ""), "\u0628" + word.replace(" ", ""),
             "\u0648\u0644" + secret.replace(" ", "")]
    for f in forms:
        for sep in SEPARATORS:
            t = f"{f}{sep}{value(r)}"
            assert flagged(t) and masked(t), (len(f), sep)
    # a word between: not the label (glued, the hamza / madda forms of "with the word" + "family" + "secret" stay
    # other words: ``test_arabic_phrases_are_not_masked``)
    f = word.replace(" ", " \u0648 ")
    assert not flagged(f"{f}: {value(r)}") and not masked(f"{f}: {value(r)}")
    # not a declared proclitic: not the label as a whole word (bare); before a value it is a label inside a longer
    # written word (round 7, decision 75: read as ffd28ca8 and cbdf5e78 read it; letters after it, strict only)
    for f in ("\u0627" + word, "\u0627" + word.replace(" ", ""), "\u05d0" + hebrew, hebrew + "\u05d5\u05ea"):
        assert not flagged(f)
        assert flagged(f"{f}: {value(r)}") and masked(f"{f}: {value(r)}") != f.startswith(hebrew)


def test_no_script_label_is_read_on_an_ascii_folded_view():
    """Persian "Hormuz" + "crossing" and Arabic three-word phrases: their ASCII-folded view turns a letter into a
    Latin lookalike, which then stands as a change of script, a word start, before the letters of the Persian
    label's first word inside a longer word; read there, the phrase was the label. The client view reads script
    labels on the views as written only (as the strict lane does)."""
    r = random.Random(SEED + 17)
    texts = ("\u0647\u0631\u0645\u0632 \u0639\u0628\u0648\u0631", "\u0647\u0631\u0645\u0632-\u0639\u0628\u0648\u0631",
             "\u0647\u0631\u0645\u0632\u0639\u0628\u0648\u0631", "\u0645\u062d\u0627\u0631\u0645 \u0632 \u0639\u0628\u0648\u0631",
             "\u0627\u0644\u0647\u0631\u0645 \u0632 \u0639\u0628\u0648\u0631")
    for t in texts:
        assert sg.check(t) is None, len(t)
    # (round 7, decision 75; OWNER DECISION) "Hormuz" + "crossing" holds the Persian label inside a longer written
    # word ON THE VIEW AS WRITTEN: before a value it is read as ffd28ca8 and cbdf5e78 read it (the client view
    # with the words apart by a space or nothing, not a hyphen); the other three are read on no view
    got = [masked(f"{t}: ready today") or masked(f"{t}: {value(r)}") for t in texts]
    assert got == [True, False, True, False, False]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sg, "_INWORD", {k: (re.compile("(?!x)x"), None) for k in ("any", "end")})
        prev.clear_guard_caches()
        assert not any(masked(f"{t}: ready today") or masked(f"{t}: {value(r)}") for t in texts)
    prev.clear_guard_caches()


# ================================================================================================ S-3: spelled

def test_a_label_spelled_one_unit_at_a_time_is_read_with_one_separator():
    r = random.Random(SEED + 9)
    for e in sg._SCRIPT + sg._CV_SCRIPT_FULL:
        units = e.replace(" ", "")
        # (every entry, the Han and kana ones and the two-letter ones included, with each of the four separators:
        # review of ced056c5, brief item 4; residual (e) and the 768 Han / kana cells of M1 are closed)
        for s in (" ", "_", "-", "."):
            spelled = s.join(units)
            for sep in (": ", " is "):
                t = f"{spelled}{sep}{value(r)}"
                assert flagged(t) and masked(t), (len(e), s, sep)
    # the Hindi entry, every code point apart (the virama the inspection view drops leaves two separators)
    for s in (" ", "_", "-", "."):
        t = s.join(HINDI) + f": {value(r)}"
        assert flagged(t) and masked(t), s
    # the Greek label decomposed (NFD), every code point apart: the word view drops the accents, which leaves two
    # separators where each stood
    nfd = unicodedata.normalize("NFD", GREEK_LABEL).replace(" ", "")
    assert len(nfd) == len(GREEK_LABEL) + 1
    for s in ("_", "-", "."):
        t = s.join(nfd) + f": {value(r)}"
        assert flagged(t) and masked(t), s
    # the two-letter labels with a hyphen or a dot between their letters are read only as the WHOLE entry so
    # spelled, a word of its own: a letter next to it, or joined to it by the same separator, makes another word
    for e in ("\ube44\ubc88", "\uc554\ud638", "\ud1a0\ud070"):
        for s in ("-", "."):
            t = s.join(e) + f": {value(r)}"
            assert flagged(t) and masked(t), s
            for other in ("\uac00" + s + s.join(e), s.join(e) + s + "\uac00", "\uac00" + s.join(e), s.join(e) + "\uac00"):
                assert not masked(f"{other}: {value(r)}"), s
    # two separators mixed between the units: not masked; (review of cbb6d7b7 F3) flagged by the strict lane, which
    # reads the units with any non-letters between them, as ffd28ca8 and cbdf5e78 read them
    for t in ("\u043f_\u0430 \u0440_\u043e \u043b_\u044c", "\u043f-\u0430 \u0440-\u043e \u043b-\u044c"):
        assert flagged(f"{t}: {value(r)}") and not masked(f"{t}: {value(r)}")


def test_residual_h_one_letter_words_joined_into_the_spelled_label_are_masked():
    """Declared residual (h) (ADR 0051 decision 73): 82 joins of the M2 word lists, Korean 30, Chinese 12, Japanese
    40, are two one-letter words joined by "-" or "." that spell a label exactly (the label in the spelled form the
    brief requires to be read; for the triples, a list "word" that is only a mark or a variation selector between
    them, which the views drop). The client view masks a value after them (it did not at ffd28ca8); the strict
    lane, the URL check and the host lanes read them as ffd28ca8 did. A sample of each language, pinned."""
    r = random.Random(SEED + 19)
    sample = (("\ube44", "\ubc88"), ("\uc554", "\ud638"), ("\ud1a0", "\ud070"),          # Korean
              ("\u53e3", "\u4ee4"), ("\u4ee4", "\u724c"), ("\u5bc6", "\u7801"))          # Chinese, Japanese
    for a, b in sample:
        for s in ("-", "."):
            t = a + s + b
            assert masked(f"{t}: ready today") and masked(f"{t}: {value(r)}"), s
            # as at ffd28ca8: the strict lane flags them (its letters-only view), a cited URL holding them is not
            # fetched, a host built from the hyphenated one is refused by the check lane
            assert flagged(t) and sg.url_has_secret(f"https://site.test/blog/{t}/")
            assert s == "." or sg.check(t + ".site.test") is not None
        # the same two words with a mark or a selector typed between (the triples)
        assert masked(f"{a}-\ufe0f-{b}: {value(r)}") and masked(f"{a}.\u0301.{b}: {value(r)}")


def test_a_label_word_with_one_space_put_inside_is_read_as_written():
    """The disguised label of the leak corpus (a no-break space or a line break typed into the word): ONE space or
    "_" inside a label word is read, letter for letter as written (nothing folded), as both lanes of ffd28ca8 read
    every script label; so it reads nothing ffd28ca8 did not. Not for the Greek label (its accents and final sigma
    are folded) nor the marked ones."""
    r = random.Random(SEED + 16)
    for sep in ("\u00a0", "\u2028", "\n", " ", "_"):
        for label in (RUSSIAN, KOREAN, HEBREW, "\u043a\u043b\u044e\u0447 api", "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631"):
            w = label.split(" ")[0]
            t = w[:2] + sep + w[2:] + label[len(w):] + f": {value(r)}"
            assert flagged(t) and masked(t), (len(label), hex(ord(sep)))
    assert [sg._split_alternatives(e, sg._SCRIPT_SPELL[sg._SCRIPT.index(e)]) for e in (sg._SCRIPT[-1], HINDI, THAI)] == [[], [], []]
    # the Greek phrases of MUST 1 stay unread, with any one space or none
    assert not any(flagged(p) for p in greek_phrases())


# ================================================================================================ the Latin lookalikes

def test_ordinary_greek_words_side_by_side_are_not_a_latin_label():
    r = random.Random(SEED + 10)
    assert len(GREEK_ACCENT_TRIPLES) == 40
    texts = [t.replace("+", sep) for t in GREEK_ACCENT_TRIPLES for sep in (" ", "-")]
    # (they spell a Latin label once their accented letters are read as lookalikes, as cbdf5e78 read them: the
    # mutation test below)
    assert [i for i, t in enumerate(texts) if flagged(t) or masked(f"{t}: {value(r)}")] == []


def test_a_latin_label_spelled_with_one_accented_lookalike_is_still_read():
    r = random.Random(SEED + 11)
    forms = ["p\u03acssword", "t\u03ccken", "s\u03adcret", "T\u038cKEN", "passw\u03ccrd", "ap\u03af key"]
    assert [flagged(f"{f}: {value(r)}") for f in forms] == [True] * 6
    # ... also beside a word written in Greek (the text then has one-script words: the lone letter is still read)
    assert flagged(f"{GREEK_CODE} p\u03acssword: {value(r)}")
    assert sg._label_norm("p" + chr(0x3ac) + "ssword") == "password" and sg._one_script_spans("p" + chr(0x3ac) + "ssword") == []
    # in a word written wholly in one other script the accented letter is a letter of that word: not read as Latin
    greek = "".join(chr(o) for o in (0x3ad, 0x3b3, 0x3b9, 0x3bd, 0x3b5))           # "became"
    assert sg._one_script_spans(greek + " x") == [(0, 5)] and not sg._label_norm(greek).isascii()
    assert sg._refold(sg._label_norm(greek)).isascii()          # (the second pass would read it as Latin letters)


def test_residual_g_glued_lone_lookalike_joins_stay_flagged():
    """Declared residual (g) (ADR 0051 decision 73): 165 glued joins of the M2 word lists (Greek 141, Persian 20,
    Arabic 3, Japanese 1) in which a one-letter word of another script carrying a mark is glued between Latin
    letters read, through the lone-letter lookalike pass, as a Latin label. That is the homoglyph-evasion shape
    the pass exists for (``p`` + alpha with tonos + ``ssword``, asserted by ``tests/test_review_cd0a6c7d.py``) and
    cannot be told from it: they are flagged on purpose. A sample of each language, pinned, so that a change to it
    is deliberate."""
    sample = ("p\u03ce", "cl\u03ad", "\u03cctp", "\u03cct\u03c1", "pi\u03ae",           # Greek
              "c\u0622e", "has\u0622of", "has\u0622on", "has\u0622one",                    # Persian
              "c\u0623e", "c\u0625e",                                                      # Arabic
              "\u043e\u0308tp")                                                            # Japanese list
    assert [sg.check(t) for t in sample] == ["a_label"] * len(sample)
    # ... the same letters as separate words are each read in their own script (``_fold_words``): not flagged
    assert [sg.check(" ".join(t)) for t in ("c\u0623e", "c\u0625e")] == [None, None]


def test_letters_of_separate_words_are_read_each_in_its_own_word():
    """A label split over separate words (``to ken``) or spelled one letter at a time is read from those words;
    each is read on its own, so a one-letter word of another script carrying a mark (an Arabic alef with a hamza
    between two Latin letters) is that letter, not a Latin lookalike."""
    r = random.Random(SEED + 14)
    for t in ("c \u0623 e", "c-\u0623-e", "c \u0625 e", "\u03cc t p", "has \u0622 of"):
        assert sg.check(t) is None, len(t)        # (the client view reads one character at a time, as at ffd28ca8)
    assert sg._fold_words("c", "\u0623", "e") != "cle" and sg._fold_words("to", "ken") == "token"
    # two letters of one other script in a row inside a word are a word of that script, not lookalikes: a Latin-
    # letter token of the Arabic / Persian word lists glued to the interjection "ah" (alef with a madda or a hamza,
    # then heh) is no label (36 such joins of those lists, ADR 0051 decision 73)
    for t in ("thes\u0622\u0647", "thes\u0623\u0647", "withas\u0622\u0647", "withas\u0623\u0647"):
        assert sg.check(t) is None, len(t)
    assert flagged(f"to ken: {value(r)}") and flagged(f"p a s s w o r d: {value(r)}")


def test_korean_takes_its_particles_and_nothing_else():
    r = random.Random(SEED + 15)
    for f in ("\ube44\ubc00\ubc88\ud638", "\ube44\ubc00\ubc88\ud638\ub97c", "\ube44\ubc88\uc774", "\uc554\ud638\ub294", "\ud1a0\ud070\uc744"):
        assert flagged(f"{f}: {value(r)}") and masked(f"{f}: {value(r)}"), len(f)
    # "encryption" ("cipher" + a suffix that is not a particle), "decryption": not masked; (round 7, decision 75)
    # a word before the label is a label inside a longer written word: masked before a value, as at ffd28ca8
    for f in ("\uc554\ud638\ud654", "\ubcf5\ud638\ud654"):
        assert not masked(f"{f}: {value(r)}"), len(f)
    assert masked(f"\ud574\uc554\ud638: {value(r)}") and not flagged("\ud574\uc554\ud638")


def test_mutation_with_the_second_pass_everywhere_the_greek_triples_read_as_latin_labels(monkeypatch):
    monkeypatch.setattr(sg, "_one_script_spans", lambda s: [])
    monkeypatch.setattr(sg, "_ONE_SCRIPT_TEXT", None)                 # (the one-pass answer for such a text, off)
    prev.clear_guard_caches()
    assert sum(flagged(t.replace("+", " ")) for t in GREEK_ACCENT_TRIPLES) == 40


_TAG_RUN = re.compile(r"([^ Lm])(?:\1|m)*")


def _one_script_spans_loop(s: str) -> list:
    """``sg._one_script_spans`` written as a loop over every run of one tag (``_TAG_RUN``): the definition the
    pattern ``sg._ONE_SCRIPT_RUN`` is tested against (kept here: review 0334eeae NIT 2, test-only code is not in
    the production module)."""
    tags = s.translate(sg._SCRIPT_TAG)
    out: list = []
    n = len(tags)
    for m in _TAG_RUN.finditer(tags):
        a, b = m.span()
        if len(m.group(0).replace("m", "")) >= 2 or ((a == 0 or tags[a - 1] == " ") and (b == n or tags[b] == " ")):
            out.append((a, b))
    return out


def _latin_form_by_the_spans(monkeypatch, s: str) -> str:
    """The Latin normal form by its DEFINITION, without the shortcuts of M6 (what the measurements of round 5
    read): the first pass of the whole text; when it dropped a mark and the text is not ASCII, the second pass on
    the whole text when it has no one-script word, else part by part around the one-script words
    (``_refold_mixed``) -- with no other test in between (review 0334eeae NIT 1: the shortcut that kept the
    first pass whenever the second pass of the whole text changed nothing was not output-identical to this)."""
    if s.isascii():
        return s.lower()
    f = sg._FOLD.fold_cased(s)
    if f.isascii():
        return f
    g = unicodedata.normalize("NFC", unicodedata.normalize("NFKD", f).translate(sg._LABEL_MARKS))
    if g != f and not g.isascii():
        spans = _one_script_spans_loop(s)
        g = sg._refold(g) if not spans else sg._refold_mixed(s, spans)
    return g


def test_no_character_outside_the_marked_blocks_is_a_letter_of_a_marked_entry():
    """M6 (cost): the marked step is skipped for a view without a character of the marked blocks
    (``_MARKED_BLOCK_RX``). Every code point: none outside those blocks gives, on the letters-only view, a letter
    of the Hindi or the Thai key."""
    keys = set("".join(k for sp, k in sg._SPELL_KEYS if sp[0]))
    assert len(keys) >= 8
    bad = [o for o in range(0x110000) if not 0xD800 <= o <= 0xDFFF and set(sg._script_letters(chr(o))) & keys
           and sg._MARKED_BLOCK_RX.search(chr(o)) is None]
    assert bad == []


def test_the_latin_form_shortcuts_give_the_same_answer(monkeypatch):
    """M6 (cost): a text of one other script and ASCII is answered in one pass (``_ONE_SCRIPT_TEXT``), a text whose
    parts outside its one-script words are ASCII and touch no letter without ``_refold_mixed``
    (``_mixed_outside``), and the spans are found by one pattern (``_ONE_SCRIPT_RUN``). Each gives what the loop
    and the part-by-part fold give, on every phrase of this file and on random texts of Greek, Cyrillic, Hebrew,
    Arabic, Devanagari, Latin, marks, plain punctuation and ASCII punctuation (the case-ignorable characters among
    them, across which the final-sigma rule looks: a capital sigma before a Latin letter is not final, alone it
    is)."""
    r = random.Random(SEED + 18)
    pool = (list("\u03a3\u03c3\u03c2\u039a\u03a9\u038c\u03ac\u03cc\u03bf\u03c1\u03b1\u03ba\u0390\u03ca\u03c4")
            + list("\u0430\u043e\u0439\u0451\u0419\u0441") + ["\u0301", "\u0308", "\u0300"]
            + list("  .:'^`,;-_0!aoxSKpw") + ["\u0623", "\u0647", "\u092a", "\u093e", "\u00e9", "\u00a0"]
            + list("\u0964\u00ab\u201d\u2014\u060c\u05be\u05e1\u05bc\u0640\u064e\u094d\u2019\u2026"))
    texts = ["".join(r.choice(pool) for _ in range(r.randint(1, 14))) for _ in range(20000)]
    texts += greek_phrases() + [t.replace("+", sep) for t in GREEK_ACCENT_TRIPLES for sep in (" ", "-", "", ".")]
    texts += ["\u03a3\u0391\u03a3x", "\u03a3\u0391\u03a3.x", "\u03a3\u0391\u03a3 x", "user" + GREEK_LABEL.upper() + "x9"]
    # (found by mutating each shortcut: a capital sigma after a Latin letter or after another Greek word across
    # case-ignorable characters only, and a lone accented letter in a Latin word beside a Greek word)
    texts += ["  \u03cc s:\u03a3", "a.\u03a3 \u03cc", " \u03ac.'\u03a3 \u03ac", "\u03cc \u0391'\u03a3 ",
              "\u03c1 \u03cca", "\u03a3w\u03cc:\u03b1"]
    # (review 0334eeae NIT 1: texts the shortcut "keep the first pass when the second pass of the whole text changes
    # nothing" read otherwise than the part-by-part fold; 659 of 300,000 random texts of this pool did)
    nit1 = ["\u060c\u03a9\u064eS\xab\xe9\u039a\u2019\u03a3", "\u039a\u0419\u2019\u03a3\u05bc",
            "\u0964\u094d,\u0390'\u0441\u03c3^\u03a3"]
    texts += nit1
    fast = sum(1 for t in texts if sg._ONE_SCRIPT_TEXT.fullmatch(t) and not sg._ONE_SCRIPT_ACROSS.search(t)
               and t != t.encode("ascii", "ignore").decode())
    assert fast > 1500                                                   # (the one-pass answer is exercised)
    bad = [i for i, t in enumerate(texts) if sg._label_norm(t) != _latin_form_by_the_spans(monkeypatch, t)]
    assert bad == []
    assert [i for i, t in enumerate(texts) if sg._one_script_spans(t) != _one_script_spans_loop(t)] == []
    # the final sigma: folded as one string or part by part, a capital sigma touching a Latin letter differs; the
    # shortcut is not taken there
    t = "\u03a3\u0391\u03a3x"
    assert sg._ONE_SCRIPT_ACROSS.search(t) and sg._mixed_outside(t, sg._one_script_spans(t))


# ================================================================================================ N-1: the import check

def test_the_import_check_arms_are_each_needed(monkeypatch):
    russian = (RUSSIAN,)
    assert sg._label_keys("_TEST", russian, False) == (RUSSIAN,)
    monkeypatch.setattr(sg, "_SCRIPT_FOLD", {0x430: "x"})                 # the letters-match arm of ``_label_keys``
    with pytest.raises(ValueError):
        sg._label_keys("_TEST", russian, False)
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_SCRIPT_WORD", {})                           # capitals not folded: UPPER / Title refused
    monkeypatch.setattr(sg, "_natural_spellings", lambda e: (e,))
    assert sg._cv_script_keys("_TEST", russian) == (RUSSIAN,)
    for arm in (str.upper, str.title):
        monkeypatch.setattr(sg, "_natural_spellings", lambda e, arm=arm: (e, arm(e)))
        with pytest.raises(ValueError):
            sg._cv_script_keys("_TEST", russian)
    monkeypatch.undo()
    assert sg._natural_spellings(RUSSIAN) == (RUSSIAN, RUSSIAN.upper(), RUSSIAN.title())
    # a declared form that does not fit its entry stops the import
    monkeypatch.setattr(sg, "_SCRIPT_FORMS", {RUSSIAN: {"endings": ("1",)}})
    with pytest.raises(ValueError):
        sg._script_words("_TEST", russian)
    monkeypatch.undo()
    # the full forms are checked at import too: one the lanes do not read whole ("house") stops it
    sg._check_label_lists()
    monkeypatch.setattr(sg, "_CV_SCRIPT_FULL", sg._CV_SCRIPT_FULL + ("\u0434\u043e\u043c",))
    with pytest.raises(ValueError):
        sg._check_label_lists()


def test_one_pattern_reads_the_script_labels_in_both_lanes(monkeypatch):
    r = random.Random(SEED + 12)
    texts = [f"{e}: {value(r)}" for e in (RUSSIAN, GREEK_LABEL, HEBREW, KOREAN)]
    assert all(flagged(t) and masked(t) for t in texts)
    monkeypatch.setattr(sg, "_SCRIPT_SEQ", re.compile("(?!x)x"))
    monkeypatch.setattr(sg, "_STRICT_SEQ", re.compile("(?!x)x"))     # (and the strict lane's wider joins, round 7)
    prev.clear_guard_caches()
    assert not any(flagged(t.split(":")[0]) for t in texts)
    # (round 7: the in-word rule and the strict lane's wider joins, decision 75, read a label too; all off, nothing
    # is read)
    monkeypatch.setattr(sg, "_INWORD", {k: (re.compile("(?!x)x"), None) for k in ("any", "end")})
    prev.clear_guard_caches()
    assert not any(flagged(t) or masked(t) for t in texts)


# ================================================================================================ end to end

def test_a_citation_run_citing_the_ordinary_phrases_fetches_them_all(tmp_path):
    """URL resolution on: own-site URLs whose raw paths hold the Greek "code to database" phrase, a Thai join that
    is not the label, the Japanese loanword, a Hindi glued join and an Arabic phrase are all fetched and classified
    as resolved, and the owner reads their titles and hints in full."""
    greek = f"{GREEK_CODE} {GREEK_TO} {GREEK_BAS_WORDS[0]}"
    thai = THAI_PAIRS[0].replace("+", "")
    hindi = "".join(HINDI_GLUED_PAIRS[0].split("+"))
    arabic = ARABIC_PHRASES[0]
    words = [greek, thai, JAPANESE_LOANWORD, hindi, arabic]
    urls = [f"https://site.test/blog/{w.replace(' ', '-')}" for w in words]
    a = ProviderAnswer("ANSWER", "Z Best Media explains " + "; ".join(words) + ".", urls, "m", "1",
                       cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
    a.citation_titles = {u: f"{w}: the guide" for u, w in zip(urls, words)}
    a.citation_hints = {u: f"{w} is explained here" for u, w in zip(urls, words)}
    h = vw.harness(tmp_path, SEO_CALLUM_RESOLVE_URLS="1")
    fetcher = cd0.PageFetcher()
    h.ports.engines["openai"] = vw.Port([a])
    h.ports.fetcher = fetcher
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
        "prompts": ["who recovers marketplace revenue"], "engines": ["openai"]}), 201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN,
                                                     "prompt_set_id": psid}), 201)
    view = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}", caller="dashboard"))
    rows = view["result"]["facts"]["rows"]
    fields = cd0.evidence_fields(view)
    classes = collections.Counter((row["class"], row.get("reason"), (row.get("chain") or {}).get("resolution"))
                                  for row in rows)
    print(f"end to end, ordinary phrases: fetched={len(set(urls) & set(fetcher.fetched))} of {len(urls)} {dict(classes)}")
    assert run["status"] == "completed" and sorted(set(urls) & set(fetcher.fetched)) == sorted(urls)
    assert set(classes) == {("STRENGTH_URL", None, "RESOLVED")}
    assert fields and not any(f["alert"] for _k, f in fields)
    out = json.dumps(view, ensure_ascii=False)
    assert all(w in out for w in words)


def test_a_citation_run_carrying_the_label_forms_shows_no_value(tmp_path):
    """The label forms of this round (the spelled forms, the inflected and proclitic forms, the marked entries) in
    the answer, the title and the hint of a real run: no value shown to the owning tenant, nothing on disk."""
    r = random.Random(SEED + 13)
    forms = [" ".join(HINDI), "_".join(THAI), "\u043f\u0430\u0440\u043e\u043b\u044f", GREEK_CODE + " \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7",
             "\u05d4\u05e1\u05d9\u05e1\u05de\u05d4", "\u0628\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631", HINDI + "\u094b\u0902", THAI,
             "-".join(RUSSIAN), "\ube44\ubc00\ubc88\ud638\ub97c"]
    answers, values = [], []
    for i, label in enumerate(forms):
        url = f"https://site.test/guides/{vw.MARK}-note-{i}"
        vals = [rs(r, LOWER, 9) + str(r.randint(10, 99)) for _ in range(3)]
        a = ProviderAnswer("ANSWER", f"Z Best Media sellers note {i}. {label}: {vals[0]}", [url], "m", "1",
                           cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
        a.citation_titles = {url: f"{label} = {vals[1]}"}
        a.citation_hints = {url: f"{label} is {vals[2]}"}
        answers.append(a)
        values.extend(vals)
    h = vw.harness(tmp_path)
    port = vw.Port(answers)
    h.ports.engines["openai"] = port
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
        "prompts": [f"who recovers marketplace revenue, question {i}" for i in range(4)], "engines": ["openai"]}),
        201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN,
                                                     "prompt_set_id": psid}), 201)
    assert run["status"] == "completed" and port.asked >= len(forms)
    view = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}", caller="dashboard"))
    fields = cd0.evidence_fields(view)
    disk = vw.written(h)
    print(f"end to end, label forms: forms={len(forms)} values={len(values)} fields={len(fields)} "
          f"masked={sum(1 for _k, f in fields if f['alert'])}")
    assert cd0.shown_values(view, values) == 0 and sum(1 for v in values if v in disk) == 0
    assert len(fields) >= 30 and all(f["alert"] for _k, f in fields)
