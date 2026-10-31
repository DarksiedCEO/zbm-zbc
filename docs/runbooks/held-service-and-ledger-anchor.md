# Runbook: a held service, reconcile mode, and the ledger anchor

Fix wave A, AEGIS rounds 1 and 2 (ADR 0025 sections 7 and 8). For Finance (`FIN_`), Legal (`LEGAL_`) and Compliance
(`COMPLIANCE_`). Reconcile routes: `/fin/v1/reconcile`, `/legal/v1/reconcile`, `/compliance/v1/reconcile` (your
Andre token). Every check below is `GET /health` on the private network or through the tunnel (no token).

## What `/health` says

| `status` (HTTP) | Meaning | Section |
|---|---|---|
| `ok` (200) | Writes work. A `warnings` list (still 200) needs you but stops nothing. | 5 |
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
     in your notes. Then, on Compliance's volume (`railway ssh` into compliance), move both files aside with today's
     date: `mv ledger_anchors.jsonl ledger_anchors.<date>.jsonl` and `mv ledger_anchor_state.json
     ledger_anchor_state.<date>.json`, and redeploy compliance. The next run starts a new chain from today's head;
     the old files stay as evidence.
  3. If you did not restore the ledger: someone rewrote it. Keep both volumes as they are, take snapshots, and treat
     the ledger as untrusted from the stored entry onward.
- **`anchor_check_failing`**: the job itself failed (a garbage record inside the store, a state file Compliance cannot
  read, a full disk). The log line `anchor_check_failing` names the error type. Fix the cause; the flag clears on the
  next successful run (daily, and at every start). A garbage record in the middle of the store is not repaired by
  itself: move the store aside as in step 2 above after checking it.
- **`ledger_anchor_stale`**: no anchor stored or confirmed for more than 26 hours. Usually Compliance cannot read
  `GET /ledger/anchor` (its ledger token changed, or the ledger is down). Check Compliance's log for
  `compliance scheduler: ledger anchor run failed`. It clears at the next good run.
- **A torn last line** (a crash mid-append) is cut back to the last whole record by itself, with the ALERT line
  `ledger_anchor_store_repaired`. Nothing to do.
- **Warning `ledger_anchor_unkeyed`**: `COMPLIANCE_LEDGER_ANCHOR_KEY` is not set, so the store is only a hash chain
  (anyone who can write Compliance's volume can rewrite it consistently). Set the key (`openssl rand -hex 32`, Sealed)
  **before the first anchor is stored**. If the store already has unkeyed records, setting the key makes them fail
  verification (`ledger_anchor_mismatch`, "does not verify at record 1"): move both files aside as in step 2 when you
  set the key.

## 5. Warnings (200, `warnings` list)

| Warning | Where | What to do |
|---|---|---|
| `rotation_expired` | finance, legal, compliance | A rotation window ended with `_NEXT` variables still set; they are ignored. Remove them and `<P>_ROTATION_ENDS_AT` (token-rotation runbook). |
| `ledger_anchor_unkeyed` | compliance | Section 4. |
| `clock_skew` | ledger | The host clock jumped. `recorded_at` never goes backwards; while the warning lasts, timestamps follow the monotonic clock. A warning that stays (the last entry is ahead of the wall clock) means an entry was stamped under a wrong clock at boot: fix the host clock and note the time of the first wrong entry; the entries stay verifiable and `seq` is the order of record (services/ledger-rust/README.md, "Time"). |
