RELIABILITY GATE REPORT (AG-RELIABILITY), AEGIS round 26b re-adjudication

Binding: repo zbm-zbc, candidateSha d80adc5528ddad4f27427098efe2beee32ee5d6a. `git rev-parse HEAD` in the review worktree printed exactly this SHA. The worktree stayed clean (`git status --short` empty after the Go test). Probes ran from scratchpad scripts, with PYTHONDONTWRITEBYTECODE and the prebuilt venvs under .../c680f8a3-.../scratchpad/venvs/{detection-py,fulfillment-py}. The Go test used GOCACHE in the scratchpad. Nothing was edited, committed or pushed.

GATE RESULT: RELIABILITY_YELLOW (blocking by contract because of the not-verified surfaces listed below). I found no Critical or High reliability finding. I recorded 4 new Low findings and no Medium. The overall verdict is not rendered here.

=====================================================================
A. CI4-2 (open High): detection-py over-cap request line
=====================================================================
Question: what does the server do when a client sends a 1 MiB request line?

Code path, cited from the candidate:
1. services/detection-py/src/serve.py:168-172 runs uvicorn with `http=_HeadDeadlineH11Protocol` and `h11_max_incomplete_event_size=MAX_HEADER_BYTES` (16 KiB, api.py:191).
2. h11 raises `RemoteProtocolError` once the receive buffer passes 16 KiB. uvicorn 0.46.0, h11_impl.py:181-185, answers with `send_400_response("Invalid HTTP request received.")`.
3. That function (h11_impl.py:303-319) writes a complete `400 Bad Request` with `connection: close`, then calls `transport.close()`.
4. `transport` is `_GracefulTransport`, so `close()` goes to `GracefulCloseMixin._graceful_close` (services/detection-py/src/graceful_close.py:159-185). It:
   - sets `_closing`, so later `data_received` input is dropped (:194-197);
   - frees the concurrency slot;
   - calls `write_eof()`, which sends FIN after the 400 is flushed (:177);
   - sets `drain_left = DRAIN_MAX_BYTES` (64 KiB) with a 1 s timer (DRAIN_TIMEOUT_S, :185);
   - when more than 64 KiB arrives or the timer fires, `_drain_over` runs `raw.close()` (:187-192). If unread bytes remain, the kernel sends RST.
5. The module docstring (graceful_close.py:11-12) states the design: "A client that keeps sending past either bound still gets the kernel's RST, by design."

Answers to the specific questions:
- A status is sent first, and it is 400 (not 414 or 431). Observed in all 220 trials below.
- The close with unread data pending is deliberate and bounded: 64 KiB of drain and 1 s, at most `drains_max` (512) concurrent drains. Past the cap it closes at once (:174-175).
- The RST can destroy an unread response on the client side. I observed it as `ConnectionResetError` or `BrokenPipeError` on the client, always after the 400 had been written.
- The server stays healthy for other clients. After every probe batch, `GET /health` returned 200. The server log shows only "Invalid HTTP request received." warnings, with no traceback.
- "Closed or reset with no response" is therefore a legitimate refusal, not an unhandled error path. It is the TCP consequence of a client that outsends the drain bound while the server has already refused it.

Probe 1: real serve.py, loopback, 1 MiB request line, client sends in chunks and reads as it goes, 40 connections per chunk size.
Command: `venvs/detection-py/bin/python probe.py` (scratchpad/probe.py). Raw output, trimmed:
```
1048576 {(b'HTTP/1.1 400 Bad Request', "final recv ConnectionResetError..."): 2, (400, "send BrokenPipeError... final recv"): 36, (400, "send ConnectionResetError"): 2}
65536   {(400, "send BrokenPipe"): 21, (400, "send ConnectionReset"): 9, (400, "recv ConnectionReset"): 9, (400, ''): 1}
4096    {(400, ''): 19, (400, "recv ConnectionReset"): 4, (400, "final recv ConnectionReset"): 15, (400, "send BrokenPipe"): 1, (400, "send ConnectionReset"): 1}
health after: b'HTTP/1.1 200 OK'
rc 143
```

Probe 2: the test's own pattern (sendall the whole 1 MiB, then read), 100 connections.
Command: `venvs/detection-py/bin/python probe2.py`. Raw output:
```
{('', 'ConnectionResetError', b'HTTP/1.1 400'): 34, ('BrokenPipeError', 'ConnectionResetError', b'HTTP/1.1 400'): 29, ('ConnectionResetError', 'ConnectionResetError', b'HTTP/1.1 400'): 33, ('', '', b'HTTP/1.1 400'): 4}
health after: b'HTTP/1.1 200 OK'
rc 143
```
The 400 arrived in 100 of 100 trials on this Mac, even when sendall failed with BrokenPipeError or ConnectionResetError. That matches the stated BrokenPipeError source in CI #4 (the old test's `_request` helper, not the server).

Linux: BSD stacks keep data already received when an RST arrives, but Linux discards the client's unread receive queue on RST. I could not test Linux, so a Linux client may see a bare reset with no 400. The probe pattern cannot show this, and the fix wave does not claim to have reproduced it either. This is consistent with the design above.

R26B-RL-1, Low (test strength, not product). Class: NOT_VERIFIED / weak oracle.
- Evidence: services/detection-py/tests/test_request_limits_live.py:440-453 (and the sibling at ~:425-436) assert only `status != 200`.
- Reasoning: a 500 from an unhandled-exception path would pass. A reset with no response also passes, though that outcome is legitimate. The observed behaviour is 400.
- Repro (reasoning chain): a mutant server that answered 500 to an over-cap line passes the assertion.
- Falsifier: the test asserts `status in (400, 0)` or a health check on the same server afterwards.
- This answers AEGIS's question "does accepting an early close weaken the test". Accepting the close is legitimate, because the server writes the 400 first. What weakens it is the `!= 200` oracle. The module-scoped `server` fixture and later tests would catch a crashed server, but not a 500.

=====================================================================
B. detection-py launcher / SIGTERM (shared launch_guard)
=====================================================================
`launch_guard.py` is byte-identical in all 10 services: `md5 -q */src/launch_guard.py */src/*/launch_guard.py | sort | uniq -c` printed `10 f3b8ee9c7466fb62f9b5d76a68fceb31`.
- Delivery-py has its own SIGTERM handler (serve.py:150, unchanged) and uses launch_guard only for the switch-interval check.
- All other launchers use `sigterm_exits()`.

Probe 3: real detection serve.py under a wrapper that registers `atexit` and writes a file (scratchpad/wrap.py), SIGTERM after 3 s.
Raw output: `rc=143` / `yes`. A double SIGTERM gave `rc2=143` / `yes`. Exit status 143 and `atexit` both run.
- The server log shows "Shutting down / Application shutdown complete / Finished server process".
- Previous disposition is restored in `finally` (launch_guard.py:76-81).

Not verified: the exit status now reads as 143 (a normal exit) instead of death by signal. Only delivery-py already did this. I found no systemd units or compose files in the repo (`git ls-files | grep -i systemd|compose|k8s|helm|Dockerfile` found only the two delivery-py Dockerfiles), so I could not check a supervisor's reading of 143.

=====================================================================
C. fulfillment-py: `_FifoBytes`, small-model reservation, `LoopLag.settle()`
=====================================================================
Reading, with reasoning chains. api.py lines are at d80adc5.

- `_FifoBytes` (api.py ~:1051-1087) and `_SmallModelHold` (~:1090-1112).
- `_off_loop` (api.py:1334-1456):
  - the hold is created before the `try` (:1349);
  - reserve happens before `lane.acquire()` in the small branch (:1388-1389);
  - the `finally` blocks release both pools (:1439-1457), and the release is idempotent;
  - on a parse error, `small_model.release()` runs in the outer `finally`.
- No circular wait: holders of the small lane already hold their reservation and need no further pool bytes. Large bodies use the shared pool, which is a different pool.
- Auth runs before `_off_loop` (api.py:1809-2020, `dependencies=[Depends(require_auth)]`), so unauthenticated clients cannot reserve.

Probe 4: `_FifoBytes` and `_SmallModelHold` under random concurrency and cancellation, in-process on the real api.py.
Setup: 300 rounds, each with 20 tasks reserving 1-100 of a 100-byte pool, holding for up to 2 ms, about half cancelled at random times (scratchpad/fifo.py).
Command: `FULFILLMENT_SERVICE_TOKEN=... venvs/fulfillment-py/bin/python fifo.py`. Raw output:
```
rounds with leaked used/queue: 0 other exceptions: {'ValueError': 29}
```
No leak: `used == 0` and the queue was empty after every round. The 29 ValueErrors are the already-recorded R26B-RT-6 (the cancelled-waiter path in `acquire`: `queue.remove(entry)` after `_grant` already popped it). It is not worse than stated: no pool leak, and no stuck waiters behind it, because `_grant` skips done waiters (:1076). The concurrency exercised was one event loop with cancellation interleaving. It was not the full ASGI path.

R26B-RL-2, Low. Class: TIMEOUT_UNBOUNDED.
- Evidence: `_FifoBytes.acquire` (api.py ~:1058-1073) has no deadline, per its own docstring "no time limit and no refusal".
- Reasoning: the reservation is taken before `lane.acquire()`, and the small lane is also an unbounded semaphore wait (pre-existing).
- A body without Content-Length, such as chunked, counts as "small" because `large` needs a declared length over 64 KiB (:1340-1342). Reserve is `min(4 MiB, 16 x bytes_received + 16 KiB)` (:1034-1035). A chunked body of about 262 KiB or more therefore reserves the entire 4 MiB pool for its whole parse and agent work, and every other small request queues behind it in FIFO order.
- Impact: latency and serialization for authenticated callers, with a bounded holder duration. No deadlock, no refusal. I did not verify whether Starlette cancels an abandoned handler on client disconnect, so abandoned waiters may still take queue slots and run their work.
- Repro: not executed end to end; the reasoning chain above is from the cited lines.
- Falsifier: a live test with 2 chunked 300 KiB bodies and one 1 KiB request shows the 1 KiB request is not delayed behind them, or a bounded wait exists.

`LoopLag.settle()` (http_limits.py:269-278): it cancels the pending handle, calls `_tick()` (which re-arms) and returns `lost`. It is called from the protocol's data_received and body start, and from the app's client-wait clocks (api.py:521, 527). Reading shows no state left inconsistent, because `_tick` re-arms. Not exercised by me beyond that reading.

=====================================================================
D. delivery-py
=====================================================================
Read from the diff; I did not run the delivery suite. There is no delivery venv in the shared venv set, and the suite takes about an hour (OPEN.md W26B-4).

- Cancelled-admission ids (service.py:200, :343, :746-747, :840-842, :1550-1553).
  - `_apply` is the single path for both live commit (:311-312) and startup replay (:209), so rebuilding at start is the same code as the live path.
  - The set grows by one per operator cancel, deliberately unbounded and tiny.
  - The cancel record is committed before in-memory state changes (:1550, `_commit` writes the log line, then `_apply`).
  - `_cancelled_running` is still set after the commit. A crash between commit and that line is covered by the rebuild at start.
  - No flaw found.
- Cap check after idempotency (service.py:894-897, :1383-1386; api.py:321-362). `cap_check` runs after `_idem` returns no entry and before anything is recorded, and it raises inside `with self.lock` (released by the context manager). A recorded request replays its answer even if DLV_MAX_FINDINGS was lowered. No flaw found.
- git isolation (gitport.py:62-82). `HOME=/nonexistent` and `core.hooksPath=/dev/null`. `_isolation()` raises `GitRefused` when `/nonexistent` exists, which fails closed for every git command. Nothing is created now, so a SIGKILL leaves nothing.
  - Probe 5 (git on this Mac): a repo whose `.git/hooks/pre-commit` would touch a marker and exit 1, run with `HOME=/nonexistent GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git -c core.hooksPath=/dev/null commit`. Result: commit succeeded (`rc=0`), no marker file was created, and `/nonexistent` is absent on this box.
- env allowlist (config.py:61-65, :211-221). `__CF_USER_TEXT_ENCODING` is allowed only when `platform == "darwin"` and the value matches `0xH:0xH:0xH` (fullmatch). It is fail-closed on a bad value. No flaw found.
- non-UTF-8 gate (srcdiff.py:87-122, loop.py:839-852, :1434-1440, service.py:504-509). The check runs on the raw git bytes. On failure it fails the round or run (EVIDENCE_UNAVAILABLE) and does not accept, so it is fail-closed. A non-UTF-8 file name is not itself flagged (git quotes such names); only content is.

R26B-RL-3, Low. Class: RESOURCE_EXHAUSTION.
- Evidence: `is_non_utf8_file` (srcdiff.py:~113-122) reads the whole file with `fh.read()`, while the sibling `is_binary_file` (:124-132) reads only `BINARY_SNIFF_BYTES`.
- It is evaluated for every changed src file after `or` (loop.py:843-846), before the report-size cap (loop.py:1453).
- Impact: memory proportional to a model-written file in the service process. The existing `git.diff` (loop.py:841) already holds the tracked diff in memory, so this is about 2x of an existing exposure. I could not verify a worktree file-size cap, so this is a reasoning chain only.
- Falsifier: a size limit on worktree files enforced before this call, or a `fh.read(N)` / incremental decoder.

=====================================================================
E. devtools/hygiene_check.py
=====================================================================
Read from the diff.
- Process table (`_proc_table`, :178-198, `_environ_of` :200-227). macOS reads the marker through sysctl KERN_PROCARGS2 for this user's processes. Apple platform binaries withhold the environment, which the docstring states. `leftover_processes` (:230-253) finds group, session and marker matches, then descendants, then calls `kill_pids`.

R26B-RL-4, Low. Class: FAIL_OPEN (detector).
- Evidence: `_environ_of` returns `b""` on any sysctl failure (second sysctl failing after the size query, e.g. the target's environment grew between the two calls, or the process exited). The row then carries an empty environment and is treated as not marked.
- Impact: a rare, silent miss of an orphan that left the process group. The detector is best-effort and documents the platform gaps (setsid plus scrubbed environment, Apple binaries), but this miss is not documented. It is reasoning only, not reproduced.
- Falsifier: retry the sysctl once, or report an unreadable environment as a row note.

- `kill_pids` (:256-275): SIGTERM, wait up to 5 s, then SIGKILL, wait up to 5 s. Violations are recorded before the kill (:458-466), so a process that survives SIGKILL (stuck in D state) still produces a failure. Pid reuse is the known R26B-SEC-2 and is not re-reported; I found nothing worse than stated.

Test-only changes read with no concern: ledger-rust `persistence.rs` (inside `mod tests`, per-process scratch dir with `libc::atexit` doing `remove_dir`, so non-empty dirs are kept) and `server.rs:849` (a clippy `allow` attribute).

=====================================================================
F. orchestrator-go
=====================================================================
`git diff a6aee4e d80adc5 -- services/orchestrator-go` shows a one-file change, `cmd/orchestrator/server_limits_test.go`, +5/-1. No product code changed; the claim is confirmed.
- `ReadTimeout` is applied: cmd/orchestrator/main.go:82-83 sets 15 s, and `newServer` sets `ReadTimeout: readTimeout` at main.go:109 (together with ReadHeaderTimeout, WriteTimeout, IdleTimeout, MaxHeaderBytes).
- The clock in `trickle` now starts before `net.Dial` (test lines ~66-71).
- A precision note: Go starts the read deadline when the server begins reading the request on the accepted connection, which is at or after accept and after the dial call returns on the client. That is later than "from accept", but it is still after the client's pre-dial `start`, so the lower-bound argument holds.
- Probe 6: `go test -count=1 -run TestSlowBodyIsCutOffBeforeAnyScanAndOthersAreServed -v ./cmd/orchestrator/`. Raw output: `closed by the server after 15.002773209s; it sent "HTTP/1.1 408 Request Timeout\r"` / `--- PASS (15.51s)`. This is one run on a Mac. The CI #5 failure was on ubuntu.

=====================================================================
G. surfacesTested / denominator
=====================================================================
Required surfaces: 14. Tested by execution: 6. Read-only reasoning: 7. Not tested: 1.

| # | Surface | Method |
|---|---|---|
| 1 | detection-py over-cap request line behavior | executed: probes 1, 2 (220 connections, real server) |
| 2 | detection-py server health after a refusal | executed: `/health` 200 after each batch |
| 3 | launch_guard SIGTERM and atexit (detection server) | executed: probe 3 (single and double SIGTERM) |
| 4 | launch_guard in the other 8 launchers | reasoning only: byte-identical file (10/10 md5), usage grep |
| 5 | `_FifoBytes` cancellation, FIFO, pool leaks | executed: probe 4 (300 rounds, one event loop) |
| 6 | `_off_loop` small-model flow, deadlock | reasoning from cited code |
| 7 | `LoopLag.settle()` | reasoning only |
| 8 | git isolation (hooks, HOME) | executed: probe 5 (macOS git) |
| 9 | cancelled-admission persistence and rebuild | reasoning only (single `_apply` path); service/tests not run |
| 10 | cap check after idempotency | reasoning only |
| 11 | non-UTF-8 gate | reasoning only |
| 12 | env allowlist | reasoning only |
| 13 | hygiene_check process table and `kill_pids` | reasoning only |
| 14 | orchestrator-go ReadTimeout | executed: probe 6 plus code read (main.go) |

=====================================================================
H. Could not verify
=====================================================================
- Linux behaviour of the CI4-2 race: whether the client loses the 400 to the RST. This Mac delivered the 400 in all 220 trials, and the Linux RST path was not reproduced.
- delivery-py runtime behaviour: no delivery venv available and the suite takes about an hour, so I ran no delivery tests. Findings D are from the diff and code reading only. `tests/test_round26b.py` was not run.
- fulfillment-py end-to-end ASGI behaviour of the small-model pool, including whether Starlette cancels abandoned waiting handlers (RL-2) and the real concurrency of agent-work duration. Probe 4 covered only the pool class under asyncio cancellation.
- Supervisor reading of exit status 143 (no systemd/compose manifests in the repo).
- The hygiene macOS sysctl TOCTOU (RL-4) was not reproduced.
- Memory footprint claims (F-6, 91 MiB Linux, 96 MiB placement) were not measured by me. They are already recorded as W25-EA-3 / R26B-RT-3.

Known items not re-reported (found no worse than stated): R26B-RT-6 (reproduced in probe 4: 29 ValueErrors, no leak), R26B-SEC-2, W26B-2 / R26B-TT-3, W25-EA-3 / R26B-RT-3.

New findings: R26B-RL-1 (Low), R26B-RL-2 (Low), R26B-RL-3 (Low), R26B-RL-4 (Low). No Critical, High or Medium. CI4-2: no unhandled error path in the product, the refusal is a deliberate and bounded 400 plus graceful close, and the open question reduces to the weak `!= 200` oracle (RL-1).
