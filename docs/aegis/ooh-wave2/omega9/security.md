# AEGIS SECURITY gate — omega9

- repoIdentity: DarksiedCEO/zbm-zbc (local branch ooh-dept-15-wave2)
- candidateSha: 91ea9a1948253a3203150b38a8c2f2f839f232cd
- treeOid: 482eb8983853a8bdff118c16446474b0b16537da (checked: `git rev-parse 91ea9a1^{tree}`; the frozen copy review-91ea9a1 has the same HEAD and tree)
- scope: `git diff 99569bd 91ea9a1 -- services` (pii.py, store.py, svc_campaigns.py, 2 test files), plus gitleaks and the personal-data check as ordered
- author: AG-AEGIS-SECURITY (fresh context, read-only on the candidate; experiments in my own `git archive` copies `omega9/security/copy` (91ea9a1) and `omega9/security/copy-99569bd`); in-process TestClient and FakeLedger only; nothing live
- Python: venv3.12 and venv3.13. Every result below is the same on both (`diff` of the route tables: identical)

## Verdict: BLOCK

One HIGH (F1, THEORETICAL: the recovery was not run, see the note). Without F1 the verdict would be SECURITY_GREEN with Medium/Low findings. M-1 and L-2 from omega8 are **closed**. No new bypass of the H-1 compare was found.

What holds (executed): in the three fields of the finding, these are refused or redacted, and nothing reaches the log, the pending files, a ledger event, /audit/export or the unit view: card numbers with space, dash, dot, underscore, colon, slash, en dash, full-width digits and dashes, zero-width characters between groups, Amex 4-6-5, 19 digits, a card followed by more digits, `io-` and JSON-like wrappers; SSNs `ddd-dd-dddd` with any dash, `SSN 123456789`, `social security number: 123 45 6789`. The same is true for price, hold-evidence and contract vendor_text, the face text attribute, the vendor ref attribute and the campaign name (`poc-routes-venv3.12.txt`, the `clean` rows). Controls: ordinary refs and ABA routing/trace references in the funds reference are accepted as before (200), and a card in the funds reference is still refused 422 FUNDS_METHOD_CONFLICT.

## Findings

### F1 — HIGH (THEORETICAL, blocking): the log keeps an unkeyed SHA-256 of the raw body, so a redacted card or SSN can be recovered offline. Class: SECRET_LEAK
- Path: services/ooh-py/src/service.py:67 (`request_sha = payload_sha256(body)`), service.py:269 (`_req` stores `request_sha` in the line data), ledger.py:67-70 (plain SHA-256 of sorted JSON). svc_campaigns.py:417 redacts `vendor_text` in `data`, but `request_sha` is computed over the raw body.
- Executed (`poc/request_sha_raw.py`, `poc-request-sha.txt`, both Pythons): POST inventory with vendor_text `faces west, contact 4111 1111 1111 1111 for access` returned 201. The stored text is `faces west, contact [redacted] for access`, and the clear secret is not in the line. But the stored `request_sha` equals `payload_sha256(raw body)`: **True** for the card and for `123-45-6789`. It does not equal the hash over the redacted body (False). Every other input of that hash is in the same line: the request_id (as the `rk` suffix), face_id, the flight, and the text around `[redacted]`. `request_sha` is stripped from /audit/export (False), so the exposure is the log file (and the pending file, which holds the same line by design; not opened separately).
- Why HIGH: the only unknown is the secret, and the space is small: 10^9 for an SSN, and about 10^9 for a 16-digit card once the issuer prefix is known. The one measurement I made is generic SHA-256 throughput on this host: 1,000,000 hashes of a 159-byte input in 0.41 s on one core, with 14 cores. By arithmetic, one format is a search of minutes. So the line holds an equivalent of the PAN/SSN, which goes against "a card number must never be stored" (ADR 0022) and against the fix's own claim of "redacted before anything is written to the log". **Not executed:** I did not build or run a recovery, so this is labeled THEORETICAL, not exploited.
- Reproducer: `venv3.12/bin/python -I omega9/security/poc/request_sha_raw.py omega9/security/copy/services/ooh-py`
- Falsifier: `request_sha` is keyed (HMAC with a secret that is not in the data dir) or computed over the redacted body. Then `sha over raw body` prints False and no stored value depends only on the secret plus the visible fields. Idempotency must still tell same-body from different-body requests.

### F2 — MEDIUM: a zero-width or other format character inside a digit group defeats the vendor_text redaction. Class: SECRET_LEAK
- Path: svc_campaigns.py:64. `observed()` turns every non-printable character into a space **before** `pii.has_personal_data`, so `_norm`'s zero-width removal (pii.py `_ZW`) never sees them. A group split into pieces shorter than 3 digits is never joined (pii.py `_card_from`), and the 10-digit phone rule no longer sees a run.
- PoC (`poc-zw-observed.txt`): `pii.has_personal_data("4​111 1111 1111 1111")` = True, yet `observed()` keeps `4 111 1111 1111 1111`. The same happens with U+FEFF (`41 11 1111 1111 1111`), U+2060 (`4111 1111 1111 111 1`) and an SSN (`123-4 5-6789`). Route: POST inventory with `4​111 1111 1111 1111` returned 201 with `personal_data_redacted: false`, and the digits reached store, export and unit-view (`poc-routes-*.txt`). The face attribute path calls pii directly and refuses it, as it should.
- Falsifier: run pii (or `_ZW` removal) before the printable pass in `observed()`. Then the four inputs above come back `[redacted]`.

### F3 — MEDIUM: SSN shapes outside onboarding-py's two shapes are stored, in all three fields. Class: SECRET_LEAK
- Path: pii.py `_SSN` (dash only, not after `\w` or a dash) and `_SSN_VALUE` (cue words; the word must not be glued to the digits).
- PoC (`poc-routes-*.txt`): stored with 200/201 and reaching store and export:
  - client_approval_doc_ref: `123.45.6789`, `123_45_6789`, `123:45:6789`, `SSN123456789`, `io-123-45-6789`, `doc-123-45-6789`;
  - unit vendor_text (also unit view): `123.45.6789`, `123/45/6789`, `123_45_6789`, `SSN123456789`, `x123-45-6789`, `io-123-45-6789`, `TIN 123456789`; price, hold-evidence and contract vendor_text give the same result for `123.45.6789` / `SSN123456789`;
  - face text attribute, vendor ref attribute and campaign name: `123.45.6789` and `SSN123456789` accepted.
  - The ADR's accepted limit covers only "spaces or no separators and no SSN word" (`123 45 6789` is also stored, which is the accepted limit and not counted here).
- Falsifier: these inputs return 422 / `[redacted]`.

### F4 — MEDIUM: 13 other Ref fields are not screened at all (outside the three fields; pre-existing). Class: SECRET_LEAK
- `4111-1111-1111-1111` and `123-45-6789` are accepted and stored in sources.doc_ref, faces.vendor_face_ref, brief_doc_ref, prices.source_ref, hold-evidence vendor_ref and contracts io_doc_ref, and the SSN also in funds evidence_ref (200). The same `Ref` type is used unscreened in models.py:114,116,139,150,152,161,177,187,213,233,246,254,264,287,299,309,310. OPEN.md:72's fix plan says "in every free-text and ref field", so this fix does not meet that plan as written. Grade MEDIUM because the scope given was the three fields; if Ω reads OOH2-PII-1 by its OPEN.md fix plan, these fields are the original HIGH, not yet fixed. (The row also stays open by its own terms: the purge/re-key step is not built.)
- Falsifier: each of these fields returns 422 ATTRIBUTE_VALUE_PERSONAL_DATA for both values.

### F5 — LOW: card digits in non-ASCII scripts or URL-encoded are not seen (vendor text and text attributes only)
- `٤١١١ ١١١١ ١١١١ ١١١١` (Arabic-Indic), `4111 1111 1111 ١١١١`, Devanagari with spaces, and `4111%201111%201111%201111` are stored (vendor_text 201: store, export, unit-view; face text attribute 200). pii.py `_DIGIT_RUN` is `[0-9]`, and NFKC does not map these digits. Refs and campaign names reject them by their ASCII patterns. HTML-entity digits are also missed (`poc-pii-forms.txt`).

### F6 — LOW: `names_a_card` is not byte-for-byte unchanged in behaviour, but HTTP behaviour is unchanged
- pii.py `_DIGIT_RUN` adds `re.I`, so in Python `[^0-9a-z]` with `re.I` no longer treats U+0131 `ı` (and U+1D6A4, which NFKC turns into it) as a separator. The old rule refused `4111ı1111ı1111ı1111`; the new one accepts it. Differential over 200k random inputs (`poc-names-a-card-diff.txt`): 5,747 diffs, every one with U+0131; 0 diffs in 200k inputs without it. An exhaustive scan of all code points (`poc-rei-letters.txt`) finds only those two. Through the route the funds `evidence_ref` pattern is ASCII-only, so the input is refused 422 by the schema (`poc-routes` funds rows). The card refusal in the funds reference is unchanged at the HTTP level; the claim "names_a_card unchanged" is false at the function level.
- Falsifier: drop `re.I` (names_a_card already casefolds), and the differential shows 0 diffs.

### Item 2 — omega8 M-1 / L-2 re-check (store.py at 91ea9a1)
- **M-1 CLOSED.** dos.py fifo: `file_problem` returns "differs" at once; `still blocked after 3 s: False`; append refused, not blocked. devzero: "cannot be read (OSError)", RSS 20 MiB. Sparse 1 GiB: problem in 0.00 s, RSS 20→20 MiB. Service-level test_zz_aegis_sec8_poc.py: `job alive False | write alive False | audit alive False {'job': 200, 'audit': 200, 'write': 503}`, which is the predecessor's falsifier (the test, which asserts the hang, now fails on both Pythons) (`poc-store-r8-rerun.txt`, `poc-store-fifo-devzero.txt`, `poc-fifo-service.txt`).
- **L-2 CLOSED.** matrix `symlink_same_bytes`: `file_problem=PROBLEM`, write refused.
- **No new H-1 bypass found.** The 16-row matrix: every file≠memory case gives PROBLEM and REFUSED; ok/COMMITTED appears only when file==memory (replaced_inode_same, edit_then_restore). Extra probes on the write path first (`poc/store_extra.py`, `poc-store-extra.txt`): a FIFO before any check, a directory, a unix socket, sparse 1 GiB, and the data dir swapped for a symlink to an edited copy (O_NOFOLLOW covers only the last component, but the byte compare catches it): all refused / PROBLEM, none hang. L-1 (TOCTOU between compare and pwrite) is unchanged: committed once, then caught by the next check and the next write (Low, still open).
- Full suite in my copy: 870 passed on both 3.12 and 3.13 (`pytest-suite.txt`).

### Item 3 — gitleaks 8.30.1
- `git` 84a5377..91ea9a1 (33 commits) with the candidate's `.gitleaks.toml` (sha256 40410aba…, identical to the frozen copy's): no leaks. With default rules: no leaks.
- `dir` on the frozen copy review-91ea9a1 with its own toml: no leaks. (A first run with the working tree's fix26b toml, which lacks the allowlist, reported the 2 deliberate `sk_test_51…SimulatedKey…` strings in finance-py, which the delta does not change; the candidate's toml allowlists them exactly.)

### Item 4 — personal data
- `git grep -i -E "<USER>|/Users/|thereal<USER>|gmail\.com" 91ea9a1 -- docs claude`: no `<USER>`, no owner e-mail, no home path. The only `/Users/` is `/Users/runner/work/_temp` (GitHub runner). Whole tree: 0 hits for <USER>. Placeholders: `<HOME>` 3511, `<USER>` 7, `<OWNER-EMAIL>` 4 lines.
- INFO: docs/aegis/round-26b/provenance.md:49-50 has the commit identity `<OWNER-EMAIL>`. It was already present at 99569bd and is not introduced by this delta; it is the public commit author of every commit in the range.
- 99569bd..91ea9a1 added lines: e-mails are only example.com and the test fixtures jane@163.com, jane@126.com, jane@lamar.com, bob@lamar.com; SSNs are only 123-45-6789 (and its U+2010 form); cards are only public test numbers (4111…, 4242…, 5555…, 3782…, 3530…, 2223…) and non-card controls. No real personal data (`pii-diff-scan.txt`, `pii-personal.txt`).

## Boundaries (denominator 9 declared / 8 tested)
| boundary | tested | method |
|---|---|---|
| unit vendor_text redaction | yes | route PoC, 29 forms, store+ledger+export+unit view |
| other vendor_text routes (price, hold-evidence, contract) | yes | route PoC, 4/4/3 forms |
| face text attribute / vendor ref attribute / campaign name | yes | route PoC, 7 forms each |
| client_approval_doc_ref | yes | route PoC, 16 forms |
| other Ref fields | yes | route PoC, 6 fields × 2 |
| funds ref false positives / names_a_card | yes | route rows + 400k differential |
| log integrity (M-1, L-2, H-1 compare) | yes | omega8 PoCs re-run + 5 new |
| secrets/personal data in history and tree | yes | gitleaks git/dir, git grep |
| redacted secret recoverable from stored derivative (F1) | **no** | stored hash = raw-body hash shown; recovery NOT RUN |

## NOT RUN
- Offline recovery from `request_sha` (F1): not built, not run, deliberately. F1 is therefore THEORETICAL.
- The pending file for a redacted unit was not opened on its own (it holds the same line by design).
- A real cross-process race for L-1; Linux FIFO/O_RDWR behaviour (macOS only); proofs vendor_text (svc_campaigns.py:774 route) was not called (it goes through the same `observed()` helper, shown by grep only); the idempotency 409/200 oracle on a guessed body was not exercised.
