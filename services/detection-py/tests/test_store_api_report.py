"""ADR 0020: the /stores routes, the scan path on real-shaped store data, and the client report. Fakes only."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api
import rr_report
from _store_fakes import (NOW, SECRETS, TOKEN, TOKEN_B, Clock, FakeShopify, MemoryLedger, code_app, conn, order,
                          write_connections, write_secret)
from conftest import TEST_SERVICE_TOKEN
from store_connector.service import StoreService

AUTH = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}
ORDER_AGENTS = {"affiliate-coupon-extension-v1": "/agents/affiliate-coupon-extension/detect",
                "discount-misuse-v1": "/agents/discount-misuse/detect",
                "abandoned-cart-coverage-v1": "/agents/abandoned-cart-coverage/detect"}
EMPTY_AGENTS = {"renewal-never-triggered-v1": ("/agents/renewal-never-triggered/detect", "subscriptions"),
                "server-side-attribution-v1": ("/agents/server-side-attribution/detect", "events"),
                "cross-channel-attribution-v1": ("/agents/cross-channel-attribution/detect", "touchpoints"),
                "platform-integration-v1": ("/agents/platform-integration/detect", "statuses"),
                "contract-pricing-term-drift-v1": ("/agents/contract-pricing-term-drift/detect", "terms")}


@pytest.fixture
def client():
    return TestClient(api.app)


@pytest.fixture
def store(tmp_path, monkeypatch):
    fake = FakeShopify(shops={"acme-store": [], "bravo-store": []}, tokens={"acme-store": TOKEN, "bravo-store": TOKEN_B})
    conns = [conn("acme", "acme-store", write_secret(tmp_path, "a", TOKEN)),
             conn("bravo", "bravo-store", write_secret(tmp_path, "b", TOKEN_B))]
    clock = Clock()
    ledger = MemoryLedger()
    svc = StoreService(connections_file=write_connections(tmp_path, conns), ledger=ledger, transport=fake.transport,
                       monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: NOW)
    monkeypatch.setattr(api, "STORE", svc)
    svc.fake, svc.ledger_fake = fake, ledger
    return svc


def _stacked(n, a="SAVE10", b="TAKE5"):
    return order(n, codes=[code_app(a, pct=10.0), code_app(b, amount="5.00")])


def simulate_scan(client: TestClient, client_id: str) -> dict:
    """What orchestrator-go does for a connected tenant (orchestrator.go RunFullScan), through the real routes."""
    r = client.post("/stores/snapshot", json={"client_id": client_id}, headers=AUTH)
    assert r.status_code == 200, r.text
    snap = r.json()
    findings = []
    for agent, path in ORDER_AGENTS.items():
        for i in range(0, max(len(snap["orders"]), 1), 500):
            r = client.post(path, json={"client_id": client_id, "orders": snap["orders"][i:i + 500]}, headers=AUTH)
            assert r.status_code == 200, r.text
            findings += r.json()["findings"]
    for agent, (path, field) in EMPTY_AGENTS.items():
        body = {"client_id": client_id, field: []}
        if field == "subscriptions":
            body["as_of"] = NOW.isoformat()
        r = client.post(path, json=body, headers=AUTH)
        assert r.status_code == 200 and r.json()["findings"] == [], r.text
    overlaps = {}
    if findings:
        r = client.post("/correlation/overlaps", json={"findings": findings}, headers=AUTH)
        overlaps = r.json()
    return {"scan_id": "0" * 32, "client_id": client_id, "tenant_defaulted": False,
            "data_source": f"shopify:{snap['snapshot_id']}", "as_of": NOW.isoformat(), "findings": findings,
            "overlapping_claims": overlaps, "agents_run": list(ORDER_AGENTS) + list(EMPTY_AGENTS),
            "non_live_data_source": False, "ledger_entries_written": len(findings),
            "ledger_events_created": len(findings) + 2, "ledger_events_already_present": 0,
            "ledger_append_retries": 0, "ledger_verify": {"valid": True},
            "store_snapshot_id": snap["snapshot_id"], "store_coverage": snap["coverage"]}


# ---- routes -------------------------------------------------------------------------------------------------

def test_store_routes_need_auth(client):
    assert client.get("/stores/acme/status").status_code == 401
    assert client.post("/stores/sync", json={"client_id": "acme"}).status_code == 401
    assert client.post("/stores/snapshot", json={"client_id": "acme"}).status_code == 401


def test_default_service_is_not_connected_and_makes_no_outbound_call(client, monkeypatch):
    calls = []
    from store_connector.net import DeadlineBackend
    from store_connector.shopify_client import ShopifySession

    def record(*a, **k):
        calls.append(a)
        raise AssertionError("no outbound call may be attempted")
    monkeypatch.setattr(ShopifySession, "_post", record)
    monkeypatch.setattr(DeadlineBackend, "connect_tcp", record)
    st = client.get("/stores/acme/status", headers=AUTH).json()
    assert st["connected"] is False and st["reason"] == "NOT_CONNECTED"
    for path in ("/stores/sync", "/stores/snapshot"):
        r = client.post(path, json={"client_id": "acme"}, headers=AUTH)
        assert r.status_code == 409 and r.json()["detail"]["reason"] == "NOT_CONNECTED"
    assert calls == []


def test_store_route_input_validation(client):
    assert client.get("/stores/bad%20id/status", headers=AUTH).status_code == 422
    assert client.post("/stores/sync", json={"client_id": "a|b"}, headers=AUTH).status_code == 422
    assert client.post("/stores/sync", content=b"x" * (2 * 1024 * 1024), headers={**AUTH,
                       "content-type": "application/json"}).status_code == 413


def test_sync_snapshot_and_status_through_the_api(client, store):
    store.fake.shops["acme-store"] = [_stacked(1), order(2)]
    r = client.post("/stores/sync", json={"client_id": "acme"}, headers=AUTH)
    assert r.status_code == 200, r.text
    st = client.get("/stores/acme/status", headers=AUTH).json()
    assert st["connected"] and st["snapshot_id"] == r.json()["snapshot_id"] and st["shop"] == "acme-store"
    snap = client.post("/stores/snapshot", json={"client_id": "acme"}, headers=AUTH).json()
    assert len(snap["orders"]) == 2
    assert client.post("/stores/snapshot", json={"client_id": "bravo"}, headers=AUTH).json()["detail"]["reason"] \
        == "NOT_SYNCED"


def test_refusal_answers_carry_only_the_reason(client, store):
    store.fake.script = [__import__("httpx").Response(401, content=TOKEN.encode())]
    r = client.post("/stores/sync", json={"client_id": "acme"}, headers=AUTH)
    assert r.status_code == 502 and r.json() == {"detail": {"reason": "STORE_AUTH_REFUSED",
                                                           "message": "the store refused the credential"}}


# ---- scan on store data + report ----------------------------------------------------------------------------

def test_scan_on_store_data_gives_observed_findings_and_a_client_report(client, store, caplog):
    caplog.set_level(logging.DEBUG)
    nodes = [_stacked(i) for i in range(1, 4)] + [order(i) for i in range(4, 40)] + [order(40, test=True)]
    for i, n in enumerate(nodes):                     # spread over 30 days so a projection is allowed
        n["createdAt"] = f"2026-{'09' if i < 20 else '10'}-{(i % 20) + 1:02d}T10:00:00Z"
    store.fake.shops["acme-store"] = nodes
    assert client.post("/stores/sync", json={"client_id": "acme"}, headers=AUTH).status_code == 200
    scan = simulate_scan(client, "acme")
    assert len(scan["findings"]) == 3
    assert {f["evidence_class"] for f in scan["findings"]} == {"OBSERVED"}
    assert {f["agent_id"] for f in scan["findings"]} == {"discount-misuse-v1"}
    rep = rr_report.build_report(scan)
    assert rep["evidence_counts"] == {"OBSERVED": 3, "ESTIMATED": 0, "MODELED": 0, "UNKNOWN": 0}
    assert all(r["quotable"] for r in rep["findings"])
    disc = next(a for a in rep["agents"] if a["agent_id"] == "discount-misuse-v1")
    assert disc["covered"] and disc["observed_usd_in_window"] == "15.00"
    assert rep["agents"][0]["agent_id"] == "discount-misuse-v1"
    assert [a["agent_id"] for a in rep["agents"] if not a["covered"]] == [
        "affiliate-coupon-extension-v1", "abandoned-cart-coverage-v1", "renewal-never-triggered-v1",
        "server-side-attribution-v1", "cross-channel-attribution-v1", "platform-integration-v1",
        "contract-pricing-term-drift-v1"]
    (p,) = rep["projections"]
    assert p["evidence_class"] == "ESTIMATED" and p["confidence"] == "low"
    days = rep["window_days"]
    assert Decimal(p["amount_usd"]) == (Decimal("15.00") * 365 / days).quantize(Decimal("0.01"))
    assert rep["counts"]["orders_excluded"] == {"test_order": 1}
    assert rep["window_basis"] == "requested_window" and days == 60      # not truncated: the whole lookback
    md = rr_report.render_markdown(rep)
    # AEGIS golive-rr L4: assert on the table rows, not on the explanatory paragraph that also says NOT COVERED
    rows = {line.split(" | ")[0].lstrip("| "): line for line in md.splitlines()
            if line.startswith("| ") and line.count(" | ") == 3}
    assert rows["Stacked discount codes"] == "| Stacked discount codes | yes | 3 | $15.00 |"
    not_covered = [t for t, line in rows.items() if " | NOT COVERED — " in line]
    assert sorted(not_covered) == sorted(rr_report.AGENT_TITLES[a] for a in rr_report.AGENT_TITLES
                                         if a != "discount-misuse-v1")
    for t in not_covered:
        assert rows[t].endswith(" | — | — |")
    assert "est. $" in md and "quotable" in md
    assert "score" not in md.lower().replace("no overall score", "")
    assert "grand total" not in md.lower().replace("no grand total", "")
    for secret in SECRETS:
        assert secret not in md + json.dumps(rep) + caplog.text
    assert "@" not in md


def test_report_refuses_fixture_and_inconsistent_scans(client, store):
    store.fake.shops["acme-store"] = [_stacked(1)]
    client.post("/stores/sync", json={"client_id": "acme"}, headers=AUTH)
    scan = simulate_scan(client, "acme")
    for bad in ({**scan, "data_source": "fixtures"}, {**scan, "non_live_data_source": True},
                {**scan, "store_coverage": None},
                {**scan, "findings": [{**scan["findings"][0], "client_id": "bravo"}]},
                {**scan, "findings": [{**scan["findings"][0], "evidence_class": "UNKNOWN"}]}):
        with pytest.raises(rr_report.ReportInputError):
            rr_report.build_report(bad)


def test_estimated_and_unknown_findings_render_as_such():
    cov = {"shop": "acme-store", "api_version": "2026-10", "taken_at": "2026-10-24T12:00:00+00:00",
           "window": {"oldest_order_read": "2026-10-20T00:00:00+00:00"}, "counts": {}, "customer_data": "none",
           "agents": {"discount-misuse-v1": {"covered": True, "why": "x"},
                      "affiliate-coupon-extension-v1": {"covered": True, "why": "x"}}}
    base = {"client_id": "acme", "entity_id": "o1", "cause_description": "c", "methodology": "m"}
    scan = {"scan_id": "s", "client_id": "acme", "as_of": "2026-10-24T12:00:00Z", "data_source": "shopify:snap-x",
            "non_live_data_source": False, "store_coverage": cov, "store_snapshot_id": "snap-x",
            "overlapping_claims": {"acme|order|o1": []},
            "findings": [{**base, "finding_id": "f1", "agent_id": "affiliate-coupon-extension-v1",
                          "evidence_class": "ESTIMATED", "recoverable_value": {"amount_usd": "12.00",
                          "classification": "attributed", "confidence": "medium"}},
                         {**base, "finding_id": "f2", "agent_id": "discount-misuse-v1", "evidence_class": "UNKNOWN",
                          "recoverable_value": None}]}
    rep = rr_report.build_report(scan)
    assert rep["projections"] == []                          # window under 14 days, and nothing OBSERVED
    assert not any(r["quotable"] for r in rep["findings"])
    md = rr_report.render_markdown(rep)
    assert "est. $12.00" in md and "no dollar figure" in md and "do not add the two figures together" in md


def _projection_scan(window: dict, observed: str = "100.00", code: str = "SAVE10") -> dict:
    cov = {"shop": "acme-store", "api_version": "2026-10", "taken_at": "2026-10-24T12:00:00+00:00",
           "window": window, "counts": {}, "customer_data": "none",
           "agents": {"discount-misuse-v1": {"covered": True, "why": "x"}}}
    f = {"client_id": "acme", "entity_id": "shopify-order-1", "finding_id": "f1", "agent_id": "discount-misuse-v1",
         "cause_description": f"2 discount codes stacked on one order ({code}, TAKE5)", "methodology": "m",
         "evidence_class": "OBSERVED",
         "recoverable_value": {"amount_usd": observed, "classification": "observed", "confidence": "high"}}
    return {"scan_id": "s", "client_id": "acme", "as_of": "2026-10-24T12:00:00Z", "data_source": "shopify:snap-x",
            "non_live_data_source": False, "store_coverage": cov, "store_snapshot_id": "snap-x", "findings": [f]}


def test_sparse_orders_do_not_inflate_the_projection():
    """AEGIS golive-rr M2: a 60-day lookback read in full whose oldest order is 20 days old covers 60 days, not 20."""
    window = {"requested_start": "2026-08-25T12:00:00+00:00", "lookback_days": 60, "truncated_by": None,
              "oldest_order_read": "2026-10-04T12:00:00+00:00", "newest_order_read": "2026-10-23T12:00:00+00:00"}
    rep = rr_report.build_report(_projection_scan(window))
    (p,) = rep["projections"]
    assert rep["window_days"] == 60 and rep["window_basis"] == "requested_window"
    assert p["amount_usd"] == "608.33"                            # 100 x 365/60, not 100 x 365/20 = 1825.00
    assert "over 20 days" not in p["basis"] and "60-day window read" in p["basis"]
    md = rr_report.render_markdown(rep)
    assert "$1,825.00" not in md and "est. $608.33 a year" in md
    assert "every order created from 2026-08-25T12:00:00+00:00" in md
    # without requested_start, lookback_days gives the same window
    w2 = {k: v for k, v in window.items() if k != "requested_start"}
    assert rr_report.build_report(_projection_scan(w2))["projections"][0]["amount_usd"] == "608.33"


def test_truncated_read_measures_from_the_oldest_order_read():
    window = {"requested_start": "2026-08-25T12:00:00+00:00", "lookback_days": 60, "truncated_by": "max_orders",
              "oldest_order_read": "2026-10-04T12:00:00+00:00", "newest_order_read": "2026-10-23T12:00:00+00:00"}
    rep = rr_report.build_report(_projection_scan(window))
    (p,) = rep["projections"]
    assert rep["window_days"] == 20 and rep["window_basis"] == "oldest_order_read"
    assert p["amount_usd"] == "1825.00"
    assert "back to the oldest order read" in p["basis"] and "max orders limit" in p["basis"]
    assert "older orders were not read" in rr_report.render_markdown(rep)
    # truncated but no oldest order: no window, no projection
    rep = rr_report.build_report(_projection_scan({**window, "oldest_order_read": None}))
    assert rep["window_days"] is None and rep["projections"] == []


def test_discount_codes_are_escaped_in_the_markdown():
    window = {"requested_start": "2026-08-25T12:00:00+00:00", "lookback_days": 60, "truncated_by": None}
    md = rr_report.render_markdown(rr_report.build_report(
        _projection_scan(window, code="[click](https://evil.example)<img src=x>|*x*")))
    (line,) = [x for x in md.splitlines() if "discount codes stacked" in x]
    outside = _CODE_SPAN.sub("", line)
    assert re.search(r"(?<!\\)[\[\]<>|*`_]", outside) is None, line       # nothing unescaped outside code spans
    assert "\\|" in outside                                                # the pipe stays escaped, outside
    assert "` ([click](https://evil.example)<img `" in line                # literal inside a code span


_CODE_SPAN = re.compile(r"(`+) .*? \1")


@pytest.mark.parametrize("code", ["https://evil.example/x", "HTTP://EVIL.EXAMPLE", "www.evil.example",
                                  "win@evil.example", "ftp://a`b`c", "x`https://evil.example"])
def test_bare_urls_in_discount_codes_are_not_autolinked(code):
    """AEGIS golive-rr info: GFM turns a bare URL, www. name or e-mail into a link even when escaped; in a code span
    it is literal."""
    window = {"requested_start": "2026-08-25T12:00:00+00:00", "lookback_days": 60, "truncated_by": None}
    md = rr_report.render_markdown(rr_report.build_report(_projection_scan(window, code=code)))
    (line,) = [x for x in md.splitlines() if "discount codes stacked" in x]
    spans = [m_.group(0) for m_ in _CODE_SPAN.finditer(line)]
    assert any(code in sp for sp in spans), line
    outside = _CODE_SPAN.sub("", line).lower()
    assert "://" not in outside and "www." not in outside and "@" not in outside, line


@pytest.mark.parametrize("field,value", [("requested_start", "2026-08-25T12:00:00"),
                                         ("requested_start", "not a time"), ("requested_start", 5),
                                         ("taken_at", "2026-10-24T12:00:00")])
def test_naive_or_bad_timestamps_are_refused(field, value):
    window = {"requested_start": "2026-08-25T12:00:00+00:00", "lookback_days": 60, "truncated_by": None}
    scan = _projection_scan(window)
    if field == "taken_at":
        scan["store_coverage"]["taken_at"] = value
    else:
        scan["store_coverage"]["window"][field] = value
    with pytest.raises(rr_report.ReportInputError):
        rr_report.build_report(scan)


def test_report_cli_refuses_a_naive_timestamp(tmp_path):
    window = {"requested_start": "2026-08-25T12:00:00", "lookback_days": 60, "truncated_by": None}
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(json.dumps(_projection_scan(window)))
    cli = Path(__file__).resolve().parents[1] / "devtools" / "rr_report.py"
    out = subprocess.run([sys.executable, str(cli), str(scan_file), "--md", str(tmp_path / "r.md")],
                         capture_output=True, text=True, timeout=120, check=False)
    assert out.returncode == 2 and "refused:" in out.stderr and "Traceback" not in out.stderr
    assert not (tmp_path / "r.md").exists()


def test_report_cli(tmp_path, client, store):
    store.fake.shops["acme-store"] = [_stacked(1)]
    client.post("/stores/sync", json={"client_id": "acme"}, headers=AUTH)
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(json.dumps(simulate_scan(client, "acme")))
    cli = Path(__file__).resolve().parents[1] / "devtools" / "rr_report.py"
    out = subprocess.run([sys.executable, str(cli), str(scan_file), "--md", str(tmp_path / "r.md"),
                          "--json", str(tmp_path / "r.json")], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "Stacked discount codes" in (tmp_path / "r.md").read_text()
    bad = tmp_path / "fixture.json"
    bad.write_text(json.dumps({**json.loads(scan_file.read_text()), "data_source": "fixtures"}))
    out = subprocess.run([sys.executable, str(cli), str(bad), "--md", str(tmp_path / "x.md")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 2 and not (tmp_path / "x.md").exists()
