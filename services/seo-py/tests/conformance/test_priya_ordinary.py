"""priya-spec/2 section 7 (OQ-1, OQ-2, OQ-3 replaced, OQ-CAP, OQ-4 re-aimed = OQ-DISCLOSE) on the 5 frozen ordinary
corpora (oq, rv18_ord, rv19_oq19, rv20_oq20, rv21_oq21). Every query is labelled by the independent oracle
(``oracle.priya_list.mandated``) and judged by Priya (stage 1 OG with the tree's pinned V and T empty, then stage 2).
Failures print class ids and counts, never query text.

Re-pins (G-7: v1 bars REPLACED, never silently dropped; card P2-11, lead 20:50 PT):
- v1 OQ-3 "a mandated query kept is a leak" -> spec 7 OQ-3 (replaced): mandated READABLE = 0 (Z-PROMOTED).
- v1 OQ-4 "not-mandated drops equal the v1 ADR table" -> spec 7 OQ-4 / OQ-DISCLOSE: per corpus and class n, WITHHELD,
  mandated, not-mandated WITHHELD, count per reason code, delta vs 33472699 (round 21 head, v1 drops) PRINTED; the ADR
  0047 v2 tables (``priya-oq2-table-<corpus>`` markers, written by P2-15) must equal the suite exactly (heavy lane).
- v1 OQ-5 "text classes drop nothing" -> spec 7 OQ-CAP (owner decision 2, PROVISIONAL 5.0%, unchanged by the owner
  decisions after CAL-4 / CAL-5 / CAL-6): not-mandated WITHHELD <= 5.0% per corpus AND per plain / question / local
  class; multilingual is DISCLOSED (residual P-r9; a's <= 10% not adopted, OW-9).
CI lane: the CI draw of each corpus (corpora.ci_rows: oq whole, rv corpora every 4th case). Heavy lane
(CONFORMANCE_HEAVY=1, the lead's): every case. ``PRIYA_OQ_TABLE_DIR`` set -> the OQ-4 table is also written there as
JSON (oq_table_<corpus>_<lane>.json, counts only) for the ADR round entry (P2-15)."""

from __future__ import annotations

import collections
import json
import os
import re
from pathlib import Path

import corpora
import priya_gen as g
import pytest
from oracle import priya_list as o
from priya_common import _T, BRANDS, SPEC

from agents import priya

pytestmark = pytest.mark.conformance
ADR = Path(__file__).resolve().parents[4] / "docs" / "adr" / "0047-seo-priya-keyword-ledger.md"
MANIFEST = Path(__file__).resolve().parent / "data" / "manifest.json"
LANG_SCRIPTS = 12
OQ = ("oq", "rv18_ord", "rv19_oq19", "rv20_oq20", "rv21_oq21")       # spec 7 OQ-1 = section 6 table
CAP = 5.0                                                              # spec 7 OQ-CAP (owner decision 2)
CAPPED = {"plain_text": "plain", "plain": "plain", "question": "question", "local": "local"}
BASE = "33472699"
_TALLY: dict = {}
_MEMO: dict = {}
HEAVY = pytest.mark.conformance_heavy


def adr_table(corpus: str = "oq") -> dict:
    """The v1 machine-readable table of ADR 0047 (round 21 head 33472699) for one ordinary corpus: {class: (n, dropped,
    mandated, not mandated)}; ``priya-oq-table`` (frozen oq) or ``priya-oq-table-<corpus>`` markers. Empty if the ADR
    has no table for the corpus. Used for the delta vs 33472699 (OQ-4) only, never as a bar."""
    text = ADR.read_text(encoding="utf-8")
    tag = "priya-oq-table" if corpus == "oq" else "priya-oq-table-" + corpus
    if f"<!-- {tag}:begin -->" not in text:
        return {}
    block = text.split(f"<!-- {tag}:begin -->")[1].split(f"<!-- {tag}:end -->")[0]
    out = {}
    for line in block.splitlines():
        m = re.match(r"\|\s*([a-z_]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", line)
        if m:
            out[m.group(1)] = tuple(int(x) for x in m.groups()[1:])
    return out


def adr_table_v2(corpus: str) -> dict | None:
    """ADR 0047 v2 round entry (P2-15), ``priya-oq2-table-<corpus>`` markers, rows
    ``| class | n | withheld | mandated | not mandated WITHHELD | CODE=count;CODE=count |`` (reasons of every WITHHELD
    query of the class, sorted by code; ``-`` for none). None if the markers are absent."""
    text = ADR.read_text(encoding="utf-8")
    tag = "priya-oq2-table-" + corpus
    if f"<!-- {tag}:begin -->" not in text:
        return None
    block = text.split(f"<!-- {tag}:begin -->")[1].split(f"<!-- {tag}:end -->")[0]
    out = {}
    for line in block.splitlines():
        m = re.match(r"\|\s*([a-z_]+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|", line)
        if m:
            rs = {} if m.group(6) in ("", "-") else dict((k, int(v)) for k, v in
                                                          (p.split("=") for p in m.group(6).split(";")))
            out[m.group(1)] = (*(int(x) for x in m.groups()[1:5]), rs)
    return out


def _verdict(q: str):
    v = _MEMO.get(q)
    if v is None:
        state, rule, _ = priya.verdict(q, BRANDS, priya.default_vocab(), _T)
        v = _MEMO[q] = (state == "WITHHELD", rule, bool(o.mandated(q)))
    return v


def measure(name: str, heavy: bool) -> dict:
    """{class: Counter(n, withheld, mand, nm, nm_withheld, mand_readable) + reasons (every WITHHELD) + nm_reasons}."""
    key = (name, heavy)
    if key not in _TALLY:
        rows = corpora.rows(name) if heavy else ((k, q) for _i, k, q in corpora.ci_rows(name))
        t = collections.defaultdict(lambda: {"c": collections.Counter(), "reasons": collections.Counter(),
                                             "nm_reasons": collections.Counter()})
        for cls, q in rows:
            w, rule, m = _verdict(q)
            e = t[cls]
            c = e["c"]
            c["n"] += 1
            c["withheld"] += w
            c["mand"] += m
            c["nm"] += not m
            c["nm_withheld"] += w and not m
            c["mand_readable"] += m and not w
            if w:
                e["reasons"][rule] += 1
                if not m:
                    e["nm_reasons"][rule] += 1
        _TALLY[key] = dict(t)
    return _TALLY[key]


def _pct(a: int, b: int) -> float:
    return 100.0 * a / b if b else 0.0


def _full(name: str, heavy: bool) -> bool:
    return heavy or corpora.CI_STRIDE.get(name, 1) == 1


def _lane(heavy: bool) -> str:
    return "heavy" if heavy else "ci"


# ---- OQ-1, OQ-2 (kept) -------------------------------------------------------------------------------------------
def test_OQ1_corpus_shape():
    rows = list(g.ordinary())
    sizes = collections.Counter(c for c, _i, _q in rows)
    assert dict(sizes) == g.OQ_SIZES and len(rows) == 24400
    digit = sum(1 for c, _i, q in rows if any(ch.isdigit() for ch in q))
    assert digit >= 0.5 * len(rows), digit
    scripts = {ord(ch) >> 7 for c, _i, q in rows if c == "multilingual" for ch in q if not ch.isascii()}
    assert len(g._ML) >= 19 and len(scripts) >= LANG_SCRIPTS - 6
    assert set(OQ) <= set(corpora.ORDINARY), sorted(corpora.ORDINARY)        # rv21_sw: reported probe


@pytest.mark.parametrize("name", OQ)
def test_OQ2_every_query_labelled_and_every_withheld_coded(name):
    for cls, e in measure(name, False).items():
        c = e["c"]
        assert c["mand"] + c["nm"] == c["n"] and c["n"] > 0, (name, cls)
        assert sum(e["reasons"].values()) == c["withheld"] and None not in e["reasons"], (name, cls)


# ---- OQ-3 (replaced): mandated READABLE = 0 -------------------------------------------------------------------------
def _oq3(name: str, heavy: bool):
    t = measure(name, heavy)
    bad = {cls: e["c"]["mand_readable"] for cls, e in t.items() if e["c"]["mand_readable"]}
    print(f"OQ-3 {name} {_lane(heavy)}: mandated={sum(e['c']['mand'] for e in t.values())} READABLE={sum(bad.values())}")
    assert not bad, (name, bad)


@pytest.mark.parametrize("name", OQ)
def test_OQ3_mandated_readable_is_zero_ci(name):
    _oq3(name, False)


@HEAVY
@pytest.mark.parametrize("name", OQ)
def test_OQ3_mandated_readable_is_zero_heavy(name):
    _oq3(name, True)


# ---- OQ-CAP (owner decision 2): <= 5.0% per corpus and per plain / question / local class ------------------------------
def _cap(name: str, heavy: bool):
    t = measure(name, heavy)
    nm = sum(e["c"]["nm"] for e in t.values())
    w = sum(e["c"]["nm_withheld"] for e in t.values())
    cells = {"corpus": (w, nm)} | {CAPPED[cls]: (e["c"]["nm_withheld"], e["c"]["nm"]) for cls, e in t.items()
                                   if cls in CAPPED}
    assert set(cells) == {"corpus", "plain", "question", "local"}, (name, sorted(cells))
    for cell, (a, b) in cells.items():
        print(f"OQ-CAP {name} {_lane(heavy)} {cell}: {a}/{b} = {_pct(a, b):.2f}% (cap {CAP}%)")
    over = {cell: f"{a}/{b}" for cell, (a, b) in cells.items() if _pct(a, b) > CAP}
    assert not over, (name, over)                       # the cap is never moved (spec 7; owner after OQ-REAL only)


@pytest.mark.parametrize("name", OQ)
def test_OQCAP_ci(name):
    _cap(name, False)


@HEAVY
@pytest.mark.parametrize("name", OQ)
def test_OQCAP_heavy(name):
    _cap(name, True)


# ---- OQ-4 / OQ-DISCLOSE: the table for every class, per reason code, delta vs 33472699 -------------------------------
def table(name: str, heavy: bool) -> dict:
    t, base, full = measure(name, heavy), adr_table(name), _full(name, heavy)
    rows = {}
    for cls in sorted(t, key=lambda k: (k not in CAPPED, k)):
        c, b = t[cls]["c"], base.get(cls)
        rows[cls] = {"n": c["n"], "withheld": c["withheld"], "mandated": c["mand"], "not_mandated": c["nm"],
                     "nm_withheld": c["nm_withheld"], "mandated_readable": c["mand_readable"],
                     "share": round(_pct(c["nm_withheld"], c["nm"]), 2),
                     "bar": "OQ-CAP" if cls in CAPPED else "disclosed (P-r9)",
                     "reasons": dict(sorted(t[cls]["reasons"].items())),
                     "nm_reasons": dict(sorted(t[cls]["nm_reasons"].items())),
                     "v1_dropped": b[1] if b and full else None,
                     "delta_vs_" + BASE: c["withheld"] - b[1] if b and full else None}
    return {"spec": SPEC, "corpus": name, "lane": _lane(heavy), "full_corpus": full, "base": BASE,
            "vocab_sha256": priya.default_vocab().sha256, "classes": rows}


def _oq4(name: str, heavy: bool):
    tb = table(name, heavy)
    print(f"OQ-DISCLOSE {name} {tb['lane']} full={tb['full_corpus']} | class | n | WITHHELD | mandated | nm WITHHELD | "
          f"share | bar | delta vs {BASE} | reasons")
    for cls, r in tb["classes"].items():
        print(f"OQ-DISCLOSE {name} | {cls} | {r['n']} | {r['withheld']} | {r['mandated']} | {r['nm_withheld']} | "
              f"{r['share']:.2f}% | {r['bar']} | {r['delta_vs_' + BASE]} | {r['reasons']}")
    out = os.environ.get("PRIYA_OQ_TABLE_DIR")
    if out:
        Path(out, f"oq_table_{name}_{tb['lane']}.json").write_text(json.dumps(tb, indent=1, sort_keys=True) + "\n")
    for cls, r in tb["classes"].items():
        assert r["withheld"] == r["nm_withheld"] + r["mandated"] - r["mandated_readable"], (name, cls)
        assert sum(r["reasons"].values()) == r["withheld"], (name, cls)
    if heavy:
        n = json.loads(MANIFEST.read_text())["corpora"][name]["n"]
        assert sum(r["n"] for r in tb["classes"].values()) == n, name
    if tb["full_corpus"] and adr_table(name):
        assert set(adr_table(name)) == set(tb["classes"]), (name, "class set differs from the 33472699 table")


@pytest.mark.parametrize("name", OQ)
def test_OQ4_disclosure_table_ci(name):
    _oq4(name, False)


@HEAVY
@pytest.mark.parametrize("name", OQ)
def test_OQ4_disclosure_table_heavy(name):
    _oq4(name, True)


def test_OQ5_text_classes_capped_or_disclosed():
    """v1 OQ-5 re-pinned: plain text / question / local under OQ-CAP; multilingual disclosed (P-r9, OW-9)."""
    t = measure("oq", False)
    for cls in g.TEXT_CLASSES:
        c = t[cls]["c"]
        print(f"OQ-5 oq {cls}: nm WITHHELD {c['nm_withheld']}/{c['nm']} = {_pct(c['nm_withheld'], c['nm']):.2f}%")
        if cls in CAPPED:
            assert _pct(c["nm_withheld"], c["nm"]) <= CAP, cls
    assert "multilingual" not in CAPPED


@HEAVY
@pytest.mark.parametrize("name", OQ)
def test_OQDISCLOSE_adr_v2_table_equals_suite_heavy(name):
    """spec 6 OQ-DISCLOSE: ADR 0047's v2 tables equal the suite's numbers exactly (red until P2-15 writes them).
    Lead ruling 22:55 PT (F-26): mandated / not-mandated WITHHELD use OQ-2 labels (``rvgen.rv19_oracle``) via the
    reviewer suite's own tally; each disagreement with the suite oracle is reported (class, index, both labels)."""
    from test_priya_reviewer_corpora import _oq_tally                 # the OQ-2 code path (GATE-2H2), not a copy
    adr = adr_table_v2(name)
    assert adr is not None, (name, "ADR 0047 has no priya-oq2-table for this corpus (card P2-15)")
    t, dis = _oq_tally(name, True)
    for cls, idx, s, m in dis:                                       # ids + both labels only, never query text
        print(f"OQDISCLOSE-DISAGREE {name} {cls}#{idx} suite={s} oq2={m}")
    print(f"OQDISCLOSE-DISAGREE {name} total={len(dis)}")
    got = {cls: (t[cls]["n"], t[cls]["drop"], t[cls]["mand"], t[cls]["nm"], r["reasons"])
           for cls, r in table(name, True)["classes"].items()}
    assert got == adr, {cls: (adr.get(cls), v) for cls, v in got.items() if adr.get(cls) != v}
