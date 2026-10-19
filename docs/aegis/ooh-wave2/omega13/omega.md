# AEGIS Ω verdict: **RED** for cb708b9

**Tuple:** DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2, not on any remote), candidate cb708b924bb01aa5543b832573a8fa56bd041b80, tree eba95097622438b226169c88910c81cc9a999212. Range base 75b0662823b867df3325d676b1dcf69abe37a425 (provenance). Code-delta bases are 1a90498 and 99569bd. main 9531fc25 is untouched.
**Runtime pin:** REAL_PROTOCOL_LOCAL only. Darwin 25.6.0 arm64, Python 3.12.14 (Unicode 15.0) and 3.13.9 (Unicode 15.1), 127.0.0.1. Ledger-rust was built by each gate (runtime 997e5601…, data aab060db…; the hashes depend on the build path).
**Scope:** OOH2-PII-1 (the three screened fields, the request hash) plus the H-1 file-integrity work, in services/ooh-py.
**S** = /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad

**Read by Ω (read-only, nothing executed):**
- All ten omega13 reports in S/aegis/omega13.
- S/aegis/omega8/omega.md (the RED at 99569bd) and S/aegis/omega12/SUPERSEDED.md.
- S/aegis/omega13/mutation/raw/kill_table.md and mutant_list.md.
- The frozen S/review-cb708b9/services/ooh-py/src/pii.py in full (394 lines), service.py:66-105 and api.py:364-380.
- OPEN.md rows 72, 100, 140, 166 and 167, plus the full row-ID index.

I did not recompute any digest. Release Guardian (RG) independently recomputed 9 of 9, and provenance recomputed 7 of 7. RG's own digest fa817264… was checked by the builder only.

## Gates as read
| Gate | Report says | Bound to tuple | Ω ruling |
|---|---|---|---|
| SOURCE | SOURCE_GREEN. F-1 Medium; F-2, F-3, F-4 Low | yes | green as read. F-1 ruled MEDIUM (ruling 4) |
| SECURITY | SECURITY_GREEN. S13-1..4 Low; S12-1 closed; S12-2 is the documented limit | yes | green as read |
| RELIABILITY | RELIABILITY_GREEN. R13-1 Medium, R13-2 Low; R12-1 and R12-2 closed | yes | green as read |
| TEST_TRUTH: test-verification | **BLOCK** (T-1 High) | yes | **not green** |
| TEST_TRUTH: mutation gate 1 | **BLOCK** (coverage) | yes | **not green** (ruling 5) |
| TEST_TRUTH: mutation r2 (H025-H070) | **BLOCK** (22 survivors with a probe, 3 of them High) | yes | **not green** |
| DATA | DATA_GREEN. D13-L1 and D13-L2 Low; M-2, M-3 Medium (carried) | yes | green as read. D13-L1 is the same defect as F-1 and is ruled MEDIUM |
| PROVENANCE | PROVENANCE_GREEN, items 1–6. It excludes test-verification.md | yes | green as read. test-verification.md is bound by RG's recomputed digest |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE, REAL_PROTOCOL_LOCAL | yes | green for local scope only |
| Release Guardian | NOT_READY (HOLD) | yes | input only. It agrees with this verdict |

## Independent checks
- **sameTuple: TRUE.** All ten reports name the full SHA and tree. All control SHAs are ancestors (provenance §1).
- **evidenceComplete: FALSE.**
  - There is no CI run.
  - Phase-1 mutants named in the founder's own scope are UNRUN: N061 on 3.13, and N062–N070 on both Pythons (kill_table.md:41-50).
  - The H-1 named mutants X016, X017, X011, M092, M093 and T222 are UNRUN at this SHA.
  - The H050 route effect was NOT RUN.
- **noDomainMissing: TRUE.** All seven domains are present and none was substituted. The provenance gate does not cover test-verification.md; that file is bound by RG's digest. This does not change the outcome.
- **noContradictions: TRUE after stricter-wins.**
  - F-1 Medium vs D13-L1 Low → Medium.
  - R13-1 Medium vs S13-4 Low → Medium.
  - mutation-r2 says "9 equivalent (proved)", but only 6 are proved; 3 are argued (H042, H044, H069).
  - The "22 vs 32" survivor count was reconciled by RG §6: 22 + 9 + 1.
  - Mutation gate 1 says "no survivor stores card/SSN". That covers only what it ran: the backstop mutants that do store were UNRUN in gate 1.
- **independenceHeld: TRUE as read.** Every report declares fresh context and non-builder authorship, and each is digest-bound by RG and/or provenance. Ω cannot verify agent identity beyond the reports.

## Rulings

**1. Overall verdict: RED. Push-eligibility.**

What I can certify:
- At cb708b9 the shipped OOH2-PII-1 intake screen behaves correctly wherever it was measured:
  - data: 221/221 spellings × 5 paths = 1,105 cases, 0 leaks, both Pythons;
  - runtime: 477 real-HTTP cases, 0 hits;
  - security: PoCs plus a sweep over all 1,112,064 code points.
- The request hash is the hash of the kept text and could not be recovered (control found in 0.95 s).
- H-1 behaviour is unchanged (data 66/66 with okWhileChanged 0; runtime e2 W, m3 I, fifo H refused with the file unchanged; the 75b0662 control discriminates).
- No surviving bypass exists in the shipped code.

What I cannot certify:
- That the tests would catch a regression of the fix (T-1).
- That the H-1 integrity edges are still pinned at this SHA (X016, X017, X011, M092, M093, T222 UNRUN).
- CI.
- Anything live or production.

Two open Highs sit in scope:
- **OOH2-PII-1.** OPEN.md:72 says it stays open "until AEGIS confirms the intake fix and the purge/re-key step exists", and the purge/re-key step is not built.
- **OOH2-R8-MUT-EDGE** (X016, X017 High), OPEN.md:100.

As at omega8: an open High means no GREEN for any scope.

On the push: the founder's "pre-production blocker, not a push blocker" ruling is his authority over pushes. It is not part of a GREEN, and Ω certifies no push. A push of cb708b9 would be uncertified. The evidence raises no push-*safety* objection: gitleaks found 0 in two gates and two configs, there are no home paths, and only public test card/SSN values appear. A push also publishes the docs/aegis history and claude/handoff-2026-10-18-ooh-wave2.md (P-L2). mayMergeOrRelease = false.

**2. T-1 (H040, H041, H064): BLOCKING.**
- Two independent gates reproduced it on 3.12 and 3.13, scoped and on the full suite (mutation-r2; test-verification §4).
- The surviving lines are the fail-closed backstop: `_forms` at pii.py:339-348 and the `[redacted]` return at :377-378. That backstop is the core of the fix for a founder-graded High.
- Each of the three mutants brings back exactly the failure OOH2-PII-1 names: a card or SSN stored and recovered by `html.unescape`.
- T-1 is graded High by the gate that owns test truth, and R-GATE's first clause makes a High block.
- R-GATE's survivor list (money, purchase authorization, approvals, funds, tenant, ledger) does not name PII. On this item the two clauses conflict, so the stricter one wins. The founder also put this exact code first in this round's mutation scope.
- Not a bypass: the shipped code is correct. Failure class: NOT_PROVEN / GATE_NOT_GREEN.
- Only a committed founder regrade could change this ruling, and other blockers would remain.

**3. T-2 / H050: MEDIUM, non-blocking once tracked; it is not a defect at this SHA.**
- The shipped code handles the undecodable case: `forms is None or …` at pii.py:353. The TypeError exists only in the mutant.
- By reading, such a raise would reach the generic handler at api.py:377-380 and return 500 "internal error". Whether anything would be written first is NOT RUN.
- The "raise to High on a 500 on a write path" condition is about a hypothetical regression. What is missing is a test that the screen never raises on hostile input.
- It needs an OPEN.md row, and it must be shown killed with the T-1 tests.

**4. F-1 / D13-L1 (`_INTL` lookbehind, pii.py:80): MEDIUM, not High.**
- The stricter of the two grades applies.
- Aggravating:
  - the delta introduced it;
  - it fails open;
  - it was persisted in clear on 4 paths into an append-only log with no scrub;
  - the comment at pii.py:51-54 claims it "fails closed";
  - no test pins it, and its mutants (H001–H024) are UNRUN.
- Bounded:
  - only 8-digit-total international numbers right after a non-ASCII non-space character are affected;
  - numbers with 9 or more digits are caught by the cluster rule;
  - card and SSN detection and `names_a_card` are unaffected (78,154 of 78,154 identical);
  - names and refs cannot carry the character (ASCII schemas).
- Non-blocking under R-GATE once tracked. It is untracked now (ACCOUNTING_IMPURE).

**5. mutation.md coverage BLOCK under the founder's scope: PARTLY SUPERSEDED, still NOT_PROVEN.**
- r2 ran all of H025–H070 (0 UNRUN), so gate 1's 36 unrun backstop and strip_format mutants are now resolved, into real survivors (ruling 2).
- Phase 2 (H001–H024, H030–H034 outside r2, the P-series) was "if time remains". Its UNRUN status is allowed by the founder's scope and does not block by itself. It is UNRUN, not passed, so Ω certifies nothing about how well `_W`, `_DOM`, `_EMAIL_*` and `_INTL` are tested. That includes F-1's site.
- Still blocking, inside the founder's named phase 1 (`_redact_all`): N062–N070 are UNRUN on both Pythons and N061 is UNRUN on 3.13.
- Separately, the founder's re-scope left X016, X017, X011, M092, M093 and T222 unrun. These are in R-GATE scope (ledger anchoring; omega8 ruling 3).
  - OPEN.md:100 holds OOH2-R8-MUT-EDGE open "until an independent mutation gate kills them".
  - store.py changed between 99569bd and cb708b9.
  - omega12's kills at 1a90498 were superseded and do not carry forward.
  - Open High for the H-1 part of the scope.

**6. No CI: NOT_PROVEN.**
- It cannot block a push, since CI only exists after a push. It blocks GREEN, merge and release (an accounting row that is not verified).
- Local 1226/1226 on both Pythons, with identical test ids, was shown by three gates (source, reliability, test-verification). Data's 1225+1 is an explained archive artifact.
- This is local evidence only. CI7-1 and CI8-1 show that local green has not implied macOS CI green in this repo.

**7. Missing OPEN.md rows: ACCOUNTING_IMPURE, blocks GREEN.**
- OPEN.md at cb708b9 has no row for any omega13 finding:
  - source F-1..F-4;
  - security S13-1..S13-4;
  - reliability R13-1, R13-2;
  - data D13-L1, D13-L2, INFO-10..12;
  - test-verification T-1..T-3 and L-1..L-7;
  - the r2 survivors H043, H045, H046, H047, H048, H049, H050, H051, H052, H053, H054, H058..H063, H068, H070 and H031;
  - gate 1 survivors H035..H037, N050..N053, N055, N058..N061, N147, N148;
  - provenance P-L1..P-L4.
- Under "no new commit this round" this cannot be fixed at cb708b9.
- The regress raised at omega8 is still unresolved: findings first raised in a certifying round cannot already be in that round's tuple. Resolving it needs a committed founder ruling.

**8. The 99569bd RED blockers, now:**
| # | 99569bd blocker | status at cb708b9 |
|---|---|---|
| 1 | OOH2-PII-1 open High | **PARTIAL.** Intake fix verified by the security, data and runtime gates. Not test-protected (T-1). Purge/re-key step not built. New F-1 regression. Row stays open by its own terms |
| 2 | TEST_TRUTH: X016, X017, X024, X011, M092/M093 | **PARTIAL.** X024 killed. X016, X017, X011, M092, M093 UNRUN, and T222 (prior condition 2) UNRUN. New T-1 BLOCK |
| 3 | No DATA report | **CLOSED** (data.md, bound) |
| 4 | No Release Guardian | **CLOSED** (present; NOT_READY) |
| 5 | Untracked Medium/Low | **CLOSED for omega8's findings** (OOH2-R8-* at OPEN.md:89-101). **OPEN for omega13's** (ruling 7) |
| 6 | Provenance not hash-attested | **CLOSED.** Examiner-written; digest recomputed by RG. It does not cover test-verification.md, which RG bound |

Also closable at a tuple that records them: the OOH2-R12-MUT Highs N146, N156, F001, F002, plus F015 and F008, which are killed at cb708b9 by test_aegis_r12_mutation_gaps.py. H-1 behaviour for prior condition 1:
- e2 and m3 were re-witnessed by runtime.
- e3 and m2 were not named individually this round; they are covered only by data's 66-case matrix.

## Verdict: RED
- Failure classes: GATE_NOT_GREEN (TEST_TRUTH), NOT_PROVEN (CI, UNRUN mutants in scope), ACCOUNTING_IMPURE (untracked omega13 findings).
- Open High in scope: OOH2-PII-1 and OOH2-R8-MUT-EDGE.
- No SURVIVING_BYPASS in the shipped code. No TUPLE_MISMATCH. No PROVENANCE_BLOCK.
- mayMergeOrRelease: **false**. requiresFounderApproval: **true**.

## What must change for GREEN (successor tuple only; cb708b9 cannot become GREEN because the fixes need a new commit)
1. A new SHA with all seven gates fresh at it, plus a Release Guardian READY. No carry-forward.
2. T-1: an independent mutation gate on 3.12 and 3.13, whole suite, no -x, shows H040, H041, H064 and H050 killed. H043, H045, H046, H048, H049 and H051 are killed, proved equivalent, or regraded in a committed founder record. Test-verification is re-run and green (T-3 oracle gap judged by that gate).
3. The founder's phase-1 scope is complete: N062–N070 run on both Pythons and N061 on 3.13. Surviving Lows are tracked.
4. H-1 integrity: X016, X017, X011, M092, M093 and T222 killed at the new SHA on both Pythons, or a committed founder record removes them from R-GATE scope or regrades them. Then OOH2-R8-MUT-EDGE can close.
5. OOH2-PII-1 closed by its own OPEN.md:72 condition (intake fix confirmed and the purge/re-key step built and tested), or regraded below High in a committed founder record.
6. F-1 / D13-L1 tracked as Medium in OPEN.md, or fixed and re-verified by source, data and security. If pii.py:80 changes, the `_INTL` mutants of H001–H024 must run (changed-files rule).
7. OPEN.md at the new SHA has a row for every omega13 Medium and Low listed in ruling 7. This needs a committed founder ruling on the tracking regress.
8. CI green at the exact new SHA on both Pythons, with clean accounting. That requires the founder's decision to push the branch.
9. Runtime stays REAL_PROTOCOL_LOCAL. Any live or production claim needs a higher runtime level; local evidence never satisfies it.

Any change to the candidate, base, runtime, validator or workflow voids this verdict.

## Output object
```
{ tuple:{repoIdentity:"DarksiedCEO/zbm-zbc", candidateSha:"cb708b924bb01aa5543b832573a8fa56bd041b80",
         treeOid:"eba95097622438b226169c88910c81cc9a999212", baseSha:"75b0662823b867df3325d676b1dcf69abe37a425",
         runtimePin:"REAL_PROTOCOL_LOCAL darwin-arm64 py3.12.14/3.13.9"},
  scope:"OOH2-PII-1 + H-1 file integrity, services/ooh-py",
  gates:[{gate:SOURCE,verdict:GREEN},{gate:SECURITY,verdict:GREEN},{gate:RELIABILITY,verdict:GREEN},
         {gate:TEST_TRUTH,verdict:BLOCK},{gate:DATA,verdict:GREEN},{gate:PROVENANCE,verdict:GREEN},
         {gate:RUNTIME,verdict:GREEN_FOR_SCOPE(REAL_PROTOCOL_LOCAL)}] (all boundTuple: cb708b9/eba95097),
  independentChecks:{sameTuple:true, evidenceComplete:false, noDomainMissing:true, noContradictions:true, independenceHeld:true},
  runtimeClassification:"REAL_PROTOCOL_LOCAL",
  verdict:"RED",
  blockers:["T-1 High (H040/H041/H064)","OOH2-PII-1 open High","OOH2-R8-MUT-EDGE open High (X016/X017 UNRUN; X011,M092,M093,T222 UNRUN)",
            "phase-1 mutants N062-N070 (both), N061 (3.13) UNRUN","CI NOT_PROVEN","no OPEN.md rows for omega13 findings"],
  missingOrStale:["CI run","H050 route effect","test-verification.md not in provenance scope (bound by RG)"],
  mayMergeOrRelease:false, requiresFounderApproval:true }
```
