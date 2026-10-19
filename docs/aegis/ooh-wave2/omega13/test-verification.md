# AEGIS TEST-VERIFICATION gate 4a (omega13), services/ooh-py

- candidateSha cb708b924bb01aa5543b832573a8fa56bd041b80, treeOid eba95097622438b226169c88910c81cc9a999212 (checked: `git -C S/ooh2 rev-parse HEAD HEAD^{tree}`; S/review-cb708b9 gives the same pair). Frozen copy not modified (`git status --short` empty afterwards).
- Method: `git archive cb708b9 services/ooh-py docs/adr | tar -x` into S/aegis/omega13/test-verification/work (deleted afterwards). Python 3.12.14 (venv3.12) and 3.13.9 (venv3.13), pytest 9.1.1. Never more than 4 pytest processes at once (final `ps` count 0).
- Hashes verified before use: mutation.md faa98c2e...50b3 OK, mutation-r2.md c87a1a34...69c1 OK.
- Home path replaced by <HOME>; test data only.

## VERDICT: BLOCK (not TEST_GREEN)
Reason: the suite passes and is deterministic, but it cannot fail on mutants H041, H064 and H040, each of which lets an entity-encoded card or SSN be stored. I reproduced all four named survivors myself. Contract: a surviving mutant in the claimed-covered surface blocks discrimination (MUTANT_SURVIVED, NEGATIVE_CONTROL_WEAK).

## 1. Accounting (raw/full_3.12.txt, raw/full_3.13.txt)
| item | 3.12 | 3.13 |
|---|---|---|
| required (collected, `--collect-only`) | 1226 | 1226 (full-run ids identical) |
| executed | 1226 | 1226 |
| passed | 1226 | 1226 |
| failed / skipped / xfail / xpass / errors / cancelled | 0 / 0 / 0 / 0 / 0 / 0 | same |
Collection matches: the 1226 test ids of the two full runs are identical (`diff raw/ids_3.12.txt raw/ids_3.13.txt` empty). One warning per run (Starlette `httpx` deprecation), not a test issue.
Raw note, in full: my first full runs showed `1 failed, 1225 passed` on both Pythons. Cause is my extraction: I archived only services/ooh-py, and tests/test_env_documented.py reads ../../docs/adr/0019-*.md (StopIteration, full_*.txt line ~1251). With docs/adr extracted that file gives `2 passed` on both (raw/envdoc_*.txt). The full-suite mutant runs (docs/adr present, raw/mut_*_full.txt) show `1226 passed`. So the builder's 1226/1226 is confirmed. Finding L-1 below: that test is not hermetic to the service directory.

## 2. Determinism
Scoped 7 files: 439 tests. Run 1 and run 2 on each Python, plus a reversed file order on each: 439 passed every time (raw/scoped_*). The pytest-randomly and xdist plugins are not installed, and I did not install any, so random test-order inside files is NOT RUN. No flake observed in 3 runs per Python (6 total). Timing-sensitive tests exist (thread join(10) in test_aegis_r9 RT9-1 and test_aegis_h1_edges FIFO/shrink; tracemalloc bound at h1_edges:265) but did not flake.

## 3. Falsifiability review (in-scope files; read in full)
No tautologies of the `len(x)==len(x)` kind, no always-true asserts, no xfail. Skips: test_aegis_r9:182, h1_file_content:157, h1_edges:246 are environment-conditional (/dev/zero, root) and 0 skipped in both runs. Weak points:
- M-1 (Medium, the cause of the survivors) Oracle gap in the PII tests: `_absent()` (test_ooh2_pii1_card_ssn.py:53-72) searches stored bytes for the raw secret and for digit runs, but never html-unescapes the stored text. An entity-spelled secret in the stored line is therefore invisible to it. The backstop property tests (test_ooh2_pii1_cluster_rule.py:213, 243) use `pii._still_holds_personal_data(kept)` as the oracle, which is the function under test. The independent `number_cluster_spans` check is the same module, and the fuzz alphabets (cluster_rule.py:125, 236) hold no fullwidth `&`/`;`/`#`, and no secret is entity-encoded with non-ASCII markers. `NESTED`/`ENTITIES`/`DECODED_TWICE` are all ASCII-marker entities.
- L-2 Relative-oracle test: test_aegis_r10_screen_form.py:60 asserts `observed(text)["personal_data_redacted"] is free_text_has_personal_data(text)`. It passes if both are wrong together; absolute coverage comes from other tests, so Low.
- L-3 test_ooh2_pii1_cluster_rule.py:175 `status_code == 422` only, for refs that the comment says the reference pattern refuses before the PII screen (lines 169-176). It cannot tell the pattern refusal from the screen. Not a false green for the two refs given.
- L-4 test_aegis_r9_pii_hardening.py:59 `status_code in (200, 201)` for the replay; backed by equal unit_id and unchanged log length (60), so weak only in status.
- L-5 test_aegis_h1_edges.py:250 device case asserts only `ok is False` (any failure reason), and h1_edges:265 depends on tracemalloc peak < 16 MiB (host-dependent).
- L-6 test_aegis_r12_mutation_gaps.py:33 `names_a_card(fw) and names_a_card(ascii)`: truthiness only, adequate for the N156 claim.
Each finding states the file:line; a falsifier for M-1 is item 4 (mutants pass).

## 4. Mutation cross-check (independent re-run)
Method: scripts/test-verification-mut.py applies `orig`->`new` from S/aegis/omega13/mutation/raw/mutants13.json (exactly one match required) to pii.py in a fresh copy, then runs the five PII test files (card_ssn, r9, r10, cluster_rule, r12_mutation_gaps) on 3.12 and 3.13, plus the full suite on 3.12. A direct probe (raw/probe.txt, scripts/test-verification-probe*.py) compares pristine and mutant.
| id | change | scoped 5 files 3.12 | 3.13 | full suite 3.12 | probe: pristine vs mutant |
|---|---|---|---|---|---|
| H040 | `_forms`: undecodable returns `[t]` not None | 413 passed (survives) | 413 passed | 1226 passed | fullwidth-marker entity secrets nested 17 and 20 deep, card and SSN: pristine `[redacted]`/`number`; mutant stores the 632-1312 char text unredacted, no kind, a 60-pass decoder recovers 4111111111111111 and 123-45-6789 |
| H041 | `_forms` drops the decoded form | 413 passed (survives) | 413 passed | 1226 passed | fullwidth `&#x31;...` SSN and card: pristine redacted; mutant keeps the text, kinds [], one html.unescape gives 123-45-6789 / 4111111111111111 |
| H064 | backstop returns `kept` not `[redacted]` | 413 passed (survives) | 413 passed | 1226 passed | same two inputs: mutant keeps text (kinds ['number']), decodes to the SSN / card |
| H050 | `_still_holds`: `forms is None` is not a hit | 413 passed (survives) | 413 passed | 1226 passed | the H040 inputs: mutant raises `TypeError: 'NoneType' object is not iterable` from screen_free_text; pristine does not |
Both mutation reports' claims for these four are confirmed (survive scoped and full; real behavioural difference).
Test gap each shows: H041/H064: no test submits an entity-encoded card/SSN whose entity markers are non-ASCII (fullwidth) so the decoded-form branch of `_forms` and the backstop's return value are never needed; the existing entity tests pass through the primary redaction, and the stored-bytes check does not decode. H040: the never-settles path is tested only with the ASCII `&amp;` nest (cluster_rule.py:216-218, whole value `[redacted]`), not the `_forms`-returns-None route with a secret payload. H050: no test drives `_still_holds_personal_data` with an undecodable form, and no test asserts that screen_free_text never raises on hostile input. The founder-named cap `_MAX_DECODE` (H035-H037) was also reported as surviving in r2 and was not re-run by me.
NOT RUN: the remaining 18 survivors of mutation-r2.md (H043, H046, H049, etc.), the 5 killed-by-report mutants, and whether the HTTP route catches H050's TypeError (500 vs handled) - I probed the function, not the endpoint.

## 5. Findings
| id | grade | finding | disposition |
|---|---|---|---|
| T-1 | High | MUTANT_SURVIVED / NEGATIVE_CONTROL_WEAK: H041, H064, H040 not killed on either Python or the full suite; each stores a card/SSN (OOH2-PII-1). | BLOCK |
| T-2 | Medium | H050 crash survivor: screen_free_text can raise TypeError on hostile input and no test pins no-crash; request-level effect NOT RUN. (Mutation r2 grades it Medium.) | OPEN.md; raise to High if the route returns 500 on a store write path |
| T-3 | Medium | M-1 above: oracle gaps in `_absent` and the backstop property tests (self-referential oracle, no non-ASCII entity markers in fuzz alphabets). Root cause of T-1. | Fix with T-1 (add entity-decoding to `_absent`, fullwidth markers in alphabets) |
| L-1 | Low | test_env_documented.py needs docs/adr outside services/ooh-py; fails if the service dir is tested alone. Passes in the repo. | OPEN.md |
| L-2..L-6 | Low | weak assertions listed in section 3. | OPEN.md |
| L-7 | Low | random-order run NOT RUN (no plugin). | OPEN.md |

## Gate 4a fields
`{candidateSha: cb708b92..., gate: TEST_TRUTH, accounting:{required:1226, executed:1226, passed:1226, failed:0, skipped:0, cancelled:0, neutral:0, stale:0, notVerified:0} on each of 3.12 and 3.13, determinism:{reruns:2 (+1 reversed order) per Python, stable:true}, discrimination:{mutationProofPresent:true, survivors:22 in range H025-H070 per mutation-r2 (32 listed in its counts table; 4 reproduced by me), source: mutation-r2.md}, verdict: RED (BLOCK), blocking:true}`.
Note: mutation-r2.md's text says 22 survivors in one place and 32 in its counts table; I did not reconcile it.

Raw outputs: S/aegis/omega13/test-verification/raw/ (all under 1 MB; total 732 KB). Scripts: S/aegis/omega13/test-verification/scripts/.
