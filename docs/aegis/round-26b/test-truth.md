# AEGIS 26b re-adjudication: test-truth gate (4a), candidate d80adc5528ddad4f27427098efe2beee32ee5d6a

Worktree: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5. HEAD was verified as d80adc5.

Read-only on the repo. `git status --short` in the worktree was empty after every run. The probes and mutants ran in scratchpad copies (`probe_ci42.py`, `mut/`). Runs used the detection-py and delivery-py `.venv` from the main checkout, which the worktree lacks.

I do not render the overall verdict.

## Task A: CI4-2 ruling

### A1. What property the test claims, and what the product promises

- The module docstring says "a request head larger than the header cap is refused, not buffered" (`test_request_limits_live.py:23`).
- `services/detection-py/src/serve.py:9-11` says an oversized request line or header block "is refused (400) while it is still being read". `h11_max_incomplete_event_size = MAX_HEADER_BYTES` is set at `serve.py:171`.
- `services/detection-py/src/api.py:177-181,191,232` has `MAX_HEADER_BYTES = 16 * 1024`. The middleware re-checks the head and returns 431 under any launcher.
- ADR `docs/adr/0001-revenue-recovery-1a-architecture.md:122` says: "Request line + headers | 16 KiB | 400 (h11 parser, while reading); 431 (middleware, any launcher)".
- So the product does promise specific statuses: 400 from the parser, 431 from the middleware. It does not promise 414.
- The test asserts only `status != 200`. The text of the test and its comments claim only "refused, not served".

### A2. Measured server behaviour

Probe: a real `serve.py` on the worktree src, sent the new test's request.

| Request line | Result (count) |
|---|---|
| 1 MiB, N=100 | 400 in 100 of 100. The send raised nothing in 9, ConnectionResetError in 61, BrokenPipeError in 30. |
| 60 KB, N=100 | 400 in 100 of 100, no send error. |
| 17 KB and 20 KB, N=100 each | 431 in 100 of 100 each. |
| 100 B | 200 in 100 of 100. |

- After the probe a normal `/health` returned 200.
- After `terminate()` a connect gave ConnectionRefusedError.
- The server answers 400 even when the client's send raised BrokenPipe or Reset. On this Mac (macOS 26), the early close does not stop the response from being read.
- On the CI macos-26 runner the same race produced BrokenPipeError in `sendall` (OPEN.md CI4-2). I did not reproduce that. Whether the reset discards the 400 there is not established.

Targeted run of the two tests, both passing:
`pytest tests/test_request_limits_live.py -k "long_query or header_cap"` gave `2 passed, 7 deselected in 0.62s`.

### A3. Outcome matrix, OLD vs NEW

`_read_response` (`test_request_limits_live.py:118-137`) on a clean EOF with no bytes: `data=b""`, so `head` is empty and `status = 0`. A clean EOF therefore returns status 0, not an exception. This holds in both versions.

| Outcome | OLD (via `_request`, lines 140-162) | NEW (lines 440-453) |
|---|---|---|
| Server answers 400 or 431 | pass | pass |
| Server answers 200 (cap broken) | FAIL | FAIL |
| Clean EOF with no bytes | pass (status 0) | pass (status 0) |
| BrokenPipe or Reset in `sendall` | ERROR (test fails, uncaught) | pass (ignored) |
| ConnectionResetError on read | ERROR | pass (status 0) |
| Read timeout (10 s, server silent) | ERROR (`TimeoutError`) | ERROR (not caught) |
| Connect refused (server dead before the test) | ERROR | ERROR (connect is outside the try) |
| Server dies after accept, mid-test | pass on EOF (status 0), ERROR on RST | pass on both |

The NEW test accepts strictly more than the OLD one. The additions are exactly the two exception classes on send and on read.

### A4. Do the newly accepted outcomes hide a defect the OLD test caught?

Yes, by accident, and only partly.

Demonstration in `scratchpad/mut/dummy.py`. The same 1 MiB request line was sent to a stub server that closes (EOF) or resets (RST) every connection, 10 trials each:
- Stub "eof", NEW logic: `!= 200` true in 10 of 10, so it passes.
- Stub "eof", OLD logic: error in 10 of 10 (BrokenPipeError or ConnectionResetError), so it fails.
- Stub "rst": the same split, NEW passes 10 of 10, OLD errors 10 of 10.

So the OLD test failed against a "close every connection" or "crashing" server, but only because a 1 MiB send to a closed peer raises. That was a side effect, not a pinned assertion. The NEW test passes such a server. The defects now admitted:
- a server that closes or resets every connection without ever answering;
- a server that crashes on an over-cap line (the module-scoped `server` fixture could be dead for later tests; see A6);
- a parser that drops the line without the promised 400 or 431.

Genuine defects still caught by the NEW test:
- Mutant 1, run on a scratchpad copy: `MAX_HEADER_BYTES` raised to 64 MiB. Both `test_long_query_string_is_refused` and the header-cap test FAILED (`AssertionError` at line 453).
- `/health?q=` is served 200 if the cap is gone, so the primary defect (no head limit, the original wave-1 finding) is caught.

### A5. Was `status != 200` already too weak before the change?

Yes.
- It passes 400, 431, 413, 500, 503, 404 and status 0 alike.
- It never pinned the 400/431 that ADR 0001:122 and `serve.py:9-11` state.
- A 500 on the over-cap line passes. So does a 503 or a hang-up.
- It was and is a negative-only assertion: "not 200".
- The sibling test `test_request_head_larger_than_the_header_cap_is_refused` (lines 427-437) has the identical weakness. It also uses the same try/except pattern and `status != 200`, with a 4 MiB header. That pattern predates this wave. CI4-2 only brought the long-query test into line with it.

### A6. Does anything else pin the positive behaviour?

- Statuses 400 or 431 on the live server: nothing. `grep` over tests for 431 finds only `tests/test_request_limits.py:91-95` (`test_oversized_head_is_431_under_any_launcher`). That uses TestClient and the middleware, not the h11 parser or a real socket. The h11 400 path, the one this test exercises, has no live status assertion anywhere.
- Server still up afterwards: nothing inside this test. Later tests in the same module and fixture assert 200:
  - `test_slow_request_head_is_cut_off_and_others_are_served` asserts `/health == 200`, but only inside its loop.
  - `test_keep_alive_and_pipelined_requests_still_work...` asserts 200.
  - The fixture itself asserts 200 only at start-up.
- These are order-dependent and indirect. A crash caused by the long query would surface in a later test, not in this one.
- Other live suites configure `h11_max_incomplete_event_size=16*1024` (`test_fix21_graceful_close.py:78`, `test_live_graceful_close_module.py:249`), but I did not confirm any asserts the 400.

### A7. RULING: WEAKENED (Medium)

What is lost:
- The test no longer distinguishes "refused with the documented 400/431" from "connection dropped with no answer".
- It also does not detect a server that crashes or closes on that input.
- Before this change, the dropped-connection and crash classes failed by accident (see A4). Now they pass.
- The positive property "refused with the promised status, and the service keeps serving" is pinned nowhere live.
- The core defect (the head cap removed so the line is served 200) is still caught (mutant 1).
- The tolerance itself is defensible. On a 1 MiB write an early close is inherent to refusing while reading, and it is the product's stated behaviour. The weakness is `!= 200` being the whole assertion, not the tolerance as such.

Severity: Medium. It is a test-strength gap on a negative control, not a product defect. The product answers 400 in 100 of 100 here.

Assertions that would restore the lost strength (recommendation only; I edited nothing):
1. Add a deterministic status pin that avoids the race. Send an over-cap line small enough to fit the socket buffer, for example a 60 KB query. Measured 100 of 100 returned 400 with no send error. Assert `status == 400`. A 17-20 KB line gives 431 from the middleware. Optionally keep the 1 MiB case with `status in (0, 400, 431)`, which drops the "500 passes" hole.
2. After the 1 MiB case, assert a fresh `_request(server, "GET", "/health")` returns 200. This pins "the server survived", and would also catch a crash or a close-everything server.
3. Apply the same two changes to the sibling header-cap test.

### A8. CI4-2 disposition

- It can close as a High (the CI-red cause is fixed and the green run is proven, see Task C), PROVIDED the weakening is recorded as a new Medium finding (id suggestion `CI4-2b`).
- Re-grade the residual: High is not warranted. Do not close the weakening silently. I recommend closing CI4-2 and opening `CI4-2b` as Medium, closure condition being the assertions in A7 items 1 and 2.
- The founder's closure condition (an AEGIS ruling on whether early close weakens the test) is answered above: it weakens it, Medium.
- If the adjudicator holds that "closes without a status" must not count as a refusal at all, then A7 items 1 and 2 are required before closing.

## Task B: the other three changes

**B1. Delivery `test_round26b.py`: `wait_idle(240)` before counting events (CI4-1). NOT WEAKENED. A Low observation remains.**

- `wait_idle` (`service.py:550-564`) blocks until no run is live and `_queue.unfinished_tasks == 0`. It raises `TimeoutError` after the timeout, so a stuck first run fails the test rather than passing it.
- The baseline count `n_events` is now taken after the first run's own background events (crossing_git, local_log) are done, which was the 22 == 20 flake. The assertions `len(h.events()) == n_events` (replay records nothing, and the capped new request records nothing) are unchanged, and the cap-before-lookup mutant is stated to still fail it (OPEN.md CI4-1).
- It does not hide a real event-count defect, because the replay must still add zero events and a new request must still be refused 422 with zero events.
- Low (new, `W26B-T1`): the post-replay count is read right after `post`, without a second `wait_idle`. A defective replay that re-enqueues a run and records asynchronously could be missed by the count. The `finally` does `wait_idle` but then counts nothing. Restoring assertion: `h.svc.wait_idle(240)` between the replay and the final two `len(h.events())` checks. This weakness existed before the change; it did not worsen.
- I ran the test here: `1 passed, 13 deselected in 9.42s`.

**B2. Go `trickle` start clock moved before the dial (CI5-1). NOT WEAKENED (conservative direction).**

- Go's `ReadTimeout` clock starts at accept, which is at or after the dial's connect. `start := time.Now()` before `net.Dial` is therefore always at or earlier than the server's start, so `open >= timeout` can no longer fail on a legitimately timeout-cut connection.
- The only slack created is the dial latency, about a fraction of a millisecond on loopback. A too-early cut passes only if it is less than that slack early.
- The lower bound is also the property check that "the timeout is what cut it". The exact configured timeouts remain pinned by `TestServerHasExplicitTimeoutsAndLimits` (cited in the test comment). I did not read that test.
- A cut at, for example, 14 s or 10 s still fails. The test names the 408 and "no scan started before the body arrived" assertions too (lines 132-143).
- Local run: `go test -count=1 -run 'TestSlowBodyIsCutOff|TestSlowlorisHeader'` gave `ok ... 15.932s`.

**B3. `ci.yml` delivery-py `timeout-minutes` 60 to 120 (CI4-3). NOT WEAKENED as a test, with a Medium process caveat.**

- A job limit is a hang guard, not a test assertion. It does not change what any test accepts.
- A hang is still bounded at 120 min (`ci.yml:205`) and individual tests keep their own timeouts, for example `wait_idle` raising `TimeoutError`.
- A real hang would now cost up to 2 h of runner time before cancel. The slowest observed leg took about 74 min (macos 4424 s test step, 17:15:08 minus job start 16:00:36), well inside the limit.
- Runtime itself is tracked as W26B-4 (Medium, open).

## Task C: CI run 37135247923 accounting

Run metadata: `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/aegis26b-r2/ci6-run-37135247923.json`. Command: `gh api repos/DarksiedCEO/zbm-zbc/actions/jobs/<id>/logs`. Logs are saved as `job-<id>.log` in the same directory.

Raw: `headSha d80adc5528ddad4f27427098efe2beee32ee5d6a`, `conclusion success`, `event workflow_dispatch`, `headBranch fix26b`, and the aggregate `required` job `success`.

| Job (id) | Raw summary line from the log | Passed | Failed | Skipped | Hygiene |
|---|---|---|---|---|---|
| detection-py 3.12 ubuntu-24.04 (111238390828) | `504 passed, 1 warning in 42.35s`; `tests counted: 504; skips: 0; hygiene violations: 0` | 504 | 0 | 0 | 0 |
| detection-py 3.13 ubuntu-24.04 (111238390832) | `504 passed, 1 warning in 42.08s`; same counted line | 504 | 0 | 0 | 0 |
| detection-py 3.13 macos-26 (111238390975) | `504 passed, 1 warning in 42.23s`; same counted line | 504 | 0 | 0 | 0 |
| delivery-py 3.12 ubuntu-24.04 (111238390368) | `724 passed, 3 skipped, 1 warning in 2990.31s`; `tests counted: 727; skips: 3; hygiene violations: 0` | 724 | 0 | 3 | 0 |
| delivery-py 3.13 ubuntu-24.04 (111238390425) | `724 passed, 3 skipped, 1 warning in 3195.47s`; `tests counted: 727; skips: 3` | 724 | 0 | 3 | 0 |
| delivery-py 3.13 macos-26 (111238390414) | `723 passed, 4 skipped, 1 warning in 4424.28s`; `tests counted: 727; skips: 4; hygiene violations: 0` | 723 | 0 | 4 | 0 |
| orchestrator-go ubuntu-24.04 (111238390417) | `tests counted: 61; skips: 0; hygiene violations: 0`; 61 `--- PASS`, 0 `--- FAIL`, 0 `--- SKIP` | 61 | 0 | 0 | 0 |
| orchestrator-go macos-26 (111238390403) | `tests counted: 61; skips: 0; hygiene violations: 0`; 61 `--- PASS`, 0 FAIL or SKIP | 61 | 0 | 0 | 0 |
| hygiene-static (111238362301) | unittest `Ran 51 tests in 5.779s OK`; `Ran 10 tests in 0.028s OK`; `hygiene_check lint: 0 violation(s)`; `docs/test-counts.md: 13 suites, one row each` | 61 (51 + 10) | 0 | 0 | lint 0 |

- Go `ok` lines per package: `cmd/orchestrator 18.178s` (macOS) and `16.779s` (ubuntu), plus `internal/client` and `internal/orchestrator`. All `ok`.
- The hygiene-static `... ok` line count in the log is 61.
- Local detection-py collection: `504 tests collected in 0.29s`, equal to executed 504 on all three legs, so nothing in the suite was unexecuted.
- The delivery-py `tests counted: 727` is the same on all three legs (724+3, 724+3, 723+4). I did not independently collect the delivery-py denominator.

Skips against the allowlist (`devtools/hygiene_allowlist.json`, `expected_skips["python:delivery-py"]`):
- `tests/test_live_docker.py:72`, `:83`, `:98`: "Docker live: not provable here..." (ubuntu: `DLV_LIVE_SANDBOX_IMAGE is not set`; macOS: `docker CLI unavailable: FileNotFoundError`). They match the allowlist regex `^Docker live: not provable here`. The `delivery-docker-live` job (success) is the proof leg that must run these and fails on any skip.
- macOS only: `tests/test_round22.py:152` "Linux /proc (the scrub is also checked through os.environ)", matching `^Linux /proc \(the scrub is also checked through os.environ\)$`.
- The hygiene checker reported `hygiene violations: 0` on each leg, so R5 (unexpected skips) is clean. detection-py and orchestrator-go have no allowlisted skips and had none.

Did the three changed tests execute, by name, on each OS leg?
- Go: YES, by name, on both OS legs.
  - Ubuntu: `--- PASS: TestSlowlorisHeaderIsCutOffAndOthersAreServed (5.03s)` and `--- PASS: TestSlowBodyIsCutOffBeforeAnyScanAndOthersAreServed (15.03s)`.
  - macOS: the same two tests passed in 5.03 s and 15.04 s.
- Python detection-py and delivery-py: NOT VERIFIABLE by name. CI runs `pytest -q -rs`, so the logs print dots, counts and skips only. `grep test_long_query` and `grep round26b` match nothing in any job log. What can be said:
  - the aggregate counts equal the collected counts (504; 727 delivery in the log),
  - 0 failed and 0 error on every leg,
  - the test is not in the skip lists, so it cannot have been skipped.
  - That implies the test executed and passed, but this is an inference, not a name match.
- The detection macos-26 leg, the one that failed in CI #4, passed as a whole, 504 passed with 0 skipped.
- The CI4-2 race is stochastic. One green run does not show the flake is gone. It did not reproduce in my 100-trial probe either.

## Findings

| id | Severity | Finding | Evidence |
|---|---|---|---|
| CI4-2b | Medium | `test_long_query_string_is_refused` accepts an early close with no status. It passes a server that closes or resets every connection. No live test pins the documented 400 (parser) or 431 (middleware), nor that the server survives the request. | `test_request_limits_live.py:440-453`; ADR 0001:122; `serve.py:9-11`; stub servers eof and rst: NEW passes 10 of 10, OLD errored 10 of 10; real server 400 in 100 of 100 (1 MiB) and 400 in 100 of 100 (60 KB); mutant 1 caught |
| CI4-2c | Low | The sibling `test_request_head_larger_than_the_header_cap_is_refused` (lines 427-437) has the same `!= 200` weakness and was the template for the fix. This predates the wave. | same file |
| W26B-T1 | Low | The replay test does not `wait_idle` between the replay and the final event count, so an asynchronously recording defective replay could be missed. Not a regression from the change. | `test_round26b.py:~95-108` |
| W26B-4 | Medium (existing, open) | delivery-py suite runtime 50-74 min per leg, while the limit is 120 min. | Task C table |

No Critical or High finding from this review.

## Gate result for 4a (test-truth, my share only)

- Accounting: exact and complete for the four named jobs, with 0 failed and skips within the allowlist. See the Task C table.
- Determinism: the legs are green in this run, but I performed no ≥2 reruns of the full suites. Task A and B targeted reruns are described under "what I could not verify".
- Discrimination: one mutant was run locally, by me, in a scratchpad copy, and was killed. `mutation-fuzz` proof was not supplied to me. Per my charter, absent that proof the discrimination dimension is NOT_VERIFIED, and I report no TEST_TRUTH_GREEN.
- Gate: NON_BLOCKING_FINDING for the test-accounting dimension (one Medium and two Lows above). Discrimination for the CI4-2 test: NOT_VERIFIED beyond my single mutant.

## What I could not verify

1. The CI macos-26 BrokenPipe race itself (did not reproduce here, 100 trials on macOS 26, Python 3.14 locally vs 3.13 on CI). I do not know whether, on that runner, the server's 400 is lost when the client sees the pipe error. So I cannot say whether status 0 is ever reached on CI or whether a 400 is always readable.
2. The delivery-py and detection-py test names in the CI logs (quiet `-q` output). The changed tests' execution on each OS leg is an inference from counts, not a name match.
3. The delivery-py denominator (727) was not independently collected here.
4. Whole-suite determinism reruns (≥2) for detection-py, delivery-py, orchestrator-go, and the hygiene-static job. Only the targeted tests were run locally: detection long-query and header-cap (1 run, plus the probe), the delivery replay test (1 run), and the two Go tests (1 run).
5. Mutation proof from `mutation-fuzz` at d80adc5. None was supplied. My own mutants were limited to one real-server mutant (head cap removed) and two stub servers, not a systematic discrimination run.
6. The claim that `TestServerHasExplicitTimeoutsAndLimits` pins the exact timeouts (quoted from a code comment, not read).
7. Whether any other live suite (`test_fix21_graceful_close.py`, `test_live_graceful_close_module.py`) asserts the 400 on an over-cap line. I saw only that they configure the same h11 cap.
8. Behaviour on Linux of the 1 MiB request line against the server (all my probing was macOS).
