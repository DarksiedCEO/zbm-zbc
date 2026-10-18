VERDICT: SECURITY_GREEN (blocking=false; no CRITICAL/HIGH)
Binding: repo DarksiedCEO/zbm-zbc (public) · branch ooh-dept-15-wave2 (local) · SHA ce665c5b3aef1bcd56aacee9523559150dd8cb7b · tree 5530ba08de8a78fad4c004db4fedeb8c40b45a4a. Checked with `git rev-parse HEAD HEAD^{tree}` on the frozen worktree review-ce665c5; `git status --porcelain` = 0 lines before and after. Scope: services/ooh-py and the full diff 84a5377..ce665c5 (42 files, +7062/-179, 25 commits). Newest delta: 4a0a4b4..ce665c5.
Author: aegis-security gate agent (AG-AEGIS-SECURITY), fresh context. The gate wrote this report itself. Nothing is carried forward from the 4a0a4b4 verdict: every item below was run again at ce665c5. PoCs ran on `git archive ce665c5` extracted to security/src (never the worktree). Paths are relative to S/aegis/omega6/.

## 0. Delta
- `git diff 4a0a4b4 ce665c5 -- services/ooh-py/src` = 0 bytes. The sha1 of all 20 src .py files matches the 4a0a4b4 archive (security/src-sha1-ce665c5.txt, empty diff).
- Both patch files are byte-equal (cmp) to a fresh `git diff`.
- The delta touches 4 files: ADR 0022 (+21/-4, mutation-gate prose, and one "Open" item; line 151 "a card number must never be stored" is unchanged), docs/test-counts.md 767→777, tests/test_aegis_integrity_path.py (the `env` fixture now yields and closes the service), and tests/test_aegis_omega5_mutation.py (+152, new).

## 1. Suite and gitleaks 8.30.1
- pytest on the archive: 3.12 777 passed, EXIT=0. 3.13 777 passed, EXIT=0 (security/pytest-3.1x.txt).
- gitleaks found 0 in each scan:
  - `git --log-opts=84a5377..ce665c5` (25 commits, ~413 KB).
  - `git --log-opts=4a0a4b4..ce665c5` (~8.7 KB).
  - `dir` on the archive tree (~19.62 MB). It uses the repo .gitleaks.toml: useDefault=true plus exact-string allowlists for other services only, none for ooh.
  - `dir` on a copy of ooh-py and docs with no .gitleaks.toml, so default rules only (~2.1 MB). The copy was deleted after the scan.
  - JSON files: security/gitleaks-*.json, all `[]`.

## 2. New and changed test files and ADR delta (public repo)
- I read test_aegis_omega5_mutation.py in full, plus the fixture hunk and the ADR and test-counts hunks.
- Python regex scan of the 177 added lines (security/delta-added-lines.txt, delta-scan.out). It looked for: e-mail, AWS, Stripe, GitHub, Slack, PEM, JWT, SSN, PAN, phone and URL shapes, and the words password, secret, token, api_key, bearer and private. Result: 0 hits.
- So: 0 secrets, 0 personal data, 0 e-mails. The only data: tenant ids ("wire-1", "stuck-1", …), "Bad Actor", fault strings ("denied", "disk full", "unlink failed") and a fixed 5.0 s monotonic value.
- Side effects: monkeypatches are limited to the test (ST.os.unlink, SV.time, methods of the instance's log). Files go only to the pytest tmp_path. No network, subprocess or environment writes. The fixture change only releases the data-dir lock.

## 3. Route × identity matrix (poc/matrix.py)
- 58 routes, 57 with a captured 2xx example. 31 identities, 1798 cells.
- 0 mismatches on 3.12 and on 3.13.
- matrix.tsv and matrix.313.tsv are byte-identical to each other and to omega5's matrix.tsv.

## 4. Boundaries (12 declared, 12 tested)
- attacks.py: "SUMMARY 98 held, 0 broken" on both interpreters. It covers:
  - the founder gate;
  - tenant and IDOR crossover in paths and bodies;
  - escalation to Andre-only actions;
  - request-id replay;
  - vendor cost reaching the hub;
  - $10,000.00 → 201 and $10,000.01 → SPEND_LIMIT_EXCEEDED;
  - 7 d + 1 s → AUTHORITY_TOO_LONG;
  - signer ≠ andre → 422;
  - wire, check or card for client funds → 422;
  - no Finance (31) verification → FUNDS_NOT_FINANCE_VERIFIED;
  - FACE_ALREADY_ORDERED across tenants;
  - PA UNDETERMINED → PRINCIPAL_AGENT_NOT_CONFIRMED.
- delta, volatile, extra, limit, pii_store and leaks: after normalizing ids and timestamps, every output is identical to omega5 on 3.12. On 3.13, leaks.py L1 HELD this run, where omega5 had the known random-hex substring false positive.
- leaks_l1strict.py, 40 runs on each interpreter: 15 held, 2 broken every time (security/poc/leaks_l1strict.bulk.out). The 2 BROKEN items are M-1.
- New: poc/pii_keys_strict.py. Full Brief body plus one extra key, top-level and nested, 13 spellings: all 26 refused with 422.
  - snake/dash forms and `SSN` are refused by the personal-data key screen.
  - `phoneNumber`, `emailAddress`, `email`, `e_mail` and `creditCard` are refused only by `extra_forbidden`.
  - This is defense in depth only, not a finding: models.py has no free-form dict fields (grep).
  - It also corrects my harness: the omega5 L3 checks for those keys "held" only because `expected_version` was missing.

## 5. Findings
**M-1 · MEDIUM · SECRET_LEAK (card number and SSN stored) · re-executed at ce665c5 and still open.** The code is unchanged (svc_campaigns.py:59-70; pii.py; svc_inventory.py:73; models.py:18, 168).
- pii_store.out and .313.out show:
  - unit.vendor_text with the spaced PAN → 201, stored.
  - unit.vendor_text with SSN 123-45-6789 → 201, stored.
  - A face attribute holding the SSN → 200, stored.
  - client_approval_doc_ref "4111-1111-1111-1111" (andre) → 200, stored.
  - /audit/export read as compliance_38: PAN True, SSN True. The unit view read as ooh_agent: PAN True, SSN True. The hub: False.
  - Control: the PAN as a funds evidence_ref → 422 FUNDS_METHOD_CONFLICT.
- This contradicts ADR 0022:151.
- MEDIUM because only trusted internal callers write or read these values, and they never reach a tenant or the hub.
- Reproducer: `python poc/pii_store.py`.
- Falsifier: those requests redact or refuse both values.

**L-1 · LOW · TOKEN_REPLAY · still open.** extra.out X3: the run-together funds ref on another tenant, after the dashed one on acme, → HTTP 200. The action is Andre-only, and the Finance (31) dedup holds.
- Reproducer: `python poc/extra.py`.
- Falsifier: X3 → 409.

**L-3 · LOW · PII hygiene (real-domain e-mails in a public repo) · still open, unchanged.** The hits are in security/l3-emails.out:
- tests/test_aegis_ooh_final.py:316 (jane@163.com, jane@126.com), :328 (jane@163.com), :362 (bob@lamar.com, jane@lamar.com);
- src/pii.py:34;
- ADR 0022:126.
None is in the ce665c5 delta.

**L-4 · LOW · UNKNOWN (log integrity / availability) · new PoC of the item the ADR now lists as "Open".** poc/setaside_open.py, on 3.12 and 3.13:
- A `tenant_created` line with no tenant_id is put in pending → it is set aside.
- Its ooh anchor is then recorded → check 2 gives ok=True, the line is appended to the log, pending.discarded is cleared, and the next write → 201.
- On restart with the same data dir, startup raises `KeyError: 'tenant_id'` (service.py:116 → :139 → :298 → svc_tenants.py:201), so the service does not start.
- The ADR discloses the silent append. It does not disclose the restart crash.
- It fails closed (no wrong state is served). The preconditions are trusted-only: write access to the data dir AND a valid ooh-department anchor on the ledger (the ooh ledger token).
- Reproducer: `python poc/setaside_open.py`.
- Falsifier: check 2 reports ok=False ("an operator must inspect the log") and does not append.

No CRITICAL or HIGH findings. blocking=false.

## 6. Not run
- Prior L-2 (a consistent-chain log rewrite) was not run again.
- I did not test the real ledger-rust over the network, uvicorn/serve.py HTTP parsing, or TLS. Everything ran through the in-process TestClient against the FakeLedger, the counting or scripted ledger, or an httpx MockTransport.
- Timing side channels; concurrent-request TOCTOU and races; DoS and the slow-drip ledger.
- The 17 mutants the builder still calls equivalent were not checked independently. I did not run a mutation tool.
- I did not check whether the 777 tests kill the 13 mutants named in the ADR. That belongs to the mutation gate.
- I did not scan the full history before 84a5377 with gitleaks.
- The original leaks.py has not been changed and still carries the 1/16-per-run hex flake. The strict variant sits next to it.
