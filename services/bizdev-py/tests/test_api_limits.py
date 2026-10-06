"""The request discipline copied from service-py: bearer auth, caller tokens, Andre's header, JSON-only bodies,
size and shape limits, no-store, and error bodies that never echo the request."""

import json

from helpers import CALLERS, SERVICE_TOKEN, rid


def test_bearer_required(h):
    assert h.client.get("/nbd/v1/status").status_code == 401
    r = h.client.get("/nbd/v1/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_caller_required_and_scoped(h):
    r = h.client.get("/nbd/v1/status", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert r.status_code == 403 and r.json()["detail"] == "CALLER_UNKNOWN"
    h.refused(h.get("/status", caller="bizdev_agent"), 403, "CALLER_NOT_ALLOWED")
    h.refused(h.post("/finance/events", {"request_id": rid(), "finance_event_id": "fin:ev-1",
                                         "deal_id": "nb-pdl-" + "0" * 40, "kind": "payment", "amount": "1.00",
                                         "currency": "USD"}, caller="bizdev_agent"), 403, "CALLER_NOT_ALLOWED")
    h.refused(h.post("/outreach/email", {"request_id": rid(), "contact_id": "nb-cnt-" + "0" * 40,
                                         "template_id": "nb-tpl-" + "0" * 40, "version": 1,
                                         "content_sha256": "0" * 64}, caller="dashboard"), 403, "CALLER_NOT_ALLOWED")


def test_andre_header_only_through_dashboard(h):
    p = h.partner()
    r = h.client.post(f"/nbd/v1/partners/{p['partner_id']}/payee",
                      json={"request_id": rid(), "finance_payee_ref": "fin:payee-1",
                            "tax_info_ref": "vault:tax:abcdefghijklmnop"},
                      headers={**h.headers("bizdev_agent"), "X-Andre-Approval-Token": h.headers(andre=True)[
                          "X-Andre-Approval-Token"]})
    assert r.status_code == 403 and r.json()["detail"] == "CALLER_NOT_ALLOWED"


def test_json_only_and_limits(h):
    hdr = h.headers("bizdev_agent")
    r = h.client.post("/nbd/v1/partners", content=b"partner_key=x", headers={**hdr, "Content-Type":
                                                                             "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    big = json.dumps({"request_id": rid(), "notes": "x" * 200_000})
    r = h.client.post("/nbd/v1/partners", content=big, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 413
    deep = "[" * 40 + "]" * 40
    r = h.client.post("/nbd/v1/partners", content=deep, headers={**hdr, "Content-Type": "application/json"})
    assert r.status_code == 422
    r = h.client.get("/nbd/v1/partners?" + "a=" + "b" * 5000, headers=h.headers())
    assert r.status_code == 414


def test_no_store_and_no_docs(h):
    r = h.get("/status")
    assert r.headers["cache-control"] == "no-store"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(path).status_code == 404


def test_validation_errors_do_not_echo_input(h):
    secret = "Probe-Secret-Value-8812"
    r = h.post("/partners", {"request_id": rid(), "partner_key": secret, "kind": "referral", "brands": ["zbm"],
                             "name": "N", "domain": "n.test"})
    assert r.status_code == 422 and secret not in r.text
    r = h.post("/partners", {"request_id": rid(), "partner_key": "abc", "kind": secret, "brands": ["zbm"],
                             "name": "N", "domain": "n.test"})
    assert r.status_code == 422 and secret not in r.text


def test_unknown_job_and_status_filters(h):
    h.refused(h.post("/jobs/drop-tables/run", {"request_id": rid()}, caller="scheduler"), 422, "JOB_UNKNOWN")
    assert h.get("/submissions?status=evil").status_code == 422


def test_intelligences_listed(h):
    out = h.ok(h.get("/intelligences", caller="bizdev_agent"))
    assert [x["number"] for x in out] == list(range(1, 14))
    assert CALLERS
