# AEGIS-DATA (Gate 5) — omega10
- repo: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: 0f5fbe33642a62068239f4df83edd8602730fd8e
- treeOid: ffc3bd9caa09782a69e91d7466ce1b30eef16516
- scope: persistence/data integrity of `git diff 91ea9a1 0f5fbe3 -- services` (ooh-py: pii.py, service.py, store.py, svc_campaigns.py + 2 tests); OOH2-PII-1 for unit vendor_text (redact), face text attribute and client_approval_doc_ref (refuse)
- author: AEGIS-DATA (AG-AEGIS-DATA v1.0.0), fresh context, read-only on candidate; Claude Opus 5.5 subagent; 2026-10-18
- method: own `git archive` trees + disposable data dirs + own ledger-rust build under scratchpad/data10run (outside omega10); no shared/real datastore. omega9 PoCs reused as a starting point; every result below is from this run.
- raw/PoCs: scratchpad/aegis/omega10/data/{poc,out}

## Environment
- ledger: `services/ledger-rust` identical at 91ea9a1 and 0f5fbe3 (`git diff --quiet` rc 0); built by me from my t-0f5fbe3 archive, `cargo build --release --offline`, SDKROOT=MacOSX26.5.sdk, private CARGO_TARGET_DIR → `server` sha256 3adac90b1965835487d2c6bd90c2e8794b0999d906a27d16cb079b1b92a131d1 (differs from omega9's build: path-dependent; not a provenance claim). One ledger process/port/file per case, stopped at case end.
- service: in-process (helpers.Harness + real HttpLedgerClient over HTTP) from my `git archive` trees t-0f5fbe3 / t-91ea9a1 / t-75b0662 / t-99569bd (t-0f5fbe3 == frozen review copy except its `__pycache__`); Python 3.12 and 3.13 venvs.

## 3b. Start-up read of the log (poc/start_read.py → out/start-read-0f5fbe3-3.1x.json)
Log written via API, path replaced, fresh process started; data-dir snapshot (type/size/sha256/link target) + ledger event count before/after; 20 s alarm.
| case | start (3.12 = 3.13) | data dir changed | ledger events added | after the regular file is put back |
|---|---|---|---|---|
| control (regular file) | STARTED, write 201 | log grew (the write) | 2 | n/a |
| symlink → identical copy | REFUSED StoreCorrupt "cannot be opened (OSError) … not a link"; copy untouched | none | 0 | started, integrity ok, state diff [] |
| symlink dangling | REFUSED same | none (no target created) | 0 | same |
| FIFO | REFUSED "not a regular file", no hang | none | 0 | same |
| directory | REFUSED "not a regular file" | none | 0 | same |
| unix socket (extra) | REFUSED "cannot be opened (OSError) … not a link" (reason text says link for a socket: wording only) | none | 0 | same |
| device node | **NOT RUN**: mknod EPERM (non-root); code path = same fstat/S_ISREG branch as FIFO/dir (store.py `_read_at_start`) | — | — | — |
→ C13 VERIFIED (6/7 cases; device NOT RUN).

## 2. request_sha: idempotent retries and logs written earlier (poc/xv_idem.py → out/xi-{A}-to-{B}-3.1x.json, real ledger)
Writer A runs a lifecycle (tenant, 3 units: plain / card `4111 1111 1111 1111` / `123.45.6789` vendor_text, face attribute, campaign → authorized → submitted PO) and records its 60 2xx requests; reader B starts on the same data dir + ledger file, replays all 60, then 5 same-request_id/other-body retries, one new write, a restart. "sha fn" = A's `request_sha(body)` vs B's for each of the 60 bodies. Results identical on 3.12 and 3.13.
| A → B | start / integrity / state diff | sha fn equal | replays of A's 60 | other-body retries | lines added by replays+retries | new write, restart, anchors==lines |
|---|---|---|---|---|---|---|
| 0f5fbe3 → 0f5fbe3 | ok / ok / [] | 60/60 | 60 original status (31×201, 29×200) | 5/5 409 | 0 | 201, ok, 61/61, 0 problems |
| 91ea9a1 → 91ea9a1 (control) | ok / ok / [] | 60/60 | 60 original | 5/5 409 | 0 | 61/61 |
| 75b0662 → 75b0662 (control) | ok / ok / [] | 60/60 | 60 original | 5/5 409 | 0 | 61/61 |
| 91ea9a1 → 0f5fbe3 | ok / ok / [] | 58/60: the 2 PII vendor_text bodies differ | 58 original; **2 (card, 123.45.6789 units) 409 REQUEST_ID_REUSED** | 5/5 409 | 0 | 201, ok, 61/61, 0 problems |
| 75b0662 → 0f5fbe3 | ok / ok / [] | 58/60 same 2 | 58 original; same 2 → 409 | 5/5 409 | 0 | 61/61, 0 problems |
- Non-PII request_sha unchanged across 91ea9a1/75b0662 → 0f5fbe3 (58/58), every such retry answers as first time, 0 duplicate lines. → C9, C11 VERIFIED.
- A retry spanning the upgrade of a request whose vendor_text held PII answers 409 instead of its first answer; nothing written (INFO-4). The old lines keep their raw-body request_sha (log is append-only) → see §1c.

## 4. Funds reference: names_a_card 99569bd vs 0f5fbe3 (poc/funds_corpus.py, funds_decide.py, funds_service.py → out/funds-names_a_card-diff.json, out/funds-service-*.json)
- Unit level: my corpus (seed 20261018 over both trees' test strings + the omega9 non-ASCII-separator cases + 26 added Arabic-Indic/Devanagari/combining-mark/ZW card refs) = 77,980 refs. `names_a_card`, `funds_ref_parts`, `funds_ref_key` per ref: result-row sha256 **1d5f5981a7966eeed328e95ac261262b7870d6cf3f2f853137b0253c03490f53 at 99569bd and 0f5fbe3, on 3.12 and 3.13**; refused as card 9122 = 9122; rows differing **0** (omega9's 6 U+0131 rows now agree again: `_DIGIT_RUN` no longer re.I) → INFO-1 closed.
- Service level (38 refs, real data dir): output sha256 0e2e7d2d663277ac59133e48eecb41add4dab78f235be5d67687d64a33d24a77 identical at both SHAs on both Pythons (status, detail, lines, ledger events, ref_in_log).
→ C10 VERIFIED (same set, 77,980/77,980 and 38/38).

## 3. H-1 re-run on the real ledger (poc/rl_omega9.py unchanged, h1_summary.py → out/h1-0f5fbe3-3.1x.{json,summary.txt})
- 66 cases per Python at 0f5fbe3 (same matrix as omega9: same-size edits, prefix/tail, deleted, replaced, symlink, FIFO, directory, adopt-bad-prefix × before/at_append/check_first; tenant + submit money lines; restore and restart variants).
- Per-case verdict lines (counts stripped) **identical to omega9's at 91ea9a1** on 3.12 and on 3.13 (`diff` empty), and 3.12 == 3.13. any forced check/audit ok while changed: **0/66** each Python; dup event ids 0; restore cases rolled forward once, anchors == lines.
- The 20 summary "FAIL" flags are the same 20 as omega9, all known: 15 check_first|restore (write refused before any anchor → nothing to roll forward, anchors==lines 14/14), 3 deleted (the write-path open re-creates a 0-byte file where none was: documented), 4 restart-on-deleted/shortened (= M-3; the deleted|at_append|*|restore ones roll forward 15/15 / 43/43).
→ C1, C2, C3 VERIFIED unchanged.

## 5. M-3 and M-2 (poc/m3_stale_delete.py, poc/targeted.py → out/m3-*, out/targeted-*)
- M-3: output **byte-identical at 91ea9a1 and 0f5fbe3** on both Pythons (sha256 8bcb404b…e81a ×4): shorten_then_restore → pending.line deleted at restart 1, "anchors beyond the local log", inflight not applied, 503, anchors/lines 14/13. **M-3 UNCHANGED, open MEDIUM.**
- M-2: targeted output semantically equal at both SHAs (json == True; bytes differ only in key order of one counter): B_consistent_rewrite_inflated_funds → STARTED, health degraded, write 503, **hub read 200 client_funds_received 99999.00**, no marker. **M-2 UNCHANGED, open MEDIUM.**

## 1. OOH2-PII-1 — persisted bytes, real ledger (poc/pii_persist3.py, pii_summary3.py → out/pii-0f5fbe3-3.1x.{json,summary.txt}; in-process poc/probe10.py → out/probe10-*.json)
55 distinct spellings (omega9's 39 + 16 new; one duplicate entry errored on its case-dir name, its first run counted) × 5 paths (unit vendor_text; face text attribute `location_description`; client_approval_doc_ref; vendor_text in pending.line after a failed append; in pending.discarded after a kill between pending write and anchor) = 275 cases per Python, outcomes identical on 3.12/3.13. Scanned: every file of data dir + ledger dir, /audit/export (paged), /audit/evidence, campaign/unit/face views; clear, NFKC, JSON-escaped, Unicode-digit-folded, combining-mark-stripped forms, and the digit string inside any letter-free run.
| class | spellings | vendor_text | face attribute | approval ref | pending.line / .discarded |
|---|---|---|---|---|---|
| **D9-H1 set**: 123.45.6789, 123_45_6789, 123/45/6789, १२३.४५.६७८९, ٤١١١ ١١١١…, ४१११ १११…, ५५५५-…-४४४४ | 7 | redacted, 0 leaks | 422 PERSONAL_DATA, nothing written | 422, nothing written | 0 leaks |
| D9-H1 keyword forms (SSN 123.45.6789, SSN:123.45.6789, tax id 123_45_6789, ITIN:912.70.1234, + SSN 123/45/6789, tax id: 123.45.6789) — separate run out/pii-keyword-0f5fbe3-3.1x.* (30 cases/Python) | 6 | redacted, 0 leaks | 422 PERSONAL_DATA, nothing written | 422 (detector or schema), nothing written | 0 leaks |
| omega9 ASCII cards/dashed SSNs/keyword forms + new 123:45:6789, SSN123456789, ı-separated card | 30 | redacted, 0 leaks | 422 | 422 | 0 leaks |
| **new: SSN with spaced dash / doubled or other punctuation**: `123 - 45 - 6789`, `123 – 45 – 6789`, `SSN: 123 - 45 - 6789`, `123--45--6789`, `123..45..6789`, `123 . 45 . 6789`, `123,45,6789`, `SSN 123,45,6789`, `123\|45\|6789`, `123·45·6789`, `123+45+6789` | 11 | **stored in clear** (201, redacted:false) | **200, stored** | `123--45--6789`, `123..45..6789`: **200, stored**; others 422 schema | **clear** |
| **new: combining mark inside a digit group**: `1̃23-45-6789`, `4̃111 1111 1111 1111` | 2 | **clear** | **200, stored** | 422 schema | **clear** |
| **new divergent**: `123  45  6789` (two spaces), U+1680 / U+E000 / U+0378 separators | 4 | redacted (observed() folds them to a space) but see §1c | **200** (not refused), stored `[redacted]` | 422 schema | 0 leaks |
| documented non-detection `123456789` (no keyword) | 1 | clear | 200 | 200 | clear |
- Leaks in: ooh_log.jsonl, /audit/export, unit/face/campaign views, pending.line, pending.discarded. **Ledger file: 0 leaks** in all 275 cases.
- Refused requests: **89/89 nothing written** (log length, ledger event count, every file sha256 unchanged), 0 leaks, control write after each 200 (47 ATTRIBUTE_VALUE_PERSONAL_DATA, 42 Ref schema).
- Cause (pii.py at 0f5fbe3): `_SSN` (l.161-162) takes exactly one character from `[-‐-―−._:/ ]` between groups, so a spaced or doubled separator, a comma, bar, middle dot or plus is not an SSN; `_SSN_VALUE` (l.164) allows at most 12 characters between first and last digit and no comma/bar; combining marks (Mn) are neither removed by `strip_format` (only Cf) nor digits, so they split a group (`_DIGIT_RUN`/`_SSN` need contiguous `[0-9]`/`\d{3}`).
- Unit replays: 55 × 3 = 165 → 0 duplicate lines, 0 duplicate ledger events, units == 1 each; same request_id + other body → 409 55/55.
- Same request_id, vendor_text differing only inside the redacted number (poc/idem_pii_collapse.py → out/idem-pii-collapse-*.json): 0f5fbe3 answers the first unit (201, 0 lines; stored text identical either way), 91ea9a1 answered 409 (INFO-5).

## 1c. request_sha recovery (poc/request_sha_recover3.py → out/request-sha-recover-*.json*)
| log / spelling | stored | request_sha = | search (area digits 123 known) |
|---|---|---|---|
| 0f5fbe3 unit `123-45-6789` | `[redacted]` | hash of the redacted body (checked: equal) | 10^6 tried, **not found** |
| 0f5fbe3 unit `123.45.6789` | `[redacted]` | redacted body | 10^6, **not found** |
| 0f5fbe3 unit card `4111 1111 1111 1111` (BIN6+last4 known) | `[redacted]` | redacted body | 10^5 Luhn candidates, **not found** |
| **0f5fbe3 unit `123  45  6789`** | `[redacted]`, redacted:true | **raw body** | **found in 2.50 s** (456,790 tries) |
| **0f5fbe3 unit `123 45 6789`** | `[redacted]` | **raw body** | **found in 2.67 s** |
| **0f5fbe3 face attribute `billing 123  45  6789`** (200, not refused) | `billing [redacted]` | **raw body** (`_screened` rewrites only `vendor_text` keys) | **found in 2.12 s** with expected_version 1 (2, 3: not found) |
| 91ea9a1-written log after upgrade to 0f5fbe3 (card unit) | `[redacted]` | raw body (written at 91ea9a1, kept) | **found in 0.14 s** (INFO-3) |
- Unit lines also keep `vendor_text.sha256` = sha256 of the *redacted* text (checked), not a recovery path.
- Cause: service.py `_screened` (l.68-78) redacts only when `pii.has_personal_data(raw)` is true and only under the key `vendor_text`; `observed()` (svc_campaigns.py:64) first maps every non-printable character to a space and collapses whitespace, so it redacts spellings the raw check misses; svc_inventory.parse_value (l.73) refuses on the raw check and then stores `observed(value)`.
→ D9-M1: CLOSED for spellings the raw detector sees; **open for the divergent set (D10-M1)**.

- L-A re-run (poc/h1_stale_report.py → out/h1-stale-report-0f5fbe3-3.12.json): equal to omega9's at 91ea9a1 with timestamps stripped (cached `/health` ok, `/status` + `/audit/integrity` ok within 10 s of the last forced check, then ok:false, write 503, file untouched). L-A UNCHANGED.

## Checks
| id | area | result | method |
|---|---|---|---|
| C1 | H-1 write path: a changed file is never written over | VERIFIED (unchanged) | rl_omega9 66×2, real ledger; verdicts == omega9 |
| C2 | H-1 never reported ok while changed (forced checks/audit) | VERIFIED; cached reads lag (L-A, unchanged) | rl_omega9 + h1_stale_report |
| C3 | H-1 recovery: rolled forward once, anchors==lines, no dup | VERIFIED (restore); restart-on-deleted/shortened = M-3 | rl_omega9, m3_stale_delete |
| C5 | M-3 | UNCHANGED (open) | m3 output byte-identical 91ea9a1 vs 0f5fbe3 |
| C6 | M-2 | UNCHANGED (open) | targeted output json-equal 91ea9a1 vs 0f5fbe3 |
| C7a | D9-H1 spellings (13 incl. keyword forms) reach no persisted byte | **VERIFIED: 0 leaks** (7×5 + 6×5 cases per Python) | pii_persist3 + keyword run, real ledger |
| C7b | OOH2-PII-1 overall: no card/SSN in persisted bytes | **MISMATCH** (D10-H1: 13 new spellings stored in clear) | pii_persist3 275×2 |
| C8 | a refused request writes nothing | VERIFIED (89/89 + 12/12 per Python) | file sha256 + log length + ledger count |
| C9 | request_sha idempotency: same body replay, other body 409 | VERIFIED: 165 PII-unit replays + 60×5 lifecycle replays, 0 duplicate lines/events; other body 409 (55/55, 5/5 ×5); PII-only difference now replays (INFO-5) | pii_persist3, xv_idem, idem_pii_collapse |
| C10 | names_a_card same set as 99569bd | VERIFIED 77,980/77,980 rows + 38/38 service | funds_decide, funds_service |
| C11 | 91ea9a1 / 75b0662 logs start and replay at 0f5fbe3; non-PII request_sha unchanged | VERIFIED (58/58 bodies; state diff []; 61/61 anchors); PII-bearing retries 409 (INFO-4) | xv_idem |
| C12 | redacted value not recoverable from persisted bytes | VERIFIED for detected spellings (3 searches, not found); **MISMATCH** for divergent spellings (D10-M1) | request_sha_recover3 |
| C13 | start-up read: symlink/FIFO/dir/socket refuse with a reason, nothing written | VERIFIED 6 cases ×2; device NOT RUN | start_read |

convergence: n/a (no schema/format migration in the delta: log format unchanged; a 91ea9a1/75b0662 log starts with state diff [], C11). idempotency: [{path: unit inventory add (PII vendor_text, 55 spellings), replays: 165, duplicateEffects: 0}, {path: full lifecycle 60 requests, A→B for 5 pairs × 2 Pythons, replays: 600, duplicateEffects: 0}, {path: other-body retries, replays: 50 + 110, duplicateEffects: 0}].

## Findings
- **D10-H1 HIGH (BLOCKING) — OOH2-PII-1 residual: SSNs (and a card) in further common spellings are stored in clear.** `123 - 45 - 6789`, `123 – 45 – 6789`, **`SSN: 123 - 45 - 6789`**, `123--45--6789`, `123..45..6789`, `123 . 45 . 6789`, `123,45,6789`, **`SSN 123,45,6789`**, `123|45|6789`, `123·45·6789`, `123+45+6789`, and a combining mark inside a digit group (`1̃23-45-6789`, `4̃111 1111 1111 1111`) reach ooh_log.jsonl, pending.line, pending.discarded, /audit/export and unit/face/campaign views. vendor_text 201 `personal_data_redacted:false`; face text attribute **200, stored**; client_approval_doc_ref **200, stored** for `123--45--6789` and `123..45..6789` (the other shapes fail the Ref schema). The log is append-only and anchored, so a stored line cannot be scrubbed without the integrity failure of §3. Ledger file: 0 leaks. Code: pii.py:161-164 (`_SSEP` single character, `_SSN_VALUE` 7-12 middle characters, no `,|·+`), pii.py:67-75 (`strip_format` removes Cf only, not Mn). The D9-H1 spellings themselves are closed (C7a).
  - Reproducer: `SECRETS_ONLY="SSN: 123 - 45 - 6789|123 - 45 - 6789|123--45--6789|123,45,6789" python poc/pii_persist3.py <ledger-bin> <fresh-dir> <tree-0f5fbe3>` then `python -I poc/pii_summary3.py out.json`; in-process `python -I poc/probe10.py <tree>`.
  - Falsifier: the same command gives vendor_text `personal_data_redacted:true` with 0 leaks and face/approval 422 with log length, ledger count and file hashes unchanged for every spelling in the bold rows of §1.
- **D10-M1 MEDIUM — D9-M1 residual: request_sha still hashes the raw body when the raw check misses but storage redacts.** For `123  45  6789` (two spaces) and U+1680/U+E000/U+0378 separators, the raw `has_personal_data` is false, so `_screened` keeps the raw vendor_text, while `observed()` (non-printables → space, whitespace collapsed) stores `[redacted]`. The face text attribute path behaves the same and also answers 200 instead of refusing (parse_value refuses on the raw check, then stores `observed(value)`), and `_screened` never rewrites the attribute `value` key. Measured: SSN recovered from ooh_log.jsonl alone in 2.50 s (unit, two spaces), 2.67 s (unit, U+1680), 2.12 s (face attribute) with the area digits known; full 10^9 space ≈ 80 min one core at ~200k/s (extrapolated, not run). Code: service.py:68-78, svc_campaigns.py:64-67, svc_inventory.py:73 + 112.
  - Reproducer: `SEP="  " python -I poc/request_sha_recover3.py <unit-dir>/ooh/ooh_log.jsonl ssn_area3 123`; `FIELD=face EXPECTED_VERSION=1 SEP="  " python -I poc/request_sha_recover3.py <face-dir>/ooh/ooh_log.jsonl ssn_area3 123` (dirs from pii_persist3 with those spellings).
  - Falsifier: the stored request_sha equals the hash of the body as stored (redacted) or is keyed outside the data dir; the search then finds nothing in 10^6 candidates; and the face attribute answers 422 for those spellings.
- **M-3 MEDIUM (unchanged, open)** — an anchored pending line is deleted at start when the log is one line shorter or deleted; after restore "anchors beyond the local log", line lost, 503 for good. Reproducer: `poc/m3_stale_delete.py` shorten_then_restore; rl_omega9 `deleted|at_append|*|restart`, `shorter_line|at_append|submit|restart`. Falsifier: after the restore the line is applied, anchors == lines.
- **M-2 MEDIUM (unchanged, open)** — after a consistent rewrite, hub read 200 serves client_funds_received 99999.00 (true 6000.00) with no integrity marker while writes 503. Reproducer: `poc/targeted.py <tree> <dir>` key B_consistent_rewrite_inflated_funds. Falsifier: hub read 503 or flagged while integrity.ok is false.
- **L-A LOW (documented, unchanged)** — cached reads report ok for up to 10 s after a file change with no write. Reproducer: `poc/h1_stale_report.py`. Falsifier: those reads report the file problem immediately.
- INFO-1 — CLOSED (names_a_card identical to 99569bd, §4).
- INFO-3 (extended) — card/SSN bytes and raw-body request_sha written before 0f5fbe3 stay in the log after upgrade; measured: a 91ea9a1-written redacted card recovered from the upgraded log in 0.14 s. No scrub path for an anchored append-only log.
- INFO-4 — a retry spanning the upgrade of a request whose vendor_text held PII answers 409 REQUEST_ID_REUSED instead of its first answer (2/60 per pair; 0 lines written).
- INFO-5 — same request_id with vendor_text differing only inside the redacted number now answers the first unit (0 lines; stored text identical either way); service.py's docstring "two different requests never hash alike" does not hold for vendor_text.
- Wording only: a unix socket at the log path is refused with "… must be a regular file, not a link".

## NOT RUN
- Device node at the log path (mknod needs root; EPERM recorded). No shared/staging/production datastore (none in scope). Isolation mode: one disposable data dir + one ledger process/port/file per case under scratchpad/data10run; proves exclusivity only, not tenant RLS (not in the delta). No CI, no GPU/full-space search (10^9 extrapolated). Separator spellings beyond the corpus (other punctuation, tabs+dash mixes) only partially probed; campaign names and free-text fields other than the three named; concurrency of start-up read vs writes (AEGIS-RELIABILITY); Windows/Linux filesystems; bitwise build provenance of the ledger binary.
- Candidate suite on my archive (context only, tests passing ≠ property proven): see out/pytest-0f5fbe3-3.1x.txt.

- Suite result: 924 passed on 3.12 and 924 passed on 3.13 (archive incl. `docs/` from the same SHA; a first run without `docs/` failed test_env_documented on the missing ADR: my archive's scope, not the candidate).

## Verdict
blocking: true (D10-H1 HIGH). D9-H1's spellings closed (0 leaks); D9-M1 closed for detected spellings, open for the divergent set (D10-M1 MEDIUM); H-1 unchanged and the start-up read refuses symlink/FIFO/directory/socket with nothing written; request_sha replays and 409s verified, earlier non-PII logs replay unchanged; names_a_card equals 99569bd; M-2, M-3 open MEDIUM unchanged.

VERDICT: BLOCK
