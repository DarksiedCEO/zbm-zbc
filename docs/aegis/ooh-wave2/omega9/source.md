# AEGIS SOURCE gate (Gate 1) - ooh-py delta 99569bd..91ea9a1

- candidateSha: 91ea9a1948253a3203150b38a8c2f2f839f232cd
- treeOid: 482eb8983853a8bdff118c16446474b0b16537da
- repo: DarksiedCEO/zbm-zbc, branch ooh-dept-15-wave2; frozen copy scratchpad/review-91ea9a1 (HEAD and tree identical at start and end; `git status --porcelain` empty at end; only ignored __pycache__ created by my probes)
- Author: AEGIS-SOURCE (AG-AEGIS-SOURCE v1.0.0), fresh context, read-only on the candidate
- Verdict: **SOURCE_GREEN** (scoped to the changed files; 0 CRITICAL, 0 HIGH; 1 MEDIUM and 2 LOW non-blocking findings)
- Raw outputs: scratchpad/aegis/omega9/source/ (pytest-3.12.txt, pytest-3.13.txt, probe.py, probe-out.txt, diff_names_a_card.py, diff-out-3.12.txt, diff-out-3.13.txt, old/svc_campaigns_99569bd.py)

## Denominator (inclusion rule: every file in `git diff 99569bd 91ea9a1 -- services` plus the docs named in the brief)
Required 5 code files + 4 doc artifacts = 9; inspected 9. Source files read in full: pii.py (all), the store.py and svc_campaigns.py diffs plus the surrounding functions (observed, client_approve, parse_value, file_problem, append_prepared), both test files (test names and the head of the PII file, the FIFO/symlink/device tests). Docs read: ADR 0022 sections "AEGIS Omega RED at 99569bd" and "OOH2-PII-1", OPEN.md rows OOH2-PII-1, OOH2-R8-*, Closed OOH2-RT-H1 / OOH2-MUT-T222, test-counts.md row.

## Areas
| path | class | evidence |
|---|---|---|
| services/ooh-py/src/pii.py | REAL | card_spans/luhn_card/aba_routing/_ssn_spans implemented and wired into `_spans`, used by has_personal_data and redact; 61 probe forms, below |
| services/ooh-py/src/svc_campaigns.py | REAL | names_a_card delegates to pii.card_spans; client_approve:864 screens before `_idem` and before any commit; observed():65-67 redacts vendor text |
| services/ooh-py/src/svc_inventory.py (unchanged, call site) | REAL | parse_value:73 refuses kind text/ref via pii; text then goes through observed |
| services/ooh-py/src/store.py | REAL | file_problem:246-268 opens O_RDONLY|O_NOFOLLOW|O_NONBLOCK, fstat S_ISREG, size before read; write path S_ISREG at :206 |
| tests/test_ooh2_pii1_card_ssn.py, tests/test_aegis_h1_edges.py | REAL | assert on log file, pending files and ledger events; FIFO test asserts check returns within 10 s and next write is 503 |
| docs (ADR 0022, OPEN.md, test-counts.md) | REAL (claims checked, see DOC_DRIFT) | |

## Required checks
1. names_a_card behaviourally identical to 99569bd for the funds reference: YES. The only change is the digit-run block. Old inner `break` on a joined-group rule or >19 digits becomes `return False` inside `_card_from` for that start index, and `any()` moves to the next start index, which is what the old loop did. Old text was `NFKC(value).casefold()`; new passes the same string to card_spans. The token-based first part of the function is untouched (diff shows no change there). Differential run: old function extracted from `git show 99569bd:...svc_campaigns.py` vs new, 100,012 inputs (60,000 random over digits/separators/letters/NBSP/Arabic-Indic/fullwidth digits, 40,000 Luhn-valid cards with random IIN/length/separator/trailing group/date prefix, 12 hand cases): 0 differences on Python 3.12 and 3.13 (diff-out-3.*.txt). `_luhn_card`/`_aba_routing` are aliases to the moved functions; bodies are byte-identical to the removed ones.
2. Refusal that still writes: none found. client_approve raises Invalid at svc_campaigns.py:864-865 after the read-only `_campaign` lookup and before `rk`, `_idem`, `check_kill`, `_commit`; parse_value raises before any state change. The tests re-read log file, pending files and ledger events after each refusal.
3. Fields of the finding: unit vendor_text -> `observed()` (has_personal_data then redact; sample: "pay 4242 4242 4242 4242 and SSN 123-45-6789" -> "pay [redacted] and SSN [redacted]", flag true); face text attribute -> parse_value:73 refuses; client_approval_doc_ref -> :864 refuses, and its type `Ref` (models.py:18, `[A-Za-z0-9._:-]{1,100}`) is ASCII only with no spaces.
4. Suite: `python -m pytest -q tests` in the frozen copy: **870 passed, 1 warning on 3.12 (88.24 s) and 870 passed, 1 warning on 3.13 (90.43 s)**, 0 failed, 0 skipped. Matches docs/test-counts.md `python:ooh-py | 870`.

## Findings

### S-1 MEDIUM (non-blocking) - UNSAFE_DEFAULT / bypass: grouped card number in non-ASCII-script digits is stored
- Claim: pii.py docstring and ADR 0022 "OOH2-PII-1": "a run of digit groups with any separators ... holding 13-19 digits ... Luhn"; known limits list only words and letter separators.
- Path: services/ooh-py/src/pii.py:99-100 (`_DIGIT_RUN = [0-9]+...`, `_GROUP = [0-9]+`); NFKC (pii.py `_norm`) maps fullwidth, circled, superscript and mathematical digits to ASCII but not Arabic-Indic or Devanagari digits.
- Evidence (probe-out.txt): `٤٢٤٢ ٤٢٤٢ ٤٢٤٢ ٤٢٤٢`, `٤٢٤٢-٤٢٤٢-٤٢٤٢-٤٢٤٢`, `४२४२ ४२४२ ४२४२ ४२४२` and `٤١١١ ١١١١ ١١١١ ١١١١` all PASS has_personal_data and redact is a no-op. `svc_campaigns.observed("pay ٤٢٤٢ ٤٢٤٢ ٤٢٤٢ ٤٢٤٢ now")` returns personal_data_redacted False with the digits in `text`. An unbroken run of these digits is caught by the phone rule (`\d{10,}` matches Unicode digits), so only the separated form passes. Reaches the log in unit vendor_text and in a face text attribute; NOT in client_approval_doc_ref (ASCII-only REF). The same gap exists for the funds reference (pre-existing, ASCII regex), so it is not a behaviour change.
- Reproducer: `venv3.12/bin/python -I aegis/omega9/source/probe.py` (cases arabic, dev, arabic-dash, arabic 4111) or the observed() call above from services/ooh-py/src.
- Falsifier: has_personal_data returning True for these strings, or a normalisation of Arabic-Indic/Devanagari digits before card_spans.
- Not in the ADR known limits; recommend adding it there or mapping `unicodedata.digit` first. Not fixed by me.

### S-2 LOW (non-blocking) - DOC_DRIFT: card groups shorter than three digits are not joined, undocumented in pii.py/ADR
- pii.py:131-135 (`_card_from`, `len(g) >= 3`) is explained only in a code comment. Docstring (pii.py:12-15) and ADR say "any separators between digit groups, 13-19 digits".
- Evidence: `4 2 4 2 4 2 4 2 4 2 4 2 4 2 4 2` (Luhn-valid 4242... digits), `4242 4242 4242 42 42` PASS; `4242 4242 4242 424 2` is refused.
- Reproducer: probe snippet in this session (values above through pii.has_personal_data). Falsifier: refusal of those strings. Rule is inherited unchanged from the funds screen.

### S-3 LOW (non-blocking) - DOC_DRIFT: SSN shapes outside "ddd-dd-dddd with a dash" and the stated "no separators or spaces" limit
- ADR "Known limits" and pii.py:25-27 name: nine digits with spaces or no separators and no SSN word. Observed PASS (probe-out.txt): `123.45.6789`, `123_45_6789`, `123/45/6789`, `123 - 45 - 6789`, `social-security 123 45 6789`, `SSN` + 25 characters + nine digits (beyond the 20-character window, matches the stated window), `123-45-6789-1`, `x123-45-6789` (stated: glued to a word/dash).
- The first four are stated nowhere. In client_approval_doc_ref, REF admits `.`, `_` and `:` so `123.45.6789`, `123_45_6789`, `123:45:6789` pass screening and reach the log (has_personal_data False for the first two as shown). Shapes are a strict superset of onboarding-py's (which also only handles hyphen), so parity with the stated model holds; the gap is that the ADR lists fewer limits than exist.
- Reproducer: probe.py cases ssn-dots, ssn-under, ssn-slash, ssn-ws, ssn-hy. Falsifier: any of these refused.

No finding for: dead code in the delta (the aliases `_luhn_card`/`_aba_routing` are kept deliberately and documented; grep shows tests/other modules may import them - not required to be used), FAKE_SUCCESS, HARDCODED_RESULT, MOCK_ONLY, DUPLICATE_OWNERSHIP (the card rule moved, not copied: no second copy in svc_campaigns), UNREACHABLE.

## Other checks without a finding
- store.py docstring claims: "a refused write writes nothing; a deleted log is re-created empty by the open" matches :201 (O_CREAT); write path rejects non-regular at :206 (FIFO opened O_RDWR does not block on this platform; test asserts 503). "an unforced check returns the last result while ok ... forced at most every 10 s" - claim about the caller of file_problem, not re-verified here (runtime gate territory).
- file_problem: missing file returns `differs` only when memory holds lines (:256-257); fstat then size equality precedes the read (:261), `finally: os.close(fd)`; no silent catch other than reporting the OSError class.
- Performance of the new regexes: has_personal_data on 100,000 repetitions of "1234-" took 1.1 s, on 10,000 0.11 s (linear); DOC_MAX is 4000 characters.
- Docs vs state: test-counts 870 matches the run. OPEN.md Closed rows OOH2-RT-H1 / OOH2-MUT-T222 cite omega8 evidence (not re-run here); OOH2-R8-* rows state "open until AEGIS confirms" - consistent. OOH2-PII-1 remains open in OPEN.md with purge/re-key "not built": the doc does not claim it is closed.

## NOT RUN / not inspected
- Mutation, security exploitation, reliability, runtime witnessing, provenance (peer gates).
- Other services with the shared H-1 defect (seo, sales, bizdev, service, influencer): out of scope, tracked open.
- Other free-text/ref fields outside the finding's three (e.g. `source_ref` models.py:139, Ref-typed so ASCII; contract fields) not assessed for PII.
- Python 3.14 and non-POSIX platforms (O_NOFOLLOW/O_NONBLOCK are POSIX-only; the write path already used O_NOFOLLOW).
- Real device-node (not symlink) and real FIFO behaviour on Linux; run was macOS.
- Existing log content/purge step (OOH2-PII-1 purge/re-key): not built, stated as such in docs.

blocking: false
