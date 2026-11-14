"""farrah-spec/1 A-FA-01 (money edge cases), O-FA-1..3 (the bound, usefulness, no false insufficiency) and PF-FA-1.

Measurements the spec asks for (O-FA-2's band-width distribution per corpus class, PF-FA-1's time) are printed and,
when CONFORMANCE_MEASURE_DIR is set, written there as JSON."""

from __future__ import annotations

import json
import os
import random
import time
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from agents import farrah
from conformance.gen.attribution import arow, dataset, freshness, reconciliation
from conformance.gen.attribution_truth import corpus, truth_case
from conformance.gen.farrah_util import ok_run, set_map, set_plan

RID = "seo-att-" + "0" * 40
D = date(2026, 9, 12)
HI = Context(prec=600)


def _measure(name: str, obj) -> None:
    print(f"MEASURE {name} {json.dumps(obj, sort_keys=True)}")
    out = os.environ.get("CONFORMANCE_MEASURE_DIR")
    if out:
        with open(os.path.join(out, f"{name}.json"), "w") as fh:
            json.dump(obj, fh, sort_keys=True, indent=1)


# ------------------------------------------------------------------------------------------------ A-FA-01

def _edge_amount(rng):
    fam = rng.choice(("big", "tiny", "refund", "negzero", "exp", "forty", "plain"))
    if fam == "big":
        return fam, f"{rng.randint(10 ** 14, 10 ** 15)}.{rng.randint(0, 99):02d}"
    if fam == "tiny":
        return fam, "0.005"
    if fam == "refund":
        return fam, f"-{rng.randint(1, 5000)}.{rng.randint(0, 99):02d}"
    if fam == "negzero":
        return fam, rng.choice(("-0", "-0.00", "0E+3", "-0e-2"))
    if fam == "exp":
        return fam, rng.choice(("1E+3", "2.5e-3", "-4E2", "7.125E+1", "1e15", "3E-40"))
    if fam == "forty":
        digits = "".join(rng.choice("0123456789") for _ in range(39))
        return fam, rng.choice((f"1{digits}", f"0.{digits}1", f"{digits[:20]}.{digits[20:]}"))
    return fam, f"{rng.randint(1, 999)}.{rng.randint(0, 99):02d}"


def _no_float(x) -> bool:
    if isinstance(x, float):
        return False
    if isinstance(x, dict):
        return all(_no_float(v) for v in x.values())
    if isinstance(x, list):
        return all(_no_float(v) for v in x)
    return True


def _a_fa_01(n, seed) -> float:
    """Returns the slowest run's wall time (A-FA-01's own bound, 5 s, is asserted in the lead lane only)."""
    rng = random.Random(seed)
    worst = 0.0
    for i in range(n):
        rows, want, refunds = [], {"organic_search": Decimal(0), "ai_referral": Decimal(0)}, 0
        for j in range(rng.randint(1, 40)):
            fam, value = _edge_amount(rng)
            ai = rng.random() < 0.3
            rows.append(arow(D, value, "claude.ai" if ai else "google", "referral" if ai else "organic",
                             "Referral" if ai else "Organic Search", f"/{j}"))
            with localcontext(HI):
                want["ai_referral" if ai else "organic_search"] += Decimal(value)
            refunds += (Decimal(value) < 0) and not ai
        if rng.random() < 0.3:
            rows += [arow(D, "0.005", landing=f"/t{k}") for k in range(1000)]
            with localcontext(HI):
                want["organic_search"] += Decimal("5.000")
        doc = dataset("acme", "analytics", rows, rng=rng)
        t0 = time.perf_counter()
        res, facts = farrah.compute("acme", RID, "2026-09", {"basis": "analytics", "dataset": doc, "state": "FRESH",
                                                             "reconcile_difference": None, "other_dataset_id": None,
                                                             "refused": {}}, None, "2026-10-09T00:00:00Z")
        worst = max(worst, time.perf_counter() - t0)
        assert _no_float(res) and _no_float(facts), (i, seed)
        for c in farrah.ATTRIBUTED:
            ch = res["channels"][c]
            if ch["figure"] is not None:
                assert Decimal(ch["figure"]) == want[c], (i, seed, c)               # exact, never rounded
        assert facts["refund_rows"]["organic_search"] == refunds, (i, seed)        # refunds counted and listed
    return worst


def test_A_FA_01_money_edge_cases_fast():
    _a_fa_01(200, 1)


@pytest.mark.lead_gate
def test_A_FA_01_money_edge_cases_lead():
    worst = _a_fa_01(2000, 2)
    _measure("A_FA_01_worst_run_s", {"worst_s": round(worst, 3)})
    assert worst <= 5.0, worst


def test_A_FA_01_a_million_half_cent_rows_sum_exactly_and_floats_are_refused():
    assert farrah.exact_sum(farrah.amount("0.005") for _ in range(10 ** 6)) == Decimal("5000")
    assert farrah.exact_sum([farrah.amount("1E+15"), farrah.amount("0.005")]) == Decimal("1000000000000000.005")
    for bad in (0.005, float("nan"), True, "NaN", "Infinity", "1" * 41, "1e41", "0x10", "1_000", " 1", "\uff11"):
        with pytest.raises(farrah.InputRefused):
            farrah.amount(bad)
    rows = [arow(D, 12.5)]                                                       # a float in a row: refused whole
    with pytest.raises(farrah.InputRefused):
        farrah.validate_dataset(dataset("acme", "analytics", rows), "acme", "analytics")
    big = dataset("acme", "analytics", [arow(D, "0.005", landing=f"/{k}") for k in range(farrah.MAX_ROWS)])
    res, _ = farrah.compute("acme", RID, "2026-09", {"basis": "analytics", "dataset": big, "state": "FRESH",
                                                     "reconcile_difference": None, "other_dataset_id": None,
                                                     "refused": {}}, None, "2026-10-09T00:00:00Z")
    assert res["channels"]["organic_search"]["figure"] == "250"
    assert farrah.display("1234.565") == "1234.56" and farrah.display("1234.575") == "1234.58"   # half-even


# ------------------------------------------------------------------------------------------------ O-FA-1..3

def _install(fa, i, case):
    tid = f"t{i}"
    if tid not in fa.svc.tenants:
        fa.tenant(tid)
    set_plan(fa, tid, "managed_2", case["basis"])
    if case["channel_map"]:
        set_map(fa, tid, case["channel_map"])
    doc = {**case["doc"], "tenant_id": tid}
    fa.store.add(fa.svc, doc)
    t = fa.store.tenant(tid)
    t["freshness"] = freshness(tid, {case["basis"]: "FRESH"})
    if case.get("other") is not None:
        other = {**case["other"], "tenant_id": tid}
        fa.store.add(fa.svc, other)
        item = {**case["rc4"], "datasets": [doc["dataset_id"], other["dataset_id"]]}
        t["reconciliation"] = reconciliation(tid, [item])
    return tid


def _corpus_run(fa, n, seed):
    widths: dict = {}
    useful = [0, 0]
    insufficient_on_complete = 0
    for i, sp in enumerate(corpus(n, seed)):
        case = truth_case(sp["seed"], sp["kind"], "acme", sp["unknown_share"], sp["flags"])
        tid = _install(fa, i, case)
        v = ok_run(fa, tid=tid)
        complete = not any(sp["flags"][k] for k in ("other_row", "thresholded", "dropped", "sampled"))
        for c in farrah.ATTRIBUTED:
            ch = v["result"]["channels"][c]
            truth = case["truth"][c]
            if ch["label"] == "INSUFFICIENT_EVIDENCE":
                assert truth == 0 and case["visible"]["unknown"] == 0, (sp["seed"], c)
                continue
            lo, hi = Decimal(ch["lower"]), Decimal(ch["upper"])
            assert lo <= truth <= hi, ("O-FA-1", sp["seed"], c)                  # 100%: the bound
            key = f"{sp['kind']}:{c}:u<={sp['unknown_share']}"
            with localcontext(HI):
                widths.setdefault(key, []).append(float((hi - lo) / hi) if hi else 0.0)
        if complete:
            insufficient_on_complete += v["result"]["channels"]["organic_search"]["label"] == "INSUFFICIENT_EVIDENCE"
        if sp["unknown_share"] <= 0.05 and not any(sp["flags"].values()):
            useful[1] += 1
            useful[0] += v["result"]["channels"]["organic_search"]["label"] in ("HIGH", "MEDIUM")
    return widths, useful, insufficient_on_complete


def _report(widths, useful, n):
    dist = {}
    for k, xs in sorted(widths.items()):
        xs = sorted(xs)
        dist[k] = {"n": len(xs), "median": round(xs[len(xs) // 2], 4), "p90": round(xs[int(len(xs) * 0.9)], 4),
                   "max": round(xs[-1], 4)}
    _measure(f"O_FA_2_band_width_{n}", {"useful_high_or_medium": useful, "band_width_share_of_upper": dist})


def test_O_FA_1_O_FA_2_O_FA_3_corpus_fast(fa):
    widths, useful, ins = _corpus_run(fa, 50, 3)
    _report(widths, useful, 50)
    assert ins == 0                                                                # O-FA-3
    assert useful[1] == 0 or useful[0] / useful[1] >= 0.95, useful                 # O-FA-2 (organic_search)


@pytest.mark.lead_gate
def test_O_FA_1_O_FA_2_O_FA_3_corpus_lead(fa):
    widths, useful, ins = _corpus_run(fa, 500, 4)
    _report(widths, useful, 500)
    assert ins == 0 and useful[1] > 0 and useful[0] / useful[1] >= 0.95, useful


@pytest.mark.lead_gate
def test_PF_FA_1_a_month_of_50000_rows_within_2_seconds(fa):
    rows = [arow(D, "12.34", "google" if k % 3 else "chatgpt.com", "organic" if k % 3 else "referral",
                 "Organic Search" if k % 3 else "Referral", f"/p/{k}") for k in range(50_000)]
    fa.store.add(fa.svc, dataset("acme", "analytics", rows))
    fa.store.tenant("acme")["freshness"] = freshness("acme")
    best = None
    for k in range(3):
        fa.store.tenant("acme")["freshness"]["panels"][0]["inputs"][0]["state"] = ("FRESH", "DUE", "FAILED_LAST")[k]
        t0 = time.perf_counter()
        v = ok_run(fa)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
        assert v["status"] == "completed"
    _measure("PF_FA_1", {"rows": 50_000, "best_of_3_s": round(best, 3)})
    assert best <= 2.0, best
