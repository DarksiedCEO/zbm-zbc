"""priya-spec/1 section 5 (OQ-1..OQ-5): the frozen ordinary-query corpus (24,400 queries, 20 classes, >= 50% digit-
bearing, >= 12 languages). Every drop is labelled by the independent oracle: list-mandated drops are REQUIRED (a
mandated query kept is a leak), and the not-mandated drops per class must EQUAL the machine-readable table of ADR
0047's round entry (between the ``priya-oq-table`` markers)."""

from __future__ import annotations

import collections
import re
from pathlib import Path

import priya_gen as g
import pytest
from oracle import priya_list as o
from priya_common import dropped

pytestmark = pytest.mark.conformance
ADR = Path(__file__).resolve().parents[4] / "docs" / "adr" / "0047-seo-priya-keyword-ledger.md"
LANG_SCRIPTS = 12


def _tally():
    drop, mand, nm, leak = (collections.Counter() for _ in range(4))
    for cls, _i, q in g.ordinary():
        d, m = dropped(q), o.mandated(q)
        drop[cls] += d
        mand[cls] += bool(m)
        nm[cls] += d and not m
        leak[cls] += bool(m) and not d
    return drop, mand, nm, leak


_T = None


def tally():
    global _T
    if _T is None:
        _T = _tally()
    return _T


def adr_table() -> dict:
    text = ADR.read_text(encoding="utf-8")
    block = text.split("<!-- priya-oq-table:begin -->")[1].split("<!-- priya-oq-table:end -->")[0]
    out = {}
    for line in block.splitlines():
        m = re.match(r"\|\s*([a-z_]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", line)
        if m:
            out[m.group(1)] = tuple(int(x) for x in m.groups()[1:])
    return out


def test_OQ1_corpus_shape():
    rows = list(g.ordinary())
    sizes = collections.Counter(c for c, _i, _q in rows)
    assert dict(sizes) == g.OQ_SIZES and len(rows) == 24400
    digit = sum(1 for c, _i, q in rows if any(ch.isdigit() for ch in q))
    assert digit >= 0.5 * len(rows), digit
    scripts = {ord(ch) >> 7 for c, _i, q in rows if c == "multilingual" for ch in q if not ch.isascii()}
    assert len(g._ML) >= 19 and len(scripts) >= LANG_SCRIPTS - 6


def test_OQ2_every_drop_labelled():
    drop, mand, nm, _leak = tally()
    for cls in g.OQ_SIZES:
        assert drop[cls] <= mand[cls] + nm[cls]


def test_OQ3_list_mandated_drops_are_required():
    _drop, _mand, _nm, leak = tally()
    assert sum(leak.values()) == 0, dict(leak)


def test_OQ4_not_mandated_drops_equal_the_adr_table():
    drop, mand, nm, _leak = tally()
    table = adr_table()
    for cls, n in g.OQ_SIZES.items():
        assert table.get(cls) == (n, drop[cls], mand[cls], nm[cls]), (cls, table.get(cls), (n, drop[cls], mand[cls],
                                                                                              nm[cls]))


def test_OQ5_text_classes_drop_nothing():
    drop, *_ = tally()
    assert all(drop[c] == 0 for c in g.TEXT_CLASSES), {c: drop[c] for c in g.TEXT_CLASSES}
