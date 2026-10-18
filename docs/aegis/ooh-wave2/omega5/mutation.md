# AEGIS MUTATION / TEST_TRUTH gate, Omega 5

Author: AEGIS-MUTATION-FUZZ (independent reviewer, fresh context; read-only on the candidate, mutants only in scratch copies)
Candidate: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2, service services/ooh-py
SHA 4a0a4b478a88fbaaeb864a7535779e586b39f64a, tree e0640db37e4a40f58e0a291fd21d90a56f45d1ec (frozen worktree $S/review-4a0a4b4; `git rev-parse` re-checked at the end, `git status` clean)
Interpreters: Python 3.12 ($S/venv3.12) and 3.13 ($S/venv3.13)
Raw data: $S/aegis/omega5/mutation/ (jsonl per run, raw/ copy, raw/probes/ for the reviewer's probes). Sandbox = `git archive HEAD` of the candidate in $S/aegis/omega5/mutation/tree plus per-worker copies in work/.

## VERDICT: BLOCK (narrow)
No bypass of the three headline classes was found by execution, and every one of the 170 behaviour-changing survivors from 5116917 is now killed. But the rule is "an undefended, non-equivalent survivor on the ledger-commit and integrity path blocks the push", and I found some, so it blocks:
- 4 of the builder's 21 "equivalent" mutants are NOT equivalent (H198, H229, H399, H448). All LOW: none changes a commit/anchor/tamper verdict. The ADR 0022 sentence "Each only adds a ledger read, changes a value no caller reads, ..." is wrong for these four.
- Extension hunt, store.py: T222 (clear_pending swallows an unlink failure) is undefended and non-equivalent. Executed demonstration: the 503 says maybe=false, the pending line stays on disk, and when its anchor later appears the write takes effect (pristine says maybe=true). MEDIUM (needs an unlink failure plus an anchor appearing after a "certain" ledger refusal). This is the "503 maybe=false takes effect later" class.
- V001: the anchor payload key name `line_sha256` can be changed with all 767 tests green (the wire format of the anchor is pinned only by the code itself). MEDIUM (format contract; a rename bricks every existing anchor on upgrade, and nothing would tell you).
All of these are closed by small tests; the passing-on-pristine probes are in raw/probes/ and can be adopted. If Omega rules that LOW items 1 and the MEDIUM items are not push-blocking, the evidence below supports a GREEN-with-findings reading; I will not round it up myself.

Mutation survivor counts (the number requested for the morning): see section 9.

## 1. Header / baseline (item 2)
- 767 passed on 3.12 (88 s) and 3.13 (85 s), whole suite, no -x, `-p no:cacheprovider`, sandbox of SHA 4a0a4b4.
- No-op mutant (comment appended to service.py): 767 passed on 3.13 (111 s, full313.jsonl id NOOP) and 3.12 (156 s, s312.jsonl id NOOP). With test_aegis_integrity_path.py ignored: NOOP = 717 passed (ign313.jsonl).
- `git diff 5116917 4a0a4b4 -- services/ooh-py/src` = 0 bytes (verified). `diff -r` of the sandbox src against the 5116917 mutation tree: identical, so the 5116917 mutant positions apply unchanged.
- Diff of the commit: tests/test_aegis_integrity_path.py (+622), ADR 0022 (+13), test-counts (717 -> 767). Nothing else.

## 2. The 234 survivors of 5116917, whole suite, no -x, 3.13 (item 3)
Command: `run5.py 3.13 7 surv234.txt full313.jsonl a` (copies of the sandbox, one mutant per copy, `python -m pytest -q -rfE tests`). Python 3.13, SHA 4a0a4b4.
- 213 KILLED, 21 SURVIVED. Survivors = exactly the builder's list: H085 H192 H198 H229 H252 H253 H254 H255 H311 H322 H344 H357 H371 H384 H399 H416 H432 H441 H448 H453 H469. No claimed-equivalent was killed; no unlisted one survived.
- Killer names for every mutant: raw/killers_234.tsv. Every one of the 213 is killed ONLY by tests in tests/test_aegis_integrity_path.py (no failing test outside that file in any of the 213). All 48 test functions in the file kill at least one mutant (none is dead weight).
- The builder's own claims, re-checked: my 170 probed survivors: 170/170 killed (claim: all 170, holds). Of the 64 I had not probed: 43 killed, 21 survive (the builder claimed 33 killed; 43 is the true number; the 21 left match).
- 3.12 sample (>= 40 required): 55 mutants = the 21 claimed-equivalent + 34 random killed (seed 55): killers and verdict identical to 3.13 for all 55 (s312.jsonl). NOOP 767 passed.
- Load-bearing check: 40 random of the 213 killed mutants (seed 56) re-run on 3.13 with `--ignore=tests/test_aegis_integrity_path.py`: all 40 SURVIVE (717 passed), plus NOOP 717 (ign313.jsonl). The other 173 are covered by construction (their failing sets lie wholly inside the ignored file) and were not individually re-run.

## 3. The 21 claimed-equivalent mutants (item 4)
Judged by reading the code at the exact SHA; the ones I doubted were probed with tests that pass on pristine 3.12 and 3.13 (raw/probes/test_eq_probes.py).
EQUIVALENT (17), reasons confirmed:
- H192 `force` -> True at service.py:316: reached only when the unforced guard already failed, i.e. >= INTEGRITY_RETRY_S (15) elapsed, so `< FORCED_MIN_S` (10) is false anyway.
- H252, H253, H254, H255: the only thing they gate is `_after_integrity()`, a no-op (no override anywhere: grep).
- H311, H344, H416, H441: `rolled` is True with a non-None problem; the only reader is `problem is None and rolled`, so unread. (H229 differs, see below.)
- H322: in the own-pending path `appended` is always True when problem is None, so `(None, True)` is the same value.
- H453: `return None, True` -> `return (None, True)` is the identical value.
- H432: `(None, False)` instead of the "not vouched: no anchor" string leads to the same write_discarded/clear_pending outcome in both callers.
- H357: `aside is not None` -> True: `_roll_forward(None)` raises TypeError, caught, returns stale; clear_discarded on a missing file is a no-op.
- H371: `appended_any |= appended` only drives the re-read of entries; the aside line's anchor was already in the first read.
- H384: a non-str actor makes `re.fullmatch` raise TypeError, which is caught to the same "not vouched: stale".
- H469: the epoch check at `_anchor_problem` already rejected any anchor whose subject is not `log:{epoch}` (shadowed).
- H085: only changes whether the retry error is re-raised or wrapped (message and class); `took_effect` stays "unknown", and no caller reads anything else. Probe (lost, lost) passes on the mutant.
NOT EQUIVALENT (4), each with a probe that passes on pristine and fails on the mutant (3.13; pristine passes on 3.12 and 3.13):
- H198 (service.py:316, `and self._last_integrity_try` -> True): with time.monotonic() < 10 s (host booted < 10 s ago) the first forced check is skipped and the cached "not ok" is returned without reading the ledger. Probe test_P_H198_first_forced_check_within_boot_window. LOW (fail-closed, boot window only).
- H229 (:327, `problem is None and rolled` -> `rolled`): when clear_pending fails after an appended pending line and the re-read of the ledger fails, the integrity problem text is replaced ("the pending line could not be set aside" -> "the ledger cannot be read"). ok stays False. Probe test_P_H229_message_when_clear_fails_and_reread_fails. LOW (wrong reason shown to the operator).
- H399 (:417, stale -> `False` flipped to True): a stale/unvouched pending line is deleted without first being written to pending.discarded, so a failing write_discarded no longer blocks (pristine: integrity not ok "could not be set aside"; mutant: ok True). Probe test_P_H399_stale_pending_set_aside_write_fails. LOW (the line is inert and cleared in the same pass anyway).
- H448 (:439, "operator must inspect" `True` -> False): an aside line that is appended but cannot be applied leaves pending.discarded behind for one more cycle. Probe test_P_H448_aside_line_anchored_but_unapplicable. LOW.
Observation on the PRISTINE candidate (not a mutant; probe test_P_aside_unapplicable_integrity_verdict): in `_settle_pending` the aside branch (:397-404) ignores the problem returned by `_roll_forward`. An aside line that is vouched by the ledger, chains, has a valid actor and a known kind, but cannot be applied (e.g. tenant_created without tenant_id) is appended and integrity stays ok=True, the in-memory state lacks the line, and a restart then dies with KeyError 'tenant_id' (verified: the own-pending and file-pending paths answer "an operator must inspect the log", the aside path does not). Needs a ledger-valid anchor for a malformed line, so not reachable from this service's own writes. LOW/MEDIUM, non-blocking, for the next wave.

## 4. Vacuity of the new tests (item 5)
Method: genvac.py generated 132 mutants over service.py 150-452: every refusal code `R("X")` swapped to the other codes, every `_maybe()` dropped, every message/key string of length > 6 replaced, and 6 boundary mutants on the two rate-limit constants. Ran with the new file FIRST and -x (run5x.py, 3.13): 108 killed, 24 survived the whole suite.
- Killed first by the new file: 63. Killed by older files only (the new file passed): 45. All `_maybe()` drops and all `R()` swaps in the commit/refusal paths are killed; control halves (maybe=false vs maybe=true, ok vs not ok) are asserted.
- The 24 survivors, run against my probes (vacext_batt313.jsonl): killed by probes (so real, and undefended): V001 (anchor payload key `line_sha256`), V045-V047 (`_drop_pending` integrity dict keys and text), V082-V085 (lost-answer / anchored-not-written dict keys `checked_at`, `problem`). Not killed and not equivalent-in-effect: V005, V024, V026, V087 (`checked_at` key in the read-failure, fault and closed dicts: cosmetic), V017-V019/V051 (the `_gate` closed refusal code: still 503, wrong code; `_commit` pins SERVICE_CLOSED), V074/V075 (informational keys `event_type`/`subject_id` inside the log's evidence entry; nothing reads them back), VB0-VB3 (`<` vs `<=` and +1 on the two timers: boundary equality, only at exact-second ties). V063/V064: messages of exceptions that are caught and discarded (equivalent).
- Behaviour, not just private state: the tests assert integrity verdicts and problem strings, HTTP status and detail (INTEGRITY_UNVERIFIED), `len(log)`, tenant membership (what was applied), the anchors on the ledger, and the contents of pending/pending.discarded. A handful also read `_own_pending`, always next to an observable assertion.

## 5. Named mutants (item 6): whole suite, no -x, both interpreters
R19, C06, C02, C03, C04, C07, C08 and X-T10 (4 tests), X-API054, X-API065, X-M001, X-M002, X-M004, X-G41, W34-333, W34-201, W34-204 (the ten B1-B8 mutants): each killed by its own named test on 3.13 and on 3.12 (named313.jsonl, named312.jsonl), identical fail sets. NOOP 767 passed on both. C03 is now also killed by four test_ip_own_pending/first_line tests.
(The "without test_aegis_commit_path.py" survival check was done at 5116917 and not repeated; the src is byte-identical.)

## 6. Legacy (item 7)
The src is byte-identical to e680f98 and 5116917 (empty diff), and 4a0a4b4 only adds tests, so a killed mutant cannot come back; I verified rather than assumed: 3.13, whole-suite order with -x (run5x.py): all 390 legacy survivors re-run: 390 SURVIVED (0 killed by the 50 new tests); 60 random of the 100 prior kills (seed 77): 60 KILLED. 0 changes in either direction (leg313.jsonl, leg313b.jsonl). Legacy survivors elsewhere stay as Omega ruled: they block merge, not push.

## 7. Extension hunt (item 8)
Generated line-level mutants (cond, operands, and/or, compare, return values, raise/stmt -> pass, bool flips): service.py 1-149 and 453-584 (A001-A070, B001-B136), store.py 51-340 (T001-T286): 502 compiling mutants. 3.13, whole suite with -x (ext313.jsonl): 304 killed, 197 survived, 1 (A001, `JOBS` removed) crashed pytest with empty output (counted killed by import failure, not individually confirmed). My battery of 101 tests (raw/probes/test_ext_battery.py; 101 passed on pristine 3.12 and 3.13) then ran against the 197 survivors: 142 are killed by it (so non-equivalent and undefended), 55 are not killed (27 of them T180-T206 = `RecordLog.verify()`, never called anywhere: dead code; B084 is an identity mutant of the generator; the rest shadowed or unprobed, see below).
Against the three questions:
- A write commits without its anchor: NO survivor does. The anchor is recorded in `_commit` before the append and no mutated line in the extension range touches it. Store write-path survivors (T157/T159 file-memory disagree guard, T163-T171 short-write/cut-back/fault, T166-T169, T126-T128/T212/T218/T223/T247 closed-store guards, A058) are undefended defence-in-depth: killed by my probes only.
- A 503 maybe=false takes effect later: T222 (store.py:262). Evidence: raw/T222_late_demo.txt. Pristine: unlink failure -> maybe=true. T222: `{'detail': 'LEDGER_UNAVAILABLE', 'maybe': False}`, pending.line remains, and when the anchor is recorded afterwards the integrity check appends and applies the tenant. T251 (:296, same for clear_discarded) is equivalent at service level (the only caller swallows StoreWriteError).
- A tampered log passes the integrity check: NO. 29 tamper tests (12 middle-line, 12 last-line, 5 first-line variants: hash, canonical form, prev, seq, not JSON, not a dict, empty line, torn tail, reordered, duplicated, format missing, trailing empty line; first, middle and last line) were applied against each verify_lines / start-up check mutant (T016, T024, T032, T034, T036, T038, T071, T079, T081): "started AND integrity ok" never happened. T024/T032/T034/T036/T038 (verify_lines operand/hash/canonical checks) change only WHY a tampered log is refused (StoreCorrupt at start vs. no matching anchor); the ledger anchors shadow them. T071 (:103) makes an empty log file refuse to start (availability only; probe test_X_empty_log_file_starts_clean).
Other undefended non-equivalent survivors found (all fail closed or are read-only; none is on the three classes): A053/A049 (service start with an unheld data-directory claim no longer raises DataDirBusy; the second writer would then be stopped only by the equally undefended T157/T159 guard; the pair masks each other and I did not run the combined mutant), A055/A056 (a failed start no longer releases the claim / the exception is swallowed and the service object is returned already closed), A058/A045/A047, A059 (close() on an in-memory service would crash), and the /audit/evidence and /audit/export routes: B046, B057, B065-B071 (committed-status checks: unanchored evidence reported `committed`), B100, B104/B110, B030/B031, B122-B134. B065 means the route's stated rule "unanchored evidence = attempted, not done" has no test that exercises an unanchored line. These are legacy-class (older than this commit): block merge, not push.
Not equivalent and not killed by the battery, left uncleared: T148-T155 (adoption branch of append_prepared), T166/T168/T283 (fsync statements; not observable without a crash), T172, T062 (makedirs, shadowed by the DataDirLock which creates the directory), T016/T079/T081 (shadowed by verify_lines), A036/A037/A070 (init order, equivalent by the forced start-up check), B034-B037 (comment says never reached), B052/B053, B079/B080, B106/B108 (redundant with the payload hash check).

## 8. What I did NOT run
- Extension hunt (502 mutants), the battery, and the vacuity set ran on 3.13 only (the batteries pass on pristine 3.12 too). The legacy re-run was 3.13 only.
- Legacy: 390 survivors + 60 of 100 prior kills (not the 40 others, and not the other ~740 prior kills of omega 3).
- Load-bearing "file ignored" run: 40 of 213 individually; the rest by construction.
- Mutants in svc_campaigns.py, svc_inventory.py, svc_planning.py, svc_contracts.py, svc_tenants.py, api.py, ledger.py beyond the named ones and the 390+60 legacy set.
- Combined (two-mutant) runs, e.g. A053 with T157. No concurrency/race or resource-exhaustion attack was executed (the bounded-time budget went to the mutation matrix); the interleaving of close() with a commit is argued from the single service lock, not attacked.
- A001 not confirmed individually; H-series mutants were not re-generated (positions reused from 5116917, valid because src is identical).
- gitleaks, the personal-data scan, provenance, Release Guardian, Omega, the evidence/handoff commit and any push: outside this gate; I did none of them. I made no commit and no push. Nothing was written to any tracked repo (frozen worktree `git status` clean, HEAD and tree re-verified).

## 9. Counts
- Integrity-path census (417 mutants, 5116917): 234 survived; at 4a0a4b4: 213 killed, 21 survive: 17 equivalent, 4 non-equivalent LOW (H198, H229, H399, H448).
- Extension (502 mutants): 197 survive the suite; 142 of those shown non-equivalent by probes; 55 not killed (27 dead code, the rest shadowed/equivalent/unprobed); 0 would let a write commit without its anchor; 1 (T222) lets a maybe=false 503 take effect later under the stated conditions; 0 let a tampered log pass.
- Undefended non-equivalent survivors that need a test before push: H198, H229, H399, H448, T222, V001 (plus the LOW cosmetic V045-V047/V082-V085).
- Survivors total across everything I ran: 21 + 197 + 24 (vacuity, 8 of them equivalent/cosmetic) + 390 legacy.
