VERDICT: RELIABILITY_GREEN at 4a0a4b478a88fbaaeb864a7535779e586b39f64a. No HIGH or CRITICAL finding; blocking: false.
Binding: DarksiedCEO/zbm-zbc · ooh-dept-15-wave2 · 4a0a4b478a88fbaaeb864a7535779e586b39f64a · tree e0640db37e4a40f58e0a291fd21d90a56f45d1ec. src, ledger-rust and devtools deltas from 5116917 empty (verified by the gate).
Author: AG-RELIABILITY, independent reviewer.

(Transcribed by the builder session from the gate's hand-back message; the gate's role contract forbids report files. Its own run logs, each headed with SHA and interpreter, are in omega5/reliability/runs/ (30 files).)

Ledger: own build (SDKROOT MacOSX26.5), sha256 9a706f86bc10cc46ae269431aa5bdcf44529adf7d27f66caa013ce6443aa3ae2.

Item 2, full suite: 3.12.14 767 passed (91 s); 3.13.9 767 passed (81 s).
Item 3 (3.12 unless noted): F1 witness passes (log lines = anchors = 16); faults 64, faults2 160, faults3 48, concurrency 13 passed. SIGKILL seeds 4-11 with alternate ledger kills: all RESULT PASS (acked 1009/1434/1027/773/1063/1231/1616/2121); dups [], acked_missing [], unrecoverable [], pending_files [], ledger_valid true, log length = anchors = acked every seed. War room seeds 1, 7, 13: exit 0, 156/156. d1b0de9 control: faults2 8 failed / 152 passed (rl-un F1 class); orphan_anchor reproduces F1 at d1b0de9 — detection power re-shown.
Item 4, new test file: 50 collected; 30/30 sequential and 30/30 shuffled runs pass; 4/4 runs under 28 CPU burners pass. Timing: 5 tests use time.monotonic offsets; can flake only under a >=10 s process stall (LOW). Leak: env fixture never closes h.svc, so 1 ooh.lock fd per test (50 per run; whole suite ends at 74 fds) (LOW, test hygiene).
Item 5, F2 re-measured: slow-drip write 503 after 88.1 s, /health stalled 87.6 s, clean recovery (unchanged; MEDIUM, non-blocking). Hang: 10.0 s / 9.5 s. HTTP 500: immediate 503.
Item 6: no leftover processes.

Findings:
1. MEDIUM F2 (carried): slow-drip ledger holds the service lock ~88 s; ledger.py HTTP client has no total-time deadline.
2. LOW (new): env fixture in tests/test_aegis_integrity_path.py leaks one ooh.lock fd per test; fix = yield + h.svc.close().
3. LOW (new): 5 timing-offset assertions theoretically flaky under a >=10 s stall; fix = inject clock.
4. LOW (carried): F3 ReDoS in funds_ref_parts (unreachable via API; not re-measured), 15 s retry wait.

NOT RUN: Linux/CI; new stress beyond poc/concurrency.py and SIGKILL streams; F3 fuzz, replaycheck, live_run, mutation; SIGKILL seeds outside 4-11; service-only kill seeds; war room beyond 1/7/13; disk-full, fd exhaustion, multi-process contention; harnesses on 3.13.
