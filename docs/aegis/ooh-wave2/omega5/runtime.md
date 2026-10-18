RUNTIME: REAL_PROTOCOL_LOCAL for scope ooh-py (Department 15) at 4a0a4b4: the production entrypoint `cd src && python -m api` against a real ledger-rust process built by this gate, over loopback HTTP, on Python 3.12.14 and 3.13.9 (macOS arm64).
- Re-witnessed: F1, R19 and C07, each with a ledger SIGKILL and restart.
- New integrity-path witnesses on real processes:
  - (a) service SIGKILL between the ledger anchor and the log append -> rolled forward on restart;
  - (b) pending.line without an anchor -> set aside in pending.discarded and not applied, then revived when the anchor lands; b2 stale -> cleared;
  - (c) anchor removed from the real ledger's own storage -> the ledger refuses to start; after an operator reset, integrity is not-ok and writes are refused.
- No live or production claim.
VERDICT: RUNTIME_GREEN_FOR_SCOPE (one LOW non-blocking observation, O-1)

Binding:
- candidateSha 4a0a4b478a88fbaaeb864a7535779e586b39f64a · tree e0640db37e4a40f58e0a291fd21d90a56f45d1ec · branch ooh-dept-15-wave2 (local) · repo DarksiedCEO/zbm-zbc
- ledger-rust tree 4f1ab90e2f81245130d6eb87091745146f6398e6 · ooh-py/src tree 6d8a444233eed6ac29bcbdca37417c70e5002acc
- ledger binary = sha256 cea61a6cf8fb03d07a411be380550a28d7d5ee94c20f2a0c4f45c16ef90cc69f (built by this gate)

Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega5 run). Written by the gate itself in fresh context. The omega4 driver was reused; omega4's verdicts were not relied on.

## 0. Binding and delta
- `git -C /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/review-4a0a4b4 rev-parse HEAD HEAD^{tree}` -> 4a0a4b47…, e0640db3…. `git status --porcelain` -> 0 lines, before and after all runs.
- `git diff --stat 5116917 4a0a4b4`: docs/adr/0022 (+13), docs/test-counts.md (+1/-1), services/ooh-py/tests/test_aegis_integrity_path.py (+622).
- `git diff --stat 5116917 4a0a4b4 -- services/ooh-py/src services/ledger-rust` -> **empty**.
- Tree OIDs are identical at both SHAs: ledger-rust 4f1ab90e…, ooh-py/src 6d8a4442….
- The candidate was extracted with `git archive 4a0a4b4 | tar -x` into /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src.
- After all runs, a second archive in /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/verify_extract was compared with `diff -r` (excluding __pycache__ and target): identical.
- The main repo working tree shows only the pre-existing ?? .git.backup-preimport/ and ?? .idea/.

## Execution conditions
- Host: Darwin 25.6.0 arm64 (T6041), Andres-MacBook-Pro.local, 2026-10-18 03:55–04:07 UTC.
- Python: /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv3.12/bin/python (3.12.14) and /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv3.13/bin/python (3.13.9).
- Rust: cargo and rustc 1.90.0 (Homebrew).
- Dependency mode: a REAL ledger-rust process and a REAL `python -m api` process, all on 127.0.0.1 with bind(0) ports. Runs were sequential and single-client.
- The war room is the exception: it runs in-process with fake ledgers (§3).

## 1. Ledger build (by this gate)
Command: `cd /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src/services/ledger-rust && SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/target`
- rc=0 into a fresh target dir: "Compiling ledger-rust v0.1.0 (/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src/services/ledger-rust) … Finished release in 5.41s" (/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/cargo_build.log).
- `shasum -a 256 /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/target/release/server` -> **cea61a6cf8fb03d07a411be380550a28d7d5ee94c20f2a0c4f45c16ef90cc69f**. Mach-O 64-bit arm64, 2161008 bytes.
- Every real-ledger run below used this binary. No prebuilt binary was used.
- The hash differs from omega4's 9ec28d5f… although the source tree is identical. The source path is embedded in the binary (omega4/… vs omega5/…), and the size is the same. Not investigated further.

## 2. live_run.py (real entrypoint + real ledger)
Command, from /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src/services/ooh-py: `LEDGER_BIN=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/target/release/server LIVE_WORK_DIR=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/live<v> /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python devtools/live_run.py`
- live_run.py:156 reads LEDGER_BIN, and :181/503/524 start `[sys.executable, "-m", "api"]`.

| | py3.12 | py3.13 |
|---|---|---|
| rc / result | 0, "37/37 checks passed" | 0, "37/37 checks passed" |
| processes | ledger pid 66762; service 66767, then 66774 and 66775 | ledger 66788; service 66801, then 67680 and 67826 |
| stdout sha256 | 6e608678…8c87 | 63fec578…dd2 |
| ledger.jsonl (93 lines; "93 from ooh") | 8e2f0e45…348f | b6cba0cc…2bf |
| ooh_log.jsonl (42 lines) | aa5d9b77…f19d | b49bfcd9…6d93 |

Disclosure: the first attempt on each Python ran without creating LIVE_WORK_DIR, and both exited rc=1 with FileNotFoundError in tempfile.mkdtemp. This was a harness precondition, not candidate behavior. The outputs are kept in /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/disclosed/ and are not counted.

## 3. War room (in-process harness, NOT real-ledger)
Command, from /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src: `/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python devtools/warroom/run.py --department ooh-py --seed N --python /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python --out /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/wr_<v>_<N>`

| run | rc | gate | cases (PASS/FAIL/ERROR) | known failures | MUST rate | stdout sha256 |
|---|---|---|---|---|---|---|
| py3.12 seed 1 | 0 | PASS | 156 (156/0/0) | 0 | 100% | 0830f47d… |
| py3.12 seed 7 | 0 | PASS | 156 (156/0/0) | 0 | 100% | 119fa9f3… |
| py3.12 seed 13 | 0 | PASS | 170 (170/0/0) | 0 | 100% | cba8d3cd… |
| py3.13 seed 1 | 0 | PASS | 156 (156/0/0) | 0 | 100% | 0d59b006… |
| py3.13 seed 7 | 0 | PASS | 156 (156/0/0) | 0 | 100% | a48ea401… |

- The war room reports "LLM personas: NOT_CONNECTED".
- README:58 says each case runs "in a fresh in-memory harness with fake ledgers", and drivers/ooh_py.py uses FakeLedger. This is chaos coverage, not real-protocol evidence.
- Disclosure: a first batch was launched from zsh, where `set -- $x` did not split, so every interpreter path was invalid ("no such file or directory: …venv3.12 1/bin/python"). Kept in /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/disclosed/wr_zsh_split/ and not counted.

## 4. F1 / R19 / C07 on the real ledger, with a ledger SIGKILL and restart
Driver: /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/scripts/commit_witness.py (sha256 d09302db…14dd).
- It is omega4's driver (9909d549…) with only the label string changed from omega4 to omega5 (diff: 2 lines).
- A loopback byte relay sits in LEDGER_SERVICE_URL, used only for fault timing. It forwards bytes to the real ledger and never answers on the ledger's behalf.

Command: `SCEN=<f1|r19|c07> LEDGER_BIN=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/target/release/server TREE=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src WORK=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/cw_<scen>_<v> /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python -I /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/scripts/commit_witness.py`

All 6 runs (3 scenarios × 2 Pythons) gave **RESULT PASS**. Observed on py3.12; py3.13 was identical in outcome.

- **F1** (out 4ff2e939…; py3.13 52c7be78…):
  - The relay forwarded the anchor and the real ledger answered "HTTP/1.1 201 Created". The ledger was then SIGKILLed (returncode -9) and the answer dropped.
  - W2 -> 503 {LEDGER_UNAVAILABLE, maybe: True}. Integrity became False ("a ledger answer was lost; …"). W3 -> 503 INTEGRITY_UNVERIFIED.
  - After the ledger restart and 16 s, /audit/integrity was ok with log_length 2.
  - The same request_id got 201 with the original created_at.
  - FINAL: log [t-before, t-x, t-after], log 3 = anchors 3, ledger valid (6 entries). The evidence view showed committed 3, attempted 0.
- **R19** (7624c0d0…; py3.13 5d206a7e…):
  - The evidence event was forwarded and recorded, then the ledger was SIGKILLed before the answer was passed back.
  - W2 -> 503 {LEDGER_UNAVAILABLE, maybe: False}. No pending line, integrity stayed True. W3 -> 503 LEDGER_UNAVAILABLE.
  - FINAL: log [t-before, t-after], log 2 = anchors 2, ledger valid (5 entries). The evidence view showed committed 2, attempted 1. t-x never took effect.
- **C07** (82c868ba…; py3.13 9b55e888…):
  - The anchor was recorded (201), then ooh_log.jsonl was chmod 0400.
  - W2 -> 503 {STORE_UNAVAILABLE, maybe: True}. Integrity became False ("anchored but not written"). W3 -> 503 INTEGRITY_UNVERIFIED.
  - Permissions were restored, then the ledger was SIGKILLed (-9). W3b -> 503 INTEGRITY_UNVERIFIED. The ledger was restarted.
  - After 16 s, integrity was ok and log_length 2 with t-x rolled forward.
  - FINAL: log 3 = anchors 3, ledger valid (6 entries).

Artifacts, ledger.jsonl / ooh_log.jsonl sha256 (py3.12; py3.13):
- F1: 0a6843ac… / ba6a26c1…; 368baf35… / 3f327922…
- R19: 9efbf354… / 83e63c0d…; 7b68be52… / 77e65bcd…
- C07: bb9ea5c6… / b6ed96ce…; fe225ca3… / bafbc8b9…

## 5. Integrity-path scenarios on real processes (new; driver written by this gate)
Driver: /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/scripts/integrity_witness.py (**v4**, sha256 d4960982a42d09062d637208a4112257b9014215b442373570b5a035d447aa62). All counted runs used v4.

Command: `SCEN=<a|b|b2|c|clat> LEDGER_BIN=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/target/release/server TREE=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/src WORK=/private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/iw_<scen>_<v> /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/venv<v>/bin/python -I /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/scripts/integrity_witness.py`

All 10 runs (5 scenarios × 2 Pythons) gave **RESULT PASS**. Observed values below are from py3.12; py3.13 was identical in outcome.

**(a) Crash after the anchor and before the append (the relay timed it; nothing was planted).** iw_a_3.12.out 882b8ca1…; py3.13 e4193ea6….
- The relay forwarded the log_anchor "tenant_created #2", and the real ledger answered "HTTP/1.1 201 Created".
- Before the answer was passed back, the **service** was SIGKILLed (pid 75024, returncode -9).
- State at the kill: ooh dir [ooh.lock, ooh_log.jsonl, pending.line], log_lines 1, pending sha256 ad11d14c…. The ledger held 2 ooh anchors.
- The client saw RemoteProtocolError.
- After the service restart, integrity was ok with no problem, and the dir was [ooh.lock, ooh_log.jsonl], so pending.line was gone.
- The local log tenants were [t-before, t-x], and **log line 2 is byte-identical to pending.line** (sha256 equal). The line rolled forward.
- A retry with the same request_id got 201 with the original created_at. The new write W4 got 201.
- FINAL, after a SIGTERM restart: log [t-before, t-x, t-after], log 3 = anchors 3, ledger valid (6 entries).

**(b) pending.line with no anchor on restart, revived later.** 1213d579…; py3.13 a26cb9ee….
- The relay held the log_anchor request and did not forward it. The service was SIGKILLed (-9).
- State: pending.line present (73f012a1…), log_lines 1, ledger anchors 1.
- After the restart, integrity was ok and the dir was [ooh.lock, ooh_log.jsonl, **pending.discarded**], with no pending.line.
- **pending.discarded is byte-identical to pending.line.** The log tenants were [t-before], so the line was **not applied**.
- The held request was then delivered late to the real ledger, which answered "HTTP/1.1 201 Created".
- POST /ooh/v1/jobs/integrity/run (scheduler) -> 200 with ok True.
- The log tenants became [t-before, t-x] and pending.discarded was removed: **revived**.
- W4 -> 201. FINAL: log 3 = anchors 3, ledger valid.

**(b2) Set aside, the anchor never arrives, and a new write takes the seq.** 5dd60e21…; py3.13 304faebf….
- The setup and restart matched (b): pending.discarded present, log [t-before].
- W3 t-y -> 201 and took seq 2.
- The integrity job returned ok, pending.discarded was cleared (stale), and the log was [t-before, t-y]. t-x was never applied.
- FINAL: log [t-before, t-y, t-after], log 3 = anchors 3, ledger valid (7 entries; the t-x evidence event stays "attempted").

**(c) Anchor deleted or rolled back in the ledger's own storage (my copy).** 2036e2e5…; py3.13 98594649….
- W1 and W2 -> 201. The audit was ok with log_length 2.
- The ledger was SIGKILLed (-9). The last ledger line was removed from ledger.jsonl: seq 3, department ooh, log_anchor "tenant_created #2", sha256 30ae953b…. The file went from 4 lines to 3.
- (1) A plain ledger start exited **rc 1**: "REFUSING TO START … holds 3 entries but the head checkpoint records 4 … acknowledged entries were removed (truncation or a restore of an older copy)".
  - After 11 s, /audit/integrity -> ok **False**, "the ledger cannot be read", ledger_valid False.
  - W3 -> **503 INTEGRITY_UNVERIFIED**.
- (2) The operator reset ran with `LEDGER_ALLOW_RESET=<binding printed by the refusal>` (py3.12: 4:32223148635adc25/3:74c8437cbc5c7a6d in the v3 run; v4 used its own printed value).
  - The ledger applied the reset and exited rc 0 without serving, by design. A plain start then served the rolled-back log: 3 entries, 1 ooh anchor.
  - POST /jobs/integrity/run -> ok **False**, "**local log line 2 has no matching anchor on the ledger**", ledger_valid True. The audit returned the same.
  - W4 -> **503 INTEGRITY_UNVERIFIED**. After a service SIGTERM restart, status.integrity was still False with the same problem, and W5 -> **503 INTEGRITY_UNVERIFIED**.
  - The ledger stayed at 3 entries and 1 ooh anchor from the reset to the end, so refused writes recorded nothing. The local log stayed [t-before, t-x].

**(clat) Informative only: a write before any check after a rollback.** 9e9e3aef…; py3.13 72ce5103….
- The rollback was the same as in (c). Right after the reset and restart, with no integrity check yet, W3 t-lat -> **201** and was appended as local line 3 with its anchor on the ledger.
- The next job reported ok False, "local log line 2 has no matching anchor". W4 and W5, the latter after a service restart, -> 503 INTEGRITY_UNVERIFIED. See O-1.

**Falsifiers.** Same driver and binary, py3.12, on scratch copies under /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/fals. Each mutant's diff against the candidate is in fals/<m>.diff.
- **ma2** (service.py:428 `e = by_id.get(eid)` -> `e = None`, so a disk or aside line is never vouched) vs (a): **RESULT FAIL**. After the restart, the line sat in pending.discarded and the log was [t-before]. Integrity went False ("the ledger holds anchors beyond the local log"), and the retry and W4 got 503. FINAL: log 1, anchors 2. (fals/ma2_a.out 58555279…)
- **mb** (anchor check in _roll_forward disabled) vs (b): **RESULT FAIL**. The unanchored line was applied at restart (log [t-before, t-x]) and integrity was False ("local log line 2 has no matching anchor"). The later late anchor made it look whole. (f45304e2…)
- **mc** (`_anchor_problem` returns None) vs (c): **RESULT FAIL**. After the rollback the job said ok True, and W4 and W5 got **201**. (22ff1ecd…)
- **ma** (disk path in _settle_pending never rolls forward) vs (a): **RESULT PASS**. The mutant does not discriminate. The disk line is set aside, then revived from pending.discarded in the same check because its anchor is on the ledger. At runtime the two paths are redundant for this case. This is reported as an equivalent mutant at this level, not as a defect. (77ec79ae…)

**Disclosures: driver iterations that are not counted.** All are kept in /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/runtime/disclosed/.
- v1 (iw_a_v1): (a) gave FAIL on a predicate bug. It hashed log line 2 *with* its trailing newline against pending.line, which has none. Without the newline the hashes are equal (e6708b43…). The observed behavior was identical to the passing run.
- v2 (iw_v2): all a, b and b2 runs gave FAIL on a race in the driver. The main thread read procs["service"].returncode before the relay thread's wait() had set it (None), while the relay log shows "returncode -9". Every behavioral value was as in the passing runs.
- v3 (iw_v3_c): c and clat crashed in the driver because it expected the reset start to serve; ledger-rust applies the reset and exits rc 0. The v3 a, b and b2 runs passed but were re-run under v4 so that one driver hash covers every counted run (iw_v3_abb2_pass).

## 6. External systems
- GET /ooh/v1/status ports in every scenario: finance_31, vendor_orders, esign, inventory_data, measurement and programmatic_dooh are all NOT_CONNECTED.
- Every URL was http://127.0.0.1:<port>. No live or production endpoint was read or written, and nothing was pushed.

## 7. Leftover processes
- `ps -axo pid,command | grep -E "omega5/runtime|python -m api|integrity_witness|commit_witness"` -> no process of this gate.
- One `venv3.12/bin/python -m api` (pid 76593) was seen and left alone because it belongs to a peer gate: cwd /private/tmp/claude-501/-<HOME>-PycharmProjects-zbm-zbc/f0fc1212-c099-4834-8985-8a68230f83b3/scratchpad/aegis/omega5/reliability/c/services/ooh-py/src, parent pid 76425 `python -u poc/sigkill.py 11 20 L`.

## Findings
**O-1 (LOW, non-blocking): rollback detection waits for the next integrity check.**
- Claim: the ledger rollback in (c) needs an operator reset (LEDGER_ALLOW_RESET) to serve at all. Once it serves, the service keeps its cached integrity ok until the next check, which runs at start, on POST /jobs/integrity/run, or on GET /audit/integrity.
- Evidence: `_commit` checks only `self.integrity["ok"]` (service.py:161), and `_gate` -> `verify_integrity()` is unforced and returns the cached value while ok (service.py:314–315).
- Observed: one write was accepted in that window (clat W3 201). It was caught at the next check, and every write after that was refused.
- Docs: no doc claims immediate refusal.
- Reproducer: SCEN=clat as above.
- Falsifier: a forced check before every commit would make clat W3 return 503.
- Severity: the requested property, not-ok plus refusal after the rollback, holds once checked. Ω decides whether scheduler cadence is acceptable.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN and PRODUCTION_PROVEN: every port is NOT_CONNECTED and no deployed SHA was read. Not claimed.
- pytest suite, including the new tests/test_aegis_integrity_path.py: not run. Test accounting belongs to AEGIS-TEST.
- omega4's F1, R19 and C07 falsifiers (the pre-fix d1b0de9 tree and the r19/c07 mutants) were not re-run at this SHA. The F1, R19 and C07 runs here are positive-only. Their discrimination was shown at omega4 on identical src, and I do not carry it forward as proof.
- (a) was injected by killing the service from the relay at the exact point. The "plant pending.line" variant was not run.
- (c) covered tail deletion with an operator reset. Not run:
  - deleting a middle ledger entry (it would break the ledger chain, so the ledger would refuse to start);
  - a forged head plus log rewrite;
  - deleting ledger.jsonl entirely.
- A late anchor arriving after b2's seq reuse was not run.
- ledger-rust per-caller scoped mode (LEDGER_CALLERS_FILE): only the shared-token mode was run.
- Linux and other OS/arch, other Pythons, CI.
- Disk-full and fsync injection.
- Concurrent writers during faults, and the multi-writer SIGKILL soak.
- Integrity-path scenarios with the ledger killed at the same time as the service.
