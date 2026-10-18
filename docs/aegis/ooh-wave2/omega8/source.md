# AEGIS SOURCE gate (Gate 1), scoped re-run
candidateSha: 99569bd85a791c102c6df293d7e987743813644b
treeOid: 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae
Author: written by the AEGIS-SOURCE reviewer agent (fresh context, read-only; independent of the builder).
Scope: changed files only (services/ooh-py/src/store.py, src/service.py, tests/test_aegis_h1_file_content.py) plus the ADR 0022 H-1 section, OPEN.md rows OOH2-RT-H1 / OOH2-MUT-DEAD, docs/test-counts.md.

## Verdict: SOURCE_GREEN (no CRITICAL/HIGH; 3 LOW, 1 INFO; none blocking)

## Checks
- Worktree HEAD and tree match at start and end; `git status --porcelain` empty at start and end.
- `git diff --stat 75b0662 99569bd -- services`: 3 files (service.py +2, store.py 69, new test 180), all under services/ooh-py/ (non-ooh-py path count = 0).
- ooh-py suite, frozen worktree: Python 3.12 = 805 passed, 1 warning; Python 3.13 = 805 passed, 1 warning (raw: source/pytest-3.12.txt, source/pytest-3.13.txt). 805 = 792 + 13 new tests; matches docs/test-counts.md.
- `RecordLog.verify()` removal: `git grep` in services/ooh-py at 99569bd finds no `log.verify` / `.verify()` on a RecordLog (remaining `verify` hits are ledger client, founder gate, verify_lines, verify_integrity). The other services (bizdev, clipper, compliance, creative, ...) each import their own copy of store.py, not ooh-py's; none reference ooh-py. No caller broken. Raw: source/grep-callers.txt.
- Areas (denominator): areasRequired 3 source/test files + 3 doc claims, areasInspected 3 + 3, all read in full.

## Areas
- services/ooh-py/src/store.py: REAL (read in full; append_prepared, _pread_exact, file_problem, _memory_bytes all implemented, no stub/placeholder/TODO).
- services/ooh-py/src/service.py: REAL (read in full; file_problem wired at line 335-336 in verify_integrity; log.fault override after it at 337-338 still wins).
- services/ooh-py/tests/test_aegis_h1_file_content.py: REAL (13 tests; each asserts file bytes, status, and integrity; not mock-only: real files, real service Harness; only `_fake_os` pread shim in the short-read test).

## Bypass hunt (what I tried to break, result)
- Size gate: `not (adopt or size == expected)` refuses any other size; then full `_pread_exact(fd, expected, 0) != mem` runs for BOTH write and adopt. A same-size edit anywhere in the first `expected` bytes is refused (tests: first/middle/last).
- Adopt branch: needs size == expected+len(line)+1, prefix == mem, tail == line+"\n". Same-size other tail and changed earlier line both refused (tests). Not bypassable.
- Short reads: `_pread_exact` loops until n bytes or EOF; a file that shrank mid-read returns fewer bytes and mismatches (refused). One-byte pread test passes.
- Deleted file: size 0 != expected (non-empty memory) -> refused; file_problem returns DIFFERS. FileNotFoundError->b"" is only equal to memory when memory is empty (correct).
- Unreadable/other OSError: file_problem returns a reason (fail closed); append path wraps OSError to StoreWriteError.
- Fault latch and closed instance precede the file check; fail_next_append unchanged.
- In-memory log (no path): no file, file_problem None, by design.
- No fake-success path found: refusal raises StoreWriteError before `self._lines.append`; _commit turns it into 503 maybe:true and sets integrity not ok; recovery after restoring the file is exercised by test_h1_putting_the_file_back_rolls_the_refused_write_forward.

## Findings
(All LOW/INFO; no reproducer needs a fix to clear the verdict. I propose no fix.)

### F1 LOW DOC_DRIFT: store.py header claims "unchanged in behaviour"
- store.py:2-4 says the file is "Copied from services/seo-py/src/store.py, unchanged in behaviour". Since 99569bd the behaviour differs from seo-py (whole-file compare, file_problem, no verify()). ADR 0022 and OPEN.md state the divergence correctly; the module docstring does not.
- Reproducer: `git diff --no-index <(git show 99569bd:services/seo-py/src/store.py) <(git show 99569bd:services/ooh-py/src/store.py)`; or read store.py:2-4 against :176-249.
- Falsifier: seo-py's store.py at 99569bd also compares the full file and has file_problem (it does not, per OPEN.md rows OOH2-RT-H1-SEO).

### F2 LOW DOC_DRIFT: "file untouched" is not true for a deleted log file
- store.py:177-178 and ADR section ("leaves the file untouched") and test docstring line 4 say a refused write leaves the file untouched. `os.open(..., O_CREAT)` at store.py:195 re-creates an empty ooh_log.jsonl when it was deleted and memory is non-empty. The test itself records it (test_aegis_h1_file_content.py:148 asserts `_disk(h) == b""`, "nothing written into the re-created file"). No data is written and the write is refused, so no integrity effect; the claim is slightly overbroad. Also a deleted-then-recreated empty file would let the operator-visible state change (file now exists, still DIFFERS).
- Reproducer: run test_h1_deleted_log_file_is_reported_and_refused; observe the file exists (size 0) after the refused append.
- Falsifier: the refusal path leaves no file when the log was deleted (the test line 148 would then raise FileNotFoundError).

### F3 LOW: cached integrity means reads show ok between checks
- service.py:314-315: when integrity is ok and not forced, verify_integrity returns the cache without calling file_problem. A file edit is therefore reported only at the next forced check (integrity job/audit route, rate-limited by FORCED_MIN_S=10 at :316-317) or refused at the next write (append_prepared, which re-reads regardless). Writes cannot slip through; GET /health may say ok until a check. ADR/test wording "every integrity check also compares" is accurate; "a changed file stops writes even before one" (test docstring line 5) is accurate only after a check runs. No scheduled caller of the job was found in ooh-py/src (the job is an external route).
- Reproducer: edit a line same-size, call GET health: integrity.ok stays True; then POST audit/integrity: problem DIFFERS.
- Falsifier: health or _gate calling file_problem on every call (it does not; service.py:314).

### F4 INFO: cost and check-then-write window (documented / inherent)
- Whole log read and joined (`_memory_bytes`, O(n) memory) before every write and at every forced check, under the service lock; documented in the docstring and ADR ("grows with the log"). A writer outside the flock (advisory) can still change the file between the compare and the pwrite; the next write/check detects it. Not a regression against the stated claim.

## Docs vs code
- ADR 0022 H-1 section: each bullet verified against code and a test (byte compare before write; adopt needs whole file; file_problem at integrity check; pread completion; cost note; verify() replaced and had no caller; 13-test list matches). Cross-service statement "same check in seo, sales, bizdev, service, influencer" is a claim about other services; I did not read them (Not run).
- OPEN.md OOH2-RT-H1 and OOH2-MUT-DEAD rows: accurate; both honestly kept open "until AEGIS confirms". test-counts.md 805: matches the run.
- Minor: store.py:13 is a very long line (style only).

## Not run
- No mutation testing of the new code (a mutant run on the new branches, e.g. dropping the prefix compare or the adopt tail compare, was not done; tests exist for each, and I did not prove they kill every mutant).
- No live/ledger-rust run, no HTTP-level end-to-end beyond the pytest Harness.
- Other services' store.py (seo, sales, bizdev, service, influencer) not read; their H-1 status is unverified here.
- No concurrency/TOCTOU stress, no large-log performance measurement, no non-macOS-vs-Linux difference check (ran on this macOS host only).
- Security, reliability, data-integrity, provenance and test-accounting gates not in my scope.
