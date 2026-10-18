VERDICT: SECURITY_GREEN
Binding: repo DarksiedCEO/zbm-zbc · branch ooh-dept-15-wave2 (local) · SHA 4a0a4b478a88fbaaeb864a7535779e586b39f64a · tree e0640db37e4a40f58e0a291fd21d90a56f45d1ec. Verified with `git rev-parse HEAD HEAD^{tree}` on the frozen worktree review-4a0a4b4, with 0 porcelain lines before and after the runs. PoCs ran on `git archive 4a0a4b4` extracted to security/src. Scope: the full diff 84a5377..4a0a4b4 (services/ooh-py, ADR 0022, docs). Newest delta: 5116917..4a0a4b4.
Author: aegis-security gate agent (AG-AEGIS-SECURITY). The gate wrote this report itself; the builder did not transcribe it. Nothing is carried forward from the 5116917 verdict: every item was executed again at 4a0a4b4. Paths are relative to S/aegis/omega5/security/.

## 0. Delta
- `git diff 5116917 4a0a4b4 -- services/ooh-py/src` is 0 bytes.
- The sha1 of all 20 .py files under src is identical to the 5116917 archive (src-sha1-*.txt).
- Both patch files on disk are byte-equal (cmp) to a fresh `git diff`.
- The delta touches 3 files:
  - ADR 0022 (+13 lines, prose about the mutation gate). Line 151, "a card number must never be stored", is unchanged.
  - docs/test-counts.md: 717 → 767.
  - tests/test_aegis_integrity_path.py: +622 lines, new.

## 1. Suite and secrets scan
- pytest on the archive:
  - 3.12: 767 passed, EXIT=0.
  - 3.13: 767 passed, EXIT=0.
  - Output: pytest-3.12.txt and pytest-3.13.txt.
- gitleaks 8.30.1:
  - `git --log-opts=84a5377..4a0a4b4` on the worktree: 24 commits, ~404 KB, 0 findings (gitleaks-git-84a5377..4a0a4b4.json = []).
  - `dir` on the archive tree: ~19.61 MB, 0 findings (gitleaks-dir-archive.json = []).

## 2. New test file (public repo)
- Read in full: 622 lines.
- Regex scan of the 636 added delta lines (delta-added-lines.txt). Patterns: e-mail, AWS, Stripe, GitHub and Slack keys, PEM, JWT, SSN, PAN, phone and URL shapes, and the words password, secret, token and api_key.
- Results: 2 hits, both `@pytest.fixture` or `@pytest.mark.parametrize` decorators, which are false positives. So the file has 0 secrets, 0 personal data and 0 e-mails.
- The only data: tenant ids, the fault strings "disk full", "fsync failed" and "unlink failed", "Bad Actor" or "a"*65 as actors, and fixed timestamps.
- File I/O goes only to the pytest tmp_path (lines 89, 96, 545). There are no network, subprocess or environment writes.
- It monkeypatches only objects of the per-test instance.

## 3. Route × identity matrix (poc/matrix.py)
- 58 routes, 57 of them with a captured 2xx example.
- 31 identities, giving 1798 cells.
- 0 mismatches on 3.12 and 0 on 3.13.
- matrix.tsv and matrix.313.tsv are byte-identical to the omega4 TSVs and to each other.

## 4. Boundaries (12 declared, 12 tested; omega4 harnesses re-pathed to omega5)
- attacks.py: "SUMMARY 98 held, 0 broken" on 3.12 and on 3.13. It covers:
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
- delta, volatile, extra, limit and pii_store: after normalizing ids and timestamps, the output is identical to omega4 on both interpreters.
- leaks.py: identical on 3.12. On 3.13 one check flipped, L1.no-vendor-text-or-pii-to-ledger: HELD → BROKEN. The investigation:
  - Diagnostic re-run (poc/leaks_l1diag.py): 80 runs on 3.13 gave 5 hits, all of them the needle "4111" inside a random hex event_id or payload_sha256. Example: `ooh-anc-f264db5b63779e8863b5ca4111e8…` (leaks_l1diag.bulk313.out). No value the attacker supplied was involved.
  - Conclusion: this is a substring false positive in my harness, not a leak.
  - Strict re-check (poc/leaks_l1strict.py; leaks_l1strict.bulk.out): it puts the needles against the summary, actor, type and department fields, the full PII strings against every field, and requires the hash and id fields to match their id patterns. It held 80/80 on 3.12 and 80/80 on 3.13.
  - The 2 remaining leaks.py BROKEN items (L2 view and local log) are the M-1 SSN and PAN finding.

## 5. Findings
**M-1 · MEDIUM · SECRET_LEAK (card/SSN persisted) · re-executed at 4a0a4b4, still open.** Code is unchanged. Locations: svc_campaigns.py:59-70, pii.py:1-3, 80, 96; svc_inventory.py:73; models.py:18, 168.
- Observed in pii_store.out and .313.out:
  - unit.vendor_text with the spaced PAN → 201, stored.
  - unit.vendor_text with SSN 123-45-6789 → 201, stored.
  - A face attribute holding the SSN → 200, stored.
  - client_approval_doc_ref with the dashed PAN (andre) → 200, stored.
  - /audit/export read as compliance_38: PAN True, SSN True. The unit view read as ooh_agent: PAN True, SSN True. The hub view: False.
  - Control: the PAN as a funds evidence_ref → 422 FUNDS_METHOD_CONFLICT.
- This contradicts ADR 0022:151.
- Why MEDIUM, not HIGH: only trusted internal callers write or read these values, and they never reach a tenant or the hub.
- Reproducer: `python poc/pii_store.py`.
- Falsifier: those requests redact or refuse both values.

**L-1 · LOW · TOKEN_REPLAY · still open.** extra.out X3: the run-together funds ref on another tenant, after the dashed one on acme, → HTTP 200. It is Andre-only, and the Finance (31) dedup holds.

**L-3 · LOW · PII hygiene · still open, unchanged.** Locations: tests/test_aegis_ooh_final.py:316 (jane@163.com, jane@126.com), :328 (jane@163.com) and :362 (bob@lamar.com, jane@lamar.com); src/pii.py:34; ADR 0022:126. None is in the new file.

No CRITICAL or HIGH findings. blocking=false.

## 6. NOT RUN
- Prior L-2 (a consistent-chain log rewrite) was not re-executed.
- The real ledger-rust over the network and uvicorn/serve.py HTTP parsing were not tested. Everything ran through the in-process TestClient against the FakeLedger, the scripted ledger or a MockTransport.
- Timing side channels were not tested.
- Concurrent-request TOCTOU and races were not tested.
- DoS and the slow-drip ledger were not tested.
- The 21 mutants the builder declared equivalent were not checked independently.
- The original leaks.py has not been changed and still carries the 1/16-per-run flake. The strict variant sits next to it.
