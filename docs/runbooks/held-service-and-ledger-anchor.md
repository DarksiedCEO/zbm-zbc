# Runbook: a held service, reconcile mode, and the ledger anchor

Fix wave A, AEGIS rounds 1 and 2 (ADR 0025 sections 7 and 8). For Finance (`FIN_`), Legal (`LEGAL_`) and Compliance
(`COMPLIANCE_`). Reconcile routes: `/fin/v1/reconcile`, `/legal/v1/reconcile`, `/compliance/v1/reconcile` (your
Andre token). Every check below is `GET /health` on the private network or through the tunnel (no token).

## What `/health` says

| `status` (HTTP) | Meaning | Section |
|---|---|---|
| `ok` (200) | Writes work. A `warnings` list (still 200) needs you but stops nothing. | 5 |
| ledger `degraded` (503) with `ledger_clock_behind_log` | The ledger's newest entry is more than 5 minutes ahead of the host clock. Appends still work. | 5 |
| `degraded` (503) | The instance cannot write now: `degraded_reasons` names why (ledger unreachable, a write not yet reconciled, a log fault, `ledger_anchor_*`). It usually clears by itself once the ledger is back. | 4 |
| `starting` (503) | The start-up hold is waiting for the ledger (`ledger_unavailable_at_start`). Clears by itself. | |
| `refused` (503) | The start-up hold refuses this log: `interrupted_write_anchored`, `volume_generation_mismatch` or `log_divergence`. Needs you. | 1, 2 |
| `maintenance` (503) | Reconcile mode is on (`<P>_RECONCILE_MODE=1`): every write is refused. | 3 |

Railway's deploy healthcheck is `/livez` (200 whenever the process serves), so a held or maintenance deployment is
still "Active" in Railway. Only `/health` and the alerts tell you.

## 1. `interrupted_write_anchored`: a crash right after the anchor (AEGIS round 2 N-10)

What happened: the service was killed (OOM, host move, SIGKILL) after the ledger recorded the write's anchor
(`local_log_appended`) and before the service saw the answer or finished the local line. A restored volume snapshot
taken inside a write looks exactly the same, so the service does not guess: it holds, and nothing it serves changes.
The service stays down for writes until you do this (about 10 minutes):

1. Find out whether the write was **acknowledged**: did the caller get a 2xx for it? Look at the caller's log around
   the time of the crash (the timestamp is in the service's log line `startup refused … interrupted_write_anchored`).
   - **Not acknowledged** (the caller saw a timeout, a 5xx or a dropped connection): the usual case after a crash.
     Continue with step 2; the caller retries the operation afterwards.
   - **Acknowledged** (the caller holds a 2xx): this was a restored or rolled-back volume, not a crash. Do **not**
     void it. Restore the newest volume backup that contains the write, or record the operation again by hand from the
     caller's copy, then go to section 2.
2. Deploy the service with `<P>_RECONCILE_MODE=1`. `/livez` answers 200, `/health` answers 503 `maintenance`.
3. `GET /<dept>/v1/reconcile` with your token. Read the plan: `voidable.lines` and `voidable.event_ids` must name only
   the one interrupted write (its event types are listed). `fatal` must be empty; if it is not, stop and go to
   section 2.
4. `POST /<dept>/v1/reconcile` with exactly `head_sha256`, `void_lines` and `void_event_ids` from the plan.
   The voids are recorded on the ledger and in the log (`reconcile`, not automatic).
5. Remove `<P>_RECONCILE_MODE` and deploy. `/health` is 200 `ok`.
6. Have the caller retry the operation (the same request id is fine: a voided write is "not done").

## 2. `volume_generation_mismatch` or `log_divergence`

The volume holds a log the ledger does not agree with: a restored or cloned volume, another instance writing, or a
tampered log. Do not reconcile blindly. Stop any second instance first. Then compare the ledger's entries for the
department (`GET /ledger/entries`, a read token) with the newest volume backup. Restore the backup that matches the
ledger, or, if only voidable residue remains, follow section 1 from step 2. A FATAL plan cannot be reconciled:
restore the data directory.

## 3. `maintenance`: reconcile mode left on

`<P>_RECONCILE_MODE=1` refuses every write. If you are not in the middle of section 1 or 2, remove the variable and
deploy. The ops-alerts health poll alerts on `maintenance` after 10 minutes (`health_poll.py`).

## 4. The ledger anchor on Compliance's `/health` (AEGIS round 2 N-2, N-6)

Compliance stores the ledger's head daily in `ledger_anchors.jsonl` on its own volume and checks that the ledger still
extends the last stored head. The state of that check is kept beside the store in `ledger_anchor_state.json`, so a
restart does not hide it.

- **`ledger_anchor_mismatch`**: the ledger now holds fewer entries than the stored anchor, or a different hash at that
  position. The ledger was rolled back or rewritten, or the anchor store was altered. Treat it as an incident:
  1. Do not redeploy the ledger. Read the ALERT line `ledger_anchor_mismatch` in Compliance's log (it names the
     stored time and the entry count) and compare `GET /ledger/anchor` with the last record in
     `ledger_anchors.jsonl` and with your off-Railway copy (railway.md, "Daily external anchor").
  2. If the ledger was restored from a backup **on purpose** (you know why entries are missing), record that decision
     in your notes. Then, on Compliance's volume (`railway ssh` into compliance), **copy** both files to dated
     archives (`cp ledger_anchors.jsonl ledger_anchors.<date>.jsonl`, `cp ledger_anchor_state.json
     ledger_anchor_state.<date>.json`; never delete them: they are the history), then move the live pair aside and
     redeploy compliance. The next run starts a new chain from today's head; the archived chain stays verifiable with
     the same key.
  3. If you did not restore the ledger: someone rewrote it. Keep both volumes as they are, take snapshots, and treat
     the ledger as untrusted from the stored entry onward.
- **`anchor_check_failing`**: the job itself failed (a garbage record inside the store, a state file Compliance cannot
  read, a full disk). The log line `anchor_check_failing` names the error type. Fix the cause; the flag clears on the
  next successful run (daily, and at every start). A garbage record in the middle of the store is not repaired by
  itself: move the store aside as in step 2 above after checking it.
- **`anchor_store_missing`**: the state file says anchors were stored, but `ledger_anchors.jsonl` is gone, empty or
  shorter than it was. No new chain is started over it. Find out why (a volume restore, a deletion). Restore
  `ledger_anchors.jsonl` **and** `ledger_anchor_state.json` together from the same volume backup (a store restored
  alone is older than the state and stays red). If only the store survives, keep it: after Andre's review run the
  one-time resync below; it rebuilds the state from the store's own verified records and discards nothing.
- **`anchor_state_invalid`** (AEGIS round 4 N4-5): the state file is missing beside a store with history, is unsigned
  (every state written before round 4: the first start after the upgrade shows this once), or does not verify with
  `COMPLIANCE_LEDGER_ANCHOR_KEY` (edited, or another key). Nothing is written over it. After checking why, run once on
  compliance (`railway ssh`):
  `COMPLIANCE_LEDGER_ANCHOR_KEY=<the key> python3 src/ledger_anchor.py resync "$COMPLIANCE_DATA_DIR"`. It repairs a
  torn tail back to the last valid record, verifies every record, keeps any recorded mismatch, and writes a signed
  state. If a record does not verify it refuses; treat that as a mismatch (above).
- **A failed append** (ENOSPC mid-record) is cut back to the size before it and the run fails
  (`anchor_check_failing`); it is never reported as stored. Free space; the next run stores the anchor.
- **`ledger_anchor_stale`**: no anchor stored or confirmed for more than 26 hours, counted from the last good run
  kept in the state file (a restart does not reset it). Usually Compliance cannot read
  `GET /ledger/anchor` (its ledger token changed, or the ledger is down). Check Compliance's log for
  `compliance scheduler: ledger anchor run failed`. It clears at the next good run.
- **A torn last line** (a crash mid-append) is cut back to the last whole record by itself, with the ALERT line
  `ledger_anchor_store_repaired`; a complete, valid last record that only lost its newline is kept. Nothing to do.
- **Warning `ledger_anchor_unkeyed`**: `COMPLIANCE_LEDGER_ANCHOR_KEY` is not set, so the store is only a hash chain
  (anyone who can write Compliance's volume can rewrite it consistently). Set the key (`openssl rand -hex 32`, Sealed)
  **before the first anchor is stored**. If the store already has unkeyed records, setting the key makes them fail
  verification (`ledger_anchor_mismatch`, "does not verify at record 1"): move both files aside as in step 2 when you
  set the key.

## 5. Rotation and the ledger clock (503)

- **`rotation_expired_unpromoted`** (finance, legal, compliance): a rotation window ended with `_NEXT` variables still
  set. Only the new values work now; the old ones are refused. Promote each `_NEXT` value to current, delete the
  `_NEXT` variables and `<P>_ROTATION_ENDS_AT`, deploy (docs/runbooks/token-rotation.md).
- **`ledger_clock_behind_log`** (ledger): the newest entry is more than 5 minutes ahead of the host clock. New entries
  are stamped 1 µs after it (never backwards) until real time catches up. Fix the host clock. The 503 clears by
  itself after (size of the jump − 5 minutes) of real time. When the jump is known and understood (for example a VM
  that booted with a wrong clock), acknowledge it: `POST /ledger/clock/acknowledge` `{"through_seq": <newest seq>}`
  with a ledger bearer and your `X-Andre-Approval-Token`. It is recorded on the ledger and turns the 503 into the
  200 warning `ledger_clock_behind_log_acknowledged` for that pinned run only; another jump is a 503 again. If an entry was written
  under a clock far in the future (for example at boot), the stamps stay pinned and the alarm stays on until real
  time passes that entry: note the `seq` of the first wrong entry; `seq` is the order of record and the chain stays
  verifiable (services/ledger-rust/README.md, "Time").

## 6. Warnings (200, `warnings` list)

| Warning | Where | What to do |
|---|---|---|
| `ledger_anchor_unkeyed` | compliance | Section 4. |
| `ledger_clock_behind_log_acknowledged` | ledger | Section 5: a pinned run you acknowledged; it ends when real time passes the newest stamp. |
