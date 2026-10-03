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
| W26B-1 (new) | M | in tree | — | plateau test 5/5 fail here | 3/3 pass | — |

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

(filled in at the end)

## AEGIS round 26b

(filled in at the end)
