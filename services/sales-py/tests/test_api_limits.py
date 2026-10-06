"""The shared request discipline (security-py's api blocks): auth, callers, limits, no-store, no echo."""

from __future__ import annotations

from helpers import CALLERS, SERVICE_TOKEN, rid


def test_no_bearer_401_and_wrong_bearer_401(h):
    assert h.client.get("/sales/v1/status").status_code == 401
    r = h.client.get("/sales/v1/status", headers={"Authorization": "Bearer wrong", "X-SALES-Caller-Token":
                                                  CALLERS["dashboard"]})
    assert r.status_code == 401
    r = h.client.get("/sales/v1/status", headers=[(b"authorization", b"Bearer t\xe9st-token")])
    assert r.status_code == 401                       # a non-ASCII token is a 401, never a 500


def test_unknown_or_missing_caller_403(h):
    r = h.client.get("/sales/v1/status", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    h.refused(r, 403, "CALLER_UNKNOWN")
    r = h.client.get("/sales/v1/status", headers={"Authorization": f"Bearer {SERVICE_TOKEN}",
                                                  "X-SALES-Caller-Token": "nope" * 10})
    h.refused(r, 403, "CALLER_UNKNOWN")


def test_caller_not_allowed_on_route(h):
    h.refused(h.get("/sales/v1/status", "sales_agent"), 403, "CALLER_NOT_ALLOWED")
    h.refused(h.post("/sales/v1/jobs/integrity/run", {"request_id": rid()}, "dashboard"), 403, "CALLER_NOT_ALLOWED")


def test_body_limits(h):
    big = {"request_id": rid(), "x": "a" * (300 * 1024)}
    assert h.post("/sales/v1/suppressions", big, "hub").status_code == 413
    r = h.client.post("/sales/v1/suppressions", content=b"request_id=1", headers={**h.headers("hub"),
                                                                                  "content-type": "text/plain"})
    assert r.status_code == 415
    deep = {"a": 1}
    for _ in range(40):
        deep = {"a": deep}
    assert h.post("/sales/v1/suppressions", deep, "hub").status_code == 422
    assert h.client.get("/sales/v1/leads?" + "a=" * 3000, headers=h.headers()).status_code == 414


def test_errors_never_echo_input(h):
    r = h.post("/sales/v1/suppressions", {"request_id": rid(), "email": "ECHO-ME-not-an-email",
                                          "reason": "manual"}, "hub")
    assert r.status_code == 422 and "ECHO-ME" not in r.text
    r = h.post("/sales/v1/suppressions", {"request_id": rid(), "reason": "ECHO-ME"}, "hub")
    assert r.status_code == 422 and "ECHO-ME" not in r.text


def test_no_store_everywhere_and_no_docs(h):
    assert h.get("/sales/v1/status").headers["cache-control"] == "no-store"
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert h.client.get(p).status_code in (401, 404)


def test_unknown_job_and_bad_ids(h):
    h.refused(h.post("/sales/v1/jobs/explode/run", {"request_id": rid()}, "scheduler"), 422, "JOB_UNKNOWN")
    h.refused(h.get("/sales/v1/leads/not-an-id"), 422, "INVALID")
    h.refused(h.get("/sales/v1/leads/sl-led-" + "0" * 40), 404, "LEAD_NOT_FOUND")


def test_integrity_job(h):
    r = h.ok(h.job("integrity"))
    assert r["integrity"]["ok"] is True and r["ledger_valid"] is True
