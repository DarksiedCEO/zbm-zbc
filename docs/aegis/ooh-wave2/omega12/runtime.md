# AEGIS RUNTIME gate — omega12

- **candidateSha** 1a904983d6e064b912bbd95a7afa1668da6c50c9 · **treeOid** 83356dd062ff3e668358785f5e6d963cdb3950c2
- subtree services/ooh-py/src **8ca31e9ec3e755c7f90be5a92f306439102773a8** · subtree services/ledger-rust **4f1ab90e2f81245130d6eb87091745146f6398e6** (the same as at f5ee168)
- repo DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2 (`git rev-parse ooh-dept-15-wave2` = 1a90498…, the tip). Commit author: Andre <<OWNER-EMAIL>>, Sun Oct 18 10:58:54 2026 -0700.
- Controls: f5ee168c9e9148241b709ec93acc660c88d08ba7 (PII), 75b0662 (H-1).
- **Author of this report:** AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega12 run). Fresh context, independent of the builder and of the peer gates. I read omega11's runtime.md and its scripts only as a model. No omega11 verdict is carried forward.

**Classification: REAL_PROTOCOL_LOCAL** · **VERDICT: RUNTIME_GREEN_FOR_SCOPE** (scope: OOH2-PII-1 under the founder decisions, plus a short H-1 and start-up-refusal re-run, services/ooh-py at 1a90498). No CRITICAL or HIGH finding. One LOW finding (O12-1) and INFO items are listed below.

## 0. Binding
- Delta f5ee168..1a90498 under services: pii.py and service.py, plus two test files (`git diff --stat`). The service.py change is **docstring only** (request_sha). store.py blob is **8d81ac82…** at both SHAs, and `git hash-object` on my f5ee168 copy gives the same.
- In my copy, each blob equals `git rev-parse 1a90498:<path>`: store.py 8d81ac82…, service.py 6c7233e2…, pii.py 7ac2003f…, svc_campaigns.py f5291986…, svc_inventory.py 33bf4094….
- Copies: `git archive <sha> | tar -x` into runtime/src_1a90498, src_f5ee168 and src_75b0662. After all runs, a fresh `git archive` compared with `diff -r` was **IDENTICAL** for all three. There was no __pycache__ (drivers ran with `-I -B`; the services ran with PYTHONDONTWRITEBYTECODE=1) and no `target` dir under omega12.
- Frozen copy review-1a90498: HEAD 1a90498…, tree 83356dd…, `status --porcelain` showed 0 lines. Main repo: only the pre-existing `?? .git.backup-preimport/` and `?? .idea/`.

## Execution conditions
- Host: Darwin 25.6.0 arm64 (this Mac). Runs were 2026-10-18, 18:04–19:12 UTC, on 127.0.0.1 only, with ports from bind(0).
- Python: venv3.12 (3.12.14) and venv3.13 (3.13.9), as recorded in result.json.
- Dependency mode was **REAL**:
  - **Ledger:** the ledger-rust binary was built here from my 1a90498 copy: `SDKROOT=…/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir scratchpad/rt12-target`. It returned rc=0 ("Compiling ledger-rust v0.1.0 (…omega12/runtime/src_1a90498/services/ledger-rust) … Finished `release` … 5.18s"; cargo_build.log sha256 561b18be…2c6b). The binary is rt12-target/release/server, sha256 **45817c8cb4c7ba270baa531b2d0131f247df2197a89250c6593e47b2f961f27c**, Mach-O arm64.
    - The ledger subtree is identical to omega11's, but the binary hash differs from omega11's 381735e1…. I did not investigate why; the build path differs.
    - Every run, including the controls, used this binary.
  - **Service:** the real entrypoint `python -m api` (cwd <copy>/services/ooh-py/src), over real HTTP (httpx). The .out logs record "started service … python -m api", and /health returned ok.
  - LEDGER_SERVICE_URL pointed at that ledger. There was no mock or relay, and OOH_NON_PRODUCTION was unset.
- Concurrency:
  - two batches (3.12 and 3.13), so at most **2 service+ledger pairs** at once, one client each, with sequential calls.
  - Before each run, batch12.sh waited while `pgrep -f pytest | wc -l` was ≥ 4: 30 and 44 pause lines in the batch logs.
  - Disclosure D1: the check ran only at run start, so a run in progress was not paused (the post-run count reached 5 once, at 18:40). The `pgrep -f` count also includes a peer agent's shell whose command line contains the word "pytest". I ran no pytest.
- Memory watchdog: watchdog12.sh (512 MB, 20 ms) never fired (no runs/watchdog.log) and was stopped via runs/watchdog.stop.
- Batch: 22 runs, **all rc=0** (runs/batch_3.12.log 7bf71232…64bb, runs/batch_3.13.log 0ab051c9…db01).

## 1. OOH2-PII-1 through real HTTP (pii12.py; both Pythons)
**Driver.** pii12.py is omega11's pii11.py transformed by the recorded script gen_pii12.py. The changes:
- the value lists were extended (the SSN and CARD lists are byte-identical);
- the scanner was hardened for the new rule.

Setup is through the API only, against the real ledger. Per value:
- unit **vendor_text** "faces west, contact <v> for access" -> expect 201, redacted, kinds set;
- face **location_description** "billing <v>" -> expect 422;
- **client_approval_doc_ref** = <v> at PROPOSAL_SENT -> expect 422.

Bodies were sent as exact bytes.

**Values: 120 entries, 119 distinct** (U+0640 3-2-4 appears in both the omega11 list and the O11 list):
- **omega11's 65:** 47 SSN spellings and 18 card spellings. They include the hidden-character, other-script-digit and separator forms.
- **O11-1 forms, 29:**
  - "12ー34ー56ー789" style, with ー U+30FC, tatweel U+0640, U+02C9, U+1427, U+A78F and ʼ U+02BC;
  - "41ー11ー11ー11ー11ー11ー11ー11" pairs with each of the six;
  - "1234ʼ56789" style with each of the six;
  - the 4-4-4-4 and 3-2-4 forms with the first five;
  - "1234ー5678ー9".
- **Other non-ASCII letters as separators, 5:** ǀ U+01C0, Greek ο, Cyrillic х ("4111 х 1111 х …"), ぃ, 々.
- **HTML-entity forms, 21:**
  - named: "4111&nbsp;1111&nbsp;1111&nbsp;1111", "123&ndash;45&ndash;6789", &mdash;, &middot;, &shy;, &zwj;, &hyphen;, &NonBreakingSpace;, and "&nbsp" without the semicolon;
  - nested: "&amp;nbsp;" (card), "&amp;ndash;", "&amp;amp;ndash;" and "&amp;#45;";
  - numeric: decimal-entity digits "&#52;&#49;…", hex-entity digits "&#x31;…&#x39;", "&#X34;…-1111-1111-1111", "123&#x2D;45&#x2D;6789", "12&#x2D;34…", "4111&#x20;…", "4111&#160;…", "41&#x30FC;11…".
- **Boundary probes, 4:** "4111 x 1111 x …", "123ⓐ45ⓐ6789", full-width "4111ｘ1111…" and a **4-level** nested "4111&amp;amp;amp;nbsp;…".
- **False-positive probes, 12.**

**Scan after every case** (408 per run), cumulative and as the delta new since the case began:
- sources: every file in OOH_DATA_DIR, every file in the ledger-rust store dir, `GET /ledger/entries`, service.log and ledger.log;
- views: raw, \u-decoded, and HTML-entity-decoded to a fixed point (up to 10 passes);
- searched for:
  - the literal value;
  - its digits (NFKC, any script as ASCII, from the fully entity-decoded value) within: digit runs with separators removed, clusters ended by Unicode letters, and **clusters ended by ASCII letters only** (the founder rule);
  - sha256 of: the raw body bytes; the ensure_ascii body; the canonical unredacted body; the canonical body with vendor_text NFKC, entity-decoded, entity-decoded NFKC, or spaces-collapsed NFKC; and the text raw, NFKC, collapsed, entity-decoded, or entity-decoded NFKC.

**Independent sweep (sweep12.py), over every file under each run dir:**
- ASCII-letter clusters of the raw, \u-decoded and entity-decoded views, plus all digits of each entity-decoded line concatenated;
- targets: 123456789, 912701234, 4111111111111111, 5555555555554444 and 378282246310005, plus all 1338 recorded secret-case hashes.

**Candidate 1a90498.** The per-value rows (rows_c1a90498_3.12.txt = rows_c1a90498_3.13.txt, sha256 ae0285c8…dc9) are **identical across Pythons**.
- vendor_text: **120/120 -> 201, `personal_data_redacted: true`, `personal_data_kinds` set.**
  - O11-1 and pair forms get kinds ["number"]; 4-4-4-4 gets ["card"]; 3-2-4 gets ["ssn"]; entity forms get card, ssn or number.
  - The text is "faces west, contact [redacted] for access", with "SSN [redacted]", "ITIN [redacted]" or "([redacted]" as in omega11, plus the 2 residues in O12-2.
  - **Hash positive check 120/120:** the canonical body with vendor_text = the returned text is in ooh_log.jsonl. No raw, unredacted, NFKC or entity-decoded body or text hash is present. The request hash uses the redacted text.
- face: **120/120 -> 422 ATTRIBUTE_VALUE_PERSONAL_DATA**, ledger Δ = 0.
- doc_ref: **120/120 -> 422**, ledger Δ = 0. Of these, 10 were refused by ATTRIBUTE_VALUE_PERSONAL_DATA (the 10 ASCII forms of omega11) and 109+1 by the schema pattern (O10-1). The final approve with "io-acme-signed-1" -> 200.
- **Delta hits for all 360 secret cases: 0, in every source and view. Hash hits: 0** (driver and sweep).
- Cumulative-only hits: 76 face/doc_ref cases whose digits are 4111111111111111 (cum_explain.txt dd0605ec…b798).
  - These come only from ooh_log.jsonl lines 999 and 1471. Those are exactly the lines holding the 4-level-nested boundary probe (see O12-1).
  - No other line of ooh_log.jsonl, ledger.jsonl, service.log or ledger.log holds these digits.
  - The sweep's other hits are the letter-separated boundary probes (lines 975/983/991/1459/1463/1467). One 3.12 line-concat-only hit (line 1534) is the 8-digit FP doc_ref "12345678" concatenated with an adjacent id's digits.
- Ledger /verify valid (3130 entries). The data dir holds [ooh.lock, ooh_log.jsonl] (ooh_log.jsonl 3.12 sha256 1445a6aa…d74c).

**Control f5ee168** (rows_cf5ee168_3.12 = _3.13, 1a48ff14…aea7, identical across Pythons):
- **40 values are stored in clear in vendor_text** (201, `personal_data_redacted: false`, text as typed), and the same **40 are accepted on the face** (200, ledger Δ = 2). These are:
  - all 18 O11-1 non-shaped forms (12ー34…, the 41ー11… pairs, 1234ʼ56789 ×6, 1234ー5678ー9);
  - 4 other-letter forms (ǀ, ο, 々 pairs);
  - **18 entity forms**, including "4111&nbsp;1111&nbsp;1111&nbsp;1111", "123&ndash;45&ndash;6789", "&amp;nbsp;", "&amp;amp;ndash;", "&amp;#45;", "&#x31;…&#x39;", "123&#x2D;45&#x2D;6789", "&NonBreakingSpace;" and "41&#x30FC;…".
- Driver delta hits are 22–26 per stored case. The independent sweep finds 14 digit lines and **160 hash lines**.
- The old 65 are all redacted at the control, as omega11 found.
- **The cases discriminate.**

**Idempotency (INFO, O11-2 unchanged), both Pythons:** the same request_id with "contact 123-45-6789 …" and then "987-65-4321 …" -> 201, the same unit_id. The same request_id with non-PII text -> 409 REQUEST_ID_REUSED.

## 2. Short H-1 and start-up refusals (store.py byte-identical to f5ee168)
Driver: sw12.py = omega11 sw11.py with only `omega11` replaced by `omega12` and `common11` by `common12`. table.txt 668a8697…0dff.
- **e2 W** (3.12 and 3.13):
  - same-size fault (last line -> "y"×len), and fault_sha != orig_sha;
  - W3 -> **503 STORE_UNAVAILABLE, file unchanged**, anchors 2->3; audit ok False; W4 -> 503 INTEGRITY_UNVERIFIED;
  - put back -> job and audit ok; W3 retry 201, W5 201, restart ok, write 201;
  - FINAL 5 lines = 5 anchors, /verify valid, tenants [t-before, t-mid, t-x, t-z, t-after].
- **m3 I** (both): audit ok False; W3 and W4 -> 503 INTEGRITY_UNVERIFIED, unchanged, anchors 2->2; put back ok; FINAL 5 = 5, valid.
- **fifo H** (3.12 and 3.13): A1, J1 and ST ok False; W3 and W4 503 INTEGRITY_UNVERIFIED; J2 ok False; put back -> ok, retry 201; FINAL 5 = 5, valid.
- **Control 75b0662 e2 W** (both): W3 -> **201** (file changed), audit ok, W4 201, FINAL 6 = 6 with t-y. The scenario discriminates.
- Totals: 10 non-201 candidate POSTs, and the log changed in 0 of them. The largest call took 0.364 s. Sampled service RSS was 68,848–81,824 kB.
- **Restart on the fault** (both Pythons, 10 runs): every one exited rc=1 within 0.225–0.27 s, with peak RSS 58,176–60,128 kB.
  - FIFO and directory: `StoreCorrupt: the log is not a regular file; refusing to start (inspect the data directory)`.
  - Symlink to identical bytes and symlink to /dev/zero: `…cannot be opened (OSError); refusing to start: it must be a regular file, not a link` (store.py:147). The symsame target was unchanged.
  - chmod 000: `…cannot be opened (PermissionError); refusing to start: inspect the data directory`.
  - These match omega11 at f5ee168.

## 3. Findings
- **No CRITICAL or HIGH. O11-1 is closed at runtime** for the 29 + 5 forms tried, which are stored at the f5ee168 control and redacted or refused at the candidate.
- **O12-1 (LOW): entity decoding is bounded at 3 passes** (pii.py:314 `for _ in range(3)`).
  - The probe "4111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111&amp;amp;amp;nbsp;1111" needs 4 passes to decode. At the candidate it was:
    - vendor_text 201 unredacted, stored as typed (ooh_log.jsonl line 999);
    - face 200, ledger Δ = 2 (line 1471);
    - doc_ref 422 (schema).
  - Why LOW: in every form up to the 4th decode, the digit groups are separated by ASCII letters ("amp", "nbsp"). That is the founder's accepted boundary class, like "4111 x 1111 …". One HTML render shows "&amp;amp;nbsp;", not a card. The 3-pass nesting "123&amp;amp;ndash;45…" is caught.
  - If the founder rules that entity nesting must be decoded to a fixed point, this becomes MEDIUM or HIGH and must be re-gated.
  - Reproducer: pii12.py boundary probe (runs/pii_c1a90498_*). Falsifier: decoding until unchanged (bounded by the length) would yield 201 redacted, kinds ["card"].
- **O12-2 (INFO): partial residues.**
  - "&#X34;&#X31;&#X31;&#X31;-1111-1111-1111" is kept as "&#X34;&#X31;&#X31;&#X[redacted]": 3 leading digits (411), entity-encoded.
  - The decimal-entity PAN is kept as "&#[redacted];".
  - Neither keeps 9 or more digits, and the sweep finds no target.
- **O12-3 (INFO): false positives observed**, as the design says, both Pythons:
  - Redacted in vendor_text (kinds ["number"]) and refused on the face (422): GPS "34.0522,-118.2437"; ZIP+4 pair; date range "2026-11-01 - 2026-11-30"; "io-2026-1018-0001" ("io-[redacted]"; doc_ref 422 ATTRIBUTE_VALUE_PERSONAL_DATA); "1,234,567,890 impressions"; "lat 34.052235 lon -118.243683" (the lon only).
  - **New with the ASCII-only rule:** a CJK date range "2026年11月01日〜2026年11月30日" -> "[redacted]日", and a Greek address "Οδός 12345 Τ.Κ. 6789" -> "Οδός [redacted]".
  - Not affected: "12345678" (doc_ref approved 200), "14x48 billboard on Route 101", "1,250,000円 / 4週", "&copy; 2026 Lamar".
- **O11-2 (INFO, carried):** a redacted-hash replay, as in §1.
- **O10-1 (LOW, carried):** the doc_ref schema refuses 110 of 120 entries before the PII rule runs.
- **O10-2 (LOW, carried):** a start-up refusal is an uncaught StoreCorrupt traceback (rc=1). It is prompt and fail-closed.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN and PRODUCTION_PROVEN are not claimed. No deployed SHA was read and no live endpoint was touched (127.0.0.1 only).
- pytest: none run by this gate. That belongs to AEGIS-TEST.
- Not run for PII:
  - vendor_text on endpoints other than unit create;
  - face text attributes other than location_description, and "ref"-kind attributes;
  - e-mail and phone shapes;
  - the /audit/evidence view;
  - doc_ref beyond the schema;
  - Unassigned (Cn) characters as separators: the founder's "kept" decision was not probed through HTTP;
  - non-ASCII letters beyond the 11 tried;
  - the request_sha cache (64 entries) under eviction or concurrency.
- Not run for the store: e2/m2/e3 beyond e2 W and m3 I, symsame and symzero while running, the HW modes, the 75b0662 I mode, a close() fault, races and concurrent writers, a large log, Linux/CI, and the other five services that share store.py.
- Disclosures: D1 (pytest gate at run start only, as in Execution conditions); D2 (the ledger binary hash differs from omega11's, as in Execution conditions).

## Raw evidence (omega12/runtime/)
- Runs: runs/<name>.out and runs/<name>/result.json, service.log, ledger.log, and the ooh/ and ledger/ dirs (22 runs).
- Tables and analyses:
  - rows_c1a90498_3.12.txt and _3.13.txt: ae0285c8…dc9
  - rows_cf5ee168_3.12.txt and _3.13.txt: 1a48ff14…aea7
  - pii_an_c1a90498_3.12.txt 7d85d24b…72cc, _3.13.txt f3632c8d…ec40
  - sweep_pii_c1a90498_3.12.txt 5b3d267e…6d6b, _3.13.txt 82701619…a941
  - sweep_pii_cf5ee168_3.12.txt 465d2714…e0ade, _3.13.txt 5864efa4…1d91
  - cum_explain.txt dd0605ec…b798
  - table.txt 668a8697…0dff
- Scripts (sha256): pii12.py 3f1f8033…5594, gen_pii12.py 6e387087…8020, common12.py cb11583a…b05, sw12.py 55ebd298…b7b4, sweep12.py f5f162c7…6f, an12.py 780baf9a…8a486, rows12.py 2e747ecd…1ac1, cum12.py 601d0426…3d3b5, lines12.py d15ac2eb…ad1, table12.py aa11e071…abe, batch12.sh 4d67b762…f3a, watchdog12.sh 7e9b1768…485.
- Leftovers: no omega12 process is left (`ps … | grep omega12/runtime|rt12-target|watchdog12` -> none). There were no candidate edits, commits or pushes.
