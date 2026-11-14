"""Round 2 (review of 3b99c9d5): F-1 exact absolute value (B-FA-24, A-FA-01), F-2 a restricted / retired producer
(AC-11, CB-19, CB-2), F-3 a depth and size bound before any recursion (AC-11 "0 raises", UB), F-4 records that never
depend on an id's own digits (CB-15 D-07, UB)."""

from __future__ import annotations

import copy
import json
import random
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

import sink_guard
import svc_attribution
from agents import farrah
from conformance.gen.attribution import arow, crow, dataset, freshness, reconciliation, rc4
from conformance.gen.farrah_util import ok_run, rid, run, set_map, set_plan, simple

D = date(2026, 9, 9)


# ------------------------------------------------------------------------------------------------ F-1

@pytest.mark.parametrize("diff", ["-1234567890123456789012345678.91", "9876543210987654321098765432109876543.21",
                                  "-0.000000000000000000000000000000000001", "1E+39", "-3141592653589793238462643383"])
def test_B_FA_24_A_FA_01_reconciliation_difference_exact_at_every_magnitude(fa, diff):
    doc = simple(fa, n_org=3, n_ai=1, n_unk=1, rev="300.00", unk_rev="100.00")    # F = 900, total 1300
    other = fa.store.add(fa.svc, dataset("acme", "crm", []))
    fa.store.tenant("acme")["reconciliation"] = reconciliation("acme", [rc4(doc, other, diff)])
    v = ok_run(fa)
    got = Fraction(Decimal(v["result"]["channels"]["organic_search"]["upper"]))
    term = abs(Fraction(Decimal(diff))) * 900 / 1300                # the oracle: exact rationals
    want = 1000 + term
    # GF-4: the one division is rounded at precision 40, half-even; nothing else is rounded (F-1)
    ulp = Fraction(10) ** (Decimal(term.numerator / term.denominator).adjusted() - 39)
    assert abs(got - want) <= ulp / 2, diff


def test_B_FA_24_no_builtin_on_the_money_path():
    """abs / sum / round / float on a Decimal use (or leave) the thread's 28-digit context: never on the money path."""
    import inspect
    import re
    src = inspect.getsource(farrah.select) + inspect.getsource(farrah.compute) + inspect.getsource(farrah.exact_sum)
    assert not re.search(r"(?<![\w.])(abs|sum|round|float)\(", src)


# ------------------------------------------------------------------------------------------------ F-2

def _move(fa, to, expected):
    r = fa.post("/agents/osei/lifecycle", {"request_id": rid(), "to": to, "expected_state": expected,
                                           "reason": "FOUNDER_DECISION"}, caller="dashboard", andre=True)
    assert r.status_code == 200, r.text[:200]


@pytest.mark.parametrize("states", [("restricted",), ("restricted", "retired")])
def test_AC_11_CB_19_a_restricted_or_retired_producer_is_refused_through_the_lifecycle_route(fa, states):
    doc = simple(fa)
    other = fa.store.add(fa.svc, dataset("acme", "crm", []))
    fa.store.tenant("acme")["reconciliation"] = reconciliation("acme", [rc4(doc, other, "-50.00")])
    before = ok_run(fa)                                       # active: the dataset is used
    assert before["inputs"]
    prev = "active"
    for to in states:
        _move(fa, to, prev)
        prev = to
    v = ok_run(fa)
    assert v["inputs"] == [] and v["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "AGENT_RESTRICTED" in v["limitations"] and v["supersedes"] == before["run_id"]
    assert all(c["figure"] is None for c in v["result"]["channels"].values())
    facts = fa.svc.attribution_runs[v["run_id"]]["facts"]
    assert facts["inputs_refused"] == {"AGENT_RESTRICTED": 2} and facts["input_refs"] == []
    req = fa.ledger.of_type("attribution_run_requested")[-1]["_payload"]
    assert req["input_refs"] == []                           # nothing of Osei's documents is recorded
    if states == ("restricted",):
        _move(fa, "active", "restricted")
        assert ok_run(fa)["inputs"] == before["inputs"]      # active again: used again


def test_AC_11_a_producer_restricted_mid_run_is_refused_at_commit(fa, monkeypatch):
    simple(fa)
    real = farrah.compute

    def compute(*a, **k):
        _move(fa, "restricted", "active")
        return real(*a, **k)
    monkeypatch.setattr(farrah, "compute", compute)
    v = ok_run(fa)
    assert v["status"] == "interrupted" and v["interrupted_reason"] == "AGENT_RESTRICTED"
    assert not fa.ledger.of_type("attribution_run_recorded")


# ------------------------------------------------------------------------------------------------ F-3

def _deep(depth, leaf=None, key="a", as_list=False):
    x = cur = ([] if as_list else {})
    for _ in range(depth):
        nxt = [] if as_list else {}
        if as_list:
            cur.append(nxt)
        else:
            cur[key] = nxt
        cur = nxt
    return x


def _install_raw(fa, docs, records=None, recon=None, fresh=None):
    t = fa.store.tenant("acme")
    t["datasets"], t["records"] = docs, records if records is not None else []
    t["reconciliation"], t["freshness"] = recon, fresh


@pytest.mark.parametrize("depth", [3000, 10_000, 100_000])
def test_AC_11_UB_deep_surface_parts_refused_whole_no_raise(fa, depth):
    good = simple(fa)
    fa.store.tenant("acme")["datasets"] = []
    d = copy.deepcopy(good)
    d["surface"]["parts"]["deep"] = _deep(depth)
    rec = {**fa.store.tenant("acme")["records"][0], "dataset_id": d["dataset_id"]}
    _install_raw(fa, [d], [rec], fresh=freshness("acme"))
    r = run(fa)
    assert r.status_code == 201, r.status_code
    v = r.json()
    assert v["status"] == "completed" and v["inputs"] == []
    assert fa.svc.attribution_runs[v["run_id"]]["facts"]["inputs_refused"] == {"DATASET_INVALID": 1}


class _Weird:
    pass


def _pathological(rng, good, rec):
    k = rng.choice(("deep_rows", "deep_list", "deep_records", "deep_recon", "deep_fresh", "cycle", "int_key",
                    "object", "bytes", "wide_parts", "set", "tuple", "deep_completeness", "huge_labels",
                    "deep_totals", "nan_float"))
    d = copy.deepcopy(good)
    recs, recon, fresh = [dict(rec)], None, freshness("acme")
    depth = rng.choice((40, 3000, 20_000))
    if k == "deep_rows":
        d["rows"][0]["landingPage"] = _deep(depth)
    elif k == "deep_list":
        d["labels"] = _deep(depth, as_list=True)
    elif k == "deep_records":
        recs = [_deep(depth)] + recs
    elif k == "deep_recon":
        recon = _deep(depth)
    elif k == "deep_fresh":
        fresh = {"contract": "osei.panel_freshness/1", "x": _deep(depth)}
    elif k == "cycle":
        d["surface"]["parts"]["self"] = d["surface"]["parts"]
    elif k == "int_key":
        d["surface"]["parts"][7] = "x"
    elif k == "object":
        d["surface"]["parts"]["o"] = _Weird()
    elif k == "bytes":
        d["rows"][0]["sessionSource"] = b"google"
    elif k == "wide_parts":
        d["surface"]["parts"].update({f"k{i}": i for i in range(5000)})
    elif k == "set":
        d["labels"] = {"incomplete:sampled"}
    elif k == "tuple":
        d["rows"] = tuple(d["rows"])
    elif k == "deep_completeness":
        d["completeness"]["rows_dropped_cap"] = _deep(depth)
    elif k == "huge_labels":
        d["labels"] = ["incomplete:sampled"] * 100_000
    elif k == "deep_totals":
        d["totals"] = {"purchaseRevenue": _deep(depth)}
    else:
        d["rows"][0]["purchaseRevenue"] = float("nan")
    return k, d, recs, recon, fresh


def test_AC_11_UB_pathological_shapes_never_raise(fa):
    good = simple(fa)
    rec = fa.store.tenant("acme")["records"][0]
    rng = random.Random(31)
    seen = set()
    for i in range(120):
        k, d, recs, recon, fresh = _pathological(rng, good, rec)
        seen.add(k)
        _install_raw(fa, [d], recs, recon, fresh)
        r = run(fa)
        assert r.status_code == 201, (i, k, r.status_code)
        v = r.json()
        assert v["status"] == "completed", (i, k)
        assert v["inputs"] in ([], [good["dataset_id"]]), (i, k)
    assert len(seen) >= 12


def test_AC_11_bounded_is_iterative_and_exact():
    farrah.bounded(_deep(farrah.MAX_DEPTH - 1), "X")
    with pytest.raises(farrah.InputRefused):
        farrah.bounded(_deep(farrah.MAX_DEPTH + 1), "X")
    with pytest.raises(farrah.InputRefused):
        farrah.bounded(_deep(1_000_000), "X")                 # never a RecursionError
    with pytest.raises(farrah.InputRefused):
        farrah.bounded(list(range(farrah.MAX_NODES + 1)), "X")


# ------------------------------------------------------------------------------------------------ F-4

DIGITS = "0123456789"


def _all_digit(h: str) -> str:
    return "".join(DIGITS[int(c, 16) % 10] for c in h)


def test_CB_15_D_07_all_digit_dataset_id_is_attributable(fa):
    did = "ds-" + "3141592653589793"
    rows = [arow(D, "10.00", landing=f"/{i}") for i in range(12)]
    doc = dataset("acme", "analytics", rows)
    doc = {**doc, "dataset_id": did, "read_id": "read-" + did[3:]}
    fa.store.add(fa.svc, doc)
    fa.store.tenant("acme")["freshness"] = freshness("acme")
    v = ok_run(fa)
    assert v["status"] == "completed" and v["inputs"] == [did]


def test_CB_15_D_07_forced_all_digit_ids_in_every_id_bearing_field(fa, monkeypatch):
    """Run id, dataset refs, the inputs reference, the result's and the channel map's keyed HMACs, all forced to
    decimal digits only: every commit is taken, every read verifies, nothing is refused at the sink."""
    did = "ds-" + "2718281828459045"
    rows = [crow(D, "100.00", lead_source="Google Search", ref=f"r{i}") for i in range(12)]
    doc = {**dataset("acme", "crm", rows), "dataset_id": did, "read_id": "read-2718281828459045"}
    fa.store.add(fa.svc, doc)                                # Osei recorded it before the forcing
    fa.store.tenant("acme")["freshness"] = freshness("acme", {"crm": "FRESH"})
    real_derived = svc_attribution.derived_id
    monkeypatch.setattr(svc_attribution, "derived_id", lambda p, *parts: "seo-%s-%s" % (
        p, _all_digit(real_derived(p, *parts)[8:])))
    for v in (fa.svc.vault, fa.svc.optext_vault):
        real_fh = v.field_hash
        monkeypatch.setattr(v, "field_hash", lambda t, f, x, _r=real_fh: (_r(t, f, x)[0], _all_digit(_r(t, f, x)[1])))
    set_plan(fa, "acme", "managed_2", "crm")
    assert set_map(fa, "acme", {"organic_search": ["Google Search"]}).status_code == 200
    v = ok_run(fa)
    assert v["status"] == "completed" and set(v["run_id"][8:]) <= set(DIGITS)
    assert v["inputs"] == [did] and v["result"]["channels"]["organic_search"]["figure"] == "1200"
    assert fa.ok(fa.get(f"/tenants/acme/attribution-runs/{v['run_id']}"))["result"] is not None
    recs = [json.loads(x) for x in fa.svc.log.raw_lines()]
    for r in recs:
        if r["kind"].startswith(("attribution_run", "tenant_channel_map", "tenant_plan")):
            assert not sink_guard.violations(r["kind"], r["data"], stored=True), r["kind"]
    real = svc_attribution.mac_field("1" * 64)
    assert real.startswith("seo-att-") and sink_guard.classify(real) == "service_id"


def test_CB_15_mac_ok_reads_both_recorded_forms(fa):
    obj = {"v": 1}
    mac = fa.svc.vault.field_hash("acme", "revenue_attribution", svc_attribution.canonical(obj))[1]
    for recorded in (mac, svc_attribution.ref("hmac", mac)):
        assert svc_attribution.mac_ok(fa.svc.vault, "acme", "revenue_attribution", obj, recorded)
        assert not svc_attribution.mac_ok(fa.svc.vault, "acme", "revenue_attribution", {"v": 2}, recorded)
        assert not svc_attribution.mac_ok(fa.svc.vault, "globex", "revenue_attribution", obj, recorded)
