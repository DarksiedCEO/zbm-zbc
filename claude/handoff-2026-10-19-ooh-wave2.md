# Handoff: OOH / DOOH (dept 15) wave 2, parked at Ω RED on cb708b9 (2026-10-19)

## Status in one line
OOH wave 2 is **parked at RED** (founder, 2026-10-19). Ω ruled RED on cb708b9. Nothing is certified and nothing is merged;
`main` is untouched. The branch `ooh-dept-15-wave2` is pushed to origin (branch only) on the founder's instruction. All OOH
work is stopped. Every open `OOH2-*` line in docs/findings/OPEN.md is a pre-production blocker.

## Branch and commits
- Repo DarksiedCEO/zbm-zbc, branch `ooh-dept-15-wave2`, forked from `integration-2026-09-24` at 84a5377.
- Candidate Ω ruled on: **cb708b924bb01aa5543b832573a8fa56bd041b80** (tree eba95097622438b226169c88910c81cc9a999212).
  **Certified SHA: none.**
- Evidence commit: **3c28fbe6af0373fdae1604546b37c4854738cc34** (docs/aegis/ooh-wave2/omega13/ and the OPEN.md rows).
- This handoff is in the commit after 3c28fbe, at `claude/handoff-2026-10-19-ooh-wave2.md`, with a copy at
  `docs/aegis/ooh-wave2/handoff-2026-10-19-ooh-wave2.md`.
- Chain after 75b0662: da7b960, eae7a86, 8b613e3, 99569bd (H-1 fix; Ω RED), 010ef48, 49a7f3a, 91ea9a1, 0f5fbe3, cd1924c,
  f5ee168, 6daccf6, 1a90498, 5ac1909, cb708b9, 3c28fbe, then this handoff commit. 91ea9a1, 0f5fbe3, f5ee168 and 1a90498 were
  SUPERSEDED without Ω on the founder's instruction (omega9..omega12/SUPERSEDED.md).
- Ω history: RED at 4a0a4b4 (omega5), RED at 99569bd (omega8), RED at cb708b9 (omega13).
- CI: none had run for cb708b9 before this push. The push will start CI on the branch; its result is not part of any Ω round.

## Test counts
- cb708b9: 1226 collected, 1226 passed on Python 3.12.14 and 3.13.9, same test ids on both (source, reliability and
  test-verification gates). The scoped PII and integrity files (439 tests) were run twice on each Python and in reverse file
  order with no flake. Random order not run (plugin not installed). docs/test-counts.md says 1226.
- Before the push: hygiene check, gitleaks and both test suites re-run on the tip (see the push record in the session).

## Gate verdicts at cb708b9 (docs/aegis/ooh-wave2/omega13/)
| Gate | Verdict | Report sha256 |
|---|---|---|
| SOURCE | SOURCE_GREEN (F-1 Medium; F-2..F-4 Low) | 6b23dc73c218081aca46c120e29c73d48b8f5cb59ab513a4d97fdce31b117b7f |
| SECURITY | SECURITY_GREEN (S13-1..S13-4 Low) | a6a219d5ba24aa87c0e93172a0917f4c13fefde268c9e081a890f2f311dae8ff |
| RELIABILITY | RELIABILITY_GREEN (R13-1 Medium, R13-2 Low) | a682fb3a996e060f8f494ec7b8c23d69a914264e03f06283943a34db5e4e534f |
| DATA | DATA_GREEN (D13-L1, D13-L2 Low; M-2, M-3 carried) | bf2a6a4ce60fae4fc1b87548a55f5768cb22e9bbf28ff7944ee0ce29978e5453 |
| RUNTIME | RUNTIME_GREEN_FOR_SCOPE (REAL_PROTOCOL_LOCAL) | de07e8538a46517721b580131425f3a13b0a7c04909762c44411774b62d6d6df |
| MUTATION run 1 | BLOCK (coverage; 187/188 of 226 UNRUN at the 90-min cap) | faa98c2e4bd4bb6abeb9fb126e5bbb8a9ab4c9afb54a8d214ee874b5d52a50b3 |
| MUTATION run 2 (H025-H070) | BLOCK (32 full-suite survivors; H040/H041/H064 card/SSN; H050 crash) | c87a1a347a1d93303fca86ae772aed16f3f1eaf18bb6ba7da56b1b9fec5669c1 |
| TEST-VERIFICATION | BLOCK (T-1 High; T-2, T-3 Medium; L-1..L-7 Low) | ff904db312e2d230bb7239f58b8002a70306d69cecd22d515eb658942b429536 |
| PROVENANCE | PROVENANCE_GREEN (P-L1..P-L4 Low) | 8840056a7360e3f7436c019eecb4f7173dfbc78d34ae9dfc4c735eee67c0c29a |
| Release Guardian | NOT_READY | fa81726446e3eeae4fbc92b2b7d07a211f7dd60b6baaca60df50bd18711cb4b0 |
| **Ω** | **RED** | omega.md 171f0b74d65ec4f6b74a072cdc05a61ef061d4d0ab16181c09c50c084c75c166 (saved verbatim by the builder) |

Every gate wrote its own report and returned its hash; the builder checked each one. The committed copies replace home paths
and names, so their hashes differ where such a string occurred (omega13/MANIFEST.md lists both).

## Why it is parked at RED
The shipped OOH2-PII-1 screen behaves correctly wherever it was measured (data 1,105 cases, runtime 477 HTTP cases, security
sweep over every code point: no leak). Ω's blockers are about test strength, the unbuilt purge step and evidence:
1. **T-1 High** (OOH2-R13-TV-T1): mutants H040, H041, H064 of the fail-closed backstop survive the full suite; each would let
   a card or SSN be stored. H050 (a crash on hostile input) is unpinned too (OOH2-R13-TV-T2). Root cause: the test oracles
   (OOH2-R13-TV-T3).
2. **OOH2-PII-1 High** stays open: the purge/re-key step before real client data is not built.
3. **F-1 Medium** (OOH2-R13-SRC-F1): a regression in cb708b9. The `_INTL` lookbehind at pii.py:80 misses an 8-digit
   international number after a non-ASCII punctuation mark. Fix: an ASCII-only lookbehind, then run the `_INTL` mutants.
4. **OOH2-R8-MUT-EDGE High**: the H-1 mutants X016, X017, X011, M092, M093 and T222 were not run at cb708b9.
5. Founder phase-1 mutants N062-N070 (and N061 on 3.13) UNRUN (OOH2-R13-MUT-UNRUN).
6. No CI at the exact SHA (OOH2-R13-CI).

## What a GREEN needs (Ω's list, for the wave that resumes OOH)
A new commit with all seven gates fresh at it and Release Guardian READY, plus: tests that kill H040, H041, H064 and H050
(and kill, prove equivalent or regrade H043, H045, H046, H048, H049, H051); N061-N070 run; X016, X017, X011, M092, M093,
T222 killed; the purge/re-key step built and tested (or OOH2-PII-1 regraded in a committed founder record); F-1 fixed or
tracked; OPEN.md rows for every finding of that round; CI green at the exact SHA on both Pythons.

## Open questions for the founder
1. Purge/re-key step: build it in the next OOH wave, or regrade OOH2-PII-1?
2. Ω's tracking regress (findings first raised in a certifying round cannot be in that round's tuple) needs a committed
   founder ruling, or every round ends ACCOUNTING_IMPURE.
3. H-1 stays an open High in seo, sales, bizdev, service and influencer (OOH2-RT-H1-*). When?
4. The 13 other reference fields (OOH2-R9-SEC-F4) and structured vendor fields (OOH2-PII-1-STRUCT): next OOH wave?
5. SEO add-ons (Perplexity adapter) were held until after OOH. With OOH parked, may they start?

## Locked decisions (unchanged)
Max single purchase $10,000; Andre's authorization lasts 7 days; principal vs agent UNDETERMINED, so purchases stay blocked
until a CPA/counsel confirms; client pays 100% first and Finance (31) verifies cleared funds; clients pay by ACH, vendors by
ACH or check, never card; markup disclosed and set per deal from evidence (15% only a suggestion); the client hub never shows
vendor cost; one open order per face per overlapping flight across tenants; confirmed hold before purchase; Andre is OOH
Director, Buyer and signer, agents never commit money or contracts; measurement providers and the programmatic DOOH platform
not chosen (ports only).

## Session housekeeping
All background agents, mutation runners, test servers and wait loops of this session were stopped before the push. No gate
is running. Scratchpad worktrees (ooh2, pii-fix*, review-*) are local only and can be removed with `git worktree prune`
after the session.
