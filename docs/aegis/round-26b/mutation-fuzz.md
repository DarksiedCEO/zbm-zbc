# AEGIS 26b mutation-fuzz gate, candidate d80adc5528ddad4f27427098efe2beee32ee5d6a

Sandbox: the detached worktree `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/mutation-d80adc5`. Each mutant was planted one at a time with `scratchpad/mut.sh` and reverted with `git checkout -- <file>`.

At the end:
- `git status` printed "Not currently on any branch. nothing to commit, working tree clean". `git status --short` printed nothing.
- `git rev-parse HEAD` printed `d80adc5528ddad4f27427098efe2beee32ee5d6a`.
- The real repo was never touched. Its HEAD is still 08a8b8d, with only the pre-existing untracked `.git.backup-preimport/` and `.idea/`.

Platform: macOS arm64, Python 3.13.9, Go 1.25.5, cargo offline. Other reviewers' CI, Linux and Docker legs were not run.

Interpreters:
- detection and fulfillment ran with the red team's venvs (`.../c680f8a3.../scratchpad/venvs/<service>`).
- delivery-py and the hygiene unit tests ran with the real repo's `services/delivery-py/.venv/bin/python`, with the sandbox as cwd. The package imports from the sandbox `src`.

## Totals

| Area | Mutants | Killed | Survived |
|---|---|---|---|
| detection-py request limits | 14 | 8 | 6 |
| orchestrator-go limits | 12 | 12, but 6 only by the pin test | 0 |
| delivery-py | 20 | 14 | 6 |
| hygiene_check | 22 | 15 | 7 |
| fulfillment small-model pool | 7 | 6 | 1 |
| shared launch_guard | 6 | 5 | 1 (killed only by the hygiene L4 drift check) |
| ledger-rust `unique_suffix` | 7 | 4 | 3 |

Each "Killed" figure counts a mutant once, whichever test killed it. Three delivery-py mutants and the hygiene L3 mutant H4 survive their unit tests but are killed by the CI `lint --strict-allowlist` step. These are noted in the tables. The killed totals include these cases.

## 1. detection-py request limits and CI4-2

Commands:
- Live tests: `cd services/detection-py && python -m pytest -q tests/test_request_limits_live.py -k "<names>"`.
- Source files: `src/serve.py` (cap passed as `h11_max_incomplete_event_size=MAX_HEADER_BYTES`, `send_400_response`, head deadline) and `src/api.py:191` (`MAX_HEADER_BYTES = 16*1024`) with `_BodyLimitMiddleware` at `api.py:229-232` (the 431 check).
- Each mutant ran against the d80adc5 tests (`header_cap`, `long_query`, `slow_request_head`, `keep_alive_and`).
- It also ran 5 times against the previous body of `test_long_query_string_is_refused`. I saved `git show 4c3a21a:...test_request_limits_live.py` as an untracked file in the sandbox and removed it at the end.

### Baseline flakiness

On this box the unmutated old test body failed 10 of 20 runs, with BrokenPipeError. The d80adc5 body passed 20 of 20. Old-body results under mutation are therefore only meaningful where all 5 runs fail.

### Mutants

| # | file:line, change | d80adc5 tests | old long_query body (5 runs) |
|---|---|---|---|
| D1 | serve.py cap `MAX_HEADER_BYTES` -> 2 MiB | SURVIVED (4 passed) | 5/5 pass |
| D2 | serve.py cap -> 64 MiB (cap raised) | SURVIVED | 5/5 pass |
| D3 | serve.py cap kwarg removed | SURVIVED (equivalent: h11's default is 16 KiB) | 2 pass / 3 fail (flake rate) |
| D5 | serve.py cap x2 (32 KiB) | SURVIVED (the tests only use 1 MiB and 4 MiB) | 4 pass / 1 fail |
| D6 | api.py `MAX_HEADER_BYTES` -> 64 MiB (parser and middleware both) | KILLED by `test_request_head_larger_than_the_header_cap_is_refused` and `test_long_query_string_is_refused` (`assert 200 != 200`), deterministic | 5/5 fail |
| D7 | serve.py `send_400_response` writes `HTTP/1.1 200 OK` (refusal turned into serving) | KILLED by the same two tests | 5/5 fail |
| D8 | `send_400_response` just closes the socket (close without refusing) | SURVIVED | 3 fail / 2 pass (flake rate) |
| D9 | `send_400_response` writes 204 | SURVIVED (only 200 is rejected) | 4 fail / 1 pass (flake) |
| D10 | api.py middleware 431 check removed (`if head > self.max_head` -> `if False`) | SURVIVED (the parser cap refuses first) | 3 fail / 2 pass (flake) |
| S1 | serve.py `REQUEST_HEAD_TIMEOUT_S` 10 -> 60 | SURVIVED (`slow_request_head`; the test imports the constant and takes 62 s) | not run |
| S2 | `REQUEST_HEAD_TIMEOUT_S` -> 0.5 | KILLED by `test_keep_alive_and_pipelined_requests_still_work_with_the_head_deadline` | not run |
| S3 | head deadline not armed at connect | KILLED by `test_slow_request_head_is_cut_off_and_others_are_served` | not run |
| S4 | `timeout_keep_alive=5` -> 500 | SURVIVED (`keep_alive_and` only sleeps 1 s) | not run |
| S5 | head deadline callback does nothing | KILLED by `test_slow_request_head_is_cut_off_and_others_are_served` | not run |

### CI4-2 answer

Tolerating an early close does not remove any kill the old body could make deterministically.
- Every mutant the old body killed on all 5 runs (D6, D7) is also killed by the new body.
- Every other old-body "kill" (D8, D9, D10, D3, D5) is at the unmutated flake rate. They are not kills.
- The old body was a false-red test, failing 50% of the time on this box with no mutant planted.

The weakness is in the assertion `status != 200`, which is the same in both bodies. It accepts any non-200 answer, and a reset or empty read (status 0) as "refused". Mutants D8 and D9 (a close with no refusal, a 204) therefore survive in both.

A mutant that never answers is still caught. The socket's 10 s timeout raises an uncaught TimeoutError, and the test fails.

### Findings

**MF-1, Medium: the parser-level head cap in `serve.py` is untested.** Raising it to 2 MiB or 64 MiB (D1, D2), removing it (D3, equivalent), or removing the middleware's 431 (D10) each survive, because the two layers mask each other. The only kills (D6, D7) change both layers. The tests cannot show that an oversized head is refused while it is being read, as the `serve.py` and `api.py` comments claim.
- With the cap at 64 MiB, a request head of up to 64 MiB is buffered before the middleware answers 431.
- Severity reasoning: defense in depth only (the 431 still happens), and the code is unchanged by 26b (only the test changed). Not blocking.
- Falsifier: a test that sends a head of about 1 MiB with `Connection: close` and a stub app, or asserts the answer is the parser's 400 rather than the middleware's 431.

**MF-2, Low: the refusal status is not asserted** (D8, D9 survive), which is the known tolerance in CI4-2. A close without a response counts as refused by design. The test cannot tell "refused" from "dropped". This is not a regression from 26b.

**MF-3, Low: S1 and S4 survive.** The head deadline can be raised to 60 s without a failure, because the test follows the imported constant. The keep-alive value 5 is unpinned.

### Parser fuzz

I ran `scratchpad/fuzz.py` against a locally started `serve.py` (HEAD d80adc5), with a bisecting boundary search. All results are bounded, and the server stayed healthy and exited 143 on SIGTERM.

| Input | Result |
|---|---|
| Request-line path | first refusal at 16356 path bytes (431 from the middleware, which counts path + query + 4/header); 16355 gives 404 |
| Single header value | 431 first at 16345 value bytes (head 16402 B); one byte less gives 200 |
| 100 000 headers (1.09 MB) | 400 |
| About 2500 tiny headers (15 KB) | 200 |
| 5 MiB with no CRLF | 400, immediately |
| NUL in the path, duplicate Content-Length | 400 |
| 300 connections with a partial request line | all 300 closed by the server after about 11 s, /health stayed 200, RSS 64 MB |

Informational, not findings: h11 accepts a lone-LF request, obs-fold headers, and Content-Length together with Transfer-Encoding (answered 405 on a POST to /health). This could matter only behind a proxy that parses differently.

## 2. orchestrator-go slow-client limits

Command: `cd services/orchestrator-go && go test -count=1 -run '<TestSlow|TestServerHasExplicit|TestOversized...>' ./cmd/orchestrator`. The mutants are in `cmd/orchestrator/main.go`, in `newServer` (lines 104-113) and the constants (lines 82-83, 109-111).

| # | change | `TestSlow*` (live binary) | `TestServerHasExplicitTimeoutsAndLimits` |
|---|---|---|---|
| G1 | `ReadTimeout` 10 s | KILLED by `TestSlowBodyIsCutOffBeforeAnyScanAndOthersAreServed` (cut at 10.001 s, "before readTimeout 15s") | killed |
| G2 | `ReadTimeout` 30 s | SURVIVED (ok, 30.6 s) | killed |
| G3 | `ReadTimeout` removed | KILLED (STILL OPEN at the 45 s hang guard) | killed |
| G4 | `ReadHeaderTimeout` 2 s | KILLED by `TestSlowlorisHeaderIsCutOffAndOthersAreServed` | killed |
| G5 | `ReadHeaderTimeout` 12 s | SURVIVED (ok) | killed |
| G6 | `ReadHeaderTimeout` removed (Go falls back to ReadTimeout, 15 s) | SURVIVED (ok) | killed |
| G7 | const `readTimeout` 8 s | SURVIVED (the test uses the same constant) | killed |
| G8 | const `readTimeout` 40 s | SURVIVED | killed |
| G9 | const `readHeaderTimeout` 2 s | SURVIVED | killed |
| G10 | const `maxHeaderBytes` 1 MiB | `TestOversizedHeaderIsRefused` KILLED (100 KiB header gave 200) | killed |
| G11 | `Handler: limitRequests(h)` -> `h` | KILLED (the scan started before its body arrived, a 502 came before the 408) | not run |

Conclusion: the live tests enforce only a lower bound ("not cut before the timeout") and use the production constants. A longer or removed timeout is caught only by the unit pin `TestServerHasExplicitTimeoutsAndLimits` (`server_limits_test.go:217`, exact 5 s / 15 s / 16 KiB).

I did not test a mutant that bypasses `newServer`. It is the only constructor, called at `main.go:255`.

The 26b change moved `start := time.Now()` before the dial in `trickle`. It is sound: with the timeout shorter (G1, G4) the lower-bound assertion still fails, and I saw no false alarm.

**MF-4, Low:** longer timeouts (G2, G5, G6) survive the live tests, and only the pin test kills them. That is acceptable because it is the intended design, but it should be stated. No Critical or High finding.

## 3. delivery-py

Command: `python -m pytest -q -p no:cacheprovider tests/test_round26b.py` (14 passed in 43 s baseline), plus `tests/test_round25.py` for the cap tests.

| # | file, change | tests | result |
|---|---|---|---|
| L1 | service.py, `create_fix_run`: cap_check before `_idem` | `-k "dlv_max_findings or lowered"` | KILLED by `test_a_recorded_fix_run_replays_its_answer_after_dlv_max_findings_is_lowered` (422 != 202) |
| L2 | service.py, `review`: cap_check before `_idem` | same | SURVIVED (3 passed) |
| L3 | `review`: cap_check never called | same | KILLED by `test_dlv_max_findings_caps_the_run_a_failing_review_would_open` |
| L4 | `create_fix_run`: cap_check never called | same | KILLED by `test_a_recorded_fix_run_replays...` and `test_dlv_max_findings_caps_the_findings_document` |
| E1 | config.py:217, CF name allowed on any platform | `-k "corefoundation or this_platform"` | KILLED by `test_macos_corefoundation_encoding_name_is_allowed_on_darwin_only_and_only_in_its_shape` |
| E2 | value check `if False` | same | KILLED by the same test |
| E3 | `fullmatch` -> `match` | same | KILLED by the same test |
| E4 | `fullmatch` -> `search` | same | KILLED by the same test |
| E5 | regex `{1,8}` -> `+` (unbounded hex digits) | same | SURVIVED |
| E6 | regex uppercase hex only | same | SURVIVED (lowercase is not tested) |
| E7 | CF name skips the forbidden-name check | same | SURVIVED (equivalent: the name is not forbidden) |
| C1 | service.py:746, cancelled-set lookup disabled | `-k cancelled` | KILLED by `test_a_cancelled_admission_replayed_after_its_answer_left_the_map_is_refused_as_cancelled` |
| C2 | service.py:343, cancel not added to the set | same | KILLED by that test and `test_cancelled_admission_ids_survive_the_start_up_replay_of_the_log` |
| C3 | code `"CANCELLED"` -> `"CANCELED"` | same | KILLED by both |
| C4 | service.py:840-841, `_red_finish` set check dropped | `-k cancel` over test_round22/23/24/25/26b | SURVIVED (6 passed, 85 s); likely equivalent, as the red team concluded |
| C5 | set add made unconditional (`if True`) | `-k cancelled` | KILLED by `test_cancelled_admission_ids_survive_the_start_up_replay_of_the_log` |
| U1 | loop.py, round gate: `is_non_utf8_file` dropped | `-k utf8` | KILLED by `test_a_non_utf8_file_written_under_src_fails_the_round` |
| U2 | loop.py, engine end gate `if non_utf8:` -> `if False:` | `-k utf8` | SURVIVED |
| U3 | service.py, review re-scan gate disabled | `-k utf8` | SURVIVED |
| U4 | srcdiff.py, final `close()` removed (last diff section unchecked) | `-k utf8` | KILLED by `test_non_utf8_source_in_a_diff_is_named_and_utf8_text_is_not` |
| U5 | srcdiff.py, decode with `"replace"` | `-k utf8` | KILLED by the same test |
| U6 | `is_non_utf8_file` reads only 10 bytes | `-k utf8` | KILLED by both utf8 tests |

I ran no other delivery tests. Grep shows no test other than `test_round26b.py` mentions non-UTF-8 content or `non_utf8_src_change`.

### Findings

**MF-5, Low: L2, the review half of N25-D-4 is untested.** A replayed failing review after `DLV_MAX_FINDINGS` is lowered is not tested. This confirms R26B-RT-5. The code is correct as written. Falsifier: a replayed failing review returns its recorded answer.

**MF-6, Low: U2 and U3, the engine end-of-run gate (`non_utf8_src_change`) and the service re-scan gate have no test.** Only the per-round gate U1 is tested. This confirms R26B-RT-4. The end gate is the only catch for a deleted Latin-1 file or a symlink, which `is_non_utf8_file` does not judge.

**MF-7, Low (cosmetic):** E5 and E6 survive. Longer hex fields and lowercase hex values are accepted. This is harmless, and the CF value stays shape-bound by the 3-field anchored regex.

C4 is probably equivalent: it cannot be reached with the id only in the cancelled set.

The `__CF_USER_TEXT_ENCODING` rule is darwin-only, and the value shape is pinned against a trailing newline, `;rm`, missing fields and a non-hex value. The mutants that weaken it (E1-E4) are killed.

## 4. devtools/hygiene_check.py

Commands:
- Unit tests: `python -B -m unittest devtools/test_hygiene_check.py -k l3 -k f9 -k strict_allowlist -k system_tmp -k real_tmp -k launch_guard -k RepoIgnore`. The baseline is 10 OK.
- The CI lint step: `python devtools/hygiene_check.py lint --strict-allowlist` (baseline 0 violations).
- Scope: I did not run the full self-test. The two known macOS failures (setsid, subreaper) are unrelated.

| # | location, change | unit result | CI lint step |
|---|---|---|---|
| H1 | the pin takes the whole paragraph, not the sentence | KILLED by `test_l3_a_commit_pins_the_counts_of_its_own_sentence_only` | not needed |
| H2 | `_sentence` start always 0 | KILLED by the same test | not needed |
| H3 | `_sentence` end ignored | SURVIVED | 0 violations, SURVIVED |
| H4 | `_SENTENCE_END` without the whitespace lookahead (3.13.9 splits) | SURVIVED | 4 violations, KILLED |
| H5 | `_SENTENCE_END` drops `;` | SURVIVED | 0 violations, SURVIVED |
| H6 | any hash-looking token pins | KILLED by `test_l3_a_count_pinned_by_an_all_digit_commit_id` | not needed |
| H7 | pinning never pins | KILLED (3 tests) | not needed |
| H8 | `--system-tmp` ignored | KILLED by `test_r3_cases_do_not_watch_the_real_tmp` and `test_r3_new_entry_in_the_given_system_tmp` | not needed |
| H9 | `--system-tmp` set but the default is bogus | KILLED by `test_r3_new_entry_in_system_tmp` | not needed |
| H10 | `lint_expected_skips` check disabled | KILLED by `test_strict_allowlist_flags_an_expected_skip_no_source_can_produce` | not needed |
| H11 | check always flags | KILLED by the same test | not needed |
| H12 | loose `$`-dropped regex removed | SURVIVED | 0 violations, SURVIVED |
| H13 | strict regex removed, loose only | SURVIVED | not run |
| H14 | `apps/` search dir ignored | SURVIVED | 0 violations, SURVIVED |
| H15 | `lint_expected_skips` not wired into `lint` | KILLED (the planted-skip test) | not needed |
| H16 | launch_guard `*/src/launch_guard.py` glob dropped | KILLED by `test_l4_launch_guard_planted` | not needed |
| H17 | nested `*/src/*/launch_guard.py` glob dropped | SURVIVED | 0 violations, SURVIVED |
| H18 | f-string constant text dropped from `_source_strings` | SURVIVED | 0 violations, SURVIVED |
| H19 | `.venv` / `node_modules` filter removed | SURVIVED (the sandbox has no `.venv`) | 0 violations |
| H20 | launch_guard group never compared | KILLED | not needed |

### Findings

**MF-8, Medium: H17.** `test_l4_launch_guard_planted` plants the nested `src/zbm_c/launch_guard.py` as identical to the others. It never plants a differing one. Dropping the nested glob, which is delivery-py's own copy, survives both the unit test and the CI step. delivery-py's copy could then drift from the nine others undetected. The test asserts "not flagged" for the nested file, so it cannot see the glob disappear. Falsifier: plant a differing nested copy and require a violation. Severity Medium because L4 is the only drift guard for the shared copy, and the one non-`src/` layout is exactly the one untested.

**MF-9, Low: H3, H5, H12, H13, H14, H18.** These sub-conditions of `_sentence` and `lint_expected_skips` are untested:
- the sentence-end boundary
- the semicolon
- the loose regex for an f-string with a trailing `$`
- the `apps/` directory
- f-string constant text

Each survives both the unit tests and the real-repo lint. The lint over the real tree stays at 0 violations either way, so the real allowlist does not currently depend on them.

H4 is killed only by the CI lint step, not by the unit tests.

H19 is not testable in this sandbox.

## 5. As-time-allows

### fulfillment small-model pool

Command: `python -m pytest -q tests/test_fix26b_request_memory.py tests/test_fix26b_launcher_guards.py`. Baseline 14 passed, 37 s. The constants are at `api.py:1038-1044`.

| # | change | result |
|---|---|---|
| F1 | `_SMALL_MODEL_PER_BODY_BYTE` 16 -> 8 | KILLED by 4 tests (budget, pool, "not below what it holds" x2) |
| F2 | 16 -> 15 | KILLED only by `test_the_small_model_pool_is_a_fixed_term_beside_the_body_budget` (a pin) |
| F3 | base 16 KiB -> 0 | KILLED only by the same pin |
| F4 | pool 4 MiB -> 64 MiB | KILLED only by the same pin |
| F5 | pool 4 MiB -> 1 MiB | KILLED only by the same pin |
| F6 | `min(...)` cap removed | SURVIVED (equivalent: 16 x 64 KiB + 16 KiB = 1.06 MiB, under 4 MiB) |
| F7 | `await small_model.reserve(...)` removed | KILLED by `test_small_bodies_models_in_flight_at_once_stay_inside_the_budget_while_stalled_senders_hold_it` and 3 "not below" cases |

I did not re-verify the red team's R26B-RT-3 transient-peak claim. These mutants say nothing about it.

### shared launch_guard switch-interval range

The mutated copy was `services/fulfillment-py/src/launch_guard.py`. Command: `pytest tests/test_fix25_switch_interval.py tests/test_fix26b_launcher_guards.py`.

| # | change | result |
|---|---|---|
| G1 | max 50 ms -> 5 s | KILLED by `test_the_launcher_refuses_an_interval_outside_the_range[0.0500001]` and `[0.5]` |
| G2 | min 100 us -> 1 us | KILLED by `...[0.0000999]` |
| G3 | range boundary made exclusive | KILLED by `test_the_launcher_serves_with_the_interval_in_force_it_reports[0.0001-100]` and `[0.05-50000]` |
| G4 | nan accepted by the env check | SURVIVED (equivalent: `sys.setswitchinterval(nan)` sets 0.0, and the in-force check refuses it) |
| G5 | in-force check dropped | KILLED by `test_an_interval_not_in_force_refuses` |
| G6 | SIGTERM previous disposition not restored | SURVIVED in-service tests; killed only by the hygiene L4 copy-drift check |

The earlier run of `test_fix26b_launcher_guards.py` alone missed G1-G3, because the range tests live in `test_fix25_switch_interval.py`. Any all-ten-copies mutant would not trip L4. Only one copy, fulfillment's, was exercised here. The red team did legal-py for the SIGTERM and max-interval cases.

**MF-10, Low: G6.** Restoring the previous SIGTERM disposition is untested in the services' own suites. A single-copy drift is caught by L4, but a change made in all ten copies is not.

### ledger-rust `common::unique_suffix`

Command: `CARGO_TARGET_DIR=<scratch> cargo test --offline --test harness_names`. Baseline 3 passed. The mutants are in `tests/common/mod.rs`.

| # | change | result |
|---|---|---|
| R1 | `fetch_add` -> `load` | KILLED by `unique_suffixes_never_repeat_within_a_process` and `port_files_made_at_the_same_instant_from_many_threads_get_distinct_paths` |
| R2 | counter dropped from the suffix | KILLED by the same two tests |
| R3 | pid dropped | SURVIVED |
| R4 | time dropped | SURVIVED |
| R5 | `PortFile` bypasses the scratch dir | KILLED by `every_scratch_path_lives_in_one_per_process_directory` |
| R6 | `PortFile` uses the label only | KILLED by the distinct-paths test |
| R7 | atexit `remove_dir` removed | SURVIVED |

**MF-11, Low: R3, R4, R7.** Cross-run uniqueness (pid and time) and the empty-directory cleanup at exit are not testable inside one process or are untested. This matters only for the wave's "no leftover temp entry" claim.

## Not mutated or not run

- Docker, Linux, the macos-26 and ubuntu CI legs, and delivery-docker-live. No CI result for d80adc5 was seen here.
- The full delivery-py suite (about 1 hour). Only the targeted files above ran.
- `graceful_close.py`, which is unchanged by 26b, and the other services' copies of `launch_guard.py` and `serve.py`. Only the detection `serve.py` and the fulfillment `launch_guard.py` copy were mutated.
- The R26-1 macOS `_environ_of` path in hygiene_check, `lint_counts` `COMMIT_RE` commit existence, and the `hygiene-static` self-test on Linux.
- Fulfillment mutants other than the small-model pool: the red team's R26B-RT-1, RT-2 and RT-6 are not re-run.
- The transient parse peak versus the reservation (R26B-RT-3).
- No multi-process race, replay or tenant-escape attacks. The attack classes run were boundary, traversal-style head abuse (oversized and malformed heads, partial request lines), refusal-path and cap mutation, ordering and replay mutation (delivery admission and idempotency), and substitution (the non-UTF-8 gate).
- Resource-exhaustion fuzzing was limited to a head and header flood and 300 stalled partial lines against detection. No body-size, concurrency or memory-exhaustion fuzzing was run.

## Findings summary

| ID | Severity | Item |
|---|---|---|
| MF-1 | Medium | detection `serve.py` parser-level head cap untested (D1, D2, D3, D10 survive; only D6 and D7 kill) |
| MF-8 | Medium | hygiene L4 nested `launch_guard.py` glob untested (H17) |
| MF-2 | Low | detection head tests accept any non-200 or a bare close (D8, D9) |
| MF-3 | Low | detection head timeout (S1) and keep-alive (S4) unpinned |
| MF-4 | Low | orchestrator longer timeouts (G2, G5-G9) caught only by the unit pin |
| MF-5 | Low | delivery review cap-before-idem order untested (L2) |
| MF-6 | Low | delivery engine end gate and re-scan gate untested (U2, U3) |
| MF-7 | Low | CF value regex hex width and case (E5, E6) |
| MF-9 | Low | hygiene sentence boundary, `apps/`, f-string and loose-regex branches untested |
| MF-10 | Low | SIGTERM restore untested per service (G6) |
| MF-11 | Low | ledger harness pid, time and atexit-cleanup mutants survive (R3, R4, R7) |

No finding is Critical or High. I found no bypass of a protection: no mutant that weakens a real refusal passes every layer unnoticed except through the redundancy noted in MF-1. I am not making a verdict.

## Gate result

NON_BLOCKING_FINDING. There are two Medium and nine Low findings and no Critical or High. CI4-2: the tolerant `test_long_query_string_is_refused` at d80adc5 does not weaken the test against its previous body. On this box the previous body failed 10 of 20 unmutated runs, and every mutant it killed on all 5 runs, D6 and D7, the new body kills too.

Nothing was committed or pushed.

Key paths:
- Sandbox worktree: `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/mutation-d80adc5`
- Scripts and raw outputs, in `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/`:
  - `mut.sh`, `dmut.sh`, `gomut.sh`, `dlvmut.sh`, `hmut.sh`, `hmut2.sh`, `hmut3.sh`, `fmut.sh`, `fmut2.sh`, `fuzz.py`
  - `go-mut.out`, `dlv-mut.out`, `h-mut.out`, `h-mut2.out`, `f-mut.out`
- Code under mutation:
  - `services/detection-py/src/serve.py` and `services/detection-py/src/api.py`
  - `services/orchestrator-go/cmd/orchestrator/main.go`
  - `services/delivery-py/src/zbm_delivery/service.py`
  - `services/delivery-py/src/zbm_delivery/config.py`
  - `services/delivery-py/src/zbm_delivery/engine/loop.py`
  - `services/delivery-py/src/zbm_delivery/engine/srcdiff.py`
  - `devtools/hygiene_check.py`
  - `services/fulfillment-py/src/api.py` and `services/fulfillment-py/src/launch_guard.py`
  - `services/ledger-rust/tests/common/mod.rs`
