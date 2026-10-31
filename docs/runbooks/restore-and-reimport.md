# Backups, restores and the Stripe re-import

Red team finding CH-3 (Oct 30-31 2026). ADR 0026. Finance code: `services/finance-py/src/svc_restore.py`.
Guard test: `services/finance-py/tests/test_restore_reimport.py` reproduces the red team's restore scenario.

The one rule of this page: **a restore is never a cleanup.** If the reconcile plan after a restore lists money
(`money_at_risk`), that money was paid, refunded or disputed at Stripe after the backup. It is real. Finance refuses to
forget it unless you say so explicitly, and then it keeps a break open until the Stripe re-import has booked it again.
Nothing on this page deletes a paid invoice, a payment or a refund, and no step asks you to.

## 1. What Railway does and does not do (read once)

From Railway's backup documentation (docs.railway.com/reference/backups, read Oct 31 2026):

- Each **volume** is backed up on its own. Scheduled backups: daily (kept 6 days), weekly (kept 27 days), monthly
  (kept 89 days). Manual backups any time (capped at 50% of the volume size). The IaC sets DAILY on every volume.
- Backups are incremental and copy-on-write.
- Restoring creates a **new volume** named after the backup's date, mounted where the old one was; the old volume
  stays in the project, unmounted. The restore is a staged change: you review it and click Deploy, then the service
  redeploys. Restores stay inside the same project and environment.
- **Not documented by Railway:** whether a backup taken while the service writes is consistent, and whether the
  daily backups of different volumes are taken at the same moment. Assume neither. The daily backups of `ledger-data`,
  `finance-data`, `legal-data`, `compliance-data` and `seo-data` are taken at **different times**.
- Wiping a volume deletes its backups.
- Not verified on this project: whether `railway config plan` wants to rename or recreate a restored volume (the IaC
  names `finance-data` etc.). After any restore, run `railway config plan` and read it before any apply; never apply a
  plan that deletes or recreates a data volume.

Why order matters: every department anchors its local log on the **ledger**.
- A department **older** than the ledger (its backup is from before the ledger's) starts in reconcile mode: the ledger
  holds evidence of things the department no longer has. That is fixable (sections 4-5).
- A ledger **older** than a department is FATAL for that department: its log cites ledger events that do not exist,
  and even `FIN_RECONCILE_MODE=1` refuses. The only way out is to restore that department too, to a backup taken no
  later than the ledger's. So: **the ledger is always backed up last.**

## 2. Taking a consistent backup set (before risky work, and weekly)

About 10 minutes. Finance pauses its writes for the window (Stripe webhooks and fin-cron get a 503 and retry later;
nothing is lost). Open the finance tunnel and make the header files first, as in docs/deploy/railway.md, Compliance
step I (`ssh -N -L 127.0.0.1:18410:127.0.0.1:8410 <finance-service-instance-id>@ssh.railway.com`; in `~/.zbm-cmp`,
`fin-auth.hdr` from `FIN_SERVICE_TOKEN` and `fin-andre.hdr` from `FIN_ANDRE_APPROVAL_TOKEN`, both with `read -rs`;
tokens are never typed on a command line). Below, `F=http://127.0.0.1:18410` and `H='-H @fin-auth.hdr -H
@fin-andre.hdr -H Content-Type:application/json'`.

1. **Quiesce Finance and record a marker.**
   ```
   curl -sS -X POST $H $F/fin/v1/backup/quiesce -d '{"request_id":"bkp-2026-11-07","minutes":30}'
   ```
   Keep `marker.marker_id` and `marker.log_head_seq` from the answer.
2. **Back up the departments** in the Railway dashboard, each service → its volume → Backups → **Backup now**:
   `finance-data`, then `legal-data`, `compliance-data`, `seo-data`. Note each backup's date stamp.
3. **Back up the ledger LAST:** `ledger-data` → Backup now.
4. **Resume Finance:**
   ```
   curl -sS -X POST $H $F/fin/v1/backup/resume -d '{"request_id":"bkp-2026-11-07-r","marker_id":"<marker_id>"}'
   ```
   `"consistent": true` means Finance did not write during the window. `false` (the window ran out, or something
   wrote): take the set again.
5. Write down in your password manager notes: the date, the marker id, the five backup date stamps. That is one
   **backup set**. Restore only whole sets (section 3).

Legal, Compliance and seo have no quiesce switch. Their writes between their backup and the ledger's backup show up
after a restore as reconcilable lines on those services (their READMEs: "Reconciling the local log with the ledger").
They are never FATAL because the ledger is newer.

## 3. Restoring (only for real loss: a corrupted or wiped volume)

1. Pick the newest **backup set** from section 2 (or, if none exists, the daily backups, see "Daily backups only").
2. Restore the department volumes of that set first (each service → Backups → the set's stamp → Restore → Deploy),
   then the ledger's. For the ledger, its log will be shorter than its checkpoint: follow the one-shot
   `LEDGER_ALLOW_RESET` flow in services/ledger-rust/README.md (set the exact value the refusal prints, start once,
   unset).
3. `railway config plan`: read it. Do not apply anything that deletes or recreates a volume.
4. Each department that refuses to start with a VOIDABLE problem: section 4 (Finance) or its README (the others).

**Daily backups only (no set).** Choose, for the ledger, a daily backup that is NOT OLDER than the backups you choose
for every department. If a department's newest usable backup is newer than the ledger's, use an older backup of that
department. Expect more to re-import (section 5).

## 4. Finance after a restore: reconcile without forgetting money

1. Finance's log says `refusing to start: ... VOIDABLE`. In Railway, set `FIN_RECONCILE_MODE=1` on finance and
   redeploy. Only reads and the reconcile route answer.
2. Read the plan:
   ```
   curl -sS $H $F/fin/v1/reconcile > plan.json
   ```
   - `fatal` non-empty: STOP. The ledger is older than Finance (section 1). Restore Finance to an older backup.
   - `money_at_risk` non-empty: these are payments, invoices, refunds, credit notes or journal entries made after the
     backup. **They are not deleted at Stripe and must not be deleted from the books.** Go on: the next step keeps
     them recoverable.
3. Reconcile, acknowledging the money (exactly the plan's `head_sha256`, `voidable.lines`, `voidable.event_ids`):
   ```
   python3 -c 'import json; p=json.load(open("plan.json")); print(json.dumps({"request_id": "rec-1",
     "head_sha256": p["head_sha256"], "void_lines": p["voidable"]["lines"],
     "void_event_ids": p["voidable"]["event_ids"], "money_events_acknowledged": True}))' > rec.json
   curl -sS -X POST $H $F/fin/v1/reconcile -d @rec.json
   ```
   Without `money_events_acknowledged` Finance answers 409 `RESTORE_GUARD`. With it, the answer says
   `"stripe_reimport_required": true` and names `reimport_break_id`: a high-severity break that stays open until
   step 5 runs.
4. Unset `FIN_RECONCILE_MODE`, redeploy finance. Invoices paid after the backup now show `issued`. **Do not chase any
   client and do not void anything yet.** Stripe redeliveries of events Finance lost may arrive: a paid Checkout page
   Finance no longer knows answers 2xx, opens a `stripe_unknown_session` break and logs `ALERT finance:
   stripe_unknown_session` (CH-4). Leave those breaks; step 5 resolves them.

## 5. The Stripe re-import (always a dry run first)

Covers at most 92 days per run; run it in parts for longer. Dates are Los Angeles dates (`FIN_BUSINESS_TZ`).
`since` = the date of the backup you restored (the break's `since` field shows it); `until` = today.

1. Dry run:
   ```
   curl -sS -X POST $H $F/fin/v1/stripe/reimport \
     -d '{"request_id":"imp-1","since":"2026-11-07","until":"2026-11-09","dry_run":true}' > dry.json
   ```
   Read `plan.actions`: `relink_page` (a Checkout page Finance lost, read back from Stripe), `book_payment`,
   `book_refund`, `book_dispute`. Compare with the Stripe Dashboard (Payments, filtered to the same dates). Nothing is
   booked by a dry run.
2. Real run, naming the dry run's plan:
   ```
   curl -sS -X POST $H $F/fin/v1/stripe/reimport \
     -d '{"request_id":"imp-2","since":"2026-11-07","until":"2026-11-09","dry_run":false,"plan_sha256":"<from dry.json>"}'
   ```
   If Stripe or the books changed since the dry run, the answer is 409: dry-run again and read it again.
   Each action is booked exactly as its webhook would have been (Compliance is asked for each payment; a payment from a
   client Compliance does not currently allow is booked and HELD, as always). Running it twice books nothing twice.
3. Check: `results` has no `failed` items, `breaks_resolved` names the restore break, the paid invoices show `paid`
   again, and the next reconciliation (fin-cron at 07:xx, or `POST /fin/v1/reconciliations/run`) shows Stripe
   balance leg L3 in balance. A `failed` item: fix its cause (usually Stripe unreachable) and run steps 1-2 again.
4. Anything Stripe does not know (a wire, an ACH credit to the bank) is not re-imported: re-post those bank lines
   through the bank feed as usual.

## 6. Drill (before the first paid client, then quarterly)

The red team requires one drill before real money is booked. On a **test-mode** project or a local stack:
take a backup set (section 2), pay two invoices with Stripe test mode, make one Dashboard refund, restore the set
(section 3), reconcile (section 4), re-import (section 5). Pass: the invoices are `paid` again, the refund is booked,
the restore break is resolved, L3 is in balance, Finance integrity is green. Record the date and result in your notes.
