# AEGIS RELIABILITY gate at 75b0662 (transcribed)

> Transcription note: the RELIABILITY gate's role contract forbids report files. This is a builder transcription of
> the gate's final hand-back. Verdict, counts and findings are copied exactly; some prose is shortened. Run logs:
> reliability/runs/ (each headed with SHA and interpreter); harnesses and plugins: reliability/ and reliability/poc/.

VERDICT: RELIABILITY_GREEN at 75b0662823b867df3325d676b1dcf69abe37a425. No HIGH or CRITICAL. blocking: false.
Binding: DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2, 75b0662823b867df3325d676b1dcf69abe37a425, tree 67c15180382fab1a391b78756467c1a79a934973, services/ooh-py, base 84a5377. Frozen worktree clean; runs from the gate's own `git archive` copy (control from `git archive d1b0de9`).
Author: AG-RELIABILITY, independent reviewer (read-only; no commit, push or edit).

## Delta
`git diff ce665c5 HEAD`: docs/adr/0022, docs/test-counts.md, tests/test_aegis_integrity_path.py (+4/-2), tests/test_aegis_store_write_path.py (new, +280). src, ledger-rust and devtools empty. Own ledger build sha256 e65be4cc45e20dd142543ea6501acf2116ff1f6cbbf5d2817efa6d168b62b696 (source identical to ce665c5; embedded build path assumed, not checked).

## Results
1. Full suite: 3.12.14 792 passed (85.1 s); 3.13.9 792 passed (80.9 s). Shuffles: 3.12 seeds 1-4, 792 each; 3.13 seed 9, 792. (Seeds 3-4 ~160 s on a busy host, load 17-20; nothing failed.)
2. Re-runs (3.12): orphan_anchor 201 after restart, log 16 = anchors 16. faults 64, faults2 160, faults3 48, concurrency 13 passed. d1b0de9 control: faults2 8 failed / 152 passed (rl-un F1 class); orphan_anchor 503 INTEGRITY_UNVERIFIED, log 13 vs anchors 14. SIGKILL seeds 4-11 against the real ledger: all PASS; acked 4=828, 5=1474, 6=1376, 7=1145, 8=1357, 9=1576, 10=1745, 11=1268; every seed dups [], acked_missing [], unrecoverable [], ledger_valid true. War room seeds 1, 7, 13: Gate PASS, 156/156, 156/156, 170/170.
3. New and changed tests (75): 3.12 30/30 sequential, 30/30 shuffled; 3.13 10/10 and 10/10; under load ~20, 4 sequential and 4 shuffled passed (a first load-shuffle attempt ran no tests because of a zsh word-splitting error in the gate's own script; discarded and re-run as run_load2.sh).
4. Isolation: the `store.os` proxy is restored before env teardown (including test_t169's mid-test reset). A per-test plugin over the full suite (poc/leakplug.py, leakcheck.log): `store.os is os` held, no os function replaced, 0 violations, only MainThread alive at the end. release(): `Harness.restart()` reuses the cached DataDirLock from `config._HELD`, so teardown releases the lock the restarted harness also used, only after the test body. Released locks stay in `_HELD` with `_fd` None (74 of 95 at suite end); harmless today because every env data dir is a unique tmp_path.
5. fd leak: fdclose.py: close() alone growth 5, with release() 0. Per file: integrity_path 1 (was 50), omega5_mutation 0 (was 10), store_write_path 0. Full suite 21 (was 84), from tests that do not use env (test_aegis_commit_path 8, omega_red 2, core 2, one each in ooh_r1, ooh_r2, contracts, finance_verification, hold_confirmation, locked_decisions, planning, registry, and test_ip_first_line_of_an_empty_log_rolls_forward_under_its_own_epoch).
6. F2 (hang.py, 3.12): slow-drip 503 after 88.2 s, /health stalled 87.7 s, clean recovery. Hang 503 after 10.0 s (/health 9.5 s). HTTP 500 immediate. Unchanged.
7. Leftover processes: none of the gate's own.

## Findings (none blocking)
1. LOW, prior fd leak partly resolved: 21 fds remain from tests that bypass env; close() still does not close the fd (src/store.py:430-444). The gate would not carry it forward.
2. LOW, new, latent: `_HELD` keeps a released lock, so a future test reusing a data-dir realpath after env teardown would get an unprotected lock. No such test exists.
3. MEDIUM, carried: F2 slow-drip stall (~88 s); no total deadline on the HTTP client.
4. LOW, carried: 15 s retry wait.
5. LOW, carried: F3 ReDoS in funds_ref_parts (not re-measured).
6. LOW, carried: monotonic timing offsets; no flake under load ~20.

## Not run
Linux/CI; SIGKILL service-only mode and other seeds; other war room seeds; poc harnesses on 3.13; F3 fuzz, replaycheck, live_run, mutation; full suite under controlled heavy load; disk-full, fd exhaustion, multi-process contention; ADR/test-counts text beyond 792; xdist/thread isolation.
