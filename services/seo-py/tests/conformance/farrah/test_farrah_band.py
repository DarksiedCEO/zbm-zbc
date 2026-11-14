"""farrah-spec/1 section 3: the figure, the band, the label, the contract and the month rules (B-FA-20..29)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Context, Decimal, localcontext

import pytest

from agents import farrah
from conformance.gen.attribution import arow, dataset, reconciliation, rc4
from conformance.gen.attribution_truth import corpus, truth_case
from conformance.gen.farrah_util import ok_run, run, simple

D = date(2026, 9, 10)
RID = "seo-att-" + "0" * 40


def sel_of(doc, state="FRESH", recon=None, other=None):
    return {"basis": doc["source"], "dataset": doc, "state": state, "reconcile_difference": recon,
            "other_dataset_id": other, "refused": {}}


def comp(doc, **kw):
    cm = kw.pop("channel_map", None)
    return farrah.compute("acme", RID, "2026-09", sel_of(doc, **kw), cm, "2026-10-09T00:00:00Z")


def oracle(case) -> dict:
    """Decimal recomputation of F, L, U from the generator's own knowledge (never from the module)."""
    with localcontext(Context(prec=300)):
        return _oracle(case)


def _oracle(case) -> dict:
    v, doc = case["visible"], case["doc"]
    widen = case["other_row_sum"] if case["other_row_sum"] > 0 else Decimal(0)
    share = doc["completeness"]["other_row_share"]
    if case["other_row_sum"] == 0 and share is not None and Decimal(share) > 0:
        widen += Decimal(share) * Decimal(doc["totals"]["purchaseRevenue"])
    widen += case["dropped_sum"]
    out = {}
    for c in farrah.ATTRIBUTED:
        out[c] = (v[c], v[c] + v["unknown"] + widen)
    return out


def test_B_FA_20_B_FA_21_oracle_recomputation_on_generated_datasets():
    specs = corpus(1000, seed=20)
    for sp in specs:
        case = truth_case(sp["seed"], sp["kind"], "acme", sp["unknown_share"], {**sp["flags"], "reconcile": False})
        res, facts = comp(case["doc"], channel_map=case["channel_map"])
        want = oracle(case)
        for c in farrah.ATTRIBUTED:
            ch = res["channels"][c]
            lo, hi = want[c]
            if ch["label"] == "INSUFFICIENT_EVIDENCE":
                assert hi == 0 and case["truth"][c] == 0, (sp["seed"], c)
                continue
            assert Decimal(ch["figure"]) == Decimal(ch["lower"]) == lo, (sp["seed"], c)       # F = L exactly
            assert Decimal(ch["upper"]) == hi, (sp["seed"], c)
            assert lo <= case["truth"][c] <= hi, (sp["seed"], c)                            # O-FA-1 property
    assert res["data_class"] == "measured" and res["effect_class"] == "attributed"


@pytest.mark.xfail(strict=True, reason="QB-FA-6: U = F + R_unknown is not an upper bound when the unknown revenue "
                                      "holds a refund (the spec's formula, built as written)")
def test_B_FA_21_refund_in_the_unknown_channel_keeps_truth_in_band():
    rows = [arow(D, "100.00", landing="/a"), arow(D, "40.00", "(not set)", "(not set)", "Unassigned", "(not set)"),
            arow(D, "-30.00", "(not set)", "(not set)", "Unassigned", "(not set)")]
    res, _ = comp(dataset("acme", "analytics", rows))
    truth_if_both_unknown_rows_were_organic_but_the_refund_was_not = Decimal("140.00")
    assert Decimal(res["channels"]["organic_search"]["upper"]) >= truth_if_both_unknown_rows_were_organic_but_the_refund_was_not


def _ladder(n, f_per_row, unk):
    rows = [arow(D, f_per_row, landing=f"/{i}") for i in range(n)]
    if unk != "0":
        rows.append(arow(D, unk, "(not set)", "(not set)", "Unassigned", "(not set)"))
    return dataset("acme", "analytics", rows)


@pytest.mark.parametrize("n,per,unk,state,want", [
    (30, "9", "30", "FRESH", "HIGH"),             # width = 10% of U exactly, 30 rows
    (30, "9", "30.01", "FRESH", "MEDIUM"),        # just over 10%
    (29, "9", "29", "FRESH", "MEDIUM"),           # 10% but 29 rows
    (30, "9", "30", "DUE", "MEDIUM"),             # not fresh: never HIGH
    (10, "13", "70", "FRESH", "MEDIUM"),          # width = 35% of U exactly, 10 rows
    (10, "13", "70.01", "FRESH", "LOW"),          # just over 35%
    (9, "13", "63", "FRESH", "LOW"),              # 35% but 9 rows
    (40, "10", "0", "FRESH", "HIGH"),
    (40, "10", "0", "STALE", "INSUFFICIENT_EVIDENCE"),
    (40, "10", "0", "FAILED_LAST", "INSUFFICIENT_EVIDENCE"),
    (40, "10", "0", "NOT_CONNECTED", "INSUFFICIENT_EVIDENCE"),
    (40, "10", "0", None, "MEDIUM"),              # Osei states no freshness: not shown CONNECTED and fresh
    (0, "1", "0", "FRESH", "INSUFFICIENT_EVIDENCE"),   # U = 0: nothing to attribute
])
def test_B_FA_25_label_table_boundaries(n, per, unk, state, want):
    res, _ = comp(_ladder(n, per, unk), state=state)
    ch = res["channels"]["organic_search"]
    assert ch["label"] == want, (n, per, unk, state, ch)
    if want == "INSUFFICIENT_EVIDENCE":
        assert ch["figure"] is None and ch["lower"] is None and ch["upper"] is None       # never 0
    assert farrah.THRESHOLDS["heuristic"] is True and farrah.THRESHOLDS["calibrated"] is False


def test_B_FA_25_no_basis_dataset_is_insufficient():
    res, facts = farrah.compute("acme", RID, "2026-09", {"basis": "analytics", "dataset": None, "state": None,
                                                         "reconcile_difference": None, "other_dataset_id": None,
                                                         "refused": {}}, None, "2026-10-09T00:00:00Z")
    assert all(c["label"] == "INSUFFICIENT_EVIDENCE" and c["figure"] is None for c in res["channels"].values())
    assert facts["outcome"] == "INSUFFICIENT_EVIDENCE" and "MONTH_NOT_COVERED" in facts["limitations"]


def _flagged(**kw):
    rows = [arow(D, "10.00", landing=f"/{i}") for i in range(40)]
    rows.append(arow(D, "4.00", "(not set)", "(not set)", "Unassigned", "(not set)"))
    if kw.pop("other_row", False):
        rows.append(arow(D, "6.00", "(other)", "(other)", "(other)", "(other)"))
    return dataset("acme", "analytics", rows, **kw)


@pytest.mark.parametrize("kw,upper,label,codes", [
    ({}, "404", "HIGH", []),
    ({"other_row": True}, "410", "MEDIUM", ["incomplete:other_row_share"]),
    ({"other_row_share": "0.02", "totals": {"purchaseRevenue": "500"}}, "414", "MEDIUM",
     ["incomplete:other_row_share"]),
    ({"thresholded": True}, "404", "MEDIUM", ["incomplete:thresholded"]),
    ({"sampled": True}, "404", "LOW", ["incomplete:sampled"]),
    ({"rows_dropped_cap": {"rows": 3, "revenue": "25.50"}}, "429.5", "MEDIUM", ["incomplete:rows_dropped_cap"]),
    ({"rows_dropped_cap": {"rows": 3, "revenue": None}}, "404", "LOW", ["incomplete:rows_dropped_cap"]),
    ({"rows_dropped_cap": 2}, "404", "LOW", ["incomplete:rows_dropped_cap"]),
])
def test_B_FA_22_B_FA_23_completeness_and_cap_widening(kw, upper, label, codes):
    res, _ = comp(_flagged(**kw))
    ch = res["channels"]["organic_search"]
    assert (ch["lower"], ch["upper"], ch["label"]) == ("400", upper, label), ch
    assert [a for a in res["assumptions"] if a.startswith("incomplete:")] == codes


def test_B_FA_24_reconciliation_widening(fa):
    doc = simple(fa, n_org=40, n_ai=10, n_unk=0, rev="10.00")              # F(org) 400, F(ai) 100, total 500
    crm = fa.store.add(fa.svc, dataset("acme", "crm", []))
    t = fa.store.tenant("acme")
    t["reconciliation"] = reconciliation("acme", [rc4(doc, crm, "-50.00")])
    v = ok_run(fa)
    org = v["result"]["channels"]["organic_search"]
    assert (org["lower"], org["upper"]) == ("400", "440")                   # 400 + |-50| x 400/500
    assert v["result"]["channels"]["ai_referral"]["upper"] == "110"
    assert "RECONCILE_DIFFERENCE_WIDENED" in v["assumptions"] and v["inputs"] == [doc["dataset_id"], crm["dataset_id"]]
    t["reconciliation"] = reconciliation("acme", [rc4(doc, crm, "0", result="NOT_COMPARABLE: NO_ANALYTICS_REVENUE")])
    v = ok_run(fa)
    assert v["result"]["channels"]["organic_search"]["upper"] == "400"     # NOT_COMPARABLE adds nothing
    assert "RECONCILE_DIFFERENCE_WIDENED" not in v["assumptions"] and v["supersedes"] is not None
    other_month = rc4(doc, crm, "-50.00", month="2026-08")
    t["reconciliation"] = reconciliation("acme", [other_month])
    v = ok_run(fa)
    assert v["result"]["channels"]["organic_search"]["upper"] == "400"     # not this month: never used


def test_B_FA_26_CB_5_no_numeric_confidence_score(fa):
    simple(fa)
    v = ok_run(fa)
    keys: list = []

    def walk(x):
        if isinstance(x, dict):
            for k, y in x.items():
                keys.append(k)
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(v)
    walk(fa.ok(fa.get("/tenants/acme/attribution-runs", caller="compliance_38")))
    assert not [k for k in keys if re.search(r"score|grade|percent|rating|confidence", k, re.I)]
    for c in v["result"]["channels"].values():
        assert set(c) == {"figure", "lower", "upper", "label", "rows"} and c["label"] in farrah.LABELS


def test_B_FA_27_assumptions_listed_in_every_result():
    res, _ = comp(_flagged())
    assert res["assumptions"] == ["ATTRIBUTED_NOT_INCREMENTAL", "SESSION_SCOPED_LAST_CLICK",
                                  "CRM_SOURCE_FIELDS_AS_RECORDED", "AI_REFERRERS_LIST_NOT_COMPLETE",
                                  "AI_ANSWERS_OFTEN_SEND_NO_REFERRER", "UNKNOWN_COUNTED_IN_UPPER_BOUND"]
    crm, _ = farrah.compute("acme", RID, "2026-09", sel_of(dataset("acme", "crm", [])), None, "2026-10-09T00:00:00Z")
    assert "SESSION_SCOPED_LAST_CLICK" not in crm["assumptions"] and len(crm["assumptions"]) == 5


def test_B_FA_28_result_contract_strict_keys(fa):
    simple(fa)
    v = ok_run(fa)
    doc = v["result"]
    assert set(doc) == set(farrah.RESULT_KEYS) and farrah.validate_result(doc)
    assert doc["contract"] == "farrah.revenue_attribution/1" and doc["refresh_tier"] == "monthly"
    assert set(doc["totals"]) == {"basis_revenue", "unknown_revenue"} and doc["month"] == "2026-09"
    assert not farrah.validate_result({**doc, "score": 1}) and not farrah.validate_result(
        {k: x for k, x in doc.items() if k != "inputs"})
    assert json.loads(json.dumps(doc)) == doc


@pytest.mark.parametrize("now,closed", [("2026-10-04T06:59:59+00:00", False),   # LA: Oct 3 23:59
                                        ("2026-10-04T07:00:00+00:00", True),    # LA: Oct 4 00:00
                                        ("2026-09-30T12:00:00+00:00", False), ("2026-12-01T00:00:00+00:00", True)])
def test_B_FA_29_only_a_closed_month(fa, now, closed):
    simple(fa)
    fa.clock.at = datetime.fromisoformat(now).astimezone(timezone.utc)
    r = run(fa)
    if closed:
        assert r.status_code == 201, r.text
    else:
        fa.refused(r, 422, "MONTH_NOT_CLOSED")
        assert not fa.ledger.of_type("attribution_run_requested")


def test_B_FA_29_changed_basis_dataset_supersedes(fa):
    simple(fa)
    first = ok_run(fa)
    again = ok_run(fa)
    assert again["run_id"] == first["run_id"]                             # unchanged inputs: the current run
    newer = dataset("acme", "analytics", [arow(D, "5.00", landing=f"/{i}") for i in range(12)],
                    produced_at="2026-10-08T12:00:00Z")
    fa.store.add(fa.svc, newer)
    second = ok_run(fa)
    assert second["supersedes"] == first["run_id"] and second["inputs"] == [newer["dataset_id"]]
    old = fa.ok(fa.get(f"/tenants/acme/attribution-runs/{first['run_id']}"))
    assert old["superseded_by"] == second["run_id"] and old["result"] is not None     # the old run stays
