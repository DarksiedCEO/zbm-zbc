"""core-spec/1 O-2: the independent "label's own words" oracle (``oracle/core_own_words.py``) holds every STRICT-R
cell of the matrix's CI sample (each is a spelling of a listed class) and none of the pinned collisions (Greek "code
to bas-", the Arabic S-2 phrases), and it reads no guard module."""
import inspect

import pytest

import test_review_cbdf5e78 as cbd
from conformance import core_matrix as M
from conformance.oracle import core_own_words as O

pytestmark = pytest.mark.conformance


def test_core_O_2_the_oracle_holds_every_listed_spelling():
    missed: dict = {}
    n = 0
    for c in M.ci_cells():
        if c.strict == "R" and c.sep_class in ("1", "2", "3a"):
            n += 1
            if not O.own_words(c.text):
                missed[f"SP{c.sp:02d}"] = missed.get(f"SP{c.sp:02d}", 0) + 1
    print(f"O-2 oracle: R cells={n} not held={missed}")
    assert n > 10000 and not missed


def test_core_O_2_the_oracle_holds_no_pinned_collision():
    greek = [f for p in cbd.greek_phrases() for f in (p, p.upper(), p.title())]
    assert sum(O.own_words(t) for t in greek) == 0
    assert sum(O.own_words(t) for t in cbd.ARABIC_PHRASES) == 0


def test_core_O_2_the_oracle_is_independent_of_the_guard():
    assert "secret_guard" not in inspect.getsource(O).replace("``agents.secret_guard``", "")
