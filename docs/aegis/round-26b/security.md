# AEGIS round 26b re-adjudication — SECURITY gate (delta 4c3a21a -> d80adc5)

**Gate verdict: SECURITY_GREEN for the scoped delta.** No Critical or High security finding. One Medium and three Low findings, all in test or evidence quality; none is a defect in the server. `blocking: false`. Overall verdict not rendered.

- Repo: `DarksiedCEO/zbm-zbc`, candidate `d80adc5528ddad4f27427098efe2beee32ee5d6a`, tree `5c059f868fe4426859c737170ae75ea9a94f351e`.
- Worktree: `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5` — HEAD confirmed, `git status --short` empty before and after my probes.
- Scope: the diff from 4c3a21a, detection-py's request-head cap, and the four scanner jobs of run 37135247923. The prior report's other boundaries were not re-tested.
- Mode: read-only. Scratch files (venv, probe scripts, logs) are in `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/sec-d80/`.

## 1. Diff 4c3a21a..d80adc5

`git diff --stat 4c3a21a d80adc5` shows 6 files. Outside `FIX_WAVE_26B_REPORT.md` and `docs/findings/OPEN.md`, exactly four changed:

| File | Change | Security effect |
|---|---|---|
| `.github/workflows/ci.yml` | delivery-py `timeout-minutes: 60` -> `120` plus a comment | None. No scanner, permission, secret or trigger change. |
| `services/delivery-py/tests/test_round26b.py` | adds `h.svc.wait_idle(240)` before counting events | None. Test-only; the replay assertion is unchanged. |
| `services/orchestrator-go/cmd/orchestrator/server_limits_test.go` | `start := time.Now()` moved before `net.Dial` | None. It makes the measured open time slightly longer, so the lower bound (open >= ReadTimeout) is easier to meet by microseconds; an early-cut bug of that size would be masked. |
| `services/detection-py/tests/test_request_limits_live.py` | `test_long_query_string_is_refused` tolerates a send error or reset | See section 2. |

`git diff --stat 4c3a21a d80adc5 -- services/detection-py/src .gitleaks.toml services/*/requirements.txt services/ledger-rust/Cargo.lock apps/dashboard-ts/package-lock.json` is empty. No service source, lockfile or scanner config changed, so the prior report's code-level results (R26B-SEC-1..5) are not disturbed by content. They still need re-binding by the adjudicator under the no-carry-forward rule; I did not re-run them.

## 2. detection-py request-line cap (CI4-2)

### Server code

- **Cap setting:** `services/detection-py/src/serve.py:171` passes `h11_max_incomplete_event_size=MAX_HEADER_BYTES`; `services/detection-py/src/api.py:191` sets `MAX_HEADER_BYTES = 16 * 1024` (16384 bytes, request line plus headers).
- **Enforced while reading:** h11 0.16.0 `_connection.py:485` raises `RemoteProtocolError` when the incomplete-event buffer exceeds the cap; uvicorn 0.46.0 `h11_impl.py:181-184` answers 400 "Invalid HTTP request received." and closes.
- **Read size:** each read hands the parser at most 16 KiB (`graceful_close.py:38`, `:126`). After close starts, nothing more is parsed or buffered (`graceful_close.py:194-196`).
- **Second layer:** the ASGI middleware answers 431 at `api.py:229-232`, but only after the whole head is parsed, so it does not bound memory.
- **Refusal path:** FIN, then a drain of at most 64 KiB or 1 s, then close (`graceful_close.py:35-36`, `:177-185`). A client still sending past the drain bound gets an RST by design (`:11-12`, `:120-124`). The concurrency slot is released at `:165`; timers and the drain set are cleaned at `:205-212`. Slow heads are cut at 10 s (`serve.py:33`, `:119-132`).

### Live probe of the real launcher

Command: `venv/bin/python probe.py real`, which starts `python serve.py --host 127.0.0.1 --port <free>` with cwd at the worktree `src`, on loopback, with a throwaway token and `PYTHONDONTWRITEBYTECODE=1`. Python 3.13.9, uvicorn 0.46.0, h11 0.16.0. Tuples are (status, bytes sent, send error, read error, seconds, peak RSS, response start).

```
pid 52963 port 60542 rss0_KiB 58096 conns0 (0, 2)
total_head_bytes 16384 -> (200, ...)
total_head_bytes 16385 -> (200, ...)
total_head_bytes 16400 -> (200, ...)
total_head_bytes 16500 -> (431, ...)
total_head_bytes 32768 -> (431, ...)
total_head_bytes 32769 -> (400, 32769, None, None, ...)
total_head_bytes 65536 -> (400, ...)
total_head_bytes 1048576 -> (400, 0, 'ConnectionResetError', None, 0.0, 0, b'HTTP/1.1 400 Bad Request...')
64MiB line, 64KiB chunks, 2ms gap -> (400, 65550, None, None, 0.01, 58608, ...) rss_KiB 58608
64MiB line, 1KiB chunks, 1ms gap (slow) -> (400, 16398, None, None, 0.06, 58608, ...) rss_KiB 58608
the_test x30 statuses {400: 30}
200 concurrent 1MiB lines statuses {400: 200} rss before/after KiB 59952 65680
after 3s: health 200 rss_KiB 65680 conns(established/closing, lsof lines) (0, 2) alive True
stderr {'Traceback': 0, 'Exception': 0, 'Invalid HTTP request': 235, 'bytes': 9886}
exit 143
```

### Answers

- **Is the cap enforced before the line is buffered?** Yes. A slow-sent 64 MiB line was cut after 16398 bytes; a fast one after about 64 KiB reached the kernel. RSS did not move (58608 KiB before and after).
- **What bounds it?** `h11_max_incomplete_event_size` = `api.MAX_HEADER_BYTES` = 16384 bytes, with at most one further 16 KiB read buffered, so about 32 KiB per connection.
- **Is the server healthy after refusal?** Yes. After 235 refusals, 200 of them concurrent 1 MiB lines, `/health` was 200, there were 0 established or closing TCP connections, 0 tracebacks, and SIGTERM gave a clean exit 143. RSS rose 5.7 MiB across the 200-connection burst and I did not test whether it returns; it is not growth per refused byte.
- **Is an early close a valid refusal?** Yes. The 1 MiB probe shows it on this Mac: `sendall` raised `ConnectionResetError` and the 400 was still readable. The reset is the designed RST once the client sends more than 64 KiB past the answer. The pre-change test would have died on the send error against a correct server. All 30 runs of the new test logic read a 400.
- **Does the new form weaken the test?** Barely. The old `_read_response` already returned status 0 on a silent close, and `0 != 200` already passed. The only new acceptance is a send error or a reset during the read.
- **Could the test pass with the cap raised or removed?** Yes — see R26B-D-SEC-1. This was equally true of the 4c3a21a form.

### Variant servers

Command: `venv/bin/python probe.py variants`. A scratch launcher (`sec-d80/variant.py`) runs the worktree's own `serve._HeadDeadlineH11Protocol` and `api.app` with the cap parameterised; the worktree is untouched. `the_test` is the logic of `test_request_limits_live.py:444-453`.

```
variant parser_cap=16384 middleware=mw: the_test statuses=[400 x5] -> test PASSES; 64MiB line -> status=400 sent=65550 peak_rss_KiB=58064 (rss0=58032)
variant parser_cap=268435456 middleware=mw: the_test statuses=[431 x5] -> test PASSES; 64MiB line -> status=431 sent=67108878 peak_rss_KiB=135776 (rss0=58256)
variant parser_cap=268435456 middleware=nomw: the_test statuses=[200 x5] -> test FAILS; 64MiB line -> status=200 sent=67108878 peak_rss_KiB=136320 (rss0=57728)
variant parser_cap=16384 middleware=nomw: the_test statuses=[400 x5] -> test PASSES; 64MiB line -> status=400 sent=65550 peak_rss_KiB=57632
httptools parser_cap=None middleware=mw: the_test statuses=[400 x5] -> test PASSES; 64MiB line -> status=400 sent=67108878 peak_rss_KiB=3947840 (rss0=57328)
```

## Findings

### R26B-D-SEC-1 — Medium — the suite does not prove the pre-buffer head cap

- **Class:** NOT_VERIFIED (evidence gap; the control itself is effective at d80adc5, observed above).
- **Claim:** `test_long_query_string_is_refused` (`tests/test_request_limits_live.py:440-453`) and its sibling `test_request_head_larger_than_the_header_cap_is_refused` (`:427-437`) assert only `status != 200`. With the parser cap raised to 256 MiB the test still passes on the middleware's after-the-fact 431, while a 64 MiB request line is fully read and buffered (+77 MiB RSS for one connection). Under `python -m uvicorn api:app` (httptools) it also passes, while one 64 MiB line drove RSS to about 3.9 GB.
- **What the tests do catch:** only removal of both layers (the 200 row above).
- **Nothing else pins it:** `grep -rn "h11_max_incomplete_event_size\|MAX_HEADER_BYTES" services/detection-py/tests` finds only hand-built configs in `test_fix21_graceful_close.py:78` and `test_live_graceful_close_module.py:249`, plus a comment. No test asserts `serve.py` passes the cap, the 400 status, bytes accepted, or memory.
- **Origin:** pre-existing. The 4c3a21a form (`_request(...)` then `assert status != 200`) accepted 431 and 0 as well. The d80adc5 change neither created nor closed it.
- **Input to the CI4-2 ruling:** accepting an early close is legitimate and does not materially weaken the test, but the test was not a proof of the memory-DoS control before or after. Green CI on this test is evidence of "not served 200" only.
- **Reproducer:** `cd .../scratchpad/sec-d80 && venv/bin/python probe.py variants`.
- **Falsifier:** a test at d80adc5 that fails on the `parser_cap=268435456 middleware=mw` variant — for example one asserting status 400, or that a slow-sent over-cap line is closed within about 32 KiB.

### R26B-D-SEC-2 — Low — reset-as-refusal also accepts a crashed server

- **Class:** NOT_VERIFIED (test quality).
- **Claim:** with `status = 0` on `ConnectionResetError` and ignored send errors (`:447-452`), a server that died on the request would satisfy this test. The old form accepted a silent close but not a reset.
- **Mitigation:** the fixture is module-scoped (`:75`), so the later tests (`:456`, `:481`) would fail against a dead server. Nothing inside this test checks `/health` afterwards.
- **Observed:** the real server answered 400 in 30/30 runs and never crashed.
- **Falsifier:** a `/health == 200` check after the refusal inside the test.

### R26B-D-SEC-3 — Low — effective head bounds are not exactly 16 KiB

- **Class:** UNKNOWN (accuracy of a documented limit); bounded, not a DoS.
- **Claim:** heads of 16385-16400 total bytes are served 200, because the middleware sum at `api.py:229-230` leaves out the method, the HTTP version and line framing. Heads of 16500-32768 bytes pass the parser (the check at h11 `_connection.py:485` only fires on an incomplete event, and reads are 16 KiB) and are refused 431 by the middleware; 32769 and above get the parser's 400.
- **Effect:** the per-connection buffered head is at most about 32 KiB, not 16 KiB. The README's "16 KiB head cap" is approximate.
- **Reproducer:** `probe.py real`, the `total_head_bytes` rows.
- **Falsifier:** a 16385-byte head refused.

### R26B-D-SEC-4 — Low — two audit jobs print no package count; gitleaks skips merge diffs

- **Class:** NOT_VERIFIED (evidence quality).
- **Claim:** pip-audit and npm audit print only "No known vulnerabilities found" / "found 0 vulnerabilities" in CI, so the run's logs alone do not show how many packages were audited. I established the denominators locally (section 3).
- **Claim:** gitleaks scans patch output of non-merge commits. The 59 merge commits were not scanned as merges, and 3 of them have a non-empty combined diff (conflict-resolution content). I did not scan those 3.
- **Falsifier:** `pip-audit -f json` and `npm audit --json` output in CI; `--log-opts` including `-m` or `--cc`.

## 3. CI run 37135247923 — scanner jobs

`gh run view 37135247923`: workflow CI, `workflow_dispatch`, branch fix26b, **headSha d80adc5528ddad4f27427098efe2beee32ee5d6a**, conclusion success, 53 jobs all success. The `required` job log lists all 13 needs as `success` and none skipped, so the scanners ran rather than being path-filtered.

| Job (id) | Evidence it scanned something | Reconciled against the repo |
|---|---|---|
| secret-scan (111238362277) | `fetch-depth: 0`, all 63 origin branches fetched, checksum `OK`, version `8.30.1`, `200 commits scanned.`, `scanned ~12574330 bytes (12.57 MB) in 1.19s`, `no leaks found` | `git rev-list --count --no-merges --remotes=origin --tags` = 200 (259 with merges). Exact match. |
| audit-rust (111238362562) | `Fetching advisory database from https://github.com/RustSec/advisory-db.git`, `Loaded 1290 security advisories`, `Scanning Cargo.lock for vulnerabilities (70 crate dependencies)`; no warnings | `grep -c '^name = ' services/ledger-rust/Cargo.lock` = 70. Match. The cargo-audit binary came from cache; the advisory DB was fetched fresh. |
| audit-python (111238362340) | 10 `== services/*-py/requirements.txt` lines, each followed 6-7 s later by `No known vulnerabilities found`; the uv.lock step prints the two excluded git-sourced deer-flow packages and the same result. No count printed. | 10 requirements files exist, 5 pins each. A local `pip-audit==2.10.1 -r services/detection-py/requirements.txt --strict -f json` audited 26 dependencies, 0 with vulnerabilities. `uv.lock` has 211 packages, 2 excluded. |
| audit-node (111238362143) | `found 0 vulnerabilities` about 0.8 s after start. No count printed. | A local `npm audit --audit-level=high --json` on the same lockfile reports 63 dependencies in total and 0 vulnerabilities at every level, so the `high` threshold hid nothing. |

**Masking in ci.yml:** `grep -nE 'continue-on-error|\|\| true'` finds no `continue-on-error` anywhere. `|| true` appears at:
- line 476 — docker digest lookup, not a scanner;
- line 536 — registry container cleanup, not a scanner;
- line 570 — the `grep` that prints the excluded git-sourced packages; the `pip-audit` call on line 571 is not masked.

The scanner steps fail the job on findings: `--strict` with `set -euo pipefail` (556-571), bare `cargo audit` (593), `npm audit --audit-level=high` (608), gitleaks `--exit-code 1` (635). All four are in `required.needs` (681-684).

**Detection leg:** `python-tests (detection-py, 3.13, macos-26)` (111238390975) reports `504 passed`, `tests counted: 504; skips: 0; hygiene violations: 0`. This is the leg that was red in CI #4.

## Boundaries tested (delta scope)

| Boundary | Tested | Method |
|---|---|---|
| detection-py request-line/head size cap (memory DoS) | yes | live loopback probe: 12 head sizes, 64 MiB fast and slow, RSS sampled |
| Refusal-path health (leaked connections/tasks, exceptions) | yes | 200 concurrent 1 MiB lines, then lsof, `/health`, stderr scan, exit code |
| Slow-sent over-cap line | yes | 1 KiB per 1 ms, cut at 16398 bytes |
| Whether the test suite proves the cap | yes | test logic run against 5 server variants |
| CI scanners actually scanning; failure masking | yes | job logs plus local reconciliation; ci.yml grep |
| The other three changed files | yes | diff read only |

Denominator: 6 declared, 6 tested.

## Not verified

- CI's pip-audit and npm audit package counts (not in the logs; local reproduction only, pip-audit for 1 of 11 inputs).
- The 3 merge commits with non-empty combined diffs, which gitleaks does not scan.
- The macos-26 runner's actual outcome inside the test (400 read versus reset); only "passed" is logged.
- Whether the +5.7 MiB RSS after the 200-connection burst returns over time.
- Slowloris below the cap: the 10 s head deadline was read (`serve.py:119-132`), not exercised.
- A sustained flood beyond 200 connections, and Linux behaviour (the probe ran on macOS/arm64 only).
- The prior report's five findings and its other boundaries were not re-run at d80adc5.

A peer process running mutation scripts (`dmut.sh` against `services/detection-py/src`) was visible in the process list during my run. It is not mine, and my status checks of the shared worktree were clean.

```json
{"candidateSha":"d80adc5528ddad4f27427098efe2beee32ee5d6a","treeOid":"5c059f868fe4426859c737170ae75ea9a94f351e","gate":"SECURITY","scope":"delta 4c3a21a..d80adc5 + detection-py head cap + CI run 37135247923 scanner jobs","verdict":"SECURITY_GREEN","blocking":false,"findings":[{"id":"R26B-D-SEC-1","severity":"MEDIUM","class":"NOT_VERIFIED"},{"id":"R26B-D-SEC-2","severity":"LOW","class":"NOT_VERIFIED"},{"id":"R26B-D-SEC-3","severity":"LOW","class":"UNKNOWN"},{"id":"R26B-D-SEC-4","severity":"LOW","class":"NOT_VERIFIED"}],"denominator":{"boundariesDeclared":6,"boundariesTested":6}}
```

