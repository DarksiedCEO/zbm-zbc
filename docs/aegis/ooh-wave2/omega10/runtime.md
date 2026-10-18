RUNTIME (H-1 re-witness, log-file hardening, RT9-1 start-up read, OOH2-PII-1): **REAL_PROTOCOL_LOCAL** for services/ooh-py at 0f5fbe3.
VERDICT: **RUNTIME_GREEN_FOR_SCOPE**. There are no blocking findings. RT9-1 does not reproduce at the candidate and does reproduce at the 91ea9a1 control.

Binding:
- candidateSha 0f5fbe33642a62068239f4df83edd8602730fd8e · treeOid ffc3bd9caa09782a69e91d7466ce1b30eef16516
- subtree services/ooh-py/src e046f81a25b5daec6031c1006ca0b9f3d366ffd4 · subtree services/ledger-rust 4f1ab90e2f81245130d6eb87091745146f6398e6 (the same ledger subtree at 91ea9a1 and 75b0662)
- repo DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2 (`git rev-parse ooh-dept-15-wave2` = 0f5fbe3…, so the candidate is the branch tip)
- controls:
  - 91ea9a1948253a3203150b38a8c2f2f839f232cd (tree 482eb898…, ooh-py/src e3d8e68a…)
  - 75b0662823b867df3325d676b1dcf69abe37a425 (tree 67c15180…, ooh-py/src 6d8a4442…)
- store.py blob 045b4aa1… and pii.py blob 44493ca5… in my copy (`git hash-object`); both equal `git rev-parse 0f5fbe3:<path>`.

Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega10 run). Fresh context, independent of the builder and of omega9.
- I read omega9's runtime.md and its scripts only as a model.
- My drivers are new files (common10.py, sw10.py, pii10.py, batch10.sh, summ10.py, table10.py, watchdog10.sh). They were derived from omega9's by recorded edits.
- No omega9 verdict is carried forward.

## 0. Binding checks
- Frozen worktree review-0f5fbe3: `git rev-parse HEAD HEAD^{tree}` -> 0f5fbe3…, ffc3bd9c…; `status --porcelain` -> 0 lines. Main repo: only the pre-existing `?? .git.backup-preimport/` and `?? .idea/`.
- Every run used my own copies:
  - `git archive 0f5fbe3 | tar -x` -> runtime/src_0f5fbe3
  - the same for runtime/src_91ea9a1 and runtime/src_75b0662
- After all runs, fresh `git archive` extracts were compared with `diff -r`: all three copies are **IDENTICAL**. The candidate was not mutated.
- The code delta 91ea9a1..0f5fbe3 under services is limited to services/ooh-py:
  - store.py +36: `_read_at_start` uses O_RDONLY|O_NOFOLLOW|O_NONBLOCK, then fstat, S_ISREG and pread; the write open adds O_NONBLOCK; `_close_quietly`.
  - service.py +18: request_sha is computed over the body with vendor_text redacted.
  - pii.py: strip_format, any-script digits and wider SSN separators.
  - svc_campaigns.py: observed() applies strip_format.

## Execution conditions
- Host: Darwin 25.6.0 (macOS 26.7.1) arm64. Runs 2026-10-18, 15:52–16:08 UTC. 127.0.0.1 only, with ports from bind(0).
- Python: scratchpad venv3.12 (3.12.14) and venv3.13 (3.13.9).
- Dependency mode is REAL:
  - the real ledger-rust binary, built here;
  - the real service entrypoint `python -m api` (cwd = <copy>/services/ooh-py/src), which is api.main -> api.build -> serve.run -> uvicorn, over real HTTP (httpx);
  - LEDGER_SERVICE_URL points at that ledger. There is no mock, relay or fake ledger, and OOH_NON_PRODUCTION is unset.
- Faults are filesystem changes made by the driver while the service runs (or before a restart): same-size edits, unlink + mkfifo, symlinks and mkdir.
- Concurrency: one sequential stream per Python. The two streams ran in parallel on independent ports and data dirs, with one client per service. The rdir restarts and the 91ea9a1 rzero controls ran afterwards, one at a time.
- Every client call has a timeout: 20 s, or 0.2 s for the start-up /health polls.
- RSS was taken with `ps -o rss=` after each call, and every 20 ms during the start-up observations.
- Memory safety:
  - the driver SIGKILLs a starting service above 512 MB RSS;
  - an external watchdog (scripts/watchdog10.sh, 512 MB, 20 ms) SIGKILLs any scratch-venv `python -m api` above 512 MB. It never fired: runs/watchdog.log does not exist.

## 1. Ledger build (this gate, from my 0f5fbe3 copy)
- `cd runtime/src_0f5fbe3/services/ledger-rust && SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir scratchpad/rt10-target`
  - rc=0, "Compiling ledger-rust v0.1.0 (…omega10/runtime/src_0f5fbe3/services/ledger-rust) … Finished `release` … in 5.43s" (runtime/cargo_build.log, sha256 d8d60762…7f44).
- Binary: scratchpad/rt10-target/release/server, sha256 e7a0ebabcedd673dd121b2508add411098e8ccb7799efe85065fb4d9b7507405, Mach-O 64-bit arm64.
  - The target dir is outside omega10: `find omega10/runtime -name target -type d` -> none.
  - All runs, including the controls, used this binary. The ledger tree is the same at all three SHAs.

## 2. Drivers and raw evidence (runtime/)
- Scripts (sha256):
  - common10.py b3604394…7596
  - sw10.py 1e059c3d…58dc
  - pii10.py 17481aa4…bd5ee
  - batch10.sh e5b2b608…a0ae
  - summ10.py 0f1d886b…d49a
  - table10.py 9c0cf58a…6149
  - watchdog10.sh c130922a…c8f1
- Disclosure, my own driver bug: in the first sw10.py (sha256 271e79df…e78a, recorded in scripts/sw10.py.pre-rdir-fix.sha256), `rdir` was missing from the restart-scenario tuple.
  - The batch's 4 "rdir" runs therefore ran the while-running H path with a directory in place of the log. I kept them, renamed to runs/c*_dirrun_H-asfirst_*.
  - I then fixed the tuple (one sed substitution) and re-ran rdir as a restart, 4 runs (runs/batch_rerun.log).
  - All other batch runs used the pre-fix file. The fix only adds "rdir" to that tuple, so their code path is unchanged.
- Raw evidence:
  - runs/<name>.out (timestamped log of every call) and runs/<name>/result.json, plus ledger.log, service.log, and the ooh/ and ledger/ data dirs;
  - batch logs runs/batch_3.12.log (2df30f87…) and runs/batch_3.13.log (f4340b1f…); every sw10 run is rc=0 except the 4 misrouted rdir runs (rc=1, the driver's own put-back failed on a directory);
  - 68 .out files in total.
- Summaries:
  - summary.txt (afd4e9ca4e69b79a7b58ec5f86aa0d8313935e3a938d3ed5e7d30070daff4877)
  - table.txt (d8d648b39d801e7bd47ac84579a4efebcb15bfc6260e8fc4082cfaae6c22f6b1), one line per run
  - pii_table.txt 8b33d980…aeb5a
  - pii_independent_sweep.txt 4acec247…22a2

## 3. H-1 at 0f5fbe3 (both Pythons, identical results)
Setup: W1 t-before and W2 t-mid (201), then 11 s past FORCED_MIN_S. Same-size faults (each with `same_size: true` and fault_sha != orig_sha recorded):
- e2: the last line -> b"y"*len
- e3: the last line forged and self-consistent (t-mid -> t-mie, record_sha256 recomputed)
- m2: line 1 -> b"y"*len
- m3: line 1 forged (t-before -> t-beforf)

**Mode W (a write first):** c0f5fbe3_{e2,e3,m2,m3}_W_{3.12,3.13}, 8 runs
- W3 -> **503 STORE_UNAVAILABLE (maybe true)**, **file_unchanged=True**. Anchors 2->3 (anchored before the write) and pending.line is kept.
- audit and job -> ok False, "the pending line is anchored on the ledger but cannot be written here".
- W4 -> 503 INTEGRITY_UNVERIFIED: unchanged, anchors 3->3.
- Put back while running: job and audit -> ok True (rolled forward). The W3 retry with the same request_id -> 201, file unchanged, anchors 3->3. W5 201. Restart -> audit ok, write 201.
- FINAL: 5 lines = 5 anchors, ledger /verify valid, tenants [t-before, t-mid, t-x, t-z, t-after]. The refused t-y never appears.

**Mode I (integrity first):** c0f5fbe3_{e2,e3,m2,m3}_I_*, 8 runs
- Audit, audit again, job and /status -> **ok False, "the local log file differs from the lines this service holds: an operator must inspect it"**.
- W3 and W4 -> 503 INTEGRITY_UNVERIFIED: unchanged, anchors 2->2.
- Put back: ok True. The W3 retry -> 201 (anchors 2->3), W5 201, restart ok, FINAL 5 = 5 and valid.

**Refused-write bytes:** across all sw10 runs, 66 non-201 POSTs were recorded and the file changed in **0** of them (table10.py over runs/c*/result.json).

**Control 75b0662** (c75b0662_{e2,e3,m2,m3}_{W,I}_{3.12,3.13}, 16 runs):
- W3 -> **201**; audit and job -> **ok True**; W4 -> 201; FINAL 6 = 6, with t-y accepted.
- The scenarios discriminate, and 75b0662 still accepts them.

## 4. Hardening while running: FIFO, symlink to identical bytes, symlink to /dev/zero (both Pythons, 12 runs)
Fault while running: unlink the log, then mkfifo, or a symlink to runs/<run>/same_bytes_target.jsonl (the exact original bytes), or a symlink to /dev/zero.
- Mode H: A1, A2, J1 and /status, then W3, J2, W4.
- Mode HW: W3 first.

Results:
- Every call answered. The largest call on a faulted log in any candidate run was **≤ 0.16 s**. The largest call of any kind was 0.535 s, a 201 W_restart on a regular file after the put-back. There were no timeouts.
- Sampled service RSS stayed between 68,976 and 84,208 kB across all sw10 runs. The regular-file e/m runs are in the same band, so memory was bounded.
- Mode H: A1, A2, J1 and /status -> **ok False**:
  - fifo: "the local log file differs…";
  - symsame and symzero: "the local log file cannot be read (OSError)".
  - W3 and W4 -> 503 INTEGRITY_UNVERIFIED, anchors 2->2.
- Mode HW: W3 -> **503 STORE_UNAVAILABLE**, anchors 2->3, pending kept, and the log is still a fifo or still a symlink. Then integrity -> ok False ("pending line … cannot be written here"), and W4 -> 503 INTEGRITY_UNVERIFIED.
- symsame (H and HW): `target_unchanged: True`. Nothing is written through the symlink.
- Put back (a regular file with the original bytes): J3 and A3 ok True, W3 retry 201, W5 201, restart ok, FINAL 5 = 5 and valid, in all 12 runs.
- Extra (from my misrouted rdir, c*_dirrun_H-asfirst, 4 runs): with a **directory** in place of the log while running, at both 0f5fbe3 and 91ea9a1:
  - A1, J1 and /status -> ok False ("differs…");
  - W3 and W4 -> 503 INTEGRITY_UNVERIFIED, anchors 2->2;
  - calls ≤ 0.043 s at the candidate.

## 5. RT9-1: restart with the log replaced (both Pythons)
The service was stopped (SIGTERM), the fault placed, and `python -m api` started again. It was observed every 20 ms for at most 15 s, recording exit, /health and peak RSS.

| run | 0f5fbe3 (candidate) | 91ea9a1 (control) |
|---|---|---|
| FIFO (rfifo) | **exit rc=1 after 0.236 s (3.12) / 0.276 s (3.13)**, peak RSS 58–60 MB. Last line: `store.StoreCorrupt: the log is not a regular file; refusing to start (inspect the data directory)` (store.py:150) | **no answer within 15 s, still alive**, peak 58–60 MB, no service output. Stopped by SIGTERM |
| symlink to identical bytes (rsym) | **exit rc=1 after 0.238 / 0.267 s**, `StoreCorrupt: the log cannot be opened (OSError); refusing to start: it must be a regular file, not a link` (store.py:145). Target unchanged | starts "degraded" in 0.47 / 0.40 s; integrity ok False; write 503 INTEGRITY_UNVERIFIED; target unchanged |
| symlink to /dev/zero (rzero) | **exit rc=1 after 0.258 / 0.298 s**, peak 58.5 / 60.2 MB, same "cannot be opened (OSError) … not a link" reason | **RSS 846,416 kB (3.12) / 825,168 kB (3.13) within 0.65 / 0.48 s**, SIGKILLed by my driver's 512 MB cap (rc -9) |
| directory (rdir) | **exit rc=1 after 0.52 / 0.60 s**, `StoreCorrupt: the log is not a regular file; refusing to start` | exit rc=1 after 0.57 / 0.75 s with an uncaught `IsADirectoryError` |

(Control log_tail lines show the earlier process's shutdown, because service.log is appended to. The restarted control process wrote nothing.)

Result:
- At the candidate, all four restart faults make start-up refuse promptly (≤ 0.6 s) with a StoreCorrupt reason, and memory stays bounded (≤ 60.2 MB peak).
- The control reproduces RT9-1 (the FIFO hangs; /dev/zero grows past 800 MB in under 0.7 s).
- **RT9-1: closed for the witnessed cases.**

## 6. OOH2-PII-1 through real HTTP (pii10.py; both Pythons)
The setup is omega9's, through the API only against the real ledger: tenant, PRINCIPAL determination, market readiness, and a vendor. Then per case: a campaign, brief and plan, a face and source, and for doc_ref the full path to PROPOSAL_SENT.

Values (19 per field, 57 cases per run):
- **New forms:**
  - "123.45.6789", "123_45_6789", "123/45/6789", "SSN:123.45.6789", "ITIN:912.70.1234"
  - Arabic-Indic "٤١١١ ١١١١ ١١١١ ١١١١", Devanagari "४१११ ११११ ११११ ११११", extended Arabic-Indic "۵۵۵۵-۵۵۵۵-۵۵۵۵-۴۴۴۴"
  - zero-width forms "4111 11​11 1111 1111", "4111​1111​1111​1111", "123-4​5-6789"
- **Regression:** omega9's 5 cards and 3 SSNs.
- **doc_ref:** the 11 new forms plus omega9's 8 doc_ref values.

Scan after each case, cumulative over the whole run:
- Sources:
  - every file in OOH_DATA_DIR;
  - every file in the ledger-rust store dir (ledger.jsonl, .head, .lock);
  - `GET /ledger/entries`;
  - service.log.
- Each source is checked in raw form and with \uXXXX escapes decoded, for:
  - the literal value;
  - its digits (any script read as ASCII) as a substring of any digit run, with spaces, dashes, ._:/ and Cf/format characters removed;
  - sha256 of the unredacted vendor text;
  - sha256 of the request body as sent (BUILD_CONTRACTS §2 formula).
- Independent sweep afterwards (pii_independent_sweep.txt): every file in each run dir, now including ledger.log, normalised and searched for 123456789, 912701234, 4111111111111111, 5555555555554444 and 378282246310005.

**0f5fbe3 (pii_c0f5fbe3_3.12, _3.13), identical:**
- unit vendor_text, **19/19 -> 201 with `personal_data_redacted: true`**:
  - text examples: "faces west, contact [redacted] for access", "… SSN:[redacted] …", "… ITIN:[redacted] …";
  - the unit GET does not contain the value.
- face location_description, **19/19 -> 422 ATTRIBUTE_VALUE_PERSONAL_DATA**, ledger entries Δ = 0.
- client_approval_doc_ref, **19/19 -> 422**. Each time the ledger Δ = 0, the state stays PROPOSAL_SENT with the same version, and the control approve with "io-acme-signed-1" -> 200.
  - 10 of them -> ATTRIBUTE_VALUE_PERSONAL_DATA: "123.45.6789", "123_45_6789", "SSN:123.45.6789", "ITIN:912.70.1234" and omega9's 6.
  - 9 -> string_pattern_mismatch: the schema `^[A-Za-z0-9._:-]{1,100}$` refuses "/", non-ASCII digits, zero-width characters and spaces before the PII rule runs.
- **Scan hits: 0 of 57 cases, in every source.** Independent sweep: **no digit string in any file**, ledger.log included.
- End state: ledger /verify valid (1306 entries); the data dir is [ooh.lock, ooh_log.jsonl].

**Control 91ea9a1 (pii_c91ea9a1_*), both Pythons:**
- vendor_text: these forms are **stored unredacted** (`personal_data_redacted: false`, text as typed): "123.45.6789", "123_45_6789", "123/45/6789", "SSN:123.45.6789", "ITIN:912.70.1234", the 3 non-ASCII-digit cards, "4111 11​11 1111 1111" (stored as "4111 11 11 1111 1111") and "123-4​5-6789".
- face attribute: these forms are **accepted, 200** (ledger Δ = 2): "123.45.6789", "123_45_6789", "123/45/6789", "SSN:123.45.6789", "ITIN:912.70.1234" and the 3 non-ASCII-digit cards.
- doc_ref: "123.45.6789", "123_45_6789", "SSN:123.45.6789" and "ITIN:912.70.1234" are **accepted, 200 CLIENT_APPROVED**.
- The independent sweep finds 123456789, 912701234, 4111111111111111 and 5555555555554444 **in ooh/ooh_log.jsonl**.
- **sha256(request body as sent) was found in ooh_log.jsonl for all 19 vendor_text cases at 91ea9a1, and in 0 at 0f5fbe3.** This is extra evidence for the service.py request_sha change (r9 security F1), and it also confirms that my body-hash formula matches the server's.
- Not discriminating at 91ea9a1 (already refused or redacted there):
  - the zero-width forms on the face attribute;
  - "4111​1111​1111​1111" in vendor_text;
  - every schema-refused doc_ref value.

**OOH2-PII-1: closed for the witnessed fields and values.** Nothing reached the log, the pending files, the ledger store, the ledger entries, ledger.log or service.log.

## Findings
- **None blocking.** No CRITICAL, HIGH or MEDIUM.
- **O10-1 (LOW, informational, not a defect):** for client_approval_doc_ref, the "/", non-ASCII-digit, zero-width and space forms never reach the PII rule. The field's schema pattern refuses them first (422 string_pattern_mismatch at both SHAs). The PII rule is witnessed on that field only for the ASCII [A-Za-z0-9._:-] forms.
  - Reproducer: pii10.py, doc_ref cases 3 and 6–11.
  - Falsifier: a doc_ref schema that admitted those characters would route them to the PII rule.
- **O10-2 (LOW, informational):** a start-up refusal ends as an uncaught StoreCorrupt traceback (rc=1). The reason is the last stderr line. It is prompt and fail-closed; it is just not a structured message.
- **Earlier findings:**
  - RT9-1 (MEDIUM at 91ea9a1): **closed** (§5).
  - H-1: not reproduced at 0f5fbe3 (§3).
  - r8 M-1 while running: closed for the witnessed cases (§4).

## Leftovers / external
- No live or production endpoint was read or written. Only 127.0.0.1 was used.
- No candidate edit, commit or push.
- `ps -axo pid,command | grep -E "omega10/runtime|rt10-target|watchdog10"` -> no process left (the watchdog was stopped via runs/watchdog.stop).
- No `target` dir under omega10. I removed scripts/__pycache__, which my own driver imports had created; the source copies had none (PYTHONDONTWRITEBYTECODE=1).

## NOT RUN / NOT PROVEN
- LIVE_PROVEN / PRODUCTION_PROVEN: not claimed. No deployed SHA was read, and no live provider was involved.
- pytest, including test_aegis_r9_pii_hardening.py: AEGIS-TEST's job.
- Not run:
  - a symlink to a large regular file, or to another device;
  - a FIFO with a live writer;
  - TOCTOU races (between open/fstat and pread, or between the compare and pwrite);
  - concurrent writers;
  - the cost of a large log;
  - Linux/CI.
  - The start-up path was witnessed only for FIFO, symlinks (to identical bytes and to /dev/zero) and a directory. A socket and a hard link were not tried.
- Not run for PII:
  - fields other than these 3;
  - other card networks or lengths, digit scripts beyond Arabic-Indic, extended Arabic-Indic and Devanagari, and Cf characters other than U+200B;
  - false-positive controls (ordinary numbers wrongly redacted);
  - the /audit/evidence "attempted" view.
  - On doc_ref, see O10-1.
- The other five services that share the store.py code (OOH2-RT-H1-<SERVICE>) were not exercised.
- O8-1 (the FORCED_MIN_S throttle) was not re-run.
