"""
Contract: Search & Answer Intelligence (2)'s invoice verification client (services/seo-py/src/finance_client.py,
ADR 0017 W3-2) against THIS service's real invoice route and representation.

The client is loaded read-only from its source file and driven through Finance's real ASGI app (the TestClient's
in-process transport: no socket), with the ``seo_02`` caller token, over an invoice walked through Finance's own
flow: drafted by onboarding -> issued by Andre -> paid by a matched bank statement line. Nothing in seo-py is modified
or imported by ``src/``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from helpers import ANDRE_TOKEN, CALLERS, SERVICE_TOKEN, rid

SERVICES = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, SERVICES / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


SEO_FIN = _load("peer_seo_finance_client", "seo-py/src/finance_client.py")
CLIENT = "zbm-seo-client-1"


def _client(hr, service=SERVICE_TOKEN, caller=None):
    return SEO_FIN.FinanceClient("http://testserver", service, caller or CALLERS["seo_02"],
                                 transport=hr.client._transport, sleep=lambda s: None)


def _draft(hr):
    body = {"request_id": rid(), "entity": "zbm", "client_id": CLIENT, "kind": "service",
            "lines": [{"line_code": "strategy_services", "quantity": 1, "unit_price": "2000.00",
                       "description": "Search audit"}], "payment_methods": ["ach"],
            "legal_ref": {"doc_id": "msa-seo", "version": 1, "doc_sha256": "d" * 64, "acceptance_id": "acc-seo"}}
    return hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]


def test_seo_02_is_a_known_caller():
    import config
    assert "seo_02" in config.CALLER_NAMES and SEO_FIN.CALLER_NAME_AT_FINANCE == "seo_02"


def test_the_seo_client_reads_finance_s_real_invoice_through_its_whole_life(hr):
    inv = _draft(hr)
    iid = inv["invoice_id"]
    c = _client(hr)
    facts = c.lookup(iid)
    assert facts["status"] == "draft" and facts["entity"] == "zbm" and facts["client_id"] == CLIENT
    assert SEO_FIN.assess(facts, CLIENT) == "INVOICE_NOT_PAID"
    issued = hr.ok(hr.post(f"/fin/v1/invoices/{iid}/decision", {"request_id": rid(), "content_sha256":
                                                                inv["content_sha256"], "decision": "approve"},
                           andre=ANDRE_TOKEN))["invoice"]
    assert issued["status"] == "issued"
    assert SEO_FIN.assess(c.lookup(iid), CLIENT) == "INVOICE_NOT_PAID"
    hr.receive("zbm", "1010", "2000.00", iid)
    facts = c.lookup(iid)
    assert facts["status"] == "paid" and facts["paid_at"]
    assert SEO_FIN.assess(facts, CLIENT) == "PAID"
    assert SEO_FIN.assess(facts, "someone-else") == "INVOICE_TENANT_MISMATCH"
    assert "2000.00" not in repr(facts) and "Search audit" not in repr(facts)      # only what the verdict needs


def test_finance_s_real_refusals_map_to_the_seo_client_s_codes(hr):
    iid = _draft(hr)["invoice_id"]
    unknown = "fin-inv-" + "0" * 26
    with pytest.raises(SEO_FIN.FinanceLookupError) as e:
        _client(hr).lookup(unknown)
    assert e.value.code == "INVOICE_NOT_FOUND"
    for c in (_client(hr, service="x" * 40), _client(hr, caller="y" * 40)):
        with pytest.raises(SEO_FIN.FinanceLookupError) as e:
            c.lookup(iid)
        assert e.value.code == "FINANCE_AUTH_REFUSED"


def test_seo_02_cannot_write_anything(hr):
    inv = _draft(hr)
    r = hr.post("/fin/v1/invoices", {"request_id": rid(), "entity": "zbm"}, caller="seo_02")
    assert r.status_code in (403, 422)
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision", {"request_id": rid(), "content_sha256":
                                                                   inv["content_sha256"], "decision": "approve"},
                caller="seo_02")
    assert r.status_code == 403
    r = hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/stripe-checkout", {"request_id": rid()}, caller="seo_02")
    assert r.status_code == 403
