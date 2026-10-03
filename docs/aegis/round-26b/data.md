# AEGIS-DATA gate report — round 26b re-adjudication (data / persistence)

**Gate result: DATA_GREEN for the scoped claim. No Critical or High findings; 4 Low findings, non-blocking under the OPEN.md rule.** This is the data gate only, not the overall verdict.

## Binding

- Repo: `/Users/andrelove/PycharmProjects/zbm-zbc`
- candidateSha: `d80adc5528ddad4f27427098efe2beee32ee5d6a`
- treeOid: `5c059f868fe4426859c737170ae75ea9a94f351e`
- Base: `a6aee4e9fe78bb58a94243acce406d2e7b702387` (confirmed as the merge-base)
- Scope: `git diff a6aee4e d80adc5 -- services devtools .gitignore` (125 files). Nothing outside this diff is certified.
- Worktree: `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/review-d80adc5`. HEAD matched the candidate before and after; `git status --short` was empty before and after.
- Isolation mode: exclusivity only. Everything ran in-process against fakes (`FakeLedgerClient`, `FakeDockerCli`) with file-backed logs in per-run temp dirs, or against Rust test scratch under a private `TMPDIR` with `CARGO_TARGET_DIR` in my scratchpad. No real, shared or production ledger, service or database was contacted. This does not prove behaviour against the real ledger-rust server over HTTP, real Docker, or Linux.

## What persisted state the wave touches

The system persists to append-only, hash-chained JSONL logs: the Rust ledger and one `<dept>_log.jsonl` per department, each line anchored on the ledger. There is no SQL, no migrations and no schema. Migration lineage and greenfield/brownfield convergence are therefore not applicable (`convergence: {greenfieldHash: n/a, brownfieldHash: n/a, identical: n/a}`).

Of the 35 changed non-test files under `services/`, only these bear on persisted state or accounting:

| File | Relevance |
|---|---|
| `services/delivery-py/src/zbm_delivery/service.py`, `api.py` | Idempotency ordering; cancelled-admission memory rebuilt from the log |
| `services/ledger-rust/src/persistence.rs` | Test module only (see D-CHK-5) |
| `services/ledger-rust/src/bin/server.rs` | One `#[allow(clippy::unnecessary_cast)]` attribute |
| `services/fulfillment-py/src/api.py`, `http_limits.py` | In-memory byte accounting; nothing persisted |
| five `devtools/live_run.py` (clipper-network, compliance, finance, legal, verification) | Add `shutil.rmtree` of the run's work dir |
| `.gitignore` | Which runtime logs are ignored |

The rest is launcher guards, non-persisting delivery changes and tests:
- `launch_guard.py` is byte-identical across all ten copies (sha256 `d8fa90f8…bfc10`).
- The `serve.py` changes are the switch-interval check and the SIGTERM wrapper.
- `gitport.py`, `srcdiff.py`, `engine/loop.py` and `config.py` add refusals and git isolation; they write no state.
- No `store.py`, credit, billing, payment or webhook module is in the diff. A filename filter over the changed files matched only `persistence.rs` and one test file.

## Checks

| id | area | result | method |
|---|---|---|---|
| D-CHK-1 | delivery: cap after idempotency lookup, fix-runs | VERIFIED | code read + wave test run |
| D-CHK-2 | delivery: same for review | NOT_VERIFIED (measured); correct by inspection | code read only |
| D-CHK-3 | delivery: cancelled admission replay, concurrent and after restart | VERIFIED | own probe, 5 runs |
| D-CHK-4 | delivery: torn or truncated log tail | VERIFIED (fails closed) | own probe |
| D-CHK-5 | ledger-rust: no product or persistence logic changed | VERIFIED | diff read + tests run |
| D-CHK-6 | ledger-rust: `libc::atexit` cannot remove real data | VERIFIED | code read + tests run |
| D-CHK-7 | `.gitignore`: no fixture lost, runtime logs still ignored | VERIFIED, with gap D-26B-2 | `git check-ignore`, `git ls-tree` |
| D-CHK-8 | fulfillment: small-model pool cannot leak or go negative | VERIFIED, with D-26B-1 | own fuzz probe |
| D-CHK-9 | finance-py / legal-py: launcher guard and test only | VERIFIED | diff read |
| D-CHK-10 | `live_run.py` `rmtree` target identity | VERIFIED, with note D-26B-3 | diff read |

### D-CHK-1 — cap after the idempotency lookup (fix-runs)

- The route passes the cap as a callback: `api.py:324-326`. The service runs it after the lookup and before anything is recorded: `service.py:894-898`.
- A recorded request returns its stored answer at `service.py:895-896`, before the cap.
- Ran `tests/test_round26b.py -k "replays_its_answer or cancelled_admission"`: 3 passed in 27.88s.
- Idempotency measurement: 1 replay with the cap lowered from 2 to 1, identical 202 body, 0 new ledger events. A new request id over the cap got 422 with 0 new events.
- Nothing can be double-recorded on this path: the run id is derived from caller, request id and facts, and `_admit_locked` refuses an existing run (`service.py:950-952`).

### D-CHK-2 — cap after the lookup (review)

- `service.py:1383-1387` has the same order: lookup, return recorded answer, then cap, then `_review_checks`.
- I did not replay a recorded fail review under a lowered cap. This is already registered as R26B-RT-5 (Low); I concur.

### D-CHK-3 — cancelled admissions

- The never-evicted set is declared at `service.py:200` and filled in `_apply` at `service.py:342-343`. `_apply` runs both on live commit (`service.py:311-312`) and on the start-up replay of the whole log (`service.py:207-209`).
- It is consulted before any admission is reserved (`service.py:745-749`) and again when the containers end (`service.py:839-846`).
- The cancel's `admission_closed` record is written in the same log line as the cancel's idempotency record (`service.py:1547-1551`).
- A cancel is recognised by the reason code `CANCELLED`. That string appears 4 times in `src` (`reasons.py:27`, `service.py:862`, `1519`, `1523`); only `_cancelled_answer` (862) reaches an `admission_closed` record, so a plain refusal cannot be mistaken for a cancel.
- Probe: `scratchpad/aegis-data-probe/test_probe_data.py`, with `CLOSED_ADMISSIONS_MAX=0` so only the new set remembers. Results over 5 runs:

| Scenario | Replays | Result | Duplicate effects |
|---|---|---|---|
| Concurrent: first attempt's RED container still running (thread alive before and after) | 5 | all 409 `CANCELLED` | 0 extra admission containers (total stayed 1), 0 `fix_run_reviewed`, 0 child runs, exactly 1 `admission_closed` line and 1 `admission_cancelled` event |
| After restart: new service on the same data dir and ledger | 5 | all 409 `CANCELLED` | 0 containers, 0 `fix_run_reviewed`, 0 runs holding the finding |

- After restart the id was in `_cancelled_admissions` and not in `_closed_admissions`, so the answer came from the set rebuilt from the log.
- One of my probe runs failed on my own assertion: the first attempt's container-start event landed after my baseline count. I corrected the probe to assert the total; 4 of 4 runs then passed. It was a probe race, not a product defect.

### D-CHK-4 — torn tail

Measured on the probe's disposable log; the service refused to start in all three cases:

| Damage | Start-up result |
|---|---|
| Partial bytes appended, no newline | `StoreCorrupt: log ends with a torn line; refusing to start` (`store.py:84-85`) |
| The cancel line cut in half | Same `StoreCorrupt` |
| Whole lines removed back to before the cancel | `RuntimeError: refusing to start: local log truncated: the ledger anchors line 1347 but the local log has 1277 lines …` |

A lost or torn cancel record therefore cannot yield a running service that has forgotten the cancel. `store.py` is unchanged by the wave.

### D-CHK-5 — ledger-rust product logic

- `src/persistence.rs`: one hunk at line 404, inside `#[cfg(test)] mod tests`, which opens at line 400-401 and closes at 1086. It changes `scratch_path` only.
- `src/bin/server.rs`: one attribute plus comment at 849-851.
- `Cargo.toml` and `Cargo.lock` are unchanged; `libc = "0.2"` was already a dependency (`Cargo.toml:20`).
- The test files change scratch naming only (`common::scratch_dir()`, `common::unique_suffix()`).
- Correction to the claim as stated: `src/persistence.rs` is in the diff, not only `tests/` and `server.rs`. The change is test-only code, so the substance holds.
- Ran `cargo test --offline --locked`: lib `persistence` filter 23 passed; `harness_names` 3 passed; `server_events` 11 passed.

### D-CHK-6 — atexit cleanup

- The handler calls `std::fs::remove_dir`, which is non-recursive and fails on a non-empty directory (`tests/common/mod.rs:40-44`, `src/persistence.rs:409-413`).
- Its target is a `OnceLock` path set once to a directory this process created: `<tmp>/zbm-ledger-tests-<pid>_<nanos>_<n>` or `<tmp>/zbm-ledger-unit-<pid>_<nanos>`.
- It is compiled only into test binaries. It cannot delete a file or a directory with content.
- After the runs above, my private `TMPDIR` was empty.

### D-CHK-7 — `.gitignore`

- Tracked `*.jsonl` files are identical at base and candidate: 7, all under `services/ledger-rust/tests/fixtures/`.
- `git ls-files -ci --exclude-standard` returns nothing, so no tracked file matches an ignore rule.
- The runtime names in source are the ledger default `ledger_data/ledger.jsonl` (`server.rs:916`) and six `LOG_NAME`s: `cn_log`, `compliance_log`, `dlv_log`, `fin_log`, `legal_log`, `vi_log`. Torn side files are `<log>.torn-<nanos>` (`persistence.rs:175`).
- `git check-ignore --no-index -v` confirms each of these is ignored. A fixture such as `services/creative-py/tests/fixtures/events.jsonl` is now addable, as intended.

### D-CHK-8 — fulfillment small-model pool

- Code: `_FifoBytes` and `_SmallModelHold` at `services/fulfillment-py/src/api.py:1045-1113`; lifecycle in `_off_loop` at 1349, 1390, 1405, 1441-1442 and 1454.
- Release is idempotent and is reached on every exit path through the two `finally` blocks.
- Probe: `scratchpad/aegis-data-probe/fifo_probe.py` runs the two classes extracted from the candidate file. 5 seeds, 3000 tasks each with random sizes (including over the pool), random over-reservation counts and about 1640 cancellations per seed.
- Every seed ended with `used=0`, `over=0`, `queue=0`, peak `used` never above the limit, and neither counter ever negative.
- The granted-then-cancelled path gives the bytes back (`used` 0).

### D-CHK-9 — finance-py and legal-py

- `src/serve.py` in both: switch-interval check through `launch_guard`, and `uvicorn.run` wrapped in `sigterm_exits()`.
- `devtools/live_run.py` in both: work-dir handling only.
- Everything else is tests and README. No store, credit, ledger-client or billing code changed.
- The SIGTERM change makes a stop a normal interpreter exit instead of death by signal. It cannot make a log append less durable than before.

### D-CHK-10 — `live_run.py` rmtree

- `shutil.rmtree(work)` runs only when `LIVE_WORK_DIR` (or finance's deprecated `FIN_LIVE_WORKDIR`) is unset.
- `work` is always the fresh return value of `tempfile.mkdtemp(prefix=…)`, so the process owns it exclusively. It cannot point at a service data dir.

## Findings

### D-26B-1 — Low — `_FifoBytes.acquire` raises `ValueError` in place of `CancelledError`

- **Where:** `services/fulfillment-py/src/api.py:1058-1066` and `1072-1078`.
- **What happens:** a queued waiter is cancelled, and a release runs in the same loop pass before the cancelled task resumes. `_grant` pops the cancelled entry (1074-1076). The task then runs `self.queue.remove(entry)` (1064) on an entry that is gone.
- **Measured:** directed probe gives `['ValueError', 50] used 50 queue 0`; the fuzz hit it once in 5 seeds.
- **Impact:** accounting stays intact — no leak, no negative, and the next waiter is granted. A cancelled request surfaces the wrong exception type.
- **Reproducer:** `fifo_probe.py`, the `directed()` case.
- **Falsifier:** that case printing `CancelledError`.
- Whether production ever cancels a handler task at that await (server shutdown is the plausible route) was not measured; that is for AEGIS-RELIABILITY.

### D-26B-2 — Low — a ledger log with a non-default name inside the tree is no longer ignored

- **Where:** `.gitignore:35-43`; `LEDGER_LOG_PATH` is free-form (`services/ledger-rust/src/bin/server.rs:915-916`).
- **Measured:** `git check-ignore` reports NOT IGNORED for `services/ledger-rust/my-ledger.jsonl`, `services/ledger-rust/ledger_a.jsonl` and `live-work/finlive-x/ledger_a.jsonl`. All were ignored under the old `*.jsonl` rule.
- **Impact:** an operator who points `LEDGER_LOG_PATH` at a custom name inside the working tree and runs `git add -A` would stage real evidence data. No script, README or CI file in the tree uses a non-default name; I grepped `services/*/devtools`, `services/*/README.md` and `.github` and found only `ledger.jsonl`.
- **Converse:** a fixture named exactly `ledger.jsonl` or `<dept>_log.jsonl` is silently un-addable anywhere, including ledger-rust's fixtures dir, because the old negation rule is gone. No such fixture exists today.
- **Reproducer:** `git check-ignore --no-index -v services/ledger-rust/my-ledger.jsonl` exits 1.
- **Falsifier:** a rule matching it.

### D-26B-3 — Low — live-run evidence is deleted by default, including after a failed run

- **Where:** `finish_work_dir` in the five `devtools/live_run.py`, for example `services/finance-py/devtools/live_run.py:74-79` and `393-398`.
- **What happens:** with `LIVE_WORK_DIR` unset, the run's ledger and service logs are removed when the run ends, whether it passed or failed.
- **Impact:** dev tooling on a disposable directory, not production data. A failed local run's logs are gone unless the variable was set beforehand; the narration on stdout survives.
- **Reproducer:** by inspection of the `finally` in `main()`; not executed.
- **Falsifier:** a keep-on-failure branch.

### D-26B-4 — Low — a refused replay of a cancelled admission still appends evidence

- **Where:** the checks that run before `_red_begin` in `review()` and `create_fix_run()` (`service.py:1388-1408`, `899-922`).
- **Measured:** 5 replays after restart added 30 ledger events — 15 `crossing_git_requested` and 15 `local_log_appended` — and 15 local log lines.
- **Impact:** no state effect: no run, review, container or admission record. The log and ledger grow by 6 events per replay from an authenticated `aegis` caller. This is the existing behaviour for any refused request that reaches the git checks; the wave did not introduce it. The inline comment "nothing recorded or run again" (`service.py:749`) is accurate for state but not for evidence.
- **Reproducer:** `test_probe_data.py`, the "replay event types" line.
- **Falsifier:** 0 new events on replay.

## Idempotency summary

| Path | Replays | Duplicate effects |
|---|---|---|
| `POST /dlv/v1/fix-runs`, recorded request, cap lowered | 1 | 0 (0 new ledger events) |
| `POST /dlv/v1/fix-runs/{id}/review`, cancelled admission, concurrent | 5 | 0 state effects |
| Same, after restart | 5 | 0 state effects; 30 evidence-only events (D-26B-4) |
| `POST /dlv/v1/fix-runs/{id}/review`, recorded review, cap lowered | 0 | not measured |

## Not verified

- The review half of N25-D-4 by replay (inspection only).
- Behaviour against the real ledger-rust server, real Docker and Linux. The atexit removal and all probes were measured on macOS (Darwin 25.6) only.
- A cancel whose ledger event `admission_cancelled` is written but whose local commit then fails (`service.py:1540` then `1551`). Read as a possibility in code that predates this wave; neither changed nor measured here.
- Any money path as a whole: finance credit and ledger double-spend, billing or webhook replay. The wave changed no such logic, so nothing was measured, and this gate asserts nothing about them beyond "not changed by this diff".
- Tenant isolation at the storage layer: not touched by the diff, not assessed.
- Rollback and restore paths: unchanged by the wave, not assessed.
- Full suites were not run, per instruction. I ran 3 of 14 tests in `test_round26b.py`, 37 ledger-rust tests, and my two probes.
- Whether a chunked small-lane body with no declared length can reserve the whole 4 MiB small-model pool. That is an availability question for AEGIS-RELIABILITY and was not examined.

## Gate object

```
{ candidateSha: "d80adc5528ddad4f27427098efe2beee32ee5d6a",
  treeOid: "5c059f868fe4426859c737170ae75ea9a94f351e",
  gate: "DATA",
  verdict: "DATA_GREEN",            // scoped claim; Low findings are NON_BLOCKING_FINDING
  convergence: { greenfieldHash: null, brownfieldHash: null, identical: null },   // no SQL or migrations
  findings: [ D-26B-1 Low, D-26B-2 Low, D-26B-3 Low, D-26B-4 Low ],
  blocking: false }
```

Any content change to the candidate voids this result.

## Files

Probe scripts are kept for reproduction in `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/d498d3cf-afb3-4870-8755-d7d820d377d5/scratchpad/aegis-data-probe/`:
- `conftest.py`
- `test_probe_data.py`
- `fifo_probe.py`

Run the delivery probe with `/Users/andrelove/PycharmProjects/zbm-zbc/services/delivery-py/.venv/bin/python -m pytest -p no:cacheprovider -c /dev/null --rootdir <probe dir> -s -q test_probe_data.py` and `PYTHONDONTWRITEBYTECODE=1`. Run `fifo_probe.py` with the fulfillment-py venv's Python.

The disposable cargo target and temp dirs were removed. Nothing was edited, committed or pushed.
