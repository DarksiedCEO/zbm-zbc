"""farrah-spec/1 B-FA-04 (inputs validated as consumer, CB-19), AC-11 (forged inter-agent documents) and A-FA-02
(month and timezone games)."""

from __future__ import annotations

import copy
import json
import random
from datetime import date

import pytest

from agents import farrah
from conformance.gen.attribution import FROZEN, arow, dataset, freshness, reconciliation, rc4
from conformance.gen.farrah_util import ok_run, simple

FAST, LEAD = 1000, 20_000


def test_CB_19_frozen_copy_equals_the_consumer_constants():
    c = FROZEN["contracts"]
    assert sorted(farrah.DATASET_KEYS) == c["osei.dataset/1"]["keys"]
    assert sorted(farrah.WINDOW_KEYS) == c["osei.dataset/1"]["window"]
    assert sorted(farrah.COMPLETENESS_KEYS) == c["osei.dataset/1"]["completeness"]
    assert sorted(farrah.ANALYTICS_ROW_KEYS) == c["osei.dataset/1"]["analytics_row"]
    assert sorted(farrah.CRM_ROW_KEYS) == c["osei.dataset/1"]["crm_row"]
    assert sorted(farrah.RECORD_KEYS) == c["records"]["keys"]
    assert farrah.RECORD_KIND == c["records"]["kinds"] and farrah.QUARANTINE_KIND == c["records"]["quarantine_kinds"]
    assert sorted(farrah.RECON_KEYS) == c["osei.reconciliation/1"]["keys"]
    assert list(farrah.FRESHNESS_STATES) == c["osei.panel_freshness/1"]["states"]
    assert farrah.MAX_ROWS == c["osei.dataset/1"]["max_rows"]


def test_B_FA_04_valid_recorded_whole_month_is_used(fa):
    doc = simple(fa)
    v = ok_run(fa)
    assert v["inputs"] == [doc["dataset_id"]] and v["labels"]["organic_search"] == "HIGH"


@pytest.mark.parametrize("start,end,covered", [("2026-09-01", "2026-09-30", True), ("2026-08-15", "2026-10-02", True),
                                               ("2026-09-02", "2026-09-30", False), ("2026-09-01", "2026-09-29", False),
                                               ("2026-08-01", "2026-08-31", False)])
def test_B_FA_04_coverage_at_month_edges(fa, start, end, covered):
    doc = dataset("acme", "analytics", [arow(date(2026, 9, 15), "100.00")], start=date.fromisoformat(start),
                  end=date.fromisoformat(end))
    fa.store.add(fa.svc, doc)
    fa.store.tenant("acme")["freshness"] = freshness("acme")
    v = ok_run(fa)
    if covered:
        assert v["inputs"] and v["result"]["channels"]["organic_search"]["figure"] == "100"
    else:
        assert v["inputs"] == [] and "MONTH_NOT_COVERED" in v["limitations"]
        assert all(c["figure"] is None and c["label"] == "INSUFFICIENT_EVIDENCE"
                   for c in v["result"]["channels"].values())


# ------------------------------------------------------------------------------------------------ AC-11 forgeries

def _valid(rng):
    rows = [arow(date(2026, 9, 1 + i), f"{rng.randint(1, 900)}.{rng.randint(0, 99):02d}") for i in range(5)]
    return dataset("acme", "analytics", rows, rng=rng)


BAD_VALUES = (1.5, float("nan"), float("inf"), True, [], {}, 10 ** 50, "NaN", "Infinity", "1e999", "1,000.00",
              "\u0661\u0662", "\uff11\uff12")


def _paths(doc, prefix=()):
    for k, v in doc.items():
        yield prefix + (k,)
        if isinstance(v, dict) and k in ("window", "surface", "validation", "completeness", "evidence_ref"):
            yield from _paths(v, prefix + (k,))


def forge(rng, doc):
    """One forgery class applied to a valid document; every class here is certainly invalid."""
    d = copy.deepcopy(doc)
    cls = rng.choice(("missing", "extra", "type", "contract", "tenant", "id_unicode", "amount", "row_key",
                      "row_date", "state", "count", "too_many_rows", "nested_missing", "nested_extra"))
    if cls == "missing":
        d.pop(rng.choice(sorted(d)))
    elif cls == "extra":
        d["x" + str(rng.randint(0, 9))] = 1
    elif cls == "type":
        k = rng.choice([p for p in _paths(d) if len(p) == 1 or p[0] in ("window", "evidence_ref")])
        node = d
        for x in k[:-1]:
            node = node[x]
        node[k[-1]] = rng.choice((1.5, True, 10 ** 12))
    elif cls == "contract":
        d["contract"] = rng.choice(("osei.dataset/2", "osei.datasets/1", "callum.mnc_priority/2", "OSEI.DATASET/1"))
    elif cls == "tenant":
        d["tenant_id"] = rng.choice(("globex", "zbm", "acme ", "ACME", "acme\u200b"))
    elif cls == "id_unicode":
        d["dataset_id"] = rng.choice(("ds-\u0430" + d["dataset_id"][4:], "ds-" + "\uff10" * 16,
                                      d["dataset_id"].upper(), d["dataset_id"] + "\u200b"))
    elif cls == "amount":
        d["rows"][rng.randrange(len(d["rows"]))]["purchaseRevenue"] = rng.choice(BAD_VALUES)
    elif cls == "row_key":
        d["rows"][0]["userId"] = "u1"
    elif cls == "row_date":
        d["rows"][0]["date"] = rng.choice(("20261001", "2026-13-01", "yesterday", 20260901, "2026-08-31"))
    elif cls == "state":
        d["validation"]["state"] = rng.choice(("QUARANTINED", "valid", "VALID ", None))
    elif cls == "count":
        d["completeness"]["rows_dropped_cap"] = rng.choice((-1, 10 ** 12, 1.0, "3"))
    elif cls == "too_many_rows":
        d["rows"] = d["rows"] * (farrah.MAX_ROWS // len(d["rows"]) + 1)
    elif cls == "nested_missing":
        d[rng.choice(("window", "surface", "validation", "completeness", "evidence_ref"))].popitem()
    else:
        d[rng.choice(("window", "surface", "validation", "completeness", "evidence_ref"))]["zz"] = 0
    return cls, d


def _ac11_datasets(n, seed):
    rng = random.Random(seed)
    base = _valid(rng)
    farrah.validate_dataset(base, "acme", "analytics")
    for i in range(n):
        cls, d = forge(rng, base)
        try:
            farrah.validate_dataset(d, "acme", "analytics")
            farrah._in_window(d, "analytics")
        except farrah.InputRefused:
            continue
        raise AssertionError(f"AC-11 case {i} class {cls} seed {seed}: accepted")


def test_AC_11_forged_datasets_refused_whole_fast():
    _ac11_datasets(FAST, 11)


@pytest.mark.lead_gate
def test_AC_11_forged_datasets_refused_whole_lead():
    _ac11_datasets(LEAD, 12)


def test_AC_11_forged_reconciliation_and_freshness_refused_whole():
    rng = random.Random(13)
    a, c = _valid(rng), dataset("acme", "crm", [], rng=rng)
    recon = reconciliation("acme", [rc4(a, c, "12.00")])
    fresh = freshness("acme")
    farrah.validate_reconciliation(recon, "acme")
    farrah.validate_freshness(fresh, "acme")
    for i in range(FAST):
        for doc, fn in ((recon, farrah.validate_reconciliation), (fresh, farrah.validate_freshness)):
            d = copy.deepcopy(doc)
            cls = rng.choice(("missing", "extra", "contract", "tenant", "item", "state", "nan"))
            if cls == "missing":
                d.pop(rng.choice(sorted(d)))
            elif cls == "extra":
                d["more"] = []
            elif cls == "contract":
                d["contract"] = d["contract"].replace("/1", "/9")
            elif cls == "tenant":
                d["tenant_id"] = "globex"
            elif cls == "item":
                (d.get("items") or d.get("panels"))[0]["extra"] = 1
            elif cls == "state" and "panels" in d:
                d["panels"][0]["inputs"][0]["state"] = rng.choice(("fresh", "OK", 1))
            elif cls == "nan" and "items" in d:
                d["items"][0]["values"]["difference"] = rng.choice((float("nan"), "NaN", 1.5, "1e999"))
            else:
                d["produced_at"] = rng.choice(("2026-10-05", 1.5, None, "2026-02-30T00:00:00Z"))
            with pytest.raises(farrah.InputRefused):
                fn(d, "acme")


@pytest.mark.parametrize("forgery", ["no_record", "wrong_hmac", "other_tenant_record", "quarantined_read",
                                     "not_valid_record", "retired_producer_kind", "foreign_dataset",
                                     "other_tenant_hmac"])
def test_AC_11_forged_unrecorded_foreign_datasets_never_used(fa, forgery):
    rng = random.Random(14)
    doc = dataset("acme", "analytics", [arow(date(2026, 9, 3), "100.00")], rng=rng)
    t = fa.store.tenant("acme")
    t["datasets"].append(doc)
    from conformance.gen.attribution import record
    rec = record(fa.svc, doc)
    if forgery == "wrong_hmac":
        rec["dataset_hmac"] = "0" * 64
    elif forgery == "other_tenant_record":
        rec["tenant_id"] = "globex"
    elif forgery == "quarantined_read":
        t["records"].append({**rec, "kind": "source_read_quarantined"})
    elif forgery == "not_valid_record":
        rec["validation_state"] = "QUARANTINED"
    elif forgery == "retired_producer_kind":
        rec["kind"] = "gsc_read_recorded"
    elif forgery == "foreign_dataset":
        doc["tenant_id"] = "globex"
    elif forgery == "other_tenant_hmac":
        rec["dataset_hmac"] = fa.svc.vault.field_hash("globex", "dataset", json.dumps(
            doc, sort_keys=True, separators=(",", ":")))[1]
    if forgery != "no_record":
        t["records"].append(rec)
    t["freshness"] = freshness("acme")
    v = ok_run(fa)
    assert v["inputs"] == [] and v["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert all(c["figure"] is None for c in v["result"]["channels"].values())
    facts = fa.svc.attribution_runs[v["run_id"]]["facts"]
    assert sum(facts["inputs_refused"].values()) == 1


# ------------------------------------------------------------------------------------------------ A-FA-02

def _edge_rows():
    """Rows at the edges of September 2026 in America/Los_Angeles, in every form a row's time can take."""
    return [("2026-09-01T00:00:00-07:00", True), ("2026-08-31T23:59:59-07:00", False),
            ("2026-09-30T23:59:59-07:00", True), ("2026-10-01T00:00:00-07:00", False),
            ("2026-09-01T06:59:59Z", False), ("2026-09-01T07:00:00Z", True),
            ("2026-10-01T06:59:59Z", True), ("2026-10-01T07:00:00Z", False),
            ("20260901", True), ("2026-09-30", True), ("2026-08-31", False), ("2026-10-01", False),
            ("2026-09-30T23:59:59", True), ("2026-09-01T00:00:00", True)]


def test_A_FA_02_each_row_in_exactly_one_month_of_the_dataset_timezone():
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Los_Angeles")
    for value, in_sep in _edge_rows():
        assert (farrah.month_of(farrah.local_day(value, tz)) == "2026-09") is in_sep, value


def _a_fa_02(fa, n, seed):
    rng = random.Random(seed)
    edge = _edge_rows()
    for i in range(n):
        tz = rng.choice(("America/Los_Angeles", "America/New_York", "UTC", "Pacific/Auckland"))
        rows, want = [], 0
        for value, _ in rng.sample(edge, 6):
            rows.append(arow(value, "1.00"))
        dst = rng.choice(("2026-03-08T02:30:00-08:00", "2026-11-01T01:30:00-07:00", "2026-11-01T01:30:00-08:00"))
        rows.append(arow(dst, "1.00"))
        from zoneinfo import ZoneInfo
        for r in rows:
            want += farrah.month_of(farrah.local_day(r["date"], ZoneInfo(tz))) == "2026-09"
        doc = dataset("acme", "analytics", rows, tz=tz, start=date(2026, 2, 1), end=date(2026, 11, 30), rng=rng)
        sel = {"basis": "analytics", "dataset": doc, "state": "FRESH", "reconcile_difference": None,
               "other_dataset_id": None, "refused": {}}
        res, facts = farrah.compute("acme", "seo-att-" + "0" * 40, "2026-09", sel, None, "2026-10-09T00:00:00Z")
        assert facts["rows"]["organic_search"] == want, (i, seed)
        mismatched = dataset("acme", "analytics", rows, tz=tz, property_tz="Europe/Paris", rng=rng)
        assert not farrah.covers(mismatched, "2026-09")                     # mismatched timezones: not covered


def test_A_FA_02_month_and_timezone_games_fast(fa):
    _a_fa_02(fa, 100, 21)


@pytest.mark.lead_gate
def test_A_FA_02_month_and_timezone_games_lead(fa):
    _a_fa_02(fa, 1000, 22)


def test_A_FA_02_mismatched_timezone_dataset_month_not_covered(fa):
    simple(fa, property_tz="America/New_York")
    v = ok_run(fa)
    assert "MONTH_NOT_COVERED" in v["limitations"] and v["inputs"] == []
