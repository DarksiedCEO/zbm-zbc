# AEGIS TEST-VERIFICATION (Gate 4a), omega8

candidateSha: 99569bd85a791c102c6df293d7e987743813644b
treeOid: 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae
repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2 (local)
Author line: AG-TEST-VER (AEGIS test-verification, read-only, fresh context); candidate commit author: Andre <<OWNER-EMAIL>>
Worktree HEAD/tree/porcelain verified identical at start and end (porcelain empty).
Raw outputs: /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega8/test-verification/

## Verdict: BLOCK (reason: NO_DISCRIMINATION_PROOF only)
The test-accounting dimension is clean. TEST_TRUTH_GREEN cannot be reported because no mutation-fuzz discrimination proof
at this SHA was supplied to me (omega8/mutation/ holds generators and raw dirs only, no finished kill/survivor report).
Per contract: absent proof = BLOCK. Not a test defect. Re-check once mutation-fuzz delivers {injected, killed, survivors} at 99569bd.

## Accounting (whole services/ooh-py suite, no -x, pytest -rA, -p no:cacheprovider, 2 runs per interpreter)
required (collected) 805 | executed 805 | passed 805 | failed 0 | skipped 0 | xfail 0 | xpass 0 | errors 0 | cancelled/neutral/stale 0
- 3.12 run1: "805 passed, 1 warning in 116.24s (0:01:56)"; run2: "805 passed, 1 warning in 123.77s (0:02:03)"
- 3.13 run1: "805 passed, 1 warning in 110.41s (0:01:50)"; run2: "805 passed, 1 warning in 122.39s (0:02:02)"
- collect-only: "805 tests collected" on both. PASSED lines counted per run file: 805 each; 0 SKIPPED/FAILED/ERROR/XFAIL lines.
- The only warning: StarletteDeprecationWarning (httpx vs starlette.testclient), unrelated.
- Determinism: 2 stable reruns per interpreter, identical counts: stable = true.
- In-scope tests: omega5_mutation 10 defs, store_write_path 15, h1_file_content 11 + 2 extra parametrize cases = 38 PASSED lines, none skipped
  (test_h1_unreadable_log_file_is_reported has a root-only skip; it ran as non-root and PASSED).
- Files: test-verification/run-3.12-r{1,2}.txt, run-3.13-r{1,2}.txt, collect-*.txt.

## Falsifiability of the H-1 file (old store.py + service.py from 75b0662 in a git-archive copy)
Copy: omega8/falsify-old. Result: 11 failed, 2 passed (raw: test-verification/falsify-old-h1.txt).
Behaviour checks (fail against old code because it does the wrong thing), 8:
- test_h1_same_size_edit..._refuses_the_next_write[first|middle|last]: old answers 201 (expected 503 STORE_UNAVAILABLE maybe=true)
- test_h1_integrity_check_reports_a_changed_file_before_any_write: old integrity ok=True, problem None
- test_h1_putting_the_file_back_rolls_the_refused_write_forward: old answers 201, not 503
- test_h1_adoption_needs_the_whole_file_to_match: DID NOT RAISE StoreWriteError (old adopts on size alone)
- test_h1_deleted_log_file_is_reported_and_refused: old integrity ok=True
- test_h1_unreadable_log_file_is_reported: old integrity ok=True
API-only checks (fail against old code with AttributeError: no RecordLog.file_problem), 3:
- test_h1_integrity_check_is_ok_on_an_unchanged_file
- test_h1_no_file_yet_and_an_in_memory_log_have_no_file_problem
- test_h1_short_reads_are_completed (its behavioural part, a 201 under 1-byte preads, does not discriminate old code; it fails only on the file_problem call)
Pass against old code (regression guards, not H-1 discriminators), 2:
- test_h1_adoption_still_works_when_the_file_matches
- test_h1_adoption_refuses_another_line_of_the_same_size (old size/last-line logic already refuses this)
So the H-1 defect is shown to be caught by 8 of 13 test cases; the 3 API-only tests and 2 guards prove no H-1 regression-detection by themselves.

## Per-test review (read at 99569bd)
General: mocks only at fault-injection points as claimed: the `_OS` proxy replaces store.os attributes (delegating the rest to real os),
scripted ledger (led.script, fail_reads_after), monkeypatched RecordLog methods. No mock stands in for a real dependency's success path.
Every test asserts observable outcome (HTTP status/body, file bytes, len(log), tenants, ledger anchors, integrity record), not just "no exception".
- omega5_mutation: V001 compares to literal expected dict (not derived from code under test except epoch/seq, reasonable). T222 x2, H198, H229, H399, H448,
  V045-V047, V082-V085 assert exact problem text/keys and disk state. No vacuous assertions found.
- store_write_path: T144/T172 assert 503 + disk and memory unchanged. T148-T159 helper `_refused_untouched` asserts the raise and unchanged bytes. T163-T171 assert cut-back,
  latched fault. Durability test asserts ordering: fsync(pending.tmp) < replace < fsync(dir); pwrite(log) < fsync(log). T276/T278/T286 assert state unchanged
  (disk, len, anchors) and no pending/tmp files.
- h1_file_content: see falsifiability above.
Docstring check, test_aegis_store_write_path.py "the file must be exactly memory before a write": TRUE at 99569bd. store.py:176-203 compares size
(`size == expected` or adopt-size) and `_pread_exact(fd, expected, 0) != mem` byte for byte before any write, else raises "the log file and memory disagree".
The docstring is accurate for 99569bd; it would have overclaimed at 75b0662 (size only), which is the H-1 defect. The same docstring's "only a file that
already ends with exactly this line is adopted" is slightly loose: adoption also requires the prefix to equal memory (stricter, not weaker).
Header says "BLOCK at ce665c5"; historical, fine.

## Findings
F1 LOW (VACUOUS_ASSERTION risk, low): test_t222_* monkeypatch `ST.os.unlink`, and `ST.os` is the real `os` module here, so this patches os.unlink process-wide for the test
   (restored by monkeypatch at teardown), unlike the `_OS` proxy used elsewhere. The module docstring claim "Nothing outside store.py sees it" applies to the proxy, not to these two
   tests. Pass is genuine (raise path observed, 3.12 and 3.13). Reproducer: grep "ST.os, \"unlink\"" tests/test_aegis_omega5_mutation.py. Falsifier: tmp cleanup failing under the patch (it did not).
F2 LOW (NEGATIVE_CONTROL_WEAK): 3 of 11 H-1 tests are API-only against the pre-fix code (listed above) and 2 are guards; discrimination of H-1 rests on 8 tests. Adequate, noted.
F3 LOW: `test_h1_short_reads_are_completed` ends with `monkeypatch.undo()` mid-test, which also undoes the env fixture's monkeypatches if they share the fixture instance;
   it passes and the remaining assertions only read state, so no false green observed.
F4 BLOCKING (HIGH per contract): NO_DISCRIMINATION_PROOF: no mutation-fuzz proof at 99569bd was provided; the H-1 falsifiability above is mine and is not a mutation proof of the 21 T-mutants/V-mutants.

## NOT RUN
- Mutation execution / kill counts for T144..T286, V001..V085, H198..H448 (peer mutation-fuzz; not in my scope).
- No test run on any SHA other than 99569bd; no CI; no live ledger; ooh-py only (no other services' suites).
- Python versions other than 3.12 and 3.13; platforms other than macOS (darwin); root-user run (the mode-0 test branch).
- No stress/repeat >2 reruns; no random-order or xdist run.

## Update after the mutation report
Read: omega8/mutation.md (sha256 verified = 75948357afe2f2f642162469716d2891871f6c409dcb4d3d22ea6fc3278e4a76), bound to the same SHA 99569bd / tree 7d08f34.
Independent spot check: I ran the mutation gate's probe file (9 tests) against a pristine git-archive copy of 99569bd on 3.12: "9 passed"
(test-verification/probe-pristine-3.12.txt). So the probes are valid on the shipped code; I did not re-run the mutants (not my scope) and rely on the mutation report for kill/survive results.
Discrimination proof now present: 153 mutants per Python (identical on 3.12 and 3.13), 136 killed, 17 survived, 5 equivalent by reading, 12 non-equivalent survivors; T222 killed by both test_t222_* tests.
Consequence for my gate: Gate 4a discrimination = MUTANT_SURVIVED. Survivors in the claimed-covered H-1 surface: X016, X017 (first/last byte of the whole-file compare never edited by any test:
my earlier review saw that H-1 tests flip only an interior hex digit, which agrees), X011 (adopt compare lacks the trailing newline), X024 (integrity check of an empty in-memory log with junk in the file),
M092/M093 (adopt-path fsync), M011/M013/X008 (_pread_exact EOF guard), M080, M091, M044 (M044 outside the H-1 diff, not counted).
Does the suite discriminate the H-1 change? Partly. It catches the headline defect (interior same-size edit, deleted file, unreadable file, adopt on size alone: 8 behaviour tests fail on the 75b0662 code, 136 of 153 mutants die),
but it does NOT prove "byte for byte": a mutant that ignores the first or last byte of the file survives, so the new docstring claim "the file must be exactly memory ... byte for byte" is not pinned at its edges.
Verdict (updated): BLOCK, now with two grounds: F4 NO_DISCRIMINATION_PROOF is closed (proof exists), replaced by F5 HIGH MUTANT_SURVIVED (X016, X017 HIGH; X011, X024, M092/M093, M011/M013/X008 MEDIUM; M080, M091 LOW).
Accounting findings unchanged (805/805 on 3.12 and 3.13, deterministic). Not TEST_TRUTH_GREEN until tests kill the survivors (the mutation gate's probes in omega8/mutation/probes/test_probe8_survivors.py are candidates, written by that gate, not adopted by me) and mutation-fuzz re-runs at the new SHA.
Additional NOT RUN: mutant re-execution by me; the probe file on 3.13.
