"""P2-4 (priya-spec/2 F-6, F-7, OW-5, OW-10, Z-1): a WITHHELD row is stored as a Wave-1 store code
``{c:query:<16 hex>}`` + its metrics + its reason code; no text, no other hash, no evidence map. No vault key ->
the ingest is refused VAULT_UNAVAILABLE before any row is written. Lead ruling 12:00 PT (I-17): over the OG-8
bound the query is withheld OG-8 before any other rule runs (bounded cost)."""

from __future__ import annotations

import base64
import json
import re
import time

import pytest
from test_priya_service import SECRET_TEXTS, every_stored_byte, run, setup

import vault
from agents import priya, priya_gate as pg
from agents.priya import SourceResult

V = pg.load_vocab()
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
KEY_ID = "k-test1"
CODE_RE = re.compile(r"^\{c:query:[0-9a-f]{16}\}$")
RAW_WITHHELD = ("mail me@example", "jane at example dot com", "zzqxvbt kitten")
GSC_FIELDS = {
    "source",
    "data_class",
    "metric",
    "clicks",
    "impressions",
    "ctr",
    "position",
    "impressions_poisson_95",
    "date_range",
    "timezone",
    "aggregation",
    "data_state",
}
BING_FIELDS = {
    "source",
    "data_class",
    "metric",
    "clicks",
    "impressions",
    "ctr",
    "avg_impression_position",
    "position_note",
    "impressions_poisson_95",
    "date_range",
}
WINDOW = {"start": "2026-10-01", "end": "2026-10-28", "timezone": "America/Los_Angeles"}


def coder(tenant="acme"):
    return vault.Coder(
        vault.load_vault_key({"SEO_VAULT_KEY": KEY, "SEO_VAULT_KEY_ID": KEY_ID}), tenant
    )


def gsc(rows):
    return SourceResult("search_console", "OK", "OK", rows, {"window": WINDOW})


def bing(rows):
    return SourceResult("bing_webmaster", "OK", "OK", rows, {})


def build(c, tenant="acme"):
    g = gsc(
        [("kitten food", 9, 400, 0.02, 3.0)]
        + [(q, 2, 30 + n, 0.06, 4.0) for n, q in enumerate(RAW_WITHHELD)]
    )
    b = bing(
        [
            ("mail me@example", "2026-10-05", 1, 7, None, 2.5),
            ("kitten food", "2026-10-05", 1, 5, None, 3.0),
        ]
    )
    return priya.build(
        tenant,
        "seo-kwl-" + "0" * 40,
        "2026-11-08T12:00:00+00:00",
        [],
        g,
        b,
        vocab=V,
        coder=c,
    )


def test_withheld_row_has_exactly_code_reason_tier_and_metrics():
    led = build(coder())
    rows = led["withheld_rows"]
    assert len(rows) == 3
    assert {r["reason"] for r in rows} == {"OG-1", "OG-3", "OG-6"}
    for r in rows:
        assert set(r) == {"code", "reason", "tier", "metrics"}
        assert CODE_RE.match(r["code"]), r["code"]
        assert r["reason"] in priya.REASON_CODES and r["tier"] in (
            "ESTABLISHED",
            "EMERGING",
        )
        assert set(r["metrics"]) == {"search_console", "bing_webmaster"}
        assert set(r["metrics"]["search_console"]) == GSC_FIELDS
    both = [r for r in rows if r["metrics"]["bing_webmaster"] is not None]
    assert len(both) == 1 and set(both[0]["metrics"]["bing_webmaster"]) == BING_FIELDS
    assert both[0]["metrics"]["bing_webmaster"]["date_range"] == {
        "start": "2026-10-05",
        "end": "2026-10-05",
    }
    # metrics conserved: the withheld rows sum to the withheld counts
    w = led["counts"]["withheld"]
    assert (
        sum(r["metrics"]["search_console"]["clicks"] for r in rows) + 1 == w["clicks"]
    )
    assert [r["code"] for r in rows] == sorted(
        r["code"] for r in rows
    )  # deterministic order, never by text


def test_no_text_no_key_material_anywhere_in_the_ledger():
    c = coder()
    blob = json.dumps(build(c), ensure_ascii=False)
    for raw in RAW_WITHHELD + ("me@example", "zzqxvbt", "jane"):
        assert raw not in blob, raw
    assert KEY not in blob and "redacted" in repr(c)
    assert "evidence_map" not in blob and "vault" not in blob


def test_codes_are_stable_per_tenant_and_differ_across_tenants():
    a1 = {r["reason"]: r["code"] for r in build(coder("acme"))["withheld_rows"]}
    a2 = {r["reason"]: r["code"] for r in build(coder("acme"))["withheld_rows"]}
    b = {
        r["reason"]: r["code"]
        for r in build(coder("globex"), tenant="globex")["withheld_rows"]
    }
    assert a1 == a2 and not set(a1.values()) & set(b.values())


def test_no_coder_refuses_vault_unavailable_never_the_ephemeral_fallback():
    with pytest.raises(priya.VaultUnavailable) as ei:
        build(None)
    assert ei.value.code == "VAULT_UNAVAILABLE"


def test_gate_section_names_the_pins_and_the_key_id():
    gate = build(coder())["gate"]
    assert gate == {
        "vocab_sha256": pg.VOCAB_SHA256,
        "gate_status": "OK",
        "codes_key_id": KEY_ID,
        "tenant_vocab_sha256": pg.hashlib.sha256(b"").hexdigest(),
    }


def test_over_long_query_is_og8_before_any_rule_and_bounded():
    q = "x@y " * 262144  # 1 MB; OG-1 would fire, OG-8 is decided first (I-17)
    assert pg.og_fired(q, V) == ["OG-8"]
    t0 = time.process_time()  # CPU time (hygiene L1): same 0.05 s PF-2 bar, not inflated by box load
    assert priya.verdict(q, [], V, frozenset()) == ("WITHHELD", "OG-8", None)
    assert time.process_time() - t0 < 0.05  # PF-2 bound
    assert pg.og("a" * 512, V) == (
        "WITHHOLD",
        "OG-6",
    )  # at the bound the other rules still run


def test_service_without_a_vault_key_refuses_before_any_record_or_read(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("SEO_VAULT_KEY", raising=False)
    monkeypatch.delenv("SEO_VAULT_KEY_ID", raising=False)
    h, fake = setup(tmp_path, data_dir=str(tmp_path / "data"))
    out = run(h, code=503)
    assert "VAULT_UNAVAILABLE" in json.dumps(out)
    assert (
        h.ledger.of_type("keyword_ledger_started") == []
        and h.ledger.of_type("keyword_ledger_recorded") == []
    )
    assert not [r for r in fake.api_requests() if r["path"].endswith("/query")]
    assert h.svc.keyword_runs == {}


def test_service_run_report_has_key_id_and_pins_and_no_withheld_text(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SEO_VAULT_KEY", KEY)
    monkeypatch.setenv("SEO_VAULT_KEY_ID", KEY_ID)
    h, _fake = setup(tmp_path, data_dir=str(tmp_path / "data"))
    out = run(h)
    led = out["ledger"]
    assert led["withheld_rows"] and all(
        CODE_RE.match(r["code"]) for r in led["withheld_rows"]
    )
    rec = h.ledger.of_type("keyword_ledger_recorded")[-1]["_payload"]
    for d in (rec, out):
        assert d["codes_key_id"] == KEY_ID and d["vocab_sha256"] == pg.VOCAB_SHA256
        assert d["tenant_vocab_sha256"] == pg.hashlib.sha256(b"").hexdigest()
    blob = every_stored_byte(
        h, tmp_path
    )  # data dir (seo_log.jsonl included), ledger events, audit export
    for s in SECRET_TEXTS:
        assert s not in blob, s
    handed = json.dumps(
        [
            led["withheld_rows"],
            led["gate"],
            {k: v for k, v in out.items() if k != "ledger"},
        ]
    )
    for b in (blob, handed):
        assert KEY not in b and "jane.doe" not in b and "example.com" not in b
