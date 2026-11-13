"""priya-spec/1 M-2: the boundary set, enumerated in full on every run (gaps of exactly 3, mixed gaps, every Nd block,
every listed zero-width / bidi character, number words with hyphens / spaces / run together / one word among digits,
every encoding with and without padding, chunked encodings split at EVERY position, rot13 and reversal of the whole
query and of each word, UATP, Diners 36, every vanity code with / without the leading 1, E.164 at 8 and 15 digits,
'+' separated from the first digit by 0-3 characters). This set is the builder's own enumeration (its size is
asserted below); the reference harness's 70,346 cases were not available to the builder."""

from __future__ import annotations

import collections

import priya_gen as g
import pytest
from priya_common import dropped

pytestmark = pytest.mark.conformance
FAMILIES = {"gap_exactly_3", "gap_mixed_1_3", "nd_block", "invisible", "number_words", "one_number_word",
            "plus_gap_0_3", "e164_8", "e164_15", "uatp_diners36", "vanity_code", "encoding", "chunk_every_position",
            "whole_query_rot13_reversal"}


def test_M2_boundary_set_every_case_drops():
    cases = list(g.boundary())
    fams = collections.Counter(f for f, _q in cases)
    assert set(fams) == FAMILIES and len(cases) == BOUNDARY_SIZE, (len(cases), dict(fams))
    kept = collections.Counter(f for f, q in cases if not dropped(q))
    assert sum(kept.values()) == 0, dict(kept)


BOUNDARY_SIZE = 14300            # the builder's enumeration (a generator change must change this: drift guard)


def test_M2_other_script_context_inside_a_word_reversed_spelled_email():
    """D22 boundary: the whole query reversed word by word when a tab (not a space) joined the context to the value,
    so a context word of another script lands between the dot-word and the top-level domain; letters (CJK) and marks
    (a Devanagari vowel sign) of other scripts are markers, never part of the mailbox."""
    import random

    import privacy_r18_corpus as rc
    rng = random.Random("M2-d22")
    for ctx in ("\u4eca\u5929", "\u0927\u0928\u094d\u092f\u0935\u093e\u0926", "\u3067\u3059", "\uac10\uc0ac"):
        for _ in range(10):
            local, dom = rc.email(rng).split("@")
            label, tld = dom.split(".", 1)
            q = f"{local} at {label} dot {tld}\t{ctx}"
            rev = " ".join(w[::-1] for w in q.split(" "))
            assert dropped(rev), ctx
