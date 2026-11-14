"""Shared helpers of the Priya conformance modules (priya-spec/1). Failures print ids and counts, never text."""

from __future__ import annotations

import collections

from agents import priya
from agents import search_performance as sp

BRANDS = ["acme"]
SPEC = "priya-spec/1"


def dropped(q: str) -> bool:
    """Z-1 at the function level, as Priya decides (``priya.build``'s slot): a row is dropped when its raw text OR its
    stored key is personal data; only a row kept by both ever reaches an output."""
    return sp.classify_query(q, BRANDS) == "pii_suspect" or sp.classify_query(priya.normalize(q), BRANDS) == "pii_suspect"


def leak_report(items) -> dict:
    """items: iterable of (key, text). Returns {key: count} of LEAKED texts (kept raw or kept key)."""
    out = collections.Counter()
    for key, q in items:
        if not dropped(q):
            out[key] += 1
    return dict(out)


def define(module_globals: dict, name: str, fn, marks=()) -> None:
    """Define a test function ``name`` in a module (one test per spec item, the item id in its name)."""
    fn.__name__ = name
    for m in marks:
        fn = m(fn)
    module_globals[name] = fn
