RUNTIME (H-1 re-witness, log-file hardening and start-up refusals, OOH2-PII-1 under the founder's digit-cluster rule): **REAL_PROTOCOL_LOCAL** for services/ooh-py at f5ee168.
VERDICT: **RUNTIME_GREEN_FOR_SCOPE**. No CRITICAL or HIGH finding.
- One MEDIUM finding, O11-1, needs a founder ruling. Digits split by Unicode *letter-category* characters that render as a dash or dot (e.g. U+30FC "ー") stay outside the rule when the grouping is not a card shape or 3-2-4. A full PAN in pairs was witnessed stored in clear.
- If the founder rules that such characters are separators, O11-1 becomes HIGH and this gate is BLOCK for OOH2-PII-1.

Binding:
- candidateSha f5ee168c9e9148241b709ec93acc660c88d08ba7 · treeOid da5fc085a0669ba5f92df726cfefcb28bd505cab
- subtree services/ooh-py/src 9edb05e6faabe0a692f557d0e42654a14dfbecf1
- subtree services/ledger-rust 4f1ab90e2f81245130d6eb87091745146f6398e6 (the same at 0f5fbe3 and 75b0662)
- repo DarksiedCEO/zbm-zbc, local branch ooh-dept-15-wave2 (`git rev-parse ooh-dept-15-wave2` = f5ee168…, the branch tip)
- controls:
  - 0f5fbe33642a62068239f4df83edd8602730fd8e (tree ffc3bd9c…, ooh-py/src e046f81a…), for PII
  - 75b0662823b867df3325d676b1dcf69abe37a425 (tree 67c15180…, ooh-py/src 6d8a4442…), for H-1
- In my copy (`git hash-object`), each blob equals `git rev-parse f5ee168:<path>`:
  - store.py 8d81ac82…
  - service.py 320dbb67…
  - pii.py 93f3ef93…
  - svc_campaigns.py f5291986…
  - svc_inventory.py 33bf4094…

Author: AG-AEGIS-RUNTIME (aegis-runtime gate agent, omega11 run). Fresh context, independent of the builder and of omega10.
- I read omega10's runtime.md and scripts only as a model.
- My drivers are new files in runtime/scripts/:
  - common11.py, sw11.py, summ11.py, table11.py and watchdog11.sh are derived from omega10's by recorded sed/patch edits;
  - pii11.py is new (the business-flow helpers are omega10 pii10.py lines 24–100, verbatim);
  - pii11b.py is pii11.py with only the value lists replaced;
  - piitable11.py, ctl11.py, sweep11.py and batch11.sh are new;
  - probe11.py and probe11b.py are in-process value selection, NOT evidence.
- No omega10 verdict is carried forward.

## 0. Binding checks
- Frozen worktree review-f5ee168: `rev-parse HEAD HEAD^{tree}` -> f5ee168…, da5fc085…; `status --porcelain` -> 0 lines. Main repo: only the pre-existing `?? .git.backup-preimport/` and `?? .idea/`.
- Every run used my own copies, from `git archive <sha> | tar -x`: runtime/src_f5ee168, runtime/src_0f5fbe3, runtime/src_75b0662.
- After all runs, I compared fresh `git archive` extracts with `diff -r`:
  - 0f5fbe3 and 75b0662: IDENTICAL.
  - f5ee168: identical except one stray `services/ooh-py/src/__pycache__/pii.cpython-312.pyc` (disclosure D1). With it removed, IDENTICAL.
- The code delta 0f5fbe3..f5ee168 under services/ooh-py/src:
  - pii.py: `screen_form`; the wider strip_format; `number_cluster_spans` (pii.py:242, a letter ends a cluster: `ch.isalpha()` at :246, CLUSTER_MIN_DIGITS = 9 at :239); `screen_free_text` (:272) and `free_text_has_personal_data` (:299).
  - service.py:77: `_screened` always replaces vendor_text by `screen_free_text(x)[0]`. request_sha has an in-memory 64-entry cache keyed by a digest of the body.
  - svc_campaigns.py:64: `observed` uses screen_free_text and adds `personal_data_kinds`. client_approval_doc_ref uses free_text_has_personal_data.
  - svc_inventory.py:73: kind "text" attributes use free_text_has_personal_data.
  - store.py: the start-up open error names ELOOP ("not a link"), else "inspect the data directory"; `_close_quietly` after the append fsync.

## Execution conditions
- Host: Darwin 25.6.0 arm64 (this Mac). Runs 2026-10-18, 17:04–17:19 UTC. 127.0.0.1 only, with ports from bind(0).
- Python: scratchpad venv3.12 (3.12.14) and venv3.13 (3.13.9).
- Dependency mode is REAL:
  - the real ledger-rust binary, built here (§1);
  - the real entrypoint `python -m api` (cwd = <copy>/services/ooh-py/src), which is api.main -> serve.run -> uvicorn, over real HTTP (httpx);
  - LEDGER_SERVICE_URL points at that ledger. There is no mock, relay or fake ledger, and OOH_NON_PRODUCTION is unset.
- PII request bodies were sent as exact bytes (`json.dumps(body, ensure_ascii=False)`), so the raw-body sha256 is known.
- Concurrency:
  - up to 4 independent streams in parallel (3.12/3.13 × PII/sw) on separate ports and data dirs (17:09–17:15), and 2–3 at other times;
  - one client per service, sequential calls.
- Timeouts: every client call has one (20 s; 30 s for PII calls; 0.2 s for the start-up /health polls).
- Memory caps:
  - the driver SIGKILLs a starting service above 512 MB;
  - watchdog11.sh (512 MB, 20 ms) never fired (runs/watchdog.log does not exist) and was stopped via runs/watchdog.stop.
- Batch: 62 runs recorded in runs/batch_3.12.log and runs/batch_3.13.log, **all rc=0**.

## 1. Ledger build (this gate, from my f5ee168 copy)
- `cd runtime/src_f5ee168/services/ledger-rust && SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo build --release --offline --locked --target-dir scratchpad/rt11-target`
  - rc=0, "Compiling ledger-rust v0.1.0 (…omega11/runtime/src_f5ee168/services/ledger-rust) … Finished `release` … in 7.13s" (runtime/cargo_build.log, sha256 7e3fa881…cb5).
- Binary: scratchpad/rt11-target/release/server, sha256 381735e1bbb08f4f5f67b91119e01666abd75da2a2c76b7872c3503390741a51, Mach-O 64-bit arm64.
  - The target dir is outside omega11: `find omega11/runtime -name target -type d` -> none.
  - All runs, including the controls, used this binary (same ledger subtree at all three SHAs).

## 2. H-1 at f5ee168 (both Pythons; same results, timing excepted)
Setup: W1 t-before and W2 t-mid (201), then 11 s past FORCED_MIN_S. The same-size faults are e2 (last line -> "y"*len), e3 (last line forged and self-consistent), m2 (line 1 -> "y"*len) and m3 (line 1 forged). All 32 e/m runs, candidate and control, recorded `same_size: true` and fault_sha != orig_sha.

**Mode W:** cf5ee168_{e2,e3,m2,m3}_W_{3.12,3.13}, 8 runs
- W3 -> **503 STORE_UNAVAILABLE**, **file unchanged**, anchors 2->3, pending kept.
- audit and job -> ok False ("pending line … cannot be written here").
- W4 -> 503 INTEGRITY_UNVERIFIED.
- Put back while running -> job and audit ok True (rolled forward). The W3 retry -> 201, W5 201, restart -> audit ok, write 201.
- FINAL: 5 lines = 5 anchors, ledger /verify valid, tenants [t-before, t-mid, t-x, t-z, t-after].

**Mode I:** cf5ee168_{e2,e3,m2,m3}_I_*, 8 runs
- audit, audit again, job and /status -> **ok False** ("the local log file differs…").
- W3 and W4 -> 503 INTEGRITY_UNVERIFIED, unchanged, anchors 2->2.
- Put back -> ok True. W3 retry 201, W5 201, restart ok, FINAL 5 = 5 and valid.

**Control 75b0662** (c75b0662_{e2,e3,m2,m3}_{W,I}_{3.12,3.13}, 16 runs):
- W3 -> **201** (file changed); audit and job -> **ok True**; W4 -> 201; FINAL 6 = 6, with the refused-at-candidate t-y accepted.
- The scenarios discriminate.

Across all candidate sw runs there were **56 non-201 POSTs, and the log file changed in 0 of them** (summ11.py and table11.py over runs/cf5ee168_*/result.json).

## 3. Hardening and start-up refusals (candidate, both Pythons)
**While running** (fault: unlink + mkfifo, or a symlink to the identical bytes, or a symlink to /dev/zero), 12 runs:
- Mode H: A1, A2, J1 and /status -> ok False; W3 and W4 -> 503 INTEGRITY_UNVERIFIED, anchors 2->2.
- Mode HW: W3 -> 503 STORE_UNAVAILABLE (pending kept), then integrity ok False, and W4 503.
- symsame: target_unchanged True.
- Put back -> ok True, retry 201, restart ok, FINAL 5 = 5 and valid, in all 12.
- No timeouts and no transport errors. The largest call on a faulted log was **0.175 s** (cf5ee168_m3_W_3.12 W3).
  - The largest call of any kind was 0.811 s: a 201 W1 on a regular file before any fault, under 4-stream load.
  - Sampled service RSS was 68,896–83,552 kB.

**Restart on the fault** (SIGTERM, place the fault, start `python -m api`, observe every 20 ms for ≤ 15 s), 10 runs:

| fault | 3.12 | 3.13 | last line |
|---|---|---|---|
| FIFO (rfifo) | rc=1 after 0.295 s, peak 59,184 kB | rc=1 after 0.473 s, 59,392 kB | `StoreCorrupt: the log is not a regular file; refusing to start (inspect the data directory)` (store.py:151) |
| symlink to identical bytes (rsym) | rc=1 after 0.311 s, 58,400 kB | rc=1 after 0.339 s, 59,344 kB | `StoreCorrupt: the log cannot be opened (OSError); refusing to start: it must be a regular file, not a link` (store.py:147); target unchanged |
| symlink to /dev/zero (rzero) | rc=1 after 0.32 s, 58,368 kB | rc=1 after 0.345 s, 59,472 kB | same "not a link" reason |
| directory (rdir) | rc=1 after 0.29 s, 58,656 kB | rc=1 after 0.513 s, 59,424 kB | "not a regular file" |
| **new: log chmod 000 (rperm)** | rc=1 after 0.272 s, 58,656 kB | rc=1 after 0.324 s, 60,080 kB | `StoreCorrupt: the log cannot be opened (PermissionError); refusing to start: inspect the data directory`. This is the changed non-ELOOP branch, witnessed |

Every start-up refusal was prompt (≤ 0.52 s) and bounded (≤ 60.1 MB).

## 4. OOH2-PII-1 under the founder's rule, through real HTTP (pii11.py; both Pythons)
Setup is through the API only, against the real ledger: tenant, PRINCIPAL determination, market readiness, and a vendor. Per value:
- **unit vendor_text** "faces west, contact <v> for access" -> expect 201, redacted, `personal_data_kinds` non-empty;
- **face location_description** "billing <v>" -> expect 422;
- **client_approval_doc_ref** = <v> on a campaign at PROPOSAL_SENT -> expect 422.

**65 card/SSN values** (pii11.py SSN + CARD), including:
- the five that got through at 0f5fbe3: "123 - 45 - 6789", "123--45--6789", "123..45..6789", "123,45,6789", "123|45|6789";
- "123  45  6789", ~ * + # ; ( ) %20 &#45; tab, NBSP, ideographic space, figure dash, "SSN 123 - 45 - 6789", "ITIN 912 - 70 - 1234";
- **hidden characters:** U+200B, U+2060, soft hyphen, BOM, CGJ U+034F, Hangul filler U+3164, private-use U+E000, a combining acute on digits, Mongolian VS U+180E, VS-17 U+E0100;
- **other-script digits:** Arabic-Indic, Devanagari, Bengali, Thai, full-width, mathematical bold, circled, superscript, mixed scripts;
- **cards:** 4111/5555/3782 in space, dash, " - ", comma, pipe, "..", double-space, semicolon, newline, U+02BC, zero-width, Arabic-Indic, Devanagari "/" and extended Arabic-Indic forms, and bare 16 digits.

Scan after every case (each of 219 per run), of every source:
- the sources: each file in OOH_DATA_DIR, each file in the ledger-rust store dir, `GET /ledger/entries`, service.log and ledger.log;
- both cumulatively and as the delta new since the case began, raw and \u-decoded;
- searched for:
  - the literal value;
  - its digits (NFKC, any script as ASCII), as a substring of digit runs with separators/format characters removed, AND of digit clusters joined by any non-letter characters;
  - sha256 of the raw body bytes, of the ensure_ascii body, of the canonical unredacted body (BUILD_CONTRACTS §2) and of the unredacted text (raw, NFKC, spaces collapsed).

Independent post-run sweep (sweep11.py -> pii_independent_sweep.txt): every file under each run dir, letter-free digit clusters, searched for 123456789, 912701234, 4111111111111111, 5555555555554444 and 378282246310005, plus all 681 recorded secret-case hashes.

**f5ee168 (pii_cf5ee168_3.12, _3.13; the 219 cases are identical across Pythons in status, reason, stored text and kinds):**
- vendor_text: **65/65 -> 201, `personal_data_redacted: true`, `personal_data_kinds` set** ("ssn" / "card" / "number"). The text is "faces west, contact [redacted] for access", or "… SSN [redacted] …", "… ITIN [redacted] …", "… ([redacted] …".
  - **Hash positive check:** for 65/65, sha256 of the canonical body with vendor_text replaced by the text as returned **is in ooh_log.jsonl**, while the raw-body and unredacted-body hashes are not. The request hash uses the redacted text only.
- face: **65/65 -> 422 ATTRIBUTE_VALUE_PERSONAL_DATA**, ledger Δ = 0.
- doc_ref: **65/65 -> 422**, ledger Δ = 0, state PROPOSAL_SENT and version unchanged; the final control approve with "io-acme-signed-1" -> 200.
  - 10 by ATTRIBUTE_VALUE_PERSONAL_DATA: 123--45--6789, 123..45..6789, 123-45-6789, 123.45.6789, 123_45_6789, 123456789, 4111-1111-1111-1111, 4111..1111..1111..1111, 5555.5555.5555.4444, 4111111111111111.
  - 55 by the schema pattern `^[A-Za-z0-9._:-]{1,100}$` (O10-1, unchanged).
- **Scan hits for the 65 values: 0, cumulative and delta, in every source.** The independent sweep finds 0 digit hits and 0 hash hits in both runs. Ledger /verify valid (1736 entries); the data dir is [ooh.lock, ooh_log.jsonl].

**Control 0f5fbe3 (pii_c0f5fbe3_*, both Pythons, identical):**
- vendor_text: **31 stored unredacted or hashed in clear**. These include all five named spellings, "123  45  6789" (redacted in the text, but **sha256 of the canonical unredacted body is in the log**: r10 F1), bare 123456789, U+200B/BOM/U+180E (stored as "123456789"), Devanagari " - ", Thai "|" and math-bold ",".
- face: the same 31 -> **200 accepted**, ledger Δ = 2.
- doc_ref: 123--45--6789, 123..45..6789 and 123456789 -> **200 CLIENT_APPROVED**.
- The independent sweep finds 123456789 and 912701234 in ooh/ooh_log.jsonl, and 111 secret-case hash lines.
- The cases discriminate (ctl_3.12.txt, ctl_3.13.txt).

**Idempotency under the redacted hash** (INFO, O11-2): the same request_id with "contact 123-45-6789 …" and then "contact 987-65-4321 …" -> both 201, **the same unit_id** (a replay; the second number is never stored). Then the same request_id with non-PII text -> 409 REQUEST_ID_REUSED. Both Pythons. This follows from "the hash uses the redacted text only".

**Add-on: letter-category separators (pii11b.py; both Pythons, identical)**. These are U+30FC KATAKANA-HIRAGANA PROLONGED SOUND MARK (Lm), U+0640 TATWEEL (Lm), U+02C9 MODIFIER LETTER MACRON (Lm), U+1427 CANADIAN SYLLABICS FINAL MIDDLE DOT (Lo) and U+A78F LATIN LETTER SINOLOGICAL DOT (Lo). All are `isalpha()`, and all render as a dash or dot.
- Cards 4-4-4-4 and SSN 3-2-4 with these separators: **caught** by the card/SSN label rules. vendor_text 201 redacted (kinds card/ssn); face 422.
- **Not caught:** "12ー34ー56ー789" and its 4 sibling separators, "1234ー5678ー9", and the full PAN in pairs "41ー11ー11ー11ー11ー11ー11ー11".
  - vendor_text -> 201, `personal_data_redacted: false`, stored as typed;
  - face -> 200 accepted (ledger Δ = 2);
  - doc_ref -> 422 (schema).
  - In runs/piib_cf5ee168_3.12/ooh/ooh_log.jsonl (sha256 5f2e6da0…b654), lines 15, 135, 143, 147, 197 and 201 hold these texts. Deleting the marks gives **123456789** (4×) and **4111111111111111** (2×). The raw and unredacted hashes are present too. This is O11-1.
- Control 0f5fbe3: all 12 of these vendor_text values are stored and all 12 face values are accepted, including the 4-4-4-4 and 3-2-4 forms the candidate catches.

**False positives and boundary (INFO, O11-3 and O11-4), both Pythons:**
- Accepted false positives, as the design says:
  - GPS "34.0522,-118.2437", ZIP+4 pair "90210-1234 90211-5678", date range "2026-11-01 - 2026-11-30" -> vendor_text redacted (kinds ["number"]), face 422, doc_ref 422 (schema);
  - "io-2026-1018-0001" -> vendor_text "io-[redacted]", face 422, doc_ref **422 ATTRIBUTE_VALUE_PERSONAL_DATA**.
  - Not affected: "12345678" (8 digits; doc_ref approved 200) and "14x48 billboard on Route 101".
  - The date-range scan hit in the unit delta is my scanner joining the unit's own `flight: ["2026-11-01","2026-11-30"]` field, not the redacted text.
- Boundary (letter separators, outside the rule by design): "4111 x 1111 x 1111 x 1111" and "123ⓐ45ⓐ6789" (NFKC "123a45a6789") -> vendor_text stored, face 200, doc_ref 422 (schema).

## Findings
- **None CRITICAL or HIGH.**
- **O11-1 (MEDIUM, design boundary; needs a founder ruling).**
  - What happens: the cluster rule ends a cluster at any `str.isalpha()` character (pii.py:246). Unicode modifier letters and syllabic marks that render as a dash or dot therefore split the digits. Unless the grouping is a card shape or SSN 3-2-4, the value passes, so a PAN or nine SSN digits in pairs or other groupings are stored in clear (vendor_text) or accepted (face text attribute).
  - Why not HIGH:
    - the founder's rule is literally "non-letter separators", and these are letters to Unicode;
    - standard spellings with the same characters are caught;
    - plain-letter separators ("41x11x…") are the same accepted class.
  - If the founder rules that dash- or dot-looking Lm/Lo characters are separators, this is HIGH and the gate is BLOCK for OOH2-PII-1.
  - Reproducer: pii11b.py (runs/piib_cf5ee168_*), e.g. POST …/inventory with vendor_text "contact 41ー11ー11ー11ー11ー11ー11ー11 for access" -> 201 unredacted.
  - Falsifier: a cluster rule that treats non-ASCII Lm, or characters with no case mapping, as separators would redact it (expect 201 redacted, kinds ["card"]).
- **O11-2 (INFO):** the same request_id with a different SSN replays the first response (201, same unit) rather than 409. This is an accepted consequence of hashing redacted text. Reproducer: pii11.py §4 ("idem").
- **O11-3 (INFO):** the accepted false positives were observed as listed above (GPS, ZIP+4, date ranges, and dashed references with ≥ 9 digits).
- **O10-1 (LOW, carried):** doc_ref's schema refuses 55/65 (and all pii11b) values before the PII rule runs. The rule is witnessed on doc_ref for 10 ASCII forms.
- **O10-2 (LOW, carried):** a start-up refusal is still an uncaught StoreCorrupt traceback (rc=1). It is prompt and fail-closed.
- H-1: not reproduced at f5ee168; the control 75b0662 still accepts. RT9-1 and r8 M-1: still closed for the witnessed cases.

## Disclosures (my own process)
- D1: my in-process probes (probe11.py, NOT evidence) ran with `-I`, which ignores PYTHONDONTWRITEBYTECODE. That wrote `src_f5ee168/services/ooh-py/src/__pycache__/pii.cpython-312.pyc` at 17:01:45 UTC, before any HTTP run.
  - Its recorded source mtime and size equal pii.py's, so it is bytecode of the unmodified source; the 3.12 services may have loaded it.
  - I removed it, and runtime/scripts/__pycache__ too, after the runs. The fresh `git archive` diff is then IDENTICAL.
- D2: I added the `piib` entry to batch11.sh while batches were running, using `sed -i` (a rename to a new inode; running shells kept the old file). Every started batch logged its "end" line and all 62 run lines are rc=0. The final sha256 is 2d318b28…055e.
- D3: summ11.py was patched after the runs to skip PII result files (a KeyError on the new format). It only affects the summary.

## Raw evidence (runtime/)
- runs/<name>.out (62 timestamped logs), runs/<name>/result.json, ledger.log, service.log, and the ooh/ and ledger/ dirs.
- Tables:
  - table.txt db0fc530…a2984
  - summary.txt d8319ed6…a85e2
  - pii_table.txt f3128bf1…709a
  - ctl_3.12.txt 3719d0ae…20fc
  - ctl_3.13.txt b6439a97…353
  - pii_independent_sweep.txt da874420…dacde
  - sweep_pii_cf5ee168_3.12.txt e7055572…19ba
  - sweep_pii_cf5ee168_3.13.txt 9beec59f…adc6
  - batch_3.12.log 87804d36…2860
  - batch_3.13.log 94f0b379…4850
- Scripts (sha256):
  - pii11.py 37a32db0…12a8
  - pii11b.py b6de9a7f…1ff1a
  - sw11.py 64263388…0f50f
  - common11.py c0d38ce9…032f
  - sweep11.py 8fddae94…84c8
  - ctl11.py 4ae38c7b…aa46f
  - piitable11.py 94efdb54…12d8
  - table11.py 71fcf00d…3823
  - summ11.py 3f877a26…c8e
  - watchdog11.sh 782ad680…106
  - probe11.py ecbde2d9…5e73
  - probe11b.py 39947e18…2f47

## Leftovers / external
- No live or production endpoint was read or written. Only 127.0.0.1 was used.
- No candidate edit, commit or push.
- `ps -axo pid,command | grep -E "omega11/runtime|rt11-target|watchdog11"` -> none.
- No `target` dir under omega11; no __pycache__ left.

## NOT RUN / NOT PROVEN
- LIVE_PROVEN / PRODUCTION_PROVEN: not claimed. No deployed SHA was read, and no live provider was involved.
- pytest (test_ooh2_pii1_cluster_rule.py, test_aegis_r10_screen_form.py, …): AEGIS-TEST's job.
- Not run for PII:
  - vendor_text on endpoints other than unit create (evidence, prices, contracts all call `observed`, but were not exercised);
  - face text attributes other than location_description;
  - "ref"-kind attributes (still the old has_personal_data);
  - e-mail and phone redaction;
  - the /audit/evidence "attempted" view;
  - doc_ref beyond the schema (O10-1);
  - Lm/Lo separators beyond the 5 tried, and 3-3-3 or other groupings beyond those listed.
  - The request_sha cache (64 entries) was not probed for eviction or concurrency.
- Not run for the store:
  - a close() failure after fsync (`_close_quietly`; no fault injection for close);
  - a FIFO with a live writer, a socket or a hard link;
  - TOCTOU races and concurrent writers;
  - a large log;
  - Linux/CI.
  - The 91ea9a1 RT9-1 controls were not re-run (closed at omega10; the delta does not touch that path except the message branch, witnessed in rperm).
- The other five services that share the store.py code were not exercised. O8-1 (the FORCED_MIN_S throttle) was not re-run.
