AEGIS round 26b re-adjudication: final verdict (AG-AEGIS-OMEGA)

VERDICT: INSUFFICIENT_EVIDENCE (charter schema: BLOCKED). Certification is withheld for d80adc5.
R-GATE, plain answer: no Critical or High finding is open on the merits after the CI4-2 ruling below. One record item does block merging d80adc5 exactly as it stands: its own docs/findings/OPEN.md still lists CI4-2 and CI5-1 as open High. Closing them takes a commit, which makes a new SHA. The merge needs founder approval in every case; nothing here grants it.

I ran nothing. I read the seven reports, the raw run JSON, raw job logs and candidate files.

## SUBJECT

- Repo DarksiedCEO/zbm-zbc, candidate d80adc5528ddad4f27427098efe2beee32ee5d6a, tree 5c059f868fe4426859c737170ae75ea9a94f351e, base a6aee4e9fe78bb58a94243acce406d2e7b702387 (integration-2026-09-24).
- Scope: fix wave 26b (a6aee4e..d80adc5), intended merge into integration-2026-09-24, not main.
- runtimePin: none supplied.
- Runtime level reached: GitHub-hosted CI (ubuntu-24.04, macos-26, Docker on ubuntu) plus local macOS probes. No live or production level, and none is claimed.
- Local fix26b is 08a8b8d, one docs-only commit ahead and unpushed. It is not the subject and this verdict does not cover it.
- This verdict supersedes the prior INSUFFICIENT_EVIDENCE on 4c3a21a by reference. It certifies nothing beyond the tuple above.

## CONDITIONS OF THE PRIOR VERDICT

| # | Condition | Result | Deciding evidence |
|---|---|---|---|
| 1 | Exact commit pushed; CI bound to it; every required job green | MET | `ci6-run-37135247923.json`: headSha d80adc5, conclusion success, 53 jobs all success (I enumerated them). Provenance: attempt 1, no re-runs, only run on this SHA. Raw logs I read: hygiene-static 51 + 10 tests OK with every `test_r4_*` case ok on Linux, lint 0 violations; delivery-py 724 passed / 3 skipped on both ubuntu legs and 723 / 4 on macos-26; ledger-rust 117 on Linux and 116 on macos-26; docker-live `3 passed`, `3 test cases, 0 skipped`. |
| 2 | Diff from 4c3a21a touches only the report and OPEN.md | NOT MET as written | Provenance: four more files changed (ci.yml timeout 60 to 120, and three test files in delivery-py, detection-py, orchestrator-go). No product source changed. All four were reviewed at d80adc5. |
| 3 | Mutation-fuzz report bound to the SHA | MET | `mutation-fuzz.md`, own worktree at d80adc5, 88 mutants. Result NON_BLOCKING_FINDING. |
| 4 | Provenance, reliability, data reports on the same tuple | PARTIAL | Provenance PASS and data GREEN (scoped). Reliability is bound but returned YELLOW: 6 of 14 surfaces executed. |
| 5 | CI3-1..CI3-6 confirmed by their CI legs | MET in substance, record wrong | All six legs are green in run 37135247923 on the candidate. The OPEN.md rows cite other runs (finding AO-1). |
| 6 | All 18 Medium/Low findings entered, severities unchanged | MET | OPEN.md lines 30-48: R26B-RT-1..7, SEC-1..5, TT-1..6 present with the prior severities. |
| 7 | Go, dashboard (Node 22) and go vet SHA-bound with exit codes | MET | Both orchestrator-go legs: Vet step success, `tests counted: 61; skips: 0`. Dashboard: `tests counted: 27`, ci.yml pins Node 22. Checkout lines show d80adc5. |

## CI4-2 RULING

**Ruling: accepting an early close does not weaken the property the test states, and the tolerance is required for a correct server. The change did remove accidental detection of three wrong-server states, and the test's oracle is weak. CI4-2 may close as a High; the residual is Medium and must be opened in the same commit.**

How the five positions reconcile, from their evidence:

- **The accepted set of server behaviours did not change.** `_read_response` (lines 118-137, read by me) returns status 0 on a clean EOF, so the old body already passed a close with no answer whenever the close arrived cleanly. The new body adds only two exception classes on send and one on read (test-truth A3, security section 2).
- **The old body was not a usable oracle.** Unmutated, it failed 10 of 20 runs (mutation-fuzz) and 3 of 5 (red team RT2-4). Its failures under silent close, crash and poison (red team M2, M2b, M3, M10: 3/3) are the same exception it raised against the correct server. Mutation-fuzz saw the same class (D8) fail only 3 of 5. So mutation-fuzz is right that no reliable kill was lost.
- **Test-truth and red team are also right.** Those three states now pass every time (test-truth stubs 10/10; red team 3/3), where before they failed most of the time. That is a real loss of incidental coverage.
- **Reliability and security are right that the close is a legitimate refusal.** The server writes a 400, sends FIN, drains at most 64 KiB or 1 s, then closes; a client still sending gets an RST by design (`graceful_close.py:11-12`). The real server answered 400 in every probe: 100/100, 5/5, 220/220, 30/30 and 200/200 concurrent, across four reviewers.
- **What still has teeth.** Every mutant that serves the over-cap line with 200 fails the new test deterministically: red team M1b and M7, mutation-fuzz D6 and D7, test-truth mutant 1, security's no-middleware variant.

Disposition:

- **CI4-2 closes** as High once a commit records this ruling. The High was a red `required` leg; `python-tests (detection-py, 3.13, macos-26)` is green on d80adc5 (504 passed, 0 skipped, read by me), and the new body cannot fail from the send race.
- **Severity of the residual: Medium.** Two reviewers graded it Medium and three Low; the stricter grade stands. It is not High because the control was observed working on the candidate by four reviewers, the served-200 defect is still caught, and the `!= 200` oracle predates the wave.
- **Limit of the ruling:** every probe was macOS. Reliability notes a Linux client may lose the 400 to the RST; nobody tested it. The CI logs cannot show which branch the test took.

Follow-ups that must be opened with the closure:

| id | Severity | Finding | Closure condition (reviewers' falsifiers) |
|---|---|---|---|
| CI4-2b | Medium | The long-query test passes a server that drops the connection silently, crashes, or refuses everything afterwards; no live test pins the documented 400/431 or that the server survives | A live test that fails on red team M2, M3 and M10 |
| R26B-D-SEC-1 | Medium | The parser-level head cap is proven by no test: raised to 64 MiB or 256 MiB, the whole detection suite still passes 504/504 | A test that fails on security's `parser_cap=268435456 middleware=mw` variant |
| CI4-2c | Low | The sibling header-cap test (lines 427-437) has the same oracle | Same as CI4-2b |
| RT2-4 | Low | OPEN.md's "the race itself does not reproduce here" is false | Text corrected |
| RT2-5 | Low | The next test's `/health` check can run zero times | At least one successful `/health` required in the loop |

## FINDINGS

**Open Critical or High on d80adc5 after the ruling: none on the merits.**
- CI4-2: closes per the ruling above.
- CI5-1: its stated proof is satisfied on the candidate. Both orchestrator-go legs are green in run 37135247923, and test-truth and red team each quote `--- PASS: TestSlowBodyIsCutOffBeforeAnyScanAndOthersAreServed` on both. The candidate's register still says "closure pending CI"; the closure exists only in unpushed 08a8b8d.

**New findings to enter in OPEN.md, de-duplicated (26 entries).** Where reviewers disagreed on severity I took the stricter. I lowered none.

| id | Same finding as | Severity | One line |
|---|---|---|---|
| CI4-2b | RT2-1, MF-2, R26B-RL-1, R26B-D-SEC-2 | Medium | Long-query test accepts silent close, crash and poisoned server; 400/431 and survival unpinned (three reviewers said Low; Medium kept) |
| R26B-D-SEC-1 | RT2-2, MF-1 | Medium | Parser head cap untested; the two layers mask each other |
| RT2-3 | relates to W26B-4 | Medium | 120-minute delivery limit is inside observed macOS variance (CI #4 pace projects to 115-132 min); test-truth called it "well inside", the stricter reading stands |
| MF-8 | | Medium | Hygiene L4 nested `launch_guard.py` glob untested; delivery-py's copy could drift unseen |
| CI4-2c | | Low | Sibling header-cap test has the same `!= 200` oracle |
| RT2-4 | | Low | False "does not reproduce here" statement in the CI4-2 row |
| RT2-5 | | Low | `test_slow_request_head…` can skip its `/health` assertion |
| RT2-6 | MF-4 | Low | Go slow-client tests enforce a lower bound only (about 1-2 ms resolution, no upper bound); a `main()`-level override escapes the pin test |
| RT2-7 | | Low | CI4-1's `wait_idle` fix is unverifiable locally and rests on CI; replay of a still-live run is no longer exercised |
| W26B-T1 | | Low | No `wait_idle` between the replay and the final event count |
| MF-3 | | Low | Detection head deadline and keep-alive values unpinned |
| MF-7 | | Low | CF encoding regex: hex width and case untested |
| MF-9 | | Low | Hygiene `_sentence` and `lint_expected_skips` sub-branches untested |
| MF-10 | | Low | SIGTERM disposition restore untested per service |
| MF-11 | | Low | Ledger harness pid, time and atexit-cleanup mutants survive |
| R26B-RL-2 | | Low (provisional) | A chunked body of about 262 KiB may reserve the whole 4 MiB small-model pool with no wait bound. Reasoning only, never executed; data left the same question unexamined. I keep the reporter's grade but it is unproven in either direction |
| R26B-RL-3 | | Low | `is_non_utf8_file` reads whole files |
| R26B-RL-4 | | Low | `_environ_of` returns empty on sysctl failure, a silent orphan miss |
| D-26B-2 | | Low | A ledger log with a non-default name inside the tree is no longer git-ignored |
| D-26B-3 | | Low | Live-run evidence is deleted by default, including after a failed run |
| D-26B-4 | | Low | A refused replay of a cancelled admission still appends 6 evidence events |
| R26B-D-SEC-3 | | Low | Effective head bound is about 32 KiB, not 16 KiB |
| R26B-D-SEC-4 | | Low | pip-audit and npm audit print no package count; gitleaks skips merge diffs |
| AO-1 (mine) | red team section 5, provenance observations 1-2 | Low | Eight closure rows cite runs on other SHAs that concluded failure (detail below) |
| AO-2 (mine) | | Low | `FIX_WAVE_26B_REPORT.md:281` at d80adc5 says the CI5-1 fix 700fce6 is "local, not pushed"; it is an ancestor of the pushed candidate |
| AO-3 (mine) | | Low | Ten open rows are still owned by "wave 26" after wave 26b (F-2, N25-I-2, TG-1, DLV-CLAMP, W25-EA-1/2/3/9, C4-5, H9-R6m); R-GATE says the next wave fixes them. Most need a founder ruling |

Already registered, annotate only: MF-5 and data D-CHK-2 are R26B-RT-5; MF-6 is R26B-RT-4; D-26B-1 is R26B-RT-6 (reproduced again by reliability, 29 ValueErrors, no leak).

Closable by run 37135247923: R26B-TT-1 (R4 cases green on Linux) and R26B-TT-2 (Go, dashboard and vet now SHA-bound).

**Records citing the wrong evidence (AO-1):**
- CI3-2, CI3-3, CI4-1, CI4-3 are "proven by CI #5 (run 37129375575 … @ b24ffde)". That run concluded failure and is a different SHA.
- CI3-1, CI3-4, CI3-5, CI3-6 are "proven by CI #4 (run 37125312276 … @ d40de72)". Also a failure run on a different SHA.
- All eight legs are green in run 37135247923 on d80adc5, so the closures hold; the citations must move to that run.

**Contradictions between reports (stricter reading applied):**
- Test-truth says the CI race "did not reproduce here", yet its own table shows send errors in 91 of 100 trials. What it did not reproduce is loss of the 400.
- Red team's baseline row has the old test passing 3/3, while its RT2-4 has it failing 3 of 5 and mutation-fuzz 10 of 20. The runs used different `-k` selections.
- OPEN.md and test-truth say Go's ReadTimeout runs "from accept"; reliability says it starts when the server begins reading. The lower-bound argument holds either way.
- Data corrects the lead's brief: `src/persistence.rs` is in the diff, though only its test module.

**Assertion-laundering risks:**
- The implementer closed eight Highs itself on runs that were not on the candidate, before this adjudication.
- 08a8b8d declares "CI5-1 closed" and "53/53" ahead of any ruling. The evidence supports it, but the closure was self-issued.
- The prior verdict I was given is the lead's condensed paraphrase, not the adjudicator's text. I used it only for the list of conditions.
- The seven report files were extracted by the lead. I cannot confirm they equal the hand-backs.

## GATES

Required level for every gate is E3. No evidence is E5.

| Gate | Result | Observed | Note |
|---|---|---|---|
| Provenance | PASS | E3; SHA binding E4 | SHA, tree and parent agree across local git, ls-remote and the API; I confirmed headSha in the raw JSON and d80adc5 checkout lines in the logs. Branch is unprotected; the run was a manual dispatch |
| CI Linux | PASS | E3 | All ubuntu jobs success; counts read in raw logs. Single run |
| CI macos-26 | PASS | E3 | All macos-26 jobs success. One sample for tests that were racy |
| CI docker-live | PASS | E3 | Built from the recorded base digest; 3 passed, 0 skipped |
| Test truth | PASS for accounting; INSUFFICIENT_EVIDENCE for determinism | E3 / none | Counts exact, skips allowlisted and covered on another leg. No repeat run. Python test names not in the quiet logs; execution inferred from 504 collected = 504 passed, 0 skipped |
| Red team | PASS (scoped) | E3 | 5 claims attacked, 20 mutants; no Critical or High |
| Mutation-fuzz | PASS (scoped) | E3 | 88 mutants; survivors are oracle gaps (2 Medium, 9 Low), no bypass of a protection found |
| Reliability | INSUFFICIENT_EVIDENCE | E3 for 6 surfaces, E0-E1 for 8 | YELLOW stands. CI and the data gate executed some of the reasoning-only surfaces, but one gate cannot stand in for another |
| Data | PASS (scoped) | E3 | 10 checks; review-half replay not measured |
| Security | PASS for the delta; INSUFFICIENT_EVIDENCE for the full wave | E3 / E2 | The nine boundaries were tested at 4c3a21a only. Product source is byte-identical per provenance, but a changed SHA voids carried proof under the no-carry-forward rule |

Cross-reviewer facts reproduced independently (E4): the real server answers 400; the old test body is flaky; raising the parser cap survives the suite; the `_FifoBytes` ValueError.

## INDEPENDENCE

- Weak form only. Implementers, the seven reviewers and I are AI agents from one operator's sessions, same model family. No human or outside reviewer.
- The session lead implemented the wave, wrote every reviewer brief and mine, and extracted the reports into files.
- What held: no reviewer built the candidate, I built and reviewed nothing, and the CI ran on GitHub's runners.
- What did not: correlated blind spots are likely, and the briefs framed what each reviewer looked at.
- Reviewers shared one machine. Provenance saw 19 ignored `.pyc` files appear in the review worktree mid-gate (tracked content stayed byte-identical); security saw a peer's mutation script running.

Charter checks: sameTuple true (except the carried security base); evidenceComplete false; noDomainMissing true; noContradictions false (listed, resolved strict); independenceHeld weak.

## VERDICT

**INSUFFICIENT_EVIDENCE.** Failure classes: GATE_NOT_GREEN (reliability YELLOW), NOT_PROVEN (full-wave security at this SHA; determinism). Not CERTIFIED, and not CONDITIONAL, because the necessary record commit changes the SHA and this verdict cannot carry over.

What a re-adjudication needs to return CERTIFIED:
1. A docs-only commit on top of d80adc5 that closes CI4-2 citing this ruling, closes CI5-1 citing run 37135247923, re-cites the eight closures, and enters the 26 findings above.
2. That commit pushed, with a provenance check that the diff from d80adc5 touches only OPEN.md and the report, and a green CI run on it. That run also supplies the second sample for determinism.
3. Reliability: the eight unexecuted surfaces executed at that SHA, or a written founder ruling on their scope.
4. Security: the nine boundaries re-bound at that SHA, or a written founder ruling accepting the prior report plus the verified no-source-change diff.
5. A founder statement accepting the independence limits, or an outside review.

**R-GATE:** on findings, nothing blocks. On the record, d80adc5's register shows two open Highs, so the tree as it stands does not satisfy R-GATE on its face; item 1 fixes that. Whether to merge to integration without AEGIS certification is the founder's decision.

```
{ tuple:{repoIdentity:"DarksiedCEO/zbm-zbc", candidateSha:"d80adc5528ddad4f27427098efe2beee32ee5d6a", treeOid:"5c059f868fe4426859c737170ae75ea9a94f351e", baseSha:"a6aee4e9fe78bb58a94243acce406d2e7b702387", runtimePin:null },
  scope:"fix wave 26b, merge into integration-2026-09-24",
  independentChecks:{sameTuple:true, evidenceComplete:false, noDomainMissing:true, noContradictions:false, independenceHeld:false},
  runtimeClassification:"CI (GitHub-hosted) + local macOS; no live/production",
  verdict:"BLOCKED", mayMergeOrRelease:false, requiresFounderApproval:true }
```

## UNVERIFIED

- Everything requiring execution: I ran no command.
- That the seven report files equal the reviewers' hand-backs, and that the condensed prior verdict is faithful.
- Every git fact (ancestry, tree OID, ls-remote, diff contents, worktree cleanliness): from provenance only.
- The reviewers' probe scripts and raw outputs: not read.
- Run JSON: job conclusions enumerated by search; step level only partly. The 48 skipped steps I saw are the two onboarding-only setup steps.
- Logs: the evidence folder holds 9 job logs. I read docker-live, ledger-rust, dashboard and `required` from the red team's copy in `scratchpad/rt/logs`, outside the handed set; the other 40 logs I did not open.
- Named execution of the Python tests on CI, and which branch the long-query test took there.
- Linux behaviour of the over-cap close; CI's Python 3.12/3.13 versus the local 3.14 and 3.13.
- The workflow blob the run used, deleted runs, signatures.
- The contents of 08a8b8d.
- The fulfillment macos-26 skip, audit package counts in CI, the three merge commits gitleaks did not scan.
- R26B-RL-2's real severity.
- Whether any reviewer's work was affected by peers sharing the machine.

Files:
- /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/aegis26b-r2/ (seven reports, run JSON, 9 job logs)
- /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/rt/logs/ (red team's 53 job logs)
- /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5/docs/findings/OPEN.md
- /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5/services/detection-py/tests/test_request_limits_live.py
- /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5/FIX_WAVE_26B_REPORT.md
- /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/c680f8a3-4c00-4324-89bb-e2650aad9434/scratchpad/aegis26b/VERDICT.md
