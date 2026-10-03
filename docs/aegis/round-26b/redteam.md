# AEGIS 26b re-adjudication: red team / claim falsification

**Gate result: NON_BLOCKING_FINDING.** No Critical or High finding; three Medium and four Low. CI4-2 is not falsified in the dangerous direction, but the test is measurably weaker than before; the evidence for your ruling is in section 1.

## Subject and method

- **Subject:** zbm-zbc, candidate `d80adc5528ddad4f27427098efe2beee32ee5d6a` (= `origin/fix26b`), tree `5c059f868fe4426859c737170ae75ea9a94f351e`.
- **Scope:** the five claims in the brief; code diff `4c3a21a..d80adc5 -- .github services` (4 files).
- **Sandbox:** `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/redteam-d80adc5` (detached at d80adc5).
- **Mutants:** every mutant was planted there by script and restored in a `finally`. At the end `git status --short` in the sandbox is empty and HEAD is d80adc5. The main repo is untouched (only the pre-existing `.git.backup-preimport/` and `.idea/` are untracked).
- **Platform:** macOS arm64, no Docker. Detection venv is Python 3.14.4 (CI uses 3.12/3.13), delivery venv 3.13.9, Go 1.25.5. The repo's own venvs were used as interpreters only, with `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`.
- **Scripts and logs:** `<scratchpad>/rt/` holds `mut.py`, `mut2.py`, `probe.py`, `gomut.py`, `dmut.py` and `logs/` (all 53 job logs of run 37135247923, plus the macOS delivery logs of CI #4 and #5).
- **Denominator:** 5 claims identified (the brief's list), 5 attacked. 20 mutants or probes executed (9 detection, 7 Go, 5 delivery, less one shared baseline each, plus one full-suite run).
- **Not repeated:** everything in the earlier red-team report on 4c3a21a.

## Findings

| ID | Severity | Class | Claim | One line |
|---|---|---|---|---|
| RT2-1 | Medium | MUTANT_SURVIVED | CI4-2 | New long-query test passes for a silent close, a crashed server and a poisoned server; the old test failed all three |
| RT2-2 | Medium | MUTANT_SURVIVED | CI4-2 surface | Removing the parser head cap (16 KiB to 64 MiB) passes the whole detection suite, 504/504 (same on 4c3a21a) |
| RT2-3 | Medium | CLAIM_FALSIFIED (partly) | CI4-3 | 120 min is not clear of observed runner variance: CI #4's macOS pace projects to 115–132 min |
| RT2-4 | Low | CLAIM_FALSIFIED | CI4-2 text | "The race itself does not reproduce here" is false: the old test fails 3 of 5 runs on this Mac |
| RT2-5 | Low | VACUOUS_SUCCESS_CONDITION | CI4-2 neighbour | The next test's `/health` check is skipped when the server closes at once |
| RT2-6 | Low | MUTANT_SURVIVED | CI5-1 | Lower bound has about 1–2 ms resolution and no upper bound; a main()-level override escapes the pin test |
| RT2-7 | Low | INSUFFICIENT_EVIDENCE | CI4-1 | The `wait_idle` fix is not verifiable locally (a no-op `wait_idle` passes 3/3); it rests on CI alone |

## 1. CI4-2 — `test_long_query_string_is_refused` (High, open)

Command per cell: `pytest -q -p no:cacheprovider <file> -k test_long_query_string_is_refused`, three runs each. OLD is the 4c3a21a file copied in as a temporary untracked test (removed); NEW is the d80adc5 file. Mutants are in `services/detection-py/src/serve.py` (a `data_received` override on `_HeadDeadlineH11Protocol` that fires once more than 16 KiB of head has arrived) or `src/api.py`.

| Mutant (server defect) | OLD test | NEW test |
|---|---|---|
| M0 baseline | 3/3 pass | 3/3 pass |
| M1 parser cap 16 KiB to 64 MiB (middleware 431 intact; server buffers the whole 1 MiB line) | 3/3 pass | 3/3 pass |
| M1b cap removed in parser and middleware (line read, answered 200) | 3/3 FAIL `assert 200 != 200` | 3/3 FAIL `assert 200 != 200` |
| M2 `transport.abort()`, no HTTP answer | 3/3 FAIL ConnectionResetError | **3/3 pass** |
| M2b `transport.close()` (FIN), no HTTP answer | 3/3 FAIL ConnectionResetError | **3/3 pass** |
| M3 process crash (`os._exit(1)`) on the long line | 3/3 FAIL ConnectionResetError | **3/3 pass** |
| M5 hang (`pause_reading`, never answers) | 2 FAIL TimeoutError, **1 pass** (10.47 s) | 3/3 FAIL TimeoutError |
| M7 early `200 OK` then close | 3/3 FAIL ConnectionResetError | 3/3 FAIL `assert 200 != 200` |
| M10 poisoned server (aborts this and every later connection) | 3/3 FAIL ConnectionResetError | **3/3 pass** |

- **What still holds:** no mutant in which the over-cap line is served (200) passes the new test (M1b, M7). The assertion `status != 200` still has teeth for "accepted and served".
- **What weakened (RT2-1):** the new test accepts three wrong-server states that the old test rejected: silent close with no HTTP answer, process death, and a server that refuses everything afterwards. The old test rejected them only because any reset or EPIPE failed it, which is also why it flaked.
- **Liveness after the long line:** nothing in the test checks it. Running the module tail (`-k "header_cap or long_query or slow_request_head or keep_alive"`, NEW file) shows what the neighbours catch:
  - **M3 crash:** `test_request_head_larger_than_the_header_cap_is_refused` PASSED although the server died during it. The next three failed with `ConnectionRefusedError`. A crash is caught, but only by the following test and attributed to the wrong one.
  - **M10 poisoned:** header-cap PASSED, long-query PASSED, slow-request-head PASSED, and only the keep-alive test failed (`assert 0 == 200`).
  - **M2 silent abort:** `4 passed`. Nothing in the module tail distinguishes "400/431 answered" from "connection dropped without a word".
- **RT2-5:** `test_slow_request_head_is_cut_off_and_others_are_served` passed under M10 because its first `s.sendall(b"a")` raises `OSError` and the loop breaks before the `/health` request. Its "others are served" assertion can run zero times.
- **Real server behaviour here (`probe.py`, five connections per size):**
  - 8 KiB and 15 KiB: 200.
  - 17 KiB: `431`.
  - 64 KiB: `400 Bad Request`.
  - 1 MiB: `send=BrokenPipeError recv=HTTP/1.1 400 Bad Request`, 5/5; server alive afterwards.
  - So on this machine the real server always answers 400, even when the client's send breaks. The `status = 0` branch is not needed for the real behaviour seen here; it only widens what passes.
- **RT2-4:** OPEN.md says "the race itself does not reproduce here". The OLD file with `-k "header_cap or long_query"`, five runs, gave BrokenPipeError, pass, ConnectionResetError, pass, BrokenPipeError. This supports that the old test was racy; only the statement is wrong.
- **RT2-2:** with M1 planted, the full detection suite (`python -m pytest -q -p no:cacheprovider`) gives `504 passed`. The module docstring's "refused, not buffered" and `api.py`'s "the real bound is in the HTTP parser" are enforced by no test. This predates the wave.
- **M5 note:** the old test's one pass under a hang is the 10 s head deadline (`REQUEST_HEAD_TIMEOUT_S = 10.0`) closing cleanly as the client's 10 s timeout expires. The new test has the same shape; I saw 3/3 fail but the race exists in principle.

Reproducers and falsifiers:
- **RT2-1:** reproduce with `rt/mut.py` rows M2, M2b, M3, M10. Falsifier: assert the answer is 400 or 431 when one is read, and after the long-line connection open a fresh one and assert `/health` is 200. If a bare close is ruled acceptable, the liveness check alone kills M3 and M10.
- **RT2-2:** reproduce with `h11_max_incomplete_event_size=64 * 1024 * 1024` in `serve.py` main(). Falsifier: a test that pins the launcher kwarg, or that fails when the server reads more than a small multiple of 16 KiB of a never-ending request line before closing.
- **RT2-5:** falsifier: require at least one successful `/health` inside the trickle loop.

## 2. CI5-1 — Go slow-client clock before the dial (closed in 08a8b8d; "pending CI" in the candidate tree)

Command: `go test -race -count=1 -v -run 'TestSlowBodyIsCutOff|TestServerHasExplicitTimeoutsAndLimits' ./cmd/orchestrator`. The mutant is `srv.ReadTimeout = <expr>` planted in `main()` right after `newServer(...)`, so the struct pin test cannot see it.

| Planted ReadTimeout | Clock | Measured | Slow-body test | Pin test |
|---|---|---|---|---|
| baseline | new | 15.002557 s | PASS | PASS |
| 15 s − 100 µs | new | 15.004798 s | **PASS** | PASS |
| 15 s − 100 µs | old (after dial) | 15.001927 s | **PASS** | PASS |
| 15 s − 500 µs | new | 15.001145 s | **PASS** | PASS |
| 15 s − 2 ms | new | 14.999928 s | FAIL | PASS |
| 15 s − 20 ms | new | 14.981191 s | FAIL | PASS |
| 15 s − 200 ms | new | 14.801999 s | FAIL | PASS |
| 2 × 15 s | new | 30.004590 s | **PASS** | PASS |

- **The attack as posed did not land materially.** Moving the clock adds only loopback dial time. The padding that hides a short timeout is 1–5 ms of close-detection latency, present with either clock position; the 100 µs mutant passes with both.
- **The claim's logic survives.** The client's start precedes the server's deadline start, and Go deadlines do not fire early, so a correct server cannot read short.
- **RT2-6:** a timeout short by 0.5 ms or less survives, and a doubled timeout survives (the hang guard is 45 s). `TestServerHasExplicitTimeoutsAndLimits` pins `newServer`'s struct only and passed under every mutant. The upper bound was dropped deliberately in wave 25. Falsifier: pin the running server's timeouts, or add an upper bound against a yardstick.
- **CI evidence on d80adc5 (run 37135247923):** ubuntu slow-body 15.00309247 s and slow-header 5.001450256 s; macOS 15.004552541 s and 5.001964917 s. Both legs report `tests counted: 61; skips: 0`.
- **Binding note:** the CI5-1 "closed" row exists only in commit 08a8b8d, which is docs-only (`FIX_WAVE_26B_REPORT.md`, `OPEN.md`) and on no remote (`git branch -r --contains 08a8b8d` is empty). The Go fix itself (700fce6) is in the candidate and was exercised by CI #6.
- **Limit:** one run per mutant, local only; padding on a loaded CI runner will be larger, so the surviving-shortfall window is wider there.

## 3. CI4-1 — `wait_idle(240)` before the event count (closed)

Command: `pytest -q -p no:cacheprovider tests/test_round26b.py -k replays_its_answer` in `services/delivery-py`.

| Mutant | Result |
|---|---|
| D0 baseline | 1 passed |
| D1 `cap_check()` moved before `self._idem(...)` in `create_fix_run` | FAIL `assert 422 == 202` — the mutant claim is true |
| D2 replay branch records an event | FAIL `assert 267 == 265` — "a replay records nothing" still has teeth |
| D3 `wait_idle` made a no-op in `service.py` | pass 3/3 |
| D4 test without the new `wait_idle` line (the 4c3a21a test) | pass 3/3 |
| D5 test calls `wait_idle(0.0)` | FAIL `TimeoutError: engine did not go idle` |

- **Silent timeout:** not possible. `wait_idle` (`src/zbm_delivery/service.py:550-564`) raises `TimeoutError` when the deadline passes, and D5 shows it.
- **Early return:** I found none. The run is committed in a live state and `_queue.put` happens before the route returns; `task_done` is called only after `execute` returns.
- **RT2-7:** D3 and D4 show the CI #4 race does not occur on this Mac. The fix's effect is evidenced only by CI: both ubuntu legs red in CI #4, green in CI #5 and CI #6 (4 of 4).
- **Side effect:** the replay is now always of a settled run; replay while the run is still live is no longer exercised by this test.
- The review half of this ordering is still untested (R26B-RT-5 in the earlier report).

## 4. CI4-3 — delivery-py job limit 60 to 120 min "a hang guard"

**No hang found.** CI #4's macOS leg was 6.7 min into a segment that takes 10.7–11.8 min when it was cancelled.

Minutes from suite start to each pytest progress line, `delivery-py (3.13, macos-26)`:

| Run | 9% | 19% | 29% | 39% | 49% | 59% | 69% | 79% | 89% | 99% | 100% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| CI #4 37125312276 (d40de72) | 9.2 | 15.0 | 23.7 | 32.3 | 40.6 | 52.5 | cancelled at 59.2 | | | | |
| CI #5 37129375575 (b24ffde) | 9.5 | 15.5 | 20.1 | 26.0 | 29.3 | 38.7 | 50.5 | 53.6 | 73.2 | 82.3 | 84.5 |
| CI #6 37135247923 (d80adc5) | 7.5 | 12.0 | 15.9 | 19.8 | 21.9 | 29.4 | 40.1 | 43.2 | 62.5 | 71.7 | 73.8 |

- **RT2-3:** the same suite on the same runner image took 52.5, 38.7 and 29.4 min to reach 59% within three hours. The only code change between those commits is the one `wait_idle` line. CI #4's pace is 1.79× CI #6 and 1.36× CI #5, which projects to 132 min and 115 min for the full suite against the 120-minute job limit.
- **Consequence:** `required` on macOS can go red by timeout with no defect, and a genuine slowdown of that size is indistinguishable from runner noise. W26B-4 (Medium, wave 27) already names "timeout risk"; this puts a number on it. "A hang guard, not a speed bound" is not supported by the three runs.
- **No per-test evidence exists:** the suite runs `pytest -q -rs` with no `--durations`. The slowest segment (79% to 89%, 72 tests in `test_round22/23/24.py`) took 835 s and 870 s on ubuntu and 1158 s on macOS in CI #6. A single test stalling for many minutes inside it would leave no trace.
- **Ubuntu is stable, so the wave did not slow the suite.** Test-step seconds:

| Run | ubuntu 3.12 | ubuntu 3.13 |
|---|---|---|
| a6aee4e (pre-wave) | 3141 | 3226 |
| CI #4 | 3372 | 3207 |
| CI #5 | 3222 | 3154 |
| CI #6 | 2999 | 3206 |

- **Falsifier for RT2-3:** `--durations=25` in the delivery job plus a limit with measured headroom over the slowest observed pace, or the suite split (W26B-4).

## 5. "CI #6 run 37135247923 is 53/53 green including `required`"

Not falsified.

- **SHA binding:** `gh run view 37135247923` gives `headSha d80adc5…`, `workflow_dispatch`, `success`. All 55 checkout lines in the 53 logs show d80adc5.
- **Job count:** 53, matching the matrix (27 python-tests, 3 delivery, 2 ledger, 2 Go, 1 dashboard, 10 live-runs, 1 docker-live, 3 audits, 1 secret scan, 1 hygiene-static, `changes`, `required`).
- **Path filter:** the `changes` log says "no reliable base commit; running every job". `workflow_dispatch` forces all four groups true (`ci.yml:81-91`). No job was skipped.
- **Suppressed failures:** `ci.yml` has no `continue-on-error` and no `|| true` on a test step (only on registry teardown and an informational digest lookup). `required` lists all 12 other jobs plus `changes`; its log prints every one as `success`.
- **Tests actually ran:** every suite job prints a pytest/go/cargo summary and a `hygiene_check: … exited 0; tests counted: N; … hygiene violations: 0` line. The counts equal `docs/test-counts.md` (`--counts` defaults to `check`, `devtools/hygiene_check.py:1238`):

| Suite | Count in CI #6 |
|---|---|
| detection | 504 (×3 legs, 0 skips, including macos-26) |
| fulfillment | 1105 |
| onboarding | 763 |
| creative | 821 |
| compliance | 636 |
| verification | 299 |
| clipper-network | 368 |
| finance | 358 |
| legal | 308 |
| delivery | 727 |
| Go | 61 |
| Rust | 117 (116 on darwin, one named platform-only test) |
| dashboard | 27 |

- **Skips:** only the allowlisted ones. Three Docker-live tests skip in the delivery legs and run in `delivery-docker-live` (`3 passed`, `3 test cases, 0 skipped`). Two `/proc` tests skip on macOS (one delivery, one fulfillment).
- **Other jobs:** hygiene-static `Ran 51 tests … OK`, `Ran 10 tests … OK`, `lint: 0 violation(s)`. Live runs print their checks (for example finance `33/33 checks passed`, both ledgers `valid: True`).
- **Limits:**
  - `cargo audit` prints no explicit clean line; its success is the step's exit status.
  - The detection macos-26 log cannot show which branch the long-query test took (400 read, or reset). One green run of a previously racy test is one sample.
  - CI3-2, CI3-3, CI4-1 and CI4-3 are recorded in OPEN.md as "proven by CI #5 … @ b24ffde", a run that concluded `failure` on a different SHA. CI #6 re-proves those legs on d80adc5, but the rows in the candidate tree cite the wrong run.

## Survived (best attack, limit)

- **CI4-2 "over-cap line is not served":** M1b and M7 fail the new test. Limit: macOS only; whether a Linux client can lose a 200 behind a reset was not tested.
- **CI5-1 lower-bound logic:** see section 2.
- **CI4-1 mutant claim and `wait_idle` soundness:** see section 3.
- **Claim 5:** see section 5. Limit: log reading only; I did not re-run any CI leg.

## Could not test

- Anything on Linux or under Docker, and every CI leg (logs read, not re-run).
- The detection attacks on CI's Python 3.12/3.13 (local venv is 3.14.4).
- Repeated runs for flake rate of the new long-query test on macos-26.
- The full delivery suite locally (too long for a shared machine).
- A replay while the run is still live.
- The Go mutants under CI load.

