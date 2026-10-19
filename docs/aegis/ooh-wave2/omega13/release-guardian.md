# AEGIS RELEASE GUARDIAN: omega13 (AG-RELEASE-GUARD v1.0.0)

- repoIdentity: DarksiedCEO/zbm-zbc. Local branch ooh-dept-15-wave2 in worktree S/ooh2. S = the session scratchpad; <HOME> = the user's home directory.
- candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80
- treeOid: eba95097622438b226169c88910c81cc9a999212
- scope: the readiness ledger for the nine omega13 gate reports in S/aegis/omega13. This report certifies nothing. Ω decides. The founder merges.
- Last Ω: RED at 99569bd. 91ea9a1, 0f5fbe3, f5ee168 and 1a90498 were SUPERSEDED without an Ω verdict. Nothing from those runs carries forward.
- author: Release Guardian. Fresh context, read-only. I did not build or review any of this work. The only file I wrote is this report.

## RECOMMENDATION: NOT_READY (HOLD). blocking: true
By founder instruction, this still goes to Ω. Ω is final. This report is a ledger, not a gate.

## 0. Binding (commands run and what they returned)
- `git -C S/ooh2 rev-parse HEAD HEAD^{tree}` returned cb708b924bb01aa5543b832573a8fa56bd041b80 and eba95097622438b226169c88910c81cc9a999212. Branch: ooh-dept-15-wave2. `git status --porcelain`: 0 lines.
- `shasum -a 256 S/aegis/omega13/*.md` (I recomputed every digest):

| report | recomputed sha256 | builder's value | header binds full SHA + full tree |
|---|---|---|---|
| source.md | 6b23dc73c218081aca46c120e29c73d48b8f5cb59ab513a4d97fdce31b117b7f | MATCH | yes (lines 3-4) |
| security.md | a6a219d5ba24aa87c0e93172a0917f4c13fefde268c9e081a890f2f311dae8ff | MATCH | yes (lines 4-5; gate object line 217) |
| reliability.md | a682fb3a996e060f8f494ec7b8c23d69a914264e03f06283943a34db5e4e534f | MATCH | yes (lines 2-3) |
| data.md | bf2a6a4ce60fae4fc1b87548a55f5768cb22e9bbf28ff7944ee0ce29978e5453 | MATCH | yes (lines 3-4) |
| runtime.md | de07e8538a46517721b580131425f3a13b0a7c04909762c44411774b62d6d6df | MATCH | yes (line 3) |
| mutation.md | faa98c2e4bd4bb6abeb9fb126e5bbb8a9ab4c9afb54a8d214ee874b5d52a50b3 | MATCH | yes (lines 4-5) |
| mutation-r2.md | c87a1a347a1d93303fca86ae772aed16f3f1eaf18bb6ba7da56b1b9fec5669c1 | MATCH | yes (lines 2-3) |
| test-verification.md | ff904db312e2d230bb7239f58b8002a70306d69cecd22d515eb658942b429536 | MATCH | yes (line 3) |
| provenance.md | 8840056a7360e3f7436c019eecb4f7173dfbc78d34ae9dfc4c735eee67c0c29a | MATCH | yes (lines 4-5) |

- 9 of 9 digests match. No report names a different SHA as its subject. Other SHAs appear only as labelled controls (1a90498, 75b0662, 99569bd and others), and provenance §1 shows all of them are ancestors.
- Provenance ran before test-verification.md existed. Its §5 row for that file reads "NOT PRESENT … NOT CHECKED". This report binds test-verification.md by the digest in the table above. Its header names the full candidate SHA and tree. Its hash pre-check lists mutation.md faa98c2e…50b3 and mutation-r2.md c87a1a34…69c1, and both equal the current files.
- No STALE_VERDICT and no TUPLE_MISMATCH.

## 1. Per-gate verdicts (quoted from each report)
| gate | verdict as written | tupleMatches | counts as GREEN here |
|---|---|---|---|
| Source | "verdict: **SOURCE_GREEN** (no CRITICAL/HIGH; one MEDIUM, three LOW, none blocking)" | true | GREEN_FOR_SCOPED_CLAIM |
| Security | "## Verdict: SECURITY_GREEN (for the scoped claim). No CRITICAL or HIGH. Non-blocking." | true | GREEN_FOR_SCOPED_CLAIM (no pytest run by this gate) |
| Reliability | "## VERDICT: RELIABILITY_GREEN (for the scoped claim; two non-blocking findings)" | true | GREEN_FOR_SCOPED_CLAIM |
| Data | "VERDICT: DATA_GREEN (scoped to the claims above at cb708b9 / tree eba95097; non-blocking: D13-L1 LOW, D13-L2 LOW, M-2 MEDIUM, M-3 MEDIUM)" | true | GREEN_FOR_SCOPED_CLAIM |
| Runtime | "**Classification: REAL_PROTOCOL_LOCAL** · **VERDICT: RUNTIME_GREEN_FOR_SCOPE**" | true | GREEN_FOR_SCOPED_CLAIM (local only, not live) |
| Mutation (gate 1) | "## Verdict: BLOCK (coverage, not a bypass)" | true | **BLOCK** |
| Mutation r2 (H025-H070) | "## VERDICT (range H025-H070): BLOCK" | true | **BLOCK** |
| Test verification | "## VERDICT: BLOCK (not TEST_GREEN)" | true | **BLOCK** |
| Evidence provenance | "**PROVENANCE_GREEN** for the scoped claim: items 1–6 … It excludes test-verification.md, which was not present." | true | GREEN_FOR_SCOPED_CLAIM (test-verification.md bound here instead) |

The worst required verdict wins: **BLOCK** (3 of 9 gates).
- Required gate MutationFuzz: BLOCK.
- Required gate TestVerification: BLOCK.
- Required gates Security, Reliability and EvidenceProvenance: GREEN for their scoped claims.

The builder's reading of each verdict and finding list matches what the reports say, including source F-1 Medium with F-2..F-4 Low, security S13-1..4 Low, reliability R13-1 Medium with R13-2 Low, and test-verification T-1 High, T-2/T-3 Medium and L-1..L-7 Low.

## 2. CI
- `gh run list -R DarksiedCEO/zbm-zbc --commit cb708b9…` returned `[]`.
- `gh api repos/DarksiedCEO/zbm-zbc/commits/cb708b9…/check-runs` returned HTTP 422 "No commit found for SHA".
- `git ls-remote origin refs/heads/ooh-dept-15-wave2` returned nothing. This agrees with provenance §1: the branch is on no remote.
- **CI status: NOT_PROVEN.** No CI run exists for this SHA. This is not CI_RED.
- Local suite results are evidence of local runs only, not of CI:
  - source §10: 1226 passed on 3.12 and on 3.13;
  - reliability §1: 1226 passed on 3.12 and on 3.13;
  - test-verification §1: 1226 of 1226 collected, executed and passed on 3.12 and 3.13, with identical ids.

## 3. Critical / High findings
No CRITICAL anywhere. HIGH:
1. **T-1 High (test-verification §5): MUTANT_SURVIVED / NEGATIVE_CONTROL_WEAK.** Mutants H041, H064 and H040 survive the scoped run and the full suite on both Pythons, and each one stores a card or SSN.
   - Test-verification reproduced all of them itself (§4: 413 scoped passed, 1226 full passed).
   - Mutation-r2 grades the same three "High (OOH2-PII-1)":
     - H041 and H064 store text that one `html.unescape` turns into a card or SSN;
     - H040 needs a decoder with 17 or more passes.
   - Disposition: BLOCK.
2. **OOH2-PII-1 itself** is High by founder ruling (OPEN.md:72 at cb708b9): "blocks production, not the push of ooh-dept-15-wave2". T-1 shows that the fix's backstop is not pinned by any test.
   - mutation-r2 says it reports BLOCK "on the OOH2-PII-1 grading, not on R-GATE". None of the survivors is in money, purchase authorization, approvals, funds, tenant isolation or the ledger.
- Conditional: **T-2 Medium (H050)**. screen_free_text can raise TypeError on hostile input. Test-verification says to "raise to High if the route returns 500 on a store write path". The route-level effect is NOT RUN, so the condition is undecided.
- Source F-1 says: "SOURCE_GREEN is wrong if … Ω rules that F-1 is HIGH". That is Ω's ruling to make.

## 4. Medium / Low findings (each needs an OPEN.md row under R-GATE)
Where two gates found the same defect at different grades, the worse grade is listed.
- **_INTL lookbehind regression: Medium.**
  - source F-1 grades it MEDIUM; data D13-L1 grades it LOW. They are the same defect at pii.py:80.
  - Effect: an 8-digit international number written right after a non-ASCII non-space character is stored in clear in vendor_text, and data measured it on 4 persistence paths.
  - The H001-H024 mutants, which cover the `_INTL` lookbehinds, are UNRUN (§5).
- **Screen cost under the service lock: Medium.** Reliability R13-1 grades it MEDIUM (normalisations go from 6 to 30 per request; the screen is x3 to x13 slower). Security S13-4 grades the same regression LOW.
- Mutation-r2 survivors graded Medium:
  - H043, H046 and H049 let a 10-digit cluster through;
  - H048 and H051 leave an e-mail residue;
  - H050 crashes the screen (the same issue as T-2);
  - H045 is graded Low-Medium.
- Mutation gate 1 survivors:
  - N058-N061 (the `_redact_all` post-loop check): LOW-MEDIUM;
  - N050-N053, N055, H035-H037, N147 and N148: LOW (test gaps);
  - F007, N054, N056 and N057: equivalent by reading.
- Mutation-r2 Low survivors: H047, H052, H053, H054, H058, H059, H060, H061, H062, H063, H068 and H070. H031 is a survivor with no probe and no proof.
- T-3 Medium: the oracle gaps (M-1 in §3 of test-verification), which are the root cause of T-1. T-2 Medium: H050.
- M-2 and M-3 Medium: carried and unchanged. They already have rows OOH2-DATA-M2 and OOH2-DATA-M3 (OPEN.md:142-143).
- Low:
  - source F-2, F-3, F-4;
  - security S13-1, S13-2, S13-3 (plus S13-4 above), and the carried S11-3 and S11-4;
  - reliability R13-2;
  - data D13-L2, which is the same composition-order class as S13-1;
  - test-verification L-1 to L-7;
  - provenance P-L1 to P-L4;
  - runtime O10-1 and O10-2 (carried).
- Documented limit, not a finding: S12-2 / OOH2-R12-SEC-S2 (URL, JS and CSS escapes). The row exists at OPEN.md:137.

## 5. UNRUN / NOT RUN / NOT CHECKED (as the reports state them)
- **Mutation gate 1:**
  - 187 of the 226-list are UNRUN on 3.12 and 188 on 3.13, because the 90-minute clock ran out.
  - Phase 2 was entirely UNRUN: H001-H024 (`_W`, `_DOM`, `_EMAIL_WORD`, the `_INTL` lookbehinds), the 113 P-series mutants, the stale P015, P020 and P120-P123, and N061 on 3.13.
  - Outside the founder's scope this round, also UNRUN: T222, X016, X017, X011, M092, M093, M011, M013, X008, M080, M091, M044, N025, N026, N029, N030, N031, N033, N152, N178 and N179, plus the 132 AST mutants.
  - mutation-r2 later ran H025-H029 and H040-H070.
- **Mutation r2:**
  - UNRUN: 0 in its range.
  - NOT RUN: the probes through a running service or into the log; H035-H039, taken from gate 1 without re-verification; attack classes other than mutation.
  - Not proven: the equivalence of H042, H044 and H069 rests on argument and fuzz only (§NOT RUN, §Equivalent).
- **Test verification:**
  - NOT RUN: the 18 other r2 survivors, whether the HTTP route turns H050's TypeError into a 500, and random test order (L-7, no plugin installed).
- **Security:**
  - pytest was not run, because the machine was at its process limit.
  - Also NOT RUN: Python 3.14 / Unicode 16, the bidi display construction (THEORETICAL), and F4 (the 13 other Ref fields, out of scope).
- **Source:**
  - NOT RUN: an HTTP witness, mutation, Python 3.14, the ledger/store/integrity code, a check that any consumer decodes entities, and an exhaustive search past the 5-pass and 16-pass caps.
- **Reliability:**
  - NOT RUN: quiet-machine timings, Unicode 16/17, Linux, uvicorn under real load, multi-client lock contention, 30x loops, SIGKILL / disk-full.
- **Data:**
  - NOT RUN: the device node (mknod EPERM), L-A, recovery of old INFO-3 lines, INFO-7 (3.14), the D13-L1 sweep on 3.13 (3.12 only), writers other than unit add (code inspection only), concurrency, and ledger build provenance.
- **Runtime:**
  - LIVE_PROVEN and PRODUCTION_PROVEN are not claimed. No pytest was run by this gate.
  - The PII sub-items listed in its §NOT RUN were not run: other endpoints, other face text fields, e-mail and phone shapes, the /audit/evidence view, the request_sha cache under concurrency, and others.
  - Store modes beyond e2 W, m3 I and fifo H were not run.
  - D2: the ledger binary hash differs from omega12's. Not investigated.
- **Provenance:**
  - NOT CHECKED: test-verification.md (bound here instead), the denominator and method of each claim, an inventory of card/SSN test data, and a re-extraction of gl.tgz.
- **CI:** NOT_PROVEN (§2).

## 6. Count check: mutation-r2's "22" against "32"
- I recomputed from `S/aegis/omega13/mutation-r2/raw/results.jsonl` (148 records: 2 NOOP plus 146 H records; 64 full-suite runs, all rc 0).
- 41 H ids were run: H025-H034 and H040-H070. The range H025-H070 has 46 mutants, so this is 46 minus the 5 (H035-H039) taken from gate 1.
- Per Python:
  - killed in the scoped run: 9 (H025, H028, H030, H034, H055, H056, H057, H065, H067), identical on 3.12 and 3.13;
  - survived the scoped run: 32, and all 32 also pass the full suite (survivors that fail the full suite: none).
- The report's tables account for the 32 as 22 survivors with a probe + 9 "equivalent" (H026, H027, H029, H032, H033, H042, H044, H066, H069) + H031 (no probe). 22 + 9 + 1 = 32. **Reconciled.** It agrees with the builder's note.
- Two caveats:
  - The counts table says "equivalent (proved) 9", but the same report says H042, H044 and H069 are "Not proven … beyond argument and fuzz". Strictly, that is 6 proved and 3 argued.
  - Test-verification's §5 note "22 … and 32 … I did not reconcile it" is resolved by this section.
- Including gate 1's surviving H035, H036 and H037, the whole H025-H070 range has 35 full-suite survivors and 11 kills (the 9 above plus H038 and H039).

## 7. OPEN.md at cb708b9 (`git show cb708b9:docs/findings/OPEN.md`, 230 lines)
- Searched for `R13|omega13|S13-|R13-|D13-|T-1|H040|H041|H064|H050|_INTL|lookbehind`: **no row exists for any omega13 finding.** That covers F-1..F-4, S13-1..4, R13-1/2, D13-L1/L2, T-1..T-3, L-1..L-7, the r2 survivors and P-L1..4.
- Rows that do exist: OOH2-PII-1 (High, :72), OOH2-R12-* (:133-141), OOH2-DATA-M2/M3 (:142-143) and OOH2-PII-1-FP (:123).
- This is expected: the founder ruled no new commit this round. Under R-GATE it is still an open ledger gap (UNRESOLVED_FINDING): every Medium and Low above needs an ID row before release.

## 8. Blockers (failure classes)
1. **NOT_PROVEN / UNRESOLVED_FINDING, High:** T-1. H040, H041 and H064 survive and store a card or SSN. Test-verification BLOCK.
2. **NOT_PROVEN, MutationFuzz:** mutation-r2 BLOCK. 22 survivors with a probe, three of them High.
3. **NOT_PROVEN, MutationFuzz coverage:** mutation gate 1 BLOCK. 187 or 188 mutants UNRUN, including the H001-H024 `_INTL` lookbehind mutants, where F-1 sits.
4. **CI NOT_PROVEN:** no CI run exists for cb708b9, and the commit is not on GitHub.
5. **UNRESOLVED_FINDING (ledger):** no OPEN.md row exists for any omega13 Medium or Low finding.
6. **Open conditional:** T-2 (H050). The route-level 500 was NOT RUN, so the possible upgrade to High is undecided.

Not blockers:
- digests (9/9 match);
- tuple binding (9/9 exact);
- stale verdicts (none);
- the provenance gap for test-verification.md, which §0 closes by digest.

## Output object
```
{ candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80, treeOid: eba95097622438b226169c88910c81cc9a999212,
  perGate: [
    {gate: Source, verdict: SOURCE_GREEN, tupleMatches: true},
    {gate: Security, verdict: SECURITY_GREEN, tupleMatches: true},
    {gate: Reliability, verdict: RELIABILITY_GREEN, tupleMatches: true},
    {gate: Data, verdict: DATA_GREEN, tupleMatches: true},
    {gate: Runtime, verdict: RUNTIME_GREEN_FOR_SCOPE, tupleMatches: true},
    {gate: MutationFuzz (gate 1), verdict: BLOCK, tupleMatches: true},
    {gate: MutationFuzz (r2 H025-H070), verdict: BLOCK, tupleMatches: true},
    {gate: TestVerification, verdict: BLOCK, tupleMatches: true},
    {gate: EvidenceProvenance, verdict: PROVENANCE_GREEN, tupleMatches: true} ],
  ci: NOT_PROVEN (no run; commit not on remote),
  blockers: [T-1 High (H040/H041/H064), mutation-r2 BLOCK, mutation gate-1 coverage BLOCK, CI NOT_PROVEN,
             no OPEN.md rows for omega13 findings, T-2 conditional],
  recommendation: HOLD (NOT_READY; forwarded to Ω by founder instruction), blocking: true }
```
