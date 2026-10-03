# Fix wave 26b — report

Branch `fix26b` from `integration-2026-09-24` @ `a6aee4e`. Written as the wave runs; the newest state is at the
top of each section. Nothing here is pushed or merged; `main` is untouched.

## Brief (as received, 2026-10-02 ~22:30 PT)

- Scope: every Medium/Low line in `docs/findings/OPEN.md` owned by wave 26 / 26b, including R26-1..R26-5 and W26-ST.
  W26-ST (hygiene self-test flake, ~1 in 27) first.
- Rules: FIX_WAVE_1_COMMON.md and founder rulings R-GATE / R-LOAD / R-HYGIENE. New commits on `fix26b` only. No push,
  no merge, no change to `main`, no force. Failing-first test for each fix where a test is possible; affected suites
  run after each fix; small verified commits.
- CI #3 on `a6aee4e`: wait for macOS / docker-live results before fixing items that depend on them; a new
  High/Critical goes into this report and OPEN.md, fixed if clearly in scope, else flagged.
- End: AEGIS round 26b on the fix26b candidate, verdict recorded, then stop.

### Working constraints found at the start

- `FIX_WAVE_1_COMMON.md` is not on this machine (not in the repo, not on disk) — it lives in the cloud session's
  scratchpad (the out-of-repo reference class OPEN.md C4-5 tracks). This wave follows the rules the repo itself
  records: R-GATE (OPEN.md header), R-HYGIENE (README "Hygiene check", `docs/ci.md`), R-LOAD (2 busy CPU loops,
  as used by earlier waves' timing evidence), and the brief above.
- Build box: Apple M4 Pro, 14 cores, macOS (Darwin 25.6). Python 3.13.9 and 3.14.4; **no Python 3.12**. Docker
  Desktop is installed but its daemon does not start ("Docker Desktop is unable to start"), so nothing docker-live
  can be proven here. Rust 1.90 (Homebrew), Go, Node 24.
- The scratchpad records that OPEN.md's `ref` column points at (`review25/…`, `review26/…`, `w26-reports/…`) are
  not on this machine either; where a line needs them, this report says so.

## CI #3 (`a6aee4e`, run 37096514743) — completed: failure

Finished before this wave started (54m40s, 2026-10-03 04:25Z). 8 of 53 jobs failed (CI #2 on fc19ce7: 16).
Failing jobs and first-look cause (details and dispositions under "New findings"):

| job | first look |
|---|---|
| delivery-py (3.12 / 3.13, ubuntu-24.04) | `test_l2_bind_is_loopback_only_and_health_shape` asserts sandbox "unavailable" but GitHub's Ubuntu runner has Docker ("available"); its nested run fails `test_live_tests_leave_git_status_unchanged`; hygiene R3-tmp: new `/tmp/worktrees` |
| delivery-py (3.13, macos-26) | live launcher refuses to start: `__CF_USER_TEXT_ENCODING` outside `DLV_ENV_ALLOWLIST` (macOS sets it); a1/a2/a4/a4b/a5 adversarial tests fail |
| delivery-docker-live | image build fails: `apt-get purge -y curl wget` exit 100 |
| ledger-rust (macos-26) | `server_slow_clients.rs` 50-conflicting-posts and 800-parallel-posts tests panic |
| python-tests (fulfillment-py, 3.13, macos-26) | fix8 128-sender test: phys_footprint stays at peak (growth 76 MiB, never settles) — the W26-EA-1 hypothesis (R26-5) |
| python-tests (creative-py, 3.13, macos-26) | regex growth `(.)\1+` on combining marks 98.95 ≥ 64 |
| required | aggregate of the above |

## Progress log

(newest first; times PT from `date`. The first entries were estimated and ran ~25 min ahead; corrected here.)

- 04:00 — AEGIS 26b final adjudication: INSUFFICIENT_EVIDENCE (recorded below). CI3-1..CI3-6 moved back to the
  open table (closure pending CI); all 18 round findings entered in OPEN.md. Wave stops here, as briefed.
- 03:48 — three gate reports in (test truth, red team, security: no Critical/High); orchestrator-go 61/61 and
  dashboard 27/27 run at 4c3a21a.
- 02:28 — candidate SHA fixed at 4c3a21a; final evidence sweep running there (every Python suite, ledger-rust,
  hygiene self-test, lint, counts check), sequential, in a separate worktree. Agent worktrees and the tool-made
  branches (`worktree-agent-*`, all at 9531fc2 with no commits of their own) removed; `.claude/` no longer
  appears in `git status`.
- 02:20 — fulfillment memory-model batch committed (4c3a21a): full fulfillment suite on the merged tree 1104 passed.
- 01:55 — timing-test batch committed (5d40f9a): creative 821, fulfillment 1092, onboarding 763 passed on the
  merged tree. The engineer had made fix7's "big batch during the flood" printed-only; the lead checked that the
  200-within-10-s assertions still stand, corrected the overclaiming docstring/comment, and opened W26B-2.
- 01:30 — launcher/ports batch committed (8e87de2) after the lead finished delivery's C5-3/C5-6 residuals itself.
  A tool warning flagged "security test removal": it was the shared `test_shared_ports.py` once-only test being
  replaced by a reuse-after-exhaustion test; reviewed and accepted (every caller now owner-checks its port).
- 00:58 — clean delivery run at e4dda30 (runwt): 721 passed, 4 skipped (allowlisted), 0 violations.
- 00:35 — C1-1 committed (6276183). Three engineer agents dispatched in separate worktrees (fulfillment memory
  model; timing tests; shared launchers/ports): they leave diffs, the lead reviews, re-runs and commits. The
  launcher agent's first worktree was created by the tool at 9531fc2 (old main) and it was blocked; re-dispatched
  on a worktree the lead made at 6276183.
- 00:24 — C6-3-res (624bc03), OAUTH-4 (9a11ee2), R25B-1 + R26-2 (9f27a73: onboarding 757 passed) committed.
- 00:05 — F-9 + H9-R3m (10b12bd), CI3-5 + W26B-1 (67221fe: creative 814 passed) committed; OPEN.md placement fix
  (2f878b5: 19 closed rows had been inserted under the open table's header; CI3-3's row restored).
- 00:00 — creative full suite re-running with CI3-5 + W26B-1 (run worktree). Report updated.
- 23:58 — CI3-6 committed (982d291) after the full fulfillment suite (1089 passed, 0 violations).
- 23:55 — new finding W26B-1 (creative N2 plateau fails 5/5 on this Mac, a6aee4e too): same allocator cause.
- 23:52 — R26-1 committed (0652bc4).
- 23:49 — N25-X-1 (9f03317) and XS-PYC (02c60d0) committed after verification/finance/legal/clipper suites:
  280/337/289/349 passed, 0 hygiene violations each.
- 23:35 — CI3-3 (77fd749) and the delivery batch N25-D-4/N25-D-1/N25-D-2/N24-D-1-res/DLV-HOST (ab1b7a8) committed.
- 23:29 — full delivery suite finished: 714 passed, 4 allowlisted skips, 0 failed (its hygiene line is void: this
  wave edited tracked files and ran other suites during it — re-run clean before AEGIS).
- 23:00-23:25 — docs/hygiene items committed: W25-EB-R1/R2 (21b6750), W25-EB-R3 + R25B-2 (140303c), F-11
  (d03a16c), C4-4 (a8bf717), C5-8b (39f131b).
- 22:57 — CI3-4 + W26-3b + R26-4 committed (48d0bd7).
- 22:50 — CI3-2 committed (6b4d5ac).
- 22:45 — CI3-1 committed (fc2613e).
- 22:40 — W26-ST committed (2447389), first item as briefed.
- 22:33 — branch `fix26b` created at a6aee4e; CI #3 found already complete (failure); report started.

## Items

Status per OPEN.md line. `fixed` = commit on fix26b with a failing-first test where one is possible (each closed
row in `docs/findings/OPEN.md` carries the evidence); `waiting` = needs a CI result, the founder, or a machine this
box is not; `not done` = with the reason.

| item | sev | status | commit | failing-first | verified here | needs CI |
|---|---|---|---|---|---|---|
| W26-ST | M | fixed | 2447389 | 3 self-tests | macOS self-test (2 Linux-only cases as on a6aee4e) | hygiene-static (Linux) |
| CI3-1 (new) | H | fixed | fc2613e | tests/harness_names.rs | ledger-rust suite, 5 extra runs | ledger-rust macos-26 |
| CI3-2 (new) | H | fixed | 6b4d5ac | guard (56 cases) + env test | /tmp/worktrees reproduced, gone | delivery-py ubuntu |
| CI3-3 (new) | H | fixed | 77fd749 | double + env tests | delivery 714 passed | delivery-py macos-26 |
| CI3-4 (new) | H | fixed | 48d0bd7 | — (no daemon) | static tests | delivery-docker-live |
| W26-3b | M | fixed | 48d0bd7 | 2 static tests | checksums fetched + downloaded | delivery-docker-live |
| R26-4 | L | fixed | 48d0bd7 | 2 static tests (step shell run) | yes | — |
| W25-EB-R1, -R2 | L | fixed | 21b6750 | — (docs) | read against the code | — |
| W25-EB-R3, R25B-2 | L | fixed | 140303c | self-test | strict lint 0 | hygiene-static |
| F-11 | L | fixed | d03a16c | self-test | no __pycache__ left | — |
| C4-4 | L | fixed | a8bf717 | — (docs) | — | — |
| C5-8b | L | fixed | 39f131b | self-test | git check-ignore | — |
| N25-D-4, N25-D-1, N25-D-2, N24-D-1-res, DLV-HOST | L | fixed | ab1b7a8 | 6 tests | rounds 24-26b on the staged tree | delivery-py |
| N25-X-1 | M | fixed | 9f03317 | mutants x20/x50 now fail | 4 suites | — |
| XS-PYC | L | fixed | 02c60d0 | before/after run (not in-suite) | 3 suites | — |
| R26-1 | M | fixed | 0652bc4 | portable-path self-test | macOS | hygiene-static |
| CI3-6 (new; R26-5's W26-EA-1) | H | fixed | 982d291 | live test failed here | fulfillment 1089 passed | fulfillment macos-26 |
| CI3-5 (new) | H | in tree | — | not reproducible here | regex tests pass; full suite running | creative macos-26 |
| CI3-5 / W26B-1 (fixed) | H / M | fixed | 67221fe | (not reproducible here) / 5/5 fail here | creative 814 | creative macos-26 |
| F-9, H9-R3m | L | fixed / stated | 10b12bd | 2 self-tests | strict lint 0 | hygiene-static |
| OAUTH-4 | L | reviewed, no change | 9a11ee2 | — | changelog read | — |
| R25B-1, R26-2 | M | fixed | 9f27a73 | scripted timings; defect run | onboarding 757 | onboarding macos-26 |
| C6-3-res | L | fixed | 624bc03 | SIGKILL child test | delivery files | delivery-py |
| C1-1 | L | fixed | 6276183 | harness test | ledger-rust suite | ledger-rust |
| C5-3, C5-4, C6-2, C5-6 | M/L | fixed | 8e87de2 | per-service tests | 9 suites + live 10/10 | all Python jobs, live-runs |
| F-4, F-10, R26-5, W25-EA-6/7/8 | M/L | fixed | 5d40f9a | mutants | 3 suites | creative/fulfillment/onboarding |
| W25-EA-2 | M | partly (onboarding) | 5d40f9a | mutant | — | ruling for the rest |
| F-6, W25-EA-4/5, F-3, R26-3, F-7, F-8, F-5 | M/L | fixed / documented | 4c3a21a | 8 of 12 new tests failed first | fulfillment 1104 | fulfillment |
| W25-EA-3, W25-EA-9 | M / Info | open — ruling | 4c3a21a | — | measured | founder |
| W26B-2 (new) | M | open (wave 27) | — | — | measured | — |

### W26-ST (Medium) — fixed

- Cause 1 (AEGIS r26 root cause): `lint_counts` skipped ids made only of digits (`not h.isdigit()`), so whenever a
  7-char short id had no a-f (~1 commit in 27) a count tied to it lost its exemption — and the self-test that pins a
  count to `git rev-parse --short=7 HEAD` failed deterministically on such a HEAD. Fix: every 7-40 hex token that
  resolves to a commit pins (`devtools/hygiene_check.py` `lint_counts`). A digit run that is not a commit resolves
  to nothing, so it still pins nothing (asserted).
- Cause 2: the R3 dynamic self-tests watched the real /tmp, so any entry another process made during a case failed
  the cases that expect a clean run. Fix: `hygiene_check.py run --system-tmp DIR` (default /tmp; self-test only,
  CI never passes it); the self-test points every case at a private dir, and one case
  (`test_r3_new_entry_in_system_tmp`) still runs against the real /tmp, asserting only its own entry.
- Failing first: `test_l3_a_count_pinned_by_an_all_digit_commit_id` (makes an all-digit commit with
  `git commit-tree` until one appears; a miss in 3000 tries ~1e-49) failed on a6aee4e's checker with
  `L3-counts README.md:1 … '84 passed'`; `test_r3_cases_do_not_watch_the_real_tmp` and
  `test_r3_new_entry_in_the_given_system_tmp` failed (no such option). All three pass after the fix.
- Suite run here (macOS, Python 3.13.9, pytest 9.1.1 as CI's hygiene-static installs): 44 run, 42 pass; the 2
  failures are `test_r4_a_process_outlives_the_suite` and `test_r4_ps_table_with_a_subreaper_does_not_report_its_own_ps`,
  both Linux-only (child subreaper / setsid'd orphan on macOS = R26-1) and failing identically on an untouched
  a6aee4e worktree on this Mac. `hygiene_check.py lint` and `lint --strict-allowlist`: 0 violations.
- Not verified here: the Linux run of the self-test (CI `hygiene-static`).

## New findings

CI #3 failures not in OPEN.md, each recorded there as High (each turns the merge gate `required` red — the class
AEGIS r25 graded High for F-1) and closed by the commit that fixes it:

- **CI3-1** ledger-rust macos-26 — fixed (fc2613e). Test port files collided (macOS clock = µs; same label) so
  one test drove another's server. `common::unique_suffix()`; `tests/harness_names.rs` failed first (3511/4000
  distinct). Also allowed clippy's macOS-only `unnecessary_cast` (the job's next step would have failed).
- **CI3-2** delivery-py ubuntu (3.12, 3.13) — fixed (6b4d5ac). Runner has Docker → "available"; seven tests made
  `/tmp/worktrees`. `base_env` sets an unreachable `DOCKER_HOST` and refuses a tmp outside the suite's temp dir.
  Reproduced the `/tmp/worktrees` entry with a6aee4e's tests on this Mac; gone with the fix. Ubuntu leg: CI.
- **CI3-3** delivery-py macos-26 — fixed in the tree, committed after the full suite. Two causes: (a) the argv
  Docker double ran the adapter's GNU-only `mv -f -T` and `find -printf '%p\n'` with macOS's BSD tools → every
  harness run HARNESS_ERROR "write failed (mv)" (12 adversarial tests + g14); the double now gives those two
  spellings their GNU meaning when the host tool is not GNU (production is Debian, unchanged); (b) the service
  refused to start on macOS: CoreFoundation writes `__CF_USER_TEXT_ENCODING` into CPython's own environment —
  allowed on darwin only, value-checked (product change in `config.py`, ADR 0011 item 18 and README updated).
  Four affected files on macOS: a6aee4e 14 failed + 4 errors → 0.
- **CI3-4** delivery-docker-live — fixed (48d0bd7). `apt-get purge curl wget`: wget never installed. Build: CI.
- **CI3-5** creative-py macos-26 — `(.)\1+` growth 98.95 ≥ 64 on combining marks (open; see the regex items).
- **CI3-6** fulfillment-py macos-26 — fix8 128-sender: phys_footprint stays at its peak (the W26-EA-1 hypothesis
  in R26-5 confirmed by CI); open.

## What passed / failed / could not be verified / waiting on the founder

Candidate: **4c3a21abdb1e777cc6ed1f0ca8a822c4b26d2196** (code). Later commits change only this report and OPEN.md.
Local only: `fix26b` is not pushed, so there is no CI run for it.

### Passed (on this Mac — macOS 26.6, M4 Pro, Python 3.13.9 — at 4c3a21a, one sequential sweep in a clean worktree)

| check | result |
|---|---|
| `hygiene_check.py lint --strict-allowlist` | 0 violations |
| `hygiene_check.py counts --check` | 13 suites, one row each |
| ledger-rust (`cargo test --locked`, hygiene wrapper) + clippy | 116 run (117 on Linux: one Linux-only test), 0 failed, 0 violations |
| detection / fulfillment / onboarding / creative | 504 / 1104 + 1 allowlisted skip / 763 / 821 passed, 0 violations each |
| compliance / verification / clipper-network / finance / legal | 636 / 299 / 368 / 358 / 308 passed, 0 violations each |
| delivery-py | 723 passed, 4 allowlisted skips (3 Docker-live, 1 Linux /proc), 0 violations |
| orchestrator-go (`go vet`, `go test -race`) | 61 passed, 0 violations |
| dashboard-ts (npm ci, lint, build, test) | 27/27 passed; the wrapper could not count Node 24's output (W26B-3) |
| live runs (launcher engineer, five services) | 10/10, 0 violations |
| AEGIS 26b gates | test truth GREEN, red team GREEN, security GREEN (no Critical/High) |

### Failed

- `devtools/test_hygiene_check.py` on macOS: 2 of 61 fail — `test_r4_a_process_outlives_the_suite` (setsid / no
  subreaper on macOS) and `test_r4_ps_table_with_a_subreaper_does_not_report_its_own_ps` (needs a child
  subreaper). Both fail identically on a6aee4e here; both are Linux-only properties. No green run of this
  self-test exists for 4c3a21a until CI's Linux `hygiene-static` runs.
- Nothing else failed at the candidate.

### Could not verify

- **Any CI leg.** Linux (3.12 and 3.13), macos-26 and docker-live have not run on 4c3a21a; the six CI #3
  failures (CI3-1..CI3-6) are fixed on this Mac only. CI3-4 (image build) and W26-3b/R26-4's pins could not
  be built here (no Docker daemon). CI3-5 did not reproduce here at all.
- **Python 3.12** (not installed here) and **Linux-only paths** (VmHWM, `/proc`, the subreaper, R4 on Linux,
  the 128/128 burst F-3, AEGIS's 91 MiB F-6 scenario).
- **Docker-live properties** (3 delivery tests skip here by design).
- **Independence:** the implementers (lead + three engineer agents) and the gate reviewers are all AI agents
  started from this one session — separate contexts, same model; no human or outside review.
- AEGIS's mutation-level discrimination was sampled by the red team (`aegis26b/redteam.md`), not exhaustive.

### Open after this wave (OPEN.md, tracked under R-GATE)

- **High, fix committed but NOT proven (closure pending CI): CI3-1..CI3-6.**
- New Medium: W26B-2 (fix7 big batch refused during a 32-sender flood), R26B-RT-1 (fix5 keep-alive test
  weaker than a6aee4e's), R26B-RT-2 (fix7 overload test no longer bounds the wait), R26B-RT-3 (ADR 0002
  overclaims that the parse in progress is counted), R26B-SEC-1 (sandbox image's pip install without hashes).
- New Medium (test truth): R26B-TT-2, R26B-TT-3.
- New Low: R26B-RT-4..7, R26B-SEC-2..5, R26B-TT-1, R26B-TT-4..6, W26B-3.
- Carried: F-2 / N25-I-2 (the 9e55931 lists not re-checked), W25-EA-2 (rest), W25-EA-3, W25-EA-9, TG-1,
  DLV-CLAMP, W25-EA-1, C4-5, H9-R6m — rulings (below) or wave 27.

### Waiting on the founder (rulings the wave cannot make)

1. **TG-1 (textguard divergences, ADR 0007 table):** (a) should clipper-network's `approval_forgery` also match
   `certified` / `certify (this|me|all|it)`, as verification-py and finance-py do? (b) should delivery-py keep
   `guarantee_coercion` (it dropped it; nothing says why)? Each answer is a one-line change plus a test.
2. **DLV-CLAMP:** a failing review whose child run would carry more than `DLV_MAX_FINDINGS` (200) findings is now a
   422 (spec D12: 200 per run). Keep that for reviews, or allow a review's child run a different cap?
3. **W25-EA-1 / C4-5 (documents outside the repo):** `BUILD_CONTRACTS.md` (21 files cite it),
   `revenue-recovery-founder-decisions.md`, `revenue-recovery-roadmap.md`, `CLIPPER_NETWORK_SPEC.md`, `LEGAL_SPEC.md`,
   `DEPT28_SPEC.md`, `FIX_WAVE_23b.md`, `FIX_WAVE_1_COMMON.md` and the reviewers' log paths live only in session
   scratchpads (none is on this machine). Commit them under `docs/` (they must be supplied), or mark every
   reference as external?
4. **H9-R6m (test counts after a merge):** today the merge author regenerates `docs/test-counts.md` by hand
   (`--counts write`). Removing that step means either a CI job that commits to `main` (a bot push — against the
   "engineers never push" rule unless you exempt it) or a merge-time check that only fails earlier. Which, if any?
5. **W25-EA-9:** under a stall flood, large legit astral batches are mostly 503 — the measured cost of counting the
   model. Confirm the trade-off or ask for a different one (numbers: the fulfillment engineer's section below).
6. **The push of `fix26b`.** Nothing is pushed. AEGIS 26b says the candidate may not be merged until a CI run on the
   pushed commit is green (it is the only way the six CI3 Highs can close). Pushing `fix26b` for that CI run is
   your decision; its head is a report/OPEN.md-only change after the candidate 4c3a21a.
7. **The rest of W25-EA-2:** keep fulfillment fix7/fix4_live/fix9 and detection's latency literals as documented
   sanity bounds (L1 would need to see `_pct(...)`/`max(list)` to allowlist them) or delete them — the timing
   engineer measured they do not discriminate the planted defects; detection's 0.5 s is an ADR 0001 product bound.
8. **The AEGIS chain:** whether provenance / reliability / data / mutation-fuzz gates are required for this repo
   (the adjudicator lists them as missing), or a ruling that removes them.

## AEGIS round 26b re-adjudication — candidate d80adc5 (2026-10-03, after CI #6)

**Verdict: INSUFFICIENT_EVIDENCE** (certification withheld) on d80adc5528ddad4f27427098efe2beee32ee5d6a, tree 5c059f86.
It supersedes the verdict on 4c3a21a below. **No Critical or High finding is open** after the ruling on CI4-2; under
R-GATE nothing blocks a merge into `integration-2026-09-24` on findings. Merging without certification is the
founder's decision.

Why not CERTIFIED (the adjudicator's reasons): the reliability gate came back YELLOW (6 of 14 surfaces executed, the
rest read from the code); the full-wave security review is bound to 4c3a21a and only the delta was re-tested at
d80adc5 (no product source changed in between, verified by provenance); determinism has one CI sample; independence
is the weak form (every implementer, reviewer and the adjudicator is an AI agent started from this operator's
sessions, the lead wrote every brief and saved the reports); and the record commit that closes CI4-2 is a new SHA the
verdict does not cover.

**CI4-2 ruling — closed as a High.** Accepting an early close does not weaken the property the test states, and a
correct server needs the tolerance: it answers 400, sends FIN, drains at most 64 KiB or 1 s and closes, so a client
still writing a 1 MiB line gets a reset by design. Four reviewers saw the real server answer 400 in every probe.
Every mutant that serves the over-cap line with 200 still fails the test. What the change did remove is the previous
body's accidental failure on a silent close, a crash and a server that refuses everything afterwards — failures it
also raised against the correct server about half the time on this Mac. The residual is tracked: CI4-2b (Medium:
the `!= 200` oracle pins neither the 400/431 nor that the server survives), R26B-D-SEC-1 (Medium: the parser-level
head cap is proven by no test), CI4-2c and RT2-5 (Low). Limit: every probe ran on macOS; a Linux client losing the 400
to the reset was not tested.

| gate | result at d80adc5 |
|---|---|
| provenance | PASS — SHA, tree and parent agree across local git, ls-remote and the API; CI run 37135247923 is attempt 1 on that SHA, nothing re-run or skipped |
| CI Linux / macos-26 / docker-live | PASS — every job of run 37135247923 green; counts read from raw logs |
| test truth | PASS for accounting; INSUFFICIENT_EVIDENCE for determinism (one run) |
| red team | PASS for the scope — 5 claims attacked, no Critical/High; 3 Medium, 4 Low |
| mutation-fuzz | PASS for the scope — 88 mutants; survivors are test-oracle gaps (2 Medium, 9 Low), no protection bypassed |
| reliability | INSUFFICIENT_EVIDENCE — YELLOW: no Critical/High/Medium found, 4 Low, 8 surfaces not executed |
| data | PASS for the scope — cancelled-admission replay refused concurrently and after restart, torn logs fail closed; 4 Low |
| security | PASS for the delta; INSUFFICIENT_EVIDENCE for the full wave at this SHA |

Prior conditions: 1 (CI on the pushed commit) met; 2 (diff from 4c3a21a is records only) not met as written — four
more files changed (ci.yml and three test files, no product source), all reviewed; 3 (mutation-fuzz) met; 4
(provenance, reliability, data) partly — reliability YELLOW; 5 (CI3-1..CI3-6 on CI) met, the rows cited the wrong runs
(AO-1, corrected); 6 and 7 met.

Recorded in OPEN.md with this section: CI4-2 closed on the ruling; R26B-TT-1 and R26B-TT-2 closed on CI #6; the eight
CI3/CI4 closures now cite CI #6 on d80adc5 (they cited CI #4 / CI #5, runs on other SHAs that failed overall); 23 new
open rows (4 Medium: CI4-2b, R26B-D-SEC-1, RT2-3, MF-8; 19 Low) and RT2-4, AO-1, AO-2 fixed in the same commit.

For a CERTIFIED re-adjudication the adjudicator asks for: (1) this record commit pushed, a provenance check that it
changes only OPEN.md and this report, and a green CI run on it (also the second determinism sample); (2) the eight
unexecuted reliability surfaces run at that SHA, or a founder ruling on their scope; (3) the security boundaries
re-bound at that SHA, or a founder ruling accepting the 4c3a21a report plus the no-source-change diff; (4) a founder
statement accepting the independence limits, or an outside review.

After the verdict (founder, 2026-10-03): the delivery-py macos-26 leg's limit raised 120 -> 180 min (RT2-3; the line
stays open), and the round's records committed under `docs/aegis/round-26b/`.

Records (`docs/aegis/round-26b/`, copied from the session scratchpad `aegis26b-r2/`): `provenance.md`, `test-truth.md`, `redteam.md`,
`mutation-fuzz.md`, `reliability.md`, `data.md`, `security.md`, `VERDICT.md` — each the reviewer's own final report,
extracted by the lead — and the raw CI run record.

## CI #6 result — first all-green run

Run 37135247923 on **d80adc5** (pushed): **53/53 jobs green, `required` green**. CI5-1 closed on it (orchestrator-go
ubuntu and macos-26 green). Every High from CI #3/#4/#5 is now closed on CI evidence except **CI4-2**, which waits for
the AEGIS ruling (a test that now accepts an early close; green CI does not close it). The AEGIS 26b verdict
(INSUFFICIENT_EVIDENCE on 4c3a21a) stood until the re-adjudication at d80adc5 (section above) — the diff 4c3a21a..d80adc5 adds
the CI #4/#5 test/CI fixes, so it is a new subject, not a record-only change. Open Medium/Low: OPEN.md (wave 27).

## CI #5 result (2026-10-03 08:49 PT)

Run 37129375575 on b24ffde finished: 51 green, 2 red — orchestrator-go (ubuntu-24.04) (CI5-1, fix 700fce6 local,
not pushed) and the `required` roll-up. All three delivery-py legs GREEN: ubuntu 3.13 53 min, ubuntu 3.12 54 min,
macos-26 86 min. **Closed on this run: CI3-2, CI3-3, CI4-1, CI4-3.** Detection macos-26 green, but CI4-2 stays open
for the AEGIS ruling. Open Highs now: **CI4-2** (AEGIS ruling) and **CI5-1** (needs CI #6). The table below is the
08:11 snapshot; OPEN.md has the closures.

## Current state (2026-10-03 08:11 PT)

**Commits.** Pushed: `fix26b` on origin at **b24ffde** (CI #4 fixes). Local only, NOT pushed: **a4e96a2** (OPEN.md:
CI4-2 closes only on an AEGIS ruling; W26B-4 delivery runtime) and **700fce6** (CI5-1 fix) — plus this report commit.
`integration-2026-09-24` (a6aee4e) and `main` (9531fc2) untouched. AEGIS 26b verdict on 4c3a21a: INSUFFICIENT_EVIDENCE
(stands until CI is green and AEGIS re-adjudicates).

**CI runs** (all workflow_dispatch on `fix26b`; a push to `fix26b` alone starts nothing):
- CI #4: run 37125312276 on d40de72 (code = 4c3a21a) — 48 green, 3 red, 1 cancelled.
- CI #5: run 37129375575 on b24ffde — in progress: 48 green, 1 red (orchestrator-go ubuntu → CI5-1), still running:
  delivery-py (3.12, ubuntu-24.04), delivery-py (3.13, ubuntu-24.04), delivery-py (3.13, macos-26).

**High items from CI** (cause / fix / proof):

| id | status | cause | fix | proving run |
|---|---|---|---|---|
| CI3-1 ledger-rust macos-26 | **closed** | two tests shared one port file (macOS clock = µs) so one drove the other's server; not a ledger bug, not the open-file limit | fc2613e | CI #4 37125312276 green, nothing skipped |
| CI3-2 delivery-py ubuntu | open | runner has Docker ("available"); 7 tests made /tmp/worktrees | 6b4d5ac | CI #4: both causes gone but red from CI4-1; CI #5 delivery ubuntu legs running |
| CI3-3 delivery-py macos-26 | open | BSD mv/find in the test double; `__CF_USER_TEXT_ENCODING` refused | 77fd749 | CI #4: no failure through 59%, cancelled by 60-min limit; CI #5 macOS leg running |
| CI3-4 delivery-docker-live | **closed** | `apt-get purge curl wget`: wget never installed | 48d0bd7 | CI #4 green (image built, 3 live tests ran) |
| CI3-5 creative-py macos-26 | **closed** | regex growth read through the regex engine's stack allocation on the CI VM | 67221fe | CI #4 green |
| CI3-6 fulfillment-py macos-26 | **closed** | libmalloc large cache kept freed buffers charged to the process | 982d291 | CI #4 green |
| CI4-1 delivery-py ubuntu | open | this wave's N25-D-4 test counted events while the admitted run still recorded (22 == 20) | b24ffde | CI #5 delivery ubuntu legs (running) |
| CI4-2 detection-py macos-26 | open — **waits on AEGIS** | BrokenPipe: server closed early on a 1 MiB request line; the test now accepts that close as the refusal (race not reproduced here) | b24ffde | CI #5 detection macOS green, but green CI does NOT close it: AEGIS must rule whether accepting an early close weakens the test |
| CI4-3 delivery-py macos-26 | open | job cancelled at its 60-min limit (no failure) | b24ffde (limit 120) | CI #5 delivery macOS leg (running); runtime itself tracked as W26B-4 (Medium) |
| CI5-1 orchestrator-go ubuntu | open, fix local | the slow-client test's clock started after `Dial`, but the server's ReadTimeout runs from accept, so the cut read 0.16 ms under 15 s | 700fce6 (local at 08:11; pushed since — it is an ancestor of d80adc5, AO-2) | needs the next run (CI #6) |

Next: when CI #5 finishes, report the delivery legs; then, only with the founder's approval, push a4e96a2 + 700fce6
(+ report) and start CI #6; then AEGIS re-adjudication (and its ruling on CI4-2).

## CI #4 (after the founder's push, 2026-10-03 morning)

`fix26b` pushed at d40de72 (founder-approved; integration and main unchanged). The push alone does not trigger the
workflow (it runs on main / integration-** / pull requests / by hand), so the lead started it by hand:
run 37125312276 (workflow_dispatch, every job runs), 48 green, 3 red + `required`, 1 cancelled.

| CI #3 failure | CI #4 |
|---|---|
| CI3-1 ledger-rust macos-26 | **green — closed** |
| CI3-2 delivery-py ubuntu 3.12/3.13 | its causes fixed (0 hygiene violations; the Docker-"available" test passes; 723 passed) but red from one other test → CI4-1 |
| CI3-3 delivery-py macos-26 | no failure through 59%, then cancelled by the 60-min job limit → CI4-3; unproven |
| CI3-4 delivery-docker-live | **green — closed** (image built, 3 live tests ran) |
| CI3-5 creative-py macos-26 | **green — closed** |
| CI3-6 fulfillment-py macos-26 | **green — closed** |
| (new) detection-py macos-26 | BrokenPipe in a test's send → CI4-2 |

Also green in CI #4: hygiene-static on Linux (the two R4 self-tests that cannot run on a Mac included), every
other Python leg, orchestrator-go and dashboard on both OSes, live-runs, audits, secret scan.

New Highs, fixed in the next commit (local, not pushed): CI4-1 (this wave's N25-D-4 test counted events while the
admitted run was still recording — now waits for idle; 6/6 here, mutant still caught), CI4-2 (detection's long
query-string test handles the server's early close like its sibling test), CI4-3 (delivery-py job limit 60 → 120
min). They close only on the next CI run.

## AEGIS round 26b

Plan (AEGIS rules): the lead implemented this wave, so the lead does not certify it. Independent read-only reviewer
agents, given the code and OPEN.md but not the lead's conclusions, check the candidate at its exact SHA: (1) source
and red team — falsify each wave-26b "Closed" row against its literal claim; (2) test truth — re-check failing-first
and mutant claims; (3) security — the delivery changes (env allowlist, git isolation, cancel set, non-UTF-8 gate);
(4) a final adjudicator renders the verdict over that evidence. Ceiling, stated in advance: `fix26b` is not pushed,
so no CI run exists for it; every CI-dependent gate (Linux legs, macos-26 legs, docker-live) is
INSUFFICIENT_EVIDENCE until the founder pushes, and the verdict cannot exceed CONDITIONAL.

### Result — candidate 4c3a21abdb1e777cc6ed1f0ca8a822c4b26d2196 (tree d0d8e4f1)

**Final adjudication: INSUFFICIENT_EVIDENCE (BLOCKED; may not be merged).** Not the CONDITIONAL the lead had
stated as the ceiling — the adjudicator did not adopt it, and its reasons stand:

| gate | result |
|---|---|
| test truth (accounting, removed/loosened tests, determinism) | sub-gate GREEN; gate INSUFFICIENT_EVIDENCE (no mutation-fuzz proof; self-test exit 1 on macOS; go/dashboard runs not SHA-bound) |
| red team (claim falsification) | PASS for the scope — no Critical/High; 3 Medium, 4 Low |
| security | PASS for the scope (SECURITY_GREEN) — 1 Medium, 4 Low; image reviewed statically |
| CI Linux / macos-26 / docker-live | INSUFFICIENT_EVIDENCE — no run exists (not pushed) |
| mutation-fuzz, provenance, reliability, data | INSUFFICIENT_EVIDENCE — no report |
| closure of the six Highs CI3-1..CI3-6 | NOT_PROVEN — each closure's proof is a CI leg that has not run; under R-GATE that blocks a merge |

Acted on: CI3-1..CI3-6 moved back to OPEN.md's open table as "fix committed on fix26b; closure pending CI" (they
were wrongly moved to Closed by this wave on local evidence alone); all 18 Medium/Low findings of the round entered
in OPEN.md (R26B-RT-1..7, R26B-TT-1..6, R26B-SEC-1..5) with the reviewers' severities.

Independence, as the adjudicator states it: weak form only — implementers (the lead and three engineer agents),
the three reviewers and the adjudicator are AI agents started from one session (likely one model family); the lead
wrote every brief; the suite accounting is the implementer's own sweep; the red team planted mutants in the same
worktree the sweep used (after it ended, by timestamps, but nothing binds that).

Conditions for a CERTIFIED re-adjudication (all): (1) push the exact commit and get a CI run bound to it with every
required job green — Linux hygiene-static incl. the two R4 self-tests, delivery-py ubuntu 3.12/3.13, ledger-rust
Linux, the macos-26 legs, delivery-docker-live (build + 3 live tests); (2) if the pushed commit is not 4c3a21a,
check that the diff from 4c3a21a touches only this report and OPEN.md, and adjudicate that commit anew; (3) a
mutation-fuzz report bound to it; (4) provenance, reliability and data reports, or a founder ruling removing them
from the chain; (5) CI3-1..CI3-6 confirmed by those legs; (6) the 18 findings in OPEN.md (done); (7) orchestrator-go,
dashboard-ts (Node 22) and go vet with SHA-bound logs (a CI run does it).

Records (outside the repo, session scratchpad `aegis26b/`): `redteam.md`, `test-truth.md`, `security.md`,
`VERDICT.md` (the adjudicator could not write; the lead saved a condensed transcription, labelled as such), and the
sweep logs `sweep-4c3a21a/`.
