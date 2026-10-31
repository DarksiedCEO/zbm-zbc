"""Founder alerts (ADR 0027; red team OPS-H2, OPS-H5, CH-6..CH-8): Andre gets ONE email per window for what needs him,
deduplicated, rate-limited, retried with backoff, never carrying client contact, card, bank or hold-reason data, and
an alert failure never blocks bookkeeping. Nothing leaves the machine: Resend is an httpx.MockTransport or a fixture."""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

import httpx
import pytest

import config as config_mod
import founder_alerts as FA
import mail as MAIL
from helpers import ANDRE_TOKEN, NOW, Harness, make_fakes, rid
from ports import MailOutcome

TO = "andre.alerts@example.com"
FROM = "Z Best Media Alerts <alerts@zbestmedia.com>"
ALERTS_JOB = "/fin/v1/jobs/founder-alerts/run"


def cfg(**over) -> FA.AlertSettings:
    s = FA.settings_from_env({"FOUNDER_ALERT_EMAIL": TO, "FOUNDER_ALERT_FROM": FROM}, True)
    for k, v in over.items():
        setattr(s, k, v)
    return s


def C(cat="finance_high_break", ref="fin-brk-AAAA", src="finance"):
    return FA.Condition.make(cat, src, ref)


# --------------------------------------------------------------------------------------------- the engine

def test_a_condition_is_emailed_once_then_only_as_a_reminder():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    s = FA.run(st, [C()], NOW, cfg(), mail)
    assert s["sent"] == 1 and len(mail.sent) == 1
    for minutes in (5, 65, 125, 23 * 60):
        assert FA.run(st, [C()], NOW + timedelta(minutes=minutes), cfg(), mail)["sent"] == 0
    s = FA.run(st, [C()], NOW + timedelta(hours=24, minutes=1), cfg(), mail)
    assert s["sent"] == 1 and "(reminder)" in mail.sent[-1]["text"]


def test_an_alert_storm_is_one_digest_per_window():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    FA.run(st, [C(ref="fin-brk-1")], NOW, cfg(), mail)
    storm = [C(ref=f"fin-brk-{i}") for i in range(1, 200)]
    for minutes in range(5, 60, 5):
        assert FA.run(st, storm, NOW + timedelta(minutes=minutes), cfg(), mail)["sent"] == 0
    s = FA.run(st, storm, NOW + timedelta(minutes=60), cfg(), mail)
    assert s["sent"] == 1 and len(mail.sent) == 2
    text = mail.sent[-1]["text"]
    assert "198 item(s)" in mail.sent[-1]["subject"] and "and 158 more" in text
    assert FA.run(st, storm, NOW + timedelta(minutes=125), cfg(), mail)["sent"] == 0       # all notified


def test_the_daily_cap_holds_even_with_a_tiny_window():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    c = cfg(window_min=5, max_per_day=3)
    for i in range(12):
        FA.run(st, [C(ref=f"fin-brk-{j}") for j in range(i + 1)], NOW + timedelta(minutes=5 * i), c, mail)
    assert len(mail.sent) == 3


def test_a_cleared_condition_that_comes_back_is_emailed_again():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    FA.run(st, [C()], NOW, cfg(), mail)
    s = FA.run(st, [], NOW + timedelta(minutes=10), cfg(), mail)
    assert s["cleared"] == 1 and st["open"] == {}
    FA.run(st, [C()], NOW + timedelta(minutes=70), cfg(), mail)
    assert len(mail.sent) == 2 and "Cleared since the last alert: 1" in mail.sent[-1]["text"]


def test_a_failed_digest_is_retried_frozen_with_backoff_then_dropped_with_an_alert_line(caplog):
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    mail.outcomes = [FA.SendOutcome(False, code="503:x")] * 20
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        FA.run(st, [C()], NOW, cfg(), mail)
        first = dict(mail.attempts[0])
        t = NOW
        waits = []
        while st["pending"] is not None:
            nxt = FA._parse(st["pending"]["next_attempt_at"])
            waits.append(int((nxt - t).total_seconds() // 60))
            assert FA.run(st, [C(), C(ref="fin-brk-NEW")], nxt - timedelta(minutes=1), cfg(), mail)["waiting"] == \
                "backoff"
            t = nxt
            FA.run(st, [C(), C(ref="fin-brk-NEW")], t, cfg(), mail)
    assert waits == [5, 15, 30, 60, 120, 240, 480]
    assert len(mail.attempts) == FA.MAX_ATTEMPTS and all(a == first for a in mail.attempts)   # same text, same key
    assert sum(waits) / 60 < 24                                                      # inside Resend's 24 h window
    assert "founder_alert_send_failed" in caplog.text and "founder_alert_dropped" in caplog.text
    assert TO not in caplog.text
    mail.outcomes = []
    FA.run(st, [C(), C(ref="fin-brk-NEW")], t + timedelta(hours=2), cfg(), mail)
    assert len(mail.sent) == 1 and "fin-brk-AAAA" in mail.sent[0]["text"] and "fin-brk-NEW" in mail.sent[0]["text"]


def test_a_permanent_refusal_is_dropped_at_once(caplog):
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    mail.outcomes = [FA.SendOutcome(False, permanent=True, code="403:validation_error")]
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        s = FA.run(st, [C()], NOW, cfg(), mail)
    assert s["dropped"] == 1 and st["pending"] is None and "founder_alert_dropped" in caplog.text


def test_a_sender_that_raises_is_a_failed_attempt_not_a_crash():
    class Boom:
        def send(self, *a):
            raise RuntimeError("boom")
    st = FA.empty_state()
    s = FA.run(st, [C()], NOW, cfg(), Boom())
    assert s["send_failed"] == 1 and st["pending"]["attempts"] == 1


def test_off_until_configured_and_a_wrong_sender_is_off_not_a_crash():
    assert not FA.settings_from_env({}, True).enabled
    assert not FA.settings_from_env({"FOUNDER_ALERT_EMAIL": TO}, True).enabled
    assert not FA.settings_from_env({"FOUNDER_ALERT_EMAIL": TO, "FOUNDER_ALERT_FROM": FROM}, False).enabled
    for bad in ("andre@gmail.com", "alerts@zbestmedia.com.evil.example", "Z <noreply@zbestmedia.com>"):
        s = FA.settings_from_env({"FOUNDER_ALERT_EMAIL": TO, "FOUNDER_ALERT_FROM": bad}, True)
        assert not s.enabled and "FOUNDER_ALERT_FROM" in s.problem
    for good in ("alerts@zbestmedia.com", "billing@zbestmedia.com", "Z Best Media <billing@zbestmedia.com>"):
        assert FA.settings_from_env({"FOUNDER_ALERT_EMAIL": TO, "FOUNDER_ALERT_FROM": good}, True).enabled
    s = FA.settings_from_env({"FOUNDER_ALERT_EMAIL": "two@a.example,three@b.example", "FOUNDER_ALERT_FROM": FROM},
                             True)
    assert not s.enabled
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    out = FA.run(st, [C()], NOW, FA.settings_from_env({}, True), mail)
    assert out["waiting"] == "alerts off" and mail.attempts == []


def test_an_email_carries_identifiers_and_categories_only():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    poison = [C(ref="ap@client.example"), C(ref="4242 4242 4242 4242"), C(ref="OFAC SDN match: Garcia Holdings"),
              C("compliance_hold", ref="hold-abcdefghijklmnopqrst", src="compliance"),
              C("ledger_unreachable", ref="unreachable", src="ledger")]
    FA.run(st, poison, NOW, cfg(), mail)
    (m,) = mail.sent
    blob = m["subject"] + m["text"]
    for bad in ("@client.example", "4242", "OFAC SDN", "Garcia", "$", "sanction"):
        assert bad not in blob, bad
    assert "hold-abcdefghijklmnopqrst" in blob and "ref-" in blob
    assert m["subject"].startswith("ZBM alert (critical)")
    with pytest.raises(ValueError):
        FA.Condition.make("hold_reason_text", "finance", "x")


def test_resend_adapter_sends_plain_text_to_the_one_address_with_the_idempotency_key():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "f1"})
    m = FA.ResendAlertMail("re_test_AlertsSimulatedKey000000", FROM, TO, transport=httpx.MockTransport(handler))
    assert m.send("s", "t", "zbm-alert-1") == FA.SendOutcome(True, code="200")
    (req,) = seen
    body = json.loads(req.content)
    assert set(body) == {"from", "to", "subject", "text"} and body["to"] == [TO] and body["from"] == FROM
    assert req.headers["idempotency-key"] == "zbm-alert-1" and "re_test_" not in repr(m)

    def status(code, name):
        return FA.ResendAlertMail("re_k", FROM, TO, transport=httpx.MockTransport(
            lambda r: httpx.Response(code, json={"name": name}))).send("s", "t", "k")
    assert not status(500, "x").permanent and not status(429, "rate_limit_exceeded").permanent
    assert status(409, "invalid_idempotent_request").permanent and status(403, "validation_error").permanent

    def boom(r):
        raise httpx.ConnectError("down", request=r)
    out = FA.ResendAlertMail("re_k", FROM, TO, transport=httpx.MockTransport(boom)).send("s", "t", "k")
    assert not out.sent and not out.permanent


def test_ledger_probe_reads_health_only():
    seen = []

    def ok(r):
        seen.append(str(r.url))
        return httpx.Response(200, json={"status": "ok", "service": "ledger-rust"})
    assert FA.ledger_probe("http://ledger.railway.internal:8090", httpx.MockTransport(ok)) is True
    assert seen == ["http://ledger.railway.internal:8090/health"]
    assert FA.ledger_probe("http://l:1", httpx.MockTransport(lambda r: httpx.Response(503))) is False
    assert FA.ledger_probe(None) is None


def test_state_file_is_atomic_private_and_survives_a_corrupt_file(tmp_path, caplog):
    sf = FA.StateFile(str(tmp_path))
    st = FA.empty_state()
    FA.run(st, [C()], NOW, cfg(), FA.FixtureAlertMail())
    sf.save(st)
    assert oct(os.stat(tmp_path / FA.STATE_NAME).st_mode & 0o777) == "0o600"
    assert sf.load()["open"] == st["open"]
    (tmp_path / FA.STATE_NAME).write_text("{not json")
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        assert sf.load() == FA.empty_state()
    assert "founder_alert_state_corrupt" in caplog.text and (tmp_path / (FA.STATE_NAME + ".corrupt")).exists()


# --------------------------------------------------------------------------------------------- Finance wiring

def harness(alerts=True, data_dir=None, mail=None, **env) -> Harness:
    from clock import FixedClock
    clock = FixedClock(NOW)
    f = make_fakes(clock)
    f["client_mail"] = MAIL.FixtureClientMail()
    e = dict(env)
    if alerts:
        e.update(FOUNDER_ALERT_EMAIL=TO, FOUNDER_ALERT_FROM=FROM)
    hr = Harness(clock=clock, fakes=f, env=e, data_dir=data_dir).ready()
    hr.alert_mail = mail or FA.FixtureAlertMail()
    hr.svc.alerts.sender = hr.alert_mail
    if alerts:
        hr.svc.alerts.cfg.problem = None          # the harness has no Resend key file; the fixture sender stands in
    return hr


def run_alerts(hr, caller="ops_alerts"):
    return hr.ok(hr.post(ALERTS_JOB, {"request_id": rid()}, caller=caller))["summary"]


def brk(hr, leg, severity=None, **extra):
    bid = f"fin-brk-{leg.upper()[:10]}{len(hr.svc.db['breaks'])}"
    hr.svc.db["breaks"][bid] = {"break_id": bid, "leg": leg, "status": "open", "subject": "zbm:invoice:fin-inv-1",
                                "opened_at": "2026-10-02T17:00:00Z", "opened_on": "2026-10-02",
                                "note": "client ap@client.example disputed $1,200.00", **(
                                    {"severity": severity} if severity else {}), **extra}
    return bid


@pytest.mark.parametrize("leg,extra,category", [
    ("client_mail_failed", {"severity": "high"}, "client_mail_failed"),
    ("stripe_unknown_session", {"severity": "high"}, "unknown_paid_checkout"),
    ("stripe_dispute", {}, "payment_disputed"),
    ("dispute_receivable", {}, "payment_disputed"),
    ("compliance_hold", {}, "compliance_hold"),
    ("compliance_unverified", {}, "compliance_hold"),
    ("1060", {"recon_id": "fin-rec-1"}, "reconciliation_mismatch"),
    ("restore_reimport_required", {"severity": "high"}, "finance_high_break"),
])
def test_each_finance_condition_becomes_its_category(leg, extra, category):
    hr = harness()
    bid = brk(hr, leg, **extra)
    s = run_alerts(hr)
    assert s["sent"] == 1
    (m,) = hr.alert_mail.sent
    assert f"{category}  finance  {bid}" in m["text"]
    assert "ap@client.example" not in m["text"] and "1,200" not in m["text"]


def test_low_breaks_resolved_breaks_and_explained_breaks_do_not_alert():
    hr = harness()
    brk(hr, "client_mail_no_contact")
    brk(hr, "stripe_refund", status="resolved", severity="high")
    brk(hr, "1060", status="explained", recon_id="fin-rec-1")
    assert run_alerts(hr)["open"] == 0 and hr.alert_mail.attempts == []


def test_a_real_permanent_mail_failure_reaches_andre():
    from test_client_mail import paid, set_contact
    hr = harness()
    set_contact(hr)
    paid(hr)
    hr.f["client_mail"].outcomes = [MailOutcome("permanent", code="403:validation_error")]
    hr.ok(hr.post("/fin/v1/jobs/client-mail/run", {"request_id": rid()}, caller="ops_cron"))
    run_alerts(hr)
    (m,) = hr.alert_mail.sent
    assert "client_mail_failed" in m["text"] and "ap@client-seven.example" not in m["text"]


def test_compliance_holds_and_expiring_rules_are_read_from_compliance():
    hr = harness()
    hr.cmp.open_holds = {"client-9": ["hold-abcdefghijklmnopqrst"]}
    hr.cmp.rule_expiry_answer = {"available": True, "within_30_days": ["US-OFAC-04", "HR-14"],
                                 "within_14_days": ["US-OFAC-04"], "within_7_days": [], "next": None}
    run_alerts(hr)
    (m,) = hr.alert_mail.sent
    assert "compliance_hold  compliance  hold-abcdefghijklmnopqrst" in m["text"]
    assert "compliance_rule_expiring  compliance  US-OFAC-04" in m["text"] and "client-9" not in m["text"]
    assert "HR-14" not in m["text"]                                   # 30 days out: not yet


def test_compliance_unreachable_alerts_after_n_minutes_not_on_a_blip():
    hr = harness()
    hr.cmp.holds_available = False
    assert run_alerts(hr)["open"] == 0
    hr.clock.advance(minutes=10)
    assert run_alerts(hr)["open"] == 0
    hr.clock.advance(minutes=6)
    run_alerts(hr)
    assert "compliance_unreachable" in hr.alert_mail.sent[0]["text"]


def test_the_ledger_down_for_n_minutes_alerts_and_the_job_needs_no_ledger(tmp_path):
    hr = harness(data_dir=str(tmp_path))
    hr.svc.alerts.probe = lambda: False
    hr.ledger.fail_all = True                     # every ledger write fails: the alert path must not need one
    before = (tmp_path / "fin_log.jsonl").read_bytes()
    calls = hr.ledger.calls
    assert run_alerts(hr)["open"] == 0
    hr.clock.advance(minutes=14)
    assert run_alerts(hr)["open"] == 0
    hr.clock.advance(minutes=2)
    s = run_alerts(hr)
    assert s["sent"] == 1 and s["state_saved"] is True
    assert "ledger_unreachable" in hr.alert_mail.sent[0]["text"]
    assert (tmp_path / FA.STATE_NAME).exists()
    assert (tmp_path / "fin_log.jsonl").read_bytes() == before                  # never in the evidence log
    assert hr.ledger.calls == calls                                              # never asked the ledger to record
    hr.svc.alerts.probe = lambda: True
    hr.clock.advance(minutes=5)
    assert run_alerts(hr)["cleared"] == 1


def test_alert_failure_never_blocks_bookkeeping_and_is_logged(caplog):
    class Boom:
        def send(self, *a):
            raise RuntimeError("resend down")
    hr = harness(mail=Boom())
    brk(hr, "stripe_dispute")
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        s = run_alerts(hr)
    assert s["send_failed"] == 1 and "founder_alert_send_failed" in caplog.text
    from test_client_mail import paid
    inv = paid(hr)
    assert hr.svc.db["invoices"][inv["invoice_id"]]["status"] == "paid"


def test_the_state_write_failing_is_logged_and_answered(tmp_path, caplog):
    hr = harness(data_dir=str(tmp_path))
    brk(hr, "stripe_dispute")

    def nope(st):
        raise PermissionError("read-only volume")
    hr.svc.alerts.state.save = nope
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        s = run_alerts(hr)
    assert s["state_saved"] is False and "founder_alert_state_write_failed" in caplog.text


def test_unconfigured_alerts_send_nothing_and_say_why():
    hr = harness(alerts=False)
    brk(hr, "stripe_dispute")
    s = run_alerts(hr)
    assert s["enabled"] is False and "FOUNDER_ALERT_EMAIL" in s["why_off"] and hr.alert_mail.attempts == []


def test_the_alerts_job_runs_while_finance_is_quiesced_for_a_backup():
    hr = harness()
    hr.ok(hr.post("/fin/v1/backup/quiesce", {"request_id": rid()}, andre=ANDRE_TOKEN))
    brk(hr, "stripe_dispute")
    assert run_alerts(hr)["sent"] == 1


def test_ops_alerts_reaches_only_the_alerts_job():
    hr = harness()
    for path in ("/fin/v1/jobs/client-mail/run", "/fin/v1/jobs/stripe-sessions/run", "/fin/v1/reconciliations/run",
                 "/fin/v1/payout-runs", "/fin/v1/invoices"):
        assert hr.post(path, {"request_id": rid()}, caller="ops_alerts").status_code == 403, path
    assert hr.get("/fin/v1/breaks", caller="ops_alerts").status_code == 403
    assert hr.post(ALERTS_JOB, {"request_id": rid()}, caller="ops_cron").status_code == 403
    assert hr.post(ALERTS_JOB, {"request_id": rid()}, caller="onboarding").status_code == 403
    assert hr.post(ALERTS_JOB, {"request_id": rid()}, caller="scheduler").status_code == 200


def test_the_job_answer_carries_counts_only():
    hr = harness()
    brk(hr, "stripe_dispute")
    s = run_alerts(hr)
    assert all(isinstance(v, (int, bool, type(None))) or k in ("waiting", "why_off") for k, v in s.items())
    assert "fin-brk" not in json.dumps(s)


def test_settings_load_from_the_environment():
    from helpers import base_env
    s = config_mod.load(base_env(FOUNDER_ALERT_EMAIL=TO, FOUNDER_ALERT_FROM=FROM, FIN_ALERT_WINDOW_MIN="30"))
    assert s.founder_alerts.to == TO and s.founder_alerts.window_min == 30
    assert not s.founder_alerts.enabled and "RESEND_API_KEY" in s.founder_alerts.problem
    s = config_mod.load(base_env(FIN_ALERT_WINDOW_MIN="1"))
    assert not s.founder_alerts.enabled


# --------------------------------------------------------------------------------------------- the daily all-clear

def daily_cfg(**over):
    return cfg(daily_summary=True, daily_hour=8, tz="America/Los_Angeles", **over)


def test_the_daily_summary_is_off_unless_switched_on():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    FA.run(st, [], NOW.replace(hour=16), cfg(), mail)                  # 09:00 Los Angeles
    assert mail.attempts == []
    s = FA.settings_from_env({"FOUNDER_ALERT_EMAIL": TO, "FOUNDER_ALERT_FROM": FROM, "FIN_ALERT_DAILY_SUMMARY": "1",
                              "FIN_ALERT_DAILY_HOUR": "7"}, True)
    assert s.daily_summary is True and s.daily_hour == 7
    assert FA.settings_from_env({"FIN_ALERT_DAILY_SUMMARY": "yes"}, True).daily_summary is False


def test_one_all_clear_a_day_after_the_hour_counts_only():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    before = NOW.replace(hour=14, minute=55)                          # 07:55 Los Angeles
    FA.run(st, [], before, daily_cfg(), mail)
    assert mail.attempts == []
    at8 = NOW.replace(hour=15, minute=5)                               # 08:05 Los Angeles
    s = FA.run(st, [], at8, daily_cfg(), mail)
    assert s["daily_sent"] is True and len(mail.sent) == 1
    assert "all clear" in mail.sent[0]["subject"].lower()
    for minutes in (5, 60, 600):
        FA.run(st, [], at8 + timedelta(minutes=minutes), daily_cfg(), mail)
    assert len(mail.sent) == 1
    FA.run(st, [], at8 + timedelta(days=1), daily_cfg(), mail)
    assert len(mail.sent) == 2


def test_the_daily_summary_with_open_items_names_categories_and_counts_only():
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    at8 = NOW.replace(hour=15, minute=5)
    conds = [C(ref="fin-brk-1"), C(ref="fin-brk-2"), C("compliance_hold", ref="hold-abcdefghijklmnopqrst",
                                                       src="compliance")]
    FA.run(st, conds, at8, daily_cfg(), mail)
    daily = [m for m in mail.sent if m["subject"].startswith("ZBM daily")]
    assert len(daily) == 1
    t = daily[0]["text"]
    assert "3 open" in daily[0]["subject"] and "finance_high_break: 2" in t and "compliance_hold: 1" in t
    assert "fin-brk" not in t and "hold-" not in t


def test_a_failed_daily_summary_is_retried_with_the_same_text_then_given_up_for_the_day(caplog):
    st, mail = FA.empty_state(), FA.FixtureAlertMail()
    mail.outcomes = [FA.SendOutcome(False, code="503:x")] * 10
    at8 = NOW.replace(hour=15, minute=5)
    with caplog.at_level(logging.ERROR, logger="finance.alert"):
        for i in range(8):
            FA.run(st, [], at8 + timedelta(minutes=5 * i), daily_cfg(), mail)
    assert len(mail.attempts) == FA.DAILY_MAX_ATTEMPTS
    assert len({(a["subject"], a["text"], a["idempotency_key"]) for a in mail.attempts}) == 1
    assert "founder_daily_summary_failed" in caplog.text


def test_finance_passes_its_business_zone_to_the_daily_summary():
    hr = harness(FIN_ALERT_DAILY_SUMMARY="1")
    assert hr.svc.alerts.cfg.daily_summary is True and hr.svc.alerts.cfg.tz == "America/Los_Angeles"
