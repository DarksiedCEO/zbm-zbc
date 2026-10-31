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
nothing is lost (a rail's own status webhooks are parked during a restore lockdown, see section 4): in live mode
Stripe retries a webhook it could not deliver for up to 3 days, with growing gaps, so a
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
   in is still booked), rail status events (verified and **parked**: applied at the lift; a `destination_changed`
   applies at once, it only puts the payee on hold; an event for a payout item the restored books lack becomes a
   restore item), the restore routes (reconcile, re-import, dispositions, corrections, bank re-feed, lift), the
   contract-deadline routes (`sow-ended`, `acknowledgements`, `deadlines/<id>/close`), the backup pause and resume,
   and the client-mail job (the founder alerts; it sends no client email while locked). Every bank transfer, rail submit, payout release and refund repayment is also refused inside
   Finance itself. `/health` answers `"status": "degraded"` with `restore_lockdown.on: true` (and `why`), and finance
   logs `ALERT finance: restore_lockdown on` every 5 minutes. The lockdown stays on while any restore break is open,
   whatever the file says; a lockdown file that cannot be read keeps Finance locked. Only you lift it (step 5).

   **Contract deadlines during a lockdown.** MSA 4.12(f) acknowledgements are recorded as usual. For MSA 4.12(g) you
   may still void an **unpaid** invoice, or credit it down without any refund, once the restore's item list is
   complete, the Stripe re-import has covered every day since the backup, you have confirmed the bank lines were fed
   again (step 4 of section 5), the bank's own closing balance of every account equals the books (less money the
   restore recorded as moved and not booked yet), no bank line since the backup is unattributed (an ACH or wire line
   without an invoice reference blocks the exception for its client, or for every invoice when its client cannot be
   told, until you apply or explain it), no payment page or issue of that invoice was recorded after the backup, and
   nothing the restore lost touches it (no money moves; the lift lists these). Refunds that
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
     approved / executing / submitting): check the bank or the rail, then record
     - `moved` with `provider_ref` (the bank or rail reference). The amount and the counterparty come from the lost
       ledger event itself (it carries them, checked against the ledger's payload SHA-256); if you also type them
       they must agree. Only when the event cannot be verified do you give `moved_amount` and `counterparty`. The old
       ids stay **fenced** for good (never transferred, submitted, released or repaid again). The counterparty (the
       payee of a payout, the campaign and client of a refund; a release, a funding transfer or a vendor payment is
       fenced by its id only) receives **no money at all, in any amount**, until the debt is booked with the
       **restore correction**:
       ```
       curl -sS -X POST $H $F/fin/v1/restore/items/<event_id>/correction \
         -d '{"request_id":"rc-1","provider_ref":"<the same reference>","payable_ids":["<the payables it paid>"]}'
       ```
       The entry is **derived** from the lost event (accounts, amounts, entity, counterparty); you never type lines.
       **One money movement, one item, one booking.** A transfer's in-flight operation and its posting(s), or a
       refund and its transfer, are the same money: one item carries it and the others say `follows_movement`
       (`primary` names it); they cannot be disposed of or booked on their own. A posting attempt the ledger shows
       reversed (the bank refused it) moved nothing and derives nothing. A second booking of the same movement, or of
       the same bank / rail reference on another item, is refused (409).
       A payout must name its payables (mandatory: the payee's, adding up to the item's net, the item's own if the
       restored books hold it); they become settled. A refund must name `refund_campaign_id` (its campaign); it is
       recorded paid, and its own transfer item is booked by the same correction (a refund's transfer item is never
       corrected on its own). A funding transfer needs nothing more. A release or a vendor payment has nothing to
       book (the money is on its payout items / the vendor record). It may touch restricted cash (flow FRC, only
       here) and resolves the reconciliation breaks on the accounts it touches. The counterparty's fence ends when
       its payables are settled / its refund is recorded paid, never earlier. The booking is a fact (the money already
       moved), so the restricted-pool gate does not refuse it.

       **A booking made in error** is reversed by you, never edited:
       ```
       curl -sS -X POST $H $F/fin/v1/restore/items/<event_id>/correction/reversal \
         -d '{"request_id":"rr-1","reason":"booked on the wrong reference"}'
       ```
       It posts the exact reversal (same accounts, opposite sides, linked to the booking, memo FRC), returns the
       payables it settled, marks a refund it recorded `reversed_restore_booking`, re-fences the counterparty and
       re-opens the item (and the items that followed it) for one new booking. The reversal is on the ledger with
       your reason.

       **An item that cannot be derived** (an older summary, an evidence row that does not verify) is booked by hand:
       ```
       curl -sS -X POST $H $F/fin/v1/restore/items/<event_id>/manual-booking \
         -d '{"request_id":"rm-1","provider_ref":"<the reference>","amount":"29.01","counterparty":"<payee id>",
              "rail":"stripe","reason":"rail statement shows the payout paid","evidence_sha256":"<sha of the statement>",
              "payable_ids":["<the payables it paid>"]}'
       ```
       You give the amount, the counterparty, the reference, the reason and the evidence's SHA-256; you never choose
       the accounts: a payout books Dr 2020 `payee:<id>` / Cr the rail account, a refund Dr 2010 `campaign:<id>` /
       Cr 1020 (`counterparty` is the campaign), a funding transfer (`"transfer_kind":"funding"`) Dr the rail account
       / Cr 1020, always ZBC. The amount and counterparty must agree with the disposition. A derivable item is refused
       here (use the correction). It settles the item and its fence exactly like a correction.

       **A payout whose payables were accrued after the backup** (the restored books hold none): the clipper network
       re-sends the handoffs of the submissions the payout paid (`POST /fin/v1/payout-handoffs`, the same
       submission ids; ask the clipper network team to re-post them for the days since `since`). Finance accrues them
       under their original payable ids; then book the correction naming them. If the re-send is not possible, use
       the manual booking with `"submission_ids":["<the submissions it paid>"]` instead of `payable_ids`: a handoff
       re-sent later for one of them is accrued and settled by your booking at once, never paid again (beyond the
       booked amount it opens a high-severity `restore_resent_over_booking` break for you).

       **The rail lookup is not wired in production yet.** The Stripe Connect and Trolley adapters are not built
       (`NotWiredRail`: every lookup answers unavailable), so `not_moved` for a payout answers 503 until they exist.
       Until then a payout item is `moved` with the rail dashboard's reference and booked with the correction or the
       manual booking; nothing in production is payable through a rail before then either.
     - `not_moved`: for ANY payout (held by the restored books or not) Finance asks the rail by the idempotency key
       the lost event carries: found -> refused (record it as moved); the rail unreachable -> 503, nothing recorded;
       not found -> the answer is `pending_not_moved` with `retry_after`: ask again after 24 h (a second lookup) with
       `provider_ref` (the rail statement reference) and `statement_date`. A release is not_moved only through its
       payout items' keys. A rail event for a lost payout is not_moved only if the rail itself said failed or
       returned. For bank money, give your attestation: `provider_ref` (the statement reference) and
       `statement_date`. An item a restore named is always looked up at the rail before it is ever submitted.
   - `reissue_after_lift`: a credit note or void you must issue again; the lockdown refuses it now, so the lift opens
     a high-severity `restore_reissue_due` break with the item's number, amounts and refund date; issue it right after
     the lift, then resolve that break.
   - `reissued` with `reissued_as`: only for a lost credit note (a credit note on the **same invoice** for at least the
     same amount and refund, issued after the item) or a lost void (the **same invoice**, voided after the item).
   - `lost` with the reason: it never happened or no longer applies (an invoice issued and voided after the backup), or
     you handled it outside Finance (a deadline re-recorded with `sow-ended` / `acknowledgements`, an email re-sent).
   `POST /fin/v1/breaks/<id>/resolution` refuses the restore break (and every deadline break).
4. Anything Stripe does not know (a wire, an ACH credit to the bank) is not re-imported: re-post those bank lines
   through the bank feed as usual (`POST /fin/v1/bank/events`), for every day from `since` to `opened_on`, then
   confirm it with the statement's figures for EVERY calendar day (weekends and holidays included: ACH and wire
   value dates fall on them) and every bank account (zbc 1020, zbc 1010, zbm 1010): the number of credit lines,
   their total (0 and 0.00 for a day without activity) and the day's closing balance:
   ```
   curl -sS -X POST $H $F/fin/v1/restore/bank-refeed -d '{"request_id":"rf-1","since":"2026-11-07","until":"2026-11-09",
     "statement_ref":"<statement id>","statement_days":[{"day":"2026-11-07","entity":"zbc","account":"1020",
     "credit_lines":2,"credit_total":"1500.00","closing_balance":"8250.00"}, ...]}'
   ```
   Finance compares each day with the bank lines it has booked; any difference is refused (409
   `RESTORE_REFEED_MISMATCH`, listing the days; a day outside the window or a missing day is refused too): feed the
   missing lines and confirm again. The last day's closing balance of each account is checked against the bank's
   own balance. The lift and the MSA 4.12(g) exception need this confirmation AND the bank's balances to equal the
   books (less money recorded as moved and not booked yet), checked again when you ask; the exception is refused for
   an invoice any bank line since the backup refers to (matched or not) and while an unattributed bank line could be
   that client's.
5. **Lift the lockdown** (only you, explicitly):
   ```
   curl -sS -X POST $H $F/fin/v1/restore/lockdown/lift -d '{"request_id":"lift-1","reason":"re-import 11-07..11-09 clean; 2 items disposed"}'
   ```
   Refused (409, listing what is missing) while the item list is incomplete, a day is not covered, or an item is not
   rebuilt or disposed of. The answer lists `lockdown_effects` (voids and credits made during the lockdown) and the
   `fenced` subjects. The lockdown file is written first: if that fails the answer is 503 and nothing changed; a retry
   with the same `request_id` always says the state as it is now. Issue any `restore_reissue_due` items now.

   **After the lift money out still waits** (`/health` `restore_lockdown.post_restore_reconcile_required: true`):
   every bank transfer, rail submit, release and refund payment answers 409 `POST_RESTORE_RECONCILE_REQUIRED` until a
   reconciliation run AFTER the lift (`POST /fin/v1/reconciliations/run`, or fin-cron's daily run) matches every leg
   (bank, rails, Stripe, sub-ledgers) and leaves no reconciliation break open. No other reconcile ends this phase
   (a second restore during it stacks a new lockdown, then this phase again). Book every `moved` item with its
   restore correction first; the parked rail events are applied at the lift in the order they arrived (one that does
   not apply yet, e.g. a return before its payment, stays held until it does). Then money out resumes. Client email
   resumes too; a message or client receipt the ledger shows was sent is marked sent and never sent again, on every
   path (the mail job and the direct send route), also after a restore that lost no money.

   **Only the affected counterparties wait.** A `moved` item not booked yet keeps its counterparty fenced (that
   payee is held out of every run with `PAYEE_HOLD` / `RESTORE_FENCED`; that campaign gets no refund). A
   reconciliation leg or break that is off by exactly the unbooked items' amounts is explained by them: it ends the
   post-restore phase and does not block anyone else's run. Any other difference still blocks everything.

   **A parked rail event still held** (one that never applies, e.g. a `returned` for an item that was lost) keeps
   the reconciliation from ending the post-restore phase. After 72 h finance logs `ALERT finance:
   rail_event_parked_72h` (once a day). Decide it:
   ```
   curl -sS -X POST $H $F/fin/v1/rail-events/stripe/<event id>/disposition \
     -d '{"request_id":"re-1","action":"discard","reason":"the rail confirms no return"}'
   ```
   `apply` applies it now (refused while it still does not apply); `discard` drops it with your reason. Both are on
   the ledger.

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
