"""priya-spec/2 section 5 MC (a)-(d): metrics conservation through priya.build on the 5 OQ corpora and the PERSONAL
sample, with seeded synthetic metrics (card P2-6). CI-lane draws here; the full corpora are the lead job
scratchpad priya-v2/p26/mc_full.py (it imports ``mc_diffs`` from this file; counts only, never text).
Every expected number is recomputed from the INPUT rows, independently of priya's own partition, and the output side
is read from the ledger rows themselves (row metrics are exact integers) plus the MC block (unrounded sums)."""

from __future__ import annotations

from collections import Counter

import pytest
from corpora import ORDINARY, ci_rows
from test_priya_codes_v2 import CI_PERSONAL, _coder
from test_priya_withheld import WINDOW, bing, gsc

from agents import priya

pytestmark = pytest.mark.conformance

SRC = ("search_console", "bing_webmaster")
DATES = tuple(f"2026-10-{d:02d}" for d in (3, 9, 14, 21, 27))
REFUSED_Q = (
    "",
    "   ",
    chr(0x200B),
    "\t" + chr(0x3000),
)  # keys that normalise to "" (refused at schema, listed)
GDATE = f"{WINDOW['start']}/{WINDOW['end']}"


def _rng(n: int) -> int:
    """Seeded synthetic metric source (LCG); no real data."""
    return (n * 6364136223846793005 + 1442695040888963407) % (1 << 63)


def source_rows(queries: list) -> tuple:
    """Seeded synthetic GSC + Bing rows for a query list: every query in GSC; every 7th also upper-cased (2 spellings,
    1 key); about 2 in 5 on 1-3 Bing dates (some without a position); the refused keys appended to both sources."""
    g, b = [], []
    for n, q in enumerate(list(queries) + list(REFUSED_Q)):
        r = _rng(n)
        imp = r % 301
        clk = 0 if imp == 0 else (r >> 9) % (imp + 1) // 3
        pos = 1.0 + ((r >> 20) % 59000) / 1000.0
        g.append((q, clk, imp, None if imp == 0 else clk / imp, pos))
        if n % 7 == 0:
            g.append((q.upper(), clk % 5, (imp % 41) + 1, None, pos + 0.5))
        if r % 5 < 2 or n >= len(queries):
            for j in range(1 + (r >> 30) % 3):
                bi = (r >> (33 + j)) % 120
                bc = bi // 4
                ipos = (
                    None
                    if (r >> 40) % 11 == 0
                    else 1.0 + ((r >> (41 + j)) % 33000) / 1000.0
                )
                b.append((q, DATES[(n + j) % len(DATES)], bc, bi, None, ipos))
    return g, b


def _expected(g: list, b: list) -> tuple:
    cells, tot = Counter(), {s: Counter() for s in SRC}
    for _q, c, i, _ctr, pos in g:
        cells[(GDATE, SRC[0], "c")] += c
        cells[(GDATE, SRC[0], "i")] += i
        t = tot[SRC[0]]
        t["rows"], t["clicks"], t["impressions"] = (
            t["rows"] + 1,
            t["clicks"] + c,
            t["impressions"] + i,
        )
        t["pw"] += (pos or 0.0) * i
        t["pi"] += i
    for _q, d, c, i, _cp, ipos in b:
        cells[(d, SRC[1], "c")] += c
        cells[(d, SRC[1], "i")] += i
        t = tot[SRC[1]]
        t["rows"], t["clicks"], t["impressions"] = (
            t["rows"] + 1,
            t["clicks"] + c,
            t["impressions"] + i,
        )
        if ipos is not None:
            t["pw"] += ipos * i
            t["pi"] += i
    return cells, tot


def mc_diffs(queries: list, coder) -> Counter:
    """The MC (a)-(d) differences of one build over ``queries`` (0 everywhere = pass) plus sizes (keys n_*)."""
    g, b = source_rows(queries)
    led = priya.build(
        "acme", "run-p26", "2026-10-29T00:00:00Z", [], gsc(g), bing(b), coder=coder
    )
    mc, cnt, d = led["counts"]["mc"], led["counts"], Counter()
    parts = [mc[p] for p in priya.MC_PARTS]
    cells, tot = _expected(g, b)
    d["n_rows_in"], d["n_withheld"] = len(g) + len(b), len(led["withheld_rows"])
    d["n_dropped_past_cap"] = mc["dropped_past_cap"]["keys"]
    # (a) rows in = READABLE + WITHHELD + refused (listed, each with a reason code)
    d["a_rows"] += abs(
        len(g) + len(b) - sum(p["by_source"][s]["rows"] for p in parts for s in SRC)
    )
    lst = mc["refused"]["listed"]
    want = {
        (s, n)
        for s, rows in zip(SRC, (g, b))
        for n, r in enumerate(rows)
        if not priya.normalize(r[0])
    }
    d["a_refused_listed"] += len(want ^ {(r["source"], r["index"]) for r in lst})
    d["a_refused_reason"] += sum(r["reason"] != priya.REFUSED_EMPTY_KEY for r in lst)
    observed = [r for r in led["rows"] if r["origin"] == "observed"]
    d["a_keys_readable"] += abs(
        mc["readable"]["keys"] - len(observed) - mc["dropped_past_cap"]["keys"]
    )
    d["a_keys_withheld"] += abs(mc["withheld"]["keys"] - len(led["withheld_rows"]))
    # (b) clicks / impressions exactly per (page, date, source) and per source; output read from the rows themselves
    got = Counter()
    for p in parts:
        for k, v in p["cells"].items():
            page, date, src = k.split("|")
            d["b_page"] += page != ""
            got[(date, src, "c")] += v["clicks"]
            got[(date, src, "i")] += v["impressions"]
    d["b_cells"] += sum(abs(cells[k] - got[k]) for k in set(cells) | set(got))
    for s in SRC:
        rc = sum(
            ((r["metrics"] or {}).get(s) or {}).get("clicks", 0)
            for r in observed + led["withheld_rows"]
        )
        ri = sum(
            ((r["metrics"] or {}).get(s) or {}).get("impressions", 0)
            for r in observed + led["withheld_rows"]
        )
        rc += (
            mc["dropped_past_cap"][s]["clicks"]
            + mc["refused"]["by_source"][s]["clicks"]
        )
        ri += (
            mc["dropped_past_cap"][s]["impressions"]
            + mc["refused"]["by_source"][s]["impressions"]
        )
        d["b_total_rows_side"] += abs(rc - tot[s]["clicks"]) + abs(
            ri - tot[s]["impressions"]
        )
        pc = sum(p["by_source"][s]["clicks"] for p in parts)
        pi = sum(p["by_source"][s]["impressions"] for p in parts)
        d["b_total_mc_side"] += abs(pc - tot[s]["clicks"]) + abs(
            pi - tot[s]["impressions"]
        )
        # (c) impression-weighted mean position within 1e-9; ctr recomputed from (b)
        pw = sum(p["by_source"][s]["position_weighted"] for p in parts)
        pn = sum(p["by_source"][s]["position_impressions"] for p in parts)
        d["c_pos_impressions"] += abs(pn - tot[s]["pi"])
        if tot[s]["pi"]:
            d["c_position_1e-9"] += abs(pw / pn - tot[s]["pw"] / tot[s]["pi"]) > 1e-9
        if tot[s]["impressions"]:
            d["c_ctr"] += (
                abs(pc / pi - tot[s]["clicks"] / tot[s]["impressions"]) > 1e-12
            )
    # (d) per-reason totals sum to the WITHHELD totals; tiers / EMERGING include WITHHELD rows
    w = cnt["withheld"]
    for f in ("queries", "clicks", "impressions"):
        d["d_by_reason"] += abs(sum(v[f] for v in w["by_reason"].values()) - w[f])
    wp = mc["withheld"]["by_source"]
    d["d_withheld_vs_mc"] += abs(w["queries"] - mc["withheld"]["keys"])
    d["d_withheld_vs_mc"] += abs(w["clicks"] - sum(wp[s]["clicks"] for s in SRC))
    d["d_withheld_vs_mc"] += abs(
        w["impressions"] - sum(wp[s]["impressions"] for s in SRC)
    )
    wt = Counter(r["tier"] for r in led["withheld_rows"])
    d["d_tiers_withheld"] += sum(
        abs(mc["tiers"]["withheld"][t] - wt[t]) for t in ("ESTABLISHED", "EMERGING")
    )
    rt = mc["tiers"]["readable"]
    d["d_tiers_readable"] += abs(
        rt["ESTABLISHED"] + rt["EMERGING"] - mc["readable"]["keys"]
    )
    if not mc["dropped_past_cap"]["keys"]:
        d["d_tiers_readable"] += abs(rt["ESTABLISHED"] - cnt["established"])
        d["d_tiers_readable"] += abs(rt["EMERGING"] - cnt["emerging_observed"])
    return d


def failures(d: Counter) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("n_") and v}


CI_OQ_N, CI_PERSONAL_N = (
    800,
    3000,
)  # CI-sized, evenly strided draws; the full corpora are the lead job


def ci_draw(names, n: int) -> list:
    qs = [q for name in names for _i, _k, q in ci_rows(name)]
    return qs[:: max(1, len(qs) // n)][:n]


@pytest.fixture(scope="module")
def coder():
    return _coder()


@pytest.mark.parametrize("name", sorted(ORDINARY))
def test_MC_a_to_d_on_each_OQ_corpus_ci_draw(name, coder):
    d = mc_diffs(ci_draw([name], CI_OQ_N), coder)
    assert failures(d) == {}, (name, dict(d))


def test_MC_a_to_d_on_the_PERSONAL_ci_sample(coder):
    qs = ci_draw(CI_PERSONAL, CI_PERSONAL_N)
    d = mc_diffs(qs, coder)
    assert failures(d) == {}, dict(d)
    assert d["n_withheld"] > 0 and d["n_rows_in"] > len(qs)


def test_MC_ROWS_CAP_overflow_rows_stay_counted(coder):
    qs = [
        f"kitten food {w}"
        for w in ("plain", "dry", "wet", "small", "large", "senior", "organic", "bulk")
    ]
    qs = [f"{q} x{n}" for n in range(140) for q in qs]  # 1,120 ordinary keys > ROWS_CAP
    d = mc_diffs(qs, coder)
    assert failures(d) == {}, dict(d)
    assert d["n_dropped_past_cap"] > 0


def test_MC_refused_rows_are_listed_with_a_reason_code_and_no_text(coder):
    g, b = source_rows(["kitten food"])
    led = priya.build(
        "acme", "run-p26", "2026-10-29T00:00:00Z", [], gsc(g), bing(b), coder=coder
    )
    lst = led["counts"]["mc"]["refused"]["listed"]
    assert lst and {r["reason"] for r in lst} == {priya.REFUSED_EMPTY_KEY}
    assert all(set(r) == {"source", "index", "reason"} for r in lst)
    assert failures(mc_diffs(["kitten food"], coder)) == {}
