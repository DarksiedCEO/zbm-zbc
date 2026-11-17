"""C2-10c4 (F10c3-1; OWNER DECISION 2026-11-15 ~15:40 PT "count and contain"): the export decision for a declared
T-2 reference field lives in sink_guard.withhold (one place). A legacy tenant_finance_client_set line holding the RAW
Finance party id (top level + evidence payload, as releases ffd28ca8 / cbdf5e78 / 3120f0b0 wrote it) is served as
WITHHELD and counted once per leaf; no export or view route serves the raw value; /health still counts the line once.
Synthetic values only; assertion messages carry counts only."""
from __future__ import annotations

import json

import pytest

import sink_guard as sg
import test_sink_audits as sa
from test_canary_all_routes import forms
from test_invoice_verification import CLIENT, FakeFinance, client_tenant, harness

KIND = "tenant_finance_client_set"
CODE = "c:text:0123456789abcdef"
ROUTES = (  # (path, caller): every GET a reader of this tenant / the log can call
    ("/status", "dashboard"), ("/tenants", "dashboard"), ("/tenants/acme", "dashboard"),
    ("/tenants/acme", "compliance_38"), ("/tenants/acme/audits", "dashboard"),
    ("/tenants/acme/citation-runs", "dashboard"), ("/tenants/acme/log-ingests", "dashboard"),
    ("/tenants/acme/schedules", "dashboard"), ("/audit/integrity", "compliance_38"),
    ("/audit/export", "compliance_38"), ("/audit/evidence", "compliance_38"),
)


def _line(ref) -> dict:
    return {"tenant_id": "acme", "finance_client_id": ref,
            "evidence": [{"event_id": "evd-legacy-0001", "payload": {"tenant_id": "acme", "finance_client_id": ref}}]}


def _hits(text: str) -> int:
    return sum(1 for f in forms(CLIENT) if f in text)


def test_withhold_serves_a_non_t2_value_in_a_declared_t2_field_as_withheld_counted_once():
    raw = _line(CLIENT)
    assert sg.violations(KIND, raw, stored=True), "control: the stored-line guard flags the raw line"
    d, n = sg.withhold(raw, KIND)
    assert n == 2, f"withheld {n}"
    assert d["finance_client_id"] == sg.WITHHELD and d["evidence"][0]["payload"]["finance_client_id"] == sg.WITHHELD
    assert d["tenant_id"] == "acme"                                     # the rest of the line is served
    d, n = sg.withhold(_line(CODE), KIND)                                  # a T-2 code is served as it is
    assert n == 0 and d == _line(CODE), f"withheld {n}"
    for other in (12345, True, 1.5):                                       # any non-null non-code leaf
        assert sg.withhold({"tenant_id": "acme", "finance_client_id": other}, KIND)[1] == 1
    assert sg.withhold({"tenant_id": "acme", "finance_client_id": None}, KIND)[1] == 0
    assert sg.withhold({"note_id": CLIENT}, "job_ran")[0] == {"note_id": CLIENT}   # no global exemption added


@pytest.mark.local_http
def test_one_raw_legacy_line_serves_zero_raw_values_over_every_export_and_view_route(tmp_path, srv):
    h = harness(tmp_path, srv, FakeFinance(), data_dir=str(tmp_path / "data"))
    client_tenant(h, bind=None)
    sa.raw_commit(h.svc, KIND, _line(CLIENT), "andre")                     # as a release wrote it
    at_rest = json.dumps(list(h.svc.log.iter_records()), default=repr)
    assert _hits(at_rest) > 0, "control: the raw value is at rest in the log"
    h2 = h.restart()
    assert h2.svc.health()["legacy_prose_lines"] == 1, "legacy_prose_lines"
    assert h2.svc._FC_T2.match(h2.svc.tenants["acme"]["finance_client_id"])
    shown, codes = {}, {}
    for path, caller in ROUTES:
        r = h2.get(path, caller=caller, params={"since": 1, "limit": 1000} if path == "/audit/export" else None)
        codes[path, caller] = r.status_code
        shown[path, caller] = _hits(r.text)
    r = h2.client.get("/health")                                           # outside the /seo/v1 prefix
    codes["/health", None], shown["/health", None] = r.status_code, _hits(r.text)
    assert all(c == 200 for c in codes.values()), [(k, c) for k, c in codes.items() if c != 200]
    assert sum(shown.values()) == 0, f"raw shown on {sum(1 for v in shown.values() if v)} routes"
    page = h2.svc.audit_export(1, 10 ** 6)
    ev = [e for e in page["events"] if e["kind"] == KIND]
    assert len(ev) == 1 and page["withheld"] == 2, f"lines {len(ev)} withheld {page['withheld']}"
    views = repr([h2.svc.tenant_view("acme"), h2.svc.tenants_view(), page, h2.svc.audit_evidence(10 ** 6, 0)])
    assert _hits(views) == 0, "raw in service views"
    assert h2.svc.health()["legacy_prose_lines"] == 1, "legacy_prose_lines after reads"


# C2-10c6 (F10c5-1): a legacy line nesting the declared T-2 field in an OBJECT (or nested lists / a container inside
# the field) is withheld the same way: one count per raw leaf, nothing else changes, no route serves the raw value.
def _shapes(ref) -> list:
    pay = {"tenant_id": "acme", "finance_client_id": ref}
    return [
        {"tenant_id": "acme", "evidence": {"event_id": "evd-legacy-0001", "payload": pay}},
        {"tenant_id": "acme", "evidence": {"k1": {"event_id": "evd-legacy-0001", "payload": pay}}},
        {"tenant_id": "acme", "evidence": [[{"event_id": "evd-legacy-0001", "payload": pay}]]},
        {"tenant_id": "acme", "finance_client_id": [ref]},
        {"tenant_id": "acme", "finance_client_id": {"v": ref}},
    ]


def test_withhold_walks_object_and_nested_shapes_of_a_declared_t2_field():
    got = [sg.withhold(line, KIND)[1] for line in _shapes(CLIENT)]
    assert got == [1] * 5, f"withheld {got}"
    # the T-2 waiver stays on the exact declared path only (no exemption widened): an off-shape code is withheld too
    assert [sg.withhold(line, KIND)[1] for line in _shapes(CODE)] == [1] * 5, "off-shape T-2 code"
    for line in _shapes(CLIENT):
        assert _hits(json.dumps(sg.withhold(line, KIND)[0])) == 0, "raw served"
    d, n = sg.withhold(_line(CLIENT), KIND)                                # list form unchanged
    assert n == 2, f"withheld {n}"
    assert sg.withhold({"note_id": CLIENT, "evidence": {"payload": {"finance_client_id": CLIENT}}}, "job_ran")[1] \
        == sg.withhold({"note_id": CLIENT, "evidence": {"payload": {"note_id": CLIENT}}}, "job_ran")[1], \
        "no global exemption / no new rule outside a typed record"


@pytest.mark.local_http
def test_object_evidence_raw_line_serves_zero_raw_values_over_every_route(tmp_path, srv):
    h = harness(tmp_path, srv, FakeFinance(), data_dir=str(tmp_path / "data"))
    client_tenant(h, bind=None)
    line = {"tenant_id": "acme", "finance_client_id": CLIENT,
            "evidence": {"event_id": "evd-legacy-0001", "payload": {"tenant_id": "acme", "finance_client_id": CLIENT}}}
    sa.raw_commit(h.svc, KIND, line, "andre")
    assert _hits(json.dumps(list(h.svc.log.iter_records()), default=repr)) > 0, "control: raw at rest"
    h2 = h.restart()
    shown, codes = {}, {}
    for path, caller in ROUTES:
        r = h2.get(path, caller=caller, params={"since": 1, "limit": 1000} if path == "/audit/export" else None)
        codes[path, caller], shown[path, caller] = r.status_code, _hits(r.text)
    r = h2.client.get("/health")
    codes["/health", None], shown["/health", None] = r.status_code, _hits(r.text)
    assert all(c == 200 for c in codes.values()), [(k, c) for k, c in codes.items() if c != 200]
    assert sum(shown.values()) == 0, f"raw shown on {sum(1 for v in shown.values() if v)} routes"
    page = h2.svc.audit_export(1, 10 ** 6)
    ev = [e for e in page["events"] if e["kind"] == KIND]
    assert len(ev) == 1 and page["withheld"] == 2, f"lines {len(ev)} withheld {page['withheld']}"
    views = repr([h2.svc.tenant_view("acme"), h2.svc.tenants_view(), page, h2.svc.audit_evidence(10 ** 6, 0)])
    assert _hits(views) == 0, "raw in service views"
