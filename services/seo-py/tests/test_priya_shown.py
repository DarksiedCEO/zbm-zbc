"""P2-5 (priya-spec/2 F-8, F-9, OW-5, OW-8, ROWS_CAP, hand-off contract): what is SHOWN of a WITHHELD row.
Ledger / API / run views / audit export: a READABLE row shows its text; a WITHHELD row shows "Withheld query {code}"
+ metrics, grouped per page and per reason family (I-21: OG = OG-1..OG-8, S2 = stage 2), with totals; the reason code
is in the API, the client sees one generic line only. Never revealable (no route, no owner override). The EMERGING
hand-off carries a WITHHELD row as code + metrics only; a READABLE-only hand-off is byte-identical (same sha).
Synthetic values only."""

from __future__ import annotations

import json
import re

import pytest
from test_priya import VAULT_ENV
from test_priya_service import QUERIES, run, setup
from test_priya_withheld import CODE_RE, bing, coder, gsc

from agents import priya

LINE = "may contain personal data"
OG_Q, S2_Q = (
    "zzqxvbt kitten",
    "12345 2019",
)  # OG-6 and S2:pii_suspect (test_priya_verdict / test_priya_withheld)
WITHHELD_TEXTS = (OG_Q, S2_Q, "zzqxvbt", "jane.doe", "example.com")


@pytest.fixture(autouse=True)
def vault_key(monkeypatch):
    """The service refuses 503 VAULT_UNAVAILABLE without a codes key (F-7): synthetic key from test_priya."""
    for k, v in VAULT_ENV.items():
        monkeypatch.setenv(k, v)


def _build(extra_gsc=(), tenant="acme"):
    g = gsc(
        [
            ("kitten food", 9, 400, 0.02, 3.0),
            (OG_Q, 2, 31, 0.06, 4.0),
            (S2_Q, 1, 11, 0.09, 5.0),
            *extra_gsc,
        ]
    )
    b = bing([(OG_Q, "2026-10-05", 1, 7, None, 2.5)])
    return priya.build(
        tenant, "run-p25", "2026-10-29T00:00:00Z", [], g, b, coder=coder(tenant)
    )


def test_withheld_view_groups_per_page_and_reason_family_with_totals_and_the_generic_line():
    led = _build()
    view = led["withheld_view"]
    assert (
        view["shown_reason"] == LINE
        and view["label"] == "Withheld query {code}"
        and view["revealable"] is False
    )
    fams = {g["family"]: g for g in view["groups"]}
    assert set(fams) == {"OG", "S2"} and all(
        g["page"] is None for g in view["groups"]
    )  # I-19: no page dimension
    assert fams["OG"]["totals"] == {
        "queries": 1,
        "clicks": 3,
        "impressions": 38,
    }  # GSC + Bing, as counts.withheld
    assert fams["S2"]["totals"] == {"queries": 1, "clicks": 1, "impressions": 11}
    w = led["counts"]["withheld"]
    assert view["totals"] == {k: w[k] for k in ("queries", "clicks", "impressions")}
    for fam, reason in (("OG", "OG-6"), ("S2", "S2:pii_suspect")):
        (row,) = fams[fam]["rows"]
        assert (
            CODE_RE.match(row["code"])
            and row["label"] == f"Withheld query {row['code']}"
        )
        assert (
            row["reason"] == reason
            and row["shown_reason"] == LINE
            and fams[fam]["shown_reason"] == LINE
        )
        assert set(row) == {
            "label",
            "code",
            "reason",
            "shown_reason",
            "tier",
            "metrics",
        }
    assert {r["query"]["text"] for r in led["rows"] if r["origin"] == "observed"} == {
        "kitten food"
    }
    blob = json.dumps([view, led["withheld_rows"], led["handoff"]], ensure_ascii=False)
    for s in WITHHELD_TEXTS:
        assert s not in blob, s


def test_totals_only_view_has_no_rows_and_no_codes():
    led = _build()
    tv = priya.withheld_view(led["withheld_rows"], rows=False)
    assert all("rows" not in g for g in tv["groups"]) and "{c:query:" not in json.dumps(
        tv
    )
    assert tv["totals"] == led["withheld_view"]["totals"]
    assert priya.withheld_view([]) == {
        "label": "Withheld query {code}",
        "shown_reason": LINE,
        "revealable": False,
        "groups": [],
        "totals": {"queries": 0, "clicks": 0, "impressions": 0},
    }


def test_reason_family_is_closed_over_every_reason_code():
    assert {priya.reason_family(r) for r in priya.REASON_CODES} == set(
        priya.REASON_FAMILIES
    )
    assert [priya.reason_family(f"OG-{n}") for n in range(1, 9)] == ["OG"] * 8


def test_handoff_carries_withheld_emerging_rows_as_code_and_metrics_only():
    led = _build()
    doc = led["handoff"]
    assert priya.validate_handoff(doc) == []
    assert doc["withheld_contract"] == priya.HANDOFF_WITHHELD_CONTRACT
    codes = {r["code"] for r in led["withheld_rows"] if r["tier"] == "EMERGING"}
    est = {r["code"] for r in led["withheld_rows"] if r["tier"] == "ESTABLISHED"}
    assert (
        {it["code"] for it in doc["withheld_items"]} == codes
        and len(codes) == 1
        and len(est) == 1
    )  # EMERGING only
    for it in doc["withheld_items"]:
        assert set(it) == {
            "code",
            "observed_counts",
        }  # no text, no key, no reason (F-9)
        assert set(it["observed_counts"]) == {
            "impressions",
            "clicks",
            "date_range",
            "source",
        }
    assert all(not CODE_RE.match(str(i["key"])) for i in doc["items"])


def test_readable_only_handoff_is_unchanged_and_its_sha_is_stable():
    g = gsc([("kitten food", 9, 400, 0.02, 3.0), ("kitten toys", 1, 12, 0.08, 6.0)])
    led = priya.build(
        "acme", "run-p25", "2026-10-29T00:00:00Z", [], g, bing([]), coder=None
    )
    doc = led["handoff"]
    assert (
        led["withheld_rows"] == []
        and "withheld_items" not in doc
        and "withheld_contract" not in doc
    )
    old = priya.handoff("acme", "run-p25", "2026-10-29T00:00:00Z", led["rows"])
    assert doc == old and priya.handoff_sha(doc) == priya.handoff_sha(old)
    assert priya.validate_handoff(doc) == []


@pytest.mark.parametrize(
    "bad, problem",
    [
        (
            {
                "withheld_items": [
                    {"code": "{c:query:0123456789abcdef}", "observed_counts": None}
                ]
            },
            "withheld_contract",
        ),
        (
            {
                "withheld_contract": priya.HANDOFF_WITHHELD_CONTRACT,
                "withheld_items": [],
            },
            "withheld_items",
        ),
        (
            {
                "withheld_contract": priya.HANDOFF_WITHHELD_CONTRACT,
                "withheld_items": [
                    {
                        "code": "{c:query:0123456789abcdef}",
                        "observed_counts": None,
                        "query": "x",
                    }
                ],
            },
            "withheld_items[0].keys",
        ),
        (
            {
                "withheld_contract": priya.HANDOFF_WITHHELD_CONTRACT,
                "withheld_items": [{"code": "kitten food", "observed_counts": None}],
            },
            "withheld_items[0].code",
        ),
        (
            {
                "withheld_contract": priya.HANDOFF_WITHHELD_CONTRACT,
                "withheld_items": [
                    {
                        "code": "{c:query:0123456789abcdef}",
                        "observed_counts": {"text": "x"},
                    }
                ],
            },
            "withheld_items[0].observed_counts",
        ),
    ],
)
def test_validate_handoff_refuses_bad_withheld_items(bad, problem):
    doc = {**_build()["handoff"]}
    doc.pop("withheld_contract")
    doc.pop("withheld_items")
    doc.update(bad)
    assert problem in priya.validate_handoff(doc)


def test_derived_variants_are_never_built_from_withheld_text():
    led = _build(extra_gsc=[("zzqxvbt kitten food", 1, 4, 0.25, 7.0)])
    texts = json.dumps(
        [r for r in led["rows"] if r["origin"] == "derived"], ensure_ascii=False
    )
    assert "zzqxvbt" not in texts and "12345" not in texts


def test_withheld_rows_never_count_against_rows_cap(monkeypatch):
    monkeypatch.setattr(priya, "ROWS_CAP", 1)
    led = _build(extra_gsc=[("kitten toys", 1, 12, 0.08, 6.0)])
    assert len(led["rows"]) == 1 and led["counts"]["rows_dropped_past_cap"] >= 1
    assert (
        len(led["withheld_rows"]) == 2
        and led["withheld_view"]["totals"]["queries"] == 2
    )


# ------------------------------------------------------------------ routes: run response, run view, audit export


def _svc_run(tmp_path):
    h, fake = setup(tmp_path, data_dir=str(tmp_path / "data"))
    fake.queries = [(q, c, i, p) for q, c, i, _ctr, p in QUERIES] + [
        (S2_Q, 1, 11, 5.0),
        (OG_Q, 2, 31, 4.0),
    ]
    return h, run(h)


def test_routes_run_response_run_view_and_audit_export_show_withheld_rows_per_family(
    tmp_path,
):
    h, out = _svc_run(tmp_path)
    led = out["ledger"]
    fams = {g["family"]: g for g in led["withheld_view"]["groups"]}
    assert (
        set(fams) == {"OG", "S2"} and fams["OG"]["totals"]["queries"] == 2
    )  # the e-mail (OG-1) + OG_Q (OG-6)
    assert {r["reason"] for g in fams.values() for r in g["rows"]} == {
        "OG-1",
        "OG-6",
        "S2:pii_suspect",
    }
    assert "rug cleaner" in {
        r["query"]["text"] for r in led["rows"]
    }  # READABLE rows show their text
    (view,) = h.ok(h.get("/tenants/acme/keyword-ledger-runs"))
    rec = h.ledger.of_type("keyword_ledger_recorded")[-1]["_payload"]
    export = h.ok(h.get("/audit/export", params={"limit": 1000}))
    emerging = sum(1 for r in led["withheld_rows"] if r["tier"] == "EMERGING")
    assert emerging == len(led["handoff"]["withheld_items"]) >= 1
    for d in (out, view, rec):
        wv = d["withheld_view"]
        assert wv["shown_reason"] == LINE and wv["revealable"] is False
        assert (
            wv["totals"] == led["withheld_view"]["totals"]
            and wv["totals"]["queries"] == 3
        )
        assert [(g["family"], g["totals"]) for g in wv["groups"]] == [
            (f, fams[f]["totals"]) for f in ("OG", "S2")
        ]
        assert (
            all("rows" not in g for g in wv["groups"])
            and d["handoff_withheld_items"] == emerging
        )
    shown = json.dumps([view, rec, export], ensure_ascii=False)
    assert (
        "{c:query:" not in shown
    )  # run views / audit export: totals only, never a code
    assert json.dumps(rec["withheld_view"], sort_keys=True) in json.dumps(
        export, sort_keys=True
    ).replace(
        json.dumps(rec["withheld_view"]),
        json.dumps(rec["withheld_view"], sort_keys=True),
    )
    blob = json.dumps([out, view, rec, export], ensure_ascii=False)
    for s in WITHHELD_TEXTS:
        assert s not in blob, s


REVEAL_PATHS = (
    "/tenants/acme/keyword-ledger/withheld",
    "/tenants/acme/keyword-ledger/withheld/{code}",
    "/tenants/acme/keyword-ledger-runs/{run}/withheld",
    "/tenants/acme/keyword-ledger-runs/{run}",
    "/tenants/acme/keyword-ledger/{code}",
    "/tenants/acme/withheld/{code}",
    "/withheld/{code}",
    "/tenants/acme/keyword-ledger/reveal/{code}",
    "/tenants/acme/keyword-ledger-runs/{run}/reveal",
)


@pytest.mark.parametrize("andre", [False, True])
def test_no_reveal_path_returns_withheld_text_to_anyone(tmp_path, andre):
    h, out = _svc_run(tmp_path)
    code = out["ledger"]["withheld_rows"][0]["code"]
    for p in REVEAL_PATHS:
        path = p.format(code=code, run=out["run_id"])
        for r in (
            h.get(path),
            h.get(path, caller="compliance_38"),
            h.post(path, {"request_id": "r-p25", "code": code}, andre=andre),
        ):
            assert r.status_code in (403, 404, 405), (path, r.status_code)
            for s in WITHHELD_TEXTS:
                assert s not in r.text
            assert not re.search(r"kitten|cleaner", r.text)
