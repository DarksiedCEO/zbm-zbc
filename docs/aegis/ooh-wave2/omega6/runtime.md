RUNTIME: REAL_PROTOCOL_LOCAL for scope ooh-py (Department 15) at ce665c5.
- Entrypoint: the production `cd src && python -m api`, against a real ledger-rust built by this gate, over loopback HTTP.
- Host: Python 3.12.14 and 3.13.9, macOS arm64.
- Re-witnessed on both Pythons: F1, R19, C07 and integrity scenarios a, b, b2, c, plus clat for O-1.
- New: T222 on real processes (scenarios d and d2) on both Pythons, plus a falsifier.
- No live or production claim.
VERDICT: RUNTIME_GREEN_FOR_SCOPE (two LOW non-blocking observations: O-1 re-confirmed, O-2 new)

Binding:
- candidateSha ce665c5b3aef1bcd56aacee9523559150dd8cb7b · tree 5530ba08de8a78fad4c004db4fedeb8c40b45a4a · branch ooh-dept-15-wave2 (local) · repo DarksiedCEO/zbm-zbc
- ledger-rust tree 4f1ab90e2f81245130d6eb87091745146f6398e6 · ooh-py/src tree 6d8a444233eed6ac29bcbdca37417c70e5002acc (both identical at 4a0a4b4)
- ledger binary sha256 e778f2bc7108f672198f15aabb9ffaacf9074923287ce5a679d1a532728bdde9, built by this gate

Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega6 run). Written in fresh context. I reused omega5's drivers; omega5's verdicts were not relied on.

## 0. Binding and delta
- `git -C /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/review-ce665c5 rev-parse HEAD HEAD^{tree}` -> ce665c5b…, 5530ba08…. `git status --porcelain` -> 0 lines, before and after the runs.
- `git diff --stat 4a0a4b4 ce665c5`: docs/adr/0022 (+21/-4), docs/test-counts.md (1/1), tests/test_aegis_integrity_path.py (+3/-1, fixture yield+close), tests/test_aegis_omega5_mutation.py (+152, new).
- `git diff --stat 4a0a4b4 ce665c5 -- services/ooh-py/src services/ledger-rust` -> **empty**.
- Tree OIDs are equal at both SHAs.
- I extracted the candidate with `git archive ce665c5 | tar -x` into /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/src. After all runs, a second archive in /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/verify_extract was compared with `diff -r` (excluding __pycache__ and target): IDENTICAL.
- Main repo working tree: only the pre-existing ?? .git.backup-preimport/ and ?? .idea/.

## Execution conditions
- Host: Darwin 25.6.0 arm64, Andres-MacBook-Pro.local, 2026-10-18 ~07:45–08:00 UTC.
- Python: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv3.12/bin/python (3.12.14) and /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv3.13/bin/python (3.13.9).
- Rust: cargo and rustc 1.90.0 (Homebrew).
- Every process ran on 127.0.0.1 with bind(0) ports: a real ledger-rust and a real `python -m api`.
- Runs were sequential and single-client. A peer gate's processes (omega6/data/poc/*) were running on the same host at the same time.
- The war room is the exception: it runs in-process with fake ledgers (§3).

## 1. Ledger build (by this gate)
`cd /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/src/services/ledger-rust && SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/target`
- rc=0: "Compiling ledger-rust v0.1.0 (…omega6/runtime/src/services/ledger-rust) … Finished release in 7.62s" (/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/cargo_build.log).
- `shasum -a 256 /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/target/release/server` -> **e778f2bc7108f672198f15aabb9ffaacf9074923287ce5a679d1a532728bdde9**. Mach-O 64-bit arm64, 2161008 bytes (same size as omega5's; the source path is embedded, so the hash differs).
- Every real-ledger run below used this binary. No prebuilt binary was used.

## 2. live_run.py (real entrypoint + real ledger)
From /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/src/services/ooh-py: `LEDGER_BIN=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/target/release/server LIVE_WORK_DIR=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/live<v> /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python devtools/live_run.py`. live_run.py:156 reads LEDGER_BIN, and :181/503/524 start `[sys.executable, "-m", "api"]`.

| | py3.12 | py3.13 |
|---|---|---|
| rc / result | 0, "37/37 checks passed" | 0, "37/37 checks passed" |
| pids | ledger 26417; service 26476, 26616, 26621 | ledger 26631; service 26640, 27074, 27080 |
| stdout sha256 | b1b661d6e0f9… | 664b17f84fd3… |
| ledger.jsonl (93 lines) | 52cef82b8dc0… | 1d173e7794b5… |
| ooh_log.jsonl (42 lines) | 1a146323b4fa… | 8a220d06703b… |

## 3. War room (in-process harness, fake ledgers, NOT real-ledger evidence)
From /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/src: `/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python devtools/warroom/run.py --department ooh-py --seed N --python /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python --out /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/wr_<v>_<N>`

| run | rc | gate | cases (PASS/FAIL/ERROR) | known | MUST | stdout sha256 |
|---|---|---|---|---|---|---|
| 3.12 seed 1 | 0 | PASS | 156/0/0 | 0 | 100% | cf553f80ead2… |
| 3.12 seed 7 | 0 | PASS | 156/0/0 | 0 | 100% | d9355a99075f… |
| 3.12 seed 13 | 0 | PASS | 170/0/0 | 0 | 100% | 4e12194083c6… |
| 3.13 seed 1 | 0 | PASS | 156/0/0 | 0 | 100% | 9805c66f6653… |
| 3.13 seed 7 | 0 | PASS | 156/0/0 | 0 | 100% | e61ee5e0ea75… |
| 3.13 seed 13 | 0 | PASS | 170/0/0 | 0 | 100% | 1a4ac04730b1… |

"LLM personas: NOT_CONNECTED". This is chaos coverage on fake ledgers, not real-protocol evidence.

## 4. F1 / R19 / C07, with a ledger SIGKILL and restart (real ledger)
- Driver: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/commit_witness.py (sha256 7a0f7ebc376751…). It is omega5's driver with only the label changed, omega5 -> omega6 (2 lines).
- Command: `SCEN=<f1|r19|c07> LEDGER_BIN=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/target/release/server TREE=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/src WORK=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/cw_<s>_<v> /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python -I /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/commit_witness.py`
- All 6 runs gave **RESULT PASS**. Values below are from py3.12; py3.13 had the same outcome.
- **F1** (out 8fce6ab2…; py3.13 effe82ff…):
  - The anchor was forwarded and the real ledger answered "HTTP/1.1 201 Created". The ledger was then SIGKILLed (returncode -9).
  - W2 -> 503 {LEDGER_UNAVAILABLE, maybe: True}. W3 -> 503 INTEGRITY_UNVERIFIED.
  - After the restart, the same request_id got 201 with the original created_at.
  - FINAL: log [t-before, t-x, t-after], log 3 = anchors 3, integrity ok.
- **R19** (07c36abf…; 4004e19a…):
  - The evidence event was recorded (201), then the ledger was SIGKILLed.
  - W2 -> 503 {LEDGER_UNAVAILABLE, maybe: False}. W3 -> 503 LEDGER_UNAVAILABLE.
  - FINAL: log [t-before, t-after], 2 = 2. t-x never took effect.
- **C07** (8a44e712…; 84ff5f27…):
  - The anchor was recorded, then ooh_log.jsonl was chmod 0400.
  - W2 -> 503 {STORE_UNAVAILABLE, maybe: True}. W3 -> INTEGRITY_UNVERIFIED.
  - The ledger was SIGKILLed (-9). W3b -> INTEGRITY_UNVERIFIED.
  - After the restart, t-x rolled forward. FINAL: log 3 = anchors 3.

## 5. Integrity-path scenarios (real processes)
- Driver: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/integrity_witness.py (sha256 9943ae843d35…). It is omega5's v4 with only the label changed (2 lines).
- Command: `SCEN=<a|b|b2|c|clat> … /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python -I /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/integrity_witness.py`
- All 10 runs gave **RESULT PASS** (py3.12 values; py3.13 had the same outcome).
- **(a)** (fffbe04f…; 17f73b25…):
  - The anchor got 201 from the real ledger, then the service was SIGKILLed before the answer was passed back.
  - After the restart: integrity ok, dir [ooh.lock, ooh_log.jsonl], and "log line 2 sha256 == pending.line sha256 at kill: True". The line rolled forward.
  - The retry got 201 with the original created_at. FINAL: log 3 = anchors 3.
- **(b)** (e7ae9650…; b4805c1e…):
  - The anchor was held and the service SIGKILLed. After the restart: integrity ok, pending.discarded present, and "pending.discarded == pending.line at kill: True". The log was [t-before], so the line was not applied.
  - The anchor was then delivered late. The integrity job ran, the log became [t-before, t-x] and pending.discarded was removed: revived.
- **(b2)** (4849d011…; 907650ad…):
  - After the set-aside, W3 t-y took seq 2.
  - The job returned ok, pending.discarded was cleared, and the log was [t-before, t-y].
- **(c)** (b64ca879…; cd0ac3a3…):
  - The last ledger line (ooh log_anchor #2) was removed. A plain ledger start gave "rc 1 … REFUSING TO START".
  - While the ledger refused to start: audit not ok, W3 -> 503 INTEGRITY_UNVERIFIED.
  - After the operator reset (LEDGER_ALLOW_RESET binding): the job reported ok False, "local log line 2 has no matching anchor on the ledger".
  - W4 -> 503 INTEGRITY_UNVERIFIED. After a service restart, W5 -> 503 INTEGRITY_UNVERIFIED.
  - The ledger did not change from the reset to the end.

## 6. NEW: T222 on real processes (pending.line unremovable + certain ledger refusal)
Driver: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/pending_witness.py (sha256 9bba3ce87c9a…), written by this gate. The relay forwards the service's request bytes **unmodified** and passes the real ledger's answer back **verbatim**.

Construction: when the W2 log_anchor request reaches the relay, pending.line is already on disk. The relay then:
1. chmods the data dir 0500, so pending.line cannot be unlinked;
2. SIGKILLs the ledger and restarts it on the same ledger.jsonl with a **rotated LEDGER_SERVICE_TOKEN**;
3. forwards the unmodified request. The **real ledger answers "HTTP/1.1 401 Unauthorized"**, which the client classifies as LedgerNotRecorded (took_effect False, certain), at ledger.py:138-139.

Two recovery orders were run:
- **d**: restore the dir, run the job, restore the token, run the job.
- **d2**: restore the token, run the job, restore the dir, run the job.

Each ran on both Pythons. The command was `SCEN=<d|d2> LEDGER_BIN=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/target/release/server TREE=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/src WORK=/private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/pw_<s>_<v> /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python -I /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/pending_witness.py`.

Observed (py3.12 d, out 8332aa7f…; d2 8fa6af05…; py3.13 d c9dfc233…, d2 3f18152a…; all four agree):
- On arrival: dir [ooh.lock, ooh_log.jsonl, pending.line], pending sha256 90671ed4…. After the chmod: mode 0o500, dir_writable False. Ledger: "HTTP/1.1 401 Unauthorized", body "missing, malformed, or invalid Authorization header".
- **W2 -> 503 {"detail": "LEDGER_UNAVAILABLE", "maybe": True}** on all 4 runs. maybe=false was never observed.
- After W2:
  - pending.line was still present with the same sha256, and the service was alive.
  - status.integrity was ok **False**. The problem text was "a ledger answer was lost; the pending line is rolled forward at the next integrity check" (see O-2).
  - The local log was [t-before]. The ledger held 1 ooh anchor and 3 entries.
- **Writes stopped**:
  - W3 -> 503 INTEGRITY_UNVERIFIED. The ledger stayed at 3 entries / 1 anchor and the local log was unchanged. Nothing was recorded.
  - After 11 s, /audit/integrity -> ok False ("the ledger cannot be read"). W3b -> 503 INTEGRITY_UNVERIFIED.
  - d2: with the token restored but the dir still 0500, job1 re-recorded the anchor and appended t-x. The log was [t-before, t-x], pending.line could not be removed, and job1 reported ok False, "the pending line could not be removed".
  - d2: W3c -> 503 INTEGRITY_UNVERIFIED.
- Recovery (both orders):
  - Once both faults were removed, job2 -> ok True. The own pending line rolled forward, which is consistent with maybe=true.
  - The W2 retry with the same request_id -> 201 with the original created_at. W4 -> 201.
  - FINAL after a service restart: log [t-before, t-x, t-after], log_length 3 = ledger anchors 3. Ledger verify valid (6 entries), no pending.line or pending.discarded.
  - The refused writes t-y, t-y2 and t-y3 never appear.
- **Disclosure: the driver's own RESULT line printed FAIL on all 4 runs.**
  - The cause is a single predicate: I expected the problem text "the pending line could not be removed".
  - At service.py:201-207, `_drop_pending` sets that text (:248) and then returns False. The else branch then overwrites it with "a ledger answer was lost…" (:205).
  - Every other predicate held. Per-predicate evaluation: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/scripts/pw_eval.py over pw_*/result.json -> /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/pw_eval.out ("FAILING: ['problem_is_could_not_be_removed']" for all 4).
  - I did not edit the driver after the fact. The raw FAIL lines stand as recorded.
- **Falsifier T222**: a scratch copy where store.py clear_pending swallows OSError (diff in /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/fals/t222.diff), run with scenario d on py3.12. Output: /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/runtime/fals/pw_d_t222_3.12.out (c6d79b82…).
  - W2 -> **503 {LEDGER_UNAVAILABLE, maybe: False}** while pending.line was still on disk, and integrity stayed ok **True**.
  - W3 was then refused only because the ledger was still rejecting the token, not by the integrity gate.
  - RESULT FAIL. The scenario discriminates.

## 7. O-1 re-check (clat)
- Out: 1b511ee1… (py3.12); 11d03d35… (py3.13).
- After the operator-reset rollback and restart, before any check, W3 t-lat -> **201** on both Pythons. One write was accepted again.
- The next job reported ok False, "local log line 2 has no matching anchor on the ledger". W4 and W5 (after a service restart) -> 503 INTEGRITY_UNVERIFIED.
- The code is unchanged: service.py:161 checks only the cached `self.integrity["ok"]`, and :314-315 return the cached value while ok.

## 8. External systems / leftovers
- /ooh/v1/status reported every port NOT_CONNECTED: finance_31, vendor_orders, esign, inventory_data, measurement, programmatic_dooh.
- Every URL was 127.0.0.1. No live or production endpoint was read or written, and nothing was pushed.
- `ps -axo pid,ppid,command | grep -E "omega6/runtime|python -m api|_witness.py|target/release/server"` -> no process of this gate.
- Peer-gate processes were seen and left alone: pids 33067, 33069, 34137, 34138 and 34175, all under /private/tmp/claude-501/-Users-andrelove-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega6/data/ (poc/weaken*.py, rl_*_path.py, data/ledger-target server).
- The finally-block restored the data-dir mode to 0700 in every run.

## Findings
**O-1 (LOW, non-blocking, re-confirmed): one write is accepted after an operator-reset rollback, before the next integrity check.**
- Reproducer: SCEN=clat.
- Falsifier: a forced check before every commit would turn clat W3 into a 503.
- Not fixed at ce665c5; src is unchanged.

**O-2 (LOW, non-blocking, new): misleading operator text on T222.**
- What happens: after a CERTAIN ledger refusal with an unremovable pending line, the integrity problem says "a ledger answer was lost…". No answer was lost. The more precise "the pending line could not be removed" is overwritten (service.py:201-207 over :248).
- Behaviour is unaffected: maybe=true, writes stop, and the line rolls forward.
- The new unit test (tests/test_aegis_omega5_mutation.py test_t222_…) asserts only ok False, so it is consistent.
- Reproducer: SCEN=d or d2 above.
- Falsifier: if the problem text were kept, pw_eval would show no failing predicates.

## NOT RUN / NOT PROVEN
- **LIVE_PROVEN and PRODUCTION_PROVEN**: every port is NOT_CONNECTED and no deployed SHA was read. Not claimed.
- **pytest suite**, including tests/test_aegis_omega5_mutation.py: not run (AEGIS-TEST's job).
- **Other T222 variants**:
  - Only a 401 certain refusal was run. A connect-error certain refusal (ECONNREFUSED to the service) was not constructed, because the relay cannot refuse a connection already accepted for a known request.
  - Shed-503 and other 4xx refusals were not run.
  - The read-only-dir fault was a chmod. A service restart while the dir is 0500 was not run.
- **Falsifiers not re-run at this SHA**:
  - omega4's F1, R19 and C07 falsifiers, and omega5's ma/ma2/mb/mc. src is identical, but I do not carry those results forward.
  - Only the T222 falsifier was run here, and only on py3.12.
- **(c) variants**: only tail deletion was run. Not run: middle-entry deletion, a forged head, deleting ledger.jsonl.
- **Ledger mode**: only shared-token mode was run, not per-caller mode (LEDGER_CALLERS_FILE).
- **Environment**: Linux, other OS/arch and CI were not run, and neither were disk-full or fsync injection.
- **Concurrency**: concurrent writers during faults and the multi-writer soak were not run, nor was a simultaneous ledger+service kill.
