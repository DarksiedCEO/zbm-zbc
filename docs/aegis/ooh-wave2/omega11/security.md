# AEGIS SECURITY gate — omega11

- repoIdentity: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2 = f5ee168)
- candidateSha: f5ee168c9e9148241b709ec93acc660c88d08ba7
- treeOid: da5fc085a0669ba5f92df726cfefcb28bd505cab (frozen copy review-f5ee168: HEAD and tree identical; its `services` tree is byte-identical to my `git archive f5ee168` copy, `diff -r` empty)
- scope: OOH2-PII-1 design change (`git diff 0f5fbe3 f5ee168 -- services`: pii.py, service.py, store.py, svc_campaigns.py, svc_inventory.py + tests) in the three screened fields (unit vendor_text redacted, face text attribute refused, client_approval_doc_ref refused); the request hash; re-run of omega9/omega10 PoCs; gitleaks; personal data in docs/ and claude/
- author: AG-AEGIS-SECURITY (fresh context, read-only on the candidate). All experiments ran in my own `git archive` copy at omega11/security/copy (in-process TestClient, FakeLedger, temp data dir under omega11/security/poc/tmp). Nothing live. Public test values only (4111…1111, 4242…, 5555…4444, 378282246310005, 4000…0002, 123-45-6789). Every result was run on venv3.12 and venv3.13, and the outputs are identical (diffed).

## Verdict: SECURITY_GREEN (for the scoped claim). No CRITICAL or HIGH. Two MEDIUM and three LOW, all non-blocking, plus F4 (MEDIUM), which stays open and out of scope.

- omega10 F1 (HIGH: the raw-text screen and storage disagreed, so "123  45  6789" was hashed in clear) is **CLOSED**. omega9 F1–F3, F5 and F6 are **CLOSED**. F4 is **OPEN**, unchanged (OOH2-R9-SEC-F4, out of scope).
- The request hash is computed over the kept (redacted) vendor text only. Shown for 453 bodies; for 30 form shapes, different secrets give the same hash. Replay and 409 behave correctly.
- Under the new rule, the predecessors' 77 card/SSN forms give 76 redacted or refused. The one form kept has ASCII letters between groups, which is accepted by design.
- New gaps, all MEDIUM or LOW:
  - S11-1 (MEDIUM): Unicode letters that render as punctuation end a cluster.
  - S11-2 (MEDIUM): HTML named and hex entities are not decoded.
  - S11-3 (LOW): non-decimal numerals.
  - S11-4 (LOW): a number split across fields or requests.
  - S11-5 (LOW): normalisation-only differences now replay instead of answering 409.
- gitleaks found nothing. No personal data was introduced.

Paths below: `$O` = <scratchpad>/aegis/omega11/security; `$S` = <scratchpad>.

---

## Item 1 — attack on the cluster rule (pii.py:242-252 `number_cluster_spans`, :272 `screen_free_text`)

Executed:
- `$O/poc/rule_attack.py` → `poc-rule-attack-3.12.txt` / `-3.13.txt` (pii level, about 120 forms).
- `$O/poc/rule_routes.py` → `poc-rule-routes-*.txt` (route level: the exact form searched in the log and pending files, with every JSON line re-dumped using ensure_ascii=False, in the ledger events, in /audit/export and in the unit view).
- `$O/poc/card_short_groups.py` → `poc-card-short-groups-*.txt`.

The rule as built: after `_norm` (screen_form plus @/. spellings), a cluster ends at any `ch.isalpha()` (pii.py:246), which is **Unicode** alphabetic. Otherwise every `ch.isdigit()` is counted. The card and SSN label rules still run inside `_free_text_spans`. Their letter classes are ASCII-only (`_DIGIT_RUN` pii.py:132, `_SSEP` pii.py:192), so a non-ASCII letter between groups is a separator to them.

| class | outcome (all three fields) | reaches a persisted byte? |
|---|---|---|
| ASCII letters between groups (`4111x1111x1111x1111`, `123x45x6789`, `41111111 and 11111111`), and NFKC letterlike symbols that become ASCII letters (ℓ, №, ™, ℅, ᵃ, ʰ, ⅰ) | KEPT, as designed ("joined by non-letter separators"; ADR 0022 lines 302 and 332 document letters as not seen) | yes. vendor_text: log, pending, export, unit view. Face attribute: log, export (HTTP 200). client_approval_doc_ref: log, **ledger**, export (HTTP 200) |
| other-script letters (Greek ο, Cyrillic о, CJK 一, Hebrew ו, Arabic ا) between card groups or a 3-2-4 SSN | redacted / refused (labelled card or ssn: the label rules treat them as separators) | no |
| the same letters with an SSN grouped 4-5 (`1234ο56789`) | KEPT | yes (S11-1) |
| letter-category characters that render as punctuation: U+02BC ʼ, U+02D0 ː, U+02C8 ˈ, U+02B9 ʹ, U+0640 ـ, U+30FC ー, U+FF70 ｰ, U+01C0 ǀ, U+3005 々 | card 4-4-4-4 and SSN 3-2-4: redacted or refused. SSN 4-5 or 2-3-4, and a card in 2-digit or 1-digit groups (`41ʼ11ʼ11ʼ11ʼ11ʼ11ʼ11ʼ11`): **KEPT** | yes. **S11-1** |
| combining letter U+0363 (on a digit or after a space) | redacted | no |
| superscript, circled, mathematical, fullwidth, Arabic-Indic digits; ⓵ and ➊ (no NFKC mapping, but `isdigit`); fractions; 🄂 | redacted | no |
| Nl/No numerals that are not `isdigit` and have no NFKC digit (〇, Suzhou 〡–〩, counting rods U+1D360) | stored as typed (`4〇〇〇〇〇〇〇〇〇〇〇〇〇〇2`) | yes. **S11-3** (LOW) |
| Roman numerals Ⅰ–Ⅸ | NFKC gives ASCII letters; not digits | n/a |
| URL encoding (`%20` separators, `%34%31…` digits), HTML decimal entities (`&#52;…`) | redacted (the entity's own digits form a cluster). omega10's one LOW LEAK (`4111%201111%201111%201111`) is now clean | no |
| HTML named entities as separators (`4111&nbsp;1111&nbsp;1111&nbsp;1111`, `123&ndash;45&ndash;6789`) and hex entities (`&#x34;&#x31;…`) | KEPT | yes. **S11-2** |
| very long input (card at character 3980 of 4000; card after 650 characters; 4000 characters of `1-`; 400 × "12345678x") | card redacted wherever it is, before OBSERVED_MAX truncation. Worst `screen_free_text` cost at the 4000-character cap: 0.017 s (3.12) / 0.024 s (3.13), `poc-cluster-cost-*`. Old screen plus observed plus hash: 0.17 s worst (`rerun-regex-cost-*`), linear | no |
| split across two fields or two requests (`ref 12345` + `ref 6789`) | each part is stored (fewer than 9 digits each) | yes. **S11-4** (LOW) |

### S11-1 — MEDIUM (non-blocking): "letter" is Unicode `isalpha`, so punctuation-looking letters (Lm/Lo) end a cluster. Class: SECRET_LEAK
- Path: services/ooh-py/src/pii.py:246 (`if ch.isalpha():`). The label rules do not cover the gap for non-standard groupings: pii.py:132 and pii.py:192 (cards need groups of 3 or more digits to join; SSNs need 3-2-4).
- PoC (executed, route level, `poc-rule-routes-3.12.txt`): POST inventory with vendor_text `contact 1234ʼ56789 please` gives HTTP 201, `reached=log/pending,export,unit-view`. The same holds for `12ー345ー6789` and `1234ـ56789`. The face attribute `location_description` gives HTTP 200, `reached=log/pending,export`. pii level (`poc-card-short-groups-3.12.txt`): `card 41ʼ11ʼ11ʼ11ʼ11ʼ11ʼ11ʼ11 thanks` is KEPT with kinds=-. The controls `41 11 11 …` and `41-11-…` are redacted (`number`).
- Why MEDIUM, not HIGH: the cluster rule's founder wording excludes letters, and these characters are Unicode letters. A clear-text number needs both an unusual grouping and one of these characters. Standard card (4-4-4-4, 4-6-5) and SSN (3-2-4) groupings with them are caught. They still read as punctuation to a person: U+02BC is the apostrophe some autocorrect setups insert, and U+30FC is the long-vowel mark a Japanese input method gives for a dash.
- Reproducer: `$S/venv3.12/bin/python -I $O/poc/rule_routes.py $O/copy/services/ooh-py` and `… $O/poc/card_short_groups.py $O/copy/services/ooh-py/src`.
- Falsifier: end a cluster only at a letter of category Lu/Ll/Lt (or ASCII letters only), so that Lm and Lo marks used as punctuation are separators. Then every `KEPT` row above for U+02BC/02D0/02C8/02B9/0640/30FC/FF70/01C0/3005 prints `redacted`, and the route rows print HTTP 201 `[redacted]` and 422.

### S11-2 — MEDIUM (non-blocking): HTML named and hex entities are not decoded before the cluster screen. Class: SECRET_LEAK
- Path: services/ooh-py/src/pii.py:110-115 (`_norm` decodes only the @ and . spellings: `&#64;`, `&commat;`, `&#46;` …). The cluster ends at the entity name's letters (pii.py:246).
- PoC (executed, `poc-rule-routes-3.12.txt`): vendor_text `contact 4111&nbsp;1111&nbsp;1111&nbsp;1111 please` gives HTTP 201, `reached=log/pending,export,unit-view`. The same holds for `123&ndash;45&ndash;6789` and `&#x34;&#x31;…` (all 16 digits as hex entities). The face text attribute gives HTTP 200 for all three; log and export. Any HTML renderer of the export shows the card with spaces. The text is legible to a person even without rendering.
- Why MEDIUM: plausible when text is pasted from HTML source. It conflicts with "regardless of separator type" in spirit, but the separator contains letters, so it conforms to the rule as written. Ω may rule otherwise.
- Falsifier: `html.unescape` (or decoding numeric and named entities) inside `_norm` before the cluster count. The three rows then print `redacted` / 422.

### S11-3 — LOW: non-decimal numerals (〇, Suzhou 〡–〩, counting rods) are stored. Deliberate encoding only, the same class as spelled-out digits (pii.py:33). PoC: `4〇〇〇〇〇〇〇〇〇〇〇〇〇〇2` and `〤〡〡〡 〡〡〡〡 …` give HTTP 201/200, reached log, export and unit view. Falsifier: count characters with `unicodedata.numeric(ch)` defined as digits.
### S11-4 — LOW: a number split across two fields or requests, or around a word, is stored in parts. This is inherent to a per-value rule. PoC: two units with `ssn part 12345` and `ssn part 6789` both give HTTP 201, reached log and export. No falsifier short of cross-record correlation; recorded, not a defect of this change.

## Item 2 — the request hash (service.py:70-103)
- **Computed over the kept text only.** `_screened` replaces every `vendor_text` string with `pii.screen_free_text(x)[0]`, always (service.py:77). Executed in `$O/poc/hash_checks.py` → `poc-hash-checks-*.txt` over 453 bodies: 8 numbers × about 20 forms each (omega9/omega10 separators, double spaces, tab, NBSP, private use, zero width, %20, Arabic-Indic, fullwidth, SSN/TIN words), in 3 templates. In each, `request_sha(body) == payload_sha256(body with vendor_text = kept)`, `!= payload_sha256(raw body)`, and the kept text holds none of the number's digits. **Failing: 0.**
- **The hash carries no information about the redacted digits**, shown without search. In `$O/poc/hash_kept.py` → `poc-hash-kept-*.txt`, for all 15 card shapes × 4 PANs and 15 SSN shapes × 3 SSNs, the different secrets give the **same** request_sha within each shape ("shapes where different secrets hash differently: none"). The 5 distinct kept texts differ only by the form's own non-digit remnants (`SSN[redacted]`, `TIN [redacted]`, `x[redacted]`, `[redacted][redacted]`). A guess therefore confirms nothing, so the omega9/omega10 recovery has no oracle left.
- **The predecessors' recovery, retried:**
  - omega9 `request_sha_raw.py`, re-run → `rerun9-request-sha-raw-*`: stored sha == sha(redacted-text body) True; == sha(raw body) **False**; request_sha in /audit/export False.
  - omega10 `f1_residual.py` → `rerun-f1-residual-*`: for `123  45  6789`, `123 \t45 \t6789` and NBSP×2, `has_personal_data(raw)=True`, the stored and hashed text are both `contact [redacted] now`, and stored == sha(cleartext) **False**.
  - omega10 `f1_divergent_route.py` → `rerun-f1-route-*`: double space, private-use separator and dash all False; the face attribute is refused 422, with no line.
  - omega10 `diverge.py` → `rerun-diverge-*`: 16 of 16 rows `same` (the raw screen and storage agree).
- **No collision of different non-PII content.** The hash input is the body with only vendor_text replaced, and every other field is hashed as sent. Executed: `faces west` vs `faces east` → 409. A different flight_end → 409. `rate 100 200` vs `rate 100 300` → 409. These collapse, by design, to a replay:
  - bodies whose vendor_text differs only inside a redacted cluster (card A / card B / SSN / literal `[redacted]` → 201, same unit_id; ADR 0022 line 369 states this);
  - accepted false positives (`2026-11-01 - 2026-11-30` vs `2026-12-01 - 2026-12-31` → 201 replay);
  - normalisation-only differences (S11-5).
- **Idempotency:** same body → 201, same unit_id. Different body → 409 REQUEST_ID_REUSED (`poc-hash-checks-*`, and the omega10 `idem.py` re-run in `rerun-idem-*`: first, retry and restart replays, clean/clean-retry/clean-different 201/201/409).
- **Cache** (service.py:84-103): memory only, 64 entries, keyed by sha256(canonical(raw body)). `git grep` finds no reference to `_SHA_CACHE` outside service.py:84-103. Nothing writes it to disk or puts it in a response. After the 453-body sweep it held 64 entries. INFO: the in-memory key is an unkeyed hash of the raw body, held only in process memory, where the raw body also is.
- Nested vendor_text (price line): HTTP 201, card digits in the log False.

### S11-5 — LOW: normalisation-only differences in vendor_text now replay instead of answering 409. Class: UNKNOWN (idempotency semantics, not a leak)
- PoC (`poc-hash-checks-3.12.txt`): under one request_id, `Face A  west`, `Face A west`, `Ｆａｃｅ Ａ west` and `Face​ A west` all give 201 with the same unit_id. At 0f5fbe3 the first difference would have answered 409. The stored text is identical in every case (`screen_form`), so no different record is lost or forged. ADR 0022 line 369 states only the redacted-cluster case, not this one.
- Falsifier: 409 for a raw-text difference while the kept text is equal. Recorded only; the design choice (hash the kept text) makes this expected.

## Item 3 — predecessor PoCs re-run at f5ee168 (both Pythons, identical)
| finding | at | status now | evidence |
|---|---|---|---|
| omega10 F1 residual (HIGH): raw screen vs storage, `123  45  6789` | 0f5fbe3 | **CLOSED** | rerun-f1-residual-*, rerun-f1-route-*, rerun-diverge-* (16/16 same) |
| omega9 F1 (HIGH): unkeyed hash of the raw body | 91ea9a1 | **CLOSED** | rerun9-request-sha-raw-*, poc-hash-checks-*, poc-hash-kept-* |
| omega9 F2: zero width or format character inside a digit group | 91ea9a1 | CLOSED | rerun(9)-routes-*: all three-field rows clean; ZW forms in poc-hash-checks redacted |
| omega9 F3: SSN shapes | 91ea9a1 | CLOSED | rerun-routes-*: unit vendor_text 40 rows clean; face attribute 7/7 refused 422; client_approval 16/16 refused 422 |
| omega9 F5 / omega10 LOW: other scripts, URL-encoded, HTML decimal entities | 91ea9a1/0f5fbe3 | CLOSED for those encodings (omega10's one LOW LEAK row `4111%201111…` is now clean: leaks 17 → 16, and the remaining 16 are all F4/funds rows). HTML named and hex entities: see S11-2 | rerun-routes-*, poc-rule-attack-* |
| omega9 F6: names_a_card equals the 99569bd version | 91ea9a1 | CLOSED | rerun-names-a-card-diff-*: 0 diffs over 200k + 200k random inputs, 10/10 fixed same |
| omega9 F4: 13 other Ref fields unscreened (MEDIUM) | — | **OPEN**, out of scope (OOH2-R9-SEC-F4) | rerun-routes-*: the 13 rows sources.doc_ref, faces.vendor_face_ref, brief_doc_ref, prices.source_ref, hold vendor_ref, contracts io_doc_ref (card + SSN), funds evidence_ref (SSN) give reached=store,export, identical to omega10. The 3 funds ACH/trace rows are intended passes (names_a_card unchanged); the PoC's collapse heuristic flags them |
| predecessor forms under the new rule | — | 76/77 caught; the one KEPT is `4111x1111x1111x1111` (by design) | rerun(9)-pii-forms-newrule-* (`poc/forms_new_rule.py` rebinds the predecessors' script to `free_text_has_personal_data` / `screen_free_text`). The old API's own output (rerun-pii-forms-*) still lists 10 misses, but that API no longer decides for the three fields |
| start-up read (RT9-1), write path | 0f5fbe3 | holds | rerun-startup-read-*: FIFO, symlink, symlink→/dev/zero, directory all refused at 0.000 s. The store.py:146 message now distinguishes ELOOP ("not a link") from other errors ("inspect the data directory"). rerun-startup-read2-*: regular file reopened with 1 record; unix socket refused. rerun-store-extra-*: FIFO, directory, socket and sparse 1 GiB all REFUSED |

Test suite in my copy: venv3.12 **1136 passed, 1 failed**. The failure is `test_env_documented` (StopIteration: my first copy held only `services/`, so `docs/adr/0019-*.md` was absent). After adding `docs/` from `git archive f5ee168`, that file gives 2 passed (`pytest-env-documented-3.12.txt`). venv3.13 full suite with docs present: **1137 passed, 0 failed** (`pytest-suite-3.13-full.txt`; an earlier 3.13 output file, interleaved with a run cut off by a timeout, was discarded).

## Item 4 — gitleaks 8.30.1 and personal data
- Candidate `.gitleaks.toml` sha256 40410abac2315fc3f0ea7c44676236d1ded64ecae0acf005ed0a1bfb7478e43d, the same as the frozen copy's.
- `gitleaks git --log-opts=84a5377..f5ee168` (36 commits, 25.35 MB): **no leaks**, exit 0, `gitleaks-git.json` = `[]`.
- `gitleaks dir` on the frozen copy review-f5ee168 (43.84 MB): **no leaks**, exit 0, `gitleaks-dir.json` = `[]`.
- Personal data at f5ee168 (`git grep`):
  - `<USER>` and `thereal<USER>`: 0 in docs/, claude/ and the whole tree.
  - `/Users/<name>` in docs/ or claude/: 0 (except `/Users/runner`).
  - Scratchpad and uv paths in docs/aegis/ooh-wave2/omega10/data/out/*.json are written as `-<HOME>-…` and `<HOME>/.local/…`.
  - 905 files use <HOME>, <USER>, <OWNER> or <OWNER-EMAIL>.
  - E-mails added in 0f5fbe3..f5ee168 are fixtures (example.com, lamar.com, 126/163.com, `@pytest`), plus the fictional `kid.name@gmail.com` quoted from the WR-F005 example. INFO: `<OWNER-EMAIL>` appears only in docs/aegis/round-26b/provenance.md:49-50, a quoted raw commit header added in f8698e7, before the range. It is the public commit author identity (the same as `git log` for f5ee168) and was not introduced by this delta.

## Boundaries (denominator 9 declared / 9 tested)
| boundary | tested | method |
|---|---|---|
| unit vendor_text: 9+ digits of a card or SSN not stored in clear | yes | rule_attack (about 120 forms), rule_routes, card_short_groups, routes re-runs. Gaps S11-1–S11-4 |
| face text attribute refused | yes | same, route level (422 or 200 and reached) |
| client_approval_doc_ref refused | yes | route level, 16 + 6 forms |
| ledger payloads and summaries, /audit/export, unit view, pending files | yes | searched in every route PoC |
| request_sha covers only the kept text; no recovery | yes | 453-body sweep, 30-shape independence, omega9/omega10 recovery PoCs |
| no collision of different non-PII bodies; replay and 409 | yes | hash_checks groups, idem re-run |
| omega9/omega10 findings F1–F6 | yes | re-run table |
| start-up read and write path (store.py delta) | yes | startup_read, startup_read2, store_extra, suite |
| secrets and personal data in history and tree | yes | gitleaks git + dir, git grep |

## NOT RUN
- No brute-force or search recovery of any hash, deliberately. The hash's independence from the secret is shown directly: equal hashes across different secrets of each shape, which removes any oracle.
- Linux-specific FIFO and O_RDWR behaviour (the host is macOS). The `startup_read.py` unix_socket case hit AF_UNIX path-too-long under my scratchpad. The socket case is covered by `startup_read2.py` (refused).
- No production, live tenant or third-party target was touched. F4 fields were exercised only to show they are unchanged (out of scope).
