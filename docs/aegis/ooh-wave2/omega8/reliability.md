# AEGIS RELIABILITY gate: ooh-py H-1 fix re-review
candidateSha: 99569bd85a791c102c6df293d7e987743813644b
treeOid: 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae
author: AG-RELIABILITY (Claude Sonnet 5.5), fresh context, read-only on the candidate. Experiments ran in a `git archive` copy (scratchpad/rel-copy; 75b0662 baseline in scratchpad/rel-old).
Worktree check: HEAD and tree matched the above at start and at end; `git status --porcelain` was empty both times.
Scope: `git diff 75b0662 99569bd -- services` = 3 files (store.py, service.py, tests/test_aegis_h1_file_content.py). Raw outputs: omega8/reliability/.

## VERDICT: RELIABILITY_GREEN (no blocking finding; 1 LOW, 1 INFO recorded)

## 1. Suites (Python 3.12.14 and 3.13.9)
- Whole ooh-py suite: 805 passed, 1 warning (deprecation, httpx/starlette), 0 failed, on 3.12 (117 s) and 3.13 (121 s). Files: reliability/full-3.12.txt, full-3.13.txt.
- tests/test_aegis_h1_file_content.py (13 tests): 30 sequential runs per version = 30/30 "13 passed" on 3.12 and on 3.13. 30 shuffled-order runs per version (pytest-randomly and random_order are NOT installed, so I shuffled the test-id list per run with seeds 1..30 and passed it on the command line): 30/30 "13 passed" on both. 0 flakes in 120 runs. Files: seq30-*.txt, shuf30-*.txt, ids-*.txt.

## 2. Failure handling (my own probe, 10 tests, reliability/test_rel_probe.py: 10 passed on 3.12 and 3.13; plus real SIGKILL script)
| Surface | Method | Observed |
|---|---|---|
| short pread | suite test (1-byte preads) re-read; `_pread_exact` loops until n or EOF (store.py:56-66) | write completes, file equals memory |
| EIO on pread (append) | pread patched to raise OSError(EIO), 300 calls | each raises StoreWriteError, file bytes unchanged, memory unchanged, no fault latched, fd count identical before/after; next write succeeds once reads work |
| EINTR | CPython retries EINTR inside os.pread (PEP 475). If an InterruptedError were surfaced anyway it is an OSError and is mapped to StoreWriteError | fail closed, file untouched, retry succeeds |
| EIO on open() in file_problem | open patched, 300 calls | returns "cannot be read (OSError)", fd count unchanged (with-block) |
| file shrinks between fstat and pread | ftruncate inside first pread | short read gives mismatch, refused "disagree", memory 3 lines, file_problem reports it |
| file grows between compare and pwrite | 4000 foreign bytes appended after the compare | the write goes in (pwrite at offset overwrites the first bytes), foreign tail remains; the NEXT write is refused and file_problem reports it. Residual TOCTOU window of one pread-to-pwrite gap, inherently unclosable without a lock on the file; detected within one step. See F-R1 (INFO). |
| FileNotFoundError / deleted file | suite test + my test | append: O_CREAT makes an empty file, size != expected so refused, nothing written, 503. file_problem: reports "differs". Operator puts file back, two integrity checks: anchored line rolls forward, log count = anchors count, integrity ok. |
| crash after compare, line fully on disk, pending+anchor present | service level: restart, and running-service check | restart: line adopted into memory, anchors == lines, next write ok. Running service: integrity check settles, ok. |
| crash with torn tail | real SIGKILL inside pwrite after half the bytes (3.12, 3.13) | restart raises StoreCorrupt "torn line; refusing to start" (fail closed, unchanged from 75b0662, needs operator). SIGKILL after full pwrite: restart sees 4 lines, file_problem None. Files: sigkill.txt |
| crash after compare before write | pending+anchor without line (existing test test_ip_disk_pending_rolls_forward_across_a_restart, passes) | converges |
| concurrency | 4 writer threads x 8 tenant creates through the HTTP harness plus 3 threads polling file_problem, then 3 integrity checks | no exception, file_problem None, integrity ok. Note: writes are serialised by log.lock, so this exercises the lock, not parallel preads of the same writer. |
| fd leaks | fd count via /dev/fd across 300 failing appends and 300 failing file_problem calls | unchanged. os.open is closed in `finally` (store.py:~245); file_problem uses `with open`. |

Code reasoning: every OSError inside the try (fstat, pread, pwrite, fsync) is mapped to StoreWriteError; the compare happens before any mutation, so a refusal leaves the file untouched (asserted in tests). `service._commit` handles StoreWriteError from append_prepared by setting integrity not ok and "maybe", and the next check rolls forward (unchanged logic, exercised above).
Note: the old `RecordLog.verify()` (chain re-verification of the file) was removed. File == memory byte for byte, and memory was chain-verified at load, so equivalence holds; no remaining caller in src (grep).

## 3. Cost (Python 3.13.9, macOS APFS, ~620-byte lines, median of 15 writes / 10 checks; reliability/bench.txt, mem.txt)
| lines (bytes) | write 75b0662 | write 99569bd | check 75b0662 (old `verify()`, not on the integrity path) | check 99569bd (`file_problem`) |
|---|---|---|---|---|
| 100 (71 KB) | 0.10 ms | 0.10 ms | 1.19 ms | 0.03 ms |
| 1 000 (0.7 MB) | 0.11 | 0.20 | 10.1 | 0.15 |
| 10 000 (7.2 MB) | 0.51 | 1.85 | 107 | 1.65 |
| 50 000 (36 MB) | 2.57 | 10.2 (max 16.7) | 535 | 9.9 |
Write cost is O(log size): about +7.6 ms and +1.5x of a tiny write at 50 000 lines. macOS fsync is cheap, so real-disk fsync (Linux ms scale) adds to both versions. Transient memory per write at 50 000 lines: +76.9 MB (memory copy plus file read) versus +0.45 MB before; freed after the write, serialised by the lock, so not cumulative. The integrity check on the shipped path gets cheaper than the old verify() would have been (it does not re-hash).
The compare runs under the log lock (and service lock via _commit); 10 ms at 50 000 lines is far below any request/ledger timeout (the ledger round trip per commit is several network calls, and serve.py timeouts are seconds). It does not threaten locks or timeouts at the sizes measured. Extrapolation (linear, not measured): 1 M lines is about 0.7 GB read and about 1.5 GB transient memory per write, about 200 ms or more. See F-R2.

## Findings
- F-R2, LOW, class RESOURCE_EXHAUSTION (scaling note, non-blocking). Claim: per-write time and transient memory grow linearly with log size (store.py `_memory_bytes`, `_pread_exact(fd, expected, 0)`, store.py append_prepared). Repro: reliability/bench.py and mem.py, numbers above. Falsifier: bench showing flat latency, or a log-size cap. Mitigation is a founder-ruled design cost, already stated in the docstring ("the cost grows with the log"). Suggest a documented log size/lines ceiling or a rolling digest check in a later wave. Not blocking: no line count in the measured range approaches a timeout.
- F-R1, INFO, class RACE. Claim: a foreign appender acting in the gap between the compare and the pwrite is overwritten at the offset and leaves a tail; detected at the next write/check, not prevented (store.py pread then pwrite, same pre-existing gap as the size check). Repro: test_grow_between_fstat_and_pwrite_detected_after in reliability/test_rel_probe.py. Falsifier: test fails if the next write is accepted after it. Requires a hostile writer with filesystem access and exact timing; the threat is in scope of the H-1 ruling only for edits seen before a write.
- No new CRASH_UNRECOVERED, CLEANUP_SKIPPED, FAIL_OPEN, or fd leak found.

## 4. Carried F2 (MEDIUM, slow-drip ledger ~88 s)
NOT re-run. The delta touches no ledger client, recorder, timeout or config code (services diff = store.py, service.py +2 lines, one test file), and the +2 lines run after the ledger entries are fetched. Treated as carried, unchanged by reasoning from the diff, not by observation.

## NOT RUN
- F2 slow-drip reproduction (above).
- Real ledger-rust binary (not needed; the fake ledger harness was used).
- pytest-randomly / random_order (not installed; id-list shuffle substituted).
- Linux and real-disk fsync costs; logs beyond 50 000 lines; multi-process contention on one data dir (claim lock is outside this delta).
- Real SIGKILL of the full HTTP service (done at RecordLog level plus service-level state reconstruction; not a live-kill of uvicorn).
- Other services (F1 siblings) out of scope.
