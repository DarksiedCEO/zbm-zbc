# AEGIS RUNTIME gate — omega13

- **candidateSha** cb708b924bb01aa5543b832573a8fa56bd041b80 · **treeOid** eba95097622438b226169c88910c81cc9a999212
- subtree services/ooh-py/src **ff25b0f321148c19e28235eebdce7022c38ab804** · subtree services/ledger-rust **4f1ab90e2f81245130d6eb87091745146f6398e6** (unchanged from 1a90498)
- repo DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2 (`git rev-parse ooh-dept-15-wave2` = cb708b9…, the tip). Commit author: Andre <<OWNER-EMAIL>>, Sun Oct 18 16:59:21 2026 -0700.
- Controls: 1a904983d6e064b912bbd95a7afa1668da6c50c9 (PII), 75b0662823b867df3325d676b1dcf69abe37a425 (H-1).
- **Author of this report:** AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega13 run). Fresh context, independent of the builder and the peer gates. I used omega12's runtime.md and scripts only as a model. No omega12 verdict is carried forward.

**Classification: REAL_PROTOCOL_LOCAL** · **VERDICT: RUNTIME_GREEN_FOR_SCOPE**
- Scope: OOH2-PII-1 under the founder rule and decisions, plus a short H-1 and start-up-refusal re-run, for services/ooh-py at cb708b9.
- No CRITICAL, HIGH or MEDIUM finding. Only INFO items and carried LOW items.

## 0. Binding
- Delta 1a90498..cb708b9 under services/ooh-py/src and services/ledger-rust: **pii.py only** (`git diff --stat`: 1 file, +88/−32).
- Blobs at the two SHAs:
  - store.py **8d81ac82…** at both;
  - service.py **6c7233e2…** at both;
  - pii.py 7ac2003f… → **b0f8530a…**.
  - `git hash-object` on my cb708b9 copy gives the same three values.
- Copies: `git archive <sha> | tar -x` into omega13/runtime/src_cb708b9, src_1a90498 and src_75b0662.
  - After all runs, a fresh `git archive` compared with `diff -r` was **IDENTICAL** for all three.
  - There was no `__pycache__` (drivers ran with `-I -B`; the services ran with PYTHONDONTWRITEBYTECODE=1) and no `target` dir under omega13.
- `diff -r` of review-cb708b9/services/ooh-py/src against my copy: identical.
- Frozen copy review-cb708b9: HEAD cb708b9…, `status --porcelain` 0 lines.
- Main repo: only the pre-existing `?? .git.backup-preimport/` and `?? .idea/`.

## Execution conditions
- Host: Darwin 25.6.0 arm64 (this Mac). Runs were 2026-10-19 00:05–01:45 UTC, on 127.0.0.1 only, with ports from bind(0).
- Python: venv3.12 (3.12.14, unidata 15.0.0) and venv3.13 (3.13.9, unidata 15.1.0), as recorded in result.json.
- Dependency mode was **REAL**:
  - **Ledger:** ledger-rust built here from my cb708b9 copy with `SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir scratchpad/rt13-target`.
    - rc=0: "Compiling ledger-rust v0.1.0 (…omega13/runtime/src_cb708b9/services/ledger-rust) … Finished `release` … 7.03s". cargo_build.log sha256 7613ff70…3a14.
    - The binary is rt13-target/release/server, sha256 **997e5601f7efd58ac0124162eb200912f84f76472d961d4306ec251758f5d0c1**, Mach-O arm64. Every run, including the controls, used it.
  - **Service:** the real entrypoint `python -m api` (cwd <copy>/services/ooh-py/src), over real HTTP (httpx).
    - The .out logs record "started service pid … venv3.1x/bin/python -m api", and /health returned status ok.
    - LEDGER_SERVICE_URL pointed at that ledger. There was no mock or relay, and OOH_NON_PRODUCTION was unset.
- Concurrency:
  - Two batches ran (batch13.sh 3.12 and 3.13), so at most **2 service+ledger pairs** at once, one client each, with sequential calls.
  - Before each run the batch waited while `pgrep -f pytest | wc -l` was ≥ 4. It paused 138 (3.12) and 118 (3.13) times, from 00:05 to 00:24.
  - The matches were peer agents' zsh wrappers whose command text contains "pgrep -f pytest". `ps -axww` showed no actual pytest interpreter. I ran no pytest.
  - Disclosure D1: before the batches I ran one 21-case smoke run (runs/smoke_probe_ccb708b9_3.13: 1 pair, probe values only) without waiting on the gate; the count read 6 at the time.
  - Disclosure D1b: the gate is checked only at run start. The post-run count reached 4 once (3.12, 00:56:24).
- Memory watchdog: watchdog13.sh (512 MB, 20 ms) never fired (no runs/watchdog.log) and was stopped via runs/watchdog.stop.
- Batch: 24 runs, **all rc=0** (runs/batch_3.12.log 654ac569…f8be9, runs/batch_3.13.log 8b0fb94f…c2f499).

## 1. OOH2-PII-1 through real HTTP (pii13.py; both Pythons)
**Driver.** pii13.py = omega12 pii12.py transformed by the recorded gen_pii13.py. Setup, the three probes per value and the exact-byte bodies are unchanged:
- unit **vendor_text** "faces west, contact <v> for access" -> expect 201, redacted, kinds set;
- face **location_description** "billing <v>" -> expect 422;
- **client_approval_doc_ref** = <v> at PROPOSAL_SENT -> expect 422.

**Values (VALUESET=main): 160 secret entries, 159 distinct, plus 16 false-positive probes** (176 values × 3 = 528 cases per run).
- The 120 omega12 secret entries (119 distinct) are unchanged.
- 40 added:
  - the **4-level nested card exactly as given**, "4111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111";
  - nbsp-nested cards at 5, 8, 15, 16, 17 and 30 decode levels;
  - ndash-nested SSNs at 4, 16 and 17 levels;
  - 4-level "&#45;", 6-level "&#x20;" (card), 5-level "&#52;" leading digit, and mixed 4/7-level nesting;
  - **non-decimal digits** (13):
    - "❹❶❶❶ ❶❶❶❶ ❶❶❶❶ ❶❶❶❶", "⓵⓶⓷-⓸⓹-⓺⓻⓼⓽";
    - Ethiopic SSN "፩፪፫-፬፭-፮፯፰፱" and Ethiopic card;
    - dingbat sans-serif card; "❶❷❸❹❺❻❼❽❾";
    - Kharoshthi card; Brahmi SSN; New Tai Lue "4᧚᧚᧚ 1111…";
    - a mixed-script card; "&#x2779;&#x2776;…" (entity-encoded dingbats);
    - parenthesized "⑴⑵⑶-…"; full-stop "⒈⒉…";
  - **%40 forms** (6): "1234567&nbsp;%40", "1234567&nbsp;&#64;" (see O13-1), "1234567 %40", "1234567%40", "1234567&#160;%40", "1234567&amp;nbsp;%40";
  - **zwj + combining** (5): "12345a&zwj;&#769;6789", raw "12345a‍́6789", "12345e&zwj;&#769;6789", "12345a&#8205;&#x301;6789", "12345á6789";
  - **unassigned separators** (2): "123͸45͸6789", "4111\U000E01F01111…".
- False-positive probes: omega12's 12, plus "faces͸west near exit 12" (an unassigned character must be kept), "&amp;amp;amp;amp;copy; 2026 Lamar", a 20-level "&amp;…copy;" and "Suite 12345 & Rd 6789".
- Boundary and documented-limit probes ran in a **separate rig** (VALUESET=probe: 7 values × 3), so their kept digits could not contaminate the main run's files.

**Scanner changes from omega12:**
- Every **digit-valued character** is counted (`unicodedata.digit`, Nd and No), not only Nd.
- Entity decoding runs to a fixed point (≤100 passes).
- Two more views are scanned on each case delta and in the sweep: Cf/Co removed then NFKC to a fixed point; and Cf/Co/Mn/Me removed.
- The cumulative per-case scan keeps omega12's 4 views (raw, \u-decoded, entity-decoded, both).
- Sources, hashes and the redacted-hash positive check are as in omega12. Sources: each file of OOH_DATA_DIR and the ledger store dir, GET /ledger/entries, service.log, ledger.log. Hashes: sha256 of raw body bytes, ensure_ascii body, canonical unredacted body and 4 normalised variants, and the text raw/NFKC/collapsed/entity-decoded/entity-decoded NFKC.

**Independent sweep (sweep13.py), over every file under each run dir, in 6 views:**
- ASCII-letter clusters, plus all digits of each line concatenated;
- targets: 123456789, 912701234, 4111111111111111, 5555555555554444, 378282246310005 and 123456740;
- plus every recorded secret-case hash.

**Candidate cb708b9.** rows_ccb708b9_3.12.txt = _3.13.txt (sha256 **b0b422bd…c4ec4d**): the outcome is **identical across Pythons**.
- For the **159 distinct secret values that are personal data under the rule** (all except "1234567&nbsp;&#64;", O13-1):
  - **vendor_text 159/159 -> 201, `personal_data_redacted: true`, kinds set.**
    - Nested forms with ≤15 levels -> "[redacted]" inside the text, kinds card or ssn.
    - 16, 17 and 30 levels -> the **whole value "[redacted]"**, kinds ["number"]. This matches the "deeper than 16 passes" decision.
    - Non-decimal forms -> card, ssn or number. %40, zwj+combining and unassigned forms -> number, ssn or card.
  - **Hash positive check 160/160**: the canonical body with vendor_text = the returned text is in ooh_log.jsonl. The request hash uses the redacted text.
  - face: **159/159 -> 422** (158 ATTRIBUTE_VALUE_PERSONAL_DATA, 1 schema), ledger Δ = 0.
  - doc_ref: **159/159 -> 422** (10 ATTRIBUTE_VALUE_PERSONAL_DATA, 149 schema), ledger Δ = 0. The final approve with "io-acme-signed-1" -> 200.
  - **Delta hits 0 and cumulative hits 0 for all 477 of these cases**, in every source and view: no literal, no digits in any form, no hash.
- The only secret-labelled cases with hits are the 3 for "1234567&nbsp;&#64;" (O13-1). The sweep's only HASH lines are that value's.
- The sweep's only digit lines in the main run: 2 lines per Python of "123456789" (line-concat only), at ooh_log.jsonl lines 1415/1951 (3.12) and the same lines in 3.13. Both are my false-positive probe "Suite 12345 & Rd 6789", which ASCII "Rd" splits, so it is kept by design. Nothing else in ooh_log.jsonl, ledger.jsonl, service.log or ledger.log.
- Ledger /verify valid (4010 entries). The data dir holds [ooh.lock, ooh_log.jsonl] (ooh_log.jsonl sha256 3.12 58ac2112…1965, 3.13 cbe33829…34a8).

**Control 1a90498** (rows_c1a90498_3.12 = _3.13, **cf05db9b…a1d7**, identical across Pythons):
- The old 119 are all redacted or refused, as omega12 found.
- **26 new personal-data forms are stored in clear in vendor_text** (201, `personal_data_redacted: false`). They are:
  - all 7 nbsp-nested cards, including the **4-level form as given**;
  - the 3 nested SSNs; the 6-level &#x20; card; the mixed nesting;
  - **"❹❶❶❶ ❶❶❶❶ …", "⓵⓶⓷-⓸⓹-⓺⓻⓼⓽", Ethiopic SSN and card**, dingbat sans-serif, "❶…❾", Kharoshthi, Brahmi, the mixed-script card;
  - **"1234567&nbsp;%40"**, "1234567&amp;nbsp;%40";
  - **"12345a&zwj;&#769;6789"**, "12345e&zwj;&#769;6789", "12345a&#8205;&#x301;6789".
- 25 of them (all except the 30-level nesting) are also **accepted on the face** (200, ledger Δ = 2).
- At the control the sweep finds 266 digit lines and 106 hash lines per Python.
- **The cases discriminate.**

**Idempotency (INFO, O11-2 unchanged), both Pythons:** the same request_id with "contact 123-45-6789 …" and then "987-65-4321 …" -> 201, the same unit_id. The same request_id with non-PII text -> 409 REQUEST_ID_REUSED.

## 2. Short H-1 and start-up refusals (store.py byte-identical to 1a90498)
Driver: sw13.py = omega12 sw12.py with only `omega12`->`omega13` and `common12`->`common13`. table.txt sha256 1b1b0caa…27d5.
- **e2 W** (3.12 and 3.13):
  - same-size fault (same_size True, 1372 bytes), and fault_sha != orig_sha;
  - W3 -> **503 STORE_UNAVAILABLE, file unchanged**, anchors 2->3; audit NOTok; W4 -> 503 INTEGRITY_UNVERIFIED;
  - put back -> job and audit ok; W3 retry 201, W5 201, restart ok, write 201;
  - FINAL 5 lines = 5 anchors, /verify valid, tenants [t-before, t-mid, t-x, t-z, t-after].
- **m3 I** (both): audit NOTok; W3 and W4 -> 503 INTEGRITY_UNVERIFIED, unchanged, anchors 2->2; put back ok; FINAL 5 = 5, valid.
- **fifo H** (both): A1, J1 and ST NOTok; W3 and W4 503 INTEGRITY_UNVERIFIED; J2 NOTok; put back -> ok, retry 201; FINAL 5 = 5, valid.
- **Control 75b0662 e2 W** (both): W3 -> **201** (file changed), audit ok, W4 201, FINAL 6 = 6 with t-y. The scenario discriminates.
- Totals: 12 non-201 candidate POSTs, and the log changed in 0 of them. The largest call took 0.157 s. Sampled service RSS was 69,216–81,440 kB.
- **Restart on the fault** (both Pythons, 10 runs): every one exited rc=1 within 0.233–0.307 s, with peak RSS 58,576–60,080 kB.
  - FIFO and directory: `StoreCorrupt: the log is not a regular file; refusing to start`.
  - Symlink to identical bytes and symlink to /dev/zero: `…cannot be opened (OSError); refusing to start: it must be a re[gular file]…`. The symsame target was unchanged.
  - chmod 000: `…cannot be opened (PermissionError); refusing to start: inspect…`.
  - These match omega12 at 1a90498.

## 3. Findings
- **No CRITICAL, HIGH or MEDIUM.**
- **O12-1 (LOW at 1a90498) is closed at runtime.** The 4-level form and 5–30 level forms are redacted or refused at cb708b9 and stored at the 1a90498 control.
- **The r12 forms are closed at runtime:** non-decimal digits, "1234567&nbsp;%40" and "12345a&zwj;&#769;6789".
- **O13-1 (INFO, probe design): "1234567&nbsp;&#64;" is not personal data under the founder rule.**
  - Raw: "1234567" and "64" are split by the ASCII letters "nbsp". Decoded: "1234567\xa0@" has 7 digits.
  - It was kept as typed (vendor_text 201 unredacted), accepted on the face (200, Δ2) and refused as doc_ref (schema 422), identically on both Pythons.
  - I listed it as a secret by mistake. It is not a miss.
  - Reproducer: runs/pii_main_ccb708b9_*/result.json, value index 147.
  - Falsifier: a rule that counted entity-encoded digit characters across an ASCII-letter entity name would redact it. The founder rule does not.
- **O13-2 (INFO, documented limit): URL and JS/CSS escapes are kept** (probe run, both Pythons): "4111%2D1111%2D1111%2D1111", "123-45-6789" (literal backslash), "4111\2d 1111\2d …", "4111\x2D1111…".
  - vendor_text 201 unredacted, face 200 (Δ2), doc_ref 422 (schema). This is as pii.py documents.
  - The boundary probes ("4111 x 1111 …", "123ⓐ45ⓐ6789", full-width ｘ) are also kept, as designed.
- **O13-3 (INFO): false positives observed**, identical on both Pythons:
  - Redacted in vendor_text (kinds ["number"]) and refused on the face (422): GPS; ZIP+4 pair; date range; "io-2026-1018-0001" ("io-[redacted]"; doc_ref 422 ATTRIBUTE_VALUE_PERSONAL_DATA); "1,234,567,890 impressions"; the "lon" of "lat … lon …"; the CJK date range ("[redacted]日"); the Greek address ("Οδός [redacted]").
  - **New: a 20-level nested non-PII "&amp;…copy; 2026 Lamar" -> the whole vendor_text becomes "[redacted]"** and the face is refused (422). This follows the "redacted whole" decision.
  - Kept as typed: "12345678" (doc_ref approved 200), "14x48 billboard on Route 101", "1,250,000円 / 4週", "&copy; 2026 Lamar", 5-level "&amp;amp;amp;amp;copy; 2026 Lamar", "Suite 12345 & Rd 6789", and **"faces͸west near exit 12", with U+0378 kept in the returned text** (the founder's unassigned-kept decision, observed through HTTP).
- **O11-2 (INFO, carried):** a redacted-hash replay, as in §1.
- **O10-1 (LOW, carried):** the doc_ref schema refuses 149 of the 159 before the PII rule runs.
- **O10-2 (LOW, carried):** a start-up refusal is an uncaught StoreCorrupt traceback (rc=1). It is prompt and fail-closed.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN and PRODUCTION_PROVEN are not claimed. No deployed SHA was read and no live endpoint was touched (127.0.0.1 only).
- pytest: none run by this gate. That belongs to AEGIS-TEST.
- Not run for PII:
  - vendor_text on endpoints other than unit create;
  - face text attributes other than location_description;
  - e-mail and phone shapes;
  - the /audit/evidence view;
  - doc_ref beyond the schema;
  - non-decimal digit scripts beyond the 13 tried;
  - unassigned characters other than U+0378 and U+E01F0;
  - nesting depths other than 4, 5, 6, 7, 8, 15, 16, 17, 20 and 30;
  - entity forms with a numeric-entity digit next to a named entity beyond O13-1;
  - the request_sha cache under eviction or concurrency;
  - the cumulative per-case scan with the two stripped views (they ran on deltas and in the final-file sweep only).
- Not run for the store: modes beyond e2 W, m3 I and fifo H and the 5 restart faults; races and concurrent writers; large logs; Linux/CI; the other services that share store.py.
- Disclosures:
  - D1, D1b: the pytest gate, as in Execution conditions.
  - D2: the ledger binary hash (997e5601…) differs from omega12's (45817c8c…) for the same ledger subtree. The build path differs. I did not investigate.

## Raw evidence (omega13/runtime/)
- Runs: runs/<name>.out and runs/<name>/result.json, service.log, ledger.log, and the ooh/ and ledger/ dirs (24 batch runs + 1 smoke).
- Tables and analyses:
  - rows_ccb708b9_3.12/3.13.txt: b0b422bd…c4ec4d
  - rows_c1a90498_3.12/3.13.txt: cf05db9b…a1d7
  - pii_an_main_ccb708b9_3.12.txt e1e1d9ec…49c0, _3.13.txt 4e0ff141…8f8d
  - pii_an_probe_ccb708b9_3.12.txt 41b3793d…482f, _3.13.txt 50d3010c…dcf0
  - pii_an_main_c1a90498_3.12.txt 13149b5e…dc90, _3.13.txt 83f7fc56…cbfb
  - sweep_pii_main_ccb708b9_3.12.txt 1d90ea80…c531a, _3.13.txt 75119de4…7eaf
  - sweep_pii_probe_ccb708b9_3.12.txt c3ae5c16…eb17, _3.13.txt 22e64824…6388
  - sweep_pii_main_c1a90498_3.12.txt 078dcce2…6dd88, _3.13.txt 67af59d4…28de
  - table.txt 1b1b0caa…27d5
- Scripts (sha256): pii13.py bd10acf2…acdd, gen_pii13.py ad2dd646…24d9, common13.py aaed70e5…d788, sw13.py 295fdf3f…0c9a, sweep13.py 204f94c3…e1f2, an13.py 1ad77383…8668, rows13.py 63f034e6…a99f, table13.py 85508cb6…7544, batch13.sh 6a91aca2…2374, watchdog13.sh 5a75bd4b…8524.
- Leftovers: no omega13 process is left (`ps -axww | grep omega13/runtime|rt13-target|watchdog13` -> none). There were no candidate edits, commits or pushes.
