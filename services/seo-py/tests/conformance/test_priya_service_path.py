"""priya-spec/1 section 0 (Z-1, Z-2) through the real service path: fake Search Console AND Bing rows -> Priya ->
the POST response, the run views, the audit export, every file under the data directory, every log record and the
ledger events. CI: 1,000 list rows (a stratified draw over every matrix cell) and 1,000 ordinary rows; no list value,
raw or as its stored key, reaches any output."""

from __future__ import annotations

import json
import logging

import priya_gen as g
import pytest
from bing_fakes import FakeBing
from bing_fakes import row as bing_row
from gsc_fakes import FakeGoogle
from helpers import rid
from oracle import priya_list as o
from test_gsc import PROP, bind, gsc_harness

from agents import priya

pytestmark = pytest.mark.conformance


def _list_rows(n: int) -> list:
    cells = g.cells()
    return [g.make(cells[i % len(cells)], g.cell_rng(cells[i % len(cells)], 1000 + i)) for i in range(n)]


def _run(tmp_path, caplog, list_rows, ordinary):
    fake = FakeGoogle()
    half = len(list_rows) // 2
    fake.queries = [(t, 1, 30, 3.0) for t in dict.fromkeys(list_rows[:half] + ordinary)]
    fake.dates_clicks = (10, 150)
    h = gsc_harness(tmp_path, fake, data_dir=str(tmp_path / "data"), SEO_GSC_MAX_ROWS="250000",
                    SEO_GSC_PAGE_ROWS="25000", SEO_GSC_MAX_PAGES="12")
    bing = FakeBing()
    bing.rows = [bing_row(t, 1, 20, i % 2) for i, t in enumerate(dict.fromkeys(list_rows[half:] + ordinary[:200]))]
    h.ports.first_party["bing_webmaster"] = bing.client()
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    caplog.set_level(logging.DEBUG)
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid(), "sources": ["gsc", "bing"],
                                                       "bing_site_domain": "acme.test"}, caller="seo_agent"), 201)
    views = [out, h.ok(h.get("/tenants/acme/keyword-ledger-runs")), h.ok(h.get("/audit/export",
                                                                                 params={"limit": 1000}))]
    blob = {"api": json.dumps(views, ensure_ascii=False),
            "logs": caplog.text + json.dumps([e.get("_payload") for e in h.ledger.events], ensure_ascii=False,
                                             default=str),
            "disk": "".join(p.read_bytes().decode("utf-8", "replace") for p in (tmp_path / "data").rglob("*")
                            if p.is_file())}
    return out["ledger"], blob


def test_Z1_Z2_service_path_list_rows_reach_no_output(tmp_path, caplog):
    list_rows = _list_rows(1000)
    ordinary = [q for _c, _i, q in list(g.ordinary())[::24]][:1000]
    led, blob = _run(tmp_path, caplog, list_rows, ordinary)
    assert led["status"] == "OK" and led["counts"]["rows_dropped_past_cap"] == 0
    assert led["sources"]["bing_webmaster"]["state"] == "OK" and led["sources"]["search_console"]["state"] == "OK"
    texts = {r["query"]["text"] for r in led["rows"]}
    keys = {priya.normalize(t) for t in list_rows}
    assert not texts & keys, len(texts & keys)
    needles = {x for t in list_rows for x in (t, priya.normalize(t)) if len(x) >= 6}
    leaks = {out: sum(1 for n in needles if n in b) for out, b in blob.items()}
    assert leaks == {"api": 0, "logs": 0, "disk": 0}, leaks
    kept_ordinary = [q for q in ordinary if priya.normalize(q) in texts]
    assert len(kept_ordinary) >= 500                                       # the path is live: ordinary rows kept
    assert not any(o.mandated(q) for q in kept_ordinary)                   # and no kept row holds a list value


@pytest.mark.conformance_heavy
def test_Z2_service_path_heavy_12000_list_rows(tmp_path, caplog):
    list_rows = _list_rows(12000)
    ordinary = [q for _c, _i, q in list(g.ordinary())[::12]][:2000]
    led, blob = _run(tmp_path, caplog, list_rows, ordinary)
    texts = {r["query"]["text"] for r in led["rows"]}
    assert not texts & {priya.normalize(t) for t in list_rows}
    needles = {x for t in list_rows for x in (t, priya.normalize(t)) if len(x) >= 6}
    assert {out: sum(1 for n in needles if n in b) for out, b in blob.items()} == {"api": 0, "logs": 0, "disk": 0}
