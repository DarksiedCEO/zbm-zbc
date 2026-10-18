VERDICT: SECURITY_GREEN (blocking=false; no CRITICAL/HIGH)
Binding: repo DarksiedCEO/zbm-zbc (public) · branch ooh-dept-15-wave2 (local) · SHA 75b0662823b867df3325d676b1dcf69abe37a425 · tree 67c15180382fab1a391b78756467c1a79a934973. Checked with `git rev-parse HEAD HEAD^{tree}` on the frozen worktree review-75b0662; `git status --porcelain` = 0 lines before and after. Scope: services/ooh-py and the full diff 84a5377..75b0662 (26 commits). Newest delta: ce665c5..75b0662.
Author: aegis-security gate agent (AG-AEGIS-SECURITY), fresh context. The gate wrote this report itself. No verdict is carried forward from ce665c5. The omega6 harnesses were reused, and every item below was run again at 75b0662. PoCs ran on `git archive 75b0662` extracted to security/src, never the worktree. Paths are relative to S/aegis/omega7/.

## 0. Delta
- `git diff ce665c5 75b0662 -- services/ooh-py/src` = 0 bytes. The sha1 of all 20 src .py files equals the ce665c5 archive (security/src-sha1-75b0662.txt; only the "./" prefix differs).
- A fresh `git diff` is byte-equal (cmp) to both supplied patch files.
- 4 files changed:
  - ADR 0022 (+17/-1). It now names the restart KeyError and adds the mutation-gate section. Line 151 "a card number must never be stored" is unchanged.
  - docs/test-counts.md: 777 → 792.
  - tests/test_aegis_integrity_path.py: the `env` fixture also calls `data_dir_lock.release()`.
  - tests/test_aegis_store_write_path.py: new, 280 lines.

## 1. Suite and gitleaks 8.30.1
- pytest on the archive: 3.12 792 passed, EXIT=0. 3.13 792 passed, EXIT=0 (security/pytest-3.1x.txt). This matches test-counts.md:32.
- gitleaks found 0 in each scan (security/gitleaks-*.json, all `[]`):
  - `git --log-opts=84a5377..75b0662`: 26 commits, ~425 KB.
  - `git --log-opts=ce665c5..75b0662`: ~12.2 KB.
  - `dir` on the archive tree: ~19.63 MB, with the repo .gitleaks.toml. That file is unchanged in 84a5377..75b0662.
  - `dir` on a copy of ooh-py and docs with no .gitleaks.toml, so default rules only: ~2.12 MB. The copy was deleted after the scan.

## 2. New and changed test files and ADR delta (public repo)
- I read test_aegis_store_write_path.py in full, plus the fixture, ADR and test-counts hunks.
- The data is only synthetic: tenant ids (open-1, fs-1, g-1..3, sw-1, w-1, ft-1/2, dur-1, pw-1, pr-1, over-1), "andre", a fixed timestamp, errno strings (denied, EIO, ENOSPC), and b"planted"/b"x"/b"y" tails.
- Side effects: monkeypatching is limited to the test, on `store.os` only. Files go only to the pytest tmp_path. No network, subprocess or environment writes.
- Regex scan of the 301 added lines (security/scan_added.py → security/delta-scan.out): HITS 0. It looked for e-mail, AWS, Stripe, GitHub, Slack, PEM, JWT, SSN, PAN, phone and URL shapes, and the words password, secret, token, api_key, bearer and private.
- So: 0 secrets, 0 personal data, 0 e-mails.

## 3. Route × identity matrix (security/poc/matrix.py)
- 58 routes, 57 with a captured 2xx example (missing: []). 31 identities, 1798 cells.
- 0 mismatches on 3.12 and on 3.13.
- matrix.tsv equals matrix.313.tsv, which equals omega6's matrix.tsv (cmp).

## 4. Boundaries (12 declared, 12 tested)
- attacks.py: "SUMMARY 98 held, 0 broken" on both interpreters. It covers:
  - the founder gate;
  - tenant and IDOR crossover in paths and bodies;
  - escalation to Andre-only actions;
  - request-id replay;
  - vendor cost reaching the hub;
  - the $10,000.00 / $10,000.01 limit;
  - 7 d + 1 s authority;
  - signer ≠ andre;
  - client funds by wire, check or card;
  - no Finance (31) verification;
  - FACE_ALREADY_ORDERED across tenants;
  - PA UNDETERMINED → blocked.
- attacks, delta, volatile, extra, limit, pii_store and leaks, on 3.12 and 3.13: after normalizing hex ids and timestamps, every output is identical to omega6.
- pii_keys_strict: 26/26 refused with 422, identical to omega6.
- leaks_l1strict, 40 runs on each interpreter (security/poc/leaks_l1strict.bulk.out):
  - 79 runs gave 15 held, 2 broken. The 2 BROKEN items are M-1.
  - 1 run on 3.13 gave 14/3: an extra `L1s.no-pii-needles-in-text-fields` failure.
  - I diagnosed it with leaks_l1strict_diag.py, 240 runs (security/poc/leaks_l1strict_diag.out). It reproduced once, on 3.12: the needle "4111" matched inside a random hex id in a ledger summary (`source:ooh-src-64111672a45056d543450…`). That is a harness false positive, not PII.
  - `L1s.no-full-pii-strings-anywhere` held in all 80 runs.

## 5. Findings (re-checked at 75b0662)
**M-1 · MEDIUM · SECRET_LEAK (card number and SSN stored) · still open.** The source is identical to ce665c5.
- pii_store.out and .313.out show:
  - unit.vendor_text with the spaced PAN → 201, stored.
  - unit.vendor_text with SSN → 201, stored.
  - A face attribute holding the SSN → 200, stored.
  - client_approval_doc_ref "4111-1111-1111-1111" (andre) → 200, stored.
  - /audit/export read as compliance_38: PAN True, SSN True. The unit view read as ooh_agent: PAN True, SSN True. The hub: False.
  - Control: the funds evidence_ref PAN → 422 FUNDS_METHOD_CONFLICT.
- This contradicts ADR 0022:151.
- Reproducer: `python security/poc/pii_store.py`.
- Falsifier: those requests redact or refuse both values.

**L-1 · LOW · TOKEN_REPLAY · still open.** extra.out X3: the run-together funds ref on another tenant → HTTP 200. The action is Andre-only, and the Finance (31) dedup holds.
- Reproducer: `python security/poc/extra.py`.
- Falsifier: X3 → 409.

**L-3 · LOW · PII hygiene (real-domain e-mails in a public repo) · still open, unchanged.** The hits are in security/l3-emails.out:
- tests/test_aegis_ooh_final.py:316 (jane@163.com, jane@126.com), :328 (jane@163.com), :362 (bob@lamar.com, jane@lamar.com);
- src/pii.py:34;
- ADR 0022:126.
None is in the ce665c5..75b0662 delta. The delta adds 0 e-mails.

**L-4 · LOW · UNKNOWN (log integrity / availability) · defect still open; disclosure gap closed.** setaside_open.py on 3.12 and 3.13:
- check 1 is ok and the line is set aside.
- check 2 gives ok=True: the log grew by 1, the malformed line was appended, and discarded was cleared.
- The next write → 201.
- Restart raises `KeyError: 'tenant_id'` (service.py:116 → :139 → :298 → svc_tenants.py:201).
- ADR 0022 (delta, ~line 217) now names the restart failure and its preconditions (data-dir write access plus a ledger-valid anchor). This is accurate against the PoC.
- Reproducer: `python security/poc/setaside_open.py`.
- Falsifier: check 2 reports ok=False and does not append.

No CRITICAL or HIGH findings. blocking=false.

## 6. Not run
- Prior L-2 (a consistent-chain log rewrite) was not run again.
- I did not test the real ledger-rust over the network, uvicorn/serve.py HTTP parsing, or TLS. Everything ran in-process with TestClient against the FakeLedger, the counting ledger, or httpx MockTransport.
- Timing side channels; concurrent-request TOCTOU and races; DoS.
- I did not check whether the 15 new tests kill the mutants they name (T144…T286). That belongs to the mutation gate. I checked only that they pass and contain no sensitive data.
- I did not scan the full history before 84a5377 with gitleaks.
- The leaks_l1strict needle check keeps its 4-char hex false positive (about 1 in 100–240 runs). I did not fix the harness.
