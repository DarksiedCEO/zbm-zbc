# AEGIS-DATA (Gate 5) — omega12
- repo: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: 1a904983d6e064b912bbd95a7afa1668da6c50c9
- treeOid: 83356dd062ff3e668358785f5e6d963cdb3950c2 (`git rev-parse 1a90498^{tree}` in the repo: matches)
- scope: persistence/data integrity of `git diff f5ee168 1a90498 -- services` (ooh-py: pii.py, service.py docstring, 2 tests); OOH2-PII-1 founder decisions after r11 (ASCII-letters-only cluster end, HTML entity decoding, unassigned kept as typed) for unit vendor_text (redact), face text attribute and client_approval_doc_ref (refuse); funds/H-1/start-up/M-2/M-3 short re-runs
- author: AEGIS-DATA (AG-AEGIS-DATA v1.0.0), fresh context, read-only on candidate; Claude Opus 5.5 subagent; 2026-10-18
- method: own `git archive` trees (t-1a90498 `diff -r -x __pycache__` vs frozen review copy: empty) + disposable data dirs + own ledger-rust build under scratchpad/data12run (outside omega12); no shared/real datastore. omega11 PoCs copied to omega12/data/poc as a starting point; every result below is from this run.
- raw/PoCs: scratchpad/aegis/omega12/data/{poc,out}

## Environment
- ledger: `services/ledger-rust` identical f5ee168 → 1a90498 (`git diff --quiet` rc 0); built by me from my t-1a90498 archive, `cargo build --release --offline`, SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk, private CARGO_TARGET_DIR under data12run → `server` sha256 26e30a2c5c3880f8874ac854531cd5098b095b7eaab4396867b359b12a069d16 (path-dependent; not a provenance claim). One ledger process/port/file per case.
- service: in-process (helpers.Harness + real HttpLedgerClient over HTTP) from my `git archive` trees t-1a90498 / t-f5ee168 / t-0f5fbe3 / t-91ea9a1 / t-75b0662 / t-99569bd; Python 3.12.14 (unidata 15.0.0) and 3.13.9 (unidata 15.1.0).

## 2. Single-code-point separator sweep, new rule (poc/detector_probe12.py → out/detector-probe-1a90498-3.1x.json, out/detector-probe-compare.txt, out/u0099-check.txt)
Population: every code point U+0020..U+2FFFF that is not a surrogate and not `str.isdigit()` = **193,720**, as the separator of SSN `123?45?6789`, card `4111?1111?1111?1111`, 3-3-3 `123?456?789`, and (new) 3-3-3 with the separator written as a hex entity `&#xHHHH;`; through `pii.screen_free_text`. Each miss classified: ASCII letter / NFKC contains an ASCII letter / screen_form or _norm contains one / OTHER.
| shape | misses 3.12 (unidata 15.0) | misses 3.13 (15.1) | ASCII letter | NFKC → ASCII letter | OTHER |
|---|---|---|---|---|---|
| SSN 3-2-4 | 1,182 | 1,182 | 52 | 1,130 | **0** |
| card 4-4-4-4 | 1,182 | 1,182 | 52 | 1,130 | **0** |
| 3-3-3 | 1,182 (f5ee168: 127,905) | 1,182 (f5ee168: 128,527) | 52 | 1,130 | **0** |
| 3-3-3, `&#xHHHH;` separator | 1,183 | 1,183 | 52 | 1,130 | 1: `&#x99;` |
- f5ee168 numbers re-measured by me (out/detector-probe-f5ee168-3.1x.json), not copied. Miss sets identical across shapes and **identical 3.12 vs 3.13** (code-point lists equal) — the Unicode-version dependence of the 3-3-3 miss set at f5ee168 (CJK letters added in 15.1) is gone. Categories of the 1,182: Ll 412, Lu 398, So 268 (circled/parenthesized letters, ™, ℡, ㎏ …), Lm 65, Nl 32 (Roman numerals), Lt 4, Lo 2 (ª º), Sc 1 (₨).
- The one OTHER: `123&#x99;456&#x99;789` — HTML5 remaps numeric references 0x80–0x9F to Windows-1252, so `html.unescape("&#x99;")` = "™" → NFKC "TM" (out/u0099-check.txt: raw U+0099 separator is redacted). It is a character that normalises to ASCII letters → within the rule.
- Hits: 0 kept texts that still hold a 9-digit cluster in kept / screen_form / _norm / decoded forms (n_kept_bad 0, both Pythons).
→ **Every single-code-point miss is an ASCII letter or normalises to one (both Pythons).** Multi-character separators: corpus only (§1).

### 2a. Digit characters (poc/digit_forms12.py, digit_forms_cards12.py → out/digit-forms-*.json, out/digit-forms-cards-*.json)
Population: every code point U+0020..U+2FFFF with `isdigit()` or a 0–9 digit/numeric value, not ASCII 0-9 = 1,237. Nine of the character in a row / as the 9th digit of `12345678?`:
| tree (3.12; 3.13 identical counts except Lo 65 not detected on both trees) | detected (run of 9) | not detected |
|---|---|---|
| f5ee168 | 800 | No 231, Nl 148, Lo 58 |
| 1a90498 | **731** | No **300**, Nl 148, Lo 58 |
- **69 characters detected at f5ee168 are not at 1a90498**: DOUBLE CIRCLED DIGIT ONE–NINE (U+24F5–24FD), NEGATIVE CIRCLED DIGIT ZERO (U+24FF), DINGBAT NEGATIVE CIRCLED / CIRCLED SANS-SERIF / NEGATIVE CIRCLED SANS-SERIF DIGIT ONE–NINE (U+2776–277E, U+2780–2788, U+278A–2792), ETHIOPIC DIGIT ONE–NINE (U+1369–1371), NEW TAI LUE THAM DIGIT ONE (U+19DA), KHAROSHTHI DIGIT ONE–FOUR, RUMI DIGIT ONE–NINE, BRAHMI NUMBER ONE–NINE. All `str.isdigit()` True, category No, not mapped by NFKC or by strip_format (which folds only Nd).
- Cards/SSNs written in these sets (7 sets × 5 shapes = 35, e.g. `⓸⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵`, `❹❶❶❶-❶❶❶❶-❶❶❶❶-❶❶❶❶`, `➀➁➂-➃➄-➅➆➇➈`, `SSN ➊➋➌ ➍➎ ➏➐➑➒`): **f5ee168 35/35 redacted (label `number`), 1a90498 0/35** (`free_text_has_personal_data` False). The card/SSN rules never caught them; only the cluster rule did, through `ch.isdigit()`.
- Cause: pii.py:261 `elif "0" <= ch <= "9":` (was `elif ch.isdigit():`). The founder decision changed what *ends* a cluster (only ASCII letters); the commit also changed what *counts* as a digit, which no decision listed. → **D12-H1** (§Findings), persistence in §1.

## 3a. Kept text and request hash across Unicode versions (poc/xver_kept12.py, xver_compare12.py → out/xver-1a90498-compare.json; per-code-point rows in data12run)
Population: every code point U+0020..U+10FFFF except surrogates = **1,112,032**, in `faces west X lit` and `123X456X789`; kept text from `pii.screen_free_text`, request hash = payload_sha256 of `{request_id, vendor_text: kept}` (the `_screened` + `payload_sha256` formula of service.py:71-104 / ledger.py:67-70, re-implemented stdlib-only because ledger.py imports httpx — equivalence checked on real service lines in §3b).
| vs 3.12.14 (unidata 15.0.0) | Cn in 15.0 assigned in other | request hash differs (faces / cluster template) | kinds differ |
|---|---|---|---|
| 3.13.9 (15.1.0) | 627 (Lo 622 CJK Ext I, So 5 U+2FFC–2FFF, U+31EF) | **0 / 0** | **0** |
| 3.14.4 (16.0.0; not a declared target — production image python:3.12-slim, ci.yml:501-547) | 5,812 | 106 / 26 | 106 |
- 3.12 ↔ 3.13: **stable for all 1,112,032** (at f5ee168 the 627 newly assigned characters were removed on 3.12 and kept on 3.13 — reliability N1; closed).
- 3.12 ↔ 3.14: the 106 are Cn→Nd (80 new decimal digits, e.g. U+10D40.. Garay; folded to ASCII on 3.14, kept as typed on 3.12) and Cn→So (26, NFKC to digits on 3.14). Founder decision "unassigned characters kept as typed" makes the hash version-independent only while a new Unicode version assigns no digits/marks/format characters among them; 15.0→15.1 qualifies, 15.0→16.0 does not → INFO-7 (no current impact).

## 6. M-2, M-3 (poc/m3_stale_delete.py, targeted.py → out/m3-*, out/targeted-*)
- Delta f5ee168→1a90498 under services = 4 files (out/delta-files.txt: pii.py, service.py docstring only, 2 tests); store.py / svc_* untouched.
- **M-3: UNCHANGED, open MEDIUM.** Output byte-identical 1a90498 = f5ee168 on 3.12 and 3.13 (sha256 prefix 8bcb404bdc662a41 ×4, same prefix as omega11's): shorten_then_restore → pending.line deleted at restart 1 ("the ledger holds anchors beyond the local log"), inflight not applied, write 503, anchors/lines 14/13.
- **M-2: UNCHANGED, open MEDIUM.** targeted output json-equal 1a90498 vs f5ee168, both Pythons: B_consistent_rewrite_inflated_funds → STARTED, health degraded, write 503, **hub read 200 hub_funds_received 99999.00**, no marker.

## 5a. Funds: names_a_card 99569bd vs 1a90498 (poc/funds_corpus.py, funds_decide.py, funds_service.py → out/funds-*)
| level | population | 99569bd | 1a90498 | rows differing |
|---|---|---|---|---|
| unit (names_a_card, funds_ref_parts, funds_ref_key) | 78,112 refs (corpus from both trees' test strings ∪ omega11's 78,062; out/funds-corpus12.json) | sha256 05b0f4ae323e0ce1…, refused-as-card 9,127 | same sha256, 9,127 | **0** (3.12 and 3.13 identical) |
| service (38 refs, real data dir) | status, detail, lines, events, ref_in_log | sha256 0e2e7d2d663277ac… | same (all 4 files one sha256) | 0 |
- names_a_card reads pii.card_spans (svc_campaigns.py:169), unchanged in the delta; the cluster/entity changes do not reach the funds path.

## 5b. H-1 matrix and start-up read, short re-run (poc/rl_omega9.py, h1_summary.py, start_read.py → out/h1-*, out/start-read-*)
- H-1: 66 cases per Python on my ledger build, 96 s each. Per-case summary of 1a90498 on 3.12 **byte-identical** to 1a90498 on 3.13, to my own f5ee168 3.12 run (out/h1-f5ee168-3.12.summary.txt), and to omega11's f5ee168 3.12/3.13 summaries (`cmp` rc 0 ×4). okWhileChanged=True **0/66** per Python; duplicate event ids 0. The 20 summary "FAIL" flags are the same known 20 (15 check_first|restore: refused before any anchor; 3 deleted: the write-path open re-creates a 0-byte file; restart-on-deleted/shortened = M-3).
- Start-up read: control STARTED + write 201; symlink → identical copy / dangling, unix socket → REFUSED "cannot be opened (OSError)"; FIFO, directory → REFUSED "not a regular file"; refusals change no data-dir entry, add 0 ledger events, and after the regular file is put back start with integrity ok, state diff []. Start messages equal to omega11's f5ee168 output case by case, both Pythons. Device node NOT RUN (mknod EPERM, non-root).
→ C1–C3, C13 VERIFIED unchanged.

## 4. False positives, both Pythons (poc/fp_corpus12.py, fp_summary12.py → out/fp-corpus-{1a90498,f5ee168}-3.1x.json, out/fp-summary-3.1x.txt)
omega11's 70 realistic OOH texts + 16 new (10 non-Latin-script lines: Japanese/Russian/Greek/Arabic/Korean dates, addresses, panel specs; French/German lines; 6 HTML-entity lines — one entered as `,500&nbsp;net &middot; 4&nbsp;weeks` because my shell expanded `$12`), through the service (helpers.Harness, in-process ledger, real data dir) in the three fields. Output json-equal 3.12 = 3.13 at each tree (new this round: the FP corpus on 3.13).
| class | n | vendor_text redacted 1a90498 (f5ee168) | face refused PD 1a90498 (f5ee168) | approval ref refused PD 1a90498 (f5ee168) |
| address | 6 | 1 (1) | 1 (1) | 0 (0) |
| zip+4 | 5 | 5 (5) | 5 (5) | 4 (4) |
| flight/date | 11 | 6 (6) | 6 (6) | 3 (3) |
| gps | 6 | 4 (4) | 4 (4) | 2 (2) |
| price | 7 | 3 (3) | 3 (3) | 0 (0) |
| impressions | 5 | 2 (2) | 2 (2) | 0 (0) |
| permit | 6 | 6 (6) | 6 (6) | 5 (5) |
| ref | 12 | 7 (7) | 7 (7) | 6 (6) |
| spec/time | 6 | 0 (0) | 0 (0) | 0 (0) |
| phone/contact (true positives for comparison) | 3 | 3 (3) | 3 (3) | 2 (2) |
| non-Latin script (r12) | 10 | 7 (2) | 7 (2) | 0 (0) |
| HTML entity (r12) | 6 | 1 (1) | 1 (1) | 0 (0) |
| mixed realistic | 3 | 3 (3) | 3 (3) | 0 (0) |
| total | 86 | 48 (43) | 48 (43) | 22 (22) |
- 3.13 table identical (out/fp-summary-3.13.txt).
- **Changes vs f5ee168: 5 newly redacted/refused texts (43 → 48 per free-text field) + 1 now redacted wider, all non-Latin script** (CJK/Cyrillic/Greek/Arabic letters no longer end a cluster): `掲出期間 2026年11月1日〜2026年11月30日` → `掲出期間 [redacted]日`; `ул. Тверская 12, стр. 3, 125009` → `ул. Тверская [redacted]`; `περίοδος 1/11/2026 έως 30/11/2026` → `περίοδος [redacted]`; `الفترة 2026/11/01 إلى 2026/11/30` → `الفترة [redacted]`; `パネル 14×48 第0451号 点灯 10時〜22時` → `パネル [redacted]時`; `東京都渋谷区 1丁目2番3号 150-0002` → `東京都渋谷区 [redacted]` (was a partial phone redaction). Korean address, French/German lines kept. One cosmetic change: `ops (310) 555-0142 ext 12` → `ops ([redacted] ext 12` (was `ops [redacted] ext 12`).
- HTML entities: 1/6 redacted (`ZIP 90017&#8209;2001` → `ZIP [redacted]`, as plain ZIP+4 already was); `R&amp;D`, `Wilshire &amp; 5th`, `14&times;48 … &ndash; lit`, `flight 2026&ndash;11&ndash;01 to 2026&ndash;11&ndash;30` kept **as typed** (raw entities, not decoded). Approval refs: unchanged 22/86 (the Ref schema `^[A-Za-z0-9._:-]{1,100}$` refuses every non-ASCII text first).
- Measured, accepted (OOH2-PII-1-FP); the non-Latin effect is new: for non-English operators, almost any date range or address with ≥9 digits is now redacted/refused regardless of the words between the numbers.

## 3b. request_sha, replays, logs written by earlier versions (poc/xv_idem12.py → out/xi-*.json; poc/idem_collapse12.py → out/idem-collapse-*.json)
Writer A: tenant, 14 units (omega11's 8 + `faces west U+2FFC lit`, `lot U+2EBF0 north` (unassigned in 15.0, assigned in 15.1), `faces west U+0378 lit` (unassigned in both), `fine &amp; dandy 14x48`, `contact 12ー34ー56ー789 for access`, `contact 4111&nbsp;1111&nbsp;1111&nbsp;1111 for access`), face attribute, campaign → authorized → submitted PO: **93 recorded 2xx requests**. Reader B = 1a90498 on the same data dir + ledger file (real ledger): forced integrity, state diff, replay all 93, 16 same-request_id/other-body retries, one new write, restart, ledger readback.
| A → B (Python) | start / integrity / state diff | A's vs B's request_sha | replays: original status / **409** | lines added (replays + retries) | other-body retries 409 | new write, restart, anchors==lines |
|---|---|---|---|---|---|---|
| 1a90498 → 1a90498 (3.12; 3.13) | ok / ok / [] | 93/93 | 93 / 0 | 0 | 16/16 | 201, ok, 94/94, 0 problems |
| **1a90498 3.12 → 1a90498 3.13**, and 3.13 → 3.12 | ok / ok / [] | **93/93** | 93 / **0** | 0 | 16/16 | 94/94 |
| f5ee168 → 1a90498 (3.12) and f5ee168 3.12 → 1a90498 3.13 | ok / ok / [] | 88/93 | 88 / **5**: U+2FFC, U+2EBF0, U+0378 texts (removed at f5ee168 3.12, kept now), look-alike cluster, entity card (unredacted at f5ee168, redacted now) | 0 | 16/16 | 94/94 |
| f5ee168 → 1a90498 (3.13) | ok / ok / [] | 90/93 | 90 / **3**: U+0378, look-alike, entity card (U+2FFC/U+2EBF0 were already kept by f5ee168 on 3.13) | 0 | 16/16 | 94/94 |
| 0f5fbe3 → 1a90498 (3.12; 3.13) | ok / ok / [] | 87/93 | 87 / **6**: whitespace/newline, NFKC ligatures, date range, io-ref (D11-L1 population), look-alike, entity card | 0 | 16/16 | 94/94 |
| 91ea9a1 → 1a90498, 75b0662 → 1a90498 (3.12; 3.13) | ok / ok / [] | 85/93 | 85 / **8**: those 6 + card + `123.45.6789` | 0 | 16/16 | 94/94 |
- Logs written at f5ee168, 0f5fbe3, 91ea9a1, 75b0662 **start** (integrity ok, state diff []) and **replay** at 1a90498 with **0 duplicate lines/events** in every one of 13 runs (1,209 replays, 208 other-body retries; readback anchors == lines 94/94, 0 problems).
- **Cross-version stability 3.12 ↔ 3.13 at 1a90498: 93/93 hashes equal, 0 status changes**, including the 15.0-unassigned/15.1-assigned code points (f5ee168 3.12 → f5ee168-era behaviour differed: N1 closed).
- Retry-across-upgrade 409s (nothing written, unit exists once) per writer: f5ee168 5 (3.12) / 3 (3.13), 0f5fbe3 6, 91ea9a1 8, 75b0662 8 of 93. New this round in that population: vendor texts with unassigned characters written at f5ee168 (removed then, kept now) and texts whose screen changed (look-alike-letter clusters, entity-spelled numbers). Same mechanism as D11-L1; ADR 0022:371-372 now documents it ("A retry, after this change, of a request accepted before it may answer 409 … (unreleased service)") and service.py:89-93 no longer claims every non-PII body hashes as before → **D11-L1 CLOSED (documented)**, population re-measured above.
- Same version, same request_id, other vendor_text (22 pairs; 3.12 = 3.13 json-equal; 0 lines added by any retry; 1 unit each):
| first → retry | 1a90498 | f5ee168 |
|---|---|---|
| omega11's 14 pairs | identical to f5ee168 (card→card, SSN→SSN, whitespace/NFKC, false-positive clusters → 201 replay; real text change → 409) | same |
| `12ー34ー56ー789` → `98ー76ー54ー321`; `4111&nbsp;…` → `4111 1111 1111 1111` / `5555&nbsp;…4444`; `掲出 2026年11月1日〜…` → `…12月…` (differ only inside a redacted cluster) | **201 replay** (by design) | 409 |
| `fine &amp; dandy` → `fine & dandy` | 409 (entities kept as typed in non-PII text) | 409 |
| `faces west U+2FFC lit` → `faces west lit`; `U+0378` → `U+0379` | **409** (kept as typed) | 201 replay (both removed) |
| `⓸⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵` → `…⓶` | **409** (not redacted: D12-H1) | 201 replay (redacted) |
→ C9, C11 VERIFIED.

## 1. OOH2-PII-1 — persisted bytes, five paths, real ledger (poc/make_secrets12.py → poc/secrets12.json; poc/pii_persist12.py, pii_summary12.py → out/pii-1a90498-3.1x.{json,summary.txt})
Corpus: omega11's 121 spellings + 66 new = **187 distinct spellings × 5 paths** (unit vendor_text; face text attribute `location_description`; client_approval_doc_ref; vendor_text in pending.line after a failed append; in pending.discarded after a kill between pending write and anchor) = **935 cases per Python, 0 errors; summaries byte-identical 3.12 = 3.13**. New: omega11's D11-L2 forms re-classed by the new rule, 19 look-alike-letter separators (ʼ ー ǀ ـ ː ˉ ꞏ 一 α а ㅡ א ก é ß Α ı …), 22 HTML-entity spellings (`&nbsp;`, `&nbsp` without `;`, `&ndash;`, `&mdash;`, `&minus;`, `&hyphen;`, `&NonBreakingSpace;`, `&#32;`, `&#x2D;`/`&#X2d;`, `&#x200B;`, `&#52;…`, hex-entity digits `&#x34;&#x31;…`, `&amp;nbsp;`, `&amp;amp;nbsp;`, …), 1 four-level entity, 5 tag spellings, 3 letter-free markup (`<>`, `<!---->`, `</>`), 5 non-decimal-digit spellings, 5 unassigned/15.1 separators, 6 @-spellings, 6 ASCII-letter-split controls. Each row labelled by my rule implementation (make_secrets12.py `expect`): **detect** (no 9-digit run split by an ASCII letter in NFKC raw or HTML-decoded form) 171, **design_miss** 15, four-level entity 1. Leak scan as omega11 plus the fully HTML-decoded bytes (10 rounds) and `\U` escapes, over every file of the data and ledger dirs, /audit/export, /audit/evidence and the views.
| expectation / class | spellings | vendor_text 201 redacted | face attribute | approval ref | pending.line / .discarded | leak cases |
|---|---|---|---|---|---|---|
| detect: omega11 non-letter classes (D9-H1, D10-H1, D10-M1, earlier, new-cluster, wide-sep, unicode-space, hidden, cards, `123456789`) | 96 | 96/96 | 96 422 PD, nothing written | 96 422, nothing written | 0 leaks | **0** |
| detect: omega11 look-alike SSN/card shapes + omega11 "letter-sep (design)" now in scope (`123ー456ー789`, `123ꓸ456ꓸ789`, `123ǀ456ǀ789`, `123&nbsp;45&nbsp;6789`, `SSN 123&nbsp;…`, `4111&nbsp;…`) | 15 | 15/15 | 15 422 PD | 15 422 | 0 | **0** (omega11: leaked, D11-L2) |
| detect: r12 look-alike letters | 17 | 17/17 | 17 422 PD | 17 422 | 0 | **0** |
| detect: r12 HTML entities (incl. `&amp;amp;nbsp;`, hex-entity digits, no-semicolon `&nbsp`) | 19 | 19/19 | 19 422 PD | 19 422 | 0 | **0** |
| detect: letter-free markup, unassigned separators, @-spellings | 13 | 13/13 | 13 422 PD | 13 422 | 0 | **0** |
| detect: omega11 digit forms (full-width, superscript, math-bold, keycap, Thai) | 5 | 5/5 | 5 422 PD | 5 422 | 0 | **0** |
| **detect: non-decimal digits** (omega11's `⓵⓶⓷-⓸⓹-⓺⓻⓼⓽` + r12 `⓸⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵`, `❹❶❶❶-…`, `➀➁➂-➃➄-➅➆➇➈`, `፩፪፫-፬፭-፮፯፰፱`, `SSN ➊➋➌ ➍➎ ➏➐➑➒`) | **6** | **0/6: stored in clear, redacted:false** | **6/6 200, stored** | 6 422 schema (non-ASCII) | **in clear in both files; rolled into the log** | **24** (log, views, export, face view, pending.line, pending.discarded) — omega11 had 0 for the first one at f5ee168 |
| four-level entity `4111&amp;amp;amp;nbsp;…` (code decodes 3 levels) | 1 | 0/1 stored as typed | 200 | 422 schema | stored | 4 (only after a 4th decoding: `:html` hits) |
| design_miss: split by ASCII letters (`123a45a6789`, `123x456x789`, `1234x56789`, `card4111x…exp`, `ⓐ` `ａ` `ᴬ` `™` → ASCII, `<br>`, `<p>`, `<b>`, `<span>`, `&lt;br&gt;`) | 15 | 0/15 (by design) | 15 200 | 11 schema 422, 4 accepted | stored | 64/75 (by design) |
- **Expected-detect totals: 165/171 spellings, 825/825 cases with 0 leaks; the 6 non-decimal-digit spellings: 24/30 cases leak (D12-H1).**
- Refused requests: **348/348 nothing written** (165 face + 171 approval among detect; plus 12 approval refusals in the other rows; log length, ledger event count and every file sha256 unchanged), control write after each OK.
- Unit replays: 171 × 3 = 513 (+48 others) → 0 duplicate lines, 0 duplicate ledger events, units == 1 each; same request_id + other text → 409 187/187.
- request_sha on every unit line = hash of the body with the **kept** text: 187/187; hash of the raw body on a redacted line: 0/165. Labels on redacted lines: ssn 82, card 45, number 37, email 1.

### 1c. request_sha recovery (poc/request_sha_recover3.py → out/request-sha-recover-1a90498.jsonl, out/request-sha-recover-control-0f5fbe3.json)
| log | spelling / separator | search | result |
|---|---|---|---|
| **control: 0f5fbe3** unit, my run (out/pii-control-0f5fbe3-3.12.json) | `123  45  6789` | SSN, area 123 known | **found in 1.03 s** (456,790 tried) |
| 1a90498 unit | `123  45  6789`, `123 45 6789`, `123-45-6789`, `123ー45ー6789`, `123&nbsp;45&nbsp;6789`, `123456789` | 10^6 each | **not found** ×6 |
| 1a90498 unit | `4111 1111 1111 1111`, `4111&nbsp;1111&nbsp;1111&nbsp;1111` (BIN6 + last4 known) | 10^5 Luhn candidates each | **not found** ×2 |
| 1a90498 face / approval | any detected spelling | — refused, no line written | n/a |
| 1a90498 unit, non-decimal digits (D12-H1) | `⓸⓵⓵⓵ …` | none needed | the number is in the line in clear |
→ C7 **MISMATCH** (D12-H1), C8 VERIFIED, C12 VERIFIED for every redacted line.

## Checks
| id | area | result | method |
|---|---|---|---|
| C1–C3 | H-1 write path / never ok while changed / recovery | VERIFIED unchanged | rl_omega9 66×2 real ledger; summary `cmp`-identical to my f5ee168 run and omega11's |
| C5 | M-3 | UNCHANGED (open MEDIUM) | m3_stale_delete byte-identical 1a90498 = f5ee168 ×2 Pythons |
| C6 | M-2 | UNCHANGED (open MEDIUM) | targeted json-equal ×4 |
| C7 | OOH2-PII-1: no 9+ digit cluster not split by ASCII letters in any persisted byte | **MISMATCH**: 165/171 spellings (825 cases) 0 leaks; **6 non-decimal-digit spellings leak on 4 of 5 paths (24 cases), both Pythons** (D12-H1) | pii_persist12 935×2 real ledger; sweep 193,720 ×4 shapes ×2; digit-form sweep 1,237/1,244 code points |
| C7a | separator sweep: every miss an ASCII letter or normalises to one | VERIFIED (1,182 misses/shape, 52 ASCII + 1,130 NFKC→ASCII; `&#x99;` → ™ → "TM"; identical 3.12/3.13) | detector_probe12 |
| C8 | a refused request writes nothing | VERIFIED 348/348 per Python | file sha256 + log length + ledger count |
| C9 | request_sha = kept-text hash; replay / 409 | VERIFIED: 187/187 unit lines kept-body hash, 0/165 raw; 561 + 1,209 replays, 0 duplicate lines/events; other kept text 409 (187/187, 208/208); difference only inside a redacted cluster → replay (by design) | pii_persist12, xv_idem12 ×13, idem_collapse12 22×2×2 |
| C9a | kept text / hash stable 3.12 ↔ 3.13 | VERIFIED: 1,112,032 code points × 2 templates 0 differences; real-service cross-Python 93/93 hashes, 0 status changes, both directions | xver_kept12, xv_idem12 READ_PY |
| C10 | names_a_card same set as 99569bd | VERIFIED 78,112/78,112 + 38/38, both Pythons | funds_decide, funds_service |
| C11 | f5ee168 / 0f5fbe3 / 91ea9a1 / 75b0662 logs start and replay at 1a90498 | VERIFIED (state diff [], 94/94 anchors, 0 duplicates); retry-across-upgrade 409 counts 5|3 / 6 / 8 / 8 of 93 | xv_idem12 |
| C12 | redacted value not recoverable via request_sha | VERIFIED: 8 searches not found; positive control at 0f5fbe3 found in 1.03 s | request_sha_recover3 |
| C13 | start-up read refusals | VERIFIED unchanged, 6 cases ×2; device NOT RUN | start_read |
| C14 | false positives measured, both Pythons | MEASURED: 48/86 free text (f5ee168 43/86), 22/86 approval refs (22); +5 all non-Latin-script lines; 3.12 = 3.13 | fp_corpus12 |

convergence: n/a (no schema or log-format migration in the delta; earlier logs start with state diff [], C11). greenfieldHash/brownfieldHash: not applicable.
idempotency: [{path: unit inventory add, 187 spellings ×2 Pythons, replays: 561, duplicateEffects: 0}, {path: full lifecycle 93 requests, 13 A→B runs, replays: 1,209, duplicateEffects: 0}, {path: same request_id other body, replays: 187 + 208 + 22×4, duplicateEffects: 0}].

## Findings
- **D12-H1 HIGH (BLOCKING) — card and SSN numbers written in non-decimal Unicode digits are stored in clear (regression from f5ee168).** pii.py:261 counts only ASCII `0`–`9` as cluster digits (`elif "0" <= ch <= "9":`; f5ee168 had `elif ch.isdigit():`). strip_format (pii.py:103) folds only category Nd, so the 69 `isdigit()` characters of category No that NFKC does not map — DOUBLE CIRCLED DIGIT ONE–NINE ⓵–⓽, NEGATIVE CIRCLED DIGIT ZERO ⓿, DINGBAT (NEGATIVE) CIRCLED (SANS-SERIF) DIGIT ❶–❾ ➀–➈ ➊–➒, ETHIOPIC DIGIT ፩–፱, KHAROSHTHI, RUMI, BRAHMI NUMBER ONE–NINE, NEW TAI LUE THAM DIGIT ONE — no longer count, and the card/SSN rules never read them. Measured: 35/35 card/SSN spellings in 7 such sets detected at f5ee168, **0/35 at 1a90498** (both Pythons); through the service on the real ledger, 6/6 spellings (incl. `⓵⓶⓷-⓸⓹-⓺⓻⓼⓽`, which omega11 verified with 0 leaks at f5ee168): unit vendor_text 201 **redacted:false, the number in clear** in ooh_log.jsonl, /audit/export and the unit view; face attribute **200, stored**; pending.line and pending.discarded hold it and it is rolled into the log (approval ref refused only by its ASCII schema). The log is append-only and anchored, with no scrub path (INFO-3). None of the founder decisions after r11 narrows which characters are digits (ADR 0022:357-391 changes only what *ends* a cluster); the candidate's own test oracle counts `ch.isdigit()` (test_ooh2_pii1_cluster_rule.py:39, :113), so the property it states fails on these inputs; the fuzz alphabet holds no No-category digit.
  - Reproducer: `python -I poc/digit_forms_cards12.py <tree-1a90498>` → `"detected": 0` (f5ee168 tree: 35); persistence: `SECRETS_ONLY="⓸⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵" python -I poc/pii_persist12.py <ledger-bin> <fresh-dir> <tree-1a90498>` → unit `stored_redacted_flag` false, `leaks` [ooh/ooh_log.jsonl:clear, …], face status 200.
  - Falsifier: the same runs show 35/35 detected and redacted:true / 422 with 0 leaks; or a founder ruling that only decimal (Nd) digits count — then it becomes a documented limit (LOW), not a regression.
  - Remedy direction (Security Engineering): count `ch.isdigit()` (or fold `unicodedata.digit()` values in strip_format) in number_cluster_spans; add No-category digits to the fuzz alphabet.
- **D11-L2 — CLOSED for entities and look-alike letters.** All 15 omega11 letter/markup spellings in scope of the new rule (`&nbsp;`, look-alike letters) now 0 leaks on all paths; tags (`<br>`, `<p>`, …), `&lt;br&gt;` and ASCII-letter splits remain stored by design (64/75 cases; ADR 0022:386).
- **D11-L1 — CLOSED (documented).** ADR 0022:371-372 and service.py:89-93 now describe the retry-across-upgrade 409; re-measured: 5 (3.12) / 3 (3.13) of 93 from f5ee168 logs (unassigned characters, look-alike clusters, entity-spelled numbers), 6 from 0f5fbe3, 8 from 91ea9a1/75b0662; nothing written, unit exists once.
- **D12-L1 LOW (non-blocking) — HTML decoding stops at three levels.** `4111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111` (four levels) is stored as typed / face 200; the number shows only after a fourth decoding (pii.py:315-320 `for _ in range(3)`). Low realism. Reproducer: `SECRETS_ONLY="4111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111" python -I poc/pii_persist12.py …` → redacted:false, leaks `:html`. Falsifier: redacted:true, 0 leaks (decode to a fixed point).
- **M-3 MEDIUM (unchanged, open)** and **M-2 MEDIUM (unchanged, open)** — as omega11; reproducers poc/m3_stale_delete.py (shorten_then_restore), poc/targeted.py (B_consistent_rewrite_inflated_funds); falsifiers: after restore the line is applied / hub read 503 or flagged while integrity.ok is false.
- INFO-7 — "unassigned kept as typed" makes the hash Unicode-version-independent only while new versions assign no digits: 3.12 vs 3.14 (Unicode 16.0; not a declared target) differ on 106 code points (80 new Nd digits, 26 So with digit NFKC). No current impact (production python:3.12-slim).
- INFO-8 — ADR 0022:362 still says unassigned characters are "removed" by screen_form (contradicts :389-391 and the code).
- INFO-9 — non-Latin-script text: the ASCII-only cluster end makes most dates/addresses with ≥9 digits in Japanese/Russian/Greek/Arabic text redacted/refused (5 of 10 realistic lines newly; §4). Accepted FP class, reported for the founder.
- OOH2-PII-1-FP (accepted) — §4.

## NOT RUN
- Device node at the log path (mknod EPERM, non-root). L-A (h1_stale_report) and the INFO-3 old-line recovery not re-run (code unchanged; not part of the asked short re-run). No shared/staging/production datastore. Isolation: one disposable data dir + one ledger process/port/file per case under scratchpad/data12run; proves exclusivity only, not tenant RLS (not in the delta). Multi-character separators: corpus only. Non-ASCII-digit sweep covers single repeated characters and 7 digit sets in 5 shapes, not mixed scripts. Other has_personal_data fields (campaign names, ref attributes) and vendor_text writers other than unit add: code inspection only (all call `observed()` / the same screen). Concurrency (AEGIS-RELIABILITY). Windows/Linux filesystems. Ledger build provenance. Python 3.14 only at unit level.
- Candidate suite on my archive (context only; passing ≠ property proven): 1190 passed on 3.12 and 1190 on 3.13 (out/pytest-1a90498-3.1x.txt).

## Verdict
blocking: true. The founder decisions work as specified where measured: every single-code-point separator miss is an ASCII letter or normalises to one (both Pythons); HTML-entity and look-alike-letter spellings are redacted/refused with 0 leaks; refused requests write nothing (348/348); request_sha is the kept-text hash, replays/409s verified, unrecoverable (control found in 1.03 s); kept text and hash are identical on 3.12 and 3.13; earlier logs start and replay; H-1, start-up, funds unchanged; M-2/M-3 open MEDIUM unchanged. But the same commit narrowed what counts as a digit, and card/SSN numbers written in non-decimal Unicode digits — one of which omega11 verified as redacted at f5ee168 — are now persisted in clear in the anchored log, pending files, export and views, and accepted in the face attribute (D12-H1 HIGH).

VERDICT: BLOCK
