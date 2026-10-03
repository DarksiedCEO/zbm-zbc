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

(newest first)

- 22:45 PT — W26-ST fixed and committed (first item, as briefed).
- 22:35 PT — branch `fix26b` created at a6aee4e; CI #3 found already complete (failure); report started.

## Items

Status per OPEN.md line. `fixed` = commit on fix26b with a failing-first test where one is possible;
`waiting` = needs a CI result, the founder, or a machine this box is not; `not done` = with the reason.

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

## What passed / failed / could not be verified / waiting on the founder

(filled in at the end)

## AEGIS round 26b

(filled in at the end)
