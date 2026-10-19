# AEGIS-DATA (Gate 5) — omega13
- repo: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: cb708b924bb01aa5543b832573a8fa56bd041b80
- treeOid: eba95097622438b226169c88910c81cc9a999212 (`git rev-parse cb708b9^{tree}` in the repo: matches)
- scope: persistence/data integrity of `git diff 1a90498 cb708b9 -- services` (ooh-py: pii.py + 2 tests; ledger-rust unchanged) against OOH2-PII-1 and the founder decisions (three fields; ASCII-letters-only cluster end; entities decoded to a fixed point, both forms screened; unassigned kept; Luhn/SSN label only; hash of redacted text; FPs accepted; URL/JS/CSS escapes = documented limit OOH2-R12-SEC-S2); D12-H1/D12-L1 re-run; funds/H-1/start-up/M-2/M-3 short re-runs
- author: AEGIS-DATA (AG-AEGIS-DATA v1.0.0), fresh context, independent of builder, read-only on candidate; Claude Opus 5.5 subagent; 2026-10-18
- method: own `git archive` trees under scratchpad/data13run/trees (t-cb708b9 `diff -r -x __pycache__` vs frozen review copy: empty) + disposable data dirs + own ledger-rust build; no shared/real datastore. omega12 PoCs copied to omega13/data/poc as a starting point; every result below is from this run.
- raw/PoCs: scratchpad/aegis/omega13/data/{poc,out}; run state (data dirs, ledger files, per-code-point rows) in scratchpad/data13run (outside omega13)

## Environment
- ledger: `services/ledger-rust` identical 1a90498 → cb708b9 and 99569bd → cb708b9 (`git diff --quiet` rc 0); built by me from t-cb708b9, `cargo build --release --offline`, SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk, CARGO_TARGET_DIR=data13run/ledger-target → `server` sha256 aab060dba6a8016cacc0c68ade4619227c3531e3ed7ff772de15440091c3f2a2 (path-dependent; not a provenance claim). One ledger process/port/file per case.
- service: in-process (helpers.Harness + real HttpLedgerClient over HTTP) from my archives t-cb708b9 / t-1a90498 / t-f5ee168 / t-0f5fbe3 / t-91ea9a1 / t-75b0662 / t-99569bd; Python 3.12.14 (unidata 15.0.0) and 3.13.9 (unidata 15.1.0).

## 2. Single-code-point separator sweep and digit sweep, new rule (poc/detector_probe13.py, digit_forms13.py, digit_forms_cards12.py → out/detector-probe-cb708b9-3.1x.txt, detector-probe-compare.txt, digit-forms-*.txt, digit-forms-compare.txt, digit-forms-cards-*.json; per-code-point rows data13run/dp-*.json, df-*.json)
Separator population (wider than omega12's U+0020..U+2FFFF): every code point **U+0020..U+10FFFF**, not a surrogate, not digit-valued (`isdigit()` or `unicodedata.digit/decimal` defined) = **1,111,224** per shape, as the single separator of SSN `123?45?6789`, card `4111?1111?1111?1111`, `123?456?789`, and `123?456?789` with the separator spelled `&#xHHHH;`, `&#NNNN;`, and as UTF-8 URL escapes `%HH` (documented limit), through `pii.screen_free_text`. Hits re-checked with my own oracle (digit-valued characters joined by non-ASCII-letters) on every form of the kept text (kept, screen_form, _norm, NFKC, and those of the text HTML-decoded to a fixed point).
| shape | misses 3.12 (15.0) | misses 3.13 (15.1) | ASCII letter | NFKC → ASCII letter | URL-escape limit | OTHER |
|---|---|---|---|---|---|---|
| SSN 3-2-4 | 1,182 | 1,182 | 52 | 1,130 | – | **0** |
| card 4-4-4-4 | 1,182 | 1,182 | 52 | 1,130 | – | **0** |
| 3-3-3 | 1,182 | 1,182 | 52 | 1,130 | – | **0** |
| 3-3-3, `&#xHHHH;` | 52 | 52 | 52 (decoded A-Z a-z) | 0 | – | **0** |
| 3-3-3, `&#NNNN;` | 0 | 0 | – | – | – | **0** (the entity's own decimal digits join the cluster) |
| 3-3-3, `%HH` | 1,079,256 | 1,079,256 | 52 | – | 1,079,204 | **0** |
- Miss sets identical across SSN/card/3-3-3 and **identical 3.12 vs 3.13** (code-point lists equal, all 6 shapes). Categories of the 1,182: Ll 412, Lu 398, So 268, Lm 65, Nl 32, Lt 4, Lo 2, Sc 1 (same as omega12, now over all 17 planes). Hits whose kept text still holds a cluster: **0** (both Pythons).
- `&#xHHHH;` vs raw: the 1,130 characters that only NFKC-map to an ASCII letter (ª, ʰ, ǅ, ⓐ, ™ …) end a cluster when typed, but **join** it when spelled as an entity: the backstop `_still_holds_personal_data` reads the decoded, not-yet-NFKC form (pii.py:339-353), so `123&#xAA;456&#xAA;789` → "[redacted]" while `123ª456ª789` → kept as `123a456a789`. Fails closed (over-redaction) → INFO-10.
- URL escapes: 1,079,204 separators kept as typed (their `%HH` hex letters end the cluster) = documented limit OOH2-R12-SEC-S2, reported not blocking; `%20`-like escapes whose hex is all decimal digits join the cluster and are redacted.

### 2a. Every digit-valued code point as a digit
Population: every code point U+0020..U+10FFFF, not ASCII 0-9, with `isdigit()` or `unicodedata.digit()/decimal()` defined = **798** (Nd 670, No 128), identical on both Pythons with **identical digit values** (798/798). Shapes per character c (inside `contact … for access`): `ccccccccc`, `c c c …`, `c-c-c-…`, `12345678c`, card `cccc cccc cccc cccc`, SSN `ccc-cc-cccc`.
| tree / Python | detected in all 6 shapes | not detected |
|---|---|---|
| cb708b9 3.12 | **798/798** | 0 |
| cb708b9 3.13 | **798/798** | 0 |
- screen_form of each is its ASCII digit (769) or NFKC digit+punctuation (`⑴`→"(1)", `⒈`→"1.", `🄂`→"1,": 29), never lost.
- Numeric-only characters (numeric value 0-9 but no digit value: Hangzhou/Roman/Cuneiform Nl 148, Lo CJK numerals 58 (65 on 3.13, CJK Ext I), No 233 e.g. Mayan numerals, Telugu fraction digits, Aegean numbers): **439** (3.13: 446); run of 9 detected 2 (U+215F, U+2189: NFKC → digits). Outside the rule (not digits; "spelled-out digits" limit, pii.py docstring) — reported, not a finding.
- D12-H1 cards/SSNs (7 non-decimal sets × 5 shapes, omega12 PoC unchanged): **cb708b9 35/35 detected** (labels card/ssn), 1a90498 0/35, f5ee168 35/35 — both Pythons (out/digit-forms-cards-*.json).
→ **C7a VERIFIED: every single-code-point miss is an ASCII letter, normalises to one, or is the documented URL-escape limit; every digit-valued code point counts as a digit (both Pythons).**

## 1. OOH2-PII-1 — persisted bytes, five paths, real ledger (poc/make_secrets13.py → poc/secrets13.json; poc/pii_persist13.py (= omega12's, unchanged), pii_summary13.py → out/pii-cb708b9-3.1x.{json,summary.txt})
Corpus: omega12's 187 spellings + 58 new = **245 distinct spellings × 5 paths** (unit vendor_text; face text attribute `location_description`; client_approval_doc_ref; vendor_text in pending.line after a failed append; in pending.discarded after a kill between pending write and anchor) = **1,225 cases per Python, 0 errors; summaries byte-identical 3.12 = 3.13**. New: 14 non-decimal-digit card/SSN spellings (7 sets: ⓵ ❶ ➀ ➊ Ethiopic Brahmi Rumi, + Kharoshthi, New Tai Lue, ⓿), 4 mixed-script digit spellings (ASCII + ⓵ + ❶ + Arabic-Indic + full-width + math-bold), 3 entity-spelled dingbat digits (incl. `&amp;#x2776;`), 10 deep entities (4, 5, 8, 15, 16, 17, 20 levels; >16 levels → whole value redacted), 4 decimal-entity separators, 5 fixed-point spellings (hidden char + combining mark), 7 multi-character separators, 8 URL/JS/CSS-escape spellings, 2 escape spellings whose hex is decimal, 2 entity-spelled letter-like separators. Each row labelled by my implementation of the rule (make_secrets13.py `expect`, identical output on both Pythons): **detect** (NFKC raw or fully-decoded form holds 9+ digit-valued characters not split by an ASCII letter) 221, **design_miss** (split by ASCII letters) 17, **escape_limit** (only URL/JS/CSS decoding shows the number) 7. Leak scan (omega12's): clear/NFKC/JSON-escaped/fully-decoded forms and the digit string inside any ASCII-letter-free run (digit values folded), over every file of the data and ledger dirs, /audit/export, /audit/evidence and the views.
| expectation / class | spellings | vendor_text 201 redacted | face attribute | approval ref | pending.line / .discarded | leak cases |
|---|---|---|---|---|---|---|
| detect: omega9–omega11 classes (D9-H1, D10-H1, D10-M1, new-cluster, wide-sep, unicode-space, hidden, digit forms, look-alike shapes, cards, letter-sep re-classed; without `⓵⓶⓷-…`) | 116 | 116/116 | 116 422 PD, nothing written | 116 422, nothing written | 0 | **0** |
| detect: r12 look-alike letters (17), entities (19), markup without letters (3), unassigned separators (4), @-spellings (6) | 49 | 49/49 | 49 422 PD | 49 422 | 0 | **0** |
| **detect: D12-H1 non-decimal digits (omega12's 6 incl. `⓵⓶⓷-⓸⓹-⓺⓻⓼⓽` + r13 14)** | **20** | **20/20** | 20 422 PD | 20 422 | 0 | **0** (omega12 at 1a90498: 24/30 leaked) |
| detect: **D12-L1 four-level entity** (r12-entity-deep) | 1 | 1/1 | 422 PD | 422 | 0 | **0** (omega12: 4 leaks) |
| detect: r13 deep entities 4–20 levels (10), mixed scripts (4), entity digits (3), decimal-entity separators (4), fixed-point (5), multi-char separators (7), digit-hex escape (1) | 34 | 34/34 | 34 422 PD | 34 422 | 0 | **0** |
| **detect total** | **221** | **221/221** | **221/221 refused** | **221/221 refused** | 442 files present, 0 leaks | **0 / 1,105** |
| design_miss: split by ASCII letters (`123a45a6789`, `ⓐ ａ ᴬ ™`→ASCII, `<br>` `<p>` `<b>` `<span>`, `&lt;br&gt;`, …) | 15 | 0/15 (by design) | 15 200 | 11 schema 422, 4 accepted | stored | 64/75 (by design) |
| design_miss but redacted: entity-spelled letter-like separators `123&#xAA;456&#xAA;789`, `123&#x2B0;45&#x2B0;6789` (INFO-10) | 2 | 2/2 redacted | 2 422 PD | 2 422 | 0 | 0 |
| **escape_limit** (OOH2-R12-SEC-S2): `4111%2D…`, `123%2F45%2F6789`, `123%E2%80%9145…`, `4111\x2d…`, `123\u002d45…`, `123\2d 45…`, `4111\u00a0…` | 7 | 0/7 stored as typed | 7 200 | 7 schema 422 | stored | 28/35 (documented limit, not blocking) |
| detect (counted in the 221): `4111%C2%A01111…` (the escapes' decimal hex digits join → 9+ digits) | 1 | 1/1 redacted | 422 | 422 | 0 | 0 |
- **Expected-detect totals: 221/221 spellings, 1,105/1,105 cases with 0 leaks, both Pythons.** The 6 D12-H1 spellings that leaked on 4 paths at 1a90498 are now redacted/refused on all 5; the D12-L1 four-level entity (and 5…16 levels) is redacted; 17- and 20-level nesting is redacted whole (`_MAX_DECODE` = 16).
- Refused requests: **464/464 nothing written** per Python (face 223, approval 241; log length, ledger event count and every file sha256 unchanged; REFUSED_BUT_WRITTEN []), control write after each OK.
- Unit replays: 245 × 3 = 735 → 0 duplicate lines, 0 duplicate ledger events, units == 1 each; same request_id + other text → 409 245/245.
- request_sha on every unit line = hash of the body with the **kept** text: 245/245; hash of the raw body on a redacted line: 0/223 (223 redacted lines: 221 detect + 2 INFO-10). Labels on redacted lines: ssn 104, card 67, number 54, email 1.

### 1c. request_sha recovery (poc/request_sha_recover3.py → out/request-sha-recover-cb708b9.jsonl, out/request-sha-recover-control-0f5fbe3.json, out/pii-control-0f5fbe3-3.12.json)
| log | spelling / separator | search | result |
|---|---|---|---|
| **control: 0f5fbe3**, my run this round | `123  45  6789` | SSN, area 123 known | **found in 0.95 s** (456,790 tried) |
| cb708b9 unit | `123-45-6789`, `123  45  6789`, `123456789`, `123ー45ー6789`, `⓵⓶⓷-⓸⓹-⓺⓻⓼⓽` (D12-H1) | 10^6 each | **not found** ×5 |
| cb708b9 unit | `4111 1111 1111 1111`, `⓸⓵⓵⓵ ⓵⓵⓵⓵ …` (D12-H1), `4111&amp;amp;amp;nbsp;…` (D12-L1) (BIN6 + last4 known) | 10^5 Luhn candidates each | **not found** ×3 |
| cb708b9 face / approval | any detected spelling | — refused, no line written | n/a |
→ C7 **VERIFIED** (D12-H1, D12-L1 closed), C8 VERIFIED, C12 VERIFIED.

## 3a. Kept text and request hash across Unicode versions (poc/xver_kept12.py, xver_compare12.py, xver_email13.py, xver_email_compare13.py → out/xver-cb708b9-compare.json, xver-upgrade-1a90498-to-cb708b9-3.12.json, xver-email-*-312v313.json; rows in data13run)
Population: every code point U+0020..U+10FFFF except surrogates = **1,112,032**; templates `faces west X lit`, `123X456X789` (request hash = payload_sha256 of `{request_id, vendor_text: kept}`, service.py `_screened` formula, stdlib re-implementation — equivalence on real service lines in §3b) and, new, the R12-2 shapes `write a@bXd.com`, `mail joXe at example.com`, `call X+500 21234` (kept-text hash + kinds).
| comparison | Cn in 15.0 assigned in 15.1 | hash differs (faces / cluster) | kinds differ | e-mail/phone templates differ |
|---|---|---|---|---|
| cb708b9 3.12.14 vs 3.13.9 | 627 (Lo 622, So 5) | **0 / 0** | **0** | **0 / 0 / 0** |
| 1a90498 3.12 vs 3.13 (for contrast) | 627 | (omega12: 0 / 0) | – | **622 / 622 / 622** (R12-2: `\w` follows the build's Unicode database) |
| upgrade 1a90498 → cb708b9 (3.12) | – | **69** / 0 | 69 | – |
- **3.12 ↔ 3.13 at cb708b9: stable for all 1,112,032 code points in all 5 templates** (R12-2 version dependence of the e-mail/phone shapes closed: 622 → 0).
- Upgrade: the 69 differing code points are exactly the non-decimal digits of D12-H1 (all digit-valued): `faces west ❶ lit` is kept as `faces west 1 lit` now (was as typed) → new hash → a retry across the upgrade of such a request answers 409 (§3b). The cluster template differs only in its label (number → phone); hash identical ("[redacted]").
- INFO-7 (3.14/Unicode 16 assigns new digits) not re-run: unchanged mechanism; production python:3.12-slim.

## 3b. request_sha, replays, logs written by earlier versions (poc/xv_idem13.py → out/xi-*.json; poc/idem_collapse13.py → out/idem-collapse-*.json)
Writer A: tenant, 18 units (omega12's 14 + `panel ❶ lit, bay ❷` (non-PII non-decimal digits), `contact ⓸⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵ ⓵⓵⓵⓵ for access` (D12-H1), `contact 4111&amp;amp;amp;nbsp;… for access` (D12-L1), `write a@b\U0002EBF0d.com` (R12-2)), face attribute, campaign → authorized → submitted PO: **105 recorded 2xx requests**. Reader B = cb708b9 on the same data dir + ledger file (real ledger): forced integrity, state diff, replay all 105, 20 same-request_id/other-body retries, one new write, restart, ledger readback.
| A → B (Python) | start / integrity / state diff | A's vs B's request_sha | replays: original status / **409** | lines added (replays + retries) | other-body retries 409 | new write, restart, anchors==lines |
|---|---|---|---|---|---|---|
| cb708b9 → cb708b9 (3.12; 3.13) | ok / ok / [] | 105/105 | 105 / 0 | 0 | 20/20 | 201, ok, 106/106, 0 problems |
| **cb708b9 3.12 → cb708b9 3.13**, and 3.13 → 3.12 | ok / ok / [] | **105/105** | 105 / **0** | 0 | 20/20 | 106/106 |
| 1a90498 → cb708b9 (3.12) | ok / ok / [] | 101/105 | 101 / **4**: `panel ❶`, D12-H1 card (in clear at 1a90498, redacted now), 4-level entity card (D12-L1), CJK Ext I e-mail (not an address on 1a90498 3.12) | 0 | 20/20 | 106/106 |
| 1a90498 → cb708b9 (3.13) | ok / ok / [] | 102/105 | 102 / **3** (the e-mail was already an address on 1a90498 3.13) | 0 | 20/20 | 106/106 |
| f5ee168 → cb708b9 (3.12 / 3.13) | ok / ok / [] | 98 / 100 of 105 | **7** / **5**: U+2FFC, U+2EBF0 (3.12 only), U+0378, look-alike cluster, entity card, `panel ❶`, 4-level entity card | 0 | 20/20 | 106/106 |
| 0f5fbe3 → cb708b9 (3.12 / 3.13) | ok / ok / [] | 95 / 96 | **10** / **9**: omega12's 6 + `panel ❶`, D12-H1 card, 4-level entity, e-mail (3.12) | 0 | 20/20 | 106/106 |
| 91ea9a1 → cb708b9, 75b0662 → cb708b9 (3.12; 3.13) | ok / ok / [] | 93 | **12**: those 10 + card + `123.45.6789` | 0 | 20/20 | 106/106 |
- Logs written at 1a90498, f5ee168, 0f5fbe3, 91ea9a1, 75b0662 **start** (integrity ok, state diff []) and **replay** at cb708b9 with **0 duplicate lines/events in all 14 runs (1,470 replays, 280 other-body retries; readback anchors == lines 106/106, 0 problems)**.
- **Cross-version stability 3.12 ↔ 3.13 at cb708b9: 105/105 hashes equal, 0 status changes, both directions.**
- Retry-across-upgrade 409s (nothing written, unit exists once) of 105: 1a90498 4|3, f5ee168 7|5, 0f5fbe3 10|9, 91ea9a1 12, 75b0662 12 (3.12|3.13). New this round in that population: non-PII text with non-decimal digits (kept as ASCII digits now), and texts whose screen changed (D12-H1, D12-L1, R12-2). Same documented mechanism (ADR 0022:371-372, D11-L1 closed); unreleased service.
- A 1a90498-written log keeps the D12-H1 card **in clear** in its line after the upgrade (append-only, no scrub path — INFO-3); 1a90498 was never released (only matters for disposable test data).
- Same version, same request_id, other vendor_text (28 pairs; 3.12 = 3.13 json-equal at cb708b9 (at 1a90498 not: the e-mail pair); 0 lines added by any retry; 1 unit each). Changes vs 1a90498:
| first → retry | cb708b9 | 1a90498 |
|---|---|---|
| `⓸⓵⓵⓵ …` → `…⓶` (D12-H1); `4111&amp;amp;amp;nbsp;…` → `5555 5555 5555 4444`; `a@b\U0002EBF0d.com` → `c@d…e.com` | **201 replay** (both redacted; by design) | 409 |
| `panel ❶ lit` → `panel 1 lit` / `panel ⓵ lit` | **201 replay** (same kept text `panel 1 lit`; by design: kept text = screen_form) | 409 |
| `call 📞+500 21234` → `…21235` | **409** (kept in clear: D13-L1) | 201 replay (redacted phone) |
| `123&#xFF06;nbsp;45…6789` → `…6780` | 409 (kept as typed: D13-L2) | – |
| omega12's 22 pairs | identical to 1a90498 except the D12-H1 pair above | – |
→ C9, C9a, C11 VERIFIED.

## 4. False positives, both Pythons (poc/fp_corpus12.py, fp_summary12.py, fp_supp13.py → out/fp-corpus-{cb708b9,1a90498}-3.1x.json, out/fp-summary-3.1x.txt, out/fp-supp-*.json)
omega12's 86 realistic OOH texts through the service (helpers.Harness, real data dir) in the three fields, at cb708b9 and 1a90498 (both re-run by me), both Pythons:
| class | n | vendor_text redacted cb708b9 (1a90498) | face refused PD | approval ref refused PD |
|---|---|---|---|---|
| address / zip+4 / flight-date / gps / price / impressions / permit / ref / spec-time | 64 | 34 (34) | 34 (34) | 20 (20) |
| phone/contact (true positives) | 3 | 3 (3) | 3 (3) | 2 (2) |
| non-Latin script / HTML entity / mixed | 19 | 11 (11) | 11 (11) | 0 (0) |
| **total** | **86** | **48 (48)** | **48 (48)** | **22 (22)** |
- **No change vs 1a90498** (fp_summary CHANGED []), summaries byte-identical 3.12 = 3.13.
- Supplement for this delta (17 lines, unit level, both trees, both Pythons; cb708b9 3.12 = 3.13): redacted **8/17 at cb708b9 vs 6/17 at 1a90498**. New: `panels ❶❷❸❹❺❻❼❽❾ north side` → `panels [redacted] north side` (nine numbering digits); `unit ⓵ 2026-11-01 to 2026-11-30` → `unit [redacted] to 2026-11-30` (⓵ joins the date). Kept text changes for non-PII lines (`Bay ❶ 14x48` → `Bay 1 14x48`, `No.❶ Wilshire` → `No.1 Wilshire`, `፩፪፫ lot` → `123 lot`); `Brand&trade; 2026 11 01 - …` kept decoded as `BrandTM [redacted]` (was `Brand&trade; [redacted]`). Measured, accepted (OOH2-PII-1-FP).

## 5a. Funds: names_a_card 99569bd vs cb708b9 (poc/funds_corpus.py, funds_decide.py, funds_service.py → out/funds-corpus13.json, funds-decide-*.json, funds-decide-sha256.txt, funds-service-*.json)
| level | population | 99569bd | cb708b9 | rows differing |
|---|---|---|---|---|
| unit (names_a_card, funds_ref_parts, funds_ref_key) | **78,154** refs (my generation from both trees' test strings 78,118 ∪ omega12's 78,112 ∪ 6 r13 refs: dingbat/double-circled/Ethiopic digits, entity card, URL-escape card, emoji+intl phone) | output sha256 75d16f36990dad6b…, refused-as-card 9,128 | **same file sha256** | **0** (all 4 files, 3.12 and 3.13, one sha256) |
| service (38 refs, real data dir) | status, detail, lines, events, ref_in_log | sha256 0e2e7d2d663277ac… | same (all 4 files one sha256; = omega12's) | 0 |
- names_a_card reads `pii.card_spans(NFKC(value))` (svc_campaigns.py:169) with `_DIGIT_RUN = [0-9]+…` (pii.py:155): the strip_format digit folding and the `_INTL` change do not reach it. `has_personal_data` (campaign names svc_campaigns.py:279, `ref` attributes svc_inventory.py:75) does read strip_format (wider: non-decimal digits now count) and `_INTL` (narrower, D13-L1) — but both fields have ASCII-only schemas without `+` (models.py:56 `^[A-Za-z0-9 ._&'-]{2,120}$`; `_REF`), so neither change is reachable there (intl-diff-3.12.txt: 174,774 of 583,581 unit-level `has_personal_data` cases change, all with a non-ASCII preceding character).
→ C10 VERIFIED.

## 5b. H-1 matrix and start-up read, short re-run (poc/rl_omega9.py, h1_summary.py, start_read.py → out/h1-cb708b9-*, out/start-read-cb708b9-*)
- H-1: 66 cases per Python on my ledger build. Per-case summary at cb708b9 3.12 **byte-identical** to cb708b9 3.13 and to omega12's 1a90498 and f5ee168 summaries (`cmp` rc 0 ×3). okWhileChanged=True **0/66** per Python; duplicate event ids 0. The 20 summary "FAIL" flags are the same known 20 (refused before any anchor; deleted → write-path open re-creates a 0-byte file; restart-on-deleted/shortened = M-3).
- Start-up read: control STARTED + write (2 ledger events); symlink → identical copy / dangling, unix socket → REFUSED "cannot be opened (OSError)"; FIFO, directory → REFUSED "not a regular file"; refusals change no data-dir entry, add 0 ledger events, and after the regular file is put back start with integrity ok, state diff []. Start messages equal to omega12's 1a90498 output case by case, both Pythons. Device node NOT RUN (mknod EPERM, non-root).
→ C1–C3, C13 VERIFIED unchanged.

## 6. M-2, M-3 (poc/m3_stale_delete.py, targeted.py → out/m3-*, out/targeted-*)
- Delta 1a90498→cb708b9 under services = 3 files (pii.py, 2 tests); store.py / svc_* / ledger-rust untouched.
- **M-3: UNCHANGED, open MEDIUM.** Output byte-identical cb708b9 = 1a90498 (my run) on 3.12 and 3.13 and = omega12's (sha256 prefix 8bcb404bdc662a41 ×5): shorten_then_restore → pending.line deleted at restart 1 ("the ledger holds anchors beyond the local log"), inflight not applied, write 503, anchors/lines 14/13.
- **M-2: UNCHANGED, open MEDIUM.** targeted output json-equal cb708b9 = 1a90498 (both Pythons) = omega12's: B_consistent_rewrite_inflated_funds → STARTED, health degraded, write 503, **hub read 200 hub_funds_received 99999.00**, no marker.

## 7. New in this delta: phone shape and composition order (poc/intl_diff13.py, intl_free13.py, composition13.py → out/intl-diff-3.12.txt, out/intl-free-3.12.txt, out/pii-d13l1-*-3.12.json, out/composition13-*.txt)
- **R12-2 `_INTL` lookbehind** (pii.py:80): 1a90498 `(?<![\w+])\+…` → cb708b9 `(?<![A-Za-z0-9_+])(?<![^\x00-\x7f\s])\+…`. The comment at pii.py:52-54 says the new class is "wider than `\w`: fails closed" — true for the e-mail shapes, where `_W` is a positive class, but in a **negative lookbehind** a wider class fails **open**: an international number directly after any non-ASCII non-space character is no longer a phone.
  - Free-text screen, every preceding code point U+0021..U+10FFFF (1,112,031), `call X+500 21234 for access` / `call X+1 555 0142 for access` (8 digits in all): redacted at 1a90498 974,321, at cb708b9 139,165 → **835,225 preceding characters lose detection** (9,880 assigned: So 5,751 (emoji, ☎ ✆ …), Mn 1,722, Sm 924, Po 568, Mc 452, Sk 120, Lo 89, Ps/Pe 130, Sc 58, Pd 23 (— –), Pi/Pf 22, …; Cn 825,345); 69 gained. `+44 20 7946 0958` (12 digits): 0 change — the 9-digit cluster rule covers every number of 9+ digits.
  - Persisted (real ledger, cb708b9 vs 1a90498, 3.12): `faces west, contact 📞+500 21234 for access` and `…—+500 21234…`: **cb708b9 unit 201 redacted:false, number in clear in ooh_log.jsonl, /audit/export and the unit view; face attribute 200 stored; pending.line and pending.discarded hold it** (approval ref: ASCII schema 422). 1a90498: redacted ['phone'] / 422 PD, 0 leaks. Control `+500 21234` (space before): redacted/refused, 0 leaks, both trees. Same-request_id retry `…21234` → `…21235`: 409 at cb708b9 (kept in clear) vs 201 replay at 1a90498.
  - Stable across 3.12/3.13 at cb708b9 (§3a `call X+500 21234` template: 0 differences). → **D13-L1 LOW.**
- **Composition order** (both trees, both Pythons, out/composition13-3.12.txt): `123&#xFF06;nbsp;45&#xFF06;nbsp;6789`, `123&#xFE60;nbsp;45…` (entity of a full-width / small ampersand) and `123&&#x200B;nbsp;45&&#x200B;nbsp;6789` (entity of a zero-width space inside an entity) are **kept as typed**; the number only shows after decode → NFKC / hidden-character removal → decode again. In every form the rule names (raw normalised; decoded to a fixed point, then normalised) the digits stay split by the ASCII letters `nbsp`, so this is outside the rule as worded; 7 sibling spellings (full-width `＆ｎｂｓｐ；`, hidden character inside an entity, soft hyphen, entity-spelled full-width digits, `&#37;40`) are redacted. Not a regression (same at 1a90498). → **D13-L2 LOW.**
- screen_form fixed point: adversarial stacks of ZWJ + combining marks + soft hyphens (44 inputs) are stable after 1 further call at cb708b9 (1a90498: 2).

## Checks
| id | area | result | method |
|---|---|---|---|
| C1–C3 | H-1 write path / never ok while changed / recovery | VERIFIED unchanged | rl_omega9 66×2 real ledger; summary `cmp`-identical to omega12's 1a90498 and f5ee168 |
| C5 | M-3 | UNCHANGED (open MEDIUM) | m3_stale_delete byte-identical cb708b9 = 1a90498 ×2 Pythons = omega12 |
| C6 | M-2 | UNCHANGED (open MEDIUM) | targeted json-equal ×5 |
| C7 | OOH2-PII-1: no 9+ digit cluster not split by ASCII letters in any persisted byte | **VERIFIED**: 221/221 detect spellings × 5 paths = 1,105 cases, **0 leaks**, both Pythons (incl. the 6 D12-H1 spellings that leaked at 1a90498, 13 more non-decimal-digit spellings, D12-L1 and 4–20-level nesting); escape-limit spellings stored as documented (28/35) | pii_persist13 1,225×2 real ledger |
| C7a | separator sweep / digit sweep | VERIFIED: 1,111,224 separators × 6 shapes ×2: every miss an ASCII letter (52), NFKC → ASCII letter (1,130) or URL-escape limit; 0 OTHER; 0 kept-bad. 798/798 digit-valued code points detected in 6 shapes ×2 | detector_probe13, digit_forms13 |
| C7b | phone rule unchanged outside the cluster rule | **MISMATCH (LOW)**: 8-digit international numbers after a non-ASCII non-space character no longer detected; persisted in clear on 4 paths (D13-L1) | intl_free13, pii_persist13 (3 spellings × 5 paths × 2 trees) |
| C8 | a refused request writes nothing | VERIFIED 464/464 per Python | file sha256 + log length + ledger count |
| C9 | request_sha = kept-text hash; replay / 409 | VERIFIED: 245/245 unit lines kept-body hash, 0/223 raw; 735 + 1,470 replays, 0 duplicate lines/events; other kept text 409 (245/245, 280/280); difference only inside a redacted cluster or folding to the same kept text → replay (by design) | pii_persist13, xv_idem13 ×14, idem_collapse13 28×2×2 |
| C9a | kept text / hash stable 3.12 ↔ 3.13 | VERIFIED: 1,112,032 code points × 5 templates 0 differences (R12-2: 622 → 0); real-service cross-Python 105/105 hashes, 0 status changes, both directions | xver_kept12, xver_email13, xv_idem13 READ_PY |
| C10 | names_a_card same set as 99569bd | VERIFIED 78,154/78,154 + 38/38, both Pythons | funds_decide, funds_service |
| C11 | 1a90498 / f5ee168 / 0f5fbe3 / 91ea9a1 / 75b0662 logs start and replay at cb708b9 | VERIFIED (state diff [], 106/106 anchors, 0 duplicates); retry-across-upgrade 409s 4|3 / 7|5 / 10|9 / 12 / 12 of 105 | xv_idem13 |
| C12 | redacted value not recoverable via request_sha | VERIFIED: 8 searches not found (incl. D12-H1, D12-L1 lines); positive control at 0f5fbe3 found in 0.95 s | request_sha_recover3 |
| C13 | start-up read refusals | VERIFIED unchanged, 6 cases ×2; device NOT RUN | start_read |
| C14 | false positives measured, both Pythons | MEASURED: 48/86 free text, 22/86 approval refs (= 1a90498); supplement 8/17 (1a90498 6/17) | fp_corpus12, fp_supp13 |

convergence: n/a (no schema or log-format migration in the delta; earlier logs start with state diff [], C11). greenfieldHash/brownfieldHash: not applicable.
idempotency: [{path: unit inventory add, 245 spellings ×2 Pythons, replays: 735, duplicateEffects: 0}, {path: full lifecycle 105 requests, 14 A→B runs, replays: 1,470, duplicateEffects: 0}, {path: same request_id other body, replays: 245 + 280 + 28×4, duplicateEffects: 0}].

## Findings
- **D12-H1 — CLOSED.** strip_format folds every `unicodedata.digit` value (pii.py:113-114); 798/798 digit-valued code points count in all 6 shapes, 35/35 omega12 card/SSN spellings (unit level) and 20/20 non-decimal spellings through the service redacted/refused with 0 leaks on all 5 paths, both Pythons; request_sha of those lines not recoverable.
- **D12-L1 — CLOSED.** Entities decoded to a fixed point (`_fully_decoded`, ≤16 passes, else whole value redacted): 4, 5, 8, 15, 16 levels redacted; 17 and 20 levels redacted whole; 0 leaks.
- **D13-L1 LOW (non-blocking) — R12-2 narrowed the international-phone rule: an 8-digit international number directly after a non-ASCII non-space character is stored in clear (regression from 1a90498).** pii.py:80 adds the negative lookbehind `(?<![^\x00-\x7f\s])`; the comment at :52-54 ("wider than `\w`: fails closed") does not hold for a lookbehind. Measured: 835,225 of 1,112,031 preceding code points (9,880 assigned: emoji/☎ So 5,751, — – Pd 23, « » Pi/Pf, Sm 924, …) lose detection of `+500 21234` / `+1 555 0142`; through the service on the real ledger `faces west, contact 📞+500 21234 for access` → unit 201 redacted:false, number in clear in the log, export and views; face attribute 200 stored; pending.line / pending.discarded hold it (1a90498: redacted ['phone'] / 422, 0 leaks). Bounded: numbers of 9+ digits are caught by the cluster rule (0 change for `+44 20 7946 0958`); campaign names, `ref` attributes and the approval ref are unreachable (ASCII schemas without `+`). Severity LOW because only 8-digit-total international numbers (rare formats) typed with no space after a symbol are affected; the log is append-only with no scrub path (INFO-3), so the founder may rate it higher.
  - Reproducer: `python -I poc/intl_free13.py <t-1a90498> <t-cb708b9>` → `"+500 21234": n_A_not_B 835225`; persistence: `SECRETS_FILE=<[["x","\U0001F4DE+500 21234","detect"]]> python -I poc/pii_persist13.py <ledger-bin> <fresh-dir> <t-cb708b9>` → unit `stored_redacted_flag` false, `leaks` [ooh/ooh_log.jsonl:clear, view0:clear, view3:clear], face status 200.
  - Falsifier: the same runs at cb708b9 show redacted:true ['phone'] / 422 with 0 leaks (n_A_not_B 0).
  - Remedy direction (Security Engineering): an ASCII-only lookbehind `(?<![A-Za-z0-9_+])` is version-independent and does not narrow.
- **D13-L2 LOW (non-blocking) — composition order: an entity that spells a character which normalises into entity syntax is kept as typed.** `123&#xFF06;nbsp;45&#xFF06;nbsp;6789`, `123&#xFE60;nbsp;…`, `123&&#x200B;nbsp;…` kept (cb708b9 and 1a90498, both Pythons); the number appears only after decode → NFKC/hidden removal → decode. Split by the ASCII letters `nbsp` in every form the rule names; not a regression; very low realism (a browser shows `＆nbsp;`). Reproducer: `python -I poc/composition13.py <t-cb708b9>` → `entity_of_fullwidth_amp` kinds []. Falsifier: kinds non-empty, kept "[redacted]". Remedy direction: iterate decode and screen_form jointly to a fixed point in `_fully_decoded` / `_forms`.
- **M-3 MEDIUM (unchanged, open)** and **M-2 MEDIUM (unchanged, open)** — reproducers poc/m3_stale_delete.py (shorten_then_restore), poc/targeted.py (B_consistent_rewrite_inflated_funds); falsifiers: after restore the line is applied / hub read 503 or flagged while integrity.ok is false.
- INFO-10 — entity-spelled letter-like separators are over-redacted: 1,130 characters that NFKC-map to an ASCII letter end a cluster when typed (`123ª456ª789` kept as `123a456a789`) but join it when spelled `&#xAA;` (the backstop `_still_holds_personal_data` reads the decoded text before NFKC, pii.py:339-353). Fails closed; FP class.
- INFO-11 — kept text now folds non-decimal digits in non-PII text (`panel ❶ lit` → `panel 1 lit`): a retry with `panel 1 lit` or `panel ⓵ lit` under the same request_id replays 201 (same kept text, by design); retry-across-upgrade 409 for such texts written earlier (§3b).
- INFO-12 — a log written at 1a90498 keeps D12-H1 numbers in clear after the upgrade (append-only, no scrub, INFO-3); 1a90498 was never released.
- INFO-7 (Unicode 16 digits, 3.14) unchanged, not re-run. INFO-8 CLOSED (ADR 0022:362 now says unassigned characters are kept). INFO-9 (non-Latin-script FPs) unchanged (§4). OOH2-PII-1-FP and OOH2-R12-SEC-S2 (URL/JS/CSS escapes: 7 spellings stored as typed, 1,079,204 single-code-point `%HH` separators) — measured, documented limits, not blocking.

## NOT RUN
- Device node at the log path (mknod EPERM, non-root). L-A (h1_stale_report) and INFO-3 old-line recovery not re-run (code unchanged; not in the asked short re-run). INFO-7 (Python 3.14 / Unicode 16) not re-run. No shared/staging/production datastore touched. Isolation: one disposable data dir + one ledger process/port/file per case under scratchpad/data13run; proves exclusivity only, not tenant RLS (not in the delta). Multi-character separators: corpus only (7 r13 + earlier). Digit sweep: single repeated characters and 7 sets × 5 shapes + 4 mixed-script spellings, not every mix. D13-L1 sweep on 3.12 only (3.13 equivalence from the §3a `call X+500 21234` template: 0 differences). Other vendor_text writers than unit add and the other `text` attributes: code inspection only (all reach `observed()` / `screen_free_text`). Concurrency (AEGIS-RELIABILITY). Windows/Linux filesystems. Ledger build provenance.
- Candidate suite on my archive (context only; passing ≠ property proven): 1225 passed, 1 failed on 3.12 and on 3.13 (out/pytest-cb708b9-3.1x.txt). The failure, test_env_documented, is an artefact of my archive (only `services/` extracted, so `docs/adr/0019-*.md` is absent → StopIteration); it is not a candidate failure.

## Verdict
blocking: false. At cb708b9 the founder rule holds where measured: 221/221 detect spellings × 5 persistence paths, 0 leaks on both Pythons, including the D12-H1 non-decimal digits (closed) and D12-L1 nesting at any depth (closed). Every single-code-point separator miss over all 17 planes is an ASCII letter, normalises to one, or is the documented URL-escape limit; all 798 digit-valued code points count as digits. Refused requests write nothing (464/464). request_sha is the kept-text hash and cannot be recovered (control found in 0.95 s). Kept text and hash are identical on 3.12 and 3.13 for every code point, and the R12-2 e-mail dependence is gone (622 → 0). Logs from 5 earlier versions start and replay with 0 duplicates; retry-across-upgrade 409 counts were measured. False positives were measured. Funds, H-1 and start-up behave as before; M-2/M-3 are still open MEDIUM, unchanged. New: D13-L1 LOW (R12-2's lookbehind lets 8-digit international numbers after a symbol through and stores them in clear) and D13-L2 LOW (composition order). Neither is HIGH or CRITICAL; both need Security Engineering follow-up.

VERDICT: DATA_GREEN (scoped to the claims above at cb708b9 / tree eba95097; non-blocking: D13-L1 LOW, D13-L2 LOW, M-2 MEDIUM, M-3 MEDIUM)
