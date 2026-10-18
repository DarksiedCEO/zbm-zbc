# AEGIS SECURITY gate — omega10

- repoIdentity: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: 0f5fbe33642a62068239f4df83edd8602730fd8e
- treeOid: ffc3bd9caa09782a69e91d7466ce1b30eef16516 (frozen copy review-0f5fbe3 HEAD+tree identical; `services` tree byte-identical to my `git archive 0f5fbe3` copy)
- scope: `git diff 91ea9a1 0f5fbe3 -- services` (pii.py, service.py, store.py, svc_campaigns.py, 2 test files) + OOH2-PII-1 whole (unit vendor_text redacted, face text attribute refused, client_approval_doc_ref refused); gitleaks + personal-data check
- author: AG-AEGIS-SECURITY (fresh context, read-only on the candidate; experiments in my own `git archive` copy omega10/security/copy and the 99569bd copy omega9/security/copy-99569bd; in-process TestClient + FakeLedger + temp data dir only; nothing live). Python venv3.12 and venv3.13; every result below is the same on both.

## Verdict: BLOCK

One HIGH, blocking: F1 is only partially closed. The idempotency hash `request_sha` still equals the SHA-256 of the
**cleartext** request body whenever a vendor_text value is redacted in storage but the raw value does not satisfy
`pii.has_personal_data` — a set that includes a plausible double-spaced SSN ("123  45  6789"). The log then holds a
hash that is a confirmation oracle for the SSN, which is exactly the F1 defect and falsifies the fix's own docstring
("the hash kept in the log never stands for a card number or an SSN"). F2, F3, F5, F6 are CLOSED. F4 (13 other Ref
fields) is unchanged and remains open (OOH2-R9-SEC-F4); per the founder's scope it is reported, not blocking. The
start-up read (RT9-1) and the write-path O_NONBLOCK change hold. No secrets or personal data in history or tree.

---

## F1 — HIGH (blocking): request_sha still hashes the cleartext body when the storage redaction and the hash redaction disagree. Class: SECRET_LEAK. **F1 PARTIALLY OPEN.**

- Path: services/ooh-py/src/service.py:74 (`_screened`: `pii.redact(x) if k=="vendor_text" and isinstance(x,str) and pii.has_personal_data(x) else _screened(x)`), service.py:81-84 (`request_sha = payload_sha256(_screened(body))`), service.py:285 (`_req` stores `request_sha` in the line data). The divergent predicate is svc_campaigns.py:64 (`observed()` does `pii.strip_format` + a printable→space pass + `" ".join(...split())` **before** `pii.has_personal_data`), whereas `_screened` calls `pii.has_personal_data(x)` on the **raw** x. pii.py:162 `_SSN` requires a single separator per gap, so "123  45  6789" (two spaces) is not personal data to the raw screen, but `observed()` collapses the doubled space to one and does redact it.
- Executed (omega10/security/poc/f1_residual.py and poc/f1_divergent_route.py; poc-f1-residual.txt, poc-f1-divergent-route-venv3.12.txt; both Pythons):
  - raw `123  45  6789` → stored vendor_text (observed) = `contact [redacted] now` (looks safe); `has_personal_data(raw)` = False; `_screened` keeps it as `contact 123  45  6789 now`; **stored request_sha == payload_sha256(cleartext body) = True**. Same for `123 \t45 \t6789` and `123  45  6789`.
  - control raw `123-45-6789` → `has_personal_data(raw)` = True, hashed over the redacted body, stored request_sha != sha(cleartext) (False). F1 closed for the canonical form.
  - Route confirmation (poc-f1-divergent-route): POST inventory with vendor_text `contact 123  45  6789 now` → HTTP 201, on-disk ooh_log.jsonl line has vendor_text.text `contact [redacted] now` AND request_sha == sha(raw sent body) = True; the face text attribute line (key "value") is likewise hashed over the raw body (request_sha == sha(raw) True), because `_screened` only replaces the key `vendor_text`.
- Why HIGH: every other input to that hash is in the same log line in cleartext (request_id as the rk suffix, face_id, flight, the text around `[redacted]`). The only unknown is the 9-digit SSN (space ~10^9) plus a small whitespace-template multiplier. SHA-256 at commodity throughput makes this a minutes-to-hours offline recovery; one hash per guess confirms it (shown deterministically with the plaintext, so no search was run). This is a stored equivalent of an SSN, against ADR 0022 and against the fix's claim. Trigger is a plausible preparer typo (double space), not an exotic attack. **Recovery/brute force NOT RUN** (deliberately); the single-hash equality is the falsifiable oracle.
- Reproducer: `venv3.12/bin/python -I omega10/security/poc/f1_residual.py omega10/security/copy/services/ooh-py/src` ; route: `.../poc/f1_divergent_route.py omega10/security/copy/services/ooh-py /dev/null`.
- Falsifier: `_screened`/`request_sha` redact vendor_text with the **same** transform that `observed()` uses to decide storage redaction (e.g. hash over `observed(x)["text"]`, or screen `observed`-normalised text), OR key the hash (HMAC with a secret not in the data dir). Then `stored request_sha == sha(cleartext body)` prints False for the double-space / tab / NBSP SSN forms too, while idempotency still tells same-body from different-body.

## F2 — CLOSED: zero-width / format char inside a digit group. Class: SECRET_LEAK
- pii.py:67-75 `strip_format` (NFKC, Cf removed, non-ASCII Nd → ASCII digit) runs before storage: svc_campaigns.py:64 now feeds `pii.strip_format(text)` into the printable pass, so `observed()` no longer blinds the screen.
- Executed (poc-routes-venv3.12.txt vs omega9): the four omega9 ZW cards and `123-4​5-6789` that were LEAK at 91ea9a1 are now `clean` (redacted, reached=-). 121 passed in test_aegis_r9_pii_hardening + test_ooh2_pii1_card_ssn (both Pythons). Falsifier: any of ZW_CARDS stored with personal_data_redacted False.

## F3 — CLOSED: SSN shapes (dot/underscore/colon/slash/space, touching letters, TIN). Class: SECRET_LEAK
- pii.py:161-164 `_SSEP`/`_SSN`/`_SSN_WORDS` widened. Executed: every F3 form that was LEAK at 91ea9a1 — unit vendor_text, price/hold/contract vendor_text, face text attribute, vendor ref attribute, campaign name, client_approval_doc_ref — now redacts (201/[redacted]) or refuses 422 ATTRIBUTE_VALUE_PERSONAL_DATA (poc-routes diff: 33 rows flipped LEAK→clean). Falsifier: any of SSN_FORMS stored.

## F5 — CLOSED (card digits in other scripts): Arabic-Indic and Devanagari cards now redact (poc-pii-forms.txt `arabic spaced`/`devanagari spaced`/`mixed ascii/arabic` MISSED→CAUGHT). URL-encoded (`4111%201111%201111%201111`) and HTML-entity digits are still MISSED (pii.py:84 only maps `%40`→@) — this is the pre-existing LOW, unchanged; one such row still stores in unit vendor_text (poc-routes). Not blocking.

## F6 — CLOSED: names_a_card matches 99569bd again. pii.py:103 `_DIGIT_RUN` drops `re.I`. Differential 99569bd vs 0f5fbe3 (poc-names-a-card-diff.txt): 0 diffs over 200k random inputs (and 0 over 200k without U+0131); `4111ı1111ı1111ı1111` old==new==True. Falsifier: any diff between the two versions.

## F4 — MEDIUM, OPEN (out of founder scope this round; OOH2-R9-SEC-F4): 13 other Ref fields unscreened.
- Executed (poc-routes, bottom block): `4111-1111-1111-1111` and `123-45-6789` accepted/stored (reached=store,export) in sources.doc_ref, faces.vendor_face_ref, brief_doc_ref, prices.source_ref, hold-evidence vendor_ref, contracts io_doc_ref, and the SSN in funds evidence_ref. Identical to omega9. Not worse. Not blocking per the assignment. Falsifier: each returns 422 ATTRIBUTE_VALUE_PERSONAL_DATA.

## New-code attack surface (item 3)
- Further card/SSN spellings in the three fields: other separators, mixed scripts, combining marks, letters between digits, NFKC tricks, URL/HTML encoding (poc-pii-forms.txt, poc-routes). Everything caught by `observed()`/`parse_value` is also redacted/refused; the only residual stored forms are the pre-existing LOW URL/HTML-entity encodings and the human-separator cases (`41 11 1111...`, `4111x1111...`) that the department documents as undetectable (pii.py:26). None reach the three fields as a usable card except via F1's hash path (above).
- Screen-vs-storage divergence probe (poc-diverge.txt): the ONLY class where the two disagree is observed()→redacted / raw-screen→False (double space, space+tab, NBSP, CR LF, private-use/unassigned separators). That drives F1. No case where the storage keeps a card/SSN that the raw screen would have caught.
- Regex cost (poc-regex-cost.txt): worst total over screen+redact+observed+request_sha at the 4000-char cap is 0.065 s (both Pythons); cost is linear in length across digit/dash/dot/email/ssn adversarial fillers. No catastrophic backtracking. Body cap 64 KiB (svc_campaigns.py:51), field cap DOC_MAX 4000 (models.py:24). Not blocking.
- Start-up read `_read_at_start` (store.py:137-153), poc-startup-read.txt: FIFO → refused "not a regular file" in 0.000 s (no block); symlink to regular / symlink to /dev/zero → refused at open (O_NOFOLLOW) "it must be a regular file, not a link"; directory → "not a regular file"; unix socket → refused at open; regular file → read, records intact, file_problem None. Thread-join 10 s guard never tripped. No symlink/FIFO/device/directory escape or hang.
- Write path O_NONBLOCK (store.py:227) + `_close_quietly` (store.py:61, :293): full suite 924 passed (venv3.12); the r9 hardening tests (incl. the failed-close and the FIFO/symlink/device/directory start-up tests) pass on both Pythons.
- Idempotency (poc-idempotency.txt): same body → 201 replay (same unit_id, log length unchanged); different ordinary text or different flight under the same request_id → 409 REQUEST_ID_REUSED; a clean body hashes/dedupes as before (`request_sha(clean) == payload_sha256(clean)`, test_r9_f1 passes). NOTE: two bodies that differ only in a redacted vendor_text value hash alike (card A vs card B vs literal "[redacted]" all equal) and are treated as a replay (201, same unit_id, no 409). This is by design (vendor_text is replaced before hashing) and is not itself a leak, but it means a different-card retry under a reused request_id is silently a replay rather than a 409 — recorded as observed behaviour, not a finding.

## gitleaks 8.30.1 (item 4)
- `git` 84a5377..0f5fbe3 (34 commits, 15.55 MB) with the candidate's .gitleaks.toml (sha256 40410abac2315fc3f0ea7c44676236d1ded64ecae0acf005ed0a1bfb7478e43d, identical to the frozen copy's): no leaks (exit 0). gitleaks-git.json empty.
- `dir` on the frozen copy (34.38 MB) with its own toml: no leaks (exit 0). gitleaks-dir.json empty.

## Personal data (item 4)
- `git grep -i -E "<USER>|thereal<USER>|/Users/[a-z]" 0f5fbe3 -- docs claude`: no `<USER>`, no owner e-mail, no personal home path. Whole tree: 0 `<USER>`, 0 `thereal<USER>@gmail.com`. The only `/Users/` is `/Users/runner/work/_temp` (GitHub runner, docs/ci.md:277) and prior report text quoting it.
- INFO (not a finding): `<OWNER-EMAIL>` appears as the commit-author identity in docs/claude handoff/report headers; it was already present at 99569bd and is the public commit author of the range, not introduced by this delta. `git diff 91ea9a1 0f5fbe3` added no real e-mail/SSN/home path (only `@pytest.mark` matched the e-mail pattern); test values are public test cards and 123-45-6789.

## Boundaries (denominator 10 declared / 10 tested)
| boundary | tested | method |
|---|---|---|
| request_sha no longer recovers a card/SSN by search (F1) | yes | single-hash oracle + on-disk log line; divergence probe — **FAILS for double-space/tab/NBSP SSN** |
| request_sha: two different bodies never hash alike / idempotent replay 200, 409 on different body | yes | poc-idempotency (201 replay, 409 diff; clean body hashes as before); caveat recorded |
| unit vendor_text redaction (F2/F3/F5 forms) | yes | route PoC, 29 forms, store+export+unit view |
| other vendor_text (price, hold-evidence, contract) | yes | route PoC |
| face text attribute / vendor ref attribute / campaign name | yes | route PoC, 422 refusals |
| client_approval_doc_ref | yes | route PoC, 422 refusals |
| other Ref fields (F4) | yes | route PoC, 7 fields × 2 (open, out of scope) |
| funds ref / names_a_card F6 | yes | 400k differential vs 99569bd |
| start-up read (symlink/FIFO/device/directory/race) + write O_NONBLOCK | yes | poc-startup-read, full suite 924 passed |
| secrets/personal data in history and tree | yes | gitleaks git+dir, git grep |

## NOT RUN
- Offline brute-force recovery of the SSN from request_sha (F1): not built, not run, deliberately. The single-hash equality (stored request_sha == sha of the known-plaintext body) is the oracle and is sufficient to establish the property.
- Linux-specific FIFO/O_RDWR behaviour (host is macOS).
- A real cross-process race between the start-up open and an attacker swapping the inode (S_ISREG on the opened fd covers the post-open swap; the open itself is O_NOFOLLOW|O_NONBLOCK).
