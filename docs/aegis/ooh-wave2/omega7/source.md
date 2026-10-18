# AEGIS SOURCE gate at 75b0662 (transcribed)

> Transcription note: the SOURCE gate's role contract forbids report files. This is a builder transcription of the
> gate's final hand-back. Verdict, counts and findings are copied exactly; some prose is shortened. The gate's pytest
> outputs (headed with SHA, tree and interpreter) are in source/.

candidateSha: 75b0662823b867df3325d676b1dcf69abe37a425
treeOid: 67c15180382fab1a391b78756467c1a79a934973 (verified with git rev-parse in the frozen worktree; git status clean before and after)
repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2, service services/ooh-py, diff base 84a5377
Author: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), read-only, fresh context.

VERDICT: SOURCE_GREEN (advisory to Ω). No CRITICAL or HIGH finding. blocking=false.

## 1. Suites
`pytest -q -p no:cacheprovider` in services/ooh-py: Python 3.12, 792 passed, 1 warning (Starlette httpx deprecation), 83.35 s. Python 3.13, 792 passed, 1 warning, 86.50 s.

## 2. Delta truthfulness
The delta is 4 files, all tests or docs; no src change since ce665c5. All 4 read in full, with store.py append_prepared (159-211), _write_file (314-336), DataDirLock (349-447) and service.py close, _settle_pending, _roll_forward.
- 792 is true: the new file has exactly 15 tests, and 777 + 15 = 792. docs/test-counts.md is correct.
- The proxy injection is sound. `_OS` delegates every attribute to the real os; only named functions are overridden, and only `store.os` is rebound. `_on_log_fd` applies the fault only to descriptors opened on the log path.
- The call-order spy (T166/T279/T283) passes calls through to the real functions, so the commit really completes (201). It shows: fsync of pending.tmp before replace(pending); fsync of the data directory after the replace; fsync of the log after its pwrite. It does not prove the fsync precedes the response, but the 201 is returned only after append_prepared returns. A minor limit, not a defect.
- Each test exercises the path its name says and asserts observable behaviour; none is a fake success. T144 and T172 give 503 with disk and memory unchanged. T149/153, T151/154, T148 and the T157/159 cases are refused with the file bytes unchanged. The adoption test shows the line is not written twice. T163/165/167 cut the disk back with no fault set. T171 gives 503 maybe:true with nothing changed. T169 latches the fault and the next append raises LOCAL_LOG_WRITE_FAULT (store.py:171-172, 201-203). T276/278 and T286 give 503 maybe:false with disk, memory and anchor count unchanged and no pending or tmp file left. The tests use the real service, real files and the existing scripted ledger double; no mock enters the runtime.
- The fixture comment is now true. `release_claim` never closes `_fd`; only `release()` (store.py:441-444) does, and the service's `close()` calls only `release_claim` (service.py:129). The ce665c5 comment and fix were wrong; the new `data_dir_lock.release()` call is right and cannot affect other tests (each uses its own tmp data dir).
- The ADR statements match the code. Not executed: the new sentence that the next restart fails with `KeyError 'tenant_id'`. It is plausible: `_start` applies log records, and only `_roll_forward` catches KeyError around `_apply` (service.py:436-440).

## 3. Full-diff re-check (grep and targeted reads; the 8765-line patch was not read line by line)
- No TODO, FIXME, NotImplemented or placeholder in src (the only "stub" hit is a launch_guard.py docstring).
- Silent catch / bare pass sites are the same set as the prior gate, none new: graceful_close.py:74, service.py:402 (M1 region, line retained, the safe direction), svc_inventory.py:407 (followed by raise INVENTORY_PORT_NOT_CONNECTED), store.py:259, :293, :334, service.py:303.
- No invented vendor, rate, inventory, measurement or market data. Rate estimates come from a sourced rate card, else RATE_CARD_UNKNOWN. The $10,000 and 7-day ceilings are founder-locked (ADR 0022). Defaults fail closed.

## 4. Carried findings (src unchanged; F1-F5 not re-derived)
- F1-F5 carried from omega5/source.md.
- The service.py:452 LOW (redundant subject clause), non-blocking.
- M1 MEDIUM, open: service.py:398-404 discards `problem` on the set-aside path while `_roll_forward` returns "...an operator must inspect the log", True (line 439). No test shows ok=False after such a line. ADR 0022 discloses it and now names the restart failure. Not blocking: it needs data-directory write access plus a ledger-valid anchor for a malformed line, which this service never writes.

## Not run
No mutation testing (the 21 mutants were judged by reading, not run). The ADR's restart KeyError claim not executed. No other services' suites, no live-ledger, CI or macOS runs. No real-filesystem fault injection (ENOSPC, EIO). The full patch was not read line by line.
Denominator: 4/4 delta files read in full; 15/15 new tests judged; 2/2 interpreters; src sweep over 20/20 files.
