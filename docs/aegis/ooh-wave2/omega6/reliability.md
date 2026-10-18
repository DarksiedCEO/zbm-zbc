# AEGIS RELIABILITY gate at ce665c5 (transcribed)

> Transcription note: the RELIABILITY gate's role contract forbids report files. This is the gate's final
> hand-back, copied by the builder without changing its findings. Run logs: reliability/runs/ (38 files, each headed
> with SHA and interpreter). Harnesses: reliability/poc/, run_*.sh.

VERDICT: RELIABILITY_GREEN at ce665c5b3aef1bcd56aacee9523559150dd8cb7b. No HIGH or CRITICAL. blocking: false.
Binding: DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2, ce665c5b3aef1bcd56aacee9523559150dd8cb7b, tree 5530ba08de8a78fad4c004db4fedeb8c40b45a4a, services/ooh-py, base 84a5377. Frozen worktree verified and clean; runs from the gate's own `git archive` copy at reliability/c.
Author: AG-RELIABILITY, independent reviewer (read-only; no commit, push or edit).

## Delta check
- `git diff 4a0a4b4..ce665c5`: 4 files (docs/adr/0022, docs/test-counts.md, tests/test_aegis_integrity_path.py +4/-2, tests/test_aegis_omega5_mutation.py new 152 lines). services/ooh-py/src, services/ledger-rust and devtools: empty.
- Own ledger build (cargo build --release, SDKROOT MacOSX26.5): sha256 f3da9d7b6df46d48b52957243759c0a4d41e35496f6743b051c967d056598b36. It differs from the omega5 binary (9a706f86...) with identical source; the gate assumes an embedded build path, not checked.

## Results
1. Full suite: 777 passed on 3.12.14 (113.9 s) and 3.13.9 (99.9 s). Three whole-file shuffles on 3.12 (seeds 1-3): 777 each.
2. Re-runs at ce665c5 (3.12):
   - F1 witness (orphan_anchor): after restart the write gets 201; log lines 16, ledger anchors 16.
   - faults 64 passed; faults2 160 passed; faults3 48 passed; concurrency 13 passed.
   - d1b0de9 control for faults2: 8 failed, 152 passed (the rl-un F1 class, e.g. submit-anc-rl-un-True). orphan_anchor at d1b0de9: 503 INTEGRITY_UNVERIFIED, log 13 vs anchors 14. The harness detects the defect; ce665c5 does not have it.
   - SIGKILL seeds 4-11 against the real ledger, alternating ledger kills (seeds 4-7: 16 cycles; 8-11: 20). All RESULT PASS. Acked writes: 4=1532, 5=988, 6=1019, 7=1073, 8=2168, 9=1529, 10=1012, 11=2128. Every seed: dups [], acked_missing [], unrecoverable [], ledger_valid true.
   - War room seeds 1, 7, 13: exit 0, Gate PASS; ooh-py 156/156, 156/156, 170/170.
3. New and changed tests (60 tests, 3.12): 30/30 sequential; 30/30 shuffled (seeds 1-30); under 28 busy-loop burners (load ~9), 4 sequential and 2 shuffled passed (14-17 s vs 4.6 s unloaded).
   - test_h198: the patch is scoped to the service module; service.py uses `time.` only at line 313; restored at teardown (identity checked). No side effect found.
   - test_t222: `ST.os.unlink` is the global os module, so the patch is process-wide. The first test filters on the PENDING_NAME suffix; the second raises on every unlink while it runs. monkeypatch is undone before env teardown; `os.unlink is original` verified afterwards. No side effect found. xdist or threaded tests would need care; none are used.
4. F2 re-measured (hang.py, 3.12): slow-drip write 503 after 88.2 s; concurrent /health stalls 87.7 s; clean recovery (201, integrity True). Hang mode: 503 after 10.0 s, /health 9.5 s. HTTP 500: immediate 503, /health 0.0 s. Unchanged from 4a0a4b4.
5. Leftover processes: none of the gate's own.

## Findings
1. LOW (test hygiene, non-blocking). The env-fixture change does NOT remove the fd leak, and its comment is wrong.
   - Observed: fd growth still 50 for test_aegis_integrity_path.py (identical to 4a0a4b4); test_aegis_omega5_mutation.py adds 10 (it imports env). Full suite ends at 84 fds from 4 (74 at 4a0a4b4). lsof shows 50 `ooh.lock` entries after the integrity file alone.
   - Cause (src/store.py:430-437): `DataDirLock.release_claim()` only clears the claim flag; the fd closes only in `DataDirLock.release()` (store.py:441-444), which `svc.close()` never calls.
   - Repro: reliability/poc/fdclose.py (runs/fdclose.log): 5 Harness + `svc.close()` gives growth 5; after `h.settings.data_dir_lock.release()` growth 0.
   - Impact: bounded, test-only (~84 fds vs a 1,048,576 limit; the macOS default 256 would be reached at ~3x the current test count). Production unaffected.
   - Fix: call `h.settings.data_dir_lock.release()` after `svc.close()`, or correct the comment.
2. MEDIUM (carried, F2, non-blocking): a slow-drip ledger holds the service lock ~88 s and /health stalls with it; the HTTP ledger client has no total deadline. The hang case is bounded at 10 s.
3. LOW (carried): the 15 s retry wait.
4. LOW (carried): F3 ReDoS in funds_ref_parts (not re-measured).
5. LOW (carried): monotonic timing offsets; flake only under a stall of 10 s or more; no flake under load.

## Not run
Linux/CI; SIGKILL service-only mode and seeds outside 4-11; war room seeds other than 1, 7, 13; F3 fuzz, replaycheck, live_run, mutation; disk-full, fd exhaustion, multi-process contention; poc harnesses on 3.13 (only the full suite ran there); the new tests as repeated loops on 3.13; a full suite under heavy load; ADR/test-counts text beyond the 777 count.
