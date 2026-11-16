"""priya-spec/2 section 5 POS (positive control, Wave-1 D-06 analog) and section 4 Z-SVC, through the real service path
(fake Search Console rows -> POST /tenants/acme/keyword-ledger -> the POST run response, its ledger and the run views)
on the frozen ORDINARY (OQ) corpora at their CI draws (I-27: all 6, rv21_sw included).

POS (a) every function-level READABLE key appears once with its key text in the ledger rows and in the POST run
response body; (b) every WITHHELD key appears as exactly one coded row (its own store code) carrying its own metrics,
also once in the ledger's withheld view; (c) 0 rows missing, 0 rows duplicated.
Z-SVC the service READABLE set == the function READABLE set (priya_common.readable, T empty), case for case.
Synthetic metrics only: every key gets a unique impression count, so a metric identifies its row. Counts are printed,
never query text."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from corpora import ORDINARY, ci_rows
from gsc_fakes import FakeGoogle
from helpers import rid
from priya_common import readable
from test_gsc import PROP, bind, gsc_harness
from test_priya import VAULT_ENV

import vault
from agents import priya
from primitives import codes

pytestmark = pytest.mark.conformance

BATCH = 900  # distinct keys per run: below priya.ROWS_CAP, so no row is dropped past the cap
CI_POS_N = 1800  # keys per corpus in the CI lane (2 runs each); the full corpora are a lead job


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    # P2-5 / V-2: synthetic vault key (F-7: the service refuses 503 VAULT_UNAVAILABLE without one)
    for k, v in VAULT_ENV.items():
        monkeypatch.setenv(k, v)


def draw(name: str, n: int) -> list:
    """Distinct-key CI draw of a corpus: (key, first raw spelling), strided, non-empty keys only."""
    seen: dict = {}
    for _i, _k, q in ci_rows(name):
        key = priya.normalize(q)
        if key and key not in seen:
            seen[key] = q
    items = list(seen.items())
    return items[:: max(1, len(items) // n)][:n]


def _code(key: str) -> str:
    with codes.coding(
        vault.Coder(vault.load_vault_key(VAULT_ENV), "acme"), own=(), evidence=None
    ):
        return codes.code("query", key)


def service_run(tmp_path, items: list) -> tuple:
    """One service run over (key, raw) items; impressions 1000 + i identify item i. Returns (response, body text)."""
    fake = FakeGoogle()
    fake.queries = [(q, 1 + i % 7, 1000 + i, 3.0) for i, (_k, q) in enumerate(items)]
    fake.dates_clicks = (10, 150)
    h = gsc_harness(
        tmp_path,
        fake,
        data_dir=str(tmp_path / "data"),
        SEO_GSC_MAX_ROWS="250000",
        SEO_GSC_PAGE_ROWS="25000",
        SEO_GSC_MAX_PAGES="12",
    )
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    out = h.ok(
        h.post(
            "/tenants/acme/keyword-ledger",
            {"request_id": rid(), "sources": ["gsc"]},
            caller="seo_agent",
        ),
        201,
    )
    runs = h.ok(h.get("/tenants/acme/keyword-ledger-runs"))
    return out, json.dumps(out, ensure_ascii=False), runs


def pos_diffs(tmp_path, items: list) -> Counter:
    """Every POS / Z-SVC difference of one run, by kind (0 everywhere = pass)."""
    d: Counter = Counter()
    out, body, runs = service_run(tmp_path, items)
    led = out["ledger"]
    d["status_not_ok"] += led["status"] != "OK"
    d["dropped_past_cap"] += led["counts"]["rows_dropped_past_cap"]
    imp = {1000 + i: k for i, (k, _q) in enumerate(items)}
    f_read = {k for k, q in items if readable(q)}
    f_with = {k for k, _q in items} - f_read
    # (a) READABLE: ledger rows, POST run response body
    texts = Counter(r["query"]["text"] for r in led["rows"])
    s_read = set(texts)
    d["a_missing_in_ledger"] += len(f_read - s_read)
    d["a_dup_in_ledger"] += sum(c - 1 for c in texts.values())
    d["a_missing_in_response"] += sum(
        1 for k in f_read if json.dumps(k, ensure_ascii=False) not in body
    )
    for r in led["rows"]:
        gm = (r["metrics"] or {}).get("search_console") or {}
        d["a_metrics_wrong"] += imp.get(gm.get("impressions")) != r["query"]["text"]
    # Z-SVC: case for case (both directions)
    d["zsvc_more_readable_in_service"] += len(s_read - f_read)
    d["zsvc_less_readable_in_service"] += len(f_read - s_read)
    # (b) WITHHELD: exactly one coded row per key, own code, own metrics; once in the ledger's withheld view
    want = {_code(k): k for k in f_with}
    got = Counter(r["code"] for r in led["withheld_rows"])
    d["b_missing"] += len(set(want) - set(got))
    d["b_dup"] += sum(c - 1 for c in got.values())
    d["b_foreign_code"] += len(set(got) - set(want))
    for r in led["withheld_rows"]:
        gm = (r["metrics"] or {}).get("search_console") or {}
        d["b_metrics_wrong"] += imp.get(gm.get("impressions")) != want.get(r["code"])
        d["b_reason_missing"] += not r.get("reason")
    view = Counter(x["code"] for g in led["withheld_view"]["groups"] for x in g["rows"])
    d["b_view_mismatch"] += sum((view - got).values()) + sum((got - view).values())
    d["b_view_total_wrong"] += led["withheld_view"]["totals"]["queries"] != len(f_with)
    # (c) 0 missing, 0 duplicated over both parts; the runs view carries the same withheld total
    d["c_missing"] += len(items) - len(s_read | set(want[c] for c in got if c in want))
    d["c_dup"] += len(s_read & {want[c] for c in got if c in want})
    rv = [r for r in runs if r["run_id"] == out["run_id"]]
    d["c_runs_view_total_wrong"] += len(rv) != 1 or rv[0]["withheld_view"]["totals"][
        "queries"
    ] != len(f_with)
    print(
        f"POS run: n={len(items)} READABLE={len(f_read)} WITHHELD={len(f_with)} "
        f"service_READABLE={len(s_read)} withheld_rows={sum(got.values())}"
    )
    return d


def failures(d: Counter) -> dict:
    return {k: v for k, v in d.items() if v}


@pytest.mark.parametrize("name", sorted(ORDINARY))
def test_POS_a_b_c_and_Z_SVC_on_each_OQ_corpus_ci_draw(name, tmp_path):
    items = draw(name, CI_POS_N)
    assert len(items) >= min(CI_POS_N, 300), (name, len(items))
    total: Counter = Counter()
    for j in range(0, len(items), BATCH):
        (tmp_path / f"b{j}").mkdir()
        total += pos_diffs(tmp_path / f"b{j}", items[j : j + BATCH])
    print(f"POS {name}: n={len(items)} differences={sum(total.values())}")
    assert failures(total) == {}, (name, failures(total))


def test_POS_is_live_both_parts_present(tmp_path):
    """Positive control of the control: one draw holds READABLE and WITHHELD keys, so both checks bite."""
    items = draw("oq", 400)
    f_read = sum(1 for _k, q in items if readable(q))
    assert 0 < f_read < len(items), (len(items), f_read)
    assert failures(pos_diffs(tmp_path, items)) == {}
