"""ADR 0026: the Railway cron service's caller (``ops_cron``) reaches the hourly housekeeping jobs (Stripe page sweep and
Compliance hold re-check, client mail) and the daily reconciliation, and nothing else: not payout runs, releases,
refunds, sweeps, receipts, invoices or reads. ``scheduler`` keeps what it had."""

from __future__ import annotations

import pytest

from helpers import Harness, rid


@pytest.fixture
def hr():
    return Harness().ready()


@pytest.mark.parametrize("path", ["/fin/v1/jobs/stripe-sessions/run", "/fin/v1/jobs/client-mail/run",
                                  "/fin/v1/reconciliations/run"])
def test_ops_cron_runs_the_housekeeping_jobs_and_the_daily_reconciliation(hr, path):
    r = hr.post(path, {"request_id": rid()}, caller="ops_cron")
    assert r.status_code in (200, 201), r.text


@pytest.mark.parametrize("method,path", [
    ("POST", "/fin/v1/jobs/accrual/run"), ("POST", "/fin/v1/jobs/rail-sync/run"), ("POST", "/fin/v1/jobs/tax-sync/run"),
    ("POST", "/fin/v1/payout-runs"), ("POST", "/fin/v1/payout-batches/b-1/release"),
    ("POST", "/fin/v1/refunds/camp-1"), ("POST", "/fin/v1/treasury/sweeps"), ("POST", "/fin/v1/treasury/funding"),
    ("POST", "/fin/v1/client-receipts/fin-crc-X/send"), ("POST", "/fin/v1/invoices"),
    ("POST", "/fin/v1/stripe/events"), ("POST", "/fin/v1/bank/events"), ("POST", "/fin/v1/close/zbm/2026-10/tasks/x"),
    ("POST", "/fin/v1/stripe/reimport"), ("POST", "/fin/v1/backup/quiesce"),
    ("GET", "/fin/v1/breaks"), ("GET", "/fin/v1/invoices/fin-inv-X"), ("GET", "/fin/v1/journal/zbm/entries"),
])
def test_ops_cron_reaches_nothing_else(hr, method, path):
    if method == "POST":
        r = hr.post(path, {"request_id": rid()}, caller="ops_cron")
    else:
        r = hr.get(path, caller="ops_cron")
    assert r.status_code == 403, (path, r.status_code)


def test_the_scheduler_still_runs_every_job(hr):
    for job in ("stripe-sessions", "client-mail", "accrual"):
        assert hr.post(f"/fin/v1/jobs/{job}/run", {"request_id": rid()}, caller="scheduler").status_code == 200


def test_the_hourly_job_is_repeatable_and_recorded_each_run(hr):
    a = hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="ops_cron"))
    hr.clock.advance(minutes=60)
    b = hr.ok(hr.post("/fin/v1/jobs/stripe-sessions/run", {"request_id": rid()}, caller="ops_cron"))
    assert a["already_ran"] is False and b["already_ran"] is False
