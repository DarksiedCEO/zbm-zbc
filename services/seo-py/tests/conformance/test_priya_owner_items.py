"""priya-spec/1 section 6, at the owner's decisions (2026-11-13, every item at its default): OI-1 ZIP + year whose 9
digits are an SSN is dropped; OI-2 '_at_' / '-at-' / '_dot_' / '-dot-' e-mails are read as spelled; OI-3 '+' may stand
up to three characters before the first digit of an E.164 phone."""

from __future__ import annotations

import random

import privacy_r18_corpus as rc
import privacy_r19_corpus as r19
import pytest
from oracle import priya_list as o
from priya_common import dropped

from agents import search_performance as sp

pytestmark = pytest.mark.conformance


def test_OI1_zip_plus_year_spelling_an_ssn_is_dropped():
    rng = random.Random("OI1")
    n = 0
    while n < 200:
        d = rc.ssn(rng)
        if not d[5:].startswith(("19", "20")):
            continue
        q = f"{rng.choice(rc._HEADS)} {d[:5]} {d[5:]}"
        assert o.mandated(q) == "stream_ssn" and dropped(q)
        n += 1


def test_OI2_separator_spelled_emails_are_dropped():
    rng = random.Random("OI2")
    for _ in range(200):
        v = r19.email_unlisted(rng) if rng.random() < 0.5 else rc.email(rng)
        for sep in ("_", "-"):
            local, dom = v.split("@")
            q = local + f"{sep}at{sep}" + f"{sep}dot{sep}".join(dom.split("."))
            assert dropped(q), sep


def test_OI3_plus_up_to_three_characters_before_the_first_digit():
    rng = random.Random("OI3")
    for gap in ("", "x", "xq", "xqk", " ", " - ", "(", "(x)"):
        for _ in range(30):
            d = rc.e164(rng)
            assert sp.digit_stream_hit("+" + gap + "q".join((d[:2], d[2:6], d[6:]))), repr(gap)
    assert not sp.digit_stream_hit("+wxqk" + "q".join(("12", "3456", "78")))
