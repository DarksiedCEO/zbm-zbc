"""priya-spec/1.1 C-2 (round 21): the FULL glue set - all 24 characters (fullwidth U+FF01-U+FF0F and U+FF1A-U+FF20,
the ideographic comma and full stop) - in every C-2 position (glued to a Latin, CJK, Arabic or Devanagari context
word before / after / on both sides, and as the value's own separators), for every class of the list; and C-4 with
D11 / D12 / D14: a spelled code digit glued to the seven keypad characters. Every case is enumerated in full on every
run; every case holds a value of the list (both readings of a glue character, the independent oracle) and is dropped.
Failures print ids and counts, never text."""

from __future__ import annotations

import collections

import priya_gen_c2 as g21
import pytest
from oracle import priya_list as o
from priya_common import dropped

pytestmark = pytest.mark.conformance
# the builder's enumeration (a generator change must change these: drift guard)
C2_SIZE = 8640            # 13 class forms x 24 glue characters x (4 scripts x 3 positions x 2 + inside: 4 or 0)
VANITY_GLUED_SIZE = 672   # 7 codes x with / without the 1 x 2 languages x 4 forms x 6


def test_C2_every_glue_character_in_every_position_for_every_class():
    cases = list(g21.cases())
    assert len(cases) == C2_SIZE, len(cases)
    chars = collections.Counter(k.split("|")[1] for k, _q in cases)
    assert len(chars) == 24 and len({k.split("|")[0] for k, _q in cases}) == len(g21.FORMS), dict(chars)
    kept = collections.Counter(k for k, q in cases if not dropped(q))
    assert sum(kept.values()) == 0, dict(kept)
    unlisted = collections.Counter(k for k, q in cases if o.mandated(q) is None)
    assert sum(unlisted.values()) == 0, dict(unlisted)


def test_C4_spelled_code_digit_glued_to_the_keypad_letters():
    cases = list(g21.glued_vanity())
    assert len(cases) == VANITY_GLUED_SIZE, len(cases)
    kept = collections.Counter(k for k, q in cases if not dropped(q))
    assert sum(kept.values()) == 0, dict(kept)
    assert all(o.mandated(q) == "vanity" for _k, q in cases)
