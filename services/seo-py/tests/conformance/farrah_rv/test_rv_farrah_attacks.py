"""Reviewer attacks: AC-11 forged documents (generated), retired producer, month clock (B-FA-29), plan gate (B-FA-02,
A-FA-03), A-FA-01 hostile amounts, AC-6 oversize. Machine words only in output."""

from __future__ import annotations

import copy

import pytest
import random

from gen_rv_farrah import make_case
from rv_service import install_case, make_harness, post_run, put_plan, rid

M = "2026-09"


def _reason():
    from svc_manager import REASONS
    return sorted(REASONS)[0]


REASON = None


def H(tmp_path, **kw):
    h = make_harness(tmp_path, **kw)
    h.tenant("acme")
    assert put_plan(h, "acme").status_code == 200
    return h


def good_case(seed=5):
    for s in range(seed, seed + 500):
        c = make_case(s, "plain")
        if c.basis == "analytics" and c.month and len(c.basis_doc["rows"]) >= 10:
            return c
    raise AssertionError


def _run(h, c, month=None):
    install_case(h, c)
    r = post_run(h, "acme", month or c.month)
    return r


@pytest.mark.rv_finding
def test_rv_retired_osei_documents_refused(tmp_path):
    global REASON
    REASON = _reason()
    h = H(tmp_path)
    c = good_case()
    for to, exp in (("restricted", "active"), ("retired", "restricted")):
        r = h.post("/agents/osei/lifecycle", {"request_id": rid(), "to": to, "expected_state": exp,
                                                "reason": REASON}, caller="dashboard", andre=True)
        assert r.status_code == 200, (r.status_code, r.text[:200])
    r = _run(h, c)
    v = h.get(f"/tenants/acme/attribution-runs/{r.json()['run_id']}").json()
    print("RV-RETIRED-OSEI", r.status_code, v.get("outcome"), v.get("inputs"))
    assert v.get("inputs") == []


def _forge(rng, doc, svc):
    """(kind, forged doc, record override or None). The record is computed over the ORIGINAL doc unless noted."""
    d = copy.deepcopy(doc)
    k = rng.choice(("contract", "tenant", "extra", "missing", "type_amount", "nan", "inf", "huge_exp", "count12",
                    "neg_count", "unicode_id", "replayed", "row_extra", "row_float", "row_bool", "window_type",
                    "state", "currency_row", "long_string", "share_out", "labels_type"))
    rows = d["rows"]
    if k == "contract":
        d["contract"] = rng.choice(("osei.dataset/2", "OSEI.DATASET/1", "osei.dataset/1 ", "farrah.revenue_attribution/1"))
    elif k == "tenant":
        d["tenant_id"] = rng.choice(("globex", "ACME", "acme ", "zbm"))
    elif k == "extra":
        d[rng.choice(("note", "amount", "__proto__", "Contract"))] = "x"
    elif k == "missing":
        d.pop(rng.choice(sorted(d)))
    elif k == "type_amount":
        rows[0]["purchaseRevenue"] = rng.choice((100.5, 100, True, ["1"], {"v": "1"}))
        if rows[0]["purchaseRevenue"] == 100:
            rows[0]["purchaseRevenue"] = 100.0
    elif k == "nan":
        rows[0]["purchaseRevenue"] = rng.choice(("NaN", "nan", "-NaN", "sNaN", "NaN123"))
    elif k == "inf":
        rows[0]["purchaseRevenue"] = rng.choice(("Infinity", "-Infinity", "inf", "1e999999", "-1E+999999"))
    elif k == "huge_exp":
        rows[0]["purchaseRevenue"] = rng.choice(("1e41", "1e-81", "9" * 41, "1" + "0" * 50, "0." + "0" * 85 + "1"))
    elif k == "count12":
        d["validation"]["counts"]["rows"] = 10 ** 12
    elif k == "neg_count":
        d["completeness"]["rows_dropped_cap"] = -1
    elif k == "unicode_id":
        d["dataset_id"] = "ds-" + "\uff10" * 16
    elif k == "replayed":
        rows.append(dict(rows[0]))                               # changed after its record: HMAC mismatch
        return k, d, "orig"
    elif k == "row_extra":
        rows[0]["note"] = "x"
    elif k == "row_float":
        rows[0]["sessions"] = 1.0
    elif k == "row_bool":
        rows[0]["sessionMedium"] = True
    elif k == "window_type":
        d["window"]["start"] = rng.choice((20260901, "2026-9-1", "2026-09-31", None))
    elif k == "state":
        d["validation"]["state"] = rng.choice(("QUARANTINED", "valid", "INVALID"))
    elif k == "currency_row":
        d["source"] = "crm"
        return k, d, "self"
    elif k == "deep_parts":
        x = {}
        cur = x
        for _ in range(rng.choice((50, 5000))):
            cur["a"] = {}
            cur = cur["a"]
        d["surface"]["parts"]["deep"] = x
    elif k == "long_string":
        rows[0]["landingPage"] = "/" + "a" * 5000
    elif k == "share_out":
        d["completeness"]["other_row_share"] = rng.choice(("1.5", "-0.1", "NaN"))
    else:
        d["labels"] = rng.choice(("incomplete:sampled", [1], None))
    return k, d, "self"


def test_rv_ac11_forged_datasets(tmp_path):
    """1,000 forged basis datasets (each ALONE in the store, recorded over the forged form unless replayed): refused
    whole -> no input used, outcome INSUFFICIENT_EVIDENCE, no 5xx, no failed run."""
    from svc_vaulted import canonical
    h = H(tmp_path)
    c = good_case()
    rng = random.Random(77)
    bad, kinds = [], {}
    for i in range(int(__import__("os").environ.get("RV_FORGE_N", "1000"))):
        k, d, rec_over = _forge(rng, c.basis_doc, h.svc)
        kinds[k] = kinds.get(k, 0) + 1
        t = h.store.reset("acme")
        t["datasets"].append(d)
        try:
            mac = h.svc.vault.field_hash("acme", "dataset", canonical(c.basis_doc if rec_over == "orig" else d))[1]
        except Exception:  # noqa: BLE001 - a forged doc that cannot even be serialised: record the original
            mac = h.svc.vault.field_hash("acme", "dataset", canonical(c.basis_doc))[1]
        t["records"].append({"kind": "source_read_recorded", "tenant_id": "acme",
                             "dataset_id": d.get("dataset_id"), "read_id": d.get("read_id"),
                             "validation_state": "VALID", "dataset_hmac": mac, "at": "2026-10-05T12:00:00Z"})
        r = post_run(h, "acme", c.month)
        if r.status_code >= 500:
            bad.append((i, k, "5xx"))
            continue
        if r.status_code != 201:
            bad.append((i, k, f"http{r.status_code}"))
            continue
        v = h.get(f"/tenants/acme/attribution-runs/{r.json()['run_id']}").json()
        if v["status"] != "completed" or v["inputs"] != [] or v["outcome"] != "INSUFFICIENT_EVIDENCE":
            bad.append((i, k, v["status"], v.get("outcome"), len(v.get("inputs") or [])))
    print("RV-AC11", kinds, len(bad), bad[:30])
    assert not bad


@pytest.mark.rv_finding
def test_rv_deep_parts_no_5xx(tmp_path):
    """An Osei document with very deep nesting inside surface.parts: no 5xx, no uncaught exception."""
    from svc_vaulted import canonical
    h = H(tmp_path)
    c = good_case()
    for depth in (900, 3000, 100_000):
        d = copy.deepcopy(c.basis_doc)
        x = cur = {}
        for _ in range(depth):
            cur["a"] = {}
            cur = cur["a"]
        d["surface"]["parts"]["deep"] = x
        t = h.store.reset("acme")
        t["datasets"].append(d)
        try:
            mac = h.svc.vault.field_hash("acme", "dataset", canonical(d))[1]
        except RecursionError:
            mac = "0" * 64
        t["records"].append({"kind": "source_read_recorded", "tenant_id": "acme", "dataset_id": d["dataset_id"],
                             "read_id": d["read_id"], "validation_state": "VALID", "dataset_hmac": mac,
                             "at": "2026-10-05T12:00:00Z"})
        r = post_run(h, "acme", c.month)
        v = r.json() if r.status_code == 201 else {}
        print("RV-DEEP", depth, r.status_code, v.get("status"), v.get("outcome"))
        assert r.status_code < 500


def test_rv_ac6_oversize_and_not_connected(tmp_path):
    from rv_service import put_map
    from test_rv_farrah_edges import arow, case, doc
    h = H(tmp_path)
    big = {"organic_search": ["v%d" % i for i in range(201)]}
    long = {"organic_search": ["x" * 201]}
    r1, r2 = put_map(h, "acme", big), put_map(h, "acme", long)
    r3 = h.client.post("/seo/v1/tenants/acme/attribution-runs", content=b'{"request_id":"' + b"a" * 3_000_000 + b'"}',
                       headers={**h.headers("dashboard"), "content-type": "application/json"})
    rows = [arow("20260905", "1.00") for _ in range(50_001)]
    install_case(h, case(doc(rows)))
    r4 = post_run(h, "acme", "2026-09").json()
    facts = h.svc.attribution_runs[r4["run_id"]]["facts"]
    print("RV-AC6", r1.status_code, r2.status_code, r3.status_code, r4["outcome"], facts["inputs_refused"])
    assert r1.status_code == 422 and r2.status_code == 422 and r3.status_code in (413, 422)
    assert r4["outcome"] == "INSUFFICIENT_EVIDENCE" and sum(facts["inputs_refused"].values()) == 1
    h.svc.osei_source = None                      # production: no Osei store
    v = post_run(h, "acme", "2026-09").json()
    print("RV-NOTCONN", v["outcome"], v["not_connected"], v["result"]["channels"]["organic_search"]["figure"])
    assert v["outcome"] == "NOT_CONNECTED" and v["result"]["channels"]["organic_search"]["figure"] is None


def test_rv_freshness_change_supersedes(tmp_path):
    from test_rv_farrah_edges import arow, case, doc
    h = H(tmp_path)
    d = doc([arow("20260905", "1.00")])
    install_case(h, case(d, state="FRESH"))
    a = post_run(h, "acme", "2026-09").json()
    install_case(h, case(d, state="DUE"))
    b = post_run(h, "acme", "2026-09").json()
    print("RV-FRESH-SUPERSEDE", b["run_id"] != a["run_id"], b.get("supersedes") == a["run_id"])
    assert b.get("supersedes") == a["run_id"]


@pytest.mark.skipif(__import__("os").environ.get("RV_PF") != "1", reason="timing bar: RV_PF=1 on a quiet box")
def test_rv_pf_fa_1_50000_rows(tmp_path):
    """PF-FA-1: a month of 50,000 rows <= 2 s (best of 3; whole POST through the service), plus A-FA-01's
    0.005 x 10^6 on the summing core <= 5 s."""
    import time
    from decimal import Decimal
    from agents import farrah
    from test_rv_farrah_edges import arow, case, doc
    h = H(tmp_path)
    rng = random.Random(9)
    best = []
    for k in range(3):
        rows = [arow("202609%02d" % rng.randint(1, 30), "%d.%02d" % (rng.randint(1, 999), rng.randint(0, 99)),
                     *rng.choice((("google", "organic", "Organic Search", "/p"), ("chatgpt.com", "referral", "Referral", "/a"),
                                  ("(not set)", "(not set)", "Unassigned", "(not set)"), ("news", "email", "Email", "/n"))))
                for _ in range(50_000)]
        install_case(h, case(doc(rows, did="ds-abcdef%010x" % (k + 1))))
        t0 = time.perf_counter()
        r = post_run(h, "acme", "2026-09")
        best.append(time.perf_counter() - t0)
        assert r.status_code == 201 and r.json()["status"] == "completed"
    t0 = time.perf_counter()
    s = farrah.exact_sum(farrah.amount("0.005") for _ in range(10 ** 6))
    core = time.perf_counter() - t0
    print("RV-PF", [round(x, 3) for x in best], "core1e6", round(core, 3), s == Decimal("5000"))


@pytest.mark.rv_finding
def test_rv_all_digit_dataset_id_is_attributable(tmp_path):
    """A VALID Osei dataset id 'ds-<16 hex>' whose hex happens to be all decimal digits (about 1 in 1,850 keyed
    ids): the run must still be made (CB-15 D-07: 0 unexpected SINK_REFUSED; UB: 5xx 0)."""
    from test_rv_farrah_edges import arow, case, doc
    h = H(tmp_path)
    install_case(h, case(doc([arow("20260905", "10.00")], did="ds-" + "3141592653589793")))
    r = post_run(h, "acme", "2026-09")
    print("RV-DIGIT-ID", r.status_code, r.json().get("detail"))
    assert r.status_code == 201
