RUNTIME: REAL_PROTOCOL_LOCAL for scope ooh-py (Department 15) at 75b0662.
VERDICT: BLOCK. One HIGH finding, H-1: scenario (e), the same-size in-place replacement of the last log line, is NOT refused.
O-1 and O-2 (LOW) are re-confirmed.

Binding:
- candidateSha 75b0662823b867df3325d676b1dcf69abe37a425 · tree 67c15180382fab1a391b78756467c1a79a934973
- branch ooh-dept-15-wave2 (local) · repo DarksiedCEO/zbm-zbc · scope services/ooh-py
- ledger-rust tree 4f1ab90e2f81245130d6eb87091745146f6398e6 · ooh-py/src tree 6d8a444233eed6ac29bcbdca37417c70e5002acc. Both are identical at ce665c5, so the src and ledger deltas are EMPTY.
- ledger binary sha256 f902fedddde1a91c12ac1e5d93ea0860267d559fcf325c32553cc65c555ece6e, built by this gate.
Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega7 run). Fresh context. I reused omega6's drivers (label change only, omega6 -> omega7); omega6's verdicts were not relied on.

## 0. Binding / delta
- `git -C $S/review-75b0662 rev-parse HEAD HEAD^{tree}` -> 75b0662…, 67c15180…. `status --porcelain` -> 0 lines, before and after.
- `git diff --stat ce665c5 75b0662`:
  - docs/adr/0022 (+17/-1)
  - docs/test-counts.md (1/1)
  - tests/test_aegis_integrity_path.py (+3/-1, fixture also releases the lock)
  - tests/test_aegis_store_write_path.py (+280, new)
- `-- services/ooh-py/src services/ledger-rust` -> empty.
- Extracted with `git archive 75b0662 | tar -x` into runtime/src. A second archive was compared with `diff -r` after all runs (runtime/verify_extract): IDENTICAL.
- Main repo: only the pre-existing ?? .git.backup-preimport/ and ?? .idea/.

## Execution conditions
- Host: Darwin 25.6.0 arm64, 2026-10-18 ~10:20–10:35 UTC.
- Python: venv3.12 (3.12.14) and venv3.13 (3.13.9). Rust: cargo 1.90.0 (Homebrew).
- Every process ran on 127.0.0.1 with bind(0) ports.
- Dependency mode: the real ledger-rust binary and the real `python -m api` entrypoint, with no mocks. The war room is the exception (§3).
- Concurrency:
  - The batch ran sequentially and single-client.
  - The py3.12 runs of e1–e4 and f, and the falsifier runs, overlapped in time with the batch, on independent ports and dirs.
  - A peer gate's processes (omega7/data/poc/weaken_omega7.py, rl_omega7.py, data/ledger-target server) ran on the host and were left alone.

## 1. Ledger build (by this gate)
- Command: `cd runtime/src/services/ledger-rust && SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir runtime/target`
- Result: rc=0, "Compiling ledger-rust v0.1.0 (…omega7/runtime/src/services/ledger-rust) … Finished release in 4.94s" (runtime/cargo_build.log).
- runtime/target/release/server: sha256 f902fedd…5ece6e, Mach-O arm64, 2161008 bytes. All real-ledger runs used it.

## 2. live_run.py (real entrypoint + real ledger)
- Command: `LEDGER_BIN=runtime/target/release/server LIVE_WORK_DIR=runtime/live<v> venv<v>/bin/python devtools/live_run.py`
- py3.12: rc=0, "37/37 checks passed". Ledger pid 64925; service pids 64928/64940/64980. Out sha256 686392e8de9f…
- py3.13: rc=0, "37/37 checks passed". Ledger pid 64989; service pids 64992/65009/65017. Out sha256 93ec88ce9d02…
- Disclosure: the first attempt (in batch.sh) failed with rc=1 FileNotFoundError, because my runner had not created LIVE_WORK_DIR. That was a runner error, not the candidate's, and is kept as live<v>.attempt1_nodir.out. The runs above were re-run after mkdir.

## 3. War room (in-process, fake ledgers, NOT real-ledger evidence)
- Command: `run.py --department ooh-py --seed N --python <v>`
- py3.12 seeds 1/7/13: rc 0, Gate PASS, 156/156, 156/156, 170/170, 0 FAIL, 0 ERROR, MUST 100%, known 0.
- py3.13: identical.
- "LLM personas: NOT_CONNECTED". Out sha256 prefixes:
  - py3.12: 06341d18 / c21055f8 / 40c9feb1
  - py3.13: 9de1382d / b97eedd4 / 982c819e

## 4. F1 / R19 / C07, with a ledger SIGKILL and restart: 6/6 RESULT PASS
- F1: real ledger "201 Created", then the ledger was SIGKILLed.
  - W2 -> 503 {LEDGER_UNAVAILABLE, maybe: True}; W3 -> INTEGRITY_UNVERIFIED.
  - After the restart, the retry got 201 with the original created_at. FINAL log 3 = anchors 3.
- R19: evidence 201, then the ledger was SIGKILLed.
  - W2 -> 503 {LEDGER_UNAVAILABLE, maybe: False}; W3 -> LEDGER_UNAVAILABLE.
  - FINAL log [t-before, t-after], 2 = 2.
- C07: the anchor was recorded and the log chmod 0400.
  - W2 -> 503 {STORE_UNAVAILABLE, maybe: True}; W3 and W3b (ledger SIGKILLed, rc -9) -> INTEGRITY_UNVERIFIED.
  - t-x rolled forward; log 3 = anchors 3.

## 5. Integrity scenarios a, b, b2, c (+clat): 10/10 RESULT PASS
- (a): the service was SIGKILLed after the ledger's 201. After the restart, "log line 2 sha256 == pending.line sha256 at kill: True". The retry got 201 with the original created_at. FINAL 3 = 3, ok.
- (b): the anchor was held and the service killed. After the restart, pending.discarded == pending.line. The anchor was delivered late, the job ran ok, and the log became [t-before, t-x] (revived).
- (b2): W3 t-y took seq 2. The job ran ok, and the stale set-aside was cleared.
- (c): ledger tail removed.
  - A plain ledger start gave rc 1 "REFUSING TO START". W3 -> INTEGRITY_UNVERIFIED.
  - After the operator reset, the job reported ok False, "local log line 2 has no matching anchor". W4 and W5 (after a service restart) -> 503. The ledger was unchanged.

## 6. Pending-unremovable (d, d2), T222, both Pythons
Observed on all 4 runs:
- W2 -> **503 {LEDGER_UNAVAILABLE, maybe: True}**. pending.line kept with the same sha; dir 0500.
- Ledger "401 Unauthorized". W3 and W3b -> INTEGRITY_UNVERIFIED, nothing recorded.
- job2 ok. The W2 retry got 201. FINAL log 3 = anchors 3, no pending files. The refused writes never appear.
- The driver printed RESULT FAIL on all 4 runs, for the same single predicate as omega6 (the problem text, which is O-2). pw_eval.out: "FAILING: ['problem_is_could_not_be_removed']" ×4.

## 7. NEW: store write path on real processes
- Driver: runtime/scripts/store_witness.py (sha256 f0aef862…), written by this gate.
- No relay: LEDGER_SERVICE_URL is the real ledger, and the faults are file edits made between requests.
- Each scenario ran on both Pythons. Outputs are runtime/sw_<s>_<v>.out; results agree across the Pythons.

**(e1) append b"planted\n" to ooh_log.jsonl: PASS**
- W3 -> 503 {STORE_UNAVAILABLE, maybe: True}. The file is unchanged (sha256 equal). The anchor was recorded (anchors 2 -> 3), consistent with maybe=true.
- Integrity reports ok False: "a log line was anchored but not written…". The job reported "…cannot be written here". W4 -> 503 INTEGRITY_UNVERIFIED.
- Restart -> the service exits rc 1, "StoreCorrupt: log line 3 is not JSON".
- After the operator removed the planted tail and restarted: t-x rolled forward, integrity ok, log 4 = anchors 4 (after t-after).

**(e2) same-size other bytes replacing the LAST line in place (b"y"*len): FAIL vs required**
- Status integrity is ok before W3.
- **W3 -> 201**. The file changed: line 3 was appended after the tampered line 2.
- **/audit/integrity -> ok True**. The **integrity job -> ok True**. **W4 -> 201**.
- Restart -> the service exits rc 1, "StoreCorrupt: log line 2 is not JSON".
- The original line was restored by this driver (an operator would normally not have those bytes): the restart was ok.

**(e3) same-size FORGED, chain-valid line (t-mid -> t-mie, record_sha256 recomputed): FAIL vs required**
- W3 -> 201 and W4 -> 201. Audit and job -> ok True.
- Restart -> rc 1, "StoreCorrupt: log chain broken at line 3". The acknowledged lines t-x and t-y now sit behind a broken chain, and the bytes of the original line 2 are gone from disk.

**(e4) forged line, no write, restart: PASS**
- After the restart, integrity ok False, "local log line 2 has no matching anchor on the ledger". The next write -> 503 INTEGRITY_UNVERIFIED.
- So "integrity must not report ok after a restart" holds in e1–e4: either the service refuses to start or integrity is not ok.
- Before a restart, e2 and e3 report ok while the file is not what memory holds.

**(f) data dir chmod 0500 before the write: PASS**
- W3 -> 503 **{STORE_UNAVAILABLE, maybe: False}**. The ledger's ooh log_anchor count stayed 1 (**no anchor recorded**).
- The file is unchanged, and the dir has no pending.line or .tmp. Integrity stays ok (correct: nothing took effect). W4 -> the same 503.
- Note: the typed tenant_created evidence event IS recorded before the pending write. This is the record-first design (ADR round 6 M1, shown as "attempted"). I did not query /audit/evidence to confirm it is shown as attempted.
- After restoring 0700: W5 201, and the W3 retry got 201. After a restart: integrity ok, log 4 = anchors 4, ledger verify valid.

**Falsifier for H-1** (scratch copy only: runtime/fals/tree, diff runtime/fals/content_check.diff, sha256 fbee27a2…)
- The change: store.py:183 also compares `os.pread(fd, expected, 0)` with the in-memory lines.
- py3.12 result: e2 W3 -> 503 {STORE_UNAVAILABLE, maybe: True}, file unchanged. e3 likewise.
- e1 and f were unchanged. So the scenario discriminates.

## 8. O-1 / O-2 re-check
- O-1 (LOW, re-confirmed): clat on both Pythons. After the operator-reset rollback, "W3 before any check" -> **201**; the next job ok False; W4 and W5 -> 503. The code is unchanged (service.py:161 cached check; :314-315).
- O-2 (LOW, re-confirmed): on d and d2, after W2 the problem reads "a ledger answer was lost…" rather than "the pending line could not be removed". service.py:205 overwrites what :247-248 set. Behaviour is otherwise correct.

## Findings
**H-1 (HIGH, BLOCK): the write path checks only the log file's SIZE against memory, so a same-size in-place change is written over, and integrity reports ok.**
- Code:
  - store.py:180-184 (`elif size != expected`) is the only file-vs-memory check before the pwrite.
  - The service never calls `RecordLog.verify()` (store.py:213): `grep -rn "log\.verify()" src` -> 0.
  - verify_integrity (service.py:307-340) checks the ledger against the in-memory line_shas, never the file.
- Contradicted claims at this SHA:
  - ADR 0022:225, "before a write the file must be exactly memory. Any other tail is refused".
  - The test docstring at tests/test_aegis_store_write_path.py:5.
  - The caller's required property (e): "replace the last line with same-size other bytes; the next write must be refused (503) and the file left untouched".
- T151/T154 ("same size other bytes") covers same-size bytes in the NEXT-line slot only, not a replaced existing line.
- Consequence:
  - 201-acknowledged writes are appended to a log that differs from memory, and /audit/integrity and the job say ok.
  - It is caught only at the next start, which refuses. With a forged line, the original bytes are lost and the acknowledged lines sit behind a broken chain, so operator surgery is needed.
- Preconditions: write access to the data dir, or same-size on-disk corruption.
- Not a regression: src is unchanged since 4a0a4b4. store.py says it is copied from seo-py, so other services may share it (not checked).
- Severity rationale: it is a falsified integrity claim in the very commit that pins this path, and a caller-required runtime property fails. Ω may adjudicate the severity. The store.py:11-12 docstring does delegate in-place edits to start-up detection.
- Reproducer: `SCEN=e2|e3 LEDGER_BIN=runtime/target/release/server TREE=runtime/src WORK=<new dir> venv3.12/bin/python -I runtime/scripts/store_witness.py`
- Falsifier: the content_check.diff copy turns e2/e3 W3 into 503 with the file unchanged.

**O-1 (LOW, non-blocking)** and **O-2 (LOW, non-blocking)**: as in §8, unchanged.

## Leftovers / external
- Every port is NOT_CONNECTED. Only 127.0.0.1 was used. No live or production endpoint was read or written. Nothing was pushed or edited in the candidate.
- `ps -axo pid,ppid,command | grep -E "omega7/runtime|python -m api|_witness.py|target/release/server"` -> no process of this gate.
- Peer-gate processes were seen and left alone: 64941, 64946, 64994, 65621 (omega7/data/…).
- All sw_* data dirs are back at 0700.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN / PRODUCTION_PROVEN: no deployed SHA was read, and every port is NOT_CONNECTED. Not claimed.
- pytest (AEGIS-TEST's job), including the new test_aegis_store_write_path.py.
- (e):
  - A same-size replacement of a MIDDLE line was not run. It is expected to behave like e2, but that is inferred, not observed.
  - A service restart while the file is tampered, but before any write, was run only for the forged case (e4).
  - Not run: a whole-file same-size rewrite, and a truncate-then-append.
- (f):
  - Only a dir chmod was run. Not run: a read-only filesystem, ENOSPC/quota, EIO, fsync failure injection, and a short pending write on a real process. Those are unit-only in the new tests.
  - The /audit/evidence "attempted" view of the refused W3 was not queried.
- Falsifiers:
  - Only the H-1 falsifier was run, on py3.12.
  - Not re-run at this SHA: omega4/5/6's falsifiers (F1/R19/C07, ma/mb/mc, T222).
- Not run: per-caller ledger mode, Linux/CI/other architectures, concurrent writers during faults, and a simultaneous ledger+service kill.
