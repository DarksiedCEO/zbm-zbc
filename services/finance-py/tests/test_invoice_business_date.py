"""Invoice calendar-day terms start on Finance's existing Los Angeles business date."""
from datetime import datetime, timezone

import pytest

from clock import FixedClock
from helpers import ANDRE_TOKEN, Harness, rid
from test_media_billing import SERVICE_LEGAL


def draft(h, days=15):
    return h.ok(h.post('/fin/v1/invoices', {'request_id': rid(), 'entity': 'zbm', 'client_id': 'zbm-date-1',
                                         'kind': 'service', 'lines': [{'line_code': 'strategy_services',
                                         'quantity': 1, 'unit_price': '100.00'}], 'payment_methods': ['ach'],
                                         'due_days': days, 'legal_ref': SERVICE_LEGAL}, caller='onboarding'), 201)['invoice']


def approve(h, inv, request=None):
    body = request or {'request_id': rid(), 'content_sha256': inv['content_sha256'], 'decision': 'approve'}
    response = h.post('/fin/v1/invoices/' + inv['invoice_id'] + '/decision', body, andre=ANDRE_TOKEN)
    return h.ok(response)['invoice'], body


@pytest.mark.parametrize('instant,day,due', [
    ('2027-01-01T02:30:00+00:00', '2026-12-31', '2027-01-15'),
    ('2026-10-03T06:59:59+00:00', '2026-10-02', '2026-10-17'),
    ('2026-10-03T07:00:00+00:00', '2026-10-03', '2026-10-18'),
    ('2026-03-08T09:59:59+00:00', '2026-03-08', '2026-03-23'),
    ('2026-03-08T10:00:00+00:00', '2026-03-08', '2026-03-23'),
    ('2026-03-09T06:59:59+00:00', '2026-03-08', '2026-03-23'),
    ('2026-11-01T08:30:00+00:00', '2026-11-01', '2026-11-16'),
    ('2026-11-01T09:30:00+00:00', '2026-11-01', '2026-11-16'),
    ('2026-11-02T07:59:59+00:00', '2026-11-01', '2026-11-16'),
    ('2026-11-02T08:00:00+00:00', '2026-11-02', '2026-11-17'),
])
def test_invoice_day_and_journal_agree_at_midnight_and_dst(instant, day, due):
    h = Harness(clock=FixedClock(datetime.fromisoformat(instant))).ready()
    inv, _ = approve(h, draft(h))
    assert inv['due_at'] == due
    assert inv['issued_at'] == datetime.fromisoformat(instant).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    assert [e['effective_date'] for e in h.svc.entries if e['memo_code'] == 'F11'] == [day]


@pytest.mark.parametrize('days,expected', [(0, '2026-10-02'), (1, '2026-10-03'), (120, '2027-01-30')])
def test_terms_are_calendar_days_including_weekends(days, expected):
    h = Harness(clock=FixedClock(datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc))).ready()
    inv, _ = approve(h, draft(h, days))
    assert inv['due_at'] == expected


def test_issued_dates_replay_unchanged_after_restart_and_clock_change(tmp_path):
    h = Harness(clock=FixedClock(datetime(2026, 10, 3, 6, 59, tzinfo=timezone.utc)),
                data_dir=str(tmp_path / 'data')).ready()
    issued, request = approve(h, draft(h))
    count = len(h.svc.entries)
    h.clock.advance(minutes=10)
    replay, _ = approve(h, issued, request)
    assert replay == issued and len(h.svc.entries) == count
    h2 = Harness(clock=h.clock, data_dir=h.settings.data_dir, ledger=h.ledger, fakes=h.f)
    assert h2.svc.db['invoices'][issued['invoice_id']]['due_at'] == issued['due_at']
    replay, _ = approve(h2, issued, request)
    assert replay == issued and len(h2.svc.entries) == count
    h2.clock.advance(days=4)
    stale = h2.post('/fin/v1/invoices/' + issued['invoice_id'] + '/decision', request, andre=ANDRE_TOKEN)
    assert stale.status_code == 409
    response = h2.post('/fin/v1/invoices/' + issued['invoice_id'] + '/decision',
                       {**request, 'request_id': rid()}, andre=ANDRE_TOKEN)
    assert response.status_code == 409
    assert h2.svc.db['invoices'][issued['invoice_id']]['due_at'] == issued['due_at']
