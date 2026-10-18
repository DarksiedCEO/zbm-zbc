# AEGIS MUTATION / TEST_TRUTH gate at ce665c5 (transcribed)

> Transcription note: the gate's role contract forbids report files. This is a builder transcription of the gate's
> final hand-back. Verdicts, counts, mutant ids and killer names are copied exactly; some prose is shortened. Raw
> data: mutation/raw/ (thirteen31x*.jsonl, kill213_313.jsonl, kill60_312.jsonl, ext197_313.jsonl, p6_all31x.jsonl,
> eq17_*.jsonl) and probes: mutation/probes6/test_p6_store.py, mutation/probes/test_ext_battery.py.

Author: AEGIS-MUTATION-FUZZ (independent reviewer, fresh context; read-only on the candidate)
Candidate: ce665c5b3aef1bcd56aacee9523559150dd8cb7b, tree 5530ba08de8a78fad4c004db4fedeb8c40b45a4a, base 84a5377. Frozen worktree re-verified at the end, `git status` clean. `git diff 4a0a4b4 ce665c5 -- services/ooh-py/src` = 0 bytes. Interpreters: Python 3.12.14, 3.13.9. Whole suite, no -x, `-p no:cacheprovider`.

## VERDICT: BLOCK
The six blockers from 4a0a4b4 and the LOW cosmetic items are closed, killed on both interpreters by truthful, non-vacuous tests. The fixture change weakened nothing. In the extension survivors the only change is that T222 is now killed. The gate found 21 further undefended non-equivalent survivors on the ledger-commit/record-first path or the append-time log guard (section 7). It had not listed them as push-blocking at 4a0a4b4; it calls that too narrow and corrects it here.

## 1. Baseline
777 passed on 3.12 and 3.13. NOOP 777 on both. With test_aegis_omega5_mutation.py ignored, NOOP = 767 on both.

## 2. The 13 mutants
Each gives 776 passed / 1 failed (T222: 775/2), identical killers on 3.12 and 3.13:
H198 test_h198_first_forced_check_reads_the_ledger_soon_after_host_boot; H229 test_h229_settling_failure_is_the_problem_reported; H399 test_h399_stale_pending_is_set_aside_before_removal; H448 test_h448_set_aside_line_appended_late_clears_the_set_aside_copy; T222 test_t222_unremovable_pending_after_certain_refusal_answers_maybe + test_t222_store_clear_pending_raises_when_the_file_cannot_be_removed; V001 test_v001_anchor_payload_wire_format_is_fixed; V045-V047 test_v045_v047_unremovable_pending_integrity_record; V082-V083 test_v082_v083_lost_answer_integrity_record; V084-V085 test_v084_v085_anchored_not_written_integrity_record.
Load-bearing: with the new file ignored, all 13 survive (767 passed) on both. The builder's mutants.jsonl ran only the new file, so it was not relied on.

## 3. Truthfulness: yes, with three LOW notes
- test_h198: the patch is module-scoped; `time.monotonic()` at service.py:313 is the only use of time; `_last_integrity_try = 0` is the `__init__` value (service.py:98). It asserts a ledger read and ok True. It also kills H226, H228, H231, H256.
- test_t222: `ST.os.unlink` is the global os (process-wide for the test, restored by monkeypatch). It fails only on the exact `pending.line` suffix. It asserts the 503 body, the file kept, ok False, and the next write INTEGRITY_UNVERIFIED.
- V-tests: exact key set and exact text, plus the 503. They read `svc.integrity` directly; `health()` returns `dict(self.integrity)` (service.py:470). LOW.
- test_h448 pins current behaviour that ADR 0022 calls open (the appended unapplicable line). The pristine probe test_P_aside_unapplicable_integrity_verdict still fails on 3.12 and 3.13 (ok True, restart KeyError 'tenant_id'). Truthful today; it must be rewritten when the open item is fixed. LOW.
- No side effects; no collateral fails.

## 4. Fixture change
All 213 integrity-path kills from 4a0a4b4 re-run on 3.13, whole suite: 213/213 killed, NOOP 777 (kill213_313.jsonl). Per mutant, the failing set inside test_aegis_integrity_path.py is identical to 4a0a4b4. Only added killers: new-file tests, in 74 mutants. 3.12: 60 random (seed 61), 60/60 killed, identical fail sets.

## 5. Equivalent and vacuity sets
- The 17 claimed-equivalent (H085 H192 H252-H255 H311 H322 H344 H357 H371 H384 H416 H432 H441 H453 H469) all survive on 3.13; the probes kill none of them. They remain equivalent.
- Vacuity survivors (24 on 3.13): 8 now killed (V001, V045-V047, V082-V085). 16 survive: V005 V017 V018 V019 V024 V026 V051 V063 V064 V074 V075 V087 VB0-VB3. Classification unchanged (cosmetic keys, closed code still 503 with a different code, exact-second boundary ties, messages of discarded exceptions). Not push-blocking.

## 6. Named mutants
R19, C06, C02, C03, C04, C07, C08 and the ten B1-B8 mutants (X-T10, X-API054, X-API065, X-M001, X-M002, X-M004, X-G41, W34-333, W34-201, W34-204): each killed by name, identical on 3.12 and 3.13.

## 7. Extension survivors and the push rule
### 7a
197 survivors re-run on 3.13 (ext197_313.jsonl): 196 survive; T222 is killed (the only change). Batteries pass on pristine 3.12 and 3.13 (101 battery tests, 13 store probes, 29 tamper-safe tests). Of the 196: the battery kills 142; the new store probes kill 8 more (T148 T149 T151 T155 T166 T168 T172 T283); 46 unkilled (T180-T206 = `RecordLog.verify()`, no caller; T016/T062/T079/T081; A036/A037/A070; A059; B034-B037; B052/B053; B079/B080; B084; B106/B108): dead code, shadowed or equivalent, merge-blocking only. Tamper: 29 tests against all 197 on both interpreters; 0 let a tampered log pass.

### 7b. The rule as applied
A survivor blocks push if: it is non-equivalent (a probe passes on pristine 3.12 and 3.13 and fails on the mutant); it is undefended (survives the repo suite); it is on the production commit/record-first path (OOH_DATA_DIR is mandatory in production, config.py:81, so in-memory branches are not production); and it is observable at the service surface (HTTP status or maybe, integrity verdict, anchors, file vs memory), or it is a happy-path durability barrier shown by a call-count spy. Correction of 4a0a4b4: T157/T159 were wrongly filed as defence in depth; they block push.

### 7c/7d. Push-blocking (21)
- HTTP-level (13): T144 (open failure gives 500 UnboundLocalError instead of 503); T157, T159 (file/memory disagree guard); T163, T165, T171 (short-write and cut-back guards; T165 acknowledges a short write); T167 (ftruncate skipped), T169 (fault latch not set), T170 (NameError); T172 (fstat EIO swallowed: the mutant answers 201 with memory 15 lines and disk 14; pristine 503); T276, T278 (short pending write undetected, anchor recorded after a partial pending line); T286 (pending write failure swallowed, commit proceeds to the anchor).
- Durability (3): T166 (log-line fsync on every commit), T279 (pending file fsync), T283 (directory fsync after the pending rename). Call-count spies.
- Loosened adoption guard (5): T148, T149, T151, T153, T154 (a longer file with an extra tail, or same size with different bytes, is adopted; pristine refuses "log file and memory disagree"). `_anchor_problem` reads memory, so this guard is the only run-time defence until a restart.
### Not push-blocking (merge-blocking)
T150, T152 (adoption disabled: fail closed; adoption has no test), T155, T168; closed-store guards T126-T128, T212, T218, T223, T247; test hooks T130-T132, T134, T136; T138, T139; T245, T246, T269, T270; in-memory branches T057, T216, T225, T227, T232-T236, T248, T255-T259; T251; T285; tamper/verify_lines internals; A053/A049/A055/A056/A058/A059; B-series audit routes. A053/A049 mask T157/T159 (combined mutants not run).

## 8. Not run
The full 502-mutant extension was not regenerated (only the 197 survivors, 3.13). Many sets were 3.13-only (the 197 re-run, the 17 equivalents, the vacuity set); on 3.12 the 13 mutants, the 17 named, 60 of the 213, the 18 store mutants against the probes, and the 29 tamper tests against the 197 were run. Legacy not re-run (argued from source identity). No combined mutants. No concurrency, exhaustion, traversal or tenant-escape fuzzing. No mutants generated in svc_*.py, api.py or ledger.py beyond the named set. gitleaks, PII scan, provenance, Release Guardian, Ω, evidence commit and push are outside this gate. No commit, push or write to any tracked repo.

## 9. What clears the block
Tests that kill the 21 and pass on pristine; probes in probes6/test_p6_store.py and probes/test_ext_battery.py may be adopted or rewritten. A re-gate on the new SHA is required.
