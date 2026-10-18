# AEGIS SOURCE gate at ce665c5 (transcribed)

> Transcription note: the SOURCE gate's role contract forbids writing report files. This is the gate's final
> hand-back, copied by the builder without edits to its findings. The gate's own pytest tails are in `source/`.

candidateSha: ce665c5b3aef1bcd56aacee9523559150dd8cb7b
treeOid: 5530ba08de8a78fad4c004db4fedeb8c40b45a4a
repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2, diff base 84a5377
Author: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), read-only, fresh context.

VERDICT: SOURCE_GREEN (advisory to Ω). No CRITICAL or HIGH finding. blocking=false.

## Not run
- Python 3.12 and 3.13 ran with the frozen worktree's venvs.
- No mutation testing. The 13 mutants were not re-run, so the gate has not shown that each new test kills its mutant. It judged the tests by reading them against the code.
- Other services' suites were not run.
- No live-ledger or CI/macOS runs.
- The 8461-line full diff was not read line by line. The 13 changed src files were swept by grep, and only the service.py and store.py regions the delta touches were read in full.

## 1. Binding and suites
- Worktree HEAD ce665c5b3aef1bcd56aacee9523559150dd8cb7b, tree 5530ba08de8a78fad4c004db4fedeb8c40b45a4a, clean.
- Python 3.12.14, `pytest -q -p no:cacheprovider` in services/ooh-py: 777 passed, 1 warning (Starlette httpx deprecation), 102.41s.
- Python 3.13.9, same command: 777 passed, 1 warning, 110.79s.
- The last 8 lines of each run, headed with SHA and tree, are in source/pytest-3.12.txt and source/pytest-3.13.txt. Full per-test output was not kept.

## 2. Delta truthfulness
- 777 is true: both interpreters ran 777, and the new file has 10 `def test_` functions. The docs/test-counts.md change from 767 to 777 matches.
- The delta is tests and docs only (4 files). `git diff --stat 4a0a4b4 HEAD` shows no src change, so there is no runtime-path risk.
- Each test exercises the path its name says and asserts observable behaviour. None is a fake success.
  - V001 spells out the literal keys `epoch`, `seq` and `line_sha256` and compares them to the ledger event's `_payload`, its `payload_sha256` and its `subject_id`. It does not call `_anchor_ids`, so a rename is caught.
  - T222 (two tests):
    - Through `_commit`: `os.unlink` is patched in `store.os` by `PENDING_NAME` suffix. The test asserts a 503 body of exactly `{"detail":"LEDGER_UNAVAILABLE","maybe":true}`, the pending file still on disk, integrity not ok, and the next write refused with `INTEGRITY_UNVERIFIED`.
    - Directly: `clear_pending` raises `StoreWriteError`.
    - The suffix patch fails only the targeted file; other unlinks reach the real function. The `ST.os` patch is process-global during the test, and monkeypatch restores it.
  - H198: `service.time` is replaced by a SimpleNamespace that has only `monotonic`. service.py uses `time` only at line 313 (`time.monotonic()`), so the namespace is enough and nothing else in the module breaks. The test asserts the ledger read count rose by exactly 1 and integrity became ok.
  - H229: `clear_pending` is made to fail, and the ledger fails every re-read after the first. The test asserts the problem text is exactly "the pending line could not be set aside" (service.py:394).
  - H399: `write_discarded` is made to fail. The test asserts the pending file bytes are unchanged (not removed before being kept) and the next write is refused.
  - H448: the first check sets the line aside; the anchor then appears and the second check appends it. The test asserts the log length rose by 1 and `_discarded` became None (service.py:401-404).
  - V045-V047, V082-V085: the tests assert `set(i) == {ok, checked_at, problem}`, `ok` False, `checked_at` a non-empty string, and the exact problem text. All three texts match service.py:205, 211 and 248.
    - The v045 test patches both `write_pending` and `clear_pending` to fail. That is the real path at lines 237-248, so it is legitimate.
  - The monkeypatches replace only fault-injection points (store I/O, `time`, the scripted ledger double). The service under test is real. The only mocks are of store methods and the scripted ledger, all test-only. There is no mock in the runtime.
- The fixture change in test_aegis_integrity_path.py now yields, then closes the service if it is still open. Correct and harmless.
- The ADR statements match the code.
  - T222 sentence: if `clear_pending` swallowed the error, `_drop_pending` would return True. `_commit` would then raise a plain LEDGER_UNAVAILABLE with no `maybe` while the file stayed on disk. This is what the ADR says; service.py:233-243 and 205-207 confirm it.
  - Open item: `_settle_pending` ignores the `problem` returned for a set-aside line (`problem, appended = self._roll_forward(aside, ...)`). When `_roll_forward` returns ("... operator must inspect the log", True), integrity is not downgraded. The own-pending and file-pending paths do answer "an operator must inspect the log" (service.py:439); the set-aside path does not. The ADR is accurate and discloses this. See M1.
  - The equivalent list drops from 21 to 17 with H198, H229, H399 and H448 removed. The arithmetic is consistent.

## 3. Full-diff sweep (grep-level over src)
- Stubs and TODO: no TODO, FIXME, NotImplemented or placeholder in src.
- Silent catch or bare pass:
  - `except BaseException` at service.py:117 and api.py:714 both clean up and re-raise.
  - The `pass` at svc_inventory.py:407 handles the inventory port's PortNotConnected and is followed by `raise Unavailable(INVENTORY_PORT_NOT_CONNECTED)`. Nothing is fetched or recorded, so the port honestly reports itself unconnected. This is not fake success.
  - The `pass` in `_a_job_ran` (service.py:303) is a deliberate no-op replay handler.
- Invented data: none found.
  - No vendor-name data. "lamar" appears only in docstring and test examples for vendor-name collision folding (svc_tenants.py:34-42).
  - Rate-card estimates come only from the face's sourced rate card (`_current_attr`). Without a sourced card the code raises `RATE_CARD_UNKNOWN`, so unknown stays UNKNOWN.
  - The $10,000 purchase and 7-day authority ceilings (policy.py:73-74) are founder-locked values cited in ADR 0022. The environment may only lower them.
  - Defaults fail closed: no ledger gives `UnconfiguredLedgerClient`, and unverified integrity refuses writes.

## 4. Carried findings
- F1-F5 are carried unchanged from the 4a0a4b4 gate (omega5/source.md). The delta touches no src, so they have not changed. They were not re-derived from scratch.
- The LOW at service.py:452 (redundant subject clause) is still present, as expected. The `subject_id != f"log:{epoch}"` check at line 451 is preceded by the epoch-set check at lines 445-447. It remains a non-blocking redundancy.
- M1 (new this pass): MEDIUM. It does not block, because the ADR discloses it as an open item for the next wave.
  - Failure class: UNSAFE_DEFAULT / FAKE_SUCCESS-adjacent.
  - Claim: a set-aside line that the ledger vouches for but that can't be applied is appended, and integrity stays ok.
  - Evidence: service.py:398-404 discards `problem` on the `appended=True` path. `_roll_forward` returns that case at service.py:439, and `_settle_pending` ignores it.
  - Reproducer: `sed -n 396,405p` and `sed -n 436,440p` of services/ooh-py/src/service.py at ce665c5.
  - Falsifier: a test showing integrity ok=False after such a line. None exists. The ADR states the case needs a ledger-valid anchor for a malformed line, which this service never writes.
  - test_h448 deliberately uses such a malformed line (tenant_created without tenant_id) and asserts neither a problem nor the integrity status. The behaviour is therefore unpinned, as the ADR says.

Denominator: 4 of 4 delta files read in full. 13 of 13 changed src files swept by grep. The full 8461-line diff was not read line by line.
