"""Dormant caller can reach only the one housekeeping operation."""
import json
import secrets

import pytest

import config
from helpers import Harness, base_env, rid


@pytest.mark.parametrize("method,path", [
    ("GET", "/fin/v1/invoices"), ("POST", "/fin/v1/jobs/accrual/run"),
    ("POST", "/fin/v1/jobs/rail-sync/run"), ("POST", "/fin/v1/reconciliations/run"),
    ("POST", "/fin/v1/payout-runs"), ("POST", "/fin/v1/stripe/events"),
    ("POST", "/fin/v1/invoices/a/approve"), ("GET", "/fin/v1/jobs/stripe-sessions/run"),
    ("POST", "/fin/v1/jobs/stripe-sessions/run/"),
    ("POST", "/fin/v1/jobs/%73tripe-sessions/run"),
])
def test_ops_cron_refused_before_body(method, path):
    token = secrets.token_hex(32)
    h = Harness(env={"FIN_CALLER_TOKENS": json.dumps({"ops_cron": token})})
    try:
        r = h.client.request(method, path, headers={**h.headers(caller=token), "Content-Type": "application/json"}, content=b"not-json",
                             follow_redirects=False)
        assert r.status_code == 403
    finally:
        h.svc.close()


def test_ops_cron_job_and_existing_scheduler(hr):
    # Token inventory is discovered by the existing harness, but absent from deployed configs is still valid.
    r = hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="ops_cron")
    assert r.status_code == 200 and r.json()["job"] == "stripe-sessions"
    assert hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="scheduler").status_code == 200
    assert hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="ops_cron",
                   bearer=secrets.token_hex(32)).status_code == 401


def test_ops_cron_is_optional():
    cfg = config.load(base_env(FIN_CALLER_TOKENS=json.dumps({"scheduler": secrets.token_hex(32)})))
    assert "ops_cron" not in cfg.caller_tokens


def test_all_other_registered_finance_routes_refuse_ops_cron(hr):
    import re
    checked = 0
    for route in hr.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/fin/"):
            continue
        concrete = re.sub(r"\{[^}]+\}", "owned", path)
        for method in getattr(route, "methods", ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            r = hr.client.request(method, concrete, headers=hr.headers("ops_cron"), json={"request_id": rid()})
            assert r.status_code == 403, (method, concrete, r.status_code)
            checked += 1
    assert checked > 50
