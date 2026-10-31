"""ADR 0028, AEGIS r2 N-L7: a client id starting ``zz-test-`` (the test client of the client acceptance runbook,
step 8) is never billed: Finance refuses its billing profile, an invoice draft and an invoice approval."""

from __future__ import annotations

from helpers import ANDRE_TOKEN, Harness, rid
from test_compliance_gate_adr0024 import draft
from test_legal_client import ACC, SHA


def test_a_test_client_gets_no_billing_profile():
    hr = Harness().ready()
    r = hr.put("/fin/v1/clients/zz-test-andre/billing-profile",
               {"request_id": rid(), "entity": "zbm", "payment_method": "ach",
                "msa": {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA, "acceptance_id": ACC}})
    assert r.status_code == 409 and "test client" in r.text


def test_a_test_client_gets_no_invoice():
    hr = Harness().ready()
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zz-test-andre", "kind": "service",
            "lines": [{"line_code": "creative_services", "quantity": 1, "unit_price": "1.00"}],
            "payment_methods": ["ach"], "legal_ref": {"doc_id": "client_msa", "version": "1.1", "doc_sha256": SHA,
                                                      "acceptance_id": ACC}}
    r = hr.post("/fin/v1/invoices", body, caller="onboarding")
    assert r.status_code == 409 and "test client" in r.text
    # an ordinary client is unaffected
    assert draft(hr)["status"] == "draft"


def test_a_test_client_draft_written_earlier_is_never_approved():
    hr = Harness().ready()
    inv = draft(hr, client="zbm-client-1")
    hr.svc.db["invoices"][inv["invoice_id"]]["client_id"] = "zz-test-andre"     # as if recorded before this rule
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"},
                andre=ANDRE_TOKEN)
    assert r.status_code == 409 and "test client" in r.text
