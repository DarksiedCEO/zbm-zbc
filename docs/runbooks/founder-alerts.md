# Founder alerts: what the email means and what to do

ADR 0027. Finance emails you when something needs you. Each email is a **digest**: everything new (or still open a day
later) since the last email, at most one email per hour, at most 30 a day. It names a **category** and an
**identifier** only. The details stay in the service, behind your token: open them there, never ask for them by email.

**Time: 2 minutes to triage one email; the work itself depends on the category.**

---

## A. Switching alerts on (once, 10 minutes)

1. Resend must already be set up for client email (`RESEND_API_KEY` on finance, `docs/deploy/railway.md`, "Client
   email"). The alert email uses the same key.
2. Railway → `finance` → Variables → `FOUNDER_ALERT_EMAIL` = the inbox you read on your phone (one address).
   `FOUNDER_ALERT_FROM` is already set by the IaC to `Z Best Media Alerts <alerts@zbestmedia.com>`. Redeploy finance.
3. Railway → `ops-alerts` → Variables → `FIN_CRON_CALLER_TOKEN` = a new `openssl rand -hex 32` (not fin-cron's).
   Redeploy finance (it reads the token through `FIN_CALLER_TOKENS`), then let `ops-alerts` tick.
4. Check: Railway → `ops-alerts` → the latest run's log shows `fin-cron: {"call": "/fin/v1/jobs/founder-alerts/run",
   ... "ok": true, "summary": {... "enabled": true ...}}`. If `enabled` is false, the finance log and the job's
   answer say why (`why_off` is not printed by the cron; call the route yourself with the scheduler token or read
   the finance log). Typical causes: the address is not one address, the sender is not alerts@ or billing@, no
   Resend key.
5. In your mail app, add a rule: from `alerts@zbestmedia.com` → mark important, never spam.
6. **The dead-man's switch (15 minutes, free; do this too).** Alerts live in Finance, so if Finance (or the cron, or the
   Resend key) is down, no alert email can come. An outside service watches for that:
   1. Go to https://healthchecks.io, sign up (free plan) with the same inbox as `FOUNDER_ALERT_EMAIL`.
   2. **Add Check**: name `zbm ops-alerts`; **Schedule**: Simple, **Period** 5 minutes, **Grace time** 20 minutes.
      Save. Integrations: keep **Email** on (your address). Optional: add the SMS or phone app integration.
   3. Copy the check's **ping URL** (`https://hc-ping.com/<uuid>`). Treat it like a password: anyone with it can
      fake "all is well".
   4. Railway → `ops-alerts` → Variables → `FOUNDER_HEARTBEAT_URL` = that URL, as a **Sealed** variable. Redeploy is
      not needed for a cron service; the next run (within 5 minutes) uses it.
   5. Within 10 minutes the check turns green in Healthchecks ("up"). The ops-alerts log shows `fin-cron: heartbeat
      pinged`. If it shows `ALERT fin-cron: heartbeat NOT pinged`, alerts are off or Finance did not answer: A.4.
   6. Test it once: in Healthchecks, **Pause** is not a test; instead temporarily clear `FOUNDER_HEARTBEAT_URL` in
      Railway; after 25 minutes Healthchecks emails you "zbm ops-alerts is DOWN". Put the URL back; it emails "UP".
7. **The daily summary email.** `FIN_ALERT_DAILY_SUMMARY` is `1` in the IaC: every day at about 8:00 Pacific
   (`FIN_ALERT_DAILY_HOUR`) you get "ZBM daily check: all clear" or the number of open items per category. If a day
   passes without it, the alert path is broken: section C.

## B. What each category means

| Category | What happened | What to do | Where |
|---|---|---|---|
| `ledger_unreachable` (critical) | The evidence ledger did not answer for 15 minutes. Every department refuses every write until it does (fail closed). | Railway → `ledger` → logs and volume usage. Full volume: [backup-restore-and-replay.md](backup-restore-and-replay.md). | Railway |
| `compliance_unreachable` | Compliance did not answer Finance for 15 minutes. Activation, invoices and Stripe pages stay closed. Payments that arrive are held `unverified`. | Railway → `compliance` → logs. Once it is back, the hourly fin-cron run re-checks held payments. | Railway |
| `compliance_hold` | A payment Compliance held (Finance break), or an open hold at Compliance: a sanctions hit on a client or owner, a jurisdiction mismatch. | `GET /fin/v1/breaks` and `GET /compliance/v1/holds` with your token. Sanctions: [ofac-owner-screening.md](ofac-owner-screening.md), section C. Never refund a sanctions hold. | Finance, Compliance |
| `payment_disputed` | A Stripe dispute or chargeback, or the receivable it left. | Stripe Dashboard → Disputes. Respond before the deadline: [chargebacks-and-disputes.md](chargebacks-and-disputes.md). | Stripe |
| `unknown_paid_checkout` | Stripe took money on a Checkout page Finance does not know (usually after a restore). | [restore-and-reimport.md](restore-and-reimport.md): the Stripe re-import. | Finance |
| `reconciliation_mismatch` | The daily reconciliation found a leg that does not match (Stripe, bank, journal). | `GET /fin/v1/breaks`; explain or resolve each break. | Finance |
| `client_mail_failed` | A client email (receipt, credit note, acknowledgement) failed for good. | Fix the cause (billing contact, Resend domain), then `POST /fin/v1/client-mail/{id}/requeue`, or send it by hand. A contractual deadline may be running (2 business days for an acknowledgement). | Finance |
| `finance_high_break` | Any other high-severity Finance break (a restore that needs a re-import, a refund reversed at Stripe). | `GET /fin/v1/breaks`; the break's note says what to do. | Finance |
| `compliance_rule_expiring` (medium) | A rule that every client activation relies on expires within 14 days. On that day every client turns REFUSE. | Re-verify the rule and approve it (`docs/deploy/railway.md`, Compliance, "Rule expiry"). | Compliance |

A line marked `(reminder)` is still open 24 hours after the first email.

## C. When there is no email but there should be

- **Healthchecks says "DOWN"**, or **the daily summary did not come**: the alert path itself is broken. In order:
  Railway → `finance` (running? its log), Railway → `ops-alerts` (latest runs: red? `ALERT fin-cron:` lines?), the
  `enabled` flag in the run summary (A.4), Resend → Emails (refused sends). Fix it, then wait for the check to turn
  "UP" and the next digest.
- **Finance is down**: the alerts live in Finance, so no alert email can come. The `ops-alerts` runs fail (Railway shows
  them red), the heartbeat ping stops, and Healthchecks emails you within about 25 minutes. The daily summary stops
  too.
- **Resend refused** (the key was revoked, the domain unverified): Finance retries the same email 8 times over about
  16 hours, then logs `ALERT finance: founder_alert_dropped`. Railway → `finance` → logs, search `founder_alert`.
- **Alerts are off**: see A.4.

## D. Checking it works (5 minutes)

There is no "send a test alert" button: an alert needs a real condition. To check the path without one:

1. Railway → `ops-alerts` → the latest run's log line shows `"ok": true` and, in its summary, `"enabled": true` and
   `"state_saved": true`.
2. When the first real condition happens, Resend → Emails shows a message with the subject `ZBM alert (...)` sent
   from `alerts@zbestmedia.com`, and it reaches your inbox (check spam the first time).
3. On a local copy (never production), the finance test `tests/test_founder_alerts.py` exercises every category.

UNVERIFIED: nothing in this runbook has run against the real Railway, Resend or a real inbox yet.

## Sources

- ADR 0027 (`docs/adr/0027-founder-alerts-business-days-offboarding-owner-screening.md`).
- Resend idempotency keys are kept 24 hours (as ADR 0026 records): https://resend.com/docs/dashboard/emails/idempotency-keys
- Railway cron jobs (shortest interval 5 minutes): https://docs.railway.com/reference/cron-jobs

- Healthchecks.io (checks, period and grace time, ping URLs, email integration): https://healthchecks.io/docs/

These pages were not re-read in this pass (UNVERIFIED); the Healthchecks screen names in A.6 may differ slightly.
