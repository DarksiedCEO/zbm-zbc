"""Health degradation must reach the actual founder-alert job, not only a poller unit test."""
import founder_alerts as FA
from helpers import Harness, rid


def configured_alerts(**env):
    hr = Harness(env={"FOUNDER_ALERT_EMAIL": "founder@example.com",
                      "FOUNDER_ALERT_FROM": "alerts@zbestmedia.com", **env}).ready()
    hr.svc.ports.founder_mail = FA.FixtureAlertMail()
    hr.svc.alerts.sender = hr.svc.ports.founder_mail
    hr.svc.alerts.cfg.problem = None  # fixture sender replaces the external email provider
    return hr


def tick(hr):
    return hr.ok(hr.post('/fin/v1/jobs/founder-alerts/run', {'request_id': rid()}, caller='ops_alerts'))['summary']


def test_missing_health_targets_reaches_founder_as_configuration_alert():
    hr = configured_alerts()
    result = tick(hr)
    assert result['sent'] == 1
    message = hr.svc.alerts.sender.sent[0]['text']
    assert 'health_poll_unconfigured' in message
    assert 'no_targets' in message
    assert result['state_saved'] is True


def health_responses(monkeypatch, answers):
    import httpx
    import health_poll
    actual_client = httpx.Client
    seen = []

    def handle(request):
        seen.append(request)
        status, body = answers[request.url.host]
        return httpx.Response(status, json=body)

    def client(*args, **kwargs):
        return actual_client(*args, **{**kwargs, 'transport': httpx.MockTransport(handle)})

    monkeypatch.setattr(health_poll.httpx, 'Client', client)
    return seen


TARGETS = ','.join(f'{name}=http://{name}.test' for name in ('finance', 'legal', 'compliance', 'ledger'))


def healthy():
    return {f'{name}.test': (200, {'status': 'ok'}) for name in ('finance', 'legal', 'compliance', 'ledger')}


def test_service_degradation_is_emailed_after_ten_minutes_and_clears(monkeypatch):
    hr = configured_alerts(OPS_HEALTH_URLS=TARGETS)
    answers = healthy()
    answers['legal.test'] = (503, {'status': 'degraded', 'degraded_reasons': ['maintenance: private detail']})
    seen = health_responses(monkeypatch, answers)
    assert tick(hr)['open'] == 0
    hr.clock.advance(minutes=10)
    result = tick(hr)
    assert result['sent'] == 1
    text = hr.svc.alerts.sender.sent[0]['text']
    assert 'service_unhealthy  legal  maintenance' in text
    assert 'private detail' not in text
    assert all(r.url.path == '/health' and 'authorization' not in r.headers for r in seen)
    answers['legal.test'] = (200, {'status': 'ok'})
    hr.clock.advance(minutes=1)
    assert tick(hr)['cleared'] == 1


def test_health_history_survives_dispatcher_recreation(tmp_path, monkeypatch):
    hr = configured_alerts(OPS_HEALTH_URLS=TARGETS, FIN_DATA_DIR=str(tmp_path))
    answers = healthy()
    answers['finance.test'] = (200, {'status': 'ok', 'write_ok': False})
    health_responses(monkeypatch, answers)
    assert tick(hr)['open'] == 0
    sender = hr.svc.alerts.sender
    hr.svc._founder_alerts = None
    hr.svc.alerts.sender = sender
    hr.clock.advance(minutes=10)
    assert tick(hr)['sent'] == 1
    assert 'service_unhealthy  finance  write_refused' in sender.sent[0]['text']


def test_health_probe_exception_does_not_suppress_existing_finance_alert():
    hr = configured_alerts()
    from test_founder_alerts import brk
    brk(hr, 'stripe_dispute')

    def broken(state, now):
        raise ValueError('private provider diagnostic')

    hr.svc.alerts.health_probe = broken
    result = tick(hr)
    assert result['sent'] == 1 and result['open'] == 2
    text = hr.svc.alerts.sender.sent[0]['text']
    assert 'payment_disputed' in text and 'health_poll_unconfigured' in text
    assert 'private provider diagnostic' not in text


def test_intermittent_health_failure_reaches_founder(monkeypatch):
    hr = configured_alerts(OPS_HEALTH_URLS=TARGETS)
    answers = healthy()
    health_responses(monkeypatch, answers)
    for n in range(5):
        answers['legal.test'] = (503, {'status': 'maintenance'}) if n % 2 == 0 else (200, {'status': 'ok'})
        tick(hr)
        hr.clock.advance(minutes=5)
    assert any('service_flapping  legal  maintenance' in mail['text'] for mail in hr.svc.alerts.sender.sent)


def test_missing_required_service_is_an_alert_even_when_polled_services_are_healthy(monkeypatch):
    hr = configured_alerts(OPS_HEALTH_URLS='finance=http://finance.test')
    health_responses(monkeypatch, healthy())
    assert tick(hr)['open'] == 3
    text = hr.svc.alerts.sender.sent[0]['text']
    for name in ('compliance', 'ledger', 'legal'):
        assert f'health_poll_unconfigured  {name}  missing' in text


def test_unconfigured_health_alert_is_deduplicated():
    hr = configured_alerts()
    assert tick(hr)['sent'] == 1
    hr.clock.advance(minutes=65)
    assert tick(hr)['sent'] == 0
    assert len(hr.svc.alerts.sender.sent) == 1


def test_code_shaped_private_health_values_never_reach_founder_email(monkeypatch):
    import secrets
    for field in ('status', 'reasons', 'degraded_reasons', 'warnings'):
        with monkeypatch.context() as context:
            canary = 'private' + secrets.token_hex(12)
            hr = configured_alerts(OPS_HEALTH_URLS=TARGETS)
            answers = healthy()
            body = {'status': 'ok', field: canary if field == 'status' else [canary]}
            answers['legal.test'] = (200, body)
            health_responses(context, answers)
            assert tick(hr)['open'] == 0
            hr.clock.advance(minutes=10)
            assert tick(hr)['sent'] == 1
            text = hr.svc.alerts.sender.sent[0]['text']
            assert canary not in text
            assert 'service_unhealthy' in text
