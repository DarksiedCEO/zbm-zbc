"""Shared helpers of the Priya conformance modules (priya-spec/2). Failures print ids and counts, never text."""

from __future__ import annotations

import collections

from agents import priya
from agents import priya_tenant as pt

BRANDS = ["acme"]
SPEC = "priya-spec/2"

_T = pt.empty_tenant_vocab("")          # T-4: the frozen corpora are judged with T empty
_MEMO: dict = {}


def readable(q: str) -> bool:
    """F-5 at the function level, as Priya decides: stage 1 OG (the tree's pinned V, T empty) then stage 2 on the raw
    text and its key (``priya.verdict``). Only a READABLE row is ever shown with its key text (spec 4 Z-PROMOTED)."""
    r = _MEMO.get(q)
    if r is None:
        if len(_MEMO) > 200_000:        # bound the memo in the heavy lane (corpora up to 452,000 rows)
            _MEMO.clear()
        r = _MEMO[q] = priya.verdict(q, BRANDS, priya.default_vocab(), _T)[0] == "READABLE"
    return r


def dropped(q: str) -> bool:
    """priya-spec/2 (CAL3-1, P2-10): a v1 DROP becomes "not READABLE" (WITHHELD with a code); never weakened (G-7)."""
    return not readable(q)


def readable_report(name: str, n: int, readable_n: int) -> None:
    """The per-corpus Z-PROMOTED count line of the CI / heavy lanes (counts only)."""
    print(f"Z-PROMOTED {name}: n={n} READABLE={readable_n}")


def leak_report(items) -> dict:
    """items: iterable of (key, text). Returns {key: count} of READABLE (promoted) texts."""
    out = collections.Counter()
    for key, q in items:
        if readable(q):
            out[key] += 1
    return dict(out)


def define(module_globals: dict, name: str, fn, marks=()) -> None:
    """Define a test function ``name`` in a module (one test per spec item, the item id in its name)."""
    fn.__name__ = name
    for m in marks:
        fn = m(fn)
    module_globals[name] = fn
