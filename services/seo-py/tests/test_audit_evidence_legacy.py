"""C2-10c5 (F10c4-1): /audit/evidence must never 5xx on a stored log line whose evidence entries are legacy or
malformed (no ``event_id``, an ``event_id`` that is not a string, an entry that is not an object, an ``evidence``
that is not a list). Such entries are never named (their ledger event stays ``attempted``), are counted
(``legacy_evidence_entries``) and serve no raw value. Synthetic values only; assertion messages carry counts only."""
from __future__ import annotations

import pytest

import test_sink_audits as sa
from test_canary_all_routes import forms
from test_invoice_verification import CLIENT, FakeFinance, client_tenant, harness
from test_withhold_t2_ref import ROUTES

KIND = "tenant_finance_client_set"
CODE = "c:text:0123456789abcdef"


def _p() -> dict:
    return {"tenant_id": "acme", "finance_client_id": CLIENT}


def _hits(text: str) -> int:
    return sum(1 for f in forms(CLIENT) if f in text)


@pytest.mark.local_http
def test_malformed_legacy_evidence_entries_serve_200_zero_raw_and_are_counted(tmp_path, srv):
    h = harness(tmp_path, srv, FakeFinance(), data_dir=str(tmp_path / "data"))
    client_tenant(h, bind=None)
    base = h.svc.audit_evidence(10 ** 6, 0)
    sa.raw_commit(h.svc, KIND, {"tenant_id": "acme", "finance_client_id": CODE, "evidence": [
        {"payload": _p()},                                      # no event_id (F10c4-1)
        {"event_id": 12345, "payload": _p()},                   # wrong type: int
        {"event_id": {"n": 7}, "payload": _p()},                # wrong type: object (unhashable)
        {"event_id": "evd-legacy-0002", "payload": "not-an-object"},   # well-formed id, bad payload: named, not 5xx
    ]}, "andre")
    sa.raw_commit(h.svc, KIND, {"tenant_id": "acme", "finance_client_id": CODE,
                                "evidence": {"payload": {"tenant_id": "acme"}}}, "andre")   # evidence not a list
    for i in range(2):                                          # same process (cache), then after a restart
        hh = h if i == 0 else h.restart()
        r = hh.get("/audit/evidence", caller="compliance_38")
        assert r.status_code == 200, f"status {r.status_code}"
        page = hh.svc.audit_evidence(10 ** 6, 0)
        assert page["legacy_evidence_entries"] == 4, f"counted {page['legacy_evidence_entries']}"
        assert page["total"] == base["total"] and page["committed"] == base["committed"], "rows changed"
        assert _hits(repr(page)) == 0, "raw in audit_evidence"
        assert r.json()["legacy_evidence_entries"] == 4 and _hits(r.text) == 0, "route count / raw"
    codes, shown = {}, {}
    for path, caller in ROUTES:
        r = hh.get(path, caller=caller, params={"since": 1, "limit": 1000} if path == "/audit/export" else None)
        codes[path, caller], shown[path, caller] = r.status_code, _hits(r.text)
    assert base.get("legacy_evidence_entries") == 0, "control: a clean log counts 0"
    assert all(c == 200 for c in codes.values()), [(k, c) for k, c in codes.items() if c != 200]
    assert sum(shown.values()) == 0, f"raw shown on {sum(1 for v in shown.values() if v)} routes"
