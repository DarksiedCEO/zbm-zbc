"""CH-10 (red team chaos): an invoice's due date counts from the BUSINESS day (FIN_BUSINESS_TZ, default
America/Los_Angeles), never from the UTC date. Around midnight and the DST changes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import config as config_mod
from clock import FixedClock
from helpers import ANDRE_TOKEN, Harness, base_env, rid
from test_media_billing import LEGAL


def _issued_on(at_utc: datetime, due_days: int = 15, **env) -> dict:
    hr = Harness(clock=FixedClock(at_utc), env=env or None).ready()
    body = {"request_id": rid(), "entity": "zbm", "client_id": "zbm-tz-1", "kind": "service",
            "lines": [{"line_code": "strategy_services", "quantity": 1, "unit_price": "100.00"}],
            "payment_methods": ["ach"], "due_days": due_days, "legal_ref": LEGAL}
    inv = hr.ok(hr.post("/fin/v1/invoices", body, caller="onboarding"), 201)["invoice"]
    out = hr.ok(hr.post(f"/fin/v1/invoices/{inv['invoice_id']}/decision",
                        {"request_id": rid(), "content_sha256": inv["content_sha256"], "decision": "approve"},
                        andre=ANDRE_TOKEN))["invoice"]
    out["_journal_dates"] = [e["effective_date"] for e in hr.svc.entries if e["memo_code"] == "F11"]
    return out


def test_ch10_new_years_eve_evening_in_los_angeles_is_still_dec_31():
    # Dec 31 2026, 6:30 pm PT = Jan 1 2027 02:30 UTC (the red team's case): due Jan 15, not Jan 16
    inv = _issued_on(datetime(2027, 1, 1, 2, 30, tzinfo=timezone.utc))
    assert inv["due_at"] == "2027-01-15"
    assert inv["_journal_dates"] == ["2026-12-31"]          # the journal and the due date agree on the day


@pytest.mark.parametrize("at_utc,due", [
    (datetime(2026, 10, 3, 6, 59, tzinfo=timezone.utc), "2026-10-17"),    # Oct 2, 11:59 pm PDT
    (datetime(2026, 10, 3, 7, 0, tzinfo=timezone.utc), "2026-10-18"),     # Oct 3, 00:00 PDT
    (datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc), "2026-03-23"),     # Mar 8, 1:59 am PST (DST starts at 2)
    (datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc), "2026-03-23"),     # Mar 8, 3:00 am PDT
    (datetime(2026, 3, 9, 6, 59, tzinfo=timezone.utc), "2026-03-23"),     # Mar 8, 11:59 pm PDT
    (datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc), "2026-11-16"),    # Nov 1, 1:30 am PDT (first pass)
    (datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc), "2026-11-16"),    # Nov 1, 1:30 am PST (second pass)
    (datetime(2026, 11, 2, 7, 59, tzinfo=timezone.utc), "2026-11-16"),    # Nov 1, 11:59 pm PST
    (datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc), "2026-11-17"),     # Nov 2, 00:00 PST
])
def test_ch10_due_dates_around_midnight_and_dst(at_utc, due):
    assert _issued_on(at_utc)["due_at"] == due


def test_ch10_the_zone_is_configurable_and_a_bad_one_refuses_start():
    # Honolulu: Oct 3 06:59 UTC is still Oct 2 (8:59 pm HST)
    assert _issued_on(datetime(2026, 10, 3, 9, 59, tzinfo=timezone.utc),
                      FIN_BUSINESS_TZ="Pacific/Honolulu")["due_at"] == "2026-10-17"
    assert config_mod.load(base_env()).business_tz == "America/Los_Angeles"
    for bad in ("Mars/Olympus", "PST; rm -rf", "UTC+8"):
        with pytest.raises(RuntimeError, match="FIN_BUSINESS_TZ"):
            config_mod.load(base_env(FIN_BUSINESS_TZ=bad))
