# AEGIS-DATA (Gate 5) — omega11
- repo: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: f5ee168c9e9148241b709ec93acc660c88d08ba7
- treeOid: da5fc085a0669ba5f92df726cfefcb28bd505cab
- scope: persistence/data integrity of `git diff 0f5fbe3 f5ee168 -- services` (ooh-py: pii.py, service.py, store.py, svc_campaigns.py, svc_inventory.py + 6 tests); OOH2-PII-1 founder design change (digit-cluster rule) for unit vendor_text (redact), face text attribute and client_approval_doc_ref (refuse)
- author: AEGIS-DATA (AG-AEGIS-DATA v1.0.0), fresh context, read-only on candidate; Claude Opus 5.5 subagent; 2026-10-18
- method: own `git archive` trees + disposable data dirs + own ledger-rust build under scratchpad/data11run (outside omega11); no shared/real datastore. omega10 PoCs reused as a starting point; every result below is from this run.
- raw/PoCs: scratchpad/aegis/omega11/data/{poc,out}

## Environment
- ledger: `services/ledger-rust` identical at 0f5fbe3 and f5ee168 (`git diff --quiet` rc 0); built by me from my t-f5ee168 archive, `cargo build --release --offline`, SDKROOT=MacOSX26.5.sdk, private CARGO_TARGET_DIR under data11run → `server` sha256 98befd4aeb4fc2de42462f3a35418ecd793b1549e9a1b04899eef85a57fd2b76 (path-dependent build; not a provenance claim). One ledger process/port/file per case.
- service: in-process (helpers.Harness + real HttpLedgerClient over HTTP) from my `git archive` trees t-f5ee168 (== frozen review copy, `diff -r -x __pycache__` empty) / t-0f5fbe3 / t-91ea9a1 / t-75b0662 / t-99569bd; Python 3.12.14 (unidata 15.0.0) and 3.13 venvs.

## 3c. Funds: names_a_card 99569bd vs f5ee168 (poc/funds_corpus.py, funds_decide.py, funds_service.py → out/funds-*)
| level | population | 99569bd | f5ee168 | rows differing |
|---|---|---|---|---|
| unit (`names_a_card`, `funds_ref_parts`, `funds_ref_key`) | 78,062 refs (corpus from both trees' test strings, seed 20261018, ∪ omega10's 77,980) | sha256 a6198d5d…6bb71510, refused-as-card 9,127 | same sha256, 9,127 | **0** (3.12 and 3.13 identical) |
| service (38 refs, real data dir) | status, detail, lines, events, ref_in_log | 0e2e7d2d…a33d24a77 | same | 0 |
→ C10 VERIFIED: names_a_card set unchanged (funds path untouched by the cluster rule).

## 2. request_sha: idempotent retries and logs written earlier (poc/xv_idem11.py → out/xi-{A}-to-{B}-3.1x.json; poc/idem_collapse11.py → out/idem-collapse-*.json)
Writer A: tenant, 8 units (plain; card `4111 1111 1111 1111`; `123.45.6789`; and 5 non-PII texts new in this run: `faces  west,\nlit 24h ` (double space, newline, trailing space), `ﬁrst ﬂoor, Ｎorth side` (NFKC ligatures/full-width), `flight 2026-11-01 - 2026-11-30`, `ref io-2026-1018-0001, 14x48`, `posted 11/01/2026`), face attribute, campaign → authorized → submitted PO: 75 recorded 2xx requests. Reader B on the same data dir + ledger file: start, replay all 75, 10 same-request_id/other-body retries, one new write, restart, ledger readback. Real ledger; 3.12 == 3.13 for every pair.
| A → B | start / integrity / state diff | A's vs B's request_sha(body) | replays of the 75 | other-body retries | lines added by replays+retries | new write, restart, anchors==lines |
|---|---|---|---|---|---|---|
| f5ee168 → f5ee168 | ok / ok / [] | 75/75 equal | 75 original status (46×201, 29×200) | 10/10 409 | 0 | 201, ok, 76/76, 0 problems |
| 0f5fbe3 → 0f5fbe3 (control) | ok / ok / [] | 75/75 | 75 original | 10/10 409 | 0 | 76/76 |
| 0f5fbe3 → f5ee168 | ok / ok / [] | 71/75: the 4 non-PII texts whitespace / NFKC / date range / io-ref | 71 original; **4 → 409 REQUEST_ID_REUSED** | 10/10 409 | 0 | 201, ok, 76/76, 0 problems |
| 91ea9a1 → f5ee168 | ok / ok / [] | 69/75: those 4 + card + `123.45.6789` | 69 original; **6 → 409** | 10/10 409 | 0 | 76/76, 0 problems |
| 75b0662 → f5ee168 | ok / ok / [] | 69/75 same 6 | 69 original; **6 → 409** | 10/10 409 | 0 | 76/76, 0 problems |
- Logs written at 0f5fbe3, 91ea9a1, 75b0662 **start** (state diff [], integrity ok) and **replay** at f5ee168, 0 duplicate lines/events. (The "line by request_id" counter in the raw output differs 24/75 at every pair incl. same-version controls: PoC artefact of derived request ids, not a result.)
- **What changes for retries of old requests**: request_sha now hashes `screen_free_text(vendor_text)[0]` always (service.py:73-78), i.e. NFKC + whitespace-collapsed + cluster-redacted text, not the raw text. A retry crossing the upgrade answers 409 REQUEST_ID_REUSED (nothing written) instead of its first answer whenever the old vendor_text (a) held a card/SSN (as INFO-4 before), or now also (b) is non-PII but not already in screen form (double spaces, newline, leading/trailing space, NFKC-changing characters) or (c) holds a ≥9-digit cluster (date range, long ref). Plain vendor texts in screen form (`faces west, lit 24h`, `posted 11/01/2026`) and every non-vendor_text body hash as before. The docstring "a body without personal data hashes as before (logs written earlier still replay)" (service.py:89-90) is false for (b) and (c) → D11-L1.
- Same version, same request_id, other vendor_text (f5ee168; 0f5fbe3 for comparison), 14 pairs, 3.12 == 3.13, 0 lines added by any retry, units == 1 each:
| first → retry | f5ee168 | 0f5fbe3 |
|---|---|---|
| card → other card; SSN → other SSN; SSN → card (differ only inside a redacted cluster) | 201 replay (same unit) — by design | 201 ×3 |
| `…6789 for access` → trailing space; `faces west` → `faces west ` | 201 replay | 409 |
| `123 45 6789` → `123  45  6789`; `faces  west\nlit` → `faces west lit`; `ﬁrst floor` → `first floor` | 201 replay | 409 |
| `flight 2026-11-01 - 2026-11-30` → `2026-12-01 - 2026-12-31`; `io-2026-1018-0001` → `…0002` (false-positive clusters) | **201 replay** (second body's values never stored; stored text `[redacted]` either way) | 409 |
| `faces west` → `faces west, lit`; price/impressions changed (<9-digit groups); SSN → SSN + e-mail | 409 | 409 |
→ C9 VERIFIED: same body replays (75×10 pair-runs, 0 duplicates), different kept text 409, difference only inside a redacted cluster (incl. false-positive clusters) or only in whitespace/NFKC form → replay, by design ("the request hash uses the redacted text only").

## 4. False positives: realistic OOH text corpus (poc/fp_corpus11.py → out/fp-corpus-{f5ee168,0f5fbe3}-3.12.json)
70 texts in 11 classes (incl. 3 true phone positives for comparison) through the service (helpers.Harness, in-process ledger, real data dir) in the three fields: unit vendor_text, face text attribute `location_description`, client_approval_doc_ref (the text with spaces → `-`; a fresh proposal after each accepted ref). Count = redacted / refused at f5ee168 (0f5fbe3 in brackets). Approval refs: 39/70 already fail the Ref schema `^[A-Za-z0-9._:-]{1,100}$` (422, no PII decision), 9 accepted.
| class | n | vendor_text redacted | face attribute refused 422 PERSONAL_DATA | approval ref refused PERSONAL_DATA |
|---|---|---|---|---|
| address | 6 | 1 (0) | 1 (0) | 0 (0) |
| ZIP+4 | 5 | 5 (0) | 5 (0) | 4 (0) |
| flight/date | 11 | 6 (0) | 6 (0) | 3 (0) |
| GPS | 6 | 4 (0) | 4 (0) | 2 (0) |
| price | 7 | 3 (0) | 3 (0) | 0 (0) |
| impressions | 5 | 2 (0) | 2 (0) | 0 (0) |
| permit | 6 | 6 (0) | 6 (0) | 5 (0) |
| ref / IO / PO | 12 | 7 (2) | 7 (2) | 6 (2) |
| spec / time | 6 | 0 | 0 | 0 |
| phone (true positive) | 3 | 3 (3) | 3 (3) | 2 (2) |
| mixed realistic lines | 3 | 3 (0) | 3 (0) | 0 (0) |
| **total** | 70 | **40 (5)** | **40 (5)** | **22 (4)** |
- New false positives (35 per free-text field; 18 for refs) are exactly the texts with ≥9 digits not split by a letter: ZIP+4 (`CA 91607-3413` → `CA [redacted]`; `zip 902101234`), any ISO date range with ` - ` or `/` or comma (`flight 2026-11-01 - 2026-11-30` → `flight [redacted]`; `flight 2026-11-01 to 2026-11-30` is kept), decimal GPS pairs (`34.0522, -118.2437`; `lat 34.052235 lon -[redacted]`: one coordinate alone with 9 digits), totals ≥ 9 digits incl. separators (`$1,250,000.00` → `$[redacted]`; `CPM $3.25, 1,250,000 impressions` → `CPM $[redacted] impressions`; `12,500.00 / 13,750.00 / 15,000.00`), impression lists, every permit/IO/invoice number with ≥9 digits (`io-2026-1018-0001` → `io-[redacted]`, `permit LA-2026-001234`, `Permit No. 123456789`). Kept: street addresses with 5-digit ZIP, `Nov 1 2026 - Nov 30 2026`, `11/01/26`, DMS GPS (`34°03'08.0"N …` letters split it), single prices, `14x48`, times, `Geopath 31234567` (8 digits).
- Labels: every new redaction is labelled `number` (not card/ssn); 10-digit refs keep the pre-existing `phone` label.
- Effect on data (measured): a redacted vendor_text keeps no copy of the original anywhere (C7); a refused face attribute / approval ref writes nothing (C8); a retry with a different false-positive value replays the first answer (§2). Accepted trade-off per OOH2-PII-1-FP; reported, not blocking. Recommend operators type flights as `2026-11-01 to 2026-11-30` (kept), not ` - `.

## 3b. Start-up read of the log (poc/start_read.py → out/start-read-f5ee168-3.1x.json)
| case | start (3.12 = 3.13) | data dir changed | ledger events added | after the regular file is put back |
|---|---|---|---|---|
| control (regular file) | STARTED, write 201 | log grew (the write) | (the write) | n/a |
| symlink → identical copy / dangling | REFUSED StoreCorrupt "cannot be opened (OSError) … not a link"; copy untouched, no target created | none | 0 | started, integrity ok, state diff [] |
| FIFO / directory | REFUSED "not a regular file", no hang | none | 0 | same |
| unix socket | REFUSED "cannot be opened (OSError) … inspect the data directory" (store.py:146: "not a link" now only for ELOOP — omega10 wording note closed) | none | 0 | same |
| device node | **NOT RUN** (mknod EPERM, non-root) | — | — | — |
→ C13 VERIFIED, refusals unchanged (6/7; device NOT RUN).

## 5. M-3, M-2, L-A (poc/m3_stale_delete.py, targeted.py, h1_stale_report.py → out/m3-*, targeted-*, h1-stale-report-*)
- **M-3: UNCHANGED, open MEDIUM.** Output byte-identical f5ee168 = 0f5fbe3 = omega10's 0f5fbe3 run, both Pythons (sha256 8bcb404bdc662a41… ×6): shorten_then_restore → pending.line deleted at restart 1 ("the ledger holds anchors beyond the local log"), inflight not applied, write 503, anchors/lines 14/13.
- **M-2: UNCHANGED, open MEDIUM.** targeted output json-equal f5ee168 vs 0f5fbe3 on both Pythons: B_consistent_rewrite_inflated_funds → STARTED, health degraded, write 503, **hub read 200 hub_funds_received 99999.00**, no marker.
- L-A (LOW, documented): unchanged — after a file edit with no write, cached `/health` ok and `/status` + `/audit/integrity` ok within 10 s, then ok:false, write 503.

## 3. H-1 re-run on the real ledger (poc/rl_omega9.py unchanged, h1_summary.py → out/h1-f5ee168-3.1x.{json,summary.txt})
- 66 cases per Python (same matrix as omega9/omega10). Per-case summary **byte-identical to omega10's 0f5fbe3 run** (re-summarised by me from omega10's JSON with the same h1_summary.py) on 3.12 and on 3.13; 3.12 == 3.13. okWhileChanged=True **0/66**, duplicate event ids 0. The 20 summary "FAIL" flags are the same known 20 (15 check_first|restore: write refused before any anchor, nothing to roll forward; 3 deleted: the write-path open re-creates a 0-byte file — documented; restart-on-deleted/shortened = M-3).
- (A first run lacked my copy of money_evidence.py, so 3 rows had an empty money_evidence field; re-run with it, the result above; first run kept outside omega11.)
→ C1, C2, C3 VERIFIED unchanged.

## 1. OOH2-PII-1 — persisted bytes, real ledger (poc/pii_persist11.py + secrets11.json, pii_summary11.py → out/pii-f5ee168-3.1x.{json,summary.txt})
121 distinct spellings × 5 paths (unit vendor_text; face text attribute `location_description`; client_approval_doc_ref; vendor_text in pending.line after a failed append; in pending.discarded after a kill between pending write and anchor) = **605 cases per Python, 0 errors, summaries identical on 3.12 and 3.13**. Scanned per case: every file of the data dir and ledger dir, /audit/export (paged), /audit/evidence, campaign/unit/face views; clear, NFKC, JSON-escaped, Unicode-digit-folded (decimal + digit values), mark/format/private-use-stripped forms, and the secret's digit string inside any letter-free run.
| class | spellings | vendor_text (201) | face attribute | approval ref | pending.line / .discarded | leaks |
|---|---|---|---|---|---|---|
| **D10-H1** (`123 - 45 - 6789`, `123 – 45 – 6789`, `SSN: 123 - 45 - 6789`, `123--45--6789`, `123..45..6789`, `123 . 45 . 6789`, `123,45,6789`, `SSN 123,45,6789`, `123\|45\|6789`, `123·45·6789`, `123+45+6789`, `SSN:123--45--6789`, `1̃23-45-6789`, `4̃111 1111 1111 1111`) | 14 | 14/14 redacted | 14/14 422 PERSONAL_DATA, nothing written | 14/14 422 (3 PERSONAL_DATA, 11 schema), nothing written | files present 14+14, 0 leaks | **0** |
| **D10-M1** divergent (`123  45  6789`, U+1680 / U+E000 / U+0378 separators) | 4 | 4/4 redacted | 4/4 422 PERSONAL_DATA (was 200) | 4/4 422 schema | 0 leaks | **0** |
| **D9-H1** (7) + D9-H1 keyword forms (6) | 13 | 13/13 | 13/13 422 PD | 13/13 422 | 0 leaks | **0** |
| earlier omega9/10 spellings | 29 | 29/29 | 29/29 422 PD | 29/29 422 | 0 leaks | **0** |
| `123456789` (documented non-detection before) | 1 | redacted | 422 PD | 422 PD | 0 leaks | **0** |
| new: 9+ digit clusters (`123 456 789`, `1234 56789`, `(123) 45-6789`, `123*45*6789`, `123%2045%206789`, …) | 12 | 12/12 | 12/12 | 12/12 | 0 | **0** |
| new: wide/multi separators, `\n`, `\t`, `\r\n` | 8 | 8/8 | 8/8 | 8/8 | 0 | **0** |
| new: NBSP, figure space, ideographic space, NNBSP | 4 | 4/4 | 4/4 | 4/4 | 0 | **0** |
| new: full-width, superscript, math-bold, double-circled `⓵…`, keycap `1⃣…`, Thai digits | 6 | 6/6 | 6/6 | 6/6 | 0 | **0** |
| new: hidden chars (ZWSP, WJ, VS16, Hangul filler U+3164, CGJ, VS-17, combining mark after a separator) | 7 | 7/7 | 7/7 | 7/7 | 0 | **0** |
| new: SSN/card shapes with punctuation-like *letters* (ー U+30FC, ـ U+0640, ꓸ U+A4F8, ǀ U+01C0, ㅡ U+3161) | 9 | 9/9 (legacy SSN/card rules) | 9/9 | 9/9 | 0 | **0** |
| new: Diners 14, 16-digit, letters touching (`card4111111111111111exp`) | 4 | 4/4 | 4/4 | 4/4 | 0 | **0** |
| letter-separated, **design: not detected** (`123ー456ー789`, `123ꓸ456ꓸ789`, `123ǀ456ǀ789`, `123&nbsp;45&nbsp;6789`, `SSN 123&nbsp;45&nbsp;6789`, `4111&nbsp;1111&nbsp;1111&nbsp;1111`, `4111<br>1111<br>1111<br>1111`, `123a45a6789`, `123ⓐ45ⓐ6789`→`123a45a6789`, `123™45™6789`→`123TM45TM6789`) | 10 | stored clear, redacted:false | 200, stored | 9 schema 422, `123a45a6789` 200 | clear | 41/50 cases (D11-L2) |
- Totals (non-letter classes, 111 spellings, 555 cases): **0 leaks** in log, pending.line, pending.discarded, ledger file, /audit/export, /audit/evidence, views. Refused requests: **231/231 nothing written** (111 face + 120 approval incl. the letter class; log length, ledger event count and every file sha256 unchanged), control write after each.
- Labels (`personal_data_kinds` on the stored unit line): ssn 63, card 33, number 15 — the card/SSN rules now label; detection is their union with the cluster rule (pii.py:269-270 `_free_text_spans`), so the card/SSN rules still also detect (catches the letter-lookalike SSN/card shapes above; stricter than "labels only": INFO-6).
- Unit replays: 121 × 3 = 363 → 0 duplicate lines, 0 duplicate ledger events, units == 1 each; same request_id + other text → 409 121/121.

### 1a. Detector sweep over every separator character (poc/detector_probe11.py → out/detector-probe-f5ee168-3.1x.json)
Each code point U+0020..U+2FFFF (non-digit, non-surrogate; 193,720) as the separator of an SSN `123?45?6789`, a card `4111?1111?1111?1111` and a 3-3-3 nine-digit run, through `screen_free_text`. Not redacted: SSN 1,299, card 1,240, 3-3-3 127,905 (3.12, unidata 15.0) / 128,527 (3.13, unidata 15.1: more CJK letters). **Every miss, on both Pythons, is a character that is a letter or whose NFKC form contains a letter** (0 misses with a non-letter separator). SSN/card misses are all characters NFKC-mapping to ASCII letters (™, ℡, №, Roman numerals, modifier letters, ª º); 3-3-3 misses are the non-ASCII letters (CJK, Hangul, Lm/Lo lookalikes ー ꓸ ǀ ـ).

### 1c. request_sha recovery (poc/request_sha_recover3.py → out/request-sha-recover-*.jsonl/json)
| log / spelling | request_sha = | search (area digits 123 known) |
|---|---|---|
| **control, 0f5fbe3** unit `123  45  6789` (my run, out/pii-control-0f5fbe3-3.12.json) | raw body | **found in 1.93 s** (attack works) |
| **control, 0f5fbe3** face attribute `billing 123  45  6789` (200 there) | raw body | **found in 1.61 s** |
| f5ee168 unit `123  45  6789`, U+1680, `123 45 6789`, `123-45-6789`, `123 - 45 - 6789`, `123.45.6789`, `123456789` | hash of the body with the kept (redacted) text: **121/121 unit lines** (`log_request_sha_eq_hash_of_kept_body`); raw-body hash 0/111 redacted lines | 7 × 10^6 tried, **not found** |
| f5ee168 unit card `4111 1111 1111 1111` (BIN6+last4 known) | kept body | 10^5 Luhn candidates, **not found** |
| f5ee168 face attribute (any spelling) | — refused, no line written | n/a |
| 91ea9a1-written log after upgrade to f5ee168 (card unit) | raw body (old line kept, append-only) | **found in 0.13 s** (INFO-3, pre-existing data) |
- service.py:73-78: `_screened` replaces every `vendor_text` by `pii.screen_free_text(x)[0]` unconditionally, the same function `observed()` stores (svc_campaigns.py:64) → the raw/stored divergence of D10-M1 is gone. Other vendor_text writers (svc_campaigns.py:416/615/773, svc_inventory.py:217, svc_contracts.py:97) all use `observed()`: code inspection only.
→ D10-H1 CLOSED, D10-M1 CLOSED (C7, C12 VERIFIED).

## Checks
| id | area | result | method |
|---|---|---|---|
| C1 | H-1 write path: a changed file is never written over | VERIFIED (unchanged) | rl_omega9 66×2, real ledger; summary == omega10 |
| C2 | H-1 never reported ok while changed (forced checks/audit) | VERIFIED; cached reads lag (L-A, unchanged) | rl_omega9 + h1_stale_report |
| C3 | H-1 recovery: rolled forward once, anchors==lines, no dup | VERIFIED (restore); restart-on-deleted/shortened = M-3 | rl_omega9, m3_stale_delete |
| C5 | M-3 | UNCHANGED (open MEDIUM) | output byte-identical 0f5fbe3 vs f5ee168 ×2 Pythons |
| C6 | M-2 | UNCHANGED (open MEDIUM) | targeted json-equal 0f5fbe3 vs f5ee168 ×2 |
| C7 | OOH2-PII-1: no 9+ digit card/SSN cluster (non-letter separators) in any persisted byte | **VERIFIED: 0 leaks, 555/555 cases per Python** (D9-H1, D10-H1, D10-M1, earlier + 54 new spellings); letter-separated spellings stored (design, D11-L2) | pii_persist11 605×2 real ledger + detector sweep 193,720 separators ×3 shapes ×2 |
| C8 | a refused request writes nothing | VERIFIED 231/231 per Python | file sha256 + log length + ledger count |
| C9 | request_sha idempotency | VERIFIED: same body replays (363 unit + 750 lifecycle replays, 0 duplicate lines/events); other kept text 409 (121/121, 10/10 ×10 runs); difference only inside a redacted cluster or in whitespace/NFKC form → replay (by design) | pii_persist11, xv_idem11, idem_collapse11 |
| C10 | names_a_card same set as 99569bd | VERIFIED 78,062/78,062 + 38/38 | funds_decide, funds_service |
| C11 | 0f5fbe3 / 91ea9a1 / 75b0662 logs start and replay at f5ee168 | VERIFIED (state diff [], 76/76 anchors, 0 duplicates); retries of old vendor_text requests not in screen form → 409 (D11-L1) | xv_idem11 |
| C12 | redacted value not recoverable from persisted bytes (request_sha) | VERIFIED for every line written at f5ee168 (8 searches not found; positive control at 0f5fbe3 found in 1.6-1.9 s); pre-upgrade lines still recoverable (INFO-3) | request_sha_recover3 |
| C13 | start-up read refusals | VERIFIED unchanged, 6 cases ×2; device NOT RUN | start_read |
| C14 | false positives measured | MEASURED: 40/70 realistic OOH texts redacted/refused in free text (5 before), 22/70 approval refs (4 before) — accepted trade-off OOH2-PII-1-FP | fp_corpus11 |

convergence: n/a (no schema or log-format migration in the delta; logs from 0f5fbe3/91ea9a1/75b0662 start with state diff [], C11). greenfieldHash/brownfieldHash: not applicable.
idempotency: [{path: unit inventory add, 121 spellings, replays: 363, duplicateEffects: 0}, {path: full lifecycle 75 requests, 5 A→B pairs × 2 Pythons, replays: 750, duplicateEffects: 0}, {path: same request_id other body, replays: 121 + 100 + 28, duplicateEffects: 0}].

## Findings
- **D10-H1 — CLOSED.** All 14 D10-H1 spellings, the D9-H1 list and the 29 earlier spellings: vendor_text redacted, face attribute and approval ref refused with nothing written, 0 leaks in every persisted byte (C7, C8).
- **D10-M1 — CLOSED.** request_sha is the hash of the body with the kept vendor text for 121/121 unit lines; the D10-M1 searches that succeeded at 0f5fbe3 (re-run here as positive control: 1.93 s / 1.61 s) find nothing at f5ee168; the face attribute now refuses the divergent spellings (C12).
- **D11-L1 LOW (non-blocking) — retries crossing the upgrade of non-PII vendor_text requests answer 409; docstring claim false.** request_sha now hashes `screen_free_text(vendor_text)[0]` always (service.py:73-78), so an old line's raw-text hash no longer matches when the old vendor_text had double spaces/newline/leading-trailing space, NFKC-changing characters, or a ≥9-digit cluster: 4/75 retries 409 REQUEST_ID_REUSED from a 0f5fbe3 log, 6/75 from 91ea9a1/75b0662 logs (nothing written, unit exists once). service.py:89-90 says "a body without personal data hashes as before (logs written earlier still replay)" — false for those bodies. Risk: a client that treats 409 as "use a new request_id" creates a second unit (not measured; client behaviour out of scope).
  - Reproducer: `python -I poc/xv_idem11.py <ledger-bin> <fresh-dir> <tree-0f5fbe3> <tree-f5ee168>` → `read.replays.status_differs_from_original`.
  - Falsifier: the same run shows 75/75 original statuses (or the docstring/OPEN.md names this population and the upgrade note says in-flight retries may 409).
- **D11-L2 LOW (non-blocking, documented design limit) — card/SSN digits split by letters or HTML markup are stored in clear.** `SSN 123&nbsp;45&nbsp;6789`, `4111&nbsp;1111&nbsp;1111&nbsp;1111`, `4111<br>1111<br>1111<br>1111`, `123a45a6789`, and 3-3-3 runs split by punctuation-like letters (`123ー456ー789`, `123ꓸ456ꓸ789`, `123ǀ456ǀ789`) reach the log, pending files, export and views (41/50 cases; face attribute 200; `123a45a6789` approval ref 200). Outside the founder rule ("joined by non-letter separators") and named in pii.py's docstring ("letters used as separators cannot be detected reliably"), so not blocking; `_norm` already decodes some HTML entities for @ and ., and decoding `&nbsp;`/tags before the cluster rule would close the realistic paste-from-HTML part.
  - Reproducer: `SECRETS_ONLY="SSN 123&nbsp;45&nbsp;6789|4111&nbsp;1111&nbsp;1111&nbsp;1111|4111<br>1111<br>1111<br>1111" python -I poc/pii_persist11.py <ledger-bin> <fresh-dir> <tree-f5ee168>`.
  - Falsifier: the same run shows redacted:true / 422 and 0 leaks for those spellings.
- **M-3 MEDIUM (unchanged, open)** — anchored pending line deleted at start when the log is one line shorter or deleted; after restore "anchors beyond the local log", line lost, 503 for good. Reproducer: `poc/m3_stale_delete.py` shorten_then_restore. Falsifier: after restore the line is applied, anchors == lines.
- **M-2 MEDIUM (unchanged, open)** — after a consistent rewrite, hub read 200 serves client_funds_received 99999.00 with no integrity marker while writes 503. Reproducer: `poc/targeted.py <tree> <dir>` key B_consistent_rewrite_inflated_funds. Falsifier: hub read 503 or flagged while integrity.ok is false.
- **L-A LOW (documented, unchanged)** — cached reads report ok up to 10 s after a file change with no write. Reproducer/falsifier: `poc/h1_stale_report.py`.
- INFO-3 (unchanged) — card/SSN bytes and raw-body request_sha written before 0f5fbe3/f5ee168 stay in an append-only anchored log; measured: a 91ea9a1-written card recovered from the log after upgrade to f5ee168 in 0.13 s. No scrub path (the card/SSN purge step in OPEN.md is the only remedy).
- INFO-5 (extended, by design) — same request_id with vendor_text differing only inside a redacted cluster (incl. false-positive clusters such as a changed date range or io-ref) or only in whitespace/NFKC form replays the first answer; the second body's values are never stored.
- INFO-6 — the card/SSN rules are not "labels only": `_free_text_spans` = legacy spans ∪ clusters, so they still detect (stricter; it is why SSN/card shapes with letter-lookalike separators are caught). No data risk.
- OOH2-PII-1-FP (accepted) — measured in §4: ZIP+4, ISO date ranges with ` - `/`/`/`,`, decimal GPS pairs, ≥9-digit totals/impressions, permit/IO/invoice numbers redacted or refused.

## NOT RUN
- Device node at the log path (mknod EPERM, non-root). No shared/staging/production datastore (none in scope). Isolation mode: one disposable data dir + one ledger process/port/file per case under scratchpad/data11run; proves exclusivity only, not tenant RLS (not in the delta). No CI; no full 10^9 search (not needed: hash is independent of the secret at f5ee168). Separator sweep covers single-character separators exhaustively and multi-character ones by the corpus only; CJK numerals (〇 零) and other non-Nd numerals as digits; glyph rendering of letters that may display blank; vendor_text paths other than unit add exercised by code inspection only (all call `observed()`); campaign names and other has_personal_data fields (widened by `_screened_form`, not in the three fields); concurrency (AEGIS-RELIABILITY); Windows/Linux filesystems; build provenance of the ledger binary; FP corpus on 3.13 (3.12 only; detector identical across Pythons per §1a except Unicode-version letters).
- Candidate suite on my archive (context only; tests passing ≠ property proven): 1137 passed on 3.12 and 1137 passed on 3.13 (out/pytest-f5ee168-3.1x.txt).

## Verdict
blocking: false. D10-H1 and D10-M1 closed and measured (0 leaks over 555 non-letter cases per Python; request_sha over the kept text; recovery fails where the 0f5fbe3 control succeeds); refused requests write nothing; replays and 409s verified, earlier logs start and replay (retries of old non-screen-form vendor_text requests 409: D11-L1 LOW); H-1 matrix and start-up refusals unchanged; names_a_card equals 99569bd; M-2, M-3 open MEDIUM unchanged; false positives measured and accepted (OOH2-PII-1-FP); letter/markup-separated digits remain a documented limit (D11-L2 LOW).

VERDICT: DATA_GREEN
