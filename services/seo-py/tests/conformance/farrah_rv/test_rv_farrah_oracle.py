"""Reviewer oracle over the real service path: B-FA-20..25 (F, L, U, label exact) and O-FA-1 (truth in [L, U]).
RV_N cases (default 2,000), RV_SEED0 the first seed. A failure prints seeds, families and machine words only."""

from __future__ import annotations

import collections
import os

from gen_rv_farrah import CHANNEL_MAP, make_case, oracle
from rv_service import compare, install_case, make_harness, post_run, put_map, put_plan

N = int(os.environ.get("RV_N", "2000"))
SEED0 = int(os.environ.get("RV_SEED0", "1000"))


def _run_cases(tmp_path, literal=False):
    h = make_harness(tmp_path)
    h.tenant("acme")
    assert put_map(h, "acme", CHANNEL_MAP).status_code == 200
    basis_now = None
    stats = collections.Counter()
    bad, outside, failed_runs, assumptions_bad = [], [], [], []
    for seed in range(SEED0, SEED0 + N):
        case = make_case(seed)
        if case.basis != basis_now:
            assert put_plan(h, "acme", "managed_2", case.basis).status_code == 200
            basis_now = case.basis
        install_case(h, case)
        r = post_run(h, "acme", case.month)
        if r.status_code != 201:
            bad.append((seed, case.family, f"http:{r.status_code}"))
            continue
        run = h.get(f"/tenants/acme/attribution-runs/{r.json()['run_id']}").json()
        if run["status"] != "completed":
            failed_runs.append((seed, case.family, run.get("interrupted_reason"),
                                (run.get("failure") or {}).get("where") if isinstance(run, dict) else None))
            continue
        res = run["result"]
        exp = oracle(case, literal=literal)
        diff = compare(res, exp)
        stats[case.family] += 1
        if diff:
            bad.append((seed, case.family, diff))
        # O-FA-1: truth in [L, U]; round 3 (OD-2): refund cases are checked too (only sampled cases are excluded).
        # OD-5 (GF-8): L = F + negative part of unknown rows, so L holds for every case too; the cases where the
        # truth is below F (the 32 round-3 failures: the channel's own hidden rows net negative) are counted and
        # must lie inside [L, U] like every other case.
        for c in ("organic_search", "ai_referral"):
            e = exp[c]
            if e["label"] == "INSUFFICIENT_EVIDENCE" or e["lower"] is None:
                continue
            stats["truth_checked"] += 1
            if case.comp["sampled"]:
                stats["truth_excluded"] += 1
                continue
            t = case.truth[c]
            from fractions import Fraction
            lo, up = Fraction(res["channels"][c]["lower"]), Fraction(res["channels"][c]["upper"])
            if case.hidden_negative:
                stats["truth_checked_hidden_refund"] += 1
            if t > up:
                outside.append((seed, case.family, c, "above_U"))
            if t < Fraction(res["channels"][c]["figure"]):
                stats["truth_below_F_checked"] += 1                  # was outside L = F before OD-5
                if case.hidden_net[c] < 0:
                    stats["truth_below_F_hidden_net_refund"] += 1
            if t < lo:
                outside.append((seed, case.family, c, "share_nototal" if (case.comp["other_row_share"] is not None
                                                                          and not case.has_share_total) else "below_L"))
        stats["label:" + exp["organic_search"]["label"]] += 1
        has_recon = "RECONCILE_DIFFERENCE_WIDENED" in res["assumptions"]
        if has_recon and case.recon_d in (None, "0"):
            assumptions_bad.append((seed, "recon_listed_without_difference"))
    return stats, bad, outside, failed_runs, assumptions_bad


def test_rv_oracle_service_path(tmp_path):
    stats, bad, outside, failed, abad = _run_cases(tmp_path)
    print("RV-STATS", dict(stats))
    print("RV-BAD", len(bad), bad[:40])
    print("RV-OUTSIDE", len(outside), outside[:40])
    print("RV-FAILED", len(failed), failed[:40])
    print("RV-ASSUMPTIONS", len(abad), abad[:20])
    assert not bad and not failed and not abad
    # R-2 (outside the spec: share given without Osei totals) is reported, not asserted
    assert not [o for o in outside if o[3] != "share_nototal"]
    assert stats["truth_checked_hidden_refund"] > 0
    assert stats["truth_below_F_checked"] > 0                            # OD-5: the pre-fix L failures are pinned
    assert stats["truth_below_F_hidden_net_refund"] > 0
