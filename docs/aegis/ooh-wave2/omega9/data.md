# AEGIS-DATA (Gate 5) — omega9, run 2
- repo: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: 91ea9a1948253a3203150b38a8c2f2f839f232cd
- treeOid: 482eb8983853a8bdff118c16446474b0b16537da
- scope: persistence/data integrity of `git diff 75b0662 91ea9a1 -- services` (ooh-py: pii.py, service.py, store.py, svc_campaigns.py + 3 tests)
- author: AEGIS-DATA (AG-AEGIS-DATA v1.0.0), fresh context, read-only on candidate; Claude Opus 5.5 subagent; 2026-10-18
- method: own `git archive` copies under scratchpad/data9run2 (not the shared review copy); real ledger-rust binary built by me from the candidate's services/ledger-rust into a private target dir; disposable data dirs only; no shared/real datastore. The cut-off run 1 (omega9/data-run1) was read for script ideas only; no result here is taken from it.
- raw/PoCs: scratchpad/aegis/omega9/data/{poc,out}

## Environment
- ledger: `services/ledger-rust` at 91ea9a1 (identical at 75b0662: `git diff --quiet` rc 0), built by me `cargo build --release --offline` (SDKROOT MacOSX26.5; the default 27.0 SDK fails to link) → sha256 74efb78e4f0af779418b98852acf92008b7be7443a68a4db60cedbdcfcf9c8f1. One ledger process per case, own port, own file, killed at case end.
- service: in-process (helpers.Harness + real HttpLedgerClient over HTTP), `git archive` trees t-91ea9a1 / t-75b0662 / t-99569bd; Python 3.12.14 and 3.13.9.

## 1. H-1 — log file vs memory, on the real ledger (poc/rl_omega9.py, h1_summary.py → out/h1-3.1x.{json,summary.txt})
66 cases per Python; outcomes identical on 3.12 and 3.13 (only answer bodies of one accepted case differ: ids/timestamps). Timings: `before` (changed before the request), `at_append` (changed after the real ledger holds the anchor), `check_first` (forced check + /audit/integrity before the request).

| change | before | at_append | check_first | recovery (file put back) |
|---|---|---|---|---|
| extra tail, 5 bytes short, 1 line short | 503 STORE_UNAVAILABLE maybe:true, file byte-identical | same | check ok:false "file differs"; audit ok:false; 503 INTEGRITY_UNVERIFIED, 0 anchors | before/at_append: anchored line rolled forward +1 line, applied 1, anchors==lines (15/15), restart ok, state diff [] |
| same-size in-place: line 1, last line, first/last/middle byte | 503, file identical | 503, file identical | ok:false, 503, 0 anchors | rolled forward once, 15/15 |
| deleted | 503; the open re-creates a 0-byte file (documented) | same | ok:false, 503 | rolled forward once, 15/15 |
| replaced by rename (other bytes) | 503, new inode untouched | same | ok:false | rolled forward |
| replaced by rename (identical bytes) | 201 (file == memory: correct) | 201 | ok:true (correct), 201 | n/a, 14/14, disk==memory |
| symlink (identical target / elsewhere) | 503 (O_NOFOLLOW), link + target untouched | same | ok:false | rolled forward |
| FIFO, directory | 503, no hang (20 s alarm never fired) | same | ok:false | rolled forward |
| same-size tail / adopt with edited prefix (at append) | — | 503, file identical | — | rolled forward |
| exact copy of the new line already on disk | — | adopted, 1 copy, disk==memory (tenant 201, submit 200) | — | n/a |
| submit (money line) at_append: in-place, middle byte, deleted, replaced, symlink, fifo, adopt-bad-prefix, extra tail | — | 503, nothing applied | — | rolled forward once, 43/43, money evidence 0 problems |

- Totals both Pythons: any forced check / audit reporting ok while the file differed: **0**; path written over by the service: **0** (deleted: an empty file created where none was); duplicate ledger event ids **0**; max anchors per seq **1**; ledger_readback/money_evidence problems 0 in every restore case.
- **DATA M-1 (4a0a4b4/75b0662): CLOSED at 91ea9a1.** Control run on t-75b0662 (out/h1-at-75b0662-3.12.json): `inplace_line1|at_append|tenant` → 201 over the edited line 1, then restart refused "log line 1 does not match its own hash"; at 91ea9a1 the same case → 503, file untouched, rolled forward after restore.
- Reporting latency (poc/h1_stale_report.py → out/h1-stale-report-3.1x.json, real clock, no reset): after a same-size edit with no write, `/health` "ok", `/status` and `/audit/integrity` (within FORCED_MIN_S=10 s of the last forced check) ok:true; after 10.5 s `/audit/integrity` ok:false "file differs", health degraded, the write 503, file untouched. Documented in store.py's module docstring. → L-A.
- Restart on the changed file (`|restart` cases): extra tail, in-place, replaced, symlink, fifo, directory → start refused/degraded, pending kept, after restore rolled forward 15/15. **deleted / one line shorter → the anchored line is lost (anchors 14 vs lines 13, 503 for good)** = M-3 below, not H-1 write path.

## 1b. M-3 and M-2 re-check
- M-3 (poc/m3_stale_delete.py → out/m3-{91ea9a1,75b0662}-3.1x.json, real RLIMIT_FSIZE short write): results **identical at 91ea9a1 and 75b0662** on both Pythons. shorten_then_restore: pending.line gone after restart 1, after restore "anchors beyond the local log", inflight not applied, 503, 14/13. delete_then_restore: the same (pending deleted after "another ooh log"). keep_copy / delete_keep_copy (operator kept pending.*): rolled forward, 201, 15/15. control_no_pending: 201, 14/14. (control_no_shorten's restart 2 writes back a pre-roll-forward log, a script-made rollback: not evidence.) The code path service.py `_settle_pending` (stale → write_discarded/clear_pending, then clear_discarded on "stale") is untouched by the delta (service.py diff = 2 lines in verify_integrity). **M-3 UNCHANGED, open MEDIUM**; the delete variant is the same mechanism.
- M-2 (poc/targeted.py, FakeLedger in-process → out/targeted-{91ea9a1,75b0662}-3.1x.json): `B_consistent_rewrite_inflated_funds` **identical at both SHAs**: start STARTED, health degraded, write 503, hub read 200 with client_funds_received **99999.00** (true 6000.00), no marker on the read. Only line differing between SHAs: B_runtime_same_size_disk_edit now names the file problem (file_problem() exists only at 91ea9a1); its "write 200" is a kill-switch *engage* answered `recorded:false` (volatile, not written; disk byte-identical). **M-2 UNCHANGED, open MEDIUM.**

## 2. OOH2-PII-1 — persisted bytes, real ledger (poc/pii_persist2.py, pii_summary2.py → out/pii-91ea9a1-3.1x.{json,summary.txt}, out/pii-keyword-91ea9a1-3.1x.*)
39 secret spellings × 5 paths (unit vendor_text; face text attribute `location_description`; client_approval_doc_ref; vendor_text left in pending.line by a failed append; vendor_text in pending.discarded after a kill between pending write and anchor) = 195 cases per Python; identical on 3.12/3.13. Scanned: every file of the data dir and the ledger dir (ledger JSONL incl. summaries/payload hashes), /audit/export (paged by `since`), /audit/evidence, campaign/unit/face views; clear, NFKC, JSON-escaped, and Unicode-digit-folded forms.

| class | spellings | vendor_text | face attribute | approval doc ref | pending.line / .discarded |
|---|---|---|---|---|---|
| ASCII cards (spaces, -, ., _, /, en-dash, full-width, ZW, NBSP, 15-digit Amex, 19-digit) + one unbroken 16-digit Extended-Arabic-Indic card | 16 | redacted, 0 leaks | 422 PERSONAL_DATA, nothing written | 422 (detector or schema), nothing written | 0 leaks |
| SSN with dashes (3 dash kinds) / keyword+spaces/run, tax id/ITIN dashed, Arabic-Indic dashed, SSN+Arabic-Indic run | 10 | redacted, 0 leaks | 422 | 422 | 0 leaks |
| **cards in Arabic-Indic / Devanagari digits with separators** (٤١١١ ١١١١…, ४१११ १११…, ५५५५-…-४४४४) | 3 | **stored in clear** | **200, stored** | 422 (schema [A-Za-z0-9._:-] only) | **clear** |
| **SSN 123.45.6789, 123_45_6789, 123/45/6789, १२३.४५.६७८९** | 4 | **clear** | **200, stored** | **123.45.6789, 123_45_6789: 200, stored** | **clear** |
| **SSN with keyword and dots/underscores** (SSN 123.45.6789, SSN:123.45.6789, tax id 123_45_6789, ITIN:912.70.1234) | 4 | **clear** | **200, stored** | **SSN:123.45.6789, ITIN:912.70.1234: 200, stored** | **clear** |
| documented non-detection (123 45 6789, 123456789, no keyword) | 2 | clear | 200 | 123456789: 200 | clear |

- Leaks land in: ooh_log.jsonl, /audit/export, unit/face/campaign views, pending.line, pending.discarded. **Ledger file: 0 leaks** in all 195 cases (the ledger holds hashes/ids only).
- Refused requests: 60 per Python, every one with log length, ledger event count and every file's sha256 unchanged (nothing written), 0 leaks; the control write after each refusal 200.
- In-process detector probe (poc/pii_detector_probe.py → out/pii-detector-probe-91ea9a1.json) agrees: `has_personal_data` misses 123.45.6789/_/:/ and keyword+dot forms ("SSN 123.45.6789", "tax id 123_45_6789"), spaced Arabic-Indic/Devanagari cards; detects unbroken Arabic-Indic 16-digit runs (phone rule) and dashed Arabic-Indic SSNs. Cause: `_DIGIT_RUN`/`_GROUP` use `[0-9]` (ASCII) and NFKC does not fold Arabic-Indic/Devanagari digits; `_SSN` and `_SSN_VALUE` accept only dashes/spaces between groups (pii.py:93-94 `_DIGIT_RUN`/`_GROUP`, pii.py:148-151 `_DASH`/`_SSN`/`_SSN_VALUE` at 91ea9a1).
- Replay/idempotency of the redacted unit write: 39 cases × 3 replays = 117 replays → 0 duplicate log lines, 0 duplicate ledger events, units == 1 each; same request_id + other body → 409.
- Recoverability of a *redacted* value (poc/request_sha_recover2.py → out/request-sha-recover-{ssn,card}-3.12.json): from ooh_log.jsonl alone (request key, face_id, flight, redacted template, unkeyed `request_sha` = sha256 of the raw body) the SSN 123-45-6789 was recovered in 1.14 s with the area digits known (456,790 tries, ~400k/s one core; full 10^9 space ≈ 42 min one core, extrapolated, not run) and the card 4111 1111 1111 1111 in 0.13 s with BIN+last4 known. Pre-existing (request_sha in service.py at 75b0662).
- Funds reference (names_a_card) — see §2b.

## 2b. Funds reference (names_a_card) 99569bd vs 91ea9a1
- Unit level (poc/funds_corpus.py seed 20261018 + 66 added non-ASCII-separator cases, poc/funds_decide.py → out/funds-names_a_card-diff.json): corpus 77,913 refs; result-row sha256 equal across 3.12/3.13 at each SHA; refused as card: 99569bd **9115**, 91ea9a1 **9109**; **6 rows differ, all with U+0131 (dotless ı) as the group separator** ("4111ı1111ı1111ı1111", "ACH-…", 5555…/6011… likewise): refused at 99569bd, accepted by `names_a_card` at 91ea9a1. Cause: pii.py `_DIGIT_RUN = re.compile(r"[0-9]+(?:[^0-9a-z]+[0-9]+)*", re.I)` — under re.I the negated class no longer matches ı (also İ, ſ, K, which NFKC/casefold already remove); the old inline regex in svc_campaigns had no re.I.
- Service level (poc/funds_service.py, 38 refs incl. the ı refs, FakeLedger, real data dir → out/funds-service-{99569bd,91ea9a1}-3.1x.json): **byte-identical at both SHAs** on both Pythons. The ı refs answer 422 schema validation (Ref = `^[A-Za-z0-9._:-]{1,100}$`), 0 lines, 0 ledger events: the unit-level difference is **unreachable through the API** → INFO-1.
- Same set on funds cases through the service: VERIFIED (38/38 identical). Same set at unit level: MISMATCH on 6/77,913 (unreachable).

## 3. Restart / replay across versions, real ledger (poc/cross_version.py → out/xv-{A}-to-{B}-3.1x.json)
| writer → reader | writer lines / fmt | start | integrity | state diff | replay of writer's approval request | new write | 2nd restart | read-back anchors==lines |
|---|---|---|---|---|---|---|---|---|
| 91ea9a1 → 91ea9a1 (3.12, 3.13) | 77 / [4] (card SSN refused: face 422, approval 422) | ok | ok | [] | 200, 0 lines | 201 | ok | 78/78, 0 problems |
| 75b0662 → 91ea9a1 (3.12, 3.13) | 78 / [4] (card+SSN accepted at 75b0662) | ok | ok | [] | **422 ATTRIBUTE_VALUE_PERSONAL_DATA, 0 lines** (INFO-2) | 201 | ok | 79/79, 0 problems |
| 75b0662 → 75b0662 control | 78 / [4] | ok | ok | [] | 200, 0 lines | 201 | ok | 79/79 |
- A format-4 log written at 75b0662 starts and replays at 91ea9a1; the 75b0662 card/SSN bytes stay in the log (INFO-3).
- Money evidence (poc/money_evidence.py over all 138 real-ledger logs of this run: 132 H-1 + 6 cross-version → out/money-evidence.txt): 114 money lines, **0 problems**.
- Candidate suite at 91ea9a1 (my archive): 870 passed on 3.12 and on 3.13 (out/pytest-3.1x.txt). Tests passing ≠ property proven; listed for context only.

## Checks
| id | area | result | method |
|---|---|---|---|
| C1 | H-1 write path: changed file never written over | VERIFIED | rl_omega9, 66×2 cases, real ledger |
| C2 | H-1 never reported ok while changed (forced checks/audit) | VERIFIED; cached reads lag (L-A) | rl_omega9 + h1_stale_report |
| C3 | H-1 recovery: rolled forward once, anchors==lines, no dup | VERIFIED (restore path); restart-on-deleted/shortened loses the line (M-3) | rl_omega9, m3_stale_delete |
| C4 | DATA M-1 | CLOSED | same case 201 at 75b0662, 503 at 91ea9a1 |
| C5 | M-3 | UNCHANGED (open) | m3 identical at both SHAs |
| C6 | M-2 | UNCHANGED (open) | targeted B_consistent_rewrite identical |
| C7 | OOH2-PII-1: no card/SSN in persisted bytes | **MISMATCH** (D9-H1) | pii_persist2, 195×2 + keyword 20×2 |
| C8 | refused request writes nothing | VERIFIED (60/60 per Python) | file sha256 + log length + ledger count |
| C9 | replay idempotency of redacted unit write | VERIFIED: 117 replays, 0 duplicate effects | pii_persist2 |
| C10 | names_a_card same set as 99569bd on funds cases | VERIFIED via service (38/38); unit-level 6/77,913 differ, unreachable (INFO-1) | funds_service, funds_decide |
| C11 | restart/replay 91ea9a1 log and 75b0662 log | VERIFIED | cross_version |
| C12 | redacted value not recoverable from persisted bytes | **MISMATCH** (D9-M1, pre-existing) | request_sha_recover2 |

convergence: n/a (no schema migration in the delta; log format 4 at both SHAs, verified by start + diff [] in C11). idempotency: [{path: unit inventory add (redacted vendor_text), replays: 117, duplicateEffects: 0}, {path: client-approval replay 91ea9a1→91ea9a1, replays: 1×2, duplicateEffects: 0}, {path: client-approval replay 75b0662→91ea9a1, replays: 1×2, duplicateEffects: 0 (answer 422)}].

## Findings
- **D9-H1 HIGH (BLOCKING) — OOH2-PII-1 residual: card numbers and SSNs persisted in clear (NOT closed).** SSNs with `.`, `_`, `/` (and `:` in the detector probe) separators — with or without "SSN"/"tax id"/"ITIN" — and card numbers in Arabic-Indic or Devanagari digits with separators reach ooh_log.jsonl, pending.line, pending.discarded, /audit/export and the unit/face/campaign views. Face text attribute answers **200** and stores them; client_approval_doc_ref answers **200** for `123.45.6789`, `123_45_6789`, `SSN:123.45.6789`, `ITIN:912.70.1234` (the only SSN shapes the Ref pattern `^[A-Za-z0-9._:-]` admits besides the dashed one, which is refused). vendor_text answers 201 with `personal_data_redacted:false`. The log is append-only and anchored: the line cannot be removed without the integrity failure measured in §1. Ledger file: 0 leaks. Code: pii.py:93-94, 148-151; svc_campaigns.py:864.
  - Reproducer: `SECRETS_ONLY="123.45.6789|SSN:123.45.6789|tax id 123_45_6789|٤١١١ ١١١١ ١١١١ ١١١١" python poc/pii_persist2.py <ledger-bin> <fresh-dir> <tree-91ea9a1>` then `python -I poc/pii_summary2.py out.json` → leaks listed per file/view. In-process: `python -I poc/pii_detector_probe.py <tree>`.
  - Falsifier: the same command gives vendor_text `personal_data_redacted:true` with 0 leaks, and face/approval 422 with log length, ledger count and file hashes unchanged, for every spelling in the table's bold rows.
- **D9-M1 MEDIUM (pre-existing) — a redacted value is recoverable from the log.** The log line keeps `request_sha` = unkeyed sha256 of the raw request body (service.py:67-68, 269) next to the redacted template and every other body field. Measured: SSN recovered in 1.14 s (area digits known), card in 0.13 s (BIN+last4 known), from ooh_log.jsonl alone. Present at 75b0662. Reproducer: `python -I poc/request_sha_recover2.py <unit-dir>/ooh/ooh_log.jsonl ssn_area3 123`. Falsifier: request_sha keyed with a secret held outside the data dir, or computed over the redacted body; the script then finds nothing in 10^6 candidates.
- **M-3 MEDIUM (unchanged, open)** — an anchored pending line is deleted at start when the log is one line shorter **or deleted**; after the file is put back, "anchors beyond the local log", the line is lost, writes 503 for good. Reproducer: `poc/m3_stale_delete.py` shorten_then_restore / delete_then_restore; rl_omega9 `deleted|at_append|*|restart`, `shorter_line|at_append|submit|restart`. Falsifier: after the restore the line is applied, anchors == lines.
- **M-2 MEDIUM (unchanged, open)** — after a consistent rewrite reads serve the rewritten figures (hub 99999.00) with no integrity marker while writes are 503. Reproducer: `poc/targeted.py <tree> <dir>` key B_consistent_rewrite_inflated_funds. Falsifier: hub read 503 or flagged while integrity.ok is false.
- **L-A LOW (documented)** — after a file change with no write, `/health` "ok", `/status` ok:true, and `/audit/integrity` ok:true within 10 s of the last forced check; writes refuse meanwhile and nothing is written. Reproducer: `poc/h1_stale_report.py`. Falsifier: those reads report the file problem immediately.
- INFO-1 — names_a_card no longer reads U+0131 as a separator (re.I on `_DIGIT_RUN`), 6/77,913 corpus rows; unreachable through the API (Ref schema 422). Falsifier: any ASCII-only ref differs between SHAs.
- INFO-2 — a client-approval request accepted at 75b0662 with a card ref, replayed at 91ea9a1, answers 422 instead of the recorded 200 (the PII check runs before `_idem`, svc_campaigns.py:864-867); 0 lines added, no double effect.
- INFO-3 — card/SSN bytes written before 91ea9a1 remain in the log after upgrade (xv 75b0662→91ea9a1: card_in_log true, ssn_in_log true); there is no scrub path for an anchored append-only log.
- **DATA M-1 (4a0a4b4/75b0662): CLOSED** at 91ea9a1.

## NOT RUN
- No shared/staging/production datastore touched (none exists in scope). No CI. No GPU/full-space brute force (10^9 SSN space only extrapolated). Isolation mode: one disposable data dir + one ledger process/port/file per case under scratchpad/data9run2; this proves exclusivity only, not tenant RLS (out of scope of the delta).
- Concurrency of file_problem vs writes (AEGIS-RELIABILITY's), multi-line truncation for M-3, in-place edits restarted on money lines beyond the cases listed, campaign names/other free-text fields not named in the brief, Windows/Linux filesystems, bitwise build provenance of the ledger binary.

## Verdict
blocking: true (D9-H1 HIGH). M-1 closed; M-2, M-3 open MEDIUM unchanged; D9-M1 MEDIUM new to DATA (pre-existing code).

VERDICT: BLOCK
