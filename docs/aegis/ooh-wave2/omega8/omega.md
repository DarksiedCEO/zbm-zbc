# AEGIS Ω verdict at 99569bd (transcribed)

> Transcription note: Ω is hard read-only and wrote no file. This is its final hand-back of 2026-10-18, copied by
> the builder without edits to its findings.

# AEGIS Ω verdict: RED for 99569bd

**Tuple:** DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2), candidate 99569bd85a791c102c6df293d7e987743813644b, tree 7d08f34836a7b4188ecd8d46906cfa38ecfd23ae, base 84a5377df4892efcb4cb1c221a6862075fe0e018.
**Runtime pin:** REAL_PROTOCOL_LOCAL only (macOS arm64, Python 3.12.14 and 3.13.9, ledger-rust built by the runtime gate, sha256 2d26e88d…).
**Scope (founder):** the H-1 fix in services/ooh-py (src/store.py, src/service.py, tests/test_aegis_h1_file_content.py) and the docs commits da7b960, eae7a86, 8b613e3 and the docs in 99569bd. ooh-pii-fix / 010ef48 not looked at.

**Read by Ω:** the omega5 verdict; all seven omega8 reports (incl. the test-verification update); code-delta-75b0662..99569bd.patch (every hunk); frozen store.py 1-270, service.py 300-354; docs/findings/OPEN.md 60-109. builder/ logs not used as evidence.

## Gates as read
| Gate | Report says | Bound | Ω's ruling |
|---|---|---|---|
| SOURCE | SOURCE_GREEN (3 LOW, 1 INFO) | yes; gate-written; hash cd692c62… | green as read |
| SECURITY | SECURITY_GREEN; M-1 MEDIUM (new), L-1, L-2 | yes; gate-written; 95de610f… | green for the delta; OOH2-PII-1 (HIGH) still open (ruling 6) |
| RELIABILITY | RELIABILITY_GREEN (F-R2 LOW, F-R1 INFO) | yes; gate-written; 26-hex prefix matched | green as read |
| TEST_TRUTH | test-verification BLOCK (F5 HIGH); mutation BLOCK | yes | not green |
| DATA | no report for this tuple | — | GATE_MISSING |
| PROVENANCE | PROVENANCE_PASS 1-6 | yes; builder copy, no examiner hash | pass; independence NOT_PROVEN |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE, REAL_PROTOCOL_LOCAL | yes; gate-written; 00166c21… | green for REAL_PROTOCOL_LOCAL only |
| Release Guardian | no recommendation | — | missing |

## Independent checks
- sameTuple TRUE. evidenceComplete FALSE (no DATA, no Release Guardian, no CI; the six present reports complete against their own denominators: 805/805 twice per Python, 153/153 mutants, 13/13 security boundaries).
- noDomainMissing FALSE: DATA missing; the founder scoped gates to changed files but did not waive DATA, and H-1 was first graded by DATA (M-1).
- noContradictions TRUE after the stricter result: the code is correct, the tests have gaps; BLOCK wins for TEST_TRUTH. Runtime O8-1 and source F3 qualify store.py:12-13 ("every integrity check does too"), both LOW.
- independenceHeld PARTIAL: six reports gate-written with matching hashes (mutation.md also re-checked by test-verification); test-verification append is append-only (first 73 lines hash bb369022…); provenance.md is a builder copy with no examiner hash: NOT_PROVEN.

## Rulings
**(1) H-1 FIXED at 99569bd; condition 1 met.** store.py:192-203 compares `_pread_exact(fd, expected, 0) == mem` with size gating and adopt tail `line + b"\n"` before any pwrite; service.py:335-336 adds `file_problem()` (fault still precedes, 337-338). Runtime witnessed e2, e3, m2, m3, e2-rename (write-first: 503 maybe:true, file sha256 unchanged; integrity-first: ok False "file differs"; 26 runs, no refused write changed the file); the 75b0662 control gave 201 / ok True. The docstring now at store.py:177-181 matches. LOW caveats to track: O8-1 (throttled /audit/integrity cached ok up to 10 s), SRC F1 (store.py:2-4 "unchanged in behaviour"), SRC F2 (deleted file re-created empty). OOH2-RT-H1 may close for ooh-py at a tuple whose OPEN.md records it. The five OOH2-RT-H1-<SERVICE> rows (OPEN.md:86-90) are present as ordered and do not block this scope.

**(2) T222 CLOSED at 99569bd; condition 2 met.** Killed on 3.12 and 3.13, whole suite, no -x (2 failed, 803 passed vs NOOP 805) by test_t222_store_clear_pending_raises_when_the_file_cannot_be_removed and test_t222_unremovable_pending_after_certain_refusal_answers_maybe; clear_pending and the maybe=true path unchanged. TV F1 (LOW: process-wide os.unlink patch) to be tracked.

**(3) 12 non-equivalent survivors: 6 in R-GATE blocking scope, 6 outside.** The whole-file compare and integrity check are the local half of the anchoring guarantee protecting approval, HOLD_CONFIRMED, funds and purchase records. Blocking: X016, X017 HIGH (compare ignores last/first byte; test gaps, shipped code compares every byte); X024 MEDIUM (integrity check skips the file of an empty log); X011 MEDIUM (adopt acknowledges a differing tail); M092/M093 MEDIUM (adopted line acknowledged without fsync). Outside scope, to track: M011/M013/X008 (EOF guard), M080 LOW, M091 LOW, M044 LOW. Equivalents M002, M005, M007, M084, X010 accepted (reading; probes agree on X010).

**(4) Security M-1: MEDIUM, real, a regression introduced by the fix; non-blocking under R-GATE once tracked; untracked at this tuple.** store.py:240-242 `open(self.path, "rb")` / `fh.read()` with no O_NOFOLLOW, O_NONBLOCK, S_ISREG or size cap, under self.lock inside the service lock. The old verify() read the same way but had no caller. Not fail-closed: a FIFO hangs every route. If file_problem changes, security and mutation must re-review; the gate's falsifier is the closure test.

**(5) Remaining conditions.** OPEN.md tracking satisfied for every condition-4 ID of 4a0a4b4 (OPEN.md:72-109), not for this round's findings (SEC8 M-1/L-1/L-2; SRC8 F1-F3; REL8 F-R2; RT8 O8-1; TV8 F1-F3; out-of-scope survivors): ACCOUNTING_IMPURE. Question for the founder: findings first raised in a certifying round can never already be in that round's tuple; resolving that regress needs his committed ruling. Test-verification present, accounting clean, BLOCK on F5. Independence: provenance NOT_PROVEN. CI NOT_PROVEN; required before any merge into integration.

**(6) OOH2-PII-1: HIGH, open, unfixed at this SHA; blocks.** Tracked (OPEN.md:72). An open HIGH means no GREEN for any scope. The founder's not-a-push-blocker ruling is his authority over pushes, not part of a GREEN. Purge/re-key requirement stands. 123-45-6789 in 8b613e3 is a reserved test number; no home path in the 99569bd tree; da7b960/eae7a86 history is the founder's recorded choice and pushing would publish it.

## Verdict: RED
- mayMergeOrRelease: false (no push this round; any push of this tuple would be uncertified)
- requiresFounderApproval: true

Blockers:
1. OOH2-PII-1 open HIGH (GATE_NOT_GREEN).
2. TEST_TRUTH not green: X016, X017 (HIGH), X024, X011, M092/M093 surviving in R-GATE scope.
3. No DATA report (GATE_MISSING).
4. No Release Guardian recommendation (GATE_MISSING).
5. This round's Medium/Low findings, incl. the M-1 regression, not in OPEN.md (ACCOUNTING_IMPURE).
6. provenance.md builder-copied with no examiner hash (independence NOT_PROVEN).

Closed at this tuple, not carried forward: H-1 in ooh-py (condition 1); T222 (condition 2); old condition-4 IDs tracked; test-verification report present.

## Conditions for GREEN (successor tuple only)
1. H-1: store.py and service.py byte-identical to blobs 35a439c8 and a81ab0a8 (provenance), or an independent runtime gate re-witnesses e2, e3, m2, m3 refused with the file untouched.
2. T222 killed again at that SHA on 3.12 and 3.13, whole suite, no -x, clear_pending and the maybe path unchanged.
3. Tests on the shipped code kill X016, X017, X011, X024, M092, M093, observed by an independent mutation gate on both Pythons, whole suite, no -x, re-running all changed functions; or a committed founder record places them outside R-GATE scope or regrades them.
4. OOH2-PII-1 fixed at that SHA and verified by independent security, data, runtime and mutation gates, or regraded below High in a committed founder record.
5. OPEN.md at that SHA tracks every omega8 Medium/Low: SEC8 M-1/L-1/L-2, SRC8 F1-F3, REL8 F-R2, RT8 O8-1, TV8 F1-F3, M011/M013/X008, M080, M091, M044. If file_problem changes, security and mutation re-review it.
6. All seven gates fresh at that tuple, including DATA, plus a Release Guardian recommendation; each report gate-written or gate-hash-attested; provenance relayed verbatim or its hand-back hash attested.
7. For any merge into integration: CI green at the exact SHA.

Any change to the candidate, base, runtime, validator or workflow voids this verdict.
