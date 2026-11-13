"""priya-spec/1 G-6 pins: one minimal case per named mutant, each a query the mutant would keep (the mutation runs
themselves are the lead gate's; these cases make the CI lane kill them)."""

from __future__ import annotations

import base64
import random

import privacy_r18_corpus as rc
import pytest
from priya_common import dropped

from agents import search_performance as sp

pytestmark = pytest.mark.conformance
R = random.Random(906)


def _digits(n):
    return str(R.randint(1, 9)) + "".join(str(R.randint(0, 9)) for _ in range(n - 1))


def test_G6_m09_e164_at_eight_digits():
    for _ in range(30):
        d = _digits(8)
        assert sp.digit_stream_hit("+" + "q".join((d[:2], d[2:5], d[5:])))     # letter gaps: only the stream reads it


def test_G6_m10_plus_separated_from_the_first_digit():
    for _ in range(30):
        d = _digits(9)
        assert sp.digit_stream_hit("+kq" + "q".join((d[:3], d[3:6], d[6:])))


def test_G6_context_window_check():
    for phrase in (b"my api key", b"launch code"):
        b = base64.b64encode(phrase).decode()
        assert sp.classify_query("rug cleaner " + b, ["acme"]) == "pii_suspect"
        assert sp._suspicious_windows("rug cleaner " + b)


def test_G6_vanity_gap_of_three():
    for code in ("800", "888", "855"):
        assert sp._vanity_any(f"1 - {code} - flowers") and sp._vanity_any(f"{code} - f - l - o - w - e - r - s")
        assert dropped(f"call 1 - {code} - flowers tonight")


def test_G6_address_context_of_three_tokens_and_more():
    for tail in ("open now tonight", "open now tonight please", "a b c d e f g h"):
        assert dropped(f"17 elm way {tail}") and dropped(f"62 oak lane, {tail}")


def test_G6_card_at_thirteen_digits():
    for _ in range(20):
        body = "4" + "".join(str(R.randint(0, 9)) for _ in range(11))
        d = body + rc._luhn_digit(body)
        assert len(d) == 13 and sp.stream_card(d) and dropped("x".join((d[:4], d[4:8], d[8:])))


def test_G6_uatp_on():
    body = "1" + "".join(str(R.randint(0, 9)) for _ in range(13))
    d = body + rc._luhn_digit(body)
    assert sp.stream_card(d) and dropped("q".join((d[:4], d[4:10], d[10:])))


def test_G6_diners_36_on():
    body = "36" + "".join(str(R.randint(0, 9)) for _ in range(11))
    d = body + rc._luhn_digit(body)
    assert sp.card_iin(d) and dropped(" ".join((d[:4], d[4:10], d[10:])))
