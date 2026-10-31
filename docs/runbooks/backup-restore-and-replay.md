# Backup, restore and Stripe event replay

Use this when a Railway volume is lost, corrupted, wiped or full; when a service refuses to start because its data
does not match the ledger; or when the money path (stripe-gateway → finance) was down long enough that Stripe may
have given up delivering events.

**This procedure has not been rehearsed against the real Railway deployment.** Rehearse it once in a throwaway
Railway environment before you need it (see "Rehearsal" at the end).

---

## What is backed up

| Service | Volume | Holds | Backup |
|---|---|---|---|
| ledger | `ledger-data` at `/data` | the append-only evidence ledger every department anchors to | Daily (IaC `backupSchedules: ["DAILY"]`) |
| finance | `finance-data` | invoices, journal, Stripe events already applied, holds, controls | Daily |
| legal | `legal-data` | documents, acceptances, approvals | Daily |
| seo | `seo-data` | tenants, audits, report extracts | Daily |
| compliance (after the gate merges) | `compliance-data` | register, attestations, activations | Daily |
| detection, orchestrator, stripe-gateway | none | stateless | — |
| Lead database | Neon (managed) | leads, purge runs | Neon's own history / point-in-time restore (window depends on plan; UNVERIFIED) |

Railway's backup schedules and retention: **daily backups are kept 6 days**, weekly 27 days, monthly 89 days.
Only daily is configured, so **you can go back at most 6 days**. Backups can be restored only into the same project
and environment. Wiping a volume deletes all of its backups. Manual backups are limited to 50% of the volume size.

Check now and monthly: each volume → **Backups** shows Daily, and the newest backup is less than 24 hours old.

---

## What a restore loses

Restoring a backup taken at time **B** loses everything written after **B** on that volume:

- **finance:** payments, disputes, payouts booked after B; invoices created or approved after B; holds and releases.
  Stripe already got a `200` for those events, so **Stripe will not send them again by itself.** You must replay them
  (part C).
- **legal:** acceptances and approvals after B. Re-record them from your own records (the client's emails), honestly
  dated as re-recorded.
- **seo:** audits and tenants created after B. Re-run them.
- **compliance:** attestations and activations after B. Re-record them from your saved OFAC evidence files (the
  files are on your machine; the record only holds their SHA-256).
- **ledger:** anchors written after B. Every department's local log is checked against the ledger at start, so a
  ledger restored to an older point than a department's log makes that department refuse to start (and the reverse).

**Rule: restore the ledger and every volume service it serves to backups from the same day, as close in time as
possible, and expect to use the one-shot reset and reconcile flows below.** UNVERIFIED: how the services behave when
the backups of different volumes were taken minutes or hours apart on the same day. That is what the rehearsal is for.

---

## A. Before you restore (15 minutes)

1. Open an incident log ([incident-response.md](incident-response.md#incident-log-template)). Note the time the
   problem started and the newest good backup per volume (each volume → Backups; write down each backup's date stamp).
2. **Pause new money in.** Do not send any client a new Stripe Checkout link until part D passes. If needed, ask
   clients by email to hold payments for a few hours.
3. **Save what is still readable.** For each affected service: `railway logs --service <name> >
   ~/zbm/incidents/<name>/<service>.log`. If the volume is still mounted and readable, `railway ssh` in and copy out
   anything you can (do not modify it). Railway keeps the old volume, unmounted, after a restore, so it is not lost.
4. **Note the Stripe window.** Write down time **B** (the backup you will restore) and **now**. Every Stripe event
   between B and now may need replaying.

## B. Restore

Do the volumes in this order: **ledger first**, then legal, compliance, seo, finance (the same order as the deploy
runbook's start-up order).

For each volume:

1. **(5 min)** Railway → the service → **Backups** → the chosen backup (by its date stamp) → **Restore**.
2. The change is **staged**. Open **Details** at the top of the project canvas to review it: a new volume named after
   the backup's date stamp is mounted at the original path; the previous volume stays in the project, unmounted, under
   its original name.
3. Click **Deploy**. The service redeploys on the restored volume.

### B.1 The ledger refuses to start after its restore (expected)

The ledger keeps a head checkpoint, so a restored (older) log is refused on purpose. Its log prints one
`REFUSING TO START` line with a value of the shape `<checkpoint>/<log head>`.

1. Copy that exact value.
2. Railway → `ledger` → Variables → set `LEDGER_ALLOW_RESET=<that exact value>` → deploy. The ledger records
   `reset_from`, logs `operator_reset`, and **exits without serving**. That is correct.
3. **Unset** `LEDGER_ALLOW_RESET` at once (delete the variable) → deploy again. The ledger now serves. Leaving the
   variable set makes the next start refuse.
4. Check its log for `listening on [::]:8090` and the `operator_reset` event line. Save both lines in the incident log.

### B.2 A department refuses to start after its restore

finance, legal, seo and compliance verify their local log against the ledger at start.

- **VOIDABLE problem** (a commit that failed after its anchor, a truncated tail, a version event with no local
  decision): restart the service with its reconcile flag (`FIN_RECONCILE_MODE=1` on finance, `LEGAL_RECONCILE_MODE=1`
  on legal, `COMPLIANCE_RECONCILE_MODE=1` on compliance), read `GET /fin/v1/reconcile` (or `/legal/v1/reconcile`,
  `/compliance/v1/reconcile`) with your Andre approval token through the tunnel, then `POST` the same route with
  exactly the `head_sha256`, `void_lines` and `void_event_ids` the plan lists. Unset the flag and restart.
  (`services/finance-py/README.md`, "Reconciling the local log with the ledger".) seo has no reconcile mode: if it
  refuses, restore its volume from a backup that matches the ledger's restore point. UNVERIFIED: the compliance
  route path; its refusal line names the route to call.
- **FATAL problem** (an unanchored local line, a missing cited event, foreign anchors, an empty log against a ledger
  that anchors one): it cannot be reconciled. Restore that service's volume from a backup that matches the ledger's
  restore point, then try again.
- **Time limit: 2 hours.** If the services still refuse after 2 hours, stop, keep the money path paused, and write
  down every refusal line. Do not edit volume files by hand.

## C. Replay Stripe events

Finance deduplicates every Stripe event by its event id and reads the object back from Stripe before booking it. So
sending an event that Finance already has is harmless (`"status": "duplicate"`), and sending one it lost after the
restore books it again from Stripe's current data.

Limits (Stripe): the Dashboard **Resend** button works for up to **15 days** after an event was created; the CLI
`stripe events resend` works for up to **30 days**. After 30 days an event cannot be resent: book it by hand (C.4).

1. **(10 min) List the window's events.** With the Stripe CLI logged in to **live** mode (read-only use):

   ```sh
   stripe events list --limit 100 --created.gte <unix time of B minus 1 hour> > ~/zbm/incidents/<name>/events.json
   ```

   UNVERIFIED: the exact flag spelling for the `created` filter in your CLI version; `stripe events list --help`
   shows it. If there are more than 100, page with `--starting-after <last id>`. Keep only the event types the
   endpoint subscribes to (the list in `docs/deploy/railway.md`, step 7).
2. **(5 min) Find the endpoint id.** Stripe → Workbench → Webhooks → the ZBM endpoint; its id starts with `we_`.
3. **(Per event) Resend, oldest first:**

   ```sh
   stripe events resend <evt_id> --webhook-endpoint=<we_id> --live
   ```

   (`--live` makes the CLI act on live mode; check `stripe events resend --help`.) Or, for events under 15 days old,
   open the event in the Dashboard and click **Resend**. Stripe does not guarantee order and Finance does not depend
   on it, but oldest-first makes the log easier to read.
4. **Watch the result** for each: Workbench → Webhooks → the endpoint → **Event deliveries** shows `Delivered` (200).
   A `503` means Finance could not read the object back from Stripe: wait a minute and resend that event again.
   Any other error: write it down and keep going with the rest.
5. **Events older than 30 days, or that keep failing:** book them by hand from Stripe's records: compare the payment,
   dispute and payout lists in the Dashboard with Finance's invoices and journal, and record each difference in your
   manual register for the next reconciliation. Do not invent Stripe events.

Note: resending an event that failed delivery does not cancel Stripe's own retries (live-mode retries run for up to
3 days). Duplicates are harmless because of the event-id deduplication.

### After a long outage (no restore)

If the gateway or Finance was down for more than about 3 days, Stripe stopped retrying the events it could not
deliver. Do steps 1-5 of part C for the outage window. In Workbench → Webhooks → **Event deliveries**, `Failed` events are the
ones to resend.

## D. Verify before reopening the money path

1. Every service is Active and `/health` answers 200 (through the tunnels, as in the deploy runbook).
2. Finance: for each Stripe payment in the window, the invoice shows paid (or held, if Compliance holds it), with the
   right amount. Compare Stripe Dashboard → Payments (filtered to the window) with Finance's invoices.
3. Finance: disputes and payouts in the window are booked (F7/F7a/F7l; F13p/F13q).
4. Reconcile Stripe by hand. `POST /fin/v1/reconciliations/run` accepts only the `scheduler` caller, and no
   scheduler is deployed yet, so you cannot run it. Instead compare Finance's account 1060 (Stripe balance, less
   payouts in transit) with the balance in Stripe Dashboard → Balances, and write the result and any difference in
   your manual register.
5. Legal, compliance, seo: re-record what was lost (section "What a restore loses").
6. Write the result in the incident log, then reopen: send new Checkout links again.

## E. A volume is full (5 GB)

A full ledger volume makes every department fail closed, and webhooks then fail. Before it gets there:

1. Monthly: `railway ssh` into each volume service → `df -h /data`. Above 70%: grow the volume (service → Volume
   settings) before it fills. UNVERIFIED: whether your plan allows growing in place.
2. If it is already full: grow it, redeploy, then replay Stripe events for the downtime (part C).

## F. Lead database (Neon)

Neon restores are separate from Railway. If lead rows are lost or corrupted, use Neon's point-in-time restore / branch
restore to a time before the damage (Neon Console → the project → restore). UNVERIFIED: your plan's history window.
After any restore, check the site's `/api/health` and that the purge cron still runs (`lead_purge_runs`).

## Rehearsal (once, about 3 hours, then yearly)

1. Create a separate Railway environment (not `production`) from the same IaC, with **test-mode** Stripe keys.
2. Make a few test payments, take a manual backup of each volume, make a few more payments.
3. Restore every volume to the backup, run parts B and C, and verify part D.
4. Write down every surprise and fix this runbook. Then delete the rehearsal environment.

## Sources

Checked on 2026-10-30.

- Railway volume backups (restore is staged; new volume mounted, old kept unmounted; daily 6 days, weekly 27, monthly
  89; same project + environment only; wipe deletes backups; manual backups 50% of volume):
  https://docs.railway.com/volumes/backups.md
- Stripe webhooks (live-mode retries up to three days; Dashboard Resend up to 15 days; `stripe events resend <event_id>
  --webhook-endpoint=<endpoint_id>` up to 30 days; duplicate events): https://docs.stripe.com/webhooks
- Ledger reset flow: `services/ledger-rust/README.md` (`LEDGER_ALLOW_RESET`); department reconcile flow:
  `services/finance-py/README.md`; deduplication by event id: `services/finance-py/src/svc_stripe.py`.
