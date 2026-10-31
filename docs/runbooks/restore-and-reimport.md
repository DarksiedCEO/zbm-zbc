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
nothing is lost: in live mode Stripe retries a webhook it could not deliver for up to 3 days, with growing gaps, so a
pause of minutes only delays them; a sandbox (test mode) retries only a few times over a few hours, so in a test drill
a long pause can lose a delivery, which the re-import of section 5 then books). A pause lasts at most `FIN_BACKUP_PAUSE_MAX_MIN` minutes (default 30, at most 120); a
longer `minutes` is refused. It always ends on time, also if Finance restarts inside it: the restarted Finance stays
paused until the window's end, `/health` shows `backup_pause.paused: true`, and finance logs `ALERT finance:
paused_for_backup` every 5 minutes. A marker still open after its window pauses nothing but logs `ALERT finance:
backup_marker_left_open` every 5 minutes until you resume it. Open the finance tunnel and make the header files first, as in docs/deploy/railway.md, Compliance
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
   `"consistent": true` means Finance did not write during the window. `false` gives a `reason`:
   `window_expired` or `log_moved` (take the set again), or `restarted_or_restored` (expected after a restore; after a
   deploy inside the window, take the set again).
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
   Without `money_events_acknowledged` Finance answers 409 `RESTORE_GUARD`. With it, Finance first writes the
   **restore lockdown** to its data directory (`restore_lockdown.json`; if that write fails the answer is 503 and
   nothing was done), then commits the reconcile and the restore break as one line (if that fails, 503: run the
   same reconcile again). The answer says `"stripe_reimport_required": true` and names `reimport_break_id`. The break
   records the backup's point in time (`restore_point`: the restored log's last line, its time and its ledger
   position) and its day as `since`.

   **The lockdown.** From now until you lift it, **all money out is paused**: Finance refuses every state-changing
   route and every scheduler job (409 `RESTORE_LOCKDOWN`) except an allowlist: Stripe webhooks and bank lines (money
   in is still booked), the restore routes (reconcile, re-import, dispositions, lift), the contract-deadline routes
   (`sow-ended`, `acknowledgements`, `deadlines/<id>/close`), the backup pause and resume, and the client-mail job
   (the founder alerts). Every bank transfer, rail submit, payout release and refund repayment is also refused inside
   Finance itself. `/health` answers `"status": "degraded"` with `restore_lockdown.on: true` (and `why`), and finance
   logs `ALERT finance: restore_lockdown on` every 5 minutes. The lockdown stays on while any restore break is open,
   whatever the file says; a lockdown file that cannot be read keeps Finance locked. Only you lift it (step 5).

   **Contract deadlines during a lockdown.** MSA 4.12(f) acknowledgements are recorded as usual. For MSA 4.12(g) you
   may still void an **unpaid** invoice, or credit it down without any refund, when the restore's item list is
   complete and nothing the restore lost touches that invoice (no money moves; the lift lists these). Refunds that
   fall due (MSA 4.10) go through the **Stripe Dashboard**, which Finance books. If the lockdown lasts more than **2
   business days**, finance logs `ALERT finance: restore_lockdown_long` with the contract deadlines due within 10
   business days, and again every day: finish section 5 before those deadlines.
4. Unset `FIN_RECONCILE_MODE`, redeploy finance. If the backup was taken inside a pause window (section 2), the
   restored Finance stays paused until the window's end (every write answers 503 `QUIESCED`): resume that marker
   now (`POST $F/fin/v1/backup/resume`; it answers `"consistent": false`, reason `restarted_or_restored`, which is
   expected after a restore).
   Invoices paid after the backup now show `issued`. **Do not chase any client.** The restore break lists **items**:
   every credit note, void, contract deadline and client email the ledger recorded after the backup. If the ledger
   could not be read at that moment the break says `build_incomplete: true` with no items yet: the next re-import run
   reads the ledger again before it plans anything (and plans nothing until it can). A void the restore lost never
   makes an invoice payable, and credit note numbers go on from the highest authentic number the ledger saw. Stripe redeliveries of events Finance lost may arrive: a paid Checkout page
   Finance no longer knows answers 2xx, opens a `stripe_unknown_session` break and logs `ALERT finance:
   stripe_unknown_session` (CH-4). Leave those breaks; step 5 resolves them.

## 5. The Stripe re-import (always a dry run first)

Covers at most 92 days per run; run it in parts for longer. Dates are Los Angeles dates (`FIN_BUSINESS_TZ`).
Read the restore break first (`GET $F/fin/v1/breaks`, leg `restore_reimport_required`): `since` is the day of the
backup you restored, `opened_on` the day you reconciled. Run sections 5.1-5.2 until **every day from `since` to
`opened_on`** is covered by a run that ended clean (in parts of at most 92 days; the break's `covered` lists what is
done). A run that leaves anything missing (a fresh plan for its window is not empty) does not count. Nothing closes
the break by itself: when the coverage is complete and every item is rebuilt or disposed of, the break says
`ready_to_lift: true`, and you lift the lockdown (step 5).

1. Dry run:
   ```
   curl -sS -X POST $H $F/fin/v1/stripe/reimport \
     -d '{"request_id":"imp-1","since":"2026-11-07","until":"2026-11-09","dry_run":true}' > dry.json
   ```
   Read `plan.actions`, in the order they will be booked (the order the ledger saw them): `relink_page` (a Checkout
   page Finance lost, read back from Stripe), `book_payment`, `book_refund`, `book_dispute`, and
   `rebuild_credit_note` / `rebuild_void`: credit notes and voids made after the backup are not in Stripe, so they are
   rebuilt from their evidence on the ledger (each checked against the payload SHA-256 the ledger holds; same number,
   same amounts, same refund deadline; the client is emailed only if the original email was never sent). Only
   effects that took effect are rebuilt: each has an `effect_committed` ledger mark whose HMAC (keyed from
   `FIN_CONTACT_HASH_KEY`) only this Finance can write, and only events of the `finance` department count. An attempt
   that answered an error, a mark that is missing or does not verify, a contract deadline (`deadline_recorded`) and a
   client email that was queued but whose parent is not rebuilt are never booked: they are items with status
   `needs_review` and a `why` (step 3). Compare with the Stripe Dashboard (Payments, filtered to the same dates). Nothing is
   booked by a dry run.
2. Real run, naming the dry run's plan:
   ```
   curl -sS -X POST $H $F/fin/v1/stripe/reimport \
     -d '{"request_id":"imp-2","since":"2026-11-07","until":"2026-11-09","dry_run":false,"plan_sha256":"<from dry.json>"}'
   ```
   If Stripe or the books changed since the dry run, the answer is 409: dry-run again and read it again.
   Each action is booked exactly as its webhook would have been (Compliance is asked for each payment; a payment from a
   client Compliance does not currently allow is booked and HELD, as always). Running it twice books nothing twice.
3. Check: `results` has no `failed` items and `"clean": true`; on the run that completes the coverage (with every
   item settled), `ready_to_lift` names the restore break, the paid invoices show `paid`
   again, and the first reconciliation after the lift (fin-cron at 07:xx, or `POST /fin/v1/reconciliations/run`; the
   lockdown refuses it before) shows Stripe balance leg L3 in balance. A `failed` item: fix its cause (usually Stripe unreachable) and run steps 1-2 again. A
   `rebuild_*` action that fails ("the invoice is not in the restored books", "the ledger's evidence does not verify")
   becomes a `needs_review` item and does not stop the run from counting. Only `needs_review` items can be disposed
   of: an item the re-import rebuilds (`pending`, verified evidence with an authentic mark) is refused (409). For
   each `needs_review` item (`GET $F/fin/v1/breaks`, the restore break's `items`; its `why` says what is wrong, and
   "possibly committed" means its line's anchor exists without a mark: check what the client received) read its
   ledger event, then record one of:
   ```
   curl -sS -X POST $H $F/fin/v1/restore/items/<event_id>/disposition \
     -d '{"request_id":"disp-1","disposition":"reissue_after_lift","reason":"..."}'
   ```
   - **Money that went out** (`kind` `money_out`: a transfer, payout item, payout release, refund payment or vendor
     payment the ledger saw after the backup; `in_flight`: a transfer or payout item the restored books hold as
     approved / executing / submitting): check the bank or the rail, then record `moved` (the reference in the
     reason) or `not_moved`. Its subject stays **fenced** (never transferred, submitted, released or repaid again,
     also after the lift) unless you record `not_moved`; book what moved by hand (journal correction).
   - `reissue_after_lift`: a credit note or void you must issue again; the lockdown refuses it now, so the lift opens
     a high-severity `restore_reissue_due` break with the item's number, amounts and refund date; issue it right after
     the lift, then resolve that break.
   - `reissued` with `reissued_as`: only for a lost credit note (a credit note on the **same invoice** for at least the
     same amount and refund, issued after the item) or a lost void (the **same invoice**, voided after the item).
   - `lost` with the reason: it never happened or no longer applies (an invoice issued and voided after the backup), or
     you handled it outside Finance (a deadline re-recorded with `sow-ended` / `acknowledgements`, an email re-sent).
   `POST /fin/v1/breaks/<id>/resolution` refuses the restore break (and every deadline break).
4. Anything Stripe does not know (a wire, an ACH credit to the bank) is not re-imported: re-post those bank lines
   through the bank feed as usual.
5. **Lift the lockdown** (only you, explicitly):
   ```
   curl -sS -X POST $H $F/fin/v1/restore/lockdown/lift -d '{"request_id":"lift-1","reason":"re-import 11-07..11-09 clean; 2 items disposed"}'
   ```
   Refused (409, listing what is missing) while the item list is incomplete, a day is not covered, or an item is not
   rebuilt or disposed of. The answer lists `lockdown_effects` (voids and credits made during the lockdown) and the
   `fenced` subjects. The lockdown file is written first: if that fails the answer is 503 and nothing changed; a retry
   with the same `request_id` always says the state as it is now. Then `/health` is `ok` again and money can go out.
   Issue any `restore_reissue_due` items now.

## Commit marks and the key (FIN_CONTACT_HASH_KEY)

Every committed credit note, void, deadline and client email gets an `effect_committed` mark on the ledger: an HMAC
under a key derived from `FIN_CONTACT_HASH_KEY`, with the key's id (`key <12 hex>`). A mark that could not be written
is owed: the local log itself is the queue (each line names its evidence), so Finance writes owed marks at start and
in every hourly fin-cron run, also after a restart; `/health` shows `commit_marks_pending`. Without the key nothing
is authenticated and a restore rebuilds nothing automatically: `/health` says `restore_rebuild_unavailable: true`
and the start-up log warns. **Rotating the key:** set `FIN_CONTACT_HASH_KEY` to `<new key>_<old key>` (64 hex
characters each, joined by an underscore; at most two old keys). The first key hashes contacts and signs new marks;
the old ones only verify marks written before. Keep an old key listed until no restore could reach marks it signed
(the oldest backup you keep is newer than the rotation). A mark under a key id that is not listed is an item for
you ("unknown key id"), and `/health` names it.

## 6. Drill (before the first paid client, then quarterly)

The red team requires one drill before real money is booked. On a **test-mode** project or a local stack:
take a backup set (section 2), pay two invoices with Stripe test mode, make one Dashboard refund, restore the set
(section 3), reconcile (section 4), re-import (section 5). Include one credit note and one void made after the
backup. Pass: the invoices are `paid` again, the refund is booked, the credit note (same number) and the void are
back, any item you disposed of is recorded, the lockdown is lifted and the restore break resolved, L3 is in balance, Finance integrity is green. Record the date and result in your notes.
