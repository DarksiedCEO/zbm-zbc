"""Reviewer probes at the edges of B-FA-21..25 and A-FA-01 (printed outcomes; asserts only where the spec is clear)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from gen_rv_farrah import ns, oracle
from rv_service import install_case, make_harness, post_run, put_plan

M = "2026-09"


def arow(day, rev, src="google", med="organic", grp="Organic Search", land="/"):
    return {"date": day, "sessionSource": src, "sessionMedium": med, "sessionDefaultChannelGroup": grp,
            "landingPage": land, "purchaseRevenue": rev, "sessions": 1}


UNK = dict(src="(not set)", med="(not set)", grp="Unassigned", land="(not set)")


def doc(rows, comp=None, totals=None, labels=None, did="ds-00000000000000a1", tz="UTC", start="2026-09-01",
        end="2026-09-30"):
    comp = comp or {"sampled": False, "thresholded": False, "other_row_share": None, "rows_dropped_cap": 0}
    return {"contract": "osei.dataset/1", "tenant_id": "acme", "dataset_id": did, "source": "analytics",
            "read_id": "read-" + did[3:], "window": {"start": start, "end": end, "timezone": tz}, "grain": "day",
            "surface": {"fingerprint": did[3:], "parts": {"property_settings": {"timezone": tz}}},
            "validation": {"state": "VALID", "reason": None, "counts": {}}, "completeness": comp,
            "totals": totals or {}, "rows": rows, "produced_at": "2026-10-05T12:00:00Z",
            "refresh_tier": "first_party_daily",
            "evidence_ref": {"run_id": "r1", "contract": "osei.dataset/1", "item_key": "rows"},
            "data_class": "measured", "labels": labels or []}


def crmdoc(did):
    d = doc([], did=did)
    d["source"] = "crm"
    return d


def case(d, recon=None, state="FRESH", other=None):
    return ns(basis="analytics", month=M, basis_doc=d, other_doc=other, recon_d=recon, state=state, cmap={},
              covers=True)


def run_case(h, c):
    install_case(h, c)
    r = post_run(h, "acme", M)
    assert r.status_code == 201, r.status_code
    return h.get(f"/tenants/acme/attribution-runs/{r.json()['run_id']}").json()


def H(tmp_path):
    h = make_harness(tmp_path)
    h.tenant("acme")
    assert put_plan(h, "acme").status_code == 200
    return h


def test_rv_inexact_extreme_magnitudes(tmp_path):
    """Amounts the builder ACCEPTS (40 digits, |exponent| <= 40) mixed at both extremes, with a reconciliation
    difference: exact result or a refusal, never a failed run."""
    h = H(tmp_path)
    tiny = "1." + "2" * 39 + "e-40"
    rows = [arow("20260905", "1e40"), arow("20260906", tiny), arow("20260907", "1", **UNK)]
    d = doc(rows)
    other = crmdoc("ds-00000000000000b2")
    run = run_case(h, case(d, recon="9" * 40, other=other))
    print("RV-INEXACT-UP", run["result"]["channels"]["organic_search"]["upper"] if run.get("result") else None)
    print("RV-INEXACT", run["status"], run.get("interrupted_reason"), (run.get("failure") or {}).get("where"))
    run2 = run_case(h, case(doc(rows, did="ds-00000000000000a3",
                                comp={"sampled": False, "thresholded": True,
                                      "other_row_share": "0." + "3" * 39 + "1", "rows_dropped_cap": 0},
                                labels=["incomplete:thresholded", "incomplete:other_row_share"])))
    print("RV-INEXACT-SHARE", run2["status"], run2.get("interrupted_reason"))


def test_rv_share_without_totals_truth(tmp_path):
    """Share documented relative to the true total, Osei giving no totals: is truth still <= U?"""
    h = H(tmp_path)
    rows = [arow("20260905", "100.00")]          # visible organic 100; hidden (thresholded) organic 100: share 0.5
    d = doc(rows, comp={"sampled": False, "thresholded": True, "other_row_share": "0.5", "rows_dropped_cap": 0},
            labels=["incomplete:thresholded", "incomplete:other_row_share"])
    run = run_case(h, case(d))
    up = Fraction(run["result"]["channels"]["organic_search"]["upper"])
    print("RV-SHARE-NOTOTAL upper==150", up == 150, "truth 200 inside", up >= 200)


def test_rv_negative_widening_terms(tmp_path):
    """Spec text: U += the (other) row's revenue / dropped revenue / |D| x F/total. Negative terms: literal vs
    clamped."""
    h = H(tmp_path)
    rows = [arow("20260905", "100.00"), arow("20260906", "-30.00", "(other)", "(other)", "(other)", "(other)")]
    d = doc(rows, labels=["incomplete:other_row_share"])
    c = case(d)
    run = run_case(h, c)
    got = run["result"]["channels"]["organic_search"]
    lit, clamp = oracle(c, literal=True)["organic_search"], oracle(c, literal=False)["organic_search"]
    print("RV-NEG-OTHER upper==literal", Fraction(got["upper"]) == lit["upper"], "==clamped",
          Fraction(got["upper"]) == clamp["upper"])
    # net refunds: F(c) < 0 with a reconciliation difference
    rows = [arow("20260905", "-100.00"), arow("20260906", "300.00", "newsletter", "email", "Email", "/n")]
    d2 = doc(rows, did="ds-00000000000000c4")
    other = crmdoc("ds-00000000000000c5")
    c2 = case(d2, recon="50", other=other)
    run = run_case(h, c2)
    got = run["result"]["channels"]["organic_search"]
    lit = oracle(c2, literal=True)["organic_search"]
    print("RV-NEG-RECON label", got["label"], "upper==literal", Fraction(got["upper"]) == lit["upper"],
          "assumption", "RECONCILE_DIFFERENCE_WIDENED" in run["result"]["assumptions"])


def test_rv_zero_rows_and_u_negative(tmp_path):
    h = H(tmp_path)
    rows = [arow("20260905", "0") for _ in range(40)] + [arow("20260906", "10.00")]
    run = run_case(h, case(doc(rows)))
    ch = run["result"]["channels"]["organic_search"]
    print("RV-ZERO-ROWS rows", ch["rows"], "label", ch["label"])
    rows = [arow("20260905", "-50.00")]
    run = run_case(h, case(doc(rows, did="ds-00000000000000d6")))
    ch = run["result"]["channels"]["organic_search"]
    print("RV-U-NEG label", ch["label"], "figure_is_null", ch["figure"] is None)
    rows = [arow("20260905", "50.00"), arow("20260906", "-50.00")]
    run = run_case(h, case(doc(rows, did="ds-00000000000000d7")))
    ch = run["result"]["channels"]["organic_search"]
    assert ch["figure"] is None and ch["label"] == "INSUFFICIENT_EVIDENCE"     # U = 0 -> null, never 0


def test_rv_inexact_oracle(tmp_path):
    h = H(tmp_path)
    tiny = "1." + "2" * 39 + "e-40"
    rows = [arow("20260905", "1e40"), arow("20260906", tiny), arow("20260907", "1", **UNK)]
    c = case(doc(rows), recon="9" * 40, other=crmdoc("ds-00000000000000b2"))
    run = run_case(h, c)
    got = run["result"]["channels"]["organic_search"]
    exp = oracle(c)["organic_search"]
    print("RV-DBG", run["result"]["channels"], run["result"]["totals"], run["result"]["assumptions"], run["result"]["inputs"])
    print("RV-INEXACT-ORACLE", Fraction(got["upper"]) == exp["upper"], Fraction(got["lower"]) == exp["lower"])


@pytest.mark.rv_finding
def test_rv_recon_difference_30_digits_exact(tmp_path):
    """B-FA-24 with a 30-significant-digit difference (inside A-FA-01's 40-digit class): the only rounding allowed
    is the division's (GF-4: precision 40, half-even)."""
    h = H(tmp_path)
    rows = [arow("20260905", "300.00"), arow("20260907", "100.00", **UNK)]
    c = case(doc(rows), recon="-1234567890123456789012345678.91", other=crmdoc("ds-00000000000000b2"))
    run = run_case(h, c)
    got = run["result"]["channels"]["organic_search"]
    exp = oracle(c)["organic_search"]
    print("RV-RECON30", Fraction(got["upper"]) == exp["upper"], float(Fraction(got["upper"]) - exp["upper"]))
    assert Fraction(got["upper"]) == exp["upper"]
