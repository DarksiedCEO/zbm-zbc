RUNTIME (H-1 re-witness + log-file hardening + OOH2-PII-1): **REAL_PROTOCOL_LOCAL** for services/ooh-py at 91ea9a1.
VERDICT: **RUNTIME_GREEN_FOR_SCOPE** (Ω conditions 1 and 4, as scoped below). One MEDIUM finding, non-blocking for this scope: RT9-1, the start-up read of the log is not hardened.

Binding:
- candidateSha 91ea9a1948253a3203150b38a8c2f2f839f232cd · treeOid 482eb8983853a8bdff118c16446474b0b16537da
- subtree services/ooh-py/src e3d8e68aa94c191a9f7c37a892e8391863418e69 · subtree services/ledger-rust 4f1ab90e2f81245130d6eb87091745146f6398e6 (identical at 99569bd and 75b0662)
- repo DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2 (`git rev-parse ooh-dept-15-wave2` = 91ea9a1…, so the candidate is the branch tip)
- controls: 99569bd85a791c102c6df293d7e987743813644b (tree 7d08f348…, ooh-py/src bf1cb95f…) and 75b0662823b867df3325d676b1dcf69abe37a425 (tree 67c15180…, ooh-py/src 6d8a4442…)
- store.py blob c2917835… and pii.py blob fbb1a33d… in my copy; both equal `git rev-parse 91ea9a1:<path>`.

Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega9 run). Fresh context, independent of the builder and of omega8.
- I read omega8's runtime.md and sw8.py only as a model.
- My drivers are new: common9.py, sw9.py and pii9.py. No omega8 verdict is carried forward.

## 0. Binding checks
- Frozen worktree review-91ea9a1:
  - `git rev-parse HEAD HEAD^{tree}` -> 91ea9a1…, 482eb898…
  - `status --porcelain` -> 0 lines.
- Main repo: only the pre-existing `?? .git.backup-preimport/` and `?? .idea/`.
- Every run used my own copies:
  - `git archive 91ea9a1 | tar -x` -> runtime/src91
  - `git archive 99569bd` -> runtime/src99
  - `git archive 75b0662` -> runtime/src75
- After all runs, fresh archives were compared with `diff -r`: src91, src99 and src75 are all IDENTICAL. The candidate was not mutated.
- Code delta 99569bd..91ea9a1 under ooh-py/src:
  - store.py +40: an S_ISREG check on the write path, and file_problem now uses O_NOFOLLOW|O_NONBLOCK, fstat, a size check and pread.
  - pii.py +95: card_spans/luhn and SSN shapes.
  - svc_campaigns.py: the card rule moved to pii, and a client_approval_doc_ref PII refusal was added.

## Execution conditions
- Host: Darwin 25.6.0 arm64 (MacBook Pro). Runs 2026-10-18, 14:44–15:02 UTC. 127.0.0.1 only, with ports from bind(0).
- Python: scratchpad venv3.12 (3.12.14) and venv3.13 (3.13.9).
- Dependency mode is REAL:
  - the real ledger-rust binary;
  - the real service entrypoint `python -m api` (cwd = <copy>/services/ooh-py/src), which is api.main -> serve.run -> uvicorn, over real HTTP;
  - LEDGER_SERVICE_URL is that ledger. There is no mock, relay or fake ledger, and OOH_NON_PRODUCTION is unset.
- Faults are filesystem changes made by the driver between HTTP requests, while the service runs: same-size edits, unlink + mkfifo, and symlinks.
- Concurrency:
  - one sequential stream per Python; the two streams ran in parallel on independent ports and data dirs;
  - a single client per service.
- Every client call has a timeout: 20 s for the hardening probes, and 5 s for /health and POST during a hang.
- RSS of the service process was sampled with `ps -o rss=` after each call.
- Memory safety: a watchdog (scripts/watchdog9.sh) SIGKILLs any scratch-venv `python -m api` whose RSS exceeds 1 GB.
  - During the batch it never fired: runs/watchdog.log does not exist.
  - A second watchdog (watchdog9b.sh, 512 MB, 20 ms poll) guarded the one rzero run and fired once (RT9-1).

## 1. Ledger build (this gate, from my 91ea9a1 copy)
- First attempt with `SDKROOT=$(xcrun --show-sdk-path)`: rc=101, "could not compile `zmij` (build script)". Not a candidate defect: the default SDK is wrong for this toolchain.
- Command: `cd runtime/src91/services/ledger-rust && SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir scratchpad/rt9-target`
- Result: rc=0, "Compiling ledger-rust v0.1.0 (…omega9/runtime/src91/services/ledger-rust) … Finished `release` … in 5.48s" (runtime/cargo_build.log).
- Binary: scratchpad/rt9-target/release/server, sha256 fcd30cf3facbf54f4e5baa65779fce52f533976a6e146aa667c92f206c76ea26, Mach-O 64-bit arm64. The target dir is outside omega9.
- All runs used this binary, including the controls (same ledger tree 4f1ab90e at all three SHAs).

## 2. Drivers and raw evidence (runtime/)
- scripts (sha256):
  - common9.py ffca4126…21be
  - sw9.py d42c1834…680f
  - pii9.py 3a083b85…4f19
  - batch9.sh da7d6293…8056
  - summ9.py f2cb1f99…ddf
  - table9.py 72297…f8c6
  - watchdog9.sh 0a3e8a77…87b1
  - watchdog9b.sh 1cd96d17…cd39
- Note: sw9.py was patched after the batch only to add the `rzero` scenario (3 string substitutions: rzero added to two tuples and to the docstring). The batch runs used the version before that patch; I did not record its hash. Nothing else changed.
- Raw evidence:
  - runs/<name>.out (timestamped log of every call) and runs/<name>/result.json, plus ledger.log, service.log and data dirs.
  - Batch logs runs/batch_3.12.log and runs/batch_3.13.log: 28 runs each, all rc=0.
  - Trial runs are kept as runs/trial_*; they match the batch.
- summary.txt (sha256 57b294f9…f64b) and table.txt (c44c952b…d930), one line per run.
- Every POST records:
  - the file sha256 (or its kind: fifo or symlink->target) before and after;
  - file_unchanged;
  - the ledger ooh log_anchor count before and after;
  - pending.line presence;
  - RSS.

## 3. H-1 re-witness at 91ea9a1 (both Pythons, identical results)
Setup: W1 t-before and W2 t-mid (201), then 11 s past FORCED_MIN_S. The same-size faults:
- e2: the last line -> b"y"*len
- e3: the last line forged same-size and self-consistent (t-mid -> t-mie, record_sha256 recomputed)
- m2: line 1 -> b"y"*len
- m3: line 1 forged (t-before -> t-beforf)
For each, `same_size: true` and fault_sha != orig_sha were recorded.

**Mode W (a write first):** c91_{e2,e3,m2,m3}_W_{3.12,3.13}
- W3 -> **503 {STORE_UNAVAILABLE, maybe: true}**, with **file_unchanged=True** (sha before == after).
  - The anchor count went 2 -> 3 (anchor before the write, consistent with maybe=true), and pending.line was kept.
- Audit and job -> ok False, "the pending line is anchored on the ledger but cannot be written here".
- W4 -> 503 {INTEGRITY_UNVERIFIED, maybe: false}: file unchanged, anchors 3 -> 3.
- **Put back while running** (orig bytes written):
  - job -> ok True, rolled forward;
  - audit -> ok True;
  - the W3 retry with the same request_id -> 201, file unchanged, anchors 3 -> 3 (the rolled-forward line, no new anchor);
  - W5 201; restart -> audit ok, write 201.
- FINAL: 5 lines = 5 anchors, ledger /verify valid, tenants [t-before, t-mid, t-x, t-z, t-after]. The refused t-y never appears.

**Mode I (integrity first, no write in between):** c91_{e2,e3,m2,m3}_I_*
- Audit, audit again, job and /status -> **ok False, "the local log file differs from the lines this service holds: an operator must inspect it"**.
- W3 and W4 -> 503 INTEGRITY_UNVERIFIED (maybe false): file unchanged, anchors 2 -> 2.
- Put back: job and audit -> ok True. The W3 retry -> 201 (a new write, anchors 2 -> 3), W5 201, restart ok.
- FINAL 5 = 5, valid.

**Refused-write bytes:** across all runs, 70 non-201 POSTs were recorded and the file changed in **0** of them (table9.py check over runs/c*/result.json).

**Control at 75b0662** (c75_{e2,e3,m2,m3}_W, c75_{e2,m3}_I, both Pythons):
- Mode W: W3 -> **201**, audit and job -> **ok True**, W4 -> 201, FINAL 6 = 6 with the refused-at-candidate t-y accepted.
- Mode I: audit and job after the edit -> **ok True**.
- So the scenarios discriminate, and 75b0662 still accepts them.

## 4. Hardening: FIFO, symlink to identical bytes, symlink to /dev/zero (both Pythons)
Fault while running: `unlink` the log, then one of:
- (fifo) `mkfifo(log, 0600)`
- (symsame) `symlink(log -> runs/<run>/same_bytes_target.jsonl)` holding the exact original bytes
- (symzero) `symlink(log -> /dev/zero)`

Mode H = integrity first: A1, A2, J1, /status, then W3, J2, W4. Mode HW = a write first (W3, which reaches the store's own open/fstat), then the rest.

**91ea9a1** (c91_{fifo,symsame,symzero}_{H,HW}_{3.12,3.13}, 12 runs):
- Every call answered: the **largest call duration in any run was 0.20 s**. There were no timeouts.
- RSS stayed between 68.9 and 83.9 MB throughout. For comparison, the e2/m3 runs on a regular file used 68.9–82 MB. So memory was bounded.
- Mode H:
  - A1, A2, J1 and /status -> **ok False**:
    - fifo and symzero: "the local log file differs…";
    - symsame: "the local log file cannot be read (OSError)", from O_NOFOLLOW (ELOOP).
  - W3 and W4 -> 503 INTEGRITY_UNVERIFIED, anchors 2 -> 2.
- Mode HW:
  - W3 -> **503 STORE_UNAVAILABLE maybe:true**: anchors 2 -> 3, pending kept, and the log is still a fifo, or still a symlink to the same target.
  - Then integrity -> ok False ("pending line … cannot be written here"), and W4 -> 503 INTEGRITY_UNVERIFIED.
- symsame: the **symlink target's bytes were unchanged** after all writes (`target_unchanged: True`). Nothing is written through a symlink.
- Put-back (unlink + regular file with the original bytes, while running):
  - J3 and A3 -> ok True (HW: the pending line rolls forward);
  - the W3 retry -> 201; W5 201; restart -> ok, write 201;
  - FINAL 5 = 5, valid, in all 12 runs.

**Control at 99569bd:**
- **c99_fifo_H_{3.12,3.13}: HANG.** A1 (/audit/integrity) got no answer within 20 s (`ReadTimeout`).
  - During the hang, /health (5 s) -> ReadTimeout, and a POST /tenants (5 s) -> ReadTimeout.
  - The service then **ignored SIGTERM for 5 s and was SIGKILLed** (`kill: SIGKILL (SIGTERM ignored 5 s)`).
  - Cause, by reading: 99569bd store.py:241 `open(self.path, "rb")` blocks on a FIFO while holding the service lock.
- c99_symsame_H_*: integrity -> **ok True** on the symlink (the old file_problem follows it and the bytes match). At 91ea9a1 it is ok False. This discriminates too.
  - Writes were already refused at 99569bd (its write path has O_NOFOLLOW).
- c99_fifo_HW_* and c99_symzero_HW_*: same as the candidate. A write first leaves a pending line, and a pending problem takes precedence over file_problem, so the old read is never reached. These do not discriminate.
- **c99_symzero_H was deliberately NOT RUN.** By reading, the old `fh.read()` of /dev/zero is unbounded, and I did not exhaust host memory to prove it.

## 5. OOH2-PII-1 through real HTTP (pii9.py; both Pythons)
Setup through the API only, against the real ledger:
- tenant acme; PRINCIPAL determination; los-angeles PLANNING_COVERAGE and EXECUTION_AUTHORIZED; vendor lamar with payment_methods.
- Then, per case: a campaign, brief and plan; a face and source; and for doc_ref the full path to PROPOSAL_SENT (held unit, quote, commercial model, sell price, buy limit).

Values sent:
- cards "4111 1111 1111 1111", "4111-1111-1111-1111", "5555.5555.5555.4444", "4111_1111_1111_1111", "3782 822463 10005"
- SSNs "123-45-6789", "SSN 123456789", "social security number: 123 45 6789"
- for doc_ref (whose schema is `^[A-Za-z0-9._:-]{1,100}$`): "4111-1111-1111-1111", "5555.5555.5555.4444", "4111_1111_1111_1111", "io-4111-1111-1111-1111", "123-45-6789", "SSN:123456789", plus "4111 1111 1111 1111" and "SSN 123456789"

Scan after each case (cumulative):
- every file in OOH_DATA_DIR (ooh_log.jsonl, ooh.lock, and pending.line/pending.discarded when present; none were present at any scan);
- the ledger-rust store dir (ledger.jsonl, ledger.jsonl.head, ledger.jsonl.lock);
- `GET /ledger/entries`;
- the service stdout log (extra).
Each scan looks for:
- the literal value;
- its digits as a substring of any digit run with separators " -._:‐" removed (for ≥9 digits);
- for vendor_text, sha256 of the unredacted text.
Population: 24 cases × 7 sources per Python.

**91ea9a1 (pii_c91_3.12, pii_c91_3.13), identical:**
- unit vendor_text, 8/8 -> **201**, `personal_data_redacted: true`. Text examples:
  - "faces west, contact [redacted] for access"
  - "… contact SSN [redacted] for access"
  - "… social security number: [redacted] for access"
  - The unit GET does not contain the value. **hits = [] in every source.**
- face location_description, 8/8 -> **422 ATTRIBUTE_VALUE_PERSONAL_DATA**, with ledger entries Δ = 0. **hits = [].**
- client_approval_doc_ref:
  - 6/6 schema-valid values -> **422 ATTRIBUTE_VALUE_PERSONAL_DATA** (field client_approval_doc_ref): ledger Δ = 0, state stays PROPOSAL_SENT with the same version, and the control approve with "io-acme-signed-1" then -> 200.
  - The 2 values with spaces -> 422 string_pattern_mismatch (the schema refuses them before the PII rule; still refused, nothing stored).
  - **hits = [].**
- At the end: ledger /verify valid (558 entries); the data dir is [ooh.lock, ooh_log.jsonl].

**Control at 99569bd (pii_c99_*), both Pythons:**
- vendor_text: 201, `personal_data_redacted: false`, the text stored as typed.
- face attribute: **200** (accepted, ledger Δ = 2).
- doc_ref: **200**, CLIENT_APPROVED.
- The scanner finds the literal and the digits **in data/ooh_log.jsonl** for every case, plus sha256(unredacted) for vendor_text. This is also the scanner's positive control.
- At 99569bd the digits did not reach the ledger store or /ledger/entries (only hashes are anchored); the exposure was the local log.

## Findings
**RT9-1 (MEDIUM, non-blocking for this scope; it is outside "integrity and writes while running"): the start-up read of the log still follows symlinks, blocks on a FIFO and reads a device without bound.**
- Code: 91ea9a1 store.py:118-121, RecordLog.__init__: `if os.path.exists(self.path): with open(self.path, "rb") as fh: raw = fh.read()`.
- Witnessed:
  - **rfifo** (c91_rfifo_X_3.12/3.13): restart with the FIFO in place. The new process stayed alive but **no /health within 15 s**, and its service.log has no output at all. RSS was 58–60 MB, so it is blocked before uvicorn binds. It ended only on SIGTERM.
  - **rzero** (c91_rzero_X_3.12): restart with the log a symlink to /dev/zero. The process reached **892,544 kB RSS within ~0.4 s of launch**, and my 512 MB watchdog SIGKILLed it (runs/watchdog2.log: "15:01:54 SIGKILL pid 66774 rss_kb 892544").
  - **rsym** (c91_rsym_X_*): restart with a symlink to identical bytes. The service starts "degraded", and integrity -> ok False, "cannot be read (OSError)". The write -> 503 INTEGRITY_UNVERIFIED and the target is unchanged. Fail-closed.
- Impact:
  - It needs write access to the 0700 data dir.
  - Nothing is written or served, so it is fail-closed for data. But start-up hangs (FIFO) or exhausts memory (/dev/zero) instead of refusing with a reason.
  - It is the same class as the r8 M-1 hardening, which now covers file_problem and the write path but not the start-up path.
- Reproducer: `SCEN=rfifo` (or `rzero`, with a watchdog) `MODE=X LEDGER_BIN=scratchpad/rt9-target/release/server TREE=runtime/src91 WORK=<new> venv3.12/bin/python -I runtime/scripts/sw9.py`
- Falsifier: an `os.open(path, O_RDONLY|O_NOFOLLOW|O_NONBLOCK)` + S_ISREG check at start-up would make rfifo and rzero exit promptly with a StoreCorrupt-style refusal.

**H-1: not reproduced at 91ea9a1** after the store.py change.
- e2, e3, m2 and m3 are refused with 503 and the file is unchanged; integrity-first reports not ok; the put-back rolls forward.
- 75b0662 still accepts them.

**r8 M-1 (FIFO/symlink/device on the running path): closed for the witnessed cases.**
- Prompt answers (≤0.20 s), bounded RSS, refusals, and a clean roll-forward after the put-back.
- 99569bd hangs on the FIFO, holding the lock, and ignores SIGTERM.

**OOH2-PII-1: closed for the witnessed fields and values.** Nothing reached the log, the pending files, the ledger store or the ledger entries. 99569bd stores them.

**O8-1 (LOW, omega8: the /audit/integrity FORCED_MIN_S throttle): not re-run.** The store.py docstring at 91ea9a1 (lines 14-17) now states the throttle explicitly.

## Leftovers / external
- No live or production endpoint was read or written. Only 127.0.0.1 was used.
- No candidate edit, commit or push.
- `ps -axo pid,command | grep -E "omega9/runtime|rt9-target|watchdog9"` -> no process left.
- `find omega9/runtime -name target -type d` -> none.
- I removed scripts/__pycache__, which my driver imports had created.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN / PRODUCTION_PROVEN: not claimed. No deployed SHA was read, and no live provider was involved.
- pytest, including test_aegis_h1_edges.py and test_ooh2_pii1_card_ssn.py: AEGIS-TEST's job.
- c99_symzero_H (the old unbounded read while running): not run, for host-memory safety. Inferred by reading only.
- rzero on 3.13, and the rfifo/rzero/rsym controls at 99569bd: not run.
- Not run:
  - a symlink to a large regular file, or to another device;
  - a FIFO with a live writer;
  - a TOCTOU race between fstat and pread, or between the compare and pwrite;
  - concurrent writers;
  - the cost of a large log;
  - Linux/CI.
- PII: only these 3 fields and the listed values.
  - Not run: other free-text fields, other card networks or lengths, Unicode-dash SSNs over HTTP, false-positive controls over HTTP, and the /audit/evidence "attempted" view.
  - Not run: the ledger's own stdout log (ledger.log), which holds no request bodies by design (not verified).
- The other five services that share the store.py code (OOH2-RT-H1-<SERVICE>) were not exercised.
