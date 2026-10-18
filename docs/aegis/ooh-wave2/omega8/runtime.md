RUNTIME (H-1 scope): REAL_PROTOCOL_LOCAL for services/ooh-py at 99569bd.
VERDICT: RUNTIME_GREEN_FOR_SCOPE. H-1 is closed for the witnessed scenarios. One LOW finding, O8-1 (non-blocking): the audit-route throttle.

Binding:
- candidateSha 99569bd85a791c102c6df293d7e987743813644b · treeOid 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae
- subtree services/ooh-py/src bf1cb95f8a8ccad4ac613ad4ca6a66923f33dc70 · subtree services/ledger-rust 4f1ab90e2f81245130d6eb87091745146f6398e6 (identical at 75b0662)
- repo DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2 · scope: founder-scoped to H-1 (Ω GREEN condition 1)
- control 75b0662823b867df3325d676b1dcf69abe37a425 (tree 67c15180…, ooh-py/src 6d8a4442…)
Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega8 run). Fresh context, independent of the builder. I did not rely on omega7's verdicts. Its driver was the model for mine; I wrote my own (sw8.py).

## 0. Binding checks
- Frozen worktree review-99569bd: `git rev-parse HEAD HEAD^{tree}` -> 99569bd…, 7d08f348…. `status --porcelain` -> 0 lines, at the start and at the end.
- Main repo: only the pre-existing `?? .git.backup-preimport/` and `?? .idea/`.
- Runs used my own copies: `git archive 99569bd | tar -x` -> runtime/src99, and `git archive 75b0662` -> runtime/src75.
- After all runs, fresh archives compared with `diff -r`: src99 IDENTICAL, src75 IDENTICAL. The candidate was not mutated.
- store.py blob at 99569bd is 35a439c8 (git hash-object of my copy = `git rev-parse 99569bd:services/ooh-py/src/store.py`). sha256 7a8cc64e…bd0c.
- Code delta 75b0662..99569bd under ooh-py: src/service.py +2, src/store.py +44/-25, tests/test_aegis_h1_file_content.py +180 (new).

## Execution conditions
- Host: Darwin 25.6.0 arm64, 2026-10-18 ~13:21–13:33 UTC. 127.0.0.1 only, with bind(0) ports.
- Python: venv3.12 (3.12.14) and venv3.13 (3.13.9); httpx 0.28.1, uvicorn 0.46.0.
- Dependency mode is REAL: the real ledger-rust binary and the real service entrypoint `python -m api` (api.main -> serve.run -> hardened uvicorn, real HTTP). LEDGER_SERVICE_URL is the real ledger: no relay, no mock, no fake ledger.
- Faults are file edits made by the driver between requests, while the service process runs.
- Concurrency: one sequential stream per Python, and the two streams ran in parallel on independent ports and data dirs. Single client per service.

## 1. Ledger build (by this gate, from my 99569bd copy)
- Command: `cd runtime/src99/services/ledger-rust && SDKROOT=…MacOSX26.5.sdk cargo build --release --offline --locked --target-dir scratchpad/rt8-target`. The target dir is outside omega8.
- Result: rc=0, "Compiling ledger-rust v0.1.0 (…omega8/runtime/src99/services/ledger-rust) … Finished release in 8.20s" (runtime/cargo_build.log).
- Binary: rt8-target/release/server, sha256 2d26e88d23ab1c138758ff57ea11cf97baeb72c8d9bcff2413620e3a87dbcf9d, Mach-O arm64. All runs used it, including the control (same ledger tree).

## 2. Driver
- runtime/scripts/sw8.py, sha256 6d03e23e…6531; runtime/scripts/batch8.sh 1011548f…be47; runtime/scripts/summ8.py fa949181…8978.
- Raw outputs: runtime/runs/<name>.out and runtime/runs/<name>/result.json (+ ledger.log, service.log, data dirs). Summary: runtime/summary.txt (sha256 5f6c3281…0899).
- Batch logs: runs/batch_3.12.log, runs/batch_3.13.log. All 40 runs rc=0. A trial run is kept as runs/trial_c99_e2_W_3.12*; it matches the batch.
- Every POST records: the file sha256 before and after, file_unchanged, the ledger ooh log_anchor count before and after, and whether pending.line is present.
- Faults, after W1 t-before and W2 t-mid (201), with a sleep of 11 s so the 10 s FORCED_MIN_S window since start-up has passed:
  - e1: append b"planted\n" (omega7 e1)
  - e2: the LAST line replaced in place by b"y"*len (omega7 e2)
  - e3: the LAST line replaced by a same-size, self-consistent forged line, t-mid -> t-mie with record_sha256 recomputed (omega7 e3)
  - e4: e3's fault, no write, restart (omega7 e4)
  - Added by this gate:
    - m2: an EARLIER line (line 1 of 2) -> b"y"*len
    - m3: an earlier line forged same-size (t-before -> t-beforf)
    - e4y: e2's fault, no write, restart
    - e2 rename: e2 with the file replaced by os.replace (new inode)
    - thr: a throttle probe
- Mode W (write first): fault -> W3 -> audit -> job -> W4 -> file put back WHILE RUNNING -> (11 s) job -> audit -> W3 retry (same request_id) -> W5 -> restart -> audit -> write -> final (ledger /verify).
- Mode I (integrity first): fault -> /audit/integrity, twice -> job -> W3 -> W4 (no write before the checks) -> put back -> as in W.

## 3. Results at 99569bd (both Pythons; 3.12 and 3.13 give the same results)
**e2 / e3 / m2 / m3 / e1, and e2-rename, mode W:**
- W3 -> **503 {STORE_UNAVAILABLE, maybe: true}**, with **file sha256 before == after**.
- The ledger's anchor count went 2 -> 3: the anchor is recorded before the local write, which is consistent with maybe=true. pending.line was kept.
- /status integrity -> ok False, "a log line was anchored but not written; it is rolled forward at the next integrity check".
- /audit/integrity and the job -> ok False, "the pending line is anchored on the ledger but cannot be written here".
- W4 -> 503 {INTEGRITY_UNVERIFIED, maybe: false}: file unchanged, anchors 3 -> 3.
- After the put-back (file sha == the pre-fault sha):
  - The job rolled t-x forward. The log became 3 lines, anchors stayed 3, pending.line was gone, and the job and audit gave **ok True**.
  - The W3 retry with the same request_id -> 201 with the original created_at (13:22:51Z) and no new anchor.
  - W5 -> 201. Restart -> audit ok, write 201.
- FINAL: log 5 lines = 5 anchors, ledger verify valid, tenants [t-before, t-mid, t-x, t-z, t-after]. The refused t-y never appears.

**e2 / e3 / m2 / m3, mode I (integrity with no write in between):**
- /audit/integrity -> **ok False, "the local log file differs from the lines this service holds: an operator must inspect it"**. The second call and the job gave the same; /status agrees. The file sha stayed equal to the fault sha.
- W3 and W4 -> 503 {INTEGRITY_UNVERIFIED, maybe: false}. The file was unchanged and nothing was anchored (2 -> 2).
- After the put-back: the job and audit -> ok True. The W3 retry -> 201 (a new write, anchors 2 -> 3), then W5 201. Restart -> ok.
- FINAL: 5 = 5, verify valid.

**e4 (forged, no write, restart):**
- Before the restart: audit and job -> ok False, "the local log file differs…".
- After the restart on the tampered file: integrity ok False, "local log line 2 has no matching anchor on the ledger". The write -> 503 INTEGRITY_UNVERIFIED, file unchanged.
- After the put-back and a restart: write 201, FINAL 3 = 3, verify valid.

**e4y (y-bytes, no write, restart):**
- Before the restart: ok False (file differs).
- The restart on the tampered file exits rc=1 with "store.StoreCorrupt: log line 2 is not JSON". The file was unchanged.
- After the put-back and a restart: write 201, FINAL 3 = 3.

**Refused-write bytes:** across all 26 candidate runs, every non-201 POST (W3, W4, W_restart) had file_sha_before == file_sha_after. The count of such changes is 0 (summ8 check over runs/c99_*/result.json).

## 4. Control at 75b0662 (both Pythons; same driver, same ledger)
- e2, e3 and m2, mode W:
  - **W3 -> 201** (file changed), audit and job -> **ok True**, and W4 -> 201.
  - After the put-back: ok. FINAL 6 = 6, with t-y accepted.
  - This is the old behaviour, so the scenario discriminates.
- e2 and e3, mode I: audit (twice) and job after the edit, with no write -> **ok True**; then W3 201 and W4 201.
- e4 control: audit and job before the restart -> ok True. After the restart -> "no matching anchor", and the write -> 503.
- e1 control: 503 STORE_UNAVAILABLE. The size check already caught an appended tail, so e1 does not discriminate H-1 (same as omega7).

## 5. Docstrings at 99569bd vs observed behaviour
- Location: Ω cites "store.py:159-160", which is the line numbering at 75b0662. At 75b0662 those lines held the `append_prepared` docstring "The file must be exactly the in-memory lines before the write (else refused)". At 99569bd, lines 159-160 are `prepare()` code, and the docstring is now at store.py:177-181.
- store.py:177-181 (append_prepared) says: "The file must be exactly the in-memory lines before the write, byte for byte (else refused, the file untouched: AEGIS H-1, a same-size edit of an existing line is refused too)".
  - **Matches the code**: store.py:192-203 compares `_pread_exact(fd, expected, 0) != mem`, plus the adopt tail compare.
  - **Matches what I observed**: e2, e3, m2 and m3 mode W gave 503 with the file untouched.
  - The adopt sentence is consistent with store.py:200-205 by reading, but was not runtime-witnessed.
- store.py:12-13 (module docstring) says: "While the service runs, the file must stay exactly the lines it holds: every write compares the whole file with them first, and every integrity check does too".
  - Writes: observed.
  - Integrity checks: observed whenever a check actually runs. verify_integrity calls `self.log.file_problem()` at service.py:335-336; a pending problem takes precedence, which is still not ok.
  - Caveat O8-1: an /audit/integrity call inside the 10 s window returns the cached result and runs no check.

## Findings
**O8-1 (LOW, non-blocking): /audit/integrity is throttled, so within FORCED_MIN_S (10 s) of the previous forced check it returns the cached ok after a same-size edit.**
- Observed in thr on both Pythons:
  - A_pre: ok True, checked_at 13:26:41Z.
  - Then the y-edit.
  - A_throttled (immediately after): **ok True, with the same checked_at 13:26:41Z**, so no check ran.
  - After 11 s: ok False, "file differs". The job (always=True) -> ok False.
- Code: service.py:316-317 (ADR 0012 L7, "a full ledger read at most every FORCED_MIN_S"); audit_integrity passes always=False (service.py:349).
- Why not blocking:
  - Every write re-compares the file regardless (mode W), so no write lands on an edited file in that window.
  - The scheduler job is not throttled.
  - The cached answer carries its checked_at.
- It does qualify the literal wording "every integrity check does too" (store.py:12-13) and the caller's property "an integrity check with no write in between must report not ok". That property holds for every check that runs: observed in mode I and in thr after the window.
- Reproducer: `SCEN=thr LEDGER_BIN=rt8-target/release/server TREE=runtime/src99 WORK=<new> venv3.12/bin/python -I runtime/scripts/sw8.py`
- Falsifier: a fresh check on every audit call would make A_throttled ok False with a new checked_at.

**H-1 (omega7, HIGH): not reproduced at 99569bd.**
- e2 and e3 (and m2, m3, e2-rename) are refused with 503, the file is left untouched, and integrity is not ok.
- The same scenarios at 75b0662 still give 201 and ok, so the scenarios discriminate.

**O-1 and O-2 (LOW, from omega7): not re-run.** Out of the founder-scoped H-1 scope.

## Leftovers / external
- No live or production endpoint was read or written. Every port is NOT_CONNECTED, and only 127.0.0.1 was used.
- No candidate edit, commit or push.
- `ps -axo pid,command | grep -E "omega8/runtime|rt8-target"` -> no process of this gate is left.
- `find omega8/runtime -name target -type d` -> none.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN / PRODUCTION_PROVEN: not claimed. No deployed SHA was read.
- pytest, including tests/test_aegis_h1_file_content.py: AEGIS-TEST's job.
- The adopt path at runtime (a file holding memory + exactly this line before the write); verified by reading only.
- Not run:
  - an edit landing between the byte compare and the pwrite (a TOCTOU race)
  - concurrent writers
  - a file deleted while running
  - truncate-then-append to the same size
  - a whole-file same-size rewrite of several lines
  - a large-log cost
- Restart while tampered was run only without writes on top: e4 and e4y. W-mode runs put the file back before restarting.
- The /audit/evidence "attempted" view of the refused W3 events was not queried.
- O-1, O-2, F1/R19/C07, the integrity a/b/c, pending d/d2, live_run.py and the war room were not re-run (out of scope).
- Not run: Linux/CI/other architectures, and per-caller ledger mode.
- The other five services that share the store.py code (OOH2-RT-H1-<SERVICE>) were not exercised.
