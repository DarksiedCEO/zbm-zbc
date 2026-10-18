# AEGIS Ω verdict at 4a0a4b4 (transcribed)

> Transcription note: Ω is hard read-only and wrote no file. This is its final hand-back of 2026-10-18, copied by
> the builder without edits to its findings.

# AEGIS Ω verdict: RED for 4a0a4b4. No GREEN can be reached for this tuple.

**Tuple:** repo DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2), candidate 4a0a4b478a88fbaaeb864a7535779e586b39f64a, tree e0640db37e4a40f58e0a291fd21d90a56f45d1ec, base 84a5377df4892efcb4cb1c221a6862075fe0e018.
**Runtime pin:** REAL_PROTOCOL_LOCAL only (macOS arm64, Python 3.12.14 and 3.13.9, ledger-rust built by each gate).
**Scope:** services/ooh-py plus the docs in the diff 84a5377..4a0a4b4. Nothing beyond this tuple and scope is certified.

Ω read the reports in `aegis/omega5/` and `aegis/omega7/`, and directly: in the frozen candidate store.py (1-300), service.py (150-254), docs/findings/OPEN.md and ADR 0022:151; in the descendant tests/test_aegis_omega5_mutation.py (1-75).

## Gates as read
| Gate | Report says | Bound to the tuple | Ω's ruling |
|---|---|---|---|
| SOURCE | SOURCE_GREEN (builder transcription) | yes | green as read |
| SECURITY | SECURITY_GREEN, M-1 rated MEDIUM | yes, written by the gate | not green: the founder grades card/SSN HIGH, and the stricter grade wins |
| RELIABILITY | RELIABILITY_GREEN (builder transcription) | yes | green as read |
| TEST_TRUTH | mutation.md BLOCK (narrow); mutation-rgate.md GREEN with open items (builder transcription), T222 left to Ω | yes | not green: T222 ruled in scope; no separate test-verification report |
| DATA | DATA_GREEN, M-1 rated MEDIUM | yes, written by the gate | overridden by H-1 HIGH (same behaviour) |
| PROVENANCE | PROVENANCE_PASS (builder transcription) | yes | pass |
| RUNTIME | omega5 GREEN for REAL_PROTOCOL_LOCAL | yes | overridden on H-1 by the omega7 BLOCK, on byte-identical source |
| Release Guardian | NOT_READY | yes | input only; Ω agrees with its conclusion |

## Independent checks
- Same tuple: TRUE for all omega5 reports. omega7 reports bind 75b0662 and are used only as evidence about byte-identical source (store.py blob bff84c67…, src tree 6d8a4442…, provenance item 4); not gate verdicts for this tuple.
- Evidence complete: FALSE. No test-verification report; no independent gate has observed T222 killed at any SHA (the 75b0662 mutation gate was stopped); OPEN.md has 0 ooh lines at the candidate.
- No domain missing: FALSE (test-verification).
- No contradictions: FALSE. Settled for the stricter result: DATA M-1 MEDIUM vs RUNTIME H-1 HIGH; omega5 runtime GREEN vs omega7 runtime BLOCK; mutation BLOCK vs the R-GATE re-ruling; security MEDIUM vs the founder's HIGH for card/SSN.
- Independence held: NOT_PROVEN (false). Source, reliability, mutation-rgate and provenance reached Ω only through the builder; the builder carried the mutation flip from BLOCK to GREEN; mutation.md does not say who wrote it.

## Rulings
**a. H-1: HIGH. Blocks 4a0a4b4 under R-GATE.**
- store.py:180-184 compares only size before writing. DATA C11 saw it at 4a0a4b4 (write 200 after a disk edit, integrity ok:true); omega7 e2/e3 reproduced it; the falsifier discriminates.
- False claim at this SHA: store.py:159-160 "The file must be exactly the in-memory lines before the write (else refused)".
- Restart detection (store.py:11-12; StoreCorrupt or "no matching anchor", e4) is real but covers restarts only. While running, /audit/integrity and the integrity job report ok on a file that no longer matches memory; 201-acknowledged lines are appended on top; in e3 acknowledged lines end up behind a broken chain, the original bytes are lost, and an operator must repair the file. The integrity detector fails open at runtime.
- Data dir 0700 (store.py:98), log 0600 with O_NOFOLLOW (store.py:176): only the service user, root, or same-size disk corruption. Lowers attacker likelihood (DATA's reason for MEDIUM); does not cover accidental corruption or the control reporting ok.
- HIGH kept: stricter gate wins; OPEN.md header says severities "are not lowered here"; merits do not justify a downgrade. Only a recorded founder ruling could regrade it.
- Effect: a High, so R-GATE's exception ("no code commits this round unless Ω finds a Critical or High") applies, subject to the founder assigning the work.

**b. T222: in R-GATE's anchoring scope. Blocks 4a0a4b4. Founder's must-fix before any merge into integration confirmed.**
- maybe=false tells the caller a refused write had no effect; that protects purchase authorization, funds and approvals. Under the mutant a "certain" refusal later takes effect (raw/T222_late_demo.txt).
- The shipped code is correct: store.py:261-262 re-raises; service.py:201-207 and 240-249 turn it into maybe=true and stop writes. The gap is a missing test; at 4a0a4b4 no test kills the mutant. No separate push ruling: none this round, and any push of this tuple would be uncertified.
- "Fixed" means: (1) at the SHA being certified, a test passes on the shipped code and fails on mutant T222; (2) an independent mutation gate observes this on 3.12 and 3.13, whole suite, no -x; (3) store.py:253-262 and service.py:240-249 unchanged, or re-reviewed if changed. No src change needed.
- ce665c5 tests (test_aegis_omega5_mutation.py:38-65) have the right shape (maybe:True, pending kept, clear_pending raises); that they kill T222 is only the builder's statement: NOT_PROVEN.

**c. V001: outside the blocking-survivor scope. MEDIUM, tracked in OPEN.md.** The rename is symmetric on write and read; it cannot commit without an anchor, make a refusal take effect, or pass a tampered log. Failure is fail-closed on upgrade (old anchors stop matching, 503 INTEGRITY_UNVERIFIED): availability/compatibility, not protection.

**d. Release Guardian's other points.**
- OPEN.md tracking gap: Medium/Low are non-blocking only when tracked; untracked at 4a0a4b4, so a blocker (ACCOUNTING_IMPURE). A docs-only commit after the candidate cannot fix this for 4a0a4b4: it is a new tuple needing its own Ω round, no carry-forward. A provenance proof that src and tests are byte-identical is necessary but not sufficient for that round. The uncommitted OOH2-PII-1 row is not evidence.
- No CI: NOT_PROVEN, not failed. Any certification stays limited to macOS REAL_PROTOCOL_LOCAL. CI green at the exact SHA is a condition for any merge into integration.
- Transcribed reports: raw outputs back the numbers (767 passed, SIGKILL passes, T222 demo); authorship and verdict wording cannot be verified. Recorded as an independence gap, not proof of tampering.

**e. Card/SSN (security M-1, OOH2-PII-1): HIGH, as the founder graded it.** Stricter than the gate's MEDIUM, so it stands. Reproduced at the tuple: PAN and SSN stored and readable through /audit/export and the unit view, against ADR 0022:151. An open HIGH means no GREEN for any scope; it blocks merge and pre-production, and is untracked at the candidate. The founder's ruling that it is not a push blocker is his authority over pushes; it is not part of an Ω GREEN.

## Verdict: RED
- mayMergeOrRelease: false
- requiresFounderApproval: true

Blockers:
1. H-1, HIGH (GATE_NOT_GREEN, SURVIVING_BYPASS of the runtime integrity detector).
2. Card/SSN, HIGH as graded by the founder (GATE_NOT_GREEN).
3. T222: an undefended survivor in anchoring scope (TEST_TRUTH not green).
4. Medium/Low items untracked in OPEN.md at the tuple (ACCOUNTING_IMPURE).
5. No test-verification report (GATE_MISSING).
6. Independence of four transcribed verdicts NOT_PROVEN.

Missing or stale: test-verification report; an independent observation of T222 killed; the 75b0662 mutation run (unfinished); CI (NOT_PROVEN); live and production runtime (not claimed).

## Conditions for GREEN (successor tuple only)
1. The omega7 e2 and e3 same-size edits are refused with 503 and the file left untouched, witnessed by an independent runtime gate at that SHA, and store.py:159-160 matches the code — or a committed founder ruling regrades H-1 below High.
2. T222 killed at that SHA, as defined in ruling b.
3. Card/SSN fixed and independently verified, or regraded by the founder in a committed record.
4. Every Medium/Low ID in OPEN.md at that same SHA: V001, DATA M-1 (if H-1 is regraded) and M-2, REL F2, SRC F1/F2, the aside-branch gap at service.py:397-404, H198/H229/H399/H448, SEC L-1/L-3, DATA L-1/L-2/L-3, REL fd leak/timing offsets/F3, SRC F3/F4/F5 and the service.py:452 clause, RT O-1/O-2, and the survivor groups A053/A049+T157/T159, A058/T126-T128/T212/T218/T223/T247, B046/B057/B065-B071 and the 390 legacy survivors.
5. All seven gates run fresh at that tuple, including a test-verification report and a finished mutation run, each delivered unaltered by the builder or hash-attested by its gate.
6. For any merge into integration: CI green at the exact SHA.

The builder's planned docs-only commit is not certified by this verdict. It would address condition 4 only.
