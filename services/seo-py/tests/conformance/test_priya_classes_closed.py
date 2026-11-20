"""priya-spec/1 section 1 (C1-C7): the generators produce only valid members of each closed class, judged by the
independent oracle (``oracle/priya_list.py``); the issuer table holds UATP and Diners 36."""

from __future__ import annotations

import random

import privacy_r18_corpus as rc
import privacy_r19_corpus as r19
import priya_gen as g
import pytest
from oracle import priya_list as o

from agents import search_performance as sp

pytestmark = pytest.mark.conformance
N = 300


def _vals(gen, seed):
    rng = random.Random(seed)
    return [gen(rng) for _ in range(N)]


def test_C1_card_values_are_cards():
    vals = _vals(rc.card, "C1")
    assert all(o.is_card(v) for v in vals)
    assert all(o.stream(v) == "stream_card" for v in vals)
    rng = random.Random("C1u")
    assert o.is_card(g._uatp(rng)) and o.is_card(g._diners36(rng))
    assert sp.stream_card(g._uatp(rng)) and sp.card_iin("36" + "0" * 12)


def test_C2_nanp_values_are_nanp():
    assert all(o.is_nanp(v) for v in _vals(rc.nanp, "C2"))


def test_C3_e164_values_are_e164():
    for v in _vals(rc.e164, "C3"):
        assert 8 <= len(v) <= 15 and o.stream("+" + v)


def test_C4_vanity_values_are_vanity():
    rng = random.Random("C4")
    for _ in range(N):
        assert o.vanity(o.chars(rc.vanity_form(rng, rc.vanity(rng))))


def test_C5_ssn_values_are_ssns():
    assert all(o.is_ssn(v) for v in _vals(rc.ssn, "C5"))


def test_C6_email_values_are_mailboxes_any_tld():
    rng = random.Random("C6")
    for _ in range(N):
        for v in (rc.email(rng), r19.email_unlisted(rng)):
            assert o._MAILBOX.search(v) and o.mandated(v) == "email"


def test_C7_address_values_are_addresses():
    rng = random.Random("C7")
    for _ in range(N):
        assert o.address(o.chars(rc.address(rng)))
